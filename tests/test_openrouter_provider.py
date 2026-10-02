"""Tests for the OpenRouter provider (chat/llm/openrouter_provider.py).

Covers the OpenAI-format session mechanics without any network: tool-spec
conversion, streaming accumulation against a stubbed SDK client, tool-result
formatting, pending-tool-call detection on serialized history, history
repair, and usage capture. The record_api_call dispatch to the
``llm_calls_openrouter`` raw table is covered in
tests/test_raw_token_usage_capture.py.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chat.llm.openrouter_provider import OpenRouterProvider
from chat.llm.tool_schemas import to_openai_tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def provider():
    return OpenRouterProvider()


@pytest.fixture
def session(provider):
    return provider.create_session(
        model="deepseek/deepseek-v4-flash-0731",
        system_prompt="Be helpful.",
        tools=[{
            "name": "get_time",
            "description": "Get the time.",
            "parameters": {"type": "object", "properties": {}},
        }],
    )


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------

def test_to_openai_tools_shape():
    tools = to_openai_tools([{
        "name": "get_time",
        "description": "Get the time.",
        "parameters": {"type": "object", "properties": {}},
    }])
    assert tools == [{
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the time.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]


def test_to_openai_tools_prefers_openai_description():
    tools = to_openai_tools([{
        "name": "t",
        "description": "generic",
        "parameters": {"type": "object", "properties": {}},
        "provider_descriptions": {"openai": "openai-specific", "gemini": "nope"},
    }])
    assert tools[0]["function"]["description"] == "openai-specific"


# ---------------------------------------------------------------------------
# Streaming against a stubbed client
# ---------------------------------------------------------------------------

def _chunk(delta=None, usage=None, finish_reason=None):
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice] if delta is not None else [], usage=usage)


def _text_delta(text):
    return SimpleNamespace(content=text, tool_calls=None)


def _tool_delta(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(
            index=index,
            id=id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )],
    )


_USAGE = SimpleNamespace(
    prompt_tokens=120,
    completion_tokens=30,
    total_tokens=150,
    prompt_tokens_details=SimpleNamespace(cached_tokens=100),
    completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
)


class _StubStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _StubClient:
    def __init__(self, chunks):
        self.chunks = chunks
        self.kwargs = None
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    async def _create(self, **kwargs):
        self.kwargs = kwargs
        return _StubStream(self.chunks)


def _stream_all(provider, session, message, chunks):
    client = _StubClient(chunks)
    provider._client = client

    async def _go():
        events = []
        async for event in provider.send_message_stream(session, message):
            events.append(event)
        return events

    return client, _run(_go())


def test_stream_text_and_tool_call(provider, session):
    chunks = [
        _chunk(_text_delta("Hel")),
        _chunk(_text_delta("lo")),
        _chunk(_tool_delta(0, id="call_1", name="get_time", arguments='{"a"')),
        _chunk(_tool_delta(0, arguments=': 1}')),
        _chunk(usage=_USAGE),
    ]
    client, events = _stream_all(provider, session, "hi", chunks)

    # System prompt is prepended at the call boundary, not stored in history.
    assert client.kwargs["messages"][0] == {"role": "system", "content": "Be helpful."}
    assert client.kwargs["tools"][0]["function"]["name"] == "get_time"
    assert client.kwargs["stream_options"] == {"include_usage": True}
    # OpenRouter accounting opt-in: the usage chunk then carries ``cost``.
    assert client.kwargs["extra_body"] == {"usage": {"include": True}}

    text_events = [e for e in events if e.type == "text"]
    assert "".join(e.text for e in text_events) == "Hello"
    tool_events = [e for e in events if e.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "get_time"
    assert tool_events[0].tool_args == {"a": 1}
    assert tool_events[0].tool_id == "call_1"

    # History: user message then assistant message with text + tool_calls.
    assert session.messages[0] == {"role": "user", "content": "hi"}
    assistant = session.messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Hello"
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_stream_malformed_arguments_surface_as_tool_args_error(provider, session):
    """A weaker model emitting non-JSON ``function.arguments`` must not
    poison the conversation: the event carries the parse error (so the
    loop returns it to the model instead of dispatching), and the stored
    history replays ``{}`` -- the raw text would 400 every later request."""
    chunks = [
        _chunk(_tool_delta(0, id="call_1", name="get_time", arguments='{"a": ')),
        _chunk(_tool_delta(0, arguments='oops')),
        _chunk(usage=_USAGE),
    ]
    _client, events = _stream_all(provider, session, "hi", chunks)

    tool_events = [e for e in events if e.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "get_time"
    assert tool_events[0].tool_args == {}
    assert "not valid JSON" in tool_events[0].tool_args_error
    assert '{"a": oops' in tool_events[0].tool_args_error

    stored = session.messages[1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(stored) == {}


def test_stream_non_object_arguments_surface_as_tool_args_error(provider, session):
    chunks = [
        _chunk(_tool_delta(0, id="call_1", name="get_time", arguments='[1, 2]')),
        _chunk(usage=_USAGE),
    ]
    _client, events = _stream_all(provider, session, "hi", chunks)
    tool_events = [e for e in events if e.type == "tool_call"]
    assert tool_events[0].tool_args == {}
    assert "must be a JSON object" in tool_events[0].tool_args_error
    assert json.loads(session.messages[1]["tool_calls"][0]["function"]["arguments"]) == {}


def test_stream_valid_arguments_have_no_error(provider, session):
    chunks = [
        _chunk(_tool_delta(0, id="call_1", name="get_time", arguments='{"a": 1}')),
        _chunk(usage=_USAGE),
    ]
    _client, events = _stream_all(provider, session, "hi", chunks)
    tool_events = [e for e in events if e.type == "tool_call"]
    assert tool_events[0].tool_args == {"a": 1}
    assert tool_events[0].tool_args_error == ""


def test_stream_generates_fallback_tool_id(provider, session):
    chunks = [
        _chunk(_tool_delta(0, name="get_time", arguments="{}")),
        _chunk(usage=_USAGE),
    ]
    _client, events = _stream_all(provider, session, "hi", chunks)
    tool_events = [e for e in events if e.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_id.startswith("call_")


def test_usage_capture_and_get_usage(provider, session):
    chunks = [_chunk(_text_delta("ok")), _chunk(usage=_USAGE)]
    _stream_all(provider, session, "hi", chunks)

    usage = provider.get_usage(session)
    # prompt_tokens INCLUDES the cached subset (Gemini-style convention).
    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.cached_tokens == 100
    assert usage.raw_usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "cached_prompt_tokens": 100,
        "reasoning_tokens": 5,
    }


def test_usage_capture_records_reported_cost(provider, session):
    """The OpenRouter accounting fields ride along in raw_usage verbatim
    (``cost_details`` flattened to ``upstream_inference_cost``); malformed
    values are skipped rather than recorded."""
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cost=0.000123,
        cost_details={"upstream_inference_cost": 0.0001},
        is_byok=True,
    )
    _stream_all(provider, session, "hi", [_chunk(usage=usage)])
    raw = provider.get_usage(session).raw_usage
    assert raw["cost"] == pytest.approx(0.000123)
    assert raw["upstream_inference_cost"] == pytest.approx(0.0001)
    assert raw["is_byok"] is True

    # Absent / malformed accounting fields never reach raw_usage.
    _stream_all(provider, session, "hi", [_chunk(usage=SimpleNamespace(
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
        prompt_tokens_details=None, completion_tokens_details=None,
        cost="not-a-number", cost_details=None, is_byok="yes",
    ))])
    raw = provider.get_usage(session).raw_usage
    assert "cost" not in raw
    assert "upstream_inference_cost" not in raw
    assert "is_byok" not in raw


def test_tool_results_extend_history_as_tool_messages(provider, session):
    session.messages.extend([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_time", "arguments": "{}"},
        }]},
    ])
    formatted = provider.format_tool_results(session, [
        {"name": "get_time", "result": "12:00", "tool_id": "call_1"},
    ])
    assert formatted == [
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
    ]
    provider.append_user_text(formatted, "also, hello")
    assert formatted[-1] == {"role": "user", "content": "also, hello"}

    chunks = [_chunk(_text_delta("done")), _chunk(usage=_USAGE)]
    _stream_all(provider, session, formatted, chunks)
    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]


# ---------------------------------------------------------------------------
# Pending tool calls on serialized history
# ---------------------------------------------------------------------------

def test_pending_tool_calls_detected_on_last_assistant_message(provider):
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_9", "type": "function",
            "function": {"name": "wait_for_handles", "arguments": '{"handle_ids": ["h1"]}'},
        }]},
    ]
    pending = provider.get_pending_tool_use_args_from_history(history)
    assert pending == [("call_9", "wait_for_handles", {"handle_ids": ["h1"]})]


def test_no_pending_tool_calls_when_answered(provider):
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_9", "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_9", "content": "ok"},
    ]
    assert provider.get_pending_tool_use_args_from_history(history) == []


# ---------------------------------------------------------------------------
# History repair
# ---------------------------------------------------------------------------

def _tc(tool_id):
    return {"id": tool_id, "type": "function",
            "function": {"name": "t", "arguments": "{}"}}


def test_repair_closes_orphaned_mid_history_tool_call(provider, session):
    session.messages.extend([
        {"role": "assistant", "content": None, "tool_calls": [_tc("call_1")]},
        {"role": "user", "content": "interrupting message"},
        {"role": "assistant", "content": "done"},
    ])
    repaired = provider.repair_session_history(session)
    assert repaired == 1
    inserted = session.messages[1]
    assert inserted["role"] == "tool"
    assert inserted["tool_call_id"] == "call_1"
    assert "interrupted" in inserted["content"]


def test_repair_drops_orphaned_tool_message(provider, session):
    session.messages.extend([
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_ghost", "content": "orphan"},
        {"role": "assistant", "content": "done"},
    ])
    repaired = provider.repair_session_history(session)
    assert repaired == 1
    assert all(m.get("role") != "tool" for m in session.messages)


def test_repair_leaves_final_dangling_tool_call_alone(provider, session):
    session.messages.extend([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("call_1")]},
    ])
    assert provider.repair_session_history(session) == 0
    assert len(session.messages) == 2


def test_repair_replaces_malformed_arguments_in_history(provider, session):
    """Histories persisted before the stream-time guard existed carry the
    raw malformed text; repair rewrites it so the next request is accepted."""
    session.messages.extend([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "t", "arguments": '{"a": oops'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "err"},
        {"role": "assistant", "content": "done"},
    ])
    repaired = provider.repair_session_history(session)
    assert repaired == 1
    assert session.messages[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    # Pairing is intact, so nothing else was touched.
    assert len(session.messages) == 4


def test_repair_valid_history_is_noop(provider, session):
    session.messages.extend([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ])
    before = [dict(m) for m in session.messages]
    assert provider.repair_session_history(session) == 0
    assert session.messages == before


# ---------------------------------------------------------------------------
# Misc surface
# ---------------------------------------------------------------------------

def test_upload_file_unsupported(provider):
    assert _run(provider.upload_file("/tmp/x.pdf", "application/pdf")) is None


def test_history_round_trip(provider, session):
    session.messages.append({"role": "user", "content": "hi"})
    saved = provider.save_history(session)
    restored = provider.create_session(
        model=session.model, system_prompt="s", tools=[],
        history=provider.load_history(saved),
    )
    assert restored.messages == saved


def test_client_requires_api_key(provider, monkeypatch):
    import config.inference_providers as ip

    monkeypatch.setattr(ip, "effective_api_key", lambda p: (None, None))
    with pytest.raises(ValueError, match="OpenRouter API key not configured"):
        provider._get_client()


def test_reset_cached_clients_drops_client(provider):
    provider._client = object()
    provider.reset_cached_clients()
    assert provider._client is None
