"""Quest Docs service layer: the ONE read/write path for docs.

The model-facing doc tools (chat/gemini_api/tool_handlers/docs.py) and the
``write_doc`` action request call the functions below; nothing else writes
a doc body or asset. Every operation follows the same pipeline in this one
place::

    feature gate -> doc lookup -> resolve_doc_access (chat/docs/access.py)
      -> file op (chat/docs/files.py, in asyncio.to_thread)
      -> DB bump (db/doc_store.update_after_write)
      -> read sidecar (doc_reads.json) -> realtime events (chat/docs/events.py)

Errors are raised as :class:`DocError` (model/user-facing message; tools
wrap it as ``{"error": str(e)}``) or its two structured subclasses:
:class:`DocApprovalRequired` (the write needs a ``write_doc`` action
request; carries the ``suggested_request`` payload the model forwards
unchanged) and :class:`DocDisabled` (the ``docs`` feature gate is closed
for the user; raised before any DB work).

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
- The read sidecar and the realtime publishers run on the event loop,
  never inside ``asyncio.to_thread`` (``ChatStorage.add_doc_read_ids`` and
  ``bus.publish_to_user`` rely on the single-event-loop guarantee).
- Results never expose a doc's share roster; list/read rows carry only a
  ``shared`` flag.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from chat import storage as _storage
from chat.docs import constants
from chat.docs import files as doc_files
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
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from config import feature_gates
from db import doc_store

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_REQUIRED_MESSAGE",
    "DOC_SCOPES",
    "WRITE_OPERATIONS",
    "Caller",
    "DocApprovalRequired",
    "DocDisabled",
    "DocError",
    "add_doc_image",
    "append_to_doc",
    "apply_write_operation",
    "create_doc",
    "edit_doc",
    "get_visible_doc",
    "list_docs",
    "preview_write_operation",
    "read_doc",
    "require_enabled",
    "search_docs",
]

DOC_SCOPES = ("user", "project", "all")
WRITE_OPERATIONS = ("edit", "append", "add_image")
IMAGE_PLACEMENTS = ("append", "none")

# spec 6.2: the message of every approval_required tool result.
APPROVAL_REQUIRED_MESSAGE = (
    'This doc is shared; propose the change with '
    'create_action_request(request_type="write_doc", ...)'
)

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


@dataclass(frozen=True)
class Caller:
    """Who is asking, from which kind of run.

    ``run_kind`` is one of ``chat.docs.access.RUN_KINDS``. Sandbox scripts
    (``"script"``) have no conversation: ``conversation_id=None``,
    ``project_id=None``, ``is_public=False``.
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


def _access(caller: Caller, doc: dict) -> DocAccess:
    return resolve_doc_access(
        doc,
        user_id=caller.user["id"],
        is_public=caller.is_public,
        project_id=caller.project_id,
        run_kind=caller.run_kind,
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
    size = len(body.encode("utf-8"))
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
_write_locks: dict[str, asyncio.Lock] = {}


def _write_lock(doc_id: str) -> asyncio.Lock:
    lock = _write_locks.get(doc_id)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[doc_id] = lock
    return lock


def _events():
    """chat/docs/events.py, imported lazily: it pulls in chat.realtime,
    whose package import reaches chat.gemini_api, which imports the doc tool
    handlers -- and so this module."""
    from chat.docs import events
    return events


def _publish_write(doc: dict) -> None:
    events = _events()
    events.publish_doc_changed(doc["owner_id"], doc["id"], doc["updated_at"])
    events.publish_doc_list_changed(doc["owner_id"])


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
# Visibility + read side
# ---------------------------------------------------------------------------


async def _get_visible_doc(caller: Caller, doc_id) -> tuple[dict, DocAccess]:
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise DocError("doc_id must be a non-empty string.")
    doc = await doc_store.get_doc(doc_id, with_shares=True)
    if doc is None:
        raise DocError(doc_not_found_message(doc_id))
    access = _access(caller, doc)
    if not access.visible:
        # Same text and the same work (one lookup, no file IO) as a
        # nonexistent id -- invariant 2.
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
    """
    include_user = scope in ("user", "all")
    include_project = scope in ("project", "all") and bool(caller.project_id)
    if not include_user and not include_project:
        return []

    out: list[tuple[dict, DocAccess]] = []
    before = None
    while True:
        rows = await doc_store.list_accessible_docs(
            caller.user["id"],
            project_id=caller.project_id,
            include_user_docs=include_user,
            include_project_docs=include_project,
            # A public conversation can only ever see public docs; filtering
            # in SQL keeps hidden private rows from eating the LIMIT.
            mode="public" if caller.is_public else None,
            limit=limit,
            before=before,
        )
        for row in rows:
            access = _access(caller, row)
            if not access.visible:
                continue
            out.append((row, access))
            if limit is not None and len(out) >= limit:
                return out
        if limit is None or len(rows) < limit:
            return out
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])


def _list_row(doc: dict, access: DocAccess) -> dict:
    return {
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


async def list_docs(
    caller: Caller, scope: str = "all", limit: int = LIST_DEFAULT_LIMIT,
) -> list[dict]:
    """Docs this caller can see, newest-updated first.

    ``scope``: ``"user"`` (user docs), ``"project"`` (this conversation's
    project docs; empty outside a project) or ``"all"`` (both). ``limit``
    is clamped to 1..200. Hidden docs never appear.
    """
    require_enabled(caller)
    scope = _check_scope(scope)
    limit = _check_limit(limit, LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
    return [_list_row(doc, access) for doc, access in await _visible_docs(caller, scope, limit)]


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
        ``{"results": [{id, title, mode, scope, matches: [{line, snippet}]}],
        "truncated": bool}``, at most ``limit`` (1..50) results.
    """
    require_enabled(caller)
    if not isinstance(query, str) or not query.strip():
        raise DocError("query must be a non-empty string.")
    scope = _check_scope(scope)
    limit = _check_limit(limit, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT)

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
            results.append({
                "id": doc["id"],
                "title": doc["title"],
                "mode": doc["mode"],
                "scope": _scope(doc),
                "matches": matches,
            })
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

    Returns:
        ``{id, title, mode, scope, total_lines, content, writable,
        write_note, start_line, end_line, truncated, note?}`` (start/end are
        the returned range; 0/0 for an empty doc).
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
    _record_read(caller, doc["id"])
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

    ``target="user"``: a user doc in ``creation_mode(caller.is_public)``.
    ``target="project"``: a doc of the conversation's project (which the
    user must own), in the project's mode -- which equals the
    conversation's, since a public conversation is one in a public project.
    Only top-level and Slack conversations create docs.

    Order: lay out the directory under a fresh id, then insert the row (a
    crash in between leaves an invisible orphan directory, never a row
    without a body); the directory is removed again if the insert fails.
    Records the new doc as read and publishes doc_list_changed + doc_changed.

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

    user_id = caller.user["id"]
    conversation_mode = creation_mode(caller.is_public)
    project_id = None
    mode = conversation_mode
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
        size = await _files(doc_files.init_doc, doc_id, content)
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


def _normalize_write_params(operation: str, params: dict) -> dict:
    """Validate and complete one operation's params (defaults filled in).

    The result has exactly the keys of ``suggested_request.params`` minus
    ``operation``/``doc_id``; unknown keys in ``params`` are ignored (the
    write_doc handler validates the request shape itself).
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
        return {
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": _bool_param(params, "replace_all", False),
        }
    if operation == "append":
        content = _normalize_text(_str_param(params, "content"))
        if not content.strip():
            raise DocError("content must not be empty.")
        return {
            "content": content,
            "ensure_blank_line": _bool_param(params, "ensure_blank_line", True),
        }
    workspace_path = _str_param(params, "workspace_path")
    if not workspace_path.strip():
        raise DocError("workspace_path must not be empty.")
    placement = _str_param(params, "placement", default="append") or "append"
    if placement not in IMAGE_PLACEMENTS:
        raise DocError('placement must be "append" or "none".')
    return {
        "workspace_path": workspace_path,
        "alt": _str_param(params, "alt", default=""),
        "placement": placement,
    }


def _suggested_request(operation: str, doc_id: str, params: dict) -> dict:
    return {
        "request_type": "write_doc",
        "params": {"operation": operation, "doc_id": doc_id, **params},
    }


async def _resolve_write(
    caller: Caller, doc_id: str, operation: str, params: dict,
) -> tuple[dict, DocAccess, dict]:
    """Gate, normalize, look up and check everything that is not the verdict.

    Order: gate -> params -> visibility (hidden == missing) -> denied ->
    edit's read-before-edit -> add_image's conversation requirement. The
    approval verdict is left to the caller.
    """
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
    text = text.replace("[", "\\[").replace("]", "\\]")
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


async def _load_workspace_image(caller: Caller, workspace_path: str) -> tuple[bytes, str]:
    """Bytes + name hint of a workspace image, validated by magic bytes."""
    from chat.action_request_types._io_attachments import resolve_workspace_file

    try:
        # Path guards only (absolute / '..' / containment / exists / regular);
        # the doc-specific size cap is checked on the descriptor below, so
        # the error names the 5 MB image limit, not the 50 MB upload cap.
        path = await resolve_workspace_file(
            caller.conversation_id,
            caller.project_id,
            workspace_path,
            max_size_bytes=sys.maxsize,
        )
    except RuntimeError as exc:
        raise DocError(str(exc)) from None
    data = await asyncio.to_thread(_read_image_file, path, workspace_path)
    _check_image(data)
    return data, PurePosixPath(workspace_path.replace("\\", "/")).name


# ---------------------------------------------------------------------------
# Writes: appliers
#
# Each applier holds the per-doc asyncio lock around the WHOLE write (file
# op in a thread -> doc_store.update_after_write -> events), so DB counters
# and updated_at land in write order. Body changes go through
# files.modify_body, which reads, transforms and writes doc.md in one
# critical section of the per-doc threading lock (a separate read_body +
# write_body pair would let concurrent writers clobber each other).
# ---------------------------------------------------------------------------


async def _write_edit(caller: Caller, doc: dict, clean: dict, write_source: str) -> dict:
    doc_id = doc["id"]
    replaced: list[int] = []

    def transform(current: str) -> str:
        new_body, count = _apply_edit_text(current, clean)
        replaced.append(count)
        return new_body

    async with _write_lock(doc_id):
        new_body, size = await _files(doc_files.modify_body, doc_id, transform)
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

    async with _write_lock(doc_id):
        new_body, size = await _files(doc_files.modify_body, doc_id, transform)
        updated = await _finish_write(doc_id, content_size=size, write_source=write_source)
    return {
        "appended_lines": _total_lines(clean["content"]),
        "total_lines": _total_lines(new_body),
        "updated_at": updated["updated_at"],
    }


async def _write_add_image(caller: Caller, doc: dict, clean: dict, write_source: str) -> dict:
    doc_id = doc["id"]
    data, name_hint = await _load_workspace_image(caller, clean["workspace_path"])
    append = clean["placement"] == "append"
    new_body: Optional[str] = None
    async with _write_lock(doc_id):
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
                new_body, size = await _files(doc_files.modify_body, doc_id, transform)
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
    while hidden/missing (``doc_not_found_message``) and ``denied``
    (``deny_reason`` -- e.g. the doc was switched to public meanwhile)
    still refuse, and edit still requires the read sidecar.

    Raises:
        DocDisabled, DocApprovalRequired (unless bypass_approval), DocError.
    """
    doc, access, clean = await _resolve_write(caller, doc_id, operation, params)
    if access.write == "approval" and not bypass_approval:
        # Dry-run first so a stale old_string, a bad image or an over-cap
        # body fails now instead of after the model forwarded the request.
        await _compute_preview(caller, doc, access, operation, clean)
        raise DocApprovalRequired(
            APPROVAL_REQUIRED_MESSAGE,
            _suggested_request(operation, doc["id"], clean),
        )
    source = _write_source_or_default(write_source, caller)
    return await _WRITERS[operation](caller, doc, clean, source)


async def preview_write_operation(
    caller: Caller, doc_id: str, operation: str, params: dict,
) -> dict:
    """Compute a write without performing it (the write_doc pre-card).

    Resolves access exactly like :func:`apply_write_operation` (same
    errors), except that an ``approval`` (or ``free``) verdict never
    raises -- the caller inspects ``access``. Reads the live body and
    applies the operation in memory.

    Returns:
        ``{"doc", "access", "current_body", "new_body", "replacements"}``;
        add_image adds ``"asset_name_preview"`` (sanitized stem + sniffed
        extension; the stored name may get a ``-2`` suffix on collision),
        ``"image_bytes_size"`` and ``"markdown"``. For add_image with
        placement ``"none"`` the body is unchanged.
    """
    doc, access, clean = await _resolve_write(caller, doc_id, operation, params)
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
