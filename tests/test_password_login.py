"""Email/password sign-in (auth/password_login.py and friends).

Covers the hashing helpers, the login / link / set / change routes against a
real temporary SQLite database, the one-method-at-a-time rule (password
routes 404 under Google sign-in, Google login refused under password
sign-in, password-issued sessions dropped after the switch), and the
bootstrap hand-over of admin passwords.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth.config as auth_config
import auth.password_login as password_login
import auth.session as session_mod
import config.password_hashing as hashing
import db.password_store as password_store
import db.user_store as user_store
from auth.config import ALLOWED_DOMAIN, COOKIE_NAME
from db.models import Base

ALICE = f"alice@{ALLOWED_DOMAIN}"
OUTSIDER = "mallory@elsewhere.test"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_scrypt(monkeypatch):
    # Keep the suite fast; the format carries n, so verification still works.
    monkeypatch.setattr(hashing, "_SCRYPT_N", 2 ** 10)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "quest.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(user_store, "AsyncSessionLocal", factory)
    monkeypatch.setattr(password_store, "AsyncSessionLocal", factory)
    yield
    _run(engine.dispose())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(session_mod, "get_secret_key", lambda: "test-secret")
    # Admission policy as in prod (the domain check), whatever QUEST_ENV is.
    monkeypatch.setattr("config.environment.enforce_domain", lambda: True)
    monkeypatch.setattr(auth_config, "allowed_login_emails", lambda: [])
    monkeypatch.setattr(auth_config, "allowed_login_domain", lambda: ALLOWED_DOMAIN)
    # Fresh rate limiters for every test.
    for name in ("_failed_by_email", "_failed_by_ip", "_links_by_email", "_links_by_ip"):
        old = getattr(password_login, name)
        monkeypatch.setattr(password_login, name, password_login._SlidingWindow(old.limit, old.window))


@pytest.fixture
def method(monkeypatch):
    """Set the active sign-in method; defaults to password."""
    state = {"value": "password"}
    monkeypatch.setattr(auth_config, "login_method", lambda: state["value"])

    def _set(value):
        state["value"] = value

    _set("password")
    return _set


def _client():
    app = FastAPI()
    app.include_router(password_login.router)

    # A cookie-authenticated probe standing in for the rest of the app.
    @app.get("/whoami")
    async def whoami(request: Request):
        user = await session_mod.get_user_from_cookie(request.cookies.get(COOKIE_NAME, ""))
        return {"email": user["email"] if user else None}

    return TestClient(app)


def _create_account(email=ALICE, password="correct horse"):
    _run(user_store.create_user(email=email, name="Alice", api_key=f"k-{email}"))
    _run(password_store.set_password_hash(email, hashing.hash_password(password)))


def _link(email=ALICE, purpose="reset", ttl=timedelta(hours=1)):
    return _run(password_store.create_password_token(email, purpose, ttl))


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

class TestHashing:
    def test_round_trip_and_salting(self):
        a = hashing.hash_password("hunter2hunter2")
        b = hashing.hash_password("hunter2hunter2")
        assert a != b
        assert a.startswith("scrypt$")
        assert hashing.verify_password("hunter2hunter2", a)
        assert not hashing.verify_password("hunter3hunter3", a)

    @pytest.mark.parametrize("stored", [None, "", "bcrypt$x", "scrypt$1$2", "scrypt$a$b$c$d$e"])
    def test_malformed_hashes_never_verify(self, stored):
        assert not hashing.verify_password("anything", stored)

    def test_password_policy(self):
        assert hashing.password_problem("short")
        assert hashing.password_problem("x" * 300)
        assert hashing.password_problem(None)
        assert hashing.password_problem("eight ch") is None

    def test_fingerprint_changes_with_hash(self):
        a = hashing.hash_password("same password")
        b = hashing.hash_password("same password")
        assert hashing.password_fingerprint(a) != hashing.password_fingerprint(b)
        assert hashing.password_fingerprint(None) is None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLogin:
    def test_success_sets_bound_session(self, method):
        _create_account()
        client = _client()
        resp = client.post("/auth/password/login", json={"email": "ALICE@" + ALLOWED_DOMAIN, "password": "correct horse"})
        assert resp.status_code == 200
        assert client.get("/whoami").json() == {"email": ALICE}
        payload = session_mod.get_cookie_serializer().loads(client.cookies[COOKIE_NAME])
        assert payload["pw"]

    def test_wrong_password_and_unknown_account_look_alike(self, method):
        _create_account()
        client = _client()
        wrong = client.post("/auth/password/login", json={"email": ALICE, "password": "nope nope"})
        unknown = client.post("/auth/password/login", json={"email": "bob@" + ALLOWED_DOMAIN, "password": "nope nope"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_account_without_password_cannot_sign_in(self, method):
        _run(user_store.create_user(email=ALICE, name="Alice", api_key="k"))
        resp = _client().post("/auth/password/login", json={"email": ALICE, "password": "anything at all"})
        assert resp.status_code == 401

    def test_repeated_failures_are_rate_limited(self, method):
        _create_account()
        client = _client()
        for _ in range(password_login._failed_by_email.limit):
            client.post("/auth/password/login", json={"email": ALICE, "password": "wrong pass"})
        resp = client.post("/auth/password/login", json={"email": ALICE, "password": "correct horse"})
        assert resp.status_code == 429

    def test_outside_admission_policy_is_refused(self, method):
        _create_account(email=OUTSIDER)
        resp = _client().post("/auth/password/login", json={"email": OUTSIDER, "password": "correct horse"})
        assert resp.status_code == 403

    def test_disabled_under_google_sign_in(self, method):
        method("google")
        _create_account()
        resp = _client().post("/auth/password/login", json={"email": ALICE, "password": "correct horse"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "password_login_disabled"


# ---------------------------------------------------------------------------
# Set-password links
# ---------------------------------------------------------------------------

class TestLinks:
    def test_invite_creates_account_and_signs_in(self, method):
        token = _link(purpose="invite")
        client = _client()
        info = client.post("/auth/password/link-info", json={"token": token}).json()
        assert info == {"email": ALICE, "purpose": "invite", "account_exists": False, "name": ""}
        resp = client.post("/auth/password/set", json={"token": token, "password": "brand new pw", "name": "Alice A"})
        assert resp.status_code == 200
        user = _run(user_store.get_user_by_email(ALICE))
        assert user["name"] == "Alice A"
        assert user["password_fp"]
        assert client.get("/whoami").json() == {"email": ALICE}

    def test_link_is_single_use(self, method):
        token = _link()
        client = _client()
        assert client.post("/auth/password/set", json={"token": token, "password": "first password"}).status_code == 200
        again = client.post("/auth/password/set", json={"token": token, "password": "second password"})
        assert again.status_code == 404

    def test_expired_link_is_rejected(self, method):
        token = _link(ttl=timedelta(seconds=-1))
        resp = _client().post("/auth/password/set", json={"token": token, "password": "some password"})
        assert resp.status_code == 404

    def test_weak_password_keeps_the_link_usable(self, method):
        token = _link()
        client = _client()
        assert client.post("/auth/password/set", json={"token": token, "password": "short"}).status_code == 400
        assert client.post("/auth/password/set", json={"token": token, "password": "long enough"}).status_code == 200

    def test_reset_signs_out_old_sessions_and_voids_other_links(self, method):
        _create_account()
        old_session = _client()
        old_session.post("/auth/password/login", json={"email": ALICE, "password": "correct horse"})
        assert old_session.get("/whoami").json() == {"email": ALICE}

        first, second = _link(), _link()
        assert _client().post("/auth/password/set", json={"token": first, "password": "a new password"}).status_code == 200
        assert old_session.get("/whoami").json() == {"email": None}
        assert _client().post("/auth/password/link-info", json={"token": second}).status_code == 404

    def test_request_link_needs_email(self, method):
        with patch.object(password_login, "smtp_configured", return_value=False):
            resp = _client().post("/auth/password/request-link", json={"email": ALICE})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "email_not_configured"

    def test_request_link_emails_allowed_addresses_only(self, method):
        _create_account()
        sent = AsyncMock()
        with patch.object(password_login, "smtp_configured", return_value=True), \
                patch.object(password_login, "send_email", sent):
            client = _client()
            ok = client.post("/auth/password/request-link", json={"email": ALICE})
            outsider = client.post("/auth/password/request-link", json={"email": OUTSIDER})
            # Same generic answer either way (no account enumeration).
            assert ok.json() == outsider.json()
        assert [c.args[0] for c in sent.await_args_list] == [ALICE]
        assert "/set-password#token=" in sent.await_args.args[2]


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------

class TestChange:
    def test_change_requires_current_password(self, method):
        _create_account()
        client = _client()
        other = _client()
        for c in (client, other):
            c.post("/auth/password/login", json={"email": ALICE, "password": "correct horse"})

        bad = client.post("/auth/password/change", json={"current_password": "nope nope", "new_password": "another one"})
        assert bad.status_code == 400
        ok = client.post("/auth/password/change", json={"current_password": "correct horse", "new_password": "another one"})
        assert ok.status_code == 200
        # This session got a fresh cookie; the other one is signed out.
        assert client.get("/whoami").json() == {"email": ALICE}
        assert other.get("/whoami").json() == {"email": None}

    def test_unauthenticated(self, method):
        resp = _client().post("/auth/password/change", json={"new_password": "another one"})
        assert resp.status_code == 401

    def test_refused_while_impersonating(self, method, monkeypatch):
        import chat.auth as chat_auth

        admin_email = "admin@" + ALLOWED_DOMAIN
        _create_account()
        _create_account(email=admin_email, password="admin password")
        monkeypatch.setattr(chat_auth, "is_admin", lambda email: email == admin_email)
        alice = _run(user_store.get_user_by_email(ALICE))
        admin = _run(user_store.get_user_by_email(admin_email))
        client = _client()
        client.cookies.set(COOKIE_NAME, session_mod.get_cookie_serializer().dumps(
            {"v": auth_config.COOKIE_VERSION, "uid": alice["id"], "imp": admin["id"]}
        ))
        assert client.get("/whoami").json() == {"email": ALICE}  # a live impersonation
        before = _run(password_store.get_password_hash(ALICE))

        resp = client.post(
            "/auth/password/change",
            json={"current_password": "correct horse", "new_password": "another one"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "impersonation_forbidden"
        assert _run(password_store.get_password_hash(ALICE)) == before


# ---------------------------------------------------------------------------
# Switching to Google sign-in
# ---------------------------------------------------------------------------

class TestSwitchToGoogle:
    def test_password_sessions_end_but_account_survives(self, method):
        _create_account()
        client = _client()
        client.post("/auth/password/login", json={"email": ALICE, "password": "correct horse"})
        method("google")
        assert client.get("/whoami").json() == {"email": None}
        # The account (same email) is still there for the Google callback.
        assert _run(user_store.get_user_by_email(ALICE))["id"]

    def test_google_login_refused_under_password_sign_in(self, method):
        import auth.google_login as google_login
        app = FastAPI()
        app.include_router(google_login.router)
        client = TestClient(app)
        assert client.get("/auth/login-url").status_code == 404
        assert client.get("/auth/callback?code=x&state=y").status_code == 404
        resp = client.get("/auth/", follow_redirects=False)
        assert resp.status_code in (302, 307) and resp.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Bootstrap hand-over
# ---------------------------------------------------------------------------

class TestPendingAdminPasswords:
    def test_applied_and_file_removed(self, tmp_path, method):
        _create_account()  # existing account gets its password replaced
        hashing.write_pending_admin_passwords(tmp_path, {
            ALICE: hashing.hash_password("from the wizard"),
            "root@" + ALLOWED_DOMAIN: hashing.hash_password("root password"),
        })
        applied = _run(password_login.apply_pending_admin_passwords(tmp_path))
        assert sorted(applied) == sorted([ALICE, "root@" + ALLOWED_DOMAIN])
        assert not hashing.pending_admin_passwords_path(tmp_path).exists()
        stored = _run(password_store.get_password_hash("root@" + ALLOWED_DOMAIN))
        assert hashing.verify_password("root password", stored)
        client = _client()
        resp = client.post("/auth/password/login", json={"email": ALICE, "password": "from the wizard"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin endpoints (Settings > Sign-in)
# ---------------------------------------------------------------------------

ADMIN = {"id": 99, "email": "admin@" + ALLOWED_DOMAIN}


@pytest.fixture
def admin_routes(monkeypatch):
    import chat.routes.admin as admin_routes
    monkeypatch.setattr(admin_routes, "is_admin", lambda email: email == ADMIN["email"])
    writes = []
    monkeypatch.setattr("config.server_config.update_server_config", writes.append)
    monkeypatch.setattr(
        "config.server_config.load_server_config",
        lambda: {"allowed_login_emails": ["old@friend.test"]},
    )
    admin_routes.config_writes = writes
    return admin_routes


def _request():
    from starlette.requests import Request as StarletteRequest
    return StarletteRequest({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "scheme": "https", "server": ("quest.test", 443), "query_string": b"",
    })


class TestAdminSignIn:
    def test_switch_requires_google_oauth(self, method, admin_routes, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(admin_routes, "_google_login_configured", lambda: False)
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_set_login_method(
                admin_routes.LoginMethodUpdate(login_method="google"), user=ADMIN))
        assert exc.value.detail["error"] == "google_oauth_not_configured"
        assert admin_routes.config_writes == []

    def test_switch_to_google(self, method, admin_routes, monkeypatch):
        monkeypatch.setattr(admin_routes, "_google_login_configured", lambda: True)
        result = _run(admin_routes.admin_set_login_method(
            admin_routes.LoginMethodUpdate(login_method="google"), user=ADMIN))
        assert result == {"login_method": "google"}
        assert admin_routes.config_writes == [{"login_method": "google"}]

    def test_switch_back_to_password_is_file_only(self, method, admin_routes):
        from fastapi import HTTPException
        method("google")
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_set_login_method(
                admin_routes.LoginMethodUpdate(login_method="password"), user=ADMIN))
        assert exc.value.status_code == 400

    def test_non_admin_forbidden(self, method, admin_routes):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_get_sign_in(user={"id": 1, "email": ALICE}))
        assert exc.value.status_code == 403

    def test_invite_link_for_new_user(self, method, admin_routes):
        result = _run(admin_routes.admin_create_password_link(
            admin_routes.PasswordLinkRequest(email=ALICE), _request(), user=ADMIN))
        assert result["url"].startswith("https://quest.test/set-password#token=")
        assert result["account_exists"] is False
        assert result["added_to_allowed_emails"] is False
        token = result["url"].split("#token=", 1)[1]
        info = _run(password_store.get_valid_password_token(token))
        assert info["email"] == ALICE and info["purpose"] == "invite"

    def test_invite_outside_policy_allow_lists_the_address(self, method, admin_routes):
        result = _run(admin_routes.admin_create_password_link(
            admin_routes.PasswordLinkRequest(email=OUTSIDER), _request(), user=ADMIN))
        assert result["added_to_allowed_emails"] is True
        assert admin_routes.config_writes == [
            {"allowed_login_emails": ["old@friend.test", OUTSIDER]},
        ]

    def test_invite_refused_under_google_sign_in(self, method, admin_routes):
        from fastapi import HTTPException
        method("google")
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_create_password_link(
                admin_routes.PasswordLinkRequest(email=ALICE), _request(), user=ADMIN))
        assert exc.value.detail["error"] == "password_login_disabled"
