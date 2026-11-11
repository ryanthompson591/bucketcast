"""Public endpoints: the ones listeners and podcast apps reach, with no login.

For now that is only the download-tracking redirect. It never serves audio:
it records the request and sends the app on to the file in the bucket.
"""

from __future__ import annotations

import time

from flask import Blueprint, abort, current_app, redirect, request

from .episodes import AUDIO_TYPES, EPISODE_ID_RE
from .shows import ID_RE
from .stats import DownloadEvent, hash_ip, load_salt

bp = Blueprint("public", __name__)

# Downloads come in bursts, so remember each show's episode -> audio file map
# briefly instead of reading episodes.json from the bucket on every request.
EPISODE_CACHE_SECONDS = 60


def tracking_path(show_id: str, episode_id: str, audio_path: str) -> str:
    """The app-relative URL that counts a download of this episode."""
    ext = audio_path.rsplit(".", 1)[-1].lower() if "." in audio_path else "mp3"
    return f"/e/{show_id}/{episode_id}.{ext if ext in AUDIO_TYPES else 'mp3'}"


EPISODE_CACHE_MIN_SECONDS = 5


def _audio_paths(show_id: str, episode_id: str) -> dict[str, str]:
    cache = current_app.extensions.setdefault("bucketcast.audio_cache", {})
    hit = cache.get(show_id)
    now = time.monotonic()
    if hit:
        age = now - hit[0]
        # An episode we don't know yet may have just been published, so look
        # again sooner, but not on every request for a made-up ID.
        limit = EPISODE_CACHE_SECONDS if episode_id in hit[1] else EPISODE_CACHE_MIN_SECONDS
        if age < limit:
            return hit[1]
    show, _ = current_app.extensions["bucketcast.shows"].get(show_id)
    paths = {}
    if show and not show.deleted:
        episodes = current_app.extensions["bucketcast.episodes"].list(show_id)
        paths = {e.id: e.audio_path for e in episodes if e.audio_path}
    cache[show_id] = (now, paths)
    return paths


def _salt() -> str:
    ext = current_app.extensions
    if "bucketcast.stats_salt" not in ext:
        ext["bucketcast.stats_salt"] = load_salt(ext["bucketcast.storage"])
    return ext["bucketcast.stats_salt"]


def _client_ip() -> str:
    # App Engine puts the real client first in X-Forwarded-For.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or ""


@bp.get("/e/<show_id>/<episode_id>.<ext>")
def track_download(show_id, episode_id, ext):
    if not ID_RE.match(show_id) or not EPISODE_ID_RE.match(episode_id):
        abort(404)
    audio_path = _audio_paths(show_id, episode_id).get(episode_id)
    if not audio_path:
        abort(404)

    try:
        current_app.extensions["bucketcast.stats"].record(DownloadEvent(
            show_id=show_id,
            episode_id=episode_id,
            ip_hash=hash_ip(_client_ip(), _salt()),
            user_agent=request.headers.get("User-Agent", "")[:300],
            method=request.method,
            byte_range=request.headers.get("Range", "")[:100],
        ))
    except Exception:
        # Counting must never get in the way of the download itself.
        current_app.logger.exception("Could not record a download for %s/%s",
                                     show_id, episode_id)

    response = redirect(current_app.extensions["bucketcast.storage"].public_url(audio_path), 302)
    response.headers["Cache-Control"] = "no-store"
    return response
