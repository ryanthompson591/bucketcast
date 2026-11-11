"""Bucketcast: a very lightweight, self-hosted podcast host."""

from __future__ import annotations

import os
import re

from flask import Flask, abort, redirect, request, send_file, url_for

from .assets import AssetRepository
from .auth import require_admin
from .episodes import EpisodeRepository
from .shows import ShowRepository
from .stats import stats_from_config
from .storage import LocalStorage, storage_from_config

LOCAL_UPLOAD_RE = re.compile(r"^shows/[a-z0-9-]+/audio/[A-Za-z0-9._-]+$")


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    return {
        "BUCKET": os.environ.get("BUCKETCAST_BUCKET", ""),
        "LOCAL_STORAGE_DIR": os.environ.get("BUCKETCAST_LOCAL_DIR", ".local-bucket"),
        "IAP_AUDIENCE": os.environ.get("BUCKETCAST_IAP_AUDIENCE", ""),
        "ADMIN_EMAILS": [
            e.strip().lower()
            for e in os.environ.get("BUCKETCAST_ADMIN_EMAILS", "").split(",")
            if e.strip()
        ],
        "DEV_AUTH_BYPASS": _env_bool("BUCKETCAST_DEV_AUTH_BYPASS"),
        # Which half of the app this process serves: "admin", "public", or
        # "all" (both, for local development). See create_app.
        "ROLE": os.environ.get("BUCKETCAST_ROLE", "all").strip().lower() or "all",
        # Only needed for local development, where media URLs are site-relative.
        "PUBLIC_BASE_URL": os.environ.get("BUCKETCAST_PUBLIC_BASE_URL", ""),
        # Download stats: the BigQuery table the log sink writes to
        # (project.dataset.table). Unset means a local file, for development.
        "STATS_BQ_TABLE": os.environ.get("BUCKETCAST_STATS_BQ_TABLE", ""),
        "STATS_LOCAL_DIR": os.environ.get("BUCKETCAST_STATS_LOCAL_DIR", ".local-stats"),
        # Artwork uploads go through the app (audio never does).
        "MAX_CONTENT_LENGTH": 12 * 1024 * 1024,
    }


ROLES = ("all", "admin", "public")


def create_app(config: dict | None = None, storage=None, stats=None) -> Flask:
    """Build the app.

    In production it runs as two App Engine services from the same code. The
    ``public`` service answers listeners and podcast apps and contains no
    admin routes at all. The ``admin`` service sits behind Identity-Aware
    Proxy and contains no public ones. Locally, ``all`` serves both.
    """
    app = Flask(__name__)
    app.config.update(load_config())
    if config:
        app.config.update(config)
    role = app.config.get("ROLE", "all")
    if role not in ROLES:
        raise ValueError(f"BUCKETCAST_ROLE must be one of {', '.join(ROLES)}; got {role!r}")

    app.extensions["bucketcast.storage"] = storage or storage_from_config(app.config)
    app.extensions["bucketcast.shows"] = ShowRepository(app.extensions["bucketcast.storage"])
    app.extensions["bucketcast.episodes"] = EpisodeRepository(
        app.extensions["bucketcast.storage"]
    )
    app.extensions["bucketcast.assets"] = AssetRepository(app.extensions["bucketcast.storage"])

    app.extensions["bucketcast.stats"] = stats or stats_from_config(app.config)

    from .admin import bp as admin_bp
    from .public import bp as public_bp

    if role in ("all", "admin"):
        app.register_blueprint(admin_bp)
    if role in ("all", "public"):
        app.register_blueprint(public_bp)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    if role == "admin":
        # The public "/" isn't registered here, so send visitors to the admin.
        @app.get("/")
        def root():
            return redirect(url_for("admin.index"))

    st = app.extensions["bucketcast.storage"]
    if isinstance(st, LocalStorage):
        # Dev only: serve "bucket" files that would be public in production.
        @app.get("/_local_media/<path:path>")
        def local_media(path):
            p = st.path_for(path)
            if not p.is_file():
                abort(404)
            return send_file(p, conditional=True)

        # Dev only: stands in for the bucket's signed upload URL. In production
        # the browser PUTs audio straight to Cloud Storage and never hits the app.
        @app.put("/_local_upload/<path:path>")
        def local_upload(path):
            require_admin()
            if not LOCAL_UPLOAD_RE.match(path):
                abort(403, "Uploads are only accepted into a show's audio folder.")
            length = request.content_length
            if length is None:
                abort(411)
            # Read the raw body so the artwork-sized MAX_CONTENT_LENGTH doesn't apply.
            st.write_stream(path, request.environ["wsgi.input"], length)
            return "", 200

    return app
