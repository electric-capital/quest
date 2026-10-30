"""Doc note suggestions: the ``suggest_doc_note`` body and the review path
(docs/architecture/doc-notes-review.md).

The doc-notes twin of chat/project_memories.py. A model proposes a change
to a Quest Doc's notes with the non-blocking ``suggest_doc_note`` tool:
``kind="memory"`` adds one fact (or, with ``replaces_memory_id``, replaces
one), ``kind="instructions"`` proposes the complete new text of the
owner's instructions field. :func:`register_suggestion` checks the
proposal and writes a pending row in ``doc_note_suggestions``
(db/doc_note_suggestion_store.py) for the conversation's user to review;
nothing reaches the notes until that person accepts it
(:func:`accept_suggestion`) in the web UI or the inbox.

Who may suggest (invariant 4, taint): a conversation whose write verdict on
the doc (``resolve_doc_access()``) is ``free`` or ``approval``, or
``denied`` with one of the two Slack causes (the person could write; only
the approval card cannot be opened in Slack; ``notes.suggestion_allowed()``,
which ``list_doc_notes`` shares for its ``can_suggest``), so a private
conversation can never put text into a public doc's notes. Instructions are the
owner's: only the owner's conversations may suggest a new text. The
conversation must also have seen the doc's current notes (the seen record
of chat/docs/service.py), so a model never proposes a change to notes it
has not read.

Review is the suggestion's user's alone and is never gated by ``doc_notes``
(a person accepting is a person acting; a pending row stays reviewable
while the gate is closed). Accept re-checks on a fresh row that the user
may still edit the doc (a memory) or still owns it (instructions); dismiss
needs nothing but ownership of the suggestion. A doc the user can no
longer see keeps its pending rows listed with the doc's title and current
notes blanked (:func:`review_rows`), so hidden notes never leak.

Realtime: every change to a conversation's pending set publishes
``doc_note_suggestions_changed`` to the reviewing user
(:func:`publish_changed`): a registration (also to the conversation of an
instructions suggestion it superseded), an accept, a dismiss, and the doc
delete paths (:func:`capture_pending_pairs` before the delete,
:func:`publish_changed_for_pairs` after). An accept that changed the notes
also publishes ``doc_notes_changed`` to the doc's audience through
``service.publish_notes_changed()``.

Errors: :class:`NoteSuggestionError` (a ``DocError`` with a machine
``code``; :data:`HTTP_STATUS` maps the review codes to the HTTP status of
spec section 7). Registration also raises the doc tools' own errors, so a
refusal reads exactly as it does there: ``DocDisabled`` (the ``docs`` gate),
a plain ``DocError`` with ``doc_not_found_message`` for a hidden or missing
doc and with the verdict's ``deny_reason`` for a denied write, and
``DocNotesUnseen`` (the notes block) for unseen notes. The strings the
model reads are the ``DOC_NOTE_*`` constants of chat/docs/notes.py.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from chat import storage as _storage
from chat.docs import notes as doc_notes
from chat.docs import service
from chat.docs.access import resolve_doc_access
from chat.docs.constants import (
    DOC_MAX_MEMORIES,
    DOC_NOTE_MAX_PENDING_PER_CONVERSATION,
    DOC_NOTE_MAX_PENDING_PER_DOC,
    DOC_NOTE_SUGGESTION_KINDS,
    doc_not_found_message,
    doc_notes_disabled_message,
)
from chat.docs.service import Caller, DocError, DocNotesUnseen
from db import doc_memory_store, doc_store
from db import doc_note_suggestion_store as store
from db.doc_memory_store import (
    DuplicateMemoryError,
    InvalidMemoryError,
    MemoriesCapReachedError,
)
from db.doc_note_suggestion_store import (
    KIND_INSTRUCTIONS,
    KIND_MEMORY,
    SuggestionAlreadyResolvedError,
    SuggestionTargetGoneError,
)
from db.doc_store import InvalidInstructionsError, StaleInstructionsError

logger = logging.getLogger(__name__)

__all__ = [
    "HTTP_STATUS",
    "NoteSuggestionError",
    "accept_suggestion",
    "capture_pending_pairs",
    "dismiss_suggestion",
    "publish_changed",
    "publish_changed_for_pairs",
    "register_suggestion",
    "review_rows",
]

# Run kinds that suggest from a conversation of their own; a suggestion row
# needs that conversation. The read-only run kinds are refused by their
# denied write verdict.
_CONVERSATION_RUN_KINDS = frozenset({"top_level", "slack"})

# Review messages (the UI and the inbox show them; the model never does).
SUGGESTION_NOT_FOUND_MESSAGE = "Suggestion not found."
SUGGESTION_RESOLVED_MESSAGE = "This suggestion was already accepted or dismissed."
FORBIDDEN_MEMORY_MESSAGE = (
    "You can no longer edit this doc, so you cannot change its memories. "
    "Dismiss the suggestion instead."
)
FORBIDDEN_INSTRUCTIONS_MESSAGE = "Only the doc's owner can change its instructions."
STALE_INSTRUCTIONS_MESSAGE = (
    "The doc's instructions changed since this suggestion was shown. Review "
    "it against the current text and accept again."
)
INVALID_EXPECTED_VERSION_MESSAGE = (
    "expected_instructions_version must be a non-negative integer."
)

# Review error codes -> HTTP status, for the suggestion routes.
HTTP_STATUS: dict[str, int] = {
    "suggestion_not_found": 404,
    "suggestion_resolved": 409,
    "forbidden": 403,
    "stale_instructions": 409,
    "invalid_memory": 400,
    "invalid_instructions": 400,
    "memories_cap_reached": 409,
    "duplicate_memory": 409,
}

# Bound of a SQLite INTEGER (the instructions token).
_MAX_VERSION = 2**63


class NoteSuggestionError(DocError):
    """A suggestion was refused, at registration or at review.

    ``code`` is the stable machine-readable code (registration:
    ``doc_notes_disabled``, ``invalid_request``, ``instructions_owner_only``,
    ``memory_not_found``, ``invalid_memory``, ``invalid_instructions``,
    ``conversation_pending_cap_reached``, ``doc_pending_cap_reached``,
    ``memories_cap_reached``; review: the keys of :data:`HTTP_STATUS`).
    ``current`` is set for ``stale_instructions`` only: ``{"instructions",
    "instructions_version"}`` of the doc now. ``memory_id`` is set for
    ``duplicate_memory`` only: the memory that already has the text.
    ``str(e)`` is the user/model-facing message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current: Optional[dict] = None,
        memory_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.current = current
        self.memory_id = memory_id


# One asyncio.Lock per suggesting user around checks 4-6 of
# register_suggestion, so parallel suggest calls in one model response
# cannot both pass a cap or a duplicate guard (one process, one event
# loop). Weak values, like service._write_locks.
_suggest_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


def _suggest_lock(user_id: int) -> asyncio.Lock:
    lock = _suggest_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _suggest_locks[user_id] = lock
    return lock


def _is_version(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value < _MAX_VERSION
    )


def _ui_access(user_id: int, doc: Mapping[str, Any]):
    """The UI verdict (``run_kind="ui"``), as chat/docs/routes.py computes it."""
    return resolve_doc_access(
        doc, user_id=user_id, is_public=False, project_id=None, run_kind="ui",
    )


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------


async def publish_changed(user_id: int, conversation_id: str) -> int:
    """Publish ``doc_note_suggestions_changed`` with the fresh counts to the
    reviewing user's tabs: ``pending_count`` for this conversation (the
    composer bubble) and ``total_pending`` across the user's conversations
    (the inbox).

    Returns:
        The conversation's pending count. A bus failure is logged and never
        fails the caller's write.
    """
    pending_count = await store.count_pending(user_id, conversation_id)
    total_pending = await store.count_pending_for_user(user_id)
    try:
        from chat.realtime.bus import bus
        from chat.realtime.events import make_doc_note_suggestions_changed

        bus.publish_to_user(
            user_id,
            make_doc_note_suggestions_changed(conversation_id, pending_count, total_pending),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "[doc-notes] publish failed (conversation=%s)", conversation_id,
        )
    return pending_count


async def capture_pending_pairs(doc_ids: Iterable[str]) -> list[tuple[int, str]]:
    """The ``(user_id, conversation_id)`` pairs of the pending suggestions on
    ``doc_ids``, for a delete path to call BEFORE the docs go (the rows
    cascade with them).

    Best-effort: a failure is logged and gives ``[]`` (the bubbles then
    refresh on their next event or reload). No query for empty input.
    """
    try:
        return await store.list_pending_pairs_for_docs(doc_ids)
    except Exception:
        logger.warning(
            "[doc-notes] could not list the pending suggestions of deleted docs",
            exc_info=True,
        )
        return []


async def publish_changed_for_pairs(
    pairs: Iterable[tuple[int, str]], *, exclude_user_id: Optional[int] = None,
) -> None:
    """:func:`publish_changed` for each captured pair, after the delete, so
    bubbles and badges drop the rows that went with the docs.

    ``exclude_user_id`` skips that user's pairs (a deleted account has no
    tabs to update). Best-effort per pair: a failure is logged and the
    remaining pairs are still published.
    """
    for user_id, conversation_id in pairs:
        if exclude_user_id is not None and user_id == exclude_user_id:
            continue
        try:
            await publish_changed(user_id, conversation_id)
        except Exception:
            logger.warning(
                "[doc-notes] could not publish doc_note_suggestions_changed "
                "(user=%s, conversation=%s)",
                user_id, conversation_id, exc_info=True,
            )


# ---------------------------------------------------------------------------
# Register (the suggest_doc_note body)
# ---------------------------------------------------------------------------


def _invalid(message: str) -> NoteSuggestionError:
    return NoteSuggestionError("invalid_request", message)


def _check_arguments(kind: Any, content: Any, reasoning: Any, replaces_memory_id: Any) -> Optional[str]:
    """The argument shape, which reveals nothing about the doc.

    Returns:
        ``replaces_memory_id`` stripped, None for none (a blank string
        counts as none, as in the store).

    Raises:
        NoteSuggestionError: ``invalid_request``.
    """
    if not isinstance(kind, str) or kind not in DOC_NOTE_SUGGESTION_KINDS:
        raise _invalid(doc_notes.DOC_NOTE_INVALID_KIND_MESSAGE)
    if not isinstance(content, str):
        raise _invalid(doc_notes.DOC_NOTE_INVALID_CONTENT_MESSAGE)
    if reasoning is not None and not isinstance(reasoning, str):
        raise _invalid(doc_notes.DOC_NOTE_INVALID_REASONING_MESSAGE)
    if replaces_memory_id is not None and not isinstance(replaces_memory_id, str):
        raise _invalid(doc_notes.DOC_NOTE_INVALID_REPLACES_MESSAGE)
    replaces = (replaces_memory_id or "").strip() or None
    if replaces is not None and kind == KIND_INSTRUCTIONS:
        raise _invalid(doc_notes.DOC_NOTE_REPLACES_WITH_INSTRUCTIONS_MESSAGE)
    return replaces


# Longest model-supplied memory id echoed back in a refusal.
_ECHO_MAX_CHARS = 100


def _memory_not_found(memory_id: str) -> NoteSuggestionError:
    shown = memory_id if len(memory_id) <= _ECHO_MAX_CHARS else memory_id[:_ECHO_MAX_CHARS] + "..."
    return NoteSuggestionError(
        "memory_not_found", doc_notes.DOC_NOTE_MEMORY_NOT_FOUND_MESSAGE.format(memory_id=shown),
    )


def _normalize(kind: str, content: str) -> str:
    """The stored form of the proposed text (character caps per kind).

    Raises:
        NoteSuggestionError: ``invalid_memory`` / ``invalid_instructions``;
            an empty instructions text is refused here (it is allowed
            only as a reviewer's override at accept).
    """
    try:
        text = store.normalize_suggestion_content(kind, content)
    except InvalidMemoryError as exc:
        raise NoteSuggestionError(exc.code, str(exc)) from None
    except InvalidInstructionsError as exc:
        raise NoteSuggestionError(exc.code, str(exc)) from None
    if kind == KIND_INSTRUCTIONS and text == "":
        raise NoteSuggestionError(
            InvalidInstructionsError.code, doc_notes.DOC_NOTE_EMPTY_INSTRUCTIONS_MESSAGE,
        )
    return text


async def _refuse_unseen_notes(
    caller: Caller, doc: dict, current: Optional[Mapping[str, Any]] = None,
) -> None:
    """Check 3: raise ``DocNotesUnseen`` (block form) when this conversation
    has not seen the doc's current notes (``needs_delivery()``), after
    recording the delivery it carries, so the identical retry passes.

    Mirrors ``service._refuse_unseen_notes`` but with ``needs_delivery()``:
    a model proposing a change must have seen every current note, memories
    included. Decides on ``current`` (a notes state read since the row) or
    else the row first (no notes query for a current record or a doc
    without notes), then on the freshly loaded state.
    """
    seen = service.notes_seen(caller, doc["id"])
    if not doc_notes.needs_delivery(current if current is not None else doc, seen):
        return
    state, block = await service.load_notes(doc["id"])
    if not doc_notes.needs_delivery(state, seen):
        return
    text = doc_notes.render_notes_text(block)
    # Last step, no await between the record and the raise
    # (service.record_notes_delivery).
    service.record_notes_delivery(
        caller, doc["id"], state["instructions_version"], state["memories_version"],
    )
    raise DocNotesUnseen(
        doc_notes.DOC_NOTE_SUGGEST_UNSEEN_MESSAGE.format(title=doc["title"]),
        block,
        text.text,
    )


async def register_suggestion(
    caller: Caller,
    doc_id: str,
    kind: str,
    content: str,
    reasoning: Optional[str] = None,
    replaces_memory_id: Optional[str] = None,
) -> dict:
    """The ``suggest_doc_note`` tool body: check the proposal and register a
    pending suggestion for the conversation's user to review.

    Checks, in order (spec section 6.3):

    1. the ``docs`` gate, the ``doc_notes`` gate, the argument shape, then
       visibility (a hidden doc reads exactly like a missing one);
    2. the write verdict: ``free`` or ``approval``, or ``denied`` with a
       Slack cause; then, for ``instructions``, ownership of the doc;
    3. the seen record is current (``needs_delivery()`` false);
    4. ``replaces_memory_id`` names a memory of this doc;
    5. the text (character caps; instructions not empty), then the guards:
       ``already_saved``, ``already_suggested`` (any conversation of this
       user), the per-conversation and per-doc pending caps (this user's
       rows only), and ``DOC_MAX_MEMORIES`` unless the suggestion replaces
       a memory;
    6. the insert. A new instructions suggestion supersedes (dismisses)
       this user's older pending instructions suggestion for the doc, which
       never counts against the caps it frees.

    Checks 4-6 hold a per-user lock. Afterwards
    ``doc_note_suggestions_changed`` goes to this conversation and to every
    other conversation of a superseded row.

    Returns:
        ``{"status": "suggested", "suggestion_id", "pending_count",
        "message"}``; ``{"status": "already_saved", "message"}`` (plus
        ``"memory_id"`` for a memory); or ``{"status": "already_suggested",
        "suggestion_id", "pending_count", "message"}``. ``pending_count``
        is this conversation's pending count.

    Raises:
        ValueError: a ``ui`` caller, or a ``top_level`` / ``slack`` caller
            without a conversation (programming errors).
        DocDisabled: the ``docs`` gate is closed for the user.
        DocNotesUnseen: check 3 (its delivery recorded).
        NoteSuggestionError: the codes listed on the class.
        DocError: ``doc_not_found_message(doc_id)`` for a hidden or missing
            doc (also one deleted, or whose conversation was deleted,
            before the insert), or the verdict's ``deny_reason`` for a
            denied write.
    """
    if caller.run_kind == "ui":
        raise ValueError(
            "register_suggestion() serves model runs; the UI has no "
            "conversation to suggest from."
        )
    if caller.run_kind in _CONVERSATION_RUN_KINDS and not caller.conversation_id:
        raise ValueError(
            f"A {caller.run_kind} caller needs a conversation_id to register a suggestion."
        )

    # 1. Gates, argument shape, visibility.
    service.require_enabled(caller)
    if not service.notes_enabled(caller):
        raise NoteSuggestionError("doc_notes_disabled", doc_notes_disabled_message())
    replaces = _check_arguments(kind, content, reasoning, replaces_memory_id)
    doc, access = await service.get_visible_doc(caller, doc_id)

    # 2. Taint (invariant 4: free, approval, or denied by a Slack cause
    # only), then instructions ownership.
    if not doc_notes.suggestion_allowed(access.write, access.deny_cause):
        raise DocError(access.deny_reason)
    user_id = caller.user["id"]
    if kind == KIND_INSTRUCTIONS and doc["owner_id"] != user_id:
        raise NoteSuggestionError(
            "instructions_owner_only", doc_notes.DOC_NOTE_INSTRUCTIONS_OWNER_ONLY_MESSAGE,
        )

    # 3. The conversation has seen the current notes.
    await _refuse_unseen_notes(caller, doc)

    conversation_id = caller.conversation_id
    async with _suggest_lock(user_id):
        # 4. The replaced memory.
        if replaces is not None and await doc_memory_store.get_doc_memory(
            doc["id"], replaces,
        ) is None:
            raise _memory_not_found(replaces)

        # 5. The text, then the guards in the spec's order.
        text = _normalize(kind, content)
        state = await doc_store.get_notes_state(doc["id"])
        if state is None:
            raise DocError(doc_not_found_message(doc_id))
        # Check 3 again on this state, which the guards and the base version
        # come from: notes changed since the row was read (an owner saving
        # the instructions meanwhile) must reach the model first, or the
        # suggestion would claim a base it never saw and a default accept
        # would overwrite that change unseen.
        await _refuse_unseen_notes(caller, doc, state)
        if kind == KIND_MEMORY:
            memories = await doc_memory_store.list_doc_memories(doc["id"])
            saved = next((m for m in memories if m["content"] == text), None)
            if saved is not None:
                return {
                    "status": "already_saved",
                    "memory_id": saved["id"],
                    "message": doc_notes.DOC_NOTE_ALREADY_SAVED_MEMORY_MESSAGE,
                }
        elif text == state["instructions"]:
            return {
                "status": "already_saved",
                "message": doc_notes.DOC_NOTE_ALREADY_SAVED_INSTRUCTIONS_MESSAGE,
            }

        duplicate = await store.find_pending_with_content(user_id, doc["id"], kind, text)
        if duplicate is not None:
            return {
                "status": "already_suggested",
                "suggestion_id": duplicate["id"],
                "pending_count": await store.count_pending(user_id, conversation_id),
                "message": doc_notes.DOC_NOTE_ALREADY_SUGGESTED_MESSAGE,
            }

        # A replaced instructions suggestion frees the slots it holds.
        pending_on_doc = await store.list_pending_for_doc(user_id, doc["id"])
        to_supersede = (
            [r for r in pending_on_doc if r["kind"] == KIND_INSTRUCTIONS]
            if kind == KIND_INSTRUCTIONS
            else []
        )
        here = await store.count_pending(user_id, conversation_id) - sum(
            1 for r in to_supersede if r["conversation_id"] == conversation_id
        )
        if here >= DOC_NOTE_MAX_PENDING_PER_CONVERSATION:
            raise NoteSuggestionError(
                "conversation_pending_cap_reached",
                doc_notes.DOC_NOTE_CONVERSATION_CAP_MESSAGE.format(count=here),
            )
        on_doc = len(pending_on_doc) - len(to_supersede)
        if on_doc >= DOC_NOTE_MAX_PENDING_PER_DOC:
            raise NoteSuggestionError(
                "doc_pending_cap_reached",
                doc_notes.DOC_NOTE_DOC_CAP_MESSAGE.format(count=on_doc),
            )
        if (
            kind == KIND_MEMORY
            and replaces is None
            and state["memory_count"] >= DOC_MAX_MEMORIES
        ):
            raise NoteSuggestionError(
                MemoriesCapReachedError.code,
                doc_notes.DOC_NOTE_MEMORIES_CAP_MESSAGE.format(max_memories=DOC_MAX_MEMORIES),
            )

        # 6. The insert (the store supersedes in the same transaction).
        try:
            row, superseded = await store.create_suggestion(
                user_id, doc["id"], conversation_id, kind, text,
                reasoning=reasoning,
                base_instructions_version=(
                    state["instructions_version"] if kind == KIND_INSTRUCTIONS else None
                ),
                replaces_memory_id=replaces,
            )
        except SuggestionTargetGoneError:
            # The doc (or the conversation) went after the checks: report it
            # like a missing doc.
            raise DocError(doc_not_found_message(doc_id)) from None
        except ValueError:
            # The replaced memory was deleted after check 4 (the store
            # re-checks it under the doc's write lock).
            if replaces is not None and await doc_memory_store.get_doc_memory(
                doc["id"], replaces,
            ) is None:
                raise _memory_not_found(replaces) from None
            raise

    pending_count = await publish_changed(user_id, conversation_id)
    for other in sorted({r["conversation_id"] for r in superseded} - {conversation_id}):
        await publish_changed(user_id, other)
    logger.info(
        "[doc-notes] suggested %s %s in conversation %s (doc=%s, pending=%d, superseded=%d)",
        kind, row["id"], conversation_id, doc["id"], pending_count, len(superseded),
    )
    return {
        "status": "suggested",
        "suggestion_id": row["id"],
        "pending_count": pending_count,
        "message": (
            doc_notes.DOC_NOTE_SUGGESTED_SUPERSEDED_MESSAGE
            if superseded
            else doc_notes.DOC_NOTE_SUGGESTED_MESSAGE
        ),
    }


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def _not_found() -> NoteSuggestionError:
    return NoteSuggestionError("suggestion_not_found", SUGGESTION_NOT_FOUND_MESSAGE)


def _resolved() -> NoteSuggestionError:
    return NoteSuggestionError("suggestion_resolved", SUGGESTION_RESOLVED_MESSAGE)


async def _publish_changed_quietly(user_id: int, conversation_id: str) -> None:
    """:func:`publish_changed` after a landed accept or dismiss: a failure
    (its count queries included) is logged, never raised."""
    try:
        await publish_changed(user_id, conversation_id)
    except Exception:
        logger.warning(
            "[doc-notes] could not publish doc_note_suggestions_changed "
            "(user=%s, conversation=%s)",
            user_id, conversation_id, exc_info=True,
        )


def _advance_seen(
    conversation_id: str, doc_id: str, before: Mapping[str, Any], after: Mapping[str, Any],
) -> None:
    """Advance the proposing conversation's seen record to ``after`` when it
    was current at ``before`` (the notes just before the accept), so a
    model is never refused for its own rule. Synchronous, no ``await``
    between the read and the record; storage failures are logged."""
    try:
        seen = _storage.ChatStorage.get_doc_notes_seen(conversation_id, doc_id)
        if doc_notes.needs_delivery(before, seen):
            return
        _storage.ChatStorage.record_doc_notes_seen(
            conversation_id, doc_id,
            after["instructions_version"], after["memories_version"],
        )
    except (OSError, _storage.InvalidStorageIdError):
        logger.warning(
            "[doc-notes] could not advance the seen record after an accept "
            "(conversation=%s, doc=%s)",
            conversation_id, doc_id, exc_info=True,
        )


async def accept_suggestion(
    user: dict,
    suggestion_id: str,
    *,
    content: Optional[str] = None,
    expected_instructions_version: Optional[int] = None,
) -> dict:
    """Accept one of the user's pending suggestions and apply it to the
    doc's notes (one store transaction). Not gated by ``doc_notes``.

    ``content`` overrides the proposed text (the reviewer edited it; ``""``
    clears the instructions). ``expected_instructions_version`` is the
    version an instructions row's diff was rendered against (default: the
    version the proposal was written against); a memory suggestion ignores
    it. Access is re-checked on a fresh doc row: a memory needs the UI
    verdict ``free`` (``can_edit``), instructions need ownership.

    After the commit: ``doc_note_suggestions_changed`` to the user;
    ``doc_notes_changed`` to the doc's audience when a version moved (a
    no-op accept moves none); and, when the accepted text is the suggested
    one, the proposing conversation's seen record advances to the new
    versions if it was current before the accept.

    Returns:
        ``{"suggestion": row, "memory": memory_dict_or_None, "notes":
        state}``: the accepted row, the memory written (memory kind), and
        the ``get_notes_state()`` dict after the accept.

    Raises:
        NoteSuggestionError: ``suggestion_not_found`` (missing, another
            user's, or its doc gone), ``suggestion_resolved``,
            ``forbidden``, ``stale_instructions`` (with ``current``),
            ``invalid_memory``, ``invalid_instructions`` (also a malformed
            ``expected_instructions_version``), ``memories_cap_reached``,
            ``duplicate_memory`` (with ``memory_id``). On every refusal the
            row stays pending and the notes are unchanged.
    """
    user_id = user["id"]
    row = await store.get_suggestion(user_id, suggestion_id)
    if row is None:
        raise _not_found()
    if row["status"] != "pending":
        raise _resolved()
    doc = await doc_store.get_doc(row["doc_id"], with_shares=True)
    if doc is None:
        raise _not_found()
    if row["kind"] == KIND_MEMORY:
        if _ui_access(user_id, doc).write != "free":
            raise NoteSuggestionError("forbidden", FORBIDDEN_MEMORY_MESSAGE)
        expected = None
    else:
        if doc["owner_id"] != user_id:
            raise NoteSuggestionError("forbidden", FORBIDDEN_INSTRUCTIONS_MESSAGE)
        if expected_instructions_version is not None and not _is_version(
            expected_instructions_version
        ):
            raise NoteSuggestionError(
                InvalidInstructionsError.code, INVALID_EXPECTED_VERSION_MESSAGE,
            )
        expected = expected_instructions_version

    try:
        result = await store.accept_suggestion(
            user_id, suggestion_id,
            content=content, expected_instructions_version=expected,
        )
    except SuggestionAlreadyResolvedError:
        raise _resolved() from None
    except StaleInstructionsError as exc:
        raise NoteSuggestionError(
            StaleInstructionsError.code,
            STALE_INSTRUCTIONS_MESSAGE,
            current={
                "instructions": exc.current["instructions"],
                "instructions_version": exc.current["instructions_version"],
            },
        ) from None
    except DuplicateMemoryError as exc:
        raise NoteSuggestionError(exc.code, str(exc), memory_id=exc.memory_id) from None
    except (InvalidMemoryError, InvalidInstructionsError, MemoriesCapReachedError) as exc:
        raise NoteSuggestionError(exc.code, str(exc)) from None
    if result is None:
        raise _not_found()

    accepted = result["suggestion"]
    before, after = result["notes_before"], result["notes_after"]
    # The accept has landed: nothing below may fail it.
    await _publish_changed_quietly(user_id, accepted["conversation_id"])
    if (
        before["instructions_version"] != after["instructions_version"]
        or before["memories_version"] != after["memories_version"]
    ):
        await service.publish_notes_changed(accepted["doc_id"], after)
    unedited = content is None or (
        store.normalize_suggestion_content(row["kind"], content) == row["content"]
    )
    if unedited:
        _advance_seen(accepted["conversation_id"], accepted["doc_id"], before, after)
    logger.info(
        "[doc-notes] accepted %s suggestion %s (doc=%s, edited=%s)",
        row["kind"], suggestion_id, row["doc_id"], not unedited,
    )
    return {"suggestion": accepted, "memory": result["memory"], "notes": after}


async def dismiss_suggestion(user: dict, suggestion_id: str) -> dict:
    """Dismiss one of the user's pending suggestions; the notes are
    untouched. Needs nothing but ownership of the suggestion (no edit
    access, no gate), then publishes ``doc_note_suggestions_changed``.

    Returns:
        The dismissed row.

    Raises:
        NoteSuggestionError: ``suggestion_not_found``, ``suggestion_resolved``.
    """
    user_id = user["id"]
    try:
        row = await store.dismiss_suggestion(user_id, suggestion_id)
    except SuggestionAlreadyResolvedError:
        raise _resolved() from None
    if row is None:
        raise _not_found()
    await _publish_changed_quietly(user_id, row["conversation_id"])
    return row


async def review_rows(user_id: int, rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The store's suggestion rows as the review lists show them (the
    conversation bubble, the Notes panel block, the inbox). Not gated.

    Each row gains ``doc_title``, and ``replaces_content`` (the replaced
    memory's current text; None when the row replaces nothing, is an
    instructions row, or the memory is gone, like the twins' rows).
    Instructions rows also gain ``current_instructions`` (the field now),
    ``current_instructions_version`` (its ``instructions_version``, the
    token an accept sends as ``expected_instructions_version``) and
    ``instructions_moved`` (the field moved past the version the proposal
    was written against). Those notes-derived fields describe what an
    accept would change, so only pending rows get them; a resolved row has
    ``None`` / ``None`` / False (an accepted instructions row would
    otherwise always read as moved).

    Each distinct doc is looked up once and resolved with the UI verdict.
    For a doc the user can no longer see (or that is gone) the doc's
    fields are blanked: ``doc_title`` ``""``, ``current_instructions`` and
    ``current_instructions_version`` None, ``instructions_moved`` False,
    ``replaces_content`` None, so a hidden doc's notes never leak
    (invariant 3). The row's own text stays: the user's own conversation
    wrote it.
    """
    rows = [dict(r) for r in rows]
    visible: dict[str, Optional[dict]] = {}
    states: dict[str, Optional[dict]] = {}
    for doc_id in dict.fromkeys(r["doc_id"] for r in rows):
        doc = await doc_store.get_doc(doc_id, with_shares=True)
        if doc is None or not _ui_access(user_id, doc).visible:
            visible[doc_id] = None
            continue
        visible[doc_id] = doc
        if any(
            r["doc_id"] == doc_id and r["kind"] == KIND_INSTRUCTIONS
            and r["status"] == "pending"
            for r in rows
        ):
            states[doc_id] = await doc_store.get_notes_state(doc_id)

    out: list[dict] = []
    for row in rows:
        doc = visible[row["doc_id"]]
        item = {**row, "doc_title": doc["title"] if doc is not None else ""}
        is_pending = row["status"] == "pending"
        replaces_content = None
        if row["kind"] == KIND_INSTRUCTIONS:
            state = states.get(row["doc_id"]) if doc is not None and is_pending else None
            base = row["base_instructions_version"]
            item["current_instructions"] = state["instructions"] if state else None
            item["current_instructions_version"] = (
                int(state["instructions_version"]) if state else None
            )
            item["instructions_moved"] = bool(
                state is not None
                and base is not None
                and base < state["instructions_version"]
            )
        elif doc is not None and is_pending and row["replaces_memory_id"]:
            memory = await doc_memory_store.get_doc_memory(
                row["doc_id"], row["replaces_memory_id"],
            )
            replaces_content = memory["content"] if memory is not None else None
        item["replaces_content"] = replaces_content
        out.append(item)
    return out
