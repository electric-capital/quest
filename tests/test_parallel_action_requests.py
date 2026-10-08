"""Parallel ``create_action_request`` calls in one tool-call batch.

Covers the three pieces that let a model open several approval cards in
ONE response and resume only once every card is resolved:

1. The dispatch loop in ``run_conversation_turn`` catches
   ``SuspendForActionRequest`` per call, keeps dispatching the rest of
   the batch (further cards + regular tools), and unwinds cleanly after
   the batch with no ``tool_result`` for the suspended calls.
2. ``_run_resume`` is held by ``_unresolved_blocking_handle_ids`` while
   any dangling ``create_action_request`` tool_use still has a pending
   wait-handle row, so resolving the first card does not resume the
   conversation with ``still_waiting`` markers for the others.
3. The resume bucket replays a sibling regular tool's persisted
   ``tool_output`` from chat_history.json instead of the ``interrupted``
   marker.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.llm.base import StreamEvent

from chat.llm.base import UsageStats


def _run(coro):
    return asyncio.run(coro)


_USER = {"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"}


@pytest.fixture()
def _patched_conversation(monkeypatch):
    """Patch run_conversation_turn's collaborators so a run needs no DB / files."""
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


def _tool_call(name, tool_id, args=None):
    return StreamEvent(
        type="tool_call", tool_name=name, tool_id=tool_id, tool_args=args or {},
    )


def _make_provider(turns, pending=None):
    """Fake provider whose ``send_message_stream`` yields ``turns[i]`` on the
    i-th call. ``pending`` seeds ``get_pending_tool_use_args`` (resume bucket).
    """
    provider = MagicMock()
    provider.get_pending_tool_use_args = MagicMock(return_value=pending or [])
    provider.repair_session_history = MagicMock(return_value=0)
    provider.get_usage = MagicMock(
        return_value=UsageStats(input_tokens=1, output_tokens=1, cached_tokens=0),
    )
    provider.format_tool_results = MagicMock(
        side_effect=lambda _chat, results: {"tool_results": list(results)},
    )
    calls = {"n": 0}

    async def _stream(_chat, _message):
        i = calls["n"]
        calls["n"] += 1
        for ev in turns[i] if i < len(turns) else []:
            yield ev

    provider.send_message_stream = _stream
    return provider


def _run_conversation(conv_mod, provider, *, message="hello"):
    import chat.llm.config as llm_config  # noqa: F401  (kept for parity)
    orig = conv_mod.get_provider_instance
    conv_mod.get_provider_instance = lambda _n, _i=None: provider  # type: ignore[assignment]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    messages_out: list[dict] = []
    try:
        returned = _run(conv_mod.run_conversation_turn(
            app=None,
            user=_USER,
            message=message,
            conversation_id="conv-parallel-ar",
            timezone="UTC",
            model="fake-model",
            on_event=on_event,
            messages_out=messages_out,
        ))
    finally:
        conv_mod.get_provider_instance = orig  # type: ignore[assignment]
    return returned, events, messages_out


# ---------------------------------------------------------------------------
# 1. Dispatch loop keeps going after the first suspend
# ---------------------------------------------------------------------------


def test_batch_dispatches_every_card_and_sibling_tool(
    _patched_conversation, monkeypatch,
):
    conv_mod = _patched_conversation
    from chat.gemini_api import turn_tools
    from chat.gemini_api.turn_tools import SuspendForActionRequest

    arm_calls: list[str] = []

    async def fake_create_action_request(ctx, call, turn):
        arm_calls.append(call.raw_tool_id)
        n = len(arm_calls)
        raise SuspendForActionRequest(
            handle_id=f"wh-{n}", tool_id=call.raw_tool_id, request_id=n,
        )

    monkeypatch.setitem(
        turn_tools.TURN_TOOL_HANDLERS, "create_action_request",
        fake_create_action_request,
    )
    # TURN_TOOL_HANDLERS is imported by name into conversation.py; it is the
    # same dict object, so setitem above is visible there too.
    assert conv_mod.TURN_TOOL_HANDLERS is turn_tools.TURN_TOOL_HANDLERS

    dispatched: list[str] = []

    async def fake_dispatch(app, provider, user, conversation_id, timezone,
                            tool_name, args, **kw):
        dispatched.append(tool_name)
        return json.dumps({"ok": True, "tool": tool_name}), []

    monkeypatch.setattr(conv_mod, "_dispatch_tool_call", fake_dispatch)

    provider = _make_provider([[
        _tool_call("create_action_request", "t-ar-1",
                   {"request_type": "x", "params": {}, "reasoning": "a"}),
        _tool_call("get_current_time", "t-reg", {}),
        _tool_call("create_action_request", "t-ar-2",
                   {"request_type": "y", "params": {}, "reasoning": "b"}),
    ]])

    returned, events, messages_out = _run_conversation(conv_mod, provider)

    # Both cards opened, and the regular sibling tool still ran.
    assert arm_calls == ["t-ar-1", "t-ar-2"]
    assert dispatched == ["get_current_time"]

    # Suspended calls have NO tool_result; the regular one does (durably).
    results = [m for m in messages_out if m.get("type") == "tool_result"]
    assert [m["tool_id"] for m in results] == ["t-reg"]
    assert json.loads(results[0]["tool_output"])["tool"] == "get_current_time"

    # All three tool_use rows are durable so the cards line up on reload.
    uses = [m for m in messages_out if m.get("type") == "tool_use"]
    assert [m["tool_id"] for m in uses] == ["t-ar-1", "t-reg", "t-ar-2"]

    # Clean suspend: no next model turn, no error, no stats, returns the list.
    assert provider.format_tool_results.call_count == 0
    assert not any(m.get("type") in ("error", "stats") for m in messages_out)
    assert not any(e.get("type") in ("error", "stats") for e in events)
    assert returned is messages_out


def test_return_to_caller_suspend_still_unwinds_immediately(
    _patched_conversation, monkeypatch,
):
    """The batch-continue behavior is scoped to create_action_request."""
    conv_mod = _patched_conversation
    from chat.gemini_api import turn_tools
    from chat.gemini_api.turn_tools import SuspendForActionRequest

    async def fake_return_to_caller(ctx, call, turn):
        raise SuspendForActionRequest(
            handle_id="wh-r", tool_id=call.raw_tool_id, request_id=7,
        )

    monkeypatch.setitem(
        turn_tools.TURN_TOOL_HANDLERS, "return_to_caller", fake_return_to_caller,
    )
    dispatched: list[str] = []

    async def fake_dispatch(app, provider, user, conversation_id, timezone,
                            tool_name, args, **kw):
        dispatched.append(tool_name)
        return "{}", []

    monkeypatch.setattr(conv_mod, "_dispatch_tool_call", fake_dispatch)

    provider = _make_provider([[
        _tool_call("return_to_caller", "t-ret", {"response": "done"}),
        _tool_call("get_current_time", "t-reg", {}),
    ]])
    returned, _events, messages_out = _run_conversation(conv_mod, provider)

    assert dispatched == []  # unwound on the first suspend
    assert returned is messages_out


# ---------------------------------------------------------------------------
# 2. Resume gate
# ---------------------------------------------------------------------------


def test_unresolved_blocking_handle_ids_scopes_by_kind_and_dangling_tool_id(
    monkeypatch,
):
    from chat.wait_handles import resume as resume_mod
    from db import tool_wait_handle_store

    rows = [
        {"id": "h-open-1", "kind": "action_request", "tool_id": "t1"},
        {"id": "h-open-2", "kind": "action_request", "tool_id": "t2"},
        # Stale row from an older turn: its tool_use is no longer dangling.
        {"id": "h-stale", "kind": "action_request", "tool_id": "t-old"},
        # wait_for_handles rows belong to other tools; never gate on them.
        {"id": "h-generic", "kind": "confirm", "tool_id": "t1"},
    ]
    monkeypatch.setattr(
        tool_wait_handle_store, "list_pending_for_conversation",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr(
        resume_mod, "_pending_tool_uses",
        lambda cid: [
            ("t1", "create_action_request", {}),
            ("t2", "create_action_request", {}),
            ("t3", "get_current_time", {}),
        ],
    )
    got = _run(resume_mod._unresolved_blocking_handle_ids(1, "conv-g"))
    assert got == ["h-open-1", "h-open-2"]


def test_unresolved_blocking_handle_ids_empty_when_all_resolved(monkeypatch):
    from chat.wait_handles import resume as resume_mod
    from db import tool_wait_handle_store

    monkeypatch.setattr(
        tool_wait_handle_store, "list_pending_for_conversation",
        AsyncMock(return_value=[]),
    )
    calls = []
    monkeypatch.setattr(
        resume_mod, "_pending_tool_uses",
        lambda cid: calls.append(cid) or [],
    )
    assert _run(resume_mod._unresolved_blocking_handle_ids(1, "conv-g")) == []
    assert calls == []  # no transcript read when nothing is pending


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


def test_run_resume_holds_while_sibling_cards_pending(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    ran = []

    async def fake_run(**kwargs):
        ran.append(kwargs["conversation_id"])
        return []

    events = _patch_resume_environment(monkeypatch, resume_mod, run_impl=fake_run)
    monkeypatch.setattr(
        resume_mod, "_unresolved_blocking_handle_ids",
        AsyncMock(return_value=["h-open-2"]),
    )

    _run(resume_mod._run_resume(None, {"id": 1}, "conv-hold"))

    assert ran == []
    # Held resumes publish NOTHING: no resume_started, no
    # send_message_finished -- the composer stays locked on the remaining
    # pending_wait_handles instead of flashing a streaming state.
    assert events == []


def test_run_resume_proceeds_once_every_card_resolved(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    ran = []

    async def fake_run(**kwargs):
        ran.append(kwargs["conversation_id"])
        return []

    events = _patch_resume_environment(monkeypatch, resume_mod, run_impl=fake_run)
    monkeypatch.setattr(
        resume_mod, "_unresolved_blocking_handle_ids",
        AsyncMock(return_value=[]),
    )

    _run(resume_mod._run_resume(None, {"id": 1}, "conv-go"))

    assert ran == ["conv-go"]
    assert [e["type"] for e in events] == [
        "resume_started", "send_message_finished",
    ]


def test_run_resume_gate_failure_falls_through(monkeypatch):
    """A broken gate check must not wedge the conversation."""
    from chat.wait_handles import resume as resume_mod

    ran = []

    async def fake_run(**kwargs):
        ran.append(kwargs["conversation_id"])
        return []

    _patch_resume_environment(monkeypatch, resume_mod, run_impl=fake_run)
    monkeypatch.setattr(
        resume_mod, "_unresolved_blocking_handle_ids",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    _run(resume_mod._run_resume(None, {"id": 1}, "conv-fallthrough"))
    assert ran == ["conv-fallthrough"]


# ---------------------------------------------------------------------------
# 3. Resume bucket: every card's verdict + replayed sibling tool output
# ---------------------------------------------------------------------------


def test_load_persisted_tool_results_maps_tool_id_to_output(monkeypatch):
    from chat.gemini_api import conversation as conv_mod

    monkeypatch.setattr(
        conv_mod.ChatStorage, "get_conversation",
        staticmethod(lambda _cid: {"messages": [
            {"type": "text", "content": "hi"},
            {"type": "tool_use", "tool_id": "t-reg"},
            {"type": "tool_result", "tool_id": "t-reg", "tool_output": "first"},
            {"type": "tool_result", "tool_id": "t-reg", "tool_output": "later"},
            {"type": "tool_result", "tool_id": "", "tool_output": "no id"},
            {"type": "tool_result", "tool_id": "t-bad", "tool_output": None},
        ]}),
    )
    assert conv_mod._load_persisted_tool_results("c") == {"t-reg": "later"}


def test_load_persisted_tool_results_swallows_read_errors(monkeypatch):
    from chat.gemini_api import conversation as conv_mod

    def _boom(_cid):
        raise OSError("disk")

    monkeypatch.setattr(
        conv_mod.ChatStorage, "get_conversation", staticmethod(_boom),
    )
    assert conv_mod._load_persisted_tool_results("c") == {}


def test_resume_bucket_closes_all_cards_and_replays_sibling(
    _patched_conversation, monkeypatch,
):
    conv_mod = _patched_conversation

    rows = {
        "t-ar-1": {
            "kind": "action_request", "status": "accepted",
            "response": {"verdict": "executed", "request_id": 1,
                         "result": {"ok": True}},
        },
        "t-ar-2": {
            "kind": "action_request", "status": "rejected",
            "response": {"verdict": "denied", "request_id": 2,
                         "feedback": "wrong channel",
                         "result": {"denied": True, "feedback": "wrong channel"}},
        },
    }

    async def fake_get_handle_by_tool_id(user_id, conversation_id, tool_id):
        return rows.get(tool_id)

    monkeypatch.setattr(
        conv_mod.tool_wait_handle_store, "get_handle_by_tool_id",
        fake_get_handle_by_tool_id,
    )
    monkeypatch.setattr(
        conv_mod, "_load_persisted_tool_results",
        lambda _cid: {"t-reg": '{"ok": true, "tool": "get_current_time"}'},
    )

    provider = _make_provider(
        turns=[[SimpleNamespace(type="text", text="all done")]],
        pending=[
            ("t-ar-1", "create_action_request", {}),
            ("t-reg", "get_current_time", {}),
            ("t-ar-2", "create_action_request", {}),
            ("t-lost", "get_workspace_file", {}),
        ],
    )

    _returned, _events, messages_out = _run_conversation(
        conv_mod, provider, message="",
    )

    provider.format_tool_results.assert_called_once()
    (_chat, results), _kw = provider.format_tool_results.call_args
    by_id = {r["tool_id"]: json.loads(r["result"]) for r in results}
    assert set(by_id) == {"t-ar-1", "t-reg", "t-ar-2", "t-lost"}
    assert by_id["t-ar-1"]["verdict"] == "executed"
    assert by_id["t-ar-2"]["verdict"] == "denied"
    assert by_id["t-ar-2"]["feedback"] == "wrong channel"
    # Sibling regular tool: persisted output replayed, not "interrupted".
    assert by_id["t-reg"] == {"ok": True, "tool": "get_current_time"}
    # Truly lost tool_use (no row, no persisted output): interrupted marker.
    assert by_id["t-lost"]["status"] == "interrupted"

    assert any(
        m.get("type") == "text" and m.get("content") == "all done"
        for m in messages_out
    )
