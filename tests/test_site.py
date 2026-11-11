from datetime import UTC, datetime

import pytest

from bucketcast import create_app
from bucketcast.assets import AssetRepository
from bucketcast.episodes import Episode, EpisodeRepository
from bucketcast.shows import Show, ShowRepository, ValidationError
from bucketcast.site import SiteBuilder, excerpt, page_path
from bucketcast.storage import LocalStorage

NOW = datetime(2026, 11, 11, 20, 0, 0, tzinfo=UTC)
BASE = "https://media.example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3" + b"\x00" * 2045


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "bucket")


def make_show(**overrides) -> Show:
    values = dict(id="scifi", title="Sci-Fi <Musicals> & More", author="Ryan",
                  description="Episodic musicals.\nFamily friendly.",
                  artwork_path="shows/scifi/artwork/cover.png")
    values.update(overrides)
    return Show(**values)


def make_episodes(count: int, **overrides) -> list[Episode]:
    """``count`` episodes, one per day, the highest number being the newest."""
    return [
        Episode(id=f"{n:032x}", title=f"Episode {n}", number=n,
                description=f"Notes for **number {n}**.",
                published_at=f"2026-{1 + (n - 1) // 28:02d}-{1 + (n - 1) % 28:02d}T12:00:00+00:00",
                audio_path=f"shows/scifi/audio/{n}.mp3", audio_bytes=1000,
                audio_type="audio/mpeg", duration_seconds=60, **overrides)
        for n in range(1, count + 1)
    ]


def build(storage, show=None, episodes=None) -> list[str]:
    pages = SiteBuilder(storage, base_url=BASE).build(
        show or make_show(), make_episodes(3) if episodes is None else episodes, NOW)
    return [p.decode("utf-8") for p in pages]


# --- helpers -----------------------------------------------------------------

def test_page_paths():
    assert page_path("scifi", 1) == "shows/scifi/index.html"
    assert page_path("scifi", 3) == "shows/scifi/page/3/index.html"


def test_excerpt_is_plain_text_cut_at_a_word():
    html = ("<p><img src='x.png' alt='Robots'></p>"
            "<p>A boy &amp; his <strong>robot</strong> parents.</p>")
    assert excerpt(html) == "A boy & his robot parents."
    long = "<p>" + "word " * 200 + "</p>"
    cut = excerpt(long)
    assert len(cut) <= 301 and cut.endswith("…") and not cut.endswith(" …")


# --- one page ----------------------------------------------------------------

def test_page_has_show_header_feed_link_and_players(storage):
    (page,) = build(storage)
    assert "<title>Sci-Fi &lt;Musicals&gt; &amp; More</title>" in page
    assert f'src="{BASE}/_local_media/shows/scifi/artwork/cover.png"' in page
    assert "Episodic musicals.\nFamily friendly." in page
    assert '<a class="subscribe" href="feed.xml">' in page
    assert f'type="application/rss+xml" title="Sci-Fi &lt;Musicals&gt; &amp; More" ' \
           f'href="{BASE}/_local_media/shows/scifi/feed.xml"' in page
    assert page.count("<audio controls") == 3
    assert f'<source src="{BASE}/_local_media/shows/scifi/audio/3.mp3" type="audio/mpeg">' in page
    assert "<script" not in page  # works with no JavaScript
    assert 'aria-label="Episode pages"' not in page  # a single page needs no pager


def test_episodes_are_newest_first_with_full_notes(storage):
    (page,) = build(storage)
    assert page.index("Episode 3") < page.index("Episode 2") < page.index("Episode 1")
    assert "Notes for <strong>number 3</strong>." in page
    assert "Read more" not in page and "…" not in page
    assert "Jan 3, 2026</time> · Episode 3</p>" in page


def test_notes_images_use_absolute_asset_urls(storage):
    AssetRepository(storage).save("scifi", "robots.jpg", PNG)
    episodes = make_episodes(1)
    episodes[0].description = "![Robots](asset:robots.jpg)\n\nA <b>boy</b>."
    (page,) = build(storage, episodes=episodes)
    assert f'<img src="{BASE}/_local_media/shows/scifi/assets/robots.jpg" alt="Robots"' in page
    assert "&lt;b&gt;boy&lt;/b&gt;" in page  # raw HTML in notes stays escaped


def test_excerpt_mode_shows_a_short_plain_summary(storage):
    episodes = make_episodes(1)
    episodes[0].description = "![Robots](asset:robots.jpg)\n\n" + "Long notes. " * 80
    (page,) = build(storage, make_show(full_notes=False), episodes)
    assert "<strong>" not in page and "robots.jpg" not in page
    assert "Long notes. Long notes." in page and "…" in page
    assert "Read more" not in page


def test_episode_thumbnail_and_season_label(storage):
    episodes = make_episodes(1, season=2, artwork_path="shows/scifi/artwork/episodes/e1.jpg")
    (page,) = build(storage, episodes=episodes)
    assert f'<img src="{BASE}/_local_media/shows/scifi/artwork/episodes/e1.jpg"' in page
    assert "Season 2 · Episode 1" in page


def test_unpublished_and_deleted_episodes_are_left_out(storage):
    episodes = make_episodes(3)
    episodes[0].deleted = True
    episodes[1].published_at = "2027-06-01T00:00:00+00:00"
    (page,) = build(storage, episodes=episodes)
    assert page.count("<article>") == 1 and "Episode 3" in page


def test_empty_show_still_gets_a_page(storage):
    (page,) = build(storage, make_show(artwork_path="", description=""), [])
    assert "No episodes yet" in page and "<audio" not in page and "<img" not in page


def test_tracked_show_plays_through_the_counting_redirect(storage):
    (page,) = build(storage, make_show(track_downloads=True, domain="mypodcast.com"),
                    make_episodes(1))
    assert f'<source src="https://mypodcast.com/e/scifi/{1:032x}.mp3"' in page
    assert '<link rel="canonical" href="https://mypodcast.com/">' in page
    assert 'href="https://mypodcast.com/feed.xml"' in page


# --- pagination --------------------------------------------------------------

def test_thirty_episodes_per_page_by_default(storage):
    pages = build(storage, episodes=make_episodes(65))
    assert [p.count("<article>") for p in pages] == [30, 30, 5]
    first, second, third = pages
    assert "Episode 65" in first and "Episode 36" in first and "Episode 35" not in first
    assert "Episode 35" in second and "Episode 6" in second
    assert "Episode 5" in third and "Episode 1</h2>" in third


def test_pager_links_are_relative_and_correct(storage):
    first, second, third = build(storage, episodes=make_episodes(65))
    assert 'href="page/2/index.html" rel="next"' in first and 'rel="prev"' not in first
    assert "Page 1 of 3" in first
    assert 'href="../../index.html" rel="prev"' in second
    assert 'href="../../page/3/index.html" rel="next"' in second
    assert 'href="../../page/2/index.html" rel="prev"' in third and 'rel="next"' not in third
    # Deeper pages reach the feed and the home page from where they sit.
    assert '<a class="subscribe" href="../../feed.xml">' in second
    assert '<h1><a href="../../index.html">' in second
    # The show description is only on the first page.
    assert "Episodic musicals." in first and 'class="about"' not in second


def test_episodes_per_page_setting(storage):
    pages = build(storage, make_show(episodes_per_page=10), make_episodes(25))
    assert [p.count("<article>") for p in pages] == [10, 10, 5]
    assert len(build(storage, make_show(episodes_per_page=10), make_episodes(10))) == 1


@pytest.mark.parametrize("bad", [0, -5, 201, "30", 2.5])
def test_episodes_per_page_is_validated(bad):
    with pytest.raises(ValidationError) as e:
        Show(id="x", title="X", episodes_per_page=bad).validate()
    assert "episodes_per_page" in e.value.errors


# --- publishing --------------------------------------------------------------

def test_publish_writes_pages_and_prunes_stale_ones(storage):
    builder, show = SiteBuilder(storage, base_url=BASE), make_show(episodes_per_page=10)
    assert builder.publish(show, make_episodes(25), NOW) == 3
    assert storage.read_bytes(page_path("scifi", 3)) is not None

    assert builder.publish(show, make_episodes(5), NOW) == 1
    assert storage.read_bytes(page_path("scifi", 1)) is not None
    assert storage.read_bytes(page_path("scifi", 2)) is None
    assert storage.read_bytes(page_path("scifi", 3)) is None

    show.deleted = True
    assert builder.publish(show, make_episodes(5), NOW) == 0
    assert storage.read_bytes(page_path("scifi", 1)) is None


# --- admin -------------------------------------------------------------------

@pytest.fixture
def client(storage):
    app = create_app({"TESTING": True, "DEV_AUTH_BYPASS": True}, storage=storage)
    return app.test_client()


def new_show(client, **extra):
    data = {"id": "my-show", "title": "My Show", "category": "Technology", "language": "en"}
    return client.post("/admin/shows/new", data={**data, **extra})


def page(client, number=1):
    r = client.get("/_local_media/" + page_path("my-show", number))
    return r.data.decode("utf-8") if r.status_code == 200 else None


def test_page_follows_admin_changes(client, storage):
    assert page(client) is None
    new_show(client)
    assert "No episodes yet" in page(client)

    storage.write_bytes("shows/my-show/audio/a.mp3", MP3, "audio/mpeg")
    client.post("/admin/shows/my-show/episodes/new", data={
        "title": "First Episode", "audio_path": "shows/my-show/audio/a.mp3",
        "published_at": "2026-01-01T00:00:00Z", "description": "Hello **there**"})
    assert "First Episode" in page(client) and "<strong>there</strong>" in page(client)

    (ep,) = EpisodeRepository(storage).list("my-show")
    client.post(f"/admin/shows/my-show/episodes/{ep.id}/delete")
    assert "First Episode" not in page(client)

    client.post("/admin/shows/my-show/delete")
    assert page(client) is None


def test_site_page_settings_in_show_form(client, storage):
    new_show(client, episodes_per_page="2", notes_style="excerpt")
    show, gen = ShowRepository(storage).get("my-show")
    assert (show.episodes_per_page, show.full_notes) == (2, False)

    form = client.get("/admin/shows/my-show").data
    assert b'name="episodes_per_page"' in form and b'value="2"' in form
    assert b'<option value="excerpt" selected>' in form

    repo = EpisodeRepository(storage)
    for n in range(1, 6):
        repo.add("my-show", Episode(title=f"Ep {n}", audio_path=f"shows/my-show/audio/{n}.mp3",
                                    published_at=f"2026-01-0{n}T00:00:00+00:00"))
    client.post("/admin/shows/my-show/feed/rebuild")
    assert page(client).count("<article>") == 2 and page(client, 3).count("<article>") == 1

    # Back to the defaults when the form sends them.
    client.post("/admin/shows/my-show", data={
        "generation": str(gen), "title": "My Show", "category": "Technology",
        "language": "en", "episodes_per_page": "30", "notes_style": "full"})
    show, _ = ShowRepository(storage).get("my-show")
    assert (show.episodes_per_page, show.full_notes) == (30, True)
    assert page(client).count("<article>") == 5 and page(client, 2) is None


def test_bad_episodes_per_page_is_reported(client, storage):
    r = new_show(client, episodes_per_page="lots")
    assert r.status_code == 200 and b"Use a whole number" in r.data
    r = new_show(client, episodes_per_page="0")
    assert r.status_code == 200 and b"from 1 to 200" in r.data
    assert ShowRepository(storage).get("my-show")[0] is None


def test_episodes_page_links_to_show_page(client):
    new_show(client)
    listing = client.get("/admin/shows/my-show/episodes").data
    assert b"Show page" in listing
    assert b"http://localhost/_local_media/shows/my-show/index.html" in listing

    new_show(client, id="other", title="Other", domain="mypodcast.com")
    listing = client.get("/admin/shows/other/episodes").data
    assert b"https://mypodcast.com/feed.xml" in listing and b"Copies in the bucket" in listing
