"""Project memory data access layer.

The project-scoped twin of db/memory_store.py: async CRUD and FTS5 search
over ``project_memories`` (one row per fact attached to a project). Every
function is scoped to a project id; callers check that the project belongs
to the user first (the routes via ``require_owned_project``, the tools
because ``ToolContext.project_id`` is the user's own conversation's
project).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, text

from db.engine import AsyncSessionLocal
from db.memory_store import MAX_MEMORY_SIZE
from db.models import Project, ProjectMemory


def _check_content(content: str) -> None:
    if not content or not content.strip():
        raise ValueError("Memory content cannot be empty.")
    if len(content.encode("utf-8")) > MAX_MEMORY_SIZE:
        raise ValueError(
            f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
        )


async def create_project_memory(project_id: str, content: str) -> dict:
    """Create a project memory. Raises ValueError on empty / oversized text."""
    _check_content(content)
    async with AsyncSessionLocal() as db:
        memory = ProjectMemory(project_id=project_id, content=content)
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return _to_dict(memory)


async def get_project_memory(project_id: str, memory_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        memory = await db.get(ProjectMemory, memory_id)
        if memory and memory.project_id == project_id:
            return _to_dict(memory)
        return None


async def list_project_memories(
    project_id: str, include_archived: bool = False,
) -> list[dict]:
    """A project's memories, newest first; archived ones only on request."""
    async with AsyncSessionLocal() as db:
        stmt = select(ProjectMemory).where(ProjectMemory.project_id == project_id)
        if not include_archived:
            stmt = stmt.where(ProjectMemory.archived == False)  # noqa: E712
        stmt = stmt.order_by(ProjectMemory.created_at.desc())
        result = await db.execute(stmt)
        return [_to_dict(m) for m in result.scalars().all()]


async def search_project_memories(project_id: str, query: str) -> list[dict]:
    """FTS5 search over a project's active memories, best match first.

    Same query syntax as ``memory_store.search_memories``; an empty query
    lists the active memories.
    """
    if not query or not query.strip():
        return await list_project_memories(project_id)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("""
                SELECT m.id, m.project_id, m.content, m.created_at,
                       m.updated_at, m.archived
                FROM project_memories m
                JOIN project_memories_fts ON m.rowid = project_memories_fts.rowid
                WHERE project_memories_fts MATCH :query
                  AND m.project_id = :project_id
                  AND m.archived = 0
                ORDER BY project_memories_fts.rank
            """),
            {"query": query, "project_id": project_id},
        )
        return [
            {
                "id": row.id,
                "project_id": row.project_id,
                "content": row.content,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "archived": bool(row.archived),
            }
            for row in result.fetchall()
        ]


async def update_project_memory(
    project_id: str, memory_id: str, content: str,
) -> Optional[dict]:
    _check_content(content)
    async with AsyncSessionLocal() as db:
        memory = await db.get(ProjectMemory, memory_id)
        if not memory or memory.project_id != project_id:
            return None
        memory.content = content
        memory.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(memory)
        return _to_dict(memory)


async def set_project_memory_archived(
    project_id: str, memory_id: str, archived: bool,
) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        memory = await db.get(ProjectMemory, memory_id)
        if not memory or memory.project_id != project_id:
            return None
        memory.archived = archived
        memory.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(memory)
        return _to_dict(memory)


async def delete_project_memory(project_id: str, memory_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        memory = await db.get(ProjectMemory, memory_id)
        if not memory or memory.project_id != project_id:
            return False
        await db.delete(memory)
        await db.commit()
        return True


def _iso(value) -> Optional[str]:
    # Raw SQL hands SQLite timestamps back as strings.
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _to_dict(memory: ProjectMemory) -> dict:
    return {
        "id": memory.id,
        "project_id": memory.project_id,
        "content": memory.content,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
        "archived": memory.archived,
    }


async def list_all_project_memory_rows() -> list[dict]:
    """Every project memory across all projects for the admin Memories
    usage report, attributed to the project's owner.

    One narrow row per ``project_memories`` row joined to its project:
    ``user_id`` (the project owner), ``project_id``, ``archived``,
    ``content_length`` (characters; never the text) and naive-UTC
    ``created_at`` / ``updated_at`` as stored. Folded per owner by
    ``build_memories_report()`` in chat/feature_usage_report.py.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Project.user_id,
                ProjectMemory.project_id,
                ProjectMemory.archived,
                func.length(ProjectMemory.content),
                ProjectMemory.created_at,
                ProjectMemory.updated_at,
            ).join(Project, Project.id == ProjectMemory.project_id)
        )
        return [
            {
                "user_id": user_id,
                "project_id": project_id,
                "archived": bool(archived),
                "content_length": int(length or 0),
                "created_at": created_at,
                "updated_at": updated_at,
            }
            for (
                user_id, project_id, archived, length, created_at, updated_at,
            ) in result.all()
        ]
