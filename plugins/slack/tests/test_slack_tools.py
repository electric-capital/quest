"""Tests for the Slack dynamic tool handlers in ``plugins/slack/tools.py``.

The (ctx, args) handlers wrap the ``plugins/slack/upstream`` endpoint
functions. These tests mock the endpoint functions themselves, so they
cover the tool-side plumbing:

* Result conversion: dict -> JSON, ToolResultWithNotices -> JSON with a
  merged ``notices`` key, HTTPException (dict and string detail) ->
  ``{"error": ...}`` JSON, unexpected exceptions wrapped.
* Argument coercion: string ints/bools from the model, None/empty optional
  strings dropped to None, endpoint defaults applied when args are omitted.
* Required-argument validation (channel, ts, query, user_id).

Plus the registration wiring (registry/dispatch/script-bridge fan-out via
the ``slack_plugin`` fixture) and the core proxy-path blocks that keep
Slack tool-only.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from chat.tool_notices import ToolResultWithNotices
from plugins.slack.tools import (
    _run_slack_endpoint,
    _tool_get_slack_conversation_history,
    _tool_get_slack_conversation_replies,
    _tool_get_slack_user_info,
    _tool_list_slack_conversations,
    _tool_list_slack_teams,
    _tool_list_slack_users,
    _tool_search_slack_messages,
    _tool_send_slack_dm_to_self,
)


def _run(coro):
    return asyncio.run(coro)


_USER = {
    "email": "test@example.com",
    "service_credentials": {
        "slack": {"oauth_blob": {"access_token": "x"}},
    },
}


def _ctx(conversation_id="conv-1", project_id=None):
    return SimpleNamespace(
        user=_USER, conversation_id=conversation_id, project_id=project_id,
    )


# ---------------------------------------------------------------------------
# _run_slack_endpoint result/error conversion
# ---------------------------------------------------------------------------

class TestRunSlackEndpoint:
    def test_dict_result_returns_json(self):
        async def endpoint():
            return {"ok": True, "channels": []}

        result = _run(_run_slack_endpoint(endpoint()))
        assert json.loads(result) == {"ok": True, "channels": []}

    def test_notices_wrapper_merged_into_result(self):
        async def endpoint():
            return ToolResultWithNotices(
                result={"ok": True, "members": []},
                notices=["Your requested limit of 100 was capped to 50."],
            )

        result = json.loads(_run(_run_slack_endpoint(endpoint())))
        assert result["ok"] is True
        assert result["notices"] == ["Your requested limit of 100 was capped to 50."]

    def test_http_exception_dict_detail_preserved(self):
        async def endpoint():
            raise HTTPException(status_code=401, detail={
                "error": "authentication_required",
                "message": "Slack authentication required.",
            })

        result = json.loads(_run(_run_slack_endpoint(endpoint())))
        assert result["error"] == "authentication_required"
        assert result["message"] == "Slack authentication required."

    def test_http_exception_string_detail_wrapped(self):
        async def endpoint():
            raise HTTPException(status_code=404, detail="channel_not_found")

        result = json.loads(_run(_run_slack_endpoint(endpoint())))
        assert result["error"] == "slack_http_404"
        assert result["message"] == "channel_not_found"

    def test_unexpected_exception_wrapped(self):
        async def endpoint():
            raise RuntimeError("boom")

        result = json.loads(_run(_run_slack_endpoint(endpoint())))
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# list_slack_teams
# ---------------------------------------------------------------------------

class TestListSlackTeams:
    def test_defaults(self):
        teams = AsyncMock(return_value={"ok": True, "teams": []})
        with patch("plugins.slack.upstream.list_teams", teams):
            result = json.loads(_run(_tool_list_slack_teams(_ctx(), {})))

        assert result == {"ok": True, "teams": []}
        kwargs = teams.call_args.kwargs
        assert kwargs["user"] is _USER
        assert kwargs["limit"] == 100
        assert kwargs["cursor"] is None

    def test_string_limit_coerced(self):
        teams = AsyncMock(return_value={"ok": True})
        with patch("plugins.slack.upstream.list_teams", teams):
            _run(_tool_list_slack_teams(_ctx(), {"limit": "25", "cursor": "abc"}))

        kwargs = teams.call_args.kwargs
        assert kwargs["limit"] == 25
        assert kwargs["cursor"] == "abc"


# ---------------------------------------------------------------------------
# list_slack_conversations
# ---------------------------------------------------------------------------

class TestListSlackConversations:
    def test_defaults_mirror_endpoint_defaults(self):
        conversations = AsyncMock(return_value={"ok": True, "channels": []})
        with patch("plugins.slack.upstream.list_conversations", conversations):
            _run(_tool_list_slack_conversations(_ctx(), {}))

        kwargs = conversations.call_args.kwargs
        assert kwargs["types"] == "public_channel,private_channel"
        assert kwargs["limit"] == 50
        assert kwargs["cursor"] is None
        assert kwargs["exclude_archived"] is True
        assert kwargs["team_id"] is None

    def test_explicit_args_passed_through(self):
        conversations = AsyncMock(return_value={"ok": True})
        with patch("plugins.slack.upstream.list_conversations", conversations):
            _run(_tool_list_slack_conversations(_ctx(), {
                "types": "public_channel,im",
                "limit": "10",
                "cursor": "cur",
                "exclude_archived": "false",
                "team_id": " T123 ",
            }))

        kwargs = conversations.call_args.kwargs
        assert kwargs["types"] == "public_channel,im"
        assert kwargs["limit"] == 10
        assert kwargs["cursor"] == "cur"
        assert kwargs["exclude_archived"] is False
        assert kwargs["team_id"] == "T123"


# ---------------------------------------------------------------------------
# get_slack_conversation_history
# ---------------------------------------------------------------------------

class TestGetSlackConversationHistory:
    def test_channel_required(self):
        result = json.loads(_run(
            _tool_get_slack_conversation_history(_ctx(), {"channel": "  "})
        ))
        assert "channel is required" in result["error"]

    def test_args_passed_through(self):
        history = AsyncMock(return_value={"ok": True, "messages": []})
        with patch("plugins.slack.upstream.get_conversation_history", history):
            result = json.loads(_run(_tool_get_slack_conversation_history(_ctx(), {
                "channel": "C123",
                "limit": "20", "oldest": "1.0", "latest": "2.0", "inclusive": "true",
            })))

        assert result == {"ok": True, "messages": []}
        kwargs = history.call_args.kwargs
        assert kwargs["channel"] == "C123"
        assert kwargs["user"] is _USER
        assert kwargs["limit"] == 20
        assert kwargs["oldest"] == "1.0"
        assert kwargs["latest"] == "2.0"
        assert kwargs["inclusive"] is True


# ---------------------------------------------------------------------------
# get_slack_conversation_replies
# ---------------------------------------------------------------------------

class TestGetSlackConversationReplies:
    def test_channel_and_ts_required(self):
        result = json.loads(_run(
            _tool_get_slack_conversation_replies(_ctx(), {"channel": "C123"})
        ))
        assert "channel and ts are required" in result["error"]

        result = json.loads(_run(
            _tool_get_slack_conversation_replies(_ctx(), {"ts": "1.2"})
        ))
        assert "channel and ts are required" in result["error"]

    def test_args_passed_through(self):
        replies = AsyncMock(return_value={"ok": True, "messages": []})
        with patch("plugins.slack.upstream.get_conversation_replies", replies):
            _run(_tool_get_slack_conversation_replies(_ctx(), {
                "channel": "C123", "ts": "1234567890.123456",
            }))

        kwargs = replies.call_args.kwargs
        assert kwargs["channel"] == "C123"
        assert kwargs["ts"] == "1234567890.123456"
        assert kwargs["limit"] == 50


# ---------------------------------------------------------------------------
# search_slack_messages
# ---------------------------------------------------------------------------

class TestSearchSlackMessages:
    def test_query_required(self):
        result = json.loads(_run(_tool_search_slack_messages(_ctx(), {"query": ""})))
        assert "query is required" in result["error"]

    def test_defaults_mirror_endpoint_defaults(self):
        search = AsyncMock(return_value={"ok": True, "messages": {}})
        with patch("plugins.slack.upstream.search_messages", search):
            _run(_tool_search_slack_messages(_ctx(), {"query": "project update"}))

        kwargs = search.call_args.kwargs
        assert kwargs["query"] == "project update"
        assert kwargs["count"] == 20
        assert kwargs["page"] == 1
        assert kwargs["sort"] == "score"
        assert kwargs["sort_dir"] == "desc"
        assert kwargs["team_id"] is None

    def test_explicit_args_coerced(self):
        search = AsyncMock(return_value={"ok": True})
        with patch("plugins.slack.upstream.search_messages", search):
            _run(_tool_search_slack_messages(_ctx(), {
                "query": "q", "count": "5", "page": "2",
                "sort": "timestamp", "sort_dir": "asc", "team_id": "T9",
            }))

        kwargs = search.call_args.kwargs
        assert kwargs["count"] == 5
        assert kwargs["page"] == 2
        assert kwargs["sort"] == "timestamp"
        assert kwargs["sort_dir"] == "asc"
        assert kwargs["team_id"] == "T9"


# ---------------------------------------------------------------------------
# get_slack_user_info
# ---------------------------------------------------------------------------

class TestGetSlackUserInfo:
    def test_user_id_required(self):
        result = json.loads(_run(_tool_get_slack_user_info(_ctx(), {})))
        assert "user_id is required" in result["error"]

    def test_passes_user_id(self):
        info = AsyncMock(return_value={"ok": True, "user": {"id": "U1"}})
        with patch("plugins.slack.upstream.get_user_info", info):
            result = json.loads(_run(_tool_get_slack_user_info(_ctx(), {"user_id": "U1"})))

        assert result["user"]["id"] == "U1"
        kwargs = info.call_args.kwargs
        assert kwargs["user_id"] == "U1"
        assert kwargs["user"] is _USER


# ---------------------------------------------------------------------------
# list_slack_users
# ---------------------------------------------------------------------------

class TestListSlackUsers:
    def test_defaults(self):
        users = AsyncMock(return_value={"ok": True, "members": []})
        with patch("plugins.slack.upstream.list_users", users):
            _run(_tool_list_slack_users(_ctx(), {}))

        kwargs = users.call_args.kwargs
        assert kwargs["limit"] == 50
        assert kwargs["cursor"] is None
        assert kwargs["team_id"] is None

    def test_notices_surface_in_result(self):
        users = AsyncMock(return_value=ToolResultWithNotices(
            result={"ok": True, "members": []},
            notices=["Your requested limit of 200 was capped to 50."],
        ))
        with patch("plugins.slack.upstream.list_users", users):
            result = json.loads(_run(_tool_list_slack_users(_ctx(), {"limit": 200})))

        assert result["ok"] is True
        assert "capped to 50" in result["notices"][0]


# ---------------------------------------------------------------------------
# send_slack_dm_to_self
# ---------------------------------------------------------------------------

class TestSendSlackDmToSelf:
    def test_message_required(self):
        result = json.loads(_run(_tool_send_slack_dm_to_self(
            _ctx(), {"message": "  "},
        )))
        assert "message is required" in result["error"]

    def test_text_only_sends_without_files(self):
        dm = AsyncMock(return_value={"ok": True, "ts": "1.2"})
        with patch("plugins.slack.upstream.send_dm_to_self", dm):
            result = json.loads(_run(_tool_send_slack_dm_to_self(
                _ctx(), {"message": "hello"},
            )))

        assert result == {"ok": True, "ts": "1.2"}
        kwargs = dm.call_args.kwargs
        assert kwargs["text"] == "hello"
        assert kwargs["user"] is _USER
        assert kwargs["files"] is None

    def test_invalid_files_shape_rejected(self):
        for bad in ("report.pdf", [1], [""], [{"path": "a"}]):
            result = json.loads(_run(_tool_send_slack_dm_to_self(
                _ctx(), {"message": "hi", "files": bad},
            )))
            assert "list of workspace-relative file paths" in result["error"], bad

    def test_too_many_files_rejected(self):
        result = json.loads(_run(_tool_send_slack_dm_to_self(
            _ctx(), {"message": "hi", "files": [f"f{i}.txt" for i in range(11)]},
        )))
        assert "Too many files" in result["error"]

    def test_workspace_read_error_surfaced(self):
        reader = AsyncMock(side_effect=RuntimeError("File not found in workspace: nope.pdf"))
        with patch(
            "chat.action_request_types._io_attachments.read_workspace_file_bytes",
            reader,
        ):
            result = json.loads(_run(_tool_send_slack_dm_to_self(
                _ctx(), {"message": "hi", "files": ["nope.pdf"]},
            )))
        assert "File not found in workspace" in result["error"]

    def test_files_resolved_and_passed_through(self):
        reader = AsyncMock(return_value=("report.pdf", b"bytes", "application/pdf"))
        dm = AsyncMock(return_value={"ok": True, "files": [{"id": "F1"}]})
        with patch(
            "chat.action_request_types._io_attachments.read_workspace_file_bytes",
            reader,
        ), patch("plugins.slack.upstream.send_dm_to_self", dm):
            result = json.loads(_run(_tool_send_slack_dm_to_self(
                _ctx(project_id="proj-1"),
                {"message": "report ready", "files": [" out/report.pdf "]},
            )))

        assert result["ok"] is True
        assert reader.call_args.args == ("conv-1", "proj-1", "out/report.pdf")
        kwargs = dm.call_args.kwargs
        assert kwargs["text"] == "report ready"
        assert kwargs["files"] == [("report.pdf", b"bytes")]
        assert kwargs["user"] is _USER


# ---------------------------------------------------------------------------
# upstream.send_dm_to_self (Slack API call sequence / scope footprint)
# ---------------------------------------------------------------------------

class TestSendDmToSelfApi:
    _SLACK_USER = {
        "service_credentials": {
            "slack": {"oauth_blob": {"access_token": "x", "user_id": "U123"}},
        },
    }

    def test_text_only_posts_directly_to_user_id(self):
        from plugins.slack.upstream import send_dm_to_self

        post = AsyncMock(return_value={"ok": True, "channel": "D42", "ts": "1.2"})
        with patch("plugins.slack.upstream.get_slack_bot_token", return_value="xoxb-1"), \
                patch("plugins.slack.upstream.proxy_slack_post_request", post):
            result = _run(send_dm_to_self(self._SLACK_USER, "hi"))

        assert result["ok"] is True
        assert post.call_count == 1
        args = post.call_args.args
        assert args[1] == "chat.postMessage"
        assert args[2] == {
            "channel": "U123",
            "text": "hi",
            "unfurl_links": False,
            "unfurl_media": False,
        }

    def test_text_only_never_lets_slack_fetch_model_written_urls(self):
        # The tool needs no approval, so a prompt-injected URL in the
        # message must not trigger Slack's server-side link crawler (which
        # would deliver whatever the model put in the query string to an
        # arbitrary host). Both unfurl classes must be explicitly off --
        # Slack's chat.postMessage default for unfurl_media is on.
        from plugins.slack.upstream import send_dm_to_self

        post = AsyncMock(return_value={"ok": True, "channel": "D42", "ts": "1.2"})
        text = "done: https://attacker.example/collect?api_key=qst_secret"
        with patch("plugins.slack.upstream.get_slack_bot_token", return_value="xoxb-1"), \
                patch("plugins.slack.upstream.proxy_slack_post_request", post):
            _run(send_dm_to_self(self._SLACK_USER, text))

        payload = post.call_args.args[2]
        assert payload["text"] == text
        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False

    def test_files_flow_sends_one_message_with_initial_comment(self):
        # File sends deliver text + files as ONE Slack message: the DM
        # channel comes from conversations.open (im:write scope) and the
        # text rides as completeUploadExternal's initial_comment -- there
        # is no separate chat.postMessage.
        from plugins.slack.upstream import send_dm_to_self

        post = AsyncMock(side_effect=[
            {"ok": True, "channel": {"id": "D42"}},  # conversations.open
            {"ok": True, "files": [{"id": "F1"}]},   # completeUploadExternal
        ])
        get_url = AsyncMock(return_value={
            "ok": True, "upload_url": "https://up.example", "file_id": "F1",
        })
        raw_upload = AsyncMock(return_value=SimpleNamespace(status_code=200))
        with patch("plugins.slack.upstream.get_slack_bot_token", return_value="xoxb-1"), \
                patch("plugins.slack.upstream.proxy_slack_post_request", post), \
                patch("plugins.slack.upstream.proxy_slack_bot_get_request", get_url), \
                patch("plugins.slack.upstream._slack_request_with_retry", raw_upload):
            result = _run(send_dm_to_self(
                self._SLACK_USER, "hi", files=[("a.txt", b"data")],
            ))

        assert result["ok"] is True
        methods = [c.args[1] for c in post.call_args_list]
        assert methods == ["conversations.open", "files.completeUploadExternal"]
        assert post.call_args_list[0].args[2] == {"users": "U123"}
        complete_payload = post.call_args_list[1].args[2]
        assert complete_payload["channel_id"] == "D42"
        assert complete_payload["files"] == [{"id": "F1", "title": "a.txt"}]
        assert complete_payload["initial_comment"] == "hi"
        assert get_url.call_args.args[0] == "files.getUploadURLExternal"


# ---------------------------------------------------------------------------
# Registry / dispatch wiring (plugin registration fan-out)
# ---------------------------------------------------------------------------

SLACK_TOOLS = [
    "list_slack_teams",
    "list_slack_conversations",
    "get_slack_conversation_history",
    "get_slack_conversation_replies",
    "search_slack_messages",
    "get_slack_user_info",
    "list_slack_users",
    "send_slack_dm_to_self",
]


@pytest.mark.usefixtures("slack_plugin")
class TestRegistryWiring:
    def test_all_slack_tools_registered(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS

        for name in SLACK_TOOLS + ["find_slack_channel"]:
            assert name in TOOL_CALL_REGISTRY, name
            assert name in TOOL_CALL_HANDLERS, name
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["name"] == name
            assert spec["parameters"]["type"] == "object"
            assert spec["requires_service"] == "slack"

    def test_required_params_declared(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        assert TOOL_CALL_REGISTRY["get_slack_conversation_history"]["parameters"]["required"] == ["channel"]
        assert TOOL_CALL_REGISTRY["get_slack_conversation_replies"]["parameters"]["required"] == ["channel", "ts"]
        assert TOOL_CALL_REGISTRY["search_slack_messages"]["parameters"]["required"] == ["query"]
        assert TOOL_CALL_REGISTRY["get_slack_user_info"]["parameters"]["required"] == ["user_id"]

    def test_action_request_types_registered(self):
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        from chat.action_request_types import get_handler

        assert "send_slack_message" in ACTION_REQUEST_TYPE_ENUM
        assert "send_slack_dm" in ACTION_REQUEST_TYPE_ENUM
        assert get_handler("send_slack_message") is not None
        assert get_handler("send_slack_dm") is not None

    def test_system_skill_registered(self):
        from chat.system_skills import CATALOG

        skill = CATALOG["system:slack"]
        assert skill.requires == "slack"
        content = skill.content_builder("http://localhost:9000", "key")
        assert "send_slack_dm_to_self" in content
        assert "send_slack_message" in content

    def test_send_slack_dm_to_self_stays_public_allowlisted(self):
        # The one plugin tool in the core public-project allowlist, via the
        # core-owned migration exemption in config/plugins.py.
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST

        assert "send_slack_dm_to_self" in PUBLIC_TOOL_CALL_ALLOWLIST


# ---------------------------------------------------------------------------
# Proxy blocking: /api/slack is tool-only
# ---------------------------------------------------------------------------

class TestSlackProxyBlocked:
    def test_slack_prefix_in_blocked_paths(self):
        from chat.route_dispatch import _BLOCKED_PROXY_PATHS

        assert "/api/slack" in _BLOCKED_PROXY_PATHS

    def test_curl_proxy_get_on_slack_read_returns_blocked_error(self):
        from chat.route_dispatch import execute_tool_call

        result, notices = _run(execute_tool_call(
            app=object(),  # blocked-path check fires before route resolution
            user=_USER,
            tool_name="curl_proxy_get",
            tool_args={"url": "http://localhost:8000/api/slack/conversations.list"},
        ))
        payload = json.loads(result)
        assert "dedicated tool" in payload["error"]
        assert notices == []

    def test_slack_simple_dm_self_blocked_from_proxy(self):
        # The dm-self HTTP route is gone (send_slack_dm_to_self is the only
        # path); the blocked prefix gives cached sessions a use-the-tool
        # error instead of a bare no-route error.
        from chat.route_dispatch import execute_tool_call

        result, notices = _run(execute_tool_call(
            app=object(),
            user=_USER,
            tool_name="curl_proxy_post",
            tool_args={"url": "http://localhost:8000/api/slack-simple/dm-self?text=hi"},
        ))
        payload = json.loads(result)
        assert "dedicated tool" in payload["error"]
        assert notices == []


# ---------------------------------------------------------------------------
# Script tool-call bridge (POST /api/tool-call)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("slack_plugin")
class TestScriptToolCallBridge:
    def test_allowlist_contains_slack_tools_and_no_loop_tools(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        for name in SLACK_TOOLS + ["find_slack_channel"]:
            assert name in SCRIPT_TOOL_CALL_ALLOWLIST, name
        # Everything allow-listed must be a real registry tool
        assert SCRIPT_TOOL_CALL_ALLOWLIST <= set(TOOL_CALL_REGISTRY)
        # Suspend / conversation-scoped / provider-part tools must stay out
        for name in (
            "wait_for_handles", "set_conversation_name", "project_db_query",
            "get_response_content", "get_workspace_file", "write_workspace_file",
            "edit_workspace_file", "list_workspace_files", "download_drive_file",
            "github_get_job_log", "authed_get", "authed_post",
            "list_files", "read_file", "write_file", "edit_file", "copy_file",
        ):
            assert name not in SCRIPT_TOOL_CALL_ALLOWLIST, name

    def test_allowed_tool_dispatches_and_returns_json(self):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        dispatch = AsyncMock(return_value=(json.dumps({"ok": True, "teams": []}), []))
        with patch("chat.gemini_api.tool_dispatch._dispatch_tool_call", dispatch):
            response = _run(script_tool_call_endpoint(
                ScriptToolCallRequest(tool_name="list_slack_teams", arguments={"limit": 5}),
                user=_USER,
            ))

        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": True, "teams": []}
        kwargs = dispatch.call_args.kwargs
        assert kwargs["tool_name"] == "tool_call"
        assert kwargs["args"] == {"tool_name": "list_slack_teams", "arguments": {"limit": 5}}
        assert kwargs["user"] is _USER
        assert kwargs["conversation_id"] is None
