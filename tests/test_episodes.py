import io

import pytest

from bucketcast import create_app
from bucketcast.episodes import (
    Episode,
    EpisodeRepository,
    audio_type_for,
    new_audio_path,
    parse_datetime,
)
from bucketcast.shows import Show, ShowRepository, ValidationError
from bucketcast.storage import Conflict, LocalStorage

MP3 = b"ID3" + b"\x00" * 2045  # 2 KiB of fake audio


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


@pytest.fixture
def client(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    ShowRepository(storage).save(Show(id="my-show", title="My Show"), None)
    return app.test_client()


def upload(client, filename="Episode One.mp3", data=MP3):
    """Do what the page's script does: ask for a URL, then PUT the file to it."""
    r = client.post("/admin/shows/my-show/episodes/upload-url",
                    json={"filename": filename, "size": len(data)})
    assert r.status_code == 200, r.data
    info = r.json
    assert client.put(info["url"], data=data, headers=info["headers"]).status_code == 200
    return info["path"]


def episode_form(path, **overrides):
    data = {"title": "Episode One", "description": "Notes", "audio_path": path,
            "published_at": "2026-11-11T18:30:00.000Z", "duration_seconds": "1234.4",
            "season": "2", "number": "7"}
    data.update(overrides)
    return data


def audio(show_id="my-show", name="a.mp3"):
    return f"shows/{show_id}/audio/{name}"


# --- helpers -----------------------------------------------------------------

def test_audio_type_for():
    assert audio_type_for("x.MP3") == "audio/mpeg"
    assert audio_type_for("x.m4a") == "audio/x-m4a"
    assert audio_type_for("x.wav") is None
    assert audio_type_for("noext") is None


def test_new_audio_path_is_safe_and_unique():
    a = new_audio_path("my-show", "My Épisode #1 (final).MP3")
    b = new_audio_path("my-show", "My Épisode #1 (final).MP3")
    assert a != b
    assert a.startswith("shows/my-show/audio/") and a.endswith("-my-pisode-1-final.mp3")


def test_parse_datetime_normalises_to_utc():
    assert parse_datetime("2026-11-11T10:30:00-08:00") == "2026-11-11T18:30:00+00:00"
    assert parse_datetime("2026-11-11T18:30") == "2026-11-11T18:30:00+00:00"
    with pytest.raises(ValueError):
        parse_datetime("yesterday")


# --- storage -----------------------------------------------------------------

def test_object_info(storage):
    assert storage.object_info(audio()) is None
    storage.write_bytes(audio(), MP3, "audio/mpeg")
    assert storage.object_info(audio())[0] == len(MP3)


# --- repository --------------------------------------------------------------

def test_guid_is_stable_uuid(storage):
    repo = EpisodeRepository(storage)
    ep = Episode(title="One", audio_path=audio())
    repo.add("my-show", ep)
    loaded = repo.get("my-show", ep.id)
    assert loaded.guid == ep.guid and len(loaded.guid) == 36
    loaded.title = "Renamed"
    repo.update("my-show", loaded, loaded.revision)
    assert repo.get("my-show", ep.id).guid == ep.guid


def test_list_newest_first_and_hides_deleted(storage):
    repo = EpisodeRepository(storage)
    old = Episode(title="Old", audio_path=audio(), published_at="2025-01-01T00:00:00+00:00")
    new = Episode(title="New", audio_path=audio(), published_at="2026-01-01T00:00:00+00:00")
    gone = Episode(title="Gone", audio_path=audio(), deleted=True)
    for ep in (old, new, gone):
        repo.add("my-show", ep)
    assert [e.title for e in repo.list("my-show")] == ["New", "Old"]
    assert len(repo.list("my-show", include_deleted=True)) == 3


def test_stale_update_conflicts(storage):
    repo = EpisodeRepository(storage)
    ep = Episode(title="One", audio_path=audio())
    repo.add("my-show", ep)
    mine, theirs = repo.get("my-show", ep.id), repo.get("my-show", ep.id)
    theirs.title = "Theirs"
    repo.update("my-show", theirs, theirs.revision)
    mine.title = "Mine"
    with pytest.raises(Conflict):
        repo.update("my-show", mine, 0)
    assert repo.get("my-show", ep.id).title == "Theirs"


def test_add_retries_when_file_changes_underneath(storage):
    repo = EpisodeRepository(storage)
    repo.add("my-show", Episode(title="First", audio_path=audio()))
    real_store, calls = repo._store, []

    def flaky_store(show_id, episodes, gen):
        calls.append(gen)
        if len(calls) == 1:  # someone else writes between our read and write
            EpisodeRepository(storage).add("my-show", Episode(title="Other", audio_path=audio()))
        real_store(show_id, episodes, gen)

    repo._store = flaky_store
    repo.add("my-show", Episode(title="Second", audio_path=audio()))
    assert sorted(e.title for e in repo.list("my-show")) == ["First", "Other", "Second"]


@pytest.mark.parametrize("fields,key", [
    ({"title": " "}, "title"),
    ({"audio_path": ""}, "audio"),
    ({"audio_path": "shows/other-show/audio/a.mp3"}, "audio"),
    ({"audio_path": "shows/my-show/audio/../show.json"}, "audio"),
    ({"published_at": "soon"}, "published_at"),
    ({"season": 0}, "season"),
    ({"number": -1}, "number"),
])
def test_episode_validation(fields, key):
    values = {"title": "T", "audio_path": audio(), **fields}
    with pytest.raises(ValidationError) as e:
        Episode(**values).validate("my-show")
    assert key in e.value.errors


# --- admin UI ----------------------------------------------------------------

def test_empty_episode_list(client):
    r = client.get("/admin/shows/my-show/episodes")
    assert r.status_code == 200 and b"No episodes yet" in r.data


def test_episode_pages_404_for_unknown_show(client):
    assert client.get("/admin/shows/nope/episodes").status_code == 404
    assert client.get("/admin/shows/nope/episodes/new").status_code == 404
    r = client.post("/admin/shows/nope/episodes/upload-url", json={"filename": "a.mp3"})
    assert r.status_code == 404


def test_upload_url_rejects_non_audio(client):
    r = client.post("/admin/shows/my-show/episodes/upload-url", json={"filename": "virus.exe"})
    assert r.status_code == 400 and "MP3 or M4A" in r.json["error"]


def test_upload_url_rejects_huge_file(client):
    r = client.post("/admin/shows/my-show/episodes/upload-url",
                    json={"filename": "a.mp3", "size": 3 * 1024**3})
    assert r.status_code == 400


def test_upload_and_publish_episode(client, storage):
    path = upload(client)
    assert path.startswith("shows/my-show/audio/") and path.endswith("-episode-one.mp3")

    r = client.post("/admin/shows/my-show/episodes/new", data=episode_form(path, explicit="on"))
    assert r.status_code == 302

    (ep,) = EpisodeRepository(storage).list("my-show")
    assert ep.title == "Episode One"
    assert ep.audio_path == path
    assert ep.audio_bytes == len(MP3)  # measured from the stored file
    assert ep.audio_type == "audio/mpeg"
    assert ep.duration_seconds == 1234
    assert ep.published_at == "2026-11-11T18:30:00+00:00"
    assert (ep.season, ep.number, ep.explicit) == (2, 7, True)

    page = client.get("/admin/shows/my-show/episodes").data
    assert b"Episode One" in page and b"20:34" in page and b"Season 2" in page
    assert client.get("/_local_media/" + path).data == MP3


def test_upload_larger_than_artwork_limit(client, storage):
    big = b"\x00" * (13 * 1024 * 1024)  # over the app's 12 MB form limit
    path = upload(client, data=big)
    assert storage.object_info(path)[0] == len(big)


def test_publish_requires_finished_upload(client, storage):
    r = client.post("/admin/shows/my-show/episodes/new",
                    data=episode_form("shows/my-show/audio/never-uploaded.mp3"))
    assert r.status_code == 200 and b"upload didn&#39;t finish" in r.data
    assert EpisodeRepository(storage).list("my-show") == []


def test_publish_rejects_audio_from_another_show(client, storage):
    storage.write_bytes("shows/other/audio/a.mp3", MP3, "audio/mpeg")
    r = client.post("/admin/shows/my-show/episodes/new",
                    data=episode_form("shows/other/audio/a.mp3"))
    assert r.status_code == 200
    assert EpisodeRepository(storage).list("my-show") == []


def test_publish_without_audio(client):
    r = client.post("/admin/shows/my-show/episodes/new", data=episode_form(""))
    assert r.status_code == 200 and b"Choose an audio file" in r.data


def test_bad_numbers_are_reported(client):
    path = upload(client)
    r = client.post("/admin/shows/my-show/episodes/new", data=episode_form(path, season="two"))
    assert r.status_code == 200 and b"Must be a whole number" in r.data


def test_edit_episode_keeps_audio_and_guid(client, storage):
    path = upload(client)
    client.post("/admin/shows/my-show/episodes/new", data=episode_form(path))
    repo = EpisodeRepository(storage)
    (ep,) = repo.list("my-show")

    r = client.post(f"/admin/shows/my-show/episodes/{ep.id}", data={
        "revision": str(ep.revision), "title": "Renamed", "audio_path": path,
        "published_at": ep.published_at, "duration_seconds": "1234", "season": "", "number": "",
    })
    assert r.status_code == 302
    after = repo.get("my-show", ep.id)
    assert (after.title, after.season, after.number) == ("Renamed", None, None)
    assert (after.guid, after.audio_path, after.audio_bytes) == (ep.guid, path, len(MP3))


def test_replace_audio(client, storage):
    path = upload(client)
    client.post("/admin/shows/my-show/episodes/new", data=episode_form(path))
    repo = EpisodeRepository(storage)
    (ep,) = repo.list("my-show")

    new_path = upload(client, filename="fixed.m4a", data=MP3 * 2)
    r = client.post(f"/admin/shows/my-show/episodes/{ep.id}",
                    data=episode_form(new_path, revision=str(ep.revision)))
    assert r.status_code == 302
    after = repo.get("my-show", ep.id)
    assert (after.audio_path, after.audio_bytes, after.audio_type) == (
        new_path, len(MP3) * 2, "audio/x-m4a")


def test_edit_conflict(client, storage):
    path = upload(client)
    client.post("/admin/shows/my-show/episodes/new", data=episode_form(path))
    repo = EpisodeRepository(storage)
    (ep,) = repo.list("my-show")
    repo.update("my-show", repo.get("my-show", ep.id), ep.revision)  # someone else saves

    r = client.post(f"/admin/shows/my-show/episodes/{ep.id}",
                    data=episode_form(path, revision=str(ep.revision), title="Mine"))
    assert b"changed somewhere else" in r.data
    assert repo.get("my-show", ep.id).title == "Episode One"


def test_delete_episode(client, storage):
    path = upload(client)
    client.post("/admin/shows/my-show/episodes/new", data=episode_form(path))
    (ep,) = EpisodeRepository(storage).list("my-show")

    assert client.post(f"/admin/shows/my-show/episodes/{ep.id}/delete").status_code == 302
    assert b"Episode One" not in client.get("/admin/shows/my-show/episodes").data
    assert client.get(f"/admin/shows/my-show/episodes/{ep.id}").status_code == 404
    assert storage.object_info(path) is not None  # the audio is kept


# --- dev-only upload route ---------------------------------------------------

def test_local_upload_only_into_audio_folders(client, storage):
    r = client.put("/_local_upload/shows/my-show/show.json", data=b"{}")
    assert r.status_code == 403
    assert ShowRepository(storage).get("my-show")[0].title == "My Show"


def test_local_upload_requires_admin(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": ""},
                     storage=storage)
    r = app.test_client().put("/_local_upload/shows/my-show/audio/a.mp3", data=MP3)
    assert r.status_code == 403
    assert storage.object_info("shows/my-show/audio/a.mp3") is None


def test_show_pages_link_to_episodes(client):
    assert b"/admin/shows/my-show/episodes" in client.get("/admin/").data
    assert b"/admin/shows/my-show/episodes" in client.get("/admin/shows/my-show").data


# --- episode thumbnail -------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def publish(client, storage, **form):
    path = upload(client)
    r = client.post("/admin/shows/my-show/episodes/new", data=episode_form(path, **form),
                    content_type="multipart/form-data")
    return r, EpisodeRepository(storage).list("my-show")


def test_episode_without_thumbnail(client, storage):
    r, (ep,) = publish(client, storage)
    assert r.status_code == 302 and ep.artwork_path == ""


def test_episode_thumbnail_upload(client, storage):
    r, (ep,) = publish(client, storage, artwork=(io.BytesIO(PNG), "thumb.png", "image/png"))
    assert r.status_code == 302
    assert ep.artwork_path.startswith(f"shows/my-show/artwork/episodes/{ep.id}-")
    assert ep.artwork_path.endswith(".png")
    assert client.get("/_local_media/" + ep.artwork_path).data == PNG
    assert ep.artwork_path.encode() in client.get("/admin/shows/my-show/episodes").data


def test_episode_thumbnail_wrong_type(client, storage):
    r, episodes = publish(client, storage, artwork=(io.BytesIO(b"GIF89a"), "x.gif", "image/gif"))
    assert r.status_code == 200 and b"JPEG or PNG" in r.data
    assert episodes == []


def test_invalid_episode_writes_no_thumbnail(client, storage):
    r = client.post("/admin/shows/my-show/episodes/new", content_type="multipart/form-data",
                    data=episode_form("", artwork=(io.BytesIO(PNG), "thumb.png", "image/png")))
    assert r.status_code == 200
    assert storage.list_dirs("shows/my-show/artwork") == []


def test_edit_keeps_replaces_and_removes_thumbnail(client, storage):
    _, (ep,) = publish(client, storage, artwork=(io.BytesIO(PNG), "thumb.png", "image/png"))
    repo = EpisodeRepository(storage)
    url = f"/admin/shows/my-show/episodes/{ep.id}"

    def save(**form):
        current = repo.get("my-show", ep.id)
        r = client.post(url, content_type="multipart/form-data",
                        data=episode_form(current.audio_path, revision=str(current.revision),
                                          **form))
        assert r.status_code == 302, r.data
        return repo.get("my-show", ep.id)

    assert save(title="Renamed").artwork_path == ep.artwork_path  # untouched

    jpeg = b"\xff\xd8\xff" + b"\x00" * 32
    replaced = save(artwork=(io.BytesIO(jpeg), "new.jpg", "image/jpeg"))
    assert replaced.artwork_path.endswith(".jpg")
    assert client.get("/_local_media/" + replaced.artwork_path).data == jpeg

    assert save(remove_artwork="on").artwork_path == ""


def test_list_falls_back_to_show_artwork(client, storage):
    shows = ShowRepository(storage)
    show, gen = shows.get("my-show")
    show.artwork_path = shows.save_artwork(show, PNG, "image/png")
    shows.save(show, gen)
    publish(client, storage)
    assert show.artwork_path.encode() in client.get("/admin/shows/my-show/episodes").data
