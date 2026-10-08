"""REST API endpoints for file browser functionality.

Two workspaces are browsable: a conversation's own workspace
(/conversations/{id}/files..., every conversation incl. project ones)
and a project's shared workspace (/projects/{id}/files...). Both route
sets share the root-parameterised handlers below; the copy routes move
entries between a project conversation's workspace and its project's.
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import HTTPException, Depends, APIRouter, UploadFile, File, Query, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from typing import List, Optional
from chat.realtime import bus, events as realtime_events
from chat.conversation_access import (
    resolve_owned_project_workspace,
    resolve_owned_workspace,
)
from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.file_storage import (
    list_workspace_files,
    save_uploaded_file,
    save_uploaded_file_with_path,
    get_file_download,
    get_file_content,
    create_folder_zip,
    delete_workspace_item,
    count_workspace_item_files,
    create_workspace_folder,
    copy_entry,
    CopyEntryError,
    is_scratch_source,
    MAX_FILE_SIZE
)
from auth.google_credentials import make_authenticated_request


# Composer paste attachment configuration.
# Only PNG and JPEG are accepted; other clipboard image types (gif/webp/svg)
# are rejected up front. Magic-byte sniffing is performed in addition to the
# content_type check so a tampered request can't smuggle non-image bytes.
_COMPOSER_ATTACHMENT_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg"})
# 10 MB per image: gives some headroom over Anthropic's 5 MB limit (the
# provider returns None for oversized images and the conversation loop
# substitutes a text note). Gemini accepts larger files.
_COMPOSER_ATTACHMENT_MAX_SIZE = 10 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _sniff_image_mime(data: bytes) -> Optional[str]:
    """Return the inferred image MIME by magic bytes, or None if unknown."""
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    return None


# Raster image types served inline with their real MIME by the download
# endpoint, so the chat UI can embed workspace images directly via
# <img src=".../files/download?path=...."> (inline markdown images and
# composer-attachment thumbnails). SVG is deliberately NOT here: served
# inline as image/svg+xml, a workspace SVG opened directly would execute
# its scripts on the app origin. Everything else stays a generic
# octet-stream attachment download.
_INLINE_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

logger = logging.getLogger(__name__)

DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"


def _publish_file_list_changed(
    user_id: int,
    scope: str,
    conversation_id: Optional[str],
    project_id: Optional[str],
) -> None:
    """Best-effort: publish ``file_list_changed`` so file browsers in any
    tab silent-refresh.

    ``scope`` is the workspace that changed, set explicitly by each route:
    ``"conversation"`` for the conversation routes (project conversations
    included -- they write to their own conversation workspace) and
    ``"project"`` for the project routes, whose events reach every sibling
    conversation's tab via the ``scope === "project"`` filter on the FE.
    """
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_file_list_changed(
                conversation_id=conversation_id,
                project_id=project_id,
                scope=scope,
            ),
        )
    except Exception:
        logger.debug(
            "[file_routes] publish file_list_changed failed "
            "(user_id=%s, scope=%s, conversation_id=%s, project_id=%s)",
            user_id, scope, conversation_id, project_id, exc_info=True,
        )


@contextmanager
def _file_errors():
    """Map the file_storage ``ValueError`` / ``FileNotFoundError`` contract
    onto 400 ``invalid_path`` / 404 ``not_found``."""
    try:
        yield
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_path", "message": str(e)},
        )
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(e)},
        )


# ---------------------------------------------------------------------------
# Root-parameterised handlers. ``root`` is a browsable workspace root from
# ``resolve_owned_workspace`` (conversation workspace) or
# ``resolve_owned_project_workspace`` (shared project workspace); the route
# wrappers below own authentication, ownership and the realtime event.
# ---------------------------------------------------------------------------


async def _list(root: Path, path: str) -> dict:
    """List one directory: ``{currentPath, files, canGoUp}``."""
    with _file_errors():
        return await asyncio.to_thread(list_workspace_files, root, path)


async def _upload(
    root: Path,
    files: List[UploadFile],
    paths: Optional[List[str]],
    path: str,
) -> dict:
    """Save uploaded files under ``path``: ``{uploadedFiles, errors}``.

    Per-file failures are collected in ``errors`` rather than failing the
    request.
    """
    uploaded_files = []
    errors = []

    for i, file in enumerate(files):
        try:
            # Read file content
            content = await file.read()

            # Check file size
            if len(content) > MAX_FILE_SIZE:
                errors.append({
                    "filename": file.filename or "unknown",
                    "error": "file_too_large",
                    "message": f"File exceeds maximum size of {MAX_FILE_SIZE // (1024 * 1024)}MB"
                })
                continue

            # Determine the save path for this file
            if paths and i < len(paths) and "/" in paths[i]:
                # Folder upload: paths[i] contains relative path like "folder/sub/file.txt"
                result = await save_uploaded_file_with_path(
                    root,
                    paths[i],
                    content,
                    path  # base destination path
                )
            else:
                # Flat upload: use existing behavior
                result = await save_uploaded_file(
                    root,
                    file.filename or "unnamed_file",
                    content,
                    path
                )
            uploaded_files.append(result)

        except ValueError as e:
            # Use the relative path when available (more informative for folder uploads)
            err_filename = (paths[i] if paths and i < len(paths) else None) or file.filename or "unknown"
            errors.append({
                "filename": err_filename,
                "error": "invalid_file",
                "message": str(e)
            })
        except Exception as e:
            err_filename = (paths[i] if paths and i < len(paths) else None) or file.filename or "unknown"
            errors.append({
                "filename": err_filename,
                "error": "upload_failed",
                "message": str(e)
            })

    return {
        "uploadedFiles": uploaded_files,
        "errors": errors
    }


async def _read_content(root: Path, path: str) -> dict:
    """Text content of a viewable file: ``{name, path, content, size}``."""
    with _file_errors():
        content, filename, size = await asyncio.to_thread(
            get_file_content, root, path
        )
    return {
        "name": filename,
        "path": path,
        "content": content,
        "size": size
    }


def _download(root: Path, path: str) -> FileResponse:
    """Serve one file; raster images inline with their real MIME."""
    with _file_errors():
        file_path, filename = get_file_download(root, path)
    inline_mime = _INLINE_IMAGE_MIMES.get(os.path.splitext(filename)[1].lower())
    if inline_mime:
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type=inline_mime,
            content_disposition_type="inline",
        )
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream"
    )


async def _download_folder(root: Path, path: str) -> FileResponse:
    """Serve a folder as a temporary zip, deleted after the response."""
    try:
        with _file_errors():
            temp_zip_path, folder_name = await asyncio.to_thread(
                create_folder_zip, root, path
            )
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "zip_creation_failed",
                "message": str(e)
            }
        )
    return FileResponse(
        path=temp_zip_path,
        filename=f"{folder_name}.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, temp_zip_path),
    )


async def _info(root: Path, path: str) -> dict:
    """``{name, type, fileCount}`` for a file or folder."""
    with _file_errors():
        result = await asyncio.to_thread(count_workspace_item_files, root, path)
    return {
        "name": result["name"],
        "type": result["type"],
        "fileCount": result["count"],
    }


async def _delete(root: Path, path: str) -> dict:
    """Delete a file or folder: ``{name, type, deletedCount}``."""
    with _file_errors():
        result = await asyncio.to_thread(delete_workspace_item, root, path)
    return {
        "name": result["name"],
        "type": result["type"],
        "deletedCount": result["deleted_count"],
    }


class CreateFolderRequest(BaseModel):
    """Request body for creating a new folder in the workspace."""
    path: str  # parent directory (relative, "" or "/" for workspace root)
    name: str  # folder name to create (no slashes, no ..)


def _create_folder(root: Path, body: CreateFolderRequest) -> dict:
    """Create one empty folder: ``{name, path}``."""
    with _file_errors():
        result = create_workspace_folder(root, body.path, body.name)
    return {
        "name": result["name"],
        "path": result["path"],
    }


class SaveToDriveRequest(BaseModel):
    """Request body for saving a workspace file to Google Drive."""
    path: str
    title: str


async def _save_to_drive(root: Path, body: SaveToDriveRequest, user: dict) -> dict:
    """Upload a markdown file to Google Drive as a Google Doc.

    Reads the raw markdown from ``root`` and uploads it to Google Drive,
    which natively converts markdown to a Google Document. Returns
    ``{id, name, url}`` of the created Doc.
    """
    with _file_errors():
        content, filename, size = get_file_content(root, body.path)

    # Upload markdown to Google Drive via multipart upload.
    # Drive natively converts markdown to Google Docs format on import.
    boundary = "----quest_doc_import_boundary"
    metadata = json.dumps({
        "name": body.title,
        "mimeType": "application/vnd.google-apps.document",
    })
    multipart_body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: text/markdown; charset=UTF-8\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--"
    )

    url = f"{DRIVE_UPLOAD_BASE}/files?uploadType=multipart"

    async with httpx.AsyncClient() as client:
        response = await make_authenticated_request(
            client,
            user,
            "POST",
            url,
            content=multipart_body.encode("utf-8"),
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )

    data = response.json()

    if not response.is_success:
        error_msg = data.get("error", {}).get("message", "Drive API error")
        raise HTTPException(status_code=response.status_code, detail=error_msg)

    doc_id = data.get("id", "")
    doc_name = data.get("name", body.title)
    return {
        "id": doc_id,
        "name": doc_name,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }


# Create APIRouter for file endpoints
router = APIRouter(
    prefix="/app/api",
    tags=["files"]
)


# ---------------------------------------------------------------------------
# Conversation workspace routes: the conversation's own workspace for every
# conversation, standalone or in a project. Events: scope "conversation".
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/files")
async def list_files(
    conversation_id: str,
    path: str = Query(default="", description="Relative path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """List a directory of the conversation workspace.

    Returns ``{currentPath, files, canGoUp}``; 404 if the conversation is
    not the caller's or the directory is missing, 400 for an invalid path.
    """
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return await _list(root, path)


@router.post("/conversations/{conversation_id}/files/upload")
async def upload_files(
    conversation_id: str,
    files: List[UploadFile] = File(...),
    paths: Optional[List[str]] = Form(None),
    path: str = Query(default="", description="Destination path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Upload files into the conversation workspace.

    ``paths`` carries per-file relative paths for folder uploads. Returns
    ``{uploadedFiles, errors}``; 404 if the conversation is not the caller's.
    """
    user_id = user["id"]
    meta, root = await resolve_owned_workspace(user_id, conversation_id)
    result = await _upload(root, files, paths, path)
    if result["uploadedFiles"]:
        _publish_file_list_changed(
            user_id, "conversation", conversation_id, meta.get("project_id"),
        )
    return result


@router.post("/conversations/{conversation_id}/composer-attachments")
async def upload_composer_attachments(
    conversation_id: str,
    files: List[UploadFile] = File(...),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Persist images pasted into the chat composer.

    Accepts PNG/JPEG only and stores each file under
    ``workspace/pasted/<attachment_id>.<ext>`` so the on-disk layout is
    distinct from the user-managed workspace root. Returns the array of
    refs the composer will pass with the next ``send_message`` envelope.

    The endpoint is intentionally stricter than ``/files/upload``: it
    rejects non-image content_types, validates magic bytes, and caps the
    per-file size at 10 MB. Anthropic's 5 MB image limit is enforced
    later in ``anthropic_provider.upload_file`` -- larger files still
    land on disk and the LLM handoff degrades to a text note.
    """
    user_id = user["id"]

    meta, workspace_path = await resolve_owned_workspace(user_id, conversation_id)

    attachments: list[dict] = []
    errors: list[dict] = []

    for upload in files:
        original_name = upload.filename or "pasted-image"
        try:
            content = await upload.read()
        except Exception as exc:
            errors.append({
                "filename": original_name,
                "error": "read_failed",
                "message": str(exc),
            })
            continue

        if len(content) == 0:
            errors.append({
                "filename": original_name,
                "error": "empty_file",
                "message": "Empty file",
            })
            continue

        if len(content) > _COMPOSER_ATTACHMENT_MAX_SIZE:
            errors.append({
                "filename": original_name,
                "error": "file_too_large",
                "message": (
                    f"Image exceeds maximum size of "
                    f"{_COMPOSER_ATTACHMENT_MAX_SIZE // (1024 * 1024)}MB"
                ),
            })
            continue

        declared_mime = (upload.content_type or "").lower()
        sniffed_mime = _sniff_image_mime(content)
        # Trust the sniffed MIME when present; fall back to declared content
        # type only when sniffing is inconclusive (defends against a hostile
        # client lying about content_type).
        mime_type = sniffed_mime or declared_mime
        if mime_type not in _COMPOSER_ATTACHMENT_ALLOWED_MIMES:
            errors.append({
                "filename": original_name,
                "error": "unsupported_mime",
                "message": "Only PNG and JPEG images are accepted",
            })
            continue
        if sniffed_mime is None or (
            declared_mime
            and declared_mime in _COMPOSER_ATTACHMENT_ALLOWED_MIMES
            and declared_mime != sniffed_mime
        ):
            # Mismatch between declared content_type and the magic-byte
            # sniff: reject so a tampered request can't pass off arbitrary
            # bytes as a JPEG/PNG. (A missing content_type is OK as long as
            # the magic bytes are valid.)
            errors.append({
                "filename": original_name,
                "error": "mime_mismatch",
                "message": "File contents do not match a PNG or JPEG image",
            })
            continue

        ext = "png" if mime_type == "image/png" else "jpg"
        attachment_id = uuid.uuid4().hex
        stored_filename = f"{attachment_id}.{ext}"

        try:
            saved = await save_uploaded_file(
                workspace_path,
                stored_filename,
                content,
                "pasted",
            )
        except ValueError as exc:
            errors.append({
                "filename": original_name,
                "error": "invalid_file",
                "message": str(exc),
            })
            continue
        except Exception as exc:
            errors.append({
                "filename": original_name,
                "error": "save_failed",
                "message": str(exc),
            })
            continue

        # ``saved["path"]`` is "/pasted/<attachment_id>.<ext>"; strip the
        # leading slash for the workspace-relative ref carried on the WS
        # envelope and the persisted message row.
        workspace_rel = saved["path"].lstrip("/")
        attachments.append({
            "attachment_id": attachment_id,
            "filename": stored_filename,
            "workspace_path": workspace_rel,
            "mime_type": mime_type,
            "size_bytes": saved["size"],
        })

    if attachments:
        _publish_file_list_changed(
            user_id, "conversation", conversation_id, meta.get("project_id"),
        )

    return {
        "attachments": attachments,
        "errors": errors,
    }


@router.get("/conversations/{conversation_id}/files/content")
async def read_file_content(
    conversation_id: str,
    path: str = Query(..., description="File path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Read the text content of a conversation-workspace file.

    Returns ``{name, path, content, size}``; 404 if the conversation or file
    is missing, 400 for an invalid path or unsupported type.
    """
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return await _read_content(root, path)


@router.get("/conversations/{conversation_id}/files/download")
async def download_file(
    conversation_id: str,
    path: str = Query(..., description="File path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Download a conversation-workspace file (raster images inline)."""
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return _download(root, path)


@router.get("/conversations/{conversation_id}/files/download-folder")
async def download_folder_as_zip(
    conversation_id: str,
    path: str = Query(..., description="Folder path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Download a conversation-workspace folder as a zip archive.

    404 if the conversation or folder is missing, 400 for an invalid path,
    500 if zip creation fails.
    """
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return await _download_folder(root, path)


@router.get("/conversations/{conversation_id}/files/info")
async def file_info(
    conversation_id: str,
    path: str = Query(..., description="File or folder path within workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """``{name, type, fileCount}`` for a conversation-workspace entry."""
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return await _info(root, path)


@router.delete("/conversations/{conversation_id}/files")
async def delete_file(
    conversation_id: str,
    path: str = Query(..., description="File or folder path within workspace to delete"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Delete a conversation-workspace file or folder.

    Returns ``{name, type, deletedCount}``; 404 if the conversation or path
    is missing, 400 for an invalid path or the root.
    """
    user_id = user["id"]
    meta, root = await resolve_owned_workspace(user_id, conversation_id)
    result = await _delete(root, path)
    _publish_file_list_changed(
        user_id, "conversation", conversation_id, meta.get("project_id"),
    )
    return result


@router.post("/conversations/{conversation_id}/files/create-folder")
async def create_folder(
    conversation_id: str,
    body: CreateFolderRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create an empty folder in the conversation workspace.

    Returns ``{name, path}``; 404 if the conversation or parent is missing,
    400 for an invalid path or name.
    """
    user_id = user["id"]
    meta, root = await resolve_owned_workspace(user_id, conversation_id)
    result = _create_folder(root, body)
    _publish_file_list_changed(
        user_id, "conversation", conversation_id, meta.get("project_id"),
    )
    return result


@router.post("/conversations/{conversation_id}/files/save-to-drive")
async def save_file_to_drive(
    conversation_id: str,
    body: SaveToDriveRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Save a conversation-workspace markdown file to Google Drive as a Doc.

    Returns ``{id, name, url}`` of the created Doc.
    """
    _meta, root = await resolve_owned_workspace(user["id"], conversation_id)
    return await _save_to_drive(root, body, user)


# ---------------------------------------------------------------------------
# Copy / move between a project conversation's workspace and its project's
# workspace (devplan 00009 section 7.3). Events: both scopes.
# ---------------------------------------------------------------------------


class CopyEntryRequest(BaseModel):
    """Request body for the copy-to-project / copy-from-project routes."""
    path: str  # source path in the source space
    dest: Optional[str] = None  # destination path; default = ``path``
    overwrite: bool = False
    move: bool = False
    include_hidden: bool = False


_COPY_ERROR_STATUS = {
    "not_found": 404,
    "destination_exists": 409,
}


async def _copy_between_spaces(
    user: dict, conversation_id: str, body: CopyEntryRequest, *, to_project: bool,
) -> dict:
    """Shared body of the two copy routes.

    400 ``not_a_project_conversation`` for a standalone conversation;
    ``copy_entry`` refusals map to 404 ``not_found``, 409
    ``destination_exists`` and 400 for the rest; an I/O failure while
    copying is 500 ``copy_failed`` (both spaces are still announced as
    changed, since part of a merge may have been written). A move whose
    copy completed but whose source removal failed returns 200 with
    ``moved: false``. Scratch roots
    (``is_scratch_source``) are refused as a copy-to-project source only:
    copying into the conversation workspace promotes nothing.
    """
    user_id = user["id"]
    meta, conversation_root = await resolve_owned_workspace(user_id, conversation_id)
    project_id = meta.get("project_id")
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_a_project_conversation",
                "message": "This conversation is not part of a project",
            },
        )
    _project, project_root = await resolve_owned_project_workspace(user_id, project_id)

    if to_project and is_scratch_source(body.path):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "forbidden_source",
                "message": (
                    ".responses/, .subagent_responses/ and pasted/ hold "
                    "conversation scratch files and cannot be copied to "
                    "the project"
                ),
            },
        )

    dest = body.dest if body.dest and body.dest.strip("/ ") else body.path
    src_root, dst_root = (
        (conversation_root, project_root) if to_project
        else (project_root, conversation_root)
    )
    try:
        result = await asyncio.to_thread(
            copy_entry, src_root, body.path, dst_root, dest,
            overwrite=body.overwrite,
            include_hidden=body.include_hidden,
            move=body.move,
        )
    except CopyEntryError as e:
        raise HTTPException(
            status_code=_COPY_ERROR_STATUS.get(e.code, 400),
            detail={"error": e.code, "message": e.message},
        )
    except OSError as e:
        logger.warning(
            "copy %s project failed (conversation=%s, path=%r): %s",
            "to" if to_project else "from", conversation_id, body.path, e,
        )
        _publish_file_list_changed(user_id, "conversation", conversation_id, project_id)
        _publish_file_list_changed(user_id, "project", conversation_id, project_id)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "copy_failed",
                "message": f"The copy failed: {e.strerror or e}",
            },
        )

    _publish_file_list_changed(user_id, "conversation", conversation_id, project_id)
    _publish_file_list_changed(user_id, "project", conversation_id, project_id)
    return result


@router.post("/conversations/{conversation_id}/files/copy-to-project")
async def copy_to_project(
    conversation_id: str,
    body: CopyEntryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Copy (or move) an entry from the conversation workspace into the
    project workspace. See ``chat.file_storage.copy_entry`` for the rules."""
    return await _copy_between_spaces(user, conversation_id, body, to_project=True)


@router.post("/conversations/{conversation_id}/files/copy-from-project")
async def copy_from_project(
    conversation_id: str,
    body: CopyEntryRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Copy (or move) an entry from the project workspace into the
    conversation workspace. See ``chat.file_storage.copy_entry``."""
    return await _copy_between_spaces(user, conversation_id, body, to_project=False)


# ---------------------------------------------------------------------------
# Project workspace routes: the workspace shared by every conversation of
# the project (404 unless the caller owns it). Events: scope "project".
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/files")
async def list_project_workspace_files(
    project_id: str,
    path: str = Query(default="", description="Relative path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List a directory of the project workspace (empty for a project that
    never wrote a file)."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return await _list(root, path)


@router.post("/projects/{project_id}/files/upload")
async def upload_project_files(
    project_id: str,
    files: List[UploadFile] = File(...),
    paths: Optional[List[str]] = Form(None),
    path: str = Query(default="", description="Destination path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Upload files into the project workspace: ``{uploadedFiles, errors}``."""
    user_id = user["id"]
    _project, root = await resolve_owned_project_workspace(user_id, project_id)
    result = await _upload(root, files, paths, path)
    if result["uploadedFiles"]:
        _publish_file_list_changed(user_id, "project", None, project_id)
    return result


@router.get("/projects/{project_id}/files/content")
async def read_project_file_content(
    project_id: str,
    path: str = Query(..., description="File path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Read the text content of a project-workspace file."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return await _read_content(root, path)


@router.get("/projects/{project_id}/files/download")
async def download_project_file(
    project_id: str,
    path: str = Query(..., description="File path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Download a project-workspace file (raster images inline)."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return _download(root, path)


@router.get("/projects/{project_id}/files/download-folder")
async def download_project_folder_as_zip(
    project_id: str,
    path: str = Query(..., description="Folder path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Download a project-workspace folder as a zip archive."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return await _download_folder(root, path)


@router.get("/projects/{project_id}/files/info")
async def project_file_info(
    project_id: str,
    path: str = Query(..., description="File or folder path within the project workspace"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """``{name, type, fileCount}`` for a project-workspace entry."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return await _info(root, path)


@router.delete("/projects/{project_id}/files")
async def delete_project_file(
    project_id: str,
    path: str = Query(..., description="File or folder path within the project workspace to delete"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a project-workspace file or folder."""
    user_id = user["id"]
    _project, root = await resolve_owned_project_workspace(user_id, project_id)
    result = await _delete(root, path)
    _publish_file_list_changed(user_id, "project", None, project_id)
    return result


@router.post("/projects/{project_id}/files/create-folder")
async def create_project_folder(
    project_id: str,
    body: CreateFolderRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create an empty folder in the project workspace."""
    user_id = user["id"]
    _project, root = await resolve_owned_project_workspace(user_id, project_id)
    result = _create_folder(root, body)
    _publish_file_list_changed(user_id, "project", None, project_id)
    return result


@router.post("/projects/{project_id}/files/save-to-drive")
async def save_project_file_to_drive(
    project_id: str,
    body: SaveToDriveRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Save a project-workspace markdown file to Google Drive as a Doc."""
    _project, root = await resolve_owned_project_workspace(user["id"], project_id)
    return await _save_to_drive(root, body, user)
