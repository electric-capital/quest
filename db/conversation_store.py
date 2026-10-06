"""Conversation metadata data access layer.

Provides async CRUD operations for the conversations table, which stores
user-to-conversation ownership and per-conversation timestamps.  The
actual conversation content (chat_history.json, sdk_history.json,
workspace files) lives on disk at data/chats/{conversation_id}/.

This module follows the same async pattern as db/memory_store.py and
db/guide_store.py: each function opens a fresh AsyncSessionLocal() session.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Select, and_, or_, select

from db.engine import AsyncSessionLocal
from db.models import Conversation, User


async def create_conversation(
    user_id: int,
    conversation_id: str,
    created_at: datetime,
    project_id: Optional[str] = None,
    routine_id: Optional[str] = None,
    model: Optional[str] = None,
    origin: Optional[str] = None,
    custom_name: Optional[str] = None,
) -> dict:
    """Insert a new conversations row, optionally linked to a project.

    Args:
        user_id: Owner's integer user ID (FK to users.id).
        conversation_id: UUID string matching the on-disk directory name.
        created_at: Conversation creation timestamp (UTC).
        project_id: Optional project UUID to link this conversation to.
        routine_id: Optional routine UUID that created this conversation.
        model: Optional LLM model ID used for this conversation.
        origin: Optional origin discriminator ("web" or "slack"). NULL means
            the conversation came from the web UI.
        custom_name: Optional sidebar title to set at creation time (routine
            runs are named after their routine). Truncated to
            ``MAX_CONVERSATION_NAME_LENGTH``; None leaves the auto-title path
            in charge.

    Returns:
        The newly created row as a dict.
    """
    if custom_name is not None:
        custom_name = custom_name.strip()[:MAX_CONVERSATION_NAME_LENGTH] or None
    async with AsyncSessionLocal() as db:
        conv = Conversation(
            id=conversation_id,
            user_id=user_id,
            project_id=project_id,
            routine_id=routine_id,
            model=model,
            origin=origin,
            custom_name=custom_name,
            created_at=created_at,
            last_message_at=created_at,  # initialised to created_at; updated on first message
        )
        # Mirror the SQL server defaults locally so the dict we return is
        # complete without a second SELECT. The schema has ``archived``
        # default false and ``last_message_seq`` server default '0'; setting
        # the Python attrs explicitly means we can skip ``db.refresh(conv)``
        # and save a round trip on every conversation create.
        if conv.archived is None:
            conv.archived = False
        if conv.last_message_seq is None:
            conv.last_message_seq = 0
        db.add(conv)
        await db.commit()
        return _conversation_to_dict(conv)


async def get_conversation_meta(user_id: int, conversation_id: str) -> Optional[dict]:
    """Return the conversations row for (user_id, conversation_id), or None.

    Used for ownership verification: returns None if the conversation does
    not exist or belongs to a different user.

    Args:
        user_id: The requesting user's integer ID.
        conversation_id: UUID of the conversation.

    Returns:
        Dict with conversation metadata, or None.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv and conv.user_id == user_id:
            return _conversation_to_dict(conv)
        return None


# Chunk size for ``IN (...)`` lookups, well under SQLite's bound-parameter cap.
_IN_CHUNK = 500


async def get_conversations_meta(user_id: int, conversation_ids) -> dict[str, dict]:
    """Batch :func:`get_conversation_meta`: ``{conversation_id: meta}`` for
    the ids among ``conversation_ids`` that exist AND belong to ``user_id``.

    Missing ids and other users' conversations are simply absent (same
    ownership rule as the single lookup). Non-string ids are ignored and
    duplicates collapse. One session, one ``WHERE user_id = ? AND id IN
    (...)`` query per 500 ids.

    Args:
        user_id: The requesting user's integer ID.
        conversation_ids: Any iterable of conversation UUID strings.

    Returns:
        Dict of conversation id -> the same metadata dict
        :func:`get_conversation_meta` returns.
    """
    ids = sorted({cid for cid in conversation_ids if isinstance(cid, str) and cid})
    found: dict[str, dict] = {}
    if not ids:
        return found
    async with AsyncSessionLocal() as db:
        for i in range(0, len(ids), _IN_CHUNK):
            result = await db.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.id.in_(ids[i:i + _IN_CHUNK]),
                )
            )
            for conv in result.scalars().all():
                found[conv.id] = _conversation_to_dict(conv)
    return found


async def list_conversations_meta(
    user_id: int,
    include_archived: bool = False,
    *,
    exclude_projects: bool = False,
    include_slack: bool = True,
    include_inference: bool = True,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Return conversations for a user, ordered by last_message_at DESC.

    Defaults return the user's full list (legacy behavior). The keyword-only
    params let the sidebar list endpoint filter server-side and page with a
    keyset cursor instead of shipping every row on each refresh.

    Args:
        user_id: The user's integer ID.
        include_archived: If True, include archived conversations in results.
        exclude_projects: If True, drop project-linked conversations
            (project_id set) -- the top-level sidebar never renders them.
        include_slack: If False, drop origin="slack" rows.
        include_inference: If False, drop origin="inference_api" rows.
        limit: Optional row cap (already-validated by the caller).
        before: Optional keyset cursor as ``(last_message_at, id)`` -- return
            only rows strictly older than this pair under the
            ``(last_message_at DESC, id DESC)`` sort. Keyset (not OFFSET) so
            newly prepended conversations can't shift the page window.

    Returns:
        List of conversation metadata dicts, most recently active first.
    """
    async with AsyncSessionLocal() as db:
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Conversation.archived == False)
        if exclude_projects:
            stmt = stmt.where(Conversation.project_id.is_(None))
        # origin is NULL for legacy web conversations; a bare ``origin != x``
        # would silently drop those NULL rows, hence the explicit is_(None).
        if not include_slack:
            stmt = stmt.where(
                or_(Conversation.origin.is_(None), Conversation.origin != "slack")
            )
        if not include_inference:
            stmt = stmt.where(
                or_(
                    Conversation.origin.is_(None),
                    Conversation.origin != "inference_api",
                )
            )
        if before is not None:
            before_ts, before_id = before
            stmt = stmt.where(
                or_(
                    Conversation.last_message_at < before_ts,
                    and_(
                        Conversation.last_message_at == before_ts,
                        Conversation.id < before_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            Conversation.last_message_at.desc(), Conversation.id.desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        conversations = result.scalars().all()
        return [_conversation_to_dict(c) for c in conversations]


async def update_last_message_at(conversation_id: str, ts: datetime) -> None:
    """Set last_message_at for the given conversation.

    Called after every message append so the sidebar sort order stays
    current without scanning message arrays.  No-op if not found.

    Args:
        conversation_id: UUID of the conversation.
        ts: The timestamp of the latest message (UTC).
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        conv.last_message_at = ts
        await db.commit()


async def update_last_message_seq(conversation_id: str, new_seq: int) -> None:
    """Advance ``last_message_seq`` to ``new_seq`` if higher than the current
    value (idempotent).

    Used by ``ChatStorage.append_*`` to keep the DB cache aligned with the
    JSON file's high-water mark. If the row is missing the call is a no-op
    -- the next append will re-attempt and the on-disk file remains the
    source of truth either way.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        if conv.last_message_seq is None or new_seq > conv.last_message_seq:
            conv.last_message_seq = new_seq
            await db.commit()


async def get_last_message_seq(conversation_id: str) -> int:
    """Return the cached high-water seq for a conversation, or 0 if missing."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return 0
        return conv.last_message_seq or 0


async def list_project_conversations_meta(project_id: str, include_archived: bool = False) -> list[dict]:
    """Return all conversations for a project, ordered by last_message_at DESC.

    Args:
        project_id: The project's UUID string.
        include_archived: If True, include archived conversations in results.

    Returns:
        List of conversation metadata dicts.
    """
    async with AsyncSessionLocal() as db:
        stmt = select(Conversation).where(Conversation.project_id == project_id)
        if not include_archived:
            stmt = stmt.where(Conversation.archived == False)
        stmt = stmt.order_by(Conversation.last_message_at.desc())
        result = await db.execute(stmt)
        conversations = result.scalars().all()
        return [_conversation_to_dict(c) for c in conversations]


async def get_project_for_conversation(conversation_id: str) -> Optional[str]:
    """Return the project_id for a conversation, or None if standalone.

    Args:
        conversation_id: Conversation UUID.

    Returns:
        project_id string or None.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv:
            return conv.project_id
        return None


async def set_conversation_project(
    user_id: int, conversation_id: str, project_id: str
) -> Optional[dict]:
    """Attach a standalone conversation to a project.

    Only standalone conversations can be moved -- a conversation that already
    belongs to a project is left untouched (its workspace files live in the
    old project's shared workspace, so re-pointing the row alone would be
    wrong).

    Args:
        user_id: Owner's integer user ID.
        conversation_id: UUID of the conversation.
        project_id: UUID of the destination project.

    Returns:
        Updated conversation dict, or None if not found / not owned /
        already in a project.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id or conv.project_id is not None:
            return None
        conv.project_id = project_id
        await db.commit()
        await db.refresh(conv)
        return _conversation_to_dict(conv)


async def archive_conversation(user_id: int, conversation_id: str) -> Optional[dict]:
    """Set archived=True on a conversation. Returns updated dict or None if not found."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id:
            return None
        conv.archived = True
        await db.commit()
        await db.refresh(conv)
        return _conversation_to_dict(conv)


async def unarchive_conversation(user_id: int, conversation_id: str) -> Optional[dict]:
    """Set archived=False on a conversation. Returns updated dict or None if not found."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id:
            return None
        conv.archived = False
        await db.commit()
        await db.refresh(conv)
        return _conversation_to_dict(conv)


MAX_CONVERSATION_NAME_LENGTH = 100


async def rename_conversation(
    user_id: int, conversation_id: str, custom_name: Optional[str]
) -> Optional[dict]:
    """Set or clear the custom_name on a conversation.

    Args:
        user_id: Owner's integer user ID.
        conversation_id: UUID of the conversation.
        custom_name: New name string, or None to clear (revert to auto-title).

    Returns:
        Updated conversation dict, or None if not found / not owned.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id:
            return None
        conv.custom_name = custom_name
        await db.commit()
        await db.refresh(conv)
        return _conversation_to_dict(conv)


async def update_conversation_model(conversation_id: str, model: str) -> None:
    """Set the model on a conversation, only if not already set.

    This is called after model resolution to persist the effective model
    on the conversation record.  No-op if the conversation already has a
    model set, or if the conversation is not found.

    Args:
        conversation_id: UUID of the conversation.
        model: LLM model ID string (e.g. "claude-sonnet-4-6").
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        if conv.model is not None:
            return  # Already set; do not overwrite
        conv.model = model
        await db.commit()


async def set_conversation_model(conversation_id: str, model: str) -> None:
    """Unconditionally set the model on a conversation.

    Unlike update_conversation_model(), this always overwrites the existing
    value.  Used by the PATCH endpoint when the user explicitly changes
    the model.

    Args:
        conversation_id: UUID of the conversation.
        model: LLM model ID string.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        conv.model = model
        await db.commit()


async def set_conversation_flags(conversation_id: str, flags: list[str]) -> None:
    """Set the per-conversation ``flags`` array, only if not already set.

    Flags are start-of-conversation only (parsed from the magic ``%%flags[...]``
    first line), so this mirrors ``update_conversation_model`` /
    ``set_conversation_auto_title``: it is a no-op once any flags are present, or
    if the conversation is not found. An empty ``flags`` list is also a no-op
    (nothing to persist).

    Args:
        conversation_id: UUID of the conversation.
        flags: List of enabled flag-name strings (e.g. ["nested_subagents"]).
    """
    if not flags:
        return
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        if conv.flags:
            return  # Already set; do not overwrite (start-of-conversation only)
        conv.flags = list(flags)
        await db.commit()


async def get_conversation_flags(conversation_id: str) -> list[str]:
    """Return the conversation's enabled flags, or [] if none / not found.

    NULL/missing is treated as "no flags" (empty list), mirroring how
    application code treats an unset flag set. The loop normally reads flags off
    the ``meta`` dict already loaded by the send handler; this helper exists for
    callers that don't have ``meta`` in hand.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return []
        return list(conv.flags or [])


def _conversation_to_dict(conv: Conversation) -> dict:
    """Convert a Conversation ORM instance to a plain dict."""
    return {
        "id": conv.id,
        "user_id": conv.user_id,
        "project_id": conv.project_id,
        "routine_id": conv.routine_id,
        "model": conv.model,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "last_message_seq": conv.last_message_seq or 0,
        "archived": conv.archived,
        "custom_name": conv.custom_name,
        "auto_title": conv.auto_title,
        "origin": conv.origin,
        "flags": conv.flags or [],
    }


async def list_latest_active_conversations(
    limit: int = 20, include_routines: bool = True
) -> list[dict]:
    """Return the most recently active conversations across all users.

    Admin-only callers use this for system monitoring. Sorted by
    ``last_message_at DESC`` and joined to ``users`` so the caller can
    render owner identity without a second query. With
    ``include_routines=False``, conversations created by a routine
    (``routine_id`` set -- scheduled or one-click runs) are filtered out
    in SQL so the list stays ``limit`` rows long.

    Each returned dict carries the row fields needed by
    ``ChatStorage._resolve_list_title`` (``custom_name``, ``auto_title``,
    ``last_message_seq``) plus the owner's ``email`` and ``name``. The
    route handler resolves the displayed title so the store stays
    oblivious to display logic.
    """
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Conversation, User.email, User.name)
            .join(User, Conversation.user_id == User.id)
            .order_by(Conversation.last_message_at.desc())
            .limit(limit)
        )
        if not include_routines:
            stmt = stmt.where(Conversation.routine_id.is_(None))
        result = await db.execute(stmt)
        return [
            _admin_conversation_row(conv, email, name)
            for conv, email, name in result.all()
        ]


async def get_conversations_with_users(
    conversation_ids: list[str],
) -> dict[str, dict]:
    """Return conversation rows joined to users, keyed by conversation id.

    Batch companion to ``list_latest_active_conversations`` for admin views
    that already know which conversations they want (e.g. the cost-analysis
    ranking). Same row shape; ids without a surviving conversation row are
    simply absent (the ``llm_calls_*`` tables outlive deleted conversations).
    """
    if not conversation_ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation, User.email, User.name)
            .join(User, Conversation.user_id == User.id)
            .where(Conversation.id.in_(conversation_ids))
        )
        return {
            conv.id: _admin_conversation_row(conv, email, name)
            for conv, email, name in result.all()
        }


def routine_conversation_ids_query(routine_id: str) -> Select:
    """Single-column ``SELECT id`` of every conversation a routine created.

    Built for ``IN (subquery)`` use by the raw-usage aggregation queries
    (``get_usage_by_model_for_conversation_query``): a long-running hourly
    routine owns thousands of conversations, so the id set stays inside
    SQL instead of round-tripping as a bind-parameter list. Rows keep
    their ``routine_id`` only while the routine exists (SET NULL on routine
    delete) and disappear with the conversation itself, so a deleted run's
    calls are no longer attributable to the routine.
    """
    return select(Conversation.id).where(Conversation.routine_id == routine_id)


async def list_routine_conversation_rows(routine_id: str) -> list[dict]:
    """Return every conversation a routine created, newest first.

    Companion to ``routine_conversation_ids_query`` for the per-routine
    cost report: a narrow projection (no users join -- the caller already
    owns the routine) carrying the ``ChatStorage._resolve_list_title``
    inputs, the run start (``created_at``) the report buckets costs by,
    and ``model`` as the run's model proxy. Ordered by ``created_at DESC``
    with ``id`` as the tiebreak so same-instant runs page stably.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Conversation.id,
                Conversation.created_at,
                Conversation.custom_name,
                Conversation.auto_title,
                Conversation.last_message_seq,
                Conversation.model,
            )
            .where(Conversation.routine_id == routine_id)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        )
        return [
            {
                "id": conv_id,
                "created_at": created_at,
                "custom_name": custom_name,
                "auto_title": auto_title,
                "last_message_seq": last_message_seq or 0,
                "model": model,
            }
            for conv_id, created_at, custom_name, auto_title, last_message_seq, model
            in result.all()
        ]


async def list_conversation_activity_rows() -> list[dict]:
    """Return a minimal owner/routine/activity projection of ALL conversations.

    Admin user-report companion to the per-conversation admin queries: the
    report needs, for every surviving conversation, who owns it, whether a
    routine created it (cost split + exclusions), and the created/last-active
    bounds so the per-range active-days scan can skip chat-history files that
    cannot overlap the requested window. One narrow full-table query --
    deliberately no users join, the caller already holds the user roster.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Conversation.id,
                Conversation.user_id,
                Conversation.routine_id,
                Conversation.created_at,
                Conversation.last_message_at,
            )
        )
        return [
            {
                "id": conv_id,
                "user_id": user_id,
                "routine_id": routine_id,
                "created_at": created_at,
                "last_message_at": last_message_at,
            }
            for conv_id, user_id, routine_id, created_at, last_message_at
            in result.all()
        ]


def _admin_conversation_row(conv: Conversation, email: str, name: str | None) -> dict:
    """Project a (Conversation, user email, user name) join row for admin views."""
    return {
        "id": conv.id,
        "user_id": conv.user_id,
        "user_email": email,
        "user_name": name or "",
        "project_id": conv.project_id,
        "routine_id": conv.routine_id,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "last_message_seq": conv.last_message_seq or 0,
        "custom_name": conv.custom_name,
        "auto_title": conv.auto_title,
        "origin": conv.origin,
        # ``model`` is set by ``update_conversation_model`` on first
        # turn and only overwritten by the explicit PATCH; per-message
        # model is not tracked, so this is our closest proxy for
        # "model used by the most recent model output".
        "last_model": conv.model,
    }


async def set_conversation_auto_title(conversation_id: str, auto_title: str) -> None:
    """Set ``auto_title`` on a conversation, only if not already set.

    Called from ``ChatStorage.append_message`` / ``append_structured_messages``
    on the first user message so the sidebar list endpoint can derive titles
    from the DB without opening chat_history.json. No-op if the row already
    has a cached title or if the row does not exist.
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        if conv.auto_title is not None:
            return
        conv.auto_title = auto_title
        await db.commit()
