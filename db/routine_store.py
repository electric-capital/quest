"""Routine data access layer.

Provides async CRUD operations for project routines. Routines are canned prompts
attached to projects that can be run in one click.

This module follows the same async pattern as db/memory_store.py and
db/guide_store.py: each function opens a fresh AsyncSessionLocal() session.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete

from db.engine import AsyncSessionLocal
from db.models import Project, Routine, RoutineSchedule


# Maximum routine name length
MAX_ROUTINE_NAME_LENGTH = 100

# Maximum routine prompt size: 16KB
MAX_ROUTINE_PROMPT_SIZE = 16 * 1024


class StaleRoutineError(Exception):
    """Raised when update_routine() detects that the row was modified
    concurrently by someone else (expected_updated_at mismatch).

    Carries the current row (post-conflict) so the route handler can return
    it in the 409 body, sparing the FE a second GET round-trip.
    """

    def __init__(self, current: dict):
        super().__init__("Routine was modified by another writer")
        self.current = current


async def create_routine(
    user_id: int,
    project_id: str,
    name: str,
    prompt: str,
    guide_id: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Create a new routine in a project."""
    # Validation
    if not name or not name.strip():
        raise ValueError("Routine name cannot be empty.")
    if len(name) > MAX_ROUTINE_NAME_LENGTH:
        raise ValueError(f"Routine name exceeds maximum length of {MAX_ROUTINE_NAME_LENGTH} characters.")
    if not prompt or not prompt.strip():
        raise ValueError("Routine prompt cannot be empty.")
    if len(prompt.encode("utf-8")) > MAX_ROUTINE_PROMPT_SIZE:
        raise ValueError(f"Routine prompt exceeds maximum size of {MAX_ROUTINE_PROMPT_SIZE} bytes.")

    async with AsyncSessionLocal() as db:
        # Stamp updated_at on create so the optimistic-concurrency guard in
        # update_routine() has a non-null baseline to compare on first edit.
        now = datetime.now(timezone.utc)
        routine = Routine(
            user_id=user_id,
            project_id=project_id,
            name=name.strip(),
            prompt=prompt,
            guide_id=guide_id,
            model=model,
            updated_at=now,
        )
        db.add(routine)
        await db.commit()
        await db.refresh(routine)
        return _routine_to_dict(routine)


async def get_routine(user_id: int, routine_id: str) -> Optional[dict]:
    """Get a specific routine by ID, scoped to user."""
    async with AsyncSessionLocal() as db:
        routine = await db.get(Routine, routine_id)
        if routine and routine.user_id == user_id:
            return _routine_to_dict(routine)
        return None


async def list_routines(project_id: str) -> list[dict]:
    """List all routines for a project, ordered alphabetically by name."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Routine)
            .where(Routine.project_id == project_id)
            .order_by(Routine.name.asc())
        )
        routines = result.scalars().all()
        return [_routine_to_dict(r) for r in routines]


async def get_routine_labels(routine_ids: set[str]) -> dict[str, dict]:
    """Return display labels for a batch of routine ids, across all owners.

    Admin user-report companion: the per-routine cost breakdown only knows
    routine ids (from the conversation rows) and needs the routine name plus
    the owning project's name to label each row -- routine names are only
    unique per project, so the project name disambiguates. One query,
    no per-id round trips; ids without a surviving routine row (the
    conversations' ``routine_id`` is SET NULL on routine delete, so this is
    a race at most) are simply absent from the result.

    Returns:
        Mapping of routine_id -> {"name", "project_id", "project_name"}.
    """
    if not routine_ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Routine.id, Routine.name, Routine.project_id, Project.name)
            .join(Project, Project.id == Routine.project_id)
            .where(Routine.id.in_(routine_ids))
        )
        return {
            routine_id: {
                "name": name,
                "project_id": project_id,
                "project_name": project_name,
            }
            for routine_id, name, project_id, project_name in result.all()
        }


async def list_routines_with_schedules(project_id: str) -> list[dict]:
    """List all routines for a project, including schedule data.

    Each routine dict includes a 'schedule' key that is either a dict with
    schedule data or None if the routine has no schedule. This avoids N+1
    API calls when the frontend needs to show schedule indicators.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Routine, RoutineSchedule)
            .outerjoin(RoutineSchedule, Routine.id == RoutineSchedule.routine_id)
            .where(Routine.project_id == project_id)
            .order_by(Routine.name.asc())
        )
        rows = result.all()
        routines = []
        for routine, schedule in rows:
            d = _routine_to_dict(routine)
            if schedule:
                d["schedule"] = _schedule_summary(schedule)
            else:
                d["schedule"] = None
            routines.append(d)
        return routines


async def update_routine(
    user_id: int,
    routine_id: str,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    guide_id: Optional[str] = ...,  # sentinel: None means "clear guide", ... means "don't change"
    model: Optional[str] = ...,  # sentinel: None means "clear model", ... means "don't change"
    expected_updated_at: Optional[str] = None,
) -> Optional[dict]:
    """Update a routine's name, prompt, and/or guide.

    ``expected_updated_at`` is the optimistic-concurrency token: if provided,
    it must match the row's current ``updated_at`` (ISO string) or the call
    raises :class:`StaleRoutineError`. Passing ``None`` skips the check
    (back-compat path for legacy rows with NULL updated_at).
    """
    if name is not None:
        if not name or not name.strip():
            raise ValueError("Routine name cannot be empty.")
        if len(name) > MAX_ROUTINE_NAME_LENGTH:
            raise ValueError(f"Routine name exceeds maximum length of {MAX_ROUTINE_NAME_LENGTH} characters.")
    if prompt is not None:
        if not prompt or not prompt.strip():
            raise ValueError("Routine prompt cannot be empty.")
        if len(prompt.encode("utf-8")) > MAX_ROUTINE_PROMPT_SIZE:
            raise ValueError(f"Routine prompt exceeds maximum size of {MAX_ROUTINE_PROMPT_SIZE} bytes.")

    async with AsyncSessionLocal() as db:
        routine = await db.get(Routine, routine_id)
        if not routine or routine.user_id != user_id:
            return None

        # Optimistic concurrency check. Compare before mutating so we can
        # surface the current row state in the conflict response.
        if expected_updated_at is not None:
            current_token = routine.updated_at.isoformat() if routine.updated_at else None
            if current_token != expected_updated_at:
                raise StaleRoutineError(current=_routine_to_dict(routine))

        # Track whether anything actually changed so we don't bump
        # updated_at on a true no-op save (which would generate spurious
        # 409s for the next opener).
        changed = False
        new_name = name.strip() if name is not None else None
        if new_name is not None and new_name != routine.name:
            routine.name = new_name
            changed = True
        if prompt is not None and prompt != routine.prompt:
            routine.prompt = prompt
            changed = True
        if guide_id is not ... and guide_id != routine.guide_id:
            routine.guide_id = guide_id
            changed = True
        if model is not ... and model != routine.model:
            routine.model = model
            changed = True

        if changed:
            routine.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(routine)
        return _routine_to_dict(routine)


async def delete_routine(user_id: int, routine_id: str) -> bool:
    """Delete a routine by ID, scoped to user."""
    async with AsyncSessionLocal() as db:
        routine = await db.get(Routine, routine_id)
        if not routine or routine.user_id != user_id:
            return False
        await db.delete(routine)
        await db.commit()
        return True


async def delete_all_project_routines(project_id: str) -> int:
    """Delete all routines for a project (used when deleting a project)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(Routine).where(Routine.project_id == project_id)
        )
        await db.commit()
        return result.rowcount


async def delete_all_user_routines(user_id: int) -> int:
    """Delete all routines for a user (used during account deletion)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(Routine).where(Routine.user_id == user_id)
        )
        await db.commit()
        return result.rowcount


def _routine_to_dict(routine: Routine) -> dict:
    """Convert a Routine ORM instance to a plain dict."""
    return {
        "id": routine.id,
        "project_id": routine.project_id,
        "user_id": routine.user_id,
        "name": routine.name,
        "prompt": routine.prompt,
        "guide_id": routine.guide_id,
        "model": routine.model,
        "created_at": routine.created_at.isoformat() if routine.created_at else None,
        "updated_at": routine.updated_at.isoformat() if routine.updated_at else None,
    }


def _schedule_summary(schedule: RoutineSchedule) -> dict:
    """Extract a schedule summary for inclusion in routine list responses."""
    from db.schedule_store import _next_due_view
    from db.schedule_timing import parse_weekly_days

    return {
        "id": schedule.id,
        "schedule_type": schedule.schedule_type,
        "daily_time_local": schedule.daily_time_local,
        "timezone": schedule.timezone,
        "weekly_days": (
            parse_weekly_days(schedule.weekly_days)
            if schedule.schedule_type == "weekly" else None
        ),
        "hourly_minute": schedule.hourly_minute,
        "interval_minutes": schedule.interval_minutes,
        "is_enabled": schedule.is_enabled,
        "is_running": schedule.is_running,
        "next_due_at": _next_due_view(schedule),
        "last_run_completed_at": schedule.last_run_completed_at.isoformat() if schedule.last_run_completed_at else None,
    }
