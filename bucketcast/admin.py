"""Admin UI: list, create, edit and delete shows and their episodes."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    url_for,
)

from .assets import AssetRepository
from .auth import require_admin
from .episodes import (
    MAX_AUDIO_BYTES,
    Episode,
    EpisodeRepository,
    audio_prefix,
    audio_type_for,
    new_audio_path,
)
from .feed import FeedBuilder, feed_path, feed_problems
from .notes import render_notes
from .shows import CATEGORIES, Show, ShowRepository, ValidationError, normalize_domain
from .site import SiteBuilder, page_path
from .stats import StatsStore, since_for, summarize
from .storage import Conflict

bp = Blueprint("admin", __name__, url_prefix="/admin")

FIELDS = ["title", "description", "author", "owner_name", "owner_email",
          "category", "language", "domain"]


def repo() -> ShowRepository:
    return current_app.extensions["bucketcast.shows"]


def storage():
    return current_app.extensions["bucketcast.storage"]


@bp.before_request
def _guard():
    require_admin()
    if request.method == "POST":
        _check_same_origin()


def _check_same_origin() -> None:
    """Lightweight CSRF protection: refuse cross-site form posts."""
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403, "Cross-site request refused.")
    origin = request.headers.get("Origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.host:
        abort(403, "Cross-origin request refused.")


@bp.app_template_global()
def artwork_url(show: Show) -> str:
    return storage().public_url(show.artwork_path) if show.artwork_path else ""


@bp.get("/")
def index():
    return render_template("admin/shows.html", shows=repo().list())


@bp.route("/shows/new", methods=["GET", "POST"])
def new_show():
    show = Show(id="", title="")
    errors = {}
    if request.method == "POST":
        show.id = request.form.get("id", "").strip().lower()
        errors = _apply_form(show)
        if not errors:
            try:
                _save(show, generation=None)
                return redirect(url_for(
                    ".index", notice=f"Created “{show.title}”." + _publish(show.id)))
            except ValidationError as e:
                errors = e.errors
            except Conflict:
                errors = {"id": "A show with this ID already exists."}
    return render_template("admin/show_form.html", show=show, errors=errors,
                           categories=CATEGORIES, is_new=True, generation="")


@bp.route("/shows/<show_id>", methods=["GET", "POST"])
def edit_show(show_id):
    show, generation = repo().get(show_id)
    if not show or show.deleted:
        abort(404)
    errors = {}
    if request.method == "POST":
        posted_gen = request.form.get("generation", "")
        errors = _apply_form(show)
        if not errors:
            try:
                _save(show, generation=int(posted_gen) if posted_gen else generation)
                return redirect(url_for(
                    ".index", notice=f"Saved “{show.title}”." + _publish(show.id)))
            except ValidationError as e:
                errors = e.errors
            except Conflict:
                errors = {"_form": "This show was changed somewhere else since you opened it. "
                                   "Reload the page and make your edits again."}
    return render_template("admin/show_form.html", show=show, errors=errors,
                           categories=CATEGORIES, is_new=False, generation=generation)


@bp.post("/shows/<show_id>/delete")
def delete_show(show_id):
    show, generation = repo().get(show_id)
    if not show or show.deleted:
        abort(404)
    show.deleted = True
    show.domain = ""  # free the domain for reuse
    try:
        repo().save(show, generation)
    except Conflict:
        return redirect(url_for(".edit_show", show_id=show_id,
                                notice="Couldn't delete: the show was just changed. Try again."))
    _publish(show_id)  # removes the deleted show's feed
    return redirect(url_for(".index",
                            notice=f"Deleted “{show.title}”. Its files are still in the bucket."))


# --- feed --------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def feed_builder() -> FeedBuilder:
    base = current_app.config.get("PUBLIC_BASE_URL") or request.url_root
    return FeedBuilder(storage(), base_url=base)


def _publish(show_id: str) -> str:
    """Rebuild the show's feed after a change. Returns text to add to the notice.

    The change itself is already saved, so a failure here is reported, not raised.
    """
    try:
        builder = feed_builder()
        builder.publish(show_id)
        show, _ = repo().get(show_id)
        SiteBuilder(storage(), base_url=builder.base_url).publish(
            show, episodes().list(show_id) if show else [], show_id=show_id)
        return ""
    except Exception:
        current_app.logger.exception("Could not rebuild the feed and page for %s", show_id)
        return (" The feed and show page could not be rebuilt; "
                "use “Rebuild” on the Episodes page to try again.")


@bp.post("/shows/<show_id>/feed/rebuild")
def rebuild_feed(show_id):
    _show_or_404(show_id)
    failed = _publish(show_id)
    return redirect(url_for(".list_episodes", show_id=show_id,
                            notice=failed.strip() or "Feed and show page rebuilt."))


# --- download stats ----------------------------------------------------------

STATS_RANGES = {"7": 7, "30": 30, "90": 90, "all": None}
STATS_CACHE_SECONDS = 300


def stats_store() -> StatsStore:
    return current_app.extensions["bucketcast.stats"]


def _summary(show_id: str, range_key: str):
    """Counted downloads for the dashboard, cached briefly (BigQuery bills per query)."""
    cache = current_app.extensions.setdefault("bucketcast.stats_cache", {})
    key, now = (show_id, range_key), time.monotonic()
    if key in cache and now - cache[key][0] < STATS_CACHE_SECONDS:
        return cache[key][1]
    days = STATS_RANGES[range_key]
    today = datetime.now(UTC).date()
    summary = summarize(stats_store().rows(show_id, since_for(days, today)), days, today)
    if not current_app.config.get("TESTING") and current_app.config.get("STATS_BQ_TABLE"):
        cache[key] = (now, summary)
    return summary


@bp.get("/shows/<show_id>/stats")
def show_stats(show_id):
    show = _show_or_404(show_id)
    range_key = request.args.get("range", "30")
    if range_key not in STATS_RANGES:
        range_key = "30"
    summary, error = None, ""
    try:
        summary = _summary(show_id, range_key)
    except Exception:
        current_app.logger.exception("Could not load download stats for %s", show_id)
        error = "The download numbers couldn't be loaded. Check the app's logs for the reason."

    rows, chart = [], None
    if summary:
        rows = _episode_rows(show_id, summary.by_episode)
        chart = _bar_chart(summary.by_day)
    return render_template("admin/stats.html", show=show, summary=summary, error=error,
                           episode_rows=rows, chart=chart, range_key=range_key,
                           source=stats_store().describe())


def _episode_rows(show_id: str, counts: dict[str, int]) -> list[tuple[str, int]]:
    """(title, downloads) per episode, most downloaded first, then newest first."""
    rows = []
    remaining = dict(counts)
    for position, episode in enumerate(episodes().list(show_id, include_deleted=True)):
        n = remaining.pop(episode.id, 0)
        if episode.deleted and not n:
            continue
        title = episode.title + (" (deleted)" if episode.deleted else "")
        rows.append((-n, position, title, n))
    # Downloads logged for an episode that no longer exists at all.
    rows += [(-n, len(rows) + i, "Unknown episode", n)
             for i, n in enumerate(remaining.values())]
    return [(title, n) for _, _, title, n in sorted(rows)]


def _bar_chart(by_day: list, width: int = 720, height: int = 220) -> dict:
    """Geometry for the downloads-per-day bar chart, drawn as inline SVG."""
    left, right, top, bottom = 44, 8, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    peak = max((n for _, n in by_day), default=0)
    # Round the axis up to a tidy number so gridlines land on whole downloads.
    step, i = 1, 0
    while step * 5 < peak:  # 1, 2, 5, 10, 20, 50, ... until five steps cover the peak
        step = step * 5 // 2 if i % 3 == 1 else step * 2
        i += 1
    top_value = step * max(2, -(-peak // step))
    slot = plot_w / max(len(by_day), 1)
    bar_w = max(1.0, min(28.0, slot - 2))  # 2px gap between neighbours
    label_every = max(1, -(-len(by_day) // 8))
    bars = []
    for i, (day, n) in enumerate(by_day):
        h = plot_h * n / top_value
        bars.append({
            "x": round(left + i * slot + (slot - bar_w) / 2, 2),
            "y": round(top + plot_h - h, 2),
            "w": round(bar_w, 2),
            "h": round(h, 2),
            "r": min(4, round(bar_w / 2, 2), round(h, 2)),
            "slot_x": round(left + i * slot, 2),
            "slot_w": round(slot, 2),
            "n": n,
            "label": f"{day:%b} {day.day}",
            "full": f"{day:%a}, {day:%b} {day.day}, {day.year}",
            "tick": i % label_every == 0,
            "cx": round(left + i * slot + slot / 2, 2),
        })
    grid = [{"y": round(top + plot_h - plot_h * v / top_value, 2), "value": v}
            for v in range(0, top_value + 1, step)]
    return {"width": width, "height": height, "left": left, "right": width - right,
            "top": top, "base": top + plot_h, "bars": bars, "grid": grid, "peak": peak}


# --- episodes ----------------------------------------------------------------

def episodes() -> EpisodeRepository:
    return current_app.extensions["bucketcast.episodes"]


def _show_or_404(show_id: str) -> Show:
    show, _ = repo().get(show_id)
    if not show or show.deleted:
        abort(404)
    return show


def _episode_or_404(show_id: str, episode_id: str) -> Episode:
    episode = episodes().get(show_id, episode_id)
    if not episode or episode.deleted:
        abort(404)
    return episode


@bp.app_template_global()
def media_url(path: str) -> str:
    return storage().public_url(path) if path else ""


@bp.app_template_filter()
def duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@bp.app_template_filter()
def filesize(size: int) -> str:
    mb = (size or 0) / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


@bp.get("/shows/<show_id>/episodes")
def list_episodes(show_id):
    show = _show_or_404(show_id)
    builder = feed_builder()
    has_feed = storage().object_info(feed_path(show_id)) is not None
    return render_template("admin/episodes.html", show=show,
                           episodes=episodes().list(show_id),
                           feed_url=builder.feed_url(show) if has_feed else "",
                           site_url=builder.site_url(show) if has_feed else "",
                           direct_feed_url=builder.media_url(feed_path(show_id)),
                           direct_site_url=builder.media_url(page_path(show_id, 1)),
                           feed_problems=feed_problems(show), now=_now_iso())


@bp.post("/shows/<show_id>/episodes/upload-url")
def episode_upload_url(show_id):
    """Hand the browser a URL to PUT the audio file to, straight into the bucket."""
    _show_or_404(show_id)
    data = request.get_json(silent=True) or {}
    filename = str(data.get("filename") or "")
    content_type = audio_type_for(filename)
    if not content_type:
        return {"error": "Audio must be an MP3 or M4A file."}, 400
    size = data.get("size")
    if isinstance(size, int) and size > MAX_AUDIO_BYTES:
        return {"error": "Audio files can be at most 2 GB."}, 400
    path = new_audio_path(show_id, filename)
    return {
        "url": storage().signed_upload_url(path, content_type),
        "path": path,
        # The browser must send exactly these headers; they are part of the signature.
        "headers": {"Content-Type": content_type},
    }


@bp.route("/shows/<show_id>/episodes/new", methods=["GET", "POST"])
def new_episode(show_id):
    show = _show_or_404(show_id)
    episode = Episode(title="", explicit=show.explicit)
    errors = {}
    if request.method == "POST":
        errors = _apply_episode_form(show_id, episode)
        if not errors:
            try:
                _apply_episode_artwork(show_id, episode)
                episodes().add(show_id, episode)
                return redirect(url_for(
                    ".list_episodes", show_id=show_id,
                    notice=f"Published “{episode.title}”." + _publish(show_id)))
            except ValidationError as e:
                errors = e.errors
    return render_template("admin/episode_form.html", show=show, episode=episode,
                           errors=errors, is_new=True,
                           notes_html=_render_notes(show_id, episode.description))


@bp.route("/shows/<show_id>/episodes/<episode_id>", methods=["GET", "POST"])
def edit_episode(show_id, episode_id):
    show = _show_or_404(show_id)
    episode = _episode_or_404(show_id, episode_id)
    errors = {}
    if request.method == "POST":
        try:
            revision = int(request.form.get("revision", ""))
        except ValueError:
            revision = episode.revision
        errors = _apply_episode_form(show_id, episode)
        if not errors:
            try:
                _apply_episode_artwork(show_id, episode)
                episodes().update(show_id, episode, revision)
                return redirect(url_for(
                    ".list_episodes", show_id=show_id,
                    notice=f"Saved “{episode.title}”." + _publish(show_id)))
            except ValidationError as e:
                errors = e.errors
            except Conflict:
                errors = {"_form": "This episode was changed somewhere else since you opened "
                                   "it. Reload the page and make your edits again."}
    return render_template("admin/episode_form.html", show=show, episode=episode,
                           errors=errors, is_new=False,
                           notes_html=_render_notes(show_id, episode.description))


@bp.post("/shows/<show_id>/episodes/<episode_id>/delete")
def delete_episode(show_id, episode_id):
    _show_or_404(show_id)
    episode = _episode_or_404(show_id, episode_id)
    episode.deleted = True
    try:
        episodes().update(show_id, episode, episode.revision)
    except Conflict:
        return redirect(url_for(".edit_episode", show_id=show_id, episode_id=episode_id,
                                notice="Couldn't delete: the episode was just changed. "
                                       "Try again."))
    return redirect(url_for(
        ".list_episodes", show_id=show_id,
        notice=f"Deleted “{episode.title}”. Its audio file is still in the bucket."
               + _publish(show_id)))


# --- show assets and notes ---------------------------------------------------

def assets() -> AssetRepository:
    return current_app.extensions["bucketcast.assets"]


def _wants_json() -> bool:
    return request.accept_mimetypes.best == "application/json"


@bp.get("/shows/<show_id>/assets")
def list_assets(show_id):
    show = _show_or_404(show_id)
    items = assets().list(show_id)
    if _wants_json():
        return {"assets": [a.to_dict() for a in items]}
    return render_template("admin/assets.html", show=show, assets=items,
                           error=request.args.get("error", ""))


@bp.post("/shows/<show_id>/assets")
def upload_asset(show_id):
    _show_or_404(show_id)
    upload = request.files.get("file")
    try:
        if not upload or not upload.filename:
            raise ValidationError({"file": "Choose an image to upload."})
        asset = assets().save(show_id, upload.filename, upload.read())
    except ValidationError as e:
        message = e.errors["file"]
        if _wants_json():
            return {"error": message}, 400
        return redirect(url_for(".list_assets", show_id=show_id, error=message))
    if _wants_json():
        return asset.to_dict()
    return redirect(url_for(".list_assets", show_id=show_id, notice=f"Added {asset.name}."))


@bp.post("/shows/<show_id>/assets/<name>/delete")
def delete_asset(show_id, name):
    _show_or_404(show_id)
    if not assets().delete(show_id, name):
        abort(404)
    return redirect(url_for(".list_assets", show_id=show_id, notice=f"Deleted {name}."))


@bp.post("/shows/<show_id>/notes-preview")
def notes_preview(show_id):
    """Render Markdown notes exactly as the feed and show page will."""
    _show_or_404(show_id)
    data = request.get_json(silent=True) or {}
    return {"html": _render_notes(show_id, str(data.get("text") or ""))}


def _render_notes(show_id: str, text: str) -> str:
    return render_notes(text, lambda name: assets().url(show_id, name))


def _apply_episode_artwork(show_id: str, episode: Episode) -> None:
    """Store a newly chosen episode image, or clear it. May raise ValidationError."""
    episode.validate(show_id)  # validate before writing any files
    art = request.files.get("artwork")
    if art and art.filename:
        episode.artwork_path = episodes().save_artwork(
            show_id, episode, art.read(), art.mimetype
        )
    elif request.form.get("remove_artwork") == "on":
        episode.artwork_path = ""


def _apply_episode_form(show_id: str, episode: Episode) -> dict:
    """Copy form values onto ``episode``. Returns field errors."""
    f = request.form
    errors = {}
    episode.title = f.get("title", "").strip()
    episode.description = f.get("description", "").strip()
    episode.explicit = f.get("explicit") == "on"
    # ``published_at`` is filled in (as UTC) by the page's script; the visible
    # field is the fallback and is read as UTC.
    published = f.get("published_at", "").strip() or f.get("published_local", "").strip()
    if published:
        episode.published_at = published

    for name in ("season", "number"):
        raw = f.get(name, "").strip()
        try:
            setattr(episode, name, int(raw) if raw else None)
        except ValueError:
            errors[name] = "Must be a whole number."

    raw = f.get("duration_seconds", "").strip()
    if raw:
        try:
            episode.duration_seconds = max(0, round(float(raw)))
        except ValueError:
            errors["duration_seconds"] = "Duration must be a number of seconds."

    audio_path = f.get("audio_path", "").strip()
    if audio_path and audio_path != episode.audio_path:
        info = None
        if audio_path.startswith(audio_prefix(show_id)) and ".." not in audio_path:
            info = storage().object_info(audio_path)
        if info is None:
            errors["audio"] = "The audio upload didn't finish. Choose the file again."
        elif info[0] > MAX_AUDIO_BYTES:
            errors["audio"] = "Audio files can be at most 2 GB."
        else:
            episode.audio_path = audio_path
            # Size comes from the bucket, never from the browser.
            episode.audio_bytes = info[0]
            episode.audio_type = audio_type_for(audio_path) or info[1]
    return errors


def _apply_form(show: Show) -> dict:
    """Copy form values onto ``show``. Returns field errors (currently none)."""
    f = request.form
    for name in FIELDS:
        setattr(show, name, f.get(name, "").strip())
    show.domain = normalize_domain(show.domain)
    show.explicit = f.get("explicit") == "on"
    show.track_downloads = f.get("track_downloads") == "on"
    show.full_notes = f.get("notes_style", "full") != "excerpt"
    errors = {}
    per_page = f.get("episodes_per_page", "").strip()
    if per_page:
        try:
            show.episodes_per_page = int(per_page)
        except ValueError:
            errors["episodes_per_page"] = "Use a whole number."
    return errors


def _save(show: Show, generation):
    show.validate()  # validate before writing any files
    art = request.files.get("artwork")
    if art and art.filename:
        show.artwork_path = repo().save_artwork(show, art.read(), art.mimetype)
    repo().save(show, generation)
