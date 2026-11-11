"""Bucketcast: a very lightweight, self-hosted podcast host."""

from __future__ import annotations

import os

from flask import Flask, abort, send_file

from .shows import ShowRepository
from .storage import LocalStorage, storage_from_config


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
        # Artwork uploads go through the app (audio never does).
        "MAX_CONTENT_LENGTH": 12 * 1024 * 1024,
    }


def create_app(config: dict | None = None, storage=None) -> Flask:
    app = Flask(__name__)
    app.config.update(load_config())
    if config:
        app.config.update(config)

    app.extensions["bucketcast.storage"] = storage or storage_from_config(app.config)
    app.extensions["bucketcast.shows"] = ShowRepository(app.extensions["bucketcast.storage"])

    from .admin import bp as admin_bp

    app.register_blueprint(admin_bp)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    st = app.extensions["bucketcast.storage"]
    if isinstance(st, LocalStorage):
        # Dev only: serve "bucket" files that would be public in production.
        @app.get("/_local_media/<path:path>")
        def local_media(path):
            p = st.path_for(path)
            if not p.is_file():
                abort(404)
            return send_file(p)

    return app
