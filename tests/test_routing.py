import pytest

from bucketcast import create_app
from bucketcast import public as public_module
from bucketcast.episodes import Episode, EpisodeRepository
from bucketcast.public import normalize_host
from bucketcast.shows import Show, ShowRepository
from bucketcast.storage import LocalStorage

EP = "0a85c0a16b3d406490a8d9298cb335b5"


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


@pytest.fixture
def client(storage, tmp_path):
    from bucketcast.stats import LocalStats

    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage,
                     stats=LocalStats(tmp_path / "stats"))
    c = app.test_client()
    for show_id, title, domain in [("scifi", "Sci-Fi Musicals", "scifimusicalpodcast.com"),
                                   ("cooking", "Cooking Hour", "cooking.example.org"),
                                   ("nodomain", "No Domain", "")]:
        c.post("/admin/shows/new", data={"id": show_id, "title": title, "domain": domain,
                                         "category": "Technology", "language": "en",
                                         "episodes_per_page": "2"})
    return c


def get(client, path, host):
    return client.get(path, headers={"Host": host})


@pytest.mark.parametrize("host,expected", [
    ("scifimusicalpodcast.com", "scifimusicalpodcast.com"),
    ("WWW.SciFiMusicalPodcast.com", "scifimusicalpodcast.com"),
    ("scifimusicalpodcast.com:8080", "scifimusicalpodcast.com"),
    ("scifimusicalpodcast.com.", "scifimusicalpodcast.com"),
    ("localhost:5000", "localhost"),
    ("", ""),
])
def test_normalize_host(host, expected):
    assert normalize_host(host) == expected


def test_each_domain_gets_its_own_show(client):
    scifi = get(client, "/", "scifimusicalpodcast.com")
    cooking = get(client, "/", "cooking.example.org")
    assert scifi.status_code == 200 and b"Sci-Fi Musicals" in scifi.data
    assert cooking.status_code == 200 and b"Cooking Hour" in cooking.data
    assert b"Cooking Hour" not in scifi.data
    assert scifi.content_type == "text/html; charset=utf-8"
    assert scifi.headers["Cache-Control"] == "public, max-age=300"


def test_www_and_port_reach_the_same_show(client):
    for host in ("www.scifimusicalpodcast.com", "scifimusicalpodcast.com:443",
                 "SCIFIMUSICALPODCAST.COM"):
        assert b"Sci-Fi Musicals" in get(client, "/", host).data
    assert b"Sci-Fi Musicals" in get(client, "/index.html", "scifimusicalpodcast.com").data


def test_feed_is_served_at_the_domain(client):
    r = get(client, "/feed.xml", "scifimusicalpodcast.com")
    assert r.status_code == 200
    assert r.content_type == "application/rss+xml; charset=utf-8"
    assert b"<title>Sci-Fi Musicals</title>" in r.data
    # The feed names its own public address.
    assert b'href="https://scifimusicalpodcast.com/feed.xml" rel="self"' in r.data


def test_unknown_host_gets_a_friendly_404(client):
    for path in ("/", "/feed.xml", "/page/2/"):
        r = get(client, path, "someone-else.example.com")
        assert r.status_code == 404 and b"No podcast here" in r.data
    # A show without a domain is not reachable by host at all.
    assert get(client, "/", "nodomain").status_code == 404
    assert get(client, "/", "localhost").status_code == 404


def test_paginated_pages_are_served(client, storage):
    repo = EpisodeRepository(storage)
    for n in range(1, 6):
        repo.add("scifi", Episode(title=f"Ep {n}", audio_path=f"shows/scifi/audio/{n}.mp3",
                                  published_at=f"2026-01-0{n}T00:00:00+00:00"))
    client.post("/admin/shows/scifi/feed/rebuild")

    host = "scifimusicalpodcast.com"
    first = get(client, "/", host).data.decode()
    assert "Ep 5" in first and 'href="page/2/index.html"' in first
    for path in ("/page/2/", "/page/2/index.html"):
        assert "Ep 3" in get(client, path, host).data.decode()
    assert "Ep 1" in get(client, "/page/3/", host).data.decode()
    assert get(client, "/page/4/", host).status_code == 404
    assert get(client, "/page/0/", host).status_code == 404
    assert get(client, "/page/abc/", host).status_code == 404
    # Without the trailing slash Flask redirects to the canonical form.
    assert get(client, "/page/2", host).status_code == 308


def test_download_redirect_works_on_the_show_domain(client, storage):
    EpisodeRepository(storage).add("scifi", Episode(
        id=EP, title="One", audio_path="shows/scifi/audio/one.mp3"))
    r = get(client, f"/e/scifi/{EP}.mp3", "scifimusicalpodcast.com")
    assert r.status_code == 302 and r.headers["Location"].endswith("/audio/one.mp3")


def test_public_routes_need_no_login(storage, tmp_path):
    ShowRepository(storage).save(Show(id="scifi", title="Sci-Fi", domain="pod.example.com"), None)
    setup = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    setup.test_client().post("/admin/shows/scifi/feed/rebuild")

    locked = create_app({"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": ""},
                        storage=storage).test_client()
    assert locked.get("/admin/", headers={"Host": "pod.example.com"}).status_code == 403
    assert locked.get("/", headers={"Host": "pod.example.com"}).status_code == 200
    assert locked.get("/feed.xml", headers={"Host": "pod.example.com"}).status_code == 200


def test_domain_changes_are_picked_up_after_the_cache_expires(client, storage, monkeypatch):
    assert get(client, "/", "new.example.net").status_code == 404
    shows = ShowRepository(storage)
    show, gen = shows.get("cooking")
    show.domain = "new.example.net"
    shows.save(show, gen)

    # Still cached: the old mapping answers until the cache is refreshed.
    assert get(client, "/", "new.example.net").status_code == 404
    monkeypatch.setattr(public_module, "DOMAIN_CACHE_SECONDS", 0)
    assert b"Cooking Hour" in get(client, "/", "new.example.net").data
    assert get(client, "/", "cooking.example.org").status_code == 404


def test_deleted_show_stops_being_served(client, monkeypatch):
    monkeypatch.setattr(public_module, "DOMAIN_CACHE_SECONDS", 0)
    assert get(client, "/", "cooking.example.org").status_code == 200
    client.post("/admin/shows/cooking/delete")
    assert get(client, "/", "cooking.example.org").status_code == 404
    assert get(client, "/feed.xml", "cooking.example.org").status_code == 404
