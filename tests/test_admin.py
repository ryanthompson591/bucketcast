import io

import pytest

from bucketcast import create_app
from bucketcast.shows import Show, ShowRepository, ValidationError
from bucketcast.storage import Conflict, LocalStorage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


@pytest.fixture
def client(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    return app.test_client()


def new_show(client, **overrides):
    data = {"id": "my-show", "title": "My Show", "category": "Technology", "language": "en"}
    data.update(overrides)
    return client.post("/admin/shows/new", data=data, content_type="multipart/form-data")


# --- storage ---------------------------------------------------------------

def test_write_json_create_only(storage):
    storage.write_json("a.json", {"x": 1}, if_generation_match=0)
    with pytest.raises(Conflict):
        storage.write_json("a.json", {"x": 2}, if_generation_match=0)


def test_write_json_stale_generation(storage):
    gen = storage.write_json("a.json", {"x": 1}, if_generation_match=0)
    storage.write_json("a.json", {"x": 2}, if_generation_match=gen)
    with pytest.raises(Conflict):
        storage.write_json("a.json", {"x": 3}, if_generation_match=gen)
    assert storage.read_json("a.json")[0] == {"x": 2}


def test_local_storage_rejects_path_escape(storage):
    with pytest.raises(ValueError):
        storage.read_json("../outside.json")


# --- repository ------------------------------------------------------------

def test_repository_roundtrip(storage):
    repo = ShowRepository(storage)
    repo.save(Show(id="one", title="One", domain="HTTPS://One.Example.com/"), None)
    show, gen = repo.get("one")
    assert show.title == "One"
    assert show.domain == "one.example.com"
    assert gen is not None
    assert [s.id for s in repo.list()] == ["one"]


def test_duplicate_domain_rejected(storage):
    repo = ShowRepository(storage)
    repo.save(Show(id="one", title="One", domain="pod.example.com"), None)
    with pytest.raises(ValidationError) as e:
        repo.save(Show(id="two", title="Two", domain="pod.example.com"), None)
    assert "domain" in e.value.errors


@pytest.mark.parametrize("bad_id", ["", "-x", "x-", "Has Caps", "a/b", "a" * 64])
def test_invalid_ids(bad_id):
    with pytest.raises(ValidationError):
        Show(id=bad_id, title="T").validate()


# --- admin UI --------------------------------------------------------------

def test_admin_locked_without_iap_config(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": ""},
                     storage=storage)
    assert app.test_client().get("/admin/").status_code == 403


def test_admin_requires_iap_header(storage):
    cfg = {"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": "/projects/1/apps/x",
           "ADMIN_EMAILS": ["owner@example.com"]}
    app = create_app(cfg,
                     storage=storage)
    assert app.test_client().get("/admin/").status_code == 401


def test_empty_list(client):
    r = client.get("/admin/")
    assert r.status_code == 200
    assert b"No shows yet" in r.data


def test_create_and_list_show(client):
    r = new_show(client, domain="mypodcast.com", explicit="on")
    assert r.status_code == 302
    r = client.get("/admin/")
    assert b"My Show" in r.data and b"mypodcast.com" in r.data and b"Explicit" in r.data


def test_create_validation_errors(client):
    r = new_show(client, id="Bad ID", title="", owner_email="nope")
    assert r.status_code == 200
    assert b"Please fix the highlighted fields" in r.data
    assert b"Title is required" in r.data


def test_duplicate_id(client):
    new_show(client)
    r = new_show(client, title="Another")
    assert b"already exists" in r.data


def test_edit_show(client, storage):
    new_show(client)
    gen = ShowRepository(storage).get("my-show")[1]
    r = client.post("/admin/shows/my-show", data={
        "generation": str(gen), "title": "Renamed", "category": "Comedy", "language": "en-US",
    })
    assert r.status_code == 302
    show, _ = ShowRepository(storage).get("my-show")
    assert (show.title, show.category, show.language) == ("Renamed", "Comedy", "en-US")


def test_edit_conflict(client, storage):
    new_show(client)
    repo = ShowRepository(storage)
    show, stale = repo.get("my-show")
    repo.save(show, stale)  # someone else saves first
    r = client.post("/admin/shows/my-show", data={
        "generation": str(stale), "title": "Mine", "category": "Comedy", "language": "en",
    })
    assert b"changed somewhere else" in r.data


def test_artwork_upload(client, storage):
    r = new_show(client, artwork=(io.BytesIO(PNG), "cover.png", "image/png"))
    assert r.status_code == 302
    show, _ = ShowRepository(storage).get("my-show")
    assert show.artwork_path.startswith("shows/my-show/artwork/cover-")
    assert client.get("/_local_media/" + show.artwork_path).data == PNG


def test_artwork_wrong_type(client):
    r = new_show(client, artwork=(io.BytesIO(b"GIF89a"), "x.gif", "image/gif"))
    assert b"JPEG or PNG" in r.data


def test_delete_show_frees_domain(client, storage):
    new_show(client, domain="mypodcast.com")
    r = client.post("/admin/shows/my-show/delete")
    assert r.status_code == 302
    assert b"My Show" not in client.get("/admin/").data
    assert client.get("/admin/shows/my-show").status_code == 404
    r = new_show(client, id="other", title="Other", domain="mypodcast.com")
    assert r.status_code == 302


def test_cross_site_post_refused(client):
    r = client.post("/admin/shows/new", data={"id": "x", "title": "X"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_healthz(client):
    assert client.get("/healthz").json == {"ok": True}
