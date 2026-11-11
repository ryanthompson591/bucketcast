"""Storage backends.

All bucket access goes through a ``Storage`` object so the rest of the app
never talks to Google Cloud Storage directly. Two backends are provided:

* ``GCSStorage``: production, backed by a Cloud Storage bucket.
* ``LocalStorage``: development and tests, backed by a local directory.

JSON writes support optimistic concurrency: ``read_json`` returns a
*generation* token, and passing it back to ``write_json`` makes the write fail
with ``Conflict`` if someone else changed the object in the meantime.
Passing ``if_generation_match=0`` means "only create; fail if it exists".
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class Conflict(Exception):
    """The object changed (or already exists) since it was read."""


class Storage:
    def read_json(self, path: str) -> tuple[Any, int | None]:
        """Return ``(data, generation)``, or ``(None, None)`` if missing."""
        raise NotImplementedError

    def write_json(self, path: str, data: Any, if_generation_match: int | None = None) -> int:
        """Write JSON; return the new generation."""
        raise NotImplementedError

    def write_bytes(self, path: str, data: bytes, content_type: str, public: bool = False) -> None:
        raise NotImplementedError

    def list_dirs(self, prefix: str) -> list[str]:
        """Names of the immediate "subdirectories" under ``prefix``."""
        raise NotImplementedError

    def public_url(self, path: str) -> str:
        raise NotImplementedError


class LocalStorage(Storage):
    """Filesystem-backed storage for local development and tests."""

    def __init__(self, root: str | os.PathLike, url_prefix: str = "/_local_media/"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.url_prefix = url_prefix

    def _p(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"path escapes storage root: {path}")
        return p

    @staticmethod
    def _gen(p: Path) -> int:
        return p.stat().st_mtime_ns

    def read_json(self, path):
        p = self._p(path)
        if not p.exists():
            return None, None
        return json.loads(p.read_text("utf-8")), self._gen(p)

    def write_json(self, path, data, if_generation_match=None):
        p = self._p(path)
        current = self._gen(p) if p.exists() else 0
        if if_generation_match is not None and current != if_generation_match:
            raise Conflict(path)
        self._atomic_write(p, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
        # Guarantee the generation changes even on very fast successive writes.
        new = self._gen(p)
        if new == current:
            os.utime(p, ns=(new + 1, new + 1))
            new += 1
        return new

    def write_bytes(self, path, data, content_type, public=False):
        self._atomic_write(self._p(path), data)

    def list_dirs(self, prefix):
        p = self._p(prefix)
        if not p.is_dir():
            return []
        return sorted(c.name for c in p.iterdir() if c.is_dir())

    def public_url(self, path):
        return self.url_prefix + path

    def path_for(self, path: str) -> Path:
        return self._p(path)

    @staticmethod
    def _atomic_write(p: Path, data: bytes) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)


class GCSStorage(Storage):
    """Google Cloud Storage backend."""

    def __init__(self, bucket_name: str):
        from google.cloud import storage  # imported lazily so dev/tests don't need it

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self.bucket_name = bucket_name

    def read_json(self, path):
        from google.api_core.exceptions import NotFound

        blob = self._bucket.blob(path)
        try:
            raw = blob.download_as_bytes()
        except NotFound:
            return None, None
        # download_as_bytes() reloads metadata, so blob.generation now matches
        # the content we just read.
        return json.loads(raw), blob.generation

    def write_json(self, path, data, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket.blob(path)
        blob.cache_control = "no-cache"
        try:
            blob.upload_from_string(
                json.dumps(data, indent=2, sort_keys=True),
                content_type="application/json",
                if_generation_match=if_generation_match,
            )
        except PreconditionFailed as e:
            raise Conflict(path) from e
        return blob.generation

    def write_bytes(self, path, data, content_type, public=False):
        # Public access is granted at the bucket level (see deployment guide),
        # not per object, so `public` is informational here.
        blob = self._bucket.blob(path)
        blob.upload_from_string(data, content_type=content_type)

    def list_dirs(self, prefix):
        prefix = prefix.rstrip("/") + "/"
        it = self._client.list_blobs(self._bucket, prefix=prefix, delimiter="/")
        for _ in it:  # prefixes are populated only after iterating
            pass
        return sorted(p[len(prefix):].rstrip("/") for p in it.prefixes)

    def public_url(self, path):
        return f"https://storage.googleapis.com/{self.bucket_name}/{path}"


def storage_from_config(config: dict) -> Storage:
    if config.get("BUCKET"):
        return GCSStorage(config["BUCKET"])
    return LocalStorage(config.get("LOCAL_STORAGE_DIR", ".local-bucket"))
