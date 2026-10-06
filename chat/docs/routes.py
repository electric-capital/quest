"""HTTP routes for Quest Docs (spec section 7, the v1 rows).

Everything is under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` and refused with 403
``docs_disabled`` while the ``docs`` feature gate is closed for the user
(checked first in every handler, before any DB work).

Visibility goes through the one access rule
(``chat.docs.access.resolve_doc_access`` with ``run_kind="ui"``, via
``chat.docs.service.get_visible_doc``): a doc the user cannot see is a
404 ``doc_not_found`` with exactly the body a nonexistent id gets.
Rename, mode switch and delete additionally require ownership (403
``forbidden`` for a visible non-owned doc) -- a route check, not part of the
matrix.

Rows returned to the UI are the store's doc dict plus ``scope``,
``shared`` and ``access`` (what the UI may offer); the share roster
(``shares``) is included for the owner only. All sync file IO runs in
``asyncio.to_thread``. Doc content is never logged.

User docs are always private: POST with ``mode: "public"`` and no project
is 400 ``user_doc_mode_private``; the only public docs are the docs of a
public project, which take the project's mode. No doc's mode can be
switched in v1: ``PUT /docs/{id}/mode`` stays registered but refuses every
doc, and ``access.can_switch_mode`` is always false.

Errors are ``HTTPException(detail={"error": <code>, "message": ...})``
except the optimistic-concurrency conflict, which is a flat 409
``{"error": "stale_update", "message", "current": row}`` like routines.
"""

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import events as doc_events
from chat.docs import files as doc_files
from chat.docs import service as doc_service
from chat.docs.access import DocAccess, resolve_doc_access
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from chat.file_routes import _INLINE_IMAGE_MIMES
from chat.project_routes import _get_visible_project, _public_projects_enabled_for
from config.feature_gates import docs_enabled_for
from db import doc_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

DOWNLOAD_FORMATS = ("md", "zip")

# DocRequestError codes that are not a plain 400.
_REQUEST_ERROR_STATUS = {
    "project_not_found": 404,
    "duplicate_title": 409,
}

# Characters never allowed in a download file name (path separators,
# Windows-reserved punctuation, quotes); control/format characters are
# dropped separately by Unicode category.
_FILENAME_UNSAFE_RE = re.compile(r'[/\\:*?"<>|]+')
_FILENAME_MAX_LEN = 120

# Every name files.add_asset can produce (sanitize_asset_name stem + a
# sniffed extension); the detail payload lists nothing else.
_LISTED_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class CreateDocRequest(BaseModel):
    title: str
    description: Optional[str] = None
    mode: Optional[str] = None
    project_id: Optional[str] = None


class UpdateDocRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    expected_updated_at: Optional[str] = None


class SetDocModeRequest(BaseModel):
    mode: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"error": code, "message": message},
    )


def _require_docs_enabled(user: dict) -> None:
    """403 ``docs_disabled`` unless the docs gate is open for this user."""
    if not docs_enabled_for(user.get("email") or ""):
        raise _http_error(403, "docs_disabled", docs_disabled_message())


def _ui_caller(user: dict) -> doc_service.Caller:
    return doc_service.Caller(
        user=user, conversation_id=None, project_id=None,
        is_public=False, run_kind="ui",
    )


def _ui_access(user: dict, doc: dict) -> DocAccess:
    return resolve_doc_access(
        doc, user_id=user["id"], is_public=False, project_id=None, run_kind="ui",
    )


def _doc_not_found(doc_id: str) -> HTTPException:
    # The one body for a missing AND a hidden doc (invariant 2).
    return _http_error(404, "doc_not_found", doc_not_found_message(doc_id))


def _doc_row(user: dict, doc: dict, access: DocAccess) -> dict:
    """The UI row: doc dict + scope/shared/access; ``shares`` owner-only.

    ``access.can_switch_mode`` is always false in v1 (user docs are always
    private, project docs keep their project's mode); the key stays in the
    row shape for the frontend type.
    """
    is_owner = doc["owner_id"] == user["id"]
    shares = doc.get("shares") or []
    row = {key: value for key, value in doc.items() if key != "shares"}
    row["scope"] = "project" if doc.get("project_id") else "user"
    row["shared"] = bool(shares)
    if is_owner:
        row["shares"] = list(shares)
    else:
        # The owner's conversation ids are not the recipient's business.
        row["last_write_source"] = None
    row["access"] = {
        "can_rename": is_owner,
        "can_switch_mode": False,
        "can_delete": is_owner,
        "write": access.write,
    }
    return row


def _asset_rows(assets: list[dict]) -> list[dict]:
    """``[{"name", "size", "mime"}]`` for the detail payload.

    ``assets`` comes from ``files.list_assets`` (regular, non-hidden files
    directly in ``assets/``, sorted by name). Left out: names outside
    ``[A-Za-z0-9._-]`` (``add_asset`` never writes one, so anything else
    was planted) and entries the asset route would 404 -- a name ``files``
    refuses or an extension outside ``_INLINE_IMAGE_MIMES``.
    """
    rows = []
    for asset in assets:
        name = asset["name"]
        # fullmatch: "$" alone would also accept a trailing newline.
        if not _LISTED_ASSET_NAME_RE.fullmatch(name):
            continue
        try:
            doc_files._validate_asset_name(name)
        except doc_files.DocFileError:
            continue
        mime = _INLINE_IMAGE_MIMES.get(os.path.splitext(name)[1].lower())
        if mime is None:
            continue
        rows.append({"name": name, "size": asset["size"], "mime": mime})
    return rows


async def _get_doc_for_ui(user: dict, doc_id: str) -> tuple[dict, DocAccess]:
    """``(doc, access)`` for a doc the user may see, else 404 doc_not_found.

    Docs of a public project are hidden (same 404) while the
    ``public_projects`` gate is closed for the user, matching the project
    routes' "hidden public projects 404 on every by-id endpoint" rule. A
    project doc's mode mirrors its project's ``public`` flag, so no project
    lookup is needed.
    """
    try:
        doc, access = await doc_service.get_visible_doc(_ui_caller(user), doc_id)
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError:
        raise _doc_not_found(doc_id)
    if (
        doc.get("project_id") is not None
        and doc.get("mode") == "public"
        and not _public_projects_enabled_for(user)
    ):
        raise _doc_not_found(doc_id)
    return doc, access


def _raise_request_error(exc: "doc_service.DocRequestError", doc_id: str):
    status = {
        "forbidden": 403,
        "duplicate_title": 409,
        "project_not_found": 404,
    }.get(exc.code, 400)
    raise _http_error(status, exc.code, str(exc))


def _require_owner(user: dict, doc: dict, action: str) -> None:
    if doc["owner_id"] != user["id"]:
        raise _http_error(403, "forbidden", f"Only the doc's owner can {action}.")


async def _require_visible_project(user: dict, project_id: str) -> dict:
    """The project, owned by the user and not hidden by the public-projects
    gate (the same check the project routes use), else 404."""
    project = await _get_visible_project(user, project_id)
    if project is None:
        raise _http_error(404, "project_not_found", "Project not found.")
    return project


async def _doc_files_call(fn, *args):
    """Run a sync chat/docs/files.py call in a thread, mapping its errors.

    ``DocFileError`` (missing body, symlink or special file in the doc
    directory) -> 400 ``invalid_doc_files``; any other OSError -> 500
    ``doc_storage_error`` without the path.
    """
    try:
        return await asyncio.to_thread(fn, *args)
    except doc_files.DocFileError as exc:
        raise _http_error(400, "invalid_doc_files", str(exc))
    except OSError as exc:
        logger.warning("[docs] file operation failed", exc_info=True)
        raise _http_error(
            500, "doc_storage_error",
            f"Doc storage error: {exc.strerror or exc.__class__.__name__}.",
        )


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a ``<updated_at_iso>|<doc_id>`` keyset cursor (400 on garbage)."""
    ts_part, sep, id_part = cursor.partition("|")
    if not sep or not id_part:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    return ts, id_part


async def _visible_docs_page(
    user: dict,
    project_id: Optional[str],
    limit: Optional[int],
    before: Optional[tuple[datetime, str]],
) -> tuple[list[tuple[dict, DocAccess]], bool]:
    """Visible docs, newest-updated first, plus ``has_more``.

    Candidates come from ``doc_store.list_accessible_docs`` (owner or a
    share); every row still passes the access rule. With ``limit`` it
    gathers ``limit + 1`` visible rows (paging the store past any hidden
    ones) so ``has_more`` is exact; without it, the whole list.
    """
    want = None if limit is None else limit + 1
    out: list[tuple[dict, DocAccess]] = []
    while True:
        rows = await doc_store.list_accessible_docs(
            user["id"],
            project_id=project_id,
            include_user_docs=project_id is None,
            include_project_docs=project_id is not None,
            limit=want,
            before=before,
        )
        for doc in rows:
            access = _ui_access(user, doc)
            if not access.visible:
                continue
            out.append((doc, access))
            if want is not None and len(out) >= want:
                break
        if want is None or len(out) >= want or len(rows) < want:
            break
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])
    has_more = limit is not None and len(out) > limit
    if has_more:
        out = out[:limit]
    return out, has_more


def _download_filename(title: str, ext: str) -> str:
    """A file name derived from the doc title, safe for any file system."""
    name = unicodedata.normalize("NFC", title or "")
    name = "".join(
        ch for ch in name if not unicodedata.category(ch).startswith("C")
    )
    name = _FILENAME_UNSAFE_RE.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:_FILENAME_MAX_LEN].rstrip(" .-") or "doc"
    return f"{name}.{ext}"


def _attachment_disposition(filename: str) -> str:
    """``attachment`` Content-Disposition: ASCII fallback + RFC 5987 UTF-8."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@router.get("/docs")
async def list_user_docs(
    project_id: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List docs, newest-updated first.

    Without ``project_id``: the user's user docs (no project) they own or
    hold a share on. With ``project_id``: that project's docs (the project
    must be the user's and visible, else 404 ``project_not_found``).

    With no ``limit`` the whole list is returned; ``limit`` (1..200) pages
    with an opaque keyset ``next_cursor`` the client echoes back as
    ``cursor`` (400 ``invalid_cursor`` on garbage). ``has_more`` and
    ``next_cursor`` are always present.

    Returns:
        ``{"docs": [row, ...], "has_more": bool, "next_cursor": str | None}``.
    """
    _require_docs_enabled(user)
    project_id = project_id or None
    if project_id is not None:
        await _require_visible_project(user, project_id)
    before = _parse_cursor(cursor) if cursor else None

    page, has_more = await _visible_docs_page(user, project_id, limit, before)
    rows = [_doc_row(user, doc, access) for doc, access in page]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last['updated_at']}|{last['id']}"
    return {"docs": rows, "has_more": has_more, "next_cursor": next_cursor}


@router.post("/docs", status_code=201)
async def create_ui_doc(
    body: CreateDocRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create an empty doc owned by the user.

    A user doc is always private: ``mode`` omitted or ``"private"`` gives
    a private doc, ``"public"`` is 400 ``user_doc_mode_private`` (a public
    doc is created inside a public project). A project doc takes its
    project's mode, and a supplied ``mode`` that disagrees is 400
    ``project_doc_mode_inherited``; a public project hidden by the
    ``public_projects`` gate is 404 ``project_not_found``. Errors also:
    409 ``duplicate_title``, 400 ``invalid_title`` / ``invalid_description``
    / ``invalid_mode``. Returns the row (201).
    """
    _require_docs_enabled(user)
    project_id = body.project_id or None
    if project_id is not None:
        await _require_visible_project(user, project_id)
    try:
        doc = await doc_service.create_doc_from_ui(
            user, body.title, body.description or "", body.mode, project_id,
        )
    except doc_service.DocRequestError as exc:
        raise _http_error(
            _REQUEST_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc),
        )
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError as exc:
        raise _http_error(500, "doc_storage_error", str(exc))
    return _doc_row(user, doc, _ui_access(user, doc))


# ---------------------------------------------------------------------------
# One doc
# ---------------------------------------------------------------------------


@router.get("/docs/{doc_id}")
async def get_ui_doc(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The row plus ``content`` (the whole ``doc.md``) and ``assets``.

    ``assets`` is ``[{"name", "size", "mime"}]`` for the embedded images
    the asset route serves: regular files directly in ``assets/`` (symlinks,
    directories, special and hidden files skipped), sorted by name, ``mime``
    from the extension, entries without a raster image extension left out.
    At most ``DOC_MAX_ASSETS`` entries, so it rides on this payload rather
    than a separate endpoint; the list route does not carry it.

    404 ``doc_not_found`` for a missing or hidden doc (identical bodies).
    """
    _require_docs_enabled(user)
    doc, access = await _get_doc_for_ui(user, doc_id)
    content = await _doc_files_call(doc_files.read_body, doc["id"])
    assets = await _doc_files_call(doc_files.list_assets, doc["id"])
    row = _doc_row(user, doc, access)
    row["content"] = content
    row["assets"] = _asset_rows(assets)
    row["last_write_conversation"] = await _last_write_conversation(user, row)
    return row


async def _last_write_conversation(user: dict, row: dict) -> Optional[dict]:
    """``{id, title, project_id}`` of the conversation named by the row's
    ``last_write_source``, for the viewer's "Last written by" footer.

    One metadata-row lookup instead of the viewer fetching the whole chat
    history for a title. None when the source is not a conversation (``ui``
    / ``action_request:<id>`` / blanked for non-owners) or the conversation
    no longer exists -- the viewer then shows "a deleted conversation".
    """
    source = row.get("last_write_source")
    if not isinstance(source, str) or not source.startswith("conversation:"):
        return None
    conversation_id = source[len("conversation:"):]
    if not conversation_id:
        return None
    from chat.storage import ChatStorage
    from db.conversation_store import get_conversation_meta

    meta = await get_conversation_meta(user["id"], conversation_id)
    if not meta:
        return None
    title = await asyncio.to_thread(ChatStorage._resolve_list_title, conversation_id, meta)
    return {
        "id": conversation_id,
        "title": title,
        "project_id": meta.get("project_id"),
    }


@router.put("/docs/{doc_id}")
async def update_ui_doc(
    doc_id: str,
    body: UpdateDocRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Rename and/or change the description (owner only).

    ``expected_updated_at`` is the optimistic-concurrency token: a mismatch
    is a flat 409 ``stale_update`` carrying the ``current`` row. 409
    ``duplicate_title``; 400 ``invalid_title`` / ``invalid_description``;
    403 ``forbidden`` for a visible doc the user does not own.
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    _require_owner(user, doc, "rename it")
    try:
        title, description = doc_service.validate_doc_metadata(
            body.title, body.description,
        )
        updated = await doc_store.update_doc_metadata(
            doc["id"],
            title=title,
            description=description,
            expected_updated_at=body.expected_updated_at,
        )
    except doc_service.DocRequestError as exc:
        raise _http_error(400, exc.code, str(exc))
    except doc_store.StaleDocError as exc:
        # Flat body (no ``detail`` wrapper), like PUT /routines/{id}.
        current = exc.current
        return JSONResponse(
            status_code=409,
            content={
                "error": "stale_update",
                "message": "This doc was modified by someone else.",
                "current": _doc_row(user, current, _ui_access(user, current)),
            },
        )
    except doc_store.DuplicateDocTitleError as exc:
        raise _http_error(409, "duplicate_title", str(exc))
    except doc_store.DocValidationError as exc:
        raise _http_error(400, "invalid_request", str(exc))
    if updated is None:
        raise _doc_not_found(doc_id)
    if updated["updated_at"] != doc["updated_at"]:
        doc_events.publish_doc_list_changed(updated["owner_id"])
        doc_events.publish_doc_changed(
            updated["owner_id"], updated["id"], updated["updated_at"],
        )
    return _doc_row(user, updated, _ui_access(user, updated))


@router.put("/docs/{doc_id}/mode")
async def set_ui_doc_mode(
    doc_id: str,
    body: SetDocModeRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Not switchable in v1: refuses every doc, whatever the body says.

    Checked in order: 404 ``doc_not_found`` (missing, hidden, or in a
    public project hidden by the ``public_projects`` gate); 403
    ``forbidden`` for a non-owner; then 400 ``user_doc_mode_private`` for a
    user doc (always private) or 400 ``project_doc_mode_inherited`` for a
    project doc (its mode is the project's). Nothing changes and no event
    is sent. Kept registered so an old client gets a clear error.
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    _require_owner(user, doc, "change its mode")
    _raise_request_error(doc_service.mode_switch_refusal(doc), doc_id)


@router.get("/docs/{doc_id}/assets/{name}")
async def get_ui_doc_asset(
    doc_id: str,
    name: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Serve one embedded image inline with its real MIME type.

    404 ``asset_not_found`` for anything that is not a regular,
    non-symlink file directly inside the doc's ``assets/`` with a raster
    image extension (traversal names included). Assets are written only by
    the server (sniffed by magic bytes on the way in); the doc directory is
    never mounted into a sandbox.
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    try:
        # read_asset re-validates the name and opens the leaf O_NOFOLLOW +
        # S_ISREG (FileResponse would follow a symlink at open time).
        data = await asyncio.to_thread(doc_files.read_asset, doc["id"], name)
    except (doc_files.DocFileError, OSError):
        raise _http_error(404, "asset_not_found", f"Image not found: {name}")
    media_type = _INLINE_IMAGE_MIMES.get(
        os.path.splitext(name)[1].lower(), "application/octet-stream",
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "private",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/docs/{doc_id}/download")
async def download_ui_doc(
    doc_id: str,
    download_format: str = Query("md", alias="format"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Download a doc: ``?format=md`` (default) = ``doc.md`` as
    ``<title>.md``; ``?format=zip`` = ``doc.md`` + ``assets/`` as
    ``<title>.zip``.

    400 ``invalid_format`` for any other format; 400 ``invalid_doc_files``
    when the doc directory holds a symlink or special file (or the body is
    missing).
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    if download_format not in DOWNLOAD_FORMATS:
        raise _http_error(
            400, "invalid_format",
            f"format must be one of: {', '.join(DOWNLOAD_FORMATS)}.",
        )
    disposition = _attachment_disposition(
        _download_filename(doc["title"], download_format),
    )
    if download_format == "md":
        content = await _doc_files_call(doc_files.read_body, doc["id"])
        return Response(
            content=content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )
    zip_path = await _doc_files_call(doc_files.build_zip, doc["id"], doc["title"])
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
        background=BackgroundTask(os.unlink, zip_path),
    )


@router.delete("/docs/{doc_id}")
async def delete_ui_doc(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a doc (owner only): the row (shares go with it), then the
    directory (best-effort, logged). 403 ``forbidden`` for a non-owner."""
    _require_docs_enabled(user)
    await _get_doc_for_ui(user, doc_id)
    try:
        await doc_service.delete_doc_from_ui(user, doc_id)
    except doc_service.DocRequestError as exc:
        _raise_request_error(exc, doc_id)
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError:
        raise _doc_not_found(doc_id)
    return {"deleted": True}
