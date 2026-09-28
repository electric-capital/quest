"""Admin operation endpoints."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked, is_admin
from auth.config import COOKIE_NAME, COOKIE_SECURE, COOKIE_VERSION, is_password_login
from auth.session import get_cookie_serializer, get_user_from_cookie, set_session_cookie
from chat.storage import ChatStorage
from db.conversation_store import (
    get_conversations_with_users,
    list_conversation_activity_rows,
    list_latest_active_conversations,
)
from db.llm_call_store import (
    get_latest_context_tokens_for_conversations,
    get_most_expensive_conversations,
    get_usage_by_model,
    get_usage_by_model_for_conversations,
    get_usage_by_user,
)
from db.guide_store import list_all_guides
from db.project_store import list_all_project_guides
from db.routine_store import get_routine_labels
from db.user_store import get_user_by_id, list_all_users

from chat.routes import router

logger = logging.getLogger(__name__)


class ImpersonateRequest(BaseModel):
    user_id: int


def _require_admin(user: dict) -> None:
    """Raise 403 unless the requesting user is an admin."""
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )


@router.post("/admin/shutdown")
async def admin_shutdown(
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Trigger a graceful server shutdown.

    Only accessible to admin users (emails in server_config.json admin_emails).
    Sets the shutdown event which triggers SIGTERM after a brief delay to allow
    the HTTP response to be delivered.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    logger.info("[admin] Shutdown requested by %s", user["email"])
    request.app.state.shutdown_event.set()
    return {"status": "shutting_down", "message": "Server shutdown initiated"}


@router.get("/admin/users")
async def admin_list_users(
    include_self: bool = False,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List all users for admin pickers.

    The impersonation picker uses the default (requesting admin filtered
    out -- you cannot impersonate yourself); the feature-gate access picker
    passes ``include_self=true`` so admins can grant themselves access.
    Only accessible to admin users.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    users = await list_all_users()
    if not include_self:
        users = [u for u in users if u["id"] != user["id"]]
    return {"users": users}


@router.post("/admin/impersonate")
async def admin_impersonate(
    body: ImpersonateRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Start impersonating another user.

    Only accessible to admin users. Sets a new session cookie with both
    the target user's ID and the admin's ID.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    # Prevent nested impersonation
    if user.get("_impersonator_uid"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "already_impersonating",
                "message": "Already impersonating. Stop current impersonation first.",
            }
        )

    # Validate target user exists
    target_user = await get_user_by_id(body.user_id)
    if not target_user:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "user_not_found",
                "message": "Target user not found.",
            }
        )

    logger.info("[admin] %s started impersonating %s", user["email"], target_user["email"])

    # Create impersonation cookie
    signed_payload = get_cookie_serializer().dumps({
        "v": COOKIE_VERSION,
        "uid": target_user["id"],
        "imp": user["id"],
    })

    response = JSONResponse({
        "success": True,
        "impersonating": {
            "id": target_user["id"],
            "email": target_user["email"],
            "name": target_user.get("name", ""),
        },
    })
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed_payload,
        httponly=True,
        max_age=60 * 60,  # 1 hour (shorter than normal 30-day session)
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@router.get("/admin/system-monitor/latest-active-conversations")
async def admin_latest_active_conversations(
    limit: int = 20,
    include_routines: bool = True,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return the most recently active conversations across all users.

    "Active" is keyed off ``conversations.last_message_at``, which the
    storage layer bumps for user input, model output, and tool-call
    completion -- the three signals the operator dashboard cares about.
    ``include_routines=false`` filters out routine-created conversations
    (``routine_id`` set) server-side so the list stays full-length.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    # Cap at 100 so a crafted query string can't fan out into a giant scan.
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    rows = await list_latest_active_conversations(
        limit=limit, include_routines=include_routines
    )

    # One grouped query per raw provider table for the whole displayed batch
    # (avoids N+1): per-conversation, per-model native token sums plus a
    # coarse conversation total, and the latest top-level call's context size.
    conversation_ids = [row["id"] for row in rows]
    usage_by_conversation = await get_usage_by_model_for_conversations(
        conversation_ids
    )
    context_by_conversation = await get_latest_context_tokens_for_conversations(
        conversation_ids
    )

    def _build_rows() -> list[dict]:
        return [
            _admin_conversation_view(
                row["id"], row,
                usage_by_conversation.get(row["id"], _EMPTY_USAGE),
                context_by_conversation.get(row["id"]),
            )
            for row in rows
        ]

    # The per-row view reads each conversation's chat_history.json
    # (active-day count, legacy title fallback): a synchronous parse of
    # potentially large files that must not stall the event loop -- the
    # persistent WebSocket's heartbeats and every in-flight model run
    # share it, and a stall long enough to trip the socket watchdog drops
    # live streams. Run the file-bound loop in a worker thread.
    conversations = await asyncio.to_thread(_build_rows)
    return {"conversations": conversations}


_EMPTY_USAGE = {
    "models": [],
    "total": {
        "call_count": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "cost_source": None,
    },
}


def _admin_conversation_view(
    conversation_id: str,
    row: Optional[dict],
    usage: dict,
    latest_context_tokens: Optional[int],
    owner: Optional[dict] = None,
) -> dict:
    """Assemble one system-reports conversation row for the JSON response.

    Shared by the latest-active and most-expensive endpoints so both tables
    render the same shape. ``row`` is the conversation+user join row, or
    None when the conversation has been deleted (cost analytics outlives
    deletion) -- the title becomes a placeholder and identity comes from
    ``owner`` (a users-table row resolved from the call rows' user_id), when
    the caller could resolve one.
    """
    if row is not None:
        title = ChatStorage._resolve_list_title(conversation_id, row)
    else:
        title = "(deleted conversation)"
        if owner is not None:
            row = {
                "user_id": owner["id"],
                "user_email": owner["email"],
                "user_name": owner.get("name") or "",
            }
    row = row or {}
    return {
        "id": conversation_id,
        "title": title,
        "user_id": row.get("user_id"),
        "user_email": row.get("user_email", ""),
        "user_name": row.get("user_name", ""),
        "project_id": row.get("project_id"),
        "routine_id": row.get("routine_id"),
        "last_message_at": row.get("last_message_at"),
        "origin": row.get("origin"),
        "last_model": row.get("last_model"),
        "usage_by_model": usage["models"],
        "usage_total": usage["total"],
        "latest_context_tokens": latest_context_tokens,
        # Distinct UTC days with at least one user message, over the whole
        # conversation lifetime (not clipped to a query window).
        "active_days": ChatStorage.count_user_message_active_days(conversation_id),
    }


def _parse_range_date(value: Optional[str], param: str) -> Optional[date]:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_params",
                "message": f"{param} must be an ISO date (YYYY-MM-DD).",
            }
        )


def _resolve_range_window(
    start: Optional[str], end: Optional[str]
) -> tuple[Optional[date], Optional[date], Optional[datetime], Optional[datetime]]:
    """Parse the shared start/end query params of the ranged report endpoints.

    ``start``/``end`` are inclusive ISO dates interpreted in UTC (the
    timezone the ``llm_calls_*`` rows are stamped in); either may be omitted
    for an open-ended range. Returns the parsed dates plus the half-open
    ``[start_dt, end_dt)`` datetime window for the call-row queries (the
    inclusive end date becomes an exclusive bound at the next midnight).
    Raises 400 on malformed or reversed dates.
    """
    start_date = _parse_range_date(start, "start")
    end_date = _parse_range_date(end, "end")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_params",
                "message": "start must not be after end.",
            }
        )
    start_dt = (
        datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        if start_date else None
    )
    end_dt = (
        datetime.combine(end_date + timedelta(days=1), datetime.min.time(),
                         tzinfo=timezone.utc)
        if end_date else None
    )
    return start_date, end_date, start_dt, end_dt


@router.get("/admin/system-monitor/most-expensive-conversations")
async def admin_most_expensive_conversations(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 30,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return the conversations with the highest estimated cost in a date range.

    ``start``/``end`` are inclusive ISO dates interpreted in UTC (the
    timezone the ``llm_calls_*`` rows are stamped in); either may be
    omitted for an open-ended range. Ranking sums each conversation's
    priced per-model estimates over calls inside the range
    (db/llm_call_store.py get_most_expensive_conversations); conversations
    deleted since are still listed, attributed via the call rows' user_id.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    _start_date, _end_date, start_dt, end_dt = _resolve_range_window(start, end)

    # Cap at 100, same guard as the latest-active endpoint.
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    ranked = await get_most_expensive_conversations(
        start=start_dt, end=end_dt, limit=limit
    )
    conversation_ids = [entry["conversation_id"] for entry in ranked]
    rows_by_id = await get_conversations_with_users(conversation_ids)
    context_by_conversation = await get_latest_context_tokens_for_conversations(
        conversation_ids
    )

    # Owner fallback for deleted conversations: the call rows still carry
    # user_id, so spend stays attributed. One lookup per distinct missing
    # owner (rare).
    fallback_users: dict[int, Optional[dict]] = {}
    owner_by_conversation: dict[str, Optional[dict]] = {}
    for entry in ranked:
        conv_id = entry["conversation_id"]
        owner = None
        if rows_by_id.get(conv_id) is None and entry["user_id"] is not None:
            owner_id = entry["user_id"]
            if owner_id not in fallback_users:
                fallback_users[owner_id] = await get_user_by_id(owner_id)
            owner = fallback_users[owner_id]
        owner_by_conversation[conv_id] = owner

    def _build_rows() -> list[dict]:
        return [
            _admin_conversation_view(
                entry["conversation_id"],
                rows_by_id.get(entry["conversation_id"]),
                {"models": entry["models"], "total": entry["total"]},
                context_by_conversation.get(entry["conversation_id"]),
                owner=owner_by_conversation.get(entry["conversation_id"]),
            )
            for entry in ranked
        ]

    # Same chat_history.json reads as the latest-active endpoint: keep
    # them off the event loop.
    conversations = await asyncio.to_thread(_build_rows)
    return {"conversations": conversations}


# Zero-activity placeholder so every user in the roster gets a full row.
_EMPTY_USER_USAGE = {
    "models": [],
    "total": {
        "call_count": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "cost_source": None,
    },
    "conversation_count": 0,
    "routine_conversation_count": 0,
    "cost_excluding_routines_usd": 0.0,
    "cost_excluding_routines_source": None,
    "cost_routines_usd": 0.0,
    "cost_routines_source": None,
    "known_cost_usd": 0.0,
    "routines": [],
}


def _routine_cost_view(routine_usage: dict, label: Optional[dict]) -> dict:
    """Project one per-routine cost entry of the user report.

    ``label`` is the routine's name/project lookup (``get_routine_labels``);
    None only if the routine vanished between the conversation scan and the
    label query (routine deletes SET NULL the conversations' ``routine_id``,
    so a missing label is a race, not a steady state) -- such a row keeps
    its spend under a placeholder name.
    """
    return {
        "routine_id": routine_usage["routine_id"],
        "routine_name": label["name"] if label else "(deleted routine)",
        "project_id": label["project_id"] if label else None,
        "project_name": label["project_name"] if label else None,
        "conversation_count": routine_usage["conversation_count"],
        "cost_usd": routine_usage["cost_usd"],
        "cost_source": routine_usage["cost_source"],
    }


@router.get("/admin/system-monitor/user-report")
async def admin_user_report(
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return per-user activity and cost aggregates for a date range.

    One row per user (the whole roster -- zero-activity users keep their
    row), sorted by known in-range cost. Token/cost aggregation windows on
    the ``llm_calls_*`` rows' ``created_at`` like the most-expensive
    endpoint; the routine/non-routine split comes from the surviving
    conversation rows' ``routine_id`` (calls of deleted conversations count
    as non-routine -- routine provenance dies with the row), and the routine
    half is additionally broken out per routine (``routine_costs``, labeled
    with the routine + project names, most expensive first) so the
    priciest routines stand out. ``active_days`` is the union of distinct
    UTC user-message days across the user's non-routine conversations,
    clipped to the range (read from chat_history.json, so deleted
    conversations contribute none).
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    start_date, end_date, start_dt, end_dt = _resolve_range_window(start, end)

    conv_rows = await list_conversation_activity_rows()
    routine_by_conv = {
        row["id"]: row["routine_id"] for row in conv_rows if row["routine_id"]
    }
    usage_by_user = await get_usage_by_user(
        start=start_dt, end=end_dt, routine_id_by_conversation=routine_by_conv
    )
    # One batched label lookup for every routine that appears in any
    # user's breakdown (routine name + owning project name).
    seen_routine_ids = {
        r["routine_id"]
        for usage in usage_by_user.values()
        for r in usage["routines"]
    }
    routine_labels = await get_routine_labels(seen_routine_ids)

    # Range-clipped active days, unioned per user across non-routine
    # conversations so a day spent in three chats counts once. The
    # created_at/last_message_at bounds prune chat-history reads that
    # cannot overlap the window.
    start_day = start_date.isoformat() if start_date else None
    end_day = end_date.isoformat() if end_date else None

    def _collect_active_days() -> dict[int, set]:
        by_user: dict[int, set] = {}
        for row in conv_rows:
            if row["routine_id"]:
                continue
            created, last = row["created_at"], row["last_message_at"]
            if end_day and created is not None and created.date().isoformat() > end_day:
                continue
            if start_day and last is not None and last.date().isoformat() < start_day:
                continue
            days = ChatStorage.user_message_active_days(row["id"], start_day, end_day)
            if days:
                by_user.setdefault(row["user_id"], set()).update(days)
        return by_user

    # This walks and parses chat_history.json for every non-routine
    # conversation in range -- seconds to tens of seconds on a busy
    # install. Done on the event loop it would freeze the persistent
    # WebSocket (heartbeats, live streams) for the duration, so it runs
    # in a worker thread.
    active_days_by_user = await asyncio.to_thread(_collect_active_days)

    users_by_id = {u["id"]: u for u in await list_all_users()}
    # Call rows outlive user deletion; keep such spend visible on a
    # placeholder row rather than dropping it.
    report_user_ids = set(users_by_id) | set(usage_by_user) | set(active_days_by_user)

    rows = []
    for user_id in report_user_ids:
        info = users_by_id.get(user_id)
        usage = usage_by_user.get(user_id, _EMPTY_USER_USAGE)
        rows.append({
            "user_id": user_id,
            "user_email": info["email"] if info else "",
            "user_name": (info.get("name") or "") if info else "(unknown user)",
            "active_days": len(active_days_by_user.get(user_id, ())),
            "conversation_count": usage["conversation_count"],
            "routine_conversation_count": usage["routine_conversation_count"],
            "cost_excluding_routines_usd": usage["cost_excluding_routines_usd"],
            "cost_excluding_routines_source": usage["cost_excluding_routines_source"],
            "cost_routines_usd": usage["cost_routines_usd"],
            "cost_routines_source": usage["cost_routines_source"],
            "routine_costs": [
                _routine_cost_view(r, routine_labels.get(r["routine_id"]))
                for r in usage["routines"]
            ],
            "usage_by_model": usage["models"],
            "usage_total": usage["total"],
            "_known_cost": usage["known_cost_usd"],
        })
    rows.sort(key=lambda r: (
        -r["_known_cost"],
        -r["usage_total"]["total_tokens"],
        -r["active_days"],
        r["user_email"],
    ))
    for row in rows:
        del row["_known_cost"]
    return {"users": rows}


def _model_user_view(user_usage: dict, info: Optional[dict]) -> dict:
    """Project one top-user entry of the models report.

    ``info`` is the users-table row (``list_all_users`` shape); None when the
    call rows reference a user that has since been deleted -- the spend
    stays visible under a placeholder identity, as in the user report.
    """
    return {
        "user_id": user_usage["user_id"],
        "user_email": info["email"] if info else "",
        "user_name": (info.get("name") or "") if info else "(unknown user)",
        "call_count": user_usage["call_count"],
        "cost_usd": user_usage["cost_usd"],
        "cost_source": user_usage["cost_source"],
    }


@router.get("/admin/system-monitor/model-report")
async def admin_model_report(
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return per-model usage, cost and audience aggregates for a date range.

    One row per model id with at least one recorded call in the range,
    sorted by known in-range cost. Token/cost aggregation windows on the
    ``llm_calls_*`` rows' ``created_at`` like the other ranged reports; the
    routine share comes from the surviving conversation rows' ``routine_id``
    (calls of deleted conversations count as non-routine) and the sub-agent
    share from the call rows' own ``call_type``. ``top_users`` lists the ten
    most expensive users of each model.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    _start_date, _end_date, start_dt, end_dt = _resolve_range_window(start, end)

    conv_rows = await list_conversation_activity_rows()
    routine_by_conv = {
        row["id"]: row["routine_id"] for row in conv_rows if row["routine_id"]
    }
    usage_by_model = await get_usage_by_model(
        start=start_dt, end=end_dt, routine_id_by_conversation=routine_by_conv
    )
    users_by_id = {u["id"]: u for u in await list_all_users()}

    rows = [
        {
            "model": entry["model"],
            "provider": entry["provider"],
            "user_count": entry["user_count"],
            "conversation_count": entry["conversation_count"],
            "cost_routines_usd": entry["cost_routines_usd"],
            "cost_routines_source": entry["cost_routines_source"],
            "cost_subagents_usd": entry["cost_subagents_usd"],
            "cost_subagents_source": entry["cost_subagents_source"],
            "top_users": [
                _model_user_view(u, users_by_id.get(u["user_id"]))
                for u in entry["users"]
            ],
            # Same shapes as the conversation rows so the FE reuses the
            # per-model token cell: exactly one model entry per row.
            "usage_by_model": [entry["usage"]],
            "usage_total": {
                "call_count": entry["usage"]["call_count"],
                "total_tokens": entry["usage"]["total_tokens"],
                "estimated_cost_usd": entry["usage"]["estimated_cost_usd"],
                "cost_source": entry["usage"]["cost_source"],
            },
        }
        for entry in usage_by_model
    ]
    return {"models": rows}


@router.get("/admin/system-monitor/guides-report")
async def admin_guides_report(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return every guide in the system, user guides and project guides alike.

    Deprecation-tracking report: guides are being retired in favor of
    skills, and this lists who still has them. One row per user guide
    (``kind="user"``: the ``guides`` table, incl. the empty auto-created
    default guides so an admin can tell "has a default row" from "wrote
    instructions") and one row per project with non-empty project
    instructions (``kind="project"``: the ``projects.guide`` text field).
    Content is reported as ``content_length`` -- the admin needs owners and
    names, not the prompt text. ``routine_count`` (user guides only) is the
    number of routines still using the guide as an override, the last
    remaining code path that applies a guide to new conversations.
    Sorted by owner email, then user guides (default first) before project
    guides, then name.
    """
    if not is_admin(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "Admin access required.",
            }
        )

    rows = []
    for guide in await list_all_guides():
        rows.append({
            "kind": "user",
            "id": guide["id"],
            "name": guide["name"],
            "user_id": guide["user_id"],
            "user_email": guide["user_email"],
            "user_name": guide["user_name"],
            "is_default": guide["is_default"],
            "project_id": None,
            "public": None,
            "content_length": guide["content_length"],
            "routine_count": guide["routine_count"],
            "created_at": guide["created_at"],
            "updated_at": guide["updated_at"],
        })
    for project in await list_all_project_guides():
        rows.append({
            "kind": "project",
            "id": project["id"],
            "name": project["name"],
            "user_id": project["user_id"],
            "user_email": project["user_email"],
            "user_name": project["user_name"],
            "is_default": None,
            "project_id": project["id"],
            "public": project["public"],
            "content_length": project["content_length"],
            "routine_count": None,
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        })
    rows.sort(key=lambda r: (
        r["user_email"],
        0 if r["kind"] == "user" else 1,
        0 if r["is_default"] else 1,
        r["name"].lower(),
    ))
    return {"guides": rows}


# ---------------------------------------------------------------------------
# Service credentials (server-level upstream API credentials)
# ---------------------------------------------------------------------------

def _effective_service_credentials(service: str) -> tuple[dict | None, str | None]:
    """Return (credentials, source) with the per-service store preferred.

    source is "store", "legacy", or None when the service is unconfigured.
    """
    from config.service_credentials import (
        read_legacy_service_credentials,
        read_service_credentials,
    )

    stored = read_service_credentials(service)
    if stored:
        return stored, "store"
    legacy = read_legacy_service_credentials(service)
    if legacy:
        return legacy, "legacy"
    return None, None


def _service_credential_detail(service: str) -> dict:
    """Schema-driven admin payload for one service (core or plugin).

    ``fields`` is the CredentialField schema the frontend renders the card
    from; ``credentials`` is the masked form view (secrets only appear as
    ``<key>_set`` booleans). Raises 404 for services no spec describes.
    """
    from config.service_specs import fields_view, form_view, get_service_spec

    spec = get_service_spec(service)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_service",
                "message": f"Unknown service: {service}",
            },
        )
    config, source = _effective_service_credentials(service)
    return {
        "service": service,
        "label": spec.label,
        "configured": config is not None and bool(spec.is_configured(config)),
        "source": source,
        "fields": fields_view(spec),
        "credentials": form_view(spec, config),
    }


@router.get("/admin/service-credentials")
async def admin_list_service_credentials(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List every credential service (core + plugins) with schema and form view.

    The frontend renders one generic card per entry from this single
    response; there are no per-service endpoints or components.
    """
    _require_admin(user)
    from config.service_specs import all_service_specs

    return {
        "services": [
            _service_credential_detail(spec.service) for spec in all_service_specs()
        ]
    }


@router.get("/admin/service-credentials/{service}")
async def admin_get_service_credentials(
    service: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return one service's credential schema and form values (secrets masked)."""
    _require_admin(user)
    return _service_credential_detail(service)


@router.put("/admin/service-credentials/{service}")
async def admin_update_service_credentials(
    service: str,
    body: dict,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Generic schema-validated save for any credential service.

    The body is a flat ``{field key: value}`` object validated against the
    service's CredentialField schema (unknown keys rejected, ``required``/
    ``required_if`` enforced, empty secret keeps the stored one). Starts
    from the currently effective config (store first, then the legacy
    location) so unrecognized stored keys survive a save.
    """
    _require_admin(user)
    from config.service_credentials import write_service_credentials
    from config.service_specs import get_service_spec, resolve_update

    spec = get_service_spec(service)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_service",
                "message": f"Unknown service: {service}",
            },
        )
    existing, _source = _effective_service_credentials(service)
    try:
        stored = resolve_update(spec, body, existing)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    write_service_credentials(service, stored)
    logger.info("[admin] %s updated %s service credentials", user["email"], service)
    return _service_credential_detail(service)


# ---------------------------------------------------------------------------
# Feature gates (server-global on/off switches for optional features)
# ---------------------------------------------------------------------------


class FeatureGateUpdate(BaseModel):
    enabled: bool
    # Per-user access list (features in PER_USER_ACCESS_FEATURES only):
    # a list restricts the enabled feature to those user emails, an explicit
    # null opens it to all users, and omitting the field keeps the stored
    # list unchanged (so plain on/off toggles never wipe the selection).
    allowed_users: Optional[list[str]] = None


def _feature_availability(feature: str) -> tuple[bool, Optional[str]]:
    """Whether ``feature`` can be turned on here: ``(available, reason)``.

    Most gates are pure switches and are always available. A gate whose
    feature depends on server configuration (voice input needs a Gemini
    Vertex model) reports False with a human reason while that
    configuration is missing; the PUT endpoint then refuses to enable it
    and the Settings toggle shows the reason. Turning a gate OFF is always
    allowed.
    """
    from config.feature_gates import FEATURE_VOICE_INPUT

    if feature == FEATURE_VOICE_INPUT:
        from chat.transcription import transcription_availability

        return transcription_availability()
    return True, None


def _feature_gate_view(feature: str, gates: dict[str, dict]) -> dict:
    from config.feature_gates import FEATURE_LABELS, PER_USER_ACCESS_FEATURES

    labels = FEATURE_LABELS.get(feature, {})
    gate = gates.get(feature) or {"enabled": False, "allowed_users": None}
    available, unavailable_reason = _feature_availability(feature)
    return {
        "feature": feature,
        "label": labels.get("label", feature),
        "description": labels.get("description", ""),
        "enabled": gate["enabled"],
        # None = every user has access while the gate is on.
        "allowed_users": gate["allowed_users"],
        "supports_user_access": feature in PER_USER_ACCESS_FEATURES,
        # False = the server lacks something the feature needs; the gate
        # cannot be turned on until it is fixed (reason says what).
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


@router.get("/admin/feature-gates")
async def admin_list_feature_gates(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List all gateable features with their server-global on/off state."""
    _require_admin(user)
    from config.feature_gates import KNOWN_FEATURES, read_feature_gates

    gates = read_feature_gates()
    return {
        "features": [
            _feature_gate_view(feature, gates) for feature in KNOWN_FEATURES
        ]
    }


@router.put("/admin/feature-gates/{feature}")
async def admin_update_feature_gate(
    feature: str,
    body: FeatureGateUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update one feature gate (persisted in data/feature_gates.json).

    ``enabled`` flips the gate; ``allowed_users`` (only for features that
    support per-user access) restricts it to specific user emails, with an
    explicit null meaning all users and an omitted field keeping the stored
    list unchanged.
    """
    _require_admin(user)
    from config.feature_gates import (
        KNOWN_FEATURES,
        PER_USER_ACCESS_FEATURES,
        set_feature_allowed_users,
        set_feature_enabled,
    )

    if feature not in KNOWN_FEATURES:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_feature",
                "message": f"Unknown feature: {feature}",
            },
        )
    allowed_users_provided = "allowed_users" in body.model_fields_set
    if allowed_users_provided and body.allowed_users is not None:
        if feature not in PER_USER_ACCESS_FEATURES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_params",
                    "message": (
                        f"Feature '{feature}' does not support per-user access"
                    ),
                },
            )
        bad = [e for e in body.allowed_users if "@" not in str(e).strip()]
        if bad:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_params",
                    "message": f"Not an email address: {bad[0]!r}",
                },
            )
    if body.enabled:
        available, reason = _feature_availability(feature)
        if not available:
            raise HTTPException(
                status_code=400,
                detail={"error": "feature_unavailable", "message": reason},
            )
    gates = set_feature_enabled(feature, body.enabled)
    if allowed_users_provided:
        gates = set_feature_allowed_users(feature, body.allowed_users)
    logger.info(
        "[admin] %s turned feature gate '%s' %s (access: %s)",
        user["email"], feature, "on" if body.enabled else "off",
        "all users" if gates[feature]["allowed_users"] is None
        else f"{len(gates[feature]['allowed_users'])} user(s)",
    )
    return _feature_gate_view(feature, gates)


# ---------------------------------------------------------------------------
# Model selection (Settings > Model Selection)
# ---------------------------------------------------------------------------


def _model_selection_view() -> dict:
    """The Model Selection table: one row per enabled, non-deprecated model
    (Vertex registry order, then each instance's models) with its selection
    entry and whether it is currently offerable (credentials configured and
    no failing health verdict -- the same filter as ``available_models``).

    Rows are the models an admin can meaningfully curate; entries stored
    for models that are currently disabled or removed stay in the file
    untouched by reads but are dropped by the next full-replacement PUT.
    ``public_mode_enabled`` tells the UI whether to show the public-menu
    slot, the Private/Public columns and the public preview at all; the
    rows carry the STORED flags regardless so a save while the gate is
    off preserves them.
    """
    from chat.llm.config import get_available_models, get_configured_models, list_model_specs
    from config.model_selection import (
        MAX_DESCRIPTOR_LENGTH,
        MAX_TOP_LEVEL_SLOTS,
        public_mode_enabled,
        read_model_selection,
        selection_for,
    )

    selection = read_model_selection()
    configured = set(get_configured_models())
    available = set(get_available_models())
    rows = []
    for spec in list_model_specs():
        if spec.deprecated or not spec.enabled:
            continue
        if spec.id in available:
            unavailable_reason = None
        elif spec.id in configured:
            unavailable_reason = "failing"
        else:
            unavailable_reason = "not_configured"
        rows.append({
            "id": spec.id,
            "wire_id": spec.wire_id,
            "display_name": spec.display_name,
            "provider_label": spec.provider_label,
            "instance_id": spec.instance_id,
            "available": unavailable_reason is None,
            "unavailable_reason": unavailable_reason,
            **selection_for(spec.id, selection),
        })
    return {
        "max_slots": MAX_TOP_LEVEL_SLOTS,
        "max_descriptor_length": MAX_DESCRIPTOR_LENGTH,
        "public_mode_enabled": public_mode_enabled(),
        "models": rows,
    }


@router.get("/admin/model-selection")
async def admin_get_model_selection(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The per-model selection settings (private/public top-level slots,
    descriptor, private/public usage flags) for every enabled model."""
    _require_admin(user)
    return _model_selection_view()


class ModelSelectionEntry(BaseModel):
    id: str
    slot: Optional[int] = None
    public_slot: Optional[int] = None
    descriptor: str = ""
    allow_private: bool = True
    allow_public: bool = True


class ModelSelectionUpdate(BaseModel):
    # Full replacement: every listed model gets exactly these settings and
    # models not listed are reset to unset (no slot, allowed everywhere).
    models: list[ModelSelectionEntry]


@router.put("/admin/model-selection")
async def admin_update_model_selection(
    body: ModelSelectionUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Replace the whole model selection (persisted in data/model_selection.json).

    Rejects unknown model ids, slots outside ``1..max_slots``, descriptors
    over the length cap, and duplicate slots within a menu (400). A slot for
    a visibility the model is not allowed in is cleared, not rejected.
    Takes effect on the next GET /app/api/config fetch and the next
    conversation turn.
    """
    _require_admin(user)
    from chat.llm.config import resolve_model
    from config.model_selection import (
        MAX_DESCRIPTOR_LENGTH,
        MAX_TOP_LEVEL_SLOTS,
        save_model_selection,
    )

    def _bad(message: str) -> HTTPException:
        return HTTPException(
            status_code=400, detail={"error": "invalid_params", "message": message},
        )

    entries: dict[str, dict] = {}
    for entry in body.models:
        if entry.id in entries:
            raise _bad(f"Model listed twice: {entry.id}")
        if resolve_model(entry.id) is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown_model", "message": f"Unknown model: {entry.id}"},
            )
        for slot in (entry.slot, entry.public_slot):
            if slot is not None and not 1 <= slot <= MAX_TOP_LEVEL_SLOTS:
                raise _bad(f"Slot must be between 1 and {MAX_TOP_LEVEL_SLOTS}: {entry.id}")
        if len(entry.descriptor.strip()) > MAX_DESCRIPTOR_LENGTH:
            raise _bad(
                f"Descriptor longer than {MAX_DESCRIPTOR_LENGTH} characters: {entry.id}"
            )
        entries[entry.id] = {
            "slot": entry.slot,
            "public_slot": entry.public_slot,
            "descriptor": entry.descriptor,
            "allow_private": entry.allow_private,
            "allow_public": entry.allow_public,
        }
    try:
        stored = save_model_selection(entries)
    except ValueError as exc:
        raise _bad(str(exc))
    logger.info(
        "[admin] %s updated model selection: %d slotted, %d restricted",
        user["email"],
        sum(1 for e in stored.values() if e["slot"] is not None or e["public_slot"] is not None),
        sum(1 for e in stored.values() if not (e["allow_private"] and e["allow_public"])),
    )
    return _model_selection_view()


# ---------------------------------------------------------------------------
# Inference providers (Settings > Inference Providers)
# ---------------------------------------------------------------------------


def _model_view(spec, statuses: dict) -> dict:
    """One model row for the admin cards.

    ``id`` is the stored (qualified) id, ``wire_id`` the string sent to the
    API -- the UI shows ``wire_id`` as the primary label. ``status`` is the
    latest model-health verdict from the store (populated by the startup
    sweep and admin rechecks), or None when the model was never checked.
    """
    return {
        "id": spec.id,
        "wire_id": spec.wire_id,
        "display_name": spec.display_name,
        "family": spec.family,
        "enabled": spec.enabled,
        "status": statuses.get(spec.id),
    }


def _vertex_status() -> dict:
    """The Vertex card: detected environment + the fixed catalog with the
    admin's enabled/disabled state (deprecated models omitted)."""
    from chat.llm.config import list_model_specs
    from chat.llm.health import get_model_health_store
    from config.inference_providers import vertex_environment_status

    statuses = get_model_health_store().get_all()
    vertex = vertex_environment_status()
    return {
        "provider": "vertex",
        "label": "Google Vertex AI",
        "kind": "detected",
        "configured": vertex["configured"],
        "detail": vertex,
        "models": [
            _model_view(spec, statuses)
            for spec in list_model_specs()
            if spec.instance_id is None and not spec.deprecated
        ],
    }


def _instance_status(instance: dict) -> dict:
    """One provider-instance card (key masked)."""
    from chat.llm.config import list_model_specs
    from chat.llm.health import get_model_health_store
    from config.inference_providers import INSTANCE_KINDS, effective_api_key

    statuses = get_model_health_store().get_all()
    kind = INSTANCE_KINDS[instance["kind"]]
    api_key, source = effective_api_key(instance["id"])
    return {
        "id": instance["id"],
        "kind": instance["kind"],
        "kind_label": kind["label"],
        "label": instance["label"],
        "configured": api_key is not None,
        "source": source,
        "credentials": {"api_key_set": api_key is not None},
        "hint": kind["hint"],
        "models": [
            _model_view(spec, statuses)
            for spec in list_model_specs()
            if spec.instance_id == instance["id"]
        ],
    }


def _instance_or_404(instance_id: str) -> dict:
    from config.inference_providers import get_instance, is_valid_instance_id

    instance = get_instance(instance_id) if is_valid_instance_id(instance_id) else None
    if instance is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_instance",
                "message": f"Unknown inference provider instance: {instance_id}",
            },
        )
    return instance


@router.get("/admin/inference-providers")
async def admin_list_inference_providers(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List inference providers: the detected Vertex environment with its
    fixed model catalog, plus every configured provider instance.

    The Vertex entry's credentials are display-only (detected from the
    environment and server_config.json); its per-model enabled flags are
    editable via PUT /admin/inference-providers/vertex. Instance entries
    never include the key itself.
    """
    _require_admin(user)
    from config.inference_providers import INSTANCE_KINDS, list_instances

    return {
        "vertex": _vertex_status(),
        "instances": [_instance_status(inst) for inst in list_instances()],
        "kinds": [
            {"kind": kind, "label": spec["label"]}
            for kind, spec in INSTANCE_KINDS.items()
        ],
    }


class VertexModelsUpdate(BaseModel):
    # Full replacement of the disabled set (Vertex registry ids).
    disabled_models: list[str]


@router.put("/admin/inference-providers/vertex")
async def admin_update_vertex_models(
    body: VertexModelsUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Replace the set of Vertex models an admin has disabled.

    Disabled models vanish from the picker and are skipped by every health
    sweep; existing conversations keep running on them. Models that just
    became enabled are rechecked in the background so a stale failing
    verdict cannot keep them hidden.
    """
    _require_admin(user)
    from chat.llm.config import MODEL_REGISTRY
    from chat.llm.health import schedule_model_rechecks
    from config.inference_providers import (
        set_vertex_disabled_models,
        vertex_disabled_models,
    )

    unknown = sorted(set(body.disabled_models) - set(MODEL_REGISTRY))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_model",
                "message": f"Not Vertex registry models: {unknown}",
            },
        )
    previously_disabled = vertex_disabled_models()
    disabled = set(set_vertex_disabled_models(body.disabled_models))
    re_enabled = [m for m in MODEL_REGISTRY if m in previously_disabled and m not in disabled]
    if re_enabled:
        from chat.llm.config import get_configured_models

        configured = set(get_configured_models())
        schedule_model_rechecks([m for m in re_enabled if m in configured])
    logger.info(
        "[admin] %s set Vertex disabled models to %s", user["email"], sorted(disabled),
    )
    return _vertex_status()


class InstanceCreate(BaseModel):
    kind: str = "openrouter"
    label: str = ""


@router.post("/admin/inference-providers/instances")
async def admin_create_inference_instance(
    body: InstanceCreate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Add an empty provider instance (no key, no models yet).

    The id is server-generated (``openrouter``, ``openrouter-2``, ...); the
    admin then saves a key and picks models via the PUT endpoint.
    """
    _require_admin(user)
    from config.inference_providers import INSTANCE_KINDS, new_instance_id, upsert_instance

    if body.kind not in INSTANCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_kind",
                "message": f"Unknown inference provider kind: {body.kind}",
            },
        )
    instance = upsert_instance({
        "id": new_instance_id(body.kind),
        "kind": body.kind,
        "label": body.label,
        "models": [],
    })
    logger.info("[admin] %s created inference instance %s", user["email"], instance["id"])
    return _instance_status(instance)


class InstanceModelUpdate(BaseModel):
    id: str
    enabled: bool = True


class InstanceUpdate(BaseModel):
    # Every field is optional: omitted = keep. An empty api_key also keeps
    # the currently stored key so the masked read endpoint round-trips
    # without ever sending the key back out.
    label: Optional[str] = None
    api_key: Optional[str] = None
    # Full replacement of the model list, in display order.
    models: Optional[list[InstanceModelUpdate]] = None


@router.put("/admin/inference-providers/instances/{instance_id}")
async def admin_update_inference_instance(
    instance_id: str,
    body: InstanceUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update an instance's label, API key and/or model list.

    Models new to the instance get their metadata (name, context/output
    limits, pricing) snapshotted from the cached OpenRouter catalog;
    entries the catalog does not list are kept as custom ids with
    conservative defaults. A key change drops cached SDK clients so the new
    key is used on the next session, and every enabled model of the
    instance is rechecked in the background (a failing verdict recorded
    under the old key or before the model existed would otherwise keep it
    hidden from the picker).
    """
    _require_admin(user)
    from chat.llm.config import reset_provider_client_caches
    from chat.llm.health import get_model_health_store, schedule_model_rechecks
    from config.inference_providers import (
        effective_api_key,
        qualify_model_id,
        read_inference_credentials,
        upsert_instance,
        write_inference_credentials,
    )

    instance = _instance_or_404(instance_id)
    key_changed = False

    if body.label is not None:
        instance["label"] = body.label

    if body.api_key is not None:
        api_key = body.api_key.strip()
        if api_key:
            config = dict(read_inference_credentials(instance_id) or {})
            config["api_key"] = api_key
            write_inference_credentials(instance_id, config)
            key_changed = True
        elif not effective_api_key(instance_id)[0] and body.models is None and body.label is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_params",
                    "message": "api_key is required (no stored key to keep).",
                },
            )

    removed_ids: list[str] = []
    if body.models is not None:
        existing = {m["id"]: m for m in instance["models"]}
        wanted: list[dict] = []
        seen: set[str] = set()
        new_wire_ids = [
            m.id.strip() for m in body.models
            if m.id.strip() and m.id.strip() not in existing
        ]
        snapshots = await _catalog_snapshots(new_wire_ids)
        for item in body.models:
            wire_id = item.id.strip()
            if not wire_id or wire_id in seen:
                continue
            seen.add(wire_id)
            base = existing.get(wire_id) or {"id": wire_id, **(snapshots.get(wire_id) or {})}
            wanted.append({**base, "enabled": item.enabled})
        removed_ids = [
            qualify_model_id(instance_id, wire_id)
            for wire_id in existing if wire_id not in seen
        ]
        instance["models"] = wanted

    instance = upsert_instance(instance)

    if removed_ids:
        await get_model_health_store().forget(removed_ids)
    if key_changed:
        reset_provider_client_caches()
    if key_changed or body.models is not None:
        from chat.llm.config import get_configured_models

        prefix = f"{instance_id}:"
        schedule_model_rechecks([
            m for m in get_configured_models() if m.startswith(prefix)
        ])

    logger.info("[admin] %s updated inference instance %s", user["email"], instance_id)
    return _instance_status(instance)


async def _catalog_snapshots(wire_ids: list[str]) -> dict[str, dict]:
    """Catalog metadata for ``wire_ids`` (empty dict per unlisted id).

    Uses the cached catalog (no refresh) so a save never blocks on the
    network beyond the first fetch; an unreachable catalog just means
    custom-id defaults.
    """
    if not wire_ids:
        return {}
    import asyncio

    from chat.llm.openrouter_catalog import catalog_snapshot, get_catalog

    catalog = await asyncio.to_thread(get_catalog)
    return {
        wire_id: catalog_snapshot(catalog["models"], wire_id) or {}
        for wire_id in wire_ids
    }


@router.delete("/admin/inference-providers/instances/{instance_id}")
async def admin_delete_inference_instance(
    instance_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Remove an instance: its config entry, its credential file, its
    cached provider object and its models' health verdicts.

    Conversations and routines that reference the instance's models keep
    their ids; a new turn on one fails at send time with a clear
    "not configured" error, the same as after a revoked key.
    """
    _require_admin(user)
    from chat.llm.config import drop_provider_instance
    from chat.llm.health import get_model_health_store
    from config.inference_providers import (
        INSTANCE_KINDS,
        delete_instance,
        qualify_model_id,
    )

    instance = _instance_or_404(instance_id)
    delete_instance(instance_id)
    drop_provider_instance(INSTANCE_KINDS[instance["kind"]]["provider"], instance_id)
    await get_model_health_store().forget([
        qualify_model_id(instance_id, m["id"]) for m in instance["models"]
    ])
    logger.info("[admin] %s deleted inference instance %s", user["email"], instance_id)
    return {"success": True}


@router.get("/admin/inference-providers/openrouter/catalog")
async def admin_openrouter_catalog(
    q: str = "",
    limit: int = 20,
    refresh: bool = False,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Typeahead candidates from the cached OpenRouter model catalog.

    Substring match on wire id and name (id-prefix hits first), at most
    ``limit`` rows. ``refresh=true`` forces a re-fetch. When the catalog
    cannot be fetched and nothing is cached, ``models`` is empty and
    ``error`` explains why -- the UI then offers custom-id entry only.
    """
    _require_admin(user)
    import asyncio

    from chat.llm.openrouter_catalog import get_catalog, search_catalog

    catalog = await asyncio.to_thread(get_catalog, refresh)
    limit = max(1, min(limit, 100))
    return {
        "models": search_catalog(catalog["models"], q, limit),
        "fetched_at": catalog["fetched_at"],
        "stale": catalog["stale"],
        "error": catalog["error"],
    }


class InferenceModelTestRequest(BaseModel):
    model: str


@router.post("/admin/inference-providers/test-model")
async def admin_test_inference_model(
    body: InferenceModelTestRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Re-run the live health check for one model and update the store.

    Backs the per-model recheck in Settings > Inference Providers: a model
    can look configured (credentials present, project set) yet still fail at
    send time -- most commonly a Claude model never enabled in Vertex Model
    Garden, or exhausted quota. The verdict is recorded in the server-global
    model-health store (the same one the startup sweep fills) and returned as
    ``{model, ok, error, checked_at}``; failures come back as data with the
    provider's explanation extracted, not as an HTTP error. Disabled models
    are never checked (400).
    """
    _require_admin(user)
    from chat.llm.config import resolve_model
    from chat.llm.health import get_model_health_store

    spec = resolve_model(body.model)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_model",
                "message": f"Unknown model: {body.model}",
            }
        )
    if not spec.enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "model_disabled",
                "message": f"Model {body.model} is disabled; enable it to check it.",
            }
        )
    result = await get_model_health_store().run_check(spec.id)
    logger.info(
        "[admin] %s health-checked model %s: %s",
        user["email"], spec.id, "ok" if result["ok"] else result["error"],
    )
    return result


@router.post("/admin/stop-impersonation")
async def admin_stop_impersonation(
    request: Request,
):
    """Stop impersonating and return to the admin's own session.

    Reads the current cookie to extract the impersonator's user ID,
    then sets a new cookie with just the admin's identity.
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "not_authenticated",
                "message": "No session cookie found.",
            }
        )

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "not_authenticated",
                "message": "Invalid session.",
            }
        )

    impersonator_uid = user.get("_impersonator_uid")
    if not impersonator_uid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_impersonating",
                "message": "Not currently impersonating.",
            }
        )

    logger.info(
        "[admin] %s stopped impersonating %s",
        user.get("_impersonator_email", "unknown"),
        user["email"],
    )

    # Create a normal (non-impersonation) cookie for the admin. Under
    # password sign-in it is bound to the admin's password again, exactly
    # like the cookie they signed in with.
    password_fp = None
    if is_password_login():
        admin_user = await get_user_by_id(impersonator_uid)
        password_fp = (admin_user or {}).get("password_fp")
    return set_session_cookie(JSONResponse({"success": True}), impersonator_uid, password_fp)


# ---------------------------------------------------------------------------
# Sign-in method + email/password account management
# ---------------------------------------------------------------------------

def _google_login_configured() -> bool:
    from auth.config import load_client_config
    try:
        config = load_client_config()
    except HTTPException:
        return False
    return bool(config.get("client_id") and config.get("client_secret"))


@router.get("/admin/sign-in")
async def admin_get_sign_in(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Sign-in method status for the admin Settings > Sign-in section."""
    _require_admin(user)
    from auth.config import login_method
    from auth.mailer import smtp_configured
    return {
        "login_method": login_method(),
        "google_oauth_configured": _google_login_configured(),
        "smtp_configured": smtp_configured(),
    }


class LoginMethodUpdate(BaseModel):
    login_method: str


@router.put("/admin/sign-in/login-method")
async def admin_set_login_method(
    body: LoginMethodUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Switch the deployment from password sign-in to Google sign-in.

    One-way from the UI: accounts are keyed by email, so every user keeps
    their account and signs in with the Google account of the same address.
    Password-issued sessions end immediately (see get_user_from_cookie).
    Requires Google OAuth client credentials so the switch cannot lock
    everyone out; going back to password sign-in is a deliberate
    server_config.json edit (``"login_method": "password"``).
    """
    _require_admin(user)
    from auth.config import LOGIN_METHOD_GOOGLE, login_method
    from config.server_config import update_server_config

    if body.login_method != LOGIN_METHOD_GOOGLE:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_switch",
                "message": 'Only switching to Google sign-in is supported here. To go back '
                           'to password sign-in, set "login_method": "password" in '
                           'server_config.json.',
            },
        )
    if login_method() == LOGIN_METHOD_GOOGLE:
        return {"login_method": LOGIN_METHOD_GOOGLE}
    if not _google_login_configured():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "google_oauth_not_configured",
                "message": "Configure the Google OAuth client under Service Credentials first.",
            },
        )
    update_server_config({"login_method": LOGIN_METHOD_GOOGLE})
    logger.warning("[admin] %s switched the sign-in method to Google", user["email"])
    return {"login_method": LOGIN_METHOD_GOOGLE}


class PasswordLinkRequest(BaseModel):
    email: str
    send_email: bool = False


@router.post("/admin/sign-in/password-links")
async def admin_create_password_link(
    body: PasswordLinkRequest,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Issue a set-password link: invites a new user or resets an existing
    user's password. The link is returned for the admin to hand over and,
    when asked and outgoing email is configured, also emailed.

    An address outside the admission policy is added to
    ``allowed_login_emails`` in server_config.json: inviting someone is how
    an admin grants them access on a password deployment.
    """
    _require_admin(user)
    from auth.config import allowed_login_emails
    from auth.mailer import MailerError, send_email, smtp_configured
    from auth.password_login import (
        invite_email_body,
        is_valid_email,
        issue_password_link,
        normalize_email,
    )
    from chat.auth import check_user_allowed
    from config.server_config import load_server_config, update_server_config
    from db.user_store import get_user_by_email

    if not is_password_login():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "password_login_disabled",
                "message": "Password sign-in is not enabled on this deployment.",
            },
        )
    email = normalize_email(body.email)
    if not is_valid_email(email):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_email", "message": "Enter a valid email address."},
        )

    added_to_allowed = False
    if not check_user_allowed(email):
        raw = load_server_config().get("allowed_login_emails")
        current = list(raw) if isinstance(raw, list) else []
        if email not in allowed_login_emails():
            update_server_config({"allowed_login_emails": current + [email]})
        added_to_allowed = True
        logger.info("[admin] %s added %s to allowed_login_emails", user["email"], email)

    account_exists = await get_user_by_email(email) is not None
    url, _ = await issue_password_link(request, email, "invite")
    logger.info(
        "[admin] %s issued a set-password link for %s (%s)",
        user["email"], email, "reset" if account_exists else "invite",
    )

    emailed = False
    email_error = None
    if body.send_email:
        if not smtp_configured():
            email_error = "Outgoing email (SMTP) is not configured."
        else:
            subject, text = invite_email_body(url, account_exists)
            try:
                await send_email(email, subject, text)
                emailed = True
            except MailerError as exc:
                email_error = str(exc)

    return {
        "email": email,
        "url": url,
        "account_exists": account_exists,
        "emailed": emailed,
        "email_error": email_error,
        "added_to_allowed_emails": added_to_allowed,
    }
