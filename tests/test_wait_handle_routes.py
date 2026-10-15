"""Tests for the wait-handle REST routes in chat/wait_handle_routes.py.

``GET /app/api/wait-handles/{id}`` and ``POST /app/api/wait-handles/{id}/resolve``
run against an isolated SQLite file through a minimal FastAPI app that
mounts only this router, with the auth dependency overridden. The resume
kick and the realtime bus are replaced with recorders.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from importlib import reload

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_wait_routes_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.tool_wait_handle_store as store_mod
    reload(store_mod)
    import chat.wait_handle_routes as routes_mod

    models_mod.Base.metadata.create_all(engine_mod.engine)

    kicks: list[tuple] = []
    events: list[tuple] = []
    monkeypatch.setattr(
        routes_mod.wait_resume, "maybe_kick_resume",
        lambda app, user, cid: kicks.append((user["id"], cid)),
    )
    monkeypatch.setattr(
        routes_mod.bus, "publish_to_user",
        lambda user_id, event: events.append((user_id, event)),
    )

    yield {
        "store": store_mod,
        "models": models_mod,
        "routes": routes_mod,
        "kicks": kicks,
        "events": events,
    }

    shutil.rmtree(tmpdir, ignore_errors=True)


def _make_user(env, label="u") -> dict:
    from db.engine import AsyncSessionLocal

    models_mod = env["models"]

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"{label}-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=label,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {"id": u.id, "email": u.email, "name": u.name}

    return _run(_create())


def _client_as(env, user) -> TestClient:
    routes_mod = env["routes"]
    app = FastAPI()
    app.include_router(routes_mod.router)

    async def _current():
        return user

    app.dependency_overrides[routes_mod.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def _handle(env, user, conversation_id="conv-1"):
    return _run(env["store"].create_handle(
        user_id=user["id"],
        conversation_id=conversation_id,
        kind="user_subagent",
        tool_id="",
        payload={"subagent_conversation_id": "sub-1"},
    ))


# ---------------------------------------------------------------------------
# GET /app/api/wait-handles/{id}
# ---------------------------------------------------------------------------


def test_get_returns_own_handle(env):
    user = _make_user(env)
    h = _handle(env, user)

    resp = _client_as(env, user).get(f"/app/api/wait-handles/{h['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == h["id"]
    assert body["status"] == "pending"
    assert body["kind"] == "user_subagent"
    assert body["conversation_id"] == "conv-1"


def test_get_unknown_handle_is_404(env):
    user = _make_user(env)

    resp = _client_as(env, user).get(f"/app/api/wait-handles/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "wait_handle_not_found"


def test_get_other_users_handle_is_404(env):
    owner = _make_user(env, "owner")
    other = _make_user(env, "other")
    h = _handle(env, owner)

    resp = _client_as(env, other).get(f"/app/api/wait-handles/{h['id']}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "wait_handle_not_found"


# ---------------------------------------------------------------------------
# POST /app/api/wait-handles/{id}/resolve
# ---------------------------------------------------------------------------


def _resolve_url(handle_id):
    return f"/app/api/wait-handles/{handle_id}/resolve"


def test_resolve_flips_row_publishes_and_kicks_resume(env):
    user = _make_user(env)
    h = _handle(env, user)

    resp = _client_as(env, user).post(_resolve_url(h["id"]), json={
        "status": "accepted",
        "response": {"answer": 42},
        "feedback": "looks good",
        "correlation_kind": "memory",
        "correlation_id": "m-1",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    # feedback is merged into the response dict.
    assert body["response"] == {"answer": 42, "feedback": "looks good"}
    assert body["correlation_kind"] == "memory"
    assert body["correlation_id"] == "m-1"

    stored = _run(env["store"].get_handle(h["id"]))
    assert stored["status"] == "accepted"
    assert stored["resolved_at"] is not None

    assert env["kicks"] == [(user["id"], "conv-1")]
    [(event_user, event)] = env["events"]
    assert event_user == user["id"]
    assert event["type"] == "wait_handle_resolved"
    assert event["handle_id"] == h["id"]
    assert event["status"] == "accepted"
    assert event["response"] == {"answer": 42, "feedback": "looks good"}


def test_resolve_feedback_alone_builds_response(env):
    user = _make_user(env)
    h = _handle(env, user)

    resp = _client_as(env, user).post(_resolve_url(h["id"]), json={
        "status": "rejected", "feedback": "no thanks",
    })

    assert resp.status_code == 200
    assert resp.json()["response"] == {"feedback": "no thanks"}


def test_resolve_rejects_unresolvable_status(env):
    user = _make_user(env)
    h = _handle(env, user)

    for status in ("timed_out", "pending", "stopped", "bogus"):
        resp = _client_as(env, user).post(
            _resolve_url(h["id"]), json={"status": status},
        )
        assert resp.status_code == 422, status

    assert _run(env["store"].get_handle(h["id"]))["status"] == "pending"
    assert env["kicks"] == []


def test_resolve_unknown_handle_is_404(env):
    user = _make_user(env)

    resp = _client_as(env, user).post(
        _resolve_url(uuid.uuid4()), json={"status": "accepted"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "wait_handle_not_found"
    assert env["kicks"] == []


def test_resolve_other_users_handle_is_404(env):
    owner = _make_user(env, "owner")
    other = _make_user(env, "other")
    h = _handle(env, owner)

    resp = _client_as(env, other).post(
        _resolve_url(h["id"]), json={"status": "accepted"},
    )

    assert resp.status_code == 404
    assert _run(env["store"].get_handle(h["id"]))["status"] == "pending"
    assert env["kicks"] == []


def test_resolve_already_resolved_is_400(env):
    """A handle the expiry sweep already timed out cannot be resolved."""
    user = _make_user(env)
    h = _handle(env, user)
    _run(env["store"].mark_timed_out([h["id"]], user["id"]))

    resp = _client_as(env, user).post(
        _resolve_url(h["id"]), json={"status": "accepted"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "already_resolved"
    assert _run(env["store"].get_handle(h["id"]))["status"] == "timed_out"
    assert env["kicks"] == []
    assert env["events"] == []


def test_resolve_race_lost_is_409(env, monkeypatch):
    """The row passed the pending pre-check but another writer (e.g. the
    expiry sweep) flipped it before the conditional update landed."""
    user = _make_user(env)
    h = _handle(env, user)
    store = env["store"]
    real_resolve = store.resolve_handle

    async def _timeout_then_resolve(handle_id, **kwargs):
        await store.mark_timed_out([handle_id], user["id"])
        return await real_resolve(handle_id, **kwargs)

    monkeypatch.setattr(store, "resolve_handle", _timeout_then_resolve)

    resp = _client_as(env, user).post(
        _resolve_url(h["id"]), json={"status": "accepted"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "race_lost"
    assert _run(store.get_handle(h["id"]))["status"] == "timed_out"
    assert env["kicks"] == []


def test_resolve_survives_a_failing_resume_kick(env, monkeypatch):
    user = _make_user(env)
    h = _handle(env, user)

    def _boom(app, user, cid):
        raise RuntimeError("kick failed")

    monkeypatch.setattr(env["routes"].wait_resume, "maybe_kick_resume", _boom)

    resp = _client_as(env, user).post(
        _resolve_url(h["id"]), json={"status": "cancelled"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
