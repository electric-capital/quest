"""Telegram read tools (plugin module).

The four ``telegram_*`` dynamic tools -- ``telegram_get_me``,
``telegram_list_dialogs``, ``telegram_get_messages``,
``telegram_list_contacts`` -- wrap the read functions in
plugins/telegram/upstream.py. The LLM reaches them via ``tool_call`` and
sandbox scripts via the ``POST /api/tool-call`` bridge (the manifest
opts all four into ``script_tool_allowlist``); there are no HTTP routes
and the ``/api/telegram`` prefix stays blocked in chat/route_dispatch.py.
Sending is the approval-gated ``send_telegram_message`` action request
(plugins/telegram/handlers.py).
"""

from __future__ import annotations

import json

from config.plugin_tool_helpers import intent_param as _intent
from config.plugin_types import PluginTool

from plugins.telegram.upstream import get_me, get_messages, list_contacts, list_dialogs


async def _run_telegram_call(coro) -> str:
    """Await a read coroutine and render a tool result.

    Serializes the dict result as JSON and converts HTTPException -- the
    read functions' error channel (``telegram_not_connected``,
    ``telegram_session_expired``, ``telegram_api_error``, ``invalid_date``)
    -- to the structured ``{"error": ...}`` JSON the other dynamic tools
    return.
    """
    from fastapi import HTTPException

    try:
        result = await coro
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            payload = dict(detail)
            payload.setdefault("error", f"telegram_http_{e.status_code}")
        else:
            payload = {"error": f"telegram_http_{e.status_code}", "message": str(detail)}
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": f"Telegram request failed: {e}"})

    return json.dumps(result, default=str)


def _arg_int(value, default: int) -> int:
    """Coerce a model-supplied optional integer argument."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _arg_bool(value) -> bool:
    """Coerce a model-supplied boolean-ish tool argument to bool."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# ---------------------------------------------------------------------------
# Handlers ((ctx, args) shape -- see config/plugin_types.py PluginTool)
# ---------------------------------------------------------------------------

async def _tool_get_me(ctx, args: dict) -> str:
    return await _run_telegram_call(get_me(ctx.user))


async def _tool_list_dialogs(ctx, args: dict) -> str:
    return await _run_telegram_call(
        list_dialogs(ctx.user, limit=_arg_int(args.get("limit"), 20)),
    )


async def _tool_get_messages(ctx, args: dict) -> str:
    dialog_id = args.get("dialog_id")
    # bool subclasses int; reject it explicitly so True never becomes dialog 1.
    if dialog_id is None or dialog_id == "" or isinstance(dialog_id, bool):
        return json.dumps({"error": "dialog_id is required (numeric Telegram dialog id)."})
    try:
        dialog_id_int = int(str(dialog_id).strip())
    except (TypeError, ValueError):
        return json.dumps({
            "error": (
                f"dialog_id must be a numeric Telegram dialog id, got "
                f"{str(dialog_id)[:100]!r}. Look it up with telegram_list_dialogs."
            ),
        })

    offset_date = args.get("offset_date")
    offset_date_str = str(offset_date).strip() if offset_date else None
    return await _run_telegram_call(get_messages(
        ctx.user,
        dialog_id_int,
        limit=_arg_int(args.get("limit"), 50),
        offset_id=_arg_int(args.get("offset_id"), 0),
        offset_date=offset_date_str or None,
        reverse=_arg_bool(args.get("reverse", False)),
    ))


async def _tool_list_contacts(ctx, args: dict) -> str:
    return await _run_telegram_call(list_contacts(ctx.user))


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------

GET_ME_TOOL = PluginTool(
    spec={
        "name": "telegram_get_me",
        "description": (
            "Get the connected Telegram account's own profile (id, username, "
            "first/last name, phone, is_premium). Requires Telegram to be "
            "connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {"intent_message": _intent("Check Telegram account")},
            "required": [],
        },
    },
    handler=_tool_get_me,
    requires_service="telegram",
)

LIST_DIALOGS_TOOL = PluginTool(
    spec={
        "name": "telegram_list_dialogs",
        "description": (
            "List the user's Telegram dialogs (chats, groups, channels) as "
            "{dialogs: [{id, name, is_user, is_group, is_channel, "
            "unread_count, last_message_date}]}, most recent first. The "
            "numeric id is what telegram_get_messages and the "
            "send_telegram_message action request take as dialog_id. "
            "Requires Telegram to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of dialogs to return (default 20).",
                },
                "intent_message": _intent("List Telegram chats"),
            },
            "required": [],
        },
    },
    handler=_tool_list_dialogs,
    requires_service="telegram",
)

GET_MESSAGES_TOOL = PluginTool(
    spec={
        "name": "telegram_get_messages",
        "description": (
            "Fetch messages from one Telegram dialog as {messages: [{id, "
            "date, text, sender_id, is_outgoing, reply_to_msg_id, "
            "has_media}]}, newest first unless reverse is true. Page with "
            "offset_id (the id of the last message received) or "
            "offset_date. Requires Telegram to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dialog_id": {
                    "type": "integer",
                    "description": (
                        "Numeric Telegram dialog id (user, group, or channel) "
                        "from telegram_list_dialogs."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default 50).",
                },
                "offset_id": {
                    "type": "integer",
                    "description": (
                        "Message id to start from, for pagination (default 0 "
                        "= from the newest)."
                    ),
                },
                "offset_date": {
                    "type": "string",
                    "description": (
                        "ISO8601 datetime (e.g. 2024-01-15T10:30:00Z); only "
                        "messages before this time are returned (exclusive)."
                    ),
                },
                "reverse": {
                    "type": "boolean",
                    "description": (
                        "If true, return messages oldest-first (default "
                        "false). Combine with offset_date to read forward "
                        "from a point in time."
                    ),
                },
                "intent_message": _intent("Read Telegram chat"),
            },
            "required": ["dialog_id"],
        },
    },
    handler=_tool_get_messages,
    requires_service="telegram",
)

LIST_CONTACTS_TOOL = PluginTool(
    spec={
        "name": "telegram_list_contacts",
        "description": (
            "List the user's Telegram contacts as {contacts: [{id, username, "
            "first_name, last_name, phone}]}. A contact's id is also its "
            "dialog_id for direct messages. Requires Telegram to be "
            "connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {"intent_message": _intent("List Telegram contacts")},
            "required": [],
        },
    },
    handler=_tool_list_contacts,
    requires_service="telegram",
)

TELEGRAM_TOOLS: tuple[PluginTool, ...] = (
    GET_ME_TOOL, LIST_DIALOGS_TOOL, GET_MESSAGES_TOOL, LIST_CONTACTS_TOOL,
)
TELEGRAM_TOOL_NAMES: frozenset[str] = frozenset(t.spec["name"] for t in TELEGRAM_TOOLS)
