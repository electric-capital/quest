"""Generic tests for the sandbox script tool-call bridge
(``POST /api/tool-call``, chat/gemini_api/script_tool_call.py).

The Slack- and plugin-specific allowlist coverage lives in the plugin
suites (e.g. plugins/slack/tests/test_slack_tools.py); this file pins the
core endpoint behaviour that holds regardless of which plugins are
loaded.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

_USER = {"email": "test@example.com"}


def _run(coro):
    return asyncio.run(coro)


def test_non_allowlisted_tool_rejected_with_400():
    from chat.gemini_api.script_tool_call import (
        ScriptToolCallRequest,
        script_tool_call_endpoint,
    )

    response = _run(script_tool_call_endpoint(
        ScriptToolCallRequest(tool_name="wait_for_handles", arguments={}),
        user=_USER, lease=None,
    ))
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert "not available from scripts" in payload["error"]


def test_non_json_result_returned_as_plain_text():
    from chat.gemini_api.script_tool_call import (
        ScriptToolCallRequest,
        script_tool_call_endpoint,
    )

    dispatch = AsyncMock(return_value=("# Gmail message\n\nhello", []))
    with patch("chat.gemini_api.tool_dispatch._dispatch_tool_call", dispatch):
        response = _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="get_gmail_messages", arguments={"message_ids": ["x"]}),
            user=_USER, lease=None,
        ))

    assert response.status_code == 200
    assert response.body.decode() == "# Gmail message\n\nhello"
    assert response.media_type == "text/plain"


def test_restricted_lease_rejects_mutating_tool_before_dispatch():
    """A container launched from an inference API run holds a lease with
    block_mutating_tools set; the bridge refuses the mutating tools with
    403 without ever reaching dispatch."""
    from chat.gemini_api.script_tool_call import (
        ScriptToolCallRequest,
        script_tool_call_endpoint,
    )
    from chat.sandbox_tokens import SandboxLease

    lease = SandboxLease(
        user_id=1, conversation_id="c", expires_at=1e12, block_mutating_tools=True,
    )
    dispatch = AsyncMock(return_value=('{"ok": true}', []))
    with patch("chat.gemini_api.tool_dispatch._dispatch_tool_call", dispatch):
        blocked = _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="send_gmail_to_self", arguments={}),
            user=_USER, lease=lease,
        ))
        allowed = _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="get_gmail_messages", arguments={}),
            user=_USER, lease=lease,
        ))

    assert blocked.status_code == 403
    assert "read-only" in json.loads(blocked.body)["error"]
    assert allowed.status_code == 200
    assert dispatch.await_count == 1


def test_lease_context_scopes_dispatch_like_the_launching_conversation():
    """The bridge hands dispatch the project and public flag recorded on
    the container's lease (and no conversation id), so the Quest Docs /
    Tables access rule scopes a script like the conversation that ran it;
    without a lease it dispatches as a standalone private caller."""
    from chat.gemini_api.script_tool_call import (
        ScriptToolCallRequest,
        script_tool_call_endpoint,
    )
    from chat.sandbox_tokens import SandboxLease

    lease = SandboxLease(
        user_id=1, conversation_id="c", expires_at=1e12, project_id="proj-1",
    )
    dispatch = AsyncMock(return_value=('{"docs": []}', []))
    with patch("chat.gemini_api.tool_dispatch._dispatch_tool_call", dispatch):
        _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="list_docs", arguments={}),
            user=_USER, lease=lease,
        ))
        _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="list_docs", arguments={}),
            user=_USER, lease=None,
        ))

    with_lease, without = (call.kwargs for call in dispatch.await_args_list)
    assert with_lease["is_script"] is True
    assert with_lease["conversation_id"] is None
    assert with_lease["project_id"] == "proj-1"
    assert with_lease["is_public"] is False
    assert without["is_script"] is True
    assert without["project_id"] is None
    assert without["is_public"] is False
