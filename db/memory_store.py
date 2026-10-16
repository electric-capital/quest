"""Memory data access layer.

Provides async CRUD operations and full-text search for user memories,
backed by SQLite (via aiosqlite) with FTS5 for text search.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, func, text

from db.engine import AsyncSessionLocal
from db.models import Memory


# Maximum memory content size: 4KB
MAX_MEMORY_SIZE = 4096


async def create_memory(user_id: int, content: str) -> dict:
    """Create a new memory for a user.

    Args:
        user_id: User's integer ID.
        content: Memory text content (max 4KB).

    Returns:
        Memory dict with id, user_id, content, created_at.

    Raises:
        ValueError: If content exceeds MAX_MEMORY_SIZE or is empty.
    """
    if not content or not content.strip():
        raise ValueError("Memory content cannot be empty.")
    if len(content.encode("utf-8")) > MAX_MEMORY_SIZE:
        raise ValueError(
            f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
        )

    async with AsyncSessionLocal() as db:
        memory = Memory(user_id=user_id, content=content)
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return _memory_to_dict(memory)


async def get_memory(user_id: int, memory_id: str) -> Optional[dict]:
    """Get a specific memory by ID, scoped to a user.

    Args:
        user_id: User's integer ID.
        memory_id: Memory UUID string.

    Returns:
        Memory dict or None if not found.
    """
    async with AsyncSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        if memory and memory.user_id == user_id:
            return _memory_to_dict(memory)
        return None


async def list_memories(user_id: int, include_archived: bool = False) -> list[dict]:
    """List memories for a user, ordered by creation time (newest first).

    By default, archived memories are excluded.

    Args:
        user_id: User's integer ID.
        include_archived: If True, include archived memories.

    Returns:
        List of memory dicts.
    """
    async with AsyncSessionLocal() as db:
        stmt = select(Memory).where(Memory.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Memory.archived == False)
        stmt = stmt.order_by(Memory.created_at.desc())
        result = await db.execute(stmt)
        memories = result.scalars().all()
        return [_memory_to_dict(m) for m in memories]


async def search_memories(user_id: int, query: str) -> list[dict]:
    """Search a user's memories using full-text search (FTS5).

    The query supports FTS5 syntax:
    - Simple words: "meeting notes"
    - Phrases: '"exact phrase"'
    - Prefix: "meet*"
    - Boolean: "meeting AND notes", "meeting OR notes", "meeting NOT agenda"

    Results are ordered by FTS5 relevance rank (best match first).

    Args:
        user_id: User's integer ID.
        query: FTS5 search query string.

    Returns:
        List of memory dicts, ordered by relevance.
    """
    if not query or not query.strip():
        return await list_memories(user_id)

    async with AsyncSessionLocal() as db:
        # Use FTS5 MATCH with a JOIN to filter by user_id.
        # memories_fts.rowid corresponds to memories.rowid.
        result = await db.execute(
            text("""
                SELECT m.id, m.user_id, m.content, m.created_at,
                       m.updated_at, m.archived
                FROM memories m
                JOIN memories_fts ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH :query
                  AND m.user_id = :user_id
                  AND m.archived = 0
                ORDER BY memories_fts.rank
            """),
            {"query": query, "user_id": user_id},
        )
        rows = result.fetchall()

        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "content": row.content,
                # raw SQL returns created_at as a string from SQLite,
                # so only call .isoformat() if it's a datetime object
                "created_at": (
                    row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else row.created_at
                ),
                "updated_at": (
                    row.updated_at.isoformat()
                    if row.updated_at and hasattr(row.updated_at, "isoformat")
                    else row.updated_at
                ),
                "archived": bool(row.archived),
            }
            for row in rows
        ]


async def update_memory(user_id: int, memory_id: str, content: str) -> Optional[dict]:
    if not content or not content.strip():
        raise ValueError("Memory content cannot be empty.")
    if len(content.encode("utf-8")) > MAX_MEMORY_SIZE:
        raise ValueError(
            f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
        )

    async with AsyncSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        if not memory or memory.user_id != user_id:
            return None
        memory.content = content
        memory.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(memory)
        return _memory_to_dict(memory)


async def archive_memory(user_id: int, memory_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        if not memory or memory.user_id != user_id:
            return None
        memory.archived = True
        memory.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(memory)
        return _memory_to_dict(memory)


async def unarchive_memory(user_id: int, memory_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        if not memory or memory.user_id != user_id:
            return None
        memory.archived = False
        memory.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(memory)
        return _memory_to_dict(memory)


async def delete_memory(user_id: int, memory_id: str) -> bool:
    """Delete a memory by ID, scoped to a user.

    Args:
        user_id: User's integer ID.
        memory_id: Memory UUID string.

    Returns:
        True if deleted, False if not found.
    """
    async with AsyncSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        if not memory or memory.user_id != user_id:
            return False
        await db.delete(memory)
        await db.commit()
        return True


async def delete_all_user_memories(user_id: int) -> int:
    """Delete all memories for a user.

    Used during account deletion to cascade-remove memory data.

    Args:
        user_id: User's integer ID.

    Returns:
        Count of deleted memories.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(Memory).where(Memory.user_id == user_id)
        )
        await db.commit()
        return result.rowcount


def _memory_to_dict(memory: Memory) -> dict:
    """Convert a Memory ORM instance to a plain dict."""
    return {
        "id": memory.id,
        "user_id": memory.user_id,
        "content": memory.content,
        "created_at": memory.created_at.isoformat() if memory.created_at else None,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        "archived": memory.archived,
    }


async def list_all_memory_rows() -> list[dict]:
    """Every memory across all users for the admin Memories usage report.

    One narrow row per ``memories`` row: ``user_id``, ``archived``,
    ``content_length`` (characters; the text itself is never returned) and
    naive-UTC ``created_at`` / ``updated_at`` as stored. Folded per user by
    ``build_memories_report()`` in chat/feature_usage_report.py.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Memory.user_id,
                Memory.archived,
                func.length(Memory.content),
                Memory.created_at,
                Memory.updated_at,
            )
        )
        return [
            {
                "user_id": user_id,
                "archived": bool(archived),
                "content_length": int(length or 0),
                "created_at": created_at,
                "updated_at": updated_at,
            }
            for user_id, archived, length, created_at, updated_at in result.all()
        ]
