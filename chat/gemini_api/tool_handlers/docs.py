"""Quest Docs tool handlers: thin wrappers over chat/docs/service.py.

Each handler takes the dispatch-built :class:`chat.docs.service.Caller`
and the raw tool arguments, coerces loosely-typed arguments (``"true"``,
``"10"``), calls the service and returns a JSON string. The two doc notes
tools (``list_doc_notes`` over ``service.list_doc_notes()``,
``suggest_doc_note`` over ``note_suggestions.register_suggestion()``)
follow the same pattern behind the ``doc_notes`` gate. Error shapes:

- ``{"error": "doc_notes_disabled", "message": ...}`` -- the notes tools
  only: the ``doc_notes`` feature gate is closed for the caller (which
  includes Quest Docs being closed), checked before anything else (no
  argument check, no DB work), for every run kind;
- ``{"error": "docs_disabled", "message": ...}`` -- the ``docs`` feature
  gate is closed for the user (checked before anything else, no DB work);
- ``{"error": "doc_notes_unseen", "message": ..., "doc_notes": {...}}`` --
  a write refused once because the conversation has not seen the doc's
  current notes (``doc_notes`` gate open, recording run): ``doc_notes`` is
  the notes block, the delivery is recorded, and the identical retry
  passes. Checked after the denied and unread refusals, before
  ``approval_required``. ``suggest_doc_note`` answers with the same shape
  (its own message) when the conversation has not seen the doc's current
  notes;
- ``{"error": "approval_required", "message": ...,
  "suggested_request": {"request_type": "write_doc", "params": {...}}}`` --
  a shared private doc: the model forwards ``suggested_request`` as a
  ``write_doc`` action request;
- ``{"error": "suggest_not_available", "message": ...}`` --
  ``suggest_doc_note`` in a run kind that may not suggest (sub-agent,
  cross-user subagent, inference API, script, routine): the dispatch
  backstop (``_tool_suggest_doc_note`` in chat/gemini_api/tool_dispatch.py,
  :func:`_suggest_doc_note_not_available_result`), after the gate and
  before :func:`_handle_suggest_doc_note`;
- ``{"error": "<code>", "message": ...}`` -- a ``suggest_doc_note``
  refusal with a machine code (``NoteSuggestionError``:
  ``invalid_request``, ``instructions_owner_only``, ``memory_not_found``,
  ``invalid_memory``, ``invalid_instructions``,
  ``conversation_pending_cap_reached``, ``doc_pending_cap_reached``,
  ``memories_cap_reached``);
- ``{"error": "<text>"}`` -- every other refusal (missing/hidden doc with
  the one not-found text, denied write, unread doc, bad arguments, caps).
  The notes tools answer a hidden or missing doc exactly like read_doc,
  and a suggestion refused by the doc's write verdict with its
  ``deny_reason``.

Write tools record ``last_write_source = "conversation:<id>"``.
"""

import json
from typing import Any, Awaitable, Callable

from chat.docs import note_suggestions, service
from chat.docs.constants import doc_notes_disabled_message
from chat.docs.note_suggestions import NoteSuggestionError
from chat.docs.notes import DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE
from chat.docs.service import (
    Caller,
    DocApprovalRequired,
    DocDisabled,
    DocError,
    DocNotesDisabled,
    DocNotesUnseen,
)


def _int_arg(args: dict, key: str, default=None):
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        raise DocError(f"{key} must be an integer.")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            pass
    raise DocError(f"{key} must be an integer.")


def _bool_arg(args: dict, key: str, default: bool) -> bool:
    raw = args.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    if isinstance(raw, int):
        return bool(raw)
    raise DocError(f"{key} must be a boolean.")


def _write_source(caller: Caller) -> str:
    if caller.conversation_id:
        return f"conversation:{caller.conversation_id}"
    return caller.run_kind


async def _run(caller: Caller, op: Callable[[], Awaitable[Any]]) -> str:
    """Gate first, then the operation; map service errors to JSON."""
    try:
        service.require_enabled(caller)
        result = await op()
    except DocDisabled as exc:
        return json.dumps({"error": "docs_disabled", "message": str(exc)})
    except DocNotesDisabled as exc:
        return json.dumps({"error": "doc_notes_disabled", "message": str(exc)})
    except NoteSuggestionError as exc:
        return json.dumps({"error": exc.code, "message": str(exc)})
    except DocNotesUnseen as exc:
        return json.dumps({
            "error": "doc_notes_unseen",
            "message": str(exc),
            "doc_notes": exc.notes_block,
        })
    except DocApprovalRequired as exc:
        return json.dumps({
            "error": "approval_required",
            "message": str(exc),
            "suggested_request": exc.suggested_request,
        })
    except DocError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(result)


async def _handle_list_docs(caller: Caller, args: dict) -> str:
    async def op():
        docs = await service.list_docs(
            caller,
            scope=args.get("scope") or "all",
            limit=_int_arg(args, "limit", service.LIST_DEFAULT_LIMIT),
        )
        return {"docs": docs, "count": len(docs)}
    return await _run(caller, op)


async def _handle_search_docs(caller: Caller, args: dict) -> str:
    async def op():
        return await service.search_docs(
            caller,
            args.get("query"),
            scope=args.get("scope") or "all",
            limit=_int_arg(args, "limit", service.SEARCH_DEFAULT_LIMIT),
        )
    return await _run(caller, op)


async def _handle_read_doc(caller: Caller, args: dict) -> str:
    async def op():
        return await service.read_doc(
            caller,
            args.get("doc_id"),
            start_line=_int_arg(args, "start_line"),
            end_line=_int_arg(args, "end_line"),
        )
    return await _run(caller, op)


async def _handle_create_doc(caller: Caller, args: dict) -> str:
    async def op():
        return await service.create_doc(
            caller,
            args.get("title"),
            args.get("content"),
            description=args.get("description") or "",
            target=args.get("target") or "user",
        )
    return await _run(caller, op)


async def _handle_edit_doc(caller: Caller, args: dict) -> str:
    async def op():
        return await service.edit_doc(
            caller,
            args.get("doc_id"),
            args.get("old_string"),
            args.get("new_string"),
            replace_all=_bool_arg(args, "replace_all", False),
            write_source=_write_source(caller),
        )
    return await _run(caller, op)


async def _handle_append_to_doc(caller: Caller, args: dict) -> str:
    async def op():
        return await service.append_to_doc(
            caller,
            args.get("doc_id"),
            args.get("content"),
            ensure_blank_line=_bool_arg(args, "ensure_blank_line", True),
            write_source=_write_source(caller),
        )
    return await _run(caller, op)


async def _handle_add_doc_image(caller: Caller, args: dict) -> str:
    async def op():
        return await service.add_doc_image(
            caller,
            args.get("doc_id"),
            args.get("workspace_path"),
            alt=args.get("alt") or "",
            placement=args.get("placement") or "append",
            write_source=_write_source(caller),
        )
    return await _run(caller, op)


# ---------------------------------------------------------------------------
# Doc notes (docs/architecture/doc-notes-tools.md)
# ---------------------------------------------------------------------------


def _doc_notes_closed_result(caller: Caller) -> str | None:
    """The ``doc_notes_disabled`` result when the ``doc_notes`` gate is
    closed for the caller (also when Quest Docs is: the gate requires it),
    else None. The first check of both notes tools, so a closed gate reads
    the same for every run kind."""
    if service.notes_enabled(caller):
        return None
    return json.dumps({
        "error": "doc_notes_disabled",
        "message": doc_notes_disabled_message(),
    })


def _suggest_doc_note_not_available_result() -> str:
    """The ``suggest_not_available`` refusal of the dispatch backstop for
    the run kinds that may not suggest doc notes."""
    return json.dumps({
        "error": "suggest_not_available",
        "message": DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE,
    })


async def _run_notes(caller: Caller, op: Callable[[], Awaitable[Any]]) -> str:
    """:func:`_run` behind the ``doc_notes`` gate."""
    closed = _doc_notes_closed_result(caller)
    if closed is not None:
        return closed
    return await _run(caller, op)


async def _handle_list_doc_notes(caller: Caller, args: dict, *, may_suggest: bool) -> str:
    """``list_doc_notes``: the notes block of a visible doc, paged by
    ``memory_offset`` (``"3"`` coerced; a bool, a non-integral value or a
    negative one refused). ``may_suggest`` is the dispatch layer's run-kind
    verdict, which the service ANDs with the doc's write verdict for
    ``can_suggest``."""
    async def op():
        return await service.list_doc_notes(
            caller,
            args.get("doc_id"),
            _int_arg(args, "memory_offset", 0),
            may_suggest=may_suggest,
        )
    return await _run_notes(caller, op)


async def _handle_suggest_doc_note(caller: Caller, args: dict) -> str:
    """``suggest_doc_note``: register a pending suggestion
    (``note_suggestions.register_suggestion()``); its result dict is
    returned unchanged. Only for the run kinds the dispatch backstop
    admits (top-level and Slack conversations, routine runs excluded)."""
    async def op():
        return await note_suggestions.register_suggestion(
            caller,
            args.get("doc_id"),
            args.get("kind"),
            args.get("content"),
            reasoning=args.get("reasoning"),
            replaces_memory_id=args.get("replaces_memory_id"),
        )
    return await _run_notes(caller, op)
