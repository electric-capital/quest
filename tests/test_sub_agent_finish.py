"""Sub-agent loop: agent_task_response ends the sub-agent at once.

The response is returned without answering the call and without any further
model request (the ephemeral session is discarded on return). Calls earlier
in the same response still run; calls after it are skipped.

Mirrors the pure-mock harness in tests/test_sub_agent_malformed_arguments.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.sub_agent import _run_sub_agent
from chat.llm.base import StreamEvent, UsageStats


@pytest.fixture()
def user():
    return {
        "id": 1,
        "email": "test@example.com",
        "name": "Test User",
        "api_key": "test-key",
        "settings": {},
    }


def _finish(response="final answer", tool_id="call_done"):
    return StreamEvent(
        type="tool_call", tool_name="agent_task_response",
        tool_args={"response": response}, tool_id=tool_id,
    )


def _tool(name, tool_id):
    return StreamEvent(type="tool_call", tool_name=name, tool_args={}, tool_id=tool_id)


def _make_provider(turns):
    """Mock provider whose n-th stream yields ``turns[n]`` (empty after)."""
    provider = MagicMock()
    provider.create_session.return_value = MagicMock()
    streams: list = []

    async def _stream(session, message):
        streams.append(message)
        index = len(streams) - 1
        for event in turns[index] if index < len(turns) else []:
            yield event

    provider.send_message_stream.side_effect = _stream
    provider.get_usage.return_value = UsageStats()
    provider.streams = streams
    return provider


def _run_agent(provider, user, on_event=None):
    dispatched = AsyncMock(return_value=("tool output", []))
    recorded = AsyncMock()
    with patch(
        "chat.gemini_api.sub_agent.record_api_call", new=recorded
    ), patch(
        "chat.gemini_api.sub_agent.record_tool_call"
    ), patch(
        "api.instructions.get_user_connected_services", return_value={}
    ), patch(
        "chat.gemini_api.sub_agent.compute_new_input_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent.compute_total_context_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent._dispatch_tool_call", new=dispatched
    ):
        result = asyncio.run(_run_sub_agent(
            app=MagicMock(),
            provider=provider,
            user=user,
            conversation_id="conv-1",
            timezone="UTC",
            model="claude-sonnet-4-6",
            agent_name="Researcher",
            prompt="do the thing",
            on_event=on_event,
            parent_tool_id="tool-1" if on_event else None,
        ))
    return result, dispatched, recorded


def test_first_turn_finish_makes_no_further_request(user):
    provider = _make_provider([[_finish()]])
    on_event = AsyncMock()

    result, dispatched, recorded = _run_agent(provider, user, on_event)

    assert result == "final answer"
    assert len(provider.streams) == 1
    provider.format_tool_results.assert_not_called()
    dispatched.assert_not_called()
    # One model call, one usage row.
    assert recorded.await_count == 1
    finished = [
        call.args[0] for call in on_event.await_args_list
        if call.args[0]["type"] == "sub_agent_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["status"] == "success"


def test_finish_after_tool_turns_stops_without_answering(user):
    provider = _make_provider([
        [_tool("get_current_time", "call_1")],
        [_finish()],
    ])

    result, dispatched, recorded = _run_agent(provider, user)

    assert result == "final answer"
    # Turn 1's tool result is sent back; the finishing turn is not answered.
    assert len(provider.streams) == 2
    assert provider.format_tool_results.call_count == 1
    assert dispatched.await_count == 1
    assert recorded.await_count == 2


def test_calls_before_finish_run_and_calls_after_are_skipped(user):
    provider = _make_provider([[
        _tool("get_current_time", "call_1"),
        _finish(),
        _tool("list_workspace_files", "call_2"),
    ]])

    result, dispatched, _ = _run_agent(provider, user)

    assert result == "final answer"
    assert len(provider.streams) == 1
    assert [call.args[5] for call in dispatched.await_args_list] == ["get_current_time"]
