"""Draft creation, send-to-self endpoints, and attachment resolvers."""

import base64
import mimetypes
from email.message import EmailMessage as PythonEmailMessage
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException, Depends

from auth.session import get_current_user
from auth.google_credentials import get_valid_service_credentials, make_authenticated_request
from chat.conversation_access import require_owned_conversation

from .constants import DRIVE_API_BASE, _MAX_TOTAL_ATTACHMENT_SIZE, _QUEST_SUBJECT_PREFIX
from .helpers import (
    attachment_content_id,
    convert_markdown_to_html,
    get_gmail_service,
    is_inline_image_mime,
)
from .models import CreateDraftRequest, SendEmailToSelfRequest


async def _resolve_drive_attachment(
    user: dict,
    file_id: str,
    filename_override: Optional[str] = None,
) -> dict:
    """Fetch a Google Drive file and return its content for attachment.

    Only non-native file types are supported. Native Google Workspace
    types (Docs, Sheets, Slides, etc.) are rejected -- the caller should
    include a link to the document in the email body instead.

    Args:
        user: Authenticated user dict (for API credentials).
        file_id: Google Drive file ID.
        filename_override: Optional filename to use instead of the Drive filename.

    Returns:
        Dict with keys: data (bytes), filename (str), mime_type (str).

    Raises:
        HTTPException: If the file is a native Google type, or cannot be fetched.
    """
    async with httpx.AsyncClient() as client:
        # Step 1: Get file metadata
        meta_url = f"{DRIVE_API_BASE}/files/{file_id}"
        meta_response = await make_authenticated_request(
            client, user, "GET", meta_url,
            params={"fields": "name,mimeType,size", "supportsAllDrives": "true"},
        )
        if not meta_response.is_success:
            raise HTTPException(
                status_code=meta_response.status_code,
                detail=f"Failed to get Drive file metadata for '{file_id}': {meta_response.text}",
            )
        metadata = meta_response.json()
        drive_name = metadata.get("name", "attachment")
        drive_mime = metadata.get("mimeType", "application/octet-stream")

        # Step 2: Reject native Google Workspace types
        if drive_mime.startswith("application/vnd.google-apps."):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot attach native Google file '{drive_name}' (type: {drive_mime}). "
                    f"Native Google files (Docs, Sheets, Slides, etc.) cannot be attached as files. "
                    f"Instead, include a link to the document in the email body: "
                    f"https://drive.google.com/file/d/{file_id}/view"
                ),
            )

        # Step 3: Download regular file binary content
        download_url = f"{DRIVE_API_BASE}/files/{file_id}"
        download_response = await make_authenticated_request(
            client, user, "GET", download_url,
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        if not download_response.is_success:
            raise HTTPException(
                status_code=download_response.status_code,
                detail=f"Failed to download Drive file '{file_id}': {download_response.text}",
            )

    return {
        "data": download_response.content,
        "filename": filename_override or drive_name,
        "mime_type": drive_mime,
    }


async def _resolve_workspace_attachment(
    conversation_id: str,
    workspace_path: str,
    filename_override: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Read a file from the conversation workspace for attachment.

    Resolves ``workspace_path`` against the conversation workspace root,
    ``data/chats/{conv}/workspace`` (``ChatStorage.get_conversation_workspace_root``),
    for standalone and project conversations alike; the shared project
    workspace is never consulted. Mirrors the path-traversal guards used by
    ``chat.action_request_types._io_attachments.read_workspace_attachments``
    so both call sites agree on what counts as a valid workspace path.

    Args:
        conversation_id: Conversation UUID.
        workspace_path: Workspace-relative path (no absolute paths, no ``..``).
        filename_override: Optional filename to use for the attachment.
            Sanitized via ``_sanitize_workspace_filename``; falls back to the
            resolved file's basename if the override sanitizes to empty.
        project_id: The conversation's project, if any. Never changes the
            root; only adds the ``copy_project_file`` hint to a not-found
            error.

    Returns:
        Dict with keys: data (bytes), filename (str), mime_type (str).

    Raises:
        HTTPException: If the file cannot be read or the path is invalid.
    """
    # Lazy import to mirror ``_io_attachments.read_workspace_attachments``
    # and avoid pulling chat-side modules in at module import time.
    from chat.gemini_api.tool_handlers import _sanitize_workspace_filename
    from chat.storage import ChatStorage

    raw_path = workspace_path or ""
    if not raw_path.strip():
        raise HTTPException(
            status_code=400,
            detail="Invalid workspace path: path cannot be empty",
        )

    workspace_dir = ChatStorage.get_conversation_workspace_root(conversation_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = workspace_dir.resolve()

    candidate_path = Path(raw_path)
    if candidate_path.is_absolute():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid workspace path {raw_path!r}: absolute paths are "
                "not allowed. Provide a workspace-relative path."
            ),
        )
    if any(part == ".." for part in candidate_path.parts):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid workspace path {raw_path!r}: parent-directory "
                "traversal ('..') is not allowed."
            ),
        )

    file_path = (workspace_root / candidate_path).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid workspace path {raw_path!r}: resolved destination "
                "is outside the conversation workspace."
            ),
        )

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Workspace file not found: {raw_path}"
                + (
                    " (paths are in this conversation's workspace; project "
                    "files must be copied into it first with "
                    "copy_project_file)"
                    if project_id else ""
                )
            ),
        )
    if not file_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Workspace path is not a regular file: {raw_path}",
        )

    file_data = file_path.read_bytes()
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    # Sanitize the override (strip path separators / leading dots). Fall
    # back to the resolved file's basename if the override is missing or
    # sanitizes to empty -- belt-and-suspenders, since add_attachment
    # RFC-2047-encodes the filename downstream.
    resolved_filename: Optional[str] = None
    if filename_override:
        candidate = _sanitize_workspace_filename(filename_override)
        if candidate:
            resolved_filename = candidate
    if not resolved_filename:
        resolved_filename = file_path.name

    return {
        "data": file_data,
        "filename": resolved_filename,
        "mime_type": mime_type,
    }


async def _resolve_gmail_attachment(
    user: dict,
    message_id: str,
    attachment_id: str,
    filename: str,
    mime_type: str = "application/octet-stream",
) -> dict:
    """Fetch an attachment from a Gmail message.

    Args:
        user: Authenticated user dict (for API credentials).
        message_id: Gmail message ID containing the attachment.
        attachment_id: Gmail attachment ID (from simplified message response).
        filename: Filename for the attachment.
        mime_type: MIME type of the attachment.

    Returns:
        Dict with keys: data (bytes), filename (str), mime_type (str).

    Raises:
        HTTPException: If the attachment cannot be fetched.
    """
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(status_code=401, detail={
            "error": "google_services_auth_required",
            "message": "Google services authorization required."
        })

    service = get_gmail_service(credentials)
    attachment = service.users().messages().attachments().get(
        userId='me', messageId=message_id, id=attachment_id
    ).execute()

    data = base64.urlsafe_b64decode(attachment['data'])

    return {
        "data": data,
        "filename": filename,
        "mime_type": mime_type,
    }


async def _resolve_request_attachments(
    user: dict,
    attachments: Optional[list],
    conversation_id: Optional[str],
) -> list:
    """Resolve a request's ``DraftAttachment`` list into raw-bytes dicts.

    Shared by the draft and send-to-self endpoints. Workspace attachments
    require a ``conversation_id`` that the caller owns; the total size is
    capped at ``_MAX_TOTAL_ATTACHMENT_SIZE``.

    Returns:
        List of dicts with keys: data (bytes), filename (str), mime_type (str).

    Raises:
        HTTPException: On invalid shapes, unowned conversations, or
            attachments that cannot be fetched.
    """
    if not attachments:
        return []

    # Validate conversation_id for workspace attachments
    has_workspace = any(a.type == "workspace" for a in attachments)
    if has_workspace and not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="conversation_id is required when using workspace attachments",
        )
    project_id = None
    if has_workspace:
        # conversation_id is trusted when injected by in-process route
        # dispatch, but these routes are also registered directly on the
        # main app and the sandbox app, where the value is client-supplied.
        # Without this check a user could attach files from another user's
        # workspace by passing that conversation id (security finding
        # #279216).
        meta = await require_owned_conversation(user["id"], conversation_id)
        project_id = meta.get("project_id") if isinstance(meta, dict) else None

    resolved_attachments = []
    for att in attachments:
        if att.type == "drive":
            if not att.drive_file_id:
                raise HTTPException(
                    status_code=400,
                    detail="drive_file_id is required for drive attachments",
                )
            resolved = await _resolve_drive_attachment(
                user, att.drive_file_id, att.filename
            )
        elif att.type == "workspace":
            if not att.workspace_path:
                raise HTTPException(
                    status_code=400,
                    detail="workspace_path is required for workspace attachments",
                )
            resolved = await _resolve_workspace_attachment(
                conversation_id,
                att.workspace_path,
                att.filename,
                project_id=project_id,
            )
        elif att.type == "gmail":
            if not att.message_id or not att.attachment_id:
                raise HTTPException(
                    status_code=400,
                    detail="message_id and attachment_id are required for gmail attachments",
                )
            resolved = await _resolve_gmail_attachment(
                user,
                att.message_id,
                att.attachment_id,
                att.filename or "attachment",
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown attachment type: {att.type}",
            )
        resolved_attachments.append(resolved)

    # Check total size
    total_size = sum(len(a["data"]) for a in resolved_attachments)
    if total_size > _MAX_TOTAL_ATTACHMENT_SIZE:
        total_mb = total_size / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"Total attachment size ({total_mb:.1f}MB) exceeds the 25MB limit.",
        )
    return resolved_attachments


def _add_attachments_to_message(
    mime_message: PythonEmailMessage,
    resolved_attachments: list,
) -> list:
    """Attach resolved files to ``mime_message`` and return their summaries.

    ``add_attachment`` auto-converts the message to multipart/mixed. Every
    attachment gets a Content-ID derived from its filename so the markdown
    body can embed it with ``![caption](cid:<token>)`` -- the only image
    source ``sanitize_email_html()`` lets through. Images are marked
    ``inline`` so clients render a referenced one in place.
    """
    attachment_summaries = []
    for att in resolved_attachments:
        maintype, subtype = att["mime_type"].split("/", 1)
        content_id = attachment_content_id(att["filename"])
        add_kwargs = {"cid": f"<{content_id}>"}
        if is_inline_image_mime(att["mime_type"]):
            add_kwargs["disposition"] = "inline"
        mime_message.add_attachment(
            att["data"],
            maintype=maintype,
            subtype=subtype,
            filename=att["filename"],
            **add_kwargs,
        )
        attachment_summaries.append({
            "filename": att["filename"],
            "content_id": content_id,
            "inline_image": is_inline_image_mime(att["mime_type"]),
        })
    return attachment_summaries


async def create_draft(
    draft_request: CreateDraftRequest,
    user: dict = Depends(get_current_user),
):
    """Create a draft email in the user's Gmail account. Does NOT send."""
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(status_code=401, detail={
            "error": "google_services_auth_required",
            "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
        })

    # --- 0. Validate body fields ---
    if draft_request.body is None and draft_request.body_md is None:
        raise HTTPException(
            status_code=400,
            detail="Either `body` (plain text) or `body_md` (markdown) is required.",
        )

    try:
        # --- 1. Resolve attachments (if any) ---
        resolved_attachments = await _resolve_request_attachments(
            user, draft_request.attachments, draft_request.conversation_id,
        )

        # --- 2. Build MIME message ---
        mime_message = PythonEmailMessage()

        if draft_request.body_md is not None:
            # Markdown body: render to HTML and build multipart/alternative.
            # When both body and body_md are provided, body is used as the
            # plain text part; otherwise the raw markdown is the plain text.
            html_body = convert_markdown_to_html(draft_request.body_md)
            plain_text = draft_request.body if draft_request.body is not None else draft_request.body_md
            mime_message.set_content(plain_text)
            mime_message.make_alternative()
            mime_message.add_alternative(html_body, subtype="html")
        else:
            # Plain text body only (existing behavior)
            mime_message.set_content(draft_request.body)

        mime_message["To"] = draft_request.to
        mime_message["Subject"] = draft_request.subject

        if draft_request.cc:
            mime_message["Cc"] = draft_request.cc
        if draft_request.bcc:
            mime_message["Bcc"] = draft_request.bcc
        if draft_request.in_reply_to_message_id:
            mime_message["In-Reply-To"] = draft_request.in_reply_to_message_id
        if draft_request.references:
            mime_message["References"] = draft_request.references

        # Add attachments (see _add_attachments_to_message for the cid: scheme).
        attachment_summaries = _add_attachments_to_message(
            mime_message, resolved_attachments,
        )

        # --- 3. Encode and create draft via Gmail API ---
        encoded_message = base64.urlsafe_b64encode(
            mime_message.as_bytes()
        ).decode("utf-8")

        draft_body = {"message": {"raw": encoded_message}}
        if draft_request.thread_id:
            draft_body["message"]["threadId"] = draft_request.thread_id

        service = get_gmail_service(credentials)
        draft = service.users().drafts().create(userId="me", body=draft_body).execute()

        return {
            "id": draft.get("id"),
            "message": {
                "id": draft.get("message", {}).get("id"),
                "threadId": draft.get("message", {}).get("threadId"),
                "labelIds": draft.get("message", {}).get("labelIds", []),
            },
            "attachments": attachment_summaries,
        }
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "403" in error_msg or "insufficient" in error_msg.lower():
            raise HTTPException(status_code=403, detail={
                "error": "insufficient_scope",
                "message": "Gmail compose permission not granted. Please re-authenticate at /auth/?force=1 to grant draft creation permission."
            })
        raise HTTPException(status_code=500, detail=str(e))


async def send_email_to_self(
    email_request: SendEmailToSelfRequest,
    user: dict = Depends(get_current_user),
):
    """Send an email to yourself. The subject is automatically prefixed with [Quest].

    The body_md field is provided as markdown and rendered to HTML for the email.
    The email is sent as multipart/alternative with both plain text
    (raw markdown) and HTML (rendered) parts. Optional attachments use the
    same shapes as drafts (workspace / drive / gmail) and each one gets a
    Content-ID so the body can embed an attached image via
    ``![caption](cid:<filename>)`` -- the only image source the sanitizer
    lets through, so remote images stay blocked.

    Uses the user's Google Services OAuth credentials to send an email
    from the user to themselves via the Gmail API. The email appears
    in the user's inbox immediately.

    Args:
        email_request: Request body with subject and body_md fields.
    """
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please connect Google Services via Settings > Data Connections.",
            },
        )

    user_email = user["email"]

    # Build the subject with [Quest] prefix
    subject = email_request.subject
    if not subject.startswith(_QUEST_SUBJECT_PREFIX):
        subject = f"{_QUEST_SUBJECT_PREFIX}{subject}"

    try:
        # Resolve attachments first so a bad path fails before anything is sent
        resolved_attachments = await _resolve_request_attachments(
            user, email_request.attachments, email_request.conversation_id,
        )

        # Render markdown body to HTML
        html_body = convert_markdown_to_html(email_request.body_md)

        # Build multipart/alternative MIME message (plain text + HTML)
        mime_message = PythonEmailMessage()
        mime_message["To"] = user_email
        mime_message["From"] = user_email
        mime_message["Subject"] = subject

        # set_content sets the plain text part, then make_alternative()
        # converts to multipart/alternative and add_alternative() adds HTML
        mime_message.set_content(email_request.body_md)
        mime_message.make_alternative()
        mime_message.add_alternative(html_body, subtype="html")

        attachment_summaries = _add_attachments_to_message(
            mime_message, resolved_attachments,
        )

        # Encode and send via Gmail API
        encoded_message = base64.urlsafe_b64encode(
            mime_message.as_bytes()
        ).decode("utf-8")

        service = get_gmail_service(credentials)
        sent_message = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": encoded_message})
            .execute()
        )

        return {
            "success": True,
            "message_id": sent_message.get("id", ""),
            "thread_id": sent_message.get("threadId", ""),
            "to": user_email,
            "subject": subject,
            "attachments": attachment_summaries,
        }
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "403" in error_msg or "insufficient" in error_msg.lower():
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "message": "Gmail compose permission not granted. Please re-connect Google Services via Settings > Data Connections.",
                },
            )
        raise HTTPException(status_code=500, detail=str(e))
