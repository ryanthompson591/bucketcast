"""The public show page.

Like the feed, a show's site is static: ``shows/<show-id>/index.html`` plus
``page/<n>/index.html`` for older episodes, rebuilt whenever the show or one
of its episodes changes. The pages work with no JavaScript.

Links between the generated files are relative, so the same files work at
the show's own domain (``/``, ``/page/2/``, ``/feed.xml``) and straight from
the bucket.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from .episodes import Episode
from .feed import FeedBuilder, feed_path, published_episodes
from .shows import SHOWS_PREFIX, Show
from .storage import Storage

SITE_CONTENT_TYPE = "text/html; charset=utf-8"
SITE_CACHE_CONTROL = "public, max-age=300"
EXCERPT_CHARS = 300

_env = Environment(
    loader=PackageLoader("bucketcast", "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_TAG_RE = re.compile(r"<[^>]+>")


def page_path(show_id: str, number: int) -> str:
    base = f"{SHOWS_PREFIX}/{show_id}"
    return f"{base}/index.html" if number == 1 else f"{base}/page/{number}/index.html"


def excerpt(notes_html: str, limit: int = EXCERPT_CHARS) -> str:
    """Plain-text start of rendered notes, cut at a word boundary."""
    text = " ".join(html.unescape(_TAG_RE.sub(" ", notes_html)).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


@dataclass
class EpisodeView:
    title: str
    date: str
    datetime: str
    number: str
    audio_url: str
    audio_type: str
    artwork_url: str
    notes: Markup
    summary: str


class SiteBuilder:
    def __init__(self, storage: Storage, base_url: str = ""):
        self.storage = storage
        self.feed = FeedBuilder(storage, base_url=base_url)

    def _view(self, show: Show, episode: Episode) -> EpisodeView:
        when = datetime.fromisoformat(episode.published_at)
        notes = self.feed.notes_html(show.id, episode.description)
        label = []
        if episode.season:
            label.append(f"Season {episode.season}")
        if episode.number:
            label.append(f"Episode {episode.number}")
        return EpisodeView(
            title=episode.title,
            date=f"{when:%b} {when.day}, {when.year}",
            datetime=episode.published_at,
            number=" · ".join(label),
            audio_url=self.feed.enclosure_url(show, episode),
            audio_type=episode.audio_type or "audio/mpeg",
            artwork_url=self.feed.media_url(episode.artwork_path),
            # Rendered by notes.render_notes, which escapes raw HTML.
            notes=Markup(notes) if show.full_notes else Markup(""),
            summary="" if show.full_notes else excerpt(notes),
        )

    def build(self, show: Show, episodes: list[Episode],
              now: datetime | None = None) -> list[bytes]:
        """One HTML document per page, first page first. Always at least one."""
        live = published_episodes(episodes, now or datetime.now(UTC))
        per_page = max(1, show.episodes_per_page)
        count = max(1, -(-len(live) // per_page))
        template = _env.get_template("site/show.html")
        pages = []
        for number in range(1, count + 1):
            # Relative links: page 1 sits at the show's root, the rest two levels down.
            root = "" if number == 1 else "../../"

            def link(n: int, root: str = root) -> str:
                return f"{root}index.html" if n == 1 else f"{root}page/{n}/index.html"

            chunk = live[(number - 1) * per_page: number * per_page]
            pages.append(template.render(
                show=show,
                description=show.description.strip(),
                artwork_url=self.feed.media_url(show.artwork_path),
                canonical_url=self.feed.site_url(show) if number == 1 else "",
                feed_href=f"{root}feed.xml",
                feed_url=self.feed.feed_url(show),
                home_href=link(1),
                episodes=[self._view(show, e) for e in chunk],
                total=len(live),
                page=number,
                page_count=count,
                newer_href=link(number - 1) if number > 1 else "",
                older_href=link(number + 1) if number < count else "",
                year=(now or datetime.now(UTC)).year,
            ).encode("utf-8"))
        return pages

    def publish(self, show: Show | None, episodes: list[Episode],
                now: datetime | None = None, show_id: str = "") -> int:
        """Write the show's pages and remove ones that no longer exist.

        Returns the number of pages written. A deleted or missing show (pass
        ``show_id``) has all of its pages removed.
        """
        show_id = show.id if show else show_id
        pages = self.build(show, episodes, now) if show and not show.deleted else []
        for number, body in enumerate(pages, start=1):
            self.storage.write_bytes(page_path(show_id, number), body, SITE_CONTENT_TYPE,
                                     public=True, cache_control=SITE_CACHE_CONTROL)
        if not pages:
            self.storage.delete(page_path(show_id, 1))
        for name in self.storage.list_dirs(f"{SHOWS_PREFIX}/{show_id}/page"):
            if not name.isdigit() or int(name) > len(pages):
                self.storage.delete(f"{SHOWS_PREFIX}/{show_id}/page/{name}/index.html")
        return len(pages)


__all__ = ["SiteBuilder", "page_path", "excerpt", "feed_path"]
