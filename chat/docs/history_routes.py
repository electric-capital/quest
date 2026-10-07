"""Quest Docs history routes (Phase 3): revision list/read/diff, restore, copy.

The HTTP face of chat/docs/history.py (the Version shape and the per-viewer
``source_label`` rules are documented there). Same conventions as
chat/docs/routes.py: under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked``, 403 ``docs_disabled`` first
while the ``docs`` gate is closed, visibility through
``routes._get_doc_for_ui`` (404 ``doc_not_found`` with one body for a
missing doc and a hidden one), rows built by ``routes._ui_row``.

Every route is for EDITORS only (``access.can_edit``: the owner or a
write-share recipient): right after visibility, a read-only viewer (read
share, everyone-read share) gets 403 ``forbidden`` ("History is available
to people who can edit this doc."), before any other check -- earlier
versions can hold text the owner removed before sharing. Order: gate ->
404 -> 403 -> the rest. The service re-checks against a fresh row after
reading (see chat/docs/history.py).

| Method | Path |
|---|---|
| GET | ``/docs/{id}/revisions`` |
| GET | ``/docs/{id}/revisions/{rev_id}`` (``?diff=current``) |
| POST | ``/docs/{id}/revisions/{rev_id}/restore`` |
| POST | ``/docs/{id}/revisions/{rev_id}/copy`` |

Errors are ``{"detail": {"error", "message"}}``: 404
``revision_not_found`` (malformed or missing id, one body), 403
``forbidden`` (not an editor), 400 ``invalid_request`` /
``invalid_title`` / ``content_too_large``, 409 ``duplicate_title`` (copy),
400 ``invalid_doc_files`` / 500 ``doc_storage_error`` for a broken doc
directory. A stale restore token is the FLAT 409 ``stale_update`` carrying
``current`` (``routes._stale_conflict``).
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import files as doc_files
from chat.docs import history
from chat.docs import service as doc_service
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from chat.docs.routes import (
    _doc_not_found,
    _get_doc_for_ui,
    _http_error,
    _require_docs_enabled,
    _stale_conflict,
    _ui_row,
)
from db import doc_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

DIFF_TARGETS = ("current",)

# DocRequestError codes that are not a plain 400.
_REQUEST_ERROR_STATUS = {
    "revision_not_found": 404,
    "forbidden": 403,
    "duplicate_title": 409,
}


async def _editable_doc(user: dict, doc_id: str):
    """``(doc, access)`` for an editor: 404 ``doc_not_found`` (missing or
    hidden, one body), then 403 ``forbidden`` for a read-only viewer."""
    doc, access = await _get_doc_for_ui(user, doc_id)
    if access.write != "free":
        raise _http_error(403, "forbidden", history.HISTORY_FORBIDDEN_MESSAGE)
    return doc, access


class RestoreRevisionRequest(BaseModel):
    # Any: a missing or non-string token is a 400 invalid_request from the
    # service, not a 422.
    expected_updated_at: Any = None


class CopyRevisionRequest(BaseModel):
    # Any: a non-string title is a 400 invalid_title from the service.
    title: Any = None


async def _call(doc_id: str, fn, *args, **kwargs):
    """Await a chat/docs/history.py call, mapping its errors to HTTP.

    ``doc_store.StaleDocError`` is not mapped (restore returns the flat 409).
    """
    try:
        return await fn(*args, **kwargs)
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocRequestError as exc:
        raise _http_error(
            _REQUEST_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc),
        )
    except doc_files.DocFileError as exc:
        raise _http_error(400, "invalid_doc_files", str(exc))
    except OSError as exc:
        logger.warning("[docs] history file operation failed", exc_info=True)
        raise _http_error(
            500, "doc_storage_error",
            f"Doc storage error: {exc.strerror or exc.__class__.__name__}.",
        )
    except doc_service.DocError as exc:
        if str(exc) == doc_not_found_message(doc_id):
            # Deleted (or access lost) while the request ran.
            raise _doc_not_found(doc_id)
        raise _http_error(500, "doc_storage_error", str(exc))


@router.get("/docs/{doc_id}/revisions")
async def list_doc_revisions(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The doc's versions for an editor: ``{"current": Version,
    "revisions": [Version, ...]}``, revisions newest first (at most
    ``DOC_REVISION_MAX_COUNT``, so unpaged). Labels are per viewer; only
    the owner gets raw ``source`` and ``conversation``. 403 ``forbidden``
    for a read-only viewer."""
    _require_docs_enabled(user)
    doc, access = await _editable_doc(user, doc_id)
    return await _call(doc["id"], history.list_versions, user, doc, access)


@router.get("/docs/{doc_id}/revisions/{rev_id}")
async def get_doc_revision(
    doc_id: str,
    rev_id: str,
    diff: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One revision for an editor: its Version plus ``content``.

    ``?diff=current`` adds ``diff`` = ``build_bounded_content_diff(old=<this
    revision>, new=<current body>)`` (the UI's "Changes since this
    version"); any other ``diff`` value is 400 ``invalid_request``. 403
    ``forbidden`` for a read-only viewer; 404 ``revision_not_found`` for a
    malformed or missing id.
    """
    _require_docs_enabled(user)
    doc, access = await _editable_doc(user, doc_id)
    if diff is not None and diff not in DIFF_TARGETS:
        raise _http_error(
            400, "invalid_request",
            f"diff must be one of: {', '.join(DIFF_TARGETS)}.",
        )
    return await _call(
        doc["id"], history.read_version, user, doc, access, rev_id,
        diff_current=diff == "current",
    )


@router.post("/docs/{doc_id}/revisions/{rev_id}/restore")
async def restore_doc_revision(
    doc_id: str,
    rev_id: str,
    body: Optional[RestoreRevisionRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Make a revision the current body again (owner or write share).

    Body ``{"expected_updated_at": str}`` (required, 400
    ``invalid_request``). 403 ``forbidden`` for a read-only viewer, 404
    ``revision_not_found``, 400 ``invalid_doc_files`` when ``doc.md`` is
    missing or not a regular file (as the list and read routes), flat 409
    ``stale_update`` (with ``current``)
    when the token no longer matches -- checked under the per-doc write
    lock, so a write landing after the client read the token always wins.
    The replaced body is snapshotted; the restored one is recorded as
    written by ``ui:<user_id>``. Restoring a body identical to the current
    one writes nothing.

    Returns:
        The row plus ``content`` (the body as stored) and ``changed``.
    """
    _require_docs_enabled(user)
    doc, access = await _editable_doc(user, doc_id)
    token = body.expected_updated_at if body is not None else None
    try:
        updated, content, changed = await _call(
            doc["id"], history.restore_revision, user, doc, access, rev_id,
            expected_updated_at=token,
        )
    except doc_store.StaleDocError as exc:
        return await _stale_conflict(user, exc.current)
    row = await _ui_row(user, updated)
    row["content"] = content
    row["changed"] = changed
    return row


@router.post("/docs/{doc_id}/revisions/{rev_id}/copy", status_code=201)
async def copy_doc_revision(
    doc_id: str,
    rev_id: str,
    body: Optional[CopyRevisionRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Copy a revision into a NEW private user doc owned by the caller
    (editors only -- 403 ``forbidden`` for a read-only viewer; also from a
    project or public-project doc).

    Body ``{"title"?: str}``: default ``"<title> (copy)"``, ``"(copy 2)"``,
    ... on collision; an explicit title that is taken is 409
    ``duplicate_title``, an invalid one 400 ``invalid_title``. The assets
    the copied body references are copied under the same names. Returns the
    new doc's row (201) and sends ``doc_list_changed`` to the caller.
    """
    _require_docs_enabled(user)
    doc, access = await _editable_doc(user, doc_id)
    title = body.title if body is not None else None
    new_doc = await _call(
        doc["id"], history.copy_revision, user, doc, access, rev_id, title=title,
    )
    return await _ui_row(user, new_doc)
