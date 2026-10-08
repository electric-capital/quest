"""Tests for nested (2nd-level) sub-agent gating (chat/gemini_api/sub_agent.py).

The per-conversation ``nested_subagents`` flag lets a 1st-level sub-agent spawn
ONE tier of 2nd-level sub-agents via ``agent_task_nested``. 2nd-level sub-agents
are restricted to NESTED_SUB_AGENT_ALLOWED_MODELS (Claude Haiku + Gemini Flash
Lite) and cannot spawn any further.

These tests mirror tests/test_sub_agent_model_denial.py: async coroutines are
driven via ``asyncio.run`` inside synchronous tests (no pytest-asyncio plugin
required) and everything uses mocks (no DB, no LLM calls). They exercise:

- the NESTED_SUB_AGENT_ALLOWED_MODELS constant,
- the tool tier chosen for the session (SUB_AGENT_TOOLS_NESTED only for a
  1st-level sub-agent with the flag on; SUB_AGENT_TOOLS otherwise and always at
  level 2),
- the defense-in-depth level-2 model guard inside _run_sub_agent(),
- the nested-spawn dispatch branch's model allow-list enforcement.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.constants import NESTED_SUB_AGENT_ALLOWED_MODELS
from chat.gemini_api.sub_agent import _run_sub_agent
from chat.llm.tool_schemas import SUB_AGENT_TOOLS, SUB_AGENT_TOOLS_NESTED


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def user():
    return {
        "id": 1,
        "email": "test@example.com",
        "name": "Test User",
        "api_key": "test-key",
        "settings": {},
    }


def _make_provider(function_calls_per_turn=None):
    """Build a mock LLMProvider whose session finishes after one empty turn.

    With no function calls, _run_sub_agent's loop breaks immediately after the
    first stream, so the session is created (capturing the tools tier) and the
    runner returns a fallback string without any further work.
    """
    provider = MagicMock()
    provider.create_session.return_value = MagicMock()

    async def _empty_stream(session, message):
        # No events -> no text, no function calls -> loop breaks.
        if False:
            yield None  # pragma: no cover - makes this an async generator

    provider.send_message_stream.side_effect = _empty_stream

    usage = MagicMock()
    usage.input_tokens = 0
    usage.output_tokens = 0
    usage.cached_tokens = 0
    usage.cache_creation_tokens = 0
    usage.cache_read_tokens = 0
    provider.get_usage.return_value = usage
    return provider


def _tools_passed_to_create_session(provider):
    """Return the ``tools`` kwarg passed to provider.create_session."""
    assert provider.create_session.called, "create_session was not called"
    return provider.create_session.call_args.kwargs["tools"]


# Patch out the no-op-but-DB-touching helpers _run_sub_agent calls so the tests
# stay pure (no DB / network). record_api_call + get_user_connected_services are
# the only external touchpoints on the empty-turn path.
def _run(coro_factory):
    with patch(
        "chat.gemini_api.sub_agent.record_api_call", new=AsyncMock()
    ), patch(
        "api.instructions.get_user_connected_services", return_value={}
    ), patch(
        "chat.gemini_api.sub_agent.compute_new_input_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent.compute_total_context_tokens", return_value=0
    ):
        return asyncio.run(coro_factory())


# ---------------------------------------------------------------------------
# Constant sanity
# ---------------------------------------------------------------------------

class TestNestedAllowedConstant:
    def test_haiku_and_flash_lite_allowed(self):
        assert "claude-haiku-4.5" in NESTED_SUB_AGENT_ALLOWED_MODELS
        assert "claude-haiku-5-5" in NESTED_SUB_AGENT_ALLOWED_MODELS
        assert "gemini-3.5-flash-lite" in NESTED_SUB_AGENT_ALLOWED_MODELS

    def test_tool_schema_copies_match_constants(self):
        # tool_schemas.py duplicates both sets to dodge a circular import.
        from chat.gemini_api import constants
        from chat.llm import tool_schemas

        assert tool_schemas.NESTED_SUB_AGENT_ALLOWED_MODELS == NESTED_SUB_AGENT_ALLOWED_MODELS
        assert tool_schemas.TEMPLATE_BATCH_ALLOWED_MODELS == constants.TEMPLATE_BATCH_ALLOWED_MODELS

    def test_pro_and_sonnet_not_allowed(self):
        assert "gemini-3.1-pro-preview" not in NESTED_SUB_AGENT_ALLOWED_MODELS
        assert "claude-sonnet-4-6" not in NESTED_SUB_AGENT_ALLOWED_MODELS
        assert "gemini-3-flash-preview" not in NESTED_SUB_AGENT_ALLOWED_MODELS


# ---------------------------------------------------------------------------
# Tool tier selection
# ---------------------------------------------------------------------------

class TestToolTierSelection:
    def test_first_level_with_flag_gets_nested_tier(self, user):
        provider = _make_provider()
        _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="claude-sonnet-4-6",
            agent_name="Researcher",
            prompt="do the thing",
            nested_enabled=True,
            level=1,
        ))
        assert _tools_passed_to_create_session(provider) is SUB_AGENT_TOOLS_NESTED

    def test_first_level_without_flag_gets_plain_tier(self, user):
        provider = _make_provider()
        _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="claude-sonnet-4-6",
            agent_name="Researcher",
            prompt="do the thing",
            nested_enabled=False,
            level=1,
        ))
        assert _tools_passed_to_create_session(provider) is SUB_AGENT_TOOLS

    def test_second_level_never_gets_nested_tier(self, user):
        # Even with nested_enabled=True, a level-2 agent is a leaf: no spawner.
        provider = _make_provider()
        _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="claude-haiku-4.5",
            agent_name="Leaf",
            prompt="count things",
            nested_enabled=True,
            level=2,
        ))
        assert _tools_passed_to_create_session(provider) is SUB_AGENT_TOOLS


# ---------------------------------------------------------------------------
# Defense-in-depth level-2 model guard
# ---------------------------------------------------------------------------

class TestLevel2ModelGuard:
    # Models not in the 2nd-level allow-list. gemini-3.1-pro-preview is omitted
    # here: it is in SUB_AGENT_DISALLOWED_MODELS so it is rejected one guard
    # earlier (covered by test_pro_rejected_at_level2 below).
    @pytest.mark.parametrize("bad_model", [
        "gemini-3-flash-preview",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
    ])
    def test_disallowed_level2_model_returns_early(self, user, bad_model):
        """_run_sub_agent(level=2, model=<not in nested allow-list>) must refuse
        before creating a session via the level-2 guard."""
        provider = MagicMock()
        provider.create_session.side_effect = AssertionError(
            "create_session must not be called for a disallowed 2nd-level model"
        )
        on_event = AsyncMock()

        result = _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model=bad_model,
            agent_name="Leaf",
            prompt="do the thing",
            on_event=on_event,
            parent_tool_id="tool-1",
            level=2,
        ))

        provider.create_session.assert_not_called()
        assert bad_model in result
        assert "2nd-level" in result

        # A sub_agent_finished(error) event resolves the UI badge.
        assert on_event.await_count == 1
        payload = on_event.await_args.args[0]
        assert payload["type"] == "sub_agent_finished"
        assert payload["status"] == "error"
        assert payload["parent_tool_id"] == "tool-1"

    def test_pro_rejected_at_level2(self, user):
        """gemini-3.1-pro-preview is rejected at level 2 too (by the broader
        SUB_AGENT_DISALLOWED_MODELS guard, which fires before the level-2 one),
        without creating a session."""
        provider = MagicMock()
        provider.create_session.side_effect = AssertionError(
            "create_session must not be called for Pro at level 2"
        )

        result = _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="gemini-3.1-pro-preview",
            agent_name="Leaf",
            prompt="do the thing",
            level=2,
        ))

        provider.create_session.assert_not_called()
        assert "gemini-3.1-pro-preview" in result
        assert "not permitted" in result.lower()

    @pytest.mark.parametrize("good_model", sorted(NESTED_SUB_AGENT_ALLOWED_MODELS))
    def test_allowed_level2_model_creates_session(self, user, good_model):
        provider = _make_provider()
        _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model=good_model,
            agent_name="Leaf",
            prompt="count things",
            level=2,
        ))
        provider.create_session.assert_called_once()


# ---------------------------------------------------------------------------
# Nested-spawn dispatch branch (1st-level agent calling agent_task_nested)
# ---------------------------------------------------------------------------

def _make_provider_with_nested_call(nested_model):
    """Mock provider where the 1st-level agent emits one agent_task_nested call
    on its first turn, then finishes (no further calls) on the next turn."""
    provider = MagicMock()
    provider.create_session.return_value = MagicMock()

    fc = MagicMock()
    fc.type = "tool_call"
    fc.tool_name = "agent_task_nested"
    fc.tool_id = "fc-1"
    fc.tool_args_error = ""  # a bare MagicMock attribute would read as an error
    fc.tool_args = {
        "name": "Leaf",
        "prompt": "count",
        "description": "counting",
        "model": nested_model,
    }

    turns = {"n": 0}

    async def _stream(session, message):
        turns["n"] += 1
        if turns["n"] == 1:
            yield fc
        # Subsequent turns: no events -> loop breaks.

    provider.send_message_stream.side_effect = _stream

    usage = MagicMock()
    usage.input_tokens = 0
    usage.output_tokens = 0
    usage.cached_tokens = 0
    usage.cache_creation_tokens = 0
    usage.cache_read_tokens = 0
    provider.get_usage.return_value = usage
    provider.format_tool_results.return_value = "formatted-results"
    provider.inject_turn_warning.return_value = None
    return provider


class TestNestedSpawnDispatch:
    def test_disallowed_nested_model_rejected_without_recursion(self, user):
        """A 1st-level agent that requests a non-allowed nested model gets a
        clean error tool result; _run_sub_agent is NOT re-entered for level 2."""
        provider = _make_provider_with_nested_call("gemini-3-flash-preview")

        # Capture tool results fed back to format_tool_results so we can assert
        # the nested call produced an error result.
        captured = {}

        def _capture_format(session, tool_results):
            captured["results"] = tool_results
            return "formatted-results"

        provider.format_tool_results.side_effect = _capture_format

        # If the rejection leaked into a real recursive spawn, get_provider_*
        # would be hit; we assert the nested branch short-circuits instead.
        _run(lambda: _run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="claude-sonnet-4-6",
            agent_name="Parent",
            prompt="delegate",
            nested_enabled=True,
            level=1,
        ))

        results = captured["results"]
        assert len(results) == 1
        nested_payload = json.loads(results[0]["result"])
        assert "error" in nested_payload
        assert "gemini-3-flash-preview" in nested_payload["error"]
        assert "2nd-level" in nested_payload["error"]

    def test_allowed_nested_model_spawns_level2(self, user):
        """An allowed nested model recurses into _run_sub_agent with level=2 and
        the same flag, and the result is fed back as the tool result."""
        provider = _make_provider_with_nested_call("claude-haiku-4.5")

        captured = {}

        def _capture_format(session, tool_results):
            captured["results"] = tool_results
            return "formatted-results"

        provider.format_tool_results.side_effect = _capture_format

        recorded_calls = []

        # Capture the real function object BEFORE patching the module name. The
        # outer call invokes the real function directly; the recursive level-2
        # call inside it resolves the (now-patched) module global and hits the
        # fake, so we can assert on how the nested spawn was invoked.
        import chat.gemini_api.sub_agent as sub_agent_mod
        real_run = sub_agent_mod._run_sub_agent

        async def fake_inner(*args, **kwargs):
            recorded_calls.append(kwargs)
            return "leaf result"

        with patch(
            "chat.gemini_api.sub_agent.get_provider_for_model", return_value="anthropic"
        ), patch(
            "chat.gemini_api.sub_agent.get_provider_instance", return_value=MagicMock()
        ), patch(
            "chat.gemini_api.sub_agent._run_sub_agent", side_effect=fake_inner
        ):
            _run(lambda: real_run(
                app=MagicMock(),
                provider=provider,
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                model="claude-sonnet-4-6",
                agent_name="Parent",
                prompt="delegate",
                nested_enabled=True,
                level=1,
            ))

        assert len(recorded_calls) == 1
        inner_kwargs = recorded_calls[0]
        assert inner_kwargs["level"] == 2
        assert inner_kwargs["model"] == "claude-haiku-4.5"
        assert inner_kwargs["nested_enabled"] is True
        # The level-2 spawn carries a nested_parent_id (the nested-agent node id)
        # so the grandchild's sub_agent_* events can be attributed to the node.
        assert inner_kwargs.get("nested_parent_id")

        results = captured["results"]
        assert len(results) == 1
        assert results[0]["result"] == "leaf result"


# ---------------------------------------------------------------------------
# Nested-agent NODE event surfacing (UI tree affordance)
# ---------------------------------------------------------------------------

class TestNestedAgentNodeEvents:
    """The nested-spawn branch emits a sub_agent_tool_use NODE event (carrying
    nested_agent_id / nested_agent_name / nested_agent_model) and a matching
    sub_agent_tool_result NODE event (carrying nested_agent_status), both keyed
    by the 1st-level agent's parent_tool_id, so the FE can render the nested
    agent as a child node with its grandchild's tool calls nested under it. The
    level-2 spawn is given a nested_parent_id == nested_agent_id."""

    def _run_with_capture(self, user, nested_model):
        provider = _make_provider_with_nested_call(nested_model)
        provider.format_tool_results.side_effect = lambda s, r: "formatted-results"

        events = []

        async def on_event(ev):
            events.append(ev)

        import chat.gemini_api.sub_agent as sub_agent_mod
        real_run = sub_agent_mod._run_sub_agent

        recorded_calls = []

        async def fake_inner(*args, **kwargs):
            recorded_calls.append(kwargs)
            return "leaf result"

        with patch(
            "chat.gemini_api.sub_agent.get_provider_for_model", return_value="anthropic"
        ), patch(
            "chat.gemini_api.sub_agent.get_provider_instance", return_value=MagicMock()
        ), patch(
            "chat.gemini_api.sub_agent._run_sub_agent", side_effect=fake_inner
        ):
            _run(lambda: real_run(
                app=MagicMock(),
                provider=provider,
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                model="claude-sonnet-4-6",
                agent_name="Parent",
                prompt="delegate",
                on_event=on_event,
                parent_tool_id="parent-tool-1",
                nested_enabled=True,
                level=1,
            ))
        return events, recorded_calls

    def test_node_use_and_result_events_emitted(self, user):
        events, recorded_calls = self._run_with_capture(user, "claude-haiku-4.5")

        node_uses = [
            e for e in events
            if e["type"] == "sub_agent_tool_use" and e.get("nested_agent_id")
        ]
        node_results = [
            e for e in events
            if e["type"] == "sub_agent_tool_result" and e.get("nested_agent_id")
        ]
        assert len(node_uses) == 1
        assert len(node_results) == 1

        node = node_uses[0]
        assert node["parent_tool_id"] == "parent-tool-1"
        assert node["agent_name"] == "Parent"
        assert node["nested_agent_name"] == "Leaf"
        assert node["nested_agent_model"] == "claude-haiku-4.5"

        result = node_results[0]
        assert result["parent_tool_id"] == "parent-tool-1"
        assert result["nested_agent_id"] == node["nested_agent_id"]
        assert result["nested_agent_status"] == "success"
        # The node result carries the nested agent's response in tool_output so
        # the FE can render it as the node's "Output" section (mirrors a normal
        # sub_agent_tool_result populating toolResult.tool_output).
        assert result["tool_output"] == "leaf result"

        # The level-2 spawn was given the node id as its nested_parent_id.
        assert recorded_calls[0]["nested_parent_id"] == node["nested_agent_id"]

    def test_node_result_errored_on_disallowed_model(self, user):
        # A rejected nested model still emits the node (use + result), with the
        # result flagged errored so the UI node badge resolves to ERRORED. The
        # level-2 spawn never happens.
        events, recorded_calls = self._run_with_capture(user, "gemini-3-flash-preview")
        assert recorded_calls == []

        node_results = [
            e for e in events
            if e["type"] == "sub_agent_tool_result" and e.get("nested_agent_id")
        ]
        assert len(node_results) == 1
        assert node_results[0]["nested_agent_status"] == "error"
