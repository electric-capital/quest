"""Project memory suggestion data access layer.

The project-scoped twin of db/memory_suggestion_store.py: rows in
``project_memory_suggestions`` registered by the non-blocking
``suggest_project_memory`` tool (chat/project_memories.py). Accepting one
creates a ``project_memories`` row of the suggestion's project and archives
the optional ``replaces_memory_id`` project memory in the same transaction.
Every function is scoped to the owning user.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update

from db.engine import AsyncSessionLocal
from db.memory_suggestion_store import (
    MAX_PENDING_PER_CONVERSATION,
    RESOLVED_STATUSES,
    SuggestionAlreadyResolvedError,
    _normalize_reasoning,
    normalize_content,
    resolved_keyset_filter,
)
from db.models import (
    MemorySuggestionStatus,
    ProjectMemory,
    ProjectMemorySuggestion,
)

__all__ = [
    "MAX_PENDING_PER_CONVERSATION",
    "SuggestionAlreadyResolvedError",
    "accept_suggestion",
    "count_pending",
    "count_pending_for_user",
    "create_suggestion",
    "dismiss_suggestion",
    "find_active_memory_with_content",
    "find_pending_with_content",
    "list_pending",
    "list_pending_for_user",
    "list_pending_summaries",
    "list_resolved_for_user",
    "normalize_content",
]


async def create_suggestion(
    user_id: int,
    project_id: str,
    conversation_id: str,
    content: str,
    reasoning: Optional[str] = None,
    replaces_memory_id: Optional[str] = None,
) -> dict:
    """Register a pending suggestion.

    Raises ValueError on empty / oversized content, or when
    ``replaces_memory_id`` is not one of the project's active memories.
    """
    content = normalize_content(content)
    reasoning = _normalize_reasoning(reasoning)
    replaces_memory_id = (replaces_memory_id or "").strip() or None

    async with AsyncSessionLocal() as db:
        if replaces_memory_id is not None:
            replaced = await db.get(ProjectMemory, replaces_memory_id)
            if (
                replaced is None
                or replaced.project_id != project_id
                or replaced.archived
            ):
                raise ValueError(
                    f"replaces_memory_id {replaces_memory_id!r} is not one of "
                    "this project's active project memories."
                )
        row = ProjectMemorySuggestion(
            user_id=user_id,
            project_id=project_id,
            conversation_id=conversation_id,
            content=content,
            reasoning=reasoning,
            replaces_memory_id=replaces_memory_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


async def list_pending(user_id: int, conversation_id: str) -> list[dict]:
    """Pending suggestions of one conversation, oldest first, each with the
    replaced project memory's current text as ``replaces_content``."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProjectMemorySuggestion, ProjectMemory.content)
            .outerjoin(
                ProjectMemory,
                ProjectMemory.id == ProjectMemorySuggestion.replaces_memory_id,
            )
            .where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.conversation_id == conversation_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .order_by(
                ProjectMemorySuggestion.created_at.asc(),
                ProjectMemorySuggestion.id.asc(),
            )
        )
        return [
            {**_to_dict(row), "replaces_content": replaced_content}
            for row, replaced_content in result.all()
        ]


async def count_pending(user_id: int, conversation_id: str) -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count())
            .select_from(ProjectMemorySuggestion)
            .where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.conversation_id == conversation_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
        )
        return int(result.scalar_one())


async def count_pending_for_user(user_id: int) -> int:
    """Pending project-memory suggestions across all of the user's conversations."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count())
            .select_from(ProjectMemorySuggestion)
            .where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
        )
        return int(result.scalar_one())


async def list_pending_for_user(user_id: int) -> list[dict]:
    """Every pending suggestion of the user, oldest first, with
    ``replaces_content`` like :func:`list_pending`."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProjectMemorySuggestion, ProjectMemory.content)
            .outerjoin(
                ProjectMemory,
                ProjectMemory.id == ProjectMemorySuggestion.replaces_memory_id,
            )
            .where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .order_by(
                ProjectMemorySuggestion.created_at.asc(),
                ProjectMemorySuggestion.id.asc(),
            )
        )
        return [
            {**_to_dict(row), "replaces_content": replaced_content}
            for row, replaced_content in result.all()
        ]


async def list_resolved_for_user(
    user_id: int, limit: int, cursor: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """One keyset page of the user's accepted / dismissed project-memory
    suggestions, most recently resolved first; same contract as
    ``memory_suggestion_store.list_resolved_for_user``."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ProjectMemorySuggestion)
            .where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.status.in_(RESOLVED_STATUSES),
            )
            .order_by(
                ProjectMemorySuggestion.resolved_at.desc(),
                ProjectMemorySuggestion.id.desc(),
            )
            .limit(limit)
        )
        keyset = resolved_keyset_filter(ProjectMemorySuggestion, cursor)
        if keyset is not None:
            stmt = stmt.where(keyset)
        result = await db.execute(stmt)
        return [_to_dict(row) for row in result.scalars().all()]


async def list_pending_summaries() -> list[dict]:
    """One row per user with pending project-memory suggestions, across all
    users; same shape as ``memory_suggestion_store.list_pending_summaries``."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                ProjectMemorySuggestion.user_id,
                func.count(ProjectMemorySuggestion.id),
                func.min(ProjectMemorySuggestion.created_at),
            )
            .where(ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING)
            .group_by(ProjectMemorySuggestion.user_id)
        )
        return [
            {"user_id": user_id, "pending_count": count, "oldest_created_at": oldest}
            for user_id, count, oldest in result.all()
        ]


async def find_pending_with_content(
    user_id: int, conversation_id: str, content: str,
) -> Optional[dict]:
    """A pending suggestion of the conversation with exactly this text."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProjectMemorySuggestion).where(
                ProjectMemorySuggestion.user_id == user_id,
                ProjectMemorySuggestion.conversation_id == conversation_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
                ProjectMemorySuggestion.content == content,
            ).limit(1)
        )
        row = result.scalars().first()
        return _to_dict(row) if row is not None else None


async def find_active_memory_with_content(
    project_id: str, content: str,
) -> Optional[str]:
    """Id of the project's active memory holding exactly this text, if any."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProjectMemory.id).where(
                ProjectMemory.project_id == project_id,
                ProjectMemory.archived == False,  # noqa: E712
                ProjectMemory.content == content,
            ).limit(1)
        )
        return result.scalar_one_or_none()


async def accept_suggestion(
    user_id: int, suggestion_id: str, content: Optional[str] = None,
) -> Optional[tuple[dict, dict]]:
    """Accept a pending suggestion: create the project memory and mark the row.

    Same contract as ``memory_suggestion_store.accept_suggestion``: one
    transaction claims the row, inserts the memory (``content`` overrides
    the proposed text) and archives the replaced memory; a concurrent
    second accept raises SuggestionAlreadyResolvedError. Returns
    ``(suggestion, project_memory)`` or None when not the user's.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(ProjectMemorySuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)

        text = normalize_content(content if content is not None else row.content)

        memory = ProjectMemory(project_id=row.project_id, content=text)
        db.add(memory)
        await db.flush()

        claimed = await db.execute(
            update(ProjectMemorySuggestion)
            .where(
                ProjectMemorySuggestion.id == suggestion_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .values(
                status=MemorySuggestionStatus.ACCEPTED,
                memory_id=memory.id,
                resolved_at=now,
            )
        )
        if claimed.rowcount != 1:
            await db.rollback()
            raise SuggestionAlreadyResolvedError("resolved")

        if row.replaces_memory_id:
            replaced = await db.get(ProjectMemory, row.replaces_memory_id)
            if replaced is not None and replaced.project_id == row.project_id:
                replaced.archived = True
                replaced.updated_at = now

        await db.commit()
        await db.refresh(row)
        await db.refresh(memory)
        from db.project_memory_store import _to_dict as _memory_to_dict
        return _to_dict(row), _memory_to_dict(memory)


async def dismiss_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    """Mark a pending suggestion dismissed; None when not the user's."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(ProjectMemorySuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)
        claimed = await db.execute(
            update(ProjectMemorySuggestion)
            .where(
                ProjectMemorySuggestion.id == suggestion_id,
                ProjectMemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .values(status=MemorySuggestionStatus.DISMISSED, resolved_at=now)
        )
        if claimed.rowcount != 1:
            await db.rollback()
            raise SuggestionAlreadyResolvedError("resolved")
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


def _to_dict(row: ProjectMemorySuggestion) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "project_id": row.project_id,
        "conversation_id": row.conversation_id,
        "content": row.content,
        "reasoning": row.reasoning,
        "replaces_memory_id": row.replaces_memory_id,
        "status": row.status,
        "memory_id": row.memory_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


async def list_all_suggestion_rows() -> list[dict]:
    """Every project memory suggestion across all users for the admin
    Memories usage report: ``user_id`` (the suggesting conversation's
    owner), ``project_id``, ``status`` and naive-UTC ``created_at`` /
    ``resolved_at`` as stored, nothing else. Folded per user by
    ``build_memories_report()`` in chat/feature_usage_report.py."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                ProjectMemorySuggestion.user_id,
                ProjectMemorySuggestion.project_id,
                ProjectMemorySuggestion.status,
                ProjectMemorySuggestion.created_at,
                ProjectMemorySuggestion.resolved_at,
            )
        )
        return [
            {
                "user_id": user_id,
                "project_id": project_id,
                "status": status,
                "created_at": created_at,
                "resolved_at": resolved_at,
            }
            for user_id, project_id, status, created_at, resolved_at in result.all()
        ]
