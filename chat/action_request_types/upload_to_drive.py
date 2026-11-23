"""UploadToDriveHandler -- approve-to-upload action request for Drive.

The model issues a
``create_action_request(request_type="upload_to_drive",
params={"files": [{"path": "...", ...}, ...], ...}, reasoning="...")``
call which appends an inline approval card to the chat. On Approve, the
handler reads each named workspace file off disk and uploads its raw
bytes to the user's Google Drive via the same multipart upload mechanism
the "Save to Drive" file-browser feature uses
(``chat/file_routes.py:save_file_to_drive``). On Deny/Revise nothing is
uploaded.

One request carries up to ``_MAX_FILES_PER_REQUEST`` files bound for a
single destination folder, so a batch of uploads costs the user one
approval round trip instead of one per file. The legacy single-file
shape (top-level ``path`` / ``filename``) is still accepted for
backward compatibility with pending pre-deploy rows and older sub-agent
proposals; ``execute()`` normalizes both shapes into one entry list.

Unlike Save-to-Drive (which is markdown-only and sets
``mimeType: application/vnd.google-apps.document`` so Drive *converts* the
file into a native Google Doc), this handler uploads each file **as-is**:
the content-type is guessed from the filename and the bytes are stored
verbatim. The result URLs are therefore the generic Drive file links
rather than the Docs editor URL.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os

import httpx

# Imported as a module-local name (rather than via _drive) so existing
# tests can monkeypatch
# ``chat.action_request_types.upload_to_drive.make_authenticated_request``
# for the upload call. The folder-creation call goes through
# ``_drive.create_folder`` and is patched on the _drive module.
from auth.google_credentials import make_authenticated_request
from chat.action_request_types import _drive
from chat.action_request_types._drive import (
    DRIVE_UPLOAD_BASE,
    DRIVE_FILE_SCOPE as _DRIVE_FILE_SCOPE,
    _drive_reauth_message,
    _resolve_folder_name,
)
from chat.action_request_types._google import _extract_google_api_error, require_google_scope
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._io_attachments import (
    read_resolved_file_bytes,
    resolve_workspace_file,
    validate_attachments_param,
)
from chat.action_request_types._param_validation import reject_unknown_params
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

# Match the shared IO-attachment cap (50 MB). uploadType=multipart is not
# resumable; a resumable upload path is out of scope for v1.
_UPLOAD_MAX_SIZE_BYTES = 50 * 1024 * 1024

# Per-request file-count cap. Uploads run sequentially on Approve, one
# file's bytes in memory at a time; the cap keeps a single approval from
# turning into a minutes-long upload marathon.
_MAX_FILES_PER_REQUEST = 10

# `folder_name` and `new_folder_parent_name` are server-injected after
# validation by the dispatch arm (conversation.py); they are not
# model-supplied and are therefore not in the allow-list (parallels
# `calendar_name` for create_calendar_invite). `path` / `filename` are
# the legacy single-file shape, kept for pending pre-deploy rows.
_ALLOWED_PARAMS = frozenset(
    {"files", "path", "filename", "folder_id", "new_folder_name", "new_folder_parent_id"}
)


def _validate_entry_path(path: str, *, label: str) -> None:
    """Same-turn shape rejection for a workspace path. Existence / size
    are deferred to execute() (validate_params has no conversation_id
    for workspace access)."""
    if os.path.isabs(path):
        raise ValueError(
            f"{label} must be workspace-relative; absolute paths are not allowed."
        )
    if any(part == ".." for part in path.replace("\\", "/").split("/")):
        raise ValueError(
            f"{label} must not contain parent-directory traversal ('..')."
        )


def _upload_entries(params: dict) -> list[dict]:
    """Normalize validated params into a ``[{path, filename?}]`` list.

    Accepts both the canonical ``files`` shape and the legacy top-level
    ``path`` / ``filename`` shape (pending rows created before the
    multi-file change carry the latter).
    """
    files = params.get("files")
    if files:
        return list(files)
    entry: dict = {"path": params["path"]}
    if params.get("filename"):
        entry["filename"] = params["filename"]
    return [entry]


class UploadToDriveHandler(ActionRequestHandler):
    """Upload workspace files to Google Drive after explicit user approval.

    Params:
        files (list, optional*): Entries of ``{path, filename?}`` --
            workspace-relative source path plus an optional Drive name
            override (defaults to the basename). 1..10 entries, no
            duplicate paths. All files land in the same destination
            folder. *Exactly one of ``files`` or the legacy ``path``
            must be supplied.
        path (str, optional*): Legacy single-file shape: workspace-relative
            path of the file to upload. No absolute paths and no ``..``
            traversal. Mutually exclusive with ``files``.
        filename (str, optional): Legacy single-file shape: name to give
            the file in Drive. Only valid alongside ``path``.
        folder_id (str, optional): Target Drive folder id. Defaults to the
            Drive root ("My Drive"). Mutually exclusive with
            ``new_folder_name``.
        new_folder_name (str, optional): Create a new Drive folder with
            this name at approval time and upload the file(s) into it.
            Mutually exclusive with ``folder_id``.
        new_folder_parent_id (str, optional): Parent folder id for the new
            folder (shared drives supported). Only valid alongside
            ``new_folder_name``; defaults to the Drive root.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.UPLOAD_TO_DRIVE

    @property
    def display_name(self) -> str:
        return "Upload to Drive"

    @property
    def approve_label(self) -> str:
        return "Upload"

    @property
    def resolved_label(self) -> str:
        return "Uploaded"

    def summary_snippet(self, params: dict) -> str:
        files = params.get("files")
        if isinstance(files, list) and files:
            first = files[0] if isinstance(files[0], dict) else {}
            name = str(first.get("filename") or "") \
                or str(first.get("path") or "").split("/")[-1]
            if len(files) > 1:
                return f"{len(files)} files: {name}, ..."
            return name
        # Legacy single {path, filename?} shape from old chat history.
        path = str(params.get("path") or "")
        return str(params.get("filename") or "") or path.split("/")[-1]

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        entries = _upload_entries(params)

        folder_id = params.get("folder_id")
        new_folder_name = params.get("new_folder_name")
        if new_folder_name:
            parent_value = params.get("new_folder_parent_name")
            parent_id = params.get("new_folder_parent_id")
            if not parent_value and parent_id and user:
                parent_value = await _resolve_folder_name(str(parent_id), user)
            if not parent_value:
                parent_value = str(parent_id) if parent_id else "My Drive (root)"
            folder_value = f'New folder "{new_folder_name}" in {parent_value}'
        elif folder_id:
            folder_value = params.get("folder_name")
            if not folder_value and user:
                folder_value = await _resolve_folder_name(str(folder_id), user)
            if not folder_value:
                folder_value = str(folder_id)
        else:
            folder_value = "My Drive (root)"

        if len(entries) == 1:
            path = entries[0].get("path", "")
            filename = entries[0].get("filename") or os.path.basename(path)
            return [
                {"key": "File", "value": str(filename)},
                {"key": "Source", "value": str(path)},
                {"key": "Destination", "value": str(folder_value)},
            ]

        fields: list[dict] = [
            {"key": "Files", "value": f"{len(entries)} files"},
        ]
        for i, entry in enumerate(entries):
            path = entry.get("path", "")
            filename = entry.get("filename")
            # Show the rename inline only when it changes the Drive name.
            if filename and filename != os.path.basename(path):
                value = f"{path} → {filename}"
            else:
                value = str(path)
            fields.append({"key": f"File {i + 1}", "value": value})
        fields.append({"key": "Destination", "value": str(folder_value)})
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        from chat.gemini_api.tool_handlers import _sanitize_workspace_filename

        validated: dict = {}

        if params.get("files") is not None:
            if params.get("path") is not None or params.get("filename") is not None:
                raise ValueError(
                    "files and path/filename are mutually exclusive: put every "
                    "upload (including a single one) in the files list."
                )
            entries = validate_attachments_param(params, "files")
            if not entries:
                raise ValueError(
                    "files must be a non-empty list of {path, filename?} objects"
                )
            if len(entries) > _MAX_FILES_PER_REQUEST:
                raise ValueError(
                    f"files supports at most {_MAX_FILES_PER_REQUEST} entries per "
                    "request; split larger batches into multiple requests."
                )
            seen_paths: set[str] = set()
            normalized_entries: list[dict] = []
            for idx, entry in enumerate(entries):
                path = entry["path"]
                _validate_entry_path(path, label=f"files[{idx}].path")
                if path in seen_paths:
                    raise ValueError(
                        f"files[{idx}].path duplicates an earlier entry "
                        f"({path!r}); list each file once per request."
                    )
                seen_paths.add(path)
                normalized: dict = {"path": path}
                filename = entry.get("filename")
                if filename:
                    # Strip path separators / leading dots so we never write
                    # a hidden Drive name; if it sanitizes to empty, drop it
                    # and fall back to the basename at execute() time.
                    cleaned = _sanitize_workspace_filename(filename)
                    if cleaned:
                        normalized["filename"] = cleaned
                normalized_entries.append(normalized)
            validated["files"] = normalized_entries
        else:
            path = params.get("path")
            if path is None or not isinstance(path, str):
                raise ValueError(
                    "Missing required parameter: files -- a non-empty list of "
                    "{path, filename?} objects to upload."
                )
            path = path.strip()
            if not path:
                raise ValueError("path must be a non-empty string")
            _validate_entry_path(path, label="path")
            validated["path"] = path

            filename = params.get("filename")
            if filename is not None:
                if not isinstance(filename, str):
                    raise ValueError("filename must be a string if provided")
                cleaned = _sanitize_workspace_filename(filename)
                if cleaned:
                    validated["filename"] = cleaned

        folder_id = params.get("folder_id")
        if folder_id is not None:
            if not isinstance(folder_id, str):
                raise ValueError("folder_id must be a string if provided")
            folder_id = folder_id.strip()
            if not folder_id:
                raise ValueError("folder_id must be a non-empty string if provided")
            validated["folder_id"] = folder_id

        new_folder_name = params.get("new_folder_name")
        if new_folder_name is not None:
            if not isinstance(new_folder_name, str):
                raise ValueError("new_folder_name must be a string if provided")
            # Drive folder names are metadata, not filesystem paths --
            # trim + non-empty only (no filename sanitization).
            new_folder_name = new_folder_name.strip()
            if not new_folder_name:
                raise ValueError("new_folder_name must be a non-empty string if provided")
            if "folder_id" in validated:
                raise ValueError(
                    "folder_id and new_folder_name are mutually exclusive: "
                    "pass folder_id to upload into an existing folder, or "
                    "new_folder_name to create the destination folder on approval."
                )
            validated["new_folder_name"] = new_folder_name

        new_folder_parent_id = params.get("new_folder_parent_id")
        if new_folder_parent_id is not None:
            if not isinstance(new_folder_parent_id, str):
                raise ValueError("new_folder_parent_id must be a string if provided")
            new_folder_parent_id = new_folder_parent_id.strip()
            if not new_folder_parent_id:
                raise ValueError(
                    "new_folder_parent_id must be a non-empty string if provided"
                )
            if "new_folder_name" not in validated:
                raise ValueError(
                    "new_folder_parent_id is only valid together with new_folder_name."
                )
            validated["new_folder_parent_id"] = new_folder_parent_id

        return validated

    async def _upload_one(
        self,
        user: dict,
        drive_name: str,
        file_bytes: bytes,
        content_type: str,
        parent_folder_id: str | None,
    ) -> dict:
        """POST one file's bytes to the Drive multipart upload endpoint and
        return the parsed response payload. Raises RuntimeError on API
        errors / reauth conditions."""
        # Build the Drive metadata. Do NOT set
        # application/vnd.google-apps.document (that triggers conversion and
        # is a Save-to-Drive, markdown-only behavior) -- we upload raw bytes.
        metadata: dict = {"name": drive_name, "mimeType": content_type}
        if parent_folder_id:
            metadata["parents"] = [parent_folder_id]

        # Assemble a multipart/related body as bytes (the Save-to-Drive code
        # builds a str then .encode()s it, which only works for text; raw
        # binary content must be concatenated as bytes).
        boundary = "----quest_drive_upload_boundary"
        metadata_json = json.dumps(metadata)
        multipart_body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata_json}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--".encode("utf-8")

        url = (
            f"{DRIVE_UPLOAD_BASE}/files"
            "?uploadType=multipart&supportsAllDrives=true"
        )

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await make_authenticated_request(
                    client,
                    user,
                    "POST",
                    url,
                    content=multipart_body,
                    headers={
                        "Content-Type": f"multipart/related; boundary={boundary}",
                    },
                )
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_drive_reauth_message()) from exc
            raise

        if response.status_code in (401, 403):
            error_text = _extract_google_api_error(response).lower()
            if "insufficient" in error_text or "permission" in error_text or "scope" in error_text:
                raise RuntimeError(_drive_reauth_message())

        if response.status_code >= 400:
            raise RuntimeError(
                f"Google Drive API error: {_extract_google_api_error(response)}"
            )

        return response.json()

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        require_google_scope(user, _DRIVE_FILE_SCOPE, _drive_reauth_message())

        if conversation_id is None:
            raise RuntimeError(
                "Cannot upload to Drive: conversation_id is missing."
            )

        entries = _upload_entries(params)

        # Pre-flight: resolve + validate every workspace file (shared
        # traversal guards + size cap) BEFORE any Drive mutation, so a
        # missing/oversize file fails the whole batch up front rather than
        # after some files (or the new folder) already landed in Drive.
        # Bytes are read one file at a time in the upload loop below.
        resolved_paths = []
        for entry in entries:
            resolved_paths.append(await resolve_workspace_file(
                conversation_id,
                project_id,
                entry["path"],
                max_size_bytes=_UPLOAD_MAX_SIZE_BYTES,
            ))

        # Create the destination folder on the fly when requested.
        created_folder: dict | None = None
        if params.get("new_folder_name"):
            created_folder = await _drive.create_folder(
                user,
                params["new_folder_name"],
                params.get("new_folder_parent_id"),
            )

        if created_folder is not None:
            parent_folder_id = created_folder["id"]
        else:
            parent_folder_id = params.get("folder_id")

        uploaded: list[dict] = []
        try:
            for entry, file_path in zip(entries, resolved_paths):
                basename, file_bytes, guessed_type = read_resolved_file_bytes(
                    file_path, entry["path"],
                )
                drive_name = entry.get("filename") or basename
                content_type = guessed_type or (
                    mimetypes.guess_type(drive_name)[0] or "application/octet-stream"
                )
                payload = await self._upload_one(
                    user, drive_name, file_bytes, content_type, parent_folder_id,
                )
                file_id = payload.get("id", "")
                uploaded.append({
                    "file_id": file_id,
                    "name": payload.get("name", drive_name),
                    "url": f"https://drive.google.com/file/d/{file_id}/view",
                })
        except Exception as exc:
            # A re-approve of this still-open request would re-upload the
            # files that already succeeded (and, when a folder was created,
            # create a *duplicate* folder -- Drive permits same-name
            # siblings); carry the recovery path in the error message.
            notes: list[str] = []
            if uploaded:
                summary = ", ".join(
                    f"{f['name']} ({f['file_id']})" for f in uploaded
                )
                notes.append(
                    f"{len(uploaded)} of {len(entries)} files were already "
                    f"uploaded: {summary}; retrying this request would upload "
                    f"them again. Propose a new upload_to_drive request with "
                    f"only the remaining files."
                )
            if created_folder is not None:
                notes.append(
                    f"folder "
                    f"'{created_folder.get('name', params['new_folder_name'])}' "
                    f"was already created as {created_folder['id']}; retrying "
                    f"this request will create another folder. Consider a new "
                    f"upload_to_drive request with folder_id={created_folder['id']}."
                )
            if notes:
                raise RuntimeError(f"{exc} -- note: {' Also: '.join(notes)}") from exc
            raise

        if "files" in params:
            result: dict = {
                "success": True,
                "count": len(uploaded),
                "files": uploaded,
            }
        else:
            # Legacy single-`path` request: keep the flat result shape the
            # model (and any pending pre-deploy row) expects.
            result = {
                "success": True,
                "file_id": uploaded[0]["file_id"],
                "name": uploaded[0]["name"],
                "url": uploaded[0]["url"],
            }
        if created_folder is not None:
            # Surface the created folder so the model can chain follow-up
            # uploads into it with plain folder_id.
            result["folder_id"] = created_folder["id"]
            result["folder_name"] = created_folder.get(
                "name", params["new_folder_name"]
            )
            result["folder_url"] = (
                f"https://drive.google.com/drive/folders/{created_folder['id']}"
            )
        return result
