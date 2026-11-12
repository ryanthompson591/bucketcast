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
import mimetypes
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO


class Conflict(Exception):
    """The object changed (or already exists) since it was read."""


class Storage:
    def read_json(self, path: str) -> tuple[Any, int | None]:
        """Return ``(data, generation)``, or ``(None, None)`` if missing."""
        raise NotImplementedError

    def write_json(self, path: str, data: Any, if_generation_match: int | None = None) -> int:
        """Write JSON; return the new generation."""
        raise NotImplementedError

    def write_bytes(
        self,
        path: str,
        data: bytes,
        content_type: str,
        public: bool = False,
        cache_control: str | None = None,
    ) -> None:
        raise NotImplementedError

    def list_dirs(self, prefix: str) -> list[str]:
        """Names of the immediate "subdirectories" under ``prefix``."""
        raise NotImplementedError

    def public_url(self, path: str) -> str:
        raise NotImplementedError

    def signed_upload_url(self, path: str, content_type: str, expires_seconds: int = 3600) -> str:
        """A URL the browser can ``PUT`` a file to, bypassing the app."""
        raise NotImplementedError

    def object_info(self, path: str) -> tuple[int, str] | None:
        """Return ``(size_in_bytes, content_type)``, or ``None`` if missing."""
        raise NotImplementedError

    def list_files(self, prefix: str) -> list[tuple[str, int]]:
        """``(name, size)`` of the files directly under ``prefix``, sorted by name."""
        raise NotImplementedError

    def read_bytes(self, path: str) -> bytes | None:
        """The object's contents, or ``None`` if missing. For small files only."""
        raise NotImplementedError

    def write_file(self, path: str, fileobj: BinaryIO, content_type: str) -> None:
        """Store a file of any size from an open, readable binary file."""
        raise NotImplementedError

    def delete(self, path: str) -> None:
        """Remove an object. Deleting something that isn't there is not an error."""
        raise NotImplementedError


class LocalStorage(Storage):
    """Filesystem-backed storage for local development and tests."""

    def __init__(
        self,
        root: str | os.PathLike,
        url_prefix: str = "/_local_media/",
        upload_prefix: str = "/_local_upload/",
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.url_prefix = url_prefix
        self.upload_prefix = upload_prefix

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
        # Guarantee the generation moves forward even on very fast successive
        # writes. Filesystems round timestamps differently (Windows keeps 100 ns,
        # some keep far less), so nudge the mtime until the change actually sticks.
        new = self._gen(p)
        step = 100
        while new <= current and step <= 10_000_000_000:
            os.utime(p, ns=(current + step, current + step))
            new = self._gen(p)
            step *= 10
        return new

    def write_bytes(self, path, data, content_type, public=False, cache_control=None):
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

    def signed_upload_url(self, path, content_type, expires_seconds=3600):
        # Not signed: the app's dev-only upload route stands in for the bucket.
        self._p(path)  # reject paths that escape the root
        return self.upload_prefix + path

    def object_info(self, path):
        p = self._p(path)
        if not p.is_file():
            return None
        return p.stat().st_size, mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    def list_files(self, prefix):
        p = self._p(prefix)
        if not p.is_dir():
            return []
        return sorted((c.name, c.stat().st_size) for c in p.iterdir() if c.is_file())

    def delete(self, path):
        self._p(path).unlink(missing_ok=True)

    def read_bytes(self, path):
        p = self._p(path)
        return p.read_bytes() if p.is_file() else None

    def write_file(self, path, fileobj, content_type):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                shutil.copyfileobj(fileobj, f, 1024 * 1024)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def write_stream(self, path: str, stream: BinaryIO, length: int) -> int:
        """Copy ``length`` bytes from ``stream`` into ``path``; return bytes written."""
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        written = 0
        try:
            with os.fdopen(fd, "wb") as f:
                while written < length:
                    chunk = stream.read(min(1024 * 1024, length - written))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            if written != length:
                raise OSError(f"upload ended early: got {written} of {length} bytes")
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return written

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

    def write_bytes(self, path, data, content_type, public=False, cache_control=None):
        # Public access is granted once on the bucket's shows/ folder (see
        # docs/DEPLOY.md), not per object, so `public` is informational here.
        blob = self._bucket.blob(path)
        if cache_control:
            blob.cache_control = cache_control
        blob.upload_from_string(data, content_type=content_type)

    def list_dirs(self, prefix):
        prefix = prefix.rstrip("/") + "/"
        it = self._client.list_blobs(self._bucket, prefix=prefix, delimiter="/")
        for _ in it:  # prefixes are populated only after iterating
            pass
        return sorted(p[len(prefix):].rstrip("/") for p in it.prefixes)

    def public_url(self, path):
        return f"https://storage.googleapis.com/{self.bucket_name}/{path}"

    def signed_upload_url(self, path, content_type, expires_seconds=3600):
        import google.auth
        from google.auth import credentials as ga_credentials
        from google.auth.transport import requests as ga_requests

        kwargs = {}
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not isinstance(creds, ga_credentials.Signing):
            # App Engine's default credentials have no private key, so ask the
            # IAM signBlob API to sign instead. The service account needs the
            # "Service Account Token Creator" role on itself (see README).
            creds.refresh(ga_requests.Request())
            kwargs = {
                "service_account_email": creds.service_account_email,
                "access_token": creds.token,
            }
        return self._bucket.blob(path).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_seconds),
            method="PUT",
            content_type=content_type,
            **kwargs,
        )

    def object_info(self, path):
        blob = self._bucket.get_blob(path)
        if blob is None:
            return None
        return blob.size, blob.content_type or "application/octet-stream"

    def list_files(self, prefix):
        prefix = prefix.rstrip("/") + "/"
        blobs = self._client.list_blobs(self._bucket, prefix=prefix, delimiter="/")
        return sorted((b.name[len(prefix):], b.size) for b in blobs if b.name != prefix)

    def delete(self, path):
        from google.api_core.exceptions import NotFound

        try:
            self._bucket.blob(path).delete()
        except NotFound:
            pass

    def read_bytes(self, path):
        from google.api_core.exceptions import NotFound

        try:
            return self._bucket.blob(path).download_as_bytes()
        except NotFound:
            return None

    def write_file(self, path, fileobj, content_type):
        self._bucket.blob(path).upload_from_file(fileobj, content_type=content_type, rewind=True)


def storage_from_config(config: dict) -> Storage:
    if config.get("BUCKET"):
        return GCSStorage(config["BUCKET"])
    return LocalStorage(config.get("LOCAL_STORAGE_DIR", ".local-bucket"))
