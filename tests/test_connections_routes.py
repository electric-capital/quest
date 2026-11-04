"""Tests for the generic multi-connection routes (auth/connections.py).

POST/PUT/DELETE /auth/connections/{service}[/{id}] (issue #84 spec 8.2),
exercised over HTTP against an isolated DB with the test-only ``multiconn``
form-kind plugin (tests/fixture_plugins/multiconn) and, for the api_key
kind, an in-test plugin.
"""

import asyncio
import dataclasses
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth.connections as connections_mod
from config import plugins as plugins_mod
from config.plugin_types import QuestPlugin, UserConnectionSpec

SERVICE = "multiconn"
HOST = "mail.example.com"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB, two users, a recorder for session invalidation."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool,
    )
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models
    import db.user_service_credential_store as cred_store
    import db.user_store as user_store

    models.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(cred_store, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(user_store, "AsyncSessionLocal", session_local)

    users = {
        name: _run(user_store.create_user(
            email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
            name=name.title(),
            api_key=f"k-{uuid.uuid4().hex}",
        ))
        for name in ("alice", "bob")
    }

    invalidated: list[int] = []
    import chat.gemini_api as gemini_api
    monkeypatch.setattr(
        gemini_api, "invalidate_user_sessions",
        lambda user_id: invalidated.append(user_id),
    )
    connections_mod.reset_rate_limits()

    class Env:
        pass

    e = Env()
    e.users = users
    e.invalidated = invalidated
    e.store = cred_store
    e.user_store = user_store
    yield e
    connections_mod.reset_rate_limits()
    sync_engine.dispose()


def client(env, who="alice"):
    app = FastAPI()
    app.include_router(connections_mod.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[connections_mod.get_current_user_cookie_or_apikey] = _current
    return TestClient(app)


def _values(username="work@example.com", password="pw", host=HOST, **extra):
    return {"host": host, "username": username, "password": password, **extra}


def _create(c, service=SERVICE, **body):
    return c.post(f"/auth/connections/{service}", json=body)


def _with_verify(monkeypatch, plugin, verify):
    """Serve ``plugin`` with its verify hook replaced (spec is frozen)."""
    patched = dataclasses.replace(
        plugin,
        user_connection=dataclasses.replace(plugin.user_connection, verify=verify),
    )
    monkeypatch.setattr(plugins_mod, "_LOADED", [patched])


def _assert_no_secrets(resp):
    text = resp.text
    for needle in ("pw", "secret-pass", "password", "oauth_blob", "sk-live"):
        assert needle not in text, f"{needle!r} leaked: {text}"


# ---------------------------------------------------------------------------
# POST (form kind)
# ---------------------------------------------------------------------------


def test_create_two_connections_first_is_default(env, multiconn_plugin):
    c = client(env)
    r1 = _create(c, values=_values("Work@Example.com"), description="  main\n box ")
    assert r1.status_code == 201, r1.text
    conn = r1.json()["connection"]
    assert conn["account"] == "work@example.com"
    assert conn["description"] == "main box"
    assert conn["is_default"] is True
    assert conn["needs_reauth"] is False
    assert conn["warning"] is None
    assert conn["created_at"]
    assert set(conn) == {
        "id", "account", "description", "is_default", "needs_reauth",
        "warning", "created_at",
    }
    _assert_no_secrets(r1)

    r2 = _create(c, values=_values("home", host="mail.example.org"))
    assert r2.status_code == 201, r2.text
    assert r2.json()["connection"]["account"] == "home@mail.example.org"
    assert r2.json()["connection"]["is_default"] is False

    alice = env.users["alice"]["id"]
    rows = _run(env.store.list_connections(alice, SERVICE))
    assert [r["account"] for r in rows] == ["work@example.com", "home@mail.example.org"]
    # Stored blob: values + verify extras.
    assert rows[1]["oauth_blob"] == {
        "verified_host": "mail.example.org",
        "values": _values("home", host="mail.example.org"),
    }
    assert env.invalidated == [alice, alice]


def test_duplicate_account_is_409_case_insensitive(env, multiconn_plugin):
    c = client(env)
    first = _create(c, values=_values("work@example.com")).json()["connection"]
    r = _create(c, values=_values("WORK@example.COM"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "connection_exists"
    assert detail["connection_id"] == first["id"]


def test_connection_limit(env, multiconn_plugin):
    c = client(env)
    for name in ("a", "b", "c"):
        assert _create(c, values=_values(f"{name}@x.com")).status_code == 201
    r = _create(c, values=_values("d@x.com"))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "connection_limit"
    assert r.json()["detail"]["limit"] == 3
    # Re-adding an existing account at the cap says connection_exists.
    r = _create(c, values=_values("a@x.com"))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "connection_exists"


@pytest.mark.parametrize("values, bad_field", [
    ({"host": HOST, "username": "u"}, "password"),                     # missing required
    ({"host": HOST, "username": "  ", "password": "pw"}, "username"),  # blank required
    ({"host": "evil.example.net", "username": "u", "password": "pw"}, "host"),  # not an option
    ({"host": HOST, "username": "u", "password": "pw", "extra": "x"}, "extra"),  # unknown key
    ({"host": HOST, "username": 5, "password": "pw"}, "username"),     # non-string
    ({"host": HOST, "username": "u", "password": "pw", "port": True}, "port"),
])
def test_invalid_values(env, multiconn_plugin, values, bad_field):
    r = _create(client(env), values=values)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_values"
    assert bad_field in detail["fields"]
    assert env.invalidated == []


def test_values_must_be_object_and_body_keys_known(env, multiconn_plugin):
    c = client(env)
    r = _create(c, values=["x"])
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_values"
    r = _create(c, values=_values(), account="x@y")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_request"


def test_description_too_long(env, multiconn_plugin):
    r = _create(client(env), values=_values(), description="x" * 201)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_description"


def test_verify_failed_returns_hook_message(env, multiconn_plugin):
    r = _create(client(env), values=_values(password="wrong"))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail == {"error": "verify_failed", "message": "Login failed: bad password"}
    assert _run(env.store.list_connections(env.users["alice"]["id"], SERVICE)) == []


def test_verify_timeout(env, multiconn_plugin, monkeypatch):
    monkeypatch.setattr(connections_mod, "VERIFY_TIMEOUT_SECONDS", 0.05)
    r = _create(client(env), values=_values(password="slow"))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "verify_failed"


def test_verify_exception_is_generic(env, multiconn_plugin, monkeypatch):
    async def _boom(values):
        raise RuntimeError(f"cannot reach {values['password']}")

    _with_verify(monkeypatch, multiconn_plugin, _boom)
    r = _create(client(env), values=_values(password="secret-pass"))
    assert r.status_code == 400
    assert r.json()["detail"] == {
        "error": "verify_failed", "message": "Could not verify the connection.",
    }
    _assert_no_secrets(r)


def test_verify_without_account_fails(env, multiconn_plugin, monkeypatch):
    from config.plugin_types import VerifyResult

    async def _no_account(values):
        return VerifyResult(account="  ")

    _with_verify(monkeypatch, multiconn_plugin, _no_account)
    r = _create(client(env), values=_values())
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "verify_failed"


def test_rate_limit_after_ten_attempts(env, multiconn_plugin):
    c = client(env)
    for _ in range(10):
        assert _create(c, values=_values(password="wrong")).status_code == 400
    r = _create(c, values=_values(password="wrong"))
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "rate_limited"
    # Per user: bob is unaffected.
    assert _create(client(env, "bob"), values=_values()).status_code == 201
    # Validation failures do not count as attempts (no verify ran).
    connections_mod.reset_rate_limits()
    for _ in range(12):
        _create(c, values={"host": "nope"})
    assert _create(c, values=_values()).status_code == 201


def test_rate_limit_window_expires(env, multiconn_plugin, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connections_mod.time, "monotonic", lambda: now[0])
    c = client(env)
    for _ in range(10):
        _create(c, values=_values(password="wrong"))
    assert _create(c, values=_values(password="wrong")).status_code == 429
    now[0] += 61
    assert _create(c, values=_values()).status_code == 201


# ---------------------------------------------------------------------------
# 404s
# ---------------------------------------------------------------------------


def test_unknown_and_single_connection_services_404(env, multiconn_plugin, example_plugin):
    c = client(env)
    for service in ("nope", example_plugin.id):
        r = _create(c, service=service, values=_values())
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "unknown_service"
        assert c.put(f"/auth/connections/{service}/1", json={}).status_code == 404
        assert c.delete(f"/auth/connections/{service}/1").status_code == 404


def test_other_users_row_is_404(env, multiconn_plugin):
    bob_conn = _create(client(env, "bob"), values=_values()).json()["connection"]
    c = client(env)
    url = f"/auth/connections/{SERVICE}/{bob_conn['id']}"
    for resp in (
        c.put(url, json={"description": "mine"}),
        c.put(url, json={"is_default": True}),
        c.delete(url),
    ):
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "connection_not_found"
    assert c.delete(f"/auth/connections/{SERVICE}/999999").status_code == 404


# ---------------------------------------------------------------------------
# PUT / DELETE
# ---------------------------------------------------------------------------


def test_default_switch_and_delete_promotes(env, multiconn_plugin):
    c = client(env)
    a = _create(c, values=_values("a@x.com")).json()["connection"]
    b = _create(c, values=_values("b@x.com")).json()["connection"]
    alice = env.users["alice"]["id"]

    r = c.put(f"/auth/connections/{SERVICE}/{b['id']}", json={"is_default": True})
    assert r.status_code == 200
    assert r.json()["connection"]["is_default"] is True
    rows = _run(env.store.list_connections(alice, SERVICE))
    assert [(r["account"], r["is_default"]) for r in rows] == [
        ("b@x.com", True), ("a@x.com", False),
    ]

    r = c.put(f"/auth/connections/{SERVICE}/{a['id']}", json={"is_default": False})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "cannot_unset_default"

    env.invalidated.clear()
    r = c.delete(f"/auth/connections/{SERVICE}/{b['id']}")
    assert r.status_code == 204
    rows = _run(env.store.list_connections(alice, SERVICE))
    assert [(r["account"], r["is_default"]) for r in rows] == [("a@x.com", True)]
    assert env.invalidated == [alice]


def test_put_description(env, multiconn_plugin):
    c = client(env)
    a = _create(c, values=_values()).json()["connection"]
    url = f"/auth/connections/{SERVICE}/{a['id']}"
    r = c.put(url, json={"description": " new\tdesc "})
    assert r.status_code == 200
    assert r.json()["connection"]["description"] == "new desc"
    r = c.put(url, json={"description": ""})
    assert r.json()["connection"]["description"] is None
    r = c.put(url, json={"description": "y" * 300})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_description"
    r = c.put(url, json={"bogus": 1})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_request"


def test_put_values_reverifies_and_keeps_secret_on_empty(env, multiconn_plugin, monkeypatch):
    c = client(env)
    a = _create(c, values=_values(password="secret-pass")).json()["connection"]
    url = f"/auth/connections/{SERVICE}/{a['id']}"
    alice = env.users["alice"]["id"]

    seen = []
    spec = multiconn_plugin.user_connection
    real_verify = spec.verify

    async def _spy(values):
        seen.append(dict(values))
        return await real_verify(values)

    _with_verify(monkeypatch, multiconn_plugin, _spy)
    # Empty password keeps the stored one; port is new.
    r = c.put(url, json={"values": {"password": "", "port": "993"}})
    assert r.status_code == 200, r.text
    _assert_no_secrets(r)
    assert seen[-1]["password"] == "secret-pass"
    row = _run(env.store.get_connection(alice, SERVICE, row_id=a["id"]))
    assert row["oauth_blob"]["values"] == _values(password="secret-pass", port="993")

    # A new password is used.
    r = c.put(url, json={"values": {"password": "fresh"}})
    assert r.status_code == 200
    row = _run(env.store.get_connection(alice, SERVICE, row_id=a["id"]))
    assert row["oauth_blob"]["values"]["password"] == "fresh"

    # Different account -> account_mismatch, nothing stored.
    r = c.put(url, json={"values": {"username": "other@example.com"}})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "account_mismatch"
    row = _run(env.store.get_connection(alice, SERVICE, row_id=a["id"]))
    assert row["oauth_blob"]["values"]["username"] == "work@example.com"

    # Verify failure -> verify_failed, description untouched.
    r = c.put(url, json={"values": {"password": "wrong"}, "description": "zzz"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "verify_failed"
    row = _run(env.store.get_connection(alice, SERVICE, row_id=a["id"]))
    assert row["description"] is None

    # Invalid merged values -> invalid_values.
    r = c.put(url, json={"values": {"host": "evil.example.net"}})
    assert r.status_code == 400
    assert r.json()["detail"]["fields"] == {"host": "Not one of the allowed choices."}


def test_put_values_case_insensitive_account(env, multiconn_plugin):
    c = client(env)
    a = _create(c, values=_values("work@example.com")).json()["connection"]
    r = c.put(
        f"/auth/connections/{SERVICE}/{a['id']}",
        json={"values": {"username": "WORK@Example.com"}},
    )
    assert r.status_code == 200, r.text


def test_writes_invalidate_sessions(env, multiconn_plugin):
    c = client(env)
    alice = env.users["alice"]["id"]
    a = _create(c, values=_values()).json()["connection"]
    c.put(f"/auth/connections/{SERVICE}/{a['id']}", json={"description": "d"})
    c.delete(f"/auth/connections/{SERVICE}/{a['id']}")
    assert env.invalidated == [alice, alice, alice]


# ---------------------------------------------------------------------------
# Other kinds
# ---------------------------------------------------------------------------


def _api_key_plugin(account_from=lambda row: (row.get("secret") or "")[3:] or None):
    return QuestPlugin(
        id="keysvc",
        label="Key Service",
        user_connection=UserConnectionSpec(
            kind="api_key",
            connected=lambda row: bool(row.get("secret")),
            multi_connection=True,
            connection_limit=5,
            account_from=account_from,
            validate_key=lambda key: None if key.startswith("sk-") else "Bad key format",
        ),
    )


def test_api_key_kind_create(env, monkeypatch):
    monkeypatch.setattr(plugins_mod, "_LOADED", [_api_key_plugin()])
    c = client(env)
    r = _create(c, service="keysvc", api_key="sk-live-Team", description="team")
    assert r.status_code == 201, r.text
    assert r.json()["connection"]["account"] == "live-team"
    _assert_no_secrets(r)
    row = _run(env.store.list_connections(env.users["alice"]["id"], "keysvc"))[0]
    assert row["secret"] == "sk-live-Team"

    r = _create(c, service="keysvc", api_key="nope")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_key"
    r = _create(c, service="keysvc", values=_values())
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_request"


def test_api_key_kind_account_unresolved(env, monkeypatch):
    monkeypatch.setattr(
        plugins_mod, "_LOADED", [_api_key_plugin(account_from=lambda row: None)],
    )
    r = _create(client(env), service="keysvc", api_key="sk-abc")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "account_unresolved"


def test_oauth_kind_create_is_use_connect_url(env, monkeypatch):
    plugin = QuestPlugin(
        id="oauthsvc", label="OAuth Service",
        user_connection=UserConnectionSpec(
            kind="oauth", connected=lambda row: True,
            multi_connection=True, account_from=lambda row: "x",
        ),
    )
    monkeypatch.setattr(plugins_mod, "_LOADED", [plugin])
    r = _create(client(env), service="oauthsvc", values={})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "use_connect_url"


# ---------------------------------------------------------------------------
# Integration review fixes
# ---------------------------------------------------------------------------


def test_real_auth_dependency_401_without_credentials(env, multiconn_plugin):
    app = FastAPI()
    app.include_router(connections_mod.router)
    c = TestClient(app)
    for resp in (
        c.post(f"/auth/connections/{SERVICE}", json={"values": _values()}),
        c.put(f"/auth/connections/{SERVICE}/1", json={"description": "x"}),
        c.delete(f"/auth/connections/{SERVICE}/1"),
    ):
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "not_authenticated"
    assert _run(env.store.list_connections(env.users["alice"]["id"], SERVICE)) == []


def test_quest_app_mounts_connection_routes():
    """quest.app serves the routes (subprocess: importing quest loads every
    plugin into the process-global registries)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import json, quest\n"
        "from chat.route_table import iter_app_routes\n"
        "routes = sorted({(m, r.path) for r in iter_app_routes(quest.app)"
        " if r.methods for m in r.methods})\n"
        "print(json.dumps(routes))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    routes = {tuple(r) for r in json.loads(proc.stdout.strip().splitlines()[-1])}
    assert ("POST", "/auth/connections/{service}") in routes
    assert ("PUT", "/auth/connections/{service}/{connection_id}") in routes
    assert ("DELETE", "/auth/connections/{service}/{connection_id}") in routes


def test_rate_limit_retry_after_header(env, multiconn_plugin):
    c = client(env)
    for _ in range(10):
        _create(c, values=_values(password="wrong"))
    r = _create(c, values=_values(password="wrong"))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == r.json()["detail"]["retry_after"] >= 1


def test_rate_limit_applies_to_put_values(env, multiconn_plugin):
    c = client(env)
    a = _create(c, values=_values()).json()["connection"]  # 1 attempt
    url = f"/auth/connections/{SERVICE}/{a['id']}"
    for _ in range(9):
        assert c.put(url, json={"values": {"password": "wrong"}}).status_code == 400
    r = c.put(url, json={"values": {"password": "fresh"}})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "rate_limited"
    # Description-only edits never verify, so they are not limited.
    assert c.put(url, json={"description": "d"}).status_code == 200


def test_form_value_length_cap(env, multiconn_plugin):
    cap = connections_mod.MAX_FORM_VALUE_LENGTH
    c = client(env)
    r = _create(c, values=_values(password="p" * (cap + 1)))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_values"
    assert detail["fields"] == {"password": f"Must be at most {cap} characters."}
    assert _create(c, values=_values(password="p" * cap)).status_code == 201


@pytest.mark.parametrize("hook_result", ["raise", ["mail.example.com"], "a-string", None])
def test_field_options_hook_failure_during_post(env, multiconn_plugin, monkeypatch, hook_result):
    def _hook():
        if hook_result == "raise":
            raise RuntimeError("options backend down")
        return hook_result

    spec = dataclasses.replace(multiconn_plugin.user_connection, field_options=_hook)
    monkeypatch.setattr(
        plugins_mod, "_LOADED",
        [dataclasses.replace(multiconn_plugin, user_connection=spec)],
    )
    # The fixture's host select has no static options, so the fallback
    # leaves nothing to choose: a clean 400, never a 500.
    r = _create(client(env), values=_values())
    assert r.status_code == 400
    assert r.json()["detail"]["fields"] == {"host": "Not one of the allowed choices."}


def test_field_options_fallback_uses_static_options(multiconn_plugin):
    from config.plugin_types import CredentialField

    spec = dataclasses.replace(
        multiconn_plugin.user_connection,
        fields=(CredentialField(key="host", label="Server", type="select",
                                options=("static.example.com",)),),
        field_options=lambda: ["not", "a", "dict"],
    )
    assert connections_mod.resolve_field_options(spec) == {"host": ["static.example.com"]}


def test_put_empty_body_is_noop(env, multiconn_plugin):
    """PUT {} changes nothing and returns the current view (200)."""
    c = client(env)
    a = _create(c, values=_values(), description="keep").json()["connection"]
    env.invalidated.clear()
    r = c.put(f"/auth/connections/{SERVICE}/{a['id']}", json={})
    assert r.status_code == 200
    assert r.json()["connection"] == a
    assert env.invalidated == []


def test_put_values_on_legacy_unnamed_row(env, multiconn_plugin):
    alice = env.users["alice"]["id"]
    # A pre-multi-connection row: written through the legacy API, account ''.
    _run(env.store.upsert_credential(
        alice, SERVICE, oauth_blob={"values": _values()},
    ))
    row = _run(env.store.list_connections(alice, SERVICE))[0]
    assert row["account"] == ""
    r = client(env).put(
        f"/auth/connections/{SERVICE}/{row['id']}", json={"values": {"password": "x"}},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == {
        "error": "account_mismatch",
        "message": "This connection has no account name; delete it and add it again.",
    }


def test_create_integrity_error_is_409_conflict(env, multiconn_plugin, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    async def _race(*_a, **_kw):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(env.store, "create_connection", _race)
    r = _create(client(env), values=_values())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conflict"
    assert env.invalidated == []


def test_verify_unsafe_account_is_verify_failed(env, multiconn_plugin, monkeypatch):
    from config.plugin_types import VerifyResult

    async def _spaced(values):
        return VerifyResult(account="two words@example.com")

    _with_verify(monkeypatch, multiconn_plugin, _spaced)
    r = _create(client(env), values=_values())
    assert r.status_code == 400
    assert r.json()["detail"] == {
        "error": "verify_failed",
        "message": (
            "The connection was verified but did not return a usable account "
            "name (account must not contain whitespace)."
        ),
    }


@pytest.mark.parametrize("acct", ["a b", 'a"b', "a`b", "a\nb"])
def test_api_key_unsafe_account_is_unresolved(env, monkeypatch, acct):
    monkeypatch.setattr(
        plugins_mod, "_LOADED", [_api_key_plugin(account_from=lambda row: acct)],
    )
    r = _create(client(env), service="keysvc", api_key="sk-abc")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "account_unresolved"


def test_put_api_key(env, monkeypatch):
    monkeypatch.setattr(plugins_mod, "_LOADED", [_api_key_plugin()])
    c = client(env)
    alice = env.users["alice"]["id"]
    conn = _create(c, service="keysvc", api_key="sk-team").json()["connection"]
    url = f"/auth/connections/keysvc/{conn['id']}"
    env.invalidated.clear()

    # Same account (case-insensitive) -> the secret is replaced.
    r = c.put(url, json={"api_key": " sk-TEAM ", "description": "rotated"})
    assert r.status_code == 200, r.text
    _assert_no_secrets(r)
    assert r.json()["connection"]["description"] == "rotated"
    row = _run(env.store.get_connection(alice, "keysvc", row_id=conn["id"]))
    assert row["secret"] == "sk-TEAM"
    assert env.invalidated == [alice]

    for body, code in (
        ({"api_key": "nope"}, "invalid_key"),
        ({"api_key": "sk-other"}, "account_mismatch"),
        ({"api_key": "sk-"}, "account_unresolved"),
        ({"api_key": ""}, "invalid_request"),
        ({"api_key": 5}, "invalid_request"),
        ({"values": {"x": "y"}}, "invalid_request"),
        ({"api_key": "sk-team", "values": {}}, "invalid_request"),
    ):
        r = c.put(url, json=body)
        assert r.status_code == 400, body
        assert r.json()["detail"]["error"] == code, body
    mismatch = c.put(url, json={"api_key": "sk-other"}).json()["detail"]["message"]
    assert mismatch.startswith("This key appears to belong to other, not team.")
    row = _run(env.store.get_connection(alice, "keysvc", row_id=conn["id"]))
    assert row["secret"] == "sk-TEAM"
    assert row["description"] == "rotated"


def test_put_api_key_is_rate_limited(env, monkeypatch):
    monkeypatch.setattr(plugins_mod, "_LOADED", [_api_key_plugin()])
    c = client(env)
    conn = _create(c, service="keysvc", api_key="sk-team").json()["connection"]
    url = f"/auth/connections/keysvc/{conn['id']}"
    for _ in range(9):
        assert c.put(url, json={"api_key": "sk-other"}).status_code == 400
    r = c.put(url, json={"api_key": "sk-team"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_put_api_key_rejected_for_form_kind(env, multiconn_plugin):
    c = client(env)
    a = _create(c, values=_values()).json()["connection"]
    r = c.put(f"/auth/connections/{SERVICE}/{a['id']}", json={"api_key": "sk-x"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_request"
