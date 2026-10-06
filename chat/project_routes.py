"""REST API endpoints for project management."""

import asyncio
import logging
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import files as doc_files
from chat.realtime import bus, events as realtime_events
from chat.storage import ChatStorage
from db import doc_store
from db.project_store import (
    create_project,
    get_project,
    list_projects,
    update_project,
    set_project_archived,
    delete_project,
)
from db.conversation_store import (
    get_conversation_meta,
    list_project_conversations_meta,
    set_conversation_project,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["projects"],
)


class CreateProjectRequest(BaseModel):
    name: str
    # Public mode: internet-enabled sandbox, no internal data access.
    # Creation-time only; projects cannot be converted public <-> private.
    public: bool = False


class CreateProjectFromConversationRequest(BaseModel):
    # Deliberately no ``public`` field: converting an existing conversation
    # moves its (potentially internal-data-bearing) workspace files into the
    # project, so public projects are creatable only via plain POST /projects
    # with an empty workspace.
    name: str
    conversation_id: str


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    guide: Optional[str] = None


class CreateProjectConversationRequest(BaseModel):
    routine_id: Optional[str] = None


def _public_projects_enabled_for(user: dict) -> bool:
    """Whether the public-projects gate is open for this user.

    The admin gate can be open to all users or restricted to specific user
    emails; outside the allowed list it behaves exactly as if closed.
    """
    from config.feature_gates import (
        is_feature_enabled_for_user, FEATURE_PUBLIC_PROJECTS,
    )
    return is_feature_enabled_for_user(FEATURE_PUBLIC_PROJECTS, user["email"])


async def _get_visible_project(user: dict, project_id: str) -> Optional[dict]:
    """``get_project``, additionally hiding public projects while gated off.

    While the ``public_projects`` feature gate is closed for this user,
    existing public projects must disappear from the UI: they are dropped
    from the list endpoint and 404 on direct access (this helper). The rows
    and workspaces are untouched, so restoring access brings them back.
    """
    project = await get_project(user["id"], project_id)
    if project and project.get("public") and not _public_projects_enabled_for(user):
        return None
    return project


async def _create_project_checked(
    user: dict, name: str, public: bool = False
) -> dict:
    """Validate the name and create a project row, mapping errors to HTTP."""
    if public and not _public_projects_enabled_for(user):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "public_projects_disabled",
                "message": "Public projects are disabled on this server",
            },
        )
    if not name or not name.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_name", "message": "Name cannot be empty"},
        )
    if len(name) > 100:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_name", "message": "Name cannot exceed 100 characters"},
        )

    try:
        return await create_project(user["id"], name=name, public=public)
    except Exception as e:
        error_msg = str(e)
        if "UNIQUE constraint failed" in error_msg or "unique" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_name",
                    "message": f"A project named '{name.strip()}' already exists",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "create_failed", "message": error_msg},
        )


@router.post("/projects", status_code=201)
async def create_user_project(
    body: CreateProjectRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new project."""
    project = await _create_project_checked(user, body.name, public=body.public)

    # Create project workspace directory
    ChatStorage.create_project_workspace(project["id"])

    project["conversation_count"] = 0
    return project


@router.post("/projects/from-conversation", status_code=201)
async def create_project_from_conversation(
    body: CreateProjectFromConversationRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new project seeded from an existing standalone conversation.

    The conversation's workspace files move into the new project's shared
    workspace and the conversation becomes the project's first conversation.
    """
    user_id = user["id"]

    meta = await get_conversation_meta(user_id, body.conversation_id)
    if not meta:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Conversation not found"},
        )
    if meta.get("project_id"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "already_in_project",
                "message": "Conversation already belongs to a project",
            },
        )
    if meta.get("origin") == "slack":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "slack_conversation",
                "message": "Slack conversations cannot be converted to projects",
            },
        )
    if meta.get("origin") == "user_subagent":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "user_subagent_conversation",
                "message": (
                    "Cross-user subagent conversations cannot be converted "
                    "to projects"
                ),
            },
        )
    if meta.get("origin") == "inference_api":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "inference_api_conversation",
                "message": (
                    "Inference API conversations cannot be converted to "
                    "projects"
                ),
            },
        )

    project = await _create_project_checked(user, body.name)
    ChatStorage.create_project_workspace(project["id"])

    # Move files before flipping the DB pointer so the conversation never
    # resolves to an empty project workspace while its files are in flight.
    ChatStorage.move_conversation_workspace_to_project(body.conversation_id, project["id"])

    updated = await set_conversation_project(user_id, body.conversation_id, project["id"])
    if updated is None:
        # Raced with another request that attached the conversation elsewhere.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict",
                "message": "Conversation was moved by another request",
            },
        )

    # Project membership changes the system prompt (project guide + skill
    # auto-loads), so drop any cached chat sessions for this user.
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user_id)

    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_conversation_list_changed(
                conversation_id=body.conversation_id,
                action="moved_to_project",
            ),
        )
    except Exception:
        logger.debug(
            "[projects] publish conversation_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )

    project["conversation_count"] = 1
    return project


@router.get("/projects")
async def list_user_projects(
    include_archived: bool = False,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List the authenticated user's projects.

    Archived projects are left out unless ``include_archived=true`` (the
    sidebar's "Show Archived" toggle), mirroring ``GET /conversations``.
    """
    projects = await list_projects(user["id"], include_archived=include_archived)
    # Hide public projects while the gate is closed for this user (the
    # rows persist; restoring access brings them back).
    if not _public_projects_enabled_for(user):
        projects = [p for p in projects if not p.get("public")]
    return {"projects": projects}


@router.get("/projects/{project_id}")
async def get_user_project(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get a specific project by ID."""
    project = await _get_visible_project(user, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )
    return project


@router.put("/projects/{project_id}")
async def update_user_project(
    project_id: str,
    body: UpdateProjectRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update a project's name and/or guide."""
    user_id = user["id"]

    # Hidden (gated-off public) projects 404 like nonexistent ones.
    if await _get_visible_project(user, project_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    try:
        project = await update_project(user_id, project_id, name=body.name, guide=body.guide)
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
                    "message": "A project with that name already exists",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "update_failed", "message": error_msg},
        )

    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    # Invalidate cached chat sessions when guide changes
    if body.guide is not None:
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user_id)

    return project


async def _set_archived_checked(user: dict, project_id: str, archived: bool) -> dict:
    """Flip the archived flag on a visible project, 404 otherwise."""
    if await _get_visible_project(user, project_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )
    project = await set_project_archived(user["id"], project_id, archived)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )
    return project


@router.put("/projects/{project_id}/archive")
async def archive_user_project(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Archive a project: hidden from the default list, scheduled routines
    paused, everything kept. The twin of ``PUT /conversations/{id}/archive``."""
    return await _set_archived_checked(user, project_id, True)


@router.put("/projects/{project_id}/unarchive")
async def unarchive_user_project(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Restore an archived project."""
    return await _set_archived_checked(user, project_id, False)


@router.delete("/projects/{project_id}")
async def delete_user_project(
    project_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a project and all its conversations."""
    user_id = user["id"]

    # Hidden (gated-off public) projects 404 like nonexistent ones -- they
    # are preserved, not deletable, while invisible.
    if await _get_visible_project(user, project_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    # Get conversation list BEFORE deleting (CASCADE will remove rows)
    conversations = await list_project_conversations_meta(project_id)
    # Same for the project's Quest Docs: the docs.project_id CASCADE removes
    # the rows (and their shares); the directories are swept below.
    doc_ids = await doc_store.list_doc_ids_for_project(project_id)

    deleted = await delete_project(user_id, project_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    # Delete filesystem: each project doc's directory (best-effort, never
    # raises) -- first, so a failure in the rmtree loops below cannot skip it
    if doc_ids:
        await asyncio.to_thread(_delete_doc_dirs, doc_ids)
        from chat.docs import events as doc_events

        doc_events.publish_doc_list_changed(user_id)

    # Delete filesystem: project workspace
    ChatStorage.delete_project_workspace(project_id)

    # Delete filesystem: each conversation's chat directory
    for conv in conversations:
        conv_dir = ChatStorage.get_conversation_dir(conv["id"])
        if conv_dir.exists():
            shutil.rmtree(conv_dir)

    return {"success": True}


def _delete_doc_dirs(doc_ids: list[str]) -> None:
    """Remove deleted docs' directories; a failure is logged, never raised
    (the rows are already gone, so the project delete has succeeded)."""
    for doc_id in doc_ids:
        try:
            doc_files.delete_doc_dir(doc_id)
        except Exception:
            logger.warning(
                "[docs] could not remove the directory of doc %s", doc_id,
                exc_info=True,
            )


# --- Project conversation endpoints ---


@router.post("/projects/{project_id}/conversations")
async def create_project_conversation(
    project_id: str,
    body: CreateProjectConversationRequest = CreateProjectConversationRequest(),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new conversation within a project."""
    user_id = user["id"]

    # Verify project exists, belongs to user, and is not hidden by the
    # public-projects gate
    project = await _get_visible_project(user, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    # A routine id is only meaningful for one of this project's own
    # routines: the stored id drives the routine's auto-loaded skills into
    # the system prompt on every turn (chat/gemini_api/conversation.py), so
    # accepting a foreign routine id here would pull another user's private
    # skill bodies into the caller's conversation. Same 404 shape as the
    # routine routes so the id's existence isn't confirmed either way.
    if body.routine_id is not None:
        from db.routine_store import get_routine
        routine = await get_routine(user_id, body.routine_id)
        if not routine or routine["project_id"] != project_id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Routine not found"},
            )
        # One-click run of a routine in a public project: refused while
        # routines there are gated off for this user (the same gate the
        # scheduler and run_conversation_turn enforce).
        from chat.routine_routes import require_project_routines_allowed
        require_project_routines_allowed(project, user)

    conversation_id, created_at_str = await ChatStorage.create_project_conversation(
        user_id, project_id, routine_id=body.routine_id,
    )

    # Same per-user WS nudge the standalone create endpoint sends, so a
    # sidebar drilled into this project picks the new row up immediately
    # instead of waiting for the first reply to finish streaming (or the
    # 30s poll).
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_conversation_list_changed(
                conversation_id=conversation_id,
                action="created",
            ),
        )
    except Exception:
        logger.debug(
            "[projects] publish conversation_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )

    # Mirror the shape of the standalone create endpoint so the FE can
    # optimistically seed its conversation store without an extra
    # GET /conversations/<id> round trip on the new-project-chat path.
    return {
        "id": conversation_id,
        "created_at": created_at_str,
        "last_message_seq": 0,
        "origin": "web",
        "project_id": project_id,
        "model": None,
    }


@router.get("/projects/{project_id}/conversations")
async def list_project_conversations_endpoint(
    project_id: str,
    include_archived: bool = Query(False),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List conversations in a project."""

    project = await _get_visible_project(user, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    conversations = await ChatStorage.list_project_conversations(project_id, include_archived=include_archived)
    return {"conversations": conversations}


@router.get("/projects/{project_id}/conversations/{conversation_id}")
async def get_project_conversation_endpoint(
    project_id: str,
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get conversation history from a project."""
    user_id = user["id"]

    # Verify project ownership (and gate visibility)
    project = await _get_visible_project(user, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Project not found"},
        )

    # Verify conversation belongs to this project
    meta = await get_conversation_meta(user_id, conversation_id)
    if not meta or meta.get("project_id") != project_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Conversation not found in this project"},
        )

    conversation = ChatStorage.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Conversation not found"},
        )

    return conversation
