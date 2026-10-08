"""Microsoft 365 (Outlook Mail) dynamic tools.

Mirrors the Gmail Simple tool surface (see api/gmail/) over Microsoft
Graph, as ``m365_``-prefixed plugin tools:

- ``m365_get_mail_messages`` -- fetch 1-50 messages by Graph id, rendered
  as a markdown document (HTML bodies converted, long URLs replaced with
  ``(#N#)`` identifiers cached per conversation).
- ``m365_list_mail_folders`` -- list mail folders (Gmail labels analog).
- ``m365_get_mail_message_urls`` -- resolve ``(#N#)`` identifiers.
- ``m365_create_mail_draft`` -- create a draft (new / reply / forward)
  with workspace or forwarded-attachment support. Does NOT send.
- ``m365_send_mail_to_self`` -- immediately email the connected mailbox
  itself ([Quest]-prefixed subject, markdown body rendered to HTML).
- ``m365_archive_mail_message`` -- tag with the "Quest archived" category
  and move to the Archive folder.
- ``m365_save_mail_attachment`` -- download an attachment into the
  conversation workspace.

Searching/listing message ids is NOT a dedicated tool: the raw Graph API
is reachable read-only via ``authed_get`` (see the service entry in
manifest.py), matching how Gmail search rides on the Gmail Raw API.
"""

import asyncio
import base64
import json
import mimetypes
import os
import urllib.parse
from pathlib import Path

import httpx

from config.plugin_types import PluginTool

from plugins.m365.render import (
    render_batch_markdown,
    render_message_markdown,
    simplify_folder,
)
from plugins.m365.upstream import (
    GRAPH_API_BASE,
    GraphAuthError,
    get_user_m365_oauth,
    graph_request,
)

# Maximum total attachment size for a draft (matches the Gmail tools'
# 25MB practical limit; Exchange Online's default max message size).
_MAX_TOTAL_ATTACHMENT_SIZE = 25 * 1024 * 1024

# Graph rejects simple attachment POSTs above ~3MB; larger files go
# through an upload session in ~3.75MB chunks (must be a multiple of
# 320 KiB per the Graph upload-session contract).
_SIMPLE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
_UPLOAD_CHUNK_SIZE = 12 * 320 * 1024

# Cap for m365_save_mail_attachment workspace downloads.
_MAX_SAVED_ATTACHMENT_SIZE = 50 * 1024 * 1024

# Microsoft Graph allows at most 4 concurrent requests per mailbox; keep
# batch fetches under that so a 50-id batch doesn't trip 429 throttling.
_BATCH_CONCURRENCY = 4

_MAX_BATCH_IDS = 50

# Category applied by m365_archive_mail_message (the [Quest]/archived
# Gmail label analog) so the user can find Quest-archived mail in Outlook.
_QUEST_ARCHIVED_CATEGORY = "Quest archived"

# Subject prefix for self-emails (same convention as send_gmail_to_self).
_QUEST_SUBJECT_PREFIX = "[Quest] "

_MESSAGE_SELECT = ",".join([
    "id", "subject", "from", "toRecipients", "ccRecipients", "bccRecipients",
    "receivedDateTime", "sentDateTime", "internetMessageId", "conversationId",
    "categories", "isRead", "body", "hasAttachments", "webLink",
])
_ATTACHMENT_META_SELECT = "id,name,contentType,size,isInline"
_FOLDER_SELECT = ",".join([
    "id", "displayName", "parentFolderId", "childFolderCount",
    "unreadItemCount", "totalItemCount",
])

_WELL_KNOWN_FOLDERS = [
    "inbox", "archive", "drafts", "sentitems", "deleteditems", "junkemail",
    "outbox",
]


def _quote_id(value: str) -> str:
    """URL-encode a Graph resource id for safe use as a path segment."""
    return urllib.parse.quote(str(value), safe="")


def _as_bool(raw) -> bool:
    return raw is True or (isinstance(raw, str) and raw.lower() == "true")


def _error(message, **extra) -> str:
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload)


def _upstream_error(response: httpx.Response, context: str) -> str:
    """Format a non-2xx Graph response as a JSON error string."""
    try:
        body = response.json()
    except Exception:
        body = response.text[:500]
    return json.dumps({
        "error": {
            "status_code": response.status_code,
            "service": f"Microsoft Graph ({context})",
            "response": body,
        },
    })


def _classify_status(status_code: int, body_text: str) -> tuple[str, str]:
    """Map a non-2xx Graph HTTP status to (error_code, error_message)."""
    if status_code == 404:
        return "not_found", "Message not found"
    if status_code == 403:
        return "access_denied", "Access denied to this message"
    if status_code == 400:
        return "invalid_id", "Invalid message ID format"
    if status_code == 429:
        return "throttled", "Microsoft Graph throttled the request; retry shortly"
    return "fetch_error", body_text or f"Microsoft Graph error (HTTP {status_code})"


def _parse_address_list(raw: str | None) -> list[dict]:
    """Parse a comma-separated address string into Graph recipient objects."""
    if not raw:
        return []
    return [
        {"emailAddress": {"address": addr.strip()}}
        for addr in raw.split(",") if addr.strip()
    ]


def _build_body(body: str | None, body_md: str | None) -> dict:
    """Build the Graph ``itemBody`` from the plain/markdown body args.

    ``body_md`` wins (rendered to HTML); Graph bodies are single-part, so
    unlike the Gmail draft there is no separate plain-text alternative.
    """
    from api.gmail.helpers import convert_markdown_to_html

    if body_md is not None:
        return {"contentType": "HTML", "content": convert_markdown_to_html(body_md)}
    return {"contentType": "Text", "content": body or ""}


# ---------------------------------------------------------------------------
# m365_get_mail_messages
# ---------------------------------------------------------------------------

async def _fetch_message_markdown(
    user: dict,
    message_id: str,
    *,
    include_html: bool,
    include_urls: bool,
    conversation_id: str | None,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Fetch and render one message; never raises (returns an ok/error entry)."""
    async with semaphore:
        try:
            response = await graph_request(
                user, "GET",
                f"{GRAPH_API_BASE}/me/messages/{_quote_id(message_id)}",
                params={
                    "$select": _MESSAGE_SELECT,
                    "$expand": f"attachments($select={_ATTACHMENT_META_SELECT})",
                },
            )
        except GraphAuthError as exc:
            return {"id": message_id, "ok": False,
                    "error_code": "m365_oauth_required", "error_message": str(exc)}
        except httpx.HTTPError as exc:
            return {"id": message_id, "ok": False,
                    "error_code": "fetch_error", "error_message": str(exc)}

    if response.status_code >= 400:
        error_code, error_msg = _classify_status(response.status_code, response.text)
        return {"id": message_id, "ok": False,
                "error_code": error_code, "error_message": error_msg}

    markdown_doc = render_message_markdown(
        response.json(),
        include_html=include_html,
        replace_urls=not include_urls,
        conversation_id=conversation_id,
    )
    return {"id": message_id, "ok": True, "markdown": markdown_doc}


async def _handle_get_mail_messages(ctx, args: dict) -> str:
    message_ids = args.get("message_ids")
    if isinstance(message_ids, str):
        message_ids = [m.strip() for m in message_ids.split(",") if m.strip()]
    if not isinstance(message_ids, list) or not message_ids:
        return _error("message_ids is required: a list of 1-50 Graph message IDs.")
    if len(message_ids) > _MAX_BATCH_IDS:
        return _error(
            f"Maximum {_MAX_BATCH_IDS} message IDs allowed per call. "
            f"Received {len(message_ids)}; split them into multiple calls."
        )

    include_html = _as_bool(args.get("include_html"))
    include_urls = _as_bool(args.get("include_urls"))

    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)
    entries = await asyncio.gather(*[
        _fetch_message_markdown(
            ctx.user, str(mid),
            include_html=include_html,
            include_urls=include_urls,
            conversation_id=ctx.conversation_id,
            semaphore=semaphore,
        )
        for mid in message_ids
    ])

    if len(entries) == 1:
        entry = entries[0]
        if entry["ok"]:
            return entry["markdown"]
        return _error({
            "code": entry["error_code"],
            "message": entry["error_message"],
            "message_id": entry["id"],
        })
    return render_batch_markdown(list(entries))


# ---------------------------------------------------------------------------
# m365_list_mail_folders
# ---------------------------------------------------------------------------

async def _handle_list_mail_folders(ctx, args: dict) -> str:
    folder_id = args.get("folder_id")
    try:
        if folder_id:
            quoted = _quote_id(folder_id)
            folder_resp = await graph_request(
                ctx.user, "GET",
                f"{GRAPH_API_BASE}/me/mailFolders/{quoted}",
                params={"$select": _FOLDER_SELECT},
            )
            if folder_resp.status_code >= 400:
                return _upstream_error(folder_resp, f"mail folder '{folder_id}'")
            children_resp = await graph_request(
                ctx.user, "GET",
                f"{GRAPH_API_BASE}/me/mailFolders/{quoted}/childFolders",
                params={"$select": _FOLDER_SELECT, "$top": 100},
            )
            if children_resp.status_code >= 400:
                return _upstream_error(children_resp, "child folders")
            return json.dumps({
                "folder": simplify_folder(folder_resp.json()),
                "child_folders": [
                    simplify_folder(f)
                    for f in children_resp.json().get("value", [])
                ],
            })

        response = await graph_request(
            ctx.user, "GET",
            f"{GRAPH_API_BASE}/me/mailFolders",
            params={"$select": _FOLDER_SELECT, "$top": 100},
        )
    except GraphAuthError as exc:
        return _error(str(exc))
    if response.status_code >= 400:
        return _upstream_error(response, "mail folders")
    return json.dumps({
        "folders": [simplify_folder(f) for f in response.json().get("value", [])],
        "note": (
            "Top-level folders only; pass folder_id to list a folder's "
            "child folders. Well-known folder names usable anywhere a "
            f"folder id is accepted: {', '.join(_WELL_KNOWN_FOLDERS)}."
        ),
    })


# ---------------------------------------------------------------------------
# m365_get_mail_message_urls
# ---------------------------------------------------------------------------

async def _handle_get_mail_message_urls(ctx, args: dict) -> str:
    from api.gmail.helpers import _get_cached_url_mapping

    message_id = args.get("message_id")
    if not message_id:
        return _error("message_id is required.")
    if not ctx.conversation_id:
        return _error(
            "URL lookup requires conversation context, which this call "
            "has no access to."
        )

    mapping = _get_cached_url_mapping(ctx.conversation_id, str(message_id))
    if mapping is None:
        return _error(
            f"No URL mappings found for message {message_id}. Mappings "
            "exist only for messages fetched with m365_get_mail_messages "
            "in this conversation (with URL replacement enabled)."
        )

    identifiers = args.get("identifiers")
    if not identifiers:
        return json.dumps({
            "message_id": message_id,
            "urls": {str(k): v for k, v in mapping.items()},
        })

    urls: dict[str, str] = {}
    errors: dict[str, str] = {}
    for raw in identifiers:
        try:
            ident = int(raw)
        except (TypeError, ValueError):
            errors[str(raw)] = f"'{raw}' is not a valid numeric identifier"
            continue
        url = mapping.get(ident)
        if url is not None:
            urls[str(ident)] = url
        else:
            errors[str(ident)] = f"No URL mapping found for identifier {ident}"

    result: dict = {"message_id": message_id, "urls": urls}
    if errors:
        result["errors"] = errors
    return json.dumps(result)


# ---------------------------------------------------------------------------
# m365_create_mail_draft
# ---------------------------------------------------------------------------

class _DraftError(Exception):
    """Internal short-circuit for draft-creation failures."""


async def _resolve_workspace_attachment(ctx, workspace_path: str,
                                        filename_override: str | None) -> dict:
    """Read a conversation-workspace file for attachment (path-guarded)."""
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _sanitize_workspace_filename,
    )

    if not ctx.conversation_id:
        raise _DraftError(
            "Workspace attachments require a conversation workspace, "
            "which this call has no access to."
        )
    raw_path = (workspace_path or "").strip()
    if not raw_path:
        raise _DraftError("workspace_path is required for workspace attachments.")

    workspace_root = (
        await conversation_workspace_dir(ctx.conversation_id)
    ).resolve()

    candidate = Path(raw_path)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise _DraftError(
            f"Invalid workspace path {raw_path!r}: absolute paths and '..' "
            "traversal are not allowed."
        )
    file_path = (workspace_root / candidate).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        raise _DraftError(
            f"Invalid workspace path {raw_path!r}: resolved destination is "
            "outside the conversation workspace."
        )
    if not file_path.is_file():
        hint = (
            " (paths are in this conversation's workspace; project files "
            "must be copied into it first with copy_project_file)"
            if ctx.project_id else ""
        )
        raise _DraftError(f"Workspace file not found: {raw_path}{hint}")

    filename = None
    if filename_override:
        filename = _sanitize_workspace_filename(filename_override) or None
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return {
        "data": file_path.read_bytes(),
        "filename": filename or file_path.name,
        "mime_type": mime_type or "application/octet-stream",
    }


async def _resolve_outlook_attachment(user: dict, message_id: str,
                                      attachment_id: str,
                                      filename_override: str | None) -> dict:
    """Fetch a file attachment from an existing Outlook message."""
    response = await graph_request(
        user, "GET",
        f"{GRAPH_API_BASE}/me/messages/{_quote_id(message_id)}"
        f"/attachments/{_quote_id(attachment_id)}",
    )
    if response.status_code >= 400:
        raise _DraftError(
            f"Failed to fetch attachment '{attachment_id}' from message "
            f"'{message_id}' (HTTP {response.status_code}): {response.text[:300]}"
        )
    attachment = response.json()
    odata_type = attachment.get("@odata.type", "")
    if not odata_type.endswith("fileAttachment"):
        type_desc = odata_type or "not a file attachment"
        raise _DraftError(
            f"Attachment '{attachment_id}' is {type_desc}; only file "
            "attachments can be re-attached (itemAttachment / "
            "referenceAttachment are not supported)."
        )
    content_b64 = attachment.get("contentBytes")
    if not content_b64:
        raise _DraftError(f"Attachment '{attachment_id}' has no content.")
    return {
        "data": base64.b64decode(content_b64),
        "filename": filename_override or attachment.get("name") or "attachment",
        "mime_type": attachment.get("contentType") or "application/octet-stream",
    }


def _attachment_summary(att: dict) -> dict:
    """Per-attachment entry reported in the draft result.

    ``content_id`` is the token the markdown body can reference as
    ``![caption](cid:<token>)`` -- the only image source the shared
    ``sanitize_email_html()`` pass keeps (see api/gmail/helpers.py).
    """
    from api.gmail.helpers import attachment_content_id, is_inline_image_mime

    return {
        "filename": att["filename"],
        "content_id": attachment_content_id(att["filename"]),
        "inline_image": is_inline_image_mime(att["mime_type"]),
    }


async def _attach_file(user: dict, draft_id: str, att: dict) -> None:
    """Attach resolved bytes to a draft (simple POST or upload session).

    Every attachment carries a ``contentId`` (see ``_attachment_summary``)
    and images are flagged ``isInline`` so Outlook renders one that the
    HTML body references via ``cid:`` in place.
    """
    summary = _attachment_summary(att)
    data: bytes = att["data"]
    if len(data) <= _SIMPLE_ATTACHMENT_LIMIT:
        response = await graph_request(
            user, "POST",
            f"{GRAPH_API_BASE}/me/messages/{_quote_id(draft_id)}/attachments",
            json_body={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["filename"],
                "contentType": att["mime_type"],
                "contentBytes": base64.b64encode(data).decode("ascii"),
                "contentId": summary["content_id"],
                "isInline": summary["inline_image"],
            },
        )
        if response.status_code >= 400:
            raise _DraftError(
                f"Failed to attach '{att['filename']}' "
                f"(HTTP {response.status_code}): {response.text[:300]}"
            )
        return

    # Large attachment: Graph upload session, chunked PUTs against the
    # pre-authorized uploadUrl (no Authorization header -- the URL carries
    # its own auth, and leaking the user's token to the upload host would
    # be a security bug).
    session_resp = await graph_request(
        user, "POST",
        f"{GRAPH_API_BASE}/me/messages/{_quote_id(draft_id)}"
        "/attachments/createUploadSession",
        json_body={
            "AttachmentItem": {
                "attachmentType": "file",
                "name": att["filename"],
                "contentType": att["mime_type"],
                "size": len(data),
                "contentId": summary["content_id"],
                "isInline": summary["inline_image"],
            },
        },
    )
    if session_resp.status_code >= 400:
        raise _DraftError(
            f"Failed to start upload session for '{att['filename']}' "
            f"(HTTP {session_resp.status_code}): {session_resp.text[:300]}"
        )
    upload_url = session_resp.json().get("uploadUrl")
    if not upload_url:
        raise _DraftError("Upload session response had no uploadUrl.")

    total = len(data)
    async with httpx.AsyncClient(timeout=120.0) as client:
        for start in range(0, total, _UPLOAD_CHUNK_SIZE):
            chunk = data[start:start + _UPLOAD_CHUNK_SIZE]
            end = start + len(chunk) - 1
            put_resp = await client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Type": "application/octet-stream",
                },
            )
            if put_resp.status_code not in (200, 201, 202):
                raise _DraftError(
                    f"Chunk upload failed for '{att['filename']}' "
                    f"(HTTP {put_resp.status_code}): {put_resp.text[:300]}"
                )


async def _handle_create_mail_draft(ctx, args: dict) -> str:
    to = args.get("to")
    subject = args.get("subject")
    body = args.get("body")
    body_md = args.get("body_md")
    reply_to = args.get("reply_to_message_id")
    forward_of = args.get("forward_of_message_id")
    attachments = args.get("attachments") or []

    if body is None and body_md is None:
        return _error("Either `body` (plain text) or `body_md` (markdown) is required.")
    if reply_to and forward_of:
        return _error(
            "reply_to_message_id and forward_of_message_id are mutually "
            "exclusive."
        )
    if not reply_to and not forward_of and (not to or not subject):
        return _error("`to` and `subject` are required for a new (non-reply) draft.")
    if forward_of and not to:
        return _error("`to` is required when forwarding a message.")

    try:
        # --- 1. Resolve attachments up-front so validation fails fast ---
        resolved: list[dict] = []
        for att in attachments:
            if not isinstance(att, dict):
                raise _DraftError("Each attachment must be an object.")
            att_type = att.get("type")
            if att_type == "workspace":
                resolved.append(await _resolve_workspace_attachment(
                    ctx, att.get("workspace_path"), att.get("filename"),
                ))
            elif att_type == "outlook":
                if not att.get("message_id") or not att.get("attachment_id"):
                    raise _DraftError(
                        "message_id and attachment_id are required for "
                        "outlook attachments."
                    )
                resolved.append(await _resolve_outlook_attachment(
                    ctx.user, att["message_id"], att["attachment_id"],
                    att.get("filename"),
                ))
            else:
                raise _DraftError(
                    f"Unknown attachment type: {att_type!r} "
                    "(expected 'workspace' or 'outlook')."
                )

        total_size = sum(len(a["data"]) for a in resolved)
        if total_size > _MAX_TOTAL_ATTACHMENT_SIZE:
            total_mb = total_size / (1024 * 1024)
            raise _DraftError(
                f"Total attachment size ({total_mb:.1f}MB) exceeds the 25MB limit."
            )

        # --- 2. Create the draft ---
        item_body = _build_body(body, body_md)
        if reply_to or forward_of:
            origin_id = _quote_id(reply_to or forward_of)
            action = "createReply" if reply_to else "createForward"
            create_resp = await graph_request(
                ctx.user, "POST",
                f"{GRAPH_API_BASE}/me/messages/{origin_id}/{action}",
                json_body={},
            )
            if create_resp.status_code >= 400:
                return _upstream_error(create_resp, action)
            draft = create_resp.json()
            draft_id = draft.get("id")

            # Replace the auto-generated body with the composed one and
            # apply any explicit recipient/subject overrides. Exchange
            # keeps the reply/forward threading (conversation, headers)
            # regardless of the body content. NOTE: the composed body
            # replaces the auto-quoted original -- include quoted text
            # yourself if the reply should carry it.
            patch: dict = {"body": item_body}
            if to:
                patch["toRecipients"] = _parse_address_list(to)
            if args.get("cc"):
                patch["ccRecipients"] = _parse_address_list(args.get("cc"))
            if args.get("bcc"):
                patch["bccRecipients"] = _parse_address_list(args.get("bcc"))
            if subject:
                patch["subject"] = subject
            patch_resp = await graph_request(
                ctx.user, "PATCH",
                f"{GRAPH_API_BASE}/me/messages/{_quote_id(draft_id)}",
                json_body=patch,
            )
            if patch_resp.status_code >= 400:
                return _upstream_error(patch_resp, "draft update")
            draft = patch_resp.json()
        else:
            payload = {
                "subject": subject,
                "body": item_body,
                "toRecipients": _parse_address_list(to),
            }
            if args.get("cc"):
                payload["ccRecipients"] = _parse_address_list(args.get("cc"))
            if args.get("bcc"):
                payload["bccRecipients"] = _parse_address_list(args.get("bcc"))
            create_resp = await graph_request(
                ctx.user, "POST", f"{GRAPH_API_BASE}/me/messages",
                json_body=payload,
            )
            if create_resp.status_code >= 400:
                return _upstream_error(create_resp, "draft creation")
            draft = create_resp.json()
            draft_id = draft.get("id")

        # --- 3. Attach files ---
        for att in resolved:
            await _attach_file(ctx.user, draft_id, att)

        result = {
            "status": "success",
            "id": draft_id,
            "conversation_id": draft.get("conversationId"),
            "subject": draft.get("subject"),
            "attachments_added": len(resolved),
            "attachments": [_attachment_summary(att) for att in resolved],
            "message": "Draft created in the Drafts folder (NOT sent).",
        }
        if forward_of:
            result["note"] = (
                "createForward copied the original message's attachments "
                "into the draft automatically."
            )
        return json.dumps(result)

    except GraphAuthError as exc:
        return _error(str(exc))
    except _DraftError as exc:
        return _error(str(exc))
    except httpx.HTTPError as exc:
        return _error(f"Microsoft Graph request failed: {exc}")


# ---------------------------------------------------------------------------
# m365_send_mail_to_self
# ---------------------------------------------------------------------------

async def _handle_send_mail_to_self(ctx, args: dict) -> str:
    from api.gmail.helpers import convert_markdown_to_html

    subject = args.get("subject")
    body_md = args.get("body_md")
    if not subject or not body_md:
        return _error("Both `subject` and `body_md` are required.")

    if not subject.startswith(_QUEST_SUBJECT_PREFIX):
        subject = f"{_QUEST_SUBJECT_PREFIX}{subject}"

    # The connected mailbox address (captured at OAuth time) -- may differ
    # from the user's Quest (Google) login email.
    blob = get_user_m365_oauth(ctx.user) or {}
    to_address = (blob.get("account") or {}).get("email")

    try:
        if not to_address:
            me_resp = await graph_request(
                ctx.user, "GET", f"{GRAPH_API_BASE}/me",
                params={"$select": "mail,userPrincipalName"},
            )
            if me_resp.status_code >= 400:
                return _upstream_error(me_resp, "mailbox lookup")
            me = me_resp.json()
            to_address = me.get("mail") or me.get("userPrincipalName")
        if not to_address:
            return _error("Could not determine the connected mailbox address.")

        response = await graph_request(
            ctx.user, "POST", f"{GRAPH_API_BASE}/me/sendMail",
            json_body={
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML",
                        "content": convert_markdown_to_html(body_md),
                    },
                    "toRecipients": [{"emailAddress": {"address": to_address}}],
                },
                "saveToSentItems": True,
            },
        )
    except GraphAuthError as exc:
        return _error(str(exc))
    if response.status_code >= 400:
        return _upstream_error(response, "sendMail")

    return json.dumps({
        "success": True,
        "to": to_address,
        "subject": subject,
        "message": "Email sent to the connected mailbox.",
    })


# ---------------------------------------------------------------------------
# m365_archive_mail_message
# ---------------------------------------------------------------------------

async def _handle_archive_mail_message(ctx, args: dict) -> str:
    message_id = args.get("message_id")
    if not message_id:
        return _error("message_id is required.")
    quoted = _quote_id(message_id)

    try:
        # Tag with the Quest category first (the [Quest]/archived analog),
        # then move. Categories survive the move; the message ID does not.
        get_resp = await graph_request(
            ctx.user, "GET", f"{GRAPH_API_BASE}/me/messages/{quoted}",
            params={"$select": "categories"},
        )
        if get_resp.status_code >= 400:
            return _upstream_error(get_resp, f"message '{message_id}'")
        categories = get_resp.json().get("categories") or []
        if _QUEST_ARCHIVED_CATEGORY not in categories:
            patch_resp = await graph_request(
                ctx.user, "PATCH", f"{GRAPH_API_BASE}/me/messages/{quoted}",
                json_body={"categories": categories + [_QUEST_ARCHIVED_CATEGORY]},
            )
            if patch_resp.status_code >= 400:
                return _upstream_error(patch_resp, "category update")

        move_resp = await graph_request(
            ctx.user, "POST", f"{GRAPH_API_BASE}/me/messages/{quoted}/move",
            json_body={"destinationId": "archive"},
        )
    except GraphAuthError as exc:
        return _error(str(exc))
    if move_resp.status_code >= 400:
        return _upstream_error(move_resp, "archive move")

    moved = move_resp.json()
    return json.dumps({
        "success": True,
        "message_id": moved.get("id"),
        "previous_message_id": message_id,
        "moved_to": "archive",
        "category_applied": _QUEST_ARCHIVED_CATEGORY,
        "note": (
            "Moving a message changes its Graph message ID; use the new "
            "message_id for any follow-up calls."
        ),
    })


# ---------------------------------------------------------------------------
# m365_save_mail_attachment
# ---------------------------------------------------------------------------

def _resolve_save_destination(workspace_root: Path, path_arg: str | None,
                              default_filename: str) -> Path | str:
    """Resolve the workspace-relative destination for a saved attachment.

    Returns the resolved absolute Path, or an error message string.
    Mirrors github_get_job_log's ``path`` semantics: default directory
    ``outlook-attachments/``; a trailing slash (or an existing directory)
    means "put the default filename inside"; absolute paths and ``..``
    are rejected.
    """
    if not path_arg:
        rel_path = Path("outlook-attachments") / default_filename
    else:
        candidate = Path(path_arg)
        if candidate.is_absolute():
            return ("Invalid path: absolute paths are not allowed. "
                    "Provide a workspace-relative path.")
        if any(part == ".." for part in candidate.parts):
            return "Invalid path: parent-directory traversal ('..') is not allowed."
        treat_as_dir = path_arg.endswith("/") or path_arg.endswith(os.sep)
        if not treat_as_dir:
            probe = workspace_root / candidate
            if probe.exists() and probe.is_dir():
                treat_as_dir = True
        rel_path = candidate / default_filename if treat_as_dir else candidate

    file_path = (workspace_root / rel_path).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        return "Invalid path: resolved destination is outside the conversation workspace."
    return file_path


async def _handle_save_mail_attachment(ctx, args: dict) -> str:
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _publish_file_list_changed,
        _sanitize_workspace_filename,
    )

    message_id = args.get("message_id")
    attachment_id = args.get("attachment_id")
    if not message_id or not attachment_id:
        return _error("message_id and attachment_id are required.")
    if not ctx.conversation_id:
        return _error(
            "m365_save_mail_attachment requires a conversation workspace, "
            "which this call has no access to."
        )

    base = (
        f"{GRAPH_API_BASE}/me/messages/{_quote_id(message_id)}"
        f"/attachments/{_quote_id(attachment_id)}"
    )
    try:
        meta_resp = await graph_request(
            ctx.user, "GET", base,
            params={"$select": "name,contentType,size,isInline"},
        )
        if meta_resp.status_code >= 400:
            return _upstream_error(meta_resp, f"attachment '{attachment_id}'")
        meta = meta_resp.json()
        odata_type = meta.get("@odata.type", "")
        if not odata_type.endswith("fileAttachment"):
            return _error(
                f"Attachment '{attachment_id}' is {odata_type or 'unknown type'}; "
                "only file attachments can be saved (itemAttachment / "
                "referenceAttachment are not supported)."
            )
        size = meta.get("size") or 0
        if size > _MAX_SAVED_ATTACHMENT_SIZE:
            return _error(
                f"Attachment is {size:,} bytes, above the "
                f"{_MAX_SAVED_ATTACHMENT_SIZE // (1024 * 1024)}MB limit."
            )

        value_resp = await graph_request(ctx.user, "GET", f"{base}/$value")
    except GraphAuthError as exc:
        return _error(str(exc))
    if value_resp.status_code >= 400:
        return _upstream_error(value_resp, "attachment content")
    data = value_resp.content

    workspace_root = (
        await conversation_workspace_dir(ctx.conversation_id)
    ).resolve()
    default_filename = (
        _sanitize_workspace_filename(meta.get("name") or "") or "attachment.bin"
    )
    destination = _resolve_save_destination(
        workspace_root, args.get("path"), default_filename,
    )
    if isinstance(destination, str):
        return _error(destination)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except Exception as exc:
        return _error(f"Failed to save attachment to workspace: {exc}")

    _publish_file_list_changed(
        ctx.user["id"], "conversation", ctx.conversation_id, ctx.project_id,
    )

    rel_written = destination.relative_to(workspace_root).as_posix()
    return json.dumps({
        "status": "success",
        "path": rel_written,
        "filename": destination.name,
        "size_bytes": len(data),
        "content_type": meta.get("contentType") or "application/octet-stream",
        "message": (
            f"Attachment saved to the workspace as '{rel_written}' "
            f"({len(data):,} bytes). Use get_workspace_file to read it."
        ),
    })


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------

def _intent_param(example: str) -> dict:
    return {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent "
            f"(max 50 characters). Example: '{example}'."
        ),
    }


GET_MAIL_MESSAGES_TOOL = PluginTool(
    spec={
        "name": "m365_get_mail_messages",
        "description": (
            "Fetch 1-50 Outlook (Microsoft 365) messages by Graph message ID, "
            "rendered as a markdown document with decoded body and key "
            "headers. HTML bodies are converted to markdown; long URLs are "
            "replaced with short (#N#) identifiers (resolve them with "
            "m365_get_mail_message_urls) unless include_urls=true. "
            "To find message IDs, search the raw Graph API via authed_get "
            "on https://graph.microsoft.com/v1.0/me/messages. "
            "Requires Microsoft 365 to be connected in Settings > Data "
            "Connections."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Graph message IDs to fetch (max 50 per call).",
                },
                "include_html": {
                    "type": "boolean",
                    "description": (
                        "Emit the raw HTML body in a fenced block instead of "
                        "the markdown conversion (default false)."
                    ),
                },
                "include_urls": {
                    "type": "boolean",
                    "description": (
                        "Keep original full URLs inline instead of replacing "
                        "them with (#N#) identifiers (default false)."
                    ),
                },
                "intent_message": _intent_param("Read latest emails"),
            },
            "required": ["message_ids"],
        },
    },
    handler=_handle_get_mail_messages,
    requires_service="m365",
)

LIST_MAIL_FOLDERS_TOOL = PluginTool(
    spec={
        "name": "m365_list_mail_folders",
        "description": (
            "List Outlook mail folders (the Gmail-labels analog) in "
            "simplified form: id, name, unread/total counts. Without "
            "arguments returns the top-level folders; pass folder_id (a "
            "folder id or a well-known name like 'inbox', 'archive', "
            "'sentitems') to get that folder plus its child folders. "
            "Requires Microsoft 365 to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": (
                        "Optional folder id or well-known folder name to "
                        "fetch a single folder and its child folders."
                    ),
                },
                "intent_message": _intent_param("List mail folders"),
            },
            "required": [],
        },
    },
    handler=_handle_list_mail_folders,
    requires_service="m365",
)

GET_MAIL_MESSAGE_URLS_TOOL = PluginTool(
    spec={
        "name": "m365_get_mail_message_urls",
        "description": (
            "Look up the full URLs behind the numeric (#N#) identifiers "
            "substituted into Outlook message bodies by "
            "m365_get_mail_messages. Omit identifiers to get all mappings "
            "for the message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Graph message ID the identifiers came from.",
                },
                "identifiers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Numeric identifiers to resolve (e.g. [1, 2]).",
                },
                "intent_message": _intent_param("Look up email links"),
            },
            "required": ["message_id"],
        },
    },
    handler=_handle_get_mail_message_urls,
    requires_service="m365",
)

CREATE_MAIL_DRAFT_TOOL = PluginTool(
    spec={
        "name": "m365_create_mail_draft",
        "description": (
            "Create a draft email in the user's Outlook (Microsoft 365) "
            "Drafts folder. Does NOT send. Write the body as markdown in "
            "body_md (rendered to HTML) -- use the plain-text body "
            "parameter only when the user explicitly asks for a plain-text "
            "email. Supports reply/forward threading via "
            "reply_to_message_id / forward_of_message_id (pass the GRAPH "
            "message id, not the Message-ID header; Exchange preserves the "
            "conversation threading automatically, and forwards copy the "
            "original attachments), and attachments from this "
            "conversation's workspace or from other Outlook messages. In "
            "a project conversation, project files must be copied into "
            "this conversation's workspace first (`copy_project_file`). "
            "NOTE: for replies "
            "and forwards the composed body REPLACES the auto-quoted "
            "original -- include quoted text in the body yourself if "
            "wanted. Total attachment size limit 25MB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient email address(es), comma-separated. "
                        "Required for new drafts and forwards; optional on "
                        "replies (defaults to the original sender)."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "Subject line. Required for new drafts; optional on "
                        "replies/forwards (defaults to RE:/FW: + original)."
                    ),
                },
                "body_md": {
                    "type": "string",
                    "description": (
                        "Markdown body, rendered to HTML. Use this by "
                        "default, even for short or simple emails. Write "
                        "each paragraph as one unwrapped line and separate "
                        "paragraphs with a blank line -- do not hard-wrap "
                        "text at a fixed column, since every newline becomes "
                        "a line break in the email. Takes precedence over "
                        "`body`. Remote images (![](https://...)) and raw "
                        "HTML are stripped from the rendered email."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Plain text body. Only use INSTEAD of body_md when "
                        "the user explicitly asks for a plain-text email. "
                        "Required if body_md is not provided."
                    ),
                },
                "cc": {"type": "string", "description": "CC recipients, comma-separated."},
                "bcc": {"type": "string", "description": "BCC recipients, comma-separated."},
                "reply_to_message_id": {
                    "type": "string",
                    "description": (
                        "Graph message ID of the message being replied to "
                        "(from the 'Graph Message ID:' line, NOT the "
                        "Message-ID header)."
                    ),
                },
                "forward_of_message_id": {
                    "type": "string",
                    "description": (
                        "Graph message ID of the message being forwarded "
                        "(original attachments are copied automatically)."
                    ),
                },
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["workspace", "outlook"],
                                "description": (
                                    "'workspace' for files in this "
                                    "conversation's workspace, 'outlook' "
                                    "for attachments from an existing "
                                    "Outlook message."
                                ),
                            },
                            "workspace_path": {
                                "type": "string",
                                "description": (
                                    "Path relative to this conversation's "
                                    "workspace (required when "
                                    "type='workspace'). In a project "
                                    "conversation, project files must be "
                                    "copied into this conversation's "
                                    "workspace first (`copy_project_file`)."
                                ),
                            },
                            "message_id": {
                                "type": "string",
                                "description": (
                                    "Graph message ID containing the "
                                    "attachment (required when type='outlook')."
                                ),
                            },
                            "attachment_id": {
                                "type": "string",
                                "description": (
                                    "Attachment ID from the message's "
                                    "attachments list (required when "
                                    "type='outlook')."
                                ),
                            },
                            "filename": {
                                "type": "string",
                                "description": "Override the attachment filename.",
                            },
                        },
                        "required": ["type"],
                    },
                    "description": (
                        "Files to attach (25MB total limit). Each attachment "
                        "gets a Content-ID equal to its filename (non "
                        "[A-Za-z0-9._-] runs become '_'); embed an attached "
                        "image in body_md with ![caption](cid:<filename>)."
                    ),
                },
                "intent_message": _intent_param("Draft reply to Alice"),
            },
            "required": [],
        },
    },
    handler=_handle_create_mail_draft,
    requires_service="m365",
    mutating=True,
)

SEND_MAIL_TO_SELF_TOOL = PluginTool(
    spec={
        "name": "m365_send_mail_to_self",
        "description": (
            "Immediately send an email from the user's Microsoft 365 "
            "mailbox to itself (not a draft). Useful for delivering "
            "reports, summaries, or reminders to the user's inbox. The "
            "subject is auto-prefixed with [Quest]; body_md is markdown "
            "rendered to HTML. The recipient is always the connected "
            "mailbox itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Email subject (auto-prefixed with [Quest]).",
                },
                "body_md": {
                    "type": "string",
                    "description": (
                        "Markdown-formatted email body (rendered to HTML). "
                        "Remote images (![](https://...)) and raw HTML are "
                        "stripped from the rendered email -- link to images "
                        "instead."
                    ),
                },
                "intent_message": _intent_param("Email summary to user"),
            },
            "required": ["subject", "body_md"],
        },
    },
    handler=_handle_send_mail_to_self,
    requires_service="m365",
    mutating=True,
)

ARCHIVE_MAIL_MESSAGE_TOOL = PluginTool(
    spec={
        "name": "m365_archive_mail_message",
        "description": (
            "Archive an Outlook message: applies a 'Quest archived' "
            "category and moves the message to the Archive folder (not "
            "deleted). NOTE: moving changes the Graph message ID; the "
            "result carries the new id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Graph message ID to archive.",
                },
                "intent_message": _intent_param("Archive newsletter"),
            },
            "required": ["message_id"],
        },
    },
    handler=_handle_archive_mail_message,
    requires_service="m365",
    mutating=True,
)

SAVE_MAIL_ATTACHMENT_TOOL = PluginTool(
    spec={
        "name": "m365_save_mail_attachment",
        "description": (
            "Download a file attachment from an Outlook message into "
            "this conversation's workspace (default folder "
            "'outlook-attachments/'; override with 'path'). Get the "
            "attachment_id from the '## Attachments' section of "
            "m365_get_mail_messages. Read the saved file afterwards with "
            "get_workspace_file (which handles PDFs/images), or process "
            "it with run_python / run_script. 50MB limit; only file "
            "attachments are supported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Graph message ID containing the attachment.",
                },
                "attachment_id": {
                    "type": "string",
                    "description": "The attachment ID from the message's attachments list.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional destination relative to this "
                        "conversation's workspace. A trailing "
                        "'/' (or an existing directory) means 'put the "
                        "original filename inside this directory'; otherwise "
                        "the value is the full file path. Defaults to "
                        "'outlook-attachments/<original name>'."
                    ),
                },
                "intent_message": _intent_param("Save invoice PDF"),
            },
            "required": ["message_id", "attachment_id"],
        },
    },
    handler=_handle_save_mail_attachment,
    requires_service="m365",
)

ALL_TOOLS = (
    GET_MAIL_MESSAGES_TOOL,
    LIST_MAIL_FOLDERS_TOOL,
    GET_MAIL_MESSAGE_URLS_TOOL,
    CREATE_MAIL_DRAFT_TOOL,
    SEND_MAIL_TO_SELF_TOOL,
    ARCHIVE_MAIL_MESSAGE_TOOL,
    SAVE_MAIL_ATTACHMENT_TOOL,
)

# Read-only tools reachable from sandbox scripts via POST /api/tool-call.
# Script calls carry no conversation context, so message bodies keep full
# URLs and the URL-lookup tool reports missing context -- the same
# degradation as the Gmail Simple script path.
SCRIPT_TOOL_NAMES = frozenset({
    "m365_get_mail_messages",
    "m365_list_mail_folders",
})
