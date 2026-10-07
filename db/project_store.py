"""Project data access layer.

Provides async CRUD operations for user projects. Projects group conversations
with a shared workspace and optional project guide.

This module follows the same async pattern as db/guide_store.py and
db/conversation_store.py: each function opens a fresh AsyncSessionLocal() session.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, func, case

from db.engine import AsyncSessionLocal
from db.models import Project, ProjectDocSource, Conversation, Skill, User


# Maximum project name length
MAX_PROJECT_NAME_LENGTH = 100

# Maximum project guide content size: 16KB (same as guides)
MAX_PROJECT_GUIDE_SIZE = 16 * 1024


async def create_project(
    user_id: int, name: str, guide: str = "", public: bool = False
) -> dict:
    """Create a new project.

    Args:
        user_id: User's integer ID.
        name: Project name (max 100 chars, unique per user).
        guide: Optional project guide text.
        public: Whether the project is public (internet-enabled sandbox,
            no internal data access). Chosen at creation time only; there
            is deliberately no update path for this flag.

    Returns:
        Project dict.

    Raises:
        ValueError: If name is empty, too long, or guide exceeds max size.
    """
    if not name or not name.strip():
        raise ValueError("Project name cannot be empty.")
    if len(name) > MAX_PROJECT_NAME_LENGTH:
        raise ValueError(
            f"Project name exceeds maximum length of {MAX_PROJECT_NAME_LENGTH} characters."
        )
    if len(guide.encode("utf-8")) > MAX_PROJECT_GUIDE_SIZE:
        raise ValueError(
            f"Project guide exceeds maximum size of {MAX_PROJECT_GUIDE_SIZE} bytes."
        )

    async with AsyncSessionLocal() as db:
        project = Project(
            user_id=user_id,
            name=name.strip(),
            guide=guide,
            public=public,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return _project_to_dict(project)


async def get_project(user_id: int, project_id: str) -> Optional[dict]:
    """Get a specific project by ID, scoped to user.

    Args:
        user_id: User's integer ID.
        project_id: Project UUID string.

    Returns:
        Project dict or None if not found / wrong user.
    """
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if project and project.user_id == user_id:
            return _project_to_dict(project)
        return None


async def list_projects(user_id: int, include_archived: bool = True) -> list[dict]:
    """List a user's projects, ordered by updated_at DESC then created_at DESC.

    Args:
        user_id: User's integer ID.
        include_archived: When False, archived projects are left out. The
            default includes them so lifecycle callers (account deletion,
            which collects every project's workspace directory) see the
            whole set; the list endpoint passes the user's choice.

    Returns:
        List of project dicts with an added 'conversation_count' field.
    """
    async with AsyncSessionLocal() as db:
        # Correlated scalar subquery for conversation count per project
        conv_count = (
            select(func.count(Conversation.id))
            .where(Conversation.project_id == Project.id)
            .correlate(Project)
            .scalar_subquery()
        )

        stmt = (
            select(Project, conv_count.label("conversation_count"))
            .where(Project.user_id == user_id)
        )
        if not include_archived:
            stmt = stmt.where(Project.archived == False)  # noqa: E712
        stmt = (
            stmt.order_by(
                case(
                    (Project.updated_at.isnot(None), Project.updated_at),
                    else_=Project.created_at,
                ).desc()
            )
        )

        result = await db.execute(stmt)
        rows = result.all()

        output = []
        for project, count in rows:
            d = _project_to_dict(project)
            d["conversation_count"] = count or 0
            output.append(d)
        return output


async def list_all_project_guides() -> list[dict]:
    """List every project with non-empty project instructions (admin reporting).

    The ``projects.guide`` text field ("Project Instructions" in the UI).
    Returns owner identity plus ``content_length`` instead of the text
    itself; projects with empty instructions are skipped. Ordered by owner
    email, then project name.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Project, User.email, User.name)
            .join(User, User.id == Project.user_id)
            .where(Project.guide != "")
            .order_by(User.email.asc(), Project.name.asc())
        )
        rows = []
        for project, email, name in result.all():
            rows.append({
                "id": project.id,
                "user_id": project.user_id,
                "user_email": email,
                "user_name": name or "",
                "name": project.name,
                "public": bool(project.public),
                "content_length": len(project.guide or ""),
                "created_at": project.created_at.isoformat() if project.created_at else None,
                "updated_at": project.updated_at.isoformat() if project.updated_at else None,
            })
        return rows


async def update_project(
    user_id: int,
    project_id: str,
    name: Optional[str] = None,
    guide: Optional[str] = None,
) -> Optional[dict]:
    """Update a project's name and/or guide.

    Args:
        user_id: User's integer ID.
        project_id: Project UUID string.
        name: New name (optional, max 100 chars).
        guide: New guide content (optional).

    Returns:
        Updated project dict, or None if not found.

    Raises:
        ValueError: If name is empty, too long, or guide exceeds max size.
    """
    if name is not None:
        if not name or not name.strip():
            raise ValueError("Project name cannot be empty.")
        if len(name) > MAX_PROJECT_NAME_LENGTH:
            raise ValueError(
                f"Project name exceeds maximum length of {MAX_PROJECT_NAME_LENGTH} characters."
            )

    if guide is not None:
        if len(guide.encode("utf-8")) > MAX_PROJECT_GUIDE_SIZE:
            raise ValueError(
                f"Project guide exceeds maximum size of {MAX_PROJECT_GUIDE_SIZE} bytes."
            )

    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project or project.user_id != user_id:
            return None

        if name is not None:
            project.name = name.strip()
        if guide is not None:
            project.guide = guide

        project.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(project)
        return _project_to_dict(project)


async def set_project_archived(
    user_id: int, project_id: str, archived: bool
) -> Optional[dict]:
    """Archive (``True``) or unarchive (``False``) a project, scoped to user.

    Only the flag changes: ``updated_at`` is left alone so unarchiving
    puts the project back where it was in the recency-ordered list rather
    than at the top.

    Returns:
        Updated project dict, or None if not found / wrong user.
    """
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project or project.user_id != user_id:
            return None
        project.archived = archived
        await db.commit()
        await db.refresh(project)
        return _project_to_dict(project)


async def delete_project(user_id: int, project_id: str) -> bool:
    """Delete a project by ID, scoped to user.

    The ON DELETE CASCADE FK on conversations.project_id will automatically
    delete conversation metadata rows. Project skills are deleted explicitly
    since the skills.project_id FK is non-cascading. The caller is responsible
    for deleting filesystem directories (project workspace + conversation dirs).

    Args:
        user_id: User's integer ID.
        project_id: Project UUID string.

    Returns:
        True if deleted, False if not found.
    """
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project or project.user_id != user_id:
            return False
        # Delete project skills first (non-cascading FK)
        await db.execute(
            delete(Skill).where(Skill.project_id == project_id)
        )
        await db.delete(project)
        await db.commit()
        return True


async def delete_all_user_projects(user_id: int) -> int:
    """Delete all projects for a user (used during account deletion).

    Deletes project skills first (non-cascading FK on skills.project_id),
    then deletes all projects.

    Returns:
        Count of deleted projects.
    """
    async with AsyncSessionLocal() as db:
        # Delete project skills for all user projects (non-cascading FK)
        await db.execute(
            delete(Skill).where(
                Skill.project_id.in_(
                    select(Project.id).where(Project.user_id == user_id)
                )
            )
        )
        result = await db.execute(
            delete(Project).where(Project.user_id == user_id)
        )
        await db.commit()
        return result.rowcount


class ProjectDocSourceError(ValueError):
    """A doc-source list was refused; ``code`` is the stable error code
    (``public_project_no_doc_sources``, ``invalid_doc_source``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def list_doc_source_project_ids(project_id: str) -> list[str]:
    """Ids of the public projects whose docs ``project_id``'s conversations
    may read (Project Settings > Docs access).

    No ownership scoping: the caller already resolved the project. Rows
    whose source project was deleted cascade away, so every id returned
    still names an existing project. Empty for an unknown project.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProjectDocSource.source_project_id)
            .where(ProjectDocSource.project_id == project_id)
            .order_by(ProjectDocSource.source_project_id.asc())
        )
        return list(result.scalars().all())


async def list_doc_source_projects(user_id: int, project_id: str) -> list[dict]:
    """The source projects of ``project_id`` as project dicts, by name,
    scoped to the owner (a source of another user's -- impossible by
    construction -- would be left out)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Project)
            .join(ProjectDocSource, ProjectDocSource.source_project_id == Project.id)
            .where(ProjectDocSource.project_id == project_id, Project.user_id == user_id)
            .order_by(Project.name.asc(), Project.id.asc())
        )
        return [_project_to_dict(row) for row in result.scalars().all()]


async def set_doc_source_projects(
    user_id: int, project_id: str, source_project_ids: list[str],
) -> Optional[list[dict]]:
    """Replace the doc sources of ``project_id`` with ``source_project_ids``.

    Validates inside one session: the project must be the user's and
    private (a public project's conversations never read other projects'
    docs: ``public_project_no_doc_sources``); every source must be a
    public project of the same user and not the project itself
    (``invalid_doc_source``). Duplicates collapse. Leaves ``updated_at``
    alone (the project list order should not jump on a settings change).

    Returns:
        The new source projects as dicts, by name (what
        :func:`list_doc_source_projects` returns); None when the project
        is not found / not the user's.

    Raises:
        ProjectDocSourceError: on a refused list (nothing changed).
    """
    wanted: list[str] = []
    for sid in source_project_ids:
        if not isinstance(sid, str) or not sid.strip():
            raise ProjectDocSourceError(
                "invalid_doc_source", "Source project ids must be non-empty strings.",
            )
        if sid not in wanted:
            wanted.append(sid)

    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project or project.user_id != user_id:
            return None
        if project.public:
            raise ProjectDocSourceError(
                "public_project_no_doc_sources",
                "Public projects cannot be given access to other projects' docs.",
            )
        sources: list[Project] = []
        for sid in wanted:
            if sid == project_id:
                raise ProjectDocSourceError(
                    "invalid_doc_source", "A project cannot be its own doc source.",
                )
            source = await db.get(Project, sid)
            if not source or source.user_id != user_id:
                raise ProjectDocSourceError(
                    "invalid_doc_source", f"Project not found: {sid}",
                )
            if not source.public:
                raise ProjectDocSourceError(
                    "invalid_doc_source",
                    f"Only public projects can be doc sources: '{source.name}' is private.",
                )
            sources.append(source)

        existing = {
            row.source_project_id: row
            for row in (await db.execute(
                select(ProjectDocSource).where(ProjectDocSource.project_id == project_id)
            )).scalars().all()
        }
        for sid, row in existing.items():
            if sid not in wanted:
                await db.delete(row)
        now = datetime.now(timezone.utc)
        for sid in wanted:
            if sid not in existing:
                db.add(ProjectDocSource(
                    project_id=project_id, source_project_id=sid, created_at=now,
                ))
        await db.commit()
        return [
            _project_to_dict(source)
            for source in sorted(sources, key=lambda row: (row.name, row.id))
        ]


def _project_to_dict(project: Project) -> dict:
    """Convert a Project ORM instance to a plain dict."""
    return {
        "id": project.id,
        "user_id": project.user_id,
        "name": project.name,
        "guide": project.guide,
        "public": bool(project.public),
        "archived": bool(project.archived),
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }
