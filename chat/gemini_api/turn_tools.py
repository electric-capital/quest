"""Per-tool handler functions for the conversation loop's dispatch chain.

``run_conversation_turn`` (chat/gemini_api/conversation.py) streams model turns
and dispatches each tool call. The tools the loop itself must handle --
sub-agent spawners, the blocking action-request / Slack-reply / wait
arms, and the origin-specific finish tools -- live here as
``async def _handle_x(ctx, call, turn) -> str`` functions registered in
:data:`TURN_TOOL_HANDLERS`. Everything else falls through to the shared
``_dispatch_tool_call``.

Handler calling convention:

* ``ctx`` is the run's :class:`~chat.gemini_api.run_context.RunContext`.
* ``call`` is the per-call :class:`ToolCall` bundle (name, registry key,
  resolved tool_id, provider-native raw_tool_id, args).
* ``turn`` is the per-turn :class:`TurnState` (mutable bookkeeping shared
  by all calls of one model turn, e.g. the agent_task per-turn cap).
* A handler returns the tool-result string, or raises one of the suspend
  sentinels defined below (:class:`SuspendForWaitHandles`,
  :class:`SuspendForSlackReply`, :class:`SuspendForActionRequest`,
  :class:`FinishInferenceResponse`) -- those are control flow, not
  errors, and must propagate to the loop unchanged.

The loop calls ``_log_large_tool_result`` once per handled call, so no
handler logs its own result.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from chat.storage import utc_timestamp
from db import tool_wait_handle_store
from db.models import ActionRequestType, ToolWaitHandleStatus
from chat.gemini_api.constants import (
    _DEPRECATED_MODELS,
    MAX_AGENT_TASKS_PER_TURN,
    SUB_AGENT_DISALLOWED_MODELS,
)
from chat.gemini_api.run_context import RunContext
from chat.gemini_api.sub_agent import (
    _run_sub_agent,
    _run_parallel_sub_agents,
    _run_parallel_sub_agents_template,
)
from chat.llm.config import get_provider_for_model, get_provider_instance, model_instance_id

logger = logging.getLogger(__name__)

# Registry key for the wait_for_handles arm, which rides on the generic
# ``tool_call`` router tool instead of having its own top-level schema.
WAIT_FOR_HANDLES_KEY = "tool_call:wait_for_handles"

# routine_completed HAS its own top-level schema, but weaker models routed
# it through ``tool_call(tool_name="routine_completed")`` anyway in live
# runs (the prompt teaches that form for every dynamic tool). Both spellings
# are accepted; this is the key for the tool_call-riding one.
ROUTINE_COMPLETED_KEY = "routine_completed"
ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY = "tool_call:routine_completed"

# Loop-handled tool arms hard-rejected in public-project conversations
# (defense in depth beside the trimmed PUBLIC_TOOLS schema): sub-agent
# spawners, action requests, and the origin-specific finish/reply tools.
# Keyed by registry key, so the tool_call-riding wait_for_handles arm is
# listed under WAIT_FOR_HANDLES_KEY. ``routine_completed`` (both keys) is
# deliberately absent: it is a pure completion signal (no reads, no writes)
# and routine runs in public projects need it (PUBLIC_ROUTINE_TOOLS).
_PUBLIC_BLOCKED_LOOP_TOOLS = frozenset({
    "agent_task",
    "agent_task_parallel",
    "agent_task_parallel_template",
    "create_action_request",
    "send_slack_reply_and_get_response",
    "return_to_caller",
    "return_final_response",
    WAIT_FOR_HANDLES_KEY,
})


# Hard caps mirroring the wait_for_handles tool description so the resume
# path applies the same validation it would have in the live arm.
#
# The default timeout is set to the maximum (~14 days) so that human-in-the-
# loop scenarios -- the user reviewing a memory suggestion later in the day,
# or a peer responding to a routed action request days from now -- work
# without per-call tuning. The model is encouraged to omit `timeout_seconds`
# entirely; it should only set a smaller cap when it deliberately wants to
# stop blocking sooner (e.g., "give the user 5 minutes, otherwise move on").
_WAIT_FOR_HANDLES_MAX_IDS = 8
_WAIT_FOR_HANDLES_MIN_TIMEOUT = 1
_WAIT_FOR_HANDLES_MAX_TIMEOUT = 14 * 24 * 60 * 60  # 1_209_600 s, ~14 days
_WAIT_FOR_HANDLES_DEFAULT_TIMEOUT = _WAIT_FOR_HANDLES_MAX_TIMEOUT
# Cap how long a model-supplied wait reason can be before truncation. The
# reason is shown directly in the chat UI, so we never reject on length --
# we only clip to a sensible upper bound.
_WAIT_FOR_HANDLES_MAX_REASON_LEN = 200


class SuspendForWaitHandles(Exception):
    """Raised by ``_await_wait_for_handles`` when the conversation must
    suspend until a wait handle is resolved.

    The exception unwinds the dispatch loop without emitting a
    ``tool_result`` event; the dangling ``wait_for_handles`` ``tool_use``
    on disk is closed by the resume bucket on the next run. Inherits from
    ``Exception`` (not ``BaseException``) so it never masks
    ``CancelledError``.
    """

    def __init__(
        self,
        handle_ids: list[str],
        tool_id: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"wait_for_handles suspended (tool_id={tool_id}, "
            f"handle_ids={handle_ids})"
        )
        self.handle_ids = handle_ids
        self.tool_id = tool_id
        self.reason = reason


class SuspendForSlackReply(Exception):
    """Raised by the ``send_slack_reply_and_get_response`` arm when the
    conversation must suspend until the user replies in Slack.

    Mirrors :class:`SuspendForWaitHandles`: the dispatch loop unwinds
    without emitting a ``tool_result``; the dangling
    ``send_slack_reply_and_get_response`` ``tool_use`` is closed by the
    resume bucket on the next run, using the matching slack_reply
    wait-handle row.
    """

    def __init__(
        self,
        handle_id: str,
        tool_id: str,
        channel: str,
        thread_ts: str,
    ) -> None:
        super().__init__(
            f"send_slack_reply_and_get_response suspended "
            f"(tool_id={tool_id}, handle_id={handle_id}, "
            f"channel={channel}, thread_ts={thread_ts})"
        )
        self.handle_id = handle_id
        self.tool_id = tool_id
        self.channel = channel
        self.thread_ts = thread_ts


class SuspendForActionRequest(Exception):
    """Raised by the ``create_action_request`` arm when the conversation
    must suspend until the user resolves the request via Approve / Revise
    / Deny on the inline card.

    Mirrors :class:`SuspendForSlackReply`: the dispatch loop unwinds
    without emitting a ``tool_result``; the dangling
    ``create_action_request`` ``tool_use`` is closed by the resume bucket
    on the next run, using the matching action_request wait-handle row's
    ``response`` payload.
    """

    def __init__(
        self,
        handle_id: str,
        tool_id: str,
        request_id: int,
    ) -> None:
        super().__init__(
            f"create_action_request suspended "
            f"(tool_id={tool_id}, handle_id={handle_id}, "
            f"request_id={request_id})"
        )
        self.handle_id = handle_id
        # Wait-handle ids of OTHER create_action_request calls suspended in
        # the same parallel tool-call batch (the dispatch loop keeps
        # dispatching after the first suspend and re-raises the first
        # sentinel with its siblings attached). Logging only -- every card
        # has its own row, and the resume gate reads the DB, not this.
        self.sibling_handle_ids: list[str] = []
        self.tool_id = tool_id
        self.request_id = request_id


class FinishInferenceResponse(Exception):
    """Raised by the ``return_final_response`` arm when a one-shot
    inference API run (origin="inference_api") has delivered its final
    answer and the loop must end.

    Unlike the suspend sentinels the run is genuinely finished: the
    matching ``tool_result`` structured message is appended BEFORE the
    raise so the read-only transcript closes cleanly, and the driver
    (chat/inference_api.py) extracts the response text from the persisted
    ``return_final_response`` tool_use in ``messages_out``. Inherits from
    ``Exception`` (not ``BaseException``) so it never masks
    ``CancelledError``.
    """

    def __init__(self, tool_id: str) -> None:
        super().__init__(
            f"return_final_response delivered (tool_id={tool_id})"
        )
        self.tool_id = tool_id


@dataclass(frozen=True)
class ToolCall:
    """One tool call as the dispatch loop hands it to a handler.

    ``tool_id`` is the loop-resolved id (falling back to a generated
    ``<name>_<hex>`` when the provider sent none) and is what the durable
    ``tool_use``/``tool_result`` messages carry. ``raw_tool_id`` is the
    provider-native id verbatim -- the ``create_action_request`` arm uses
    it for the wait handle + suspend sentinel (preserved pre-extraction
    behavior; see the dev plan's invariants).
    """

    name: str
    key: str
    tool_id: str
    raw_tool_id: str | None
    args: dict[str, Any]


@dataclass
class TurnState:
    """Mutable per-turn bookkeeping shared by all tool calls of one turn.

    A fresh instance is created for every model turn (one batch of
    function calls); handlers that enforce per-turn limits count here.
    """

    agent_task_calls: int = 0


TurnToolHandler = Callable[
    [RunContext, ToolCall, TurnState], Awaitable[str],
]


def registry_key_for(tool_name: str, args: dict[str, Any]) -> str:
    """Map a provider tool call onto its :data:`TURN_TOOL_HANDLERS` key.

    ``wait_for_handles`` rides on the generic ``tool_call`` router, so it
    is keyed as :data:`WAIT_FOR_HANDLES_KEY`; every other loop-handled
    tool is keyed by its own name. Names without a registry entry fall
    through to the shared ``_dispatch_tool_call``.
    """
    if tool_name == "tool_call":
        inner = args.get("tool_name")
        if inner == "wait_for_handles":
            return WAIT_FOR_HANDLES_KEY
        if inner == ROUTINE_COMPLETED_KEY:
            return ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY
    return tool_name


def public_blocked_result(key: str) -> str:
    """Error result for a loop-handled arm rejected in a public project.

    PUBLIC_TOOLS never advertises these tools, but prompt/schema trimming
    is not a security boundary -- this reject is.
    """
    blocked_name = (
        "wait_for_handles" if key == WAIT_FOR_HANDLES_KEY else key
    )
    return json.dumps({
        "error": (
            f"'{blocked_name}' is not available in "
            "public-project conversations."
        ),
    })


async def _emit_durable(
    ctx: RunContext,
    event: dict,
    *,
    durable_extra: dict | None = None,
) -> None:
    """Persist ``event`` as a structured message, then emit it.

    The structured copy (plus ``durable_extra`` fields and a timestamp)
    is appended to ``ctx.structured_messages`` BEFORE ``ctx.on_event`` is
    awaited, so an on_event callback that flushes messages_out to disk
    (and the boundary SDK-history save riding on it) always sees the
    message it is flushing. This makes the persist-before-emit invariant
    structural instead of comment-enforced at each site.
    """
    ctx.structured_messages.append({
        **event,
        **(durable_extra or {}),
        "timestamp": utc_timestamp(),
    })
    await ctx.on_event(event)


def _handle_response_for_model(handle: dict) -> dict:
    """Project a wait-handle row dict into the shape returned to the model."""
    return {
        "id": handle["id"],
        "kind": handle.get("kind"),
        "status": handle.get("status"),
        "tool_id": handle.get("tool_id"),
        "response": handle.get("response"),
    }


def _normalize_wait_reason(raw: Any) -> str:
    """Trim and cap a model-supplied wait reason for end-user display.

    Returns an empty string when the input is missing or non-string; callers
    in the live dispatch path treat empty as a validation error, but the
    resume path tolerates it because the wait already happened.
    """
    if not isinstance(raw, str):
        return ""
    stripped = raw.strip()
    if len(stripped) > _WAIT_FOR_HANDLES_MAX_REASON_LEN:
        stripped = stripped[:_WAIT_FOR_HANDLES_MAX_REASON_LEN]
    return stripped


async def _await_wait_for_handles(
    ctx: RunContext,
    inner_args: dict,
    tool_id: str = "",
) -> str:
    """Validate args, short-circuit if all handles are already resolved, or
    raise :class:`SuspendForWaitHandles` to suspend the conversation.

    The DB row is the source of truth -- there is no in-process future
    registry. When at least one handle is still pending this function
    raises the sentinel; the top-level loop unwinds and the dangling
    ``wait_for_handles`` ``tool_use`` left on disk is closed by the
    resume bucket on the next run (whether triggered by a wait-handle
    resolve via REST, a fresh user message, or a server restart).

    Returns a JSON string for two cases only:
    * argument-validation errors (string starts with ``{"error": ...}``);
    * all handles already in a terminal state at call time.
    """
    # Validate inputs.
    raw_ids = inner_args.get("handle_ids", [])
    if not isinstance(raw_ids, list) or not raw_ids:
        return json.dumps({
            "error": "handle_ids must be a non-empty list of strings.",
        })
    handle_ids: list[str] = []
    for h in raw_ids:
        if not isinstance(h, str) or not h:
            return json.dumps({
                "error": "handle_ids must be a non-empty list of strings.",
            })
        handle_ids.append(h)
    if len(handle_ids) > _WAIT_FOR_HANDLES_MAX_IDS:
        return json.dumps({
            "error": (
                f"handle_ids accepts at most {_WAIT_FOR_HANDLES_MAX_IDS} ids "
                "per call."
            ),
        })

    # The reason is shown verbatim in the chat UI so end users do not see
    # the raw tool name. Reject missing / empty so the model retries; cap
    # length silently.
    raw_reason = inner_args.get("reason")
    if raw_reason is None:
        return json.dumps({
            "error": (
                "reason is required: provide a short human-readable string "
                "describing what you are waiting for (shown to the user)."
            ),
        })
    if not isinstance(raw_reason, str):
        return json.dumps({
            "error": "reason must be a string.",
        })
    reason = _normalize_wait_reason(raw_reason)
    if not reason:
        return json.dumps({
            "error": (
                "reason must be a non-empty string describing what you "
                "are waiting for."
            ),
        })

    raw_timeout = inner_args.get("timeout_seconds", _WAIT_FOR_HANDLES_DEFAULT_TIMEOUT)
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = _WAIT_FOR_HANDLES_DEFAULT_TIMEOUT
    if timeout_seconds < _WAIT_FOR_HANDLES_MIN_TIMEOUT:
        timeout_seconds = _WAIT_FOR_HANDLES_MIN_TIMEOUT
    if timeout_seconds > _WAIT_FOR_HANDLES_MAX_TIMEOUT:
        timeout_seconds = _WAIT_FOR_HANDLES_MAX_TIMEOUT

    # Authoritative read from the DB up front. Already-resolved handles
    # short-circuit; unknown / not-owned ids surface as an error.
    rows = await tool_wait_handle_store.bulk_get_handles_by_ids(
        ctx.user["id"], handle_ids,
    )
    by_id = {r["id"]: r for r in rows}
    missing = [h for h in handle_ids if h not in by_id]
    if missing:
        return json.dumps({
            "error": (
                "Some handle ids are unknown or not owned by you: "
                f"{missing}"
            ),
        })

    pending_ids = [
        hid for hid in handle_ids
        if by_id[hid].get("status") == ToolWaitHandleStatus.PENDING
    ]
    if not pending_ids:
        # All resolved already -- return immediately.
        result = await _build_wait_for_handles_result(ctx.user["id"], handle_ids)
        return json.dumps(result)

    # Schedule a per-handle background timer to enforce ``timeout_seconds``.
    # If the run survives in-process until the deadline, the timer marks any
    # still-pending rows ``timed_out`` and kicks a resume so the suspended
    # conversation wakes up. The expires_at DB sweep remains the
    # restart-resilience fallback for in-memory timers lost on restart.
    from chat.wait_handles import wait_timer
    wait_timer.schedule_timeout_for_handles(
        user_id=ctx.user["id"],
        conversation_id=ctx.conversation_id,
        handle_ids=pending_ids,
        timeout_seconds=timeout_seconds,
        app=ctx.app,
        user=ctx.user,
    )

    raise SuspendForWaitHandles(
        handle_ids=pending_ids,
        tool_id=tool_id,
        reason=reason,
    )


async def _build_wait_for_handles_result(
    user_id: int, handle_ids: list[str],
) -> dict:
    """Build the JSON payload returned by ``wait_for_handles`` from the DB.

    Used both by the live dispatch arm (after waking up) and by the resume
    path that closes a dangling ``wait_for_handles`` tool_use after a
    restart. Unknown / not-owned ids appear under ``unknown``.
    """
    # TODO: enforce expires_at here -- after a server restart a row whose
    # original deadline has passed comes back as still_pending; consult
    # expires_at and call mark_timed_out for rows where expires_at <= now()
    # before building the payload, otherwise handles can sit pending forever
    # if no caller re-invokes wait_for_handles.
    rows = await tool_wait_handle_store.bulk_get_handles_by_ids(
        user_id, handle_ids,
    )
    by_id = {r["id"]: r for r in rows}
    resolved: list[dict] = []
    still_pending: list[str] = []
    unknown: list[str] = []
    for hid in handle_ids:
        row = by_id.get(hid)
        if row is None:
            unknown.append(hid)
            continue
        if row.get("status") == ToolWaitHandleStatus.PENDING:
            still_pending.append(hid)
        else:
            resolved.append(_handle_response_for_model(row))
    payload: dict[str, Any] = {
        "resolved": resolved,
        "still_pending": still_pending,
    }
    if unknown:
        payload["unknown"] = unknown
    return payload


async def _publish_request_count(user_id: int) -> None:
    """Best-effort: publish the per-user action-request counts to the
    persistent WS so the sidebar badge updates without waiting for a poll.
    """
    try:
        from chat.realtime import bus as _rt_bus
        from chat.realtime import events as _rt_events
        from db.action_request_store import count_action_requests_by_status
        counts = await count_action_requests_by_status(user_id)
        _rt_bus.publish_to_user(
            user_id,
            _rt_events.make_request_count_changed(counts),
        )
    except Exception:
        logger.debug(
            "[conversation] publish request_count_changed failed",
            exc_info=True,
        )


async def _open_action_request_card(
    ctx: RunContext,
    *,
    request_type: str,
    validated_params: dict,
    reasoning: str,
    card_reasoning: str,
    handler: Any,
    tool_id: str,
) -> tuple[int, str]:
    """Create an action-request row + linked wait handle and emit the card.

    Shared tail of the ``return_to_caller`` and ``create_action_request``
    arms. Returns ``(request_id, wait_handle_id)``; the caller performs
    any arm-specific side effects (e.g. subagent-run status updates and
    the request-count publish) and then raises
    :class:`SuspendForActionRequest` itself.

    The wait handle is linked to the action request via the correlation
    columns (populated at create time because the request_id is known up
    front); the resume bucket on the next run reads the row's response to
    close the dangling tool_use. The structured card message is appended
    BEFORE the event is emitted (via ``_emit_durable``) so an on_event
    flush sees it -- the boundary save inside ``_capturing_on_event``
    then persists chat_history.json so the card and the dangling
    tool_use are both on disk before the caller raises the suspend
    sentinel.

    ``reasoning`` is stored on the action-request row; ``card_reasoning``
    is what the inline card displays (``return_to_caller`` stores the
    model's reasoning but shows an empty string).
    """
    from chat.action_request_types import get_summary_snippet
    from db.action_request_store import create_action_request

    req_data = await create_action_request(
        user_id=ctx.user["id"],
        conversation_id=ctx.conversation_id,
        request_type=request_type,
        params=validated_params,
        reasoning=reasoning,
    )
    request_id = req_data["id"]

    preview_fields = await handler.render_preview(validated_params, ctx.user)

    wait_handle = await tool_wait_handle_store.create_handle(
        user_id=ctx.user["id"],
        conversation_id=ctx.conversation_id,
        kind="action_request",
        tool_id=tool_id,
        payload={
            "request_id": request_id,
            "request_type": request_type,
            "params": validated_params,
        },
        correlation_kind="action_request",
        correlation_id=str(request_id),
    )
    wait_handle_id = wait_handle["id"]

    await _emit_durable(ctx, {
        "type": "action_request",
        "request_id": request_id,
        "request_type": request_type,
        "params": validated_params,
        "reasoning": card_reasoning,
        "status": "open",
        "display_name": handler.display_name,
        "preview_fields": preview_fields,
        "approve_label": handler.approve_label,
        "resolved_label": handler.resolved_label,
        "summary_snippet": get_summary_snippet(request_type, validated_params),
        "wait_handle_id": wait_handle_id,
    })

    return request_id, wait_handle_id


async def _run_spawn_arm(
    ctx: RunContext,
    *,
    arm: str,
    spawn_desc: str,
    failure_prefix: str,
    runner: Callable[[dict], Awaitable[str]],
    error_event: dict | None = None,
) -> str:
    """Shared scaffolding for the three sub-agent spawn arms.

    Handles the spawn/completion/failure logging, wall-clock timing, the
    per-arm usage dict, and the merge into ``ctx.usage_acc``. ``runner``
    receives the empty usage dict and returns the tool-result string; any
    exception it raises becomes a ``{"error": "<failure_prefix>: ..."}``
    result (after emitting ``error_event``, the single-agent arm's
    ``sub_agent_finished`` error notification, when one is supplied).
    """
    logger.info(
        "[%s] Spawning %s (conversation=%s, user=%s)",
        arm, spawn_desc, ctx.conversation_id, ctx.user["email"],
    )
    start = time.time()
    usage: dict = {}
    try:
        result = await runner(usage)
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        logger.exception(
            "[%s] %s failed after %dms (conversation=%s, user=%s)",
            arm, spawn_desc, duration_ms, ctx.conversation_id,
            ctx.user["email"],
        )
        if error_event is not None:
            # Emit sub_agent_finished(error) so the UI flips from RUNNING
            # to ERRORED; the runner raised before it could emit its own
            # success event.
            try:
                await ctx.on_event({**error_event, "error": str(e)})
            except Exception:
                logger.debug(
                    "Failed to emit sub_agent_finished (error) event",
                    exc_info=True,
                )
        return json.dumps({"error": f"{failure_prefix}: {e}"})
    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "[%s] %s completed in %dms (conversation=%s, user=%s, response_length=%d)",
        arm, spawn_desc, duration_ms, ctx.conversation_id,
        ctx.user["email"], len(result),
    )
    ctx.usage_acc.add_sub_agent(usage)
    return result


async def _handle_agent_task(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Spawn one sub-agent (``agent_task``)."""
    turn.agent_task_calls += 1
    args = call.args
    agent_name = args.get("name", "Sub-Agent")
    agent_prompt = args.get("prompt", "")
    agent_model = args.get("model") or ctx.model  # Fall back to parent's model
    agent_model = _DEPRECATED_MODELS.get(agent_model, agent_model)

    if turn.agent_task_calls > MAX_AGENT_TASKS_PER_TURN:
        return json.dumps({
            "error": f"Too many agent_task calls in a single turn: {turn.agent_task_calls} (maximum is {MAX_AGENT_TASKS_PER_TURN}). Use agent_task_parallel instead."
        })
    if agent_model in SUB_AGENT_DISALLOWED_MODELS:
        # Gemini 3.1 Pro is not permitted for sub-agents (it is
        # reserved for top-level conversations). Return a clean,
        # model-readable error so the model can re-spawn with an
        # allowed model on the same turn. Runs after the
        # _DEPRECATED_MODELS remap so the deprecated alias is also
        # blocked, and before any spawn so no tokens are spent.
        logger.info(
            "[agent_task] Rejected disallowed sub-agent model '%s' for '%s' (conversation=%s, user=%s)",
            agent_model, agent_name, ctx.conversation_id, ctx.user["email"],
        )
        return json.dumps({
            "error": f"Model '{agent_model}' is not permitted for sub-agents. "
                     f"Specify a different `model` (e.g. a faster/cheaper model "
                     f"such as gemini-3.5-flash-lite, or another capable model like "
                     f"claude-opus-4-8)."
        })

    # Resolve sub-agent provider (may differ from parent)
    try:
        sub_provider_name = get_provider_for_model(agent_model)
        sub_provider = get_provider_instance(
            sub_provider_name, model_instance_id(agent_model),
        )
    except ValueError:
        sub_provider = ctx.provider

    async def _runner(usage: dict) -> str:
        return await _run_sub_agent(
            app=ctx.app,
            provider=sub_provider,
            user=ctx.user,
            conversation_id=ctx.conversation_id,
            timezone=ctx.timezone,
            model=agent_model,
            agent_name=agent_name,
            prompt=agent_prompt,
            custom_prompt=ctx.custom_prompt,
            project_id=ctx.project_id,
            project_guide=ctx.resolved_project_guide,
            skills_content=ctx.resolved_skills_content,
            usage_accumulator=usage,
            on_event=ctx.on_event,
            parent_tool_id=call.tool_id,
            nested_enabled=ctx.nested_subagents,
            workspace_notice_flags=ctx.workspace_notice_flags,
            level=1,
        )

    return await _run_spawn_arm(
        ctx,
        arm="agent_task",
        spawn_desc=f"sub-agent '{agent_name}' (model={agent_model})",
        failure_prefix=f"Sub-agent '{agent_name}' failed",
        runner=_runner,
        error_event={
            "type": "sub_agent_finished",
            "parent_tool_id": call.tool_id,
            "agent_name": agent_name,
            "status": "error",
        },
    )


async def _handle_agent_task_parallel(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Spawn a batch of parallel sub-agents (``agent_task_parallel``)."""
    tasks_list = call.args.get("tasks", [])
    task_names = [t.get("name", "Sub-Agent") for t in tasks_list]

    async def _runner(usage: dict) -> str:
        return await _run_parallel_sub_agents(
            app=ctx.app,
            provider=ctx.provider,
            user=ctx.user,
            conversation_id=ctx.conversation_id,
            timezone=ctx.timezone,
            parent_model=ctx.model,
            tasks=tasks_list,
            custom_prompt=ctx.custom_prompt,
            project_id=ctx.project_id,
            project_guide=ctx.resolved_project_guide,
            skills_content=ctx.resolved_skills_content,
            usage_accumulator=usage,
            on_event=ctx.on_event,
            parent_tool_id=call.tool_id,
            nested_enabled=ctx.nested_subagents,
            workspace_notice_flags=ctx.workspace_notice_flags,
        )

    return await _run_spawn_arm(
        ctx,
        arm="agent_task_parallel",
        spawn_desc=f"{len(tasks_list)} sub-agents: {task_names}",
        failure_prefix="Parallel sub-agents failed",
        runner=_runner,
    )


async def _handle_agent_task_parallel_template(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Spawn templated parallel sub-agents (``agent_task_parallel_template``)."""
    args = call.args
    template_prompt = args.get("prompt_template", "")
    template_model = args.get("model", "")
    template_agents = args.get("agents", [])
    agent_names = [a.get("name", "Sub-Agent") for a in template_agents]

    async def _runner(usage: dict) -> str:
        return await _run_parallel_sub_agents_template(
            app=ctx.app,
            provider=ctx.provider,
            user=ctx.user,
            conversation_id=ctx.conversation_id,
            timezone=ctx.timezone,
            prompt_template=template_prompt,
            model=template_model,
            agents=template_agents,
            custom_prompt=ctx.custom_prompt,
            project_id=ctx.project_id,
            project_guide=ctx.resolved_project_guide,
            skills_content=ctx.resolved_skills_content,
            usage_accumulator=usage,
            on_event=ctx.on_event,
            parent_tool_id=call.tool_id,
            nested_enabled=ctx.nested_subagents,
            workspace_notice_flags=ctx.workspace_notice_flags,
        )

    return await _run_spawn_arm(
        ctx,
        arm="agent_task_parallel_template",
        spawn_desc=(
            f"{len(template_agents)} sub-agents: {agent_names} "
            f"(model={template_model})"
        ),
        failure_prefix="Template parallel sub-agents failed",
        runner=_runner,
    )


async def _handle_send_slack_reply(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Post a Slack thread reply and suspend until the user answers
    (``send_slack_reply_and_get_response``)."""
    from chat import slack_driven_runtime

    text = call.args.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return json.dumps({
            "error": "text is required and must be a non-empty string",
        })
    if not ctx.slack_context:
        return json.dumps({
            "error": "send_slack_reply_and_get_response is only available in Slack-driven conversations",
        })
    if len(text) > slack_driven_runtime.MAX_REPLY_CHARS:
        return json.dumps({
            "error": (
                f"text must be at most {slack_driven_runtime.MAX_REPLY_CHARS} "
                "characters; please retry with a shorter message"
            ),
        })

    channel = ctx.slack_context.get("channel_id", "")
    thread_ts = ctx.slack_context.get("thread_ts", "")
    try:
        post_ts = await slack_driven_runtime.post_thread_reply(
            channel, thread_ts, text,
        )
    except Exception as e:
        return json.dumps({
            "error": f"send_slack_reply_and_get_response failed: {e}",
        })

    # Register a slack_reply wait handle keyed implicitly by
    # (channel, thread_ts) via the row payload, then raise the suspend
    # sentinel. The Socket Mode handler resolves the row when the user
    # replies; the resume bucket on the next run closes the dangling
    # send_slack_reply_and_get_response tool_use using the row's
    # response.user_reply.
    handle = await tool_wait_handle_store.create_handle(
        user_id=ctx.user["id"],
        conversation_id=ctx.conversation_id,
        kind="slack_reply",
        tool_id=call.tool_id,
        payload={
            "channel": channel,
            "thread_ts": thread_ts,
            "posted_text": text,
            "posted_ts": post_ts,
        },
    )
    # Do NOT call set_typing("Quest is working...") here: the run is
    # about to SUSPEND awaiting the user's next reply, not actively
    # working. Slack auto-clears the assistant.threads.setStatus
    # indicator when the bot posts via chat.postMessage, so the indicator
    # is already off after post_thread_reply above. The resume bucket
    # re-sets "Quest is working..." when the user replies and processing
    # actually resumes.
    raise SuspendForSlackReply(
        handle_id=handle["id"],
        tool_id=call.tool_id,
        channel=channel,
        thread_ts=thread_ts,
    )


async def _handle_return_to_caller(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Cross-user subagent return call (``return_to_caller``): build a
    subagent_return action-request card for the TARGET user and suspend,
    mirroring the create_action_request arm."""
    from chat.action_request_types import get_handler as _get_ar_handler
    from chat.action_request_types.subagent_return import (
        prepare_return_params,
    )
    from db.user_subagent_run_store import (
        TERMINAL_STATUSES as _RUN_TERMINAL,
    )

    if not ctx.is_user_subagent or ctx.subagent_run is None:
        return json.dumps({
            "error": (
                "return_to_caller is only available inside "
                "cross-user subagent conversations."
            ),
        })
    if ctx.subagent_run.get("status") in _RUN_TERMINAL:
        return json.dumps({
            "error": (
                "This subagent run has already ended "
                f"(status: {ctx.subagent_run.get('status')}). "
                "Stop -- no further return calls are "
                "possible."
            ),
        })

    _return_handler = _get_ar_handler("subagent_return")
    try:
        validated_params = _return_handler.validate_params(call.args)
        validated_params = await prepare_return_params(
            validated_params, ctx.subagent_run, ctx.conversation_id,
        )
    except ValueError as e:
        return json.dumps({
            "error": f"Invalid parameters: {e}",
        })

    request_id, wait_handle_id = await _open_action_request_card(
        ctx,
        request_type="subagent_return",
        validated_params=validated_params,
        reasoning=call.args.get("reasoning", ""),
        card_reasoning="",
        handler=_return_handler,
        tool_id=call.tool_id,
    )

    from db.user_subagent_run_store import (
        update_run_status as _update_run_status,
    )
    from db.models import (
        UserSubagentRunStatus as _RunStatus,
    )
    await _update_run_status(
        ctx.subagent_run["id"],
        _RunStatus.AWAITING_RETURN,
    )

    await _publish_request_count(ctx.user["id"])

    raise SuspendForActionRequest(
        handle_id=wait_handle_id,
        tool_id=call.tool_id,
        request_id=request_id,
    )


async def _handle_return_final_response(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """One-shot inference API return call (``return_final_response``).

    The response markdown stays in the persisted tool_use (appended by
    the loop before dispatch); append the closing tool_result for a clean
    read-only transcript, then end the run via the
    :class:`FinishInferenceResponse` sentinel. The driver
    (chat/inference_api.py) extracts the response from messages_out.
    """
    if not ctx.is_inference_api:
        return json.dumps({
            "error": (
                "return_final_response is only available "
                "inside inference API conversations."
            ),
        })
    if not str(call.args.get("response") or "").strip():
        return json.dumps({
            "error": (
                "Invalid parameters: response must be a "
                "non-empty markdown string."
            ),
        })

    delivered = json.dumps({"status": "delivered"})
    await _emit_durable(ctx, {
        "type": "tool_result",
        "tool_id": call.tool_id,
        "tool_output": delivered,
    }, durable_extra={"role": "assistant"})
    raise FinishInferenceResponse(tool_id=call.tool_id)


async def _handle_routine_completed(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Routine-run completion marker (``routine_completed``).

    A pure signal: the persisted ``tool_use`` is what the run drivers in
    chat/routine_runs.py look for to decide whether the run finished or
    needs a follow-up nudge turn. Nothing to execute here beyond
    acknowledging the call; the model's turn then ends naturally. Only
    meaningful in routine conversations (a ``routine_id`` on the row).

    Registered under both the top-level name and the ``tool_call``-riding
    key (see :data:`ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY`); in the latter
    form the tool's own arguments sit under ``arguments``.
    """
    if not ctx.routine_id:
        return json.dumps({
            "error": (
                "routine_completed is only available inside routine "
                "conversations."
            ),
        })
    args = call.args
    if call.key == ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY:
        inner = args.get("arguments")
        args = inner if isinstance(inner, dict) else {}
    summary = args.get("summary")
    result: dict[str, Any] = {"status": "completed"}
    if isinstance(summary, str) and summary.strip():
        result["summary"] = summary.strip()
    return json.dumps(result)


async def _enrich_params_for_preview(
    req_type: str, validated_params: dict, user: dict,
) -> None:
    """Resolve human-readable names into ``validated_params`` (in place)
    for the approval card's preview display.

    Server-injected fields (not in the handlers' _ALLOWED_PARAMS), one
    ladder per request type. New handlers (and all plugin handlers) override the
    ``ActionRequestHandler.enrich_params_for_preview`` hook instead --
    called by ``_handle_create_action_request`` right after this ladder;
    the remaining arms here are slated to migrate onto that hook.
    """
    # (The send_slack_message and send_telegram_message arms moved onto
    # the plugin handlers' enrich_params_for_preview hooks --
    # plugins/slack/handlers.py and plugins/telegram/handlers.py.)

    if req_type == "create_calendar_invite":
        from chat.action_request_types import _resolve_calendar_name
        calendar_name = await _resolve_calendar_name(
            validated_params["calendar_id"], user,
        )
        if calendar_name:
            validated_params["calendar_name"] = calendar_name

    # Resolve Drive folder ids to human names for the preview cards.
    # Server-injected (not in _ALLOWED_PARAMS), parallel to calendar_name.
    if req_type == "upload_to_drive":
        from chat.action_request_types._drive import _resolve_folder_name
        if validated_params.get("folder_id"):
            folder_name = await _resolve_folder_name(
                validated_params["folder_id"], user,
            )
            if folder_name:
                validated_params["folder_name"] = folder_name
        if validated_params.get("new_folder_parent_id"):
            parent_name = await _resolve_folder_name(
                validated_params["new_folder_parent_id"], user,
            )
            if parent_name:
                validated_params["new_folder_parent_name"] = parent_name

    if req_type == "create_drive_folder" and validated_params.get("parent_folder_id"):
        from chat.action_request_types._drive import _resolve_folder_name
        parent_name = await _resolve_folder_name(
            validated_params["parent_folder_id"], user,
        )
        if parent_name:
            validated_params["parent_folder_name"] = parent_name


async def _handle_create_action_request(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Open an approval card and suspend (``create_action_request``)."""
    from chat.action_request_types import get_handler

    if ctx.is_slack_origin:
        # Slack-driven runs have no web UI to render the Approve / Revise
        # / Deny card, and the call now blocks on that resolution.
        # Short-circuit with a structured error instead of suspending
        # forever.
        return json.dumps({
            "error": (
                "create_action_request is not available in "
                "Slack-driven conversations. The approval "
                "UI lives in the Quest web app, which the "
                "user is not using for this conversation. "
                "Deliver your proposal as part of the Slack "
                "reply text instead."
            ),
        })
    if ctx.is_user_subagent:
        # Defense in depth: the tool is not in USER_SUBAGENT_TOOLS, but
        # never let a cross-user subagent propose writes to the target
        # user.
        return json.dumps({
            "error": (
                "create_action_request is not available in "
                "cross-user subagent conversations. Return "
                "your findings via return_to_caller instead."
            ),
        })

    args = call.args
    req_type = args.get("request_type", "")
    req_params = args.get("params", {})
    req_reasoning = args.get("reasoning", "")

    # Feature-flagged: only conversations started with the user_subagents
    # flag may launch cross-user subagent runs.
    flag_blocked = (
        req_type == "run_user_subagent"
        and not ctx.user_subagents_enabled
    )
    if req_type == "subagent_return" or flag_blocked:
        # subagent_return is created only by the return_to_caller arm
        # inside subagent conversations; the generic path must not mint
        # one.
        handler = None
    else:
        handler = get_handler(req_type)
    if flag_blocked:
        if not ctx.user_subagents_gate_open:
            blocked_error = (
                "Cross-user subagents are disabled "
                "server-wide. An admin can enable the "
                "feature in Settings > Features."
            )
        else:
            blocked_error = (
                "run_user_subagent requires the "
                "'user_subagents' conversation flag, "
                "which is not enabled here. Flags are "
                "set only on a conversation's FIRST "
                "message (the composer Flags popover, "
                "or a leading "
                "%%flags[user_subagents] line) -- tell "
                "the user to start a new conversation "
                "with the flag enabled."
            )
        return json.dumps({"error": blocked_error})
    if not handler:
        # Registry-derived (not the core enum) so plugin-registered types
        # are advertised too; subagent_return stays hidden.
        from chat.action_request_types import get_all_type_names
        supported = [
            name for name in get_all_type_names()
            if name != ActionRequestType.SUBAGENT_RETURN.value
        ]
        return json.dumps({
            "error": f"Unknown request type: {req_type}",
            "supported_types": supported,
        })

    try:
        validated_params = handler.validate_params(req_params)
        # Second-stage validation against the upstream service (e.g. a
        # plugin's upstream dry-run endpoint). Default no-op on the base
        # class. Network / 5xx fallthroughs are absorbed inside the
        # handlers as WARNINGs -- only ValueError reaches here.
        validated_params = await handler.validate_against_upstream(
            validated_params, ctx.user,
        )
        # Skill create/edit pre-card checks: name collision, project
        # access, and preview-name enrichment all run in the same async,
        # user-and-project_id-bearing place as the upstream dry-runs.
        # A rejection raises ValueError so it reuses the existing
        # "Invalid parameters" early-return below -- surfaced same-turn
        # BEFORE any card so the model can self-correct. The execute-time
        # guards in the handlers remain the authoritative TOCTOU close.
        if req_type in ("create_skill", "edit_skill"):
            from chat.action_request_types.skill_precard import (
                skill_precard_check,
            )
            await skill_precard_check(
                req_type, validated_params, ctx.user, ctx.project_id,
                conversation_id=ctx.conversation_id,
            )
        # Routine create/edit pre-card checks: project scoping, routine
        # existence, name collision, and skill access, plus preview
        # enrichment (current name, skill names, prompt content_diff) and
        # the optimistic-concurrency token capture. Same contract as the
        # skill pre-card check above.
        if req_type in ("create_routine", "edit_routine"):
            from chat.action_request_types.routine_precard import (
                routine_precard_check,
            )
            await routine_precard_check(
                req_type, validated_params, ctx.user, ctx.project_id,
            )
        # Quest Docs write_doc pre-card check: the doc access rule must say
        # "approval" (a directly writable doc is refused -- call the tool;
        # hidden / denied / unread docs get the tool's own error), and the
        # operation is dry-run against the live body, plus preview
        # enrichment (title, mode, scope, share summary, content_diff,
        # image preview). Same contract as the skill pre-card check above.
        if req_type == "write_doc":
            from chat.action_request_types.doc_precard import (
                doc_precard_check,
            )
            await doc_precard_check(
                validated_params, ctx.user, ctx.project_id,
                ctx.conversation_id,
            )
    except ValueError as e:
        return json.dumps({
            "error": f"Invalid parameters: {e}",
        })

    await _enrich_params_for_preview(req_type, validated_params, ctx.user)
    await handler.enrich_params_for_preview(validated_params, ctx.user)

    # NOTE: the wait handle + sentinel deliberately use the
    # provider-native call.raw_tool_id, not the loop-resolved fallback id
    # (pre-extraction behavior, preserved as-is).
    request_id, wait_handle_id = await _open_action_request_card(
        ctx,
        request_type=req_type,
        validated_params=validated_params,
        reasoning=req_reasoning,
        card_reasoning=req_reasoning,
        handler=handler,
        tool_id=call.raw_tool_id,
    )

    await _publish_request_count(ctx.user["id"])

    # Suspend the agent loop on the linked wait-handle row. The resume
    # bucket on the next run closes the dangling create_action_request
    # tool_use using the row's response, delivered to the model as the
    # verdict + optional feedback + result.
    raise SuspendForActionRequest(
        handle_id=wait_handle_id,
        tool_id=call.raw_tool_id,
        request_id=request_id,
    )


async def _handle_wait_for_handles(
    ctx: RunContext, call: ToolCall, turn: TurnState,
) -> str:
    """Block on wait-handle rows (``tool_call`` riding ``wait_for_handles``)."""
    if ctx.is_slack_origin:
        return json.dumps({
            "error": "wait_for_handles is not available in Slack-driven conversations.",
        })
    inner_args = call.args.get("arguments", {}) or {}
    if isinstance(inner_args, dict):
        inner_args.pop("intent_message", None)
    else:
        inner_args = {}
    return await _await_wait_for_handles(
        ctx, inner_args, tool_id=call.tool_id,
    )


# Loop-handled tools, keyed by registry key (see registry_key_for). Any
# tool call whose key is absent here falls through to the shared
# _dispatch_tool_call in the conversation loop.
TURN_TOOL_HANDLERS: dict[str, TurnToolHandler] = {
    "agent_task": _handle_agent_task,
    "agent_task_parallel": _handle_agent_task_parallel,
    "agent_task_parallel_template": _handle_agent_task_parallel_template,
    "send_slack_reply_and_get_response": _handle_send_slack_reply,
    "return_to_caller": _handle_return_to_caller,
    "return_final_response": _handle_return_final_response,
    ROUTINE_COMPLETED_KEY: _handle_routine_completed,
    ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY: _handle_routine_completed,
    "create_action_request": _handle_create_action_request,
    WAIT_FOR_HANDLES_KEY: _handle_wait_for_handles,
}
