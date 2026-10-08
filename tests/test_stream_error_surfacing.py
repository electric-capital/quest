"""Tests for surfacing conversation-run errors to the user.

Covers the three fixes for "conversation silently stops with no error":

1. ``GeminiProvider.send_message_stream`` raises
   ``GeminiStreamAbnormalTermination`` when the stream ends with an
   abnormal finish reason (previously the empty turn looked like a
   normal completion).
2. ``run_conversation_turn`` appends a durable ``{"type": "error"}`` structured
   message (flushed via ``FLUSH_EVENT_TYPES``) and preserves partial
   streamed text when a turn dies.
3. ``_dispatch_tool_call`` converts unexpected handler exceptions into a
   structured error result instead of aborting the whole run.
4. A tool call whose arguments the provider could not parse
   (``StreamEvent.tool_args_error``, e.g. malformed JSON from a weaker
   chat-completions model) is answered with a structured error tool result
   instead of being dispatched on empty arguments.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from google.genai import types

from chat.llm.gemini_provider import (
    GeminiProvider,
    GeminiStreamAbnormalTermination,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Gemini stream chunk fakes
# ---------------------------------------------------------------------------


def _chunk(
    *,
    text: str | None = None,
    function_call: object | None = None,
    finish_reason: object | None = None,
    finish_message: str | None = None,
    block_reason: object | None = None,
):
    parts = []
    if text is not None or function_call is not None:
        parts.append(SimpleNamespace(text=text, function_call=function_call))
    candidate = SimpleNamespace(
        finish_reason=finish_reason,
        finish_message=finish_message,
        content=SimpleNamespace(parts=parts) if parts else None,
    )
    prompt_feedback = (
        SimpleNamespace(block_reason=block_reason)
        if block_reason is not None else None
    )
    return SimpleNamespace(
        usage_metadata=None,
        candidates=[candidate],
        prompt_feedback=prompt_feedback,
    )


class _FakeSession:
    """Minimal stand-in for a genai AsyncChat session."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._llm_last_usage = None

    async def send_message_stream(self, message):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


def _collect_events(chunks):
    provider = GeminiProvider()
    session = _FakeSession(chunks)

    async def _drain():
        events = []
        async for event in provider.send_message_stream(session, "hi"):
            events.append(event)
        return events

    return _run(_drain())


class TestGeminiFinishReason:
    def test_stop_with_text_is_normal(self):
        events = _collect_events([
            _chunk(text="hello"),
            _chunk(finish_reason=types.FinishReason.STOP),
        ])
        assert [e.type for e in events] == ["text"]
        assert events[0].text == "hello"

    def test_no_finish_reason_is_normal(self):
        events = _collect_events([_chunk(text="hello")])
        assert len(events) == 1

    def test_malformed_function_call_raises(self):
        with pytest.raises(GeminiStreamAbnormalTermination) as exc_info:
            _collect_events([
                _chunk(finish_reason=types.FinishReason.MALFORMED_FUNCTION_CALL),
            ])
        assert "MALFORMED_FUNCTION_CALL" in str(exc_info.value)

    def test_safety_raises_even_with_partial_text(self):
        with pytest.raises(GeminiStreamAbnormalTermination) as exc_info:
            _collect_events([
                _chunk(text="partial"),
                _chunk(finish_reason=types.FinishReason.SAFETY),
            ])
        assert "SAFETY" in str(exc_info.value)

    def test_finish_message_included_in_error(self):
        with pytest.raises(GeminiStreamAbnormalTermination) as exc_info:
            _collect_events([
                _chunk(
                    finish_reason=types.FinishReason.RECITATION,
                    finish_message="blocked for recitation",
                ),
            ])
        assert "blocked for recitation" in str(exc_info.value)

    def test_max_tokens_with_text_delivers_truncated_turn(self):
        events = _collect_events([
            _chunk(text="long partial answer"),
            _chunk(finish_reason=types.FinishReason.MAX_TOKENS),
        ])
        assert [e.type for e in events] == ["text"]

    def test_max_tokens_with_no_output_raises(self):
        # Truncation mid-function-call: nothing was yielded, so the turn
        # would otherwise end silently.
        with pytest.raises(GeminiStreamAbnormalTermination) as exc_info:
            _collect_events([
                _chunk(finish_reason=types.FinishReason.MAX_TOKENS),
            ])
        assert "MAX_TOKENS" in str(exc_info.value)

    def test_blocked_prompt_raises(self):
        with pytest.raises(GeminiStreamAbnormalTermination) as exc_info:
            _collect_events([
                _chunk(block_reason=types.BlockedReason.SAFETY),
            ])
        assert "block_reason" in str(exc_info.value)

    def test_function_call_turn_with_stop_is_normal(self):
        fc = SimpleNamespace(name="get_current_time", args={}, id="t1")
        events = _collect_events([
            _chunk(function_call=fc),
            _chunk(finish_reason=types.FinishReason.STOP),
        ])
        assert [e.type for e in events] == ["tool_call"]
        assert events[0].tool_name == "get_current_time"


# ---------------------------------------------------------------------------
# FLUSH_EVENT_TYPES
# ---------------------------------------------------------------------------


def test_error_is_a_durable_flush_event():
    from chat._flush_helper import FLUSH_EVENT_TYPES
    assert "error" in FLUSH_EVENT_TYPES


# ---------------------------------------------------------------------------
# Tool dispatch exception wrapping
# ---------------------------------------------------------------------------


class TestDispatchToolCallCatchesExceptions:
    def test_direct_tool_exception_becomes_error_result(self, monkeypatch):
        from chat.gemini_api import tool_dispatch

        def _boom(_timezone):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(tool_dispatch, "_handle_get_current_time", _boom)

        result, extra_parts = _run(tool_dispatch._dispatch_tool_call(
            app=None,
            provider=MagicMock(),
            user={"id": 1, "email": "t@example.com"},
            conversation_id="c1",
            timezone="UTC",
            tool_name="get_current_time",
            args={},
        ))
        parsed = json.loads(result)
        assert "handler exploded" in parsed["error"]
        assert "get_current_time" in parsed["error"]
        assert extra_parts == []

    def test_inner_tool_call_label_used_in_error(self, monkeypatch):
        from chat.gemini_api import tool_dispatch

        async def _boom(_user_id, _query):
            raise ValueError("bad memory")

        monkeypatch.setattr(tool_dispatch, "_handle_memory_search", _boom)

        # The tool_call inner branch already has its own try/except, so the
        # inner error shape is preserved (not double-wrapped by the outer
        # guard).
        result, extra_parts = _run(tool_dispatch._dispatch_tool_call(
            app=None,
            provider=MagicMock(),
            user={"id": 1, "email": "t@example.com"},
            conversation_id="c1",
            timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "memory_search", "arguments": {"query": "x"}},
        ))
        parsed = json.loads(result)
        assert "bad memory" in parsed["error"]
        assert extra_parts == []


# ---------------------------------------------------------------------------
# run_conversation_turn durable error message
# ---------------------------------------------------------------------------


@pytest.fixture()
def _patched_conversation(monkeypatch):
    """Patch run_conversation_turn's collaborators so a run needs no DB / files."""
    from chat.gemini_api import conversation as conv_mod

    monkeypatch.setattr(
        conv_mod, "load_server_config",
        lambda: {"gemini": {"model": "fake-model"}},
    )
    monkeypatch.setattr(
        conv_mod, "get_provider_for_model", lambda _m: "gemini",
    )
    monkeypatch.setattr(
        conv_mod, "get_system_prompt",
        lambda *a, **kw: "system prompt",
    )
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(
        conv_mod, "get_or_create_chat", lambda *a, **kw: object(),
    )

    import db.conversation_store as conv_store
    monkeypatch.setattr(
        conv_store, "update_conversation_model", AsyncMock(),
    )
    import db.skill_store as skill_store
    monkeypatch.setattr(
        skill_store, "get_user_autoloaded_skills",
        AsyncMock(return_value=[]),
    )
    import api.instructions as instructions
    monkeypatch.setattr(
        instructions, "get_user_connected_services", lambda _u: {},
    )

    # Patch through the conversation module's own ChatStorage binding: other
    # test modules reload ``chat.storage``, so ``from chat.storage import
    # ChatStorage`` here could resolve to a different class object than the
    # one run_conversation_turn actually calls.
    async def _fake_history(_cid):
        return {"guide_id": "g", "guide_snapshot": {"content": "guide"}}

    monkeypatch.setattr(conv_mod, "_read_chat_history", _fake_history)
    monkeypatch.setattr(
        conv_mod.ChatStorage, "set_system_prompt",
        staticmethod(lambda *a, **kw: None),
    )
    return conv_mod


def test_run_appends_durable_error_and_partial_text(_patched_conversation):
    conv_mod = _patched_conversation

    provider = MagicMock()
    provider.get_pending_tool_use_args = MagicMock(return_value=[])

    async def _failing_stream(_chat, _message):
        yield SimpleNamespace(type="text", text="partial answer ")
        raise RuntimeError("stream died")

    provider.send_message_stream = _failing_stream

    import chat.llm.config as llm_config
    orig_get_instance = llm_config.get_provider_instance
    conv_mod.get_provider_instance = lambda _n, _i=None: provider  # type: ignore[assignment]
    try:
        events: list[dict] = []

        async def on_event(event: dict) -> None:
            events.append(event)

        messages_out: list[dict] = []
        with pytest.raises(RuntimeError, match="stream died"):
            _run(conv_mod.run_conversation_turn(
                app=None,
                user={
                    "id": 1,
                    "email": "t@example.com",
                    "api_key": "k",
                    "name": "T",
                },
                message="hello",
                conversation_id="conv-err-test",
                timezone="UTC",
                model="fake-model",
                on_event=on_event,
                messages_out=messages_out,
            ))
    finally:
        conv_mod.get_provider_instance = orig_get_instance  # type: ignore[assignment]

    # Partial streamed text is preserved...
    text_msgs = [m for m in messages_out if m.get("type") == "text"]
    assert any("partial answer" in m.get("content", "") for m in text_msgs)

    # ...and a durable error message was appended for persistence.
    error_msgs = [m for m in messages_out if m.get("type") == "error"]
    assert len(error_msgs) == 1
    assert "RuntimeError: stream died" in error_msgs[0]["error"]
    assert error_msgs[0].get("stacktrace")
    assert error_msgs[0].get("timestamp")

    # The transient error event still fires for live subscribers.
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert "RuntimeError: stream died" in error_events[0]["error"]


def test_malformed_tool_arguments_return_error_result(_patched_conversation, monkeypatch):
    """The call is never dispatched; the model gets the parse error back as
    the tool result and the run continues to the next turn."""
    from chat.llm.base import StreamEvent, UsageStats

    conv_mod = _patched_conversation
    provider = MagicMock()
    provider.get_pending_tool_use_args = MagicMock(return_value=[])
    provider.get_usage = MagicMock(return_value=UsageStats())
    provider.repair_session_history = MagicMock(return_value=0)
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
    stream_calls: list = []

    async def _stream(_chat, message):
        stream_calls.append(message)
        if len(stream_calls) == 1:
            yield StreamEvent(
                type="tool_call", tool_name="get_current_time", tool_args={},
                tool_id="call_1",
                tool_args_error="Tool call arguments were not valid JSON (x). Received: {oops",
            )
        else:
            yield StreamEvent(type="text", text="done")

    provider.send_message_stream = _stream
    dispatched = AsyncMock(return_value=("should not run", []))
    monkeypatch.setattr(conv_mod, "_dispatch_tool_call", dispatched)

    orig_get_instance = conv_mod.get_provider_instance
    conv_mod.get_provider_instance = lambda _n, _i=None: provider  # type: ignore[assignment]
    try:
        messages_out: list[dict] = []

        async def on_event(_event: dict) -> None:
            pass

        _run(conv_mod.run_conversation_turn(
            app=None,
            user={"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"},
            message="hello",
            conversation_id="conv-bad-args-test",
            timezone="UTC",
            model="fake-model",
            on_event=on_event,
            messages_out=messages_out,
        ))
    finally:
        conv_mod.get_provider_instance = orig_get_instance  # type: ignore[assignment]

    dispatched.assert_not_called()
    results = [m for m in messages_out if m.get("type") == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_id"] == "call_1"
    payload = json.loads(results[0]["tool_output"])
    assert "get_current_time" in payload["error"]
    assert "not valid JSON" in payload["error"]
    assert "{oops" in payload["error"]
    assert not [m for m in messages_out if m.get("type") == "error"]
    # The error result was fed back and the model answered on the next turn.
    assert len(stream_calls) == 2
    assert any(m.get("type") == "text" and "done" in m.get("content", "") for m in messages_out)
