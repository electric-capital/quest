"""REST endpoint resolving inline connect requests (chat/connect_requests.py).

The connect card calls ``POST /app/api/connect-requests/{handle_id}/resolve``
with ``{"action": "check"}`` after the OAuth popup (or the Settings add
flow) finishes, and with ``{"action": "decline"}`` for Not now. The
client never asserts that a service is connected: ``check`` re-reads the
user from the DB and inspects the same connector rows Settings renders,
and only a connected row resolves the ``service_connection`` wait handle.
A failed check leaves the handle pending so the card can offer a retry.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from chat import connect_requests
from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.realtime import bus, events as realtime_events
from chat.storage import ChatStorage
from chat.wait_handles import resume as wait_resume
from db import tool_wait_handle_store
from db.models import ToolWaitHandleStatus

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/app/api",
    tags=["connect-requests"],
)


class ResolveConnectRequest(BaseModel):
    """Request body: ``check`` verifies the connection and resolves on
    success; ``decline`` resolves as declined."""
    action: Literal["check", "decline"]


def _not_found(handle_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={
        "error": "connect_request_not_found",
        "message": f"Connect request {handle_id} not found.",
    })


@router.post("/connect-requests/{handle_id}/resolve")
async def resolve_connect_request(
    handle_id: str,
    body: ResolveConnectRequest,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Resolve a connect card: verify-and-accept, or decline."""
    user_id = user["id"]
    handle = await tool_wait_handle_store.get_handle_for_user(user_id, handle_id)
    if handle is None or handle.get("kind") != connect_requests.HANDLE_KIND:
        raise _not_found(handle_id)
    if handle["status"] != ToolWaitHandleStatus.PENDING:
        raise HTTPException(status_code=400, detail={
            "error": "already_resolved",
            "message": f"Connect request {handle_id} is already {handle['status']}.",
            "status": handle["status"],
            "response": handle.get("response"),
        })

    payload = handle.get("payload") or {}
    service = str(payload.get("service") or "")
    label = str(payload.get("label") or service)

    if body.action == "check":
        # The credential was written by another request (the OAuth
        # callback, the key route); the dependency's user dict may predate
        # it, so read the row again.
        from db.user_store import get_user_by_id
        fresh = await get_user_by_id(user_id) or user
        row = connect_requests.find_row(fresh, service)
        if row is None or not connect_requests.row_is_connected(row):
            return {
                "resolved": False,
                "status": "not_connected",
                "service": service,
            }
        response = {
            "status": "connected",
            "service": service,
            "label": label,
            "note": (
                f"The user connected {label}. Its tools and system skill "
                "are available now: load the skill and continue with the "
                "user's original request."
            ),
        }
        new_status = ToolWaitHandleStatus.ACCEPTED.value
    else:
        response = {
            "status": "declined",
            "service": service,
            "label": label,
            "note": (
                f"The user chose not to connect {label}. Continue without "
                "it and do not offer the connect card again in this "
                "conversation unless the user asks."
            ),
        }
        new_status = ToolWaitHandleStatus.REJECTED.value

    updated = await tool_wait_handle_store.resolve_handle(
        handle_id,
        new_status=new_status,
        response=response,
        user_id=user_id,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail={
            "error": "race_lost",
            "message": (
                f"Connect request {handle_id} was resolved by another "
                "caller concurrently."
            ),
        })
    logger.info(
        "[connect_request] Resolved %s as %s (service=%s, user=%s)",
        handle_id, response["status"], service, user_id,
    )

    conversation_id = updated["conversation_id"]
    if response["status"] == "connected":
        # The connect callbacks already drop the cached sessions; doing it
        # again here guarantees the resume rebuilds the prompt (connected
        # services, skills, tools) even for a connector that forgot.
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user_id)
    try:
        ChatStorage.update_connect_request_message(
            conversation_id=conversation_id,
            wait_handle_id=handle_id,
            status=response["status"],
        )
    except Exception:
        logger.warning(
            "[connect_request] card rewrite failed for %s", handle_id,
            exc_info=True,
        )
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_wait_handle_resolved(
                conversation_id=conversation_id,
                handle_id=updated["id"],
                kind=updated.get("kind") or "",
                status=updated["status"],
                response=response,
            ),
        )
    except Exception:
        logger.debug(
            "[connect_request] publish wait_handle_resolved failed",
            exc_info=True,
        )
    try:
        wait_resume.maybe_kick_resume(request.app, user, conversation_id)
    except Exception:
        logger.exception(
            "[connect_request] Failed to kick resume for conversation %s",
            conversation_id,
        )
    return {
        "resolved": True,
        "status": response["status"],
        "service": service,
        "handle": updated,
    }
