"""The action-request card's Stop button.

Stop replaces one-click Deny on the inline card and in the Requests inbox.
It discards the request AND halts the conversation loop:

1. ``POST /action-requests/{id}/resolve`` with ``action: "stop"`` flips the
   row to ``stopped``, closes the linked wait handle as ``stopped`` with a
   ``{"verdict": "stopped"}`` response, publishes ``wait_handle_resolved``
   (so the composer unlocks) and kicks NO headless resume. Open sibling
   cards of the same conversation are stopped too; cards in other
   conversations are untouched. ``subagent_return`` cards keep the hard
   deny (the run ends immediately).
2. ``_run_resume`` holds while any dangling ``create_action_request``
   tool_use has a ``stopped`` row, so a stray re-kick never wakes the
   model without user input.
3. The resume bucket in ``run_conversation_turn`` closes the stopped
   tool_use with the ``stopped`` verdict plus a note saying the user's
   new message follows, and appends that message to the same turn.
"""

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from importlib import reload
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from chat.llm.base import UsageStats


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Resolve endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_ar_stop_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.conversation_store as conversation_store_mod
    reload(conversation_store_mod)
    import db.action_request_store as action_request_store_mod
    reload(action_request_store_mod)
    import db.tool_wait_handle_store as tool_wait_handle_store_mod
    reload(tool_wait_handle_store_mod)

    models_mod.Base.metadata.create_all(engine_mod.engine)

    from chat.storage import ChatStorage
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )

    # Record resume kicks and per-user events instead of touching the app.
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

    yield {
        "models": models_mod,
        "conversation_store": conversation_store_mod,
        "action_request_store": action_request_store_mod,
        "tool_wait_handle_store": tool_wait_handle_store_mod,
        "kicks": kicks,
        "events": events,
    }

    shutil.rmtree(tmpdir, ignore_errors=True)


class _FakeRequest:
    app = None


def _make_user(models_mod) -> dict:
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"ar-stop-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Stop Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {"id": u.id, "email": u.email, "name": u.name}

    return _run(_create())


def _open_card(stores, user, conv_id, request_type="send_slack_message"):
    """Create an open action request with its linked pending wait handle,
    the way the ``create_action_request`` dispatch arm does."""
    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type=request_type,
        params={"channel": "C1", "text": "hi"},
        reasoning="because",
    ))
    handle = _run(stores["tool_wait_handle_store"].create_handle(
        user_id=user["id"],
        conversation_id=conv_id,
        kind="action_request",
        tool_id=f"toolu_{req['id']}",
        payload={"request_id": req["id"]},
        correlation_kind="action_request",
        correlation_id=str(req["id"]),
    ))
    return req, handle


def _resolve(request_id, user, action, feedback=None):
    from chat.action_request_routes import (
        ResolveRequestBody,
        resolve_user_action_request,
    )
    return _run(resolve_user_action_request(
        request_id,
        ResolveRequestBody(action=action, feedback=feedback),
        _FakeRequest(),
        user,
    ))


def _new_conversation(stores, user):
    conv_id = str(uuid.uuid4())
    _run(stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc),
    ))
    return conv_id


def test_stop_marks_row_and_handle_stopped_without_resume_kick(_isolated_db):
    stores = _isolated_db
    user = _make_user(stores["models"])
    conv_id = _new_conversation(stores, user)
    req, handle = _open_card(stores, user, conv_id)

    resolved = _resolve(req["id"], user, "stop")

    assert resolved["status"] == "stopped"
    assert resolved["result"] == {"stopped": True}

    row = _run(stores["tool_wait_handle_store"].get_handle(handle["id"]))
    assert row["status"] == "stopped"
    assert row["response"] == {
        "verdict": "stopped",
        "request_id": req["id"],
        "result": {"stopped": True},
    }

    # The composer unlocks via the resolved event...
    resolved_events = [
        e for e in stores["events"] if e["type"] == "wait_handle_resolved"
    ]
    assert [e["status"] for e in resolved_events] == ["stopped"]
    assert resolved_events[0]["request_id"] == req["id"]
    # ...but nothing wakes the model: that only happens on the next send.
    assert stores["kicks"] == []
    # The inbox counts carry the new bucket.
    counts = [e for e in stores["events"] if e["type"] == "request_count_changed"]
    assert counts[-1]["counts"]["stopped"] == 1
    assert counts[-1]["counts"]["open"] == 0


def test_stop_also_stops_open_siblings_in_same_conversation_only(_isolated_db):
    stores = _isolated_db
    user = _make_user(stores["models"])
    conv_id = _new_conversation(stores, user)
    other_conv = _new_conversation(stores, user)
    req_a, handle_a = _open_card(stores, user, conv_id)
    req_b, handle_b = _open_card(stores, user, conv_id)
    req_other, handle_other = _open_card(stores, user, other_conv)

    _resolve(req_a["id"], user, "stop")

    ar = stores["action_request_store"]
    wh = stores["tool_wait_handle_store"]
    assert _run(ar.get_action_request(user["id"], req_b["id"]))["status"] == "stopped"
    assert _run(wh.get_handle(handle_b["id"]))["status"] == "stopped"
    assert _run(wh.get_handle(handle_b["id"]))["response"]["request_id"] == req_b["id"]
    # A card in another conversation is not the user's target.
    assert _run(ar.get_action_request(user["id"], req_other["id"]))["status"] == "open"
    assert _run(wh.get_handle(handle_other["id"]))["status"] == "pending"
    assert stores["kicks"] == []
    # No pending handle remains on the stopped conversation, so a fresh
    # GET /conversations/{id} no longer locks the composer.
    assert _run(wh.list_pending_for_conversation(user["id"], conv_id)) == []


def test_stop_on_subagent_return_is_a_hard_deny(_isolated_db, monkeypatch):
    stores = _isolated_db
    user = _make_user(stores["models"])
    conv_id = _new_conversation(stores, user)
    req, handle = _open_card(stores, user, conv_id, request_type="subagent_return")

    import db.user_subagent_run_store as run_store
    monkeypatch.setattr(
        run_store, "get_run_by_subagent_conversation",
        AsyncMock(return_value={"id": "run-1"}),
    )
    from chat import user_subagent
    finalized = []

    async def _finalize(run):
        finalized.append(run["id"])

    monkeypatch.setattr(user_subagent, "finalize_denied_return", _finalize)

    resolved = _resolve(req["id"], user, "stop")

    assert resolved["status"] == "denied"
    assert resolved["result"] == {"denied": True}
    assert _run(stores["tool_wait_handle_store"].get_handle(handle["id"]))["status"] == "rejected"
    assert finalized == ["run-1"]
    assert stores["kicks"] == []  # the run module owns the subagent lifecycle


def test_revise_still_resumes_immediately(_isolated_db):
    """Revise (deny + feedback) is unchanged: it wakes the model right away."""
    stores = _isolated_db
    user = _make_user(stores["models"])
    conv_id = _new_conversation(stores, user)
    req, handle = _open_card(stores, user, conv_id)

    resolved = _resolve(req["id"], user, "deny", feedback="wrong channel")

    assert resolved["status"] == "denied"
    assert resolved["result"] == {"denied": True, "feedback": "wrong channel"}
    assert _run(stores["tool_wait_handle_store"].get_handle(handle["id"]))["status"] == "rejected"
    assert stores["kicks"] == [conv_id]


def test_unknown_action_rejected_and_stopped_row_cannot_be_resolved_again(_isolated_db):
    stores = _isolated_db
    user = _make_user(stores["models"])
    conv_id = _new_conversation(stores, user)
    req, _handle = _open_card(stores, user, conv_id)

    with pytest.raises(HTTPException) as exc_info:
        _resolve(req["id"], user, "halt")
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "invalid_action"

    _resolve(req["id"], user, "stop")
    with pytest.raises(HTTPException) as exc_info:
        _resolve(req["id"], user, "execute")
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "already_resolved"


# ---------------------------------------------------------------------------
# 2. Resume gate
# ---------------------------------------------------------------------------


def test_stopped_handle_ids_scopes_by_dangling_tool_id(monkeypatch):
    from chat.wait_handles import resume as resume_mod
    from db import tool_wait_handle_store

    rows = {
        "t1": {"id": "h-stopped", "status": "stopped"},
        "t2": {"id": "h-accepted", "status": "accepted"},
        # t3 has no row at all.
    }

    async def fake_get(user_id, conversation_id, tool_id):
        return rows.get(tool_id)

    monkeypatch.setattr(tool_wait_handle_store, "get_handle_by_tool_id", fake_get)
    monkeypatch.setattr(
        resume_mod, "_pending_tool_uses",
        lambda cid: [
            ("t1", "create_action_request", {}),
            ("t2", "create_action_request", {}),
            ("t3", "get_current_time", {}),
        ],
    )
    assert _run(resume_mod._stopped_handle_ids(1, "conv")) == ["h-stopped"]


def _patch_resume_environment(monkeypatch, resume_mod, *, run_impl):
    from chat.realtime.bus import bus as bus_singleton
    import chat.gemini_api as gemini_mod
    import db.conversation_store as conv_store_mod

    events = []
    monkeypatch.setattr(
        bus_singleton, "publish_to_conversation",
        lambda cid, event: events.append(event),
    )

    async def fake_meta(user_id, conversation_id):
        return {"origin": "web"}

    async def fake_flags(conversation_id):
        return []

    monkeypatch.setattr(conv_store_mod, "get_conversation_meta", fake_meta)
    monkeypatch.setattr(conv_store_mod, "get_conversation_flags", fake_flags)
    monkeypatch.setattr(gemini_mod, "run_conversation_turn", run_impl)

    def fake_make_flush(conversation_id, messages_out, *, log_prefix):
        async def flush():
            return None
        return flush, asyncio.Lock()

    monkeypatch.setattr(resume_mod, "make_flush_callback", fake_make_flush)
    return events


def test_run_resume_holds_on_stopped_card(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    ran = []

    async def fake_run(**kwargs):
        ran.append(kwargs["conversation_id"])
        return []

    events = _patch_resume_environment(monkeypatch, resume_mod, run_impl=fake_run)
    monkeypatch.setattr(
        resume_mod, "_unresolved_blocking_handle_ids", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        resume_mod, "_stopped_handle_ids", AsyncMock(return_value=["h-stopped"]),
    )

    _run(resume_mod._run_resume(None, {"id": 1}, "conv-stopped"))

    assert ran == []
    # Held: no lifecycle envelopes, so the FE never latches a streaming state.
    assert events == []


def test_run_resume_stopped_gate_failure_falls_through(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    ran = []

    async def fake_run(**kwargs):
        ran.append(kwargs["conversation_id"])
        return []

    _patch_resume_environment(monkeypatch, resume_mod, run_impl=fake_run)
    monkeypatch.setattr(
        resume_mod, "_unresolved_blocking_handle_ids", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        resume_mod, "_stopped_handle_ids",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    _run(resume_mod._run_resume(None, {"id": 1}, "conv-fallthrough"))
    assert ran == ["conv-fallthrough"]


# ---------------------------------------------------------------------------
# 3. Resume bucket
# ---------------------------------------------------------------------------


def test_stopped_action_request_result_notes():
    from chat.gemini_api import conversation as conv_mod

    base = {"verdict": "stopped", "request_id": 7, "result": {"stopped": True}}
    with_input = conv_mod._stopped_action_request_result(base, "do this instead")
    assert with_input["verdict"] == "stopped"
    assert with_input["request_id"] == 7
    assert "new message" in with_input["note"]
    assert "NOT executed" in with_input["note"]

    no_input = conv_mod._stopped_action_request_result(base, "")
    assert no_input["verdict"] == "stopped"
    assert "No new user message" in no_input["note"]
    # The stored response is never mutated.
    assert "note" not in base


_USER = {"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"}


@pytest.fixture()
def _patched_conversation(monkeypatch):
    from chat.gemini_api import conversation as conv_mod

    monkeypatch.setattr(
        conv_mod, "load_server_config",
        lambda: {"gemini": {"model": "fake-model"}},
    )
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(
        conv_mod, "get_system_prompt", lambda *a, **kw: "system prompt",
    )
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(
        conv_mod, "get_or_create_chat", lambda *a, **kw: object(),
    )
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())

    import db.conversation_store as conv_store
    monkeypatch.setattr(conv_store, "update_conversation_model", AsyncMock())
    import db.skill_store as skill_store
    monkeypatch.setattr(
        skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]),
    )
    import api.instructions as instructions
    monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: {})

    async def _fake_history(_cid):
        return {"guide_id": "g", "guide_snapshot": {"content": "guide"}}

    monkeypatch.setattr(conv_mod, "_read_chat_history", _fake_history)
    monkeypatch.setattr(
        conv_mod.ChatStorage, "set_system_prompt",
        staticmethod(lambda *a, **kw: None),
    )
    return conv_mod


def _make_provider(turns, pending):
    provider = MagicMock()
    provider.get_pending_tool_use_args = MagicMock(return_value=pending)
    provider.repair_session_history = MagicMock(return_value=0)
    provider.get_usage = MagicMock(
        return_value=UsageStats(input_tokens=1, output_tokens=1, cached_tokens=0),
    )
    provider.format_tool_results = MagicMock(
        side_effect=lambda _chat, results: [{"tool_results": list(results)}],
    )
    calls = {"n": 0}

    async def _stream(_chat, _message):
        i = calls["n"]
        calls["n"] += 1
        for ev in turns[i] if i < len(turns) else []:
            yield ev

    provider.send_message_stream = _stream
    return provider


def test_resume_bucket_closes_stopped_card_and_appends_new_message(
    _patched_conversation, monkeypatch,
):
    conv_mod = _patched_conversation

    rows = {
        "t-stopped": {
            "kind": "action_request", "status": "stopped",
            "response": {"verdict": "stopped", "request_id": 9,
                         "result": {"stopped": True}},
        },
    }

    async def fake_get_handle_by_tool_id(user_id, conversation_id, tool_id):
        return rows.get(tool_id)

    monkeypatch.setattr(
        conv_mod.tool_wait_handle_store, "get_handle_by_tool_id",
        fake_get_handle_by_tool_id,
    )

    provider = _make_provider(
        turns=[[SimpleNamespace(type="text", text="ok, doing the new thing")]],
        pending=[("t-stopped", "create_action_request", {})],
    )
    orig = conv_mod.get_provider_instance
    conv_mod.get_provider_instance = lambda _n, _i=None: provider  # type: ignore[assignment]
    messages_out: list[dict] = []

    async def on_event(event: dict) -> None:
        return None

    try:
        _run(conv_mod.run_conversation_turn(
            app=None,
            user=_USER,
            message="forget that, summarise the thread instead",
            conversation_id="conv-stop-bucket",
            timezone="UTC",
            model="fake-model",
            on_event=on_event,
            messages_out=messages_out,
        ))
    finally:
        conv_mod.get_provider_instance = orig  # type: ignore[assignment]

    provider.format_tool_results.assert_called_once()
    (_chat, results), _kw = provider.format_tool_results.call_args
    assert [r["tool_id"] for r in results] == ["t-stopped"]
    closed = json.loads(results[0]["result"])
    assert closed["verdict"] == "stopped"
    assert closed["request_id"] == 9
    assert closed["result"] == {"stopped": True}
    assert "new message" in closed["note"]

    # The user's new text rides in the same turn as the tool_result.
    provider.append_user_text.assert_called_once()
    (_current_message, wrapped), _kw = provider.append_user_text.call_args
    assert "forget that, summarise the thread instead" in wrapped

    assert any(
        m.get("type") == "text" and m.get("content") == "ok, doing the new thing"
        for m in messages_out
    )
