"""Racing resolutions of one action request produce exactly one outcome.

Approve runs the handler's external write BEFORE the row flips to
``executed``, so without a claim two concurrent Approve clicks (double-click,
two tabs) both passed the OPEN check and both ran the handler -- a Twitter
DM sent twice, a user-subagent launched twice -- and the loser then crashed
on the ``None`` returned by the second ``resolve_action_request``. Approve
racing Revise had the same shape: the DM went out, the row said denied.

Two layers close it: a per-conversation resolution lock in the route so the
whole read-check-act sequence is serialized, and a conditional
``UPDATE ... WHERE status = 'open'`` in the store so the transition itself
is a compare-and-set (covers any caller outside the route).
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db, monkeypatch):
    import db.action_request_store as action_request_store_mod
    import db.conversation_store as conversation_store_mod
    import db.tool_wait_handle_store as tool_wait_handle_store_mod

    from chat.storage import ChatStorage
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )

    import chat.action_request_routes as routes_mod
    kicks: list = []
    monkeypatch.setattr(
        routes_mod.wait_resume, "maybe_kick_resume",
        lambda app, user, cid: kicks.append(cid),
    )
    events: list = []
    monkeypatch.setattr(
        routes_mod.bus, "publish_to_user",
        lambda user_id, event: events.append(event),
    )

    return {
        "models": isolated_db.models,
        "conversation_store": conversation_store_mod,
        "action_request_store": action_request_store_mod,
        "tool_wait_handle_store": tool_wait_handle_store_mod,
        "routes": routes_mod,
        "kicks": kicks,
        "events": events,
    }


class _FakeRequest:
    app = None


class _SlowHandler:
    """Stands in for a handler with a real external write (Twitter DM,
    Drive upload): yields to the loop so a racing request can interleave."""

    def __init__(self):
        self.calls = 0

    async def execute(self, params, user, conversation_id=None, project_id=None):
        self.calls += 1
        await asyncio.sleep(0.05)
        return {"sent": True, "call": self.calls}


async def _setup(stores):
    from db.engine import AsyncSessionLocal

    models_mod = stores["models"]
    async with AsyncSessionLocal() as db:
        u = models_mod.User(
            email=f"ar-race-{uuid.uuid4().hex}@example.com",
            api_key=f"k-{uuid.uuid4().hex}",
            name="Race Tester",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        user = {"id": u.id, "email": u.email, "name": u.name}

    conv_id = str(uuid.uuid4())
    await stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc),
    )
    req = await stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="send_twitter_dm",
        params={"participant_id": "1", "message": "hi"},
        reasoning="because",
    )
    handle = await stores["tool_wait_handle_store"].create_handle(
        user_id=user["id"],
        conversation_id=conv_id,
        kind="action_request",
        tool_id=f"toolu_{req['id']}",
        payload={"request_id": req["id"]},
        correlation_kind="action_request",
        correlation_id=str(req["id"]),
    )
    return user, conv_id, req, handle


def _resolve_coro(routes_mod, request_id, user, action, feedback=None):
    return routes_mod.resolve_user_action_request(
        request_id,
        routes_mod.ResolveRequestBody(action=action, feedback=feedback),
        _FakeRequest(),
        user,
    )


def _split(results):
    ok = [r for r in results if not isinstance(r, BaseException)]
    errs = [r for r in results if isinstance(r, BaseException)]
    return ok, errs


def test_concurrent_approves_run_the_handler_once(_isolated_db, monkeypatch):
    stores = _isolated_db
    routes = stores["routes"]
    handler = _SlowHandler()
    monkeypatch.setattr(routes, "get_handler", lambda _type: handler)

    async def scenario():
        user, conv_id, req, handle = await _setup(stores)
        results = await asyncio.gather(
            _resolve_coro(routes, req["id"], user, "execute"),
            _resolve_coro(routes, req["id"], user, "execute"),
            return_exceptions=True,
        )
        return user, req, handle, results

    user, req, handle, results = _run(scenario())
    ok, errs = _split(results)

    assert handler.calls == 1
    assert len(ok) == 1 and ok[0]["status"] == "executed"
    assert len(errs) == 1
    assert isinstance(errs[0], HTTPException)
    assert errs[0].status_code == 400
    assert errs[0].detail["error"] == "already_resolved"

    row = _run(stores["tool_wait_handle_store"].get_handle(handle["id"]))
    assert row["status"] == "accepted"
    assert stores["kicks"].count(req["conversation_id"]) == 1


def test_approve_racing_revise_yields_one_outcome(_isolated_db, monkeypatch):
    stores = _isolated_db
    routes = stores["routes"]
    handler = _SlowHandler()
    monkeypatch.setattr(routes, "get_handler", lambda _type: handler)

    async def scenario():
        user, conv_id, req, handle = await _setup(stores)
        results = await asyncio.gather(
            _resolve_coro(routes, req["id"], user, "execute"),
            _resolve_coro(routes, req["id"], user, "deny", feedback="no"),
            return_exceptions=True,
        )
        return user, req, handle, results

    user, req, handle, results = _run(scenario())
    ok, errs = _split(results)

    assert len(ok) == 1 and len(errs) == 1
    assert errs[0].detail["error"] == "already_resolved"
    final = _run(stores["action_request_store"].get_action_request(user["id"], req["id"]))
    row = _run(stores["tool_wait_handle_store"].get_handle(handle["id"]))
    # The handler ran iff the row says executed; the handle agrees.
    if final["status"] == "executed":
        assert handler.calls == 1
        assert row["status"] == "accepted"
    else:
        assert final["status"] == "denied"
        assert handler.calls == 0
        assert row["status"] == "rejected"
    assert len(stores["kicks"]) == 1


def test_store_resolve_is_compare_and_set(_isolated_db):
    stores = _isolated_db
    store = stores["action_request_store"]

    async def scenario():
        user, conv_id, req, handle = await _setup(stores)
        results = await asyncio.gather(
            store.resolve_action_request(user["id"], req["id"], "executed", result={"a": 1}),
            store.resolve_action_request(user["id"], req["id"], "denied", result={"b": 2}),
        )
        final = await store.get_action_request(user["id"], req["id"])
        return results, final

    results, final = _run(scenario())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert final["status"] == winners[0]["status"]
    assert final["result"] == winners[0]["result"]
    assert final["resolved_at"] is not None
