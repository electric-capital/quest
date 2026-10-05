"""Routine scheduler daemon.

Runs as an asyncio background task within the FastAPI process. Polls for
due schedules every POLL_INTERVAL_SECONDS and executes them by creating
conversations and calling run_conversation_turn() directly.

Restart resilience (the server is restarted routinely):

- Anchored schedules (daily / weekly / hourly) fire from a stored
  ``next_due_at`` instead of a narrow match window, so a poll that arrives
  late still finds the run. An overdue occurrence runs if it is within the
  type's catch-up grace (db/schedule_timing.py ``CATCH_UP_GRACE``), else it
  is recorded as missed. A backlog collapses into one run of the newest
  occurrence.
- Every occurrence is claimed by inserting a ``routine_schedule_runs`` row
  (unique per schedule + occurrence) before the run starts, so it can never
  fire twice.
- A run cut off by a shutdown or crash is marked ``interrupted`` (at
  cancellation, or by the startup sweep for a hard kill) with a notice in
  its conversation, and anchored runs are retried once in a new
  conversation while still within grace.

Routines in public projects run only while the admin
``public_project_routines`` feature gate is open for their owner (see
``_public_routine_gated``): while it is closed their schedules are skipped
without creating a conversation or a ledger row. Schedules of routines in
an archived project are paused the same way until the project is
unarchived (``_schedule_paused_reason``).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from db import schedule_timing

logger = logging.getLogger(__name__)

# How often the scheduler checks for due schedules
POLL_INTERVAL_SECONDS = 30

# Maximum age of a "running" flag before it's considered stale
STALE_THRESHOLD_HOURS = 2

# Missed occurrences recorded per catch-up (older ones in a long backlog are
# only counted in the log).
MAX_MISSED_RECORDED = 24

# Track last DST reconversion date (module-level state)
_last_reconversion_date = None

# Track active execution tasks so they can be cancelled during shutdown
_active_execution_tasks: set[asyncio.Task] = set()


async def scheduler_loop(app) -> None:
    """Main scheduler loop. Runs forever, polling for due schedules.

    Args:
        app: The FastAPI application instance (needed for run_conversation_turn).
    """
    logger.info("[scheduler] Routine scheduler started (poll interval: %ds)", POLL_INTERVAL_SECONDS)

    try:
        await _recover_interrupted_runs()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[scheduler] Startup recovery of interrupted runs failed")

    while True:
        try:
            await _poll_and_execute(app)
        except asyncio.CancelledError:
            logger.info("[scheduler] Scheduler loop cancelled, shutting down")
            raise
        except Exception:
            logger.exception("[scheduler] Error in scheduler poll cycle")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _retry_eligible(schedule: dict, occurrence_at: datetime, attempt: int, now: datetime) -> bool:
    """Whether an interrupted run should be started again."""
    from db.schedule_store import MAX_RUN_ATTEMPTS

    if not schedule.get("is_enabled"):
        return False
    stype = schedule.get("schedule_type")
    if not schedule_timing.is_anchored(stype):
        # Interval schedules simply run again on their next interval.
        return False
    if attempt >= MAX_RUN_ATTEMPTS:
        return False
    # An edit after the occurrence means the old timing no longer applies.
    updated = _parse_iso(schedule.get("updated_at"))
    if updated and updated > occurrence_at:
        return False
    return now - occurrence_at <= schedule_timing.CATCH_UP_GRACE[stype]


def _interruption_notice(will_retry: bool) -> dict:
    from chat.storage import utc_timestamp

    text = "This scheduled run was interrupted by a server restart."
    if will_retry:
        text += " The scheduler will retry it in a new conversation."
    return {"type": "error", "error": text, "timestamp": utc_timestamp()}


async def _recover_interrupted_runs() -> None:
    """Startup sweep: runs left ``running`` by a dead process become ``interrupted``.

    Also clears every schedule's ``is_running`` flag. Each interrupted
    conversation gets a notice; the retry itself happens in the poll loop.
    """
    from db.schedule_store import mark_running_runs_interrupted

    rows = await mark_running_runs_interrupted()
    now = datetime.now(timezone.utc)
    for row in rows:
        occurrence_at = _parse_iso(row["occurrence_at"])
        will_retry = _retry_eligible(row["schedule"], occurrence_at, row["attempt"], now)
        logger.warning(
            "[scheduler] Run interrupted by restart: schedule=%s occurrence=%s "
            "conversation=%s retry=%s",
            row["schedule_id"], row["occurrence_at"], row["conversation_id"], will_retry,
        )
        if row["conversation_id"]:
            try:
                await _persist_scheduler_messages(
                    row["conversation_id"], [_interruption_notice(will_retry)],
                )
            except Exception:
                logger.exception(
                    "[scheduler] Failed to add interruption notice (conversation=%s)",
                    row["conversation_id"],
                )


def _public_routine_gated(routine: dict) -> bool:
    """Whether this routine must not run: public project, gate closed.

    A routine in a public project runs unattended in the internet-enabled
    sandbox, which an admin has to opt into (Settings > Features); the
    check is per owner, like every other use of the gate.
    """
    if not routine.get("project_public"):
        return False
    from config.feature_gates import public_project_routines_enabled_for

    return not public_project_routines_enabled_for(routine.get("user_email") or "")


def _schedule_paused_reason(routine: dict) -> str | None:
    """Why this routine's schedules must not fire right now, or None.

    Two pauses share one skip path: the public-project routines gate (see
    ``_public_routine_gated``) and an archived project, whose routines stay
    defined but sleep until the project is unarchived.
    """
    if routine.get("project_archived"):
        return "its project is archived"
    if _public_routine_gated(routine):
        return (
            "routines in public projects are disabled for "
            f"{routine.get('user_email')}"
        )
    return None


async def _skip_paused_schedule(
    schedule: dict, routine: dict, now: datetime, reason: str
) -> None:
    """Pass over a due occurrence of a paused (gated or archived) routine.

    An anchored schedule's ``next_due_at`` is moved past the occurrence, so
    lifting the pause resumes at the next regular occurrence instead of
    catching up on (or recording as missed) everything that came due while
    it was paused. Logged once per skipped occurrence, never per poll.
    Interval schedules need no bookkeeping: they simply stay un-run and
    fire on the first poll after the pause lifts.
    """
    from db.schedule_store import set_next_due

    if not schedule_timing.is_anchored(schedule["schedule_type"]):
        return
    next_due = _parse_iso(schedule.get("next_due_at_stored"))
    if next_due is not None and now < next_due:
        return
    await set_next_due(schedule["id"], schedule_timing.occurrence_after(schedule, now))
    if next_due is not None:
        logger.info(
            "[scheduler] %s: SKIPPED %s -- %s (schedule=%s)",
            routine["name"], next_due.isoformat(), reason, schedule["id"],
        )


def _spawn_run(app, schedule: dict, routine: dict, run_id: str) -> None:
    # Fire-and-forget as an asyncio task so we don't block the scheduler
    # loop waiting for the model. Tracked so it can be cancelled on shutdown.
    task = asyncio.create_task(_execute_scheduled_run(app, schedule, routine, run_id))
    _active_execution_tasks.add(task)
    task.add_done_callback(_active_execution_tasks.discard)


async def _poll_and_execute(app, now: datetime | None = None) -> None:
    """Single poll cycle: retry interrupted runs, then fire due schedules.

    Only events are logged (fires, catch-ups, misses, retries); a schedule
    that is simply not due yet logs nothing, so the 30-second poll is
    silent. The next due time is visible on the schedule API / UI instead.
    """
    global _last_reconversion_date

    from db.schedule_store import list_enabled_schedules, clear_stale_running_flags

    # Clear stale running flags first
    stale_cleared = await clear_stale_running_flags(STALE_THRESHOLD_HOURS)
    if stale_cleared > 0:
        logger.warning("[scheduler] Cleared %d stale running flags", stale_cleared)

    # Keep the informational daily_time_utc column current across DST once
    # per day. Firing does not depend on it (next_due_at is DST-aware).
    today_utc = datetime.now(timezone.utc).date()
    if _last_reconversion_date != today_utc:
        _last_reconversion_date = today_utc
        await _reconvert_daily_utc_times()

    now = now or datetime.now(timezone.utc)

    await _retry_interrupted_runs(app, now)

    schedules = await list_enabled_schedules()
    for schedule in schedules:
        routine = schedule["routine"]
        try:
            paused = _schedule_paused_reason(routine)
            if paused:
                await _skip_paused_schedule(schedule, routine, now, paused)
            elif schedule_timing.is_anchored(schedule["schedule_type"]):
                await _check_anchored(app, schedule, routine, now)
            else:
                await _check_interval(app, schedule, routine, now)
        except Exception:
            logger.exception(
                "[scheduler] Error checking schedule %s (routine=%s)",
                schedule["id"], routine["name"],
            )


async def _retry_interrupted_runs(app, now: datetime) -> None:
    from db.schedule_store import list_interrupted_runs, claim_retry

    for row in await list_interrupted_runs():
        schedule = row["schedule"]
        occurrence_at = _parse_iso(row["occurrence_at"])
        if not _retry_eligible(schedule, occurrence_at, row["attempt"], now):
            continue
        if _schedule_paused_reason(schedule["routine"]):
            continue
        if not await claim_retry(row["id"]):
            continue
        logger.info(
            "[scheduler] %s: RETRY -- interrupted run of %s (attempt %d, schedule=%s)",
            schedule["routine"]["name"], row["occurrence_at"], row["attempt"] + 1,
            schedule["id"],
        )
        _spawn_run(app, schedule, schedule["routine"], row["id"])


async def _check_anchored(app, schedule: dict, routine: dict, now: datetime) -> None:
    """Fire, catch up, or record misses for a daily / weekly / hourly schedule."""
    from db.schedule_store import claim_occurrence, record_missed, set_next_due

    stype = schedule["schedule_type"]
    next_due = _parse_iso(schedule.get("next_due_at_stored"))
    if next_due is None:
        # New, just-migrated, or re-timed row: start from the next occurrence
        # after now, so a schedule never fires retroactively on creation.
        await set_next_due(schedule["id"], schedule_timing.occurrence_after(schedule, now))
        return

    if now < next_due:
        return

    latest = schedule_timing.occurrence_at_or_before(schedule, now)
    if latest < next_due:
        # next_due_at is not an occurrence of the current timing (edited
        # outside update_schedule); consider only the latest real one.
        backlog = []
    else:
        backlog = schedule_timing.occurrences_between(schedule, next_due, latest)
    upcoming = schedule_timing.occurrence_after(schedule, now)
    lateness = now - latest
    grace = schedule_timing.CATCH_UP_GRACE[stype]

    if backlog:
        logger.warning(
            "[scheduler] %s: MISSED %d occurrence(s) from %s while the server was down "
            "(schedule=%s)",
            routine["name"], len(backlog), backlog[0].isoformat(), schedule["id"],
        )
    missed = backlog[-MAX_MISSED_RECORDED:]

    if lateness > grace:
        logger.warning(
            "[scheduler] %s: MISSED %s -- %s late, past the %s catch-up grace (schedule=%s)",
            routine["name"], latest.isoformat(), _fmt_delta(lateness), _fmt_delta(grace),
            schedule["id"],
        )
        await record_missed(schedule["id"], (missed + [latest])[-MAX_MISSED_RECORDED:], upcoming)
        return

    run_id = await claim_occurrence(schedule["id"], latest, upcoming, missed)
    if run_id is None:
        # Already claimed (e.g. retried or completed) -- just move on.
        await set_next_due(schedule["id"], upcoming)
        return
    if lateness > timedelta(seconds=POLL_INTERVAL_SECONDS * 2):
        logger.info(
            "[scheduler] %s: FIRE (catch-up) -- %s occurrence %s late (schedule=%s)",
            routine["name"], stype, _fmt_delta(lateness), schedule["id"],
        )
    else:
        logger.info(
            "[scheduler] %s: FIRE -- %s occurrence %s (schedule=%s)",
            routine["name"], stype, latest.isoformat(), schedule["id"],
        )
    _spawn_run(app, schedule, routine, run_id)


async def _check_interval(app, schedule: dict, routine: dict, now: datetime) -> None:
    """Fire an every_n_minutes schedule once its interval has elapsed."""
    from db.schedule_store import claim_occurrence

    due, reason = _interval_due(schedule, now)
    if not due:
        return
    run_id = await claim_occurrence(schedule["id"], now.replace(microsecond=0))
    if run_id is None:
        return
    logger.info("[scheduler] %s: FIRE -- %s (schedule=%s)", routine["name"], reason, schedule["id"])
    _spawn_run(app, schedule, routine, run_id)


def _interval_due(schedule: dict, now: datetime) -> tuple[bool, str]:
    """Whether an every_n_minutes schedule should fire; (due, reason)."""
    interval = schedule.get("interval_minutes")
    if not interval:
        return False, "interval schedule has no interval_minutes configured"

    last_started = _parse_iso(schedule.get("last_run_started_at"))
    if schedule.get("is_running"):
        return False, "still running (started=%s)" % (
            last_started.strftime("%H:%M:%S UTC") if last_started else "unknown",)

    last_completed = _parse_iso(schedule.get("last_run_completed_at"))
    reference_time = last_completed or last_started
    if reference_time is None:
        return True, "never run before (interval=%dm)" % interval

    elapsed = (now - reference_time).total_seconds() / 60.0
    if elapsed >= interval:
        return True, "interval elapsed (%.1fm >= %dm)" % (elapsed, interval)
    return False, "not yet due (%.1fm elapsed, %dm interval, %.1fm remaining)" % (
        elapsed, interval, interval - elapsed)


def _fmt_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes / 60:.1f}h"


async def shutdown() -> None:
    """Cancel all active execution tasks and wait for them to finish.

    Called during server shutdown to ensure all in-progress scheduled runs
    are cleanly terminated.
    """
    tasks = list(_active_execution_tasks)
    if not tasks:
        logger.info("[scheduler] No active execution tasks to cancel")
        return

    logger.info("[scheduler] Cancelling %d active execution task(s)", len(tasks))
    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    cancelled_count = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
    logger.info(
        "[scheduler] Shutdown complete: %d task(s) cancelled, %d already finished",
        cancelled_count, len(tasks) - cancelled_count,
    )


async def _execute_scheduled_run(app, schedule: dict, routine: dict, run_id: str) -> None:
    """Execute a single claimed scheduled run.

    Creates a new conversation in the project, then calls run_conversation_turn()
    directly (headless -- no WebSocket stream). ``run_id`` is the ledger row
    the poll loop claimed; this closes it as completed / failed /
    interrupted.
    """
    from db.schedule_store import set_run_conversation, finish_run
    from db.user_store import get_user_by_id
    from chat.storage import ChatStorage
    from chat.gemini_api import run_conversation_turn

    schedule_id = schedule["id"]
    routine_id = routine["id"]
    routine_name = routine["name"]
    project_id = routine["project_id"]
    user_id = routine["user_id"]
    prompt = routine["prompt"]
    guide_id = routine.get("guide_id")
    routine_model = routine.get("model")

    logger.info(
        "[scheduler] Executing scheduled run: routine=%s, project=%s, user=%d, type=%s",
        routine_name, project_id, user_id, schedule["schedule_type"],
    )

    conversation_id = None
    # Shared messages list for persistence. Defined outside the try so the
    # failure path below can persist partial progress + the durable error
    # message without risking a NameError.
    messages_out: list[dict] = []

    try:
        # Look up the user record (needed by run_conversation_turn)
        user = await get_user_by_id(user_id)
        if not user:
            logger.error("[scheduler] User %d not found for schedule %s, skipping", user_id, schedule_id)
            await finish_run(schedule_id, run_id, "failed")
            return

        # Create a new conversation in the project
        conversation_id, _created_at = await ChatStorage.create_project_conversation(
            user_id, project_id, routine_id=routine_id, model=routine_model,
        )
        await set_run_conversation(schedule_id, run_id, conversation_id)

        # Save the user message (the routine prompt)
        from chat.storage import utc_timestamp
        user_timestamp = utc_timestamp()
        await ChatStorage.append_message(
            conversation_id=conversation_id,
            role="user",
            content=prompt,
            timestamp=user_timestamp,
        )

        # No-op event handler (no WebSocket to stream to)
        async def noop_event(event: dict) -> None:
            pass

        # Determine the user's timezone for the Gemini call
        user_tz = schedule.get("timezone") or "UTC"

        # Run the Gemini API conversation to completion
        await run_conversation_turn(
            app=app,
            user=user,
            message=prompt,
            conversation_id=conversation_id,
            timezone=user_tz,
            model=routine_model,
            on_event=noop_event,
            messages_out=messages_out,
            guide_id=guide_id,
            project_id=project_id,
            routine_id=routine_id,
        )

        # Persist the response messages
        await _persist_scheduler_messages(conversation_id, messages_out)

        await finish_run(schedule_id, run_id, "completed")
        logger.info(
            "[scheduler] Completed scheduled run: routine=%s, conversation=%s",
            routine_name, conversation_id,
        )

    except asyncio.CancelledError:
        # Server shutdown. Record the interruption so the next startup can
        # retry the occurrence, and tell the conversation why it stopped.
        occurrence = None
        try:
            from db.schedule_store import get_schedule_for_routine
            await finish_run(schedule_id, run_id, "interrupted")
            current = await get_schedule_for_routine(routine_id, include_runs=True) or schedule
            run = next((r for r in current.get("recent_runs", []) if r["id"] == run_id), None)
            occurrence = _parse_iso(run["occurrence_at"]) if run else None
            will_retry = bool(run) and _retry_eligible(
                current, occurrence, run["attempt"], datetime.now(timezone.utc),
            )
            if conversation_id:
                await _persist_scheduler_messages(
                    conversation_id, messages_out + [_interruption_notice(will_retry)],
                )
        except Exception:
            logger.exception(
                "[scheduler] Failed to record interruption (schedule=%s, conversation=%s)",
                schedule_id, conversation_id,
            )
        logger.info(
            "[scheduler] Interrupted scheduled run (shutdown): routine=%s, schedule=%s, "
            "occurrence=%s, conversation=%s",
            routine_name, schedule_id, occurrence.isoformat() if occurrence else "?",
            conversation_id,
        )
        raise

    except Exception:
        await finish_run(schedule_id, run_id, "failed")
        logger.exception(
            "[scheduler] Failed scheduled run: routine=%s, schedule=%s, conversation=%s",
            routine_name, schedule_id, conversation_id,
        )
        # Persist whatever the run produced before it died -- including the
        # durable ``error`` message run_conversation_turn appends on failure -- so
        # the conversation shows why the routine stopped instead of ending
        # silently after the prompt.
        if conversation_id:
            try:
                await _persist_scheduler_messages(conversation_id, messages_out)
            except Exception:
                logger.exception(
                    "[scheduler] Failed to persist messages for failed run "
                    "(conversation=%s)", conversation_id,
                )


async def _persist_scheduler_messages(
    conversation_id: str, messages_out: list[dict],
) -> None:
    """Append accumulated run messages to the conversation history."""
    from chat.storage import ChatStorage, utc_timestamp

    if not messages_out:
        return
    assistant_timestamp = utc_timestamp()
    for msg in messages_out:
        if "timestamp" not in msg:
            msg["timestamp"] = assistant_timestamp
    await ChatStorage.append_structured_messages(
        conversation_id=conversation_id,
        messages=messages_out,
    )


async def _reconvert_daily_utc_times() -> None:
    """Reconvert daily/weekly ``daily_time_utc`` values from local time + timezone.

    Called once per day (at midnight UTC). The column is informational only:
    firing uses the DST-aware ``next_due_at``.
    """
    from db.schedule_store import list_enabled_schedules, update_schedule
    from chat.schedule_routes import _local_time_to_utc

    schedules = await list_enabled_schedules()
    for entry in schedules:
        schedule = entry
        if schedule["schedule_type"] not in ("daily", "weekly"):
            continue
        local_time = schedule.get("daily_time_local")
        tz = schedule.get("timezone")
        if not local_time or not tz:
            continue
        try:
            new_utc = _local_time_to_utc(local_time, tz)
            if new_utc != schedule.get("daily_time_utc"):
                await update_schedule(
                    schedule_id=schedule["id"],
                    user_id=schedule["user_id"],
                    daily_time_utc=new_utc,
                )
                logger.info(
                    "[scheduler] DST reconversion: schedule %s UTC time %s -> %s",
                    schedule["id"], schedule.get("daily_time_utc"), new_utc,
                )
        except Exception:
            logger.exception(
                "[scheduler] Failed DST reconversion for schedule %s",
                schedule["id"],
            )


def _parse_iso(iso_str: str | None) -> datetime | None:
    """Parse an ISO datetime string to a timezone-aware datetime, or None."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
