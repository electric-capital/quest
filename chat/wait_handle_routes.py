"""REST API endpoints for tool wait handles.

Used by the frontend to resolve handles created by tools that block on
user input. Resolutions persist to the DB and then unconditionally kick
a headless resume so any dangling tool_use left by a suspend sentinel
is closed on the next run. ``maybe_kick_resume`` is dedupe-safe and
gated by ``_conversation_has_dangling_wait``, so it is a no-op when
there is nothing to resume.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.wait_handles.publish import publish_wait_handle_resolved
from chat.wait_handles import resume as wait_resume
from db import tool_wait_handle_store
from db.models import ToolWaitHandleKind, ToolWaitHandleStatus

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/app/api",
    tags=["wait-handles"],
)


_RESOLVABLE_STATUSES = {
    ToolWaitHandleStatus.ACCEPTED.value,
    ToolWaitHandleStatus.REJECTED.value,
    ToolWaitHandleStatus.CANCELLED.value,
}


class ResolveWaitHandleRequest(BaseModel):
    """Request body for resolving a wait handle."""
    status: str
    response: Optional[dict] = None
    correlation_kind: Optional[str] = None
    correlation_id: Optional[str] = None
    feedback: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status_value(cls, v: str) -> str:
        if v not in _RESOLVABLE_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_RESOLVABLE_STATUSES)}"
            )
        return v


@router.get("/wait-handles/{handle_id}")
async def get_wait_handle(
    handle_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Fetch a wait handle by id, scoped to the authenticated user."""
    handle = await tool_wait_handle_store.get_handle_for_user(user["id"], handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail={
            "error": "wait_handle_not_found",
            "message": f"Wait handle {handle_id} not found.",
        })
    return handle


@router.post("/wait-handles/{handle_id}/resolve")
async def resolve_wait_handle(
    handle_id: str,
    body: ResolveWaitHandleRequest,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Resolve a wait handle and wake any suspended ``wait_for_handles``."""
    user_id = user["id"]
    existing = await tool_wait_handle_store.get_handle_for_user(user_id, handle_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={
            "error": "wait_handle_not_found",
            "message": f"Wait handle {handle_id} not found.",
        })
    if existing.get("kind") == ToolWaitHandleKind.AUTOPROJECT_QUESTION.value:
        # An AutoProject question has its own answer route, which validates
        # the answer, checks the gate and the run, and rewrites the
        # question's transcript message; resolving it here would skip all
        # of that.
        raise HTTPException(status_code=409, detail={
            "error": "use_autoproject_answer_route",
            "message": (
                "Answer an AutoProject question through "
                "POST /autoprojects/{id}/questions/{handle_id}/answer."
            ),
        })
    if existing["status"] != ToolWaitHandleStatus.PENDING:
        raise HTTPException(status_code=400, detail={
            "error": "already_resolved",
            "message": (
                f"Wait handle {handle_id} is already {existing['status']}."
            ),
        })

    response = body.response
    if body.feedback:
        response = dict(response) if response else {}
        response.setdefault("feedback", body.feedback)

    updated = await tool_wait_handle_store.resolve_handle(
        handle_id,
        new_status=body.status,
        response=response,
        correlation_kind=body.correlation_kind,
        correlation_id=body.correlation_id,
        user_id=user_id,
    )
    if updated is None:
        # Lost a race with another resolver -- treat as already resolved.
        raise HTTPException(status_code=409, detail={
            "error": "race_lost",
            "message": (
                f"Wait handle {handle_id} was resolved by another caller "
                "concurrently."
            ),
        })

    logger.info(
        "[wait_handle] Resolved handle %s as %s (user=%s)",
        handle_id, updated["status"], user_id,
    )
    publish_wait_handle_resolved(user_id, updated, response=response)
    # The model loop suspended via the wait sentinel and returned, so the
    # dangling wait_for_handles tool_use must be closed by a headless
    # resume reading the just-resolved DB row. maybe_kick_resume is
    # dedupe-safe via _active_resumes and gated by
    # _conversation_has_dangling_wait, so it no-ops if there's nothing
    # to drive.
    try:
        wait_resume.maybe_kick_resume(
            request.app, user, updated["conversation_id"],
        )
    except Exception:
        logger.exception(
            "[wait_handle] Failed to kick resume task for conversation %s",
            updated["conversation_id"],
        )
    return updated
