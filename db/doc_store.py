"""Quest Docs data access layer.

Async CRUD over the ``docs`` and ``doc_shares`` tables, returning plain
dicts (the shape documented on :func:`_doc_to_dict`). The doc body, assets
and revisions live on disk (chat/docs/files.py); this module only keeps the
metadata, counters, and shares.

Same async pattern as db/skill_store.py: each function opens a fresh
``AsyncSessionLocal()`` session (imported at module level so tests can
monkeypatch it). Timestamps are written as ``datetime.now(timezone.utc)``
and read back naive-UTC from SQLite (every write path commits then
refreshes, so returned dicts always carry the stored form); the
``updated_at`` ISO string is the optimistic-concurrency token.

Access decisions do NOT live here -- see chat/docs/access.py. Writes that
touch the body go through chat/docs/service.py, which calls
:func:`update_after_write` once the file write landed.
"""

from datetime import datetime, timezone
from typing import Iterable, Optional
import uuid

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import IntegrityError

from chat.docs.constants import (
    DOC_DESCRIPTION_MAX_LEN,
    DOC_MODES,
    DOC_SHARE_PERMISSIONS,
    DOC_TITLE_MAX_LEN,
    doc_not_found_message,
)
from db.engine import AsyncSessionLocal
from db.models import Doc, DocShare

# Chunk size for ``IN (...)`` lookups, well under SQLite's bound-parameter cap.
_IN_CHUNK = 500


class DocValidationError(ValueError):
    """Invalid input to a doc store function (bad title, mode, permission...)."""


class DuplicateDocTitleError(ValueError):
    """Another doc of the same owner and scope already uses this title.

    Titles are unique case-insensitively per ``(owner_id, project_id)``,
    where a NULL ``project_id`` (user doc) is its own scope.
    """


class StaleDocError(ValueError):
    """``expected_updated_at`` did not match the row (concurrent write).

    Carries the current row so the route can return it in the flat 409
    ``stale_update`` body, sparing the client a second GET (mirrors
    ``db.routine_store.StaleRoutineError``).
    """

    def __init__(self, current: dict):
        super().__init__("Doc was modified by another writer")
        self.current = current


# ---------------------------------------------------------------------------
# Validation + serialization
# ---------------------------------------------------------------------------


def _validate_title(title) -> str:
    """Return the stripped title, or raise :class:`DocValidationError`."""
    if not isinstance(title, str):
        raise DocValidationError("Doc title must be a string.")
    stripped = title.strip()
    if not stripped:
        raise DocValidationError("Doc title cannot be empty.")
    if len(stripped) > DOC_TITLE_MAX_LEN:
        raise DocValidationError(
            f"Doc title exceeds maximum length of {DOC_TITLE_MAX_LEN} characters."
        )
    return stripped


def _validate_description(description) -> str:
    """Return the stripped description (None -> ""), or raise."""
    if description is None:
        return ""
    if not isinstance(description, str):
        raise DocValidationError("Doc description must be a string.")
    stripped = description.strip()
    if len(stripped) > DOC_DESCRIPTION_MAX_LEN:
        raise DocValidationError(
            f"Doc description exceeds maximum length of "
            f"{DOC_DESCRIPTION_MAX_LEN} characters."
        )
    return stripped


def _validate_mode(mode) -> str:
    if mode not in DOC_MODES:
        raise DocValidationError(
            f"Invalid doc mode {mode!r}. Must be one of: {', '.join(DOC_MODES)}."
        )
    return mode


def _validate_permission(permission) -> str:
    if permission not in DOC_SHARE_PERMISSIONS:
        raise DocValidationError(
            f"Invalid share permission {permission!r}. Must be one of: "
            f"{', '.join(DOC_SHARE_PERMISSIONS)}."
        )
    return permission


def _validate_count(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DocValidationError(f"{label} must be a non-negative integer.")
    return value


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _share_to_dict(share: DocShare) -> dict:
    """Convert a DocShare ORM instance to the share dict shape."""
    return {
        "id": share.id,
        "user_id": share.user_id,
        "permission": share.permission,
        "created_at": _iso(share.created_at),
    }


def _doc_to_dict(doc: Doc, shares: Optional[Iterable[DocShare]] = None) -> dict:
    """Convert a Doc ORM instance to the doc dict.

    Shape (``scope`` is derived by callers: ``"project" if project_id else
    "user"``)::

        {"id", "owner_id", "project_id", "title", "description", "mode",
         "content_size", "asset_count", "last_write_source",
         "created_at", "updated_at",            # ISO strings
         "shares": [{"id", "user_id", "permission", "created_at"}]}

    The ``shares`` key is present only when ``shares`` is provided (an
    empty iterable yields ``[]``).
    """
    data = {
        "id": doc.id,
        "owner_id": doc.owner_id,
        "project_id": doc.project_id,
        "title": doc.title,
        "description": doc.description,
        "mode": doc.mode,
        "content_size": doc.content_size,
        "asset_count": doc.asset_count,
        "last_write_source": doc.last_write_source,
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
    }
    if shares is not None:
        data["shares"] = [_share_to_dict(s) for s in shares]
    return data


async def _title_taken(
    db,
    owner_id: int,
    project_id: Optional[str],
    title: str,
    exclude_doc_id: Optional[str] = None,
) -> bool:
    """True when another doc in ``(owner_id, project_id)`` uses ``title``.

    Check-then-insert: two simultaneous creates (or renames) with the same
    title can both pass, exactly like the skills store. The UI and the
    tools serialize per user in practice, so this is documented rather than
    guarded with a lock.

    Compared with ``str.casefold()`` in Python rather than SQL ``lower()``:
    SQLite's ``lower()`` folds ASCII only, which would let ``"Über"`` and
    ``"über"`` (or even two identical non-ASCII titles, depending on the
    side that gets folded) slip past the check.
    """
    stmt = select(Doc.id, Doc.title).where(Doc.owner_id == owner_id)
    if project_id is None:
        stmt = stmt.where(Doc.project_id.is_(None))
    else:
        stmt = stmt.where(Doc.project_id == project_id)
    if exclude_doc_id is not None:
        stmt = stmt.where(Doc.id != exclude_doc_id)
    wanted = title.casefold()
    result = await db.execute(stmt)
    return any(t.strip().casefold() == wanted for _id, t in result.all())


async def _load_shares(db, doc_ids: list[str]) -> dict[str, list[DocShare]]:
    """Return ``{doc_id: [DocShare, ...]}`` (ordered by share id) for ``doc_ids``."""
    by_doc: dict[str, list[DocShare]] = {d: [] for d in doc_ids}
    for i in range(0, len(doc_ids), _IN_CHUNK):
        chunk = doc_ids[i:i + _IN_CHUNK]
        result = await db.execute(
            select(DocShare)
            .where(DocShare.doc_id.in_(chunk))
            .order_by(DocShare.id.asc())
        )
        for share in result.scalars().all():
            by_doc.setdefault(share.doc_id, []).append(share)
    return by_doc


async def _doc_dict_with_shares(db, doc: Doc) -> dict:
    shares = (await _load_shares(db, [doc.id]))[doc.id]
    return _doc_to_dict(doc, shares)


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


async def create_doc(
    owner_id: int,
    title: str,
    description: str = "",
    *,
    mode: str,
    project_id: Optional[str] = None,
    content_size: int = 0,
    last_write_source: Optional[str] = None,
    doc_id: Optional[str] = None,
) -> dict:
    """Create a doc row.

    Args:
        owner_id: Owning user (the project owner for project docs).
        title: 1..200 chars after stripping; unique case-insensitively per
            ``(owner_id, project_id)``.
        description: 0..500 chars after stripping.
        mode: ``"private"`` or ``"public"`` (project docs: the caller passes
            the project's mode).
        project_id: None for a user doc.
        content_size: Bytes of the initial ``doc.md``.
        last_write_source: ``conversation:<id>`` / ``ui`` / ``action_request:<id>``.
        doc_id: Optional pre-generated id (lets the service lay out the
            directory first); a uuid4 is generated when omitted.

    Returns:
        The doc dict with ``shares: []``.

    Raises:
        DocValidationError: invalid title / description / mode / size.
        DuplicateDocTitleError: title collision in the same scope.
    """
    clean_title = _validate_title(title)
    clean_description = _validate_description(description)
    _validate_mode(mode)
    _validate_count(content_size, "content_size")

    async with AsyncSessionLocal() as db:
        if await _title_taken(db, owner_id, project_id, clean_title):
            raise DuplicateDocTitleError(
                f"A doc titled '{clean_title}' already exists"
                + (" in this project." if project_id else ".")
            )
        now = datetime.now(timezone.utc)
        doc = Doc(
            id=doc_id or str(uuid.uuid4()),
            owner_id=owner_id,
            project_id=project_id,
            title=clean_title,
            description=clean_description,
            mode=mode,
            content_size=content_size,
            asset_count=0,
            last_write_source=last_write_source,
            created_at=now,
            updated_at=now,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return _doc_to_dict(doc, [])


async def get_doc(doc_id: str, *, with_shares: bool = True) -> Optional[dict]:
    """Return the doc dict (with ``shares`` unless ``with_shares=False``), or None."""
    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return None
        if not with_shares:
            return _doc_to_dict(doc)
        return await _doc_dict_with_shares(db, doc)


async def list_accessible_docs(
    user_id: int,
    *,
    project_id: Optional[str] = None,
    include_user_docs: bool = True,
    include_project_docs: bool = False,
    mode: Optional[str] = None,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Docs the user owns or holds a share on, newest first, with shares.

    Candidates are docs where ``owner_id == user_id`` or a ``doc_shares``
    row exists for the user or for everyone (``user_id IS NULL``). This is
    the candidate set only: the caller still runs every row through
    ``chat.docs.access.resolve_doc_access`` (which hides, e.g., private
    docs from public conversations).

    Scope filter (OR-ed):
        - ``include_user_docs``: user docs (``project_id IS NULL``);
        - ``include_project_docs`` with ``project_id``: that project's docs.
      With neither, the result is empty.

    Args:
        user_id: The viewing user.
        project_id: Project whose docs to include (with include_project_docs).
        include_user_docs: Include user (non-project) docs.
        include_project_docs: Include ``project_id``'s docs.
        mode: Optional ``"private"`` / ``"public"`` filter. Public-project
            callers pass ``"public"`` so the SQL ``LIMIT`` applies to the
            set they can actually see instead of being eaten by hidden rows.
        limit: Optional row cap (callers fetch ``limit + 1`` for has_more).
        before: Optional keyset cursor ``(updated_at, id)``: only rows
            strictly after this pair under the ``(updated_at DESC, id DESC)``
            sort (same scheme as ``list_conversations_meta``).

    Returns:
        Doc dicts, each with ``shares`` (loaded in one extra query).
    """
    scope_conds = []
    if include_user_docs:
        scope_conds.append(Doc.project_id.is_(None))
    if include_project_docs and project_id:
        scope_conds.append(Doc.project_id == project_id)
    if not scope_conds:
        return []
    if mode is not None:
        _validate_mode(mode)

    share_exists = exists().where(
        DocShare.doc_id == Doc.id,
        or_(DocShare.user_id == user_id, DocShare.user_id.is_(None)),
    )

    async with AsyncSessionLocal() as db:
        stmt = select(Doc).where(
            or_(Doc.owner_id == user_id, share_exists),
            or_(*scope_conds),
        )
        if mode is not None:
            stmt = stmt.where(Doc.mode == mode)
        if before is not None:
            before_ts, before_id = before
            stmt = stmt.where(
                or_(
                    Doc.updated_at < before_ts,
                    and_(Doc.updated_at == before_ts, Doc.id < before_id),
                )
            )
        stmt = stmt.order_by(Doc.updated_at.desc(), Doc.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        docs = result.scalars().all()
        if not docs:
            return []
        shares = await _load_shares(db, [d.id for d in docs])
        return [_doc_to_dict(d, shares.get(d.id, [])) for d in docs]


async def update_doc_metadata(
    doc_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
) -> Optional[dict]:
    """Rename a doc and/or change its description.

    ``expected_updated_at`` is the optimistic-concurrency token: when given
    it must equal the row's current ``updated_at`` ISO string, else
    :class:`StaleDocError` (carrying the current row, with shares) is
    raised before anything changes. ``updated_at`` is bumped only when a
    field actually changes, so a no-op save never invalidates another
    editor's token (same rule as ``update_routine``).

    Returns:
        The updated doc dict with shares, or None when the doc does not exist.

    Raises:
        DocValidationError, DuplicateDocTitleError, StaleDocError.
    """
    clean_title = _validate_title(title) if title is not None else None
    clean_description = (
        _validate_description(description) if description is not None else None
    )

    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return None

        if expected_updated_at is not None and _iso(doc.updated_at) != expected_updated_at:
            raise StaleDocError(current=await _doc_dict_with_shares(db, doc))

        changed = False
        if clean_title is not None and clean_title != doc.title:
            if await _title_taken(
                db, doc.owner_id, doc.project_id, clean_title, exclude_doc_id=doc.id,
            ):
                raise DuplicateDocTitleError(
                    f"A doc titled '{clean_title}' already exists"
                    + (" in this project." if doc.project_id else ".")
                )
            doc.title = clean_title
            changed = True
        if clean_description is not None and clean_description != doc.description:
            doc.description = clean_description
            changed = True

        if changed:
            doc.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(doc)
        return await _doc_dict_with_shares(db, doc)


async def set_doc_mode(doc_id: str, mode: str) -> Optional[dict]:
    """Switch a doc between ``private`` and ``public``.

    Does NOT check project inheritance (project docs keep the project's
    mode) -- the route refuses that case before calling. Bumps
    ``updated_at`` when the mode changes.

    Returns:
        The doc dict with shares, or None when the doc does not exist.
    """
    _validate_mode(mode)
    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return None
        if doc.mode != mode:
            doc.mode = mode
            doc.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(doc)
        return await _doc_dict_with_shares(db, doc)


async def update_after_write(
    doc_id: str,
    *,
    content_size: int,
    asset_count: Optional[int] = None,
    last_write_source: Optional[str],
) -> Optional[dict]:
    """Record a landed body/asset write: counters, source, ``updated_at``.

    Always bumps ``updated_at`` (a write happened). ``asset_count`` is left
    unchanged when None.

    Returns:
        The doc dict with shares, or None when the doc does not exist.
    """
    _validate_count(content_size, "content_size")
    if asset_count is not None:
        _validate_count(asset_count, "asset_count")
    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return None
        doc.content_size = content_size
        if asset_count is not None:
            doc.asset_count = asset_count
        doc.last_write_source = last_write_source
        doc.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(doc)
        return await _doc_dict_with_shares(db, doc)


async def delete_doc(doc_id: str) -> bool:
    """Delete a doc row and its shares. Returns False when it did not exist.

    The directory is NOT touched; callers remove it with
    ``chat.docs.files.delete_doc_dir`` after this returns.
    """
    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return False
        # Explicit, so the shares go even on a connection without
        # PRAGMA foreign_keys (the FK cascade covers the normal case).
        await db.execute(delete(DocShare).where(DocShare.doc_id == doc_id))
        await db.delete(doc)
        await db.commit()
        return True


async def list_doc_ids_for_project(project_id: str) -> list[str]:
    """Ids of every doc in a project (collect BEFORE deleting the project,
    then sweep the directories with ``files.delete_doc_dir``)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Doc.id).where(Doc.project_id == project_id).order_by(Doc.id)
        )
        return list(result.scalars().all())


async def list_doc_ids_for_user(user_id: int) -> list[str]:
    """Ids of every doc the user owns, user and project docs alike (collect
    BEFORE deleting the account, then sweep the directories)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Doc.id).where(Doc.owner_id == user_id).order_by(Doc.id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


async def add_share(doc_id: str, user_id: Optional[int], permission: str) -> dict:
    """Grant ``permission`` on a doc to one user, or to everyone (None).

    Upsert semantics: an existing grant for the same recipient (including
    the single "everyone" row) has its permission updated instead of a
    second row being inserted. The "at most one everyone row per doc" rule
    lives here because SQLite treats NULLs as distinct in the unique
    ``(doc_id, user_id)`` index. Sharing a doc with its own owner is
    refused (the owner already has full access, and a share row would turn
    the owner's own writes into approval-gated ones).

    Does not bump the doc's ``updated_at`` (access changes are not content
    changes).

    Returns:
        The share dict ``{"id", "user_id", "permission", "created_at"}``.

    Raises:
        DocValidationError: bad permission, unknown doc, or owner recipient.
    """
    _validate_permission(permission)
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int)):
        raise DocValidationError("Share user_id must be an integer or None.")

    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            raise DocValidationError(doc_not_found_message(doc_id))
        if user_id is not None and user_id == doc.owner_id:
            raise DocValidationError("A doc cannot be shared with its owner.")

        stmt = select(DocShare).where(DocShare.doc_id == doc_id)
        if user_id is None:
            stmt = stmt.where(DocShare.user_id.is_(None))
        else:
            stmt = stmt.where(DocShare.user_id == user_id)
        existing = (await db.execute(stmt.order_by(DocShare.id))).scalars().first()

        if existing is not None:
            if existing.permission != permission:
                existing.permission = permission
                await db.commit()
                await db.refresh(existing)
            return _share_to_dict(existing)

        share = DocShare(
            doc_id=doc_id,
            user_id=user_id,
            permission=permission,
            created_at=datetime.now(timezone.utc),
        )
        db.add(share)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent add_share won the unique index (per-user row or
            # the partial "everyone" index): fall back to updating that row.
            await db.rollback()
            existing = (await db.execute(stmt.order_by(DocShare.id))).scalars().first()
            if existing is None:
                raise
            if existing.permission != permission:
                existing.permission = permission
                await db.commit()
                await db.refresh(existing)
            return _share_to_dict(existing)
        await db.refresh(share)
        return _share_to_dict(share)


async def remove_share(doc_id: str, share_id: int) -> bool:
    """Delete one share row of a doc. False when no such row on that doc."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(DocShare).where(
                DocShare.id == share_id, DocShare.doc_id == doc_id,
            )
        )
        await db.commit()
        return result.rowcount > 0


async def list_shares(doc_id: str) -> list[dict]:
    """All share rows of a doc, oldest first."""
    async with AsyncSessionLocal() as db:
        shares = (await _load_shares(db, [doc_id]))[doc_id]
        return [_share_to_dict(s) for s in shares]
