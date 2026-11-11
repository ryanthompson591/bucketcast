"""Admin access control.

In production, /admin sits behind Google Identity-Aware Proxy (IAP). As
defense in depth the app also verifies IAP's signed JWT on every admin request
and, if ``ADMIN_EMAILS`` is set, checks the signed-in account against it.

The app is secure by default. Admin requests are refused unless IAP is
configured (``IAP_AUDIENCE``) *and* at least one admin account is listed
(``ADMIN_EMAILS``). The allow-list is required, not optional, so a mistake in
IAP's own access settings can't open the admin to every Google account.

The dev bypass only works off Google Cloud: on App Engine it is ignored.

Config:
  IAP_AUDIENCE      "/projects/<project-number>/apps/<project-id>" for App Engine
  ADMIN_EMAILS      comma-separated Google accounts allowed to administer
  DEV_AUTH_BYPASS   "1" to skip auth for local development only
"""

from __future__ import annotations

import os

from flask import abort, current_app, g, request

IAP_JWT_HEADER = "X-Goog-IAP-JWT-Assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


def verify_iap_jwt(token: str, audience: str) -> str:
    """Return the verified email, or raise ValueError."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_token(
        token, google_requests.Request(), audience=audience, certs_url=IAP_CERTS_URL
    )
    email = claims.get("email")
    if not email:
        raise ValueError("IAP token has no email claim")
    return email.lower()


def on_google_cloud() -> bool:
    """True when running on App Engine (or Cloud Run), judged by its own env vars."""
    return any(os.environ.get(name) for name in ("GAE_ENV", "GAE_APPLICATION", "K_SERVICE"))


def bypass_enabled(config) -> bool:
    """The dev bypass, which a deployed app never honours."""
    return bool(config.get("DEV_AUTH_BYPASS")) and not on_google_cloud()


def require_admin() -> None:
    cfg = current_app.config
    if cfg.get("DEV_AUTH_BYPASS"):
        if not on_google_cloud():
            g.admin_email = "dev@localhost"
            return
        current_app.logger.error(
            "BUCKETCAST_DEV_AUTH_BYPASS is set on a deployed app and is being ignored. "
            "Remove it from the service's configuration.")

    audience = cfg.get("IAP_AUDIENCE")
    if not audience:
        abort(403, "Admin is locked: IAP_AUDIENCE is not configured. "
                   "Set DEV_AUTH_BYPASS=1 for local development.")
    allowed = cfg.get("ADMIN_EMAILS") or []
    if not allowed:
        abort(403, "Admin is locked: ADMIN_EMAILS is not configured. "
                   "List the Google accounts allowed to administer Bucketcast.")
    token = request.headers.get(IAP_JWT_HEADER)
    if not token:
        abort(401, "Missing IAP assertion.")
    try:
        email = verify_iap_jwt(token, audience)
    except Exception:
        current_app.logger.warning("IAP JWT verification failed", exc_info=True)
        abort(401, "Invalid IAP assertion.")

    if email not in allowed:
        current_app.logger.warning("Admin access refused for %s", email)
        abort(403, "This account is not allowed to administer Bucketcast.")
    g.admin_email = email
