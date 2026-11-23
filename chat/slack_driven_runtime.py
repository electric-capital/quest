"""Runtime state and helpers for Slack-driven conversations.

The model signals "my reply is ready" by calling
``send_slack_reply_and_get_response(text=...)``. That tool posts the
reply to Slack, registers a ``slack_reply`` ``tool_wait_handles`` row
keyed implicitly by ``(channel, thread_ts)`` via its payload, and
raises the :class:`SuspendForSlackReply` sentinel to suspend the
conversation. When the user replies in that thread, the Socket Mode
handler resolves the matching row and kicks a headless resume so the
model gets the next turn.

Rapid successive user replies are debounced so the model sees them as
a single collated tool result instead of a flurry of tiny turns. The
debounce buffer is purely a per-process accelerator -- losing it on
restart is fine; the next reply just starts a fresh debounce window.
"""

import asyncio
import logging
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# How long to wait for additional user replies before flushing the
# debounce buffer. 1.5s strikes a balance between "send-send-send"
# coalescence and responsiveness.
DEBOUNCE_SECONDS = 1.5

# Hard cap on text posted via send_slack_reply_and_get_response. Slack
# chat.postMessage accepts up to ~3000 chars on Assistant threads.
MAX_REPLY_CHARS = 3000


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Debounce state per (channel, thread_ts). Each entry is {"buf": [...],
# "task": asyncio.Task or None}. Lost on restart -- the wait-handle row
# is the durable record of the suspension.
_pending_debounces: dict[tuple[str, str], dict] = {}
_active_runs: dict[str, asyncio.Task] = {}

_web_client: Optional[AsyncWebClient] = None
_typing_disabled: bool = False

# App reference set at startup so the debounce flush can call
# wait_resume.maybe_kick_resume on resolution. Slack-driven runs do not
# have a per-call ``app`` argument the way the web path does; they share
# the FastAPI app set up in quest.py's lifespan.
_app_ref = None


def set_web_client(client: Optional[AsyncWebClient]) -> None:
    """Install the AsyncWebClient used by post_thread_reply / set_typing."""
    global _web_client
    _web_client = client


def set_app_ref(app) -> None:
    """Install the FastAPI app reference for headless resume kicks."""
    global _app_ref
    _app_ref = app


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

def register_run(conversation_id: str, task: asyncio.Task) -> None:
    _active_runs[conversation_id] = task


def unregister_run(conversation_id: str) -> None:
    _active_runs.pop(conversation_id, None)


def is_run_active(conversation_id: str) -> bool:
    task = _active_runs.get(conversation_id)
    return task is not None and not task.done()


# ---------------------------------------------------------------------------
# Slack Web API helpers
# ---------------------------------------------------------------------------

async def post_thread_reply(channel: str, thread_ts: str, text: str) -> str:
    """Post a threaded reply via chat.postMessage and return the reply ts.

    Raises if the Web client is not configured.
    """
    if _web_client is None:
        raise RuntimeError("Slack Web client is not configured")

    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS]

    resp = await _web_client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text,
    )
    return resp.get("ts", "")


async def set_typing(channel: str, thread_ts: str, status_text: str) -> None:
    """Best-effort ``assistant.threads.setStatus`` call.

    No-ops and caches the failure if the Assistant feature / scope
    isn't available on this Slack app.
    """
    global _typing_disabled
    if _typing_disabled or _web_client is None:
        return
    try:
        await _web_client.assistant_threads_setStatus(
            channel_id=channel, thread_ts=thread_ts, status=status_text,
        )
    except Exception as e:
        logger.debug(
            "[slack-runtime] assistant.threads.setStatus failed (disabling): %s",
            e,
        )
        _typing_disabled = True


# ---------------------------------------------------------------------------
# Debounced user-reply -> wait-handle resolution
# ---------------------------------------------------------------------------

async def _flush_after(channel: str, thread_ts: str, delay: float) -> None:
    """After ``delay`` seconds, resolve the pending slack_reply row with the
    collated buffer and kick a headless resume."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return

    key = (channel, thread_ts)
    state = _pending_debounces.pop(key, None)
    if state is None:
        return

    collated = "\n\n".join(part for part in state.get("buf", []) if part)

    from db import tool_wait_handle_store
    from db.models import ToolWaitHandleStatus
    handle = await tool_wait_handle_store.find_pending_slack_reply(
        channel, thread_ts,
    )
    if handle is None:
        # The suspended run was cancelled (e.g. shutdown / explicit cancel)
        # before the debounce fired. Nothing to drive.
        logger.info(
            "[slack-runtime] Debounce flushed but no pending slack_reply row "
            "(channel=%s, thread_ts=%s); dropping collated reply.",
            channel, thread_ts,
        )
        return

    posted_ts = (handle.get("payload") or {}).get("posted_ts", "")
    updated = await tool_wait_handle_store.resolve_handle(
        handle["id"],
        new_status=ToolWaitHandleStatus.ACCEPTED,
        response={"user_reply": collated, "posted_ts": posted_ts},
        user_id=handle["user_id"],
    )
    if updated is None:
        # Lost a race with another resolver -- fine, the winning write
        # already drove (or will drive) the resume.
        return

    from chat.wait_handles.publish import publish_wait_handle_resolved
    publish_wait_handle_resolved(
        handle["user_id"], updated,
        response={"user_reply": collated, "posted_ts": posted_ts},
        default_kind="slack_reply",
    )

    if _app_ref is None:
        logger.warning(
            "[slack-runtime] No app reference set; cannot kick resume for "
            "slack_reply handle %s (conversation=%s)",
            handle["id"], handle["conversation_id"],
        )
        return

    try:
        from db.user_store import get_user_by_id
        user = await get_user_by_id(handle["user_id"])
    except Exception:
        logger.exception(
            "[slack-runtime] Failed to load user %s for resume kick",
            handle["user_id"],
        )
        return
    if user is None:
        logger.warning(
            "[slack-runtime] User %s not found; cannot kick resume for "
            "slack_reply handle %s",
            handle["user_id"], handle["id"],
        )
        return

    try:
        from chat.wait_handles import resume as wait_resume
        wait_resume.maybe_kick_resume(_app_ref, user, handle["conversation_id"])
    except Exception:
        logger.exception(
            "[slack-runtime] Failed to kick resume for slack_reply handle %s "
            "(conversation=%s)",
            handle["id"], handle["conversation_id"],
        )


async def enqueue_user_reply(
    channel: str, thread_ts: str, text: str,
) -> bool:
    """Buffer a user reply and schedule a debounce flush.

    Returns True if there is a pending ``slack_reply`` wait-handle row to
    resolve (i.e. the model is currently suspended waiting for a reply).
    If there is no pending row, caller should fall through to start a
    new turn.
    """
    from db import tool_wait_handle_store
    handle = await tool_wait_handle_store.find_pending_slack_reply(
        channel, thread_ts,
    )
    if handle is None:
        return False

    key = (channel, thread_ts)
    state = _pending_debounces.get(key)
    if state is None:
        state = {"buf": [], "task": None}
        _pending_debounces[key] = state

    state["buf"].append(text)

    prev_task = state.get("task")
    if prev_task is not None and not prev_task.done():
        prev_task.cancel()

    state["task"] = asyncio.create_task(
        _flush_after(channel, thread_ts, DEBOUNCE_SECONDS)
    )
    return True


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def shutdown() -> None:
    """Cancel debounce timers and active runs.

    Pending ``slack_reply`` wait-handle rows are NOT touched -- they
    survive the restart, and the resume bucket on the next run picks up
    the dangling ``send_slack_reply_and_get_response`` tool_use.
    """
    # Cancel all debounce tasks
    for state in list(_pending_debounces.values()):
        task = state.get("task")
        if task is not None and not task.done():
            task.cancel()
    _pending_debounces.clear()

    # Cancel active runs
    for task in list(_active_runs.values()):
        if not task.done():
            task.cancel()
    _active_runs.clear()
