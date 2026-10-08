"""Tests for adaptive-thinking support in the Anthropic provider.

Covers the registry-gated request configuration (``thinking`` +
``output_config.effort``), thinking/redacted_thinking block accumulation in
the streaming loop, replay-safety guards (all-thinking turns are never
persisted), and the cancellation path.
"""

import asyncio
from types import SimpleNamespace

import pytest

from chat.llm.anthropic_provider import AnthropicProvider


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_CANCEL = object()  # sentinel: raise asyncio.CancelledError at this point


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for event in self._events:
            if event is _CANCEL:
                raise asyncio.CancelledError()
            yield event


class _FakeClient:
    """Captures beta.messages.stream(**kwargs) and replays canned events."""

    def __init__(self, events):
        self.captured_kwargs = None
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.captured_kwargs = kwargs
                return _FakeStream(events)

        self.beta = SimpleNamespace(messages=_Messages())


def _ev(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _message_start(input_tokens=10):
    return _ev(
        "message_start",
        message=SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cache_creation=None,
            ),
        ),
    )


def _block_start(block_type, **fields):
    return _ev(
        "content_block_start",
        content_block=SimpleNamespace(type=block_type, **fields),
    )


def _delta(delta_type, **fields):
    return _ev(
        "content_block_delta",
        delta=SimpleNamespace(type=delta_type, **fields),
    )


_BLOCK_STOP = _ev("content_block_stop")


def _thinking_block_events(signature="sig-abc", text_deltas=()):
    """A complete thinking block: start, optional text deltas, signature, stop."""
    events = [_block_start("thinking", thinking="", signature="")]
    for chunk in text_deltas:
        events.append(_delta("thinking_delta", thinking=chunk))
    events.append(_delta("signature_delta", signature=signature))
    events.append(_BLOCK_STOP)
    return events


def _run_stream(provider, session, message, events):
    """Drive send_message_stream against a fake client; return (events, client)."""
    fake = _FakeClient(events)
    provider._get_client = lambda region, fallback_chain=(): fake

    async def go():
        out = []
        async for ev in provider.send_message_stream(session, message):
            out.append(ev)
        return out

    return asyncio.run(go()), fake


def _make_session(provider, model):
    return provider.create_session(model=model, system_prompt="sys", tools=[])


# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------

def test_opus_4_8_request_carries_adaptive_thinking_and_effort():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        _block_start("text"),
        _delta("text_delta", text="hi"),
        _BLOCK_STOP,
    ]
    _, fake = _run_stream(provider, session, "hello", events)

    assert fake.captured_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.captured_kwargs["output_config"] == {"effort": "medium"}


def test_opus_5_5_request_carries_adaptive_thinking_and_medium_effort():
    """Opus 5.5 cannot run with thinking disabled, so the registry must pin
    an explicit effort level and the request must carry adaptive thinking."""
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5-5")
    events = [
        _message_start(),
        _block_start("text"),
        _delta("text_delta", text="hi"),
        _BLOCK_STOP,
    ]
    _, fake = _run_stream(provider, session, "hello", events)

    assert fake.captured_kwargs["model"] == "claude-opus-5-5"
    assert fake.captured_kwargs["max_tokens"] == 128_000
    assert fake.captured_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.captured_kwargs["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in fake.captured_kwargs  # forced tool use 400s


def test_sonnet_5_5_request_carries_adaptive_thinking_and_high_effort():
    """Sonnet 5.5 cannot run with thinking disabled either, so the registry
    pins its effort at the model's own ``high`` default."""
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-sonnet-5-5")
    events = [
        _message_start(),
        _block_start("text"),
        _delta("text_delta", text="hi"),
        _BLOCK_STOP,
    ]
    _, fake = _run_stream(provider, session, "hello", events)

    assert fake.captured_kwargs["model"] == "claude-sonnet-5-5"
    assert fake.captured_kwargs["max_tokens"] == 128_000
    assert fake.captured_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.captured_kwargs["output_config"] == {"effort": "high"}
    assert "tool_choice" not in fake.captured_kwargs  # forced tool use 400s


def test_haiku_5_5_request_carries_adaptive_thinking_and_medium_effort():
    """Haiku 5.5 thinks by default; the registry pins its effort at the
    model's own ``medium`` default and the 128K output cap. Sampling params
    400 on this model, so the request must not carry any."""
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-haiku-5-5")
    events = [
        _message_start(),
        _block_start("text"),
        _delta("text_delta", text="hi"),
        _BLOCK_STOP,
    ]
    _, fake = _run_stream(provider, session, "hello", events)

    assert fake.captured_kwargs["model"] == "claude-haiku-5-5"
    assert fake.captured_kwargs["max_tokens"] == 128_000
    assert fake.captured_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.captured_kwargs["output_config"] == {"effort": "medium"}
    for param in ("temperature", "top_p", "top_k", "tool_choice"):
        assert param not in fake.captured_kwargs


def test_model_without_thinking_effort_sends_no_thinking_config():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-sonnet-5")
    events = [
        _message_start(),
        _block_start("text"),
        _delta("text_delta", text="hi"),
        _BLOCK_STOP,
    ]
    _, fake = _run_stream(provider, session, "hello", events)

    assert "thinking" not in fake.captured_kwargs
    assert "output_config" not in fake.captured_kwargs


# ---------------------------------------------------------------------------
# Thinking block accumulation
# ---------------------------------------------------------------------------

def test_thinking_block_preserved_in_history_with_signature():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        # display="omitted" shape: no thinking_delta text, just a signature.
        *_thinking_block_events(signature="sig-1"),
        _block_start("text"),
        _delta("text_delta", text="Hello "),
        _delta("text_delta", text="world"),
        _BLOCK_STOP,
    ]
    yielded, _ = _run_stream(provider, session, "hi", events)

    # Only text events are surfaced to the caller -- no thinking events.
    assert [e.type for e in yielded] == ["text", "text"]

    assistant = session.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "text", "text": "Hello world"},
    ]


def test_interleaved_thinking_around_tool_use_keeps_block_order():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        *_thinking_block_events(signature="sig-1", text_deltas=["let me ", "see"]),
        _block_start("tool_use", name="list_skills", id="tu_1"),
        _delta("input_json_delta", partial_json='{"query": "x"}'),
        _BLOCK_STOP,
    ]
    yielded, _ = _run_stream(provider, session, "hi", events)

    assert [e.type for e in yielded] == ["tool_call"]
    assert yielded[0].tool_name == "list_skills"
    assert yielded[0].tool_args == {"query": "x"}

    assistant = session.messages[-1]
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "let me see", "signature": "sig-1"},
        {"type": "tool_use", "id": "tu_1", "name": "list_skills",
         "input": {"query": "x"}},
    ]


def test_redacted_thinking_block_preserved_verbatim():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        _block_start("redacted_thinking", data="opaque-blob"),
        _BLOCK_STOP,
        _block_start("text"),
        _delta("text_delta", text="answer"),
        _BLOCK_STOP,
    ]
    _run_stream(provider, session, "hi", events)

    assistant = session.messages[-1]
    assert assistant["content"][0] == {
        "type": "redacted_thinking",
        "data": "opaque-blob",
    }


def test_all_thinking_turn_is_not_persisted():
    """A turn that produced only thinking (e.g. max_tokens truncation) must
    not land in history: the API strips prior-turn thinking, so it would
    replay as an empty assistant message and 400 every later request."""
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        *_thinking_block_events(signature="sig-1"),
    ]
    _run_stream(provider, session, "hi", events)

    assert session.messages == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def _run_stream_expecting_cancel(provider, session, message, events):
    fake = _FakeClient(events)
    provider._get_client = lambda region, fallback_chain=(): fake

    async def go():
        with pytest.raises(asyncio.CancelledError):
            async for _ in provider.send_message_stream(session, message):
                pass

    asyncio.run(go())


def test_cancel_mid_thinking_drops_unsigned_partial_block():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        _block_start("thinking", thinking="", signature=""),
        _delta("thinking_delta", thinking="partial reason"),
        _CANCEL,  # cancelled before signature_delta / stop
    ]
    _run_stream_expecting_cancel(provider, session, "hi", events)

    # No assistant message: the partial thinking block is unreplayable.
    assert session.messages == [{"role": "user", "content": "hi"}]


def test_cancel_after_text_keeps_completed_thinking_and_partial_text():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-4-8")
    events = [
        _message_start(),
        *_thinking_block_events(signature="sig-1"),
        _block_start("text"),
        _delta("text_delta", text="partial answ"),
        _CANCEL,  # cancelled before content_block_stop
    ]
    _run_stream_expecting_cancel(provider, session, "hi", events)

    assistant = session.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "text", "text": "partial answ"},
    ]
