"""REST API endpoints for routine schedule management.

Schedules allow routines to run automatically on a timer.
Each routine can have at most one schedule.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from db.project_store import get_project
from db.routine_store import get_routine
from db.schedule_store import (
    StaleScheduleError,
    create_schedule,
    get_schedule_for_routine,
    update_schedule,
    delete_schedule_for_routine,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["schedules"],
)


class CreateScheduleRequest(BaseModel):
    schedule_type: str  # 'daily', 'weekly', 'hourly', 'every_n_minutes'
    daily_time_local: Optional[str] = None  # "HH:MM" in user's timezone (daily + weekly)
    timezone: Optional[str] = None  # IANA timezone string (daily + weekly)
    weekly_days: Optional[list[int]] = None  # weekly: 0=Monday .. 6=Sunday
    hourly_minute: Optional[int] = None  # 0-59
    interval_minutes: Optional[int] = None  # >= 1


class UpdateScheduleRequest(BaseModel):
    schedule_type: Optional[str] = None
    daily_time_local: Optional[str] = None
    timezone: Optional[str] = None
    weekly_days: Optional[list[int]] = None
    hourly_minute: Optional[int] = None
    interval_minutes: Optional[int] = None
    is_enabled: Optional[bool] = None
    # Optimistic-concurrency token: the schedule's updated_at ISO string at
    # the time the client loaded the row. If it no longer matches, the
    # server returns 409 stale_update so a concurrent edit can't be
    # silently clobbered.
    expected_updated_at: Optional[str] = None


@router.get("/projects/{project_id}/routines/{routine_id}/schedule")
async def get_routine_schedule(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get the schedule for a routine (or 404 if no schedule).

    Includes ``recent_runs``: the newest run-ledger rows (completed / failed /
    interrupted / missed / running).
    """
    user_id = user["id"]
    await _validate_ownership(user_id, project_id, routine_id)

    schedule = await get_schedule_for_routine(routine_id, include_runs=True)
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "No schedule exists for this routine"},
        )
    return schedule


@router.post("/projects/{project_id}/routines/{routine_id}/schedule", status_code=201)
async def create_routine_schedule(
    project_id: str,
    routine_id: str,
    body: CreateScheduleRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a schedule for a routine."""
    user_id = user["id"]
    await _validate_ownership(user_id, project_id, routine_id)

    try:
        # Convert local time to UTC for daily / weekly schedules
        daily_time_utc = None
        if body.schedule_type in ('daily', 'weekly') and body.daily_time_local and body.timezone:
            daily_time_utc = _local_time_to_utc(body.daily_time_local, body.timezone)

        schedule = await create_schedule(
            user_id=user_id,
            routine_id=routine_id,
            schedule_type=body.schedule_type,
            daily_time_utc=daily_time_utc,
            daily_time_local=body.daily_time_local,
            timezone_str=body.timezone,
            hourly_minute=body.hourly_minute,
            interval_minutes=body.interval_minutes,
            weekly_days=body.weekly_days,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_schedule", "message": str(e)},
        )

    return schedule


@router.put("/projects/{project_id}/routines/{routine_id}/schedule")
async def update_routine_schedule(
    project_id: str,
    routine_id: str,
    body: UpdateScheduleRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update a routine's schedule."""
    user_id = user["id"]
    await _validate_ownership(user_id, project_id, routine_id)

    schedule = await get_schedule_for_routine(routine_id)
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "No schedule exists for this routine"},
        )

    try:
        # Rebuild UTC time if daily / weekly schedule params changed
        daily_time_utc = ...
        effective_type = body.schedule_type or schedule["schedule_type"]
        if effective_type in ('daily', 'weekly'):
            local_time = body.daily_time_local or schedule.get("daily_time_local")
            tz = body.timezone or schedule.get("timezone")
            if local_time and tz:
                daily_time_utc = _local_time_to_utc(local_time, tz)

        updated = await update_schedule(
            schedule_id=schedule["id"],
            user_id=user_id,
            schedule_type=body.schedule_type,
            daily_time_utc=daily_time_utc,
            daily_time_local=body.daily_time_local if body.daily_time_local is not None else ...,
            timezone_str=body.timezone if body.timezone is not None else ...,
            hourly_minute=body.hourly_minute if body.hourly_minute is not None else ...,
            interval_minutes=body.interval_minutes if body.interval_minutes is not None else ...,
            is_enabled=body.is_enabled,
            expected_updated_at=body.expected_updated_at,
            weekly_days=body.weekly_days if body.weekly_days is not None else ...,
        )
    except StaleScheduleError as e:
        # Return a flat-body 409 (no FastAPI `detail` wrapper) so the FE's
        # generic `handleErrorResponse` parser can read `error`/`message`/
        # `current` from the top level of the response body. See
        # frontend/src/api/client.ts handleErrorResponse + ApiClientError.
        return JSONResponse(
            status_code=409,
            content={
                "error": "stale_update",
                "message": "This routine's schedule was modified by someone else.",
                "current": e.current,
            },
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_schedule", "message": str(e)},
        )

    if not updated:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Schedule not found"},
        )

    return updated


@router.delete("/projects/{project_id}/routines/{routine_id}/schedule")
async def delete_routine_schedule(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a routine's schedule."""
    user_id = user["id"]
    await _validate_ownership(user_id, project_id, routine_id)

    deleted = await delete_schedule_for_routine(routine_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "No schedule exists for this routine"},
        )

    return {"success": True}


async def _validate_ownership(user_id: int, project_id: str, routine_id: str):
    """Validate that the user owns the project and the routine belongs to it."""
    project = await get_project(user_id, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    routine = await get_routine(user_id, routine_id)
    if not routine or routine["project_id"] != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )


def _local_time_to_utc(time_local: str, tz_name: str) -> str:
    """Convert a local HH:MM time to UTC HH:MM using the given timezone.

    Uses today's date for DST-aware conversion. This is recalculated
    periodically by the scheduler to account for DST transitions.
    """
    from datetime import datetime as dt, date
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        raise ValueError(f"Invalid timezone: {tz_name}")

    h, m = int(time_local[:2]), int(time_local[3:])
    today = date.today()
    local_dt = dt(today.year, today.month, today.day, h, m, tzinfo=tz)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return f"{utc_dt.hour:02d}:{utc_dt.minute:02d}"
