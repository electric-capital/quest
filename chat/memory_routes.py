"""REST API endpoints for user memories."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from chat import memory_inbox
from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.conversation_access import require_owned_conversation
from chat.memory_suggestions import (
    SuggestionAlreadyResolvedError,
    accept_suggestion,
    dismiss_suggestion,
)
from db.memory_suggestion_store import list_pending
from db.memory_store import (
    MAX_MEMORY_SIZE,
    create_memory,
    delete_memory,
    get_memory,
    list_memories,
    search_memories,
    update_memory,
    archive_memory,
    unarchive_memory,
)

logger = logging.getLogger(__name__)

# Create APIRouter for memory endpoints
router = APIRouter(
    prefix="/app/api",
    tags=["memories"],
)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class CreateMemoryRequest(BaseModel):
    """Request body for creating a memory."""
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Memory content cannot be empty.")
        return v

    @field_validator("content")
    @classmethod
    def content_max_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_MEMORY_SIZE:
            raise ValueError(
                f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
            )
        return v


class UpdateMemoryRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Memory content cannot be empty.")
        return v

    @field_validator("content")
    @classmethod
    def content_max_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_MEMORY_SIZE:
            raise ValueError(
                f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
            )
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/memories")
async def list_user_memories(
    q: Optional[str] = Query(default=None, description="FTS5 search query"),
    include_archived: bool = Query(default=False, description="Include archived memories"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    user_id = user["id"]

    if q:
        try:
            results = await search_memories(user_id, q)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_search_query",
                    "message": f"Invalid search query: {e}",
                },
            )
    else:
        results = await list_memories(user_id, include_archived=include_archived)

    return {"memories": results}


@router.post("/memories", status_code=201)
async def create_user_memory(
    body: CreateMemoryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new memory for the authenticated user.

    Args:
        body: Request body with memory content.
        user: Authenticated user dictionary.

    Returns:
        The created memory dict.
    """
    user_id = user["id"]

    try:
        memory = await create_memory(user_id, body.content)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_memory",
                "message": str(e),
            },
        )

    logger.info(
        "[memory] Created memory %s for user_id %s (%d bytes)",
        memory["id"],
        user_id,
        len(body.content.encode("utf-8")),
    )

    return memory


@router.get("/memories/{memory_id}")
async def get_user_memory(
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get a specific memory by ID.

    Args:
        memory_id: UUID of the memory.
        user: Authenticated user dictionary.

    Returns:
        Memory dict.

    Raises:
        HTTPException: 404 if memory not found.
    """
    user_id = user["id"]
    memory = await get_memory(user_id, memory_id)

    if not memory:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "memory_not_found",
                "message": f"Memory {memory_id} not found.",
            },
        )

    return memory


@router.delete("/memories/{memory_id}")
async def delete_user_memory(
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a specific memory by ID.

    Args:
        memory_id: UUID of the memory.
        user: Authenticated user dictionary.

    Returns:
        Success confirmation.

    Raises:
        HTTPException: 404 if memory not found.
    """
    user_id = user["id"]
    deleted = await delete_memory(user_id, memory_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "memory_not_found",
                "message": f"Memory {memory_id} not found.",
            },
        )

    logger.info("[memory] Deleted memory %s for user_id %s", memory_id, user_id)
    return {"success": True}


@router.put("/memories/{memory_id}")
async def update_user_memory(
    memory_id: str,
    body: UpdateMemoryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    user_id = user["id"]

    try:
        memory = await update_memory(user_id, memory_id, body.content)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_memory",
                "message": str(e),
            },
        )

    if not memory:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "memory_not_found",
                "message": f"Memory {memory_id} not found.",
            },
        )

    logger.info(
        "[memory] Updated memory %s for user_id %s (%d bytes)",
        memory_id,
        user_id,
        len(body.content.encode("utf-8")),
    )
    return memory


@router.put("/memories/{memory_id}/archive")
async def archive_user_memory(
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    user_id = user["id"]
    memory = await archive_memory(user_id, memory_id)

    if not memory:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "memory_not_found",
                "message": f"Memory {memory_id} not found.",
            },
        )

    logger.info("[memory] Archived memory %s for user_id %s", memory_id, user_id)
    return memory


@router.put("/memories/{memory_id}/unarchive")
async def unarchive_user_memory(
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    user_id = user["id"]
    memory = await unarchive_memory(user_id, memory_id)

    if not memory:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "memory_not_found",
                "message": f"Memory {memory_id} not found.",
            },
        )

    logger.info("[memory] Unarchived memory %s for user_id %s", memory_id, user_id)
    return memory


# ---------------------------------------------------------------------------
# Memory suggestions (model-proposed, reviewed from the composer panel --
# see chat/memory_suggestions.py)
# ---------------------------------------------------------------------------

class AcceptSuggestionRequest(BaseModel):
    """Optional edited text to save instead of the proposed content."""
    content: Optional[str] = None


def _suggestion_not_found(suggestion_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "suggestion_not_found",
            "message": f"Memory suggestion {suggestion_id} not found.",
        },
    )


def _suggestion_resolved(exc: SuggestionAlreadyResolvedError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "suggestion_resolved",
            "message": f"This suggestion was already {exc.status}.",
            "status": exc.status,
        },
    )


@router.get("/conversations/{conversation_id}/memory-suggestions")
async def list_conversation_memory_suggestions(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Pending suggestions of a conversation the user owns, oldest first."""
    user_id = user["id"]
    await require_owned_conversation(user_id, conversation_id)
    suggestions = await list_pending(user_id, conversation_id)
    return {"suggestions": suggestions, "pending_count": len(suggestions)}


@router.get("/memory-inbox/counts")
async def memory_inbox_counts(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The user's pending suggestions across all conversations, per kind
    (``user`` personal, ``project`` project memory, ``doc`` doc notes) and
    in total (the sidebar inbox badge adds ``pending_count`` to the open
    action requests). Doc rows count while the doc-notes gate is closed."""
    return await memory_inbox.count_pending(user["id"])


@router.get("/memory-inbox/pending")
async def memory_inbox_pending(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Every pending suggestion of the three kinds (personal, project and
    doc notes) of the user, oldest first, each tagged ``kind`` (``user`` /
    ``project`` / ``doc``) and carrying its conversation's title and project
    (the inbox Memories tab's open list). A ``doc`` row also carries
    ``note_kind``, ``doc_title`` and the review fields
    (chat/memory_inbox.py); it is accepted or dismissed through
    ``/doc-note-suggestions/{id}/accept`` and ``/dismiss``, the other two
    kinds through their own suggestion routes."""
    suggestions = await memory_inbox.list_pending(user["id"])
    return {"suggestions": suggestions, "pending_count": len(suggestions)}


@router.get("/memory-inbox/resolved")
async def memory_inbox_resolved(
    cursor: Optional[str] = Query(default=None, description="Keyset cursor of the next page"),
    limit: int = Query(
        default=memory_inbox.DEFAULT_RESOLVED_PAGE_SIZE,
        ge=1, le=memory_inbox.MAX_RESOLVED_PAGE_SIZE,
    ),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One page of the user's accepted / dismissed suggestions of the three
    kinds (personal, project and doc notes), most recently resolved first;
    ``next_cursor`` is null at the end."""
    try:
        suggestions, next_cursor = await memory_inbox.list_resolved(
            user["id"], limit, cursor,
        )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_cursor", "message": "Malformed cursor."},
        )
    return {"suggestions": suggestions, "next_cursor": next_cursor}


@router.post("/memory-suggestions/{suggestion_id}/accept")
async def accept_memory_suggestion(
    suggestion_id: str,
    body: AcceptSuggestionRequest | None = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Accept a pending suggestion: creates the memory (optionally with
    edited text) and archives the memory it replaces, if any."""
    user_id = user["id"]
    content = body.content if body is not None else None
    try:
        result = await accept_suggestion(user_id, suggestion_id, content)
    except SuggestionAlreadyResolvedError as exc:
        raise _suggestion_resolved(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_memory", "message": str(exc)},
        )
    if result is None:
        raise _suggestion_not_found(suggestion_id)
    suggestion, memory = result
    logger.info(
        "[memory] Accepted suggestion %s -> memory %s for user_id %s",
        suggestion_id, memory["id"], user_id,
    )
    return {"suggestion": suggestion, "memory": memory}


@router.post("/memory-suggestions/{suggestion_id}/dismiss")
async def dismiss_memory_suggestion(
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
    logger.info(
        "[memory] Dismissed suggestion %s for user_id %s", suggestion_id, user_id,
    )
    return {"suggestion": suggestion}
