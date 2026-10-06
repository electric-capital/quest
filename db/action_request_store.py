"""Action request data access layer."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, func, update

from db.engine import AsyncSessionLocal
from db.models import ActionRequest, ActionRequestStatus, Conversation, Routine, Project


async def create_action_request(
    user_id: int,
    conversation_id: str,
    request_type: str,
    params: dict,
    reasoning: str,
) -> dict:
    """Create a new action request. Returns dict with the new auto-increment id."""
    async with AsyncSessionLocal() as db:
        req = ActionRequest(
            user_id=user_id,
            conversation_id=conversation_id,
            request_type=request_type,
            params=params,
            reasoning=reasoning,
        )
        db.add(req)
        await db.commit()
        await db.refresh(req)
        return _request_to_dict(req)


async def get_action_request(user_id: int, request_id: int) -> Optional[dict]:
    """Get a specific action request by ID, scoped to user."""
    async with AsyncSessionLocal() as db:
        req = await db.get(ActionRequest, request_id)
        if req and req.user_id == user_id:
            return _request_to_dict(req)
        return None


async def get_action_request_owners(request_ids) -> dict[int, int]:
    """``{request_id: user_id}`` for each existing request id, ANY user.

    Not user-scoped on purpose: Quest Docs attributes a write recorded as
    ``action_request:<id>`` to whoever proposed and approved that card (the
    doc owner, or a write-share recipient). Returns ids only, never request
    content. One query per 500 ids; unknown ids are simply absent.
    """
    ids = sorted({
        rid for rid in request_ids
        if isinstance(rid, int) and not isinstance(rid, bool)
    })
    owners: dict[int, int] = {}
    if not ids:
        return owners
    async with AsyncSessionLocal() as db:
        for i in range(0, len(ids), 500):
            result = await db.execute(
                select(ActionRequest.id, ActionRequest.user_id)
                .where(ActionRequest.id.in_(ids[i:i + 500]))
            )
            for rid, uid in result.all():
                owners[rid] = uid
    return owners


async def list_action_requests(
    user_id: int,
    status: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> list[dict]:
    """List action requests for a user, optionally filtered by status/conversation."""
    async with AsyncSessionLocal() as db:
        stmt = select(ActionRequest).where(ActionRequest.user_id == user_id)
        if status:
            stmt = stmt.where(ActionRequest.status == status)
        if conversation_id:
            stmt = stmt.where(ActionRequest.conversation_id == conversation_id)
        stmt = stmt.order_by(ActionRequest.created_at.desc())
        result = await db.execute(stmt)
        requests = result.scalars().all()
        return [_request_to_dict(r) for r in requests]


async def resolve_action_request(
    user_id: int,
    request_id: int,
    new_status: str,
    result: Optional[dict] = None,
) -> Optional[dict]:
    """Atomically claim an OPEN action request for one terminal outcome.

    The transition is a single conditional ``UPDATE ... WHERE status =
    'open'`` so two racing resolutions (double-click, two tabs, deny vs
    approve) can never both win: exactly one caller gets the updated dict,
    every other caller gets ``None`` and must skip its side effects.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            update(ActionRequest)
            .where(
                ActionRequest.id == request_id,
                ActionRequest.user_id == user_id,
                ActionRequest.status == ActionRequestStatus.OPEN,
            )
            .values(
                status=new_status,
                result=result,
                resolved_at=datetime.now(timezone.utc),
            )
        )
        rows = await db.execute(stmt)
        if rows.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        req = await db.get(ActionRequest, request_id)
        return _request_to_dict(req)


async def delete_all_user_action_requests(user_id: int) -> int:
    """Delete all action requests for a user (account deletion)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(ActionRequest).where(ActionRequest.user_id == user_id)
        )
        await db.commit()
        return result.rowcount


async def count_action_requests(user_id: int, status: Optional[str] = None) -> int:
    """Count action requests for a user, optionally filtered by status."""
    async with AsyncSessionLocal() as db:
        stmt = select(func.count(ActionRequest.id)).where(
            ActionRequest.user_id == user_id
        )
        if status:
            stmt = stmt.where(ActionRequest.status == status)
        result = await db.execute(stmt)
        return result.scalar() or 0


async def list_open_request_summaries() -> list[dict]:
    """One row per user that has open action requests, across all users.

    Returns ``[{"user_id", "open_count", "oldest_created_at"}]``. Used by
    the Slack pending-request notifier (chat/slack_notifier.py) to find who
    to remind; rides the ix_action_requests_user_id_status index.
    ``oldest_created_at`` is naive UTC as stored (SQLite DateTime).
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                ActionRequest.user_id,
                func.count(ActionRequest.id),
                func.min(ActionRequest.created_at),
            )
            .where(ActionRequest.status == ActionRequestStatus.OPEN)
            .group_by(ActionRequest.user_id)
        )
        result = await db.execute(stmt)
        return [
            {
                "user_id": user_id,
                "open_count": count,
                "oldest_created_at": oldest,
            }
            for user_id, count, oldest in result.all()
        ]


async def count_action_requests_by_status(user_id: int) -> dict[str, int]:
    """Count action requests for a user grouped by status.

    Returns a dict with keys 'open', 'executed', 'denied', 'stopped', and 'all',
    each mapped to the corresponding count. Missing statuses default to 0.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ActionRequest.status, func.count(ActionRequest.id))
            .where(ActionRequest.user_id == user_id)
            .group_by(ActionRequest.status)
        )
        result = await db.execute(stmt)
        rows = result.all()

        counts: dict[str, int] = {
            "open": 0, "executed": 0, "denied": 0, "stopped": 0,
        }
        for status_val, cnt in rows:
            if status_val in counts:
                counts[status_val] = cnt
        counts["all"] = sum(counts.values())
        return counts


async def list_action_requests_enriched(
    user_id: int,
    status: Optional[str] = None,
) -> list[dict]:
    """List all action requests for a user with conversation/routine/project context.

    Returns enriched dicts that include routine_name, project_name, and project_id
    in addition to the standard action request fields. Uses batch loading to avoid
    N+1 queries.
    """
    async with AsyncSessionLocal() as db:
        # 1. Get all action requests for the user
        stmt = select(ActionRequest).where(ActionRequest.user_id == user_id)
        if status:
            stmt = stmt.where(ActionRequest.status == status)
        stmt = stmt.order_by(ActionRequest.created_at.desc())
        result = await db.execute(stmt)
        requests = result.scalars().all()
        if not requests:
            return []

        # 2. Batch-load conversation metadata for all unique conversation_ids
        conversation_ids = list({r.conversation_id for r in requests})
        result = await db.execute(
            select(Conversation).where(Conversation.id.in_(conversation_ids))
        )
        conversations = result.scalars().all()
        conv_map = {c.id: c for c in conversations}

        # 3. Collect unique routine_ids and project_ids from conversations
        routine_ids = list({
            c.routine_id for c in conversations
            if c.routine_id is not None
        })
        project_ids = list({
            c.project_id for c in conversations
            if c.project_id is not None
        })

        # 4. Batch-load routine names
        routine_map: dict[str, str] = {}
        if routine_ids:
            result = await db.execute(
                select(Routine.id, Routine.name).where(Routine.id.in_(routine_ids))
            )
            routines = result.all()
            routine_map = {r.id: r.name for r in routines}

        # 5. Batch-load project names
        project_map: dict[str, str] = {}
        if project_ids:
            result = await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(project_ids))
            )
            projects = result.all()
            project_map = {p.id: p.name for p in projects}

        # 6. Assemble enriched dicts
        enriched = []
        for req in requests:
            d = _request_to_dict(req)
            conv = conv_map.get(req.conversation_id)
            if conv:
                d["routine_name"] = routine_map.get(conv.routine_id) if conv.routine_id else None
                d["project_name"] = project_map.get(conv.project_id) if conv.project_id else None
                d["project_id"] = conv.project_id
            else:
                d["routine_name"] = None
                d["project_name"] = None
                d["project_id"] = None
            enriched.append(d)

        return enriched


def _request_to_dict(req: ActionRequest) -> dict:
    return {
        "id": req.id,
        "user_id": req.user_id,
        "conversation_id": req.conversation_id,
        "request_type": req.request_type,
        "params": req.params,
        "reasoning": req.reasoning,
        "status": req.status,
        "result": req.result,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "resolved_at": req.resolved_at.isoformat() if req.resolved_at else None,
    }
