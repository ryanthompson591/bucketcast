"""Public endpoints: the ones listeners and podcast apps reach, with no login.

* **Show sites by domain.** Many custom domains point at this one app. The
  request's ``Host`` header picks the show, and its page and feed are served
  from the static files in the bucket (``/``, ``/page/<n>/``, ``/feed.xml``).
* **Download counting.** ``/e/<show>/<episode>.<ext>`` records the request
  and redirects to the audio file. It never serves audio itself.
"""

from __future__ import annotations

import time

from flask import Blueprint, Response, abort, current_app, redirect, request

from .episodes import EPISODE_ID_RE
from .feed import FEED_CACHE_CONTROL, FEED_CONTENT_TYPE, feed_path
from .shows import ID_RE
from .site import SITE_CACHE_CONTROL, SITE_CONTENT_TYPE, page_path
from .stats import DownloadEvent, hash_ip, load_salt

bp = Blueprint("public", __name__)

# Downloads come in bursts, so remember each show's episode -> audio file map
# briefly instead of reading episodes.json from the bucket on every request.
EPISODE_CACHE_SECONDS = 60


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


# --- show sites by domain ----------------------------------------------------

# How long the domain -> show map is kept before show.json files are re-read.
DOMAIN_CACHE_SECONDS = 60

NO_SHOW_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>No podcast here</title>
<style>body{font:17px/1.6 system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 20px;
color:#1d1d1f}p{color:#6e6e73}</style></head>
<body><h1>No podcast here</h1>
<p>No show is set up at this address. If you followed a link, it may be out of date.</p>
</body></html>
"""


def normalize_host(host: str) -> str:
    """``Host`` header to a bare lowercase domain: no port, trailing dot or ``www.``."""
    host = (host or "").strip().lower()
    if host.startswith("["):  # IPv6 literal
        return host
    host = host.rsplit(":", 1)[0].rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _domain_map() -> dict[str, str]:
    ext = current_app.extensions
    hit = ext.get("bucketcast.domain_cache")
    now = time.monotonic()
    if hit and now - hit[0] < DOMAIN_CACHE_SECONDS:
        return hit[1]
    domains = {normalize_host(s.domain): s.id
               for s in ext["bucketcast.shows"].list() if s.domain}
    ext["bucketcast.domain_cache"] = (now, domains)
    return domains


def show_for_host(host: str) -> str | None:
    """The ID of the show whose custom domain matches this ``Host``, if any."""
    return _domain_map().get(normalize_host(host))


def _serve(path_for_show, content_type: str, cache_control: str) -> Response:
    show_id = show_for_host(request.host)
    body = None
    if show_id:
        body = current_app.extensions["bucketcast.storage"].read_bytes(path_for_show(show_id))
    if body is None:
        return Response(NO_SHOW_PAGE, status=404, content_type=SITE_CONTENT_TYPE)
    return Response(body, content_type=content_type, headers={"Cache-Control": cache_control})


@bp.get("/")
@bp.get("/index.html")
def site_home():
    return _serve(lambda show_id: page_path(show_id, 1), SITE_CONTENT_TYPE, SITE_CACHE_CONTROL)


@bp.get("/page/<int:number>/")
@bp.get("/page/<int:number>/index.html")
def site_page(number):
    if number < 1:
        abort(404)
    return _serve(lambda show_id: page_path(show_id, number), SITE_CONTENT_TYPE,
                  SITE_CACHE_CONTROL)


@bp.get("/feed.xml")
def site_feed():
    return _serve(feed_path, FEED_CONTENT_TYPE, FEED_CACHE_CONTROL)


# --- download counting -------------------------------------------------------

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
