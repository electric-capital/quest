"""Sub-agent loop: a tool call with unparseable arguments is answered with
a structured error result instead of being dispatched (or, for
agent_task_response, terminating the agent with an empty response).

Mirrors the pure-mock harness in tests/test_nested_sub_agent.py.
"""

import asyncio
import json
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


def _make_provider(first_turn_events):
    """Mock provider: the first stream yields ``first_turn_events``, the
    second ends the agent with a well-formed agent_task_response."""
    provider = MagicMock()
    provider.create_session.return_value = MagicMock()
    streams: list = []

    async def _stream(session, message):
        streams.append(message)
        events = first_turn_events if len(streams) == 1 else [
            StreamEvent(
                type="tool_call", tool_name="agent_task_response",
                tool_args={"response": "final answer"}, tool_id="call_done",
            ),
        ]
        for event in events:
            yield event

    provider.send_message_stream.side_effect = _stream
    provider.get_usage.return_value = UsageStats()
    provider.streams = streams
    return provider


def _run(coro_factory):
    dispatched = AsyncMock(return_value=("should not run", []))
    with patch(
        "chat.gemini_api.sub_agent.record_api_call", new=AsyncMock()
    ), patch(
        "api.instructions.get_user_connected_services", return_value={}
    ), patch(
        "chat.gemini_api.sub_agent.compute_new_input_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent.compute_total_context_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent._dispatch_tool_call", new=dispatched
    ):
        return asyncio.run(coro_factory()), dispatched


def _run_agent(provider, user, on_event=None):
    return _run(lambda: _run_sub_agent(
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


def _tool_results(provider):
    """The tool_results list handed to format_tool_results on turn 1."""
    assert provider.format_tool_results.called
    return provider.format_tool_results.call_args_list[0].args[1]


def test_malformed_arguments_are_not_dispatched(user):
    provider = _make_provider([
        StreamEvent(
            type="tool_call", tool_name="get_current_time", tool_args={},
            tool_id="call_1",
            tool_args_error="Tool call arguments were not valid JSON (x). Received: {oops",
        ),
    ])
    on_event = AsyncMock()
    result, dispatched = _run_agent(provider, user, on_event)

    dispatched.assert_not_called()
    assert result == "final answer"
    results = _tool_results(provider)
    assert len(results) == 1
    assert results[0]["tool_id"] == "call_1"
    payload = json.loads(results[0]["result"])
    assert "get_current_time" in payload["error"]
    assert "{oops" in payload["error"]

    # The failed attempt still shows in the sub-agent tree.
    types = [call.args[0]["type"] for call in on_event.await_args_list]
    assert "sub_agent_tool_use" in types
    assert "sub_agent_tool_result" in types
    tool_result_event = next(
        call.args[0] for call in on_event.await_args_list
        if call.args[0]["type"] == "sub_agent_tool_result"
    )
    assert tool_result_event["tool_id"] == "sub_call_1"
    assert "{oops" in tool_result_event["tool_output"]


def test_malformed_agent_task_response_does_not_end_the_agent(user):
    """The response text is inside the unparseable payload, so the agent
    must get an error back and retry rather than finish empty-handed."""
    provider = _make_provider([
        StreamEvent(
            type="tool_call", tool_name="agent_task_response", tool_args={},
            tool_id="call_1",
            tool_args_error="Tool call arguments must be a JSON object, got list. Received: []",
        ),
    ])
    result, dispatched = _run_agent(provider, user)

    dispatched.assert_not_called()
    # Turn 1 got the error back; the agent then returned normally.
    assert len(provider.streams) >= 2
    assert result == "final answer"
    payload = json.loads(_tool_results(provider)[0]["result"])
    assert "agent_task_response" in payload["error"]
