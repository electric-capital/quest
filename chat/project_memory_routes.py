"""REST API endpoints for project memories and their suggestions.

The project twin of chat/memory_routes.py (see chat/project_memories.py):
CRUD under ``/app/api/projects/{project_id}/memories`` for the project's
owner, plus the review endpoints of the model's ``suggest_project_memory``
proposals. Public projects have project memories too.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.conversation_access import require_owned_conversation, require_owned_project
from chat.memory_routes import (
    AcceptSuggestionRequest,
    CreateMemoryRequest,
    UpdateMemoryRequest,
    _suggestion_resolved,
)
from chat.project_memories import (
    SuggestionAlreadyResolvedError,
    accept_suggestion,
    dismiss_suggestion,
)
from db import project_memory_store as store
from db.project_memory_suggestion_store import list_pending

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["project-memories"],
)


def _memory_not_found(memory_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "memory_not_found",
            "message": f"Project memory {memory_id} not found.",
        },
    )


def _invalid_memory(exc: ValueError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": "invalid_memory", "message": str(exc)},
    )


@router.get("/projects/{project_id}/memories")
async def list_project_memories(
    project_id: str,
    q: Optional[str] = Query(default=None, description="FTS5 search query"),
    include_archived: bool = Query(default=False, description="Include archived memories"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    if q:
        try:
            results = await store.search_project_memories(project_id, q)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_search_query",
                    "message": f"Invalid search query: {e}",
                },
            )
    else:
        results = await store.list_project_memories(
            project_id, include_archived=include_archived,
        )
    return {"memories": results}


@router.post("/projects/{project_id}/memories", status_code=201)
async def create_project_memory(
    project_id: str,
    body: CreateMemoryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    try:
        memory = await store.create_project_memory(project_id, body.content)
    except ValueError as e:
        raise _invalid_memory(e)
    logger.info(
        "[project-memories] Created project memory %s (project=%s, user_id=%s)",
        memory["id"], project_id, user["id"],
    )
    return memory


@router.put("/projects/{project_id}/memories/{memory_id}")
async def update_project_memory(
    project_id: str,
    memory_id: str,
    body: UpdateMemoryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    try:
        memory = await store.update_project_memory(project_id, memory_id, body.content)
    except ValueError as e:
        raise _invalid_memory(e)
    if not memory:
        raise _memory_not_found(memory_id)
    return memory


@router.put("/projects/{project_id}/memories/{memory_id}/archive")
async def archive_project_memory(
    project_id: str,
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    memory = await store.set_project_memory_archived(project_id, memory_id, True)
    if not memory:
        raise _memory_not_found(memory_id)
    return memory


@router.put("/projects/{project_id}/memories/{memory_id}/unarchive")
async def unarchive_project_memory(
    project_id: str,
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    memory = await store.set_project_memory_archived(project_id, memory_id, False)
    if not memory:
        raise _memory_not_found(memory_id)
    return memory


@router.delete("/projects/{project_id}/memories/{memory_id}")
async def delete_project_memory(
    project_id: str,
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    await require_owned_project(user["id"], project_id)
    if not await store.delete_project_memory(project_id, memory_id):
        raise _memory_not_found(memory_id)
    logger.info(
        "[project-memories] Deleted project memory %s (project=%s, user_id=%s)",
        memory_id, project_id, user["id"],
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# Project memory suggestions (model-proposed, reviewed from the composer's
# project-memory bubble -- see chat/project_memories.py)
# ---------------------------------------------------------------------------

def _suggestion_not_found(suggestion_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "suggestion_not_found",
            "message": f"Project memory suggestion {suggestion_id} not found.",
        },
    )


@router.get("/conversations/{conversation_id}/project-memory-suggestions")
async def list_conversation_project_memory_suggestions(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Pending project memory suggestions of a conversation, oldest first."""
    user_id = user["id"]
    await require_owned_conversation(user_id, conversation_id)
    suggestions = await list_pending(user_id, conversation_id)
    return {"suggestions": suggestions, "pending_count": len(suggestions)}


@router.post("/project-memory-suggestions/{suggestion_id}/accept")
async def accept_project_memory_suggestion(
    suggestion_id: str,
    body: AcceptSuggestionRequest | None = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Accept a pending suggestion: creates the project memory (optionally
    with edited text) and archives the project memory it replaces."""
    user_id = user["id"]
    content = body.content if body is not None else None
    try:
        result = await accept_suggestion(user_id, suggestion_id, content)
    except SuggestionAlreadyResolvedError as exc:
        raise _suggestion_resolved(exc)
    except ValueError as exc:
        raise _invalid_memory(exc)
    if result is None:
        raise _suggestion_not_found(suggestion_id)
    suggestion, memory = result
    logger.info(
        "[project-memories] Accepted suggestion %s -> project memory %s (user_id=%s)",
        suggestion_id, memory["id"], user_id,
    )
    return {"suggestion": suggestion, "memory": memory}


@router.post("/project-memory-suggestions/{suggestion_id}/dismiss")
async def dismiss_project_memory_suggestion(
    suggestion_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Dismiss a pending suggestion; nothing is saved."""
    user_id = user["id"]
    try:
        suggestion = await dismiss_suggestion(user_id, suggestion_id)
    except SuggestionAlreadyResolvedError as exc:
        raise _suggestion_resolved(exc)
    if suggestion is None:
        raise _suggestion_not_found(suggestion_id)
    return {"suggestion": suggestion}
