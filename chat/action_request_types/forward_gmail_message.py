"""ForwardGmailMessageHandler -- approve-to-send action request that forwards
an existing Gmail message at full fidelity.

The model issues a
``create_action_request(request_type="forward_gmail_message",
params={"message_id": "...", "to": "receipts@example.com", ...},
reasoning="...")`` call which appends an inline approval card to the chat.
On Approve the handler fetches the original message as raw RFC 822 bytes
(``messages.get?format=raw``), grafts a ``---------- Forwarded message
---------`` attribution block into its text and HTML body parts, keeps the
rest of the MIME tree byte-for-byte (inline ``cid:`` images, the HTML with
its inline styles, every attachment) and sends the result through the Gmail
``messages.send`` media-upload endpoint. The model never composes the body,
so a receipt or newsletter arrives looking exactly like the original. On
Revise / Stop nothing is sent.

The only model-authored text is the optional ``note`` (plain text, HTML
escaped into the HTML part). Everything else in the outgoing message is the
original sender's content, so the outgoing-HTML sanitizer is deliberately
not applied to the body: remote images in a forwarded receipt carry nothing
the model can see, and stripping them would defeat the point of a
high-fidelity forward.
"""

from __future__ import annotations

import base64
import copy
import html
import logging
import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses
from urllib.parse import quote

import httpx

from api.gmail.constants import GMAIL_API_BASE
from api.gmail.helpers import extract_attachments, extract_headers
from auth.google_credentials import make_authenticated_request
from chat.action_request_types._drive import (
    _extract_google_api_error,
    _get_authorized_google_scopes,
)
from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

# Model-suppliable keys. The ``original_*`` preview fields and the resolved
# ``subject`` default are server-injected by ``validate_against_upstream``
# (parallels ``calendar_name`` / ``folder_name`` on the other Google types).
_ALLOWED_PARAMS = frozenset({"message_id", "to", "cc", "bcc", "subject", "note"})

_RECIPIENT_FIELDS = ("to", "cc", "bcc")
_MAX_RECIPIENTS = 20
_MAX_NOTE_LEN = 4000
_MAX_SUBJECT_LEN = 500

_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Deliberately simple: one local part, one @, a dotted domain. Gmail does
# the real validation at send time; this catches the "forgot the domain"
# and "pasted a display name" shapes same-turn.
_EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")
_FWD_PREFIX_RE = re.compile(r"^\s*(fwd?|fw)\s*:", re.IGNORECASE)
_BODY_OPEN_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)

# Any one of these lets ``messages.send`` through. Quest asks for compose
# and modify (auth/config.py); older connections may predate that.
_SEND_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://mail.google.com/",
})

_GMAIL_SEND_URL = (
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
)
# Raw fetch and media-upload send both move whole messages (attachments
# included), so give them more room than the 30 s read endpoints.
_HTTP_TIMEOUT = 120.0

# Headers that belong to the ORIGINAL delivery and must not travel with the
# forward. Everything else that is not Content-* / MIME-Version is dropped
# too; this list only documents the notable ones for readers.
_KEEP_HEADER_PREFIXES = ("content-",)
_KEEP_HEADERS = frozenset({"mime-version"})

_ATTRIBUTION_TITLE = "---------- Forwarded message ---------"


def _gmail_reauth_message() -> str:
    return (
        "Gmail send access requires reconnecting Google Services via "
        "Settings > Data Connections."
    )


def _normalize_recipients(value, field: str) -> list[str]:
    """Accept a list of addresses or a comma-separated string; return a list.

    Display-name forms (``Name <addr>``) are reduced to the bare address so
    the card and the header show exactly what was validated.
    """
    if isinstance(value, str):
        candidates = [addr for _name, addr in getaddresses([value])]
    elif isinstance(value, list):
        candidates = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{field} entries must be strings")
            candidates.extend(addr for _name, addr in getaddresses([item]))
    else:
        raise ValueError(
            f"{field} must be an email address string or a list of them"
        )

    addresses: list[str] = []
    for addr in candidates:
        addr = addr.strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            raise ValueError(f"{field} contains an invalid email address: {addr!r}")
        if addr.lower() not in {a.lower() for a in addresses}:
            addresses.append(addr)
    if len(addresses) > _MAX_RECIPIENTS:
        raise ValueError(f"{field} may list at most {_MAX_RECIPIENTS} addresses")
    return addresses


def _format_size(size) -> str:
    try:
        n = int(size)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# MIME graft
# ---------------------------------------------------------------------------


def _attribution_lines(headers: dict) -> list[tuple[str, str]]:
    lines = [
        ("From", headers.get("from", "")),
        ("Date", headers.get("date", "")),
        ("Subject", headers.get("subject", "")),
        ("To", headers.get("to", "")),
    ]
    if headers.get("cc"):
        lines.append(("Cc", headers["cc"]))
    return lines


def _attribution_text(headers: dict, note: str | None) -> str:
    block = [_ATTRIBUTION_TITLE]
    block.extend(f"{label}: {value}" for label, value in _attribution_lines(headers))
    text = "\n".join(block) + "\n\n"
    if note:
        text = note.rstrip() + "\n\n" + text
    return text


def _attribution_html(headers: dict, note: str | None) -> str:
    rows = "<br>".join(
        f"{html.escape(label)}: {html.escape(value)}"
        for label, value in _attribution_lines(headers)
    )
    block = (
        '<div dir="ltr" class="gmail_attr">'
        f"{html.escape(_ATTRIBUTION_TITLE)}<br>{rows}<br></div><br><br>"
    )
    if note:
        escaped = html.escape(note.rstrip()).replace("\n", "<br>")
        block = f"<div>{escaped}</div><br>" + block
    return block


def _original_headers(message: EmailMessage) -> dict:
    """The From/Date/Subject/To/Cc strings quoted in the attribution block."""
    out = {}
    for name in ("from", "date", "subject", "to", "cc"):
        value = message.get(name)
        out[name] = str(value) if value is not None else ""
    return out


def _is_attachment(part: EmailMessage) -> bool:
    disposition = part.get_content_disposition()
    return disposition == "attachment" or bool(part.get_filename())


def _find_body_parts(message: EmailMessage) -> tuple[EmailMessage | None, EmailMessage | None]:
    """First non-attachment text/plain and text/html leaves, in tree order."""
    plain = html_part = None
    for part in message.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part
        elif ctype == "text/html" and html_part is None:
            html_part = part
        if plain is not None and html_part is not None:
            break
    return plain, html_part


def _prepend_to_part(part: EmailMessage, prefix: str, *, is_html: bool) -> bool:
    """Replace a text leaf's content with ``prefix`` + its decoded content.

    Returns False (and leaves the part untouched) when the original content
    cannot be decoded, e.g. an unknown charset; the forward then goes out
    with that part verbatim and the attribution only in its sibling.
    """
    try:
        content = part.get_content()
    except Exception as exc:  # LookupError (charset), UnicodeDecodeError, ...
        logger.warning("forward_gmail_message: leaving %s part verbatim: %s",
                       part.get_content_type(), exc)
        return False
    if not isinstance(content, str):
        return False

    if is_html:
        match = _BODY_OPEN_TAG_RE.search(content)
        if match:
            new_content = content[: match.end()] + prefix + content[match.end():]
        else:
            new_content = prefix + content
    else:
        new_content = prefix + content

    # set_content clears the Content-* headers and re-encodes; keep a
    # Content-ID / Content-Location if the leaf carried one (rare on body
    # text, but cheap to preserve). Quoted-printable keeps the output
    # 7-bit clean whatever the original's transfer encoding was.
    preserved = {
        name: part[name] for name in ("Content-ID", "Content-Location")
        if part.get(name) is not None
    }
    had_mime_version = part.get("MIME-Version") is not None
    part.set_content(
        new_content, subtype="html" if is_html else "plain",
        charset="utf-8", cte="quoted-printable",
    )
    if not had_mime_version:
        # set_content stamps MIME-Version on the leaf; it belongs on the
        # top-level message only.
        del part["MIME-Version"]
    for name, value in preserved.items():
        part[name] = value
    return True


def build_forward_message(
    raw: bytes,
    *,
    sender: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    note: str | None,
) -> EmailMessage:
    """Build the outgoing forward from the original's raw RFC 822 bytes.

    The original's MIME tree is kept as-is except for the first text/plain
    and text/html body leaves, which get the attribution block (and the
    optional note) prepended. Delivery headers of the original (Received,
    DKIM-Signature, Message-ID, List-*, the original From/To/...) are
    dropped; only Content-* and MIME-Version survive from the original's
    top-level header block.
    """
    original = message_from_bytes(raw, policy=policy.SMTP)
    headers = _original_headers(original)

    forward = copy.deepcopy(original)
    for name in list(forward.keys()):
        lowered = name.lower()
        if lowered.startswith(_KEEP_HEADER_PREFIXES) or lowered in _KEEP_HEADERS:
            continue
        del forward[name]

    plain, html_part = _find_body_parts(forward)
    if plain is not None:
        _prepend_to_part(plain, _attribution_text(headers, note), is_html=False)
    if html_part is not None:
        _prepend_to_part(html_part, _attribution_html(headers, note), is_html=True)
    if plain is None and html_part is None:
        # No text body at all (e.g. a bare attachment). Wrap the original
        # content under a new mixed container with the attribution as the
        # text part so the recipient still sees what this is.
        inner = forward
        forward = EmailMessage(policy=policy.SMTP)
        forward.set_content(_attribution_text(headers, note))
        forward.make_mixed()
        forward.attach(inner)

    forward["From"] = sender
    forward["To"] = ", ".join(to)
    if cc:
        forward["Cc"] = ", ".join(cc)
    if bcc:
        forward["Bcc"] = ", ".join(bcc)
    forward["Subject"] = subject
    if forward.get("MIME-Version") is None:
        forward["MIME-Version"] = "1.0"
    return forward


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class ForwardGmailMessageHandler(ActionRequestHandler):
    """Forward an existing Gmail message, unchanged, after explicit approval.

    Params:
        message_id (str): Gmail message id (from ``get_gmail_messages`` or a
            raw ``messages`` search). Required.
        to (str | list[str]): Recipient address(es). Required.
        cc, bcc (str | list[str], optional): Extra recipients.
        subject (str, optional): Defaults to ``Fwd: <original subject>``.
        note (str, optional): Plain-text note shown above the forwarded
            message (max ``_MAX_NOTE_LEN`` chars).
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.FORWARD_GMAIL_MESSAGE

    @property
    def display_name(self) -> str:
        return "Forward Gmail Message"

    @property
    def approve_label(self) -> str:
        return "Forward"

    @property
    def resolved_label(self) -> str:
        return "Forwarded"

    def summary_snippet(self, params: dict) -> str:
        to = params.get("to") or []
        if isinstance(to, str):
            to = [to]
        target = ", ".join(str(a) for a in to)
        subject = params.get("original_subject") or params.get("subject") or ""
        if subject and target:
            return f"{subject} → {target}"
        return subject or target

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields = [{"key": "To", "value": ", ".join(params.get("to") or [])}]
        if params.get("cc"):
            fields.append({"key": "Cc", "value": ", ".join(params["cc"])})
        if params.get("bcc"):
            fields.append({"key": "Bcc", "value": ", ".join(params["bcc"])})
        fields.append({"key": "Subject", "value": str(params.get("subject") or "")})

        original_bits = []
        if params.get("original_from"):
            original_bits.append(f"from {params['original_from']}")
        if params.get("original_date"):
            original_bits.append(f"on {params['original_date']}")
        original = str(params.get("original_subject") or params.get("message_id") or "")
        if original_bits:
            original = f"{original} ({' '.join(original_bits)})"
        fields.append({"key": "Forwarding", "value": original})

        attachments = params.get("original_attachments") or []
        if attachments:
            names = []
            for att in attachments:
                label = str(att.get("filename") or "attachment")
                size = _format_size(att.get("size"))
                names.append(f"{label} ({size})" if size else label)
            fields.append({"key": "Attachments", "value": ", ".join(names)})
        else:
            fields.append({"key": "Attachments", "value": "none"})

        if params.get("note"):
            fields.append({"key": "Note", "value": str(params["note"])})
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        message_id = params.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("Missing required parameter: message_id")
        message_id = message_id.strip()
        if not _MESSAGE_ID_RE.match(message_id):
            raise ValueError(
                f"message_id must be a Gmail message id (got {message_id!r})"
            )

        validated: dict = {"message_id": message_id}

        if params.get("to") is None:
            raise ValueError("Missing required parameter: to")
        for field in _RECIPIENT_FIELDS:
            if params.get(field) is None:
                continue
            addresses = _normalize_recipients(params[field], field)
            if field == "to" and not addresses:
                raise ValueError("to must contain at least one email address")
            if addresses:
                validated[field] = addresses

        subject = params.get("subject")
        if subject is not None:
            if not isinstance(subject, str):
                raise ValueError("subject must be a string if provided")
            subject = " ".join(subject.split())
            if not subject:
                raise ValueError("subject must be a non-empty string if provided")
            if len(subject) > _MAX_SUBJECT_LEN:
                raise ValueError(f"subject must be at most {_MAX_SUBJECT_LEN} characters")
            validated["subject"] = subject

        note = params.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("note must be a string if provided")
            note = note.strip()
            if len(note) > _MAX_NOTE_LEN:
                raise ValueError(f"note must be at most {_MAX_NOTE_LEN} characters")
            if note:
                validated["note"] = note

        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Read the original's headers and attachment list for the card.

        A missing message (404) or a missing Google connection is a
        same-turn rejection. Other upstream failures are logged and the
        card renders with the message id in place of the subject.
        """
        google_services_oauth = user.get("google_services_oauth") or {}
        if not google_services_oauth.get("access_token"):
            raise ValueError(
                "Google Services not connected. Please connect Google Services "
                "via Settings > Data Connections."
            )

        url = f"{GMAIL_API_BASE}/users/me/messages/{quote(params['message_id'], safe='')}"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await make_authenticated_request(
                    client, user, "GET", url,
                    params={"format": "full"},
                )
        except ValueError:
            raise
        except Exception as exc:
            logger.warning(
                "forward_gmail_message: could not read message %s for the card: %s",
                params["message_id"], exc,
            )
            return self._with_default_subject(params)

        if response.status_code == 404:
            raise ValueError(
                f"Gmail message {params['message_id']!r} was not found. Use the "
                "`Gmail Message ID:` line from get_gmail_messages."
            )
        if not response.is_success:
            logger.warning(
                "forward_gmail_message: Gmail returned %s reading %s for the card: %s",
                response.status_code, params["message_id"],
                _extract_google_api_error(response),
            )
            return self._with_default_subject(params)

        message = response.json()
        payload = message.get("payload") or {}
        headers = extract_headers(
            payload.get("headers") or [], ["from", "date", "subject", "to"],
        )
        params["original_subject"] = headers.get("subject", "")
        params["original_from"] = headers.get("from", "")
        params["original_date"] = headers.get("date", "")
        params["original_to"] = headers.get("to", "")
        params["original_attachments"] = [
            {"filename": att["filename"], "mime_type": att["mimeType"], "size": att["size"]}
            for att in extract_attachments(payload)
        ]
        return self._with_default_subject(params)

    @staticmethod
    def _with_default_subject(params: dict) -> dict:
        if not params.get("subject"):
            original = params.get("original_subject") or ""
            if _FWD_PREFIX_RE.match(original):
                params["subject"] = original.strip()
            else:
                params["subject"] = f"Fwd: {original}".strip()
        return params

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        google_services_oauth = user.get("google_services_oauth") or {}
        if not google_services_oauth.get("access_token"):
            raise RuntimeError(
                "Google Services not connected. Please connect Google Services via "
                "Settings > Data Connections."
            )
        if not (_SEND_SCOPES & _get_authorized_google_scopes(user)):
            raise RuntimeError(_gmail_reauth_message())

        sender = user.get("email")
        if not sender:
            raise RuntimeError("The signed-in user has no email address to send from.")

        message_id = params["message_id"]
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            get_url = f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}"
            response = await make_authenticated_request(
                client, user, "GET", get_url, params={"format": "raw"},
            )
            if response.status_code == 404:
                raise RuntimeError(
                    f"Gmail message {message_id!r} no longer exists (it may have "
                    "been deleted since the request was proposed)."
                )
            if response.status_code == 403:
                raise RuntimeError(_gmail_reauth_message())
            if not response.is_success:
                raise RuntimeError(
                    f"Failed to read Gmail message {message_id!r}: "
                    f"{_extract_google_api_error(response)}"
                )
            raw_b64 = (response.json() or {}).get("raw")
            if not raw_b64:
                raise RuntimeError(
                    f"Gmail returned no raw content for message {message_id!r}."
                )
            raw = base64.urlsafe_b64decode(raw_b64)

            forward = build_forward_message(
                raw,
                sender=sender,
                to=list(params["to"]),
                cc=list(params.get("cc") or []),
                bcc=list(params.get("bcc") or []),
                subject=params["subject"],
                note=params.get("note"),
            )

            send_response = await make_authenticated_request(
                client, user, "POST", _GMAIL_SEND_URL,
                params={"uploadType": "media"},
                content=forward.as_bytes(),
                headers={"Content-Type": "message/rfc822"},
            )
            if send_response.status_code == 403:
                raise RuntimeError(_gmail_reauth_message())
            if not send_response.is_success:
                raise RuntimeError(
                    f"Gmail refused to send the forward: "
                    f"{_extract_google_api_error(send_response)}"
                )
            sent = send_response.json() or {}

        attachments = [
            att.get("filename") for att in params.get("original_attachments") or []
        ]
        return {
            "success": True,
            "message_id": sent.get("id", ""),
            "thread_id": sent.get("threadId", ""),
            "forwarded_message_id": message_id,
            "to": list(params["to"]),
            "cc": list(params.get("cc") or []),
            "bcc": list(params.get("bcc") or []),
            "subject": params["subject"],
            "attachments": attachments,
        }
