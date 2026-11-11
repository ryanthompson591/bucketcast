import io

import pytest

from bucketcast import create_app
from bucketcast.assets import AssetRepository
from bucketcast.episodes import Episode, EpisodeRepository
from bucketcast.notes import render_notes
from bucketcast.shows import Show, ShowRepository, ValidationError
from bucketcast.storage import LocalStorage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JSON = {"Accept": "application/json"}


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


@pytest.fixture
def client(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    ShowRepository(storage).save(Show(id="my-show", title="My Show"), None)
    return app.test_client()


def upload(client, filename="Robot Christmas 2.JPG", data=PNG, headers=None):
    return client.post("/admin/shows/my-show/assets", headers=headers or {},
                       data={"file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


# --- storage -----------------------------------------------------------------

def test_list_files_and_delete(storage):
    assert storage.list_files("shows/x/assets") == []
    storage.write_bytes("shows/x/assets/b.png", b"12", "image/png")
    storage.write_bytes("shows/x/assets/a.png", b"1", "image/png")
    storage.write_bytes("shows/x/assets/sub/c.png", b"123", "image/png")
    assert storage.list_files("shows/x/assets") == [("a.png", 1), ("b.png", 2)]
    storage.delete("shows/x/assets/a.png")
    storage.delete("shows/x/assets/a.png")  # deleting twice is fine
    assert storage.list_files("shows/x/assets") == [("b.png", 2)]


# --- notes rendering ---------------------------------------------------------

def url_for_asset(name):
    return f"/media/{name}"


def test_notes_basic_markdown():
    html = render_notes("Hello **world**\nsecond line\n\nNew paragraph")
    assert "<strong>world</strong>" in html
    assert "<br" in html  # single newline keeps its line break
    assert html.count("<p>") == 2


def test_notes_resolve_asset_images():
    html = render_notes("![Robots](asset:robots.jpg)", url_for_asset)
    assert 'src="/media/robots.jpg"' in html and 'alt="Robots"' in html
    assert 'loading="lazy"' in html


def test_notes_keep_external_images_and_link_bare_urls():
    html = render_notes("![x](https://example.com/a.png)\n\nhttps://youtu.be/abc", url_for_asset)
    assert 'src="https://example.com/a.png"' in html
    assert '<a href="https://youtu.be/abc">' in html


def test_notes_escape_html_and_block_script_links():
    html = render_notes('<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n'
                        '[click](javascript:alert(1))')
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html


def test_notes_unknown_or_unresolved_asset_has_empty_src(storage):
    repo = AssetRepository(storage)
    html = render_notes("![a](asset:../../show.json)", lambda n: repo.url("my-show", n))
    assert 'src=""' in html
    assert 'src=""' in render_notes("![a](asset:a.png)")  # no resolver given


def test_empty_notes():
    assert render_notes("") == "" and render_notes(None) == ""


# --- asset repository --------------------------------------------------------

def test_save_tidies_name_and_avoids_collisions(storage):
    repo = AssetRepository(storage)
    first = repo.save("my-show", "Robot Christmas 2.JPG", PNG)
    second = repo.save("my-show", "robot christmas 2.jpg", PNG + b"x")
    assert first.name == "robot-christmas-2.jpg"
    assert second.name == "robot-christmas-2-2.jpg"
    assert first.markdown == "![robot christmas 2](asset:robot-christmas-2.jpg)"
    assert [a.name for a in repo.list("my-show")] == [second.name, first.name]
    assert repo.list("other-show") == []


@pytest.mark.parametrize("filename,data", [
    ("notes.txt", PNG), ("evil.svg", PNG), ("noext", PNG), ("empty.png", b""),
    ("big.png", b"\x00" * (10 * 1024 * 1024 + 1)),
])
def test_save_rejects_bad_files(storage, filename, data):
    with pytest.raises(ValidationError):
        AssetRepository(storage).save("my-show", filename, data)
    assert storage.list_files("shows/my-show/assets") == []


def test_delete_only_touches_assets(storage):
    repo = AssetRepository(storage)
    asset = repo.save("my-show", "a.png", PNG)
    assert repo.delete("my-show", "../show.json") is False
    assert repo.delete("my-show", "missing.png") is False
    assert repo.delete("my-show", asset.name) is True
    assert repo.list("my-show") == []


# --- admin UI ----------------------------------------------------------------

def test_assets_page_empty(client):
    r = client.get("/admin/shows/my-show/assets")
    assert r.status_code == 200 and b"No assets yet" in r.data
    assert client.get("/admin/shows/nope/assets").status_code == 404


def test_upload_asset_from_page(client):
    r = upload(client)
    assert r.status_code == 302
    page = client.get("/admin/shows/my-show/assets").data
    assert b"robot-christmas-2.jpg" in page
    assert b"![robot christmas 2](asset:robot-christmas-2.jpg)" in page
    served = client.get("/_local_media/shows/my-show/assets/robot-christmas-2.jpg")
    assert served.data == PNG


def test_upload_asset_as_json_for_the_picker(client):
    r = upload(client, headers=JSON)
    assert r.status_code == 200
    assert r.json["name"] == "robot-christmas-2.jpg"
    assert r.json["markdown"] == "![robot christmas 2](asset:robot-christmas-2.jpg)"
    listing = client.get("/admin/shows/my-show/assets", headers=JSON).json
    assert [a["name"] for a in listing["assets"]] == ["robot-christmas-2.jpg"]
    assert listing["assets"][0]["url"].endswith("/shows/my-show/assets/robot-christmas-2.jpg")


def test_upload_rejects_wrong_type(client):
    r = upload(client, filename="tune.mp3", headers=JSON)
    assert r.status_code == 400 and "JPEG, PNG, GIF or WebP" in r.json["error"]
    r = upload(client, filename="tune.mp3")
    assert r.status_code == 302 and "error=" in r.headers["Location"]
    assert b"JPEG, PNG, GIF or WebP" in client.get(r.headers["Location"]).data


def test_upload_without_file(client):
    r = client.post("/admin/shows/my-show/assets", data={}, headers=JSON,
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_delete_asset(client, storage):
    upload(client)
    url = "/admin/shows/my-show/assets/robot-christmas-2.jpg/delete"
    assert client.post(url).status_code == 302
    assert AssetRepository(storage).list("my-show") == []
    assert client.post(url).status_code == 404


def test_notes_preview_endpoint(client):
    upload(client)
    r = client.post("/admin/shows/my-show/notes-preview",
                    json={"text": "**Hi**\n\n![r](asset:robot-christmas-2.jpg)\n\n<b>raw</b>"})
    html = r.json["html"]
    assert "<strong>Hi</strong>" in html
    assert 'src="/_local_media/shows/my-show/assets/robot-christmas-2.jpg"' in html
    assert "<b>" not in html
    assert client.post("/admin/shows/nope/notes-preview", json={"text": "x"}).status_code == 404


def test_episode_form_shows_rendered_notes(client, storage):
    upload(client)
    episode = Episode(title="One", audio_path="shows/my-show/audio/a.mp3",
                      description="Intro\n\n![r](asset:robot-christmas-2.jpg)")
    EpisodeRepository(storage).add("my-show", episode)
    page = client.get(f"/admin/shows/my-show/episodes/{episode.id}").data
    # The raw Markdown is in the editor and the rendered image is in the preview.
    assert b"![r](asset:robot-christmas-2.jpg)" in page
    assert b'<img src="/_local_media/shows/my-show/assets/robot-christmas-2.jpg"' in page
    assert b"Insert image" in page


def test_episode_list_links_to_assets(client):
    assert b"/admin/shows/my-show/assets" in client.get("/admin/shows/my-show/episodes").data
