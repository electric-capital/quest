"""Skill Library data access layer.

Provides async CRUD operations for skills and skill sharing.
Skills support three visibility levels: private (creator only),
shared (creator + explicitly shared users), and public (all users).

This module follows the same async pattern as db/guide_store.py:
each function opens a fresh AsyncSessionLocal() session.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, or_, and_

from db.engine import AsyncSessionLocal
from db.models import Skill, SkillShare, SkillVisibility, User, Project, UserSkillAutoload, ProjectSkillAutoload, RoutineSkillAutoload, Routine


# Validation limits
MAX_SKILL_NAME_LENGTH = 100
MAX_SKILL_DESCRIPTION_LENGTH = 500
MAX_SKILL_CONTENT_SIZE = 64 * 1024  # 64KB

# Valid visibility values
VALID_VISIBILITIES = {v.value for v in SkillVisibility}


def _validate_skill_fields(
    name: Optional[str] = None,
    description: Optional[str] = None,
    content: Optional[str] = None,
    visibility: Optional[str] = None,
) -> None:
    """Validate skill fields. Raises ValueError on invalid input."""
    if name is not None:
        if not name or not name.strip():
            raise ValueError("Skill name cannot be empty.")
        if len(name) > MAX_SKILL_NAME_LENGTH:
            raise ValueError(
                f"Skill name exceeds maximum length of {MAX_SKILL_NAME_LENGTH} characters."
            )

    if description is not None:
        if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Skill description exceeds maximum length of {MAX_SKILL_DESCRIPTION_LENGTH} characters."
            )

    if content is not None:
        if not content or not content.strip():
            raise ValueError("Skill content cannot be empty.")
        if len(content.encode("utf-8")) > MAX_SKILL_CONTENT_SIZE:
            raise ValueError(
                f"Skill content exceeds maximum size of {MAX_SKILL_CONTENT_SIZE} bytes."
            )

    if visibility is not None:
        if visibility not in VALID_VISIBILITIES:
            raise ValueError(
                f"Invalid visibility '{visibility}'. Must be one of: {', '.join(sorted(VALID_VISIBILITIES))}"
            )


def _skill_to_dict(skill: Skill, creator_name: Optional[str] = None, creator_email: Optional[str] = None) -> dict:
    """Convert a Skill ORM instance to a plain dict."""
    return {
        "id": skill.id,
        "creator_id": skill.creator_id,
        "project_id": skill.project_id,
        "creator_name": creator_name,
        "creator_email": creator_email,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "visibility": skill.visibility,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_skill(
    creator_id: int,
    name: str,
    description: str = "",
    content: str = "",
    visibility: str = SkillVisibility.PRIVATE,
    project_id: Optional[str] = None,
) -> dict:
    """Create a new skill.

    Args:
        creator_id: Creator's user integer ID.
        name: Skill name (max 100 chars, unique per creator).
        description: Short description (max 500 chars).
        content: Full skill instructions (max 64KB).
        visibility: Visibility level (private/shared/public/project).
        project_id: Optional project ID for project-scoped skills.

    Returns:
        Skill dict.

    Raises:
        ValueError: If validation fails or duplicate name in project.
    """
    # For project skills, force visibility to "project" and set creator_id to None
    if project_id:
        visibility = SkillVisibility.PROJECT

    _validate_skill_fields(name=name, description=description, content=content, visibility=visibility)

    async with AsyncSessionLocal() as db:
        # For project skills, check uniqueness of (project_id, name)
        if project_id:
            existing = await db.execute(
                select(Skill).where(
                    Skill.project_id == project_id,
                    Skill.name == name.strip(),
                )
            )
            if existing.scalars().first():
                raise ValueError(f"A skill named '{name.strip()}' already exists in this project.")

        skill = Skill(
            creator_id=None if project_id else creator_id,
            project_id=project_id,
            name=name.strip(),
            description=description,
            content=content,
            visibility=visibility,
        )
        db.add(skill)
        await db.commit()
        await db.refresh(skill)

        # Fetch creator info (None for project skills)
        actual_creator_id = None if project_id else creator_id
        creator_name, creator_email = await _get_creator_info(db, actual_creator_id)
        return _skill_to_dict(skill, creator_name=creator_name, creator_email=creator_email)


async def get_skill(skill_id: str) -> Optional[dict]:
    """Get a specific skill by ID (no user scoping -- access check done in routes).

    Args:
        skill_id: Skill UUID string.

    Returns:
        Skill dict or None if not found.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(Skill.id == skill_id)
        )
        result = await db.execute(stmt)
        row = result.first()
        if not row:
            return None

        skill, creator_name, creator_email = row
        return _skill_to_dict(skill, creator_name=creator_name, creator_email=creator_email)


async def list_accessible_skills(
    user_id: int,
    owned_only: bool = False,
    visibility_filter: Optional[str] = None,
) -> list[dict]:
    """List skills accessible to a user.

    Returns the union of:
    - Skills created by the user
    - Public skills
    - Shared skills where the user has a share entry

    Args:
        user_id: User's integer ID.
        owned_only: If True, return only skills created by the user.
        visibility_filter: Optional filter by visibility level.

    Returns:
        List of skill dicts with creator info.
    """
    if visibility_filter is not None and visibility_filter not in VALID_VISIBILITIES:
        raise ValueError(
            f"Invalid visibility filter '{visibility_filter}'. Must be one of: {', '.join(sorted(VALID_VISIBILITIES))}"
        )

    async with AsyncSessionLocal() as db:
        # Subquery: skill IDs shared with this user
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == user_id)
            .scalar_subquery()
        )

        if owned_only:
            conditions = [Skill.creator_id == user_id]
        else:
            conditions = [
                or_(
                    Skill.creator_id == user_id,
                    Skill.visibility == SkillVisibility.PUBLIC,
                    and_(
                        Skill.visibility == SkillVisibility.SHARED,
                        Skill.id.in_(shared_skill_ids),
                    ),
                )
            ]

        if visibility_filter:
            conditions.append(Skill.visibility == visibility_filter)

        # Exclude project skills from user-facing listings
        conditions.append(Skill.project_id.is_(None))

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(*conditions)
            .order_by(Skill.created_at.desc())
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


async def search_accessible_skills(user_id: int, keyword: str) -> list[dict]:
    """Search skills accessible to a user by keyword.

    Performs a case-insensitive substring match against skill name and
    description.  Returns the same union of accessible skills as
    ``list_accessible_skills`` (own + public + shared), filtered to
    those where the keyword appears in the name or description.

    Args:
        user_id: User's integer ID.
        keyword: Search term for substring matching.

    Returns:
        List of matching skill dicts with creator info, ordered by
        created_at descending.
    """
    async with AsyncSessionLocal() as db:
        # Subquery: skill IDs shared with this user
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == user_id)
            .scalar_subquery()
        )

        like_pattern = f"%{keyword}%"

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(
                Skill.project_id.is_(None),
                or_(
                    Skill.creator_id == user_id,
                    Skill.visibility == SkillVisibility.PUBLIC,
                    and_(
                        Skill.visibility == SkillVisibility.SHARED,
                        Skill.id.in_(shared_skill_ids),
                    ),
                ),
                or_(
                    Skill.name.ilike(like_pattern),
                    Skill.description.ilike(like_pattern),
                ),
            )
            .order_by(Skill.created_at.desc())
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


async def get_accessible_skills_by_ids(user_id: int, skill_ids: list[str]) -> list[dict]:
    """Batch-fetch skills by ID, filtered to only those the user can access.

    Returns skills the user can access (creator, public, or shared with
    a share entry). Silently skips IDs that do not exist or that the
    user cannot access. Results are ordered by name for deterministic
    prompt ordering.

    Args:
        user_id: User's integer ID.
        skill_ids: List of skill UUID strings to fetch.

    Returns:
        List of accessible skill dicts with creator info.
    """
    if not skill_ids:
        return []

    async with AsyncSessionLocal() as db:
        # Subquery: skill IDs shared with this user
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == user_id)
            .scalar_subquery()
        )

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(
                Skill.id.in_(skill_ids),
                or_(
                    Skill.creator_id == user_id,
                    Skill.visibility == SkillVisibility.PUBLIC,
                    and_(
                        Skill.visibility == SkillVisibility.SHARED,
                        Skill.id.in_(shared_skill_ids),
                    ),
                ),
            )
            .order_by(Skill.name)
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


async def update_skill(
    creator_id: int,
    skill_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    content: Optional[str] = None,
    visibility: Optional[str] = None,
) -> Optional[dict]:
    """Update a skill. Only the creator can update.

    Args:
        creator_id: Creator's user integer ID (for ownership check).
        skill_id: Skill UUID string.
        name: New name (optional).
        description: New description (optional).
        content: New content (optional).
        visibility: New visibility (optional).

    Returns:
        Updated skill dict, or None if not found / not the creator.

    Raises:
        ValueError: If validation fails.
    """
    _validate_skill_fields(name=name, description=description, content=content, visibility=visibility)

    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill or skill.creator_id != creator_id:
            return None

        if name is not None:
            skill.name = name.strip()
        if description is not None:
            skill.description = description
        if content is not None:
            skill.content = content
        if visibility is not None:
            skill.visibility = visibility

        skill.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(skill)

        creator_name, creator_email = await _get_creator_info(db, creator_id)
        return _skill_to_dict(skill, creator_name=creator_name, creator_email=creator_email)


async def delete_skill(creator_id: int, skill_id: str) -> bool:
    """Delete a skill. Only the creator can delete.

    Args:
        creator_id: Creator's user integer ID (for ownership check).
        skill_id: Skill UUID string.

    Returns:
        True if deleted, False if not found / not the creator.
    """
    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill or skill.creator_id != creator_id:
            return False
        await db.delete(skill)
        await db.commit()
        return True


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


async def add_skill_shares(skill_id: str, user_ids: list[int]) -> list[dict]:
    """Add share entries for the specified users.

    Silently skips duplicate shares (already shared with that user).

    Args:
        skill_id: Skill UUID string.
        user_ids: List of user integer IDs to share with.

    Returns:
        Updated list of all shares for this skill.
    """
    async with AsyncSessionLocal() as db:
        for uid in user_ids:
            # Check if share already exists
            existing = await db.execute(
                select(SkillShare).where(
                    SkillShare.skill_id == skill_id,
                    SkillShare.user_id == uid,
                )
            )
            if existing.scalars().first():
                continue

            share = SkillShare(
                id=str(uuid.uuid4()),
                skill_id=skill_id,
                user_id=uid,
            )
            db.add(share)

        await db.commit()

    # Return updated list
    return await list_skill_shares(skill_id)


async def list_skill_shares(skill_id: str) -> list[dict]:
    """List all users a skill is shared with.

    Args:
        skill_id: Skill UUID string.

    Returns:
        List of share dicts with user info.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(SkillShare, User.email, User.name)
            .join(User, SkillShare.user_id == User.id)
            .where(SkillShare.skill_id == skill_id)
            .order_by(SkillShare.created_at.asc())
        )
        result = await db.execute(stmt)
        rows = result.all()

        return [
            {
                "user_id": share.user_id,
                "email": email,
                "name": name or "",
                "created_at": share.created_at.isoformat() if share.created_at else None,
            }
            for share, email, name in rows
        ]


async def clear_skill_shares(skill_id: str) -> int:
    """Remove ALL share entries for a skill.

    Used when a skill's visibility leaves ``shared`` (to ``private`` or
    ``public``) so stale shares cannot silently re-activate if the skill
    is later flipped back to ``shared``.

    Args:
        skill_id: Skill UUID string.

    Returns:
        Count of removed share rows.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(SkillShare).where(SkillShare.skill_id == skill_id)
        )
        await db.commit()
        return result.rowcount


async def remove_skill_share(skill_id: str, user_id: int) -> bool:
    """Remove a specific user's share access to a skill.

    Args:
        skill_id: Skill UUID string.
        user_id: User integer ID to remove.

    Returns:
        True if removed, False if share didn't exist.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(SkillShare).where(
                SkillShare.skill_id == skill_id,
                SkillShare.user_id == user_id,
            )
        )
        await db.commit()
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Access check
# ---------------------------------------------------------------------------


async def user_can_access_skill(user_id: int, skill_id: str) -> bool:
    """Check if a user can access a skill.

    A user can access a skill if:
    - They are the creator
    - The skill is public
    - The skill is shared and there's a share entry for this user

    Args:
        user_id: User's integer ID.
        skill_id: Skill UUID string.

    Returns:
        True if the user can access the skill.
    """
    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill:
            return False

        # Creator always has access
        if skill.creator_id == user_id:
            return True

        # Public skills are accessible to all
        if skill.visibility == SkillVisibility.PUBLIC:
            return True

        # Shared skills: check for a share entry
        if skill.visibility == SkillVisibility.SHARED:
            result = await db.execute(
                select(SkillShare).where(
                    SkillShare.skill_id == skill_id,
                    SkillShare.user_id == user_id,
                )
            )
            if result.scalars().first():
                return True

        return False


# ---------------------------------------------------------------------------
# Auto-load
# ---------------------------------------------------------------------------


async def list_user_autoloaded_skill_ids(user_id: int) -> list[str]:
    """Return list of skill IDs the user has auto-loaded.

    Args:
        user_id: User's integer ID.

    Returns:
        List of skill UUID strings.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(UserSkillAutoload.skill_id)
            .where(UserSkillAutoload.user_id == user_id)
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]


async def set_user_skill_autoload(user_id: int, skill_id: str, enabled: bool) -> None:
    """Toggle auto-load for a skill.

    If enabled=True, inserts a row (ignores if already exists).
    If enabled=False, deletes the row.

    Args:
        user_id: User's integer ID.
        skill_id: Skill UUID string.
        enabled: Whether to enable or disable auto-load.
    """
    async with AsyncSessionLocal() as db:
        if enabled:
            # Check if already exists
            existing = await db.execute(
                select(UserSkillAutoload).where(
                    UserSkillAutoload.user_id == user_id,
                    UserSkillAutoload.skill_id == skill_id,
                )
            )
            if not existing.scalars().first():
                autoload = UserSkillAutoload(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    skill_id=skill_id,
                )
                db.add(autoload)
                await db.commit()
        else:
            await db.execute(
                delete(UserSkillAutoload).where(
                    UserSkillAutoload.user_id == user_id,
                    UserSkillAutoload.skill_id == skill_id,
                )
            )
            await db.commit()


async def get_user_autoloaded_skills(user_id: int) -> list[dict]:
    """Return full skill dicts for auto-loaded skills the user can access.

    Joins user_skill_autoloads with skills and filters by access control
    (creator, public, or shared with the user).

    Args:
        user_id: User's integer ID.

    Returns:
        List of accessible skill dicts with creator info.
    """
    async with AsyncSessionLocal() as db:
        # Subquery: skill IDs shared with this user
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == user_id)
            .scalar_subquery()
        )

        # Get auto-loaded skill IDs for this user
        autoloaded_ids = (
            select(UserSkillAutoload.skill_id)
            .where(UserSkillAutoload.user_id == user_id)
            .scalar_subquery()
        )

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(
                Skill.id.in_(autoloaded_ids),
                Skill.project_id.is_(None),
                or_(
                    Skill.creator_id == user_id,
                    Skill.visibility == SkillVisibility.PUBLIC,
                    and_(
                        Skill.visibility == SkillVisibility.SHARED,
                        Skill.id.in_(shared_skill_ids),
                    ),
                ),
            )
            .order_by(Skill.name)
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


async def list_shared_with_me_skills(user_id: int) -> list[dict]:
    """Return skills accessible to the user but NOT created by them.

    Includes public skills and skills explicitly shared with the user,
    excluding skills the user created.

    Args:
        user_id: User's integer ID.

    Returns:
        List of skill dicts with creator info, ordered by created_at descending.
    """
    async with AsyncSessionLocal() as db:
        # Subquery: skill IDs shared with this user
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == user_id)
            .scalar_subquery()
        )

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(
                Skill.project_id.is_(None),
                or_(Skill.creator_id != user_id, Skill.creator_id.is_(None)),
                or_(
                    Skill.visibility == SkillVisibility.PUBLIC,
                    and_(
                        Skill.visibility == SkillVisibility.SHARED,
                        Skill.id.in_(shared_skill_ids),
                    ),
                ),
            )
            .order_by(Skill.created_at.desc())
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


# ---------------------------------------------------------------------------
# Project skills
# ---------------------------------------------------------------------------


async def list_project_skills(project_id: str) -> list[dict]:
    """Return all skills belonging to a project, ordered by created_at desc.

    Args:
        project_id: Project UUID string.

    Returns:
        List of skill dicts.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Skill)
            .where(Skill.project_id == project_id)
            .order_by(Skill.created_at.desc())
        )
        result = await db.execute(stmt)
        skills = result.scalars().all()

        return [_skill_to_dict(skill) for skill in skills]


async def get_project_skill(project_id: str, skill_id: str) -> Optional[dict]:
    """Get a specific skill scoped to a project.

    Args:
        project_id: Project UUID string.
        skill_id: Skill UUID string.

    Returns:
        Skill dict or None if not found or wrong project.
    """
    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill or skill.project_id != project_id:
            return None
        return _skill_to_dict(skill)


async def update_project_skill(
    project_id: str,
    skill_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    content: Optional[str] = None,
) -> Optional[dict]:
    """Update a project skill. Validates the skill belongs to the project.

    Args:
        project_id: Project UUID string.
        skill_id: Skill UUID string.
        name: New name (optional).
        description: New description (optional).
        content: New content (optional).

    Returns:
        Updated skill dict, or None if not found / wrong project.

    Raises:
        ValueError: If validation fails or duplicate name in project.
    """
    _validate_skill_fields(name=name, description=description, content=content)

    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill or skill.project_id != project_id:
            return None

        # Check uniqueness of (project_id, name) if name is changing
        if name is not None and name.strip() != skill.name:
            existing = await db.execute(
                select(Skill).where(
                    Skill.project_id == project_id,
                    Skill.name == name.strip(),
                    Skill.id != skill_id,
                )
            )
            if existing.scalars().first():
                raise ValueError(f"A skill named '{name.strip()}' already exists in this project.")

        if name is not None:
            skill.name = name.strip()
        if description is not None:
            skill.description = description
        if content is not None:
            skill.content = content

        skill.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(skill)

        return _skill_to_dict(skill)


async def delete_project_skill(project_id: str, skill_id: str) -> bool:
    """Delete a project skill. Validates the skill belongs to the project.

    Args:
        project_id: Project UUID string.
        skill_id: Skill UUID string.

    Returns:
        True if deleted, False if not found / wrong project.
    """
    async with AsyncSessionLocal() as db:
        skill = await db.get(Skill, skill_id)
        if not skill or skill.project_id != project_id:
            return False
        await db.delete(skill)
        await db.commit()
        return True


# ---------------------------------------------------------------------------
# Project auto-loads
# ---------------------------------------------------------------------------


async def list_project_autoloaded_skill_ids(project_id: str) -> list[str]:
    """Return list of skill IDs auto-loaded for a project.

    Args:
        project_id: Project UUID string.

    Returns:
        List of skill UUID strings.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ProjectSkillAutoload.skill_id)
            .where(ProjectSkillAutoload.project_id == project_id)
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]


async def set_project_skill_autoload(project_id: str, skill_id: str, enabled: bool) -> None:
    """Toggle a skill's auto-load for a project.

    If enabled=True, inserts a row (ignores if already exists).
    If enabled=False, deletes the row.

    Args:
        project_id: Project UUID string.
        skill_id: Skill UUID string.
        enabled: Whether to enable or disable auto-load.
    """
    async with AsyncSessionLocal() as db:
        if enabled:
            # Check if already exists
            existing = await db.execute(
                select(ProjectSkillAutoload).where(
                    ProjectSkillAutoload.project_id == project_id,
                    ProjectSkillAutoload.skill_id == skill_id,
                )
            )
            if not existing.scalars().first():
                autoload = ProjectSkillAutoload(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    skill_id=skill_id,
                )
                db.add(autoload)
                await db.commit()
        else:
            await db.execute(
                delete(ProjectSkillAutoload).where(
                    ProjectSkillAutoload.project_id == project_id,
                    ProjectSkillAutoload.skill_id == skill_id,
                )
            )
            await db.commit()


async def get_project_autoloaded_skills(project_id: str) -> list[dict]:
    """Return full skill dicts for auto-loaded skills of a project.

    Joins project_skill_autoloads with skills. Used for conversation-time
    resolution.

    Access is re-checked against the project OWNER at resolution time, so a
    stale autoload row cannot keep a skill in the prompt after its creator
    revoked the share or made it private: a row resolves only when the skill
    is local to this project, or is a user-level skill the owner created,
    that is public, or that is shared with the owner.

    Args:
        project_id: Project UUID string.

    Returns:
        List of skill dicts, ordered by name.
    """
    async with AsyncSessionLocal() as db:
        owner_id = (
            select(Project.user_id)
            .where(Project.id == project_id)
            .scalar_subquery()
        )

        # Subquery: skill IDs shared with the project owner
        shared_skill_ids = (
            select(SkillShare.skill_id)
            .where(SkillShare.user_id == owner_id)
            .scalar_subquery()
        )

        # Get auto-loaded skill IDs for this project
        autoloaded_ids = (
            select(ProjectSkillAutoload.skill_id)
            .where(ProjectSkillAutoload.project_id == project_id)
            .scalar_subquery()
        )

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(
                Skill.id.in_(autoloaded_ids),
                or_(
                    Skill.project_id == project_id,
                    and_(
                        Skill.project_id.is_(None),
                        or_(
                            Skill.creator_id == owner_id,
                            Skill.visibility == SkillVisibility.PUBLIC,
                            and_(
                                Skill.visibility == SkillVisibility.SHARED,
                                Skill.id.in_(shared_skill_ids),
                            ),
                        ),
                    ),
                ),
            )
            .order_by(Skill.name)
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


# ---------------------------------------------------------------------------
# Routine auto-loads
# ---------------------------------------------------------------------------


async def list_routine_autoloaded_skill_ids(routine_id: str) -> list[str]:
    """Return list of skill IDs auto-loaded for a routine.

    Args:
        routine_id: Routine UUID string.

    Returns:
        List of skill UUID strings.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(RoutineSkillAutoload.skill_id)
            .where(RoutineSkillAutoload.routine_id == routine_id)
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]


async def set_routine_skill_autoload(routine_id: str, skill_id: str, enabled: bool) -> None:
    """Toggle a skill's auto-load for a routine.

    If enabled=True, inserts a row (ignores if already exists).
    If enabled=False, deletes the row.

    Args:
        routine_id: Routine UUID string.
        skill_id: Skill UUID string.
        enabled: Whether to enable or disable auto-load.
    """
    async with AsyncSessionLocal() as db:
        if enabled:
            existing = await db.execute(
                select(RoutineSkillAutoload).where(
                    RoutineSkillAutoload.routine_id == routine_id,
                    RoutineSkillAutoload.skill_id == skill_id,
                )
            )
            if not existing.scalars().first():
                autoload = RoutineSkillAutoload(
                    id=str(uuid.uuid4()),
                    routine_id=routine_id,
                    skill_id=skill_id,
                )
                db.add(autoload)
                await db.commit()
        else:
            await db.execute(
                delete(RoutineSkillAutoload).where(
                    RoutineSkillAutoload.routine_id == routine_id,
                    RoutineSkillAutoload.skill_id == skill_id,
                )
            )
            await db.commit()


async def get_routine_autoloaded_skills(
    routine_id: str, user_id: int, project_id: str,
) -> list[dict]:
    """Return full skill dicts for auto-loaded skills of a routine.

    Joins routine_skill_autoloads with skills. Used for conversation-time
    resolution.

    The lookup is scoped to the routine's owner and project: a routine's
    auto-loads may include the owner's private skills, so a conversation
    row carrying a routine id that isn't one of the conversation owner's
    own routines in the conversation's project resolves to nothing rather
    than to a foreign user's skill bodies.

    Args:
        routine_id: Routine UUID string.
        user_id: Owner of the conversation being resolved; must own the
            routine.
        project_id: Project of the conversation being resolved; must be
            the routine's project.

    Returns:
        List of skill dicts, ordered by name.
    """
    async with AsyncSessionLocal() as db:
        autoloaded_ids = (
            select(RoutineSkillAutoload.skill_id)
            .join(Routine, Routine.id == RoutineSkillAutoload.routine_id)
            .where(
                RoutineSkillAutoload.routine_id == routine_id,
                Routine.user_id == user_id,
                Routine.project_id == project_id,
            )
            .scalar_subquery()
        )

        stmt = (
            select(Skill, User.name, User.email)
            .outerjoin(User, Skill.creator_id == User.id)
            .where(Skill.id.in_(autoloaded_ids))
            .order_by(Skill.name)
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            _skill_to_dict(skill, creator_name=cname, creator_email=cemail)
            for skill, cname, cemail in rows
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_creator_info(db, creator_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Fetch creator name and email from the users table.

    Returns:
        Tuple of (name, email), both None if creator_id is None or user not found.
    """
    if creator_id is None:
        return None, None

    user = await db.get(User, creator_id)
    if not user:
        return None, None
    return user.name or "", user.email


# ---------------------------------------------------------------------------
# Admin lookups (no access filtering)
# ---------------------------------------------------------------------------


async def list_skill_labels(skill_ids: list[str]) -> dict[str, dict]:
    """Return ``{skill_id: label}`` for the given DB skill ids, any owner.

    Admin System Reports companion (the Skills section names the skills the
    transcripts reference, whoever created them): ``label`` is ``{id, name,
    visibility, project_id, creator_id, creator_email, creator_name}`` --
    never the content. Ids with no row (deleted skills, ``system:*`` ids)
    are absent from the result.
    """
    ids = [sid for sid in set(skill_ids) if sid and not sid.startswith("system:")]
    if not ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Skill.id, Skill.name, Skill.visibility, Skill.project_id,
                Skill.creator_id, User.email, User.name,
            )
            .outerjoin(User, Skill.creator_id == User.id)
            .where(Skill.id.in_(ids))
        )
        return {
            skill_id: {
                "id": skill_id,
                "name": name,
                "visibility": visibility,
                "project_id": project_id,
                "creator_id": creator_id,
                "creator_email": creator_email,
                "creator_name": creator_name,
            }
            for skill_id, name, visibility, project_id, creator_id, creator_email, creator_name
            in result.all()
        }
