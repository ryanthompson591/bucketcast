"""The two-bucket design: only what listeners download is public.

These tests run the whole app on a SplitStorage made of two local folders,
then check what ended up in each, and that the public service never writes.
"""

import io

import pytest

from bucketcast import create_app
from bucketcast import public as public_module
from bucketcast.episodes import EpisodeRepository
from bucketcast.stats import SALT_PATH, LocalStats, load_salt
from bucketcast.storage import (
    LocalStorage,
    SplitStorage,
    is_public_path,
    storage_from_config,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3" + b"\x00" * 2045
HOST = {"Host": "pod.example.com"}


@pytest.fixture
def buckets(tmp_path):
    return LocalStorage(tmp_path / "public"), LocalStorage(tmp_path / "private")


@pytest.fixture
def storage(buckets):
    return SplitStorage(*buckets)


def all_files(store: LocalStorage) -> set[str]:
    return {p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()}


# --- which path goes where -----------------------------------------------------

@pytest.mark.parametrize("path", [
    "shows/my-show/audio/abc-episode.mp3",
    "shows/my-show/artwork/cover-2026.png",
    "shows/my-show/artwork/episodes/e1.jpg",
    "shows/my-show/assets/robots.jpg",
    "shows/my-show/feed.xml",
    "shows/my-show/index.html",
    "shows/my-show/page/2/index.html",
])
def test_public_paths(path):
    assert is_public_path(path)


@pytest.mark.parametrize("path", [
    "shows/my-show/show.json",
    "shows/my-show/episodes.json",
    "config/stats-salt.json",
    "shows/my-show/backups/show.json.2026",
    "shows/my-show/something-new.json",   # unknown files default to private
    "shows/my-show/feed.xml.bak",
    "shows/my-show/audio",                # the folder name itself, not a file in it
    "audio/x.mp3",                        # looks public, but not under a show
    "shows/feed.xml",
    "",
])
def test_private_paths(path):
    assert not is_public_path(path)


def test_prefixes_route_like_their_contents():
    assert is_public_path("shows/my-show/assets", is_prefix=True)
    assert is_public_path("shows/my-show/page/", is_prefix=True)
    assert not is_public_path("shows", is_prefix=True)          # the list of shows
    assert not is_public_path("shows/my-show", is_prefix=True)
    assert not is_public_path("config", is_prefix=True)


# --- the store itself ----------------------------------------------------------

def test_reads_and_writes_land_in_the_right_bucket(storage, buckets):
    public, private = buckets
    storage.write_json("shows/s/show.json", {"a": 1}, if_generation_match=0)
    storage.write_bytes("shows/s/artwork/c.png", PNG, "image/png", public=True)
    storage.write_file("shows/s/audio/a.mp3", io.BytesIO(MP3), "audio/mpeg")

    assert all_files(private) == {"shows/s/show.json"}
    assert all_files(public) == {"shows/s/artwork/c.png", "shows/s/audio/a.mp3"}
    assert storage.read_json("shows/s/show.json")[0] == {"a": 1}
    assert storage.read_bytes("shows/s/audio/a.mp3") == MP3
    assert storage.object_info("shows/s/artwork/c.png")[0] == len(PNG)
    assert storage.list_dirs("shows") == ["s"]                       # from the private side
    assert storage.list_files("shows/s/audio") == [("a.mp3", len(MP3))]

    storage.delete("shows/s/audio/a.mp3")
    assert all_files(public) == {"shows/s/artwork/c.png"}


def test_private_paths_can_never_be_published(storage, buckets):
    public, _ = buckets
    with pytest.raises(ValueError, match="private path"):
        storage.write_bytes("shows/s/show.json", b"{}", "application/json", public=True)
    with pytest.raises(ValueError, match="no public address"):
        storage.public_url("shows/s/episodes.json")
    with pytest.raises(ValueError, match="no public address"):
        storage.public_url(SALT_PATH)
    with pytest.raises(ValueError, match="only accepted for public media"):
        storage.signed_upload_url("shows/s/show.json", "application/json")
    assert all_files(public) == set()
    assert storage.public_url("shows/s/audio/a.mp3").endswith("/shows/s/audio/a.mp3")


# --- configuration -------------------------------------------------------------

@pytest.mark.parametrize("config,message", [
    ({"BUCKET": "old-single-bucket"}, "no longer used"),
    ({"PUBLIC_BUCKET": "pub"}, "Set both"),
    ({"PRIVATE_BUCKET": "priv"}, "Set both"),
    ({"PUBLIC_BUCKET": "same", "PRIVATE_BUCKET": "same"}, "must be different"),
])
def test_bucket_settings_are_checked(config, message):
    with pytest.raises(ValueError, match=message):
        storage_from_config(config)


def test_no_buckets_means_local_storage(tmp_path):
    store = storage_from_config({"LOCAL_STORAGE_DIR": str(tmp_path / "b")})
    assert isinstance(store, LocalStorage)


# --- the whole app on two buckets ----------------------------------------------

def fill(storage, tmp_path):
    """Use the admin as a person would: a show, artwork, an asset, an episode."""
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True, "ROLE": "admin",
                      "PUBLIC_BASE_URL": "https://pod.example.com"},
                     storage=storage, stats=LocalStats(tmp_path / "stats"))
    c = app.test_client()
    r = c.post("/admin/shows/new", content_type="multipart/form-data", data={
        "id": "my-show", "title": "My Show", "category": "Technology", "language": "en",
        "domain": "pod.example.com", "track_downloads": "on", "episodes_per_page": "1",
        "artwork": (io.BytesIO(PNG), "cover.png", "image/png")})
    assert r.status_code == 302, r.data
    c.post("/admin/shows/my-show/assets", content_type="multipart/form-data",
           data={"file": (io.BytesIO(PNG), "robots.png")})
    for n in (1, 2):
        path = f"shows/my-show/audio/ep{n}.mp3"
        storage.write_file(path, io.BytesIO(MP3), "audio/mpeg")
        r = c.post("/admin/shows/my-show/episodes/new", content_type="multipart/form-data",
                   data={"title": f"Episode {n}", "audio_path": path,
                         "published_at": f"2026-01-0{n}T00:00:00Z",
                         "description": "![r](asset:robots.png)",
                         "artwork": (io.BytesIO(PNG), "thumb.png", "image/png")})
        assert r.status_code == 302, r.data
    return c


def test_only_listener_files_reach_the_public_bucket(storage, buckets, tmp_path):
    public, private = buckets
    fill(storage, tmp_path)

    assert all_files(private) == {
        "shows/my-show/show.json", "shows/my-show/episodes.json", SALT_PATH}

    published = all_files(public)
    assert {"shows/my-show/feed.xml", "shows/my-show/index.html",
            "shows/my-show/page/2/index.html", "shows/my-show/assets/robots.png",
            "shows/my-show/audio/ep1.mp3", "shows/my-show/audio/ep2.mp3"} <= published
    # Nothing that isn't a listener file, whatever it is called.
    assert all(is_public_path(p) for p in published)
    assert not any(p.endswith(".json") for p in published)

    # And the published files don't quote the private ones.
    feed = public.read_bytes("shows/my-show/feed.xml").decode()
    assert "show.json" not in feed and "episodes.json" not in feed and "stats-salt" not in feed


class ReadOnly:
    """A store that passes reads through and fails the test on any change."""

    READS = ("read_json", "read_bytes", "object_info", "list_dirs", "list_files", "public_url")

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in self.READS:
            return getattr(self._inner, name)
        raise AssertionError(f"the public service tried to call storage.{name}")


def test_public_service_never_writes(storage, tmp_path, monkeypatch, caplog):
    fill(storage, tmp_path)
    (episode, _) = EpisodeRepository(storage).list("my-show")
    monkeypatch.setattr(public_module, "DOMAIN_CACHE_SECONDS", 0)

    app = create_app({"TESTING": True, "ROLE": "public"}, storage=ReadOnly(storage),
                     stats=LocalStats(tmp_path / "stats"))
    c = app.test_client()
    assert c.get("/", headers=HOST).status_code == 200
    assert c.get("/page/2/", headers=HOST).status_code == 200
    assert c.get("/feed.xml", headers=HOST).status_code == 200
    assert c.get("/", headers={"Host": "nobody.example.org"}).status_code == 404
    r = c.get(f"/e/my-show/{episode.id}.mp3", headers={"User-Agent": "Overcast/3.0"})
    assert r.status_code == 302
    assert c.get("/e/my-show/" + "0" * 32 + ".mp3").status_code == 404
    assert c.get("/healthz").status_code == 200


# --- the stats salt ------------------------------------------------------------

def test_public_service_uses_the_salt_but_never_creates_it(buckets, tmp_path):
    public, private = buckets
    storage = SplitStorage(public, private)
    assert load_salt(storage, create=False) is None
    assert all_files(private) == set()

    salt = load_salt(storage, create=True)
    assert load_salt(storage, create=False) == salt
    assert all_files(private) == {SALT_PATH} and all_files(public) == set()


def test_downloads_are_counted_before_the_salt_exists(storage, tmp_path):
    """A public service started before the admin was ever opened still works."""
    from bucketcast.episodes import Episode
    from bucketcast.shows import Show, ShowRepository

    ShowRepository(storage).save(Show(id="s", title="S"), None)
    EpisodeRepository(storage).add("s", Episode(
        id="a" * 32, title="E", audio_path="shows/s/audio/a.mp3"))
    stats = LocalStats(tmp_path / "stats")
    app = create_app({"TESTING": True, "ROLE": "public"}, storage=ReadOnly(storage), stats=stats)
    c = app.test_client()
    url = "/e/s/" + "a" * 32 + ".mp3"
    headers = {"User-Agent": "Overcast/3.0", "X-Forwarded-For": "203.0.113.7"}
    assert c.get(url, headers=headers).status_code == 302
    assert c.get(url, headers=headers).status_code == 302
    assert sum(r.downloads for r in stats.rows("s", None)) == 1  # same listener, counted once

    # Once the admin has created the real salt, the public service picks it up.
    real = load_salt(storage, create=True)
    c.get(url, headers=headers)
    assert app.extensions["bucketcast.stats_salt"] == real


def test_opening_the_admin_creates_the_salt(storage, buckets, tmp_path):
    _, private = buckets
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True, "ROLE": "admin"},
                     storage=storage)
    assert SALT_PATH not in all_files(private)
    assert app.test_client().get("/admin/").status_code == 200
    assert SALT_PATH in all_files(private)


def test_a_refused_visitor_does_not_create_the_salt(storage, buckets):
    _, private = buckets
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": "",
                      "ROLE": "admin"}, storage=storage)
    assert app.test_client().get("/admin/").status_code == 403
    assert all_files(private) == set()
