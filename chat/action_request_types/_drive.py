"""Shared Google Drive plumbing for Drive-backed action request handlers.

Used by ``upload_to_drive.py`` and ``create_drive_folder.py``: API base
constants, the ``drive.file`` scope check helpers, Google-API error
extraction, best-effort folder-name resolution for preview cards, and the
``create_folder()`` call both handlers share.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from auth.google_credentials import make_authenticated_request
from chat.action_request_types._google import _extract_google_api_error

logger = logging.getLogger(__name__)

# Upload host/path (distinct from the read/metadata base used by authed_get
# and folder creation).
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _drive_reauth_message() -> str:
    return (
        "Google Drive write access requires reconnecting Google Services via "
        "Settings > Data Connections."
    )


async def _resolve_folder_name(folder_id: str, user: dict | None) -> str | None:
    """Best-effort: resolve a Drive folder id to its human name for the
    preview card. Mirrors ``_resolve_calendar_name`` -- failures are
    swallowed and the raw id (or "My Drive (root)") is shown instead.
    """
    if not user or not user.get("google_services_oauth"):
        return None

    path = quote(folder_id, safe="")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await make_authenticated_request(
                client,
                user,
                "GET",
                f"{DRIVE_API_BASE}/files/{path}?fields=name&supportsAllDrives=true",
            )
        if response.status_code >= 400:
            return None
        payload = response.json()
        name = payload.get("name")
        if name and str(name).strip():
            return str(name).strip()
    except Exception:
        logger.warning(
            "[drive] Failed to resolve folder name for %s",
            folder_id,
            exc_info=True,
        )
    return None


async def create_folder(
    user: dict,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """Create a Drive folder via ``files.create`` and return the payload.

    Assumes the caller has already verified the Google connection and the
    ``drive.file`` scope (per-handler preconditions). Raises RuntimeError
    on API errors, using the same reauth/error-extraction heuristics as
    the multipart upload call.
    """
    metadata: dict = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    url = f"{DRIVE_API_BASE}/files?supportsAllDrives=true&fields=id,name"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await make_authenticated_request(
                client,
                user,
                "POST",
                url,
                json=metadata,
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
