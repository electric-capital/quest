"""Simple (LLM-friendly) Gmail API endpoints -- read-only operations on
messages, labels, and URL lookups.
"""

import asyncio
from typing import Optional

import httpx
from fastapi import HTTPException, Depends
from fastapi.responses import PlainTextResponse

from auth.session import get_current_user
from auth.google_credentials import (
    get_valid_service_credentials,
    make_authenticated_request,
)

from chat.conversation_access import require_owned_conversation

from .constants import GMAIL_API_BASE
from .helpers import (
    render_batch_markdown,
    render_message_markdown,
    simplify_label,
    _get_cached_url_mapping,
)

# Per-request timeout for Gmail HTTP calls so a hung upstream call cannot pin a
# connection (the old googleapiclient had its own default timeout).
_GMAIL_HTTP_TIMEOUT = 30.0

# Cap concurrent in-flight Gmail requests in the batch endpoint so a 50-id batch
# issues at most this many concurrent calls (avoids hammering the API / hitting
# per-user rate limits).
_BATCH_CONCURRENCY = 10


def _classify_status(status_code: int, body_text: str) -> tuple[str, str]:
    """Map a non-2xx Gmail HTTP status to a (error_code, error_message) pair.

    Mirrors the legacy googleapiclient string-based classification, but keys
    off the real HTTP status code instead of a stringified exception.
    """
    if status_code == 404:
        return "not_found", "Message not found"
    if status_code == 403:
        return "access_denied", "Access denied to this message"
    if status_code == 400:
        return "invalid_id", "Invalid message ID format"
    return "fetch_error", body_text or f"Gmail API error (HTTP {status_code})"


async def get_message_simple(
    message_id: str,
    user: dict = Depends(get_current_user),
    include_html: bool = False,
    include_urls: bool = False,
    conversation_id: Optional[str] = None,
):
    """Get a specific message rendered as a markdown document.

    Args:
        message_id: The Gmail message ID
        include_html: If True, emit the raw HTML body in a fenced ``html`` block
            instead of the markdown conversion (default: False)
        include_urls: If True, keep original full URLs instead of replacing them
            with short numeric identifiers (default: False)
        conversation_id: Auto-injected by route dispatch; a client-supplied
            value must name a conversation the caller owns

    Returns:
        PlainTextResponse containing a markdown document that includes the
        subject as a ``#`` heading, a labelled header block (From/To/Cc/Bcc/
        Date/Message-ID/Gmail Message ID/Thread ID/Labels/Snippet, plus an
        optional Replaced URL count line), the body, and an attachments
        section listing each ``attachmentId``.
    """
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
            }
        )

    # ``conversation_id`` is scoped to the caller's own conversations,
    # like the URL-lookup routes below.
    if conversation_id is not None:
        await require_owned_conversation(user["id"], conversation_id)

    url = f"{GMAIL_API_BASE}/users/me/messages/{message_id}"
    async with httpx.AsyncClient(timeout=_GMAIL_HTTP_TIMEOUT) as client:
        response = await make_authenticated_request(
            client, user, "GET", url, params={"format": "full"},
        )
    if not response.is_success:
        # Propagate the real upstream status code (e.g. 404 instead of the
        # legacy blanket 500). This is the natural async shape and matches the
        # draft endpoints' _resolve_drive_attachment behavior.
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch Gmail message '{message_id}': {response.text}",
        )

    message = response.json()
    markdown_doc = render_message_markdown(
        message,
        include_html=include_html,
        replace_urls=not include_urls,
        conversation_id=conversation_id,
    )
    return PlainTextResponse(
        markdown_doc,
        media_type="text/markdown; charset=utf-8",
    )


async def get_messages_batch(
    ids: str,
    user: dict = Depends(get_current_user),
    include_html: bool = False,
    include_urls: bool = False,
    conversation_id: Optional[str] = None,
):
    """Fetch multiple messages rendered as a single markdown document.

    Args:
        ids: Comma-separated list of message IDs (max 50)
        include_html: If True, emit the raw HTML body in a fenced ``html`` block
            instead of the markdown conversion (default: False)
        include_urls: If True, keep original full URLs instead of replacing them
            with short numeric identifiers (default: False)
        conversation_id: Auto-injected by route dispatch; a client-supplied
            value must name a conversation the caller owns

    Returns:
        PlainTextResponse containing a markdown document with a top-level
        ``# Gmail messages batch`` heading, a counts line, an optional
        ``## Batch errors`` section (emitted first when any id failed), and
        ``===``-separated per-message sections rendered the same way as the
        single-message endpoint. Order is preserved from the request.
    """
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
            }
        )

    # ``conversation_id`` is scoped to the caller's own conversations,
    # like the URL-lookup routes below.
    if conversation_id is not None:
        await require_owned_conversation(user["id"], conversation_id)

    # Parse and validate IDs
    message_ids = [id.strip() for id in ids.split(',') if id.strip()]

    if not message_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "message": "No message IDs provided. Pass comma-separated IDs in the 'ids' parameter."
            }
        )

    if len(message_ids) > 50:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_ids",
                "message": f"Maximum 50 message IDs allowed. Received {len(message_ids)}."
            }
        )

    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def _fetch_one(client: httpx.AsyncClient, message_id: str) -> dict:
        """Fetch and render a single message; never raises -- returns an
        ok-entry or an error-entry so one bad id can't abort the batch."""
        async with semaphore:
            try:
                url = f"{GMAIL_API_BASE}/users/me/messages/{message_id}"
                response = await make_authenticated_request(
                    client, user, "GET", url, params={"format": "full"},
                )
            except HTTPException as e:
                # Auth failures from make_authenticated_request -> per-id error.
                return {
                    "id": message_id,
                    "ok": False,
                    "error_code": "fetch_error",
                    "error_message": str(e.detail),
                }
            except httpx.RequestError as e:
                # Transport errors / timeouts.
                return {
                    "id": message_id,
                    "ok": False,
                    "error_code": "fetch_error",
                    "error_message": str(e),
                }

            if not response.is_success:
                error_code, error_msg = _classify_status(
                    response.status_code, response.text
                )
                return {
                    "id": message_id,
                    "ok": False,
                    "error_code": error_code,
                    "error_message": error_msg,
                }

            markdown_doc = render_message_markdown(
                response.json(),
                include_html=include_html,
                replace_urls=not include_urls,
                conversation_id=conversation_id,
            )
            return {
                "id": message_id,
                "ok": True,
                "markdown": markdown_doc,
            }

    async with httpx.AsyncClient(timeout=_GMAIL_HTTP_TIMEOUT) as client:
        tasks = [_fetch_one(client, mid) for mid in message_ids]
        # gather preserves submission order, so entries stay in request order.
        results = await asyncio.gather(*tasks, return_exceptions=True)

    entries: list[dict] = []
    for message_id, result in zip(message_ids, results):
        if isinstance(result, BaseException):
            # Defensive: _fetch_one shouldn't raise, but never abort the batch.
            entries.append({
                "id": message_id,
                "ok": False,
                "error_code": "fetch_error",
                "error_message": str(result),
            })
        else:
            entries.append(result)

    batch_markdown = render_batch_markdown(entries)
    return PlainTextResponse(
        batch_markdown,
        media_type="text/markdown; charset=utf-8",
    )


async def list_labels_simple(user: dict = Depends(get_current_user)):
    """List all labels in LLM-friendly format."""
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
            }
        )

    url = f"{GMAIL_API_BASE}/users/me/labels"
    async with httpx.AsyncClient(timeout=_GMAIL_HTTP_TIMEOUT) as client:
        response = await make_authenticated_request(client, user, "GET", url)
    if not response.is_success:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to list Gmail labels: {response.text}",
        )

    labels = response.json().get('labels', [])
    return {
        'labels': [simplify_label(label) for label in labels]
    }


async def get_label_simple(
    label_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific label in LLM-friendly format."""
    credentials = await get_valid_service_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
            }
        )

    url = f"{GMAIL_API_BASE}/users/me/labels/{label_id}"
    async with httpx.AsyncClient(timeout=_GMAIL_HTTP_TIMEOUT) as client:
        response = await make_authenticated_request(client, user, "GET", url)
    if not response.is_success:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch Gmail label '{label_id}': {response.text}",
        )

    return simplify_label(response.json())


async def get_urls_by_identifiers(
    message_id: str,
    identifiers: str,
    user: dict = Depends(get_current_user),
    conversation_id: Optional[str] = None,
):
    """Look up full URLs by their numeric identifiers.

    Args:
        message_id: The Gmail message ID.
        identifiers: Comma-separated numeric identifiers (e.g., "1,2,3").
        conversation_id: Auto-injected by route dispatch.

    Returns:
        Object with 'urls' (dict mapping identifier to URL) and 'errors' (dict of identifiers not found).
    """
    if not conversation_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_context",
                "message": "URL lookup requires conversation context (auto-injected by system)."
            }
        )

    # Parse identifiers
    raw_ids = [s.strip() for s in identifiers.split(',') if s.strip()]
    if not raw_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "message": "No identifiers provided. Pass comma-separated numeric IDs."
            }
        )

    parsed_ids: list[int] = []
    for raw in raw_ids:
        try:
            parsed_ids.append(int(raw))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_identifier",
                    "message": f"'{raw}' is not a valid numeric identifier."
                }
            )

    # ``conversation_id`` is trusted when injected by in-process route
    # dispatch, but these routes are also registered on the main app and the
    # sandbox app where it is client-supplied (security finding #279217).
    await require_owned_conversation(user["id"], conversation_id)
    mapping = _get_cached_url_mapping(conversation_id, message_id)
    if mapping is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "mapping_not_found",
                "message": f"No URL mappings found for message {message_id}"
            }
        )

    urls = {}
    errors = {}
    for ident in parsed_ids:
        url = mapping.get(ident)
        if url is not None:
            urls[ident] = url
        else:
            errors[ident] = f"No URL mapping found for identifier {ident}"

    result = {"message_id": message_id, "urls": urls}
    if errors:
        result["errors"] = errors
    return result


async def get_urls_for_message(
    message_id: str,
    user: dict = Depends(get_current_user),
    conversation_id: Optional[str] = None,
):
    """Look up all URL mappings for a message.

    The conversation_id is auto-injected by route dispatch.
    """
    if not conversation_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_context",
                "message": "URL lookup requires conversation context (auto-injected by system)."
            }
        )
    # ``conversation_id`` is trusted when injected by in-process route
    # dispatch, but these routes are also registered on the main app and the
    # sandbox app where it is client-supplied (security finding #279217).
    await require_owned_conversation(user["id"], conversation_id)
    mapping = _get_cached_url_mapping(conversation_id, message_id)
    if mapping is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "mapping_not_found",
                "message": f"No URL mappings found for message {message_id}"
            }
        )
    return {"message_id": message_id, "urls": mapping}
