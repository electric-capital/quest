"""When ``run_conversation_turn`` saves sdk_history.json.

The session is saved after every model response that requested tools
(before any of them is dispatched, in a worker thread) and once at turn
end or on error (synchronously, after the final flush) -- never per
flushed event, and not on cancellation, whose handlers amend the last
saved state:

* a tool round followed by an answer saves twice, however many tool
  events the round flushed; N rounds save N + 1 times;
* a suspend (action request, wait handles, Slack reply) and an inference
  finish leave exactly the pre-dispatch save, which already ends on the
  dangling tool_use;
* an error saves once more, after the durable error is flushed;
* a cancelled turn makes no exit save, and a cancel that lands during a
  save waits for the write before unwinding.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.llm.base import StreamEvent, UsageStats


_USER = {"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"}


@pytest.fixture()
def harness(monkeypatch):
    """run_conversation_turn with no DB / files, logging every step.

    ``log`` collects ("stream_done", i), ("event", type), ("dispatch",
    name), ("arm", key) and ("save", snapshot) entries in order; a save
    snapshot records the session's message roles/contents and whether it
    ran on the event-loop thread.
    """
    from chat.gemini_api import conversation as conv_mod

    log: list[tuple] = []
    session = SimpleNamespace(messages=[])
    loop_thread = {"id": None}

    monkeypatch.setattr(
        conv_mod, "load_server_config",
        lambda: {"gemini": {"model": "fake-model"}},
    )
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: session)
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())

    def _save(conversation_id, chat, provider):
        log.append(("save", {
            "messages": copy.deepcopy(chat.messages),
            "on_loop": threading.get_ident() == loop_thread["id"],
        }))

    monkeypatch.setattr(conv_mod, "_save_sdk_history", _save)

    import db.conversation_store as conv_store
    monkeypatch.setattr(conv_store, "update_conversation_model", AsyncMock())
    import db.skill_store as skill_store
    monkeypatch.setattr(
        skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]),
    )
    import api.instructions as instructions
    monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: {})

    async def _fake_history(_cid):
        return {"messages": []}

    monkeypatch.setattr(conv_mod, "_read_chat_history", _fake_history)
    monkeypatch.setattr(
        conv_mod.ChatStorage, "set_system_prompt",
        staticmethod(lambda *a, **kw: None),
    )

    async def _dispatch(app, provider, user, conversation_id, timezone,
                        tool_name, args, **kw):
        log.append(("dispatch", tool_name))
        return json.dumps({"ok": True}), []

    monkeypatch.setattr(conv_mod, "_dispatch_tool_call", _dispatch)

    def make_provider(turns):
        """Provider whose i-th stream yields ``turns[i]`` (a list of events or
        an exception to raise) and records the turn on ``session``."""
        provider = MagicMock()
        provider.get_pending_tool_use_args = MagicMock(return_value=[])
        provider.repair_session_history = MagicMock(return_value=0)
        provider.get_usage = MagicMock(
            return_value=UsageStats(input_tokens=1, output_tokens=1, cached_tokens=0),
        )
        provider.format_tool_results = MagicMock(
            side_effect=lambda _chat, results: [r["tool_id"] for r in results],
        )
        calls = {"n": 0}

        async def _stream(chat, message):
            i = calls["n"]
            calls["n"] += 1
            chat.messages.append({"role": "user", "content": message})
            turn = turns[i] if i < len(turns) else []
            if isinstance(turn, Exception):
                raise turn
            for ev in turn:
                yield ev
            chat.messages.append({"role": "assistant", "content": [
                ev.tool_id if ev.type == "tool_call" else ev.text for ev in turn
            ]})
            log.append(("stream_done", i))

        provider.send_message_stream = _stream
        monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: provider)
        return provider

    async def run(provider, *, message="hello"):
        loop_thread["id"] = threading.get_ident()
        messages_out: list[dict] = []

        async def on_event(event: dict) -> None:
            log.append(("event", event.get("type")))

        returned = await conv_mod.run_conversation_turn(
            app=None, user=_USER, message=message,
            conversation_id="conv-save-points", timezone="UTC",
            model="fake-model", on_event=on_event, messages_out=messages_out,
        )
        return returned, messages_out

    return SimpleNamespace(
        conv_mod=conv_mod, log=log, session=session,
        make_provider=make_provider, run=run,
    )


def _tool(name, tool_id, args=None):
    return StreamEvent(type="tool_call", tool_name=name, tool_id=tool_id, tool_args=args or {})


def _text(text):
    return StreamEvent(type="text", text=text)


def _saves(log):
    return [entry[1] for entry in log if entry[0] == "save"]


def _index(log, entry):
    return log.index(entry)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def test_tool_round_then_answer_saves_before_dispatch_and_at_turn_end(harness):
    provider = harness.make_provider([
        [_tool("get_current_time", "t1"), _tool("list_workspace_files", "t2")],
        [_text("done")],
    ])
    asyncio.run(harness.run(provider))
    log = harness.log

    saves = _saves(log)
    assert len(saves) == 2  # not one per tool_use / tool_result event
    assert sum(1 for e in log if e[0] == "event" and e[1] in ("tool_use", "tool_result")) == 4

    # 1st save: after the tool-requesting response, before any dispatch or
    # tool event, on a worker thread; the session ends on the tool_use(s).
    first = next(i for i, e in enumerate(log) if e[0] == "save")
    assert _index(log, ("stream_done", 0)) < first < _index(log, ("event", "tool_use"))
    assert first < _index(log, ("dispatch", "get_current_time"))
    assert saves[0]["on_loop"] is False
    assert saves[0]["messages"][-1] == {"role": "assistant", "content": ["t1", "t2"]}

    # 2nd save: the turn end, after the stats flush, on the loop thread,
    # with the final answer in the session.
    assert log[-1][0] == "save"
    assert _index(log, ("event", "stats")) < len(log) - 1
    assert saves[1]["on_loop"] is True
    assert saves[1]["messages"][-1] == {"role": "assistant", "content": ["done"]}


def test_each_tool_round_saves_once(harness):
    provider = harness.make_provider([
        [_tool("get_current_time", "a1"), _tool("get_current_time", "a2"),
         _tool("get_current_time", "a3")],
        [_tool("get_current_time", "b1")],
        [_tool("get_current_time", "c1"), _tool("get_current_time", "c2")],
        [_text("answer")],
    ])
    asyncio.run(harness.run(provider))
    saves = _saves(harness.log)
    assert len(saves) == 4  # three tool rounds + turn end
    assert [s["messages"][-1]["content"] for s in saves] == [
        ["a1", "a2", "a3"], ["b1"], ["c1", "c2"], ["answer"],
    ]


def test_plain_answer_saves_once_at_turn_end(harness):
    provider = harness.make_provider([[_text("hi there")]])
    asyncio.run(harness.run(provider))
    saves = _saves(harness.log)
    assert len(saves) == 1
    assert harness.log[-1][0] == "save"
    assert saves[0]["messages"][-1] == {"role": "assistant", "content": ["hi there"]}


# ---------------------------------------------------------------------------
# Suspend and inference finish
# ---------------------------------------------------------------------------


def _suspend_action_request(call):
    from chat.gemini_api.turn_tools import SuspendForActionRequest
    return SuspendForActionRequest(handle_id="wh-1", tool_id=call.raw_tool_id, request_id=1)


def _suspend_slack_reply(call):
    from chat.gemini_api.turn_tools import SuspendForSlackReply
    return SuspendForSlackReply(
        handle_id="wh-1", tool_id=call.raw_tool_id, channel="D1", thread_ts="1.0",
    )


def _suspend_wait_handles(call):
    from chat.gemini_api.turn_tools import SuspendForWaitHandles
    return SuspendForWaitHandles(handle_ids=["wh-1"], tool_id=call.raw_tool_id, reason="r")


def _finish_inference(call):
    from chat.gemini_api.turn_tools import FinishInferenceResponse
    return FinishInferenceResponse(tool_id=call.raw_tool_id)


@pytest.mark.parametrize("tool_name, args, key, make_exc", [
    ("create_action_request", {"request_type": "x"}, "create_action_request",
     _suspend_action_request),
    ("send_slack_reply_and_get_response", {"text": "hi"},
     "send_slack_reply_and_get_response", _suspend_slack_reply),
    ("tool_call", {"tool_name": "wait_for_handles", "arguments": {}},
     "tool_call:wait_for_handles", _suspend_wait_handles),
    ("return_final_response", {"response": "r"}, "return_final_response",
     _finish_inference),
])
def test_suspend_keeps_only_the_pre_dispatch_save(
    harness, monkeypatch, tool_name, args, key, make_exc,
):
    from chat.gemini_api import turn_tools

    assert key in turn_tools.TURN_TOOL_HANDLERS

    async def _arm(ctx, call, turn):
        harness.log.append(("arm", call.key))
        raise make_exc(call)

    monkeypatch.setitem(turn_tools.TURN_TOOL_HANDLERS, key, _arm)
    provider = harness.make_provider([[_tool(tool_name, "t-block", args)]])

    returned, messages_out = asyncio.run(harness.run(provider))

    assert returned is messages_out
    saves = _saves(harness.log)
    assert len(saves) == 1
    assert _index(harness.log, ("save", saves[0])) < _index(harness.log, ("arm", key))
    # The saved session ends on the dangling tool_use the resume closes.
    assert saves[0]["messages"][-1] == {"role": "assistant", "content": ["t-block"]}
    assert not any(e == ("event", "stats") for e in harness.log)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


def test_error_saves_after_the_error_flush(harness):
    provider = harness.make_provider([
        [_tool("get_current_time", "t1")],
        RuntimeError("stream died"),
    ])
    with pytest.raises(RuntimeError, match="stream died"):
        asyncio.run(harness.run(provider))
    log = harness.log
    saves = _saves(log)
    assert len(saves) == 2  # the tool round + the error exit
    assert log[-1][0] == "save"
    assert _index(log, ("event", "error")) == len(log) - 2
    assert saves[1]["on_loop"] is True
    # The failed round's user turn (the tool results) is in the saved session.
    assert saves[1]["messages"][-1]["role"] == "user"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancelled_turn_makes_no_exit_save(harness, monkeypatch):
    async def main():
        dispatched = asyncio.Event()

        async def _blocking_dispatch(*_a, **_kw):
            harness.log.append(("dispatch", "slow_tool"))
            dispatched.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(harness.conv_mod, "_dispatch_tool_call", _blocking_dispatch)
        provider = harness.make_provider([[_tool("slow_tool", "t1")], [_text("never")]])
        task = asyncio.create_task(harness.run(provider))
        await asyncio.wait_for(dispatched.wait(), 10)
        task.cancel()  # Stop pressed mid-tool
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    saves = _saves(harness.log)
    assert len(saves) == 1  # the pre-dispatch save only
    assert saves[0]["messages"][-1] == {"role": "assistant", "content": ["t1"]}


def test_cancel_during_the_pre_dispatch_save_waits_for_the_write(harness, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    real_save = harness.conv_mod._save_sdk_history

    def _slow_save(conversation_id, chat, provider):
        entered.set()
        assert release.wait(10)
        real_save(conversation_id, chat, provider)
        harness.log.append(("save_done",))

    monkeypatch.setattr(harness.conv_mod, "_save_sdk_history", _slow_save)
    provider = harness.make_provider([[_tool("get_current_time", "t1")], [_text("x")]])

    async def main():
        task = asyncio.create_task(harness.run(provider))
        await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "the cancelled run abandoned its in-flight save"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert ("save_done",) in harness.log
    assert not any(e[0] == "dispatch" for e in harness.log)
