"""Shared tool dispatch for the LLM integration.

Routes tool calls to the appropriate handler function via two name-keyed
handler tables (no per-tool ``elif`` ladder):

- ``TOOL_CALL_HANDLERS``: dynamic tools invoked through the ``tool_call``
  meta tool. Keys mirror ``chat.llm.tool_schemas.TOOL_CALL_REGISTRY``;
  plugins extend both at once via :func:`register_dynamic_tool` (the
  spec/handler pair registered as one unit).
- ``DIRECT_TOOL_HANDLERS``: tools the model calls by their own name.
  Unknown direct names fall through to HTTP route dispatch
  (curl_proxy_get / curl_proxy_post).

Every handler shares one signature: ``async (ctx: ToolContext, args: dict)
-> str | (str, extra_parts)``.

Provider-agnostic: uses LLMProvider interface for file upload operations.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, Awaitable, Callable, Union

from chat.route_dispatch import execute_tool_call
from chat.llm.config import get_provider_for_model
from chat.gemini_api.authed_get import handle_authed_get, handle_authed_post
from chat.gemini_api.tool_handlers import (
    _handle_get_current_time,
    _handle_list_workspace_files,
    _handle_get_workspace_file,
    _handle_load_gmail_attachment,
    _handle_write_workspace_file,
    _handle_edit_workspace_file,
    _handle_download_drive_file,
    _handle_google_export_doc,
    _handle_archive_gmail_message,
    _handle_list_gmail_quest_labels,
    _handle_modify_gmail_labels,
    _handle_get_gmail_messages,
    _handle_list_gmail_labels,
    _handle_get_gmail_message_urls,
    _handle_create_gmail_draft,
    _handle_send_gmail_to_self,
    _handle_set_conversation_name,
    _handle_memory_search,
    _handle_memory_list,
    _handle_list_skills,
    _handle_search_skills,
    _handle_load_skills,
    _handle_list_my_skills,
    _handle_get_skill,
    _handle_list_routines,
    _handle_run_script,
    _handle_run_python,
    _handle_project_db_query,
    _handle_get_response_content,
)

# Threshold in bytes: tool results larger than this are logged to the
# dedicated large_tool_results log file.
_LARGE_RESULT_THRESHOLD = 2048

logger = logging.getLogger(__name__)
_large_tool_logger = logging.getLogger("large_tool_results")


def _get_connected_services(user: dict[str, Any]) -> dict[str, bool]:
    """Compute the user's connected-services map (used for system skill gating)."""
    from api.instructions import get_user_connected_services
    return get_user_connected_services(user)


def _log_large_tool_result(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    model: str,
    is_sub_agent: bool,
    agent_name: str | None = None,
    user_email: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Log a tool call result to the large_tool_results log if it exceeds the size threshold.

    Results are logged as one JSON object per line for easy programmatic parsing.

    Args:
        tool_name: Name of the tool that produced the result.
        args: The parameters sent to the tool.
        result: The full result string from the tool.
        model: The LLM model ID (e.g. 'gemini-3.1-pro-preview').
        is_sub_agent: Whether the caller is a sub-agent.
        agent_name: Sub-agent display name (only set when is_sub_agent=True).
        user_email: The user's email address.
        conversation_id: The conversation UUID.
    """
    result_length = len(result.encode("utf-8"))
    if result_length <= _LARGE_RESULT_THRESHOLD:
        return

    # Derive provider from model, falling back gracefully
    try:
        provider = get_provider_for_model(model) if model else "unknown"
    except ValueError:
        provider = "unknown"

    entry = {
        "timestamp": datetime.now(dt_timezone.utc).isoformat(),
        "tool_name": tool_name,
        "tool_args": args,
        "result": result,
        "result_length_bytes": result_length,
        "model": model,
        "provider": provider,
        "call_type": "sub_agent" if is_sub_agent else "top_level",
        "agent_name": agent_name,
        "user_email": user_email,
        "conversation_id": conversation_id,
    }

    _large_tool_logger.info(json.dumps(entry, default=str))


# ---------------------------------------------------------------------------
# Dispatch context and handler tables
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call context threaded to every table-dispatched tool handler."""

    app: Any
    provider: Any
    user: dict[str, Any]
    conversation_id: str | None
    timezone: str
    project_id: str | None = None
    model: str = ""
    is_sub_agent: bool = False
    agent_name: str | None = None
    is_public: bool = False
    # One-shot inference API run (origin="inference_api"): the
    # approval-free mutating tools are refused -- see
    # chat.llm.tool_schemas.mutating_tool_call_tools().
    is_inference_api: bool = False


ToolResult = Union[str, tuple[str, list]]
ToolHandler = Callable[[ToolContext, dict], Awaitable[ToolResult]]


def _as_bool(raw: Any) -> bool:
    """Coerce a loosely-typed boolean tool argument (True or 'true')."""
    return raw is True or (isinstance(raw, str) and raw.lower() == "true")


def _split_result(out: ToolResult) -> tuple[str, list]:
    if isinstance(out, tuple):
        return out
    return out, []


async def _tool_get_current_time(ctx: ToolContext, args: dict) -> str:
    return _handle_get_current_time(ctx.timezone)


async def _tool_list_workspace_files(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_workspace_files(
        ctx.user["id"], ctx.conversation_id, project_id=ctx.project_id,
    )


async def _tool_get_workspace_file(ctx: ToolContext, args: dict) -> tuple[str, list]:
    return await _handle_get_workspace_file(
        ctx.provider, ctx.user["id"], ctx.conversation_id, args.get("path", ""),
        project_id=ctx.project_id, model=ctx.model,
    )


async def _tool_write_workspace_file(ctx: ToolContext, args: dict) -> str:
    return await _handle_write_workspace_file(
        ctx.user["id"], ctx.conversation_id, args.get("path", ""),
        args.get("content", ""), project_id=ctx.project_id,
    )


async def _tool_edit_workspace_file(ctx: ToolContext, args: dict) -> str:
    return await _handle_edit_workspace_file(
        ctx.user["id"], ctx.conversation_id, args.get("path", ""),
        args.get("old_string", ""), args.get("new_string", ""),
        replace_all=_as_bool(args.get("replace_all")),
        project_id=ctx.project_id,
    )


async def _tool_memory_search(ctx: ToolContext, args: dict) -> str:
    return await _handle_memory_search(ctx.user["id"], args.get("query", ""))


async def _tool_memory_list(ctx: ToolContext, args: dict) -> str:
    return await _handle_memory_list(ctx.user["id"])


async def _tool_wait_for_handles_stub(ctx: ToolContext, args: dict) -> str:
    # Top-level wait_for_handles rides on tool_call and is intercepted by the
    # conversation loop (chat/gemini_api/turn_tools.py) before dispatch ever
    # sees it; reaching this handler means a sub-agent tried to block.
    return json.dumps({
        "error": "wait_for_handles is only available to the top-level agent. Sub-agents cannot block on user-input handles."
    })


async def _tool_download_drive_file(ctx: ToolContext, args: dict) -> str:
    return await _handle_download_drive_file(
        ctx.user, ctx.conversation_id, args.get("file_id", ""),
        filename=args.get("filename"), project_id=ctx.project_id,
    )


async def _tool_google_export_doc(ctx: ToolContext, args: dict) -> str:
    return await _handle_google_export_doc(
        ctx.user, ctx.conversation_id, args.get("document_id", ""),
        args.get("format", ""),
        filename=args.get("filename"), project_id=ctx.project_id,
    )


async def _tool_authed_get(ctx: ToolContext, args: dict) -> str:
    return await handle_authed_get(
        args.get("url", ""), headers=args.get("headers"), user=ctx.user,
        force_large_response=_as_bool(args.get("force_large_response")),
        conversation_id=ctx.conversation_id,
        project_id=ctx.project_id,
        output_file=args.get("output_file"),
    )


async def _tool_authed_post(ctx: ToolContext, args: dict) -> str:
    return await handle_authed_post(
        args.get("url", ""), body=args.get("body"),
        headers=args.get("headers"), user=ctx.user,
        force_large_response=_as_bool(args.get("force_large_response")),
        conversation_id=ctx.conversation_id,
        project_id=ctx.project_id,
        output_file=args.get("output_file"),
    )


async def _tool_get_response_content(ctx: ToolContext, args: dict) -> str:
    return await _handle_get_response_content(
        ctx.conversation_id, args.get("hash", ""),
        int(args.get("offset", 0)), int(args.get("length", 0)),
    )


async def _tool_archive_gmail_message(ctx: ToolContext, args: dict) -> str:
    return await _handle_archive_gmail_message(
        ctx.user, args.get("message_id", ""),
        add_labels=args.get("add_labels") or [],
    )


async def _tool_list_gmail_quest_labels(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_gmail_quest_labels(ctx.user)


async def _tool_modify_gmail_labels(ctx: ToolContext, args: dict) -> str:
    return await _handle_modify_gmail_labels(
        ctx.user,
        args.get("message_ids") or [],
        args.get("add_labels") or [],
        args.get("remove_labels") or [],
    )


async def _tool_get_gmail_messages(ctx: ToolContext, args: dict) -> str:
    return await _handle_get_gmail_messages(
        ctx.user, ctx.conversation_id, args.get("message_ids") or [],
        include_html=args.get("include_html", False),
        include_urls=args.get("include_urls", False),
    )


async def _tool_list_gmail_labels(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_gmail_labels(ctx.user, label_id=args.get("label_id"))


async def _tool_get_gmail_message_urls(ctx: ToolContext, args: dict) -> str:
    return await _handle_get_gmail_message_urls(
        ctx.user, ctx.conversation_id, args.get("message_id", ""),
        identifiers=args.get("identifiers"),
    )


async def _tool_create_gmail_draft(ctx: ToolContext, args: dict) -> str:
    return await _handle_create_gmail_draft(ctx.user, ctx.conversation_id, args)


async def _tool_send_gmail_to_self(ctx: ToolContext, args: dict) -> str:
    return await _handle_send_gmail_to_self(
        ctx.user, args.get("subject"), args.get("body_md"),
        conversation_id=ctx.conversation_id,
        attachments=args.get("attachments"),
    )


async def _tool_set_conversation_name(ctx: ToolContext, args: dict) -> str:
    return await _handle_set_conversation_name(
        ctx.user["id"], ctx.conversation_id, args.get("name", ""),
    )


async def _tool_project_db_query(ctx: ToolContext, args: dict) -> str:
    return await _handle_project_db_query(ctx.project_id, args.get("query", ""))


async def _tool_load_gmail_attachment(ctx: ToolContext, args: dict) -> tuple[str, list]:
    return await _handle_load_gmail_attachment(
        ctx.provider, ctx.user, args.get("message_id", ""),
        args.get("attachment_id", ""),
        filename=args.get("filename", "attachment"),
        mime_type=args.get("mime_type", "application/octet-stream"),
        model=ctx.model,
    )


async def _tool_list_skills(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_skills(
        ctx.user["id"],
        connected_services=_get_connected_services(ctx.user),
        has_project=bool(ctx.project_id),
    )


async def _tool_search_skills(ctx: ToolContext, args: dict) -> str:
    return await _handle_search_skills(
        ctx.user["id"], args.get("keyword", ""),
        connected_services=_get_connected_services(ctx.user),
        has_project=bool(ctx.project_id),
    )


async def _tool_load_skills(ctx: ToolContext, args: dict) -> str:
    from chat.gemini_api.constants import get_proxy_base_url
    return await _handle_load_skills(
        ctx.user["id"], args.get("skill_ids", []),
        connected_services=_get_connected_services(ctx.user),
        has_project=bool(ctx.project_id),
        base_url=get_proxy_base_url(),
        api_key=ctx.user.get("api_key", ""),
        conversation_id=ctx.conversation_id,
    )


async def _tool_list_my_skills(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_my_skills(
        ctx.user["id"], project_id=ctx.project_id,
        exclude_own=args.get("exclude_own", False),
        exclude_shared=args.get("exclude_shared", False),
        exclude_public=args.get("exclude_public", False),
        exclude_project=args.get("exclude_project", False),
    )


async def _tool_get_skill(ctx: ToolContext, args: dict) -> str:
    return await _handle_get_skill(
        ctx.user["id"], args.get("skill_id", ""), project_id=ctx.project_id,
        conversation_id=ctx.conversation_id,
    )


async def _tool_list_routines(ctx: ToolContext, args: dict) -> str:
    return await _handle_list_routines(ctx.user["id"], project_id=ctx.project_id)


def _script_timeout(args: dict) -> int | None:
    timeout = args.get("timeout")
    return int(timeout) if timeout is not None else None


async def _tool_run_script(ctx: ToolContext, args: dict) -> str:
    return await _handle_run_script(
        ctx.user["id"], ctx.conversation_id, args.get("path", ""),
        args=args.get("args", ""), timeout=_script_timeout(args),
        project_id=ctx.project_id,
        public=ctx.is_public,
        block_mutating_tools=ctx.is_inference_api,
    )


async def _tool_run_python(ctx: ToolContext, args: dict) -> str:
    return await _handle_run_python(
        ctx.user["id"], ctx.conversation_id, args.get("script", ""),
        args=args.get("args", ""), timeout=_script_timeout(args),
        project_id=ctx.project_id,
        public=ctx.is_public,
        block_mutating_tools=ctx.is_inference_api,
    )


# Dynamic tools invoked through the tool_call meta tool. Keys must stay in
# sync with chat.llm.tool_schemas.TOOL_CALL_REGISTRY (asserted by
# tests/test_tool_dispatch_table.py); plugin tools are added to both via
# register_tool_call_handler().
TOOL_CALL_HANDLERS: dict[str, ToolHandler] = {
    "get_current_time": _tool_get_current_time,
    # The nine Slack tools are plugin-registered (plugins/slack) via
    # register_tool_call_handler().
    "list_workspace_files": _tool_list_workspace_files,
    "get_workspace_file": _tool_get_workspace_file,
    "write_workspace_file": _tool_write_workspace_file,
    "edit_workspace_file": _tool_edit_workspace_file,
    "memory_search": _tool_memory_search,
    "memory_list": _tool_memory_list,
    "wait_for_handles": _tool_wait_for_handles_stub,
    "download_drive_file": _tool_download_drive_file,
    "google_export_doc": _tool_google_export_doc,
    "archive_gmail_message": _tool_archive_gmail_message,
    "list_gmail_quest_labels": _tool_list_gmail_quest_labels,
    "modify_gmail_labels": _tool_modify_gmail_labels,
    "get_gmail_messages": _tool_get_gmail_messages,
    "list_gmail_labels": _tool_list_gmail_labels,
    "get_gmail_message_urls": _tool_get_gmail_message_urls,
    "create_gmail_draft": _tool_create_gmail_draft,
    "send_gmail_to_self": _tool_send_gmail_to_self,
    "set_conversation_name": _tool_set_conversation_name,
    "project_db_query": _tool_project_db_query,
    "authed_get": _tool_authed_get,
    "authed_post": _tool_authed_post,
    "get_response_content": _tool_get_response_content,
}

# Tools the model calls by their own (top-level) name. Anything not in this
# table falls through to HTTP route dispatch (curl_proxy_get /
# curl_proxy_post). agent_task* / create_action_request / the suspend tools
# never reach dispatch -- the conversation loop handles them.
DIRECT_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "get_current_time": _tool_get_current_time,
    "list_workspace_files": _tool_list_workspace_files,
    "get_workspace_file": _tool_get_workspace_file,
    "load_gmail_attachment": _tool_load_gmail_attachment,
    "write_workspace_file": _tool_write_workspace_file,
    "memory_search": _tool_memory_search,
    "memory_list": _tool_memory_list,
    "list_skills": _tool_list_skills,
    "search_skills": _tool_search_skills,
    "load_skills": _tool_load_skills,
    "list_my_skills": _tool_list_my_skills,
    "get_skill": _tool_get_skill,
    "list_routines": _tool_list_routines,
    "run_script": _tool_run_script,
    "run_python": _tool_run_python,
    "project_db_query": _tool_project_db_query,
}


def register_tool_call_handler(name: str, handler: ToolHandler) -> None:
    """Register a plugin-contributed dynamic tool handler.

    The name must already be present in TOOL_CALL_REGISTRY (register the
    spec first via chat.llm.tool_schemas.register_tool_call_tool) so the
    two tables can never drift for plugin tools.
    """
    from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
    if name in TOOL_CALL_HANDLERS:
        raise ValueError(f"Tool handler already registered: {name!r}")
    if name not in TOOL_CALL_REGISTRY:
        raise ValueError(
            f"Tool {name!r} has no TOOL_CALL_REGISTRY spec; register the "
            "spec before the handler"
        )
    TOOL_CALL_HANDLERS[name] = handler


def register_dynamic_tool(spec: dict, handler: ToolHandler) -> None:
    """Register a dynamic tool's spec AND handler as one unit.

    The single registration surface for tool_call-routed tools contributed
    outside the core tables (the plugin loader): the spec lands in
    ``TOOL_CALL_REGISTRY`` and the handler in ``TOOL_CALL_HANDLERS``, and a
    failure on the handler side removes the just-added spec again, so the
    two tables can never hold a spec without its handler. Raises ValueError
    (duplicate name, malformed spec, non-callable handler) with both tables
    unchanged.
    """
    from chat.llm.tool_schemas import (
        PLUGIN_TOOL_NAMES,
        TOOL_CALL_REGISTRY,
        register_tool_call_tool,
    )
    if not callable(handler):
        raise ValueError(
            f"Tool {spec.get('name', '')!r} handler is not callable"
        )
    register_tool_call_tool(spec)
    try:
        register_tool_call_handler(spec["name"], handler)
    except BaseException:
        # Undo only the spec this call added; whatever blocked the handler
        # step (e.g. a pre-existing handler entry) is not ours to remove.
        TOOL_CALL_REGISTRY.pop(spec["name"], None)
        PLUGIN_TOOL_NAMES.discard(spec["name"])
        raise


def unregister_dynamic_tool(name: str) -> None:
    """Remove a dynamic tool registered via :func:`register_dynamic_tool`.

    Drops the spec, the handler, and the plugin-tool classification entry;
    a name that is not registered is a no-op. Only for names added through
    :func:`register_dynamic_tool` -- never call it with a core tool name.
    """
    from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES, TOOL_CALL_REGISTRY
    TOOL_CALL_REGISTRY.pop(name, None)
    TOOL_CALL_HANDLERS.pop(name, None)
    PLUGIN_TOOL_NAMES.discard(name)


async def _dispatch_tool_call(
    app,
    provider,
    user: dict[str, Any],
    conversation_id: str,
    timezone: str,
    tool_name: str,
    args: dict[str, Any],
    project_id: str | None = None,
    model: str = "",
    is_sub_agent: bool = False,
    agent_name: str | None = None,
    is_public: bool = False,
    is_inference_api: bool = False,
) -> tuple[str, list]:
    """Dispatch a tool call to the appropriate handler.

    Handles all local tools (get_current_time, list_workspace_files,
    get_workspace_file, write_workspace_file, memory_search, memory_list)
    and falls back to route dispatch for HTTP tools
    (curl_proxy_get, curl_proxy_post).

    This function does NOT handle agent-specific tools (agent_task,
    agent_task_parallel, agent_task_response) -- those are handled by
    the caller since they require loop-specific context.

    An unexpected exception from any handler is converted into a
    structured ``{"error": ...}`` result so a single failing tool call is
    surfaced to the model (which can retry or adapt) instead of aborting
    the entire conversation run.

    Args:
        app: FastAPI app instance (for route dispatch).
        provider: LLMProvider instance (for file uploads).
        user: Authenticated user dict.
        conversation_id: Conversation UUID.
        timezone: User's IANA timezone string.
        tool_name: Name of the tool to execute.
        args: Tool call arguments (intent_message already popped).
        project_id: Optional project UUID for project-aware workspace resolution.
        model: The LLM model ID string (for large result logging).
        is_sub_agent: Whether the caller is a sub-agent (for large result logging).
        agent_name: Sub-agent display name (for large result logging).
        is_public: Whether the conversation belongs to a public project.
            When True, only tools in the public allowlist execute (hard
            reject, not just prompt trimming), the sandbox runs with the
            internet-enabled public profile, and the user's API key is
            never injected into containers.
        is_inference_api: Whether the conversation is a one-shot inference
            API run (origin="inference_api"). When True, the approval-free
            mutating dynamic tools (``mutating_tool_call_tools()``) and the
            mutating internal proxy paths are hard-rejected, and sandbox
            containers get a lease that refuses them on the bridge too.

    Returns:
        Tuple of (result_string, extra_parts) where extra_parts is a
        list of provider-specific content parts (empty for most tools).
    """
    try:
        return await _dispatch_tool_call_inner(
            app, provider, user, conversation_id, timezone,
            tool_name, args, project_id=project_id, model=model,
            is_sub_agent=is_sub_agent, agent_name=agent_name,
            is_public=is_public, is_inference_api=is_inference_api,
        )
    except Exception as exc:
        label = tool_name
        if tool_name == "tool_call" and args.get("tool_name"):
            label = f"tool_call:{args['tool_name']}"
        logger.exception(
            "Tool '%s' raised (conversation=%s, user=%s)",
            label, conversation_id, user.get("email"),
        )
        return json.dumps({"error": f"Tool '{label}' failed: {exc}"}), []


async def _dispatch_tool_call_inner(
    app,
    provider,
    user: dict[str, Any],
    conversation_id: str,
    timezone: str,
    tool_name: str,
    args: dict[str, Any],
    project_id: str | None = None,
    model: str = "",
    is_sub_agent: bool = False,
    agent_name: str | None = None,
    is_public: bool = False,
    is_inference_api: bool = False,
) -> tuple[str, list]:
    """Un-guarded dispatch body; see ``_dispatch_tool_call``."""
    extra_parts = []
    log_tool_name = tool_name

    ctx = ToolContext(
        app=app,
        provider=provider,
        user=user,
        conversation_id=conversation_id,
        timezone=timezone,
        project_id=project_id,
        model=model,
        is_sub_agent=is_sub_agent,
        agent_name=agent_name,
        is_public=is_public,
        is_inference_api=is_inference_api,
    )

    # Public-project enforcement. This is the security boundary, not the
    # trimmed PUBLIC_TOOLS schema or the filtered prompt: anything outside
    # the allowlist is rejected here regardless of what the model asked for.
    # Plugin-registered tools are blocked automatically -- the allowlist is
    # core-only and plugins cannot extend it.
    if is_public:
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST
        if tool_name == "tool_call":
            requested = args.get("tool_name", "")
            if requested and requested not in PUBLIC_TOOL_CALL_ALLOWLIST:
                available = ", ".join(sorted(PUBLIC_TOOL_CALL_ALLOWLIST))
                return json.dumps({
                    "error": (
                        f"Tool '{requested}' is not available in public-project "
                        f"conversations. Available dynamic tools: {available}."
                    ),
                }), []
        elif tool_name not in ("run_script", "run_python") and \
                tool_name not in PUBLIC_TOOL_CALL_ALLOWLIST:
            return json.dumps({
                "error": (
                    f"Tool '{tool_name}' is not available in public-project "
                    "conversations."
                ),
            }), []

    # Inference-run enforcement. Inference API calls must not change
    # anything: the mutating dynamic tools run without an approval card
    # (there is nobody to approve one), so they are refused here regardless
    # of what the prompt advertised. Workspace writes stay allowed -- the
    # run's conversation owns a fresh workspace. Mutating proxy paths are
    # refused by execute_tool_call (block_mutating) below.
    if is_inference_api and tool_name == "tool_call":
        from chat.llm.tool_schemas import mutating_tool_call_tools
        requested = args.get("tool_name", "")
        if requested and requested in mutating_tool_call_tools():
            return json.dumps({
                "error": (
                    f"Tool '{requested}' is not available in inference API "
                    "runs: it changes external state and inference runs "
                    "are read-only. Report what the caller should do "
                    "instead in the final response."
                ),
            }), []

    if tool_name == "tool_call":
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        inner_tool_name = args.get("tool_name", "")
        inner_args = args.get("arguments", {})

        if not inner_tool_name:
            result = json.dumps({"error": "tool_call requires a 'tool_name' parameter."})
        elif inner_tool_name not in TOOL_CALL_REGISTRY:
            available = ", ".join(sorted(TOOL_CALL_REGISTRY.keys()))
            result = json.dumps({"error": f"Unknown tool: '{inner_tool_name}'. Available dynamic tools: {available}."})
        elif not isinstance(inner_args, dict):
            result = json.dumps({"error": "tool_call 'arguments' must be a JSON object."})
        else:
            # Pop intent_message from inner_args if present (same pattern as caller)
            inner_args.pop("intent_message", None)
            log_tool_name = f"tool_call:{inner_tool_name}"
            handler = TOOL_CALL_HANDLERS.get(inner_tool_name)
            if handler is None:
                # Should not reach here if TOOL_CALL_REGISTRY is in sync
                result = json.dumps({"error": f"tool_call: no handler for '{inner_tool_name}'."})
            else:
                try:
                    result, extra_parts = _split_result(await handler(ctx, inner_args))
                except Exception as exc:
                    result = json.dumps({"error": f"tool_call '{inner_tool_name}' failed: {exc}"})
    elif tool_name in DIRECT_TOOL_HANDLERS:
        result, extra_parts = _split_result(
            await DIRECT_TOOL_HANDLERS[tool_name](ctx, args)
        )
    else:
        # HTTP tools (curl_proxy_get, curl_proxy_post) via route dispatch
        result, notices = await execute_tool_call(
            app, user, tool_name, args, conversation_id=conversation_id,
            block_mutating=is_inference_api,
        )
        for notice in notices:
            extra_parts.append(provider.make_text_part(notice))

    _log_large_tool_result(
        log_tool_name, args, result, model, is_sub_agent,
        agent_name=agent_name,
        user_email=user.get("email"),
        conversation_id=conversation_id,
    )

    return result, extra_parts
