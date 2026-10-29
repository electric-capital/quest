"""HTTP routes for reviewing doc note suggestions (spec 00011 section 7,
phase A6; docs/architecture/doc-notes.md, Review).

The doc-notes twin of the suggestion half of chat/project_memory_routes.py.
A model proposes a change to a Quest Doc's notes with ``suggest_doc_note``
(chat/docs/note_suggestions.py); these routes let the suggestion's own
user list, accept and dismiss it:

| Method | Path | Answers |
|---|---|---|
| GET | ``/app/api/docs/{doc_id}/note-suggestions`` | the caller's own pending suggestions for this doc, oldest first (the Notes panel's review block) |
| GET | ``/app/api/conversations/{conversation_id}/doc-note-suggestions`` | the conversation's pending suggestions, oldest first (the composer bubble) |
| POST | ``/app/api/doc-note-suggestions/{suggestion_id}/accept`` | apply one to the doc's notes |
| POST | ``/app/api/doc-note-suggestions/{suggestion_id}/dismiss`` | resolve one without a change |

Auth: ``get_current_user_cookie_or_apikey_checked`` (401 without
credentials, 403 ``access_denied`` for a user the admission policy no
longer lets in).

Gating: none. Neither ``doc_notes`` nor ``docs`` is checked, by any route:
a pending row must always be reviewable (spec section 9), so the Slack
reminder, which counts every pending row, never points at something the
user cannot find, and a person accepting a row while a gate is closed is a
person acting.

Check order and errors:

- ``GET /docs/{doc_id}/note-suggestions``: the doc is resolved with the UI
  verdict (``run_kind="ui"``) without the docs gate; a doc the user cannot
  see, including one whose share was revoked while the user's rows are
  still pending, is 404 ``doc_not_found`` with exactly the body of a
  missing id and of every docs route (``chat.docs.routes._doc_not_found``).
  Then the user's pending rows for the doc, never another user's.
- ``GET /conversations/{conversation_id}/doc-note-suggestions``: 404
  ``conversation_not_found`` for a conversation the user does not own or
  that does not exist (``require_owned_conversation``). Rows on a doc the
  user can no longer see stay listed, its fields blanked by
  ``review_rows()`` (``doc_title`` ``""``, ``current_instructions`` null,
  ``instructions_moved`` false, ``replaces_content`` null).
- ``POST .../accept``: ``accept_suggestion()`` checks, in order, the row
  (404 ``suggestion_not_found`` for a missing id, another user's row or a
  row whose doc is gone,
  409 ``suggestion_resolved``), access on a fresh doc row (403
  ``forbidden``: a memory needs edit access now, instructions need
  ownership), then the store's refusals: 400 ``invalid_memory`` /
  ``invalid_instructions`` (also a malformed
  ``expected_instructions_version``), 409 ``memories_cap_reached``, 409
  ``duplicate_memory`` (the detail adds ``memory_id``, the memory that
  already has the text), 409 ``stale_instructions``. On every refusal the
  row stays pending and the notes are unchanged.
- ``POST .../dismiss``: 404 ``suggestion_not_found``, 409
  ``suggestion_resolved``; needs no edit access (a revoked or read-only
  recipient clears their own rows).

Errors are ``{"detail": {"error", "message", ...}}`` (``note_error()``)
except ``stale_instructions``, a FLAT 409 ``{"error": "stale_instructions",
"message", "current": {"instructions", "instructions_version"}}``
(``stale_instructions_response()``): the row re-renders its diff against
``current`` and the user accepts again with that version.

Shapes (chat/docs/note_payloads.py):

- Lists: ``{"suggestions": [row, ...], "pending_count": n}``; each row is a
  ``review_rows()`` row through ``public_suggestion()``: the store row with
  ``kind`` renamed ``note_kind``, plus ``doc_title``, ``replaces_content``,
  ``current_instructions`` and ``instructions_moved``.
- Accept body (optional): ``{"content"?, "expected_instructions_version"?}``.
  Both are typed loosely so a malformed value gets this API's own 400 code
  from the service; a body that is not a JSON object, or not JSON at all,
  is still FastAPI's 422, as on the twins. ``content`` overrides the proposed
  text (``""`` clears the instructions); ``expected_instructions_version``
  is the version an instructions row's diff was rendered against (default:
  the version the proposal was written against; a memory row ignores it).
- Accept 200: ``{"suggestion": row, "memory": memory | null, "notes":
  counters}``: the accepted row (``public_suggestion()``), the memory
  written (``public_memory()``, labelled for the caller; null for
  instructions) and the notes counters after the accept
  (``notes_counters()``). No editor id or raw ``source`` leaves the server.
- Dismiss 200: ``{"suggestion": row}``.

Realtime: ``accept_suggestion()`` and ``dismiss_suggestion()`` publish
``doc_note_suggestions_changed`` (and, for an accept that changed the
notes, ``doc_notes_changed``) themselves; the routes publish nothing.
Suggestion text is never logged.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.conversation_access import require_owned_conversation
from chat.docs import note_payloads
from chat.docs import note_suggestions
from chat.docs import service as doc_service
from chat.docs.note_suggestions import HTTP_STATUS, NoteSuggestionError
from chat.docs.routes import _doc_not_found, _ui_caller
from db import doc_store
from db import doc_note_suggestion_store as suggestion_store
from db.user_store import get_users_by_ids

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)


class AcceptDocNoteSuggestionRequest(BaseModel):
    """Optional accept body. Loosely typed on purpose: the service validates
    both fields and answers ``invalid_memory`` / ``invalid_instructions``."""

    content: Optional[Any] = None
    expected_instructions_version: Optional[Any] = None


def _review_refusal(exc: NoteSuggestionError) -> JSONResponse:
    """The HTTP form of a review refusal. Returns the flat 409
    ``stale_instructions`` response; raises every other code as the usual
    ``HTTPException`` (``HTTP_STATUS`` gives the status)."""
    if exc.code == "stale_instructions":
        return note_payloads.stale_instructions_response(str(exc), exc.current)
    if exc.code == "duplicate_memory":
        raise note_payloads.note_error(409, exc.code, str(exc), memory_id=exc.memory_id)
    raise note_payloads.note_error(HTTP_STATUS[exc.code], exc.code, str(exc))


async def _review_list(user_id: int, rows: list[dict]) -> dict:
    review = await note_suggestions.review_rows(user_id, rows)
    suggestions = [note_payloads.public_suggestion(row) for row in review]
    return {"suggestions": suggestions, "pending_count": len(suggestions)}


async def _public_memory(user_id: int, doc_id: str, memory: dict) -> dict:
    """The accepted memory labelled for the caller: owner or not is read
    after the accept (a doc gone meanwhile counts as not owned).

    The accept has landed by now, so a failed lookup must not turn it into
    an error: it is logged and the memory gets the neutral label."""
    try:
        doc = await doc_store.get_doc(doc_id, with_shares=False)
        is_owner = doc is not None and doc["owner_id"] == user_id
        user_ids = note_payloads.label_user_ids([memory], is_owner=is_owner)
        users = await get_users_by_ids(user_ids) if user_ids else {}
    except Exception:
        logger.warning(
            "[doc-notes] could not label an accepted memory (doc=%s)", doc_id,
            exc_info=True,
        )
        is_owner, users = False, {}
    return note_payloads.public_memory(
        memory, viewer_id=user_id, is_owner=is_owner, users=users,
    )


@router.get("/docs/{doc_id}/note-suggestions")
async def list_doc_note_suggestions(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The caller's own pending suggestions for a doc they can see, oldest
    first (the Notes panel's review block)."""
    try:
        doc, _access = await doc_service._get_visible_doc(_ui_caller(user), doc_id)
    except doc_service.DocError:
        raise _doc_not_found(doc_id)
    user_id = user["id"]
    rows = await suggestion_store.list_pending_for_doc(user_id, doc["id"])
    return await _review_list(user_id, rows)


@router.get("/conversations/{conversation_id}/doc-note-suggestions")
async def list_conversation_doc_note_suggestions(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Pending doc note suggestions of a conversation the user owns, oldest
    first (the composer bubble); a doc no longer visible keeps its rows,
    blanked."""
    user_id = user["id"]
    await require_owned_conversation(user_id, conversation_id)
    rows = await suggestion_store.list_pending(user_id, conversation_id)
    return await _review_list(user_id, rows)


@router.post("/doc-note-suggestions/{suggestion_id}/accept")
async def accept_doc_note_suggestion(
    suggestion_id: str,
    body: AcceptDocNoteSuggestionRequest | None = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Accept a pending suggestion: write the memory (optionally edited) or
    the instructions (behind ``expected_instructions_version``)."""
    user_id = user["id"]
    try:
        result = await note_suggestions.accept_suggestion(
            user, suggestion_id,
            content=body.content if body is not None else None,
            expected_instructions_version=(
                body.expected_instructions_version if body is not None else None
            ),
        )
    except NoteSuggestionError as exc:
        return _review_refusal(exc)
    accepted = result["suggestion"]
    memory = result["memory"]
    return {
        "suggestion": note_payloads.public_suggestion(accepted),
        "memory": (
            await _public_memory(user_id, accepted["doc_id"], memory)
            if memory is not None
            else None
        ),
        "notes": note_payloads.notes_counters(result["notes"]),
    }


@router.post("/doc-note-suggestions/{suggestion_id}/dismiss")
async def dismiss_doc_note_suggestion(
    suggestion_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Dismiss a pending suggestion; the notes are untouched. Needs no edit
    access."""
    try:
        row = await note_suggestions.dismiss_suggestion(user, suggestion_id)
    except NoteSuggestionError as exc:
        return _review_refusal(exc)
    logger.info(
        "[doc-notes] dismissed suggestion %s (doc=%s, user_id=%s)",
        suggestion_id, row["doc_id"], user["id"],
    )
    return {"suggestion": note_payloads.public_suggestion(row)}
