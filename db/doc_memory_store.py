"""Quest Doc memories data access layer (docs/architecture/doc-notes.md).

A doc's memories are short plain-text facts kept beside its body, newest
first, at most ``DOC_MAX_MEMORIES`` per doc. People add, edit and delete
them in the doc's Notes panel (owner and write-share recipients), or by
accepting a ``suggest_doc_note`` suggestion. This module owns every write
to ``doc_memories``: each landed change bumps ``docs.memories_version`` by
exactly one and resyncs the cached ``docs.memory_count`` in the same
transaction. It never touches ``docs.updated_at`` (the body's concurrency
token), ``last_write_source`` or ``instructions_version`` (the instructions
are db/doc_store.py's).

Transaction shape of every write (the public functions and the
``*_in_session`` helpers alike), so concurrent writers to one doc can
neither lose a version bump nor both pass the cap:

1. ``UPDATE docs SET memories_version = memories_version WHERE id =
   :doc_id``: a write that changes no value but takes SQLite's write lock
   (see ``db.user_store.bump_session_epoch``), so every later step runs
   serialized against other writers of the doc; rowcount 0 means the doc
   is gone;
2. the duplicate check (exact text, after stripping), then the cap check
   (create only: ``DOC_MAX_MEMORIES`` rows already);
3. the insert, update or delete;
4. ``UPDATE docs SET memories_version = memories_version + 1, memory_count
   = (SELECT count(*) FROM doc_memories WHERE doc_id = :doc_id)``.

Refusals, not-found and no-op updates stop before step 4, so they bump
nothing; the public functions then roll back.

No access checks: callers resolve ``chat.docs.access.resolve_doc_access()``
(and the UI's ``can_edit``) first. Same session pattern as
db/doc_store.py: each public function opens a fresh ``AsyncSessionLocal()``
(imported at module level so tests can monkeypatch it), timestamps are
written ``datetime.now(timezone.utc)`` and returned as the stored naive-UTC
ISO strings.
"""

from datetime import datetime, timezone
from typing import Optional
import re
import uuid

from sqlalchemy import delete, func, select, update

from chat.docs.constants import DOC_MAX_MEMORIES, DOC_MEMORY_MAX_CHARS
from db.engine import AsyncSessionLocal
from db.models import Doc, DocMemory


class InvalidMemoryError(ValueError):
    """Memory text that cannot be saved: not a string, empty after
    stripping, or longer than ``DOC_MEMORY_MAX_CHARS`` characters. Routes
    answer 400 ``code``."""

    code = "invalid_memory"


class DuplicateMemoryError(ValueError):
    """Another memory of the same doc already has exactly this text.

    ``memory_id`` is that memory's id. Routes answer 409 ``code``.
    """

    code = "duplicate_memory"

    def __init__(self, memory_id: str):
        super().__init__("This doc already has a memory with exactly this text.")
        self.memory_id = memory_id


class MemoriesCapReachedError(ValueError):
    """The doc already has ``DOC_MAX_MEMORIES`` memories. Routes answer 409
    ``code``."""

    code = "memories_cap_reached"

    def __init__(self):
        super().__init__(
            f"This doc already has the maximum of {DOC_MAX_MEMORIES} memories. "
            "Delete or edit one instead."
        )


# ---------------------------------------------------------------------------
# Content and source
# ---------------------------------------------------------------------------


def normalize_memory_content(content) -> str:
    """Return the memory text as stored: stripped, inner newlines kept.

    The cap counts characters, not bytes (the personal and project memory
    stores cap bytes, so their ``normalize_content`` does not apply here).

    Raises:
        InvalidMemoryError: not a string, empty after stripping, or more
            than ``DOC_MEMORY_MAX_CHARS`` characters.
    """
    if not isinstance(content, str):
        raise InvalidMemoryError("Memory content must be a string.")
    text = content.strip()
    if not text:
        raise InvalidMemoryError("Memory content cannot be empty.")
    if len(text) > DOC_MEMORY_MAX_CHARS:
        raise InvalidMemoryError(
            f"Memory content exceeds the maximum length of "
            f"{DOC_MEMORY_MAX_CHARS} characters."
        )
    return text


_UI_SOURCE_RE = re.compile(r"ui:[1-9][0-9]{0,17}")
_SUGGESTION_SOURCE_RE = re.compile(
    r"suggestion:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def validate_source(source) -> str:
    """Return ``source`` when it is ``ui:<user_id>`` or
    ``suggestion:<suggestion_id>`` (a lowercase uuid), else raise
    ValueError. A bad source is a programming error, not user input."""
    if not isinstance(source, str) or not (
        _UI_SOURCE_RE.fullmatch(source) or _SUGGESTION_SOURCE_RE.fullmatch(source)
    ):
        raise ValueError(
            f"Invalid doc memory source {source!r}: expected 'ui:<user_id>' "
            "or 'suggestion:<suggestion_id>'."
        )
    return source


def ui_source(user_id: int) -> str:
    """The ``source`` of a memory a person wrote in the Notes panel."""
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError(f"Invalid user id {user_id!r} for a doc memory source.")
    return validate_source(f"ui:{user_id}")


def suggestion_source(suggestion_id: str) -> str:
    """The ``source`` of a memory added by accepting a suggestion."""
    return validate_source(f"suggestion:{suggestion_id}")


# ---------------------------------------------------------------------------
# Serialization and transaction steps
# ---------------------------------------------------------------------------


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _to_dict(memory: DocMemory) -> dict:
    """``{"id", "doc_id", "content", "source", "created_by", "updated_by",
    "created_at", "updated_at"}`` (ISO strings)."""
    return {
        "id": memory.id,
        "doc_id": memory.doc_id,
        "content": memory.content,
        "source": memory.source,
        "created_by": memory.created_by,
        "updated_by": memory.updated_by,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
    }


async def _lock_doc(db, doc_id: str) -> bool:
    """Step 1: take the write lock with a write that changes nothing.
    False when the doc does not exist."""
    result = await db.execute(
        update(Doc)
        .where(Doc.id == doc_id)
        .values(memories_version=Doc.memories_version)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount > 0


async def _record_change(db, doc_id: str) -> None:
    """Step 4: bump ``memories_version`` by one and resync ``memory_count``."""
    await db.flush()
    count = (
        select(func.count())
        .select_from(DocMemory)
        .where(DocMemory.doc_id == doc_id)
        .scalar_subquery()
    )
    await db.execute(
        update(Doc)
        .where(Doc.id == doc_id)
        .values(memories_version=Doc.memories_version + 1, memory_count=count)
        .execution_options(synchronize_session=False)
    )


async def _find_duplicate(
    db, doc_id: str, content: str, *, exclude_id: Optional[str] = None,
) -> Optional[str]:
    """The id of a memory of ``doc_id`` whose text is exactly ``content``."""
    stmt = select(DocMemory.id).where(
        DocMemory.doc_id == doc_id, DocMemory.content == content,
    )
    if exclude_id is not None:
        stmt = stmt.where(DocMemory.id != exclude_id)
    stmt = stmt.order_by(DocMemory.created_at.asc(), DocMemory.id.asc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _get_memory(db, doc_id: str, memory_id: str) -> Optional[DocMemory]:
    result = await db.execute(
        select(DocMemory)
        .where(DocMemory.id == memory_id, DocMemory.doc_id == doc_id)
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Session-level helpers (one step of a caller's transaction)
# ---------------------------------------------------------------------------
#
# For callers that change a memory as one step of a larger transaction
# (accepting a doc note suggestion: claim the row, then insert or overwrite
# the memory). Contract: they run in the caller's open session and never
# commit or roll back; they normalize ``content`` themselves; they follow
# the four steps of the module docstring (step 1 is a cheap no-op when the
# caller already holds the write lock); a refusal raises having changed no
# value, and the caller rolls back as for any failed step; a landed change
# has bumped ``memories_version`` by exactly one and resynced
# ``memory_count`` when the helper returns.


async def insert_memory_in_session(
    db,
    doc_id: str,
    content: str,
    *,
    source: str,
    user_id: Optional[int],
    now: datetime,
) -> Optional[dict]:
    """Insert a memory in the caller's transaction (contract above).

    ``created_by`` = ``updated_by`` = ``user_id``; ``created_at`` =
    ``updated_at`` = ``now``.

    Returns:
        The memory dict, or None when the doc does not exist.

    Raises:
        InvalidMemoryError, DuplicateMemoryError, MemoriesCapReachedError;
        ValueError for a malformed ``source``.
    """
    clean = normalize_memory_content(content)
    validate_source(source)
    if not await _lock_doc(db, doc_id):
        return None
    duplicate = await _find_duplicate(db, doc_id, clean)
    if duplicate is not None:
        raise DuplicateMemoryError(duplicate)
    existing = (
        await db.execute(
            select(func.count())
            .select_from(DocMemory)
            .where(DocMemory.doc_id == doc_id)
        )
    ).scalar_one()
    if existing >= DOC_MAX_MEMORIES:
        raise MemoriesCapReachedError()
    memory = DocMemory(
        id=str(uuid.uuid4()),
        doc_id=doc_id,
        content=clean,
        source=source,
        created_by=user_id,
        updated_by=user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(memory)
    await _record_change(db, doc_id)
    # Read back the stored (naive-UTC) timestamps.
    await db.refresh(memory)
    return _to_dict(memory)


async def overwrite_memory_in_session(
    db,
    doc_id: str,
    memory_id: str,
    content: str,
    *,
    user_id: Optional[int],
    now: datetime,
) -> Optional[dict]:
    """Replace a memory's text in the caller's transaction (contract above).

    Sets ``updated_by`` = ``user_id`` and ``updated_at`` = ``now``; keeps
    ``source`` and ``created_*``. Identical text is a no-op: no bump, the
    memory returned unchanged.

    Returns:
        The memory dict, or None when the memory (or the doc) is gone or
        belongs to another doc -- an accept then inserts instead.

    Raises:
        InvalidMemoryError; DuplicateMemoryError when another memory of the
        doc already has this text.
    """
    memory, _changed = await _overwrite(
        db, doc_id, memory_id, content, user_id=user_id, now=now,
    )
    return memory


async def _overwrite(
    db, doc_id: str, memory_id: str, content: str, *,
    user_id: Optional[int], now: datetime,
) -> tuple[Optional[dict], bool]:
    """:func:`overwrite_memory_in_session` plus whether anything changed."""
    clean = normalize_memory_content(content)
    if not await _lock_doc(db, doc_id):
        return None, False
    memory = await _get_memory(db, doc_id, memory_id)
    if memory is None:
        return None, False
    if memory.content == clean:
        return _to_dict(memory), False
    duplicate = await _find_duplicate(db, doc_id, clean, exclude_id=memory.id)
    if duplicate is not None:
        raise DuplicateMemoryError(duplicate)
    memory.content = clean
    memory.updated_by = user_id
    memory.updated_at = now
    await _record_change(db, doc_id)
    await db.refresh(memory)
    return _to_dict(memory), True


# ---------------------------------------------------------------------------
# Public functions (own session, commit)
# ---------------------------------------------------------------------------


async def list_doc_memories(doc_id: str) -> list[dict]:
    """A doc's memories, newest first (``created_at DESC, id DESC``).

    At most ``DOC_MAX_MEMORIES`` rows, so no paging. Empty for a missing doc.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocMemory)
            .where(DocMemory.doc_id == doc_id)
            .order_by(DocMemory.created_at.desc(), DocMemory.id.desc())
        )
        return [_to_dict(m) for m in result.scalars().all()]


async def get_doc_memory(doc_id: str, memory_id: str) -> Optional[dict]:
    """One memory of ``doc_id``; None when missing or on another doc."""
    async with AsyncSessionLocal() as db:
        memory = await _get_memory(db, doc_id, memory_id)
        return _to_dict(memory) if memory is not None else None


async def create_doc_memory(
    doc_id: str,
    content: str,
    *,
    source: str,
    user_id: Optional[int],
) -> Optional[dict]:
    """Add a memory to a doc (see :func:`insert_memory_in_session`).

    Returns:
        The memory dict, or None when the doc does not exist.

    Raises:
        InvalidMemoryError, DuplicateMemoryError, MemoriesCapReachedError;
        ValueError for a malformed ``source`` (use :func:`ui_source` /
        :func:`suggestion_source`).
    """
    clean = normalize_memory_content(content)
    validate_source(source)
    async with AsyncSessionLocal() as db:
        memory = await insert_memory_in_session(
            db, doc_id, clean,
            source=source, user_id=user_id, now=datetime.now(timezone.utc),
        )
        if memory is None:
            await db.rollback()
            return None
        await db.commit()
        return memory


async def update_doc_memory(
    doc_id: str,
    memory_id: str,
    content: str,
    *,
    user_id: Optional[int],
) -> Optional[dict]:
    """Replace a memory's text; last write wins (no token).

    Identical text changes nothing (no bump, no ``updated_at`` change).
    Semantics in :func:`overwrite_memory_in_session`.

    Returns:
        The memory dict, or None when the memory is missing or belongs to
        another doc.

    Raises:
        InvalidMemoryError, DuplicateMemoryError.
    """
    clean = normalize_memory_content(content)
    async with AsyncSessionLocal() as db:
        memory, changed = await _overwrite(
            db, doc_id, memory_id, clean,
            user_id=user_id, now=datetime.now(timezone.utc),
        )
        if not changed:
            await db.rollback()
            return memory
        await db.commit()
        return memory


async def delete_doc_memory(doc_id: str, memory_id: str) -> bool:
    """Delete one memory of ``doc_id``. False when it is missing or belongs
    to another doc. Suggestions pointing at it keep their row (the foreign
    keys null ``replaces_memory_id`` / ``memory_id``)."""
    async with AsyncSessionLocal() as db:
        if not await _lock_doc(db, doc_id):
            await db.rollback()
            return False
        result = await db.execute(
            delete(DocMemory).where(
                DocMemory.id == memory_id, DocMemory.doc_id == doc_id,
            )
        )
        if result.rowcount == 0:
            await db.rollback()
            return False
        await _record_change(db, doc_id)
        await db.commit()
        return True
