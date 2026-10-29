"""Doc note suggestion data access layer (docs/architecture/doc-notes.md).

Rows in ``doc_note_suggestions``: changes to a Quest Doc's notes that a
model proposed through the non-blocking ``suggest_doc_note`` tool, waiting
for a person. The reviewer is ``user_id``, the proposing conversation's
user; every function here except the cross-user reports
(:func:`list_pending_summaries`, :func:`list_pending_pairs_for_docs`,
:func:`list_all_suggestion_rows`) is scoped to that user. This is the
doc-notes twin of db/project_memory_suggestion_store.py and shares its
lifecycle (``pending`` -> ``accepted`` | ``dismissed``), its
``SuggestionAlreadyResolvedError`` and its resolved-list keyset cursor
(db/memory_suggestion_store.py), so the inbox pages every suggestion table
with one cursor.

Two kinds (``DOC_NOTE_SUGGESTION_KINDS``):

- ``memory``: one fact. Accepting adds a ``doc_memories`` row, or, with
  ``replaces_memory_id``, overwrites that memory in place (inserting when
  it is gone by then), and records the memory in ``memory_id``.
- ``instructions``: the complete proposed text of ``docs.instructions``,
  written against ``base_instructions_version``. Accepting replaces the
  field behind an ``expected_instructions_version`` token.

Supersede rule: a user has at most one pending instructions suggestion per
doc, so they never review two competing full texts. :func:`create_suggestion`
enforces it in its own transaction. Its first write is a no-op ``UPDATE`` of
the doc row, which takes SQLite's write lock as db/doc_memory_store.py's
writers do (step 1 of its module docstring), so the replaced-memory check,
the UPDATE that dismisses the user's older pending instructions suggestions
for the doc (from any of their conversations) and the insert run serialized
against every other notes writer of the doc: two concurrent creates for one
user and doc never both leave a pending row, and a memory deleted meanwhile
cannot slip between the check and the insert.

Accept is one transaction, all or nothing: claim the row first (``pending``
-> ``accepted``, which takes the write lock), then make the notes write
through the notes stores' session helpers (``db.doc_memory_store.*_in_session``,
``db.doc_store.write_instructions_in_session``), which never commit and
raise having changed nothing. Any refusal rolls the whole transaction back,
the claim included, so the row stays pending and the notes are unchanged.
The notes versions are read with ``db.doc_store.load_notes_state_in_session``
only, never from a loaded ``Doc`` (the notes writers bump them with SQL-side
UPDATEs that do not refresh loaded objects). Every returned dict is built
inside the transaction, before the commit, so a delete landing right after
it cannot turn a landed change into an error.

Content is normalized by the notes stores' own functions
(``doc_memory_store.normalize_memory_content``,
``doc_store.normalize_instructions``), not the twins' ``normalize_content``:
the doc caps (``DOC_MEMORY_MAX_CHARS``, ``DOC_INSTRUCTIONS_MAX_CHARS``)
count characters, not bytes, and instructions may be empty (a proposal to
clear them).

No access checks: chat/docs/note_suggestions.py resolves the doc's access,
ownership and the pending caps before calling in. Same session pattern as
db/doc_store.py: each public function opens a fresh ``AsyncSessionLocal()``
(imported at module level so tests can monkeypatch it) and commits its own
work; timestamps are written ``datetime.now(timezone.utc)`` and returned as
the stored naive-UTC ISO strings.
"""

from datetime import datetime, timezone
from typing import Iterable, Optional
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from chat.docs.constants import DOC_NOTE_SUGGESTION_KINDS
from db import doc_memory_store, doc_store
from db.doc_store import _IN_CHUNK
from db.engine import AsyncSessionLocal
from db.memory_suggestion_store import (
    RESOLVED_STATUSES,
    SuggestionAlreadyResolvedError,
    _normalize_reasoning,
    resolved_keyset_filter,
)
from db.models import Doc, DocMemory, DocNoteSuggestion, MemorySuggestionStatus

__all__ = [
    "SuggestionAlreadyResolvedError",
    "SuggestionTargetGoneError",
    "accept_suggestion",
    "count_pending",
    "count_pending_for_doc",
    "count_pending_for_user",
    "create_suggestion",
    "dismiss_suggestion",
    "find_pending_with_content",
    "get_suggestion",
    "list_all_suggestion_rows",
    "list_pending",
    "list_pending_for_doc",
    "list_pending_for_user",
    "list_pending_pairs_for_docs",
    "list_pending_summaries",
    "list_resolved_for_user",
    "normalize_suggestion_content",
]

KIND_INSTRUCTIONS = "instructions"
KIND_MEMORY = "memory"


class SuggestionTargetGoneError(LookupError):
    """:func:`create_suggestion` found its doc gone, or the insert's foreign
    key refused the row because the doc or the conversation was deleted
    after the caller's checks. Nothing is written; the caller reports the
    doc as not found."""

# Bound of a SQLite INTEGER (as db/doc_store.py's instructions token).
_MAX_VERSION = 2**63


# ---------------------------------------------------------------------------
# Validation and serialization
# ---------------------------------------------------------------------------


def normalize_suggestion_content(kind: str, content) -> str:
    """Return the suggestion text as stored, normalized for its kind.

    ``memory``: ``doc_memory_store.normalize_memory_content`` (stripped,
    1..``DOC_MEMORY_MAX_CHARS`` characters). ``instructions``:
    ``doc_store.normalize_instructions`` (stripped,
    0..``DOC_INSTRUCTIONS_MAX_CHARS`` characters; ``""`` proposes clearing
    the field).

    Raises:
        ValueError: ``kind`` is not one of ``DOC_NOTE_SUGGESTION_KINDS``.
        InvalidMemoryError: invalid memory text (code ``invalid_memory``).
        InvalidInstructionsError: invalid instructions text (code
            ``invalid_instructions``).
    """
    if kind not in DOC_NOTE_SUGGESTION_KINDS:
        raise ValueError(
            f"Invalid doc note suggestion kind {kind!r}. Must be one of: "
            f"{', '.join(DOC_NOTE_SUGGESTION_KINDS)}."
        )
    if kind == KIND_MEMORY:
        return doc_memory_store.normalize_memory_content(content)
    return doc_store.normalize_instructions(content)


def _is_version(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value < _MAX_VERSION
    )


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _to_dict(row: DocNoteSuggestion) -> dict:
    """``{"id", "user_id", "doc_id", "conversation_id", "kind", "content",
    "reasoning", "base_instructions_version", "replaces_memory_id",
    "status", "memory_id", "created_at", "resolved_at"}`` (ISO strings,
    naive UTC as stored)."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "doc_id": row.doc_id,
        "conversation_id": row.conversation_id,
        "kind": row.kind,
        "content": row.content,
        "reasoning": row.reasoning,
        "base_instructions_version": row.base_instructions_version,
        "replaces_memory_id": row.replaces_memory_id,
        "status": str(row.status),
        "memory_id": row.memory_id,
        "created_at": _iso(row.created_at),
        "resolved_at": _iso(row.resolved_at),
    }


def _oldest_first(stmt):
    return stmt.order_by(
        DocNoteSuggestion.created_at.asc(), DocNoteSuggestion.id.asc(),
    )


async def _pending_rows(*where) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(_oldest_first(
            select(DocNoteSuggestion).where(
                DocNoteSuggestion.status == MemorySuggestionStatus.PENDING, *where,
            )
        ))
        return [_to_dict(row) for row in result.scalars().all()]


async def _count_pending(*where) -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count())
            .select_from(DocNoteSuggestion)
            .where(DocNoteSuggestion.status == MemorySuggestionStatus.PENDING, *where)
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_suggestion(
    user_id: int,
    doc_id: str,
    conversation_id: str,
    kind: str,
    content,
    *,
    reasoning=None,
    base_instructions_version: Optional[int] = None,
    replaces_memory_id: Optional[str] = None,
) -> tuple[dict, list[dict]]:
    """Register a pending suggestion, superseding for the instructions kind.

    ``content`` and ``reasoning`` are normalized here
    (:func:`normalize_suggestion_content`; reasoning collapsed to one line
    and cut, like the twins). A blank ``replaces_memory_id`` counts as
    none. Kind ``instructions`` needs ``base_instructions_version`` (the
    ``instructions_version`` the proposal was written against) and takes no
    ``replaces_memory_id``; kind ``memory`` takes no
    ``base_instructions_version``, and its ``replaces_memory_id`` must name
    a memory of this doc.

    The transaction first takes the doc's write lock (module docstring);
    then, for kind ``instructions``, every pending instructions suggestion
    of this user for this doc (from any of their conversations) is marked
    ``dismissed`` with ``resolved_at`` = now, before the insert. Memory
    suggestions never supersede.

    Returns:
        ``(new_row, superseded_rows)``: the new row dict and the superseded
        rows as dicts after the update, oldest first (always ``[]`` for a
        memory).

    Raises:
        ValueError: unknown kind; a missing or invalid
            ``base_instructions_version`` on an instructions suggestion, or
            one given for a memory; ``replaces_memory_id`` on an
            instructions suggestion; a ``replaces_memory_id`` that is not a
            memory of this doc. Nothing is written.
        InvalidMemoryError, InvalidInstructionsError: invalid content.
        SuggestionTargetGoneError: the doc or the conversation is gone.
    """
    content = normalize_suggestion_content(kind, content)
    reasoning = _normalize_reasoning(reasoning)
    if replaces_memory_id is not None and not isinstance(replaces_memory_id, str):
        raise ValueError("replaces_memory_id must be a string.")
    replaces_memory_id = (replaces_memory_id or "").strip() or None
    if kind == KIND_INSTRUCTIONS:
        if replaces_memory_id is not None:
            raise ValueError(
                "replaces_memory_id applies to memory suggestions only."
            )
        if not _is_version(base_instructions_version):
            raise ValueError(
                "An instructions suggestion needs base_instructions_version, "
                "a non-negative integer."
            )
    elif base_instructions_version is not None:
        raise ValueError(
            "base_instructions_version applies to instructions suggestions only."
        )

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        try:
            locked = await db.execute(
                update(Doc)
                .where(Doc.id == doc_id)
                .values(memories_version=Doc.memories_version)
                .execution_options(synchronize_session=False)
            )
            if locked.rowcount == 0:
                raise SuggestionTargetGoneError(f"Doc {doc_id} is gone.")
            superseded_ids: list[str] = []
            if kind == KIND_INSTRUCTIONS:
                result = await db.execute(
                    update(DocNoteSuggestion)
                    .where(
                        DocNoteSuggestion.user_id == user_id,
                        DocNoteSuggestion.doc_id == doc_id,
                        DocNoteSuggestion.kind == KIND_INSTRUCTIONS,
                        DocNoteSuggestion.status == MemorySuggestionStatus.PENDING,
                    )
                    .values(status=MemorySuggestionStatus.DISMISSED, resolved_at=now)
                    .returning(DocNoteSuggestion.id)
                    .execution_options(synchronize_session=False)
                )
                superseded_ids = list(result.scalars().all())
            elif replaces_memory_id is not None:
                replaced_doc = (
                    await db.execute(
                        select(DocMemory.doc_id).where(DocMemory.id == replaces_memory_id)
                    )
                ).scalar_one_or_none()
                if replaced_doc != doc_id:
                    raise ValueError(
                        f"replaces_memory_id {replaces_memory_id!r} is not a "
                        "memory of this doc."
                    )
            row = DocNoteSuggestion(
                id=str(uuid.uuid4()),
                user_id=user_id,
                doc_id=doc_id,
                conversation_id=conversation_id,
                kind=kind,
                content=content,
                reasoning=reasoning,
                base_instructions_version=(
                    base_instructions_version if kind == KIND_INSTRUCTIONS else None
                ),
                replaces_memory_id=replaces_memory_id,
                status=MemorySuggestionStatus.PENDING,
                created_at=now,
            )
            db.add(row)
            try:
                await db.flush()
            except IntegrityError as exc:
                raise SuggestionTargetGoneError(
                    "The doc or the conversation was deleted."
                ) from exc
            await db.refresh(row)
            superseded: list[dict] = []
            if superseded_ids:
                result = await db.execute(_oldest_first(
                    select(DocNoteSuggestion).where(
                        DocNoteSuggestion.id.in_(superseded_ids)
                    )
                ))
                superseded = [_to_dict(r) for r in result.scalars().all()]
            created = _to_dict(row)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return created, superseded


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def get_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    """One suggestion of the user, any status; None when missing or another
    user's."""
    async with AsyncSessionLocal() as db:
        row = await db.get(DocNoteSuggestion, suggestion_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_dict(row)


async def list_pending(user_id: int, conversation_id: str) -> list[dict]:
    """Pending suggestions of one conversation, oldest first (the composer
    bubble)."""
    return await _pending_rows(
        DocNoteSuggestion.user_id == user_id,
        DocNoteSuggestion.conversation_id == conversation_id,
    )


async def count_pending(user_id: int, conversation_id: str) -> int:
    """Pending suggestions of one conversation."""
    return await _count_pending(
        DocNoteSuggestion.user_id == user_id,
        DocNoteSuggestion.conversation_id == conversation_id,
    )


async def list_pending_for_doc(user_id: int, doc_id: str) -> list[dict]:
    """The user's pending suggestions for one doc, from any of their
    conversations, oldest first (the Notes panel's review block). Never
    another user's rows."""
    return await _pending_rows(
        DocNoteSuggestion.user_id == user_id,
        DocNoteSuggestion.doc_id == doc_id,
    )


async def count_pending_for_doc(user_id: int, doc_id: str) -> int:
    """The user's pending suggestions for one doc (the per-doc cap counts
    per reviewing user)."""
    return await _count_pending(
        DocNoteSuggestion.user_id == user_id,
        DocNoteSuggestion.doc_id == doc_id,
    )


async def list_pending_for_user(user_id: int) -> list[dict]:
    """Every pending suggestion of the user, oldest first (the inbox's open
    list)."""
    return await _pending_rows(DocNoteSuggestion.user_id == user_id)


async def count_pending_for_user(user_id: int) -> int:
    """Pending suggestions across all of the user's conversations."""
    return await _count_pending(DocNoteSuggestion.user_id == user_id)


async def list_resolved_for_user(
    user_id: int, limit: int, cursor: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """One keyset page of the user's accepted / dismissed suggestions,
    most recently resolved first (``resolved_at DESC, id DESC``); same
    contract as ``memory_suggestion_store.list_resolved_for_user``:
    ``cursor`` (from ``decode_resolved_cursor``) continues after that row.
    At most ``limit`` rows."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(DocNoteSuggestion)
            .where(
                DocNoteSuggestion.user_id == user_id,
                DocNoteSuggestion.status.in_(RESOLVED_STATUSES),
            )
            .order_by(
                DocNoteSuggestion.resolved_at.desc(), DocNoteSuggestion.id.desc(),
            )
            .limit(limit)
        )
        keyset = resolved_keyset_filter(DocNoteSuggestion, cursor)
        if keyset is not None:
            stmt = stmt.where(keyset)
        result = await db.execute(stmt)
        return [_to_dict(row) for row in result.scalars().all()]


async def list_pending_summaries() -> list[dict]:
    """One row per user with pending doc note suggestions, across all
    users: ``[{"user_id", "pending_count", "oldest_created_at"}]``
    (naive-UTC datetime as stored); same shape as
    ``memory_suggestion_store.list_pending_summaries``."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                DocNoteSuggestion.user_id,
                func.count(DocNoteSuggestion.id),
                func.min(DocNoteSuggestion.created_at),
            )
            .where(DocNoteSuggestion.status == MemorySuggestionStatus.PENDING)
            .group_by(DocNoteSuggestion.user_id)
            .order_by(DocNoteSuggestion.user_id)
        )
        return [
            {"user_id": user_id, "pending_count": count, "oldest_created_at": oldest}
            for user_id, count, oldest in result.all()
        ]


async def find_pending_with_content(
    user_id: int, doc_id: str, kind: str, content: str,
) -> Optional[dict]:
    """A pending suggestion of this user, doc and kind with exactly this
    text (already normalized), from any of the user's conversations (the
    ``already_suggested`` guard). The oldest one when several match."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(_oldest_first(
            select(DocNoteSuggestion).where(
                DocNoteSuggestion.user_id == user_id,
                DocNoteSuggestion.doc_id == doc_id,
                DocNoteSuggestion.kind == kind,
                DocNoteSuggestion.status == MemorySuggestionStatus.PENDING,
                DocNoteSuggestion.content == content,
            )
        ).limit(1))
        row = result.scalars().first()
        return _to_dict(row) if row is not None else None


async def list_pending_pairs_for_docs(doc_ids: Iterable[str]) -> list[tuple[int, str]]:
    """The distinct ``(user_id, conversation_id)`` pairs of the pending
    suggestions on these docs, sorted.

    The delete paths call it BEFORE deleting the docs (the rows go with
    them) to learn whose bubbles and badges must hear about it. ``IN``
    lookups are chunked like db/doc_store.py's. Empty input runs no query.
    """
    ids = list(dict.fromkeys(doc_ids))
    if not ids:
        return []
    pairs: set[tuple[int, str]] = set()
    async with AsyncSessionLocal() as db:
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i:i + _IN_CHUNK]
            result = await db.execute(
                select(DocNoteSuggestion.user_id, DocNoteSuggestion.conversation_id)
                .where(
                    DocNoteSuggestion.doc_id.in_(chunk),
                    DocNoteSuggestion.status == MemorySuggestionStatus.PENDING,
                )
                .distinct()
            )
            pairs.update((int(user_id), cid) for user_id, cid in result.all())
    return sorted(pairs)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


async def accept_suggestion(
    user_id: int,
    suggestion_id: str,
    *,
    content: Optional[str] = None,
    expected_instructions_version: Optional[int] = None,
) -> Optional[dict]:
    """Accept a pending suggestion and apply it to the doc's notes, in one
    transaction (module docstring).

    ``user_id`` is the accepting (= reviewing) user: the memory's
    ``created_by`` / ``updated_by`` and the doc's
    ``instructions_updated_by``. ``content`` overrides the proposed text
    (the reviewer edited it; ``""`` clears the instructions). Steps:

    1. load the row fresh; another user's or a missing row returns None, a
       resolved one raises SuggestionAlreadyResolvedError;
    2. claim it (``pending`` -> ``accepted``, ``resolved_at`` = now): the
       first write, so it takes the write lock; a concurrent accept or
       dismiss that won first makes this raise
       SuggestionAlreadyResolvedError("resolved");
    3. ``notes_before`` = the doc's notes state, under the lock;
    4. memory kind: overwrite ``replaces_memory_id`` in place, or insert
       (source ``suggestion:<id>``) when there is none or it is gone, then
       record the memory in ``memory_id``. Instructions kind: write the
       field behind ``expected_instructions_version`` (default: the row's
       ``base_instructions_version``, the version the proposal was written
       against);
    5. ``notes_after`` = the state after the write; commit.

    Every write uses one ``now``. On any refusal the transaction rolls back,
    the claim included: the row stays pending and the notes are unchanged.

    Returns:
        ``{"suggestion": row_dict, "memory": memory_dict_or_None,
        "notes_before": state, "notes_after": state}`` (states are
        ``doc_store.get_notes_state()`` dicts), or None when the suggestion
        is not the user's, is missing, or its doc is gone.

    Raises:
        SuggestionAlreadyResolvedError: the row is no longer pending.
        InvalidMemoryError, DuplicateMemoryError, MemoriesCapReachedError:
            memory kind (the cap does not apply to an in-place replace).
        InvalidInstructionsError: invalid text or
            ``expected_instructions_version``.
        StaleInstructionsError: the field moved past the expected version
            (carries ``.current``).
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(DocNoteSuggestion, suggestion_id, populate_existing=True)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)
        doc_id = row.doc_id
        memory: Optional[dict] = None
        try:
            claimed = await db.execute(
                update(DocNoteSuggestion)
                .where(
                    DocNoteSuggestion.id == suggestion_id,
                    DocNoteSuggestion.status == MemorySuggestionStatus.PENDING,
                )
                .values(status=MemorySuggestionStatus.ACCEPTED, resolved_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise SuggestionAlreadyResolvedError("resolved")

            notes_before = await doc_store.load_notes_state_in_session(db, doc_id)
            if notes_before is None:
                await db.rollback()
                return None

            text = content if content is not None else row.content
            if row.kind == KIND_MEMORY:
                if row.replaces_memory_id:
                    memory = await doc_memory_store.overwrite_memory_in_session(
                        db, doc_id, row.replaces_memory_id, text,
                        user_id=user_id, now=now,
                    )
                if memory is None:
                    memory = await doc_memory_store.insert_memory_in_session(
                        db, doc_id, text,
                        source=doc_memory_store.suggestion_source(row.id),
                        user_id=user_id, now=now,
                    )
                    if memory is None:
                        await db.rollback()
                        return None
                await db.execute(
                    update(DocNoteSuggestion)
                    .where(DocNoteSuggestion.id == suggestion_id)
                    .values(memory_id=memory["id"])
                    .execution_options(synchronize_session=False)
                )
            elif row.kind == KIND_INSTRUCTIONS:
                expected = (
                    expected_instructions_version
                    if expected_instructions_version is not None
                    else row.base_instructions_version
                )
                written = await doc_store.write_instructions_in_session(
                    db, doc_id, text,
                    expected_version=expected, user_id=user_id, now=now,
                )
                if written is None:
                    await db.rollback()
                    return None
            else:
                raise ValueError(f"Unknown doc note suggestion kind {row.kind!r}.")

            notes_after = await doc_store.load_notes_state_in_session(db, doc_id)
            await db.refresh(row)
            accepted = {
                "suggestion": _to_dict(row),
                "memory": memory,
                "notes_before": notes_before,
                "notes_after": notes_after,
            }
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return accepted


async def dismiss_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    """Mark a pending suggestion dismissed (``resolved_at`` = now); the
    notes are untouched.

    Returns:
        The row dict, or None when the suggestion is missing or another
        user's.

    Raises:
        SuggestionAlreadyResolvedError: the row is no longer pending
            (also when a concurrent accept or dismiss won the claim).
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.get(DocNoteSuggestion, suggestion_id, populate_existing=True)
        if row is None or row.user_id != user_id:
            return None
        if row.status != MemorySuggestionStatus.PENDING:
            raise SuggestionAlreadyResolvedError(row.status)
        try:
            claimed = await db.execute(
                update(DocNoteSuggestion)
                .where(
                    DocNoteSuggestion.id == suggestion_id,
                    DocNoteSuggestion.status == MemorySuggestionStatus.PENDING,
                )
                .values(status=MemorySuggestionStatus.DISMISSED, resolved_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise SuggestionAlreadyResolvedError("resolved")
            await db.refresh(row)
            dismissed = _to_dict(row)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return dismissed


# ---------------------------------------------------------------------------
# Admin report
# ---------------------------------------------------------------------------


async def list_all_suggestion_rows() -> list[dict]:
    """Every doc note suggestion across all users for the admin Memories
    usage report: ``user_id`` (the reviewer), ``doc_id``, ``kind``,
    ``status`` and naive-UTC ``created_at`` / ``resolved_at`` datetimes as
    stored, nothing else (no content, no reasoning)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                DocNoteSuggestion.user_id,
                DocNoteSuggestion.doc_id,
                DocNoteSuggestion.kind,
                DocNoteSuggestion.status,
                DocNoteSuggestion.created_at,
                DocNoteSuggestion.resolved_at,
            )
        )
        return [
            {
                "user_id": user_id,
                "doc_id": doc_id,
                "kind": kind,
                "status": status,
                "created_at": created_at,
                "resolved_at": resolved_at,
            }
            for user_id, doc_id, kind, status, created_at, resolved_at in result.all()
        ]
