"""Tests for the per-tool handler registry in chat/gemini_api/turn_tools.py.

Covers the routing surface (registry keys, the tool_call-riding
wait_for_handles key, the public-project blocklist) and the handler-level
origin guards that run before any external side effect, plus the
persist-before-emit invariant made structural by ``_emit_durable``.
"""

import asyncio
import json

import pytest

from chat.gemini_api.constants import MAX_AGENT_TASKS_PER_TURN
from chat.gemini_api.run_context import RunContext
from chat.gemini_api.turn_tools import (
    _PUBLIC_BLOCKED_LOOP_TOOLS,
    ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY,
    TURN_TOOL_HANDLERS,
    WAIT_FOR_HANDLES_KEY,
    FinishInferenceResponse,
    ToolCall,
    TurnState,
    _emit_durable,
    _handle_agent_task,
    _handle_create_action_request,
    _handle_return_final_response,
    _handle_return_to_caller,
    _handle_send_slack_reply,
    public_blocked_result,
    registry_key_for,
)
from chat.gemini_api.usage import UsageAccumulator


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(**overrides):
    """Minimal RunContext; handlers under test only read a few fields."""
    events: list[dict] = []

    async def _record_event(event):
        events.append(event)

    fields = dict(
        app=None,
        user={"id": 1, "email": "t@example.com"},
        conversation_id="c1",
        timezone="UTC",
        model="test-model",
        origin="web",
        project_id=None,
        routine_id=None,
        slack_context=None,
        provider=None,
        provider_name="gemini",
        chat=None,
        is_slack_origin=False,
        is_user_subagent=False,
        is_inference_api=False,
        is_public=False,
        nested_subagents=False,
        user_subagents_enabled=False,
        user_subagents_gate_open=True,
        subagent_run=None,
        subagent_caller=None,
        custom_prompt="",
        resolved_project_guide="",
        resolved_skills_content="",
        on_event=_record_event,
        structured_messages=[],
        usage_acc=UsageAccumulator(),
    )
    fields.update(overrides)
    ctx = RunContext(**fields)
    return ctx, events


def _call(name, args=None, key=None, tool_id="tid-1", raw_tool_id="raw-1"):
    return ToolCall(
        name=name,
        key=key or name,
        tool_id=tool_id,
        raw_tool_id=raw_tool_id,
        args=args or {},
    )


# ---------------------------------------------------------------------------
# Routing surface
# ---------------------------------------------------------------------------


class TestRegistryRouting:
    def test_registry_covers_all_loop_handled_tools(self):
        assert set(TURN_TOOL_HANDLERS) == {
            "agent_task",
            "agent_task_parallel",
            "agent_task_parallel_template",
            "send_slack_reply_and_get_response",
            "return_to_caller",
            "return_final_response",
            "routine_completed",
            ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY,
            "create_action_request",
            WAIT_FOR_HANDLES_KEY,
        }

    def test_plain_names_key_to_themselves(self):
        assert registry_key_for("agent_task", {}) == "agent_task"
        assert registry_key_for("create_action_request", {"request_type": "x"}) \
            == "create_action_request"

    def test_wait_for_handles_rides_on_tool_call(self):
        key = registry_key_for(
            "tool_call", {"tool_name": "wait_for_handles", "arguments": {}},
        )
        assert key == WAIT_FOR_HANDLES_KEY
        assert key in TURN_TOOL_HANDLERS

    def test_other_tool_call_tools_fall_through(self):
        # Any other tool_call inner tool must NOT hit the registry -- it
        # belongs to the shared _dispatch_tool_call path.
        key = registry_key_for("tool_call", {"tool_name": "get_current_time"})
        assert key == "tool_call"
        assert key not in TURN_TOOL_HANDLERS

    def test_public_blocklist_covers_every_registry_key(self):
        # Public-project conversations must never reach a loop-handled
        # arm; if a new handler is registered it must be blocked too (or
        # this coupling consciously revisited). routine_completed is the
        # one deliberate exception: a pure completion signal with no reads
        # or writes, needed by routine runs in public projects.
        completion_keys = {"routine_completed", ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY}
        assert set(TURN_TOOL_HANDLERS) - completion_keys <= _PUBLIC_BLOCKED_LOOP_TOOLS
        assert not (completion_keys & _PUBLIC_BLOCKED_LOOP_TOOLS)

    def test_routine_completed_rides_on_tool_call_too(self):
        key = registry_key_for(
            "tool_call", {"tool_name": "routine_completed", "arguments": {}},
        )
        assert key == ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY
        assert TURN_TOOL_HANDLERS[key] is TURN_TOOL_HANDLERS["routine_completed"]

    def test_public_blocked_result_names_the_inner_tool(self):
        payload = json.loads(public_blocked_result(WAIT_FOR_HANDLES_KEY))
        assert "'wait_for_handles'" in payload["error"]
        payload = json.loads(public_blocked_result("agent_task"))
        assert "'agent_task'" in payload["error"]


# ---------------------------------------------------------------------------
# Handler-level guards (no external side effects)
# ---------------------------------------------------------------------------


class TestHandlerGuards:
    def test_agent_task_enforces_per_turn_cap(self):
        ctx, _ = _make_ctx()
        turn = TurnState(agent_task_calls=MAX_AGENT_TASKS_PER_TURN)
        result = _run(_handle_agent_task(
            ctx, _call("agent_task", {"name": "A", "prompt": "p"}), turn,
        ))
        payload = json.loads(result)
        assert "Too many agent_task calls" in payload["error"]
        # The rejected call still counted toward the per-turn total.
        assert turn.agent_task_calls == MAX_AGENT_TASKS_PER_TURN + 1

    def test_slack_reply_requires_text(self):
        ctx, _ = _make_ctx()
        result = _run(_handle_send_slack_reply(
            ctx, _call("send_slack_reply_and_get_response", {"text": "  "}),
            TurnState(),
        ))
        assert "error" in json.loads(result)

    def test_slack_reply_requires_slack_context(self):
        ctx, _ = _make_ctx(slack_context=None)
        result = _run(_handle_send_slack_reply(
            ctx, _call("send_slack_reply_and_get_response", {"text": "hi"}),
            TurnState(),
        ))
        assert "Slack-driven" in json.loads(result)["error"]

    def test_return_to_caller_requires_subagent_origin(self):
        ctx, _ = _make_ctx(is_user_subagent=False, subagent_run=None)
        result = _run(_handle_return_to_caller(
            ctx, _call("return_to_caller", {"response": "r"}), TurnState(),
        ))
        assert "cross-user subagent" in json.loads(result)["error"]

    def test_return_final_response_requires_inference_origin(self):
        ctx, _ = _make_ctx(is_inference_api=False)
        result = _run(_handle_return_final_response(
            ctx, _call("return_final_response", {"response": "answer"}),
            TurnState(),
        ))
        assert "inference API" in json.loads(result)["error"]

    def test_return_final_response_rejects_empty_response(self):
        ctx, _ = _make_ctx(is_inference_api=True)
        result = _run(_handle_return_final_response(
            ctx, _call("return_final_response", {"response": "   "}),
            TurnState(),
        ))
        assert "non-empty markdown" in json.loads(result)["error"]

    def test_return_final_response_closes_transcript_and_finishes(self):
        ctx, events = _make_ctx(is_inference_api=True)
        with pytest.raises(FinishInferenceResponse) as excinfo:
            _run(_handle_return_final_response(
                ctx,
                _call("return_final_response", {"response": "# Done"},
                      tool_id="tid-9"),
                TurnState(),
            ))
        assert excinfo.value.tool_id == "tid-9"
        # The closing tool_result was persisted (with role + timestamp)
        # and emitted (without them) BEFORE the sentinel raised.
        assert len(ctx.structured_messages) == 1
        durable = ctx.structured_messages[0]
        assert durable["type"] == "tool_result"
        assert durable["tool_id"] == "tid-9"
        assert durable["role"] == "assistant"
        assert "timestamp" in durable
        assert json.loads(durable["tool_output"]) == {"status": "delivered"}
        assert events == [{
            "type": "tool_result",
            "tool_id": "tid-9",
            "tool_output": durable["tool_output"],
        }]

    def test_create_action_request_blocked_in_slack_runs(self):
        ctx, _ = _make_ctx(is_slack_origin=True)
        result = _run(_handle_create_action_request(
            ctx, _call("create_action_request", {"request_type": "x"}),
            TurnState(),
        ))
        assert "Slack-driven" in json.loads(result)["error"]

    def test_create_action_request_blocked_in_subagent_runs(self):
        ctx, _ = _make_ctx(is_user_subagent=True)
        result = _run(_handle_create_action_request(
            ctx, _call("create_action_request", {"request_type": "x"}),
            TurnState(),
        ))
        assert "return_to_caller" in json.loads(result)["error"]

    def test_create_action_request_rejects_unknown_type(self):
        ctx, _ = _make_ctx()
        result = _run(_handle_create_action_request(
            ctx,
            _call("create_action_request", {"request_type": "not_a_type"}),
            TurnState(),
        ))
        payload = json.loads(result)
        assert "Unknown request type" in payload["error"]
        assert "subagent_return" not in payload["supported_types"]

    def test_create_action_request_never_mints_subagent_return(self):
        ctx, _ = _make_ctx()
        result = _run(_handle_create_action_request(
            ctx,
            _call("create_action_request", {"request_type": "subagent_return"}),
            TurnState(),
        ))
        assert "Unknown request type" in json.loads(result)["error"]

    def test_run_user_subagent_blocked_when_gate_closed(self):
        ctx, _ = _make_ctx(
            user_subagents_enabled=False, user_subagents_gate_open=False,
        )
        result = _run(_handle_create_action_request(
            ctx,
            _call("create_action_request",
                  {"request_type": "run_user_subagent"}),
            TurnState(),
        ))
        assert "disabled server-wide" in json.loads(result)["error"]

    def test_run_user_subagent_blocked_without_flag(self):
        ctx, _ = _make_ctx(
            user_subagents_enabled=False, user_subagents_gate_open=True,
        )
        result = _run(_handle_create_action_request(
            ctx,
            _call("create_action_request",
                  {"request_type": "run_user_subagent"}),
            TurnState(),
        ))
        assert "user_subagents" in json.loads(result)["error"]


# ---------------------------------------------------------------------------
# _emit_durable
# ---------------------------------------------------------------------------


class TestEmitDurable:
    def test_appends_before_emitting(self):
        """The durable copy must be visible to the on_event callback --
        that ordering is what lets flush-on-event persistence see the
        message it is flushing."""
        msgs: list[dict] = []
        seen_at_emit: list[int] = []

        async def _flushing_event(_event):
            seen_at_emit.append(len(msgs))

        ctx, _ = _make_ctx(
            structured_messages=msgs, on_event=_flushing_event,
        )
        _run(_emit_durable(ctx, {"type": "stats", "stats": {}}))
        assert seen_at_emit == [1]

    def test_durable_extra_only_on_persisted_copy(self):
        ctx, events = _make_ctx()
        _run(_emit_durable(
            ctx,
            {"type": "tool_result", "tool_id": "t", "tool_output": "{}"},
            durable_extra={"role": "assistant"},
        ))
        assert ctx.structured_messages[0]["role"] == "assistant"
        assert "timestamp" in ctx.structured_messages[0]
        assert events == [
            {"type": "tool_result", "tool_id": "t", "tool_output": "{}"},
        ]
