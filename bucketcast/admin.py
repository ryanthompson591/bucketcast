"""Admin UI: list, create, edit and delete shows."""

from __future__ import annotations

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

from .auth import require_admin
from .shows import CATEGORIES, Show, ShowRepository, ValidationError, normalize_domain
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
                return redirect(url_for(".index", notice=f"Created “{show.title}”."))
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
                return redirect(url_for(".index", notice=f"Saved “{show.title}”."))
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
    return redirect(url_for(".index",
                            notice=f"Deleted “{show.title}”. Its files are still in the bucket."))


def _apply_form(show: Show) -> dict:
    """Copy form values onto ``show``. Returns field errors (currently none)."""
    f = request.form
    for name in FIELDS:
        setattr(show, name, f.get(name, "").strip())
    show.domain = normalize_domain(show.domain)
    show.explicit = f.get("explicit") == "on"
    return {}


def _save(show: Show, generation):
    show.validate()  # validate before writing any files
    art = request.files.get("artwork")
    if art and art.filename:
        show.artwork_path = repo().save_artwork(show, art.read(), art.mimetype)
    repo().save(show, generation)
    # TODO(#5, #6): regenerate feed.xml and index.html for this show.
