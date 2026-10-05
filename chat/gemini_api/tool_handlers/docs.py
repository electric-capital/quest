"""Quest Docs tool handlers: thin wrappers over chat/docs/service.py.

Each handler takes the dispatch-built :class:`chat.docs.service.Caller`
and the raw tool arguments, coerces loosely-typed arguments (``"true"``,
``"10"``), calls the service and returns a JSON string. Error shapes:

- ``{"error": "docs_disabled", "message": ...}`` -- the ``docs`` feature
  gate is closed for the user (checked before anything else, no DB work);
- ``{"error": "approval_required", "message": ...,
  "suggested_request": {"request_type": "write_doc", "params": {...}}}`` --
  a shared private doc: the model forwards ``suggested_request`` as a
  ``write_doc`` action request;
- ``{"error": "<text>"}`` -- every other refusal (missing/hidden doc with
  the one not-found text, denied write, unread doc, bad arguments, caps).

Write tools record ``last_write_source = "conversation:<id>"``.
"""

import json
from typing import Any, Awaitable, Callable

from chat.docs import service
from chat.docs.service import Caller, DocApprovalRequired, DocDisabled, DocError


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
