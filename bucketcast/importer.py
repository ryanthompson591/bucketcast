"""Import a show from an existing podcast RSS feed.

Reads any standard podcast feed (Libsyn's included), then copies the show's
settings, artwork, episodes, audio and notes into the bucket.

What matters most is that every episode keeps its original GUID. Podcast
apps recognise episodes by GUID, so a preserved GUID means subscribers don't
see the back catalogue as new and download it all again.

The import is safe to re-run: episodes whose GUID is already present are
skipped, and an existing show's settings are left alone.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from .assets import ASSET_TYPES, MAX_ASSET_BYTES, AssetRepository
from .episodes import AUDIO_TYPES, MAX_AUDIO_BYTES, Episode, EpisodeRepository, new_audio_path
from .feed import NAMESPACES, FeedBuilder
from .html2md import html_to_markdown
from .shows import (
    ARTWORK_TYPES,
    CATEGORIES,
    ID_RE,
    LANG_RE,
    MAX_ARTWORK_BYTES,
    Show,
    ShowRepository,
    ValidationError,
)
from .site import SiteBuilder
from .storage import Storage

USER_AGENT = "Bucketcast-Import/0.1 (+https://github.com/ryanthompson591/bucketcast)"
MAX_FEED_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 60

# Enclosure MIME type -> our file extension, for audio whose URL has no usable one.
AUDIO_EXT_BY_TYPE = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
                     "audio/x-m4a": "m4a", "audio/m4a": "m4a", "audio/aac": "m4a"}
IMAGE_EXT_BY_TYPE = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                     "image/webp": "webp"}

ITUNES = "{" + NAMESPACES["itunes"] + "}"
CONTENT = "{" + NAMESPACES["content"] + "}"
ATOM = "{" + NAMESPACES["atom"] + "}"
PODCAST = "{" + NAMESPACES["podcast"] + "}"


class FeedImportError(Exception):
    """The import can't go ahead (bad feed, bad show ID, network failure)."""


# --- fetching ------------------------------------------------------------------

def http_open(url: str) -> BinaryIO:
    """Open ``url`` for reading. Only http(s); follows redirects."""
    if urlparse(url).scheme not in ("http", "https"):
        raise FeedImportError(f"Not an http(s) address: {url}")
    # Feeds sometimes carry addresses with literal spaces, which must be encoded.
    request = urllib.request.Request(url.strip().replace(" ", "%20"),
                                     headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)  # noqa: S310


def _read_limited(stream: BinaryIO, limit: int, what: str) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise FeedImportError(f"{what} is larger than {limit // (1024 * 1024)} MB")
    return data


# --- parsing -------------------------------------------------------------------

@dataclass
class FeedItem:
    title: str
    guid: str
    published_at: str
    notes_html: str = ""
    audio_url: str = ""
    audio_type: str = ""
    audio_bytes: int = 0
    duration_seconds: int = 0
    season: int | None = None
    number: int | None = None
    explicit: bool = False
    image_url: str = ""


@dataclass
class ParsedFeed:
    title: str
    description: str = ""
    author: str = ""
    owner_name: str = ""
    owner_email: str = ""
    categories: list[str] = field(default_factory=list)
    language: str = "en"
    explicit: bool = False
    image_url: str = ""
    self_url: str = ""
    podcast_guid: str = ""
    items: list[FeedItem] = field(default_factory=list)


def _text(el: ET.Element | None, path: str) -> str:
    return ((el.findtext(path) if el is not None else "") or "").strip()


def _flag(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "explicit")


def _int(value: str) -> int | None:
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if n >= 1 else None


def parse_duration(value: str) -> int:
    """``1834``, ``30:34`` or ``1:02:03`` to seconds (0 if unreadable)."""
    parts = value.strip().split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0
    if not 1 <= len(numbers) <= 3 or any(n < 0 for n in numbers):
        return 0
    seconds = 0.0
    for n in numbers:
        seconds = seconds * 60 + n
    return round(seconds)


def parse_date(value: str) -> str:
    """An RSS ``pubDate`` to a UTC ISO string."""
    dt = parsedate_to_datetime(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _fullest_notes(item: ET.Element) -> str:
    """The most complete copy of an episode's notes.

    Feeds carry notes twice, in ``content:encoded`` and ``description``, and
    hosts differ on which one is cut down (one may lack the images, the other
    may be a plain summary). The longer copy is the fuller one.
    """
    copies = [_text(item, f"{CONTENT}encoded"), _text(item, "description")]
    return max(copies, key=len) or _text(item, f"{ITUNES}summary")


def parse_feed(xml: bytes) -> ParsedFeed:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise FeedImportError(f"The feed isn't valid XML: {e}") from e
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedImportError("That address isn't a podcast RSS feed (no <rss><channel>).")

    image = channel.find(f"{ITUNES}image")
    self_link = next((link.get("href", "") for link in channel.findall(f"{ATOM}link")
                      if link.get("rel") == "self"), "")
    feed = ParsedFeed(
        title=_text(channel, "title"),
        description=_text(channel, "description") or _text(channel, f"{ITUNES}summary"),
        author=_text(channel, f"{ITUNES}author"),
        owner_name=_text(channel, f"{ITUNES}owner/{ITUNES}name"),
        owner_email=_text(channel, f"{ITUNES}owner/{ITUNES}email"),
        categories=[c.get("text", "") for c in channel.iter(f"{ITUNES}category")],
        language=_text(channel, "language") or "en",
        explicit=_flag(_text(channel, f"{ITUNES}explicit")),
        image_url=(image.get("href", "") if image is not None else "")
        or _text(channel, "image/url"),
        self_url=self_link,
        podcast_guid=_text(channel, f"{PODCAST}guid"),
    )
    if not feed.title:
        raise FeedImportError("The feed has no title.")

    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        item_image = item.find(f"{ITUNES}image")
        title = _text(item, "title") or _text(item, f"{ITUNES}title")
        audio_url = enclosure.get("url", "") if enclosure is not None else ""
        try:
            published = parse_date(_text(item, "pubDate"))
        except (TypeError, ValueError):
            published = ""
        try:
            length = int(enclosure.get("length", "0")) if enclosure is not None else 0
        except ValueError:
            length = 0
        feed.items.append(FeedItem(
            title=title,
            # A feed without GUIDs is identified by its audio address, as apps do.
            guid=_text(item, "guid") or audio_url,
            published_at=published,
            notes_html=_fullest_notes(item),
            audio_url=audio_url,
            audio_type=(enclosure.get("type", "") if enclosure is not None else "").lower(),
            audio_bytes=length,
            duration_seconds=parse_duration(_text(item, f"{ITUNES}duration")),
            season=_int(_text(item, f"{ITUNES}season")),
            number=_int(_text(item, f"{ITUNES}episode")),
            explicit=_flag(_text(item, f"{ITUNES}explicit")),
            image_url=item_image.get("href", "") if item_image is not None else "",
        ))
    return feed


def _filename(url: str, fallback: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    return name or fallback


def _extension(url: str, content_type: str, by_type: dict[str, str],
               allowed) -> str | None:
    ext = _filename(url, "").rsplit(".", 1)[-1].lower() if "." in _filename(url, "") else ""
    if ext in allowed:
        return ext
    return by_type.get((content_type or "").split(";")[0].strip().lower())


# --- importing -----------------------------------------------------------------

@dataclass
class Report:
    show_created: bool = False
    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (title, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


class Importer:
    """Copies a show from a feed into storage.

    ``opener`` opens a URL and returns a readable binary stream; it exists so
    tests can run without a network.
    """

    def __init__(self, storage: Storage, opener: Callable[[str], BinaryIO] = http_open,
                 base_url: str = "", log: Callable[[str], None] = print):
        self.storage = storage
        self.opener = opener
        self.base_url = base_url
        self.log = log
        self.shows = ShowRepository(storage)
        self.episodes = EpisodeRepository(storage)
        self.assets = AssetRepository(storage)

    # -- downloads --

    def _fetch(self, url: str, limit: int, what: str) -> tuple[bytes, str]:
        with self.opener(url) as stream:
            content_type = ""
            headers = getattr(stream, "headers", None)
            if headers is not None:
                content_type = headers.get("Content-Type", "") or ""
            return _read_limited(stream, limit, what), content_type

    def _copy_audio(self, show_id: str, item: FeedItem) -> tuple[str, int, str]:
        with self.opener(item.audio_url) as stream:
            headers = getattr(stream, "headers", None)
            served_type = (headers.get("Content-Type", "") if headers is not None else "") or ""
            ext = _extension(item.audio_url, item.audio_type or served_type,
                             AUDIO_EXT_BY_TYPE, AUDIO_TYPES)
            if not ext:
                raise FeedImportError(
                    f"unsupported audio format ({item.audio_type or 'unknown type'}); "
                    "only MP3 and M4A can be imported")
            stem = _filename(item.audio_url, "episode").rsplit(".", 1)[0]
            path = new_audio_path(show_id, f"{stem}.{ext}")
            # Spool to disk so a large file is never held in memory.
            with tempfile.TemporaryFile() as spool:
                shutil.copyfileobj(stream, spool, 1024 * 1024)
                size = spool.tell()
                if size == 0:
                    raise FeedImportError("the audio file is empty")
                if size > MAX_AUDIO_BYTES:
                    raise FeedImportError("the audio file is larger than 2 GB")
                spool.seek(0)
                self.storage.write_file(path, spool, AUDIO_TYPES[ext])
        return path, size, AUDIO_TYPES[ext]

    def _import_asset(self, show_id: str, url: str, cache: dict[str, str]) -> str:
        """Copy an image used in notes into the show's assets; return what to link to."""
        if url in cache:
            return cache[url]
        result = url  # leave the original address in place if the copy fails
        try:
            data, content_type = self._fetch(url, MAX_ASSET_BYTES, "An image in the notes")
            ext = _extension(url, content_type, IMAGE_EXT_BY_TYPE, ASSET_TYPES)
            if ext:
                stem = _filename(url, "image").rsplit(".", 1)[0]
                result = "asset:" + self.assets.save(show_id, f"{stem}.{ext}", data).name
        except (OSError, FeedImportError, ValidationError) as e:
            self.log(f"    ! couldn't copy image {url}: {e}")
        cache[url] = result
        return result

    def _artwork(self, url: str, what: str) -> tuple[bytes, str] | None:
        if not url:
            return None
        try:
            data, content_type = self._fetch(url, MAX_ARTWORK_BYTES, what)
        except (OSError, FeedImportError) as e:
            self.log(f"    ! couldn't download {what.lower()}: {e}")
            return None
        ext = _extension(url, content_type, IMAGE_EXT_BY_TYPE, ("jpg", "jpeg", "png"))
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext or "")
        if mime not in ARTWORK_TYPES:
            self.log(f"    ! {what.lower()} isn't a JPEG or PNG; skipped")
            return None
        return data, mime

    # -- show --

    def _new_show(self, show_id: str, feed: ParsedFeed, feed_url: str, report: Report) -> Show:
        category = next((c for c in feed.categories if c in CATEGORIES), None)
        if not category:
            category = "Technology"
            listed = ", ".join(c for c in feed.categories if c) or "none"
            report.warnings.append(
                f"Category set to Technology; the feed's ({listed}) isn't one Bucketcast "
                "knows. Change it in the show's settings.")
        language = feed.language if LANG_RE.match(feed.language) else "en"
        owner_email = feed.owner_email if "@" in feed.owner_email else ""
        show = Show(
            id=show_id, title=feed.title, description=feed.description, author=feed.author,
            owner_name=feed.owner_name, owner_email=owner_email, category=category,
            language=language, explicit=feed.explicit, podcast_guid=feed.podcast_guid,
            # So the new feed announces itself as this show's new home.
            previous_feed_url=feed.self_url or feed_url,
        )
        show.validate()
        return show

    # -- run --

    def run(self, show_id: str, feed_url: str, dry_run: bool = False,
            limit: int | None = None) -> Report:
        report = Report(dry_run=dry_run)
        if not ID_RE.match(show_id or ""):
            raise FeedImportError("Show ID must be lowercase letters, numbers and dashes.")
        try:
            xml, _ = self._fetch(feed_url, MAX_FEED_BYTES, "The feed")
        except OSError as e:
            raise FeedImportError(f"Couldn't download the feed: {e}") from e
        feed = parse_feed(xml)
        self.log(f"Feed: {feed.title} ({len(feed.items)} items)")

        show, generation = self.shows.get(show_id)
        if show and show.deleted:
            raise FeedImportError(f"Show '{show_id}' was deleted. Pick a different show ID.")
        if show:
            self.log(f"Show '{show_id}' already exists; its settings are left as they are.")
        else:
            try:
                show = self._new_show(show_id, feed, feed_url, report)
            except ValidationError as e:
                raise FeedImportError(f"The feed's show details aren't usable: {e}") from e
            report.show_created = True
            if not dry_run:
                art = self._artwork(feed.image_url, "Show artwork")
                if art:
                    show.artwork_path = self.shows.save_artwork(show, *art)
                else:
                    report.warnings.append("No show artwork was imported; add it in settings.")
                self.shows.save(show, None)
            self.log(f"{'Would create' if dry_run else 'Created'} show '{show_id}'.")

        existing = {} if report.show_created and dry_run else {
            e.guid: e for e in self.episodes.list(show_id, include_deleted=True)}
        asset_cache: dict[str, str] = {}
        done = 0
        # Oldest first, so an interrupted import leaves a tidy prefix of the catalogue.
        for item in sorted(feed.items, key=lambda i: i.published_at):
            title = item.title or "(untitled)"
            if not item.audio_url:
                report.skipped.append((title, "no audio file in the feed"))
                continue
            if item.guid in existing:
                report.skipped.append((title, "already imported"))
                continue
            if limit is not None and done >= limit:
                report.skipped.append((title, "over the --limit"))
                continue
            done += 1
            if dry_run:
                report.imported.append(title)
                continue
            self.log(f"  importing: {title}")
            try:
                self._import_item(show_id, item, asset_cache)
                report.imported.append(title)
            except (OSError, FeedImportError, ValidationError, ValueError) as e:
                self.log(f"    ! failed: {e}")
                report.failed.append((title, str(e)))

        if not dry_run:
            feed_builder = FeedBuilder(self.storage, base_url=self.base_url)
            feed_builder.publish(show_id)
            fresh, _ = self.shows.get(show_id)
            SiteBuilder(self.storage, base_url=self.base_url).publish(
                fresh, self.episodes.list(show_id), show_id=show_id)
        return report

    def _import_item(self, show_id: str, item: FeedItem, asset_cache: dict[str, str]) -> None:
        if not item.published_at:
            raise FeedImportError("the episode has no readable publish date")
        episode = Episode(
            title=item.title or "Untitled episode",
            guid=item.guid,
            published_at=item.published_at,
            season=item.season,
            number=item.number,
            explicit=item.explicit,
            duration_seconds=item.duration_seconds,
        )
        episode.description = html_to_markdown(
            item.notes_html, lambda url: self._import_asset(show_id, url, asset_cache))
        episode.audio_path, episode.audio_bytes, episode.audio_type = self._copy_audio(
            show_id, item)
        art = self._artwork(item.image_url, "Episode artwork")
        if art:
            episode.artwork_path = self.episodes.save_artwork(show_id, episode, *art)
        self.episodes.add(show_id, episode)


def format_report(report: Report) -> str:
    verb = "Would import" if report.dry_run else "Imported"
    lines = [f"{verb} {len(report.imported)} episode(s)."]
    lines += [f"  + {title}" for title in report.imported]
    if report.skipped:
        lines.append(f"Skipped {len(report.skipped)}:")
        lines += [f"  - {title}: {reason}" for title, reason in report.skipped]
    if report.failed:
        lines.append(f"FAILED {len(report.failed)}:")
        lines += [f"  ! {title}: {reason}" for title, reason in report.failed]
    lines += [f"Note: {w}" for w in report.warnings]
    if report.dry_run:
        lines.append("Dry run: nothing was downloaded or changed.")
    return "\n".join(lines)


def guids_in(xml: bytes) -> list[str]:
    """Episode GUIDs in a feed, for checking an import kept them all."""
    return [g for g in (re.sub(r"\s+", " ", i.guid).strip() for i in parse_feed(xml).items) if g]


