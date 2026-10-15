"""Expiry enforcement for wait handles: per-handle in-memory timers plus a
durable ``expires_at`` sweep.

Two mechanisms flip an overdue ``pending`` row to ``timed_out`` and wake its
conversation, and both go through :func:`_announce_timeouts` afterwards
(``wait_handle_resolved`` on the user channel, then
:func:`chat.wait_handles.resume.maybe_kick_resume`):

- **Timers.** When ``_await_wait_for_handles`` raises
  :class:`SuspendForWaitHandles`, it first persists the deadline on the rows
  (``expires_at``) and calls :func:`schedule_timeout_for_handles` to register
  a background ``asyncio.Task`` per handle that sleeps for
  ``timeout_seconds``. If the handle is resolved before the deadline, the
  task observes the non-pending status when it fires and no-ops (tasks are
  not eagerly cancelled on resolve -- they are a firing convenience, not a
  coordination primitive).
- **Sweep.** :func:`expiry_sweep_loop`, started from the app lifespan, runs
  :func:`sweep_expired_handles` at startup and then every
  ``EXPIRY_SWEEP_INTERVAL_SECONDS``: it times out every pending row whose
  ``expires_at`` has passed. This is the restart-resilience fallback for
  timers lost with the process, and the only enforcement for deadlines set
  at create time (the ``user_subagent`` caller-handle backstop).

Every flip goes through :func:`db.tool_wait_handle_store.mark_timed_out`, a
per-row conditional update, so a timeout racing a resolve is decided by the
DB row's state: whichever write lands first wins, and the loser no-ops.
This module deliberately does not implement futures, register/discard
plumbing, or shutdown drain coupling -- the durable behaviour lives in the
DB.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# How often the expiry sweep re-scans for pending handles whose expires_at
# has passed. It also runs once at startup, so a deadline that elapsed while
# the server was down is enforced on boot; between passes, an overdue row
# can stay pending for at most this long.
EXPIRY_SWEEP_INTERVAL_SECONDS = 60


# Active timer tasks keyed by handle_id. We keep a reference so the tasks
# do not get garbage-collected mid-flight, and so the same handle can
# replace its timer if a later wait_for_handles call extends the deadline.
_active_timers: dict[str, asyncio.Task] = {}


def schedule_timeout_for_handles(
    user_id: int,
    conversation_id: str,
    handle_ids: list[str],
    timeout_seconds: int,
    *,
    app: Any = None,
    user: Any = None,
) -> None:
    """Schedule a per-handle expiry task.

    Best-effort: a missing event loop (e.g. called outside an asyncio
    context, which should not happen from the model loop) is logged and
    skipped. ``app`` and ``user`` are optional; when omitted, the resume
    kick on expiry is skipped and the model sees the ``timed_out`` status
    the next time the conversation runs.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "[wait-timer] No running loop; cannot schedule timeout for "
            "handles=%s",
            handle_ids,
        )
        return

    for handle_id in handle_ids:
        existing = _active_timers.get(handle_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = loop.create_task(
            _expire_after(
                user_id=user_id,
                conversation_id=conversation_id,
                handle_id=handle_id,
                timeout_seconds=timeout_seconds,
                app=app,
                user=user,
            )
        )
        _active_timers[handle_id] = task
        task.add_done_callback(
            lambda t, hid=handle_id: _active_timers.pop(hid, None)
            if _active_timers.get(hid) is t else None
        )


async def _expire_after(
    user_id: int,
    conversation_id: str,
    handle_id: str,
    timeout_seconds: int,
    app: Any,
    user: Any,
) -> None:
    """Sleep, then mark a still-pending handle ``timed_out`` and kick resume."""
    try:
        await asyncio.sleep(timeout_seconds)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.debug(
            "[wait-timer] Sleep failed for handle %s",
            handle_id, exc_info=True,
        )
        return

    # mark_timed_out only flips a row that is still PENDING (conditional
    # update), so a handle resolved during the sleep comes back empty.
    try:
        from db import tool_wait_handle_store
        flipped = await tool_wait_handle_store.mark_timed_out(
            [handle_id], user_id,
        )
    except Exception:
        logger.exception(
            "[wait-timer] mark_timed_out failed for handle %s",
            handle_id,
        )
        flipped = []

    if not flipped:
        # Already resolved by another path -- nothing for us to wake up.
        return

    logger.info(
        "[wait-timer] Handle %s timed out after %ds (conversation=%s)",
        handle_id, timeout_seconds, conversation_id,
    )
    _announce_timeouts(flipped, app, user)


def _announce_timeouts(
    flipped: list[dict],
    app: Any,
    user: Optional[dict],
    *,
    skip_conversation_id: Optional[str] = None,
) -> None:
    """Publish and wake for rows this process just flipped to ``timed_out``.

    Publishes ``wait_handle_resolved`` per row so viewing tabs unlock the
    composer, then kicks one headless resume per affected conversation
    (``maybe_kick_resume`` is gated on a dangling wait in the saved
    transcript, so it no-ops where nothing is suspended). The kicks are
    skipped when ``app`` or ``user`` is missing, and for
    ``skip_conversation_id`` -- the conversation whose live model run
    flipped the rows and reads their status directly.
    """
    from chat.realtime import bus, events as realtime_events

    for row in flipped:
        try:
            bus.publish_to_user(
                row["user_id"],
                realtime_events.make_wait_handle_resolved(
                    conversation_id=row["conversation_id"],
                    handle_id=row["id"],
                    kind=row.get("kind") or "",
                    status=row["status"],
                ),
            )
        except Exception:
            logger.debug(
                "[wait-timer] publish wait_handle_resolved (timeout) failed",
                exc_info=True,
            )

    if app is None or user is None:
        return

    from chat.wait_handles import resume as wait_resume

    conversation_ids = dict.fromkeys(row["conversation_id"] for row in flipped)
    for conversation_id in conversation_ids:
        if conversation_id == skip_conversation_id:
            continue
        try:
            wait_resume.maybe_kick_resume(app, user, conversation_id)
        except Exception:
            logger.exception(
                "[wait-timer] Failed to kick resume after timeout "
                "(conversation=%s)", conversation_id,
            )


async def expire_overdue_handles(
    app: Any,
    user: dict,
    handle_ids: list[str],
    *,
    current_conversation_id: Optional[str] = None,
) -> list[dict]:
    """Time out any of ``handle_ids`` whose persisted ``expires_at`` has
    passed, without waiting for the next sweep pass.

    Called by the ``wait_for_handles`` arm so a model never suspends on a
    handle that is already overdue. Rows are flipped through
    ``mark_timed_out(expired_by=now)``, so only rows the DB still holds as
    pending and overdue change. ``current_conversation_id`` (the caller's
    own, live conversation) gets no resume kick. Returns the flipped rows.
    """
    from db import tool_wait_handle_store

    flipped = await tool_wait_handle_store.mark_timed_out(
        handle_ids, user["id"], expired_by=datetime.now(timezone.utc),
    )
    if flipped:
        logger.info(
            "[wait-timer] Timed out %d overdue handle(s) at the wait site "
            "(user=%s): %s",
            len(flipped), user["id"], [row["id"] for row in flipped],
        )
        _announce_timeouts(
            flipped, app, user,
            skip_conversation_id=current_conversation_id,
        )
    return flipped


async def sweep_expired_handles(
    app: Any, *, now: Optional[datetime] = None,
) -> list[dict]:
    """One sweep pass: time out every pending handle whose ``expires_at``
    is at or before ``now`` and wake the affected conversations.

    The candidate read is only a hint. Each user's candidates are flipped
    through ``mark_timed_out(expired_by=now)``, so a row resolved, or given
    a later deadline, after the read is left alone, and two overlapping
    passes cannot both announce the same row. Returns the rows this pass
    flipped.
    """
    from db import tool_wait_handle_store
    from db.user_store import get_user_by_id

    now = now or datetime.now(timezone.utc)
    candidates = await tool_wait_handle_store.list_expired_pending(now)
    if not candidates:
        return []

    by_user: dict[int, list[str]] = {}
    for row in candidates:
        by_user.setdefault(row["user_id"], []).append(row["id"])

    timed_out: list[dict] = []
    for user_id, ids in by_user.items():
        try:
            flipped = await tool_wait_handle_store.mark_timed_out(
                ids, user_id, expired_by=now,
            )
        except Exception:
            logger.exception(
                "[wait-timer] Expiry sweep could not time out handles %s "
                "(user=%s)", ids, user_id,
            )
            continue
        if not flipped:
            continue
        logger.info(
            "[wait-timer] Expiry sweep timed out %d handle(s) (user=%s): %s",
            len(flipped), user_id, [row["id"] for row in flipped],
        )
        try:
            user = await get_user_by_id(user_id)
        except Exception:
            logger.exception(
                "[wait-timer] Expiry sweep could not load user %s; "
                "skipping resume kicks", user_id,
            )
            user = None
        _announce_timeouts(flipped, app, user)
        timed_out.extend(flipped)
    return timed_out


async def expiry_sweep_loop(
    app: Any, interval_seconds: float = EXPIRY_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Background task (started in the app lifespan): run
    :func:`sweep_expired_handles` immediately, then every
    ``interval_seconds``, until cancelled."""
    logger.info(
        "[wait-timer] Wait-handle expiry sweep started (interval: %ds)",
        interval_seconds,
    )
    while True:
        try:
            await sweep_expired_handles(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[wait-timer] Wait-handle expiry sweep failed")
        await asyncio.sleep(interval_seconds)
