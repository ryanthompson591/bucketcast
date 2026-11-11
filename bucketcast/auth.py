"""Admin access control.

In production, /admin sits behind Google Identity-Aware Proxy (IAP). As
defense in depth the app also verifies IAP's signed JWT on every admin request
and, if ``ADMIN_EMAILS`` is set, checks the signed-in account against it.

The app is secure by default: if IAP isn't configured (no ``IAP_AUDIENCE``)
and the dev bypass isn't on, admin requests are refused.

Config:
  IAP_AUDIENCE      "/projects/<project-number>/apps/<project-id>" for App Engine
  ADMIN_EMAILS      optional comma-separated allow-list
  DEV_AUTH_BYPASS   "1" to skip auth for local development only
"""

from __future__ import annotations

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


def require_admin() -> None:
    cfg = current_app.config
    if cfg.get("DEV_AUTH_BYPASS"):
        g.admin_email = "dev@localhost"
        return

    audience = cfg.get("IAP_AUDIENCE")
    if not audience:
        abort(403, "Admin is locked: IAP_AUDIENCE is not configured. "
                   "Set DEV_AUTH_BYPASS=1 for local development.")
    token = request.headers.get(IAP_JWT_HEADER)
    if not token:
        abort(401, "Missing IAP assertion.")
    try:
        email = verify_iap_jwt(token, audience)
    except Exception:
        current_app.logger.warning("IAP JWT verification failed", exc_info=True)
        abort(401, "Invalid IAP assertion.")

    allowed = cfg.get("ADMIN_EMAILS") or []
    if allowed and email not in allowed:
        abort(403, "This account is not allowed to administer Bucketcast.")
    g.admin_email = email
