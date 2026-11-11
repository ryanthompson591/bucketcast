"""Episode model and persistence.

A show's episodes live together in ``shows/<show-id>/episodes.json``. The
audio itself is uploaded straight from the browser to the bucket under
``shows/<show-id>/audio/`` and is only referenced from here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime

from .shows import ARTWORK_TYPES, MAX_ARTWORK_BYTES, SHOWS_PREFIX, ValidationError
from .storage import Conflict, Storage

# File extension -> the MIME type published in the feed's <enclosure>.
AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/x-m4a"}
MAX_AUDIO_BYTES = 2 * 1024 * 1024 * 1024

EPISODE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_datetime(value: str) -> str:
    """Normalise an ISO date/time to a UTC ISO string. Naive values are UTC."""
    v = (value or "").strip()
    if not v:
        raise ValueError("empty date")
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def audio_prefix(show_id: str) -> str:
    return f"{SHOWS_PREFIX}/{show_id}/audio/"


def audio_type_for(filename: str) -> str | None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return AUDIO_TYPES.get(ext)


def new_audio_path(show_id: str, filename: str) -> str:
    """A unique, URL-safe object name for an uploaded audio file."""
    stem, _, ext = filename.rpartition(".")
    slug = _SLUG_STRIP_RE.sub("-", stem.lower()).strip("-")[:60] or "episode"
    return f"{audio_prefix(show_id)}{uuid.uuid4().hex[:12]}-{slug}.{ext.lower()}"


@dataclass
class Episode:
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # The feed <guid>. Never changes once published; imports keep the old one.
    guid: str = ""
    description: str = ""
    published_at: str = field(default_factory=_now)
    season: int | None = None
    number: int | None = None
    explicit: bool = False
    audio_path: str = ""
    audio_bytes: int = 0
    audio_type: str = ""
    duration_seconds: int = 0
    # Optional per-episode image; apps fall back to the show's artwork.
    artwork_path: str = ""
    deleted: bool = False
    # Bumped on every save; edit forms send it back to detect stale edits.
    revision: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self):
        if not self.guid:
            self.guid = str(uuid.UUID(self.id)) if EPISODE_ID_RE.match(self.id) else self.id

    @classmethod
    def from_dict(cls, d: dict) -> Episode:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, show_id: str) -> None:
        errors = {}
        if not self.title.strip():
            errors["title"] = "Title is required."
        try:
            self.published_at = parse_datetime(self.published_at)
        except ValueError:
            errors["published_at"] = "Not a valid date and time."
        for name, label in (("season", "Season"), ("number", "Episode number")):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 1):
                errors[name] = f"{label} must be a whole number, 1 or higher."
        if not self.audio_path:
            errors["audio"] = "Choose an audio file to upload."
        elif not self.audio_path.startswith(audio_prefix(show_id)) or ".." in self.audio_path:
            errors["audio"] = "The audio file isn't in this show's folder."
        if self.duration_seconds < 0:
            errors["duration_seconds"] = "Duration can't be negative."
        if errors:
            raise ValidationError(errors)


class EpisodeRepository:
    """Reads and writes ``episodes.json``.

    The whole file shares one generation, so writes are read-modify-write with
    a retry. Edits are additionally guarded by the episode's ``revision``:
    a stale edit raises ``Conflict`` instead of overwriting someone's change.
    """

    RETRIES = 4

    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _path(show_id: str) -> str:
        return f"{SHOWS_PREFIX}/{show_id}/episodes.json"

    def _load(self, show_id: str) -> tuple[list[Episode], int | None]:
        data, gen = self.storage.read_json(self._path(show_id))
        items = (data or {}).get("episodes", [])
        return [Episode.from_dict(d) for d in items], gen

    def _store(self, show_id: str, episodes: list[Episode], gen: int | None) -> None:
        self.storage.write_json(
            self._path(show_id),
            {"episodes": [e.to_dict() for e in episodes]},
            if_generation_match=0 if gen is None else gen,
        )

    def list(self, show_id: str, include_deleted: bool = False) -> list[Episode]:
        episodes, _ = self._load(show_id)
        visible = [e for e in episodes if include_deleted or not e.deleted]
        return sorted(visible, key=lambda e: e.published_at, reverse=True)

    def get(self, show_id: str, episode_id: str) -> Episode | None:
        for e in self._load(show_id)[0]:
            if e.id == episode_id:
                return e
        return None

    def save_artwork(self, show_id: str, episode: Episode, data: bytes, content_type: str) -> str:
        """Store an episode image and return its path (the caller saves the episode)."""
        ext = ARTWORK_TYPES.get(content_type)
        if not ext:
            raise ValidationError({"artwork": "Artwork must be a JPEG or PNG image."})
        if len(data) > MAX_ARTWORK_BYTES:
            raise ValidationError({"artwork": "Artwork must be 10 MB or smaller."})
        # Timestamped filename so podcast apps and CDNs pick up changes.
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        path = f"{SHOWS_PREFIX}/{show_id}/artwork/episodes/{episode.id}-{stamp}.{ext}"
        self.storage.write_bytes(path, data, content_type, public=True)
        return path

    def add(self, show_id: str, episode: Episode) -> None:
        episode.validate(show_id)
        episode.updated_at = _now()

        def change(episodes: list[Episode]) -> None:
            if any(e.id == episode.id for e in episodes):
                raise Conflict(episode.id)
            episodes.append(episode)

        self._modify(show_id, change)

    def update(self, show_id: str, episode: Episode, expected_revision: int) -> None:
        """Replace an episode, failing with ``Conflict`` if it changed since read."""
        episode.validate(show_id)
        episode.updated_at = _now()
        episode.revision = expected_revision + 1

        def change(episodes: list[Episode]) -> None:
            for i, current in enumerate(episodes):
                if current.id == episode.id:
                    if current.revision != expected_revision:
                        raise Conflict(episode.id)
                    episodes[i] = episode
                    return
            raise Conflict(episode.id)

        self._modify(show_id, change)

    def _modify(self, show_id: str, change) -> None:
        for attempt in range(self.RETRIES):
            episodes, gen = self._load(show_id)
            change(episodes)  # may raise Conflict for a genuinely stale edit
            try:
                self._store(show_id, episodes, gen)
                return
            except Conflict:
                # Someone else wrote the file between our read and write.
                if attempt == self.RETRIES - 1:
                    raise
