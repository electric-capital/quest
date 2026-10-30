"""Admin impersonation: forbidden routes and the per-request audit line.

An impersonation session (a session cookie carrying ``imp``, see
auth/session.py) acts as the target user for support, but may not issue,
reveal, rotate or revoke credentials, change the password, or destroy the
account: those routes declare ``forbid_impersonation`` and answer 403
``impersonation_forbidden``. Every request that resolves an identity through
an impersonation session writes one ``[impersonation]`` audit line naming
the admin, the target user, the method and the path.

Covers the dependency itself, the audit line (ImpersonationAuditMiddleware +
get_user_from_cookie), each guarded route group in-process (403 with an
impersonation cookie, the route's normal behaviour without one), and the
real app's route table in a subprocess (importing quest loads every plugin
into the process-global registries; same pattern as
tests/test_spa_docs_routes.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

import auth.session as session_mod
import chat.auth as chat_auth
from auth.config import COOKIE_NAME, COOKIE_VERSION
from auth.session import (
    IMPERSONATION_FORBIDDEN_MESSAGE,
    ImpersonationAuditMiddleware,
    forbid_impersonation,
    get_user_from_cookie,
    session_impersonation_claim,
)

SECRET = "test-secret"
ADMIN = {"id": 1, "email": "admin@example.test", "name": "Admin", "api_key": "admin-key",
         "session_epoch": 0}
TARGET = {"id": 2, "email": "target@example.test", "name": "Target", "api_key": "target-key",
          "session_epoch": 0}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(session_mod, "get_secret_key", lambda: SECRET)
    monkeypatch.setattr("auth.config.allowed_login_domain", lambda: "example.test")


def _cookie(payload) -> str:
    return session_mod.get_session_serializer().dumps(payload)


def _imp_cookie(uid=TARGET["id"], imp=ADMIN["id"]) -> str:
    # "e" / "ie" are both accounts' session epochs (0 for the fixtures above).
    return _cookie({"v": COOKIE_VERSION, "uid": uid, "e": 0, "imp": imp, "ie": 0})


def _normal_cookie(uid=TARGET["id"]) -> str:
    return _cookie({"v": COOKIE_VERSION, "uid": uid, "e": 0})


def _client(app, cookie: str | None = None) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    if cookie is not None:
        client.cookies.set(COOKIE_NAME, cookie)
    return client


def _is_forbidden(resp) -> bool:
    if resp.status_code != 403:
        return False
    detail = resp.json().get("detail")
    return isinstance(detail, dict) and detail.get("error") == "impersonation_forbidden"


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def impersonation(self) -> list[str]:
        return [m for m in self.messages if m.startswith("[impersonation]")]


@pytest.fixture()
def audit_log():
    """Capture auth.session log lines (independent of global logging config)."""
    logger = logging.getLogger("auth.session")
    handler = _Records()
    old_level, old_disabled = logger.level, logger.disabled
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    logger.disabled = old_disabled


@pytest.fixture()
def valid_impersonation(monkeypatch):
    """Make get_user_from_cookie accept the ADMIN-as-TARGET session."""
    users = {ADMIN["id"]: ADMIN, TARGET["id"]: TARGET}

    async def _by_id(uid):
        user = users.get(uid)
        return dict(user) if user else None

    monkeypatch.setattr(session_mod, "get_user_by_id", _by_id)
    monkeypatch.setattr(chat_auth, "is_admin", lambda email: email == ADMIN["email"])


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------

def _guarded_app() -> FastAPI:
    app = FastAPI()

    @app.post("/guarded", dependencies=[Depends(forbid_impersonation)])
    async def guarded():
        return {"ok": True}

    return app


class TestForbidImpersonation:
    def test_impersonation_cookie_gets_403(self):
        resp = _client(_guarded_app(), _imp_cookie()).post("/guarded")
        assert resp.status_code == 403
        assert resp.json()["detail"] == {
            "error": "impersonation_forbidden",
            "message": IMPERSONATION_FORBIDDEN_MESSAGE,
        }

    @pytest.mark.parametrize("cookie", [None, "garbage"])
    def test_no_session_passes_through(self, cookie):
        assert _client(_guarded_app(), cookie).post("/guarded").json() == {"ok": True}

    def test_normal_session_passes_through(self):
        assert _client(_guarded_app(), _normal_cookie()).post("/guarded").json() == {"ok": True}

    @pytest.mark.parametrize("imp", [0, -1, "1", True, 1.5, [], {}])
    def test_malformed_imp_claim_fails_closed(self, imp):
        assert _is_forbidden(_client(_guarded_app(), _imp_cookie(imp=imp)).post("/guarded"))

    def test_claim_needs_a_valid_signature(self):
        forged = URLSafeSerializer("other-secret", salt="quest-session").dumps(
            {"v": COOKIE_VERSION, "uid": TARGET["id"], "imp": ADMIN["id"]}
        )
        # Not a trusted claim -- and it resolves no identity either.
        assert _client(_guarded_app(), forged).post("/guarded").json() == {"ok": True}
        assert _run(get_user_from_cookie(forged)) is None

    def test_non_dict_payload_is_no_claim(self):
        assert _client(_guarded_app(), _cookie("just a string")).post("/guarded").status_code == 200

    def test_claim_helper(self):
        def req(cookie):
            return SimpleNamespace(cookies={COOKIE_NAME: cookie} if cookie else {})

        assert session_impersonation_claim(req(_imp_cookie())) == (TARGET["id"], ADMIN["id"])
        assert session_impersonation_claim(req(_normal_cookie())) is None
        assert session_impersonation_claim(req(None)) is None

    def test_refusal_is_logged(self, audit_log):
        _client(_guarded_app(), _imp_cookie()).post("/guarded")
        assert audit_log.impersonation() == [
            f"[impersonation] refused admin id={ADMIN['id']} as user id={TARGET['id']}: POST /guarded"
        ]


# ---------------------------------------------------------------------------
# Audit line
# ---------------------------------------------------------------------------

def _audited_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ImpersonationAuditMiddleware)

    @app.get("/probe")
    async def probe(request: Request):
        cookie = request.cookies.get(COOKIE_NAME, "")
        # Resolved twice in one request (dependency + handler style).
        first = await get_user_from_cookie(cookie)
        await get_user_from_cookie(cookie)
        return {"email": first["email"] if first else None}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        cookie = websocket.cookies.get(COOKIE_NAME, "")
        # Connect-time auth plus heartbeat re-validations.
        for _ in range(3):
            await get_user_from_cookie(cookie)
        await websocket.send_json({"ok": True})
        await websocket.close()

    return app


def _audit_line(method: str, path: str) -> str:
    return (
        f"[impersonation] admin={ADMIN['email']} (id={ADMIN['id']}) "
        f"as user={TARGET['email']} (id={TARGET['id']}): {method} {path}"
    )


class TestAuditLine:
    def test_one_line_per_impersonated_request(self, valid_impersonation, audit_log):
        client = _client(_audited_app(), _imp_cookie())
        assert client.get("/probe", params={"code": "secret"}).json() == {"email": TARGET["email"]}
        assert audit_log.impersonation() == [_audit_line("GET", "/probe")]
        client.get("/probe")
        assert audit_log.impersonation() == [_audit_line("GET", "/probe")] * 2

    def test_query_string_is_not_logged(self, valid_impersonation, audit_log):
        _client(_audited_app(), _imp_cookie()).get("/probe", params={"code": "secret"})
        assert all("secret" not in m for m in audit_log.messages)

    def test_websocket_connection_logged_once(self, valid_impersonation, audit_log):
        with _client(_audited_app(), _imp_cookie()).websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"ok": True}
        assert audit_log.impersonation() == [_audit_line("WEBSOCKET", "/ws")]

    def test_normal_session_is_not_logged(self, valid_impersonation, audit_log):
        resp = _client(_audited_app(), _normal_cookie()).get("/probe")
        assert resp.json() == {"email": TARGET["email"]}
        assert audit_log.impersonation() == []

    def test_invalid_impersonation_is_not_served_or_logged(self, valid_impersonation, monkeypatch, audit_log):
        monkeypatch.setattr(chat_auth, "is_admin", lambda email: False)
        resp = _client(_audited_app(), _imp_cookie()).get("/probe")
        assert resp.json() == {"email": None}
        assert audit_log.impersonation() == []

    def test_outside_a_request_still_attributed(self, valid_impersonation, audit_log):
        user = _run(get_user_from_cookie(_imp_cookie()))
        assert user["_impersonator_uid"] == ADMIN["id"]
        assert audit_log.impersonation() == [_audit_line("-", "-")]

    def test_middleware_passes_other_scopes_through(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append((scope["type"], session_mod._audit_request.get()))

        _run(ImpersonationAuditMiddleware(inner)({"type": "lifespan"}, None, None))
        assert seen == [("lifespan", None)]


# ---------------------------------------------------------------------------
# Guarded route groups (in-process)
# ---------------------------------------------------------------------------

def _chat_app(monkeypatch) -> FastAPI:
    from chat.routes import router

    app = FastAPI()
    app.include_router(router)

    async def _user():
        return dict(TARGET)

    app.dependency_overrides[chat_auth.get_current_user_cookie_or_apikey_checked] = _user
    return app


class TestInferenceApiKeyRoutes:
    @pytest.fixture()
    def store(self, monkeypatch):
        from db import inference_api_key_store as store

        calls = []

        async def list_keys(user_id):
            calls.append("list")
            return [{"id": "k1", "name": "ci", "token_hint": "abcd"}]

        async def count_keys(user_id):
            calls.append("count")
            return 0

        async def create_key(user_id, name, token):
            calls.append("create")
            return {"id": "k2", "user_id": user_id, "name": name, "token_hint": token[-4:]}

        async def delete_key(user_id, key_id):
            calls.append("delete")
            return True

        monkeypatch.setattr(store, "list_keys", list_keys)
        monkeypatch.setattr(store, "count_keys", count_keys)
        monkeypatch.setattr(store, "create_key", create_key)
        monkeypatch.setattr(store, "delete_key", delete_key)
        monkeypatch.setattr(store, "generate_token", lambda: "qinf-new-token")
        return calls

    def test_forbidden_while_impersonating(self, monkeypatch, store):
        client = _client(_chat_app(monkeypatch), _imp_cookie())
        assert _is_forbidden(client.get("/app/api/inference-api-keys"))
        assert _is_forbidden(client.post("/app/api/inference-api-keys", json={"name": "x"}))
        assert _is_forbidden(client.delete("/app/api/inference-api-keys/k1"))
        assert store == []

    def test_normal_session_manages_keys(self, monkeypatch, store):
        client = _client(_chat_app(monkeypatch), _normal_cookie())
        assert client.get("/app/api/inference-api-keys").json()["keys"][0]["id"] == "k1"
        created = client.post("/app/api/inference-api-keys", json={"name": "ci"})
        assert created.status_code == 200
        assert created.json()["token"] == "qinf-new-token"
        assert client.delete("/app/api/inference-api-keys/k1").json() == {"success": True}


class TestAccountRoutes:
    @pytest.fixture()
    def lookups(self, monkeypatch):
        import db.user_store as user_store

        calls = []

        async def get_user_by_email(email):
            calls.append(email)
            return None  # the handlers then answer 404 user_not_found

        monkeypatch.setattr(user_store, "get_user_by_email", get_user_by_email)
        return calls

    @pytest.mark.parametrize("path", ["/app/api/logout-and-disconnect", "/app/api/delete-account"])
    def test_forbidden_while_impersonating(self, monkeypatch, lookups, path):
        resp = _client(_chat_app(monkeypatch), _imp_cookie()).post(path)
        assert _is_forbidden(resp)
        assert lookups == []

    @pytest.mark.parametrize("path", ["/app/api/logout-and-disconnect", "/app/api/delete-account"])
    def test_normal_session_reaches_the_handler(self, monkeypatch, lookups, path):
        resp = _client(_chat_app(monkeypatch), _normal_cookie()).post(path)
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "user_not_found"
        assert lookups == [TARGET["email"]]

    def test_deprecated_user_endpoint(self, monkeypatch):
        """GET /app/api/user returns users.api_key: refused under impersonation."""
        app = _chat_app(monkeypatch)
        assert _is_forbidden(_client(app, _imp_cookie()).get("/app/api/user"))
        no_session = _client(app).get("/app/api/user")
        assert no_session.status_code == 401
        assert no_session.json()["detail"]["error"] == "not_authenticated"


class TestApiKeyResetRoute:
    def test_auth_reset_api_key(self, monkeypatch):
        import auth.google_login as google_login

        updates = []

        async def update_user_field(email, **fields):
            updates.append((email, fields))
            return True

        monkeypatch.setattr(google_login, "update_user_field", update_user_field)
        app = FastAPI()
        app.include_router(google_login.router)

        assert _is_forbidden(_client(app, _imp_cookie()).post("/auth/reset-api-key"))
        assert updates == []
        # No session: the route's own redirect to the sign-in page.
        resp = _client(app).post("/auth/reset-api-key")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/"


def _fill(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "x", path)


def _route_calls(router):
    for route in router.routes:
        for method in sorted(route.methods):
            yield method, route.path


class TestCredentialConnectionRouters:
    """Every route of the core credential-connection routers is guarded."""

    @pytest.mark.parametrize("module", [
        "auth.service_key", "auth.connections", "auth.airtable",
        "auth.ramp", "auth.google_services",
    ])
    def test_every_route_forbidden_while_impersonating(self, module):
        import importlib

        router = importlib.import_module(module).router
        app = FastAPI()
        app.include_router(router)
        client = _client(app, _imp_cookie())
        calls = list(_route_calls(router))
        assert calls
        for method, path in calls:
            resp = client.request(method, _fill(path))
            assert _is_forbidden(resp), (method, path, resp.status_code, resp.text)

    def test_service_key_normal_session_reaches_the_handler(self):
        import auth.service_key as service_key

        app = FastAPI()
        app.include_router(service_key.router)
        resp = _client(app, _normal_cookie()).post("/auth/service-key/acme", json={"api_key": "sk-1"})
        # The handler's own answer for a service no plugin provides.
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_service"

    def test_connections_without_session_is_the_routes_own_401(self):
        import auth.connections as connections

        app = FastAPI()
        app.include_router(connections.router)
        resp = _client(app).post("/auth/connections/acme", json={"values": {}})
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "not_authenticated"


class TestPluginConnectionRouters:
    def test_mount_guards_every_plugin_route(self, monkeypatch):
        from config import plugins as plugins_mod

        router = APIRouter(prefix="/auth/tp")

        @router.get("")
        async def connect():
            return {"connect": True}

        @router.post("/disconnect")
        async def disconnect():
            return {"disconnected": True}

        plugin = SimpleNamespace(
            id="tp", user_connection=SimpleNamespace(kind="oauth", oauth_router=router),
        )
        monkeypatch.setattr(plugins_mod, "_LOADED", [plugin])
        app = FastAPI()
        plugins_mod.mount_plugin_oauth_routers(app)

        impersonating = _client(app, _imp_cookie())
        assert _is_forbidden(impersonating.get("/auth/tp"))
        assert _is_forbidden(impersonating.post("/auth/tp/disconnect"))
        normal = _client(app, _normal_cookie())
        assert normal.get("/auth/tp").json() == {"connect": True}
        assert normal.post("/auth/tp/disconnect").json() == {"disconnected": True}


# ---------------------------------------------------------------------------
# The real app (subprocess)
# ---------------------------------------------------------------------------

# Routes refused to an impersonation session regardless of plugins.
EXPLICIT_GUARDED = [
    ("GET", "/api/instructions"),
    ("POST", "/api/reset-api-key"),
    ("POST", "/auth/reset-api-key"),
    ("POST", "/auth/password/change"),
    ("GET", "/app/api/user"),
    ("POST", "/app/api/logout-and-disconnect"),
    ("POST", "/app/api/delete-account"),
    ("GET", "/app/api/inference-api-keys"),
    ("POST", "/app/api/inference-api-keys"),
    ("DELETE", "/app/api/inference-api-keys/{key_id}"),
]

# Credential-connection namespaces: every route under them is guarded.
GUARDED_PREFIXES = [
    "/auth/connections", "/auth/service-key", "/auth/airtable",
    "/auth/ramp", "/auth/google-services",
]

# Must stay usable while impersonating (support reads, and the way out).
NOT_GUARDED = [
    ("GET", "/app/api/me"),
    ("GET", "/app/api/settings"),
    ("GET", "/app/api/connectors"),
    ("POST", "/app/api/logout"),
    ("POST", "/app/api/admin/stop-impersonation"),
]

_SCRIPT = r"""
import json, re, sys
import auth.session as session_mod
session_mod.get_secret_key = lambda: "test-secret"
import quest
from fastapi.testclient import TestClient
from auth.config import COOKIE_NAME, COOKIE_VERSION
from auth.session import ImpersonationAuditMiddleware, forbid_impersonation
from config.plugins import get_loaded_plugins, plugin_auth_prefix
from chat.route_table import iter_app_routes

spec = json.loads(sys.argv[1])
app = quest.app

def guarded(dependant):
    return any(d.call is forbid_impersonation or guarded(d) for d in dependant.dependencies)

table = {}
# r.dependant is the effective one: a dependency passed to include_router
# (how plugin OAuth routers get forbid_impersonation) is merged in there,
# not on the APIRoute the plugin declared.
for r in iter_app_routes(app):
    if r.methods and hasattr(r, "dependant"):
        for m in r.methods:
            table[(m, r.path)] = guarded(r.dependant)

prefixes = list(spec["prefixes"])
plugin_prefixes = [
    plugin_auth_prefix(p.id) for p in get_loaded_plugins()
    if p.user_connection is not None and p.user_connection.kind == "oauth"
    and p.user_connection.oauth_router is not None
]
in_namespace = sorted(
    k for k in table
    if any(k[1] == p or k[1].startswith(p + "/") for p in prefixes + plugin_prefixes)
)
to_check = sorted(set(map(tuple, spec["explicit"])) | set(in_namespace))

cookie = session_mod.get_session_serializer().dumps(
    {"v": COOKIE_VERSION, "uid": 2, "e": 0, "imp": 1, "ie": 0}
)
client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
client.cookies.set(COOKIE_NAME, cookie)
responses = []
for method, path in to_check:
    resp = client.request(method, re.sub(r"\{[^}]+\}", "x", path))
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    error = detail.get("error") if isinstance(detail, dict) else None
    responses.append([method, path, resp.status_code, error])

# The two root endpoints keep working for a normal session.
quest.get_instructions_content = lambda base_url, api_key: "key=" + api_key
async def _update(email, **fields):
    return True
quest.update_user_field = _update
quest.generate_api_key = lambda: "fresh-key"
app.dependency_overrides[quest.get_current_user_cookie_or_apikey] = (
    lambda: {"id": 2, "email": "target@example.test", "api_key": "target-key"}
)
normal = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
normal.cookies.set(
    COOKIE_NAME, session_mod.get_session_serializer().dumps({"v": COOKIE_VERSION, "uid": 2, "e": 0})
)
instructions = normal.get("/api/instructions")
reset = normal.post("/api/reset-api-key")
root = {
    "instructions": [instructions.status_code, instructions.json()],
    "reset": [reset.status_code, reset.json()],
}

print(json.dumps({
    "table": [[m, p, g] for (m, p), g in table.items()],
    "plugin_prefixes": plugin_prefixes,
    "responses": responses,
    "middleware": any(m.cls is ImpersonationAuditMiddleware for m in app.user_middleware),
    "root": root,
}))
"""


@pytest.fixture(scope="module")
def real_app() -> dict:
    spec = {"explicit": EXPLICIT_GUARDED, "prefixes": GUARDED_PREFIXES}
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(spec)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    # The JSON document is the last stdout line (imports may log above it).
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRealApp:
    def test_audit_middleware_installed(self, real_app):
        assert real_app["middleware"] is True

    def test_explicit_routes_declare_the_guard(self, real_app):
        table = {(m, p): g for m, p, g in real_app["table"]}
        for key in EXPLICIT_GUARDED:
            assert table.get(key) is True, key

    def test_credential_namespaces_fully_guarded(self, real_app):
        table = {(m, p): g for m, p, g in real_app["table"]}
        assert real_app["plugin_prefixes"], "no oauth-kind plugin routers loaded"
        for prefix in GUARDED_PREFIXES + real_app["plugin_prefixes"]:
            routes = [k for k in table if k[1] == prefix or k[1].startswith(prefix + "/")]
            assert routes, prefix
            assert all(table[k] for k in routes), [k for k in routes if not table[k]]

    def test_guarded_routes_answer_403_while_impersonating(self, real_app):
        responses = real_app["responses"]
        assert len(responses) > len(EXPLICIT_GUARDED)
        wrong = [r for r in responses if (r[2], r[3]) != (403, "impersonation_forbidden")]
        assert wrong == []

    def test_support_routes_stay_usable(self, real_app):
        table = {(m, p): g for m, p, g in real_app["table"]}
        for key in NOT_GUARDED:
            assert table.get(key) is False, key

    def test_root_endpoints_work_for_a_normal_session(self, real_app):
        status, body = real_app["root"]["instructions"]
        assert status == 200
        assert body == {"instructions": "key=target-key"}
        status, body = real_app["root"]["reset"]
        assert status == 200
        assert body == {"api_key": "fresh-key"}
