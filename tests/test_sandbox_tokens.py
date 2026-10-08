"""Tests for the ephemeral sandbox tokens (chat/sandbox_tokens.py).

Script containers no longer receive the user's long-lived ``users.api_key``
as ``QUEST_API_KEY``. Each ``run_script`` / ``run_python`` run mints a
random per-run token that lives in process memory only, is bound to the
container's clamped timeout, and is revoked the moment the container
exits. The sandbox tool API server accepts ONLY these tokens; the main
app never consults the token store.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from types import SimpleNamespace
from fastapi import HTTPException

import chat.sandbox_tokens as sandbox_tokens
from chat.sandbox_tokens import (
    TOKEN_GRACE_SECONDS,
    TOKEN_PREFIX,
    active_lease_count,
    get_current_sandbox_user,
    issue_sandbox_token,
    resolve_sandbox_token,
    revoke_sandbox_token,
    sandbox_token_lease,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_store():
    sandbox_tokens._leases.clear()
    yield
    sandbox_tokens._leases.clear()


@pytest.fixture()
def _clock(monkeypatch):
    """Controllable monotonic clock for the token store."""
    state = {"now": 1000.0}
    monkeypatch.setattr(sandbox_tokens, "_now", lambda: state["now"])
    return state


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------


class TestTokenStore:
    def test_issue_resolve_revoke(self, _clock):
        tok = issue_sandbox_token(7, ttl_seconds=60, conversation_id="conv-1")
        assert tok.startswith(TOKEN_PREFIX)
        lease = resolve_sandbox_token(tok)
        assert lease is not None
        assert lease.user_id == 7
        assert lease.conversation_id == "conv-1"
        assert active_lease_count() == 1

        revoke_sandbox_token(tok)
        assert resolve_sandbox_token(tok) is None
        assert active_lease_count() == 0
        # Revoking again is a no-op.
        revoke_sandbox_token(tok)

    def test_tokens_are_unique_and_random(self):
        toks = {issue_sandbox_token(1, ttl_seconds=60) for _ in range(50)}
        assert len(toks) == 50
        # 32 random bytes urlsafe-encoded -> 43 chars after the prefix.
        assert all(len(t) - len(TOKEN_PREFIX) >= 40 for t in toks)

    def test_expiry_is_enforced(self, _clock):
        tok = issue_sandbox_token(1, ttl_seconds=30)
        _clock["now"] += 29
        assert resolve_sandbox_token(tok) is not None
        _clock["now"] += 1
        assert resolve_sandbox_token(tok) is None
        # Expired entries are purged, not merely hidden.
        assert tok not in sandbox_tokens._leases

    def test_unknown_or_foreign_shaped_bearers_never_resolve(self):
        issue_sandbox_token(1, ttl_seconds=60)
        assert resolve_sandbox_token("") is None
        assert resolve_sandbox_token("not-a-sandbox-token") is None
        assert resolve_sandbox_token(TOKEN_PREFIX + "nope") is None

    def test_ttl_must_be_positive(self):
        with pytest.raises(ValueError):
            issue_sandbox_token(1, ttl_seconds=0)

    def test_lease_context_revokes_on_every_exit_path(self):
        with sandbox_token_lease(3, ttl_seconds=60) as tok:
            assert resolve_sandbox_token(tok).user_id == 3
        assert resolve_sandbox_token(tok) is None

        with pytest.raises(RuntimeError):
            with sandbox_token_lease(3, ttl_seconds=60) as tok2:
                assert resolve_sandbox_token(tok2) is not None
                raise RuntimeError("container blew up")
        assert resolve_sandbox_token(tok2) is None
        assert active_lease_count() == 0


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, headers: dict | None = None, path: str = "/api/tool-call"):
        self.headers = headers or {}
        self.cookies = {}
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path=path)


class TestSandboxAuthDependency:
    def test_valid_token_loads_user_fresh_from_db(self, monkeypatch):
        calls = []

        async def _get_user_by_id(uid):
            calls.append(uid)
            return {"id": uid, "email": "u@example.com", "api_key": "real-key"}

        import db.user_store as user_store
        monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)

        tok = issue_sandbox_token(11, ttl_seconds=60)
        user = _run(get_current_sandbox_user(_Req({"Authorization": f"Bearer {tok}"})))
        assert user["id"] == 11
        assert calls == [11]

    def test_users_api_key_shaped_bearer_is_rejected_without_db_lookup(self, monkeypatch):
        """The sandbox dependency must never fall back to the users.api_key
        hash lookup: a leaked long-lived key is worthless on the sandbox port."""
        import db.user_store as user_store

        async def _boom(*a, **k):
            raise AssertionError("sandbox auth consulted the users.api_key lookup")

        monkeypatch.setattr(user_store, "get_user_by_api_key", _boom)

        for header in (
            {},
            {"Authorization": "Bearer k-" + uuid.uuid4().hex},
            {"Authorization": "Bearer " + TOKEN_PREFIX + "unknown"},
            {"Authorization": "Basic abc"},
        ):
            with pytest.raises(HTTPException) as exc:
                _run(get_current_sandbox_user(_Req(header)))
            assert exc.value.status_code == 401
            assert exc.value.detail["error"] == "invalid_sandbox_token"

    def test_revoked_token_is_rejected(self, monkeypatch):
        import db.user_store as user_store

        async def _get_user_by_id(uid):
            return {"id": uid, "email": "u@example.com"}

        monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)
        tok = issue_sandbox_token(5, ttl_seconds=60)
        revoke_sandbox_token(tok)
        with pytest.raises(HTTPException) as exc:
            _run(get_current_sandbox_user(_Req({"Authorization": f"Bearer {tok}"})))
        assert exc.value.status_code == 401

    def test_deleted_user_is_rejected(self, monkeypatch):
        import db.user_store as user_store

        async def _get_user_by_id(uid):
            return None

        monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)
        tok = issue_sandbox_token(5, ttl_seconds=60)
        with pytest.raises(HTTPException) as exc:
            _run(get_current_sandbox_user(_Req({"Authorization": f"Bearer {tok}"})))
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Sandbox app: every route authenticates via the sandbox token only
# ---------------------------------------------------------------------------


def _auth_callables(dependant) -> set:
    """Collect every dependency callable in a route's dependency tree."""
    found = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= _auth_callables(sub)
    return found


class TestSandboxAppAuth:
    def test_every_route_auth_dependency_is_overridden(self):
        from chat.sandbox_api import _db_bearer_auth_dependencies, create_sandbox_app

        app = create_sandbox_app()
        db_deps = set(_db_bearer_auth_dependencies())
        assert set(app.dependency_overrides) == db_deps
        assert all(v is get_current_sandbox_user for v in app.dependency_overrides.values())

        for route in app.routes:
            if not hasattr(route, "dependant") or route.path == "/health":
                continue
            deps = _auth_callables(route.dependant)
            # Each script-facing route must declare exactly one of the DB
            # bearer auth dependencies (so the override applies) and no
            # other auth mechanism we did not account for.
            declared = deps & db_deps
            assert declared, f"{route.path} declares no known auth dependency"
            unknown = {
                d for d in deps
                if getattr(d, "__module__", "").startswith(("auth.", "chat.auth"))
                and d not in db_deps
            }
            assert not unknown, f"{route.path} uses un-overridden auth deps: {unknown}"

    def test_tool_call_route_accepts_sandbox_token_and_rejects_users_api_key(self, monkeypatch):
        """End-to-end through the ASGI app: a users.api_key bearer is 401 on
        the sandbox port even when it would resolve on the main app; the
        ephemeral token authenticates and reaches the tool dispatch."""
        import db.user_store as user_store

        real_user = {
            "id": 21, "email": "u@example.com", "name": "U",
            "api_key": "users-api-key-" + uuid.uuid4().hex, "settings": {},
        }

        async def _get_user_by_api_key(key):
            # On the main app this key WOULD authenticate.
            return dict(real_user) if key == real_user["api_key"] else None

        async def _get_user_by_id(uid):
            return dict(real_user) if uid == real_user["id"] else None

        monkeypatch.setattr(user_store, "get_user_by_api_key", _get_user_by_api_key)
        monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)

        from chat.sandbox_api import create_sandbox_app
        app = create_sandbox_app()
        # get_current_time is NOT in the script allowlist: an authenticated
        # request reaches the endpoint body and gets its 400 allowlist
        # error, which is distinguishable from the 401 auth rejection
        # without needing any connector or DB behind the tool.
        body = {"tool_name": "get_current_time", "arguments": {}}

        async def _go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as c:
                r_none = await c.post("/api/tool-call", json=body)
                r_key = await c.post(
                    "/api/tool-call", json=body,
                    headers={"Authorization": f"Bearer {real_user['api_key']}"},
                )
                with sandbox_token_lease(real_user["id"], ttl_seconds=60) as tok:
                    r_tok = await c.post(
                        "/api/tool-call", json=body,
                        headers={"Authorization": f"Bearer {tok}"},
                    )
                # After the lease ends the very same token is dead.
                r_dead = await c.post(
                    "/api/tool-call", json=body,
                    headers={"Authorization": f"Bearer {tok}"},
                )
                return r_none, r_key, r_tok, r_dead

        r_none, r_key, r_tok, r_dead = _run(_go())
        assert r_none.status_code == 401
        assert r_key.status_code == 401
        assert r_key.json()["detail"]["error"] == "invalid_sandbox_token"
        assert r_tok.status_code == 400, r_tok.text
        assert "not available from scripts" in r_tok.json()["error"]
        assert r_dead.status_code == 401

    def test_main_app_auth_never_accepts_a_sandbox_token(self, monkeypatch):
        """chat.auth / auth.session resolve bearers through the users table
        only; a sandbox token is not there, so it is 401 on the main app."""
        import auth.session as session_auth
        import chat.auth as chat_auth
        import db.user_store as user_store

        seen = []

        async def _get_user_by_api_key(key):
            seen.append(key)
            return None

        # Both modules bound the name at import time.
        monkeypatch.setattr(user_store, "get_user_by_api_key", _get_user_by_api_key)
        monkeypatch.setattr(session_auth, "get_user_by_api_key", _get_user_by_api_key)
        monkeypatch.setattr(chat_auth, "get_user_by_api_key", _get_user_by_api_key)

        tok = issue_sandbox_token(1, ttl_seconds=60)
        assert resolve_sandbox_token(tok) is not None
        for dep in (
            chat_auth.get_current_user_cookie_or_apikey,
            session_auth.get_current_user_cookie_or_apikey,
            session_auth.get_current_user,
        ):
            with pytest.raises(HTTPException) as exc:
                _run(dep(_Req({"Authorization": f"Bearer {tok}"})))
            assert exc.value.status_code == 401
        # They asked the users table (and got nothing), never the token store.
        assert seen == [tok, tok, tok]


# ---------------------------------------------------------------------------
# run_script / run_python: token minted per run, revoked at exit
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, on_communicate):
        self._on_communicate = on_communicate
        self.returncode = 0

    async def communicate(self, input=None):
        await self._on_communicate()
        return b"ok\n", b""

    def kill(self):
        pass

    async def wait(self):
        return 0


class TestSandboxHandlersMintPerRunTokens:
    @pytest.fixture()
    def _stub_container(self, monkeypatch, tmp_path):
        """Stub podman: capture argv, and while the 'container' runs check
        that its QUEST_API_KEY resolves; record what happens after."""
        import chat.gemini_api.tool_handlers.sandbox as sandbox_mod
        from chat.gemini_api.tool_handlers import _common

        captured: dict = {}

        async def _ws(conversation_id, project_id=None):
            return tmp_path

        monkeypatch.setattr(sandbox_mod, "conversation_workspace_dir", _ws)
        monkeypatch.setattr(
            sandbox_mod, "_publish_file_list_changed", lambda *a, **k: None,
        )
        monkeypatch.setattr(
            sandbox_mod, "get_sandbox_seccomp_profile_path",
            lambda: str(tmp_path / "seccomp.json"),
        )

        async def _spawn(*argv, **kwargs):
            captured["argv"] = list(argv)
            env = dict(
                a.split("=", 1) for a in argv if "=" in a and a.startswith("QUEST_")
            )
            captured["env"] = env

            async def _during_run():
                tok = env.get("QUEST_API_KEY")
                lease = resolve_sandbox_token(tok) if tok else None
                captured["lease_during_run"] = lease

            return _FakeProcess(_during_run)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        return captured

    def test_run_python_injects_ephemeral_token_not_users_key(self, _stub_container):
        from chat.gemini_api.tool_handlers.sandbox import _handle_run_python

        result = json.loads(_run(_handle_run_python(
            42, "conv-9", "print('hi')", timeout=10,
        )))
        assert result["exit_code"] == 0

        env = _stub_container["env"]
        tok = env["QUEST_API_KEY"]
        assert tok.startswith(TOKEN_PREFIX)
        # Bound to the launching user + conversation while the container ran...
        lease = _stub_container["lease_during_run"]
        assert lease is not None
        assert lease.user_id == 42
        assert lease.conversation_id == "conv-9"
        # ...and dead the moment it exited.
        assert resolve_sandbox_token(tok) is None
        assert active_lease_count() == 0

    def test_run_script_token_ttl_tracks_clamped_timeout(self, _stub_container, _clock, tmp_path):
        from chat.gemini_api.tool_handlers.sandbox import _handle_run_script
        from chat.gemini_api.constants import SCRIPT_RUNNER_MAX_TIMEOUT

        (tmp_path / "x.py").write_text("print(1)\n")
        # Ask for far more than the cap: the token must expire with the
        # clamped container lifetime, not the requested one.
        _run(_handle_run_script(42, "conv-9", "x.py", timeout=10_000))
        lease = _stub_container["lease_during_run"]
        assert lease is not None
        assert lease.expires_at == pytest.approx(
            _clock["now"] + SCRIPT_RUNNER_MAX_TIMEOUT + TOKEN_GRACE_SECONDS
        )

    def test_public_profile_mints_nothing_usable(self, _stub_container):
        from chat.gemini_api.tool_handlers.sandbox import _handle_run_python

        _run(_handle_run_python(42, "conv-9", "print('hi')", public=True))
        assert "QUEST_API_KEY" not in _stub_container["env"]
        assert "QUEST_PORT" not in _stub_container["env"]
        assert active_lease_count() == 0

    def test_token_revoked_even_when_container_times_out(self, monkeypatch, _stub_container):
        from chat.gemini_api.tool_handlers.sandbox import _handle_run_python
        import chat.gemini_api.tool_handlers.sandbox as sandbox_mod

        async def _hang(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(sandbox_mod.asyncio, "wait_for", _hang)
        result = json.loads(_run(_handle_run_python(42, "conv-9", "while True: pass")))
        assert result["timed_out"] is True
        assert resolve_sandbox_token(_stub_container["env"]["QUEST_API_KEY"]) is None
        assert active_lease_count() == 0

    def test_dispatch_no_longer_threads_users_api_key(self):
        """The dispatch table passes no api_key to the sandbox handlers, so a
        blank or scrubbed users.api_key (inference runs) cannot affect the
        bridge."""
        import inspect
        from chat.gemini_api.tool_handlers.sandbox import (
            _handle_run_python, _handle_run_script,
        )
        for fn in (_handle_run_python, _handle_run_script):
            assert "api_key" not in inspect.signature(fn).parameters


# ---------------------------------------------------------------------------
# Inference-run leases: the bridge refuses mutating tools and routes
# ---------------------------------------------------------------------------


class TestRestrictedLeases:
    def test_lease_flag_defaults_off_and_round_trips(self):
        tok = issue_sandbox_token(5, ttl_seconds=60)
        assert resolve_sandbox_token(tok).block_mutating_tools is False
        tok2 = issue_sandbox_token(5, ttl_seconds=60, block_mutating_tools=True)
        assert resolve_sandbox_token(tok2).block_mutating_tools is True
        with sandbox_token_lease(5, ttl_seconds=60, block_mutating_tools=True) as tok3:
            assert resolve_sandbox_token(tok3).block_mutating_tools is True

    def _auth(self, monkeypatch):
        async def _get_user_by_id(uid):
            return {"id": uid, "email": "u@example.com", "api_key": ""}
        import db.user_store as user_store
        monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)

    def test_dependency_exposes_lease_and_blocks_mutating_routes(self, monkeypatch):
        from chat.sandbox_tokens import get_sandbox_lease
        self._auth(monkeypatch)
        tok = issue_sandbox_token(5, ttl_seconds=60, block_mutating_tools=True)
        hdr = {"Authorization": f"Bearer {tok}"}

        req = _Req(hdr, path="/api/tool-call")
        user = _run(get_current_sandbox_user(req))
        assert user["id"] == 5
        assert get_sandbox_lease(req).block_mutating_tools is True

        for path in ("/api/gmail-simple/drafts", "/api/gmail-simple/send-self"):
            with pytest.raises(HTTPException) as exc_info:
                _run(get_current_sandbox_user(_Req(hdr, path=path)))
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail["error"] == "mutating_route_blocked"

    def test_unrestricted_lease_reaches_mutating_routes(self, monkeypatch):
        self._auth(monkeypatch)
        tok = issue_sandbox_token(5, ttl_seconds=60)
        user = _run(get_current_sandbox_user(
            _Req({"Authorization": f"Bearer {tok}"}, path="/api/gmail-simple/drafts"),
        ))
        assert user["id"] == 5

    def test_main_app_callers_hold_no_lease(self):
        from chat.sandbox_tokens import get_sandbox_lease
        assert get_sandbox_lease(_Req({})) is None

    def test_bridge_refuses_mutating_tool_on_restricted_lease(self, monkeypatch):
        """End-to-end through the sandbox ASGI app: a container launched by
        an inference run cannot draft mail via POST /api/tool-call, while a
        read tool on the same lease still reaches dispatch."""
        self._auth(monkeypatch)
        from chat.sandbox_api import create_sandbox_app
        app = create_sandbox_app()
        tok = issue_sandbox_token(5, ttl_seconds=60, block_mutating_tools=True)
        hdr = {"Authorization": f"Bearer {tok}"}

        async def _go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as c:
                blocked = await c.post(
                    "/api/tool-call", headers=hdr,
                    json={"tool_name": "create_gmail_draft", "arguments": {}},
                )
                route_blocked = await c.post(
                    "/api/gmail-simple/send-self", headers=hdr,
                    json={"subject": "x", "body_md": "y"},
                )
                # memory_list is allow-listed and read-only: it passes the
                # gate and fails (if at all) inside the handler, not with
                # the restriction error.
                read = await c.post(
                    "/api/tool-call", headers=hdr,
                    json={"tool_name": "memory_list", "arguments": {}},
                )
                return blocked, route_blocked, read

        blocked, route_blocked, read = _run(_go())
        assert blocked.status_code == 403
        assert "read-only" in blocked.json()["error"]
        assert route_blocked.status_code == 403
        assert route_blocked.json()["detail"]["error"] == "mutating_route_blocked"
        assert read.status_code != 403

    def test_handlers_thread_the_flag_into_the_lease(self, monkeypatch, tmp_path):
        import chat.gemini_api.tool_handlers.sandbox as sandbox_mod
        from chat.gemini_api.tool_handlers.sandbox import _handle_run_python

        async def _ws(conversation_id, project_id=None):
            return tmp_path
        monkeypatch.setattr(sandbox_mod, "conversation_workspace_dir", _ws)
        monkeypatch.setattr(sandbox_mod, "_publish_file_list_changed", lambda *a, **k: None)
        monkeypatch.setattr(
            sandbox_mod, "get_sandbox_seccomp_profile_path",
            lambda: str(tmp_path / "seccomp.json"),
        )
        seen = {}

        async def _spawn(*argv, **kwargs):
            env = dict(a.split("=", 1) for a in argv if a.startswith("QUEST_"))

            async def _during_run():
                seen["lease"] = resolve_sandbox_token(env["QUEST_API_KEY"])
            return _FakeProcess(_during_run)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        _run(_handle_run_python(42, "conv-9", "print(1)", block_mutating_tools=True))
        assert seen["lease"].block_mutating_tools is True
