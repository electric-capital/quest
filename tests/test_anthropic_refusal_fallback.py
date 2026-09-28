"""Tests for refusal-fallback support in the Anthropic provider.

Opus-5-class models run safety classifiers that can decline a request with
``stop_reason: "refusal"``. Models with a ``refusal_fallback_models``
registry entry get the Anthropic SDK's client-side
``BetaRefusalFallbackMiddleware`` (Vertex has no server-side ``fallbacks``
param), which retries the refused request on the fallback models and
splices their output onto the open stream behind a ``fallback`` seam block.

Covers the registry chain resolution, the middleware patch entries, the
client cache keyed by (region, chain), the per-session BetaFallbackState
pinning, the streaming seam-block handling, and the terminal-refusal
error surfacing.

Reuses the fake streaming client from tests/test_anthropic_thinking.py.
"""

import asyncio
from types import SimpleNamespace

import pytest

from chat.llm.anthropic_provider import (
    AnthropicProvider,
    AnthropicStreamRefusal,
)
from tests.test_anthropic_thinking import (
    _BLOCK_STOP,
    _FakeClient,
    _block_start,
    _delta,
    _ev,
    _make_session,
    _message_start,
    _run_stream,
    _thinking_block_events,
)


def _fallback_seam(from_model="claude-opus-5", to_model="claude-opus-4-8",
                   category="cyber"):
    """A complete fallback seam block (start + stop; seams carry no deltas)."""
    return [
        _block_start(
            "fallback",
            from_=SimpleNamespace(model=from_model),
            to=SimpleNamespace(model=to_model),
            trigger=SimpleNamespace(type="refusal", category=category),
        ),
        _BLOCK_STOP,
    ]


def _refusal_delta(category="cyber", explanation=None):
    return _ev(
        "message_delta",
        delta=SimpleNamespace(
            stop_reason="refusal",
            stop_details=SimpleNamespace(
                type="refusal", category=category, explanation=explanation,
            ),
        ),
        usage=SimpleNamespace(output_tokens=3),
    )


def _text_block(text):
    return [_block_start("text"), _delta("text_delta", text=text), _BLOCK_STOP]


# ---------------------------------------------------------------------------
# Registry chain resolution and middleware entries
# ---------------------------------------------------------------------------

def test_opus_5_resolves_opus_4_8_fallback_chain():
    provider = AnthropicProvider()
    assert provider._get_fallback_chain("claude-opus-5") == ("claude-opus-4-8",)


def test_opus_5_5_resolves_opus_4_8_fallback_chain():
    provider = AnthropicProvider()
    assert provider._get_fallback_chain("claude-opus-5-5") == ("claude-opus-4-8",)


def test_sonnet_5_5_resolves_sonnet_5_fallback_chain():
    provider = AnthropicProvider()
    assert provider._get_fallback_chain("claude-sonnet-5-5") == ("claude-sonnet-5",)


def test_models_without_config_have_no_chain():
    provider = AnthropicProvider()
    assert provider._get_fallback_chain("claude-sonnet-5") == ()
    assert provider._get_fallback_chain("claude-opus-4-8") == ()


def test_invalid_fallback_entries_are_skipped(monkeypatch):
    from chat.llm.config import MODEL_REGISTRY

    entry = dict(MODEL_REGISTRY["claude-opus-5"])
    entry["refusal_fallback_models"] = [
        "no-such-model",          # not in the registry
        "gemini-3.7-flash",       # not an Anthropic model
        "claude-opus-4-8",        # valid
    ]
    monkeypatch.setitem(MODEL_REGISTRY, "claude-opus-5", entry)

    provider = AnthropicProvider()
    assert provider._get_fallback_chain("claude-opus-5") == ("claude-opus-4-8",)


def test_fallback_entries_derive_request_knobs_from_fallback_registry():
    provider = AnthropicProvider()
    entries = provider._build_fallback_entries(("claude-opus-4-8",))
    assert entries == [{
        "model": "claude-opus-4-8",
        "max_tokens": 128_000,
        # Opus 4.8 has thinking_effort "medium" in the registry, so the hop
        # patch re-enables adaptive thinking at that effort regardless of
        # what the refused model's request carried.
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }]


def test_fallback_entry_for_thinking_less_model_unsets_thinking():
    provider = AnthropicProvider()
    entries = provider._build_fallback_entries(("claude-sonnet-4-6",))
    # Explicit None is the middleware's "unset this field" patch value.
    assert entries == [{
        "model": "claude-sonnet-4-6",
        "max_tokens": 8_192,
        "thinking": None,
        "output_config": None,
    }]


# ---------------------------------------------------------------------------
# Client construction and caching
# ---------------------------------------------------------------------------

def test_client_cache_keyed_by_region_and_chain(monkeypatch):
    import config.server_config as server_config

    monkeypatch.setattr(
        server_config, "load_server_config",
        lambda: {"anthropic": {"vertex_project_id": "test-project"}},
    )

    provider = AnthropicProvider()
    plain = provider._get_client("global")
    with_fallbacks = provider._get_client("global", ("claude-opus-4-8",))

    assert plain is not with_fallbacks
    assert provider._get_client("global") is plain
    assert provider._get_client("global", ("claude-opus-4-8",)) is with_fallbacks

    # Only the fallback-configured client carries the refusal middleware.
    from anthropic import BetaRefusalFallbackMiddleware

    assert not plain.middleware
    assert len(with_fallbacks.middleware) == 1
    assert isinstance(with_fallbacks.middleware[0], BetaRefusalFallbackMiddleware)


def test_fallback_middleware_sends_no_beta_header(monkeypatch):
    """Vertex rejects the SDK's default ``fallback-credit-2026-07-01`` beta
    with a 400 on every request (refused or not), so the middleware must be
    built with an explicit, Vertex-safe beta list -- currently none."""
    import config.server_config as server_config
    from anthropic.lib.middleware._fallbacks import DEFAULT_BETAS

    monkeypatch.setattr(
        server_config, "load_server_config",
        lambda: {"anthropic": {"vertex_project_id": "test-project"}},
    )

    provider = AnthropicProvider()
    middleware = provider._get_client("global", ("claude-opus-4-8",)).middleware[0]

    assert "fallback-credit-2026-07-01" in DEFAULT_BETAS  # the hazard still exists
    assert middleware._betas == ()
    assert not any(str(b).startswith("fallback-credit") for b in middleware._betas)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def test_session_gets_fallback_state_only_with_chain():
    from anthropic import BetaFallbackState

    provider = AnthropicProvider()
    opus5 = _make_session(provider, "claude-opus-5")
    sonnet5 = _make_session(provider, "claude-sonnet-5")

    assert isinstance(opus5.fallback_state, BetaFallbackState)
    assert sonnet5.fallback_state is None


def test_fallback_state_entered_around_the_stream():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5")

    entered = []

    class _RecordingState:
        def __enter__(self):
            entered.append("enter")
            return self

        def __exit__(self, *exc):
            entered.append("exit")

    session.fallback_state = _RecordingState()
    events = [_message_start(), *_text_block("ok"),
              _ev("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
                  usage=SimpleNamespace(output_tokens=1))]
    _run_stream(provider, session, "hi", events)

    assert entered == ["enter", "exit"]


# ---------------------------------------------------------------------------
# Streaming: fallback seam handling
# ---------------------------------------------------------------------------

def test_seam_block_drops_preboundary_thinking_and_is_not_persisted():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5")

    events = [
        _message_start(),
        # The refused model thought and streamed some text before declining.
        *_thinking_block_events(signature="sig-refused"),
        *_text_block("partial "),
        # Middleware splices the seam, then the fallback model's answer.
        *_fallback_seam(),
        *_text_block("answer"),
        _ev("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=5)),
    ]
    stream_events, _ = _run_stream(provider, session, "hi", events)

    # Both the pre-boundary partial and the fallback answer streamed out.
    assert [e.text for e in stream_events if e.type == "text"] == ["partial ", "answer"]

    # The seam yields a model_fallback StreamEvent so the conversation
    # loop can persist a user-visible notice row, with display names
    # resolved from the registry.
    fallback_events = [e for e in stream_events if e.type == "model_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0].data == {
        "from_model": "claude-opus-5",
        "to_model": "claude-opus-4-8",
        "category": "cyber",
        "from_display": "Claude Opus 5",
        "to_display": "Claude Opus 4.8",
    }

    # History: pre-boundary thinking dropped (the continuation rules forbid
    # echoing it), the seam block itself never persisted, text kept.
    assistant = session.messages[-1]
    assert assistant["role"] == "assistant"
    assert [b["type"] for b in assistant["content"]] == ["text", "text"]

    # Analytics marker recorded and surfaced via raw_usage.
    assert session.last_usage["refusal_fallback"] == {
        "from_model": "claude-opus-5",
        "to_model": "claude-opus-4-8",
        "category": "cyber",
    }
    assert provider.get_usage(session).raw_usage["refusal_fallback"][
        "to_model"] == "claude-opus-4-8"


def test_stream_uses_beta_messages_surface():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5")
    events = [_message_start(), *_text_block("ok"),
              _ev("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
                  usage=SimpleNamespace(output_tokens=1))]
    _, fake = _run_stream(provider, session, "hi", events)
    # _FakeClient only exposes beta.messages.stream -- reaching here means
    # the provider called the beta surface (required by the middleware).
    assert fake.captured_kwargs["model"] == "claude-opus-5"


# ---------------------------------------------------------------------------
# Conversation-loop notice persistence
# ---------------------------------------------------------------------------

def test_stream_turn_persists_fallback_notice_between_partials():
    """_stream_turn turns the model_fallback StreamEvent into a durable
    notice row, flushed between the declined model's partial output and
    the fallback model's answer."""
    from chat.gemini_api.conversation import _stream_turn
    from tests.test_anthropic_thinking import _FakeClient as FakeClient

    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5")
    events = [
        _message_start(),
        *_text_block("partial "),
        *_fallback_seam(),
        *_text_block("answer"),
        _ev("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=5)),
    ]
    provider._get_client = lambda region, fallback_chain=(): FakeClient(events)

    structured: list[dict] = []
    emitted: list[dict] = []

    async def on_event(event):
        emitted.append(event)

    ctx = SimpleNamespace(
        provider=provider, chat=session,
        on_event=on_event, structured_messages=structured,
    )
    text_parts, function_calls = asyncio.run(_stream_turn(ctx, "hi"))

    # Pre-switch partial flushed as its own text message, then the notice.
    assert [m["type"] for m in structured] == ["text", "model_fallback"]
    assert structured[0]["content"] == "partial "
    notice = structured[1]
    assert notice["from_display"] == "Claude Opus 5"
    assert notice["to_display"] == "Claude Opus 4.8"
    assert notice["category"] == "cyber"
    assert notice["timestamp"]

    # The remaining turn text is returned for the normal end-of-turn append.
    assert "".join(text_parts) == "answer"
    assert function_calls == []

    # on_event carried the flush-boundary event (its type is in
    # FLUSH_EVENT_TYPES so callers persist both rows and fan out
    # message_appended).
    from chat._flush_helper import FLUSH_EVENT_TYPES

    assert "model_fallback" in FLUSH_EVENT_TYPES
    assert any(e.get("type") == "model_fallback" for e in emitted)


# ---------------------------------------------------------------------------
# Terminal refusal surfacing
# ---------------------------------------------------------------------------

def _run_stream_expecting_refusal(provider, session, events):
    fake = _FakeClient(events)
    provider._get_client = lambda region, fallback_chain=(): fake

    async def go():
        streamed = []
        with pytest.raises(AnthropicStreamRefusal) as excinfo:
            async for ev in provider.send_message_stream(session, "hi"):
                streamed.append(ev)
        return streamed, excinfo.value

    return asyncio.run(go())


def test_whole_chain_refusal_raises_with_category():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-opus-5")

    events = [_message_start(), _refusal_delta(category="cyber")]
    streamed, exc = _run_stream_expecting_refusal(provider, session, events)

    assert streamed == []
    assert exc.category == "cyber"
    assert "Claude Opus 5" in str(exc)
    assert "cyber" in str(exc)
    # Fallbacks are configured for opus-5, so the message says the chain
    # declined too.
    assert "fallback" in str(exc).lower()
    # Nothing replayable was produced -- no assistant turn persisted.
    assert session.messages == [{"role": "user", "content": "hi"}]


def test_mid_stream_refusal_persists_partial_text_then_raises():
    provider = AnthropicProvider()
    session = _make_session(provider, "claude-sonnet-5")

    events = [
        _message_start(),
        *_text_block("partial answer"),
        _refusal_delta(category=None, explanation="Declined."),
    ]
    streamed, exc = _run_stream_expecting_refusal(provider, session, events)

    assert [e.text for e in streamed if e.type == "text"] == ["partial answer"]
    assert exc.category is None
    assert "Claude Sonnet 5" in str(exc)
    # No fallbacks configured for sonnet-5 -- no chain claim in the message.
    assert "fallback" not in str(exc).lower()
    assistant = session.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [{"type": "text", "text": "partial answer"}]
