"""Headless resume of a conversation suspended on a wait sentinel.

When a wait handle is resolved by an HTTP caller (or the Slack socket
worker) after the suspended dispatch arm is gone, this module kicks off
a fresh ``run_conversation_turn`` task that picks up the dangling tool_use and
closes it via the resume path's DB re-query.

Three suspend sentinels feed into here:
* ``wait_for_handles`` (web confirm-action / generic wait flow)
* ``send_slack_reply_and_get_response`` (Slack-driven thread suspend)
* ``create_action_request`` (web action-request Approve / Revise; a Stop
  resolves the handle but deliberately kicks NO resume -- see
  ``_stopped_handle_ids``)

For Slack-origin conversations the resume reads the conversation's
stored ``origin`` and the matching ``slack_conversations`` row so the
restarted ``run_conversation_turn`` boots with the right tools and
``slack_context``; otherwise the model would re-enter web mode and have
no way to reply to the user in Slack.
"""

import asyncio
import logging
from typing import Optional

from chat._flush_helper import FLUSH_EVENT_TYPES, FlushFn, make_flush_callback
from chat.storage import utc_timestamp

logger = logging.getLogger(__name__)


# Active resume tasks keyed by conversation_id, so we don't double-fire
# if the user clicks Accept on multiple stale cards in quick succession
# or if two REST callers race on the same handle resolution.
_active_resumes: dict[str, asyncio.Task] = {}


def get_active_resume(conversation_id: str) -> Optional[asyncio.Task]:
    """Return the in-flight resume task for a conversation, if any.

    Used by the WS ``stop`` handler: the FE shows the same stop button
    during a headless resume as during a send, so stop must be able to
    cancel the resume task too.
    """
    task = _active_resumes.get(conversation_id)
    if task is not None and not task.done():
        return task
    return None


def is_resuming(conversation_id: str) -> bool:
    """Return True if a resume task for this conversation is in flight."""
    return get_active_resume(conversation_id) is not None


# Conversations whose resume task has passed the hold gates and published
# ``resume_started`` -- i.e. is actually streaming. A held resume (sibling
# cards still pending, or a stopped card) sits in ``_active_resumes`` for a
# moment without ever emitting the paired lifecycle envelopes, so the WS
# ``run_active`` report must not count it as live: a client that latched a
# streaming state on it would wait for a ``send_message_finished`` that
# never comes.
_streaming_resumes: set[str] = set()


def is_resume_streaming(conversation_id: str) -> bool:
    """True while a resume run is streaming on this conversation (between
    its ``resume_started`` and ``send_message_finished`` publishes)."""
    return (
        conversation_id in _streaming_resumes
        and get_active_resume(conversation_id) is not None
    )


def _pending_tool_uses(conversation_id: str) -> list[tuple[str, str, dict]]:
    """Return the dangling ``(tool_id, name, args)`` tool_uses at the end of
    the conversation's ``sdk_history.json``.

    Delegates to the provider for inspecting the saved transcript so the
    quirks of each SDK's serialization (e.g. Gemini splitting one
    assistant turn across multiple Content entries) live in one place.
    Returns ``[]`` if the file is missing, the provider is unknown, or the
    check raises -- callers treat an empty list as "nothing to drive."
    """
    from chat.gemini_api.history import _load_sdk_history
    from chat.llm.config import get_provider_instance

    try:
        loaded = _load_sdk_history(conversation_id)
    except Exception:
        logger.debug(
            "[wait-resume] sdk_history load failed for %s",
            conversation_id, exc_info=True,
        )
        return []
    if loaded is None:
        return []
    history, provider_name = loaded
    if not history:
        return []

    try:
        provider = get_provider_instance(provider_name)
    except Exception:
        logger.debug(
            "[wait-resume] unknown provider %r for conversation %s",
            provider_name, conversation_id, exc_info=True,
        )
        return []

    try:
        return list(provider.get_pending_tool_use_args_from_history(history))
    except Exception:
        logger.debug(
            "[wait-resume] pending tool_use inspection failed for %s",
            conversation_id, exc_info=True,
        )
        return []


def _conversation_has_dangling_wait(conversation_id: str) -> bool:
    """Heuristic check: does sdk_history.json end on a suspendable tool use?

    Recognises all three suspend-by-sentinel cases:
    * ``wait_for_handles`` (web confirm-action / generic wait flow)
    * ``send_slack_reply_and_get_response`` (Slack-driven thread suspend)
    * ``create_action_request`` (web action-request Approve / Revise / Stop)
    * ``request_service_connection`` (web inline connect card)

    Returns False if the file is missing, the provider is unknown, or the
    check raises -- the caller treats False as "no resume needed."
    """
    pending = _pending_tool_uses(conversation_id)
    for _tid, name, args in pending:
        if name in (
            "wait_for_handles",
            "send_slack_reply_and_get_response",
            "create_action_request",
            "return_to_caller",
        ):
            return True
        if name == "tool_call" and (args or {}).get("tool_name") in (
            "wait_for_handles", "request_service_connection",
        ):
            return True
    return False


def _conversation_has_dangling_slack_reply(conversation_id: str) -> bool:
    """Like :func:`_conversation_has_dangling_wait`, but specifically for
    ``send_slack_reply_and_get_response`` -- used by Slack dispatch /
    resume tasks to decide whether the model is still in a user-wait
    suspend (in which case Slack already cleared the typing indicator)
    versus actually finished (in which case we must clear the indicator
    to avoid a stale "Quest is working..." sticking after the run).
    """
    return any(
        name == "send_slack_reply_and_get_response"
        for _tid, name, _args in _pending_tool_uses(conversation_id)
    )


# Wait-handle kinds whose row is created BY the dangling tool_use itself
# (row.tool_id == tool_use id), so a still-pending row means that
# specific tool_use has nothing to be closed with yet. ``wait_for_handles``
# is deliberately absent: its rows belong to other tools and its contract
# is "return when ANY resolves."
_ONE_ROW_PER_TOOL_USE_KINDS = frozenset({
    "action_request", "slack_reply", "service_connection",
})


async def _unresolved_blocking_handle_ids(
    user_id: int, conversation_id: str,
) -> list[str]:
    """Ids of still-pending wait handles that a resume would have to close
    with a ``still_waiting`` marker.

    A parallel tool-call batch can leave SEVERAL dangling
    ``create_action_request`` tool_uses on disk, one card each. Resolving
    the first card must NOT resume the conversation -- the resume bucket
    would close the other cards' tool_uses with ``still_waiting`` and the
    model would carry on without their verdicts. This returns the pending
    ``action_request`` / ``slack_reply`` rows whose ``tool_id`` is one of
    the dangling tool_uses; the resume proceeds only when it is empty.
    Scoping by dangling tool_id keeps a stale row from a long-gone turn
    from wedging the conversation.
    """
    from db import tool_wait_handle_store

    pending_rows = await tool_wait_handle_store.list_pending_for_conversation(
        user_id, conversation_id,
    )
    if not pending_rows:
        return []
    dangling_ids = {
        tid for tid, _name, _args in _pending_tool_uses(conversation_id) if tid
    }
    if not dangling_ids:
        return []
    return [
        row["id"] for row in pending_rows
        if row.get("kind") in _ONE_ROW_PER_TOOL_USE_KINDS
        and row.get("tool_id") in dangling_ids
    ]


async def _stopped_handle_ids(
    user_id: int, conversation_id: str,
) -> list[str]:
    """Ids of ``stopped`` wait handles whose tool_use is still dangling.

    Stop (the action-request card's third button) halts the conversation
    loop: the handle is terminal so the composer unlocks, but the model
    must NOT wake up headlessly -- its dangling ``create_action_request``
    tool_use is closed only by the user's next message, with that message
    appended in the same turn (see the resume bucket in
    ``run_conversation_turn``). A stray resume kick (e.g. the deferred
    re-kick after an earlier sibling approval) would otherwise resume the
    model with no user input. Scoped by dangling tool_id like
    :func:`_unresolved_blocking_handle_ids`.
    """
    from db import tool_wait_handle_store
    from db.models import ToolWaitHandleStatus

    dangling_ids = [
        tid for tid, _name, _args in _pending_tool_uses(conversation_id) if tid
    ]
    stopped: list[str] = []
    for tool_id in dangling_ids:
        row = await tool_wait_handle_store.get_handle_by_tool_id(
            user_id, conversation_id, tool_id,
        )
        if row is not None and row.get("status") == ToolWaitHandleStatus.STOPPED:
            stopped.append(row["id"])
    return stopped


async def _run_resume(
    app, user: dict, conversation_id: str,
) -> None:
    """Drive one ``run_conversation_turn`` continuation with no fresh user input.

    Picks up the conversation's stored ``origin`` so a Slack-driven
    conversation resumes with ``origin="slack"`` and the right
    ``slack_context`` -- otherwise the model would re-enter with web tools
    and have no way to reply to the user in Slack after closing the
    dangling ``send_slack_reply_and_get_response`` tool_use.

    Publishes the same run-lifecycle envelopes as a WS send --
    ``resume_started`` at the top, ``send_message_finished`` in the
    ``finally`` -- and mirrors transient events (text deltas, sub-agent
    updates) on the conversation channel, so viewing tabs render the
    resumed run live and hold the composer in the streaming/stop state
    until it ends. The two lifecycle publishes are paired inside this
    coroutine: a task cancelled before its first tick emits neither, so a
    client can never latch a streaming state that no terminal event will
    clear.
    """
    from chat.gemini_api import run_conversation_turn
    from chat.realtime.bus import bus
    from chat.realtime.socket import _publish_transient_event
    from db.conversation_store import get_conversation_meta

    # Multi-card gate: hold the continuation while any sibling card of a
    # parallel create_action_request batch is still pending. Best-effort
    # -- a failing check falls through to the pre-gate behavior. Runs
    # BEFORE ``resume_started`` so a held resume publishes nothing and the
    # composer stays locked on the remaining pending_wait_handles.
    try:
        still_pending = await _unresolved_blocking_handle_ids(
            user["id"], conversation_id,
        )
    except Exception:
        logger.debug(
            "[wait-resume] pending-handle gate failed (conversation=%s)",
            conversation_id, exc_info=True,
        )
        still_pending = []
    if still_pending:
        logger.info(
            "[wait-resume] Holding resume for conversation %s: %d "
            "dangling tool_use(s) still have pending wait handles (%s)",
            conversation_id, len(still_pending), still_pending,
        )
        return

    # Stop gate: a stopped card means "wait for the user's next message",
    # never a headless wake-up. Best-effort like the gate above.
    try:
        stopped = await _stopped_handle_ids(user["id"], conversation_id)
    except Exception:
        logger.debug(
            "[wait-resume] stopped-handle gate failed (conversation=%s)",
            conversation_id, exc_info=True,
        )
        stopped = []
    if stopped:
        logger.info(
            "[wait-resume] Holding resume for conversation %s: %d "
            "dangling tool_use(s) were stopped by the user (%s); waiting "
            "for the next user message", conversation_id, len(stopped), stopped,
        )
        return

    # Mark the run live BEFORE ``resume_started`` goes out so a subscribe
    # ack computed after that publish reports ``run_active: true``.
    _streaming_resumes.add(conversation_id)
    try:
        bus.publish_to_conversation(conversation_id, {
            "type": "resume_started",
            "conversation_id": conversation_id,
        })
    except Exception:
        logger.debug(
            "[wait-resume] publish resume_started failed (conversation=%s)",
            conversation_id, exc_info=True,
        )

    messages_out: list[dict] = []
    _flush: Optional[FlushFn] = None
    interrupted = False
    run_failed = False
    origin = "web"
    slack_context: Optional[dict] = None
    suspended_for_slack_reply = False

    try:
        meta = await get_conversation_meta(user["id"], conversation_id)
        model = (meta or {}).get("model")
        project_id = (meta or {}).get("project_id")
        routine_id = (meta or {}).get("routine_id")
        origin = (meta or {}).get("origin") or "web"

        if origin == "slack":
            try:
                from chat.slack_conversation_store import (
                    get_slack_conversation_by_id,
                )
                slack_row = await get_slack_conversation_by_id(conversation_id)
            except Exception:
                logger.exception(
                    "[wait-resume] Failed to look up slack_conversations row "
                    "for conversation %s; falling back to web-mode resume",
                    conversation_id,
                )
                slack_row = None
            if slack_row is not None:
                slack_context = {
                    "channel_id": slack_row.get("slack_channel_id", ""),
                    "thread_ts": slack_row.get("slack_thread_ts", ""),
                }
            else:
                # No row to recover Slack thread coordinates -- the model has
                # nowhere to post its next reply, so dropping back to web mode
                # would still leave the conversation broken. Log loudly and
                # bail; the Socket Mode path will retry on the next user reply.
                logger.error(
                    "[wait-resume] origin=slack but no slack_conversations row "
                    "found for conversation %s; cannot dispatch Slack resume",
                    conversation_id,
                )
                return

        # Slack-driven runs need to be tracked in slack_driven_runtime's
        # _active_runs registry so Socket Mode's is_run_active check sees the
        # in-flight resume (otherwise a fresh user reply mid-resume would
        # double-dispatch a parallel turn against the same conversation).
        if origin == "slack":
            from chat import slack_driven_runtime
            current_task = asyncio.current_task()
            if current_task is not None:
                slack_driven_runtime.register_run(conversation_id, current_task)
                current_task.add_done_callback(
                    lambda t, cid=conversation_id: slack_driven_runtime.unregister_run(cid)
                )
            # Re-assert the typing indicator now that processing is actually
            # resuming (Slack auto-cleared it when the bot posted the previous
            # reply, and we deliberately did NOT re-set it before the suspend
            # so the user-wait period showed no stale "working" status). This
            # mirrors the legacy await_user_reply path that called set_typing
            # right after the user's reply unblocked the future.
            if slack_context is not None:
                try:
                    await slack_driven_runtime.set_typing(
                        slack_context.get("channel_id", ""),
                        slack_context.get("thread_ts", ""),
                        "Quest is working...",
                    )
                except Exception:
                    logger.debug(
                        "[wait-resume] set_typing on resume start failed",
                        exc_info=True,
                    )

        # Load per-conversation flags so a resumed turn honors the same opt-in
        # behaviors (e.g. nested_subagents) as the original turn. Flags are set at
        # the start of the conversation and read from the DB on every turn; the
        # resume path has no ``meta`` dict so it queries directly. Best-effort.
        from db.conversation_store import get_conversation_flags
        try:
            resumed_flags = await get_conversation_flags(conversation_id)
        except Exception:
            logger.debug(
                "[wait-resume] get_conversation_flags failed (conversation=%s)",
                conversation_id, exc_info=True,
            )
            resumed_flags = []

        _flush, _ = make_flush_callback(
            conversation_id, messages_out, log_prefix="[wait-resume]",
        )
        flush = _flush

        async def _on_event(event: dict) -> None:
            if event.get("type") in FLUSH_EVENT_TYPES:
                await flush()
            # Transient mirroring on the conversation channel (text deltas,
            # sub-agent updates), same as the WS send path, so viewing tabs
            # stream the resumed run live instead of only seeing durable
            # message_appended boundaries.
            try:
                _publish_transient_event(conversation_id, event)
            except Exception:
                logger.debug(
                    "[wait-resume] publish_transient_event failed "
                    "(conversation=%s, type=%s)",
                    conversation_id, event.get("type"),
                    exc_info=True,
                )

        await run_conversation_turn(
            app=app,
            user=user,
            message="",
            conversation_id=conversation_id,
            timezone="UTC",
            model=model,
            on_event=_on_event,
            messages_out=messages_out,
            project_id=project_id,
            routine_id=routine_id,
            origin=origin,
            slack_context=slack_context,
            flags=resumed_flags or None,
        )
        # Detect a slack_reply suspend by inspecting the saved transcript:
        # run_conversation_turn converts SuspendForSlackReply into a normal return,
        # so the dispatcher cannot tell apart "model finished" from "model
        # suspended awaiting user reply" by the return alone. A dangling
        # send_slack_reply_and_get_response tool_use on disk means the latter,
        # in which case Slack already auto-cleared the typing indicator on
        # the chat.postMessage and we must NOT re-clear it (clearing is fine
        # only when the run truly ended, which is what we use it for below).
        if origin == "slack":
            try:
                suspended_for_slack_reply = (
                    _conversation_has_dangling_slack_reply(conversation_id)
                )
            except Exception:
                suspended_for_slack_reply = False
    except asyncio.CancelledError:
        interrupted = True
        logger.info(
            "[wait-resume] Resume task cancelled (conversation=%s)",
            conversation_id,
        )
        try:
            from chat.routes._helpers import _save_interrupted_sdk_history
            from chat.gemini_api.session import remove_chat_session
            _save_interrupted_sdk_history(
                user["id"], conversation_id, messages_out,
            )
            remove_chat_session(user["id"], conversation_id)
        except Exception:
            logger.exception(
                "[wait-resume] Failed to save interrupted SDK history "
                "(conversation=%s)", conversation_id,
            )
        raise
    except Exception:
        run_failed = True
        logger.exception(
            "[wait-resume] run_conversation_turn failed (conversation=%s)",
            conversation_id,
        )
    finally:
        if interrupted and _flush is not None:
            # Durable marker mirroring the send path's stop handling, so a
            # stopped resume shows "interrupted" after a reload too.
            messages_out.append({
                "type": "interrupted",
                "timestamp": utc_timestamp(),
            })
        if _flush is not None:
            try:
                await _flush()
            except Exception:
                logger.exception(
                    "[wait-resume] Final flush failed (conversation=%s)",
                    conversation_id,
                )
        # Clear the Slack typing indicator now that this resume task has
        # finished, EXCEPT when the model suspended again on another
        # send_slack_reply_and_get_response (in which case Slack already
        # cleared it on the chat.postMessage and we should not race the
        # suspend bucket). For natural turn-end / errors / cancellations,
        # clearing prevents the stale "Quest is working..." indicator that
        # would otherwise persist until the next user message.
        if origin == "slack" and not suspended_for_slack_reply and slack_context is not None:
            try:
                from chat import slack_driven_runtime
                await slack_driven_runtime.set_typing(
                    slack_context.get("channel_id", ""),
                    slack_context.get("thread_ts", ""),
                    "",
                )
            except Exception:
                logger.debug(
                    "[wait-resume] set_typing clear on resume end failed",
                    exc_info=True,
                )
        # Terminal lifecycle envelope for every subscribed tab -- same type
        # as the WS send path so the FE drains its streaming buffer and
        # re-enables the composer through one code path.
        try:
            bus.publish_to_conversation(conversation_id, {
                "type": "send_message_finished",
                "conversation_id": conversation_id,
                "interrupted": interrupted,
                "error": run_failed,
            })
        except Exception:
            logger.debug(
                "[wait-resume] publish send_message_finished failed "
                "(conversation=%s)", conversation_id, exc_info=True,
            )
        # Same event-loop step as the publish above: nothing may await
        # between the two, so a subscribe ack never sees a finished run
        # as live (``is_resume_streaming``).
        _streaming_resumes.discard(conversation_id)


def _rekick_when_done(
    blocker: asyncio.Task, app, user: dict, conversation_id: str,
) -> None:
    """Re-run :func:`maybe_kick_resume` once ``blocker`` completes.

    The re-entry re-checks ``_conversation_has_dangling_wait``, so this
    is a no-op when the blocking run already closed the dangling
    tool_use itself.
    """
    blocker.add_done_callback(
        lambda _t: maybe_kick_resume(app, user, conversation_id)
    )


def maybe_kick_resume(
    app, user: dict, conversation_id: str,
) -> Optional[asyncio.Task]:
    """Start a headless resume task if one is not already running.

    Returns the task (newly created or pre-existing), or None when no
    resume is appropriate (e.g. saved history has no dangling
    wait_for_handles, so there is nothing to drive).

    Safe to call from any HTTP handler: the heavy work runs on the asyncio
    loop after the request returns.
    """
    existing = _active_resumes.get(conversation_id)
    if existing is not None and not existing.done():
        # The in-flight resume may be about to finish WITHOUT seeing the
        # handle that was just resolved (e.g. the user approved the next
        # card while the previous resume task was still unwinding from
        # its suspend). Re-check once it completes so that resolution is
        # never silently dropped.
        _rekick_when_done(existing, app, user, conversation_id)
        return existing

    # Cross-guard against an in-flight WS send run: both paths drive
    # run_conversation_turn over the same shared in-memory session, and
    # concurrent appends interleave messages, orphaning tool_use blocks
    # (every later Anthropic call then 400s). Defer until the send
    # completes; the re-entry no-ops if the send run itself closed the
    # dangling tool_use.
    try:
        from chat.realtime.socket import get_active_send_run
        send_task = get_active_send_run(conversation_id)
    except Exception:
        send_task = None
    if send_task is not None:
        logger.info(
            "[wait-resume] Send run active for conversation %s; deferring "
            "resume until it completes", conversation_id,
        )
        _rekick_when_done(send_task, app, user, conversation_id)
        return None

    if not _conversation_has_dangling_wait(conversation_id):
        return None

    task = asyncio.create_task(_run_resume(app, user, conversation_id))
    _active_resumes[conversation_id] = task
    task.add_done_callback(
        lambda t, cid=conversation_id: _active_resumes.pop(cid, None)
    )
    logger.info(
        "[wait-resume] Kicked headless resume for conversation %s",
        conversation_id,
    )
    return task
