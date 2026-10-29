"""HTTP routes for a Quest Doc's notes (docs/architecture/doc-notes.md,
docs/api/quest-docs-api.md): the instructions and the memories as the
Notes panel reads and edits them.

Five routes under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` (which runs
``require_user_allowed``):

- ``GET /docs/{id}/notes`` -- any viewer. The notes payload
  (:func:`_notes_payload`)::

      {"doc_id", "instructions", "instructions_version",
       "instructions_updated_at",
       "instructions_updated_by": {"id", "name", "email"} | null,
       "memories": [memory, ...],   # all of them, newest first, no budget
       "memories_version", "can_edit", "is_owner"}

  ``instructions_updated_by`` is the owner's alone (``routes._user_ref``:
  a user gone from the lookup keeps the id with null name and email);
  everyone else gets null, as with a UI row's ``last_write_user``.
  ``can_edit`` is the UI verdict ``free`` (owner or write share).
- ``PUT /docs/{id}/instructions`` ``{"instructions", "expected_instructions_version"}``
  -- owner only. ``""`` clears; identical text (after stripping) changes
  nothing. 200 with the GET payload, read after the save.
- ``POST /docs/{id}/memories`` ``{"content"}`` -- editors. 201 with the
  memory.
- ``PUT /docs/{id}/memories/{memory_id}`` ``{"content"}`` -- editors. Last
  write wins (no token); identical text changes nothing. 200 with the
  memory.
- ``DELETE /docs/{id}/memories/{memory_id}`` -- editors. 200
  ``{"success": true}``.

A memory is ``note_payloads.public_memory``: ``{id, content, source_label,
created_at, updated_at}``, its label worded for the caller (the table in
chat/docs/note_payloads.py). No response carries a memory's raw
``source``, ``created_by`` or ``updated_by``, and a non-owner never gets a
user's id, name or email.

Checks, in order, in every handler:

1. the auth dependency (401 / 403 ``access_denied``);
2. 403 ``doc_notes_disabled`` (``doc_notes_disabled_message()``) unless
   the ``doc_notes`` gate is open for the caller, before any DB work. The
   gate requires ``docs``, so this one check also covers Quest Docs being
   closed;
3. visibility through ``routes._get_doc_for_ui`` (``run_kind="ui"``): 404
   ``doc_not_found`` with the identical body for a missing and a hidden doc;
4. the role: the instructions save needs ownership (403 ``forbidden``
   "Only the doc's owner can change its instructions.", also for a write
   share, everyone share included: the instructions direct the owner's
   agent); the memory writes need the UI write verdict (403 ``forbidden``
   via ``edit_routes._require_can_edit`` for a read share);
5. the body, validated by the stores (the fields are typed loosely so a
   malformed value gets these codes; a body that is not a JSON object, or
   not JSON at all, is still FastAPI's 422, as on the share routes): 400
   ``invalid_instructions`` (not a string, NUL, over
   ``DOC_INSTRUCTIONS_MAX_CHARS``, a missing or non-integer
   ``expected_instructions_version``), 400 ``invalid_memory`` (not a
   string, NUL, empty, over ``DOC_MEMORY_MAX_CHARS``);
6. the write: 409 ``stale_instructions`` (the FLAT body of
   ``note_payloads.stale_instructions_response``, with ``current:
   {instructions, instructions_version}``), 409 ``duplicate_memory`` (with
   the existing memory's ``memory_id``), 409 ``memories_cap_reached``, 404
   ``memory_not_found`` (a missing memory or one of another doc, also
   when the doc was deleted meanwhile), 404 ``doc_not_found`` when the doc
   went before an instructions save or a memory add;
7. ``service.publish_notes_changed()`` with the ``get_notes_state()`` dict
   after the change: ``doc_notes_changed`` to the doc's audience, alone
   (no ``doc_changed`` / ``doc_list_changed``: notes never move
   ``updated_at``). Only a write that moved a version publishes; a refusal
   or an identical-text save publishes nothing. (A memory edit compares
   the memories version with the one it resolved, so another writer
   landing in between makes an identical-text edit publish once, which
   costs an open panel one re-fetch.)

Every other error is ``{"detail": {"error", "message", ...}}``
(``note_payloads.note_error`` and the docs routes' helpers). No notes
write touches the body's ``updated_at`` or ``last_write_source`` (the
stores guarantee it). Note text is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import edit_routes
from chat.docs import routes as doc_routes
from chat.docs import service as doc_service
from chat.docs.access import DocAccess
from chat.docs.constants import doc_notes_disabled_message
from chat.docs.note_payloads import (
    STALE_INSTRUCTIONS_SAVE_MESSAGE,
    label_user_ids,
    note_error,
    public_memory,
    stale_instructions_response,
)
from config.feature_gates import doc_notes_enabled_for
from db import doc_memory_store, doc_store, user_store
from db.doc_memory_store import (
    DuplicateMemoryError,
    InvalidMemoryError,
    MemoriesCapReachedError,
)
from db.doc_store import InvalidInstructionsError, StaleInstructionsError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

MEMORY_NOT_FOUND_MESSAGE = "Doc memory not found."


class SaveInstructionsRequest(BaseModel):
    # Typed loosely so every malformed body gets this API's own 400 codes
    # (the store validates both fields) instead of a FastAPI 422.
    instructions: Optional[Any] = None
    expected_instructions_version: Optional[Any] = None


class DocMemoryRequest(BaseModel):
    content: Optional[Any] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_notes_enabled(user: dict) -> None:
    """403 ``doc_notes_disabled`` unless the ``doc_notes`` gate (which
    requires ``docs``) is open for this user."""
    if not doc_notes_enabled_for(user.get("email") or ""):
        raise note_error(403, "doc_notes_disabled", doc_notes_disabled_message())


def _memory_not_found():
    return note_error(404, "memory_not_found", MEMORY_NOT_FOUND_MESSAGE)


def _memory_error(exc: Exception):
    """The HTTP error of a ``db.doc_memory_store`` refusal."""
    if isinstance(exc, DuplicateMemoryError):
        return note_error(409, exc.code, str(exc), memory_id=exc.memory_id)
    if isinstance(exc, MemoriesCapReachedError):
        return note_error(409, exc.code, str(exc))
    return note_error(400, InvalidMemoryError.code, str(exc))


def _is_owner(user: dict, doc: Mapping[str, Any]) -> bool:
    return doc["owner_id"] == user["id"]


async def _users_for(
    user: dict,
    memories: Iterable[Mapping[str, Any]],
    *,
    is_owner: bool,
    editor_id: Optional[int] = None,
) -> dict:
    """One ``get_users_by_ids`` call for the memory labels (owner only;
    the caller's own memories read "Added by you", no lookup) and the
    instructions editor; none at all when nothing needs resolving."""
    wanted = label_user_ids(memories, is_owner=is_owner)
    wanted.discard(user["id"])
    if editor_id is not None:
        wanted.add(editor_id)
    return await user_store.get_users_by_ids(wanted) if wanted else {}


async def _public_memory_for(user: dict, doc: Mapping[str, Any], memory: dict) -> dict:
    """One memory as the routes send it, labelled for ``user``."""
    is_owner = _is_owner(user, doc)
    users = await _users_for(user, [memory], is_owner=is_owner)
    return public_memory(memory, viewer_id=user["id"], is_owner=is_owner, users=users)


async def _notes_payload(user: dict, doc_id: str, doc: dict, access: DocAccess) -> dict:
    """The GET payload (module docstring) for a doc ``user`` may see.

    Reads ``get_notes_state()`` BEFORE ``list_doc_memories()`` (as
    ``service.load_notes`` does), so ``memories_version`` is never newer
    than the memories listed. 404 ``doc_not_found`` when the doc is gone.
    """
    state = await doc_store.get_notes_state(doc["id"])
    if state is None:
        raise doc_routes._doc_not_found(doc_id)
    memories = await doc_memory_store.list_doc_memories(doc["id"])
    is_owner = _is_owner(user, doc)
    editor_id = state["instructions_updated_by"] if is_owner else None
    users = await _users_for(user, memories, is_owner=is_owner, editor_id=editor_id)
    return {
        "doc_id": state["doc_id"],
        "instructions": state["instructions"],
        "instructions_version": state["instructions_version"],
        "instructions_updated_at": state["instructions_updated_at"],
        "instructions_updated_by": (
            doc_routes._user_ref(users, editor_id) if editor_id is not None else None
        ),
        "memories": [
            public_memory(memory, viewer_id=user["id"], is_owner=is_owner, users=users)
            for memory in memories
        ],
        "memories_version": state["memories_version"],
        "can_edit": access.write == "free",
        "is_owner": is_owner,
    }


async def _publish_current(doc_id: str) -> None:
    """``doc_notes_changed`` with the notes state now (after a landed
    memory write); a doc deleted meanwhile publishes nothing."""
    state = await doc_store.get_notes_state(doc_id)
    if state is not None:
        await doc_service.publish_notes_changed(doc_id, state)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/docs/{doc_id}/notes")
async def get_doc_notes(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The doc's instructions and every memory (any viewer)."""
    _require_notes_enabled(user)
    doc, access = await doc_routes._get_doc_for_ui(user, doc_id)
    return await _notes_payload(user, doc_id, doc, access)


@router.put("/docs/{doc_id}/instructions")
async def save_doc_instructions(
    doc_id: str,
    body: Optional[SaveInstructionsRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Save the instructions (owner only) against the
    ``expected_instructions_version`` the editor started from. Returns the
    notes payload."""
    _require_notes_enabled(user)
    doc, access = await doc_routes._get_doc_for_ui(user, doc_id)
    doc_routes._require_owner(user, doc, "change its instructions")
    body = body or SaveInstructionsRequest()
    expected = body.expected_instructions_version
    try:
        state = await doc_store.update_doc_instructions(
            doc["id"], body.instructions,
            expected_version=expected, user_id=user["id"],
        )
    except InvalidInstructionsError as exc:
        # Caught by its own class: it subclasses DocValidationError.
        raise note_error(400, exc.code, str(exc))
    except StaleInstructionsError as exc:
        return stale_instructions_response(STALE_INSTRUCTIONS_SAVE_MESSAGE, exc.current)
    if state is None:
        raise doc_routes._doc_not_found(doc_id)
    if state["instructions_version"] != expected:
        await doc_service.publish_notes_changed(doc["id"], state)
        logger.info(
            "[doc-notes] user %s saved the instructions of doc %s (version %s)",
            user["id"], doc["id"], state["instructions_version"],
        )
    return await _notes_payload(user, doc_id, doc, access)


@router.post("/docs/{doc_id}/memories", status_code=201)
async def add_doc_memory(
    doc_id: str,
    body: Optional[DocMemoryRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Add a memory (owner or write share). Returns it, 201."""
    _require_notes_enabled(user)
    doc, access = await doc_routes._get_doc_for_ui(user, doc_id)
    edit_routes._require_can_edit(access)
    body = body or DocMemoryRequest()
    try:
        memory = await doc_memory_store.create_doc_memory(
            doc["id"], body.content,
            source=doc_memory_store.ui_source(user["id"]), user_id=user["id"],
        )
    except (InvalidMemoryError, DuplicateMemoryError, MemoriesCapReachedError) as exc:
        raise _memory_error(exc)
    if memory is None:
        raise doc_routes._doc_not_found(doc_id)
    await _publish_current(doc["id"])
    logger.info(
        "[doc-notes] user %s added memory %s to doc %s", user["id"], memory["id"], doc["id"],
    )
    return await _public_memory_for(user, doc, memory)


@router.put("/docs/{doc_id}/memories/{memory_id}")
async def update_doc_memory(
    doc_id: str,
    memory_id: str,
    body: Optional[DocMemoryRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Replace a memory's text (owner or write share); last write wins.
    Returns the memory."""
    _require_notes_enabled(user)
    doc, access = await doc_routes._get_doc_for_ui(user, doc_id)
    edit_routes._require_can_edit(access)
    body = body or DocMemoryRequest()
    try:
        memory = await doc_memory_store.update_doc_memory(
            doc["id"], memory_id, body.content, user_id=user["id"],
        )
    except (InvalidMemoryError, DuplicateMemoryError) as exc:
        raise _memory_error(exc)
    if memory is None:
        raise _memory_not_found()
    # The store does not say whether the text changed (identical text is a
    # no-op): compare the memories version with the one resolved above.
    state = await doc_store.get_notes_state(doc["id"])
    if state is not None and state["memories_version"] != doc["memories_version"]:
        await doc_service.publish_notes_changed(doc["id"], state)
        logger.info(
            "[doc-notes] user %s updated memory %s of doc %s",
            user["id"], memory["id"], doc["id"],
        )
    return await _public_memory_for(user, doc, memory)


@router.delete("/docs/{doc_id}/memories/{memory_id}")
async def delete_doc_memory(
    doc_id: str,
    memory_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a memory (owner or write share)."""
    _require_notes_enabled(user)
    doc, access = await doc_routes._get_doc_for_ui(user, doc_id)
    edit_routes._require_can_edit(access)
    if not await doc_memory_store.delete_doc_memory(doc["id"], memory_id):
        raise _memory_not_found()
    await _publish_current(doc["id"])
    logger.info(
        "[doc-notes] user %s deleted memory %s of doc %s", user["id"], memory_id, doc["id"],
    )
    return {"success": True}
