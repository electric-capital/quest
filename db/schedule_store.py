"""Routine schedule data access layer.

Provides async CRUD operations for routine schedules (each routine has at
most one) plus the ``routine_schedule_runs`` run ledger the scheduler uses
to claim occurrences exactly once, catch up runs missed while the server was
down, and retry runs a restart interrupted (see chat/scheduler.py and
db/schedule_timing.py).

This module follows the same async pattern as db/routine_store.py:
each function opens a fresh AsyncSessionLocal() session.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, update as sa_update
from sqlalchemy.exc import IntegrityError

from db.engine import AsyncSessionLocal
from db.models import (
    Project, Routine, RoutineSchedule, RoutineScheduleRun, User,
)
from db import schedule_timing

logger = logging.getLogger(__name__)

# Ledger rows kept per schedule (older ones are pruned on each claim).
RUNS_KEPT_PER_SCHEDULE = 100

# Starts allowed for one occurrence: the first attempt plus one retry after
# an interruption.
MAX_RUN_ATTEMPTS = 2

class StaleScheduleError(Exception):
    """Raised when update_schedule() detects that the row was modified
    concurrently by someone else (expected_updated_at mismatch).

    Carries the current row (post-conflict) so the route handler can return
    it in the 409 body, sparing the FE a second GET round-trip.
    """

    def __init__(self, current: dict):
        super().__init__("Schedule was modified by another writer")
        self.current = current


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timing_view(schedule: RoutineSchedule) -> dict:
    """The fields db/schedule_timing.py reads, from an ORM row."""
    return {
        "schedule_type": schedule.schedule_type,
        "daily_time_local": schedule.daily_time_local,
        "timezone": schedule.timezone,
        "hourly_minute": schedule.hourly_minute,
        "weekly_days": schedule.weekly_days,
    }


def _compute_next_due(schedule: RoutineSchedule, now: datetime) -> Optional[datetime]:
    """Next occurrence after ``now`` for anchored types, None otherwise."""
    if not schedule_timing.is_anchored(schedule.schedule_type):
        return None
    try:
        return schedule_timing.occurrence_after(_timing_view(schedule), now)
    except (ValueError, KeyError):
        return None


async def create_schedule(
    user_id: int,
    routine_id: str,
    schedule_type: str,
    daily_time_utc: Optional[str] = None,
    daily_time_local: Optional[str] = None,
    timezone_str: Optional[str] = None,
    hourly_minute: Optional[int] = None,
    interval_minutes: Optional[int] = None,
    weekly_days: Optional[list[int]] = None,
) -> dict:
    """Create a schedule for a routine.

    Validates schedule_type and required fields for each type.
    Raises ValueError if a schedule already exists for this routine.
    """
    weekly_days = _validate_schedule_params(
        schedule_type, daily_time_utc, daily_time_local, timezone_str,
        hourly_minute, interval_minutes, weekly_days,
    )

    async with AsyncSessionLocal() as db:
        # Check for existing schedule
        result = await db.execute(
            select(RoutineSchedule).where(RoutineSchedule.routine_id == routine_id)
        )
        existing = result.scalars().first()
        if existing:
            raise ValueError("This routine already has a schedule. Delete it first or update it.")

        # Stamp updated_at on create so the optimistic-concurrency guard in
        # update_schedule() has a non-null baseline to compare on first edit.
        now = _utcnow()
        schedule = RoutineSchedule(
            routine_id=routine_id,
            user_id=user_id,
            schedule_type=schedule_type,
            daily_time_utc=daily_time_utc,
            daily_time_local=daily_time_local,
            timezone=timezone_str,
            hourly_minute=hourly_minute,
            interval_minutes=interval_minutes,
            weekly_days=schedule_timing.format_weekly_days(weekly_days),
            updated_at=now,
        )
        schedule.next_due_at = _compute_next_due(schedule, now)
        db.add(schedule)
        await db.commit()
        await db.refresh(schedule)
        return _schedule_to_dict(schedule)


async def get_schedule_for_routine(routine_id: str, include_runs: bool = False) -> Optional[dict]:
    """Get the schedule for a routine, or None if no schedule exists.

    ``include_runs`` adds ``recent_runs`` (newest ledger rows first).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RoutineSchedule).where(RoutineSchedule.routine_id == routine_id)
        )
        schedule = result.scalars().first()
        if not schedule:
            return None
        out = _schedule_to_dict(schedule)
        if include_runs:
            runs = await db.execute(
                select(RoutineScheduleRun)
                .where(RoutineScheduleRun.schedule_id == schedule.id)
                .order_by(RoutineScheduleRun.occurrence_at.desc())
                .limit(10)
            )
            out["recent_runs"] = [_run_to_dict(r) for r in runs.scalars().all()]
        return out


async def update_schedule(
    schedule_id: str,
    user_id: int,
    schedule_type: Optional[str] = None,
    daily_time_utc: Optional[str] = ...,
    daily_time_local: Optional[str] = ...,
    timezone_str: Optional[str] = ...,
    hourly_minute: Optional[int] = ...,
    interval_minutes: Optional[int] = ...,
    is_enabled: Optional[bool] = None,
    expected_updated_at: Optional[str] = None,
    weekly_days: Optional[list[int]] = ...,
) -> Optional[dict]:
    """Update a schedule. Uses ellipsis sentinels for nullable fields.

    ``expected_updated_at`` is the optimistic-concurrency token: if provided,
    it must match the row's current ``updated_at`` (ISO string) or the call
    raises :class:`StaleScheduleError`. Passing ``None`` skips the check
    (back-compat path for legacy rows with NULL updated_at).

    A change to any timing field, or re-enabling a paused schedule,
    recomputes ``next_due_at`` from now: occurrences that passed while the
    schedule was paused (or under its old timing) are not caught up.
    """
    async with AsyncSessionLocal() as db:
        schedule = await db.get(RoutineSchedule, schedule_id)
        if not schedule or schedule.user_id != user_id:
            return None

        # Optimistic concurrency check. Compare before mutating so we can
        # surface the current row state in the conflict response.
        if expected_updated_at is not None:
            current_token = schedule.updated_at.isoformat() if schedule.updated_at else None
            if current_token != expected_updated_at:
                raise StaleScheduleError(current=_schedule_to_dict(schedule))

        # Track whether anything actually changed so we don't bump
        # updated_at on a true no-op save (which would generate spurious
        # 409s for the next opener).
        changed = False
        timing_changed = False
        if schedule_type is not None and schedule_type != schedule.schedule_type:
            schedule.schedule_type = schedule_type
            changed = timing_changed = True
        if daily_time_utc is not ... and daily_time_utc != schedule.daily_time_utc:
            schedule.daily_time_utc = daily_time_utc
            changed = True
        if daily_time_local is not ... and daily_time_local != schedule.daily_time_local:
            schedule.daily_time_local = daily_time_local
            changed = timing_changed = True
        if timezone_str is not ... and timezone_str != schedule.timezone:
            schedule.timezone = timezone_str
            changed = timing_changed = True
        if hourly_minute is not ... and hourly_minute != schedule.hourly_minute:
            schedule.hourly_minute = hourly_minute
            changed = timing_changed = True
        if interval_minutes is not ... and interval_minutes != schedule.interval_minutes:
            schedule.interval_minutes = interval_minutes
            changed = True
        if weekly_days is not ...:
            stored = schedule_timing.format_weekly_days(weekly_days)
            if stored != schedule.weekly_days:
                schedule.weekly_days = stored
                changed = timing_changed = True
        if is_enabled is not None and is_enabled != schedule.is_enabled:
            schedule.is_enabled = is_enabled
            changed = True
            if is_enabled:
                timing_changed = True

        if changed:
            # Validate the merged row so a partial update can't leave e.g. a
            # weekly schedule without days.
            _validate_schedule_params(
                schedule.schedule_type, schedule.daily_time_utc,
                schedule.daily_time_local, schedule.timezone,
                schedule.hourly_minute, schedule.interval_minutes,
                schedule_timing.parse_weekly_days(schedule.weekly_days) or None,
            )
            now = _utcnow()
            if timing_changed:
                schedule.next_due_at = _compute_next_due(schedule, now)
            schedule.updated_at = now
            await db.commit()
            await db.refresh(schedule)
        return _schedule_to_dict(schedule)


async def delete_schedule_for_routine(routine_id: str) -> bool:
    """Delete the schedule for a routine (if any)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(RoutineSchedule).where(RoutineSchedule.routine_id == routine_id)
        )
        await db.commit()
        return result.rowcount > 0


async def list_enabled_schedules() -> list[dict]:
    """List all enabled schedules (used by the scheduler daemon).

    Returns schedules joined with routine data (routine name, prompt, guide_id,
    project_id, plus the project's public / archived flags and the owner's
    email for the public-project routines gate and the archived-project
    pause) so the scheduler has everything it needs to execute runs.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                RoutineSchedule, Routine, Project.public, Project.archived,
                User.email,
            )
            .join(Routine, RoutineSchedule.routine_id == Routine.id)
            .outerjoin(Project, Routine.project_id == Project.id)
            .outerjoin(User, Routine.user_id == User.id)
            .where(RoutineSchedule.is_enabled == True)
        )
        rows = result.all()
        return [
            {
                **_schedule_to_dict(sched),
                # Raw column (the public ``next_due_at`` fills in a computed
                # value when NULL); the scheduler initializes NULL rows.
                "next_due_at_stored": _iso(schedule_timing.as_utc(sched.next_due_at)),
                "routine": _routine_summary(
                    routine, project_public, user_email, project_archived,
                ),
            }
            for sched, routine, project_public, project_archived, user_email in rows
        ]


async def set_next_due(schedule_id: str, next_due_at: Optional[datetime]) -> None:
    """Set ``next_due_at`` without touching ``updated_at`` (scheduler-owned state)."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            sa_update(RoutineSchedule)
            .where(RoutineSchedule.id == schedule_id)
            .values(next_due_at=next_due_at)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Run ledger
# ---------------------------------------------------------------------------


async def claim_occurrence(
    schedule_id: str,
    occurrence_at: datetime,
    next_due_at: Optional[datetime] = ...,
    missed: Optional[list[datetime]] = None,
) -> Optional[str]:
    """Claim one occurrence for execution; returns the new run id, or None.

    In ONE transaction: records ``missed`` occurrences, inserts a
    ``running`` ledger row for ``occurrence_at``, marks the schedule running
    and (when given) advances ``next_due_at``. The unique
    (schedule_id, occurrence_at) index turns a second claim of the same
    occurrence into None instead of a double fire.
    """
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        for occ in missed or ():
            db.add(RoutineScheduleRun(
                schedule_id=schedule_id, occurrence_at=occ, status="missed",
                finished_at=now,
            ))
        run_id = str(uuid.uuid4())
        run = RoutineScheduleRun(
            id=run_id, schedule_id=schedule_id, occurrence_at=occurrence_at,
            status="running", attempt=1, started_at=now,
        )
        db.add(run)
        values = {"is_running": True, "last_run_started_at": now}
        if next_due_at is not ...:
            values["next_due_at"] = next_due_at
        try:
            # The UPDATE autoflushes the inserts, so a duplicate occurrence
            # can raise here as well as at commit.
            await db.execute(
                sa_update(RoutineSchedule)
                .where(RoutineSchedule.id == schedule_id)
                .values(**values)
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None
    await _prune_runs(schedule_id)
    return run_id


async def record_missed(
    schedule_id: str, missed: list[datetime], next_due_at: Optional[datetime],
) -> None:
    """Record occurrences that are past their catch-up grace and advance ``next_due_at``."""
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        for occ in missed:
            db.add(RoutineScheduleRun(
                schedule_id=schedule_id, occurrence_at=occ, status="missed",
                finished_at=now,
            ))
        try:
            await db.execute(
                sa_update(RoutineSchedule)
                .where(RoutineSchedule.id == schedule_id)
                .values(next_due_at=next_due_at)
            )
            await db.commit()
        except IntegrityError:
            # An occurrence already has a row (e.g. it ran); just advance.
            await db.rollback()
            await set_next_due(schedule_id, next_due_at)
    await _prune_runs(schedule_id)


async def claim_retry(run_id: str) -> bool:
    """Move an ``interrupted`` run back to ``running`` for another attempt."""
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        run = await db.get(RoutineScheduleRun, run_id)
        if not run or run.status != "interrupted" or run.attempt >= MAX_RUN_ATTEMPTS:
            return False
        run.status = "running"
        run.attempt += 1
        run.started_at = now
        run.finished_at = None
        await db.execute(
            sa_update(RoutineSchedule)
            .where(RoutineSchedule.id == run.schedule_id)
            .values(is_running=True, last_run_started_at=now)
        )
        await db.commit()
        return True


async def set_run_conversation(schedule_id: str, run_id: str, conversation_id: str) -> None:
    """Link the run's conversation to the ledger row and the schedule."""
    async with AsyncSessionLocal() as db:
        run = await db.get(RoutineScheduleRun, run_id)
        if run:
            run.conversation_id = conversation_id
        schedule = await db.get(RoutineSchedule, schedule_id)
        if schedule:
            schedule.last_conversation_id = conversation_id
        await db.commit()


async def finish_run(schedule_id: str, run_id: Optional[str], status: str) -> None:
    """Close a run: ``completed`` / ``failed`` / ``interrupted``.

    Clears the schedule's ``is_running``; ``completed`` also stamps
    ``last_run_completed_at``.
    """
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        if run_id:
            run = await db.get(RoutineScheduleRun, run_id)
            if run:
                run.status = status
                run.finished_at = now
        schedule = await db.get(RoutineSchedule, schedule_id)
        if schedule:
            schedule.is_running = False
            if status == "completed":
                schedule.last_run_completed_at = now
        await db.commit()


async def mark_running_runs_interrupted() -> list[dict]:
    """Startup recovery: every ``running`` row belongs to a dead process.

    Marks those rows ``interrupted`` and clears every schedule's
    ``is_running`` flag (only one server process runs schedules, so nothing
    is running at boot). Returns the interrupted rows joined with their
    schedule's timing fields.
    """
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RoutineScheduleRun, RoutineSchedule)
            .join(RoutineSchedule, RoutineScheduleRun.schedule_id == RoutineSchedule.id)
            .where(RoutineScheduleRun.status == "running")
        )
        rows = result.all()
        out = []
        for run, schedule in rows:
            run.status = "interrupted"
            run.finished_at = now
            out.append({**_run_to_dict(run), "schedule": _schedule_to_dict(schedule)})
        await db.execute(
            sa_update(RoutineSchedule)
            .where(RoutineSchedule.is_running == True)
            .values(is_running=False)
        )
        await db.commit()
        return out


async def list_interrupted_runs() -> list[dict]:
    """Interrupted runs that still have attempts left, with schedule + routine."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                RoutineScheduleRun, RoutineSchedule, Routine,
                Project.public, Project.archived, User.email,
            )
            .join(RoutineSchedule, RoutineScheduleRun.schedule_id == RoutineSchedule.id)
            .join(Routine, RoutineSchedule.routine_id == Routine.id)
            .outerjoin(Project, Routine.project_id == Project.id)
            .outerjoin(User, Routine.user_id == User.id)
            .where(
                RoutineScheduleRun.status == "interrupted",
                RoutineScheduleRun.attempt < MAX_RUN_ATTEMPTS,
                RoutineSchedule.is_enabled == True,
            )
        )
        return [
            {
                **_run_to_dict(run),
                "schedule": {
                    **_schedule_to_dict(schedule),
                    "routine": _routine_summary(
                        routine, project_public, user_email, project_archived,
                    ),
                },
            }
            for run, schedule, routine, project_public, project_archived, user_email
            in result.all()
        ]


async def _prune_runs(schedule_id: str, keep: int = RUNS_KEPT_PER_SCHEDULE) -> None:
    async with AsyncSessionLocal() as db:
        keep_ids = (
            select(RoutineScheduleRun.id)
            .where(RoutineScheduleRun.schedule_id == schedule_id)
            .order_by(RoutineScheduleRun.occurrence_at.desc())
            .limit(keep)
        )
        await db.execute(
            delete(RoutineScheduleRun).where(
                RoutineScheduleRun.schedule_id == schedule_id,
                RoutineScheduleRun.status != "running",
                RoutineScheduleRun.id.not_in(keep_ids),
            )
        )
        await db.commit()


async def clear_stale_running_flags(stale_threshold_hours: int = 2) -> int:
    """Clear is_running flags for schedules stuck in running state.

    A schedule is considered stale if is_running=True and last_run_started_at
    is more than stale_threshold_hours ago. Returns count of cleared flags.
    Restarts are handled separately (mark_running_runs_interrupted clears
    every flag at boot); this only guards a run stuck inside a live process.
    """
    from datetime import timedelta
    cutoff = _utcnow() - timedelta(hours=stale_threshold_hours)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_update(RoutineSchedule)
            .where(
                RoutineSchedule.is_running == True,
                RoutineSchedule.last_run_started_at < cutoff,
            )
            .values(is_running=False)
        )
        await db.commit()
        return result.rowcount


def _validate_schedule_params(schedule_type, daily_time_utc, daily_time_local,
                               timezone_str, hourly_minute, interval_minutes,
                               weekly_days=None):
    """Validate schedule parameters based on type.

    Returns the normalized ``weekly_days`` list for weekly schedules (None
    otherwise).
    """
    valid_types = schedule_timing.SCHEDULE_TYPES
    if schedule_type not in valid_types:
        raise ValueError(f"Invalid schedule_type: {schedule_type}. Must be one of {valid_types}")

    if schedule_type in ('daily', 'weekly'):
        if not daily_time_local or not timezone_str:
            raise ValueError(
                f"{schedule_type.capitalize()} schedule requires daily_time_local and timezone"
            )
        _validate_time_string(daily_time_local)
        if daily_time_utc:
            _validate_time_string(daily_time_utc)
        if schedule_type == 'weekly':
            return schedule_timing.validate_weekly_days(weekly_days)

    elif schedule_type == 'hourly':
        if hourly_minute is None:
            raise ValueError("Hourly schedule requires hourly_minute")
        if not (0 <= hourly_minute <= 59):
            raise ValueError("hourly_minute must be between 0 and 59")

    elif schedule_type == 'every_n_minutes':
        if interval_minutes is None:
            raise ValueError("Interval schedule requires interval_minutes")
        if interval_minutes < 1:
            raise ValueError("interval_minutes must be at least 1")
        if interval_minutes > 1440:
            raise ValueError("interval_minutes cannot exceed 1440 (24 hours)")
    return None


def _validate_time_string(time_str: str):
    """Validate a time string in HH:MM format."""
    if not time_str or len(time_str) != 5 or time_str[2] != ':':
        raise ValueError(f"Invalid time format: {time_str}. Must be HH:MM")
    try:
        h, m = int(time_str[:2]), int(time_str[3:])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError()
    except ValueError:
        raise ValueError(f"Invalid time format: {time_str}. Must be HH:MM with valid hours/minutes")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _next_due_view(schedule: RoutineSchedule) -> Optional[str]:
    """``next_due_at`` for display: stored for anchored types, derived for intervals."""
    if schedule_timing.is_anchored(schedule.schedule_type):
        due = schedule_timing.as_utc(schedule.next_due_at)
        if due is None and schedule.is_enabled:
            due = _compute_next_due(schedule, _utcnow())
        return _iso(due)
    if schedule.schedule_type == "every_n_minutes" and schedule.interval_minutes:
        from datetime import timedelta
        ref = schedule_timing.as_utc(schedule.last_run_completed_at or schedule.last_run_started_at)
        return _iso(ref + timedelta(minutes=schedule.interval_minutes)) if ref else None
    return None


def _schedule_to_dict(schedule: RoutineSchedule) -> dict:
    """Convert a RoutineSchedule ORM instance to a plain dict."""
    return {
        "id": schedule.id,
        "routine_id": schedule.routine_id,
        "user_id": schedule.user_id,
        "schedule_type": schedule.schedule_type,
        "daily_time_utc": schedule.daily_time_utc,
        "daily_time_local": schedule.daily_time_local,
        "timezone": schedule.timezone,
        "weekly_days": (
            schedule_timing.parse_weekly_days(schedule.weekly_days)
            if schedule.schedule_type == "weekly" else None
        ),
        "hourly_minute": schedule.hourly_minute,
        "interval_minutes": schedule.interval_minutes,
        "is_enabled": schedule.is_enabled,
        "next_due_at": _next_due_view(schedule),
        "last_run_started_at": _iso(schedule.last_run_started_at),
        "last_run_completed_at": _iso(schedule.last_run_completed_at),
        "is_running": schedule.is_running,
        "last_conversation_id": schedule.last_conversation_id,
        "created_at": _iso(schedule.created_at),
        "updated_at": _iso(schedule.updated_at),
    }


def _run_to_dict(run: RoutineScheduleRun) -> dict:
    return {
        "id": run.id,
        "schedule_id": run.schedule_id,
        "occurrence_at": _iso(schedule_timing.as_utc(run.occurrence_at)),
        "status": run.status,
        "attempt": run.attempt,
        "conversation_id": run.conversation_id,
        "started_at": _iso(schedule_timing.as_utc(run.started_at)),
        "finished_at": _iso(schedule_timing.as_utc(run.finished_at)),
    }


def _routine_summary(
    routine: Routine,
    project_public: bool = False,
    user_email: Optional[str] = None,
    project_archived: bool = False,
) -> dict:
    """Extract the routine fields needed by the scheduler.

    ``project_public`` / ``user_email`` come from the joined project and
    owner rows: the scheduler skips schedules of public-project routines
    while that feature is gated off for the owner. ``project_archived``
    likewise pauses every schedule of an archived project.
    """
    return {
        "id": routine.id,
        "project_id": routine.project_id,
        "user_id": routine.user_id,
        "name": routine.name,
        "prompt": routine.prompt,
        "guide_id": routine.guide_id,
        "model": routine.model,
        "project_public": bool(project_public),
        "project_archived": bool(project_archived),
        "user_email": user_email,
    }
