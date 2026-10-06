"""REST API endpoints for action requests."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.action_request_types import get_handler, get_preview_for_request
from chat.realtime import bus, events as realtime_events
from chat.storage import ChatStorage
from chat.wait_handles import resume as wait_resume
from db import conversation_store, tool_wait_handle_store
from db.action_request_store import (
    count_action_requests,
    count_action_requests_by_status,
    get_action_request,
    list_action_requests,
    list_action_requests_enriched,
    resolve_action_request,
)
from db.models import ActionRequestStatus, ToolWaitHandleStatus

logger = logging.getLogger(__name__)

# Server-side cap on deny-feedback length. The model only needs a short
# explanation; truncate (with a debug log) rather than 400 so the deny
# itself never fails on a long rant.
_MAX_FEEDBACK_LEN = 4000

router = APIRouter(
    prefix="/app/api",
    tags=["action-requests"],
)


async def _enrich_with_preview(request_dict: dict, user: dict | None = None) -> dict:
    """Add the handler-derived display fields to a request dict."""
    preview_data = await get_preview_for_request(
        request_dict.get("request_type", ""),
        request_dict.get("params", {}),
        user,
    )
    request_dict["preview_fields"] = preview_data["preview_fields"]
    request_dict["display_name"] = preview_data["display_name"]
    request_dict["approve_label"] = preview_data["approve_label"]
    request_dict["resolved_label"] = preview_data["resolved_label"]
    request_dict["summary_snippet"] = preview_data["summary_snippet"]
    return request_dict


class ResolveRequestBody(BaseModel):
    """Request body for executing, revising, or stopping an action request.

    * ``"execute"`` -- Approve: run the handler and resume the model with
      the result.
    * ``"deny"`` -- Revise: an optional ``feedback`` string is persisted on
      both the action_request row (in ``result.feedback``) and the linked
      wait-handle (in ``response.feedback``), and the model resumes right
      away so it can act on the reason. A bare deny (no feedback) is the
      legacy one-click Deny and still resumes the model immediately; the
      web UI no longer sends it except for ``subagent_return`` cards.
    * ``"stop"`` -- Stop: discard the request AND halt the conversation
      loop. Nothing resumes; the dangling ``create_action_request``
      tool_use is closed with a ``stopped`` verdict only when the user's
      next message arrives (see :func:`_stop_action_request`).

    ``feedback`` is silently ignored on ``execute`` and ``stop``.
    """
    action: str  # "execute", "deny", or "stop"
    feedback: Optional[str] = None

    @field_validator("feedback")
    @classmethod
    def _normalize_feedback(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if len(stripped) > _MAX_FEEDBACK_LEN:
            logger.debug(
                "[action_request] Truncating deny feedback from %d to %d chars",
                len(stripped), _MAX_FEEDBACK_LEN,
            )
            stripped = stripped[:_MAX_FEEDBACK_LEN]
        return stripped


@router.get("/action-requests")
async def list_user_action_requests(
    status: Optional[str] = Query(default=None, description="Filter by status: open, denied, executed"),
    conversation_id: Optional[str] = Query(default=None, description="Filter by conversation ID"),
    include_context: Optional[bool] = Query(default=None, description="Include conversation/routine/project context"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List action requests for the authenticated user."""
    user_id = user["id"]
    if include_context:
        requests = await list_action_requests_enriched(user_id, status=status)
        enriched = await asyncio.gather(*[_enrich_with_preview(r, user) for r in requests])
        return {"action_requests": list(enriched)}
    requests = await list_action_requests(user_id, status=status, conversation_id=conversation_id)
    enriched = await asyncio.gather(*[_enrich_with_preview(r, user) for r in requests])
    return {"action_requests": list(enriched)}


@router.get("/action-requests/count")
async def count_user_action_requests(
    status: Optional[str] = Query(default=None, description="Filter by status: open, denied, executed"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return the count of action requests for the authenticated user."""
    user_id = user["id"]
    count = await count_action_requests(user_id, status=status)
    return {"count": count}


@router.get("/action-requests/counts")
async def count_user_action_requests_all(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return counts for all action request statuses in a single response."""
    user_id = user["id"]
    counts = await count_action_requests_by_status(user_id)
    return {"counts": counts}


@router.get("/action-requests/{request_id}")
async def get_user_action_request(
    request_id: int,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get a specific action request by ID."""
    user_id = user["id"]
    req = await get_action_request(user_id, request_id)
    if not req:
        raise HTTPException(status_code=404, detail={
            "error": "action_request_not_found",
            "message": f"Action request {request_id} not found.",
        })
    return await _enrich_with_preview(req, user)


async def _resolve_linked_wait_handle(
    request: Request,
    user: dict,
    request_id: int,
    new_status: str,
    response: dict,
    kick_resume: bool = True,
) -> None:
    """Best-effort: resolve the action_request's linked wait handle (if any)
    and kick a headless resume so a suspended ``wait_for_handles`` wakes up.

    Mirrors ``chat/memory_routes.py`` -- a missing or already-resolved handle
    is not an error (in-flight requests created before this feature shipped
    have no linked handle, and racing approvals across browser tabs will
    short-circuit on the second resolve).
    """
    user_id = user["id"]
    try:
        handle = await tool_wait_handle_store.find_pending_for_action_request(
            user_id, request_id,
        )
        if handle is None:
            return
        updated = await tool_wait_handle_store.resolve_handle(
            handle["id"],
            new_status=new_status,
            response=response,
            correlation_kind="action_request",
            correlation_id=str(request_id),
            user_id=user_id,
        )
        if updated is not None:
            try:
                bus.publish_to_user(
                    user_id,
                    realtime_events.make_wait_handle_resolved(
                        conversation_id=updated["conversation_id"],
                        handle_id=updated["id"],
                        kind=updated.get("kind") or "action_request",
                        status=updated["status"],
                        request_id=request_id,
                        response=response,
                    ),
                )
            except Exception:
                logger.debug(
                    "[action_request] publish wait_handle_resolved failed",
                    exc_info=True,
                )
            if kick_resume:
                try:
                    wait_resume.maybe_kick_resume(
                        request.app, user, updated["conversation_id"],
                    )
                except Exception:
                    logger.exception(
                        "[action_request] Failed to kick resume task for "
                        "conversation %s", updated["conversation_id"],
                    )
    except Exception:
        logger.warning(
            "[action_request] Failed to resolve wait handle for action "
            "request %d", request_id, exc_info=True,
        )


async def _publish_request_count_changed(user_id: int) -> None:
    """Best-effort: refresh the per-status counts and publish to the user's
    persistent-WS channel so badges update without a 30s poll."""
    try:
        counts = await count_action_requests_by_status(user_id)
        bus.publish_to_user(
            user_id,
            realtime_events.make_request_count_changed(counts),
        )
    except Exception:
        logger.debug(
            "[action_request] publish request_count_changed failed "
            "(user_id=%s)", user_id, exc_info=True,
        )


def _publish_routine_list_changed(user_id: int, project_id: str) -> None:
    """Best-effort: tell the user's tabs a project's routine list changed so
    the sidebar refreshes its cached routines without a page reload."""
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_routine_list_changed(project_id),
        )
    except Exception:
        logger.debug(
            "[action_request] publish routine_list_changed failed "
            "(user_id=%s, project_id=%s)", user_id, project_id, exc_info=True,
        )


async def _stop_action_request(
    request: Request, user: dict, request_id: int, conversation_id: str,
) -> Optional[dict]:
    """Mark one open action request ``stopped`` and close its wait handle
    WITHOUT waking the conversation.

    The handle flips to ``stopped`` (terminal, so the composer unlocks and
    the Requests inbox drops the row) with a ``{"verdict": "stopped"}``
    response, but no headless resume is kicked: ``_run_resume`` holds on
    stopped rows, and the dangling ``create_action_request`` tool_use is
    closed by the resume bucket only when the user's next message arrives,
    with that message appended in the same turn. Returns the updated
    action_request dict, or None when the row was no longer open (a racing
    click on another tab won).
    """
    user_id = user["id"]
    ar_result: dict = {"stopped": True}
    updated = await resolve_action_request(
        user_id, request_id, ActionRequestStatus.STOPPED, result=ar_result,
    )
    if updated is None:
        return None
    logger.info(
        "[action_request] Stopped request %d for user_id %s "
        "(conversation=%s)", request_id, user_id, conversation_id,
    )
    await _resolve_linked_wait_handle(
        request,
        user,
        request_id,
        ToolWaitHandleStatus.STOPPED,
        {
            "verdict": "stopped",
            "request_id": request_id,
            "result": ar_result,
        },
        kick_resume=False,
    )
    try:
        ChatStorage.update_action_request_message(
            conversation_id=conversation_id,
            request_id=request_id,
            status=ActionRequestStatus.STOPPED,
            result=ar_result,
        )
    except Exception:
        logger.warning(
            "[action_request] Failed to update on-disk chat history "
            "for stopped request %d", request_id, exc_info=True,
        )
    return updated


async def _stop_sibling_action_requests(
    request: Request, user: dict, conversation_id: str, request_id: int,
) -> None:
    """Stop every OTHER open card of the same conversation.

    A parallel ``create_action_request`` batch opens several cards at
    once. Stop means "halt the loop", so the sibling cards cannot stay
    open: the composer would remain locked on their pending handles and
    approving one later would resume the model as if nothing had been
    stopped. Best-effort -- a failure here leaves the siblings open,
    which is the pre-Stop behaviour.
    """
    user_id = user["id"]
    try:
        pending = await tool_wait_handle_store.list_pending_for_conversation(
            user_id, conversation_id,
        )
    except Exception:
        logger.warning(
            "[action_request] Failed to list sibling handles for "
            "conversation %s", conversation_id, exc_info=True,
        )
        return
    for row in pending:
        if row.get("kind") != "action_request":
            continue
        if row.get("correlation_kind") != "action_request":
            continue
        try:
            sibling_id = int(row.get("correlation_id") or "")
        except (TypeError, ValueError):
            continue
        if sibling_id == request_id:
            continue
        sibling = await get_action_request(user_id, sibling_id)
        if not sibling or sibling["status"] != ActionRequestStatus.OPEN:
            continue
        if sibling.get("conversation_id") != conversation_id:
            continue
        await _stop_action_request(
            request, user, sibling_id, conversation_id,
        )


# Per-conversation resolution locks. Resolving a card is a read-check-act
# sequence whose side effects (the handler's external write on Approve, the
# wait-handle flip + resume kick on every outcome) happen BEFORE or outside
# the row's status transition, so two racing resolutions of the same
# conversation's cards (double-click, two tabs, Approve vs Revise, Stop
# stopping a sibling that is mid-execution) must be serialized here. The
# store's conditional UPDATE is the second line of defence. Keyed by
# conversation rather than request id so sibling Stop never nests locks.
# Process-local: the server runs a single uvicorn worker.
_resolution_locks: dict[str, asyncio.Lock] = {}
_resolution_lock_waiters: dict[str, int] = {}


@asynccontextmanager
async def _conversation_resolution_lock(conversation_id: str) -> AsyncIterator[None]:
    lock = _resolution_locks.get(conversation_id)
    if lock is None:
        lock = _resolution_locks[conversation_id] = asyncio.Lock()
    _resolution_lock_waiters[conversation_id] = (
        _resolution_lock_waiters.get(conversation_id, 0) + 1
    )
    try:
        async with lock:
            yield
    finally:
        remaining = _resolution_lock_waiters[conversation_id] - 1
        if remaining:
            _resolution_lock_waiters[conversation_id] = remaining
        else:
            del _resolution_lock_waiters[conversation_id]
            del _resolution_locks[conversation_id]


@router.post("/action-requests/{request_id}/resolve")
async def resolve_user_action_request(
    request_id: int,
    body: ResolveRequestBody,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Execute, revise (deny with feedback), or stop an action request."""
    user_id = user["id"]

    if body.action not in ("execute", "deny", "stop"):
        raise HTTPException(status_code=400, detail={
            "error": "invalid_action",
            "message": "Action must be 'execute', 'deny', or 'stop'.",
        })

    req = await get_action_request(user_id, request_id)
    if not req:
        raise HTTPException(status_code=404, detail={
            "error": "action_request_not_found",
            "message": f"Action request {request_id} not found.",
        })

    async with _conversation_resolution_lock(req["conversation_id"]):
        return await _resolve_locked(request_id, body, request, user)


def _already_resolved(request_id: int, status: Optional[str] = None) -> HTTPException:
    return HTTPException(status_code=400, detail={
        "error": "already_resolved",
        "message": (
            f"Action request {request_id} is already {status}."
            if status else
            f"Action request {request_id} was already resolved."
        ),
    })


async def _resolve_locked(
    request_id: int, body: ResolveRequestBody, request: Request, user: dict,
):
    """Body of the resolve route, run under the conversation's resolution
    lock. Re-reads the row so a resolution that queued behind another
    sees the winner's status instead of the stale pre-lock snapshot."""
    user_id = user["id"]

    req = await get_action_request(user_id, request_id)
    if not req:
        raise HTTPException(status_code=404, detail={
            "error": "action_request_not_found",
            "message": f"Action request {request_id} not found.",
        })

    if req["status"] != ActionRequestStatus.OPEN:
        raise _already_resolved(request_id, req["status"])

    action = body.action
    if action == "stop" and req["request_type"] == "subagent_return":
        # A subagent_return card lives in a read-only subagent conversation
        # the target user cannot type into, so "wait for the next message"
        # has no meaning there. Stop keeps the hard-deny semantics: the run
        # ends immediately and the caller learns the return was denied.
        action = "deny"

    if action == "stop":
        updated = await _stop_action_request(
            request, user, request_id, req["conversation_id"],
        )
        if updated is None:
            raise _already_resolved(request_id)
        await _stop_sibling_action_requests(
            request, user, req["conversation_id"], request_id,
        )
        await _publish_request_count_changed(user_id)
        return await _enrich_with_preview(updated, user)

    if action == "deny":
        # Build the persisted result. When the user supplied feedback,
        # mirror it inside ``result`` (so the action_request row is
        # self-describing for the Requests pane and any later read) AND at
        # the top level of the wait-handle ``response`` (so a model
        # blocking on wait_for_handles sees ``response.feedback``
        # alongside ``response.verdict`` without having to dig into
        # nested ``result``).
        ar_result: dict = {"denied": True}
        if body.feedback:
            ar_result["feedback"] = body.feedback

        updated = await resolve_action_request(
            user_id, request_id, ActionRequestStatus.DENIED, result=ar_result,
        )
        if updated is None:
            raise _already_resolved(request_id)
        logger.info(
            "[action_request] Denied request %d for user_id %s%s",
            request_id, user_id,
            " (with feedback)" if body.feedback else "",
        )

        wh_response: dict = {
            "verdict": "denied",
            "request_id": request_id,
            "result": ar_result,
        }
        if body.feedback:
            wh_response["feedback"] = body.feedback

        # subagent_return cards get bespoke deny semantics (see
        # docs/architecture/user-subagents.md): Revise (deny + feedback)
        # restarts the subagent loop under chat/user_subagent.py's
        # lifecycle control, and a hard Deny (no feedback) ends the run
        # immediately and tells the calling user. Either way the generic
        # resume kick is suppressed -- the run module decides whether the
        # subagent conversation continues.
        is_subagent_return = req["request_type"] == "subagent_return"

        await _resolve_linked_wait_handle(
            request,
            user,
            request_id,
            ToolWaitHandleStatus.REJECTED,
            wh_response,
            kick_resume=not is_subagent_return,
        )

        if is_subagent_return:
            try:
                from chat import user_subagent
                from db.user_subagent_run_store import (
                    get_run_by_subagent_conversation,
                )
                run = await get_run_by_subagent_conversation(
                    req["conversation_id"],
                )
                if run is not None:
                    if body.feedback:
                        await user_subagent.on_return_revised(run)
                    else:
                        await user_subagent.finalize_denied_return(run)
            except Exception:
                logger.exception(
                    "[action_request] subagent_return deny handling "
                    "failed for request %d", request_id,
                )

        # Best-effort: keep the on-disk chat_history.json action_request
        # entry coherent with the new resolution so a reload / replay
        # shows the same status (and feedback line) the live UI shows.
        try:
            ChatStorage.update_action_request_message(
                conversation_id=updated["conversation_id"],
                request_id=request_id,
                status=ActionRequestStatus.DENIED,
                result=ar_result,
                feedback=body.feedback,
            )
        except Exception:
            logger.warning(
                "[action_request] Failed to update on-disk chat history "
                "for denied request %d", request_id, exc_info=True,
            )

        await _publish_request_count_changed(user_id)
        return await _enrich_with_preview(updated, user)

    handler = get_handler(req["request_type"])
    if not handler:
        raise HTTPException(status_code=400, detail={
            "error": "unknown_request_type",
            "message": f"Unknown request type: {req['request_type']}.",
        })

    # Resolve the conversation's project_id so handlers with execute-time
    # project guards (create_skill / edit_skill) see the same project
    # context the proposal-time pre-card check ran under. Handlers that
    # resolve workspace dirs still derive membership from conversation_id.
    project_id: Optional[str] = None
    if req.get("conversation_id"):
        conv = await conversation_store.get_conversation_meta(
            user_id, req["conversation_id"]
        )
        if conv:
            project_id = conv.get("project_id")

    # write_doc records last_write_source = "action_request:<id>", so it is
    # the one handler that receives the request id. (Its doc_changed /
    # doc_list_changed events are published by the docs service itself.)
    execute_kwargs: dict = {}
    if req["request_type"] == "write_doc":
        execute_kwargs["request_id"] = request_id

    try:
        # Pass conversation_id so handlers that need workspace access
        # (e.g. attachment uploads) can resolve the conversation's
        # workspace dir.
        result = await handler.execute(
            req["params"], user, conversation_id=req["conversation_id"],
            project_id=project_id, **execute_kwargs,
        )
    except Exception as e:
        logger.exception("[action_request] Execution failed for request %d (user=%s)", request_id, user_id)
        # Keep request open so the user can retry. Leave the wait handle
        # pending too; on a successful retry both flip together.
        raise HTTPException(status_code=500, detail={
            "error": "execution_failed",
            "message": f"Failed to execute request: {e}",
        })

    updated = await resolve_action_request(user_id, request_id, ActionRequestStatus.EXECUTED, result=result)
    if updated is None:
        # Unreachable through this route (the conversation lock serializes
        # resolutions and the row was OPEN under it), so anything landing
        # here resolved the row behind our back while the handler's
        # external write was already in flight. Say so loudly.
        logger.error(
            "[action_request] Request %d (user=%s, type=%s) was resolved "
            "elsewhere while its handler executed; the handler's side "
            "effects stand but the row keeps the other outcome.",
            request_id, user_id, req["request_type"],
        )
        raise _already_resolved(request_id)
    logger.info("[action_request] Executed request %d for user_id %s (type=%s)", request_id, user_id, req["request_type"])
    if req["request_type"] in ("create_routine", "edit_routine") and project_id:
        _publish_routine_list_changed(user_id, project_id)
    # Best-effort: keep the on-disk chat_history.json action_request entry
    # coherent with the executed status (mirrors the deny branch).
    try:
        ChatStorage.update_action_request_message(
            conversation_id=updated["conversation_id"],
            request_id=request_id,
            status=ActionRequestStatus.EXECUTED,
            result=result if isinstance(result, dict) else None,
        )
    except Exception:
        logger.warning(
            "[action_request] Failed to update on-disk chat history "
            "for executed request %d", request_id, exc_info=True,
        )
    await _resolve_linked_wait_handle(
        request,
        user,
        request_id,
        ToolWaitHandleStatus.ACCEPTED,
        {
            "verdict": "executed",
            "request_id": request_id,
            "result": result,
        },
    )
    await _publish_request_count_changed(user_id)
    return await _enrich_with_preview(updated, user)
