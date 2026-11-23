"""Runtime for cross-user subagent runs (run_user_subagent action requests).

When user A approves a ``run_user_subagent`` action request, a new
conversation with ``origin="user_subagent"`` is created in target user B's
account and driven headlessly through ``run_conversation_turn`` by this module
(mirroring ``chat/wait_handles/resume.py``). The subagent runs with B's
credentials and a restricted toolset (no action requests, no sub-agent
spawning) plus one extra tool, ``return_to_caller``, whose dispatch arm
creates a ``subagent_return`` action-request card for B to approve.

Lifecycle transitions live on the ``user_subagent_runs`` row
(db/user_subagent_run_store.py):

* ``running``          -- this module is (or will be) driving the loop
* ``awaiting_return``  -- suspended on a subagent_return card
* ``returned``         -- B approved; response + files delivered to A
* ``denied``           -- B hard-denied the return call (no feedback)
* ``failed``           -- exception, or the model never called
                          return_to_caller despite a nudge

The caller side blocks on a ``user_subagent`` wait handle in A's
conversation (via ``wait_for_handles``); :func:`resolve_caller_handle`
flips it and kicks a headless resume of A's conversation.
"""

import asyncio
import logging
from typing import Optional

from chat._flush_helper import FLUSH_EVENT_TYPES, FlushFn, make_flush_callback
from db.models import ToolWaitHandleStatus, UserSubagentRunStatus

logger = logging.getLogger(__name__)

# App reference set at startup (quest.py lifespan) so handler execute()
# paths and this module's background tasks can kick headless resumes.
# Mirrors chat/slack_driven_runtime.py.
_app_ref = None

# Strong references to in-flight run tasks so they aren't garbage-collected
# mid-run. Keyed by run id.
_active_runs: dict[str, asyncio.Task] = {}

# How many times to remind the model to call return_to_caller when a loop
# ends without either a suspend or a terminal status.
_MAX_RETURN_NUDGES = 1

_NUDGE_MESSAGE = (
    "You ended your turn without calling return_to_caller. You MUST finish "
    "this subagent run by calling return_to_caller(response=..., files=[...]) "
    "with your findings (files are optional). If you cannot complete the "
    "task, call return_to_caller with a response explaining why."
)


def set_app_ref(app) -> None:
    """Install the FastAPI app reference for headless run/resume kicks."""
    global _app_ref
    _app_ref = app


def start_run(run: dict) -> Optional[asyncio.Task]:
    """Spawn the background task driving a freshly-created subagent run.

    Called from RunUserSubagentHandler.execute() right after the run row,
    conversation, and caller-side wait handle exist. Returns the task, or
    None when one is already in flight for this run id.
    """
    return _spawn(run, run["prompt"])


def resume_run_after_revise(run: dict) -> Optional[asyncio.Task]:
    """Continue a run after the target user clicked Revise on its card.

    The action_request_routes deny branch has already resolved the
    B-side handle with the feedback; driving ``run_conversation_turn`` with an
    empty message lets the resume bucket close the dangling
    ``return_to_caller`` tool_use with that verdict, so the model sees
    the feedback and can propose another return call. Driven here (with
    the generic resume kick suppressed) so the ended-without-return
    nudge/fail lifecycle applies to revised loops too.
    """
    return _spawn(run, None)


def _spawn(run: dict, message: Optional[str]) -> Optional[asyncio.Task]:
    existing = _active_runs.get(run["id"])
    if existing is not None and not existing.done():
        return None
    task = asyncio.create_task(_drive_run(run, message))
    _active_runs[run["id"]] = task
    task.add_done_callback(
        lambda t, rid=run["id"]: _active_runs.pop(rid, None)
    )
    return task


async def resolve_caller_handle(
    run: dict, new_status: str, response: dict,
) -> Optional[dict]:
    """Flip the caller-side ``user_subagent`` wait handle and wake user A.

    Resolves the handle row (first write wins), publishes
    ``wait_handle_resolved`` on the caller's per-user channel so their
    composer unlocks, and kicks a headless resume of the caller
    conversation so a suspended ``wait_for_handles`` closes with this
    response. Returns the updated handle row, or None when it was already
    resolved (timed out / cancelled) -- callers decide what that means.
    """
    from chat.wait_handles import resume as wait_resume
    from chat.wait_handles.publish import publish_wait_handle_resolved
    from db import tool_wait_handle_store
    from db.user_store import get_user_by_id

    updated = await tool_wait_handle_store.resolve_handle(
        run["wait_handle_id"],
        new_status=new_status,
        response=response,
        correlation_kind="user_subagent_run",
        correlation_id=run["id"],
        user_id=run["caller_user_id"],
    )
    if updated is None:
        logger.info(
            "[user-subagent] Caller handle %s for run %s was already "
            "resolved; not delivering %s",
            run["wait_handle_id"], run["id"], response.get("status"),
        )
        return None

    publish_wait_handle_resolved(
        run["caller_user_id"], updated,
        response=response, default_kind="user_subagent",
        conversation_id=run["caller_conversation_id"],
    )

    caller = await get_user_by_id(run["caller_user_id"])
    if caller is None:
        logger.warning(
            "[user-subagent] Caller user %s not found; cannot kick resume "
            "for conversation %s",
            run["caller_user_id"], run["caller_conversation_id"],
        )
        return updated
    try:
        wait_resume.maybe_kick_resume(
            _app_ref, caller, run["caller_conversation_id"],
        )
    except Exception:
        logger.exception(
            "[user-subagent] Failed to kick caller resume (run=%s, "
            "conversation=%s)", run["id"], run["caller_conversation_id"],
        )
    return updated


async def fail_run(run: dict, error: str) -> None:
    """Mark a run failed and deliver the failure to the caller."""
    from db.user_subagent_run_store import update_run_status

    logger.warning(
        "[user-subagent] Run %s failed: %s", run["id"], error,
    )
    await update_run_status(
        run["id"], UserSubagentRunStatus.FAILED, error=error,
    )
    await resolve_caller_handle(
        run,
        ToolWaitHandleStatus.REJECTED,
        {"status": "failed", "error": error},
    )


async def finalize_denied_return(run: dict) -> None:
    """Handle a hard deny (no feedback) of a subagent_return card.

    Ends the run immediately: the subagent conversation is NOT resumed
    (the action_request_routes deny branch suppresses its resume kick),
    and the caller learns the target user denied the return call.
    """
    from db.user_subagent_run_store import update_run_status

    await update_run_status(run["id"], UserSubagentRunStatus.DENIED)
    await resolve_caller_handle(
        run,
        ToolWaitHandleStatus.REJECTED,
        {
            "status": "denied",
            "note": (
                "The target user denied the subagent's return call. "
                "No response or files were shared."
            ),
        },
    )


async def on_return_revised(run: dict) -> None:
    """Handle a revise (deny + feedback) of a subagent_return card.

    Flips the run back to ``running`` and restarts the loop via
    :func:`resume_run_after_revise` so the model reads the feedback off
    the closed ``return_to_caller`` tool_use and can try again.
    """
    from db.user_subagent_run_store import update_run_status

    updated = await update_run_status(
        run["id"], UserSubagentRunStatus.RUNNING,
    )
    if updated is not None:
        resume_run_after_revise(updated)


async def _drive_run(run: dict, initial_message: Optional[str]) -> None:
    """Drive the subagent conversation loop until it suspends or ends.

    ``initial_message`` is the approved prompt for a fresh run, or None
    for a post-revise continuation (empty message -- the resume bucket
    closes the dangling ``return_to_caller`` tool_use with the revise
    feedback).

    Mirrors ``chat/wait_handles/resume.py:_run_resume``: durable
    persistence via the flush callback, transient event mirroring on the
    conversation channel so any tab the target user has open streams the
    run live, and paired ``resume_started`` / ``send_message_finished``
    lifecycle envelopes.
    """
    from chat.gemini_api import run_conversation_turn
    from chat.realtime.bus import bus
    from chat.realtime.socket import _publish_transient_event
    from chat.storage import ChatStorage, utc_timestamp
    from db.conversation_store import get_conversation_meta
    from db.user_store import get_user_by_id
    from db.user_subagent_run_store import get_run

    conversation_id = run["subagent_conversation_id"]

    target = await get_user_by_id(run["target_user_id"])
    if target is None:
        await fail_run(run, "Target user no longer exists.")
        return

    try:
        bus.publish_to_conversation(conversation_id, {
            "type": "resume_started",
            "conversation_id": conversation_id,
        })
    except Exception:
        logger.debug(
            "[user-subagent] publish resume_started failed (run=%s)",
            run["id"], exc_info=True,
        )

    messages_out: list[dict] = []
    _flush: Optional[FlushFn] = None
    run_failed = False

    try:
        meta = await get_conversation_meta(target["id"], conversation_id)
        model = (meta or {}).get("model")
        user_tz = (target.get("settings") or {}).get("timezone") or "UTC"

        _flush, _ = make_flush_callback(
            conversation_id, messages_out, log_prefix="[user-subagent]",
        )
        flush = _flush

        async def _on_event(event: dict) -> None:
            if event.get("type") in FLUSH_EVENT_TYPES:
                await flush()
            try:
                _publish_transient_event(conversation_id, event)
            except Exception:
                logger.debug(
                    "[user-subagent] publish_transient_event failed "
                    "(run=%s, type=%s)",
                    run["id"], event.get("type"), exc_info=True,
                )

        # Headless runs are driven with a blank ``api_key``, the same way
        # chat/inference_api.py drives its runs: in-process tools
        # authenticate from the user dict itself, and run_script/run_python
        # containers get a per-run sandbox token minted from ``user["id"]``.
        run_user = {**target, "api_key": ""}

        message = initial_message if initial_message is not None else ""
        for attempt in range(_MAX_RETURN_NUDGES + 1):
            await run_conversation_turn(
                app=_app_ref,
                user=run_user,
                message=message,
                conversation_id=conversation_id,
                timezone=user_tz,
                model=model,
                on_event=_on_event,
                messages_out=messages_out,
                origin="user_subagent",
            )

            # A suspend on the subagent_return card flips the run to
            # awaiting_return inside the return_to_caller dispatch arm;
            # any terminal state means a finalizer already ran. Only a
            # loop that ended while still "running" needs a nudge.
            current = await get_run(run["id"])
            status = (current or {}).get("status")
            if status != UserSubagentRunStatus.RUNNING:
                return

            if attempt < _MAX_RETURN_NUDGES:
                logger.info(
                    "[user-subagent] Run %s ended its loop without a "
                    "return call; nudging (attempt %d)",
                    run["id"], attempt + 1,
                )
                message = _NUDGE_MESSAGE
                try:
                    await ChatStorage.append_message(
                        conversation_id, "user", _NUDGE_MESSAGE,
                        timestamp=utc_timestamp(),
                    )
                except Exception:
                    logger.debug(
                        "[user-subagent] persisting nudge message failed "
                        "(run=%s)", run["id"], exc_info=True,
                    )

        await fail_run(
            run,
            "The subagent ended without calling return_to_caller, so "
            "there is no response to deliver.",
        )
    except asyncio.CancelledError:
        logger.info(
            "[user-subagent] Run task cancelled (run=%s)", run["id"],
        )
        raise
    except Exception as e:
        run_failed = True
        logger.exception(
            "[user-subagent] run_conversation_turn failed (run=%s)", run["id"],
        )
        await fail_run(run, f"Subagent run failed: {type(e).__name__}: {e}")
    finally:
        if _flush is not None:
            try:
                await _flush()
            except Exception:
                logger.exception(
                    "[user-subagent] Final flush failed (run=%s)", run["id"],
                )
        try:
            bus.publish_to_conversation(conversation_id, {
                "type": "send_message_finished",
                "conversation_id": conversation_id,
                "interrupted": False,
                "error": run_failed,
            })
        except Exception:
            logger.debug(
                "[user-subagent] publish send_message_finished failed "
                "(run=%s)", run["id"], exc_info=True,
            )
