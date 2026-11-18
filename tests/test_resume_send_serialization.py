"""Tests for send-vs-resume serialization and Anthropic history repair.

An approved action request kicks a headless resume (chat/wait_handles/
resume.py) while the FE composer unlocks immediately, so a user send
could race the resume on the same shared in-memory session. Interleaved
appends orphan tool_use blocks mid-history, after which every Anthropic
call for the conversation is rejected with a 400 ("tool_use ids were
found without tool_result blocks immediately after").

Covered here:
1. ``AnthropicProvider.repair_session_history`` -- self-heals corrupted
   histories (orphaned tool_use closed with a synthetic "interrupted"
   result, stray/duplicate tool_result blocks dropped) while leaving
   healthy histories and the legitimate final-message suspend shape
   untouched.
2. ``maybe_kick_resume`` cross-guards -- defers while a WS send run is
   active for the conversation, and re-kicks after an in-flight resume
   completes so a resolution arriving mid-unwind is never dropped.
3. Resume run-lifecycle events -- ``_run_resume`` publishes
   ``resume_started`` on entry and ``send_message_finished`` (with
   ``interrupted`` / ``error`` flags) in its ``finally`` so viewing tabs
   hold the composer in the streaming/stop state for the resume's whole
   lifetime; a cancelled resume also appends the durable "interrupted"
   marker; the WS ``stop`` handler cancels an active resume task.

All tests are pure mocks (no DB, no network), matching the repo's
asyncio.run test convention.
"""

import asyncio
import json
from types import SimpleNamespace

from chat.llm.anthropic_provider import AnthropicProvider


def _session(messages):
    return SimpleNamespace(messages=messages)


def _tool_use(tid, name="create_action_request"):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def _tool_result(tid, content="{}"):
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def _assert_valid_pairing(messages):
    """Assert the Anthropic pairing invariants hold for ``messages``."""
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if msg["role"] == "assistant" and isinstance(content, list):
            ids = {
                b["id"] for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            }
            if ids and i < len(messages) - 1:
                nxt = messages[i + 1]
                nxt_content = nxt.get("content")
                assert nxt["role"] == "user" and isinstance(nxt_content, list)
                answered = {
                    b.get("tool_use_id") for b in nxt_content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                assert ids <= answered
        if msg["role"] == "user" and isinstance(content, list):
            prev_ids = set()
            if i > 0 and messages[i - 1]["role"] == "assistant":
                prev = messages[i - 1].get("content")
                if isinstance(prev, list):
                    prev_ids = {
                        b["id"] for b in prev
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    }
            seen = set()
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b["tool_use_id"] in prev_ids
                    assert b["tool_use_id"] not in seen
                    seen.add(b["tool_use_id"])


# ---------------------------------------------------------------------------
# repair_session_history
# ---------------------------------------------------------------------------

def test_repair_noop_on_healthy_history():
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [_tool_use("t1")]},
        {"role": "user", "content": [_tool_result("t1")]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    snapshot = json.loads(json.dumps(messages))

    assert provider.repair_session_history(_session(messages)) == 0
    assert messages == snapshot


def test_repair_leaves_final_dangling_tool_use_for_resume_bucket():
    """The legitimate suspend shape must NOT be closed by repair -- the
    resume bucket delivers the real verdict there."""
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "edit the sheet"},
        {"role": "assistant", "content": [_tool_use("t1")]},
    ]
    snapshot = json.loads(json.dumps(messages))

    assert provider.repair_session_history(_session(messages)) == 0
    assert messages == snapshot


def test_repair_closes_orphan_before_plain_text_user_message():
    """The incident shape: a racing send appended its user text directly
    after an assistant tool_use, orphaning it mid-history."""
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "edit the sheet"},
        {"role": "assistant", "content": [_tool_use("t1")]},
        {"role": "user", "content": "please continue"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]

    repaired = provider.repair_session_history(_session(messages))

    assert repaired == 1
    blocks = messages[2]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "t1"
    assert "interrupted" in blocks[0]["content"]
    assert blocks[1] == {"type": "text", "text": "please continue"}
    _assert_valid_pairing(messages)


def test_repair_prepends_missing_result_to_block_list_message():
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [_tool_use("t1"), _tool_use("t2")]},
        # Only t1 answered; t2 orphaned by an interleaved append.
        {"role": "user", "content": [_tool_result("t1"),
                                     {"type": "text", "text": "next"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]

    repaired = provider.repair_session_history(_session(messages))

    assert repaired == 1
    ids = [b.get("tool_use_id") for b in messages[2]["content"]
           if b.get("type") == "tool_result"]
    assert sorted(ids) == ["t1", "t2"]
    _assert_valid_pairing(messages)


def test_repair_inserts_user_message_between_two_assistant_messages():
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [_tool_use("t1")]},
        {"role": "assistant", "content": [{"type": "text", "text": "hm"}]},
    ]

    repaired = provider.repair_session_history(_session(messages))

    assert repaired == 1
    assert len(messages) == 4
    inserted = messages[2]
    assert inserted["role"] == "user"
    assert inserted["content"][0]["tool_use_id"] == "t1"
    _assert_valid_pairing(messages)


def test_repair_drops_stray_and_duplicate_tool_results():
    provider = AnthropicProvider()
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [_tool_use("t1")]},
        # Duplicate answer for t1 plus a stale result for a long-gone id.
        {"role": "user", "content": [_tool_result("t1"), _tool_result("t1"),
                                     _tool_result("ancient")]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]

    repaired = provider.repair_session_history(_session(messages))

    assert repaired == 2
    results = [b for b in messages[2]["content"]
               if b.get("type") == "tool_result"]
    assert len(results) == 1 and results[0]["tool_use_id"] == "t1"
    _assert_valid_pairing(messages)


def test_repair_replaces_fully_stray_message_content_with_placeholder():
    provider = AnthropicProvider()
    messages = [
        # No preceding assistant message: this tool_result answers nothing.
        {"role": "user", "content": [_tool_result("ghost")]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    repaired = provider.repair_session_history(_session(messages))

    assert repaired == 1
    assert messages[0]["content"][0]["type"] == "text"
    _assert_valid_pairing(messages)


# ---------------------------------------------------------------------------
# maybe_kick_resume cross-guards
# ---------------------------------------------------------------------------

async def _drain(loop_turns=10):
    for _ in range(loop_turns):
        await asyncio.sleep(0)


def test_maybe_kick_resume_defers_while_send_run_active(monkeypatch):
    from chat.wait_handles import resume as resume_mod
    from chat.realtime import socket as socket_mod

    async def scenario():
        kicked = []

        async def fake_run_resume(app, user, conversation_id):
            kicked.append(conversation_id)

        monkeypatch.setattr(resume_mod, "_run_resume", fake_run_resume)
        monkeypatch.setattr(
            resume_mod, "_conversation_has_dangling_wait", lambda cid: True,
        )

        send_gate = asyncio.Event()

        async def fake_send():
            await send_gate.wait()

        send_task = asyncio.create_task(fake_send())
        monkeypatch.setitem(socket_mod._active_send_runs, "conv-1", send_task)

        got = resume_mod.maybe_kick_resume(None, {"id": 1}, "conv-1")
        assert got is None
        await _drain()
        assert kicked == []  # deferred, not dropped

        send_gate.set()
        await send_task
        await _drain()
        assert kicked == ["conv-1"]  # re-kicked once the send finished

    asyncio.run(scenario())


def _patch_resume_environment(monkeypatch, resume_mod, *, run_impl):
    """Patch out the DB / model-loop dependencies of ``_run_resume`` and
    return (events, messages) capture lists: ``events`` collects every
    ``bus.publish_to_conversation`` envelope, ``messages`` is the shared
    ``messages_out`` list handed to the flush callback.
    """
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

    messages = {}

    def fake_make_flush(conversation_id, messages_out, *, log_prefix):
        messages["out"] = messages_out

        async def flush():
            return None

        return flush, asyncio.Lock()

    monkeypatch.setattr(resume_mod, "make_flush_callback", fake_make_flush)
    return events, messages


def test_run_resume_publishes_lifecycle_events(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    async def scenario():
        async def fake_run(**kwargs):
            return None

        events, _ = _patch_resume_environment(
            monkeypatch, resume_mod, run_impl=fake_run,
        )

        await resume_mod._run_resume(None, {"id": 1}, "conv-life")

        assert events[0]["type"] == "resume_started"
        finished = events[-1]
        assert finished["type"] == "send_message_finished"
        assert finished["interrupted"] is False
        assert finished["error"] is False

    asyncio.run(scenario())


def test_run_resume_publishes_error_flag_on_failure(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    async def scenario():
        async def fake_run(**kwargs):
            raise RuntimeError("boom")

        events, _ = _patch_resume_environment(
            monkeypatch, resume_mod, run_impl=fake_run,
        )

        await resume_mod._run_resume(None, {"id": 1}, "conv-err")

        finished = events[-1]
        assert finished["type"] == "send_message_finished"
        assert finished["interrupted"] is False
        assert finished["error"] is True

    asyncio.run(scenario())


def test_run_resume_cancel_marks_interrupted(monkeypatch):
    from chat.wait_handles import resume as resume_mod
    import chat.routes._helpers as helpers_mod
    import chat.gemini_api.session as session_mod

    async def scenario():
        started = asyncio.Event()

        async def fake_run(**kwargs):
            started.set()
            await asyncio.Event().wait()

        events, messages = _patch_resume_environment(
            monkeypatch, resume_mod, run_impl=fake_run,
        )
        monkeypatch.setattr(
            helpers_mod, "_save_interrupted_sdk_history",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            session_mod, "remove_chat_session", lambda *a, **k: None,
        )

        task = asyncio.create_task(
            resume_mod._run_resume(None, {"id": 1}, "conv-cancel"),
        )
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        finished = events[-1]
        assert finished["type"] == "send_message_finished"
        assert finished["interrupted"] is True
        # The durable marker rides with the final flush, mirroring the WS
        # send path's stop handling.
        assert any(
            m.get("type") == "interrupted" for m in messages["out"]
        )

    asyncio.run(scenario())


def _stub_stop_dependencies(monkeypatch, socket_mod, *, owner_id=1):
    """Stub what ``_handle_stop`` reaches besides the run registries: the
    ownership lookup (``conv-stop`` belongs to ``owner_id``), the wait-handle
    cancel and the AutoProject-run stop. Returns the client events, the
    cancelled-handle calls and the AutoProject-stop calls."""
    import db.conversation_store as conversation_store
    from chat.autoprojects import runs as autoproject_runs

    acked, cancelled, stopped = [], [], []

    async def fake_send_to_client(conn, event):
        acked.append(event)

    async def fake_cancel_handles(user_id, conversation_id):
        cancelled.append((user_id, conversation_id))

    async def fake_get_meta(user_id, conversation_id):
        if conversation_id == "conv-stop" and user_id == owner_id:
            return {"id": conversation_id, "user_id": user_id}
        return None

    async def fake_stop_autoproject_run(user_id, conversation_id):
        stopped.append((user_id, conversation_id))

    monkeypatch.setattr(socket_mod, "_send_to_client", fake_send_to_client)
    monkeypatch.setattr(
        socket_mod, "cancel_pending_wait_handles_for_conversation", fake_cancel_handles,
    )
    monkeypatch.setattr(conversation_store, "get_conversation_meta", fake_get_meta)
    monkeypatch.setattr(autoproject_runs, "stop_autoproject_run", fake_stop_autoproject_run)
    return acked, cancelled, stopped


def test_handle_stop_cancels_active_resume(monkeypatch):
    from chat.wait_handles import resume as resume_mod
    from chat.realtime import socket as socket_mod

    async def scenario():
        acked, cancelled, stopped = _stub_stop_dependencies(monkeypatch, socket_mod)

        resume_task = asyncio.create_task(asyncio.Event().wait())
        monkeypatch.setitem(
            resume_mod._active_resumes, "conv-stop", resume_task,
        )

        conn = SimpleNamespace(user_id=1)
        await socket_mod._handle_stop(conn, {"conversation_id": "conv-stop"})
        await asyncio.wait_for(
            asyncio.gather(resume_task, return_exceptions=True), timeout=5,
        )

        assert resume_task.cancelled()
        assert cancelled == [(1, "conv-stop")]
        assert stopped == [(1, "conv-stop")]
        assert acked and acked[-1]["type"] == "stop_acknowledged"

    asyncio.run(scenario())


def test_handle_stop_ignores_another_users_conversation(monkeypatch):
    """The run registries are keyed by conversation id alone: a stop for a
    conversation the caller does not own cancels nothing and is ignored
    silently."""
    from chat.wait_handles import resume as resume_mod
    from chat.realtime import socket as socket_mod

    async def scenario():
        acked, cancelled, stopped = _stub_stop_dependencies(
            monkeypatch, socket_mod, owner_id=2,
        )

        resume_task = asyncio.create_task(asyncio.Event().wait())
        monkeypatch.setitem(
            resume_mod._active_resumes, "conv-stop", resume_task,
        )

        conn = SimpleNamespace(user_id=1)
        await socket_mod._handle_stop(conn, {"conversation_id": "conv-stop"})
        await asyncio.sleep(0)

        assert not resume_task.done()
        assert (acked, cancelled, stopped) == ([], [], [])
        resume_task.cancel()
        await asyncio.gather(resume_task, return_exceptions=True)

    asyncio.run(scenario())


def test_maybe_kick_resume_rekicks_after_inflight_resume(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    async def scenario():
        calls = []
        first_gate = asyncio.Event()

        async def fake_run_resume(app, user, conversation_id):
            calls.append(conversation_id)
            if len(calls) == 1:
                await first_gate.wait()

        monkeypatch.setattr(resume_mod, "_run_resume", fake_run_resume)
        monkeypatch.setattr(
            resume_mod, "_conversation_has_dangling_wait", lambda cid: True,
        )

        first = resume_mod.maybe_kick_resume(None, {"id": 1}, "conv-2")
        assert first is not None
        await _drain()

        # A second resolution lands while the first resume is in flight:
        # it must not be dropped once the first task completes.
        second = resume_mod.maybe_kick_resume(None, {"id": 1}, "conv-2")
        assert second is first

        first_gate.set()
        await first
        await _drain()
        assert calls == ["conv-2", "conv-2"]

        # Let the re-kicked task finish so nothing leaks across tests.
        leftover = resume_mod._active_resumes.get("conv-2")
        if leftover is not None:
            await leftover

    asyncio.run(scenario())
