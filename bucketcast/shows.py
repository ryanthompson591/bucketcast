"""Show model and persistence.

Each show lives under ``shows/<show-id>/`` in the bucket, with its settings
in ``show.json``. Episodes, the feed and pages come in later issues.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime

from .storage import Storage

SHOWS_PREFIX = "shows"

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")

# Apple Podcasts top-level categories.
CATEGORIES = [
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government",
    "Health & Fitness", "History", "Kids & Family", "Leisure", "Music", "News",
    "Religion & Spirituality", "Science", "Society & Culture", "Sports",
    "Technology", "True Crime", "TV & Film",
]

ARTWORK_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
MAX_ARTWORK_BYTES = 10 * 1024 * 1024


class ValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__(", ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass
class Show:
    id: str
    title: str
    description: str = ""
    author: str = ""
    owner_name: str = ""
    owner_email: str = ""
    category: str = "Technology"
    language: str = "en"
    explicit: bool = False
    domain: str = ""
    artwork_path: str = ""
    deleted: bool = False
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())

    @classmethod
    def from_dict(cls, d: dict) -> Show:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        errors = {}
        if not ID_RE.match(self.id or ""):
            errors["id"] = ("Use 1–63 lowercase letters, numbers and dashes "
                            "(no leading/trailing dash).")
        if not self.title.strip():
            errors["title"] = "Title is required."
        if self.owner_email and not EMAIL_RE.match(self.owner_email):
            errors["owner_email"] = "Not a valid email address."
        if self.category not in CATEGORIES:
            errors["category"] = "Pick a category from the list."
        if not LANG_RE.match(self.language or ""):
            errors["language"] = "Use a language code like en or en-US."
        if self.domain and not DOMAIN_RE.match(self.domain):
            errors["domain"] = "Not a valid domain name (e.g. mypodcast.com)."
        if errors:
            raise ValidationError(errors)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0]
    return v.rstrip(".")


class ShowRepository:
    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _path(show_id: str) -> str:
        return f"{SHOWS_PREFIX}/{show_id}/show.json"

    def get(self, show_id: str) -> tuple[Show | None, int | None]:
        if not ID_RE.match(show_id or ""):
            return None, None
        data, gen = self.storage.read_json(self._path(show_id))
        return (Show.from_dict(data) if data else None), gen

    def list(self, include_deleted: bool = False) -> list[Show]:
        shows = []
        for show_id in self.storage.list_dirs(SHOWS_PREFIX):
            show, _ = self.get(show_id)
            if show and (include_deleted or not show.deleted):
                shows.append(show)
        return sorted(shows, key=lambda s: s.title.lower())

    def save(self, show: Show, generation: int | None) -> int:
        """Create (generation=None) or update (generation from ``get``)."""
        show.domain = normalize_domain(show.domain)
        show.validate()
        if show.domain:
            for other in self.list():
                if other.id != show.id and other.domain == show.domain:
                    raise ValidationError({"domain": f"Already used by “{other.title}”."})
        show.updated_at = _now()
        # generation=None means create: the object must not already exist.
        match = 0 if generation is None else generation
        return self.storage.write_json(
            self._path(show.id), show.to_dict(), if_generation_match=match
        )

    def save_artwork(self, show: Show, data: bytes, content_type: str) -> str:
        ext = ARTWORK_TYPES.get(content_type)
        if not ext:
            raise ValidationError({"artwork": "Artwork must be a JPEG or PNG image."})
        if len(data) > MAX_ARTWORK_BYTES:
            raise ValidationError({"artwork": "Artwork must be 10 MB or smaller."})
        # Timestamped filename so podcast apps and CDNs pick up changes.
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        path = f"{SHOWS_PREFIX}/{show.id}/artwork/cover-{stamp}.{ext}"
        self.storage.write_bytes(path, data, content_type, public=True)
        return path
