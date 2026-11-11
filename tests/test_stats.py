import io
import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta

import pytest

from bucketcast import create_app
from bucketcast.episodes import Episode, EpisodeRepository
from bucketcast.feed import FeedBuilder
from bucketcast.shows import Show, ShowRepository
from bucketcast.stats import (
    BigQueryStats,
    DownloadEvent,
    LocalStats,
    Row,
    app_name,
    hash_ip,
    is_bot,
    is_countable,
    load_salt,
    summarize,
)
from bucketcast.storage import LocalStorage

TODAY = date(2026, 11, 11)
APPLE = "AppleCoreMedia/1.0.0.21A329 (iPhone; U; CPU OS 17_0 like Mac OS X; en_us)"
OVERCAST = "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)"
EP1 = "0a85c0a16b3d406490a8d9298cb335b5"
EP2 = "11111111111141118111111111111111"


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


@pytest.fixture
def stats(tmp_path):
    return LocalStats(tmp_path / "stats")


@pytest.fixture
def client(storage, stats):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage, stats=stats)
    ShowRepository(storage).save(Show(id="my-show", title="My Show", track_downloads=True), None)
    repo = EpisodeRepository(storage)
    repo.add("my-show", Episode(id=EP1, title="Episode One",
                                audio_path="shows/my-show/audio/one.mp3"))
    repo.add("my-show", Episode(id=EP2, title="Episode Two",
                                audio_path="shows/my-show/audio/two.m4a"))
    return app.test_client()


def event(day=TODAY, episode=EP1, ip="a", ua=APPLE, **kw):
    ts = datetime(day.year, day.month, day.day, 12, tzinfo=UTC).isoformat()
    return DownloadEvent(show_id="my-show", episode_id=episode, ip_hash=ip, user_agent=ua,
                         ts=ts, **kw)


# --- counting rules ----------------------------------------------------------

@pytest.mark.parametrize("method,byte_range,expected", [
    ("GET", "", True),
    ("GET", "bytes=0-", True),
    ("GET", "bytes=0-1048575", True),
    ("get", "Bytes=0-", True),
    ("GET", "bytes=0-1", False),        # the 2-byte probe
    ("GET", "bytes=1048576-", False),   # resuming or seeking
    ("HEAD", "", False),
])
def test_is_countable(method, byte_range, expected):
    assert is_countable(method, byte_range) is expected


@pytest.mark.parametrize("ua", ["", "  ", "Googlebot/2.1", "curl/8.4.0", "python-requests/2.31",
                                "Mozilla/5.0 (compatible; bingbot/2.0)", "UptimeRobot/2.0"])
def test_bots_are_recognised(ua):
    assert is_bot(ua)


@pytest.mark.parametrize("ua,name", [
    (APPLE, "Apple Podcasts"),
    ("Podcasts/1650.20 CFNetwork/1485 Darwin/23.1.0", "Apple Podcasts"),
    ("Spotify/8.8.0 iOS/17.0 (iPhone14,5)", "Spotify"),
    (OVERCAST, "Overcast"),
    ("Pocket Casts", "Pocket Casts"),
    ("okhttp/4.9.3", "Other"),
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36", "Web browser"),
])
def test_app_names(ua, name):
    assert app_name(ua) == name and not is_bot(ua)


def test_hash_ip_depends_on_salt_and_hides_the_address():
    assert hash_ip("203.0.113.9", "s1") == hash_ip("203.0.113.9", "s1")
    assert hash_ip("203.0.113.9", "s1") != hash_ip("203.0.113.9", "s2")
    assert "203.0.113.9" not in hash_ip("203.0.113.9", "s1")


def test_salt_is_created_once(storage):
    salt = load_salt(storage)
    assert len(salt) == 32 and load_salt(storage) == salt


# --- local backend -----------------------------------------------------------

def test_local_rows_dedupe_per_listener_app_episode_day(stats):
    yesterday = TODAY - timedelta(days=1)
    for e in [
        event(),                           # counted
        event(),                           # same listener again today: not counted
        event(byte_range="bytes=5000-"),   # seeking: not counted
        event(method="HEAD"),              # not a download
        event(ip="b"),                     # second listener
        event(ua=OVERCAST),                # same listener, different app
        event(episode=EP2),                # same listener, different episode
        event(day=yesterday),              # same listener, different day
        DownloadEvent(show_id="other-show", episode_id=EP1, ip_hash="z", user_agent=APPLE),
    ]:
        stats.record(e)
    rows = stats.rows("my-show", None)
    assert sorted((r.day, r.episode_id, r.user_agent, r.downloads) for r in rows) == sorted([
        (yesterday, EP1, APPLE, 1), (TODAY, EP1, APPLE, 2),
        (TODAY, EP1, OVERCAST, 1), (TODAY, EP2, APPLE, 1),
    ])
    assert [r.day for r in stats.rows("my-show", TODAY)] == [TODAY] * 3


def test_local_rows_survive_a_damaged_line(stats):
    stats.record(event())
    with stats.path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    stats.record(event(ip="b"))
    assert sum(r.downloads for r in stats.rows("my-show", None)) == 2
    assert LocalStats(stats.path.parent / "missing").rows("my-show", None) == []


# --- summarize ---------------------------------------------------------------

def test_summarize_totals_window_and_breakdowns():
    rows = [
        Row(TODAY, EP1, APPLE, 3),
        Row(TODAY, EP2, OVERCAST, 1),
        Row(TODAY, EP1, "Googlebot/2.1", 50),            # bot: ignored
        Row(TODAY - timedelta(days=3), EP1, APPLE, 2),
        Row(TODAY - timedelta(days=10), EP2, APPLE, 4),
        Row(TODAY - timedelta(days=40), EP1, APPLE, 100),  # outside 30 days
        Row(TODAY + timedelta(days=1), EP1, APPLE, 7),     # clock skew: ignored
    ]
    s = summarize(rows, 30, TODAY)
    assert (s.total, s.last_7_days, s.today) == (10, 6, 4)
    assert s.by_episode == {EP1: 5, EP2: 5}
    assert s.by_app == [("Apple Podcasts", 9), ("Overcast", 1)]
    assert len(s.by_day) == 30 and s.by_day[-1] == (TODAY, 4)
    assert s.by_day[0] == (TODAY - timedelta(days=29), 0)  # empty days are kept

    everything = summarize(rows, None, TODAY)
    assert everything.total == 110
    assert everything.by_day[0][0] == TODAY - timedelta(days=40)


def test_summarize_with_no_downloads():
    s = summarize([], 7, TODAY)
    assert (s.total, s.by_app, s.by_episode) == (0, [], {})
    assert [n for _, n in s.by_day] == [0] * 7
    assert summarize([], None, TODAY).by_day == [(TODAY, 0)]


# --- BigQuery backend (no cloud access; logging and query shape only) --------

def test_bigquery_record_writes_a_structured_log_line():
    out = io.StringIO()
    BigQueryStats("proj.dataset.downloads", stream=out).record(event(byte_range="bytes=0-"))
    line = json.loads(out.getvalue())
    assert line["event"] == "bucketcast.download" and line["severity"] == "INFO"
    assert (line["show_id"], line["episode_id"], line["ip_hash"]) == ("my-show", EP1, "a")
    assert (line["method"], line["byte_range"], line["user_agent"]) == ("GET", "bytes=0-", APPLE)
    assert out.getvalue().count("\n") == 1  # one entry per line


def test_bigquery_query_shape_and_table_validation():
    store = BigQueryStats("my-proj.podcast_logs.stdout")
    sql = store.query(has_since=True)
    assert "`my-proj.podcast_logs.stdout`" in sql
    assert "@show_id" in sql and "@since" in sql
    assert "COUNT(DISTINCT jsonPayload.ip_hash)" in sql
    assert "@since" not in store.query(has_since=False)
    for bad in ["downloads", "a.b", "a.b.c; DROP TABLE x", "a.b.`c`"]:
        with pytest.raises(ValueError):
            BigQueryStats(bad)


# --- tracking redirect -------------------------------------------------------

def test_redirect_records_and_sends_listener_to_the_file(client, stats):
    r = client.get(f"/e/my-show/{EP1}.mp3", headers={
        "User-Agent": APPLE, "Range": "bytes=0-", "X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/_local_media/shows/my-show/audio/one.mp3"
    assert r.headers["Cache-Control"] == "no-store"

    (line,) = stats.path.read_text(encoding="utf-8").splitlines()
    logged = json.loads(line)
    assert (logged["show_id"], logged["episode_id"]) == ("my-show", EP1)
    assert (logged["user_agent"], logged["byte_range"], logged["method"]) == (
        APPLE, "bytes=0-", "GET")
    assert "203.0.113.9" not in line and len(logged["ip_hash"]) == 32


def test_redirect_needs_no_login(storage, stats):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": ""},
                     storage=storage, stats=stats)
    ShowRepository(storage).save(Show(id="my-show", title="My Show"), None)
    EpisodeRepository(storage).add("my-show", Episode(
        id=EP1, title="One", audio_path="shows/my-show/audio/one.mp3"))
    c = app.test_client()
    assert c.get("/admin/").status_code == 403
    assert c.get(f"/e/my-show/{EP1}.mp3").status_code == 302


def test_redirect_distinguishes_listeners_by_address(client, stats):
    for ip in ("203.0.113.1", "203.0.113.1", "203.0.113.2"):
        client.get(f"/e/my-show/{EP1}.mp3", headers={"User-Agent": APPLE,
                                                     "X-Forwarded-For": ip})
    assert sum(r.downloads for r in stats.rows("my-show", None)) == 2


@pytest.mark.parametrize("path", [
    "/e/my-show/00000000000000000000000000000000.mp3",   # unknown episode
    f"/e/other-show/{EP1}.mp3",                          # unknown show
    "/e/my-show/not-an-id.mp3",
    "/e/My%20Show/x.mp3",
])
def test_redirect_404s_without_recording(client, stats, path):
    assert client.get(path).status_code == 404
    assert not stats.path.exists()


def test_redirect_stops_for_deleted_episode(client, storage):
    repo = EpisodeRepository(storage)
    ep = repo.get("my-show", EP2)
    ep.deleted = True
    repo.update("my-show", ep, ep.revision)
    assert client.get(f"/e/my-show/{EP2}.m4a").status_code == 404


def test_redirect_still_works_if_recording_fails(client, monkeypatch):
    def boom(self, event):
        raise OSError("disk full")

    monkeypatch.setattr(LocalStats, "record", boom)
    assert client.get(f"/e/my-show/{EP1}.mp3").status_code == 302


def test_head_request_redirects_but_is_not_counted(client, stats):
    assert client.head(f"/e/my-show/{EP1}.mp3").status_code == 302
    assert stats.rows("my-show", None) == []


# --- feed --------------------------------------------------------------------

def enclosures(storage, show):
    builder = FeedBuilder(storage, base_url="https://app.example.com")
    xml = builder.build(show, EpisodeRepository(storage).list(show.id))
    return [i.find("enclosure").get("url") for i in ET.fromstring(xml).iter("item")]


def test_feed_links_go_through_the_redirect_only_when_counting_is_on(client, storage):
    show, _ = ShowRepository(storage).get("my-show")
    assert sorted(enclosures(storage, show)) == sorted([
        f"https://app.example.com/e/my-show/{EP1}.mp3",
        f"https://app.example.com/e/my-show/{EP2}.m4a",
    ])
    show.domain = "mypodcast.com"
    assert f"https://mypodcast.com/e/my-show/{EP1}.mp3" in enclosures(storage, show)
    show.track_downloads = False
    assert "https://app.example.com/_local_media/shows/my-show/audio/one.mp3" in enclosures(
        storage, show)


def test_count_downloads_switch_on_show_form(client, storage):
    gen = ShowRepository(storage).get("my-show")[1]
    client.post("/admin/shows/my-show", data={"generation": str(gen), "title": "My Show",
                                              "category": "Technology", "language": "en"})
    assert ShowRepository(storage).get("my-show")[0].track_downloads is False
    feed = client.get("/_local_media/shows/my-show/feed.xml").data
    assert b"/e/my-show/" not in feed

    gen = ShowRepository(storage).get("my-show")[1]
    client.post("/admin/shows/my-show", data={
        "generation": str(gen), "title": "My Show", "category": "Technology",
        "language": "en", "track_downloads": "on"})
    feed = client.get("/_local_media/shows/my-show/feed.xml").data
    assert f"http://localhost/e/my-show/{EP1}.mp3".encode() in feed


# --- dashboard ---------------------------------------------------------------

def test_dashboard_with_no_downloads(client):
    r = client.get("/admin/shows/my-show/stats")
    assert r.status_code == 200
    assert b"No downloads counted in this period yet" in r.data
    assert b"Episode One" in r.data  # episodes are listed even at zero
    assert client.get("/admin/shows/nope/stats").status_code == 404


def test_dashboard_shows_counts(client, stats):
    today = datetime.now(UTC).date()
    for e in [event(day=today), event(day=today, ip="b"), event(day=today, ua=OVERCAST),
              event(day=today, episode=EP2), event(day=today, ua="Googlebot/2.1", ip="bot"),
              event(day=today - timedelta(days=60))]:
        stats.record(e)
    page = client.get("/admin/shows/my-show/stats").data.decode()
    assert '<div class="value">4</div>' in page          # 30 days; bot and old one excluded
    assert "Apple Podcasts" in page and "Overcast" in page and "Googlebot" not in page
    assert page.index("Episode One") < page.index("Episode Two")  # most downloaded first
    assert "<svg" in page and page.count('class="hit"') == 30
    assert "Show as a table" in page

    everything = client.get("/admin/shows/my-show/stats?range=all").data.decode()
    assert '<div class="value">5</div>' in everything
    assert client.get("/admin/shows/my-show/stats?range=bogus").status_code == 200


def test_dashboard_warns_when_counting_is_off(client, storage):
    shows = ShowRepository(storage)
    show, gen = shows.get("my-show")
    show.track_downloads = False
    shows.save(show, gen)
    assert b"Download counting is off" in client.get("/admin/shows/my-show/stats").data


def test_dashboard_reports_a_backend_failure(client, monkeypatch):
    def boom(self, show_id, since):
        raise RuntimeError("BigQuery unavailable")

    monkeypatch.setattr(LocalStats, "rows", boom)
    r = client.get("/admin/shows/my-show/stats")
    assert r.status_code == 200 and b"couldn&#39;t be loaded" in r.data


def test_episodes_page_links_to_stats(client):
    assert b"/admin/shows/my-show/stats" in client.get("/admin/shows/my-show/episodes").data
