"""The JSON shapes the doc notes routes send (docs/architecture/doc-notes.md).

Shared by the notes routes (chat/docs/note_routes.py), the suggestion
review routes (chat/docs/note_suggestion_routes.py) and the inbox
(chat/memory_inbox.py), so a memory, a suggestion row and a conflict read
the same everywhere. Pure functions, no I/O: callers resolve the user ids
of :func:`label_user_ids` with ``db.user_store.get_users_by_ids`` first.

- **Memory** (:func:`public_memory`): ``{id, content, source_label,
  created_at, updated_at}`` for every viewer. The raw ``source`` and the
  ``created_by`` / ``updated_by`` ids never leave the server; the owner
  learns who wrote a memory from its label only.
- **Suggestion row** (:func:`public_suggestion`): the store row with its
  ``kind`` renamed ``note_kind`` (``instructions`` / ``memory``), because
  the inbox tags every row ``kind`` (``user`` / ``project`` / ``doc``).
  A review row (one that went through ``note_suggestions.review_rows()``)
  always carries ``doc_title``, ``replaces_content``,
  ``current_instructions`` and ``instructions_moved``, the last two
  ``None`` / ``False`` on a memory row.
- **Notes counters** (:func:`notes_counters`): the versions and counts of a
  ``get_notes_state()`` dict, never text or an editor id; the accept
  response carries them.
- **Stale instructions** (:func:`stale_instructions_response`): a FLAT 409
  ``{"error": "stale_instructions", "message", "current": {"instructions",
  "instructions_version"}}``, like the docs routes' ``stale_update``.
  Every other error is the usual ``{"detail": {"error", "message", ...}}``
  (:func:`note_error`).

``source_label`` strings (:func:`source_label`), decided in phase A6:

| Source | Owner sees | Anyone else sees |
|---|---|---|
| ``ui:<the owner's id>`` | ``Added by you`` | ``Added in the Notes panel`` |
| ``ui:<another user's id>`` | ``Added by <name>`` (email when the name is empty) | ``Added in the Notes panel`` |
| ``ui:<a deleted user's id>`` | ``Added in the Notes panel`` | ``Added in the Notes panel`` |
| ``suggestion:<id>`` (live or deleted) | ``Suggested in a conversation`` | ``Suggested in a conversation`` |
| anything else | ``Added in the Notes panel`` | ``Added in the Notes panel`` |

A dead reference (a deleted user or suggestion) renders as text and never
raises.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

__all__ = [
    "MEMORY_LABEL_BY",
    "MEMORY_LABEL_PANEL",
    "MEMORY_LABEL_SUGGESTION",
    "MEMORY_LABEL_YOU",
    "STALE_INSTRUCTIONS_SAVE_MESSAGE",
    "label_user_ids",
    "note_error",
    "notes_counters",
    "public_memory",
    "public_suggestion",
    "source_label",
    "stale_instructions_response",
    "ui_source_user_id",
]

MEMORY_LABEL_PANEL = "Added in the Notes panel"
MEMORY_LABEL_SUGGESTION = "Suggested in a conversation"
MEMORY_LABEL_YOU = "Added by you"
MEMORY_LABEL_BY = "Added by {name}"

# The 409 message of the instructions save (PUT /docs/{id}/instructions);
# the accept's own is note_suggestions.STALE_INSTRUCTIONS_MESSAGE.
STALE_INSTRUCTIONS_SAVE_MESSAGE = (
    "The instructions changed since you started editing. Review the current "
    "text and save again."
)

_UI_SOURCE_RE = re.compile(r"ui:([1-9][0-9]{0,17})")
_SUGGESTION_PREFIX = "suggestion:"

# The review fields every review row carries (review_rows() leaves the
# instructions pair off memory rows).
_REVIEW_DEFAULTS = {
    "current_instructions": None,
    "instructions_moved": False,
    "replaces_content": None,
}


def ui_source_user_id(source: Any) -> Optional[int]:
    """The user id of a ``ui:<user_id>`` memory source, else None."""
    if not isinstance(source, str):
        return None
    match = _UI_SOURCE_RE.fullmatch(source)
    return int(match.group(1)) if match else None


def label_user_ids(memories: Iterable[Mapping[str, Any]], *, is_owner: bool) -> set[int]:
    """The user ids :func:`source_label` needs resolved for these memories:
    the ``ui:<user_id>`` writers, for the owner only (anyone else gets no
    names, so nothing to look up)."""
    if not is_owner:
        return set()
    return {
        user_id for memory in memories
        if (user_id := ui_source_user_id(memory.get("source"))) is not None
    }


def source_label(
    source: Any,
    *,
    viewer_id: int,
    is_owner: bool,
    users: Mapping[int, Mapping[str, Any]],
) -> str:
    """The short human label of a memory's ``source`` for this viewer
    (table in the module docstring). ``users`` is the
    ``get_users_by_ids`` result covering :func:`label_user_ids`."""
    if isinstance(source, str) and source.startswith(_SUGGESTION_PREFIX):
        return MEMORY_LABEL_SUGGESTION
    writer = ui_source_user_id(source)
    if writer is None or not is_owner:
        return MEMORY_LABEL_PANEL
    if writer == viewer_id:
        return MEMORY_LABEL_YOU
    found = users.get(writer)
    if found is None:
        return MEMORY_LABEL_PANEL
    name = (found.get("name") or "").strip() or (found.get("email") or "").strip()
    return MEMORY_LABEL_BY.format(name=name) if name else MEMORY_LABEL_PANEL


def public_memory(
    memory: Mapping[str, Any],
    *,
    viewer_id: int,
    is_owner: bool,
    users: Mapping[int, Mapping[str, Any]],
) -> dict:
    """A ``db.doc_memory_store`` memory dict as the routes send it:
    ``{id, content, source_label, created_at, updated_at}``."""
    return {
        "id": memory["id"],
        "content": memory["content"],
        "source_label": source_label(
            memory.get("source"), viewer_id=viewer_id, is_owner=is_owner, users=users,
        ),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }


def public_suggestion(row: Mapping[str, Any]) -> dict:
    """A ``doc_note_suggestions`` row as the routes send it: ``kind``
    renamed ``note_kind``; a review row (it has ``doc_title``) also gets
    every review field, ``None`` / ``False`` where ``review_rows()`` left
    one off."""
    out = {key: value for key, value in row.items() if key != "kind"}
    out["note_kind"] = row["kind"]
    if "doc_title" in row:
        for key, default in _REVIEW_DEFAULTS.items():
            out.setdefault(key, default)
    return out


def notes_counters(state: Mapping[str, Any]) -> dict:
    """``{doc_id, instructions_version, memories_version, has_instructions,
    memory_count}`` of a ``get_notes_state()`` dict: no text, no editor."""
    return {
        "doc_id": state["doc_id"],
        "instructions_version": int(state["instructions_version"]),
        "memories_version": int(state["memories_version"]),
        "has_instructions": bool(state["has_instructions"]),
        "memory_count": int(state["memory_count"]),
    }


def note_error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    """``HTTPException(detail={"error": code, "message": message, **extra})``,
    the shape of every notes error but the stale-instructions conflict."""
    return HTTPException(
        status_code=status_code, detail={"error": code, "message": message, **extra},
    )


def stale_instructions_response(message: str, current: Mapping[str, Any]) -> JSONResponse:
    """The flat 409 ``stale_instructions`` body, ``current`` cut to the two
    fields the editor needs (no editor identity)."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "stale_instructions",
            "message": message,
            "current": {
                "instructions": current["instructions"],
                "instructions_version": int(current["instructions_version"]),
            },
        },
    )
