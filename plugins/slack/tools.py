"""Slack dynamic tools (plugin module).

The nine tool_call-routed Slack tools, moved from the pre-plugin core
registry (chat/llm/tool_schemas.py specs + chat/gemini_api/tool_handlers/
handlers):

- Reads: ``list_slack_teams``, ``list_slack_conversations``,
  ``get_slack_conversation_history``, ``get_slack_conversation_replies``,
  ``search_slack_messages``, ``get_slack_user_info``, ``list_slack_users``,
  ``find_slack_channel``.
- The no-approval self-DM send: ``send_slack_dm_to_self`` (bot-delivered,
  fixed recipient; also the one plugin tool in the core public-project
  allowlist -- see ``_PUBLIC_ALLOWLIST_MIGRATED_TOOLS`` in
  config/plugins.py).

The names predate the plugin packaging (baked into transcripts, sandbox
scripts using POST /api/tool-call, and skill prose) so they ride on the
manifest's ``unprefixed_tools`` grandfather list instead of the
``slack_`` prefix rule.
"""

import json
import logging
from typing import Any

import httpx

from config.plugin_types import PluginTool

from plugins.slack import upstream
from plugins.slack.upstream import SLACK_API_BASE, get_user_slack_oauth

logger = logging.getLogger(__name__)

_SLACK_DM_SELF_MAX_FILES = 10


# ---------------------------------------------------------------------------
# Argument coercion + result rendering helpers
# ---------------------------------------------------------------------------

def _arg_int(value, default: int) -> int:
    """Coerce a model-supplied optional integer argument."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _arg_str(value) -> str | None:
    """Coerce a model-supplied optional string argument (None/empty -> None)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _arg_bool(value) -> bool:
    """Coerce a model-supplied boolean-ish tool argument to bool."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


async def _run_slack_endpoint(coro) -> str:
    """Await a Slack endpoint coroutine and render a tool result.

    Unwraps ToolResultWithNotices (limit-cap advisories) by merging the
    notices into the response dict under a ``notices`` key, and converts
    HTTPException -- the endpoints' error channel -- to the structured
    ``{"error": ...}`` JSON the other dynamic tools return.
    """
    from fastapi import HTTPException

    from chat.tool_notices import ToolResultWithNotices

    try:
        result = await coro
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            payload = dict(detail)
            payload.setdefault("error", f"slack_http_{e.status_code}")
        else:
            payload = {"error": f"slack_http_{e.status_code}", "message": str(detail)}
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": f"Slack request failed: {e}"})

    notices: list[str] = []
    if isinstance(result, ToolResultWithNotices):
        notices = result.notices
        result = result.result

    if isinstance(result, dict):
        if notices:
            result = {**result, "notices": notices}
        return json.dumps(result, default=str)
    if isinstance(result, list):
        return json.dumps(result, default=str)
    return str(result)


# ---------------------------------------------------------------------------
# Handlers ((ctx, args) shape -- see config/plugin_types.py PluginTool)
# ---------------------------------------------------------------------------

async def _tool_list_slack_teams(ctx, args: dict) -> str:
    return await _run_slack_endpoint(upstream.list_teams(
        user=ctx.user,
        limit=_arg_int(args.get("limit"), 100),
        cursor=_arg_str(args.get("cursor")),
    ))


async def _tool_list_slack_conversations(ctx, args: dict) -> str:
    exclude_archived = args.get("exclude_archived")
    return await _run_slack_endpoint(upstream.list_conversations(
        user=ctx.user,
        types=_arg_str(args.get("types")) or "public_channel,private_channel",
        limit=_arg_int(args.get("limit"), 50),
        cursor=_arg_str(args.get("cursor")),
        exclude_archived=True if exclude_archived is None else _arg_bool(exclude_archived),
        team_id=_arg_str(args.get("team_id")),
    ))


async def _tool_get_slack_conversation_history(ctx, args: dict) -> str:
    channel = _arg_str(args.get("channel"))
    if not channel:
        return json.dumps({"error": "channel is required (a Slack channel ID, e.g. 'C1234567890')."})

    return await _run_slack_endpoint(upstream.get_conversation_history(
        channel=channel,
        user=ctx.user,
        limit=_arg_int(args.get("limit"), 50),
        cursor=_arg_str(args.get("cursor")),
        oldest=_arg_str(args.get("oldest")),
        latest=_arg_str(args.get("latest")),
        inclusive=_arg_bool(args.get("inclusive")),
    ))


async def _tool_get_slack_conversation_replies(ctx, args: dict) -> str:
    channel = _arg_str(args.get("channel"))
    ts = _arg_str(args.get("ts"))
    if not channel or not ts:
        return json.dumps({
            "error": "channel and ts are required (channel ID and the thread parent message timestamp)."
        })

    return await _run_slack_endpoint(upstream.get_conversation_replies(
        channel=channel,
        ts=ts,
        user=ctx.user,
        limit=_arg_int(args.get("limit"), 50),
        cursor=_arg_str(args.get("cursor")),
        oldest=_arg_str(args.get("oldest")),
        latest=_arg_str(args.get("latest")),
        inclusive=_arg_bool(args.get("inclusive")),
    ))


async def _tool_search_slack_messages(ctx, args: dict) -> str:
    query = _arg_str(args.get("query"))
    if not query:
        return json.dumps({"error": "query is required."})

    return await _run_slack_endpoint(upstream.search_messages(
        query=query,
        user=ctx.user,
        count=_arg_int(args.get("count"), 20),
        page=_arg_int(args.get("page"), 1),
        sort=_arg_str(args.get("sort")) or "score",
        sort_dir=_arg_str(args.get("sort_dir")) or "desc",
        team_id=_arg_str(args.get("team_id")),
    ))


async def _tool_get_slack_user_info(ctx, args: dict) -> str:
    user_id = _arg_str(args.get("user_id"))
    if not user_id:
        return json.dumps({"error": "user_id is required (a Slack user ID, e.g. 'U1234567890')."})

    return await _run_slack_endpoint(upstream.get_user_info(user_id=user_id, user=ctx.user))


async def _tool_list_slack_users(ctx, args: dict) -> str:
    return await _run_slack_endpoint(upstream.list_users(
        user=ctx.user,
        limit=_arg_int(args.get("limit"), 50),
        cursor=_arg_str(args.get("cursor")),
        team_id=_arg_str(args.get("team_id")),
    ))


async def _tool_send_slack_dm_to_self(ctx, args: dict) -> str:
    """Send a bot-delivered Slack DM to the user themselves, optionally
    attaching workspace files.

    The recipient is fixed to the authenticated user's own Slack ID, so
    this needs no approval (unlike the send_slack_message action request)
    and is safe to expose in public-project conversations: outbound-only,
    reading nothing beyond the workspace files the model names.
    """
    message = _arg_str(args.get("message"))
    if not message:
        return json.dumps({"error": "message is required."})

    files = args.get("files")
    if files is None:
        files = []
    if not isinstance(files, list) or not all(
        isinstance(f, str) and f.strip() for f in files
    ):
        return json.dumps({
            "error": "files must be a list of workspace-relative file paths.",
        })
    files = [f.strip() for f in files]
    if len(files) > _SLACK_DM_SELF_MAX_FILES:
        return json.dumps({
            "error": (
                f"Too many files ({len(files)}); at most "
                f"{_SLACK_DM_SELF_MAX_FILES} files per DM."
            ),
        })

    resolved: list[tuple[str, bytes]] = []
    if files:
        from chat.action_request_types._io_attachments import read_workspace_file_bytes

        for raw_path in files:
            try:
                filename, data, _content_type = await read_workspace_file_bytes(
                    ctx.conversation_id, ctx.project_id, raw_path,
                )
            except RuntimeError as exc:
                return json.dumps({"error": str(exc)})
            resolved.append((filename, data))

    return await _run_slack_endpoint(
        upstream.send_dm_to_self(user=ctx.user, text=message, files=resolved or None)
    )


async def _tool_find_slack_channel(ctx, args: dict) -> str:
    """Search for Slack channels by name substring.

    Fetches channels visible to the user via conversations.list and filters
    by case-insensitive substring match on channel name. Uses auth.teams.list
    (bot token) to resolve workspace team IDs and names, since org-level
    installs store an Enterprise ID (E-prefix) rather than a workspace ID
    (T-prefix) in default_team_id.

    Returns:
        JSON string with matching channels, each containing channel_id,
        channel_name, team_id, and team_name.
    """
    slack_oauth = get_user_slack_oauth(ctx.user)
    user_token = slack_oauth.get("access_token")
    if not user_token:
        return json.dumps({
            "error": "Slack not connected. The user needs to connect Slack via Settings > Data Connections."
        })

    search_lower = str(args.get("search", "")).strip().lower()
    if not search_lower:
        return json.dumps({"error": "Search string cannot be empty."})

    try:
        async with httpx.AsyncClient() as client:
            user_headers = {"Authorization": f"Bearer {user_token}"}

            # Resolve workspace team IDs. The stored default_team_id is an
            # Enterprise ID (E-prefix) for org-level installs, but
            # conversations.list requires workspace IDs (T-prefix).
            # Use auth.teams.list (bot token) to discover workspaces.
            workspace_teams: list[dict[str, str]] = []  # [{id, name}, ...]
            try:
                bot_token = upstream.get_slack_bot_token()
                teams_resp = await client.get(
                    f"{SLACK_API_BASE}/auth.teams.list",
                    params={"limit": 100},
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
                teams_data = teams_resp.json()
                if teams_data.get("ok"):
                    workspace_teams = [
                        {"id": t["id"], "name": t.get("name", t["id"])}
                        for t in teams_data.get("teams", [])
                    ]
            except Exception:
                logger.warning(
                    "[find_slack_channel] Failed to fetch workspace teams",
                    exc_info=True,
                )

            # Fall back to default_team_id if auth.teams.list didn't work
            if not workspace_teams:
                default_team_id = slack_oauth.get("default_team_id", "")
                if not default_team_id:
                    return json.dumps({
                        "error": "Could not determine workspace. Please reconnect Slack via Settings > Data Connections."
                    })
                workspace_teams = [{"id": default_team_id, "name": ""}]

            # Search channels across all workspaces
            matches = []
            for workspace in workspace_teams:
                ws_id = workspace["id"]
                ws_name = workspace["name"]
                cursor = None
                max_pages = 10
                page = 0

                while page < max_pages:
                    page += 1
                    params: dict[str, Any] = {
                        "types": "public_channel,private_channel",
                        "limit": 200,
                        "exclude_archived": True,
                        "team_id": ws_id,
                    }
                    if cursor:
                        params["cursor"] = cursor

                    resp = await client.get(
                        f"{SLACK_API_BASE}/conversations.list",
                        params=params,
                        headers=user_headers,
                    )
                    data = resp.json()

                    if not data.get("ok"):
                        logger.warning(
                            "[find_slack_channel] conversations.list failed for "
                            "workspace %s: %s",
                            ws_id, data.get("error"),
                        )
                        break  # Skip this workspace, try others

                    for channel in data.get("channels", []):
                        channel_name = channel.get("name", "")
                        if search_lower in channel_name.lower():
                            matches.append({
                                "channel_id": channel["id"],
                                "channel_name": channel_name,
                                "team_id": ws_id,
                                "team_name": ws_name,
                            })

                    next_cursor = data.get("response_metadata", {}).get("next_cursor", "")
                    if not next_cursor:
                        break
                    cursor = next_cursor

        return json.dumps({
            "match_count": len(matches),
            "channels": matches,
        })

    except Exception as e:
        logger.exception("[find_slack_channel] Failed to search channels")
        return json.dumps({"error": f"Channel search failed: {e}"})


# ---------------------------------------------------------------------------
# Tool specs (moved verbatim from the pre-plugin TOOL_CALL_REGISTRY)
# ---------------------------------------------------------------------------

FIND_SLACK_CHANNEL_TOOL = PluginTool(
    spec={
        "name": "find_slack_channel",
        "description": (
            "Search for Slack channels by name. Returns matching channels with their "
            "channel ID, name, team ID, and team name. Use this to look up a channel ID "
            "before sending a message via create_action_request. The search is a "
            "case-insensitive substring match on channel names. For example, searching "
            "'general' will match '#general', '#general-engineering', etc. "
            "Only returns public and private channels visible to the user (not DMs). "
            "Requires the user to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": (
                        "Substring to search for in channel names. Case-insensitive. "
                        "Example: 'general', 'eng', 'random'."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Find #general channel'."
                    ),
                },
            },
            "required": ["search"],
        },
    },
    handler=_tool_find_slack_channel,
    requires_service="slack",
)

LIST_SLACK_TEAMS_TOOL = PluginTool(
    spec={
        "name": "list_slack_teams",
        "description": (
            "List the Slack workspaces accessible to Quest (via the shared "
            "org-level bot token). Returns each workspace's team ID "
            "(T-prefixed) and name. Use a team ID as the team_id parameter "
            "of list_slack_conversations / search_slack_messages / "
            "list_slack_users to target a specific workspace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of workspaces to return (default 100).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response's response_metadata.next_cursor.",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List Slack workspaces'."
                    ),
                },
            },
            "required": [],
        },
    },
    handler=_tool_list_slack_teams,
    requires_service="slack",
)

LIST_SLACK_CONVERSATIONS_TOOL = PluginTool(
    spec={
        "name": "list_slack_conversations",
        "description": (
            "List Slack conversations (channels, DMs, group messages) "
            "visible to the user. Returns the Slack conversations.list "
            "response (channel IDs start with 'C'). Paginate with the "
            "cursor from response_metadata.next_cursor. Requires the user "
            "to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "types": {
                    "type": "string",
                    "description": (
                        "Comma-separated conversation types: public_channel, "
                        "private_channel, mpim, im "
                        "(default: 'public_channel,private_channel')."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (1-50, default 50).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response's response_metadata.next_cursor.",
                },
                "exclude_archived": {
                    "type": "boolean",
                    "description": "Exclude archived channels (default: true).",
                },
                "team_id": {
                    "type": "string",
                    "description": (
                        "Workspace team ID (T-prefixed, from list_slack_teams). "
                        "Omit to use the user's default workspace."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List Slack channels'."
                    ),
                },
            },
            "required": [],
        },
    },
    handler=_tool_list_slack_conversations,
    requires_service="slack",
)

GET_SLACK_CONVERSATION_HISTORY_TOOL = PluginTool(
    spec={
        "name": "get_slack_conversation_history",
        "description": (
            "Get messages from a Slack channel or DM (Slack "
            "conversations.history). Timestamps are Unix seconds with "
            "microseconds (e.g. '1234567890.123456'). Paginate with the "
            "cursor from response_metadata.next_cursor while has_more is "
            "true. Requires the user to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel ID (e.g. 'C1234567890', from list_slack_conversations or find_slack_channel).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages (1-50, default 50).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response's response_metadata.next_cursor.",
                },
                "oldest": {
                    "type": "string",
                    "description": "Only messages after this Unix timestamp (e.g. '1234567890.123456').",
                },
                "latest": {
                    "type": "string",
                    "description": "Only messages before this Unix timestamp.",
                },
                "inclusive": {
                    "type": "boolean",
                    "description": "Include messages with timestamps exactly matching oldest/latest (default: false).",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read #general history'."
                    ),
                },
            },
            "required": ["channel"],
        },
    },
    handler=_tool_get_slack_conversation_history,
    requires_service="slack",
)

GET_SLACK_CONVERSATION_REPLIES_TOOL = PluginTool(
    spec={
        "name": "get_slack_conversation_replies",
        "description": (
            "Get the replies in a Slack thread (Slack conversations.replies). "
            "Pass the parent message's timestamp as ts. Paginate with the "
            "cursor from response_metadata.next_cursor while has_more is "
            "true. Requires the user to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel ID the thread lives in (e.g. 'C1234567890').",
                },
                "ts": {
                    "type": "string",
                    "description": "Thread parent message timestamp (e.g. '1234567890.123456').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages (1-50, default 50).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response's response_metadata.next_cursor.",
                },
                "oldest": {
                    "type": "string",
                    "description": "Only messages after this Unix timestamp.",
                },
                "latest": {
                    "type": "string",
                    "description": "Only messages before this Unix timestamp.",
                },
                "inclusive": {
                    "type": "boolean",
                    "description": "Include messages with timestamps exactly matching oldest/latest (default: false).",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read thread replies'."
                    ),
                },
            },
            "required": ["channel", "ts"],
        },
    },
    handler=_tool_get_slack_conversation_replies,
    requires_service="slack",
)

SEARCH_SLACK_MESSAGES_TOOL = PluginTool(
    spec={
        "name": "search_slack_messages",
        "description": (
            "Search Slack messages (Slack search.messages). The query "
            "supports Slack search syntax (e.g. 'in:#general from:@user "
            "project update'). Results are paginated by page number, not "
            "cursor. Requires the user to have connected their Slack "
            "account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (supports Slack search syntax).",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results per page (max 100, default 20).",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, 1-indexed (default 1).",
                },
                "sort": {
                    "type": "string",
                    "description": "Sort by 'score' (relevance, default) or 'timestamp'.",
                },
                "sort_dir": {
                    "type": "string",
                    "description": "Sort direction: 'asc' or 'desc' (default 'desc').",
                },
                "team_id": {
                    "type": "string",
                    "description": (
                        "Workspace team ID (T-prefixed, from list_slack_teams). "
                        "Omit to use the user's default workspace."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Search Slack for updates'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    handler=_tool_search_slack_messages,
    requires_service="slack",
)

GET_SLACK_USER_INFO_TOOL = PluginTool(
    spec={
        "name": "get_slack_user_info",
        "description": (
            "Get information about a Slack user by user ID (Slack "
            "users.info): name, display name, email (when visible), "
            "timezone, and status. User IDs start with 'U' (e.g. from "
            "message 'user' fields). Requires the user to have connected "
            "their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Slack user ID (e.g. 'U1234567890').",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Look up Slack user'."
                    ),
                },
            },
            "required": ["user_id"],
        },
    },
    handler=_tool_get_slack_user_info,
    requires_service="slack",
)

LIST_SLACK_USERS_TOOL = PluginTool(
    spec={
        "name": "list_slack_users",
        "description": (
            "List the users in a Slack workspace (Slack users.list). "
            "Paginate with the cursor from response_metadata.next_cursor. "
            "Requires the user to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (1-50, default 50).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response's response_metadata.next_cursor.",
                },
                "team_id": {
                    "type": "string",
                    "description": (
                        "Workspace team ID (T-prefixed, from list_slack_teams). "
                        "Omit to use the user's default workspace."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List Slack users'."
                    ),
                },
            },
            "required": [],
        },
    },
    handler=_tool_list_slack_users,
    requires_service="slack",
)

SEND_SLACK_DM_TO_SELF_TOOL = PluginTool(
    spec={
        "name": "send_slack_dm_to_self",
        "description": (
            "Send a Slack DM to the user themselves, delivered by the "
            "Quest bot -- no approval needed. The right choice for 'send "
            "me a reminder' / 'DM me' style requests, notifications, and "
            "scheduled routines. Optionally attaches files from this "
            "conversation's workspace. "
            "Requires the user to have connected their Slack account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Message text in Slack mrkdwn format (*bold*, "
                        "_italic_, <https://url|link text>), NOT standard "
                        "Markdown."
                    ),
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional paths, relative to this conversation's "
                        "workspace, of files to attach (max 10 files, 50 MB "
                        "each). The files are uploaded to the DM alongside "
                        "the message. Project files must be copied into this "
                        "conversation's workspace first (in a project "
                        "conversation: `copy_file` from `proj://` to "
                        "`chat://`)."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'DM report to user'."
                    ),
                },
            },
            "required": ["message"],
        },
    },
    handler=_tool_send_slack_dm_to_self,
    requires_service="slack",
    mutating=True,
)


ALL_TOOLS = (
    FIND_SLACK_CHANNEL_TOOL,
    LIST_SLACK_TEAMS_TOOL,
    LIST_SLACK_CONVERSATIONS_TOOL,
    GET_SLACK_CONVERSATION_HISTORY_TOOL,
    GET_SLACK_CONVERSATION_REPLIES_TOOL,
    SEARCH_SLACK_MESSAGES_TOOL,
    GET_SLACK_USER_INFO_TOOL,
    LIST_SLACK_USERS_TOOL,
    SEND_SLACK_DM_TO_SELF_TOOL,
)

ALL_TOOL_NAMES = frozenset(t.spec["name"] for t in ALL_TOOLS)

# Every Slack tool is script-bridge invocable (parity with the pre-plugin
# core SCRIPT_TOOL_CALL_ALLOWLIST; send_slack_dm_to_self file attachments
# need conversation context and fail with a structured error from the
# bridge).
SCRIPT_TOOL_NAMES = ALL_TOOL_NAMES
