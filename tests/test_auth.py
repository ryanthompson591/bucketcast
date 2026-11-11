import pytest

from bucketcast import auth, create_app
from bucketcast.shows import Show, ShowRepository
from bucketcast.storage import LocalStorage

AUDIENCE = "/projects/123/apps/my-project"
OWNER = "owner@example.com"
CLOUD_VARS = ("GAE_ENV", "GAE_APPLICATION", "K_SERVICE")


@pytest.fixture(autouse=True)
def not_on_cloud(monkeypatch):
    for name in CLOUD_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def storage(tmp_path):
    s = LocalStorage(tmp_path / "bucket")
    ShowRepository(s).save(Show(id="scifi", title="Sci-Fi", domain="pod.example.com"), None)
    return s


def app_for(storage, **config):
    base = {"TESTING": True, "DEV_AUTH_BYPASS": False, "IAP_AUDIENCE": AUDIENCE,
            "ADMIN_EMAILS": [OWNER]}
    return create_app({**base, **config}, storage=storage)


def signed_in_as(monkeypatch, email, expect_audience=AUDIENCE):
    """Stand in for Google's signature check: any token 'belongs' to ``email``."""
    def fake_verify(token, audience):
        assert audience == expect_audience
        if token != "good-token":
            raise ValueError("bad signature")
        return email

    monkeypatch.setattr(auth, "verify_iap_jwt", fake_verify)
    return {"X-Goog-IAP-JWT-Assertion": "good-token"}


# --- who gets in ---------------------------------------------------------------

def test_allowed_account_gets_in(storage, monkeypatch):
    headers = signed_in_as(monkeypatch, OWNER)
    r = app_for(storage).test_client().get("/admin/", headers=headers)
    assert r.status_code == 200 and OWNER.encode() in r.data


def test_other_google_account_is_refused(storage, monkeypatch):
    headers = signed_in_as(monkeypatch, "stranger@example.com")
    r = app_for(storage).test_client().get("/admin/", headers=headers)
    assert r.status_code == 403 and b"not allowed" in r.data


def test_missing_or_forged_token_is_refused(storage, monkeypatch):
    signed_in_as(monkeypatch, OWNER)
    client = app_for(storage).test_client()
    assert client.get("/admin/").status_code == 401
    bad = {"X-Goog-IAP-JWT-Assertion": "forged"}
    assert client.get("/admin/", headers=bad).status_code == 401
    # A header that merely claims an identity is worthless without the signed token.
    spoof = {"X-Goog-Authenticated-User-Email": f"accounts.google.com:{OWNER}"}
    assert client.get("/admin/", headers=spoof).status_code == 401


def test_every_admin_route_is_guarded(storage, monkeypatch):
    signed_in_as(monkeypatch, OWNER)
    app = app_for(storage)
    client = app.test_client()
    admin_rules = [r for r in app.url_map.iter_rules() if r.rule.startswith("/admin")]
    assert len(admin_rules) >= 15
    for rule in admin_rules:
        path = rule.rule.replace("<show_id>", "scifi").replace(
            "<episode_id>", "0" * 32).replace("<name>", "a.png")
        method = "GET" if "GET" in rule.methods else "POST"
        assert client.open(path, method=method).status_code == 401, rule.rule


# --- locked by default ---------------------------------------------------------

def test_locked_without_an_audience(storage, monkeypatch):
    headers = signed_in_as(monkeypatch, OWNER)
    r = app_for(storage, IAP_AUDIENCE="").test_client().get("/admin/", headers=headers)
    assert r.status_code == 403 and b"IAP_AUDIENCE" in r.data


def test_locked_without_an_allow_list(storage, monkeypatch):
    """IAP alone is not enough: with no accounts listed, nobody gets in."""
    headers = signed_in_as(monkeypatch, OWNER)
    r = app_for(storage, ADMIN_EMAILS=[]).test_client().get("/admin/", headers=headers)
    assert r.status_code == 403 and b"ADMIN_EMAILS" in r.data


# --- dev bypass ----------------------------------------------------------------

def test_bypass_works_locally(storage):
    client = app_for(storage, DEV_AUTH_BYPASS=True, IAP_AUDIENCE="",
                     ADMIN_EMAILS=[]).test_client()
    r = client.get("/admin/")
    assert r.status_code == 200 and b"dev@localhost" in r.data


@pytest.mark.parametrize("var", CLOUD_VARS)
def test_bypass_is_ignored_on_google_cloud(storage, monkeypatch, var):
    monkeypatch.setenv(var, "standard")
    assert auth.on_google_cloud()
    client = app_for(storage, DEV_AUTH_BYPASS=True).test_client()
    assert client.get("/admin/").status_code == 401  # falls through to the real check

    headers = signed_in_as(monkeypatch, OWNER)
    assert client.get("/admin/", headers=headers).status_code == 200

    unconfigured = app_for(storage, DEV_AUTH_BYPASS=True, IAP_AUDIENCE="", ADMIN_EMAILS=[])
    assert unconfigured.test_client().get("/admin/").status_code == 403


def test_local_upload_route_honours_the_same_rules(storage, monkeypatch):
    monkeypatch.setenv("GAE_ENV", "standard")
    client = app_for(storage, DEV_AUTH_BYPASS=True).test_client()
    r = client.put("/_local_upload/shows/scifi/audio/a.mp3", data=b"x")
    assert r.status_code == 401
    assert storage.object_info("shows/scifi/audio/a.mp3") is None


# --- public and admin as separate services ---------------------------------------

HOST = {"Host": "pod.example.com"}


def test_role_all_serves_both_halves(storage):
    all_in_one = app_for(storage, DEV_AUTH_BYPASS=True)
    all_in_one.test_client().post("/admin/shows/scifi/feed/rebuild")
    client = all_in_one.test_client()
    assert client.get("/admin/").status_code == 200
    assert client.get("/", headers=HOST).status_code == 200


def test_public_service_has_no_admin_routes(storage):
    app_for(storage, DEV_AUTH_BYPASS=True).test_client().post("/admin/shows/scifi/feed/rebuild")
    public = app_for(storage, ROLE="public", DEV_AUTH_BYPASS=True)
    client = public.test_client()
    assert client.get("/", headers=HOST).status_code == 200
    assert client.get("/feed.xml", headers=HOST).status_code == 200
    assert client.get("/healthz").status_code == 200
    # Not merely refused: the admin code paths are not there to reach.
    assert client.get("/admin/").status_code == 404
    assert client.post("/admin/shows/new", data={"id": "x", "title": "X"}).status_code == 404
    assert not [r for r in public.url_map.iter_rules() if r.rule.startswith("/admin")]


def test_admin_service_has_no_public_routes(storage, monkeypatch):
    headers = signed_in_as(monkeypatch, OWNER)
    admin = app_for(storage, ROLE="admin")
    client = admin.test_client()
    assert client.get("/admin/", headers=headers).status_code == 200
    assert client.get("/feed.xml", headers=HOST).status_code == 404
    assert client.get("/e/scifi/" + "0" * 32 + ".mp3").status_code == 404
    home = client.get("/")
    assert home.status_code == 302 and home.headers["Location"].endswith("/admin/")
    assert client.get("/healthz").status_code == 200


def test_unknown_role_is_rejected(storage):
    with pytest.raises(ValueError, match="BUCKETCAST_ROLE"):
        app_for(storage, ROLE="everything")
