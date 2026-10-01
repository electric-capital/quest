"""REST API endpoints for routine management.

Routines are canned prompts attached to projects that can be run in one click.
Each routine combines a prompt, an optional guide, and is scoped to a project.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.routine_costs import get_routine_cost_report
from config.feature_gates import (
    guides_enabled_for,
    public_project_routines_enabled_for,
)
from db.project_store import get_project
from db.routine_store import (
    StaleRoutineError,
    create_routine,
    get_routine,
    list_routines_with_schedules,
    update_routine,
    delete_routine,
)
from db.skill_store import (
    list_routine_autoloaded_skill_ids,
    set_routine_skill_autoload,
    user_can_access_skill,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["routines"],
)


def _require_guides_enabled(user: dict) -> None:
    """403 unless the deprecated guides feature is enabled for this user.

    Routine guide overrides are the last remaining way a new conversation
    gets a guide, so they are gated with the rest of the feature.
    """
    if not guides_enabled_for(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "guides_disabled",
                "message": (
                    "Guides are disabled on this server, so routines cannot "
                    "be given a guide override. Use auto-loaded skills instead."
                ),
            },
        )


def project_routines_allowed(project: dict, user: dict) -> bool:
    """Whether this project may have (and run) routines for this user.

    Private projects always can. A public project can only while the admin
    ``public_project_routines`` feature gate (and the ``public_projects``
    gate it sits on) is open for the user: its routines run in the
    internet-enabled sandbox, scheduled ones unattended.
    """
    return not project.get("public") or public_project_routines_enabled_for(
        user["email"]
    )


def require_project_routines_allowed(project: dict, user: dict) -> None:
    """400 ``public_project_routines_disabled`` unless routines are allowed.

    The routine and schedule rows are untouched while the gate is closed
    and become reachable again once access is restored.
    """
    if not project_routines_allowed(project, user):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "public_project_routines_disabled",
                "message": (
                    "Routines in public projects are disabled for your "
                    "account or server-wide"
                ),
            },
        )


async def get_routine_project(user: dict, project_id: str) -> dict:
    """The user's project for a routine/schedule endpoint, or an HTTP error.

    404 when the project does not exist (or is not the user's), 400 when it
    is a public project whose routines are gated off for this user.
    """
    project = await get_project(user["id"], project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )
    require_project_routines_allowed(project, user)
    return project


class CreateRoutineRequest(BaseModel):
    name: str
    prompt: str
    guide_id: Optional[str] = None
    model: Optional[str] = None


class RoutineAutoloadRequest(BaseModel):
    enabled: bool


class UpdateRoutineRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    guide_id: Optional[str] = None
    clear_guide: Optional[bool] = None  # If true, set guide_id to None
    model: Optional[str] = None
    clear_model: Optional[bool] = None  # If true, set model to None
    # Optimistic-concurrency token: the routine's updated_at ISO string at
    # the time the client loaded the row. If it no longer matches, the
    # server returns 409 stale_update so a concurrent edit can't be
    # silently clobbered.
    expected_updated_at: Optional[str] = None


@router.get("/projects/{project_id}/routines")
async def list_project_routines(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List all routines for a project."""
    user_id = user["id"]

    project = await get_project(user_id, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )
    # Routines of a public project are hidden while the gate is closed for
    # this user (the rows are kept and come back when access is restored).
    if not project_routines_allowed(project, user):
        return {"routines": []}

    routines = await list_routines_with_schedules(project_id)
    return {"routines": routines}


@router.post("/projects/{project_id}/routines", status_code=201)
async def create_project_routine(
    project_id: str,
    body: CreateRoutineRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new routine in a project."""
    user_id = user["id"]

    await get_routine_project(user, project_id)

    if not body.name or not body.name.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_name", "message": "Name cannot be empty"},
        )
    if len(body.name) > 100:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_name", "message": "Name cannot exceed 100 characters"},
        )
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_prompt", "message": "Prompt cannot be empty"},
        )
    if body.guide_id:
        _require_guides_enabled(user)

    try:
        routine = await create_routine(
            user_id=user_id,
            project_id=project_id,
            name=body.name,
            prompt=body.prompt,
            guide_id=body.guide_id,
            model=body.model,
        )
    except Exception as e:
        error_msg = str(e)
        if "UNIQUE constraint failed" in error_msg or "unique" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_name",
                    "message": f"A routine named '{body.name.strip()}' already exists in this project",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "create_failed", "message": error_msg},
        )

    return routine


@router.get("/projects/{project_id}/routines/{routine_id}")
async def get_project_routine(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get a specific routine by ID."""
    user_id = user["id"]

    await get_routine_project(user, project_id)

    routine = await get_routine(user_id, routine_id)
    if not routine or routine["project_id"] != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    return routine


@router.get("/projects/{project_id}/routines/{routine_id}/costs")
async def get_project_routine_costs(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Inference cost report for one routine (Routine Settings > Costs).

    Rolling 7/28-day totals with the preceding period, the lifetime total
    and the most recent runs; see chat/routine_costs.py for the attribution
    model (per run, bucketed by run start) and the null-on-unpriced
    convention.
    """
    user_id = user["id"]

    await get_routine_project(user, project_id)

    routine = await get_routine(user_id, routine_id)
    if not routine or routine["project_id"] != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    return await get_routine_cost_report(routine)


@router.put("/projects/{project_id}/routines/{routine_id}")
async def update_project_routine(
    project_id: str,
    routine_id: str,
    body: UpdateRoutineRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update a routine's name, prompt, and/or guide."""
    user_id = user["id"]

    await get_routine_project(user, project_id)

    # Determine guide_id value using ellipsis sentinel:
    # - ... means "don't change" (default)
    # - None means "clear the guide"
    # - a string means "set to this guide ID"
    # Clearing is always allowed (lets users tidy up leftovers); attaching a
    # guide requires the guides feature gate to be open for this user.
    guide_id_value = ...  # sentinel: don't change
    if body.clear_guide:
        guide_id_value = None
    elif body.guide_id is not None:
        _require_guides_enabled(user)
        guide_id_value = body.guide_id

    model_value = ...  # sentinel: don't change
    if body.clear_model:
        model_value = None
    elif body.model is not None:
        model_value = body.model

    try:
        routine = await update_routine(
            user_id=user_id,
            routine_id=routine_id,
            name=body.name,
            prompt=body.prompt,
            guide_id=guide_id_value,
            model=model_value,
            expected_updated_at=body.expected_updated_at,
        )
    except StaleRoutineError as e:
        # Return a flat-body 409 (no FastAPI `detail` wrapper) so the FE's
        # generic `handleErrorResponse` parser can read `error`/`message`/
        # `current` from the top level of the response body. See
        # frontend/src/api/client.ts handleErrorResponse + ApiClientError.
        return JSONResponse(
            status_code=409,
            content={
                "error": "stale_update",
                "message": "This routine was modified by someone else.",
                "current": e.current,
            },
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "update_failed", "message": str(e)},
        )
    except Exception as e:
        error_msg = str(e)
        if "UNIQUE constraint failed" in error_msg or "unique" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_name",
                    "message": "A routine with that name already exists in this project",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "update_failed", "message": error_msg},
        )

    if not routine:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    return routine


@router.delete("/projects/{project_id}/routines/{routine_id}")
async def delete_project_routine(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a routine."""
    user_id = user["id"]

    await get_routine_project(user, project_id)

    deleted = await delete_routine(user_id, routine_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    return {"success": True}


@router.get("/projects/{project_id}/routines/{routine_id}/skills/autoloaded")
async def get_routine_autoloaded_skills_route(
    project_id: str,
    routine_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get auto-loaded skill IDs for a routine."""
    user_id = user["id"]

    await get_routine_project(user, project_id)

    routine = await get_routine(user_id, routine_id)
    if not routine or routine["project_id"] != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    skill_ids = await list_routine_autoloaded_skill_ids(routine_id)
    return {"skill_ids": skill_ids}


@router.put("/projects/{project_id}/routines/{routine_id}/skills/{skill_id}/autoload")
async def toggle_routine_skill_autoload(
    project_id: str,
    routine_id: str,
    skill_id: str,
    body: RoutineAutoloadRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Toggle auto-load for a skill on a routine."""
    user_id = user["id"]

    project = await get_routine_project(user, project_id)

    routine = await get_routine(user_id, routine_id)
    if not routine or routine["project_id"] != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Routine not found"},
        )

    # Public conversations load no skills of any tier (skill bodies are
    # internal data), so a routine there cannot auto-load one. Turning an
    # auto-load off stays allowed.
    if project.get("public") and body.enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "public_project_no_skills",
                "message": "Routines in public projects cannot auto-load skills",
            },
        )

    if not await user_can_access_skill(user_id, skill_id):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Skill not found"},
        )

    await set_routine_skill_autoload(routine_id, skill_id, body.enabled)
    return {"success": True}
