"""Data-access helpers for the tool_wait_handles table.

Each row tracks a tool call that is awaiting human resolution. The row is
the sole source of truth: ``_await_wait_for_handles`` raises a sentinel
exception that suspends the conversation, and the resume bucket re-queries
this table on the next run to close the dangling tool_use.

Every transition out of ``pending`` (resolve, cancel, time out) is a
conditional ``UPDATE ... WHERE status = 'pending'`` per row, so when two
writers race (a user resolving while a timer or the expiry sweep times the
row out) the row's state in the DB decides the single winner, never a
caller's earlier read.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update

from db.engine import AsyncSessionLocal
from db.models import ToolWaitHandle, ToolWaitHandleStatus


def _row_to_dict(row: ToolWaitHandle) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "conversation_id": row.conversation_id,
        "kind": row.kind,
        "tool_id": row.tool_id,
        "status": row.status,
        "payload": row.payload,
        "response": row.response,
        "correlation_kind": row.correlation_kind,
        "correlation_id": row.correlation_id,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


async def _flip_pending(db, handle_ids: list[str], values: dict, *conditions) -> list[str]:
    """Compare-and-set each still-pending row in ``handle_ids`` to ``values``.

    One conditional ``UPDATE ... WHERE id = :id AND status = 'pending'``
    (plus ``conditions``) per id, all in the caller's transaction, so the
    DB row's state decides which writer wins. Returns the ids this call
    flipped, in request order; the caller commits (or rolls back).
    """
    flipped: list[str] = []
    for handle_id in dict.fromkeys(handle_ids):
        result = await db.execute(
            update(ToolWaitHandle)
            .where(
                ToolWaitHandle.id == handle_id,
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
                *conditions,
            )
            .values(**values)
        )
        if result.rowcount == 1:
            flipped.append(handle_id)
    return flipped


async def _load_in_order(db, handle_ids: list[str]) -> list[dict]:
    """Read back ``handle_ids`` as dicts, preserving the given order."""
    if not handle_ids:
        return []
    result = await db.execute(
        select(ToolWaitHandle).where(ToolWaitHandle.id.in_(handle_ids))
    )
    by_id = {row.id: row for row in result.scalars().all()}
    return [_row_to_dict(by_id[h]) for h in handle_ids if h in by_id]


async def create_handle(
    user_id: int,
    conversation_id: str,
    kind: str,
    tool_id: str,
    payload: dict,
    expires_at: Optional[datetime] = None,
    correlation_kind: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> dict:
    """Insert a new pending wait handle and return the resulting dict.

    ``correlation_kind`` / ``correlation_id`` may be supplied for kinds
    where the linkage to another DB row is known up front (e.g.
    ``action_request`` knows its ``action_requests.id`` at create time);
    other kinds populate them on resolve.
    """
    async with AsyncSessionLocal() as db:
        row = ToolWaitHandle(
            id=str(uuid.uuid4()),
            user_id=user_id,
            conversation_id=conversation_id,
            kind=kind,
            tool_id=tool_id,
            status=ToolWaitHandleStatus.PENDING,
            payload=payload,
            correlation_kind=correlation_kind,
            correlation_id=correlation_id,
            expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _row_to_dict(row)


async def get_handle(handle_id: str) -> Optional[dict]:
    """Fetch a single handle by id (no ownership check)."""
    async with AsyncSessionLocal() as db:
        row = await db.get(ToolWaitHandle, handle_id)
        return _row_to_dict(row) if row else None


async def get_handle_for_user(user_id: int, handle_id: str) -> Optional[dict]:
    """Fetch a handle scoped to a specific user."""
    async with AsyncSessionLocal() as db:
        row = await db.get(ToolWaitHandle, handle_id)
        if row is None or row.user_id != user_id:
            return None
        return _row_to_dict(row)


async def get_handle_by_tool_id(
    user_id: int, conversation_id: str, tool_id: str,
) -> Optional[dict]:
    """Look up the most recent handle row by ``tool_id`` for a conversation.

    The dispatch resume bucket walks dangling tool_uses by their tool_id
    and uses this lookup to find the matching wait-handle row.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ToolWaitHandle)
            .where(
                ToolWaitHandle.user_id == user_id,
                ToolWaitHandle.conversation_id == conversation_id,
                ToolWaitHandle.tool_id == tool_id,
            )
            .order_by(ToolWaitHandle.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        return _row_to_dict(row) if row else None


async def bulk_get_handles_by_ids(
    user_id: int, handle_ids: list[str],
) -> list[dict]:
    """Fetch handles by id, filtered by user ownership.

    Unknown ids and ids owned by a different user are silently dropped --
    callers should compare returned ids against the requested ones to
    detect missing/forbidden entries.
    """
    if not handle_ids:
        return []
    async with AsyncSessionLocal() as db:
        stmt = select(ToolWaitHandle).where(
            ToolWaitHandle.id.in_(handle_ids),
            ToolWaitHandle.user_id == user_id,
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [_row_to_dict(r) for r in rows]


async def find_pending_slack_reply(
    channel: str, thread_ts: str,
) -> Optional[dict]:
    """Find the most recent pending ``slack_reply`` handle for a thread.

    Slack-driven runs use ``(channel, thread_ts)`` as the implicit
    suspension key; the row's ``payload`` carries both fields. The
    Socket Mode handler resolves the matching row when the user replies
    in the thread.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ToolWaitHandle)
            .where(
                ToolWaitHandle.kind == "slack_reply",
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
            )
            .order_by(ToolWaitHandle.created_at.desc())
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        for row in rows:
            payload = row.payload or {}
            if (
                payload.get("channel") == channel
                and payload.get("thread_ts") == thread_ts
            ):
                return _row_to_dict(row)
        return None


async def find_pending_for_action_request(
    user_id: int, request_id: int,
) -> Optional[dict]:
    """Find the most recent pending ``action_request`` handle for a request.

    The action-request resolve endpoint uses this to flip the linked wait
    handle atomically with the action_request status. Returns None when
    no pending row matches (already resolved, never created, or owned by
    a different user).
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ToolWaitHandle)
            .where(
                ToolWaitHandle.user_id == user_id,
                ToolWaitHandle.kind == "action_request",
                ToolWaitHandle.correlation_kind == "action_request",
                ToolWaitHandle.correlation_id == str(request_id),
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
            )
            .order_by(ToolWaitHandle.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        return _row_to_dict(row) if row else None


async def list_pending_for_conversation(
    user_id: int, conversation_id: str,
) -> list[dict]:
    """List pending handles owned by a user, scoped to one conversation."""
    async with AsyncSessionLocal() as db:
        stmt = select(ToolWaitHandle).where(
            ToolWaitHandle.user_id == user_id,
            ToolWaitHandle.conversation_id == conversation_id,
            ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [_row_to_dict(r) for r in rows]


async def resolve_handle(
    handle_id: str,
    new_status: str,
    response: Optional[dict] = None,
    correlation_kind: Optional[str] = None,
    correlation_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    """Flip a pending handle to a terminal state. First write wins.

    The transition is a conditional ``UPDATE ... WHERE status = 'pending'``,
    so a resolve racing a timeout (or another resolve) can never overwrite
    the winner. If ``user_id`` is provided, the row is also scoped to that
    user. Returns the updated dict, or None if the handle does not exist,
    is already resolved, or is owned by a different user.
    """
    conditions = []
    if user_id is not None:
        conditions.append(ToolWaitHandle.user_id == user_id)
    async with AsyncSessionLocal() as db:
        flipped = await _flip_pending(
            db,
            [handle_id],
            {
                "status": new_status,
                "response": response,
                "correlation_kind": correlation_kind,
                "correlation_id": correlation_id,
                "resolved_at": datetime.now(timezone.utc),
            },
            *conditions,
        )
        if not flipped:
            await db.rollback()
            return None
        await db.commit()
        rows = await _load_in_order(db, flipped)
        return rows[0] if rows else None


async def cancel_pending_for_conversation(
    user_id: int,
    conversation_id: str,
    feedback: str = "Session interrupted",
) -> list[dict]:
    """Mark every pending handle for a conversation as cancelled.

    Returns the list of handles this call cancelled (may be empty; a row
    resolved or timed out concurrently is left alone). Used when the
    in-memory chat session is removed (e.g. cancellation) so any other run
    that later picks up a dangling ``wait_for_handles`` doesn't hang.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ToolWaitHandle.id).where(
                ToolWaitHandle.user_id == user_id,
                ToolWaitHandle.conversation_id == conversation_id,
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
            )
        )
        candidate_ids = list(result.scalars().all())
        if not candidate_ids:
            return []
        flipped = await _flip_pending(
            db,
            candidate_ids,
            {
                "status": ToolWaitHandleStatus.CANCELLED,
                "response": {"feedback": feedback},
                "resolved_at": datetime.now(timezone.utc),
            },
            ToolWaitHandle.user_id == user_id,
        )
        if not flipped:
            await db.rollback()
            return []
        await db.commit()
        return await _load_in_order(db, flipped)


async def mark_timed_out(
    handle_ids: list[str],
    user_id: int,
    *,
    expired_by: Optional[datetime] = None,
) -> list[dict]:
    """Transition still-pending handles to ``timed_out``.

    Each row flips through a conditional update, so a handle resolved by
    another path first (a racing accept-click) is preserved and left out of
    the result. With ``expired_by``, a row also flips only when its
    ``expires_at`` is set and no later than ``expired_by`` -- the expiry
    sweep and the ``wait_for_handles`` arm pass "now" so a deadline moved
    since their read is honoured. Returns the rows this call flipped.
    """
    if not handle_ids:
        return []
    conditions = [ToolWaitHandle.user_id == user_id]
    if expired_by is not None:
        conditions += [
            ToolWaitHandle.expires_at.is_not(None),
            ToolWaitHandle.expires_at <= expired_by,
        ]
    async with AsyncSessionLocal() as db:
        flipped = await _flip_pending(
            db,
            handle_ids,
            {
                "status": ToolWaitHandleStatus.TIMED_OUT,
                "resolved_at": datetime.now(timezone.utc),
            },
            *conditions,
        )
        if not flipped:
            await db.rollback()
            return []
        await db.commit()
        return await _load_in_order(db, flipped)


async def set_expires_at(
    handle_ids: list[str], user_id: int, expires_at: datetime,
) -> int:
    """Persist ``expires_at`` on the still-pending handles in ``handle_ids``.

    Used by the ``wait_for_handles`` arm so its timeout survives a restart
    (the expiry sweep enforces it once the in-memory timer is gone). A
    deadline that has already passed is never moved: that row is due for
    the sweep, and a new wait must not revive it. Returns the number of
    rows updated.
    """
    if not handle_ids:
        return 0
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(ToolWaitHandle)
            .where(
                ToolWaitHandle.id.in_(handle_ids),
                ToolWaitHandle.user_id == user_id,
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
                or_(
                    ToolWaitHandle.expires_at.is_(None),
                    ToolWaitHandle.expires_at > now,
                ),
            )
            .values(expires_at=expires_at)
        )
        await db.commit()
        return result.rowcount


async def list_expired_pending(now: datetime) -> list[dict]:
    """List pending handles (any user) whose ``expires_at`` is at or before
    ``now``, oldest deadline first. Read by the expiry sweep, which then
    flips them through :func:`mark_timed_out`."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ToolWaitHandle)
            .where(
                ToolWaitHandle.status == ToolWaitHandleStatus.PENDING,
                ToolWaitHandle.expires_at.is_not(None),
                ToolWaitHandle.expires_at <= now,
            )
            .order_by(ToolWaitHandle.expires_at)
        )
        return [_row_to_dict(r) for r in result.scalars().all()]
