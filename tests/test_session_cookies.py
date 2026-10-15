"""Session cookie lifetime and server-side revocation (auth/session.py).

Covers the timed signature (30-day sessions, 1-hour impersonation cookies,
enforced server-side whatever the browser does), the per-account
``session_epoch`` (a cookie dies once its account's epoch moves on), the
routes that bump it (both sign-out routes, logout-and-disconnect, password
set/change) or issue cookies under it (impersonation start/stop), and the
rejection of cookies in the pre-timed format. Runs against a real temporary
SQLite database, like tests/test_password_login.py.
"""

import asyncio
import time
from http.cookies import SimpleCookie
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import auth.session as session_mod
import db.password_store as password_store
import db.user_service_credential_store as credential_store
import db.user_store as user_store
from auth.config import COOKIE_NAME, COOKIE_VERSION
from auth.session import (
    IMPERSONATION_MAX_AGE_SECONDS,
    SESSION_MAX_AGE_SECONDS,
    get_user_from_cookie,
    set_impersonation_cookie,
    set_session_cookie,
)
from db.models import Base

SECRET = "test-secret"
ADMIN_EMAIL = "admin@example.test"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "quest.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(user_store, "AsyncSessionLocal", factory)
    monkeypatch.setattr(password_store, "AsyncSessionLocal", factory)
    monkeypatch.setattr(credential_store, "AsyncSessionLocal", factory)
    yield
    _run(engine.dispose())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(session_mod, "get_secret_key", lambda: SECRET)
    # Google sign-in: cookies here carry no password fingerprint.
    monkeypatch.setattr("auth.config.login_method", lambda: "google")
    monkeypatch.setattr("chat.auth.is_admin", lambda email: email == ADMIN_EMAIL)


def _user(email="alice@example.test"):
    return _run(user_store.create_user(email=email, name=email.split("@")[0], api_key=f"k-{email}"))


def _reload(user):
    return _run(user_store.get_user_by_id(user["id"]))


def _morsel(response):
    """The session cookie as set on *response* (a Morsel)."""
    jar = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        jar.load(header)
    return jar[COOKIE_NAME]


def _issue(user, password_fp=None):
    return _morsel(set_session_cookie(JSONResponse({}), user, password_fp)).value


def _issue_imp(target, admin):
    return _morsel(set_impersonation_cookie(JSONResponse({}), target, admin)).value


def _aged(seconds):
    """Sign while the clock reads *seconds* ago (the cookie is that old)."""
    return patch("itsdangerous.timed.time.time", return_value=time.time() - seconds)


def _resolve(cookie):
    return _run(get_user_from_cookie(cookie))


def _request(cookie=None, method="POST"):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return Request({
        "type": "http", "method": method, "path": "/", "headers": headers,
        "scheme": "https", "server": ("quest.test", 443), "query_string": b"",
    })


# ---------------------------------------------------------------------------
# Issue and verify
# ---------------------------------------------------------------------------

class TestIssue:
    def test_issued_cookie_verifies(self):
        alice = _user()
        resolved = _resolve(_issue(alice))
        assert resolved["id"] == alice["id"]
        assert "_impersonator_uid" not in resolved

    def test_payload_carries_version_uid_and_epoch(self):
        alice = _user()
        payload = session_mod.get_session_serializer().loads(_issue(alice))
        assert payload == {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0}

    def test_browser_lifetimes_match_server_lifetimes(self):
        alice, admin = _user(), _user(ADMIN_EMAIL)
        normal = _morsel(set_session_cookie(JSONResponse({}), alice))
        imp = _morsel(set_impersonation_cookie(JSONResponse({}), alice, admin))
        assert normal["max-age"] == str(SESSION_MAX_AGE_SECONDS)
        assert imp["max-age"] == str(IMPERSONATION_MAX_AGE_SECONDS)
        assert normal["httponly"] and normal["samesite"].lower() == "lax"

    @pytest.mark.parametrize("secure", [True, False])
    def test_secure_flag_follows_cookie_secure(self, monkeypatch, secure):
        monkeypatch.setattr(session_mod, "COOKIE_SECURE", secure)
        morsel = _morsel(set_session_cookie(JSONResponse({}), _user()))
        assert bool(morsel["secure"]) is secure


# ---------------------------------------------------------------------------
# Server-side lifetime
# ---------------------------------------------------------------------------

class TestLifetime:
    def test_session_within_lifetime_verifies(self):
        alice = _user()
        with _aged(SESSION_MAX_AGE_SECONDS - 60):
            cookie = _issue(alice)
        assert _resolve(cookie)["id"] == alice["id"]

    def test_session_older_than_lifetime_is_rejected(self):
        alice = _user()
        with _aged(SESSION_MAX_AGE_SECONDS + 60):
            cookie = _issue(alice)
        assert _resolve(cookie) is None

    def test_future_dated_cookie_is_rejected(self):
        alice = _user()
        with _aged(-3600):
            cookie = _issue(alice)
        assert _resolve(cookie) is None

    def test_impersonation_within_hour_verifies(self):
        alice, admin = _user(), _user(ADMIN_EMAIL)
        with _aged(IMPERSONATION_MAX_AGE_SECONDS - 60):
            cookie = _issue_imp(alice, admin)
        resolved = _resolve(cookie)
        assert resolved["id"] == alice["id"]
        assert resolved["_impersonator_uid"] == admin["id"]

    def test_impersonation_older_than_hour_is_rejected(self):
        alice, admin = _user(), _user(ADMIN_EMAIL)
        with _aged(IMPERSONATION_MAX_AGE_SECONDS + 60):
            cookie = _issue_imp(alice, admin)
        assert _resolve(cookie) is None

    def test_imp_field_gets_the_short_lifetime_however_the_cookie_was_minted(self):
        # Even a validly signed payload that adds "imp" by hand is held to
        # the impersonation lifetime: the limit follows the payload.
        alice, admin = _user(), _user(ADMIN_EMAIL)
        payload = {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0, "imp": admin["id"], "ie": 0}
        with _aged(IMPERSONATION_MAX_AGE_SECONDS * 2):
            cookie = session_mod.get_session_serializer().dumps(payload)
        assert _resolve(cookie) is None


# ---------------------------------------------------------------------------
# Rejected formats
# ---------------------------------------------------------------------------

class TestRejectedFormats:
    def test_pre_timed_cookie_is_rejected(self):
        # The untimed format signed before this one (same key, old salt).
        alice = _user()
        old = URLSafeSerializer(SECRET, salt="quest-session").dumps({"v": 2, "uid": alice["id"]})
        assert _resolve(old) is None
        # Even with the current payload shape and salt, no timestamp, no session.
        untimed = URLSafeSerializer(SECRET, salt=session_mod._SESSION_SALT).dumps(
            {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0},
        )
        assert _resolve(untimed) is None

    def test_foreign_key_is_rejected(self):
        alice = _user()
        forged = URLSafeTimedSerializer("other", salt=session_mod._SESSION_SALT).dumps(
            {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0},
        )
        assert _resolve(forged) is None

    @pytest.mark.parametrize("change", [
        {"e": None},
        {"e": True},
        {"e": -1},
        {"e": "0"},
        {"v": 2},
        {"v": True},
        {"uid": True},
        {"uid": 0},
    ])
    def test_malformed_payloads_are_rejected(self, change):
        alice = _user()
        payload = {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0, **change}
        payload = {k: v for k, v in payload.items() if v is not None}
        cookie = session_mod.get_session_serializer().dumps(payload)
        assert _resolve(cookie) is None

    @pytest.mark.parametrize("ie", [None, True, -1, "0"])
    def test_impersonation_without_valid_admin_epoch_is_rejected(self, ie):
        alice, admin = _user(), _user(ADMIN_EMAIL)
        payload = {"v": COOKIE_VERSION, "uid": alice["id"], "e": 0, "imp": admin["id"]}
        if ie is not None:
            payload["ie"] = ie
        assert _resolve(session_mod.get_session_serializer().dumps(payload)) is None


# ---------------------------------------------------------------------------
# Session epoch
# ---------------------------------------------------------------------------

class TestEpoch:
    def test_bump_returns_new_epoch(self):
        alice = _user()
        assert alice["session_epoch"] == 0
        assert _run(user_store.bump_session_epoch(alice["id"])) == 1
        assert _run(user_store.bump_session_epoch(alice["id"])) == 2
        assert _reload(alice)["session_epoch"] == 2
        assert _run(user_store.bump_session_epoch(999_999)) is None

    def test_epoch_mismatch_is_rejected(self):
        alice = _user()
        old = _issue(alice)
        _run(user_store.bump_session_epoch(alice["id"]))
        assert _resolve(old) is None
        # A cookie issued under the new epoch works.
        assert _resolve(_issue(_reload(alice)))["id"] == alice["id"]

    def test_cookie_from_a_later_epoch_is_rejected(self):
        alice = _user()
        payload = {"v": COOKIE_VERSION, "uid": alice["id"], "e": 5}
        assert _resolve(session_mod.get_session_serializer().dumps(payload)) is None

    def test_bump_affects_only_that_account(self):
        alice, bob = _user(), _user("bob@example.test")
        bob_cookie = _issue(bob)
        _run(user_store.bump_session_epoch(alice["id"]))
        assert _resolve(bob_cookie)["id"] == bob["id"]

    def test_either_accounts_bump_ends_an_impersonation(self):
        alice, admin = _user(), _user(ADMIN_EMAIL)
        cookie = _issue_imp(alice, admin)
        assert _resolve(cookie)["_impersonator_uid"] == admin["id"]
        _run(user_store.bump_session_epoch(admin["id"]))
        assert _resolve(cookie) is None

        cookie = _issue_imp(_reload(alice), _reload(admin))
        assert _resolve(cookie) is not None
        _run(user_store.bump_session_epoch(alice["id"]))
        assert _resolve(cookie) is None

    def test_password_set_ends_sessions_without_a_fingerprint(self):
        alice = _user()
        cookie = _issue(alice)  # e.g. Google-issued: no "pw" field
        assert _run(password_store.set_password_hash(alice["email"], "scrypt$x"))
        assert _reload(alice)["session_epoch"] == 1
        assert _resolve(cookie) is None

    def test_pw_fingerprint_still_checked_alongside_the_epoch(self, monkeypatch):
        monkeypatch.setattr("auth.config.login_method", lambda: "password")
        alice = _user()
        cookie = _issue(alice, password_fp="0123456789abcdef")
        # Epoch matches, fingerprint does not (the account has no password).
        assert _resolve(cookie) is None


# ---------------------------------------------------------------------------
# Routes that revoke or issue
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_logout_bumps_epoch_and_clears_cookie(self):
        import chat.routes.user as user_routes
        alice = _user()
        cookie = _issue(alice)
        other_device = _issue(alice)
        response = _run(user_routes.logout(_request(cookie)))
        assert response.status_code == 200
        assert _morsel(response)["max-age"] == "0"
        assert _reload(alice)["session_epoch"] == 1
        assert _resolve(cookie) is None
        assert _resolve(other_device) is None

    def test_logout_without_a_valid_cookie_revokes_nothing(self):
        import chat.routes.user as user_routes
        alice = _user()
        assert _run(user_routes.logout(_request())).status_code == 200
        assert _run(user_routes.logout(_request("garbage"))).status_code == 200
        assert _reload(alice)["session_epoch"] == 0

    def test_get_auth_logout_bumps_epoch(self):
        import auth.google_login as google_login
        alice = _user()
        cookie = _issue(alice)
        response = _run(google_login.auth_logout(_request(cookie, method="GET")))
        assert response.status_code in (302, 307)
        assert _morsel(response)["max-age"] == "0"
        assert _resolve(cookie) is None

    def test_logout_while_impersonating_ends_the_admins_sessions_only(self):
        import chat.routes.user as user_routes
        alice, admin = _user(), _user(ADMIN_EMAIL)
        alice_cookie = _issue(alice)
        admin_cookie = _issue(admin)
        imp_cookie = _issue_imp(alice, admin)
        _run(user_routes.logout(_request(imp_cookie)))
        assert _reload(admin)["session_epoch"] == 1
        assert _reload(alice)["session_epoch"] == 0
        assert _resolve(imp_cookie) is None
        assert _resolve(admin_cookie) is None
        assert _resolve(alice_cookie)["id"] == alice["id"]

    def test_logout_and_disconnect_bumps_epoch(self):
        import chat.routes.user as user_routes
        alice = _user()
        cookie = _issue(alice)
        resolved = _resolve(cookie)
        response = _run(user_routes.logout_and_disconnect(request=None, user=resolved))
        assert response.status_code == 200
        assert _reload(alice)["session_epoch"] == 1
        assert _resolve(cookie) is None

    def test_impersonate_and_stop_issue_cookies_under_current_epochs(self, monkeypatch):
        import chat.routes.admin as admin_routes
        monkeypatch.setattr(admin_routes, "is_admin", lambda email: email == ADMIN_EMAIL)
        monkeypatch.setattr(admin_routes, "is_password_login", lambda: False)
        alice, admin = _user(), _user(ADMIN_EMAIL)
        _run(user_store.bump_session_epoch(alice["id"]))
        _run(user_store.bump_session_epoch(admin["id"]))
        admin = _reload(admin)

        response = _run(admin_routes.admin_impersonate(
            admin_routes.ImpersonateRequest(user_id=alice["id"]), user=admin,
        ))
        morsel = _morsel(response)
        assert morsel["max-age"] == str(IMPERSONATION_MAX_AGE_SECONDS)
        payload = session_mod.get_session_serializer().loads(morsel.value)
        assert (payload["e"], payload["ie"]) == (1, 1)
        resolved = _resolve(morsel.value)
        assert resolved["id"] == alice["id"] and resolved["_impersonator_uid"] == admin["id"]

        response = _run(admin_routes.admin_stop_impersonation(_request(morsel.value)))
        restored = _morsel(response)
        assert restored["max-age"] == str(SESSION_MAX_AGE_SECONDS)
        resolved = _resolve(restored.value)
        assert resolved["id"] == admin["id"]
        assert "_impersonator_uid" not in resolved
