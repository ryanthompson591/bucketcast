# ruff: noqa: E501  (the sample feed below has long lines)
import io
import xml.etree.ElementTree as ET

import pytest

from bucketcast.__main__ import main
from bucketcast.assets import AssetRepository
from bucketcast.episodes import EpisodeRepository
from bucketcast.feed import feed_path
from bucketcast.html2md import html_to_markdown
from bucketcast.importer import (
    FeedImportError,
    Importer,
    format_report,
    guids_in,
    parse_date,
    parse_duration,
    parse_feed,
)
from bucketcast.notes import render_notes
from bucketcast.shows import Show, ShowRepository
from bucketcast.site import page_path
from bucketcast.storage import LocalStorage

FEED_URL = "https://old.example.com/show/rss"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff" + b"\x00" * 64
MP3_A, MP3_B, M4A = b"ID3" + b"A" * 5000, b"ID3" + b"B" * 7000, b"ftyp" + b"C" * 3000

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <atom:link href="https://rss.old.example.com/shows/1/feed.xml" rel="self" type="application/rss+xml"/>
  <title>Space Musicals &amp; Friends</title>
  <link>http://old.example.com/website</link>
  <language>en</language>
  <description>A series of musicals with a science fiction theme.</description>
  <itunes:author>Space Musicals</itunes:author>
  <itunes:category text="Music"/>
  <itunes:category text="Kids &amp; Family"/>
  <itunes:image href="https://static.old.example.com/art/logo_square.png"/>
  <itunes:explicit>false</itunes:explicit>
  <itunes:owner><itunes:name>Sam</itunes:name><itunes:email>sam@example.com</itunes:email></itunes:owner>
  <podcast:guid>11111111-2222-5333-8444-555555555555</podcast:guid>
  <item>
    <title>A Video-Only Post</title>
    <pubDate>Tue, 08 Nov 2022 02:59:00 +0000</pubDate>
    <guid isPermaLink="false">guid-no-audio</guid>
    <description>&lt;p&gt;Watch on YouTube.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Christmas In Robot World</title>
    <pubDate>Sat, 25 Dec 2021 12:49:32 +0000</pubDate>
    <guid isPermaLink="false">8d603e69-b3c5-43e5-824b-0d51be7dbeb1</guid>
    <description>&lt;p&gt;short&lt;/p&gt;</description>
    <content:encoded><![CDATA[<p><img src= "https://assets.old.example.com/show/1/robot Christmas.jpg" alt="Robots" width="437" /></p> <p>In a world filled with <b>Robots</b>.</p> <p>&nbsp;</p> <p>Watch: <a href="https://youtu.be/abc">https://youtu.be/abc</a></p>]]></content:encoded>
    <enclosure url="https://traffic.old.example.com/secure/show/ChristmasInRobotWorld.mp3?dest-id=1" length="20086070" type="audio/mpeg"/>
    <itunes:duration>13:57</itunes:duration>
    <itunes:explicit>false</itunes:explicit>
    <itunes:season>2</itunes:season>
    <itunes:episode>7</itunes:episode>
    <itunes:image href="https://static.old.example.com/art/robotChristmas2.jpg"/>
  </item>
  <item>
    <title>Ragna Rock</title>
    <pubDate>Sun, 15 Nov 2020 18:00:00 -0500</pubDate>
    <guid isPermaLink="true">http://old.example.com/ragna-rock</guid>
    <description>&lt;p&gt;Same image again: &lt;img src="https://assets.old.example.com/show/1/robot Christmas.jpg"&gt;&lt;/p&gt;</description>
    <enclosure url="https://traffic.old.example.com/secure/show/RagnaRock.mp3" length="5" type="audio/mpeg"/>
    <itunes:duration>1:02:03</itunes:duration>
    <itunes:explicit>yes</itunes:explicit>
  </item>
  <item>
    <title>An M4A Episode</title>
    <pubDate>Mon, 01 Jun 2020 10:00:00 GMT</pubDate>
    <guid>m4a-guid</guid>
    <enclosure url="https://traffic.old.example.com/download?id=77" length="0" type="audio/x-m4a"/>
    <itunes:duration>125</itunes:duration>
  </item>
</channel>
</rss>
"""

SITE = {
    FEED_URL: FEED,
    "https://static.old.example.com/art/logo_square.png": PNG,
    "https://assets.old.example.com/show/1/robot%20Christmas.jpg": JPG,
    "https://static.old.example.com/art/robotChristmas2.jpg": JPG,
    "https://traffic.old.example.com/secure/show/ChristmasInRobotWorld.mp3?dest-id=1": MP3_A,
    "https://traffic.old.example.com/secure/show/RagnaRock.mp3": MP3_B,
    "https://traffic.old.example.com/download?id=77": M4A,
}


class FakeWeb:
    """Stands in for the network: serves ``pages`` and records what was asked for."""

    def __init__(self, pages=None):
        self.pages = dict(SITE if pages is None else pages)
        self.requests: list[str] = []

    def __call__(self, url):
        self.requests.append(url)
        if url not in self.pages:
            raise OSError(f"HTTP Error 404: Not Found ({url})")
        return io.BytesIO(self.pages[url])


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


def importer(storage, web=None):
    web = web or FakeWeb()
    return Importer(storage, opener=web, base_url="https://app.example.com",
                    log=lambda _: None), web


# --- HTML to Markdown ----------------------------------------------------------

@pytest.mark.parametrize("html,markdown", [
    ("<p>One</p> <p>Two</p>", "One\n\nTwo"),
    ("<p>a<br>b<br/>c</p>", "a\nb\nc"),
    ("<p><b>bold</b> and <i>it</i> and <strong>s</strong><em>e</em></p>",
     "**bold** and *it* and **s***e*"),
    ('<p>See <a href="https://x.example/a b">this</a>.</p>', "See [this](https://x.example/a%20b)."),
    ('<a href="https://youtu.be/abc">https://youtu.be/abc</a>', "<https://youtu.be/abc>"),
    ('<p><img src="https://x.example/i.png" alt="An image"></p>',
     "![An image](https://x.example/i.png)"),
    ("<h2>Songs</h2><ul><li>One</li><li>Two</li></ul>", "## Songs\n\n- One\n- Two"),
    ("<ol><li>First</li><li>Second</li></ol>", "1. First\n2. Second"),
    ("<blockquote><p>Quoted</p></blockquote>", "> Quoted"),
    ("<p>&nbsp;</p><p></p><p><strong> </strong></p><p>Real</p>", "Real"),
    ("<p>2 * 3 = 6, a_b, [x]</p>", "2 \\* 3 = 6, a\\_b, \\[x\\]"),
    ("<p># not a heading</p><p>1. not a list</p><p>- nor this</p>",
     "\\# not a heading\n\n1\\. not a list\n\n\\- nor this"),
    ("<p>Kept</p><script>alert(1)</script><style>p{}</style>", "Kept"),
    ("Plain text &amp; an ampersand", "Plain text & an ampersand"),
    ("<div>Unknown <span>tags</span> keep <u>their</u> text</div>",
     "Unknown tags keep their text"),
    ("", ""),
])
def test_html_to_markdown(html, markdown):
    assert html_to_markdown(html) == markdown


def test_converted_notes_render_back_to_the_same_content():
    html = ('<p><img src="https://x.example/robot.jpg" alt="Robots"/></p> <p>In a world of '
            '<b>robots</b> lives a boy.</p><p>Credits:<br/>Thanks to <a href="https://a.example">'
            'Sanji</a> &amp; friends &lt;3</p>')
    rendered = render_notes(html_to_markdown(html))
    assert '<img src="https://x.example/robot.jpg" alt="Robots"' in rendered
    assert "<strong>robots</strong>" in rendered
    assert 'Credits:<br />\nThanks to <a href="https://a.example">Sanji</a>' in rendered
    assert "&amp; friends &lt;3" in rendered


def test_image_addresses_can_be_rewritten():
    md = html_to_markdown('<img src="https://old.example/a.jpg" alt="A">', lambda u: "asset:a.jpg")
    assert md == "![A](asset:a.jpg)"


# --- parsing -------------------------------------------------------------------

@pytest.mark.parametrize("value,seconds", [
    ("13:57", 837), ("1:02:03", 3723), ("125", 125), ("00:00:09", 9), ("", 0),
    ("soon", 0), ("1:2:3:4", 0), ("-5", 0),
])
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


def test_parse_date_normalises_to_utc():
    assert parse_date("Sun, 15 Nov 2020 18:00:00 -0500") == "2020-11-15T23:00:00+00:00"
    assert parse_date("Mon, 01 Jun 2020 10:00:00 GMT") == "2020-06-01T10:00:00+00:00"


def test_parse_feed():
    feed = parse_feed(FEED)
    assert feed.title == "Space Musicals & Friends"
    assert feed.categories == ["Music", "Kids & Family"]
    assert (feed.owner_name, feed.owner_email) == ("Sam", "sam@example.com")
    assert feed.self_url == "https://rss.old.example.com/shows/1/feed.xml"
    assert feed.podcast_guid == "11111111-2222-5333-8444-555555555555"
    assert [i.title for i in feed.items] == [
        "A Video-Only Post", "Christmas In Robot World", "Ragna Rock", "An M4A Episode"]
    robot = feed.items[1]
    assert robot.guid == "8d603e69-b3c5-43e5-824b-0d51be7dbeb1"
    assert (robot.duration_seconds, robot.season, robot.number) == (837, 2, 7)
    assert "In a world filled with" in robot.notes_html  # the fuller of the two copies
    assert feed.items[0].audio_url == "" and feed.items[2].explicit is True


@pytest.mark.parametrize("xml,message", [
    (b"not xml at all", "valid XML"),
    (b"<html><body>404</body></html>", "isn't a podcast RSS feed"),
    (b"<rss><channel><description>x</description></channel></rss>", "no title"),
])
def test_parse_feed_rejects_non_feeds(xml, message):
    with pytest.raises(FeedImportError, match=message):
        parse_feed(xml)


# --- importing -----------------------------------------------------------------

def test_import_creates_the_show(storage):
    imp, _ = importer(storage)
    report = imp.run("space", FEED_URL)
    assert report.ok and report.show_created

    show, _ = ShowRepository(storage).get("space")
    assert (show.title, show.author, show.category) == (
        "Space Musicals & Friends", "Space Musicals", "Music")
    assert (show.owner_name, show.owner_email, show.language) == ("Sam", "sam@example.com", "en")
    assert show.podcast_guid == "11111111-2222-5333-8444-555555555555"
    assert show.previous_feed_url == "https://rss.old.example.com/shows/1/feed.xml"
    assert storage.read_bytes(show.artwork_path) == PNG


def test_import_copies_episodes_audio_notes_and_images(storage):
    imp, _ = importer(storage)
    report = imp.run("space", FEED_URL)
    assert report.imported == ["An M4A Episode", "Ragna Rock", "Christmas In Robot World"]
    assert report.skipped == [("A Video-Only Post", "no audio file in the feed")]

    by_title = {e.title: e for e in EpisodeRepository(storage).list("space")}
    robot = by_title["Christmas In Robot World"]
    assert robot.guid == "8d603e69-b3c5-43e5-824b-0d51be7dbeb1"
    assert robot.published_at == "2021-12-25T12:49:32+00:00"
    assert (robot.duration_seconds, robot.season, robot.number) == (837, 2, 7)
    assert storage.read_bytes(robot.audio_path) == MP3_A
    # Size is what was actually stored, not the feed's (often wrong) number.
    assert (robot.audio_bytes, robot.audio_type) == (len(MP3_A), "audio/mpeg")
    assert robot.audio_path.endswith("-christmasinrobotworld.mp3")
    assert storage.read_bytes(robot.artwork_path) == JPG
    assert robot.description == (
        "![Robots](asset:robot-christmas.jpg)\n\nIn a world filled with **Robots**.\n\n"
        "Watch: <https://youtu.be/abc>")

    ragna = by_title["Ragna Rock"]
    assert ragna.guid == "http://old.example.com/ragna-rock" and ragna.explicit is True
    assert ragna.published_at == "2020-11-15T23:00:00+00:00"
    assert ragna.duration_seconds == 3723 and ragna.artwork_path == ""

    m4a = by_title["An M4A Episode"]
    # No file extension in the address: the format comes from the enclosure type.
    assert m4a.audio_path.endswith(".m4a") and m4a.audio_type == "audio/x-m4a"
    assert storage.read_bytes(m4a.audio_path) == M4A

    # An image used by two episodes is copied once.
    assert [a.name for a in AssetRepository(storage).list("space")] == ["robot-christmas.jpg"]
    assert "asset:robot-christmas.jpg" in ragna.description


def test_new_feed_lists_the_same_guids(storage):
    imp, _ = importer(storage)
    imp.run("space", FEED_URL)
    new_feed = storage.read_bytes(feed_path("space"))

    original = [g for g in guids_in(FEED) if g != "guid-no-audio"]
    assert sorted(guids_in(new_feed)) == sorted(original)

    channel = ET.fromstring(new_feed).find("channel")
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
          "podcast": "https://podcastindex.org/namespace/1.0"}
    assert channel.findtext("podcast:guid", namespaces=ns) == (
        "11111111-2222-5333-8444-555555555555")
    assert channel.findtext("itunes:new-feed-url", namespaces=ns).endswith("/feed.xml")
    assert [i.findtext("title") for i in channel.findall("item")] == [
        "Christmas In Robot World", "Ragna Rock", "An M4A Episode"]
    assert b"Christmas In Robot World" in storage.read_bytes(page_path("space", 1))


def test_rerun_skips_what_is_already_there(storage):
    imp, _ = importer(storage)
    imp.run("space", FEED_URL)
    before = {e.guid: e.audio_path for e in EpisodeRepository(storage).list("space")}

    again, web = importer(storage)
    report = again.run("space", FEED_URL)
    assert report.imported == [] and not report.show_created and report.ok
    assert sorted(r for _, r in report.skipped) == [
        "already imported"] * 3 + ["no audio file in the feed"]
    assert web.requests == [FEED_URL]  # nothing else was downloaded
    assert {e.guid: e.audio_path for e in EpisodeRepository(storage).list("space")} == before


def test_existing_show_settings_are_left_alone(storage):
    ShowRepository(storage).save(Show(id="space", title="My Own Title", category="Comedy"), None)
    imp, web = importer(storage)
    report = imp.run("space", FEED_URL)
    show, _ = ShowRepository(storage).get("space")
    assert (show.title, show.category, show.artwork_path) == ("My Own Title", "Comedy", "")
    assert not report.show_created and len(report.imported) == 3
    assert "https://static.old.example.com/art/logo_square.png" not in web.requests


def test_deleted_episode_is_not_brought_back(storage):
    imp, _ = importer(storage)
    imp.run("space", FEED_URL)
    repo = EpisodeRepository(storage)
    ragna = next(e for e in repo.list("space") if e.title == "Ragna Rock")
    ragna.deleted = True
    repo.update("space", ragna, ragna.revision)

    report = importer(storage)[0].run("space", FEED_URL)
    assert report.imported == []
    assert "Ragna Rock" not in [e.title for e in repo.list("space")]


def test_limit_imports_oldest_first_and_resumes(storage):
    report = importer(storage)[0].run("space", FEED_URL, limit=1)
    assert report.imported == ["An M4A Episode"]
    assert ("Ragna Rock", "over the --limit") in report.skipped

    report = importer(storage)[0].run("space", FEED_URL, limit=1)
    assert report.imported == ["Ragna Rock"]
    assert len(EpisodeRepository(storage).list("space")) == 2


def test_dry_run_changes_and_downloads_nothing(storage):
    imp, web = importer(storage)
    report = imp.run("space", FEED_URL, dry_run=True)
    assert report.dry_run and report.show_created and len(report.imported) == 3
    assert web.requests == [FEED_URL]
    assert ShowRepository(storage).get("space")[0] is None
    assert storage.list_dirs("shows") == []
    text = format_report(report)
    assert "Would import 3 episode(s)." in text and "nothing was downloaded" in text


def test_one_bad_episode_does_not_stop_the_rest(storage):
    pages = dict(SITE)
    del pages["https://traffic.old.example.com/secure/show/RagnaRock.mp3"]
    pages["https://traffic.old.example.com/download?id=77"] = b""
    report = importer(storage, FakeWeb(pages))[0].run("space", FEED_URL)

    assert not report.ok and report.imported == ["Christmas In Robot World"]
    assert dict(report.failed)["An M4A Episode"] == "the audio file is empty"
    assert "404" in dict(report.failed)["Ragna Rock"]
    assert [e.title for e in EpisodeRepository(storage).list("space")] == [
        "Christmas In Robot World"]
    # No half-imported audio is left behind for the failed ones.
    assert len(storage.list_files("shows/space/audio")) == 1
    assert "FAILED 2:" in format_report(report)

    # Fixing the source and re-running picks up only what was missing.
    report = importer(storage)[0].run("space", FEED_URL)
    assert sorted(report.imported) == ["An M4A Episode", "Ragna Rock"] and report.ok


def test_missing_images_are_survivable(storage):
    pages = {k: v for k, v in SITE.items() if "art/" not in k and "assets." not in k}
    report = importer(storage, FakeWeb(pages))[0].run("space", FEED_URL)
    assert report.ok and len(report.imported) == 3
    assert any("No show artwork" in w for w in report.warnings)
    robot = next(e for e in EpisodeRepository(storage).list("space") if "Robot" in e.title)
    assert robot.artwork_path == ""
    # The image keeps its original address rather than vanishing from the notes.
    assert "(https://assets.old.example.com/show/1/robot%20Christmas.jpg)" in robot.description


def test_unknown_category_and_unsupported_audio(storage):
    feed = FEED.replace(b'text="Music"', b'text="Musicals"').replace(
        b'text="Kids &amp; Family"', b'text="Space"').replace(
        b'type="audio/x-m4a"', b'type="audio/ogg"')
    report = importer(storage, FakeWeb({**SITE, FEED_URL: feed}))[0].run("space", FEED_URL)
    assert ShowRepository(storage).get("space")[0].category == "Technology"
    assert any("Category set to Technology" in w for w in report.warnings)
    assert "unsupported audio format" in dict(report.failed)["An M4A Episode"]


@pytest.mark.parametrize("show_id,url,pages,message", [
    ("Bad ID", FEED_URL, None, "Show ID"),
    ("space", "ftp://old.example.com/rss", None, "Couldn't download|Not an http"),
    ("space", "https://nowhere.example.com/rss", None, "Couldn't download the feed"),
    ("space", FEED_URL, {FEED_URL: b"<html>nope</html>"}, "isn't a podcast RSS feed"),
])
def test_import_refuses_bad_input(storage, show_id, url, pages, message):
    with pytest.raises(FeedImportError, match=message):
        importer(storage, FakeWeb(pages))[0].run(show_id, url)
    assert storage.list_dirs("shows") == []


def test_import_refuses_a_deleted_show(storage):
    ShowRepository(storage).save(Show(id="space", title="Gone", deleted=True), None)
    with pytest.raises(FeedImportError, match="was deleted"):
        importer(storage)[0].run("space", FEED_URL)


# --- command line --------------------------------------------------------------

def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    from bucketcast import importer as importer_module

    monkeypatch.setenv("BUCKETCAST_LOCAL_DIR", str(tmp_path / "bucket"))
    monkeypatch.delenv("BUCKETCAST_BUCKET", raising=False)
    monkeypatch.setattr(importer_module.Importer.__init__, "__defaults__",
                        (FakeWeb(), "", print))
    code = main(["import", "--show", "space", "--feed", FEED_URL, "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Feed: Space Musicals & Friends (4 items)" in out
    assert "Would import 3 episode(s)." in out and "A Video-Only Post: no audio" in out


def test_cli_reports_a_bad_feed(tmp_path, monkeypatch, capsys):
    from bucketcast import importer as importer_module

    monkeypatch.setenv("BUCKETCAST_LOCAL_DIR", str(tmp_path / "bucket"))
    monkeypatch.delenv("BUCKETCAST_BUCKET", raising=False)
    monkeypatch.setattr(importer_module.Importer.__init__, "__defaults__",
                        (FakeWeb({FEED_URL: b"<html/>"}), "", print))
    assert main(["import", "--show", "space", "--feed", FEED_URL]) == 2
    assert "Import stopped" in capsys.readouterr().err


def test_notes_come_from_whichever_copy_is_fuller():
    """Some hosts drop the images from content:encoded but keep them in description."""
    feed = FEED.replace(
        b"<description>&lt;p&gt;short&lt;/p&gt;</description>",
        b"<description>&lt;p&gt;&lt;img src=\"https://assets.old.example.com/show/1/big.jpg\""
        b" alt=\"Big\"/&gt;&lt;/p&gt; " + b"&lt;p&gt;Much longer notes.&lt;/p&gt; " * 20
        + b"</description>")
    robot = parse_feed(feed).items[1]
    assert "big.jpg" in robot.notes_html and "Much longer notes." in robot.notes_html
