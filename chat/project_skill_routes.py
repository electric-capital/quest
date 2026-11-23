"""REST API endpoints for project skill management.

Project skills are skills scoped to a specific project. They live in the
existing skills table with a project_id column and visibility='project'.
This module also handles project-level skill auto-load toggling.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.conversation_access import require_owned_project
from db.skill_store import (
    create_skill,
    list_project_skills,
    get_project_skill,
    update_project_skill,
    delete_project_skill,
    list_project_autoloaded_skill_ids,
    set_project_skill_autoload,
    user_can_access_skill,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["project-skills"],
)


class CreateProjectSkillRequest(BaseModel):
    name: str
    description: str = ""
    content: str


class UpdateProjectSkillRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None


class ProjectAutoloadRequest(BaseModel):
    enabled: bool


def _reject_public_project(project: dict) -> None:
    """Raise 400 when the target project is public.

    Public projects cannot have project skills (v1): their conversations
    run without any skill tier, so a project-skill store would be dead
    weight at best and a prompt-injection vector at worst.
    """
    if project.get("public"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "public_project_no_skills",
                "message": "Public projects cannot have project skills",
            },
        )


@router.get("/projects/{project_id}/skills")
async def list_skills_for_project(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List all project-specific skills for a project."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    if project.get("public"):
        # Public projects have no skills by construction; return empty
        # rather than 400 so read-only UI code needs no special-casing.
        return {"skills": []}

    skills = await list_project_skills(project_id)
    return {"skills": skills}


@router.post("/projects/{project_id}/skills", status_code=201)
async def create_skill_for_project(
    project_id: str,
    body: CreateProjectSkillRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new skill in a project."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    _reject_public_project(project)

    try:
        skill = await create_skill(
            creator_id=user_id,
            name=body.name,
            description=body.description,
            content=body.content,
            project_id=project_id,
        )
    except ValueError as e:
        error_msg = str(e)
        if "already exists" in error_msg:
            raise HTTPException(
                status_code=409,
                detail={"error": "duplicate_name", "message": error_msg},
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_error", "message": error_msg},
        )

    return skill


@router.get("/projects/{project_id}/skills/autoloaded")
async def get_project_autoloaded_skills(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get auto-loaded skill IDs for a project."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    if project.get("public"):
        return {"skill_ids": []}

    skill_ids = await list_project_autoloaded_skill_ids(project_id)
    return {"skill_ids": skill_ids}


@router.get("/projects/{project_id}/skills/{skill_id}")
async def get_skill_for_project(
    project_id: str,
    skill_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get a specific project skill by ID."""
    user_id = user["id"]

    await require_owned_project(user_id, project_id)

    skill = await get_project_skill(project_id, skill_id)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Skill not found"},
        )

    return skill


@router.put("/projects/{project_id}/skills/{skill_id}")
async def update_skill_for_project(
    project_id: str,
    skill_id: str,
    body: UpdateProjectSkillRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update a project skill's name, description, and/or content."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    _reject_public_project(project)

    try:
        skill = await update_project_skill(
            project_id=project_id,
            skill_id=skill_id,
            name=body.name,
            description=body.description,
            content=body.content,
        )
    except ValueError as e:
        error_msg = str(e)
        if "already exists" in error_msg:
            raise HTTPException(
                status_code=409,
                detail={"error": "duplicate_name", "message": error_msg},
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_error", "message": error_msg},
        )

    if not skill:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Skill not found"},
        )

    return skill


@router.delete("/projects/{project_id}/skills/{skill_id}")
async def delete_skill_for_project(
    project_id: str,
    skill_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a project skill."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    _reject_public_project(project)

    deleted = await delete_project_skill(project_id, skill_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Skill not found"},
        )

    return {"success": True}


@router.put("/projects/{project_id}/skills/{skill_id}/autoload")
async def toggle_project_skill_autoload(
    project_id: str,
    skill_id: str,
    body: ProjectAutoloadRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Toggle auto-load for a skill in a project."""
    user_id = user["id"]

    project = await require_owned_project(user_id, project_id)

    _reject_public_project(project)

    if body.enabled:
        # The skill must be one of this project's own skills or a skill the
        # caller can currently access (own / public / shared with them).
        # Without this check any UUID could be attached to an owned project
        # and its body would land in the project's system prompt.
        is_project_local = await get_project_skill(project_id, skill_id) is not None
        if not is_project_local and not await user_can_access_skill(user_id, skill_id):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Skill not found"},
            )

    await set_project_skill_autoload(project_id, skill_id, body.enabled)
    return {"success": True}
