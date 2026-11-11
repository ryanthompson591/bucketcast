"""Show assets: images uploaded once and used inside episode notes.

Assets live under ``shows/<show-id>/assets/``. There is no index file: the
bucket listing is the library. Notes refer to an asset by name
(``![alt](asset:robots.jpg)``), so they keep working if the bucket or the
public URL changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .shows import SHOWS_PREFIX, ValidationError
from .storage import Storage

# Extension -> content type. The extension decides how the file is served.
ASSET_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
MAX_ASSET_BYTES = 10 * 1024 * 1024

ASSET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}\.(?:jpg|jpeg|png|gif|webp)$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Asset:
    name: str
    size: int
    url: str

    @property
    def markdown(self) -> str:
        """What to paste into episode notes to show this image."""
        alt = self.name.rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
        return f"![{alt}](asset:{self.name})"

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "url": self.url,
                "markdown": self.markdown}


def asset_prefix(show_id: str) -> str:
    return f"{SHOWS_PREFIX}/{show_id}/assets/"


def is_asset_name(name: str) -> bool:
    return bool(ASSET_NAME_RE.match(name or ""))


class AssetRepository:
    def __init__(self, storage: Storage):
        self.storage = storage

    def path(self, show_id: str, name: str) -> str:
        return asset_prefix(show_id) + name

    def url(self, show_id: str, name: str) -> str:
        """Public URL for an asset, or ``""`` if the name isn't a valid one."""
        return self.storage.public_url(self.path(show_id, name)) if is_asset_name(name) else ""

    def list(self, show_id: str) -> list[Asset]:
        return [
            Asset(name, size, self.url(show_id, name))
            for name, size in self.storage.list_files(asset_prefix(show_id))
            if is_asset_name(name)
        ]

    def save(self, show_id: str, filename: str, data: bytes) -> Asset:
        """Store an uploaded image under a tidy, unused name."""
        stem, _, ext = (filename or "").rpartition(".")
        ext = ext.lower()
        if ext not in ASSET_TYPES:
            raise ValidationError({"file": "Images must be JPEG, PNG, GIF or WebP."})
        if not data:
            raise ValidationError({"file": "That file is empty."})
        if len(data) > MAX_ASSET_BYTES:
            raise ValidationError({"file": "Images must be 10 MB or smaller."})

        slug = _SLUG_STRIP_RE.sub("-", stem.lower()).strip("-_")[:60] or "image"
        name, n = f"{slug}.{ext}", 1
        while self.storage.object_info(self.path(show_id, name)) is not None:
            n += 1
            name = f"{slug}-{n}.{ext}"

        self.storage.write_bytes(self.path(show_id, name), data, ASSET_TYPES[ext], public=True)
        return Asset(name, len(data), self.url(show_id, name))

    def delete(self, show_id: str, name: str) -> bool:
        if not is_asset_name(name):
            return False
        path = self.path(show_id, name)
        if self.storage.object_info(path) is None:
            return False
        self.storage.delete(path)
        return True
