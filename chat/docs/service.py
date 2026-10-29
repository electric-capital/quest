"""Quest Docs service layer: the ONE read/write path for docs.

The model-facing doc tools (chat/gemini_api/tool_handlers/docs.py) and the
``write_doc`` action request call the functions below; nothing else writes
a doc body or asset. Every operation follows the same pipeline in this one
place::

    feature gate -> doc lookup -> resolve_doc_access (chat/docs/access.py)
      -> file op (chat/docs/files.py, in asyncio.to_thread)
      -> DB bump (db/doc_store.update_after_write)
      -> read sidecar (doc_reads.json) -> realtime events (chat/docs/events.py)

Doc notes (docs/architecture/doc-notes.md): while the ``doc_notes`` gate
is open for the caller, :func:`read_doc` adds the ``notes`` counts and,
when the notes are due (a recording run that has not seen them, recorded
in the seen sidecar ``doc_notes_seen.json``; a sub-agent or inference-API
run whenever the doc has notes), the ``doc_notes`` block;
:func:`list_docs` / :func:`search_docs` rows add the counts and
:func:`create_doc` records the new doc as seen at versions (0, 0). The
helpers in the "Doc notes" section are shared with the notes tools and
chat/docs/note_suggestions.py; :func:`publish_notes_changed` there is the
one publisher of ``doc_notes_changed``. :func:`delete_doc_from_ui` tells
the users with pending note suggestions on the doc that they went with it.

Errors are raised as :class:`DocError` (model/user-facing message; tools
wrap it as ``{"error": str(e)}``) or its structured subclasses:
:class:`DocApprovalRequired` (the write needs a ``write_doc`` action
request; carries the ``suggested_request`` payload the model forwards
unchanged), :class:`DocNotesUnseen` (a model write refused once because
the conversation has not seen the doc's current notes; carries the notes
block and its text copy, and the refusal has recorded the delivery) and
:class:`DocDisabled` (the ``docs`` feature gate is closed for the user;
raised before any DB work). :class:`DocRequestError` serves the UI routes.

Invariants kept here:

- A hidden doc and a nonexistent id are indistinguishable: one DB lookup,
  no file IO, the same ``doc_not_found_message`` text.
- ``edit`` requires the doc in the conversation's read sidecar (fed by
  :func:`read_doc` and :func:`create_doc`, never by :func:`search_docs`).
- Body changes go through ``files.modify_body`` (read + transform + write
  in one critical section of the per-doc threading lock), and every write
  holds a per-doc ``asyncio.Lock`` from the file op through the DB bump
  and the events, so concurrent writers never lose a change and the DB
  counters / ``updated_at`` land in write order.
- The read sidecar, the notes seen sidecar and the realtime publishers
  run on the event loop, never inside ``asyncio.to_thread``
  (``ChatStorage.add_doc_read_ids``, ``record_doc_notes_seen`` and
  ``bus.publish_to_user`` rely on the single-event-loop guarantee).
- A notes delivery is recorded only once the block is in the result (or
  in the :class:`DocNotesUnseen` being raised), with no ``await`` before
  the return or raise, and never for a sub-agent, inference-API or script
  read (:func:`record_notes_delivery`). The one record without a block is
  the creator's (0, 0).
- Notes before writes: in a recording run with the ``doc_notes`` gate
  open, a model write to a doc whose notes this conversation has not seen
  (``notes.write_refused()``) is refused once with :class:`DocNotesUnseen`
  in :func:`_resolve_write`, after the denied and read-before-edit checks
  and before the approval verdict, so the write tools and the write_doc
  pre-card share it. Approve-time execute skips the seen record and
  instead refuses when the card's pinned ``instructions_version`` moved
  and the live instructions are non-empty.
- Results never expose a doc's share roster; list/read rows carry only a
  ``shared`` flag.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import re
import stat
import sys
import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from chat import storage as _storage
from chat.docs import constants
from chat.docs import files as doc_files
from chat.docs import notes as doc_notes
from chat.docs.access import (
    DENY_INFERENCE_API,
    DENY_SCRIPT,
    DENY_SUB_AGENT,
    DENY_USER_SUBAGENT,
    DocAccess,
    creation_mode,
    resolve_doc_access,
    write_note,
)
from chat.docs.constants import (
    doc_not_found_message,
    docs_disabled_message,
    user_doc_in_public_conversation_message,
    user_doc_mode_private_message,
)
from config import feature_gates
from db import doc_memory_store, doc_store

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_REQUIRED_BY_OWNER_MESSAGE",
    "APPROVAL_REQUIRED_MESSAGE",
    "approval_required_message",
    "DOC_SCOPES",
    "WRITE_OPERATIONS",
    "Caller",
    "DocApprovalRequired",
    "DocDisabled",
    "DocError",
    "DocNotesUnseen",
    "DocRequestError",
    "add_doc_image",
    "append_to_doc",
    "apply_write_operation",
    "create_doc",
    "create_doc_from_ui",
    "edit_doc",
    "get_visible_doc",
    "list_docs",
    "load_notes",
    "mode_switch_refusal",
    "notes_counts",
    "notes_enabled",
    "notes_seen",
    "preview_write_operation",
    "publish_notes_changed",
    "read_doc",
    "record_notes_delivery",
    "require_enabled",
    "search_docs",
    "validate_doc_metadata",
]

# list_docs / search_docs scopes: ``user`` = user docs, ``project`` = this
# conversation's project docs (from a standalone conversation: the docs of
# all of the user's own projects, read-only), ``public`` = the public docs
# this project may read from its doc sources (the public projects listed
# in Project Settings > Project Access; for a public conversation the same
# set as ``project``), ``all`` = every doc the conversation can see.
DOC_SCOPES = ("user", "project", "public", "all")
WRITE_OPERATIONS = ("edit", "append", "add_image")
IMAGE_PLACEMENTS = ("append", "none")

# spec 6.2: the message of every approval_required tool result (the second
# wording when the owner's require-approval switch produced the verdict).
APPROVAL_REQUIRED_MESSAGE = (
    'This doc is shared; propose the change with '
    'create_action_request(request_type="write_doc", ...)'
)
APPROVAL_REQUIRED_BY_OWNER_MESSAGE = (
    'The owner requires approval for every change to this doc; propose the '
    'change with create_action_request(request_type="write_doc", ...)'
)


def approval_required_message(access: DocAccess) -> str:
    """The ``approval_required`` message for an ``approval`` verdict."""
    if access.required_by_owner:
        return APPROVAL_REQUIRED_BY_OWNER_MESSAGE
    return APPROVAL_REQUIRED_MESSAGE

# Only conversations create docs (creation is never gated, so Slack runs
# may create too); the read-only run kinds get their access.py deny text.
_CREATE_RUN_KINDS = frozenset({"top_level", "slack"})
_CREATE_DENY_REASONS = {
    "sub_agent": DENY_SUB_AGENT,
    "inference_api": DENY_INFERENCE_API,
    "user_subagent": DENY_USER_SUBAGENT,
    "script": DENY_SCRIPT,
}

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50


# ---------------------------------------------------------------------------
# Errors and the caller
# ---------------------------------------------------------------------------


class DocError(Exception):
    """A doc operation was refused or failed; ``str(e)`` is model/user-facing."""


class DocApprovalRequired(DocError):
    """The write needs a ``write_doc`` action request (shared private doc).

    ``suggested_request`` is ``{"request_type": "write_doc", "params":
    {"operation", "doc_id", ...the operation's params}}`` -- exactly what
    the model forwards as the action request.
    """

    def __init__(self, message: str, suggested_request: dict):
        super().__init__(message)
        self.suggested_request = suggested_request


class DocDisabled(DocError):
    """The ``docs`` feature gate is closed for the user."""


class DocNotesUnseen(DocError):
    """A model write refused until the conversation has seen the doc's notes.

    Raised by :func:`_resolve_write` in a recording run with the
    ``doc_notes`` gate open, once the delivery it carries is recorded, so
    the identical retry passes. ``notes_block`` is the ``doc_notes`` block
    the write tools return; ``notes_text`` its plain-text copy
    (``notes.render_notes_text()``), which the write_doc pre-card puts in
    its ``Invalid parameters`` text. ``str(e)`` is
    ``notes.DOC_NOTES_UNSEEN_MESSAGE`` with the doc's title.
    """

    def __init__(self, message: str, notes_block: dict, notes_text: str):
        super().__init__(message)
        self.notes_block = notes_block
        self.notes_text = notes_text


class DocRequestError(DocError):
    """A UI request (chat/docs/routes.py) was refused.

    ``code`` is the stable machine-readable error code the HTTP routes
    return: ``invalid_title``, ``invalid_description``, ``invalid_mode``,
    ``project_not_found``, ``project_doc_mode_inherited``,
    ``user_doc_mode_private``, ``duplicate_title``, ``forbidden`` or
    ``invalid_request``. ``str(e)`` is the user-facing message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Caller:
    """Who is asking, from which kind of run.

    ``run_kind`` is one of ``chat.docs.access.RUN_KINDS``. Sandbox scripts
    (``"script"``) have no conversation of their own
    (``conversation_id=None``: no read-before-edit record) but carry the
    ``project_id`` and ``is_public`` of the conversation that ran them.
    """

    user: dict
    conversation_id: Optional[str]
    project_id: Optional[str]
    is_public: bool
    run_kind: str


def require_enabled(caller: Caller) -> None:
    """Raise :class:`DocDisabled` unless the docs gate is open for the user."""
    email = (caller.user or {}).get("email") or ""
    if not feature_gates.docs_enabled_for(email):
        raise DocDisabled(docs_disabled_message())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _scope(doc: dict) -> str:
    return "project" if doc.get("project_id") else "user"


def _access(
    caller: Caller, doc: dict, doc_source_project_ids: frozenset[str] = frozenset(),
) -> DocAccess:
    return resolve_doc_access(
        doc,
        user_id=caller.user["id"],
        is_public=caller.is_public,
        project_id=caller.project_id,
        run_kind=caller.run_kind,
        doc_source_project_ids=doc_source_project_ids,
    )


def _has_doc_sources(caller: Caller) -> bool:
    """Whether this caller can have doc sources at all: a conversation of
    a private project (not the UI, never public; a script launched from
    such a conversation carries its project and counts)."""
    return bool(
        caller.project_id
        and not caller.is_public
        and caller.run_kind != "ui"
    )


async def _doc_sources(caller: Caller) -> frozenset[str]:
    """The public projects whose docs this caller's project may read
    (``project_doc_sources``, Project Settings > Project Access); empty for
    every caller that is not a private project conversation."""
    if not _has_doc_sources(caller):
        return frozenset()
    from db import project_store
    return frozenset(await project_store.list_doc_source_project_ids(caller.project_id))


def _reaches_own_projects(caller: Caller) -> bool:
    """Whether this caller is a standalone conversation, which reads the
    docs of all of the user's own projects (access rule 2): no project,
    not public and not the UI (a script launched from a standalone
    conversation carries no project and counts)."""
    return bool(
        caller.project_id is None
        and not caller.is_public
        and caller.run_kind != "ui"
    )


def _split_lines(body: str) -> list[str]:
    """Lines of ``body`` with their ``\\n`` (LF only -- ``str.splitlines``
    would also split on ``\\v``, ``\\f``, U+2028 ... and disagree with the
    line numbers search_docs reports)."""
    if not body:
        return []
    parts = body.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _total_lines(body: str) -> int:
    return len(_split_lines(body))


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n")


def _check_limit(value, default: int, maximum: int, name: str = "limit") -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise DocError(f"{name} must be an integer.")
    return max(1, min(maximum, value))


def _check_scope(scope) -> str:
    scope = "all" if scope is None else scope
    if scope not in DOC_SCOPES:
        raise DocError(f"scope must be one of: {', '.join(DOC_SCOPES)}.")
    return scope


def _check_body_size(body: str, *, creating: bool = False) -> None:
    # surrogatepass: a lone surrogate (valid in JSON) must not raise here;
    # files._encode_body refuses it with a clean DocFileError on write.
    size = len(body.encode("utf-8", "surrogatepass"))
    if size > constants.DOC_MAX_CONTENT_SIZE:
        if creating:
            raise DocError(
                f"Doc content is {size} bytes, over the "
                f"{constants.DOC_MAX_CONTENT_SIZE}-byte limit."
            )
        raise DocError(
            f"The doc would be {size} bytes after this change, over the "
            f"{constants.DOC_MAX_CONTENT_SIZE}-byte limit."
        )


async def _files(fn, *args, **kwargs):
    """Run a sync chat/docs/files.py call in a thread.

    ``DocFileError`` -> ``DocError`` with its message; any other ``OSError``
    (a backstop: files.py maps the expected ones) -> a generic DocError
    that names no path. A ``DocError`` raised by a ``modify_body``
    transform propagates unchanged.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except doc_files.DocFileError as exc:
        raise DocError(str(exc)) from None
    except OSError as exc:
        logger.warning("[docs] file operation failed", exc_info=True)
        raise DocError(
            f"Doc storage error: {exc.strerror or exc.__class__.__name__}."
        ) from None


def _record_read(caller: Caller, doc_id: str) -> None:
    """Add ``doc_id`` to the conversation's read sidecar (best-effort).

    Synchronous on purpose (see the module docstring). A failure only means
    a later edit_doc asks the model to read the doc again.
    """
    if not caller.conversation_id:
        return
    try:
        _storage.ChatStorage.add_doc_read_ids(caller.conversation_id, [doc_id])
    except (OSError, ValueError):
        logger.warning(
            "[docs] could not record doc read (conversation=%s, doc=%s)",
            caller.conversation_id, doc_id, exc_info=True,
        )


def _has_read(caller: Caller, doc_id: str) -> bool:
    if not caller.conversation_id:
        return False
    try:
        return doc_id in _storage.ChatStorage.get_doc_read_ids(caller.conversation_id)
    except (OSError, ValueError):
        return False


def _write_source_or_default(write_source: Optional[str], caller: Caller) -> str:
    if write_source:
        return write_source
    if caller.conversation_id:
        return f"conversation:{caller.conversation_id}"
    return caller.run_kind


# Per-doc asyncio locks around read-modify-write sequences (files.doc_lock
# is a threading.Lock taken per file call, so it cannot span the read and
# the write of one edit/append). Single event loop per process.
# Weak values: holders and waiters keep a strong reference, so the table
# does not grow with every doc id ever written.
_write_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


def _write_lock(doc_id: str) -> asyncio.Lock:
    lock = _write_locks.get(doc_id)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[doc_id] = lock
    return lock


async def _shielded(coro):
    """Run ``coro`` to completion even if the awaiting task is cancelled.

    A write is file op -> DB bump -> events; cancelling between the first
    two (the Stop button mid-turn) would leave content_size / asset_count /
    updated_at stale and no event sent. The inner task keeps running; the
    cancellation still propagates to the caller.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Retrieve the eventual outcome so a late failure is logged, not
        # reported as "exception was never retrieved".
        def _log_done(t: "asyncio.Future") -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.warning(
                    "[docs] write finished with an error after the caller "
                    "was cancelled: %s", t.exception(),
                )
        task.add_done_callback(_log_done)
        raise


def _events():
    """chat/docs/events.py, imported lazily: it pulls in chat.realtime,
    whose package import reaches chat.gemini_api, which imports the doc tool
    handlers -- and so this module."""
    from chat.docs import events
    return events


def _publish_write(doc: dict) -> None:
    """``doc_changed`` + ``doc_list_changed`` to everyone who can see the
    doc: its owner, its direct share recipients, and every connected user
    when it has an everyone row (``doc`` is the bumped row, with shares)."""
    _events().publish_doc_write(doc)


async def _finish_write(
    doc_id: str,
    *,
    content_size: int,
    write_source: str,
    asset_count: Optional[int] = None,
) -> dict:
    """DB bump after a landed file write, then the realtime events."""
    updated = await doc_store.update_after_write(
        doc_id,
        content_size=content_size,
        asset_count=asset_count,
        last_write_source=write_source,
    )
    if updated is None:
        # Deleted while we were writing; the directory sweep follows the
        # delete, so report it like any missing doc.
        raise DocError(doc_not_found_message(doc_id))
    _publish_write(updated)
    return updated


# ---------------------------------------------------------------------------
# Doc notes: gate, counts, block and seen record (docs/architecture/doc-notes.md)
#
# Shared by the doc tools here and the notes tools. The gate is the
# caller's, never the doc owner's; with it closed none of this runs.
# ---------------------------------------------------------------------------


# Run kinds whose read_doc gets the notes counts but never the block: a
# script is code, not the model; the UI has its own notes routes. Applies
# to read_doc only (a script may still fetch the notes on purpose).
_READ_DOC_COUNTS_ONLY_RUN_KINDS = frozenset({"script", "ui"})


def notes_enabled(caller: Caller) -> bool:
    """Whether the ``doc_notes`` gate is open for the caller.

    The one place the doc tools check that gate (invariant 6: closed means
    every doc tool returns what it did before notes existed).
    """
    return feature_gates.doc_notes_enabled_for((caller.user or {}).get("email") or "")


def notes_counts(doc: Mapping[str, Any]) -> dict:
    """The ``notes`` counts of a result row: ``{"has_instructions": bool,
    "memories": int}``, from a doc dict or the ``get_notes_state()`` dict.
    Never any content."""
    return {
        "has_instructions": bool(doc["has_instructions"]),
        "memories": int(doc["memory_count"]),
    }


def _block_counts(block: Mapping[str, Any]) -> dict:
    """:func:`notes_counts` of what a notes block (built from offset 0)
    shows, so a result's counts agree with its block even when a memory
    changed between the state read and the memories read."""
    return {
        "has_instructions": block["instructions"] != "",
        "memories": len(block["memories"]) + block["memories_omitted"],
    }


async def load_notes(doc_id: str, *, memory_offset: int = 0) -> tuple[dict, dict]:
    """The doc's notes state and its notes block, for a visible doc.

    Reads ``get_notes_state()`` BEFORE ``list_doc_memories()``: a memory
    change landing between the two reads then shows memories at least as
    new as the state's ``memories_version``, so the version a caller
    records is never newer than the memories it showed.

    Returns:
        ``(state, block)``: the ``get_notes_state()`` dict and the
        ``doc_notes`` block (``build_notes_block``) with memories from
        ``memory_offset``.

    Raises:
        DocError: ``memory_offset`` is not an int >= 0 (checked before any
            read), or ``doc_not_found_message`` when the doc is gone.
    """
    if isinstance(memory_offset, bool) or not isinstance(memory_offset, int) or memory_offset < 0:
        raise DocError("memory_offset must be a non-negative integer.")
    state = await doc_store.get_notes_state(doc_id)
    if state is None:
        raise DocError(doc_not_found_message(doc_id))
    memories = await doc_memory_store.list_doc_memories(doc_id)
    return state, doc_notes.build_notes_block(state, memories, memory_offset)


def notes_seen(caller: Caller, doc_id: str) -> Optional[doc_notes.NotesSeen]:
    """The caller's seen record for ``doc_id``, or None.

    None outside recording runs (``records_delivery()``): a sub-agent
    shares its parent's conversation id, but the parent's record is not
    its own, so it reads like a conversation with no record. None also
    for a record that cannot be read: no record means deliver (and refuse
    a write), the fail-closed direction.

    Raises:
        ValueError / TypeError: an unknown run kind or a non-string
            conversation id (from ``records_delivery()``).
    """
    if not doc_notes.records_delivery(caller.run_kind, caller.conversation_id):
        return None
    try:
        return _storage.ChatStorage.get_doc_notes_seen(caller.conversation_id, doc_id)
    except (OSError, _storage.InvalidStorageIdError):
        logger.warning(
            "[docs] could not read the doc notes seen record "
            "(conversation=%s, doc=%s)",
            caller.conversation_id, doc_id, exc_info=True,
        )
        return None


def record_notes_delivery(
    caller: Caller, doc_id: str, instructions_version: int, memories_version: int,
) -> None:
    """Record that the caller's model was shown ``doc_id``'s notes at these
    versions (best-effort).

    A no-op unless the run records deliveries (``records_delivery()``:
    top-level, Slack and cross-user subagent runs with a conversation), so
    sub-agent, inference-API and script reads never advance a record.
    Synchronous on purpose, like :func:`_record_read`. Call it as the last
    step once the block is in the result, with no ``await`` between it and
    the return (or raise), so a cancelled call never records a block the
    model did not get. A failed write costs one extra delivery or refusal.

    Raises:
        ValueError / TypeError: an unknown run kind or a non-string
            conversation id (from ``records_delivery()``), or a bad
            ``doc_id`` / version (from ``record_doc_notes_seen()``): caller
            bugs, never swallowed.
    """
    if not doc_notes.records_delivery(caller.run_kind, caller.conversation_id):
        return
    try:
        _storage.ChatStorage.record_doc_notes_seen(
            caller.conversation_id, doc_id, instructions_version, memories_version,
        )
    except (OSError, _storage.InvalidStorageIdError):
        logger.warning(
            "[docs] could not record doc notes delivery (conversation=%s, doc=%s)",
            caller.conversation_id, doc_id, exc_info=True,
        )


async def publish_notes_changed(doc_id: str, state: Mapping[str, Any]) -> None:
    """Publish ``doc_notes_changed`` for a change to a doc's notes.

    The one publisher of that event: an accepted note suggestion
    (chat/docs/note_suggestions.py) calls it, and every other writer of the
    instructions or the memories must call it after its store write. Loads
    the doc with its
    shares for the audience (owner, direct share recipients, every
    connected user for an everyone share; ``events.doc_audience()``) and
    sends the two versions of ``state``, the ``get_notes_state()`` dict
    after the change. A doc gone meanwhile publishes nothing.

    Best-effort: a failure is logged and never fails the write.
    """
    try:
        doc = await doc_store.get_doc(doc_id, with_shares=True)
        if doc is not None:
            _events().publish_doc_notes_changed_for(doc, state)
    except Exception:
        logger.warning(
            "[docs] could not publish doc_notes_changed (doc=%s)", doc_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Visibility + read side
# ---------------------------------------------------------------------------


async def _get_visible_doc(caller: Caller, doc_id) -> tuple[dict, DocAccess]:
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise DocError("doc_id must be a non-empty string.")
    doc = await doc_store.get_doc(doc_id, with_shares=True)
    if doc is None:
        raise DocError(doc_not_found_message(doc_id))
    # The doc-sources lookup runs before the verdict, so a hidden doc and a
    # nonexistent id still cost the same work (invariant 2).
    access = _access(caller, doc, await _doc_sources(caller))
    if not access.visible:
        # Same text as a nonexistent id -- invariant 2.
        raise DocError(doc_not_found_message(doc_id))
    return doc, access


async def get_visible_doc(caller: Caller, doc_id: str) -> tuple[dict, DocAccess]:
    """Return ``(doc, access)`` for a doc this caller may see.

    Raises:
        DocDisabled: gate closed.
        DocError: ``doc_not_found_message(doc_id)`` for a missing OR hidden doc.
    """
    require_enabled(caller)
    return await _get_visible_doc(caller, doc_id)


async def _visible_docs(
    caller: Caller, scope: str, limit: Optional[int],
) -> list[tuple[dict, DocAccess]]:
    """Visible docs in ``scope``, newest-updated first, at most ``limit``.

    Candidates come from ``doc_store.list_accessible_docs`` (owner or
    share); each is filtered through the access rule. Pages through the
    store with its keyset cursor so hidden rows never eat into ``limit``.

    A public conversation sees only public docs, and only project docs
    can be public (user docs are always private), so it lists its own
    project's docs and never queries user docs at all -- not even a
    leftover row whose stored mode still says ``public``. A private
    project conversation additionally lists the public docs of its doc
    sources (scope ``public``, included in ``all``). A standalone
    conversation lists the docs of all of the user's own projects under
    scope ``project`` (included in ``all``), read-only.
    """
    include_user = scope in ("user", "all") and not caller.is_public
    include_project = bool(caller.project_id) and (
        scope in ("project", "all") or (scope == "public" and caller.is_public)
    )
    include_own_projects = scope in ("project", "all") and _reaches_own_projects(caller)
    sources = await _doc_sources(caller) if scope in ("public", "all") else frozenset()
    if not include_user and not include_project and not include_own_projects and not sources:
        return []

    out: list[tuple[dict, DocAccess]] = []
    before = None
    while True:
        rows = await doc_store.list_accessible_docs(
            caller.user["id"],
            project_id=caller.project_id,
            include_user_docs=include_user,
            include_project_docs=include_project,
            include_owned_project_docs=include_own_projects,
            source_project_ids=sources,
            # A public conversation can only ever see public docs; filtering
            # in SQL keeps hidden private rows from eating the LIMIT.
            mode="public" if caller.is_public else None,
            limit=limit,
            before=before,
        )
        for row in rows:
            access = _access(caller, row, sources)
            if not access.visible:
                continue
            out.append((row, access))
            if limit is not None and len(out) >= limit:
                return out
        if limit is None or len(rows) < limit:
            return out
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])


def _list_row(doc: dict, access: DocAccess, *, with_notes: bool = False) -> dict:
    row = {
        "id": doc["id"],
        "title": doc["title"],
        "description": doc["description"],
        "mode": doc["mode"],
        "scope": _scope(doc),
        "project_id": doc["project_id"],
        "content_size": doc["content_size"],
        "asset_count": doc["asset_count"],
        "updated_at": doc["updated_at"],
        "shared": bool(doc.get("shares")),
        "writable": access.write,
        "write_note": write_note(access),
    }
    if with_notes:
        row["notes"] = notes_counts(doc)
    return row


async def list_docs(
    caller: Caller, scope: str = "all", limit: int = LIST_DEFAULT_LIMIT,
) -> list[dict]:
    """Docs this caller can see, newest-updated first.

    ``scope``: ``"user"`` (user docs), ``"project"`` (this conversation's
    project docs; from a standalone conversation the read-only docs of all
    of the user's own projects), ``"public"`` (the public docs readable
    from here: from a private project conversation the docs of its doc
    sources, from a public conversation its own project's docs, nothing
    elsewhere) or ``"all"`` (everything). ``limit`` is clamped to 1..200.
    Hidden docs never appear.

    Returns:
        Rows ``{id, title, description, mode, scope, project_id,
        content_size, asset_count, updated_at, shared, writable,
        write_note}``, plus ``notes`` (:func:`notes_counts`, never content)
        last while the ``doc_notes`` gate is open for the caller.
    """
    require_enabled(caller)
    scope = _check_scope(scope)
    limit = _check_limit(limit, LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
    with_notes = notes_enabled(caller)
    return [
        _list_row(doc, access, with_notes=with_notes)
        for doc, access in await _visible_docs(caller, scope, limit)
    ]


def _body_snippets(body: str, pattern: re.Pattern, max_snippets: int) -> list[dict]:
    """Up to ``max_snippets`` ``{line, snippet}`` hits; non-overlapping windows."""
    width = constants.DOC_SEARCH_SNIPPET_CHARS
    matches: list[dict] = []
    covered_to = 0
    for m in pattern.finditer(body):
        if len(matches) >= max_snippets:
            break
        if m.start() < covered_to:
            continue
        hit_len = m.end() - m.start()
        start = max(0, m.start() - max(0, (width - hit_len) // 2))
        end = min(len(body), start + width)
        start = max(0, end - width)
        matches.append({
            "line": body.count("\n", 0, m.start()) + 1,
            "snippet": body[start:end].strip(),
        })
        covered_to = end
    return matches


async def search_docs(
    caller: Caller, query: str, scope: str = "all", limit: int = SEARCH_DEFAULT_LIMIT,
) -> dict:
    """Case-insensitive search over title, description and body.

    Bounded scan: bodies of visible docs are read newest-first until the
    next body would push the scanned total past
    ``DOC_SEARCH_MAX_SCAN_BYTES``; from then on only titles/descriptions
    are matched and ``truncated`` is true. A title/description-only hit has
    ``matches: []``. Does NOT record a read (snippets are not a read).

    Returns:
        ``{"results": [{id, title, mode, scope, matches: [{line, snippet}],
        notes?}], "truncated": bool}``, at most ``limit`` (1..50) results;
        ``notes`` (:func:`notes_counts`, never content) comes last while
        the ``doc_notes`` gate is open for the caller. Notes are never
        searched.
    """
    require_enabled(caller)
    if not isinstance(query, str) or not query.strip():
        raise DocError("query must be a non-empty string.")
    scope = _check_scope(scope)
    limit = _check_limit(limit, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT)
    with_notes = notes_enabled(caller)

    needle = query.strip()
    folded = needle.casefold()
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    budget = constants.DOC_SEARCH_MAX_SCAN_BYTES
    snippets_per_doc = constants.DOC_SEARCH_SNIPPETS_PER_DOC

    results: list[dict] = []
    scanned = 0
    truncated = False
    for doc, _access_ in await _visible_docs(caller, scope, None):
        if len(results) >= limit:
            break
        meta_hit = (
            folded in doc["title"].casefold()
            or folded in (doc["description"] or "").casefold()
        )
        matches: list[dict] = []
        if not truncated and scanned + doc["content_size"] > budget:
            truncated = True
        if not truncated:
            try:
                body = await _files(doc_files.read_body, doc["id"])
            except DocError:
                body = ""
            scanned += len(body.encode("utf-8"))
            matches = _body_snippets(body, pattern, snippets_per_doc)
        if matches or meta_hit:
            row = {
                "id": doc["id"],
                "title": doc["title"],
                "mode": doc["mode"],
                "scope": _scope(doc),
                "matches": matches,
            }
            if with_notes:
                row["notes"] = notes_counts(doc)
            results.append(row)
    return {"results": results, "truncated": truncated}


async def read_doc(
    caller: Caller,
    doc_id: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> dict:
    """Read a doc body, whole (capped) or a 1-based inclusive line range.

    Every result is capped at ``DOC_READ_MAX_CHARS`` characters, cut at a
    line boundary (a single longer line is cut mid-line); a cut sets
    ``truncated`` and a ``note`` telling the model to page. Records the doc
    in the conversation's read sidecar (the edit_doc prerequisite).

    While the ``doc_notes`` gate is open for the caller, the result also
    ends with ``notes`` (:func:`notes_counts`) and, when the notes are
    delivered, ``doc_notes`` (the block of :func:`load_notes`, the current
    state even when emptied). A recording run delivers when
    ``needs_delivery()`` holds for its seen record and then records the
    delivered versions; sub-agent and inference-API runs deliver whenever
    the doc has notes and record nothing; scripts get the counts only.

    Returns:
        ``{id, title, mode, scope, total_lines, content, shared, writable,
        write_note, start_line, end_line, truncated, note?, notes?,
        doc_notes?}`` (start/end are the returned range; 0/0 for an empty
        doc).
    """
    require_enabled(caller)
    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise DocError(f"{name} must be a positive integer (lines are 1-based).")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise DocError("end_line must be greater than or equal to start_line.")

    doc, access = await _get_visible_doc(caller, doc_id)
    body = await _files(doc_files.read_body, doc["id"])
    lines = _split_lines(body)
    total = len(lines)

    first = start_line or 1
    if total and first > total:
        raise DocError(
            f"start_line {first} is past the end of the doc ({total} lines)."
        )
    last = min(end_line or total, total)
    selected = lines[first - 1:last] if total else []

    cap = constants.DOC_READ_MAX_CHARS
    kept: list[str] = []
    used = 0
    for line in selected:
        if used + len(line) > cap:
            break
        kept.append(line)
        used += len(line)

    truncated = len(kept) < len(selected)
    if not truncated:
        content = "".join(selected)
        returned_start, returned_end = (first, last) if total else (0, 0)
    elif kept:
        content = "".join(kept)
        returned_start, returned_end = first, first + len(kept) - 1
    else:
        content = selected[0][:cap]
        returned_start = returned_end = first

    result = {
        "id": doc["id"],
        "title": doc["title"],
        "mode": doc["mode"],
        "scope": _scope(doc),
        "total_lines": total,
        "content": content,
        "shared": bool(doc.get("shares")),
        "writable": access.write,
        "write_note": write_note(access),
        "start_line": returned_start,
        "end_line": returned_end,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"Content truncated at {len(content)} characters; page with "
            f"start_line/end_line (returned lines {returned_start}-"
            f"{returned_end} of {total})."
        )
    delivered: Optional[dict] = None
    if notes_enabled(caller):
        result["notes"] = notes_counts(doc)
        # Outside recording runs notes_seen() is None, so a sub-agent or
        # inference-API read gets what a conversation with no record gets:
        # the block whenever the doc has notes.
        attach = (
            caller.run_kind not in _READ_DOC_COUNTS_ONLY_RUN_KINDS
            and doc_notes.needs_delivery(doc, notes_seen(caller, doc["id"]))
        )
        if attach:
            state, block = await load_notes(doc["id"])
            result["notes"] = _block_counts(block)
            result["doc_notes"] = block
            delivered = state
    _record_read(caller, doc["id"])
    if delivered is not None:
        # Last step, no await before the return (record_notes_delivery).
        record_notes_delivery(
            caller, doc["id"],
            delivered["instructions_version"], delivered["memories_version"],
        )
    return result


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_doc(
    caller: Caller,
    title: str,
    content: str,
    description: str = "",
    target: str = "user",
) -> dict:
    """Create a doc in this conversation's mode (never gated: no shares yet).

    ``target="user"``: a user doc, always private -- refused from a public
    conversation (which never creates a private doc) with
    ``user_doc_in_public_conversation_message``.
    ``target="project"``: a doc of the conversation's project (which the
    user must own), in the project's mode -- which equals the
    conversation's (``creation_mode``), since a public conversation is one
    in a public project. Only top-level and Slack conversations create docs.

    Order: lay out the directory under a fresh id, then insert the row (a
    crash in between leaves an invisible orphan directory, never a row
    without a body); the directory is removed again if the insert fails.
    Records the new doc as read and publishes doc_list_changed + doc_changed.
    While the ``doc_notes`` gate is open for the caller, also records the
    doc as seen at notes versions (0, 0) (the creator exception: no notes
    can predate this conversation, and later ones re-arm the gate).

    Returns:
        ``{id, title, mode, scope, project_id, content_size, updated_at}``.
    """
    require_enabled(caller)
    if caller.run_kind not in _CREATE_RUN_KINDS:
        raise DocError(
            _CREATE_DENY_REASONS.get(
                caller.run_kind, "Docs can only be created from a conversation.",
            )
        )
    if not isinstance(title, str) or not title.strip():
        raise DocError("Doc title cannot be empty.")
    if not isinstance(content, str):
        raise DocError("content must be a string.")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise DocError("description must be a string.")
    target = target or "user"
    if target not in ("user", "project"):
        raise DocError('target must be "user" or "project".')
    if target == "user" and caller.is_public:
        # User docs are always private, and a public conversation must
        # never create a private doc (taint): only its project's docs.
        raise DocError(user_doc_in_public_conversation_message())

    user_id = caller.user["id"]
    conversation_mode = creation_mode(caller.is_public)
    project_id = None
    mode = "private"  # every user doc
    if target == "project":
        if not caller.project_id:
            raise DocError(
                'create_doc(target="project") is only available inside a '
                "project conversation"
            )
        from db import project_store

        project = await project_store.get_project(user_id, caller.project_id)
        if project is None:
            raise DocError(f"Project not found: {caller.project_id}")
        mode = "public" if project.get("public") else "private"
        if mode != conversation_mode:
            # Cannot happen for a real conversation (a public conversation
            # IS one in a public project); refuse rather than let content
            # cross the taint boundary.
            raise DocError(
                "This conversation's mode does not match its project's; "
                "the doc cannot be created here."
            )
        project_id = caller.project_id

    content = _normalize_text(content)
    _check_body_size(content, creating=True)
    write_source = _write_source_or_default(None, caller)

    doc_id = str(uuid.uuid4())
    try:
        size = await _files(doc_files.init_doc, doc_id, content, write_source=write_source)
        doc = await doc_store.create_doc(
            user_id,
            title,
            description,
            mode=mode,
            project_id=project_id,
            content_size=size,
            last_write_source=write_source,
            doc_id=doc_id,
        )
    except BaseException as exc:
        try:
            await asyncio.to_thread(doc_files.delete_doc_dir, doc_id)
        except Exception:
            logger.warning("[docs] cleanup of %s failed", doc_id, exc_info=True)
        if isinstance(exc, (doc_store.DuplicateDocTitleError, doc_store.DocValidationError)):
            raise DocError(str(exc)) from None
        raise

    _record_read(caller, doc["id"])
    if notes_enabled(caller):
        record_notes_delivery(caller, doc["id"], 0, 0)
    events = _events()
    events.publish_doc_list_changed(doc["owner_id"])
    events.publish_doc_changed(doc["owner_id"], doc["id"], doc["updated_at"])
    return {
        "id": doc["id"],
        "title": doc["title"],
        "mode": doc["mode"],
        "scope": _scope(doc),
        "project_id": doc["project_id"],
        "content_size": doc["content_size"],
        "updated_at": doc["updated_at"],
    }


# ---------------------------------------------------------------------------
# UI (HTTP routes): metadata validation + creating an empty doc
# ---------------------------------------------------------------------------


def validate_doc_metadata(
    title: Optional[str] = None, description: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Validate UI-supplied doc metadata field by field.

    Applies the store's own rules (db/doc_store.py) one field at a time so
    the caller learns WHICH field is bad. None means "not supplied" and is
    passed through.

    Returns:
        ``(clean_title, clean_description)`` (stripped; None when not supplied).

    Raises:
        DocRequestError: ``invalid_title`` / ``invalid_description``.
    """
    clean_title = clean_description = None
    if title is not None:
        try:
            clean_title = doc_store._validate_title(title)
        except doc_store.DocValidationError as exc:
            raise DocRequestError("invalid_title", str(exc)) from None
    if description is not None:
        try:
            clean_description = doc_store._validate_description(description)
        except doc_store.DocValidationError as exc:
            raise DocRequestError("invalid_description", str(exc)) from None
    return clean_title, clean_description


async def create_doc_from_ui(
    user: dict,
    title: str,
    description: Optional[str] = "",
    mode: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create an EMPTY doc from the UI (``POST /app/api/docs``), owned by ``user``.

    :func:`create_doc` serves conversations only (it derives the mode from
    the conversation); a UI create has no conversation. A user doc is
    always private: ``mode`` may be omitted or ``"private"``, while
    ``"public"`` is refused with ``user_doc_mode_private`` (a public doc is
    created inside a public project). A project doc always takes its
    project's mode (``projects.public``) and a supplied ``mode`` that
    disagrees is refused. The project must be owned by ``user`` (project
    docs are owned by the project owner). Content is empty, so creating a
    public project doc here cannot move internal data across the taint
    boundary.

    Same order and cleanup as :func:`create_doc`: lay out the directory
    under a fresh id, insert the row with ``last_write_source`` =
    ``ui:<user_id>`` (the UI write source, also recorded in
    ``doc.meta.json``; legacy rows hold a plain ``"ui"``), remove the
    directory again if the insert fails. Publishes doc_list_changed +
    doc_changed to the doc's audience (a new doc has no shares: the
    owner). Records no read (no conversation).

    Returns:
        The store's doc dict (with ``shares: []``).

    Raises:
        DocDisabled: gate closed.
        DocRequestError: ``invalid_title`` / ``invalid_description`` /
            ``invalid_mode`` / ``user_doc_mode_private`` /
            ``project_not_found`` / ``project_doc_mode_inherited`` /
            ``duplicate_title`` / ``invalid_request``.
        DocError: a storage failure laying out the directory.
    """
    require_enabled(
        Caller(user=user, conversation_id=None, project_id=None,
               is_public=False, run_kind="ui")
    )
    clean_title, clean_description = validate_doc_metadata(
        title if title is not None else "", description or "",
    )
    if mode is not None and mode not in constants.DOC_MODES:
        raise DocRequestError(
            "invalid_mode",
            f"mode must be one of: {', '.join(constants.DOC_MODES)}.",
        )

    user_id = user["id"]
    if project_id is not None:
        from db import project_store

        project = await project_store.get_project(user_id, project_id)
        if project is None:
            raise DocRequestError("project_not_found", "Project not found.")
        project_mode = "public" if project.get("public") else "private"
        if mode is not None and mode != project_mode:
            raise DocRequestError(
                "project_doc_mode_inherited",
                f"Project docs take their project's mode ({project_mode}); "
                "it cannot be chosen separately.",
            )
        mode = project_mode
    elif mode == "public":
        raise DocRequestError("user_doc_mode_private", user_doc_mode_private_message())
    else:
        mode = "private"

    # The UI write source (chat/docs/ui_writes.ui_write_source, inlined:
    # that module imports this one).
    write_source = f"ui:{user_id}"
    doc_id = str(uuid.uuid4())
    try:
        size = await _files(doc_files.init_doc, doc_id, "", write_source=write_source)
        doc = await doc_store.create_doc(
            user_id,
            clean_title,
            clean_description,
            mode=mode,
            project_id=project_id,
            content_size=size,
            last_write_source=write_source,
            doc_id=doc_id,
        )
    except BaseException as exc:
        try:
            await asyncio.to_thread(doc_files.delete_doc_dir, doc_id)
        except Exception:
            logger.warning("[docs] cleanup of %s failed", doc_id, exc_info=True)
        if isinstance(exc, doc_store.DuplicateDocTitleError):
            raise DocRequestError("duplicate_title", str(exc)) from None
        if isinstance(exc, doc_store.DocValidationError):
            raise DocRequestError("invalid_request", str(exc)) from None
        raise

    events = _events()
    events.publish_doc_list_changed_for(doc)
    events.publish_doc_changed_for(doc)
    return doc


# ---------------------------------------------------------------------------
# Writes: shared parameter normalization + access gate
# ---------------------------------------------------------------------------


def _str_param(params: dict, key: str, *, default: Optional[str] = None) -> str:
    value = params.get(key)
    if value is None:
        if default is None:
            raise DocError(f"{key} is required.")
        return default
    if not isinstance(value, str):
        raise DocError(f"{key} must be a string.")
    return value


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DocError(f"{key} must be a boolean.")
    return value


# Server-only pins: set by the write_doc action request at approve time,
# never part of a suggested_request (a model-supplied copy in a write_doc
# proposal is refused as an unknown param by its validate_params).
_SERVER_PINS = ("expected_sha256", "expected_instructions_version")


def _normalize_write_params(operation: str, params: dict) -> dict:
    """Validate and complete one operation's params (defaults filled in).

    The result has exactly the keys of ``suggested_request.params`` minus
    ``operation``/``doc_id``, plus the server-only pins when given
    (:data:`_SERVER_PINS`, stripped again by :func:`_suggested_request`):
    ``expected_sha256`` (add_image) and ``expected_instructions_version``
    (any operation; an int >= 0). Unknown keys in ``params`` are ignored
    (the write_doc handler validates the request shape itself).
    """
    if operation not in WRITE_OPERATIONS:
        raise DocError(f"operation must be one of: {', '.join(WRITE_OPERATIONS)}.")
    if not isinstance(params, dict):
        raise DocError("params must be an object.")
    if operation == "edit":
        old_string = _normalize_text(_str_param(params, "old_string"))
        new_string = _normalize_text(_str_param(params, "new_string"))
        if not old_string:
            raise DocError("old_string must not be empty.")
        if old_string == new_string:
            raise DocError(
                "old_string and new_string are identical -- nothing to change."
            )
        clean = {
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": _bool_param(params, "replace_all", False),
        }
    elif operation == "append":
        content = _normalize_text(_str_param(params, "content"))
        if not content.strip():
            raise DocError("content must not be empty.")
        clean = {
            "content": content,
            "ensure_blank_line": _bool_param(params, "ensure_blank_line", True),
        }
    else:
        workspace_path = _str_param(params, "workspace_path")
        if not workspace_path.strip():
            raise DocError("workspace_path must not be empty.")
        placement = _str_param(params, "placement", default="append") or "append"
        if placement not in IMAGE_PLACEMENTS:
            raise DocError('placement must be "append" or "none".')
        clean = {
            "workspace_path": workspace_path,
            "alt": _str_param(params, "alt", default=""),
            "placement": placement,
        }
        # Server-only pin: the stored asset must be the file the card
        # showed (the user can still swap the conversation-workspace file
        # between proposal and approval, through the Chat Files card or
        # their own sandbox runs).
        expected = params.get("expected_sha256")
        if expected:
            clean["expected_sha256"] = str(expected)
    # Server-only pin: the doc's instructions_version when the write_doc
    # card was proposed (_check_pinned_instructions).
    pinned = params.get("expected_instructions_version")
    if pinned is not None:
        if isinstance(pinned, bool) or not isinstance(pinned, int) or pinned < 0:
            raise DocError(
                "expected_instructions_version must be a non-negative integer."
            )
        clean["expected_instructions_version"] = pinned
    return clean


def _suggested_request(operation: str, doc_id: str, params: dict) -> dict:
    params = {k: v for k, v in params.items() if k not in _SERVER_PINS}
    return {
        "request_type": "write_doc",
        "params": {"operation": operation, "doc_id": doc_id, **params},
    }


def _check_pinned_instructions(doc: dict, clean: dict) -> None:
    """Approve-time execute: refuse when the doc's instructions changed
    since the write_doc card was proposed.

    ``doc`` is the live row. No pin (a card proposed before the pin
    existed, or while the ``doc_notes`` gate was closed for the proposer)
    passes. Cleared instructions never refuse, as in ``write_refused()``;
    memory changes never do. The seen record is never consulted: it
    describes the model's context, which compaction may clear while the
    card sits open, whereas the pin is what the proposal was held to, and
    the card shows the approver no instructions.

    Raises:
        DocError: ``notes.DOC_INSTRUCTIONS_CHANGED_MESSAGE``.
    """
    pinned = clean.get("expected_instructions_version")
    if pinned is None:
        return
    if doc["instructions_version"] != pinned and doc["has_instructions"]:
        raise DocError(doc_notes.DOC_INSTRUCTIONS_CHANGED_MESSAGE)


_NOTES_FORMS = ("block", "text")


async def _refuse_unseen_notes(caller: Caller, doc: dict, notes_form: str) -> None:
    """Invariant 1 (notes before writes): raise :class:`DocNotesUnseen`
    when this recording run has not seen the doc's current notes
    (``notes.write_refused()``), after recording the delivery it carries.

    Decides on the row first (no notes query for a doc without notes or
    with a current record), then on the freshly loaded state, so notes
    emptied meanwhile never refuse. ``notes_form`` says what the model
    will be shown: ``"block"`` (the write tools: the whole block) or
    ``"text"`` (the write_doc pre-card: ``render_notes_text()``). When the
    text had to cut the instructions, the refusal records the seen
    instructions version unchanged (never instructions the model was not
    shown whole), so the retry is refused again.

    Raises:
        DocNotesUnseen: the refusal (its delivery recorded).
        DocError: the doc is gone (``doc_not_found_message``).
    """
    if not doc_notes.records_delivery(caller.run_kind, caller.conversation_id):
        return
    seen = notes_seen(caller, doc["id"])
    if not doc_notes.write_refused(doc, seen):
        return
    state, block = await load_notes(doc["id"])
    if not doc_notes.write_refused(state, seen):
        return
    text = doc_notes.render_notes_text(block)
    instructions_version = state["instructions_version"]
    if notes_form == "text" and not text.instructions_whole:
        instructions_version = seen.instructions if seen else 0
    # Last step, no await between the record and the raise
    # (record_notes_delivery).
    record_notes_delivery(
        caller, doc["id"], instructions_version, state["memories_version"],
    )
    raise DocNotesUnseen(
        doc_notes.DOC_NOTES_UNSEEN_MESSAGE.format(title=doc["title"]),
        block,
        text.text,
    )


async def _resolve_write(
    caller: Caller,
    doc_id: str,
    operation: str,
    params: dict,
    *,
    bypass_approval: bool = False,
    notes_form: str = "block",
    check_notes: bool = True,
) -> tuple[dict, DocAccess, dict]:
    """Gate, normalize, look up and check everything that is not the verdict.

    Order: gate -> params -> visibility (hidden == missing) -> denied ->
    edit's read-before-edit -> add_image's conversation requirement ->
    doc notes (only with ``check_notes`` and the ``doc_notes`` gate open
    for the caller). The approval verdict is left to the caller, so a
    model meets the notes before it drafts a card.

    The notes step: with ``bypass_approval`` (approve-time execute) only
    the card's pinned ``expected_instructions_version`` is compared with
    the live row (:func:`_check_pinned_instructions`); otherwise a
    recording run that has not seen the current notes is refused once with
    :class:`DocNotesUnseen` (:func:`_refuse_unseen_notes`), its notes in
    ``notes_form`` (``"block"`` for the tools, ``"text"`` for the
    write_doc pre-card).

    Raises:
        DocDisabled: the ``docs`` gate is closed.
        DocNotesUnseen: the notes refusal (not with ``bypass_approval``).
        DocError: bad params, hidden/missing doc, denied, unread, no
            conversation for add_image, or a moved instructions pin.
        ValueError: an unknown ``notes_form`` (a programming error).
    """
    if notes_form not in _NOTES_FORMS:
        raise ValueError(f"Unknown notes_form: {notes_form!r}")
    require_enabled(caller)
    clean = _normalize_write_params(operation, params)
    doc, access = await _get_visible_doc(caller, doc_id)
    if access.write == "denied":
        raise DocError(access.deny_reason)
    if operation == "edit" and not _has_read(caller, doc["id"]):
        raise DocError(
            f"Doc '{doc['title']}' has not been read in this conversation. "
            "Read it with read_doc before editing."
        )
    if operation == "add_image" and not caller.conversation_id:
        raise DocError(
            "add_doc_image copies a file from the conversation workspace, so "
            "it needs a conversation."
        )
    if check_notes and notes_enabled(caller):
        if bypass_approval:
            _check_pinned_instructions(doc, clean)
        else:
            await _refuse_unseen_notes(caller, doc, notes_form)
    return doc, access, clean


def _apply_edit_text(body: str, clean: dict) -> tuple[str, int]:
    from chat.action_request_types._skill_content_edit import apply_content_edit

    try:
        return apply_content_edit(
            body,
            clean["old_string"],
            clean["new_string"],
            clean["replace_all"],
            max_size=constants.DOC_MAX_CONTENT_SIZE,
            noun="doc",
            reread_hint="re-read it with read_doc",
        )
    except ValueError as exc:
        raise DocError(str(exc)) from None


def _append_text(body: str, content: str, ensure_blank_line: bool) -> str:
    """``body`` + separator + ``content`` (always newline-terminated).

    With ``ensure_blank_line`` the appended block is separated from the
    existing text by a blank line (``"\\n\\n"`` after unterminated text,
    ``"\\n"`` after a single trailing newline, nothing after a blank line);
    without it the block just starts on a new line.
    """
    if not content.endswith("\n"):
        content += "\n"
    if not body:
        sep = ""
    elif ensure_blank_line:
        if body.endswith("\n\n"):
            sep = ""
        elif body.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
    else:
        sep = "" if body.endswith("\n") else "\n"
    return body + sep + content


def _image_markdown(alt: str, asset_name: str) -> str:
    stem = asset_name.rsplit(".", 1)[0]
    text = (alt or stem).replace("\r", " ").replace("\n", " ")
    text = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"![{text}](assets/{asset_name})"


def _check_image(data: bytes) -> str:
    ext = doc_files.sniff_image_type(data)
    if ext is None:
        raise DocError("Not a supported raster image (png, jpg, gif, webp).")
    return ext


def _read_image_file(path: Path, display: str) -> bytes:
    """Read a resolved workspace file: no symlink leaf, regular file only,
    never blocking on a FIFO, capped at ``DOC_MAX_IMAGE_SIZE``."""
    cap = constants.DOC_MAX_IMAGE_SIZE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise DocError(f"File not found in workspace: {display}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DocError(f"Refusing to read a symbolic link: {display}") from None
        raise DocError(f"Cannot read {display}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DocError(f"Not a regular file: {display}")
        if st.st_size > cap:
            raise DocError(
                f"Image {display} is {st.st_size} bytes, over the {cap}-byte "
                "per-image limit."
            )
        with os.fdopen(fd, "rb") as f:
            fd = -1  # ownership passed to the file object
            data = f.read(cap + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > cap:
        raise DocError(
            f"Image {display} is over the {cap}-byte per-image limit."
        )
    return data


def _check_expected_sha256(data: bytes, clean: dict) -> str:
    """The image's sha256; raises when ``clean`` pins a different one."""
    digest = hashlib.sha256(data).hexdigest()
    expected = clean.get("expected_sha256")
    if expected and expected != digest:
        raise DocError(
            f"The workspace file {clean['workspace_path']} changed since the "
            "change was proposed; propose it again with the current file."
        )
    return digest


async def _load_workspace_image(caller: Caller, workspace_path: str) -> tuple[bytes, str]:
    """Bytes + name hint of a workspace image, validated by magic bytes."""
    from chat.action_request_types._io_attachments import resolve_workspace_file

    try:
        # Path guards only (absolute / '..' / containment / exists / regular);
        # the doc-specific size cap is checked on the descriptor below, so
        # the error names the 5 MB image limit, not the 50 MB upload cap.
        # Conversation workspace only (project conversations included);
        # the shared project workspace is never searched -- the project id
        # only adds the copy-first hint (copy_file proj:// -> chat://) to a
        # not-found error.
        path = await resolve_workspace_file(
            caller.conversation_id,
            caller.project_id,
            workspace_path,
            max_size_bytes=sys.maxsize,
        )
    except RuntimeError as exc:
        raise DocError(str(exc)) from None
    except (ValueError, OSError):
        # NUL bytes, over-long names, ... -- never echo the resolved
        # server-side path back to the model.
        raise DocError(f"File not found in workspace: {workspace_path}") from None
    data = await asyncio.to_thread(_read_image_file, path, workspace_path)
    _check_image(data)
    return data, PurePosixPath(workspace_path.replace("\\", "/")).name


# ---------------------------------------------------------------------------
# Writes: appliers
#
# apply_write_operation holds the per-doc asyncio lock around the WHOLE
# write (re-resolved access -> file op in a thread ->
# doc_store.update_after_write -> events) and shields it from cancellation,
# so DB counters and updated_at land in write order; the appliers below
# run with that lock held. Body changes go through
# files.modify_body, which reads, transforms and writes doc.md in one
# critical section of the per-doc threading lock (a separate read_body +
# write_body pair would let concurrent writers clobber each other). Each
# passes its own write_source -- the writer of the NEW body -- which
# files.py records in doc.meta.json; a later snapshot copies that file into
# its revision sidecar, so attribution follows the body itself, not the
# row's last_write_source (which asset-only writes also bump).
# ---------------------------------------------------------------------------


async def _write_edit(caller: Caller, doc: dict, clean: dict, write_source: str) -> dict:
    doc_id = doc["id"]
    replaced: list[int] = []

    def transform(current: str) -> str:
        new_body, count = _apply_edit_text(current, clean)
        replaced.append(count)
        return new_body

    new_body, size = await _files(
        doc_files.modify_body, doc_id, transform,
        write_source=write_source,
    )
    updated = await _finish_write(doc_id, content_size=size, write_source=write_source)
    return {
        "replaced": replaced[0],
        "total_lines": _total_lines(new_body),
        "updated_at": updated["updated_at"],
    }


async def _write_append(caller: Caller, doc: dict, clean: dict, write_source: str) -> dict:
    doc_id = doc["id"]

    def transform(current: str) -> str:
        new_body = _append_text(current, clean["content"], clean["ensure_blank_line"])
        _check_body_size(new_body)
        return new_body

    new_body, size = await _files(
        doc_files.modify_body, doc_id, transform,
        write_source=write_source,
    )
    updated = await _finish_write(doc_id, content_size=size, write_source=write_source)
    return {
        "appended_lines": _total_lines(clean["content"]),
        "total_lines": _total_lines(new_body),
        "updated_at": updated["updated_at"],
    }


async def _write_add_image(caller: Caller, doc: dict, clean: dict, write_source: str) -> dict:
    doc_id = doc["id"]
    data, name_hint = await _load_workspace_image(caller, clean["workspace_path"])
    _check_expected_sha256(data, clean)
    append = clean["placement"] == "append"
    new_body: Optional[str] = None
    if append:
        # Pre-check with the provisional name so an over-cap body never
        # leaves a freshly stored asset behind (other service writers
        # are held off by the lock, so the body cannot grow meanwhile).
        provisional = (
            f"{doc_files.sanitize_asset_name(name_hint)}.{_check_image(data)}"
        )
        body = await _files(doc_files.read_body, doc_id)
        _check_body_size(
            _append_text(body, _image_markdown(clean["alt"], provisional), True)
        )
    info = await _files(doc_files.add_asset, doc_id, name_hint, data)
    markdown = _image_markdown(clean["alt"], info.name)
    if append:

        def transform(current: str) -> str:
            appended = _append_text(current, markdown, True)
            _check_body_size(appended)
            return appended

        try:
            new_body, size = await _files(
                doc_files.modify_body, doc_id, transform,
                write_source=write_source,
            )
        except DocError:
            # The asset landed (assets are additive); keep the counter
            # honest, then report the body failure.
            await _record_asset_only(doc_id, info.asset_count, write_source)
            raise
    else:
        fresh = await doc_store.get_doc(doc_id, with_shares=False)
        if fresh is None:
            raise DocError(doc_not_found_message(doc_id))
        size = fresh["content_size"]
    updated = await _finish_write(
        doc_id,
        content_size=size,
        asset_count=info.asset_count,
        write_source=write_source,
    )
    result = {
        "asset": info.name,
        "markdown": markdown,
        "appended": append,
        "asset_count": info.asset_count,
        "updated_at": updated["updated_at"],
    }
    if append:
        result["total_lines"] = _total_lines(new_body)
    return result


async def _record_asset_only(doc_id: str, asset_count: int, write_source: str) -> None:
    fresh = await doc_store.get_doc(doc_id, with_shares=False)
    if fresh is None:
        return
    try:
        await _finish_write(
            doc_id,
            content_size=fresh["content_size"],
            asset_count=asset_count,
            write_source=write_source,
        )
    except DocError:
        pass  # deleted meanwhile; the caller re-raises the body failure


_WRITERS = {
    "edit": _write_edit,
    "append": _write_append,
    "add_image": _write_add_image,
}


async def apply_write_operation(
    caller: Caller,
    doc_id: str,
    operation: str,
    params: dict,
    *,
    write_source: Optional[str],
    bypass_approval: bool = False,
) -> dict:
    """Run one write (``"edit"``, ``"append"`` or ``"add_image"``).

    ``params`` are the operation's ``suggested_request.params`` without
    ``operation``/``doc_id`` (edit: old_string, new_string, replace_all;
    append: content, ensure_blank_line; add_image: workspace_path, alt,
    placement). The write_doc action request calls this at approve time
    with ``bypass_approval=True``: an ``approval`` verdict then proceeds,
    while hidden/missing (``doc_not_found_message``, e.g. the share was
    revoked or the doc deleted meanwhile) and ``denied`` (``deny_reason``)
    still refuse, and edit still requires the read sidecar.

    Order of the refusals (:func:`_resolve_write`): denied -> unread ->
    doc notes -> the approval dry-run / ``approval_required``. For a tool
    write the notes step runs once, in the first pass outside the lock (an
    instructions change landing after it applies from the next write on):
    a recording run that has not seen the doc's notes gets
    :class:`DocNotesUnseen`. With ``bypass_approval`` the seen record is
    skipped and only a moved ``expected_instructions_version`` pin (the
    write_doc card's, a server-only key in ``params``) refuses, with
    ``notes.DOC_INSTRUCTIONS_CHANGED_MESSAGE``; that comparison is pure, so
    it runs again on the row re-read under the lock.

    Raises:
        DocDisabled, DocApprovalRequired (unless bypass_approval),
        DocNotesUnseen (unless bypass_approval), DocError.
    """
    # First pass outside the lock: hidden / missing / denied / unread docs
    # and notes refusals never take (or create) a per-doc lock.
    doc, access, clean = await _resolve_write(
        caller, doc_id, operation, params, bypass_approval=bypass_approval,
    )
    if access.write == "approval" and not bypass_approval:
        # Dry-run first so a stale old_string, a bad image or an over-cap
        # body fails now instead of after the model forwarded the request.
        await _compute_preview(caller, doc, access, operation, clean)
        raise DocApprovalRequired(
            approval_required_message(access),
            _suggested_request(operation, doc["id"], clean),
        )
    source = _write_source_or_default(write_source, caller)

    async def _locked() -> dict:
        async with _write_lock(doc["id"]):
            # Re-resolve under the lock: a share added or revoked while
            # this write queued must change the verdict (the approval card
            # and the hidden / read-only refusals are decided here). A
            # tool write's notes step is not repeated (spec 00011 section
            # 10); an approved card's pin check is, on the fresh row.
            fresh_doc, fresh_access, fresh_clean = await _resolve_write(
                caller, doc_id, operation, params,
                bypass_approval=bypass_approval, check_notes=bypass_approval,
            )
            if fresh_access.write == "approval" and not bypass_approval:
                raise DocApprovalRequired(
                    approval_required_message(fresh_access),
                    _suggested_request(operation, fresh_doc["id"], fresh_clean),
                )
            return await _WRITERS[operation](caller, fresh_doc, fresh_clean, source)

    return await _shielded(_locked())


async def preview_write_operation(
    caller: Caller, doc_id: str, operation: str, params: dict,
) -> dict:
    """Compute a write without performing it (the write_doc pre-card).

    Resolves access exactly like :func:`apply_write_operation` (same
    errors), except that an ``approval`` (or ``free``) verdict never
    raises -- the caller inspects ``access``. Reads the live body and
    applies the operation in memory.

    The doc notes step is the tools' (a recording run that has not seen
    the notes gets :class:`DocNotesUnseen`, its delivery recorded), with
    the notes as text: ``notes_text`` is ``render_notes_text()`` of the
    block, and when that cut the instructions the seen instructions
    version is left where it was, so the retry is refused again.

    Returns:
        ``{"doc", "access", "current_body", "new_body", "replacements"}``;
        add_image adds ``"asset_name_preview"`` (sanitized stem + sniffed
        extension; the stored name may get a ``-2`` suffix on collision),
        ``"image_bytes_size"`` and ``"markdown"``. For add_image with
        placement ``"none"`` the body is unchanged. ``doc`` is the live row
        (its ``instructions_version`` is what the card pins).
    """
    doc, access, clean = await _resolve_write(
        caller, doc_id, operation, params, notes_form="text",
    )
    return await _compute_preview(caller, doc, access, operation, clean)


async def _compute_preview(
    caller: Caller, doc: dict, access: DocAccess, operation: str, clean: dict,
) -> dict:
    current_body = await _files(doc_files.read_body, doc["id"])
    result: dict[str, Any] = {
        "doc": doc,
        "access": access,
        "current_body": current_body,
    }
    if operation == "edit":
        new_body, replacements = _apply_edit_text(current_body, clean)
    elif operation == "append":
        new_body = _append_text(current_body, clean["content"], clean["ensure_blank_line"])
        _check_body_size(new_body)
        replacements = 0
    else:
        data, name_hint = await _load_workspace_image(caller, clean["workspace_path"])
        count, total = await _files(doc_files.asset_stats, doc["id"])
        if count >= constants.DOC_MAX_ASSETS:
            raise DocError(
                f"This doc already has {count} images, the maximum is "
                f"{constants.DOC_MAX_ASSETS}."
            )
        if total + len(data) > constants.DOC_MAX_ASSETS_TOTAL_BYTES:
            raise DocError(
                f"Adding this image would bring the doc's images to "
                f"{total + len(data)} bytes, over the "
                f"{constants.DOC_MAX_ASSETS_TOTAL_BYTES}-byte limit."
            )
        name = f"{doc_files.sanitize_asset_name(name_hint)}.{_check_image(data)}"
        markdown = _image_markdown(clean["alt"], name)
        new_body = current_body
        if clean["placement"] == "append":
            new_body = _append_text(current_body, markdown, True)
            _check_body_size(new_body)
        replacements = 0
        result.update({
            "asset_name_preview": name,
            "image_bytes_size": len(data),
            "image_sha256": _check_expected_sha256(data, clean),
            "markdown": markdown,
        })
    result["new_body"] = new_body
    result["replacements"] = replacements
    return result


# ---------------------------------------------------------------------------
# Writes: the tool-facing entry points
# ---------------------------------------------------------------------------


async def edit_doc(
    caller: Caller,
    doc_id: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    write_source: Optional[str] = None,
) -> dict:
    """Exact search/replace (``apply_content_edit`` semantics).

    Requires the doc in this conversation's read sidecar.

    Returns:
        ``{replaced, total_lines, updated_at}``.
    """
    return await apply_write_operation(
        caller, doc_id, "edit",
        {"old_string": old_string, "new_string": new_string, "replace_all": replace_all},
        write_source=write_source,
    )


async def append_to_doc(
    caller: Caller,
    doc_id: str,
    content: str,
    ensure_blank_line: bool = True,
    *,
    write_source: Optional[str] = None,
) -> dict:
    """Append ``content`` (no prior read needed; nothing is overwritten).

    Returns:
        ``{appended_lines, total_lines, updated_at}``.
    """
    return await apply_write_operation(
        caller, doc_id, "append",
        {"content": content, "ensure_blank_line": ensure_blank_line},
        write_source=write_source,
    )


async def add_doc_image(
    caller: Caller,
    doc_id: str,
    workspace_path: str,
    alt: str = "",
    placement: str = "append",
    *,
    write_source: Optional[str] = None,
) -> dict:
    """Copy a workspace raster image into the doc's ``assets/``.

    The access verdict gates the whole call for both placements (storing
    the asset is itself a write to the doc). ``placement="append"`` also
    appends ``![alt](assets/<name>)`` -- one write, one approval;
    ``"none"`` only returns the snippet for a later edit_doc.

    Returns:
        ``{asset, markdown, appended, asset_count, updated_at,
        total_lines?}`` (``total_lines`` only when appended).
    """
    return await apply_write_operation(
        caller, doc_id, "add_image",
        {"workspace_path": workspace_path, "alt": alt, "placement": placement},
        write_source=write_source,
    )


# ---------------------------------------------------------------------------
# UI: the (always refused) mode switch, and the delete that must serialize
# with model writes
# ---------------------------------------------------------------------------


def mode_switch_refusal(doc: dict) -> DocRequestError:
    """Why ``doc``'s mode cannot be switched -- no doc's can in v1.

    A user doc is always private (``user_doc_mode_private``); a project doc
    keeps its project's immutable mode (``project_doc_mode_inherited``).
    ``PUT /docs/{id}/mode`` stays registered and raises this after its
    404 / 403 checks.
    """
    if doc.get("project_id") is None:
        return DocRequestError("user_doc_mode_private", user_doc_mode_private_message())
    return DocRequestError(
        "project_doc_mode_inherited",
        "Project docs take their project's mode; it cannot be switched.",
    )


async def delete_doc_from_ui(user: dict, doc_id: str) -> dict:
    """Owner-only delete, under the doc lock: the row (shares, notes and
    note suggestions cascade), then the directory (best-effort, logged),
    then ``doc_changed {updated_at: null}`` and ``doc_list_changed`` to the
    doc's audience (owner + share recipients, every connected user for an
    everyone row), captured under the lock BEFORE the rows go, and last
    ``doc_note_suggestions_changed`` for every conversation that had a
    pending note suggestion on the doc (also captured before the delete).

    Raises:
        DocDisabled, DocError (hidden/missing), DocRequestError ``forbidden``.
    """
    caller = Caller(user=user, conversation_id=None, project_id=None,
                    is_public=False, run_kind="ui")
    doc, _access = await get_visible_doc(caller, doc_id)
    if doc["owner_id"] != user["id"]:
        raise DocRequestError("forbidden", "Only the doc's owner can delete it.")

    async def _locked() -> dict:
        async with _write_lock(doc["id"]):
            # The share routes change shares under this same lock, so the
            # roster read here is the one the delete removes.
            current = await doc_store.get_doc(doc["id"], with_shares=True)
            if current is None:
                raise DocError(doc_not_found_message(doc_id))
            audience = _events().doc_audience(current)
            # Who had pending note suggestions on it: their bubbles and
            # badges must drop the rows the delete takes with it.
            from chat.docs import note_suggestions
            pending_pairs = await note_suggestions.capture_pending_pairs([doc["id"]])
            if not await doc_store.delete_doc(doc["id"]):
                raise DocError(doc_not_found_message(doc_id))
            try:
                await asyncio.to_thread(doc_files.delete_doc_dir, doc["id"])
            except Exception:
                logger.warning(
                    "[docs] could not remove the directory of deleted doc %s",
                    doc["id"], exc_info=True,
                )
            events = _events()
            # Open viewers of this doc re-fetch (and land in their 404
            # state); list views refresh their window.
            events.publish_doc_gone_or_access_changed_to(audience, doc["id"])
            events.publish_doc_list_changed_to(audience)
            await note_suggestions.publish_changed_for_pairs(pending_pairs)
            return doc

    return await _shielded(_locked())
