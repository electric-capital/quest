"""Dream run ledger: one ``dream_runs`` row per offline memory review.

A dream (chat/dreaming.py) opens a ``running`` row before it reads the
user's memories, and closes it ``completed`` (with what it read, proposed
and cost) or ``failed`` (with the error). The user's latest row, whatever
its status, is what gates their next scheduled dream: a failing dream
must not be retried every poll. Rows cascade with their user; the
suggestions a dream registered point back at it with ``SET NULL``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from db.engine import AsyncSessionLocal
from db.models import DreamRun, DreamRunStatus, Memory


async def create_run(user_id: int, trigger: str) -> dict:
    """Open a ``running`` row for ``user_id``; ``trigger`` is
    ``"scheduled"`` or ``"manual"``."""
    async with AsyncSessionLocal() as db:
        row = DreamRun(user_id=user_id, trigger=trigger)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


async def finish_run(run_id: str, status: str, **fields) -> Optional[dict]:
    """Close a run as ``completed`` or ``failed``, setting ``finished_at``
    and any of the ledger columns passed as ``fields`` (model,
    thinking_effort, memories_read, suggestions_created, the token counts,
    estimated_cost_usd, error). None when the row is gone."""
    assert status in (DreamRunStatus.COMPLETED, DreamRunStatus.FAILED), status
    async with AsyncSessionLocal() as db:
        row = await db.get(DreamRun, run_id)
        if row is None:
            return None
        row.status = status
        row.finished_at = datetime.now(timezone.utc)
        for name, value in fields.items():
            setattr(row, name, value)
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


async def latest_run_for_user(user_id: int) -> Optional[dict]:
    """The user's most recently started run of any status, or None."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DreamRun)
            .where(DreamRun.user_id == user_id)
            .order_by(DreamRun.started_at.desc(), DreamRun.id.desc())
            .limit(1)
        )
        row = result.scalars().first()
        return _to_dict(row) if row is not None else None


async def list_recent_runs(limit: int = 50) -> list[dict]:
    """The newest ``limit`` runs across all users (the admin panel's log)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DreamRun)
            .order_by(DreamRun.started_at.desc(), DreamRun.id.desc())
            .limit(limit)
        )
        return [_to_dict(row) for row in result.scalars().all()]


async def list_all_run_rows() -> list[dict]:
    """Every dream run across all users for the admin Memories usage
    report: ``user_id``, ``status``, ``trigger``, ``memories_read``,
    ``suggestions_created``, ``input_tokens`` (None when the model was
    never called), ``estimated_cost_usd`` (None when it was not, or the
    model has no pricing entry) and the naive-UTC ``started_at`` as
    stored; never the error text. Folded per user by
    ``build_memories_report()`` in chat/feature_usage_report.py."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                DreamRun.user_id,
                DreamRun.status,
                DreamRun.trigger,
                DreamRun.memories_read,
                DreamRun.suggestions_created,
                DreamRun.input_tokens,
                DreamRun.estimated_cost_usd,
                DreamRun.started_at,
            )
        )
        return [
            {
                "user_id": user_id,
                "status": status,
                "trigger": trigger,
                "memories_read": memories_read,
                "suggestions_created": suggestions_created,
                "input_tokens": input_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "started_at": started_at,
            }
            for (
                user_id, status, trigger, memories_read, suggestions_created,
                input_tokens, estimated_cost_usd, started_at,
            ) in result.all()
        ]


async def count_memories_created_since(user_id: int, since: Optional[datetime]) -> int:
    """Active memories of the user created after ``since`` (all of them
    when ``since`` is None)."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(func.count())
            .select_from(Memory)
            .where(Memory.user_id == user_id, Memory.archived == False)  # noqa: E712
        )
        if since is not None:
            stmt = stmt.where(Memory.created_at > since)
        result = await db.execute(stmt)
        return int(result.scalar_one())


def _to_dict(row: DreamRun) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "trigger": row.trigger,
        "status": row.status,
        "model": row.model,
        "thinking_effort": row.thinking_effort,
        "memories_read": row.memories_read,
        "suggestions_created": row.suggestions_created,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cached_tokens": row.cached_tokens,
        "estimated_cost_usd": row.estimated_cost_usd,
        "error": row.error,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
