"""Main conversation loop for the LLM integration.

Contains the run_conversation_turn() entry point called from the WebSocket handler.
Uses the LLMProvider interface so any backend (Gemini, Anthropic) can be used.
"""

import asyncio
import json
import time
import traceback
import logging
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Awaitable
from zoneinfo import ZoneInfo, available_timezones

from chat.storage import utc_timestamp, ChatStorage
from config.server_config import load_server_config
from chat._flush_helper import FLUSH_EVENT_TYPES
from db.llm_call_store import record_api_call
from db.models import ApiCallType, ToolWaitHandleStatus
from db import tool_wait_handle_store
from chat.gemini_api.constants import _DEPRECATED_MODELS, get_proxy_base_url
from chat.gemini_api.session import (
    get_or_create_chat,
    _active_chats,
)
from chat.gemini_api.history import (
    _save_sdk_history,
    _load_sdk_history,
)
from chat.gemini_api.system_prompt import get_system_prompt
from chat.gemini_api.tool_dispatch import (
    _dispatch_tool_call,
    _log_large_tool_result,
    invalid_tool_arguments_result,
)
from chat.gemini_api.run_context import RunContext
from chat.gemini_api.turn_tools import (
    _PUBLIC_BLOCKED_LOOP_TOOLS,
    _WAIT_FOR_HANDLES_MAX_IDS,
    TURN_TOOL_HANDLERS,
    FinishInferenceResponse,
    SuspendForActionRequest,
    SuspendForSlackReply,
    SuspendForWaitHandles,
    ToolCall,
    TurnState,
    _build_wait_for_handles_result,
    _emit_durable,
    public_blocked_result,
    registry_key_for,
)
from chat.gemini_api.usage import UsageAccumulator
from chat.llm.config import (
    get_provider_for_model,
    get_provider_instance,
    get_backend_for_model,
    get_model_display_name,
    model_instance_id,
)

logger = logging.getLogger(__name__)


async def _build_user_message_with_attachments(
    provider,
    wrapped_message: str,
    attachment_specs: list[dict],
    workspace_path,
    model: str = "",
) -> Any:
    """Build a provider-appropriate multimodal user-turn payload.

    For each attachment spec ``{workspace_path, mime_type, filename, ...}``:
    1. Resolve the absolute file path under the conversation workspace.
    2. Call ``provider.upload_file`` to obtain a provider-specific ref.
    3. Convert that ref into a provider-native content part via
       ``provider.make_file_part``.

    Anthropic's path is sync (the ref is already the content block dict);
    Gemini's path waits for the File API to mark the upload ACTIVE.

    When ``upload_file`` returns None (image too large for Anthropic, or
    Gemini File API failure), the failed part is replaced by a text note
    so the model knows the user intended to attach an image and where to
    find it in the workspace.

    Returns a list suitable for ``send_message_stream``: for Anthropic, a
    list of content-block dicts (text + image blocks); for Gemini, a list
    of ``types.Part`` parts (text + file parts).
    """
    parts: list[Any] = [provider.make_text_part(wrapped_message)]
    for spec in attachment_specs:
        rel = spec.get("workspace_path") or ""
        mime_type = spec.get("mime_type") or ""
        filename = spec.get("filename") or rel.rsplit("/", 1)[-1]
        # workspace_path on disk is conversation_dir/workspace/<rel>.
        abs_path = (workspace_path / "workspace" / rel).resolve()
        try:
            abs_path.relative_to((workspace_path / "workspace").resolve())
        except ValueError:
            # Path escape attempt -- skip with a text note and continue.
            parts.append(provider.make_text_part(
                f"Pasted image '{filename}' could not be attached "
                f"(invalid path)."
            ))
            continue
        if not abs_path.exists() or not abs_path.is_file():
            parts.append(provider.make_text_part(
                f"Pasted image '{filename}' could not be attached "
                f"(file not found at /{rel})."
            ))
            continue
        try:
            file_ref = await provider.upload_file(
                file_path=str(abs_path),
                mime_type=mime_type,
                display_name=filename,
                model=model,
            )
        except Exception:
            logger.warning(
                "Composer attachment upload raised (path=%s, mime=%s)",
                abs_path, mime_type, exc_info=True,
            )
            file_ref = None
        if file_ref is None:
            parts.append(provider.make_text_part(
                f"Pasted image '{filename}' could not be attached for "
                f"inline analysis. The file is still available in the "
                f"workspace at /{rel}."
            ))
            continue
        try:
            parts.append(provider.make_file_part(file_ref))
        except Exception:
            logger.warning(
                "Composer attachment make_file_part raised (mime=%s)",
                mime_type, exc_info=True,
            )
            parts.append(provider.make_text_part(
                f"Pasted image '{filename}' could not be attached for "
                f"inline analysis. The file is still available in the "
                f"workspace at /{rel}."
            ))
    return parts


def _wrap_message_with_metadata(
    message: str,
    user_timezone: str,
    loaded_skills_content: str = "",
    attached_filenames: list[str] | None = None,
) -> str:
    """Wrap a user message in an XML envelope containing UTC and local time metadata.

    The wrapped message is sent to the LLM so it has accurate time context.
    The original raw message is stored in chat history separately (by the caller).

    Args:
        message: The raw user message string.
        user_timezone: IANA timezone string (e.g. 'America/New_York'). Falls back
            to UTC if the timezone is invalid or empty.
        loaded_skills_content: Optional skill content to inject as a
            ``<conversation_skills_loaded>`` section between metadata and message.
        attached_filenames: Optional list of workspace-relative filenames the user
            attached to THIS message (uploaded to the workspace before the run).
            When present and non-empty, they are listed inside the
            ``<message_metadata>`` block so the model knows what was just attached
            and can read them via its workspace file tools. This is a per-message,
            this-turn-only hint -- NOT a workspace enumeration; later turns omit it.

    Returns:
        A string with XML metadata header followed by the original message.
    """
    now_utc = datetime.now(dt_timezone.utc)
    utc_readable = now_utc.strftime("%A, %B %d, %Y %H:%M:%S UTC")

    if user_timezone and user_timezone in available_timezones():
        tz = ZoneInfo(user_timezone)
        now_local = now_utc.astimezone(tz)
        local_readable = now_local.strftime("%A, %B %d, %Y %I:%M:%S %p %Z")
        local_line = f"User Local Time: {local_readable} ({user_timezone})"
    else:
        local_line = f"User Local Time: (unknown timezone)"

    metadata_lines = [
        "<message_metadata>",
        f"UTC Time: {utc_readable}",
        local_line,
    ]
    # Only emit the attached-files line on the turn the user actually attached
    # files (caller-supplied names; no workspace scan). Filter to non-empty
    # strings so a malformed/empty entry never produces a dangling line.
    if attached_filenames:
        clean_names = [n for n in attached_filenames if isinstance(n, str) and n]
        if clean_names:
            metadata_lines.append(
                "Files attached to this message (in the workspace, readable via "
                "your file tools): " + ", ".join(clean_names)
            )
    metadata_lines.append("</message_metadata>")

    parts = [
        "\n".join(metadata_lines),
    ]

    if loaded_skills_content:
        parts.append(
            f"<conversation_skills_loaded>\n"
            f"The user has loaded the following skills into this conversation. "
            f"These skills apply to the ENTIRE conversation from this point forward, "
            f"not just this message. Treat them as persistent instructions for all "
            f"subsequent messages.\n\n"
            f"{loaded_skills_content}\n"
            f"</conversation_skills_loaded>"
        )

    parts.append(
        f"<message>\n"
        f"{message}\n"
        f"</message>"
    )

    return "\n\n".join(parts)


def _flush_partial_text(
    text_parts: list[str], structured_messages: list[dict],
) -> None:
    """Append accumulated streamed text as a structured message.

    Used when a turn unwinds early (cancel or stream failure) so the
    partial response is persisted instead of silently discarded.
    """
    if not text_parts:
        return
    partial_text = "".join(text_parts)
    if partial_text.strip():
        structured_messages.append({
            "type": "text",
            "role": "assistant",
            "content": partial_text,
            "timestamp": utc_timestamp(),
        })


async def _stream_turn(
    ctx: RunContext,
    current_message: Any,
) -> tuple[list[str], list[Any]]:
    """Stream one model turn, forwarding text deltas to ``ctx.on_event``.

    Returns ``(text_parts, function_calls)`` on normal completion. On ANY
    error -- including ``CancelledError`` -- the partial streamed text is
    flushed into ``ctx.structured_messages`` before the exception
    propagates, so content the client already saw as deltas survives in
    the durable transcript.
    """
    text_parts: list[str] = []
    function_calls: list[Any] = []
    try:
        async for event in ctx.provider.send_message_stream(ctx.chat, current_message):
            if event.type == "text":
                text_parts.append(event.text)
                await ctx.on_event({"type": "text", "content": event.text})
            elif event.type == "tool_call":
                function_calls.append(event)
            elif event.type == "model_fallback":
                # The Anthropic refusal-fallback middleware switched models
                # mid-turn: the requested model's safety classifiers
                # declined and a fallback model is continuing the response.
                # Flush the pre-switch partial text first so the notice row
                # lands between the declined model's partial output and the
                # fallback's answer, then persist the marker the UI renders
                # as a system notice. "model_fallback" is in
                # FLUSH_EVENT_TYPES, so on_event flushes both messages and
                # fans out ``message_appended``.
                _flush_partial_text(text_parts, ctx.structured_messages)
                text_parts.clear()
                ctx.structured_messages.append({
                    "type": "model_fallback",
                    "role": "assistant",
                    **event.data,
                    "timestamp": utc_timestamp(),
                })
                await ctx.on_event({"type": "model_fallback", **event.data})
    except asyncio.CancelledError:
        # Flush any accumulated text_parts into structured_messages so
        # the caller (via messages_out / shared_messages) can persist the
        # partial response.
        _flush_partial_text(text_parts, ctx.structured_messages)
        raise
    except Exception:
        # Preserve whatever text already streamed before the failure so
        # it survives alongside the durable error message appended by the
        # top-level handler (or ahead of a retry).
        _flush_partial_text(text_parts, ctx.structured_messages)
        raise
    return text_parts, function_calls


def _load_persisted_tool_results(conversation_id: str) -> dict[str, str]:
    """Map ``tool_id`` -> ``tool_output`` for every ``tool_result`` row in
    the conversation's ``chat_history.json``.

    Used by the resume bucket to close dangling tool_uses whose handler
    DID run but whose result never reached the SDK session: when a batch
    of parallel tool calls contains a ``create_action_request`` suspend,
    the loop still dispatches the batch's other calls and persists their
    ``tool_result`` messages, then unwinds before ``format_tool_results``
    -- so on resume those results live only in chat_history.json. Later
    rows win on duplicate ids. Best-effort: any read failure yields ``{}``
    and the caller falls back to the ``interrupted`` marker.
    """
    try:
        data = ChatStorage.get_conversation(conversation_id) or {}
        messages = data.get("messages") or []
    except Exception:
        logger.debug(
            "Failed to load persisted tool results (conversation=%s)",
            conversation_id, exc_info=True,
        )
        return {}
    out: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") != "tool_result":
            continue
        tid = msg.get("tool_id")
        output = msg.get("tool_output")
        if isinstance(tid, str) and tid and isinstance(output, str):
            out[tid] = output
    return out


_STOPPED_NOTE_WITH_INPUT = (
    "The user pressed Stop on this request instead of approving it, so it "
    "was NOT executed and the conversation was halted. They have now sent "
    "a new message, which follows in this turn -- treat it as the current "
    "instruction. Do not re-issue the stopped request unless the new "
    "message asks for it."
)
_STOPPED_NOTE_NO_INPUT = (
    "The user pressed Stop on this request instead of approving it, so it "
    "was NOT executed and the conversation was halted. No new user message "
    "has arrived yet; end your turn without re-issuing the request."
)


def _stopped_action_request_result(response: dict, message: str) -> dict:
    """Tool result for a ``create_action_request`` tool_use whose card the
    user stopped.

    ``response`` is the wait-handle row's ``{"verdict": "stopped", ...}``
    dict written by the resolve endpoint; ``message`` is this run's user
    input (empty on a headless resume, which the resume gate normally
    prevents for stopped rows). The verdict stays ``stopped`` either way;
    only the explanatory ``note`` changes.
    """
    out = dict(response)
    out.setdefault("verdict", "stopped")
    out["note"] = _STOPPED_NOTE_WITH_INPUT if message else _STOPPED_NOTE_NO_INPUT
    return out


async def run_conversation_turn(
    app,
    user: dict[str, Any],
    message: str,
    conversation_id: str,
    timezone: str,
    model: str | None,
    on_event: Callable[[dict], Awaitable[None]],
    messages_out: list[dict] | None = None,
    guide_id: str | None = None,
    project_id: str | None = None,
    routine_id: str | None = None,
    skill_ids: list[str] | None = None,
    origin: str = "web",
    slack_context: dict | None = None,
    attachments: list[dict] | None = None,
    flags: list[str] | None = None,
    attached_filenames: list[str] | None = None,
) -> list[dict]:
    """Run an LLM conversation turn with streaming and tool execution.

    This is the main entry point called from the WebSocket handler.
    It streams the model's response, executes any tool calls via internal
    route dispatch, and returns structured messages for storage.

    Supports multiple LLM providers via the provider abstraction layer.

    Args:
        app: FastAPI app instance (for route dispatch).
        user: Authenticated user dict (has 'email', 'api_key').
        message: User's message text.
        conversation_id: Conversation ID string.
        timezone: User's timezone string.
        model: Model name string, or None to use config default.
        on_event: Async callback to send events to the WebSocket.
        messages_out: Optional shared list the caller passes in. Every
            structured message is appended here as the model produces them,
            so the caller can recover partial results after a
            ``CancelledError`` or one of the suspend sentinels (in either
            case the function unwinds without returning normally).
            Required for any caller that wants to flush ``chat_history.json``
            incrementally or persist a partial transcript on cancel.
        guide_id: Optional guide ID for this conversation.
        project_id: Optional project ID for project-aware workspace resolution.
        flags: Optional per-conversation flags (e.g. ["nested_subagents"]) read
            from the conversation row by the caller. NULL/empty means no flags.
            The ``nested_subagents`` flag enables 1st-level sub-agents to spawn
            one tier of 2nd-level sub-agents (see chat/conversation_flags.py).

    Returns:
        List of structured message dicts for storage.
    """
    structured_messages = [] if messages_out is None else messages_out
    start_time = time.time()
    tool_call_count = 0
    turn_count = 0
    # Accumulates top-level and sub-agent usage across all turns in the
    # while loop, so the final stats reflect the total across all
    # tool-call turns, not just the last turn.
    usage_acc = UsageAccumulator()

    # Collect sub-agent tool use/result events for persistence.
    # Keyed by parent_tool_id -> list of event dicts.
    # The wrapper below intercepts sub_agent_* events, stores them here,
    # and still forwards them to the real on_event for live streaming.
    _sub_agent_events: dict[str, list[dict]] = {}

    _original_on_event = on_event

    async def _capturing_on_event(event: dict) -> None:
        """Wrapper around on_event that captures sub-agent events for persistence."""
        event_type = event.get("type", "")
        # sub_agent_finished is also captured so the COMPLETED / ERRORED per-row
        # badge can be reconstructed when the conversation is reloaded.
        if event_type in ("sub_agent_tool_use", "sub_agent_tool_result", "sub_agent_finished"):
            parent_id = event.get("parent_tool_id", "")
            if parent_id:
                _sub_agent_events.setdefault(parent_id, []).append(event)
        await _original_on_event(event)
        # Boundary save: when the caller's on_event has just flushed
        # chat_history.json (its only flush point is on these event
        # types), persist the SDK session history too. This keeps
        # chat_history.json and sdk_history.json aligned without any
        # per-call-site explicit save sites. Best-effort: a save failure
        # is logged inside _save_sdk_history and swallowed so a flaky
        # disk does not abort the model loop.
        if event_type in FLUSH_EVENT_TYPES:
            try:
                _save_sdk_history(conversation_id, chat, provider)
            except Exception:
                logger.warning(
                    "Boundary SDK history save failed (conversation=%s)",
                    conversation_id, exc_info=True,
                )

    on_event = _capturing_on_event

    try:
        # Load config and resolve model
        config = load_server_config()
        model = model or config["gemini"]["model"]
        # Remap deprecated models to their replacements
        model = _DEPRECATED_MODELS.get(model, model)

        # Persist the effective model to the conversation record (no-op if already set)
        from db.conversation_store import update_conversation_model
        try:
            await update_conversation_model(conversation_id, model)
        except Exception:
            pass  # Non-fatal; model metadata is best-effort

        # Resolve the LLM provider for this model
        provider_name = get_provider_for_model(model)
        provider = get_provider_instance(provider_name, model_instance_id(model))

        # Resolve per-conversation flags. ``nested_subagents`` lets 1st-level
        # sub-agents spawn one tier of 2nd-level sub-agents (Haiku / Flash-Lite
        # only). NULL/empty flags mean no opt-in behaviors are active.
        from chat.conversation_flags import (
            is_flag_enabled, FLAG_NESTED_SUBAGENTS, FLAG_USER_SUBAGENTS,
        )
        nested_subagents = is_flag_enabled(flags, FLAG_NESTED_SUBAGENTS)
        # Gates the CALLER side of cross-user subagents: proposing
        # run_user_subagent requires the flag on this conversation AND the
        # server-global admin feature gate (config/feature_gates.py) --
        # the gate covers conversations whose flag was persisted before an
        # admin turned the feature off. The launched subagent conversation
        # itself needs no flag.
        from config.feature_gates import FEATURE_USER_SUBAGENTS, is_feature_enabled
        user_subagents_gate_open = is_feature_enabled(FEATURE_USER_SUBAGENTS)
        user_subagents_enabled = (
            user_subagents_gate_open and is_flag_enabled(flags, FLAG_USER_SUBAGENTS)
        )

        # Cross-user subagent conversations (origin="user_subagent") run as
        # the TARGET user with a restricted toolset and a dedicated system
        # prompt. Resolve the linking run row + caller identity up front;
        # both the prompt builder and the return_to_caller arm need them.
        is_user_subagent = origin == "user_subagent"
        # One-shot inference API conversations (origin="inference_api") run
        # headlessly behind POST /api/inference with a restricted toolset
        # and a dedicated no-intermediate-output system prompt; the final
        # answer travels through the return_final_response arm below.
        is_inference_api = origin == "inference_api"
        subagent_run = None
        subagent_caller = None
        if is_user_subagent:
            from db.user_subagent_run_store import get_run_by_subagent_conversation
            from db.user_store import get_user_by_id as _get_user_by_id
            subagent_run = await get_run_by_subagent_conversation(conversation_id)
            if subagent_run is None:
                raise RuntimeError(
                    f"Conversation {conversation_id} has origin="
                    "user_subagent but no user_subagent_runs row"
                )
            subagent_caller = await _get_user_by_id(subagent_run["caller_user_id"])

        # Resolve guide content (guides are deprecated and sit behind the
        # admin ``guides`` feature gate): while the gate is open for this
        # user an existing snapshot always wins so old conversations keep
        # their instructions; otherwise only an EXPLICIT guide_id resolves
        # (routine guide overrides -- the composer no longer offers guides
        # and sends none). There is no default-guide fallback anymore: a
        # new conversation without a guide_id runs with no custom prompt
        # and snapshots nothing. While the gate is CLOSED guides are fully
        # inert: no snapshot is read or written and an explicit guide_id is
        # ignored (the snapshot stays on disk, so reopening the gate
        # restores it). Cross-user subagent conversations run with NO
        # guide: the target user is not driving, and their guide could
        # leak personal instructions into a response destined for another
        # user.
        from config.feature_gates import guides_enabled_for
        from db.guide_store import get_guide
        custom_prompt = ""

        # Check if this conversation already has a snapshotted guide
        existing_snapshot = ChatStorage.get_guide_snapshot(conversation_id)
        if is_user_subagent:
            pass
        elif not guides_enabled_for(user["email"]):
            if guide_id or (existing_snapshot and existing_snapshot.get("guide_snapshot")):
                logger.info(
                    "Ignoring guide for conversation %s: guides feature disabled",
                    conversation_id,
                )
        elif existing_snapshot and existing_snapshot.get("guide_snapshot"):
            # Use the snapshotted content (preserves guide across edits/deletions)
            custom_prompt = existing_snapshot["guide_snapshot"].get("content", "")
        elif guide_id:
            # First message with an explicit guide (routine override).
            resolved_guide = await get_guide(user["id"], guide_id)
            if resolved_guide:
                custom_prompt = resolved_guide.get("content", "")
                # Snapshot the guide into the conversation
                ChatStorage.set_guide_snapshot(
                    conversation_id=conversation_id,
                    guide_id=resolved_guide["id"],
                    guide_name=resolved_guide["name"],
                    guide_content=custom_prompt,
                )

        # Load project guide if this conversation belongs to a project
        resolved_project_guide = ""
        project_data = None
        if project_id:
            from db.project_store import get_project
            project_data = await get_project(user["id"], project_id)
            if project_data:
                resolved_project_guide = project_data.get("guide", "")

        # Public-project conversations run with an internet-enabled sandbox
        # and are cut off from every internal resource (skills, memories,
        # connectors, action requests). The flag lives on the project row
        # (already fetched above -- zero extra queries) and is enforced at
        # the tool tier, the dispatch allowlist, and the loop arms below.
        is_public = bool(project_data and project_data.get("public"))

        # Server-global gate: a routine run (scheduled or one-click -- both
        # carry the conversation's routine_id) in a public project needs the
        # public_project_routines gate (the run executes unattended in the
        # internet-enabled sandbox). The scheduler and the run-start
        # endpoint already refuse while it is closed; this is the backstop
        # for every path into a routine conversation. Raising here surfaces
        # a durable error message instead of silently running.
        if is_public and routine_id:
            from config.feature_gates import public_project_routines_enabled_for
            if not public_project_routines_enabled_for(user["email"]):
                raise RuntimeError(
                    "Routines in public projects are disabled for your "
                    "account or server-wide. An admin can grant access in "
                    "Settings > Features."
                )

        # Admin Model Selection usage rules (config/model_selection.py):
        # a model can be unticked for private and/or public conversations.
        # The composer already hides disallowed models, so this only fires
        # for a conversation that was on the model before the admin
        # changed the rule (or a hand-crafted send); the durable error
        # tells the user to switch models.
        from config.model_selection import is_model_allowed
        if not is_model_allowed(model, public=is_public):
            raise RuntimeError(
                f"The model {get_model_display_name(model)} is not allowed in "
                f"{'public' if is_public else 'private'} conversations. "
                "Pick another model from the model menu and send again."
            )

        # Resolve auto-loaded skills from the autoloads table. Cross-user
        # subagent conversations skip every autoload tier and instead load
        # ONLY the skills the caller specified (and the caller-side
        # validation confirmed the target can see) -- the target user's
        # personal autoloads must not shape a run they are not driving.
        resolved_skills_content = ""
        from db.skill_store import get_user_autoloaded_skills
        skill_parts = []
        included_skill_ids = set()
        if is_public:
            # Public conversations get NO skills of any tier: skill bodies
            # are internal data that must not reach an internet-enabled
            # context.
            pass
        elif is_user_subagent:
            run_skill_ids = subagent_run.get("skill_ids") or []
            if run_skill_ids:
                from db.skill_store import get_accessible_skills_by_ids as _get_by_ids
                run_skills = await _get_by_ids(user["id"], run_skill_ids)
                for skill in run_skills:
                    skill_parts.append(f"### {skill['name']}\n{skill['content']}")
                    included_skill_ids.add(skill["id"])
        else:
            autoloaded_skills = await get_user_autoloaded_skills(user["id"])
            if autoloaded_skills:
                for skill in autoloaded_skills:
                    skill_parts.append(f"### {skill['name']}\n{skill['content']}")
                    included_skill_ids.add(skill["id"])

        # Resolve project auto-loaded skills (deduplicated against user auto-loads)
        if project_id and not is_public:
            from db.skill_store import get_project_autoloaded_skills
            project_autoloaded = await get_project_autoloaded_skills(project_id)
            for skill in project_autoloaded:
                if skill["id"] not in included_skill_ids:
                    skill_parts.append(f"### {skill['name']}\n{skill['content']}")
                    included_skill_ids.add(skill["id"])

        # Resolve routine auto-loaded skills (deduplicated against user + project auto-loads)
        if routine_id and not is_public:
            from db.skill_store import get_routine_autoloaded_skills
            routine_autoloaded = await get_routine_autoloaded_skills(
                routine_id, user["id"], project_id,
            )
            for skill in routine_autoloaded:
                if skill["id"] not in included_skill_ids:
                    skill_parts.append(f"### {skill['name']}\n{skill['content']}")
                    included_skill_ids.add(skill["id"])

        if skill_parts:
            resolved_skills_content = "\n\n".join(skill_parts)

        # Auto-loaded skill bodies land in the system prompt, so the model
        # has read them -- record that for the edit_skill content-edit gate.
        if included_skill_ids:
            ChatStorage.add_skill_read_ids(
                conversation_id, sorted(included_skill_ids)
            )

        # Resolve conversation-loaded skills (manually loaded via the Skill button)
        loaded_skills_content = ""
        if skill_ids and not is_public:
            from db.skill_store import get_accessible_skills_by_ids
            loaded_skills = await get_accessible_skills_by_ids(user["id"], skill_ids)
            if loaded_skills:
                loaded_skill_parts = []
                for skill in loaded_skills:
                    loaded_skill_parts.append(f"### {skill['name']}\n{skill['content']}")
                loaded_skills_content = "\n\n".join(loaded_skill_parts)
                ChatStorage.add_skill_read_ids(
                    conversation_id, [s["id"] for s in loaded_skills]
                )

        from api.instructions import get_user_connected_services
        connected_services = get_user_connected_services(user)
        is_slack_origin = origin == "slack"
        if is_user_subagent:
            from chat.gemini_api.system_prompt import get_user_subagent_system_prompt
            system_prompt = get_user_subagent_system_prompt(
                user["api_key"],
                base_url=get_proxy_base_url(),
                connected_services=connected_services,
                target_user_name=user.get("name", ""),
                target_user_email=user["email"],
                caller_name=(subagent_caller or {}).get("name", ""),
                caller_email=(subagent_caller or {}).get("email", ""),
                skills_content=resolved_skills_content,
            )
        elif is_inference_api:
            from chat.gemini_api.system_prompt import (
                get_inference_api_system_prompt,
            )
            system_prompt = get_inference_api_system_prompt(
                user["api_key"],
                base_url=get_proxy_base_url(),
                connected_services=connected_services,
                user_name=user.get("name", ""),
                user_email=user["email"],
                skills_content=resolved_skills_content,
            )
        elif is_public:
            # Public-project prompt: NO proxy preamble (it embeds the
            # user's real API key), no skills, no memory instructions, no
            # system-skills enumeration, no connector docs. Guide snapshots
            # and custom prompts are also excluded.
            from chat.gemini_api.system_prompt import (
                get_public_project_system_prompt,
            )
            from config.feature_gates import docs_enabled_for
            system_prompt = get_public_project_system_prompt(
                user_name=user.get("name", ""),
                user_email=user["email"],
                project_guide=resolved_project_guide,
                is_routine=bool(routine_id),
                # The public prompt has no connected-services gating, so
                # the Quest Docs gate is passed explicitly.
                docs_enabled=docs_enabled_for(user["email"]),
            )
        else:
            system_prompt = get_system_prompt(
                user["api_key"],
                base_url=get_proxy_base_url(),
                custom_system_prompt=custom_prompt,
                connected_services=connected_services,
                user_name=user.get("name", ""),
                user_email=user["email"],
                project_guide=resolved_project_guide,
                skills_content=resolved_skills_content,
                has_project=bool(project_id),
                is_slack=is_slack_origin,
                nested_subagents=nested_subagents,
                is_routine=bool(routine_id),
            )
        ChatStorage.set_system_prompt(conversation_id, system_prompt)
        # Always load saved SDK history from disk so it's available as a
        # fallback when the in-memory session is discarded (e.g., model change).
        key = (user["id"], conversation_id)
        history = None
        disk_history = None
        disk_provider = None
        disk_result = _load_sdk_history(conversation_id)
        if disk_result is not None:
            disk_history, disk_provider = disk_result
        if key not in _active_chats:
            # Only use disk history when its provider format matches the
            # target model's provider.  Without this check, Gemini-format
            # history (role:"model", parts:[...]) could be fed into an
            # Anthropic session (which expects role:"assistant",
            # content:[...]), causing the model to receive 30K tokens of
            # data it cannot interpret as conversation turns.
            if disk_history and disk_provider == provider_name:
                history = disk_history
            elif disk_history:
                logger.info(
                    "Disk history provider (%s) does not match target "
                    "provider (%s) for conversation %s; discarding "
                    "incompatible history.",
                    disk_provider, provider_name, conversation_id,
                )

        session_tools = None
        if is_slack_origin:
            from chat.llm.tool_schemas import SLACK_TOP_LEVEL_TOOLS
            session_tools = SLACK_TOP_LEVEL_TOOLS
        elif is_user_subagent:
            from chat.llm.tool_schemas import USER_SUBAGENT_TOOLS
            session_tools = USER_SUBAGENT_TOOLS
        elif is_inference_api:
            from chat.llm.tool_schemas import INFERENCE_API_TOOLS
            session_tools = INFERENCE_API_TOOLS
        elif is_public and routine_id:
            # Routine run in a public project: the public subset plus the
            # routine_completed completion marker (chat/routine_runs.py).
            from chat.llm.tool_schemas import PUBLIC_ROUTINE_TOOLS
            session_tools = PUBLIC_ROUTINE_TOOLS
        elif is_public:
            from chat.llm.tool_schemas import PUBLIC_TOOLS
            session_tools = PUBLIC_TOOLS
        elif routine_id:
            # Routine conversation: the top-level tier plus
            # routine_completed, which the run drivers wait for.
            from chat.llm.tool_schemas import ROUTINE_TOP_LEVEL_TOOLS
            session_tools = ROUTINE_TOP_LEVEL_TOOLS

        chat = get_or_create_chat(
            provider, user["id"], conversation_id, model, system_prompt,
            history=history, disk_history=disk_history, disk_provider=disk_provider,
            tools=session_tools,
        )

        # Self-heal histories corrupted by formerly-unserialized concurrent
        # runs (an orphaned mid-history tool_use otherwise 400s every API
        # call for this conversation, permanently). No-op on healthy
        # histories and on providers without strict pairing rules.
        repaired_blocks = provider.repair_session_history(chat)
        if repaired_blocks:
            logger.warning(
                "Repaired %d orphaned tool block(s) / malformed tool argument(s) in conversation %s "
                "history before running",
                repaired_blocks, conversation_id,
            )

        # All per-run state is now resolved -- bundle it so helpers (and,
        # incrementally, the tool arms) take one ctx argument instead of
        # long parameter lists. on_event is already the capturing wrapper.
        ctx = RunContext(
            app=app,
            user=user,
            conversation_id=conversation_id,
            timezone=timezone,
            model=model,
            origin=origin,
            project_id=project_id,
            routine_id=routine_id,
            slack_context=slack_context,
            provider=provider,
            provider_name=provider_name,
            chat=chat,
            is_slack_origin=is_slack_origin,
            is_user_subagent=is_user_subagent,
            is_inference_api=is_inference_api,
            is_public=is_public,
            nested_subagents=nested_subagents,
            user_subagents_enabled=user_subagents_enabled,
            user_subagents_gate_open=user_subagents_gate_open,
            subagent_run=subagent_run,
            subagent_caller=subagent_caller,
            custom_prompt=custom_prompt,
            resolved_project_guide=resolved_project_guide,
            resolved_skills_content=resolved_skills_content,
            on_event=on_event,
            structured_messages=structured_messages,
            usage_acc=usage_acc,
        )

        wrapped_message = _wrap_message_with_metadata(
            message, timezone, loaded_skills_content=loaded_skills_content,
            attached_filenames=attached_filenames,
        )

        # Dangling tool_use recovery. The loaded session may end on an
        # assistant turn whose tool_use has no matching tool_result, in
        # two cases:
        #   1. The dispatch arm raised one of the suspend sentinels
        #      (SuspendForWaitHandles, SuspendForSlackReply): the matching
        #      tool_wait_handles row holds the resolution. We close the
        #      tool_use using the row's response.
        #   2. The tool call was cancelled before a tool_result landed
        #      (server restart mid-tool, hard cancel, etc.): no
        #      wait-handle row exists. We close with a synthetic
        #      "interrupted" marker so the model can recover cleanly.
        # Sending a plain user message in either state would leave the
        # tool_use unanswered and Anthropic rejects the next API call.
        pending_tool_uses_with_args = provider.get_pending_tool_use_args(chat)
        # Resolve each dangling tool_use against the wait-handle table by
        # tool_id. Rows with kind="slack_reply" indicate a
        # send_slack_reply_and_get_response suspend; kind="action_request"
        # indicates a create_action_request suspend.
        wait_handle_by_tool_id: dict[str, dict] = {}
        if pending_tool_uses_with_args:
            for tool_id, _tname, _args in pending_tool_uses_with_args:
                row = await tool_wait_handle_store.get_handle_by_tool_id(
                    user["id"], conversation_id, tool_id,
                )
                if row is not None:
                    wait_handle_by_tool_id[tool_id] = row

        slack_reply_pending_present = any(
            row.get("kind") == "slack_reply"
            for row in wait_handle_by_tool_id.values()
        )

        if pending_tool_uses_with_args:
            logger.info(
                "Resuming conversation %s with %d pending tool_use(s); "
                "wait-handle rows matched=%d",
                conversation_id,
                len(pending_tool_uses_with_args),
                len(wait_handle_by_tool_id),
            )
            tool_results_for_resume: list[dict[str, Any]] = []
            # Lazily loaded: only a dangling tool_use with no wait-handle
            # row (a sibling of a parallel create_action_request suspend,
            # or a mid-tool restart) needs chat_history.json consulted.
            persisted_results: dict[str, str] | None = None
            for tool_id, tool_name, args in pending_tool_uses_with_args:
                row = wait_handle_by_tool_id.get(tool_id)
                if row is not None and row.get("kind") == "slack_reply":
                    response = row.get("response") or {}
                    payload_dict = row.get("payload") or {}
                    if row.get("status") == ToolWaitHandleStatus.PENDING:
                        # Should be rare: the resume kicked before the
                        # debounce flush completed. Close with a still-
                        # waiting marker so the model knows nothing came
                        # in yet (it can re-issue the tool if it wants).
                        result_str = json.dumps({
                            "status": "still_waiting",
                            "note": "User has not replied yet.",
                        })
                    else:
                        result_str = json.dumps({
                            "user_reply": response.get("user_reply", ""),
                            "posted_ts": payload_dict.get("posted_ts", ""),
                        })
                    tool_results_for_resume.append({
                        "name": tool_name,
                        "result": result_str,
                        "tool_id": tool_id,
                        "extra_parts": [],
                    })
                elif row is not None and row.get("kind") == "action_request":
                    # create_action_request suspends: close the dangling
                    # tool_use with the wait-handle row's response, which
                    # was set verbatim by the action_request_routes
                    # resolve endpoint:
                    #   {"verdict": "executed", "request_id": N, "result": {...}}
                    #   {"verdict": "denied",   "request_id": N, "result": {"denied": true}}
                    #   {"verdict": "denied",   "request_id": N,
                    #    "feedback": "<user text>",
                    #    "result": {"denied": true, "feedback": "<user text>"}}
                    #   {"verdict": "stopped",  "request_id": N,
                    #    "result": {"stopped": true}}
                    # Match this branch BEFORE the wait_for_handles
                    # catch-all so an action_request row never funnels
                    # through the wait_for_handles result-builder.
                    response = row.get("response") or {}
                    if row.get("status") == ToolWaitHandleStatus.PENDING:
                        # Race: the resume kicked before the resolve
                        # endpoint finished updating the row. Close with a
                        # still-waiting marker so the model can decide
                        # what to do (it may re-issue or apologise).
                        result_str = json.dumps({
                            "status": "still_waiting",
                            "note": (
                                "User has not resolved the action request yet."
                            ),
                        })
                    elif row.get("status") == ToolWaitHandleStatus.STOPPED:
                        # Stop: the user halted the loop instead of
                        # deciding, and this run is (normally) their next
                        # message. Say so explicitly -- the tool_use may be
                        # hours or days old by now -- so the model treats
                        # the appended message as the new instruction
                        # rather than re-proposing the stopped action.
                        result_str = json.dumps(
                            _stopped_action_request_result(response, message),
                        )
                    else:
                        result_str = json.dumps(response)
                    tool_results_for_resume.append({
                        "name": tool_name,
                        "result": result_str,
                        "tool_id": tool_id,
                        "extra_parts": [],
                    })
                elif (
                    tool_name == "wait_for_handles"
                    or (tool_name == "tool_call"
                        and args.get("tool_name") == "wait_for_handles")
                ):
                    # wait_for_handles suspends: close with the
                    # DB-derived payload covering all handle ids the model
                    # was waiting on.
                    inner_args = args.get("arguments", args) if tool_name == "tool_call" else args
                    if not isinstance(inner_args, dict):
                        inner_args = {}
                    handle_ids = inner_args.get("handle_ids", [])
                    if not isinstance(handle_ids, list):
                        handle_ids = []
                    handle_ids = [
                        str(h) for h in handle_ids if isinstance(h, str)
                    ][:_WAIT_FOR_HANDLES_MAX_IDS]
                    payload = await _build_wait_for_handles_result(
                        user["id"], handle_ids,
                    )
                    tool_results_for_resume.append({
                        "name": tool_name,
                        "result": json.dumps(payload),
                        "tool_id": tool_id,
                        "extra_parts": [],
                    })
                else:
                    # No wait-handle row. Two cases:
                    #   a) The call DID run and its tool_result is
                    #      persisted in chat_history.json -- a sibling of
                    #      a parallel create_action_request suspend (the
                    #      loop dispatched the whole batch before
                    #      unwinding). Replay that output verbatim.
                    #   b) Cancellation / restart in the middle of a
                    #      regular tool call: close with the interrupted
                    #      marker.
                    if persisted_results is None:
                        persisted_results = _load_persisted_tool_results(
                            conversation_id,
                        )
                    replayed = persisted_results.get(tool_id)
                    if replayed is not None:
                        result_str = replayed
                    else:
                        result_str = json.dumps({
                            "status": "interrupted",
                            "note": (
                                "The previous tool call was interrupted "
                                "before completing. Retry if needed."
                            ),
                        })
                    tool_results_for_resume.append({
                        "name": tool_name,
                        "result": result_str,
                        "tool_id": tool_id,
                        "extra_parts": [],
                    })
            current_message: Any = provider.format_tool_results(
                chat, tool_results_for_resume,
            )
            # If no Slack reply tool was pending, the user's wrapped message
            # still needs to reach the model as ordinary input on this turn.
            # Append it alongside the tool_result blocks in the user turn --
            # except when ``message`` is empty, which signals an auto-resume
            # kicked off by a wait-handle resolve (no fresh user input to
            # carry, just close the dangling tool_use).
            if (
                not slack_reply_pending_present
                and isinstance(current_message, list)
                and message
            ):
                provider.append_user_text(current_message, wrapped_message)
        else:
            if not message and not attachments:
                # Auto-resume kicked off by a wait-handle resolve, but the
                # saved session has no dangling tool_use (e.g. the suspended
                # arm completed before the process died, or the user pressed
                # Accept on a card whose model loop already moved past it).
                # Nothing to do -- bail before invoking the model with an
                # empty user turn that would just confuse it.
                logger.info(
                    "Skipping empty-message run for conversation %s: "
                    "no dangling tool_use to resume.",
                    conversation_id,
                )
                return structured_messages
            if attachments:
                # Multimodal user turn: build a provider-appropriate
                # composite (text + image parts/blocks) so the model sees
                # the pasted images alongside the text.
                conv_workspace_path = await ChatStorage.get_workspace_path(
                    conversation_id, project_id,
                )
                current_message = await _build_user_message_with_attachments(
                    provider, wrapped_message, attachments, conv_workspace_path,
                    model=model,
                )
            else:
                current_message = wrapped_message

        # Persist loaded skill IDs to disk so they survive page reloads
        if skill_ids:
            ChatStorage.add_loaded_skill_ids(conversation_id, skill_ids)

        while True:
            turn_count += 1
            turn_start_time = time.time()

            # Stream the response via the provider interface.
            # _stream_turn flushes partial text into structured_messages on
            # any error (incl. CancelledError) before it propagates.
            # For Gemini, catch ClientError for MIME-type recovery.
            try:
                text_parts, function_calls = await _stream_turn(ctx, current_message)
            except Exception as e:
                # Gemini-specific MIME type recovery: if the error is a
                # ClientError with INVALID_ARGUMENT and we have file parts
                # in the message, strip them and retry.
                error_str = str(e)
                is_mime_error = (
                    "INVALID_ARGUMENT" in error_str
                    and isinstance(current_message, list)
                    and any(getattr(p, 'file_data', None) for p in current_message
                            if hasattr(p, 'file_data'))
                )
                if not is_mime_error:
                    raise

                logger.warning(
                    "LLM API rejected file part(s) in message — stripping "
                    "file URI parts and retrying (conversation=%s, user=%s): %s",
                    conversation_id, user["email"], e,
                )

                # Build a cleaned message: keep function_response parts,
                # replace file parts with a text error note.
                from chat.llm.gemini_provider import GeminiProvider
                if isinstance(provider, GeminiProvider):
                    from google.genai import types
                    cleaned_parts = []
                    for part in current_message:
                        if getattr(part, 'file_data', None):
                            mime = getattr(part.file_data, 'mime_type', 'unknown')
                            cleaned_parts.append(types.Part.from_text(
                                text=f"[File upload error: the file ({mime}) was uploaded "
                                f"but the content generation API rejected it with: "
                                f"{error_str}. Advise the user to use run_script to "
                                f"extract contents programmatically instead.]"
                            ))
                        else:
                            cleaned_parts.append(part)
                    current_message = cleaned_parts
                else:
                    raise

                # Retry the turn with cleaned message (no file URI parts).
                turn_start_time = time.time()
                text_parts, function_calls = await _stream_turn(ctx, current_message)

            # Record per-turn usage to the database
            turn_usage = provider.get_usage(chat)
            turn_input_tokens = turn_usage.input_tokens
            turn_output_tokens = turn_usage.output_tokens
            turn_cached_tokens = turn_usage.cached_tokens
            usage_acc.add_turn(turn_usage, provider_name)

            turn_duration_ms = int((time.time() - turn_start_time) * 1000)

            try:
                await record_api_call(
                    conversation_id=conversation_id,
                    user_id=user["id"],
                    model=model,
                    call_type=ApiCallType.TOP_LEVEL,
                    input_tokens=turn_input_tokens,
                    output_tokens=turn_output_tokens,
                    duration_ms=turn_duration_ms,
                    cached_tokens=turn_cached_tokens,
                    provider=provider_name,
                    backend=get_backend_for_model(model),
                    level=1,
                    raw_usage=turn_usage.raw_usage,
                )
            except Exception:
                logger.warning("Failed to record API call usage", exc_info=True)

            # Save accumulated text as a structured message
            if text_parts:
                full_text = "".join(text_parts)
                if full_text.strip():
                    structured_messages.append({
                        "type": "text",
                        "role": "assistant",
                        "content": full_text,
                        "timestamp": utc_timestamp(),
                    })

            # If no function calls, the turn is complete
            if not function_calls:
                break

            # Execute each function call and collect responses
            tool_results = []
            turn_state = TurnState()
            # create_action_request suspends collected across this batch.
            # A model may emit several create_action_request calls in ONE
            # turn (parallel tool calls); each opens its own card + wait
            # handle, and the loop keeps dispatching the rest of the batch
            # instead of unwinding on the first suspend (which would leave
            # the later tool_uses undispatched and closed with the
            # "interrupted" marker on resume). The batch-wide suspend is
            # raised after the loop; the resume gate in
            # chat/wait_handles/resume.py holds the continuation until
            # every card is resolved.
            batch_suspends: list[SuspendForActionRequest] = []
            for fc_event in function_calls:
                tool_call_count += 1
                tool_id = fc_event.tool_id or f"{fc_event.tool_name}_{uuid.uuid4().hex[:8]}"
                args = dict(fc_event.tool_args) if fc_event.tool_args else {}

                # Extract intent_message before passing args to tool execution
                intent_message = args.pop("intent_message", "")
                if intent_message and len(intent_message) > 50:
                    intent_message = intent_message[:50]

                # _emit_durable appends to structured_messages BEFORE
                # emitting the event so any on_event callback that flushes
                # messages_out to disk (e.g. the Slack dispatcher's
                # incremental flush) sees this tool_use. Without this
                # ordering a long-running tool such as
                # send_slack_reply_and_get_response could suspend for minutes
                # or hours before the matching tool_result triggers a flush,
                # leaving the conversation view blank on refresh.
                await _emit_durable(ctx, {
                    "type": "tool_use",
                    "tool_name": fc_event.tool_name,
                    "tool_input": args,
                    "tool_id": tool_id,
                    "intent_message": intent_message,
                }, durable_extra={"role": "assistant"})

                # Execute: loop-handled arms (sub-agent spawners, blocking
                # action-request / Slack-reply / wait arms, origin-specific
                # finish tools) via the TURN_TOOL_HANDLERS registry;
                # everything else via shared dispatch. Handlers raise the
                # suspend sentinels; those unwind past this loop unchanged.
                extra_parts = []  # Additional content parts (e.g., uploaded file references)
                registry_key = registry_key_for(fc_event.tool_name, args)
                arm_handler = TURN_TOOL_HANDLERS.get(registry_key)

                if fc_event.tool_args_error:
                    # The provider could not parse the model's arguments
                    # (malformed JSON from a weaker model). Nothing to
                    # dispatch: hand the parse error back as the tool
                    # result so the model can retry, rather than letting
                    # the call run on empty arguments.
                    result = invalid_tool_arguments_result(
                        fc_event.tool_name, fc_event.tool_args_error,
                    )
                    _log_large_tool_result(
                        fc_event.tool_name, args, result, model, False,
                        user_email=user.get("email"), conversation_id=conversation_id,
                    )
                elif ctx.is_public and registry_key in _PUBLIC_BLOCKED_LOOP_TOOLS:
                    # Public-project hard reject for the loop-handled arms.
                    # PUBLIC_TOOLS never advertises these, but prompt/schema
                    # trimming is not a security boundary -- this is. Logged
                    # under the provider-visible tool name ("tool_call" for
                    # the wait_for_handles reject).
                    result = public_blocked_result(registry_key)
                    _log_large_tool_result(
                        fc_event.tool_name, args, result, model, False,
                        user_email=user.get("email"), conversation_id=conversation_id,
                    )
                elif arm_handler is not None:
                    try:
                        result = await arm_handler(
                            ctx,
                            ToolCall(
                                name=fc_event.tool_name,
                                key=registry_key,
                                tool_id=tool_id,
                                raw_tool_id=fc_event.tool_id,
                                args=args,
                            ),
                            turn_state,
                        )
                    except SuspendForActionRequest as suspend_exc:
                        if registry_key != "create_action_request":
                            # return_to_caller keeps its immediate unwind:
                            # a subagent run has exactly one return.
                            raise
                        # Card + wait handle are already durable (the arm
                        # emitted the action_request event before raising).
                        # No tool_result for this call -- the resume bucket
                        # closes it with the row's verdict. Keep dispatching
                        # the rest of the batch.
                        batch_suspends.append(suspend_exc)
                        continue
                    _log_large_tool_result(
                        registry_key, args, result, model, False,
                        user_email=user.get("email"), conversation_id=conversation_id,
                    )
                else:
                    # Shared tool dispatch (local tools + HTTP tools)
                    result, extra_parts = await _dispatch_tool_call(
                        app, provider, user, conversation_id, timezone,
                        fc_event.tool_name, args, project_id=project_id,
                        model=model, is_sub_agent=False, is_public=is_public,
                        is_inference_api=ctx.is_inference_api,
                        is_user_subagent=ctx.is_user_subagent,
                        is_slack=ctx.is_slack_origin,
                    )

                    # Emit conversation_updated event when the name was set
                    if (fc_event.tool_name == "tool_call"
                            and args.get("tool_name") == "set_conversation_name"):
                        try:
                            parsed = json.loads(result)
                            if parsed.get("name_set"):
                                await on_event({
                                    "type": "conversation_updated",
                                    "conversation_id": conversation_id,
                                    "custom_name": parsed["name"],
                                })
                        except Exception:
                            pass  # Don't break the conversation loop

                # _emit_durable appends to structured_messages BEFORE
                # emitting the event so incremental-flush on_event callbacks
                # (see above note on tool_use) can persist the tool_result
                # reliably.
                durable_extra: dict = {"role": "assistant"}
                # Attach sub-agent tool call history for persistence (display-only).
                # This allows the frontend to reconstruct the sub-agent tree
                # when the conversation is loaded from storage.
                if fc_event.tool_name in ("agent_task", "agent_task_parallel", "agent_task_parallel_template"):
                    captured = _sub_agent_events.pop(tool_id, [])
                    if captured:
                        durable_extra["sub_agent_tool_calls"] = captured
                await _emit_durable(ctx, {
                    "type": "tool_result",
                    "tool_id": tool_id,
                    "tool_output": result,
                }, durable_extra=durable_extra)

                # Collect tool result for provider formatting
                tool_results.append({
                    "name": fc_event.tool_name,
                    "result": result,
                    "tool_id": tool_id,
                    "extra_parts": extra_parts,
                })

            if batch_suspends:
                # Every non-suspending call in the batch has run and its
                # tool_result is persisted in chat_history.json (the resume
                # bucket recovers those via _load_persisted_tool_results).
                # Unwind now on the first suspend, carrying the sibling
                # handle ids for logging.
                first = batch_suspends[0]
                first.sibling_handle_ids = [
                    s.handle_id for s in batch_suspends[1:]
                ]
                raise first

            # Send function responses back to the model as the next turn
            current_message = provider.format_tool_results(chat, tool_results)

        # Emit stats using accumulated totals across all turns
        duration_ms = int((time.time() - start_time) * 1000)
        stats = usage_acc.build_stats(
            duration_ms=duration_ms,
            tool_calls=tool_call_count,
            turns=turn_count,
            provider_name=provider_name,
            model=model,
        )
        await _emit_durable(ctx, {"type": "stats", "stats": stats})

    except SuspendForWaitHandles as exc:
        # Conversation suspended on a wait_for_handles tool_use. The dangling
        # tool_use is already on disk via the boundary save inside
        # _capturing_on_event (commit b675c45) -- nothing else to do here.
        # The resume bucket on the next run reads the (then-resolved) DB row
        # and closes the tool_use cleanly. Do NOT emit error/stats events;
        # this is a successful suspend, not a turn-end.
        logger.info(
            "[wait-suspend] Conversation %s suspended on wait_for_handles "
            "(tool_id=%s, handle_ids=%s, user=%s)",
            conversation_id, exc.tool_id, exc.handle_ids, user["email"],
        )
        return structured_messages
    except SuspendForSlackReply as exc:
        # Same shape as SuspendForWaitHandles: the dangling
        # send_slack_reply_and_get_response tool_use is already on disk and
        # will be closed by the resume bucket once the user replies in
        # Slack (via the slack_reply wait-handle row).
        logger.info(
            "[wait-suspend] Conversation %s suspended on slack_reply "
            "(tool_id=%s, handle_id=%s, channel=%s, thread_ts=%s, user=%s)",
            conversation_id, exc.tool_id, exc.handle_id,
            exc.channel, exc.thread_ts, user["email"],
        )
        return structured_messages
    except SuspendForActionRequest as exc:
        # Same shape as SuspendForSlackReply: the dangling
        # create_action_request tool_use is already on disk and will be
        # closed by the resume bucket once the user clicks Approve /
        # Revise / Deny on the inline card (via the action_request
        # wait-handle row resolved by the action_request_routes resolve
        # endpoint).
        logger.info(
            "[wait-suspend] Conversation %s suspended on action_request "
            "(tool_id=%s, handle_id=%s, request_id=%s, "
            "sibling_handle_ids=%s, user=%s)",
            conversation_id, exc.tool_id, exc.handle_id,
            exc.request_id, exc.sibling_handle_ids, user["email"],
        )
        return structured_messages
    except FinishInferenceResponse as exc:
        # One-shot inference API run delivered its final answer. This is a
        # successful completion, not a suspend: the tool_use/tool_result
        # pair is already in structured_messages (and flushed to disk by
        # the boundary save). Do NOT emit error/stats events.
        logger.info(
            "[inference-api] Conversation %s finished via "
            "return_final_response (tool_id=%s, user=%s)",
            conversation_id, exc.tool_id, user["email"],
        )
        return structured_messages
    except Exception as exc:
        logger.exception("Error in run_conversation_turn (user=%s, conversation=%s)", user["email"], conversation_id)
        # Persist the error as a durable structured message BEFORE emitting
        # the event: "error" is in FLUSH_EVENT_TYPES, so the on_event flush
        # writes it to chat_history.json and fans out ``message_appended``.
        # Without this the error only ever existed as a transient WS event
        # -- any tab that was disconnected, expired, or reloaded saw the
        # conversation simply stop with no explanation.
        error_summary = f"{type(exc).__name__}: {exc}"
        structured_messages.append({
            "type": "error",
            "error": error_summary,
            "stacktrace": traceback.format_exc(),
            "timestamp": utc_timestamp(),
        })
        await on_event({
            "type": "error",
            "error": error_summary,
            "stacktrace": traceback.format_exc(),
        })
        raise

    return structured_messages
