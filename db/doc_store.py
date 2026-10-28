"""Quest Docs data access layer.

Async CRUD over the ``docs`` and ``doc_shares`` tables, returning plain
dicts (the shape documented on :func:`_doc_to_dict`). The doc body, assets
and revisions live on disk (chat/docs/files.py); this module only keeps the
metadata, counters, and shares.

Doc notes (docs/architecture/doc-notes.md) live on the same row: the
owner's ``instructions`` field and its ``instructions_version`` are written
here (:func:`update_doc_instructions`), the ``doc_memories`` rows and the
``memories_version`` / ``memory_count`` columns by db/doc_memory_store.py.
Notes writes never bump ``updated_at`` or ``last_write_source``, the way
share changes leave them alone. The instructions text is never part of the
doc dict (the column is deferred with raiseload on ``Doc``); doc dicts carry
only :data:`NOTES_STATE_KEYS`, and :func:`get_notes_state` (or
:func:`load_notes_state_in_session` inside a caller's transaction) loads
the text.

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
from collections.abc import Collection
import uuid

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from chat.docs.constants import (
    DOC_DESCRIPTION_MAX_LEN,
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MODES,
    DOC_SHARE_PERMISSIONS,
    DOC_TITLE_MAX_LEN,
    doc_not_found_message,
)
from db.engine import AsyncSessionLocal
from db.models import Doc, DocMemory, DocNoteSuggestion, DocShare

# Chunk size for ``IN (...)`` lookups, well under SQLite's bound-parameter cap.
_IN_CHUNK = 500

# The doc-notes keys of every doc dict: whether the doc has instructions,
# its memory count and both version counters -- enough to decide notes
# delivery and to show counts without a second query. Never the
# instructions text (:func:`get_notes_state` loads it). UI rows leave these
# keys out (chat/docs/routes.py ``_doc_row``); the tool rows build explicit
# dicts.
NOTES_STATE_KEYS = (
    "has_instructions", "memory_count", "instructions_version", "memories_version",
)


class DocValidationError(ValueError):
    """Invalid input to a doc store function (bad title, mode, permission...)."""


class DuplicateDocTitleError(ValueError):
    """Another doc of the same owner and scope already uses this title.

    Titles are unique case-insensitively per ``(owner_id, project_id, mode)``,
    where a NULL ``project_id`` (user doc) is its own scope.
    """


class ShareTargetGoneError(DocValidationError):
    """:func:`add_share` lost a race: the doc or the recipient user was
    deleted between the caller's checks and the insert (the foreign key
    refused the row). The caller re-checks which one is gone."""


class StaleDocError(ValueError):
    """``expected_updated_at`` did not match the row (concurrent write).

    Carries the current row so the route can return it in the flat 409
    ``stale_update`` body, sparing the client a second GET (mirrors
    ``db.routine_store.StaleRoutineError``).
    """

    def __init__(self, current: dict):
        super().__init__("Doc was modified by another writer")
        self.current = current


class InvalidInstructionsError(DocValidationError):
    """Instructions that cannot be saved: not a string, a NUL character, or
    longer than ``DOC_INSTRUCTIONS_MAX_CHARS`` characters after stripping;
    also an ``expected_version`` that is not an integer in ``0..2**63-1``.

    Routes answer 400 with ``code`` (``invalid_instructions``). Being a
    :class:`DocValidationError`, it would also be caught by the existing
    handlers for that class, which answer with their own code: a route must
    catch this class first, or answer with ``exc.code``.
    """

    code = "invalid_instructions"


class StaleInstructionsError(ValueError):
    """``expected_version`` did not match the row's ``instructions_version``
    (someone saved the instructions meanwhile).

    Carries the current notes state (the :func:`get_notes_state` dict) so
    the route can return ``current: {instructions, instructions_version}``
    in its 409 ``stale_instructions`` body, sparing the client a second GET
    (mirrors :class:`StaleDocError`).
    """

    code = "stale_instructions"

    def __init__(self, current: dict):
        super().__init__("Doc instructions were modified by another writer")
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


def normalize_instructions(text) -> str:
    """Return the instructions as stored: stripped, inner newlines kept;
    ``""`` means none.

    Raises:
        InvalidInstructionsError: not a string, a NUL character anywhere,
            or more than ``DOC_INSTRUCTIONS_MAX_CHARS`` characters (not
            bytes) after stripping.
    """
    if not isinstance(text, str):
        raise InvalidInstructionsError("Doc instructions must be a string.")
    if "\x00" in text:
        raise InvalidInstructionsError("Doc instructions cannot contain NUL characters.")
    stripped = text.strip()
    if len(stripped) > DOC_INSTRUCTIONS_MAX_CHARS:
        raise InvalidInstructionsError(
            f"Doc instructions exceed the maximum length of "
            f"{DOC_INSTRUCTIONS_MAX_CHARS} characters."
        )
    return stripped


# Bound of a SQLite INTEGER; a larger token would overflow in the driver.
_MAX_VERSION = 2**63


def _check_instructions_version(value) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < _MAX_VERSION
    ):
        raise InvalidInstructionsError(
            "expected_instructions_version must be a non-negative integer."
        )
    return value


def _as_utc(value: datetime) -> datetime:
    """``value`` as aware UTC; a naive value is taken as UTC already. SQLite
    stores the wall time without the offset, so a non-UTC offset must be
    converted before it is written."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
         "require_approval",                    # bool, owner's switch
         "has_instructions",                    # bool, non-empty instructions
         "memory_count",                        # int, doc_memories rows
         "instructions_version",                # int, notes version counters
         "memories_version",                    # int
         "created_at", "updated_at",            # ISO strings
         "shares": [{"id", "user_id", "permission", "created_at"}]}

    The ``shares`` key is present only when ``shares`` is provided (an
    empty iterable yields ``[]``). The four notes keys
    (:data:`NOTES_STATE_KEYS`) come from the row: ``has_instructions`` is
    the ``length(instructions) > 0`` column property every ``select(Doc)``
    carries, so the instructions text is never loaded here.
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
        "require_approval": bool(doc.require_approval),
        "has_instructions": bool(doc.has_instructions),
        "memory_count": int(doc.memory_count or 0),
        "instructions_version": int(doc.instructions_version or 0),
        "memories_version": int(doc.memories_version or 0),
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
    mode: str,
    title: str,
    exclude_doc_id: Optional[str] = None,
) -> bool:
    """True when another doc in ``(owner_id, project_id, mode)`` uses ``title``.

    Uniqueness is scoped per MODE on purpose (invariant 2 of the spec): a
    public conversation's ``create_doc(title=...)`` must never learn from
    the collision error whether a private doc of that title exists. Since
    user docs are always private (``create_doc`` refuses a public one) and
    project docs all carry their project's mode, a public conversation's
    creates only ever land in its public project's namespace and the mode
    key is a belt-and-braces no-op.

    Check-then-insert: two simultaneous creates (or renames) with the same
    title can both pass, exactly like the skills store. The UI and the
    tools serialize per user in practice, so this is documented rather than
    guarded with a lock.

    Compared with ``str.casefold()`` in Python rather than SQL ``lower()``:
    SQLite's ``lower()`` folds ASCII only, which would let ``"Über"`` and
    ``"über"`` (or even two identical non-ASCII titles, depending on the
    side that gets folded) slip past the check.
    """
    stmt = select(Doc.id, Doc.title).where(
        Doc.owner_id == owner_id, Doc.mode == mode,
    )
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


def _keyset_page(stmt, *, limit: Optional[int], before: Optional[tuple[datetime, str]]):
    """Apply the docs list order ``(updated_at DESC, id DESC)``, the keyset
    cursor (rows strictly after ``before``) and the optional ``limit``."""
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
    return stmt


async def _docs_with_shares(db, stmt) -> list[dict]:
    docs = (await db.execute(stmt)).scalars().all()
    if not docs:
        return []
    shares = await _load_shares(db, [d.id for d in docs])
    return [_doc_to_dict(d, shares.get(d.id, [])) for d in docs]


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
            ``(owner_id, project_id, mode)``.
        description: 0..500 chars after stripping.
        mode: ``"private"`` or ``"public"`` (project docs: the caller passes
            the project's mode). A user doc must be ``"private"``.
        project_id: None for a user doc.
        content_size: Bytes of the initial ``doc.md``.
        last_write_source: ``conversation:<id>`` / ``ui`` / ``action_request:<id>``.
        doc_id: Optional pre-generated id (lets the service lay out the
            directory first); a uuid4 is generated when omitted.

    Returns:
        The doc dict with ``shares: []``.

    Raises:
        DocValidationError: invalid title / description / mode / size, or
            a public user doc (user docs are always private).
        DuplicateDocTitleError: title collision in the same scope.
    """
    clean_title = _validate_title(title)
    clean_description = _validate_description(description)
    _validate_mode(mode)
    if project_id is None and mode != "private":
        raise DocValidationError(
            "User docs are always private; only project docs can be public."
        )
    _validate_count(content_size, "content_size")

    async with AsyncSessionLocal() as db:
        if await _title_taken(db, owner_id, project_id, mode, clean_title):
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
        if not with_shares:
            return _doc_to_dict(doc) if doc is not None else None
        # The shares query runs for a missing id too, so a hidden doc and a
        # nonexistent one cost the same two queries (invariant 2: same
        # error text, same timing class).
        shares = await _load_shares(db, [doc_id])
        if doc is None:
            return None
        return _doc_to_dict(doc, shares.get(doc_id, []))


async def list_accessible_docs(
    user_id: int,
    *,
    project_id: Optional[str] = None,
    include_user_docs: bool = True,
    include_project_docs: bool = False,
    include_owned_project_docs: bool = False,
    source_project_ids: Collection[str] = (),
    mode: Optional[str] = None,
    owned_only: bool = False,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Docs the user owns or holds a share on, newest first, with shares.

    Candidates are docs where ``owner_id == user_id`` or a ``doc_shares``
    row exists for the user or for everyone (``user_id IS NULL``) -- only
    the first with ``owned_only`` (the UI's "Your docs" list; docs shared
    with the user are :func:`list_docs_shared_with`). This is the candidate
    set only: the caller still runs every row through
    ``chat.docs.access.resolve_doc_access`` (which hides, e.g., private
    docs from public conversations).

    Scope filter (OR-ed):
        - ``include_user_docs``: user docs (``project_id IS NULL``);
        - ``include_project_docs`` with ``project_id``: that project's docs;
        - ``include_owned_project_docs``: the docs of every project the
          user owns (any mode) -- what a standalone conversation may read;
        - ``source_project_ids``: the public docs (``mode="public"``) of
          those projects -- a private project's doc sources, readable from
          its conversations.
      With none, the result is empty.

    Args:
        user_id: The viewing user.
        project_id: Project whose docs to include (with include_project_docs).
        include_user_docs: Include user (non-project) docs.
        include_project_docs: Include ``project_id``'s docs.
        include_owned_project_docs: Include the project docs the user owns.
        source_project_ids: Projects whose public docs to include.
        mode: Optional ``"private"`` / ``"public"`` filter. Public-project
            callers pass ``"public"`` so the SQL ``LIMIT`` applies to the
            set they can actually see instead of being eaten by hidden rows.
        owned_only: Only docs ``user_id`` owns (no share-based candidates).
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
    if include_owned_project_docs:
        scope_conds.append(and_(Doc.project_id.is_not(None), Doc.owner_id == user_id))
    if source_project_ids:
        scope_conds.append(
            and_(Doc.project_id.in_(list(source_project_ids)), Doc.mode == "public")
        )
    if not scope_conds:
        return []
    if mode is not None:
        _validate_mode(mode)

    if owned_only:
        relationship = Doc.owner_id == user_id
    else:
        share_exists = exists().where(
            DocShare.doc_id == Doc.id,
            or_(DocShare.user_id == user_id, DocShare.user_id.is_(None)),
        )
        relationship = or_(Doc.owner_id == user_id, share_exists)

    async with AsyncSessionLocal() as db:
        stmt = select(Doc).where(relationship, or_(*scope_conds))
        if mode is not None:
            stmt = stmt.where(Doc.mode == mode)
        stmt = _keyset_page(stmt, limit=limit, before=before)
        return await _docs_with_shares(db, stmt)


async def list_docs_shared_with(
    user_id: int,
    *,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Docs shared WITH the user that they do not own, newest first.

    Candidates: ``owner_id != user_id`` and a ``doc_shares`` row for the
    user or for everyone (``user_id IS NULL``); user AND project docs (the
    UI may list a shared project doc; conversations still never reach one
    from outside its project -- that is the access rule's business). Same
    order and keyset cursor as :func:`list_accessible_docs`; the caller
    still runs every row through the access rule and its own UI filters.

    Returns:
        Doc dicts, each with ``shares``.
    """
    share_exists = exists().where(
        DocShare.doc_id == Doc.id,
        or_(DocShare.user_id == user_id, DocShare.user_id.is_(None)),
    )
    async with AsyncSessionLocal() as db:
        stmt = select(Doc).where(Doc.owner_id != user_id, share_exists)
        stmt = _keyset_page(stmt, limit=limit, before=before)
        return await _docs_with_shares(db, stmt)


async def update_doc_metadata(
    doc_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    require_approval: Optional[bool] = None,
    expected_updated_at: Optional[str] = None,
) -> Optional[dict]:
    """Rename a doc, change its description and/or flip ``require_approval``.

    ``require_approval`` (a real bool; anything else raises
    :class:`DocValidationError`) is the owner's switch that turns every
    model-initiated write into a ``write_doc`` approval card (access rule
    7). Flipping it bumps ``updated_at`` like a rename does: open viewers
    re-fetch on the newer token and conversations resolve the new verdict
    on their next write (the access rule reads the live row).

    ``expected_updated_at`` is the optimistic-concurrency token: when given
    it must equal the row's current ``updated_at`` ISO string, else
    :class:`StaleDocError` (carrying the current row, with shares) is
    raised before anything changes. ``updated_at`` is bumped only when a
    field actually changes, so a no-op save never invalidates another
    editor's token (same rule as ``update_routine``).

    The doc's notes (instructions, memories, their versions) are not
    metadata here: they have their own writers (:func:`update_doc_instructions`,
    db/doc_memory_store.py) and are never changed by this function.

    Returns:
        The updated doc dict with shares, or None when the doc does not exist.

    Raises:
        DocValidationError, DuplicateDocTitleError, StaleDocError.
    """
    clean_title = _validate_title(title) if title is not None else None
    clean_description = (
        _validate_description(description) if description is not None else None
    )
    if require_approval is not None and not isinstance(require_approval, bool):
        raise DocValidationError("require_approval must be a boolean.")

    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return None

        if expected_updated_at is not None and _iso(doc.updated_at) != expected_updated_at:
            raise StaleDocError(current=await _doc_dict_with_shares(db, doc))

        changed = False
        if clean_title is not None and clean_title != doc.title:
            # A user doc is private whatever a legacy row says (the access
            # rule treats it so); check the namespace it actually lives in.
            effective_mode = "private" if doc.project_id is None else doc.mode
            if await _title_taken(
                db, doc.owner_id, doc.project_id, effective_mode, clean_title,
                exclude_doc_id=doc.id,
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
        if require_approval is not None and require_approval != bool(doc.require_approval):
            doc.require_approval = require_approval
            changed = True

        if changed:
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
    """Delete a doc row with its shares and notes. Returns False when it did
    not exist.

    The notes go with the row: the instructions are columns of it, and the
    doc's ``doc_note_suggestions`` and ``doc_memories`` rows are deleted
    here too. The directory is NOT touched; callers remove it with
    ``chat.docs.files.delete_doc_dir`` after this returns.
    """
    async with AsyncSessionLocal() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return False
        # Explicit, so the shares and notes go even on a connection without
        # PRAGMA foreign_keys (the FK cascade covers the normal case).
        # Suggestions first: they point at the memories.
        await db.execute(
            delete(DocNoteSuggestion).where(DocNoteSuggestion.doc_id == doc_id)
        )
        await db.execute(delete(DocMemory).where(DocMemory.doc_id == doc_id))
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


async def list_docs_for_project(project_id: str) -> list[dict]:
    """Every doc of a project as doc dicts WITH shares (collect BEFORE
    deleting the project: the ids drive the directory sweep, the shares the
    realtime audience -- both rows go with the project's CASCADE)."""
    async with AsyncSessionLocal() as db:
        stmt = select(Doc).where(Doc.project_id == project_id).order_by(Doc.id)
        return await _docs_with_shares(db, stmt)


async def list_doc_ids_for_user(user_id: int) -> list[str]:
    """Ids of every doc the user owns, user and project docs alike (collect
    BEFORE deleting the account, then sweep the directories)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Doc.id).where(Doc.owner_id == user_id).order_by(Doc.id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Notes: instructions (memories are db/doc_memory_store.py's)
# ---------------------------------------------------------------------------


async def load_notes_state_in_session(db, doc_id: str) -> Optional[dict]:
    """The :func:`get_notes_state` dict, read in the caller's session.

    The way a caller reads the exact versions its own writes produced
    inside its transaction: after :func:`write_instructions_in_session` or
    a ``db.doc_memory_store`` ``*_in_session`` helper, this read runs under
    the transaction's write lock, so no other writer can have moved the
    counters in between. Never read the counters from a ``Doc`` loaded in
    the same session: the notes writers change them with SQL-side
    ``UPDATE``s that do not refresh loaded objects.
    """
    row = (
        await db.execute(
            select(
                Doc.id,
                Doc.instructions,
                Doc.instructions_version,
                Doc.instructions_updated_at,
                Doc.instructions_updated_by,
                Doc.memories_version,
                Doc.memory_count,
            ).where(Doc.id == doc_id)
        )
    ).first()
    if row is None:
        return None
    instructions = row.instructions or ""
    return {
        "doc_id": row.id,
        "instructions": instructions,
        "has_instructions": instructions != "",
        "instructions_version": int(row.instructions_version or 0),
        "instructions_updated_at": _iso(row.instructions_updated_at),
        "instructions_updated_by": row.instructions_updated_by,
        "memories_version": int(row.memories_version or 0),
        "memory_count": int(row.memory_count or 0),
    }


async def get_notes_state(doc_id: str) -> Optional[dict]:
    """The doc's instructions and notes counters, or None for a missing doc.

    Shape::

        {"doc_id", "instructions",              # "" means none
         "has_instructions",                    # bool, instructions != ""
         "instructions_version",
         "instructions_updated_at",             # ISO string or None
         "instructions_updated_by",             # user id or None
         "memories_version", "memory_count"}

    It carries every :data:`NOTES_STATE_KEYS` key with the doc dict's
    meaning, so a consumer can take either dict. With
    :func:`load_notes_state_in_session` (its in-session form), the only
    read that loads the instructions text. No access checks: callers
    resolve ``chat.docs.access.resolve_doc_access`` first, as for every
    function here.
    """
    async with AsyncSessionLocal() as db:
        return await load_notes_state_in_session(db, doc_id)


async def write_instructions_in_session(
    db,
    doc_id: str,
    text: str,
    *,
    expected_version: int,
    user_id: Optional[int],
    now: datetime,
) -> Optional[dict]:
    """Save the instructions inside the caller's transaction; never commits.

    The core of :func:`update_doc_instructions`, for callers that write the
    instructions as one step of a larger transaction (accepting an
    instructions suggestion). It normalizes ``text`` itself
    (:func:`normalize_instructions`, idempotent on stored text), so callers
    may pass raw input.

    One compare-and-swap ``UPDATE docs ... WHERE id = :doc_id AND
    instructions_version = :expected_version AND instructions != :text``
    writes the text, ``instructions_version + 1`` (SQL-side),
    ``instructions_updated_at = now`` (converted to UTC; a naive value is
    taken as UTC) and ``instructions_updated_by = user_id``, so two saves
    holding the same token can never both land. The state is then read
    back in the same transaction, under the UPDATE's write lock, to tell
    the outcomes apart. Identical text is a no-op (no bump, no timestamp
    change). ``""`` clears the instructions, which is a change like any
    other. ``updated_at``, ``last_write_source`` and ``memories_version``
    are never touched.

    The UPDATE is SQL-side (``synchronize_session=False``): a ``Doc`` the
    caller loaded in the same session keeps its old counters. Read the
    versions from the returned dict or :func:`load_notes_state_in_session`,
    never from a loaded ``Doc``.

    ``InvalidInstructionsError`` is raised before any SQL runs.
    ``StaleInstructionsError`` is raised after the UPDATE changed nothing
    but opened a write transaction; the caller rolls back (closing the
    session does), as for any failed step.

    Returns:
        The :func:`get_notes_state` dict after the write (or the unchanged
        state for a no-op), or None when the doc does not exist.

    Raises:
        InvalidInstructionsError: invalid text or ``expected_version``.
        StaleInstructionsError: ``expected_version`` is not the row's
            ``instructions_version`` (carries the current state).
    """
    clean = normalize_instructions(text)
    _check_instructions_version(expected_version)
    result = await db.execute(
        update(Doc)
        .where(
            Doc.id == doc_id,
            Doc.instructions_version == expected_version,
            Doc.instructions != clean,
        )
        .values(
            instructions=clean,
            instructions_version=Doc.instructions_version + 1,
            instructions_updated_at=_as_utc(now),
            instructions_updated_by=user_id,
        )
        .execution_options(synchronize_session=False)
    )
    state = await load_notes_state_in_session(db, doc_id)
    if state is None:
        return None
    if result.rowcount == 0 and state["instructions_version"] != expected_version:
        raise StaleInstructionsError(current=state)
    return state


async def update_doc_instructions(
    doc_id: str,
    text: str,
    *,
    expected_version: int,
    user_id: Optional[int],
) -> Optional[dict]:
    """Save the doc's instructions (the UI's owner-only editor).

    ``text`` is plain text, stripped, newlines kept, at most
    ``DOC_INSTRUCTIONS_MAX_CHARS`` characters; ``""`` clears.
    ``expected_version`` is the optimistic-concurrency token: the
    ``instructions_version`` the editor started from. A change bumps
    ``instructions_version`` and records ``instructions_updated_at`` /
    ``instructions_updated_by``; identical text changes nothing. The body's
    ``updated_at`` and ``last_write_source`` are never touched. No access
    checks (callers check ownership first). Semantics in
    :func:`write_instructions_in_session`, which this wraps in its own
    session and commits.

    Returns:
        The new :func:`get_notes_state` dict, or None when the doc does
        not exist.

    Raises:
        InvalidInstructionsError, StaleInstructionsError.
    """
    clean = normalize_instructions(text)
    _check_instructions_version(expected_version)
    async with AsyncSessionLocal() as db:
        state = await write_instructions_in_session(
            db, doc_id, clean,
            expected_version=expected_version,
            user_id=user_id,
            now=datetime.now(timezone.utc),
        )
        if state is None:
            await db.rollback()
            return None
        await db.commit()
        return state


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
        ShareTargetGoneError: (a DocValidationError) the doc or the
            recipient was deleted while the row was being inserted.
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
        except IntegrityError as exc:
            # A concurrent add_share won the unique index (per-user row or
            # the partial "everyone" index): fall back to updating that row.
            await db.rollback()
            existing = (await db.execute(stmt.order_by(DocShare.id))).scalars().first()
            if existing is None:
                # No competing row, so a foreign key refused the insert:
                # the doc or the recipient was deleted meanwhile.
                raise ShareTargetGoneError(
                    "The doc or the share recipient no longer exists."
                ) from exc
            if existing.permission != permission:
                existing.permission = permission
                await db.commit()
                await db.refresh(existing)
            return _share_to_dict(existing)
        await db.refresh(share)
        return _share_to_dict(share)


async def remove_share(doc_id: str, share_id: int) -> bool:
    """Delete one share row of a doc. False when no such row on that doc.

    Like :func:`add_share`, does not bump the doc's ``updated_at``."""
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


async def list_all_doc_rows() -> list[dict]:
    """Every doc's metadata for the admin Docs usage report, no bodies.

    One narrow row per ``docs`` row across all owners: ``id``, ``owner_id``,
    ``project_id``, ``mode``, ``content_size``, ``asset_count``,
    ``last_write_source``, ``share_count`` (grants on the doc, the everyone
    row included) and naive-UTC ``created_at`` / ``updated_at`` as stored.
    Titles and descriptions are not returned -- the report counts and
    sizes, it never lists what people wrote. Folded per owner by
    ``build_docs_report()`` in chat/feature_usage_report.py.
    """
    share_counts = (
        select(DocShare.doc_id, func.count(DocShare.id).label("share_count"))
        .group_by(DocShare.doc_id)
        .subquery()
    )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Doc.id,
                Doc.owner_id,
                Doc.project_id,
                Doc.mode,
                Doc.content_size,
                Doc.asset_count,
                Doc.last_write_source,
                Doc.created_at,
                Doc.updated_at,
                func.coalesce(share_counts.c.share_count, 0),
            )
            .outerjoin(share_counts, share_counts.c.doc_id == Doc.id)
        )
        return [
            {
                "id": doc_id,
                "owner_id": owner_id,
                "project_id": project_id,
                "mode": mode,
                "content_size": int(content_size or 0),
                "asset_count": int(asset_count or 0),
                "last_write_source": last_write_source,
                "share_count": int(share_count or 0),
                "created_at": created_at,
                "updated_at": updated_at,
            }
            for (
                doc_id, owner_id, project_id, mode, content_size, asset_count,
                last_write_source, created_at, updated_at, share_count,
            ) in result.all()
        ]
