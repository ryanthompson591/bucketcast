import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bucketcast import create_app
from bucketcast.assets import AssetRepository
from bucketcast.episodes import Episode, EpisodeRepository
from bucketcast.feed import FeedBuilder, feed_path, feed_problems, podcast_guid
from bucketcast.shows import Show, ShowRepository, ValidationError
from bucketcast.storage import LocalStorage

GOLDEN = Path(__file__).parent / "golden" / "feed.xml"
NOW = datetime(2026, 11, 11, 20, 0, 0, tzinfo=UTC)
BASE = "https://media.example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3" + b"\x00" * 2045

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


def sample_show(**overrides) -> Show:
    values = dict(
        id="scifi", title="Sci-Fi Musicals & More", description="Episodic <musicals> in space.",
        author="Ryan", owner_name="Ryan T", owner_email="owner@example.com",
        category="Fiction", language="en", explicit=False, domain="",
        artwork_path="shows/scifi/artwork/cover-20260101000000.png",
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
    )
    values.update(overrides)
    return Show(**values)


def sample_episodes() -> list[Episode]:
    stamps = dict(created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
    return [
        Episode(
            id="0a85c0a16b3d406490a8d9298cb335b5", title="Christmas in Robot World",
            description="![Robots](asset:robots.jpg)\n\nA boy & his **robot** parents.\n"
                        "Watch: https://youtu.be/abc",
            published_at="2026-11-01T12:00:00+00:00", season=2, number=7, explicit=False,
            audio_path="shows/scifi/audio/aaaa-christmas.mp3", audio_bytes=15728640,
            audio_type="audio/mpeg", duration_seconds=1834,
            artwork_path="shows/scifi/artwork/episodes/0a85-20261101.jpg", **stamps),
        Episode(
            id="11111111111141118111111111111111", guid="libsyn-legacy-guid-123",
            title="Ragna Rock", description="", published_at="2025-11-15T08:00:00+00:00",
            explicit=True, audio_path="shows/scifi/audio/bbbb-ragna.m4a", audio_bytes=1048576,
            audio_type="audio/x-m4a", duration_seconds=0, **stamps),
        Episode(
            id="22222222222242228222222222222222", title="Not yet published",
            published_at="2027-01-01T00:00:00+00:00",
            audio_path="shows/scifi/audio/cccc-future.mp3", audio_bytes=10, **stamps),
        Episode(
            id="33333333333343338333333333333333", title="Deleted episode", deleted=True,
            published_at="2026-10-01T00:00:00+00:00",
            audio_path="shows/scifi/audio/dddd-gone.mp3", audio_bytes=10, **stamps),
    ]


def build(storage, show=None, episodes=None) -> bytes:
    show = show or sample_show()
    episodes = sample_episodes() if episodes is None else episodes
    return FeedBuilder(storage, base_url=BASE).build(show, episodes, NOW)


def parse(xml: bytes):
    return ET.fromstring(xml).find("channel")


# --- golden file -------------------------------------------------------------

def test_feed_matches_golden_file(storage):
    """The whole feed, byte for byte. Regenerate with UPDATE_GOLDEN=1 after a deliberate change."""
    xml = build(storage)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_bytes(xml)
    assert xml.decode("utf-8") == GOLDEN.read_bytes().decode("utf-8").replace("\r\n", "\n")


# --- channel -----------------------------------------------------------------

def test_channel_has_what_apple_requires(storage):
    ch = parse(build(storage))
    assert ch.findtext("title") == "Sci-Fi Musicals & More"
    assert ch.findtext("description") == "Episodic <musicals> in space."
    assert ch.findtext("language") == "en"
    assert ch.findtext("itunes:author", namespaces=NS) == "Ryan"
    assert ch.findtext("itunes:explicit", namespaces=NS) == "false"
    assert ch.find("itunes:category", NS).get("text") == "Fiction"
    assert ch.findtext("itunes:owner/itunes:email", namespaces=NS) == "owner@example.com"
    art = ch.find("itunes:image", NS).get("href")
    assert art == f"{BASE}/_local_media/shows/scifi/artwork/cover-20260101000000.png"
    self_link = ch.find("atom:link", NS)
    assert self_link.get("rel") == "self"
    assert self_link.get("href") == f"{BASE}/_local_media/shows/scifi/feed.xml"


def test_channel_link_uses_custom_domain(storage):
    ch = parse(build(storage, sample_show(domain="scifimusicalpodcast.com")))
    assert ch.findtext("link") == "https://scifimusicalpodcast.com/"


def test_podcast_guid_is_stable_and_unique(storage):
    show = sample_show()
    assert podcast_guid(show) == podcast_guid(sample_show(title="Renamed", domain="x.example.com"))
    assert podcast_guid(show) != podcast_guid(sample_show(id="other"))
    ch = parse(build(storage))
    assert ch.findtext("podcast:guid", namespaces=NS) == podcast_guid(show)
    assert ch.find("podcast:locked", NS).get("owner") == "owner@example.com"


def test_optional_channel_fields_are_left_out_when_empty(storage):
    bare = Show(id="bare", title="Bare")
    ch = parse(build(storage, bare, []))
    assert ch.findtext("description") == "Bare"  # never empty
    for tag in ("itunes:author", "itunes:owner", "itunes:image", "podcast:locked"):
        assert ch.find(tag, NS) is None
    assert ch.find("image") is None
    assert ch.findall("item") == []


# --- items -------------------------------------------------------------------

def test_only_published_episodes_newest_first(storage):
    items = parse(build(storage)).findall("item")
    assert [i.findtext("title") for i in items] == ["Christmas in Robot World", "Ragna Rock"]


def test_scheduled_episode_appears_once_its_date_passes(storage):
    builder = FeedBuilder(storage, base_url=BASE)
    later = datetime(2027, 1, 2, tzinfo=UTC)
    channel = parse(builder.build(sample_show(), sample_episodes(), later))
    assert channel.find("item").findtext("title") == "Not yet published"


def test_item_enclosure_guid_and_itunes_tags(storage):
    first, second = parse(build(storage)).findall("item")
    enc = first.find("enclosure")
    assert enc.get("url") == f"{BASE}/_local_media/shows/scifi/audio/aaaa-christmas.mp3"
    assert (enc.get("length"), enc.get("type")) == ("15728640", "audio/mpeg")
    guid = first.find("guid")
    assert guid.text == "0a85c0a1-6b3d-4064-90a8-d9298cb335b5"
    assert guid.get("isPermaLink") == "false"
    assert first.findtext("pubDate") == "Sun, 01 Nov 2026 12:00:00 +0000"
    assert first.findtext("itunes:duration", namespaces=NS) == "1834"
    assert first.findtext("itunes:season", namespaces=NS) == "2"
    assert first.findtext("itunes:episode", namespaces=NS) == "7"
    assert first.find("itunes:image", NS).get("href").endswith("/episodes/0a85-20261101.jpg")

    # An imported episode keeps its original GUID, and empty fields are left out.
    assert second.findtext("guid") == "libsyn-legacy-guid-123"
    assert second.findtext("itunes:explicit", namespaces=NS) == "true"
    assert second.find("enclosure").get("type") == "audio/x-m4a"
    for tag in ("description", "itunes:duration", "itunes:season", "itunes:image"):
        assert second.find(tag, NS) is None


def test_notes_are_rendered_with_absolute_asset_urls(storage):
    AssetRepository(storage).save("scifi", "robots.jpg", PNG)
    first = parse(build(storage)).find("item")
    html = first.findtext("content:encoded", namespaces=NS)
    assert f'<img src="{BASE}/_local_media/shows/scifi/assets/robots.jpg"' in html
    assert "<strong>robot</strong>" in html and "A boy &amp; his" in html
    assert '<a href="https://youtu.be/abc">' in html
    assert first.findtext("description") == html


def test_feed_is_well_formed_with_awkward_text(storage):
    show = sample_show(title='Tom & "Jerry" <live>', description="1 < 2 & 3 > 2 ]]> done")
    ch = parse(build(storage, show))
    assert ch.findtext("title") == 'Tom & "Jerry" <live>'
    assert ch.findtext("description") == "1 < 2 & 3 > 2 ]]> done"


# --- problems ----------------------------------------------------------------

def test_feed_problems():
    assert feed_problems(sample_show()) == []
    problems = feed_problems(Show(id="bare", title="Bare"))
    assert len(problems) == 4 and any("artwork" in p for p in problems)


# --- publishing --------------------------------------------------------------

def test_publish_writes_and_removes_feed(storage):
    shows, builder = ShowRepository(storage), FeedBuilder(storage, base_url=BASE)
    shows.save(Show(id="my-show", title="My Show"), None)
    assert builder.publish("my-show") == f"{BASE}/_local_media/shows/my-show/feed.xml"
    assert b"<title>My Show</title>" in storage.path_for(feed_path("my-show")).read_bytes()

    show, gen = shows.get("my-show")
    show.deleted = True
    shows.save(show, gen)
    assert builder.publish("my-show") is None
    assert storage.object_info(feed_path("my-show")) is None


@pytest.fixture
def client(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    return app.test_client()


def feed_titles(client, show_id="my-show"):
    r = client.get(f"/_local_media/shows/{show_id}/feed.xml")
    if r.status_code != 200:
        return None
    return [i.findtext("title") for i in ET.fromstring(r.data).find("channel").findall("item")]


def publish_episode(client, storage, title="Episode One", **form):
    storage.write_bytes("shows/my-show/audio/a.mp3", MP3, "audio/mpeg")
    data = {"title": title, "audio_path": "shows/my-show/audio/a.mp3",
            "published_at": "2026-01-01T00:00:00Z", "duration_seconds": "60", **form}
    r = client.post("/admin/shows/my-show/episodes/new", data=data)
    assert r.status_code == 302, r.data
    return next(e for e in EpisodeRepository(storage).list("my-show") if e.title == title)


def test_feed_follows_admin_changes(client, storage):
    assert feed_titles(client) is None
    client.post("/admin/shows/new", data={"id": "my-show", "title": "My Show",
                                          "category": "Technology", "language": "en"})
    assert feed_titles(client) == []  # created with the show

    ep = publish_episode(client, storage)
    assert feed_titles(client) == ["Episode One"]

    client.post(f"/admin/shows/my-show/episodes/{ep.id}", data={
        "revision": str(ep.revision), "title": "Renamed", "audio_path": ep.audio_path,
        "published_at": ep.published_at, "duration_seconds": "60"})
    assert feed_titles(client) == ["Renamed"]

    gen = ShowRepository(storage).get("my-show")[1]
    client.post("/admin/shows/my-show", data={"generation": str(gen), "title": "New Name",
                                              "category": "Comedy", "language": "en"})
    feed = client.get("/_local_media/shows/my-show/feed.xml").data
    assert b"<title>New Name</title>" in feed and b'text="Comedy"' in feed

    client.post(f"/admin/shows/my-show/episodes/{ep.id}/delete")
    assert feed_titles(client) == []

    client.post("/admin/shows/my-show/delete")
    assert feed_titles(client) is None  # a deleted show stops publishing


def test_feed_urls_are_absolute_in_admin_built_feed(client, storage):
    client.post("/admin/shows/new", data={"id": "my-show", "title": "My Show",
                                          "category": "Technology", "language": "en"})
    publish_episode(client, storage)
    feed = ET.fromstring(client.get("/_local_media/shows/my-show/feed.xml").data)
    url = feed.find("channel/item/enclosure").get("url")
    assert url == "http://localhost/_local_media/shows/my-show/audio/a.mp3"


def test_scheduled_episode_is_flagged_and_kept_out(client, storage):
    client.post("/admin/shows/new", data={"id": "my-show", "title": "My Show",
                                          "category": "Technology", "language": "en"})
    publish_episode(client, storage, title="Future", published_at="2999-01-01T00:00:00Z")
    assert feed_titles(client) == []
    assert b"Scheduled, not in the feed yet" in client.get("/admin/shows/my-show/episodes").data


def test_episodes_page_shows_feed_link_problems_and_rebuild(client, storage):
    client.post("/admin/shows/new", data={"id": "my-show", "title": "My Show",
                                          "category": "Technology", "language": "en"})
    page = client.get("/admin/shows/my-show/episodes").data
    assert b"http://localhost/_local_media/shows/my-show/feed.xml" in page
    assert b"Add show artwork" in page and b"Add an owner email" in page

    storage.delete(feed_path("my-show"))
    assert b"Not built yet" in client.get("/admin/shows/my-show/episodes").data
    r = client.post("/admin/shows/my-show/feed/rebuild")
    assert r.status_code == 302
    assert feed_titles(client) == []


def test_save_survives_a_feed_failure(client, storage, monkeypatch):
    client.post("/admin/shows/new", data={"id": "my-show", "title": "My Show",
                                          "category": "Technology", "language": "en"})

    def boom(self, show_id, now=None):
        raise RuntimeError("bucket unavailable")

    monkeypatch.setattr(FeedBuilder, "publish", boom)
    storage.write_bytes("shows/my-show/audio/a.mp3", MP3, "audio/mpeg")
    r = client.post("/admin/shows/my-show/episodes/new", data={
        "title": "Kept", "audio_path": "shows/my-show/audio/a.mp3",
        "published_at": "2026-01-01T00:00:00Z"})
    assert r.status_code == 302 and "could+not+be+rebuilt" in r.headers["Location"]
    assert [e.title for e in EpisodeRepository(storage).list("my-show")] == ["Kept"]


# --- moving from another host --------------------------------------------------

def test_new_feed_url_only_while_previous_address_is_set(storage):
    assert parse(build(storage)).find("itunes:new-feed-url", NS) is None

    moved = sample_show(previous_feed_url="https://scifimusical.libsyn.com/rss",
                        domain="scifimusicalpodcast.com")
    channel = parse(build(storage, moved))
    new_home = channel.findtext("itunes:new-feed-url", namespaces=NS)
    assert new_home == "https://scifimusicalpodcast.com/feed.xml"
    assert new_home == channel.find("atom:link", NS).get("href")


@pytest.mark.parametrize("value,ok", [
    ("", True),
    ("https://scifimusical.libsyn.com/rss", True),
    ("http://feeds.example.com/show?format=xml", True),
    ("scifimusical.libsyn.com/rss", False),
    ("ftp://example.com/rss", False),
    ("https://no-dot", False),
    ("https://example.com/has space", False),
])
def test_previous_feed_url_validation(value, ok):
    show = Show(id="x", title="X", previous_feed_url=value)
    if ok:
        show.validate()
    else:
        with pytest.raises(ValidationError) as e:
            show.validate()
        assert "previous_feed_url" in e.value.errors


def test_previous_feed_address_in_show_form(client, storage):
    data = {"id": "my-show", "title": "My Show", "category": "Technology", "language": "en"}
    r = client.post("/admin/shows/new", data={**data, "previous_feed_url": "not a url"})
    assert r.status_code == 200 and b"starting with http://" in r.data

    client.post("/admin/shows/new",
                data={**data, "previous_feed_url": " https://old.example.com/rss "})
    show, _ = ShowRepository(storage).get("my-show")
    assert show.previous_feed_url == "https://old.example.com/rss"
    feed = client.get("/_local_media/shows/my-show/feed.xml").data
    assert b"<itunes:new-feed-url>http://localhost/_local_media/shows/my-show/feed.xml<" in feed
    assert b'value="https://old.example.com/rss"' in client.get("/admin/shows/my-show").data
