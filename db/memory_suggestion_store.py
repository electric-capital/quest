"""Memory suggestion data access layer.

A suggestion is a memory the model proposed via the non-blocking
``suggest_memory`` tool (chat/memory_suggestions.py). Rows live in
``memory_suggestions`` (db/models.py) and move from ``pending`` to
``accepted`` -- which creates the ``memories`` row in the same transaction
and archives the optional ``replaces_memory_id`` memory -- or
``dismissed``. Every function is scoped to the owning user.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select, update

from db.engine import AsyncSessionLocal
from db.memory_store import MAX_MEMORY_SIZE
from db.models import (
    Conversation,
    Memory,
    MemorySuggestion,
    MemorySuggestionStatus,
    Project,
)


# Pending suggestions one conversation may hold; the tool refuses beyond it
# so a runaway turn cannot flood the review panel.
MAX_PENDING_PER_CONVERSATION = 20

# Longest reasoning line kept (characters); longer text is cut, not refused.
MAX_REASONING_LENGTH = 500

# Statuses a reviewed suggestion can hold (the inbox's "recently reviewed").
RESOLVED_STATUSES = (
    MemorySuggestionStatus.ACCEPTED,
    MemorySuggestionStatus.DISMISSED,
)


def encode_resolved_cursor(row: dict) -> str:
    """Keyset cursor for the resolved list: ``<resolved_at iso>|<id>``.

    Shared by the project-memory twin so one cursor pages both tables in
    the inbox's merged list (chat/memory_inbox.py).
    """
    return f"{row['resolved_at']}|{row['id']}"


def decode_resolved_cursor(cursor: Optional[str]) -> Optional[tuple[datetime, str]]:
    """Inverse of :func:`encode_resolved_cursor`; ValueError on junk."""
    if not cursor:
        return None
    resolved_at, sep, row_id = cursor.partition("|")
    if not sep or not row_id:
        raise ValueError("Malformed cursor.")
    return datetime.fromisoformat(resolved_at), row_id


def resolved_keyset_filter(model, cursor: Optional[tuple[datetime, str]]):
    """The ``WHERE`` clause continuing a resolved_at-desc, id-desc keyset
    page after ``cursor``, or None for the first page."""
    if cursor is None:
        return None
    resolved_at, row_id = cursor
    return or_(
        model.resolved_at < resolved_at,
        (model.resolved_at == resolved_at) & (model.id < row_id),
    )


class SuggestionAlreadyResolvedError(Exception):
    """Accept / dismiss on a suggestion that is no longer pending."""

    def __init__(self, status: str):
        super().__init__(f"suggestion already {status}")
        self.status = status


def normalize_content(content: object) -> str:
    """Strip and size-check memory text; raises ValueError like the memory store."""
    if content is None:
        raise ValueError("Memory content cannot be empty.")
    text = str(content).strip()
    if not text:
        raise ValueError("Memory content cannot be empty.")
    if len(text.encode("utf-8")) > MAX_MEMORY_SIZE:
        raise ValueError(
            f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
        )
    return text


def _normalize_reasoning(reasoning: object) -> Optional[str]:
    if reasoning is None:
        return None
    text = " ".join(str(reasoning).split())
    if not text:
        return None
    return text[:MAX_REASONING_LENGTH]


async def create_suggestion(
    user_id: int,
    conversation_id: str,
    content: str,
    reasoning: Optional[str] = None,
    replaces_memory_id: Optional[str] = None,
) -> dict:
    """Register a pending suggestion.

    Raises ValueError on empty / oversized content, or when
    ``replaces_memory_id`` is not one of the user's active memories.
    """
    content = normalize_content(content)
    reasoning = _normalize_reasoning(reasoning)
    replaces_memory_id = (replaces_memory_id or "").strip() or None

    async with AsyncSessionLocal() as db:
        if replaces_memory_id is not None:
            replaced = await db.get(Memory, replaces_memory_id)
            if replaced is None or replaced.user_id != user_id or replaced.archived:
                raise ValueError(
                    f"replaces_memory_id {replaces_memory_id!r} is not one of "
                    "the user's active memories."
                )
        row = MemorySuggestion(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            reasoning=reasoning,
            replaces_memory_id=replaces_memory_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


async def get_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        row = await db.get(MemorySuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_dict(row)


async def list_pending(user_id: int, conversation_id: str) -> list[dict]:
    """Pending suggestions of one conversation, oldest first.

    Each dict also carries ``replaces_content``: the current text of the
    memory ``replaces_memory_id`` points at (None without one), so the
    review panel can show what accepting would change.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MemorySuggestion, Memory.content)
            .outerjoin(Memory, Memory.id == MemorySuggestion.replaces_memory_id)
            .where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.conversation_id == conversation_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .order_by(MemorySuggestion.created_at.asc(), MemorySuggestion.id.asc())
        )
        return [
            {**_to_dict(row), "replaces_content": replaced_content}
            for row, replaced_content in result.all()
        ]


async def count_pending(user_id: int, conversation_id: str) -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count())
            .select_from(MemorySuggestion)
            .where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.conversation_id == conversation_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
        )
        return int(result.scalar_one())


async def count_pending_for_user(user_id: int) -> int:
    """Pending suggestions across all of the user's conversations."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count())
            .select_from(MemorySuggestion)
            .where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
        )
        return int(result.scalar_one())


async def list_pending_for_user(user_id: int) -> list[dict]:
    """Every pending suggestion of the user, oldest first, each with
    ``replaces_content`` like :func:`list_pending` (the inbox's open list)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MemorySuggestion, Memory.content)
            .outerjoin(Memory, Memory.id == MemorySuggestion.replaces_memory_id)
            .where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .order_by(MemorySuggestion.created_at.asc(), MemorySuggestion.id.asc())
        )
        return [
            {**_to_dict(row), "replaces_content": replaced_content}
            for row, replaced_content in result.all()
        ]


async def list_resolved_for_user(
    user_id: int, limit: int, cursor: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """One keyset page of the user's accepted / dismissed suggestions, most
    recently resolved first (``cursor`` from :func:`decode_resolved_cursor`
    continues after that row). At most ``limit`` rows."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(MemorySuggestion)
            .where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.status.in_(RESOLVED_STATUSES),
            )
            .order_by(MemorySuggestion.resolved_at.desc(), MemorySuggestion.id.desc())
            .limit(limit)
        )
        keyset = resolved_keyset_filter(MemorySuggestion, cursor)
        if keyset is not None:
            stmt = stmt.where(keyset)
        result = await db.execute(stmt)
        return [_to_dict(row) for row in result.scalars().all()]


async def list_pending_summaries() -> list[dict]:
    """One row per user with pending suggestions, across all users:
    ``[{"user_id", "pending_count", "oldest_created_at"}]`` (naive UTC as
    stored). The Slack reminder's counterpart of
    ``action_request_store.list_open_request_summaries``."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                MemorySuggestion.user_id,
                func.count(MemorySuggestion.id),
                func.min(MemorySuggestion.created_at),
            )
            .where(MemorySuggestion.status == MemorySuggestionStatus.PENDING)
            .group_by(MemorySuggestion.user_id)
        )
        return [
            {"user_id": user_id, "pending_count": count, "oldest_created_at": oldest}
            for user_id, count, oldest in result.all()
        ]


async def conversation_context(conversation_ids: list[str]) -> dict[str, dict]:
    """``{conversation_id: {"conversation_title", "project_id",
    "project_name"}}`` for the inbox rows' "from" line. The title follows
    the sidebar's rule (custom name, else the cached auto title, else None);
    conversations deleted meanwhile are simply absent."""
    if not conversation_ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Conversation.id,
                Conversation.custom_name,
                Conversation.auto_title,
                Conversation.project_id,
                Project.name,
            )
            .outerjoin(Project, Project.id == Conversation.project_id)
            .where(Conversation.id.in_(conversation_ids))
        )
        return {
            cid: {
                "conversation_title": custom_name or auto_title or None,
                "project_id": project_id,
                "project_name": project_name,
            }
            for cid, custom_name, auto_title, project_id, project_name in result.all()
        }


async def find_pending_with_content(
    user_id: int, conversation_id: str, content: str,
) -> Optional[dict]:
    """A pending suggestion of the conversation with exactly this text."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MemorySuggestion).where(
                MemorySuggestion.user_id == user_id,
                MemorySuggestion.conversation_id == conversation_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
                MemorySuggestion.content == content,
            ).limit(1)
        )
        row = result.scalars().first()
        return _to_dict(row) if row is not None else None


async def find_active_memory_with_content(user_id: int, content: str) -> Optional[str]:
    """Id of the user's active memory holding exactly this text, if any."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.archived == False,  # noqa: E712
                Memory.content == content,
            ).limit(1)
        )
        return result.scalar_one_or_none()


async def accept_suggestion(
    user_id: int, suggestion_id: str, content: Optional[str] = None,
) -> Optional[tuple[dict, dict]]:
    """Accept a pending suggestion: create the memory and mark the row.

    ``content`` overrides the proposed text (the user edited it in the
    panel). The claim (``pending`` -> ``accepted``), the memory insert and
    the archive of ``replaces_memory_id`` happen in one transaction, so two
    tabs accepting at once produce one memory: the loser gets
    SuggestionAlreadyResolvedError.

    Returns ``(suggestion, memory)``, or None when the suggestion is not
    the user's. Raises ValueError on bad override content.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(MemorySuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)

        text = normalize_content(content if content is not None else row.content)

        memory = Memory(user_id=user_id, content=text)
        db.add(memory)
        await db.flush()

        claimed = await db.execute(
            update(MemorySuggestion)
            .where(
                MemorySuggestion.id == suggestion_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
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
            replaced = await db.get(Memory, row.replaces_memory_id)
            if replaced is not None and replaced.user_id == user_id:
                replaced.archived = True
                replaced.updated_at = now

        await db.commit()
        await db.refresh(row)
        await db.refresh(memory)
        from db.memory_store import _memory_to_dict
        return _to_dict(row), _memory_to_dict(memory)


async def dismiss_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    """Mark a pending suggestion dismissed; None when not the user's."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(MemorySuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)
        claimed = await db.execute(
            update(MemorySuggestion)
            .where(
                MemorySuggestion.id == suggestion_id,
                MemorySuggestion.status == MemorySuggestionStatus.PENDING,
            )
            .values(status=MemorySuggestionStatus.DISMISSED, resolved_at=now)
        )
        if claimed.rowcount != 1:
            await db.rollback()
            raise SuggestionAlreadyResolvedError("resolved")
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


def _to_dict(row: MemorySuggestion) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
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
    """Every memory suggestion across all users for the admin Memories
    usage report: ``user_id``, ``status`` and naive-UTC ``created_at`` /
    ``resolved_at`` as stored, nothing else (no content, no reasoning).
    Folded per user by ``build_memories_report()`` in
    chat/feature_usage_report.py."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                MemorySuggestion.user_id,
                MemorySuggestion.status,
                MemorySuggestion.created_at,
                MemorySuggestion.resolved_at,
            )
        )
        return [
            {
                "user_id": user_id,
                "status": status,
                "created_at": created_at,
                "resolved_at": resolved_at,
            }
            for user_id, status, created_at, resolved_at in result.all()
        ]
