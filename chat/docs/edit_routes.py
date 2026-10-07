"""Quest Docs human-editing routes (Phase 3): body replace, image upload, asset delete.

- ``PUT /docs/{id}/content`` -- replace the whole body from the editor.
- ``POST /docs/{id}/assets`` -- upload an image into ``assets/`` (multipart).
- ``DELETE /docs/{id}/assets/{name}`` -- delete an image (owner only).

Same conventions as chat/docs/routes.py: under ``/app/api``, authenticated
by ``get_current_user_cookie_or_apikey_checked``, 403 ``docs_disabled``
first while the ``docs`` gate is closed for the user, then visibility via
``routes._get_doc_for_ui`` (404 ``doc_not_found``, one body for a missing
and a hidden doc). Errors are ``{"detail": {"error", "message"}}`` except the
flat 409 ``stale_update`` of the content PUT. The writes themselves live in
chat/docs/ui_writes.py (per-doc write lock, revision snapshot for body
changes, ``ui:<user_id>`` write source, realtime events).

Writing the body or uploading an image needs the UI write verdict
(``access.write == "free"``: the owner or a write-share recipient, the
row's ``access.can_edit``); a read-share recipient gets 403 ``forbidden``.
A write-share recipient's HUMAN edit of a shared private doc needs no
approval card: the card guards MODEL-initiated writes, and this is a person
the owner granted write access. Deleting an image is owner-only (the row's
``access.can_delete_assets``).

Request bodies are read by the handlers themselves, after the gate,
visibility and write checks, and only up to a cap (the declared
``Content-Length`` is checked first, then the bytes actually streamed), so
neither a stranger nor an oversized request makes the server buffer an
arbitrary body. Bad bodies are 400 ``invalid_request`` in the error shape
above, never FastAPI's 422.

Doc content is never logged.
"""

import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import constants
from chat.docs import routes
from chat.docs import service as doc_service
from chat.docs import ui_writes
from chat.docs.access import DENY_UI_READ_ONLY
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from db import doc_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

# DocRequestError codes of these routes that are not a plain 400.
_REQUEST_ERROR_STATUS = {
    "forbidden": 403,
    "asset_not_found": 404,
    "asset_in_use": 409,
}

# Room for multipart framing, part headers and the ``alt`` field on top of
# the image itself.
_UPLOAD_BODY_OVERHEAD = 64 * 1024
# The upload form carries ``file`` and an optional ``alt``; a little slack
# for a client that adds a field of its own. Non-file fields (``alt``) are
# capped at 8 KiB each by the parser.
_UPLOAD_MAX_FIELDS = 4
_UPLOAD_MAX_FIELD_SIZE = 8 * 1024
# JSON escaping can grow the body up to 6x (``\\u0001`` for one byte).
_CONTENT_BODY_FACTOR = 6
_CONTENT_BODY_OVERHEAD = 64 * 1024


class _BodyTooLarge(MultiPartException):
    """The streamed body passed its cap.

    A ``MultiPartException`` so that, raised from inside the stream that
    ``MultiPartParser.parse`` consumes, the parser closes the files it has
    spooled so far before re-raising.
    """


def _invalid_request(message: str) -> HTTPException:
    return routes._http_error(400, "invalid_request", message)


def _image_too_large() -> HTTPException:
    cap = constants.DOC_MAX_IMAGE_SIZE
    return routes._http_error(
        400, "image_too_large", f"The image is over the {cap}-byte per-image limit.",
    )


def _content_too_large() -> HTTPException:
    return routes._http_error(
        400, "content_too_large",
        f"The request is too large for a doc of at most "
        f"{constants.DOC_MAX_CONTENT_SIZE} bytes.",
    )


def _media_type(request: Request) -> Optional[str]:
    """The request's media type (lowercase, no parameters), or None."""
    value = request.headers.get("content-type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower()


def _check_declared_length(request: Request, limit: int, too_large) -> None:
    """Refuse a declared ``Content-Length`` over ``limit`` before reading."""
    declared = request.headers.get("content-length")
    if declared is None:
        return
    declared = declared.strip()
    if not (declared.isascii() and declared.isdigit()):
        raise _invalid_request("Invalid Content-Length.")
    if int(declared) > limit:
        raise too_large()


async def _bounded_stream(request: Request, limit: int) -> AsyncIterator[bytes]:
    """``request.stream()``, raising :class:`_BodyTooLarge` as soon as more
    than ``limit`` bytes have arrived (a chunked body has no length to
    check up front, and a declared one may lie)."""
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _BodyTooLarge("The request body is too large.")
        yield chunk


async def _read_json_object(request: Request) -> dict:
    """The request body as a JSON object; 400 ``invalid_request`` for any
    other media type, invalid JSON or a non-object (FastAPI would 422), 400
    ``content_too_large`` past the body cap. An empty body is ``{}`` (the
    service then reports the missing fields)."""
    media_type = _media_type(request)
    if media_type is not None and not (
        media_type == "application/json" or media_type.endswith("+json")
    ):
        raise _invalid_request("The body must be JSON (application/json).")
    limit = constants.DOC_MAX_CONTENT_SIZE * _CONTENT_BODY_FACTOR + _CONTENT_BODY_OVERHEAD
    _check_declared_length(request, limit, _content_too_large)
    chunks = []
    try:
        async for chunk in _bounded_stream(request, limit):
            chunks.append(chunk)
    except _BodyTooLarge:
        raise _content_too_large() from None
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:  # JSONDecodeError, UnicodeDecodeError
        raise _invalid_request("The body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise _invalid_request("The body must be a JSON object.")
    return data


def _service_error(exc: doc_service.DocError, doc_id: str, doc: dict) -> HTTPException:
    """The HTTP error for a service error raised AFTER the route's own
    visibility check: DocRequestError -> its code (403 / 404 / 409 / 400);
    the not-found text (the doc was deleted, or hidden by a share revoked,
    while the request queued for the write lock) -> 404 ``doc_not_found``;
    anything else is a storage failure -> 500 ``doc_storage_error`` (the
    messages name no paths)."""
    if isinstance(exc, doc_service.DocRequestError):
        return routes._http_error(
            _REQUEST_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc),
        )
    if isinstance(exc, doc_service.DocDisabled):
        return routes._http_error(403, "docs_disabled", docs_disabled_message())
    if str(exc) in (doc_not_found_message(doc_id), doc_not_found_message(doc["id"])):
        return routes._doc_not_found(doc_id)
    return routes._http_error(500, "doc_storage_error", str(exc))


def _require_can_edit(access) -> None:
    """403 ``forbidden`` unless the UI verdict is ``free`` (owner or write share)."""
    if access.write != "free":
        raise routes._http_error(
            403, "forbidden", access.deny_reason or DENY_UI_READ_ONLY,
        )


@router.put("/docs/{doc_id}/content")
async def replace_doc_content(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Replace the doc's whole body (the editor's Save).

    JSON body ``{"content": str, "expected_updated_at": str}``, both
    required (400 ``invalid_request``; also for text that is not UTF-8
    encodable, e.g. a lone surrogate, and for a body that is not a JSON
    object or not ``application/json``). CRLF is normalized to LF; 400
    ``content_too_large`` over ``DOC_MAX_CONTENT_SIZE`` after that. Order:
    403 ``docs_disabled``, 404 ``doc_not_found``, 403 ``forbidden`` (no
    write access), 400s, then the flat 409 ``stale_update`` (with
    ``current``) when ``expected_updated_at`` is not the row's token --
    re-checked under the per-doc write lock. 400 ``invalid_doc_files`` when
    ``doc.md`` is missing, a symlink or a special file (as restore).

    A body identical to the stored one writes nothing (no revision, no
    ``updated_at`` bump, no event). Otherwise the replaced body is
    snapshotted into ``revisions/``, the new one records ``ui:<user_id>``
    as its writer, and ``doc_changed`` / ``doc_list_changed`` go out.

    Returns:
        The row (``routes._ui_row``) plus ``content`` (the body as stored)
        and ``changed`` (whether anything was written).
    """
    routes._require_docs_enabled(user)
    doc, access = await routes._get_doc_for_ui(user, doc_id)
    _require_can_edit(access)
    payload = await _read_json_object(request)
    try:
        updated, content, changed = await ui_writes.replace_body_from_ui(
            user,
            doc["id"],
            payload.get("content"),
            expected_updated_at=payload.get("expected_updated_at"),
        )
    except doc_store.StaleDocError as exc:
        return await routes._stale_conflict(user, exc.current)
    except doc_service.DocError as exc:
        raise _service_error(exc, doc_id, doc)
    row = await routes._ui_row(user, updated)
    row["content"] = content
    row["changed"] = changed
    return row


def _close_parser_files(parser: MultiPartParser) -> None:
    """Close every temp file the parser spooled, on success and failure.

    ``MultiPartParser.parse`` closes them itself only on a
    ``MultiPartException``; the multipart library's own parse errors
    (``ValueError``) and our early returns would leak them otherwise.
    ``_files_to_close_on_error`` holds every file it opened, including a
    part cut off mid-way; ``items`` the finished ones. Closing twice is a
    no-op.
    """
    spooled = list(getattr(parser, "_files_to_close_on_error", ()))
    spooled += [
        value.file for _key, value in getattr(parser, "items", ())
        if isinstance(value, UploadFile)
    ]
    for file in spooled:
        try:
            file.close()
        except Exception:
            logger.debug("[docs] closing an upload temp file failed", exc_info=True)


async def _read_upload(request: Request) -> tuple[Optional[str], bytes, Optional[str]]:
    """``(filename, data, alt)`` from a ``multipart/form-data`` body, with
    at most ``DOC_MAX_IMAGE_SIZE + 1`` bytes of the file returned.

    Bounded end to end: any other media type is 400 ``invalid_request``
    before a byte is read (an urlencoded body would be buffered whole); a
    declared ``Content-Length`` over ``DOC_MAX_IMAGE_SIZE`` + 64 KiB is 400
    ``image_too_large`` before reading; the parser consumes a stream that
    aborts with ``image_too_large`` once that many bytes have arrived (a
    chunked or lying body). Starlette's parser keeps a file part in memory
    up to 1 MB and spools the rest to a temp file, so at most the capped
    body is ever held; non-file fields are capped at 8 KiB, at most one
    file part is accepted. Every spooled file is closed before returning,
    whatever happens. Malformed multipart (Starlette's
    ``MultiPartException`` or the multipart library's ``ValueError``) is
    400 ``invalid_request``; so is a form without a ``file`` part.
    """
    if _media_type(request) != "multipart/form-data":
        raise _invalid_request("The upload must be multipart/form-data.")
    cap = constants.DOC_MAX_IMAGE_SIZE
    limit = cap + _UPLOAD_BODY_OVERHEAD
    _check_declared_length(request, limit, _image_too_large)
    parser = MultiPartParser(
        request.headers,
        _bounded_stream(request, limit),
        max_files=1,
        max_fields=_UPLOAD_MAX_FIELDS,
        max_part_size=_UPLOAD_MAX_FIELD_SIZE,
    )
    try:
        try:
            form = await parser.parse()
        except _BodyTooLarge:
            raise _image_too_large() from None
        except MultiPartException as exc:
            raise _invalid_request(f"Malformed multipart upload: {exc.message}") from None
        except ValueError:
            raise _invalid_request("Malformed multipart upload.") from None
        upload = form.get("file")
        alt = form.get("alt")
        if not isinstance(upload, UploadFile):
            raise _invalid_request("file is required (multipart field 'file').")
        if upload.size is not None and upload.size > cap:
            raise _image_too_large()
        data = await upload.read(cap + 1)
        # max_files=1 and ``file`` being the file, ``alt`` is always text.
        return upload.filename, data, alt if isinstance(alt, str) else None
    finally:
        _close_parser_files(parser)


@router.post("/docs/{doc_id}/assets", status_code=201)
async def upload_doc_asset(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Upload an image into the doc's ``assets/`` (the editor's image button).

    ``multipart/form-data`` with ``file`` (required) and ``alt`` (optional;
    defaults to the uploaded file's stem). Needs write access (owner or
    write share, else 403 ``forbidden``). Validated like ``add_doc_image``:
    400 ``invalid_image`` unless the magic bytes say png/jpg/gif/webp (SVG
    and renamed text refused), 400 ``image_too_large`` over
    ``DOC_MAX_IMAGE_SIZE`` (the body is never read past that, see
    :func:`_read_upload`), 400 ``asset_limit`` past ``DOC_MAX_ASSETS`` /
    ``DOC_MAX_ASSETS_TOTAL_BYTES``, 400 ``invalid_request`` for another
    media type, a malformed form or a missing ``file``. The stored name is
    the sanitized file name plus the sniffed extension, ``-2``... on
    collision.

    The body is not changed (the editor inserts ``markdown`` itself) and no
    revision is taken; the row's ``asset_count`` and ``updated_at`` are
    bumped (``last_write_source`` keeps naming the body's writer) and the
    realtime events sent.

    Returns (201):
        ``{"asset": {"name", "size", "mime"}, "markdown":
        "![alt](assets/<name>)", "asset_count", "updated_at",
        "previous_updated_at"}`` -- the editor adopts ``updated_at`` as its
        token iff ``previous_updated_at`` equals the token it holds.
    """
    routes._require_docs_enabled(user)
    doc, access = await routes._get_doc_for_ui(user, doc_id)
    _require_can_edit(access)
    filename, data, alt = await _read_upload(request)
    try:
        return await ui_writes.add_asset_from_ui(user, doc["id"], filename, data, alt)
    except doc_service.DocError as exc:
        raise _service_error(exc, doc_id, doc)


@router.delete("/docs/{doc_id}/assets/{name:path}")
async def delete_doc_asset(
    doc_id: str,
    name: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete one of the doc's images (owner only, else 403 ``forbidden``).

    ``name`` is matched as a path so that ``a/b.png`` or an encoded
    ``..%2Fdoc.md`` reaches this handler and gets the same 404
    ``asset_not_found`` as any other bad, hidden, missing, symlinked or
    non-regular name; nothing outside ``assets/`` is touched and a symlink
    is never followed. 409 ``asset_in_use`` while the CURRENT body
    references ``assets/<name>``, also percent-, entity- or
    backslash-escaped (``ui_writes.asset_referenced``); older revisions may
    still reference it (restoring one then shows a broken image). No
    revision is taken; the row's ``asset_count`` and ``updated_at`` are
    bumped (``last_write_source`` keeps naming the body's writer) and the
    realtime events sent.

    Returns:
        ``{"deleted": true, "asset_count", "updated_at",
        "previous_updated_at"}``.
    """
    routes._require_docs_enabled(user)
    doc, _access = await routes._get_doc_for_ui(user, doc_id)
    routes._require_owner(user, doc, "delete its images")
    try:
        return await ui_writes.delete_asset_from_ui(user, doc["id"], name)
    except doc_service.DocError as exc:
        raise _service_error(exc, doc_id, doc)
