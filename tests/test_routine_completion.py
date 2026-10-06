"""Routine runs end with a ``routine_completed`` call -- or get one nudge.

Weaker models sometimes end their stream before a routine's work is done.
Routine conversations therefore expose the ``routine_completed`` tool and
the system prompt tells the model to call it last; the shared driver in
chat/routine_runs.py (used by the scheduler and the one-click first message
over the WebSocket) re-runs ONE follow-up turn when a run ends without the
call, and leaves suspended runs (dangling tool_use awaiting a wait-handle
resume) alone.
"""

import asyncio
import json

import pytest

from chat.gemini_api.run_context import RunContext
from chat.gemini_api.turn_tools import (
    ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY,
    ToolCall,
    TurnState,
    _handle_routine_completed,
)
from chat.gemini_api.usage import UsageAccumulator
from chat.llm.tool_schemas import (
    INFERENCE_API_TOOLS,
    PUBLIC_ROUTINE_TOOLS,
    PUBLIC_TOOLS,
    ROUTINE_TOP_LEVEL_TOOLS,
    SLACK_TOP_LEVEL_TOOLS,
    SUB_AGENT_TOOLS,
    TOP_LEVEL_TOOLS,
    USER_SUBAGENT_TOOLS,
)
from chat.routine_runs import (
    MAX_ROUTINE_COMPLETION_NUDGES,
    ROUTINE_COMPLETION_NUDGE,
    ROUTINE_NUDGE_MESSAGE_TYPE,
    drive_routine_run,
    routine_completed_called,
    run_ended_suspended,
)


def _run(coro):
    return asyncio.run(coro)


def _names(tier):
    return [t["name"] for t in tier]


# ---------------------------------------------------------------------------
# Tool tiers
# ---------------------------------------------------------------------------


class TestRoutineToolTiers:
    def test_routine_tiers_add_only_the_completion_tool(self):
        assert _names(ROUTINE_TOP_LEVEL_TOOLS) == _names(TOP_LEVEL_TOOLS) + ["routine_completed"]
        assert _names(PUBLIC_ROUTINE_TOOLS) == _names(PUBLIC_TOOLS) + ["routine_completed"]

    @pytest.mark.parametrize("tier", [
        TOP_LEVEL_TOOLS, SLACK_TOP_LEVEL_TOOLS, SUB_AGENT_TOOLS,
        USER_SUBAGENT_TOOLS, INFERENCE_API_TOOLS, PUBLIC_TOOLS,
    ])
    def test_non_routine_tiers_do_not_advertise_it(self, tier):
        assert "routine_completed" not in _names(tier)

    def test_summary_is_optional(self):
        spec = next(t for t in ROUTINE_TOP_LEVEL_TOOLS if t["name"] == "routine_completed")
        assert spec["parameters"]["required"] == []
        assert "summary" in spec["parameters"]["properties"]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


class TestRoutinePrompt:
    def test_routine_prompt_instructs_completion_call(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt("key", has_project=True, is_routine=True)
        assert "Routine completion (IMPORTANT)" in prompt
        assert "`routine_completed`" in prompt
        assert "treated as unfinished" in prompt

    def test_regular_prompt_never_mentions_it(self):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt("key", has_project=True)
        assert "routine_completed" not in prompt

    def test_public_routine_prompt_instructs_completion_call(self):
        from chat.gemini_api.system_prompt import get_public_project_system_prompt
        routine = get_public_project_system_prompt(is_routine=True)
        plain = get_public_project_system_prompt(is_routine=False)
        assert "`routine_completed`" in routine
        assert "routine_completed" not in plain


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _ctx(**overrides):
    async def _noop(_event):
        pass

    fields = dict(
        app=None,
        user={"id": 1, "email": "t@example.com"},
        conversation_id="c1",
        timezone="UTC",
        model="test-model",
        origin="web",
        project_id="p1",
        routine_id="r1",
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
        on_event=_noop,
        structured_messages=[],
        usage_acc=UsageAccumulator(),
    )
    fields.update(overrides)
    return RunContext(**fields)


def _call(args=None, *, via_tool_call=False):
    if via_tool_call:
        return ToolCall(
            name="tool_call", key=ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY,
            tool_id="tid-1", raw_tool_id="raw-1",
            args={"tool_name": "routine_completed", "arguments": args or {}},
        )
    return ToolCall(
        name="routine_completed", key="routine_completed",
        tool_id="tid-1", raw_tool_id="raw-1", args=args or {},
    )


class TestRoutineCompletedHandler:
    def test_requires_routine_conversation(self):
        result = json.loads(_run(_handle_routine_completed(
            _ctx(routine_id=None), _call(), TurnState(),
        )))
        assert "only available inside routine conversations" in result["error"]

    def test_acknowledges_with_summary(self):
        result = json.loads(_run(_handle_routine_completed(
            _ctx(), _call({"summary": "  Posted the digest.  "}), TurnState(),
        )))
        assert result == {"status": "completed", "summary": "Posted the digest."}

    def test_summary_optional_and_public_allowed(self):
        result = json.loads(_run(_handle_routine_completed(
            _ctx(is_public=True), _call(), TurnState(),
        )))
        assert result == {"status": "completed"}

    def test_tool_call_spelling_unwraps_arguments(self):
        # Live runs showed Gemini Flash-Lite calling
        # tool_call(tool_name="routine_completed", arguments={...}); the
        # same arm serves it and reads the summary from ``arguments``.
        result = json.loads(_run(_handle_routine_completed(
            _ctx(), _call({"summary": "Done."}, via_tool_call=True), TurnState(),
        )))
        assert result == {"status": "completed", "summary": "Done."}
        result = json.loads(_run(_handle_routine_completed(
            _ctx(routine_id=None), _call(via_tool_call=True), TurnState(),
        )))
        assert "only available inside routine conversations" in result["error"]


# ---------------------------------------------------------------------------
# Transcript predicates
# ---------------------------------------------------------------------------


def _tool_use(name, tool_id):
    return {"type": "tool_use", "tool_name": name, "tool_input": {}, "tool_id": tool_id}


def _tool_result(tool_id):
    return {"type": "tool_result", "tool_id": tool_id, "tool_output": "{}"}


class TestPredicates:
    def test_completed_detects_the_tool_use(self):
        assert not routine_completed_called([])
        assert not routine_completed_called([_tool_use("tool_call", "a"), _tool_result("a")])
        assert routine_completed_called([
            {"role": "assistant", "content": "done"},
            _tool_use("routine_completed", "b"),
            _tool_result("b"),
        ])

    def test_completed_detects_the_tool_call_spelling(self):
        via_router = {
            "type": "tool_use", "tool_name": "tool_call", "tool_id": "c",
            "tool_input": {"tool_name": "routine_completed", "arguments": {}},
        }
        assert routine_completed_called([via_router, _tool_result("c")])
        other = {
            "type": "tool_use", "tool_name": "tool_call", "tool_id": "d",
            "tool_input": {"tool_name": "get_current_time", "arguments": {}},
        }
        assert not routine_completed_called([other, _tool_result("d")])

    def test_suspended_means_a_dangling_tool_use(self):
        closed = [_tool_use("tool_call", "a"), _tool_result("a"), {"type": "stats", "stats": {}}]
        assert not run_ended_suspended(closed)
        dangling = closed + [
            _tool_use("create_action_request", "b"),
            {"type": "action_request", "request_id": 7},
        ]
        assert run_ended_suspended(dangling)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class _FakeRun:
    """Scripted run_conversation_turn: each call appends its canned messages."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.messages: list[dict] = []
        self.inputs: list[str] = []
        self.events: list[dict] = []

    async def run_turn(self, message: str) -> None:
        self.inputs.append(message)
        self.messages.extend(self.scripts.pop(0))

    async def on_event(self, event: dict) -> None:
        self.events.append(event)

    def drive(self, prompt="Do the routine"):
        return _run(drive_routine_run(
            self.run_turn,
            prompt=prompt,
            messages_out=self.messages,
            on_event=self.on_event,
            conversation_id="c1",
            log_prefix="[test]",
        ))


_COMPLETED_TURN = [
    {"role": "assistant", "content": "All done."},
    _tool_use("routine_completed", "done-1"),
    _tool_result("done-1"),
    {"type": "stats", "stats": {}},
]
_UNFINISHED_TURN = [
    _tool_use("tool_call", "t-1"),
    _tool_result("t-1"),
    {"role": "assistant", "content": "I will now"},
    {"type": "stats", "stats": {}},
]
_SUSPENDED_TURN = [
    _tool_use("create_action_request", "car-1"),
    {"type": "action_request", "request_id": 1},
]


class TestDriveRoutineRun:
    def test_completed_first_turn_runs_once(self):
        fake = _FakeRun(_COMPLETED_TURN)
        assert fake.drive() is True
        assert fake.inputs == ["Do the routine"]
        assert fake.events == []
        assert not any(m.get("type") == ROUTINE_NUDGE_MESSAGE_TYPE for m in fake.messages)

    def test_unfinished_run_gets_one_nudge_then_completes(self):
        fake = _FakeRun(_UNFINISHED_TURN, _COMPLETED_TURN)
        assert fake.drive() is True
        assert fake.inputs == ["Do the routine", ROUTINE_COMPLETION_NUDGE]
        # The notice sits between the two turns' messages, in order, and
        # was emitted through on_event so incremental flushers persist it.
        notice_idx = [
            i for i, m in enumerate(fake.messages)
            if m.get("type") == ROUTINE_NUDGE_MESSAGE_TYPE
        ]
        assert notice_idx == [len(_UNFINISHED_TURN)]
        notice = fake.messages[notice_idx[0]]
        assert notice["role"] == "system"
        assert notice["content"] == ROUTINE_COMPLETION_NUDGE
        assert notice["attempt"] == 1
        assert "timestamp" in notice
        assert len(fake.events) == 1
        assert fake.events[0]["type"] == ROUTINE_NUDGE_MESSAGE_TYPE
        assert fake.events[0]["conversation_id"] == "c1"

    def test_nudges_are_capped(self):
        fake = _FakeRun(*([_UNFINISHED_TURN] * (MAX_ROUTINE_COMPLETION_NUDGES + 1)))
        assert fake.drive() is False
        assert len(fake.inputs) == MAX_ROUTINE_COMPLETION_NUDGES + 1
        assert fake.inputs[1:] == [ROUTINE_COMPLETION_NUDGE] * MAX_ROUTINE_COMPLETION_NUDGES
        assert fake.scripts == []

    def test_suspended_run_is_not_nudged(self):
        fake = _FakeRun(_SUSPENDED_TURN)
        assert fake.drive() is False
        assert fake.inputs == ["Do the routine"]
        assert fake.events == []

    def test_completion_in_an_earlier_turn_is_not_reused(self):
        # Only the messages the CURRENT run produced count: a stale
        # routine_completed from before this driver started (e.g. the
        # caller's pre-seeded list) must not mask an unfinished run.
        fake = _FakeRun(_UNFINISHED_TURN, _COMPLETED_TURN)
        fake.messages.extend(_COMPLETED_TURN)
        assert fake.drive() is True
        assert fake.inputs == ["Do the routine", ROUTINE_COMPLETION_NUDGE]

    def test_run_errors_propagate(self):
        class Boom(RuntimeError):
            pass

        async def run_turn(_message):
            raise Boom("provider down")

        async def on_event(_event):
            pass

        with pytest.raises(Boom):
            _run(drive_routine_run(
                run_turn, prompt="x", messages_out=[], on_event=on_event,
                conversation_id="c1", log_prefix="[test]",
            ))


def test_routine_nudge_is_a_flush_boundary():
    from chat._flush_helper import FLUSH_EVENT_TYPES
    assert ROUTINE_NUDGE_MESSAGE_TYPE in FLUSH_EVENT_TYPES
