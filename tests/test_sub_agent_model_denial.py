"""Tests for sub-agent model denial (Gemini 3.1 Pro disallowed for sub-agents).

Sub-agents spawned via agent_task / agent_task_parallel must not use
gemini-3.1-pro-preview (or its deprecated alias gemini-3-pro-preview, which is
remapped to it), whether chosen explicitly or inherited from the parent model.
Top-level conversations are unaffected.

These tests exercise:
- the SUB_AGENT_DISALLOWED_MODELS constant,
- the defense-in-depth guard inside _run_sub_agent(),
- the per-task rejection in _run_parallel_sub_agents(), including the
  parent-model inheritance case and the deprecated-alias remap.

The async coroutines are driven via asyncio.run() inside synchronous tests so
no pytest-asyncio plugin is required. All tests use mocks (no DB, no LLM calls).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.constants import (
    SUB_AGENT_DISALLOWED_MODELS,
    TEMPLATE_BATCH_ALLOWED_MODELS,
)
from chat.gemini_api.sub_agent import _run_sub_agent, _run_parallel_sub_agents


# ---------------------------------------------------------------------------
# Fixtures
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


# ---------------------------------------------------------------------------
# Constant sanity
# ---------------------------------------------------------------------------

class TestDisallowedConstant:
    def test_pro_is_disallowed(self):
        assert "gemini-3.1-pro-preview" in SUB_AGENT_DISALLOWED_MODELS

    def test_pro_not_in_template_allowlist(self):
        assert "gemini-3.1-pro-preview" not in TEMPLATE_BATCH_ALLOWED_MODELS

    def test_template_allowlist_follows_price_not_family(self):
        # Hydrogen is the Flash-priced Gemini 4 model; Argon and Carbon
        # are frontier-priced and stay out of high-volume batches.
        assert "gemini-4-hydrogen" in TEMPLATE_BATCH_ALLOWED_MODELS
        assert "gemini-4-argon" not in TEMPLATE_BATCH_ALLOWED_MODELS
        assert "gemini-4-carbon" not in TEMPLATE_BATCH_ALLOWED_MODELS


# ---------------------------------------------------------------------------
# Defense-in-depth guard inside _run_sub_agent()
# ---------------------------------------------------------------------------

class TestRunSubAgentGuard:
    def test_guard_returns_early_without_session(self, user):
        """_run_sub_agent must refuse a disallowed model before creating a
        session, spending no tokens and emitting a finished(error) event."""
        provider = MagicMock()
        # create_session would record/raise if the guard failed to short-circuit.
        provider.create_session.side_effect = AssertionError(
            "create_session must not be called for a disallowed model"
        )
        on_event = AsyncMock()

        result = asyncio.run(_run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="gemini-3.1-pro-preview",
            agent_name="Researcher",
            prompt="do the thing",
            on_event=on_event,
            parent_tool_id="tool-1",
        ))

        provider.create_session.assert_not_called()
        assert "gemini-3.1-pro-preview" in result
        assert "not permitted" in result.lower()

        # A sub_agent_finished(error) event was emitted so the UI badge resolves.
        assert on_event.await_count == 1
        payload = on_event.await_args.args[0]
        assert payload["type"] == "sub_agent_finished"
        assert payload["status"] == "error"
        assert payload["parent_tool_id"] == "tool-1"

    def test_guard_no_event_without_parent_tool(self, user):
        """Without on_event/parent_tool_id the guard still returns an error
        string and never creates a session."""
        provider = MagicMock()
        provider.create_session.side_effect = AssertionError("must not be called")

        result = asyncio.run(_run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="gemini-3.1-pro-preview",
            agent_name="Researcher",
            prompt="do the thing",
        ))

        provider.create_session.assert_not_called()
        assert "not permitted" in result.lower()


# ---------------------------------------------------------------------------
# Parallel arm rejection
# ---------------------------------------------------------------------------

class TestParallelArmRejection:
    def test_pro_task_rejected_allowed_task_runs(self, user):
        """A Pro task is rejected with status=error while an allowed task runs.

        We patch _run_sub_agent so the allowed task returns a canned response and
        the Pro task would error loudly if it ever reached _run_sub_agent (it
        must be rejected before that by the per-task guard)."""

        async def fake_run_sub_agent(*args, **kwargs):
            assert kwargs["model"] != "gemini-3.1-pro-preview", (
                "disallowed model reached _run_sub_agent"
            )
            return "canned response"

        tasks = [
            {"id": "ok", "name": "Allowed", "prompt": "p", "model": "gemini-3.5-flash"},
            {"id": "bad", "name": "Pro", "prompt": "p", "model": "gemini-3.1-pro-preview"},
        ]

        with patch(
            "chat.gemini_api.sub_agent._run_sub_agent",
            side_effect=fake_run_sub_agent,
        ), patch(
            "chat.gemini_api.sub_agent.get_provider_for_model", return_value="gemini"
        ), patch(
            "chat.gemini_api.sub_agent.get_provider_instance", return_value=MagicMock()
        ):
            raw = asyncio.run(_run_parallel_sub_agents(
                app=MagicMock(),
                provider=MagicMock(),
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                parent_model="gemini-3.5-flash",
                tasks=tasks,
            ))

        results = json.loads(raw)["results"]
        assert results["ok"]["status"] == "success"
        assert results["ok"]["response"] == "canned response"
        assert results["bad"]["status"] == "error"
        assert "gemini-3.1-pro-preview" in results["bad"]["error"]

    def test_inherited_pro_from_parent_rejected(self, user):
        """A task with no explicit model inherits parent_model; if that is Pro,
        the task is rejected."""
        tasks = [{"id": "t1", "name": "Inherited", "prompt": "p"}]

        with patch(
            "chat.gemini_api.sub_agent._run_sub_agent",
            side_effect=AssertionError("must not spawn for inherited Pro"),
        ):
            raw = asyncio.run(_run_parallel_sub_agents(
                app=MagicMock(),
                provider=MagicMock(),
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                parent_model="gemini-3.1-pro-preview",
                tasks=tasks,
            ))

        results = json.loads(raw)["results"]
        assert results["t1"]["status"] == "error"
        assert "gemini-3.1-pro-preview" in results["t1"]["error"]

    def test_deprecated_alias_remapped_then_rejected(self, user):
        """A task requesting the deprecated alias gemini-3-pro-preview is
        remapped to gemini-3.1-pro-preview and then rejected."""
        tasks = [{"id": "t1", "name": "Alias", "prompt": "p", "model": "gemini-3-pro-preview"}]

        with patch(
            "chat.gemini_api.sub_agent._run_sub_agent",
            side_effect=AssertionError("must not spawn for deprecated Pro alias"),
        ):
            raw = asyncio.run(_run_parallel_sub_agents(
                app=MagicMock(),
                provider=MagicMock(),
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                parent_model="gemini-3.5-flash",
                tasks=tasks,
            ))

        results = json.loads(raw)["results"]
        assert results["t1"]["status"] == "error"
        # The error names the canonical (remapped) ID.
        assert "gemini-3.1-pro-preview" in results["t1"]["error"]

    def test_rejected_task_emits_finished_event(self, user):
        """The per-task rejection emits sub_agent_finished(error) so the UI
        badge does not hang in RUNNING."""
        on_event = AsyncMock()
        tasks = [{"id": "t1", "name": "Pro", "prompt": "p", "model": "gemini-3.1-pro-preview"}]

        with patch(
            "chat.gemini_api.sub_agent._run_sub_agent",
            side_effect=AssertionError("must not spawn"),
        ):
            asyncio.run(_run_parallel_sub_agents(
                app=MagicMock(),
                provider=MagicMock(),
                user=user,
                conversation_id="conv-1",
                timezone="UTC",
                parent_model="gemini-3.5-flash",
                tasks=tasks,
                on_event=on_event,
                parent_tool_id="tool-1",
            ))

        assert on_event.await_count == 1
        payload = on_event.await_args.args[0]
        assert payload["type"] == "sub_agent_finished"
        assert payload["status"] == "error"
        assert payload["parent_tool_id"] == "tool-1"
