"""RSS feed generation.

Each show's feed is a static file, ``shows/<show-id>/feed.xml``, rebuilt
whenever the show or one of its episodes changes. Podcast apps and
directories only ever fetch that file; they never reach the app.

The feed is RSS 2.0 with the ``itunes:`` tags Apple Podcasts requires, plus
``content:encoded`` for rich show notes and a couple of cheap Podcasting 2.0
tags (``podcast:guid``, ``podcast:locked``).
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime

from .assets import AssetRepository
from .episodes import Episode, EpisodeRepository, tracking_path
from .notes import render_notes
from .shows import SHOWS_PREFIX, Show, ShowRepository
from .storage import Storage

NAMESPACES = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}
for _prefix, _uri in NAMESPACES.items():
    ET.register_namespace(_prefix, _uri)

# Namespace defined by the Podcasting 2.0 spec for <podcast:guid>.
PODCAST_GUID_NAMESPACE = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")

FEED_CONTENT_TYPE = "application/rss+xml; charset=utf-8"
# Short enough that a new episode shows up quickly, long enough to absorb polling.
FEED_CACHE_CONTROL = "public, max-age=300"
GENERATOR = "Bucketcast (https://github.com/ryanthompson591/bucketcast)"


def feed_path(show_id: str) -> str:
    return f"{SHOWS_PREFIX}/{show_id}/feed.xml"


def _q(prefix: str, tag: str) -> str:
    return f"{{{NAMESPACES[prefix]}}}{tag}"


def _add(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _yes_no(flag: bool) -> str:
    return "true" if flag else "false"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def podcast_guid(show: Show) -> str:
    """A permanent ID for the show that survives moving the feed to a new URL."""
    return str(uuid.uuid5(PODCAST_GUID_NAMESPACE, f"bucketcast:{show.id}:{show.created_at}"))


def feed_problems(show: Show) -> list[str]:
    """Things Apple Podcasts requires that this show is still missing."""
    problems = []
    if not show.artwork_path:
        problems.append("Add show artwork (square JPEG or PNG, 1400–3000 px).")
    if not show.description.strip():
        problems.append("Add a show description.")
    if not show.author.strip():
        problems.append("Add an author.")
    if not show.owner_email.strip():
        problems.append("Add an owner email; Apple uses it to verify ownership.")
    return problems


def published_episodes(episodes: list[Episode], now: datetime) -> list[Episode]:
    """Episodes that belong in the feed: not deleted, publish date reached, newest first."""
    live = [e for e in episodes if not e.deleted and e.audio_path and _parse(e.published_at) <= now]
    return sorted(live, key=lambda e: e.published_at, reverse=True)


class FeedBuilder:
    """Builds and publishes ``feed.xml`` for a show.

    ``base_url`` makes site-relative URLs absolute. It only matters for local
    development; Cloud Storage URLs are already absolute.
    """

    def __init__(self, storage: Storage, base_url: str = ""):
        self.storage = storage
        self.base_url = base_url.rstrip("/")
        self.shows = ShowRepository(storage)
        self.episodes = EpisodeRepository(storage)
        self.assets = AssetRepository(storage)

    def absolute(self, url: str) -> str:
        return self.base_url + url if url.startswith("/") else url

    def media_url(self, path: str) -> str:
        return self.absolute(self.storage.public_url(path)) if path else ""

    def feed_url(self, show: Show) -> str:
        """The address to give listeners and directories."""
        if show.domain:
            return f"https://{show.domain}/feed.xml"
        return self.media_url(feed_path(show.id))

    def site_url(self, show: Show) -> str:
        if show.domain:
            return f"https://{show.domain}/"
        return self.media_url(f"{SHOWS_PREFIX}/{show.id}/index.html")

    def enclosure_url(self, show: Show, episode: Episode) -> str:
        """Where podcast apps fetch the audio: the bucket, or the counting redirect."""
        if not show.track_downloads:
            return self.media_url(episode.audio_path)
        base = f"https://{show.domain}" if show.domain else self.base_url
        return base + tracking_path(show.id, episode.id, episode.audio_path)

    def notes_html(self, show_id: str, text: str) -> str:
        return render_notes(text, lambda name: self.absolute(self.assets.url(show_id, name)))

    def build(self, show: Show, episodes: list[Episode], now: datetime | None = None) -> bytes:
        now = now or datetime.now(UTC)
        rss = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(rss, "channel")
        site = self.site_url(show)
        description = show.description.strip() or show.title

        _add(channel, "title", show.title)
        _add(channel, "link", site)
        _add(channel, "description", description)
        _add(channel, "language", show.language)
        _add(channel, "generator", GENERATOR)
        _add(channel, "lastBuildDate", format_datetime(now))
        _add(channel, _q("atom", "link"), href=self.feed_url(show), rel="self",
             type="application/rss+xml")

        if show.author:
            _add(channel, _q("itunes", "author"), show.author)
        _add(channel, _q("itunes", "summary"), description)
        _add(channel, _q("itunes", "type"), "episodic")
        _add(channel, _q("itunes", "explicit"), _yes_no(show.explicit))
        ET.SubElement(channel, _q("itunes", "category"), {"text": show.category})
        if show.owner_email:
            owner = _add(channel, _q("itunes", "owner"))
            _add(owner, _q("itunes", "name"), show.owner_name or show.author or show.title)
            _add(owner, _q("itunes", "email"), show.owner_email)
        if show.artwork_path:
            art = self.media_url(show.artwork_path)
            _add(channel, _q("itunes", "image"), href=art)
            image = _add(channel, "image")
            _add(image, "url", art)
            _add(image, "title", show.title)
            _add(image, "link", site)

        _add(channel, _q("podcast", "guid"), podcast_guid(show))
        if show.owner_email:
            # "yes" asks other hosts not to import this feed without the owner's say-so.
            _add(channel, _q("podcast", "locked"), "yes", owner=show.owner_email)

        for episode in published_episodes(episodes, now):
            self._add_item(channel, show, episode, site)

        ET.indent(rss, space="  ")
        return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"

    def _add_item(self, channel: ET.Element, show: Show, episode: Episode, site: str) -> None:
        item = ET.SubElement(channel, "item")
        notes = self.notes_html(show.id, episode.description)

        _add(item, "title", episode.title)
        _add(item, "guid", episode.guid, isPermaLink="false")
        _add(item, "pubDate", format_datetime(_parse(episode.published_at)))
        _add(item, "link", site)
        _add(item, "enclosure", url=self.enclosure_url(show, episode),
             length=str(episode.audio_bytes), type=episode.audio_type or "audio/mpeg")
        if notes:
            _add(item, "description", notes)
            _add(item, _q("content", "encoded"), notes)

        _add(item, _q("itunes", "title"), episode.title)
        _add(item, _q("itunes", "episodeType"), "full")
        if episode.duration_seconds:
            _add(item, _q("itunes", "duration"), str(episode.duration_seconds))
        _add(item, _q("itunes", "explicit"), _yes_no(episode.explicit))
        if episode.season:
            _add(item, _q("itunes", "season"), str(episode.season))
        if episode.number:
            _add(item, _q("itunes", "episode"), str(episode.number))
        if episode.artwork_path:
            _add(item, _q("itunes", "image"), href=self.media_url(episode.artwork_path))

    def publish(self, show_id: str, now: datetime | None = None) -> str | None:
        """Rebuild and store the show's feed. Returns its URL, or None if unpublished.

        A deleted (or missing) show has its feed removed instead.
        """
        show, _ = self.shows.get(show_id)
        if not show or show.deleted:
            self.storage.delete(feed_path(show_id))
            return None
        xml = self.build(show, self.episodes.list(show_id), now)
        self.storage.write_bytes(feed_path(show_id), xml, FEED_CONTENT_TYPE, public=True,
                                 cache_control=FEED_CACHE_CONTROL)
        return self.feed_url(show)
