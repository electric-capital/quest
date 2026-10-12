"""Markdown rendering for IMAP messages (issue #84 phase 3, spec section 10.4).

Parses the raw RFC 5322 bytes fetched with ``BODY.PEEK[]`` using the stdlib
``email`` package (``policy=email.policy.default``) and renders them in the
same LLM-facing shape as the Gmail Simple and Microsoft 365 renderers
(``api/gmail/helpers.py``, ``plugins/m365/render.py``): a labelled header
block, ``---``, ``## Body``, ``---``, ``## Attachments``.

Decisions worth knowing:

* Body choice: the first non-attachment ``text/plain`` part wins; when there
  is none (or it is blank) the first non-attachment ``text/html`` part is
  converted through the shared Gmail pipeline (``convert_html_to_markdown``
  drops ``<script>``/``<style>`` contents and turns markup into markdown text,
  so no raw HTML reaches the model; remote images become inert ``![alt](url)``
  text that nothing fetches). ``include_html=True`` shows the raw HTML part in
  a fenced block instead, like the other renderers.
* URL replacement runs for HTML-derived AND plain-text bodies (unlike Gmail /
  M365, which only replace URLs in HTML bodies): spec 10.4 asks for it and
  plain-text newsletters carry the same long tracking URLs. The mapping is
  cached under ``url_cache_key(account, folder, uidvalidity, uid)`` and also
  under ``latest_url_cache_key(account, folder, uid)`` so a lookup without a
  UIDVALIDITY can find the most recent rendering.
* Every body section is capped at ``MAX_BODY_BYTES`` of decoded payload
  BEFORE charset decoding / HTML conversion, so a huge body cannot blow up
  conversion time; a truncation note says how much was cut.
* Hostile or broken MIME never raises: unknown charsets fall back to UTF-8
  with replacement, unknown transfer encodings yield the raw payload, the
  part walk is iterative and bounded (``MAX_PARTS`` parts, ``MAX_DEPTH``
  nesting levels), and the HTML converter falls back to a flat text
  extraction on deep markup that would exhaust recursion.
* Attachments are enumerated by ``list_attachments`` in walk order with a
  1-based ``index``; the save tool MUST use the same function so
  ``attachment_index`` means the same part. An attached ``message/*`` part
  (e.g. a forwarded ``message/rfc822``) is one attachment and is not
  descended into -- its bytes are the attached message as an ``.eml``.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses
from html.parser import HTMLParser

from api.gmail.helpers import (
    _cache_url_mapping,
    _fence_for_html,
    _sanitize_header_value,
    clean_email_markdown,
    convert_html_to_markdown,
    replace_urls_with_identifiers,
)

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PARTS = 500
MAX_DEPTH = 64


@dataclass
class AttachmentInfo:
    """One attachment of a message, as listed to the model and saved by the tools."""

    index: int
    filename: str | None
    mime: str
    size: int
    disposition: str | None
    part: Message


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------


def _clean_text(value) -> str:
    """Coerce to ``str`` and make it safely UTF-8 encodable.

    Header values parsed from non-ASCII bytes can carry surrogate escapes,
    which would raise when the result is later encoded.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def _header_text(value) -> str:
    """Decoded, single-line, safe header text."""
    text = _clean_text(value)
    if "=?" in text:
        try:
            text = _clean_text(str(make_header(decode_header(text))))
        except Exception:
            pass
    return _sanitize_header_value(text).strip()


def _get_header(msg: Message, name: str) -> str:
    """Header value decoded per RFC 2047; never raises on a malformed header."""
    try:
        value = msg.get(name)
        if value is None:
            return ""
        return _header_text(value)
    except Exception:
        pass
    # Fall back to the unparsed raw value.
    try:
        for key, raw in msg.raw_items():
            if key.lower() == name.lower():
                return _header_text(raw)
    except Exception:
        pass
    return ""


def url_cache_key(account: str, folder: str, uidvalidity, uid) -> str:
    """Per-conversation URL-cache key of one message identity."""
    return f"imap:{account}:{folder}:{uidvalidity}:{uid}"


def latest_url_cache_key(account: str, folder: str, uid) -> str:
    """Secondary key holding the most recently cached mapping for (account, folder, uid).

    ``latest`` can never collide with an integer UIDVALIDITY.
    """
    return f"imap:{account}:{folder}:latest:{uid}"


def format_address_list(value) -> str:
    """Format addresses as ``Name <addr>, addr2``.

    Accepts a header string, a list of ``(name, address)`` tuples, or a list
    of dicts with ``name`` and ``address`` / ``email`` (or ``mailbox`` +
    ``host``) keys. Never raises; returns ``""`` for nothing usable.
    """
    try:
        pairs: list[tuple[str, str]] = []
        if value is None:
            return ""
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        if isinstance(value, str):
            text = _header_text(value)
            pairs = getaddresses([text]) if text else []
            if not any(a or n for n, a in pairs):
                return text
        else:
            for item in value:
                if isinstance(item, dict):
                    address = item.get("address") or item.get("email") or ""
                    if not address and item.get("mailbox"):
                        address = item["mailbox"] + (
                            f"@{item['host']}" if item.get("host") else ""
                        )
                    pairs.append((item.get("name") or "", address))
                elif isinstance(item, (tuple, list)) and len(item) >= 2:
                    pairs.append((item[0] or "", item[1] or ""))
                elif item:
                    pairs.append(("", str(item)))
        out = []
        for name, address in pairs:
            name = _header_text(name)
            address = _header_text(address)
            if name and address and name != address:
                out.append(f"{name} <{address}>")
            elif address or name:
                out.append(address or name)
        return ", ".join(out)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# MIME walking
# --------------------------------------------------------------------------


def parse_message(raw: bytes) -> EmailMessage:
    """Parse raw message bytes; never raises (returns an empty message instead)."""
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        return email.message_from_bytes(raw or b"", policy=email.policy.default)
    except Exception:
        return EmailMessage(policy=email.policy.default)


def _content_type(part: Message) -> str:
    try:
        return (part.get_content_type() or "application/octet-stream").lower()
    except Exception:
        return "application/octet-stream"


def _disposition(part: Message) -> str | None:
    try:
        value = part.get_content_disposition()
    except Exception:
        return None
    return value.lower() if value else None


def _filename(part: Message) -> str | None:
    """Decoded filename (RFC 2231 and, non-standard but common, RFC 2047)."""
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return None
    name = _header_text(name)
    return name or None


def _is_message_container(part: Message) -> bool:
    return _content_type(part).startswith("message/") and _safe_is_multipart(part)


def _safe_is_multipart(part: Message) -> bool:
    try:
        return part.is_multipart()
    except Exception:
        return False


def _walk_leaves(msg: Message) -> list[Message]:
    """Leaf parts in ``msg.walk()`` order, iterative and bounded.

    ``message/*`` containers (attached emails, delivery reports) count as
    leaves and are not descended into, except when the top-level message
    itself is passed in.
    """
    leaves: list[Message] = []
    stack: list[tuple[Message, int]] = [(msg, 0)]
    visited = 0
    while stack and visited < MAX_PARTS:
        part, depth = stack.pop()
        visited += 1
        if part is not msg and _is_message_container(part):
            leaves.append(part)
            continue
        if _safe_is_multipart(part):
            if depth >= MAX_DEPTH:
                continue
            try:
                children = part.get_payload()
            except Exception:
                children = []
            if not isinstance(children, list):
                continue
            for child in reversed(children):
                if isinstance(child, Message):
                    stack.append((child, depth + 1))
            continue
        leaves.append(part)
    return leaves


def _is_body_candidate(part: Message, mime: str) -> bool:
    return (
        mime in ("text/plain", "text/html")
        and _disposition(part) != "attachment"
        and not _filename(part)
    )


def _part_bytes(part: Message) -> bytes:
    """Decoded payload of a leaf (or the attached message for ``message/*``)."""
    try:
        if _is_message_container(part):
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                chunks = []
                for item in inner:
                    try:
                        chunks.append(item.as_bytes(policy=email.policy.compat32))
                    except Exception:
                        chunks.append(bytes(item))
                return b"\n".join(chunks)
            return b""
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            if isinstance(raw, str):
                return raw.encode("utf-8", "surrogateescape")
            return b""
        return bytes(payload)
    except Exception:
        return b""


def attachment_payload(info: AttachmentInfo) -> bytes:
    """Decoded bytes of an attachment (``.eml`` bytes for an attached message)."""
    return _part_bytes(info.part)


def _choose_body_parts(msg: Message) -> tuple[Message | None, Message | None]:
    """(first plain candidate, first html candidate)."""
    plain = html = None
    for part in _walk_leaves(msg):
        mime = _content_type(part)
        if not _is_body_candidate(part, mime):
            continue
        if mime == "text/plain" and plain is None:
            plain = part
        elif mime == "text/html" and html is None:
            html = part
        if plain is not None and html is not None:
            break
    return plain, html


def list_attachments(msg: Message) -> list[AttachmentInfo]:
    """Deterministic attachment enumeration (1-based ``index``).

    Every leaf that is not a body candidate (``text/plain`` / ``text/html``
    without attachment disposition or filename): attachment disposition,
    a filename, or a non-text type. Inline images count. Unchosen body
    alternatives (e.g. the HTML twin of a plain body) are not attachments.
    The save tool must use this same function to resolve ``attachment_index``.
    """
    result: list[AttachmentInfo] = []
    for part in _walk_leaves(msg):
        mime = _content_type(part)
        if _is_body_candidate(part, mime):
            continue
        if mime.startswith("text/") and _disposition(part) != "attachment" and not _filename(part):
            continue
        result.append(
            AttachmentInfo(
                index=len(result) + 1,
                filename=_filename(part),
                mime=mime,
                size=len(_part_bytes(part)),
                disposition=_disposition(part),
                part=part,
            )
        )
    return result


# --------------------------------------------------------------------------
# Body decoding
# --------------------------------------------------------------------------


def _decode_text(part: Message) -> tuple[str, int]:
    """Decode a text part, capped at ``MAX_BODY_BYTES``; returns (text, full byte size)."""
    data = _part_bytes(part)
    total = len(data)
    if total > MAX_BODY_BYTES:
        data = data[:MAX_BODY_BYTES]
    try:
        charset = part.get_content_charset() or "utf-8"
    except Exception:
        charset = "utf-8"
    try:
        text = data.decode(charset, "replace")
    except Exception:
        text = data.decode("utf-8", "replace")
    text = _clean_text(text).replace("\x00", "")
    return text.replace("\r\n", "\n").replace("\r", "\n"), total


class _TextExtractor(HTMLParser):
    """Flat text extraction used when the markdown converter gives up."""

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_markdown(html: str) -> str:
    try:
        return convert_html_to_markdown(html)
    except Exception:  # incl. RecursionError on deeply nested markup
        try:
            extractor = _TextExtractor()
            extractor.feed(html)
            extractor.close()
            return "".join(extractor.parts)
        except Exception:
            return ""


def _truncation_note(total: int) -> str:
    return (
        f"\n\n_[Body truncated: showing the first {MAX_BODY_BYTES:,} of "
        f"{total:,} bytes.]_"
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _cache_mapping(conversation_id, keys, mapping) -> bool:
    try:
        for key in keys:
            _cache_url_mapping(conversation_id, key, mapping)
        return True
    except Exception:
        return False


def render_message_markdown(
    raw: bytes,
    *,
    folder: str,
    uid: int,
    uidvalidity: int,
    account: str,
    flags: list[str] | None = None,
    replace_urls: bool = True,
    conversation_id: str | None = None,
    cache_key: str | None = None,
    include_html: bool = False,
) -> str:
    """Render one fetched message (raw ``BODY.PEEK[]`` bytes) as markdown.

    URL replacement runs only when ``replace_urls`` and ``conversation_id``
    are both given; the mapping is stored under ``cache_key`` (default
    ``url_cache_key(...)``) and the ``latest_url_cache_key(...)`` pointer.
    If caching fails the URLs are left as-is. Never raises on hostile MIME.
    """
    msg = parse_message(raw)

    # --- Body ------------------------------------------------------------
    replaced_url_count: int | None = None
    plain_part, html_part = _choose_body_parts(msg)
    body_section = "## Body\n\n_(empty body)_"
    try:
        if include_html and html_part is not None:
            html, total = _decode_text(html_part)
            note = _truncation_note(total) if total > MAX_BODY_BYTES else ""
            fence = _fence_for_html(html)
            body_section = f"## Body (HTML)\n\n{fence}html\n{html}\n{fence}{note}"
        else:
            body_text, total = ("", 0)
            if plain_part is not None:
                body_text, total = _decode_text(plain_part)
            if not body_text.strip() and html_part is not None:
                html, total = _decode_text(html_part)
                body_text = clean_email_markdown(_html_to_markdown(html)) or ""
            if body_text.strip():
                if replace_urls and conversation_id:
                    replaced, mapping = replace_urls_with_identifiers(body_text)
                    if mapping:
                        keys = [
                            cache_key or url_cache_key(account, folder, uidvalidity, uid),
                            latest_url_cache_key(account, folder, uid),
                        ]
                        if _cache_mapping(conversation_id, keys, mapping):
                            body_text = replaced
                            replaced_url_count = len(mapping)
                note = _truncation_note(total) if total > MAX_BODY_BYTES else ""
                # Plain bodies are NOT fenced: they can contain backticks.
                body_section = f"## Body\n\n{body_text}{note}"
    except Exception:
        body_section = "## Body\n\n_(body could not be decoded)_"

    # --- Header block ----------------------------------------------------
    subject = _get_header(msg, "Subject")
    header_lines = [f"Subject: {subject or '(no subject)'}"]
    header_lines.append(f"From: {_get_header(msg, 'From') or '(unknown)'}")
    header_lines.append(f"To: {_get_header(msg, 'To') or '(unknown)'}")
    cc = _get_header(msg, "Cc")
    if cc:
        header_lines.append(f"Cc: {cc}")
    bcc = _get_header(msg, "Bcc")
    if bcc:
        header_lines.append(f"Bcc: {bcc}")
    reply_to = _get_header(msg, "Reply-To")
    if reply_to:
        header_lines.append(f"Reply-To: {reply_to}")
    header_lines.append(f"Date: {_get_header(msg, 'Date') or '(unknown)'}")
    header_lines.append(f"Message-ID: {_get_header(msg, 'Message-ID') or '(none)'}")
    header_lines.append(f"Account: {_header_text(account)}")
    header_lines.append(f"Folder: {_header_text(folder)}")
    header_lines.append(f"UID: {_header_text(uid)}")
    header_lines.append(f"UIDVALIDITY: {_header_text(uidvalidity)}")
    if flags is not None:
        clean_flags = [f for f in (_header_text(x) for x in flags) if f]
        header_lines.append(f"Flags: {', '.join(clean_flags) if clean_flags else '(none)'}")
    if replaced_url_count is not None:
        header_lines.append(f"Replaced URL count: {replaced_url_count}")
    header_block = "\n".join(header_lines)

    # --- Attachments -------------------------------------------------------
    try:
        attachments = list_attachments(msg)
    except Exception:
        attachments = []
    if attachments:
        lines = ["## Attachments", ""]
        for att in attachments:
            name = att.filename or "(unnamed)"
            inline = ", inline" if att.disposition == "inline" else ""
            lines.append(
                f"- **{name}** -- `{att.mime}`, {att.size:,} bytes{inline}, "
                f"attachment_index: `{att.index}`"
            )
        attachments_section = "\n".join(lines)
    else:
        attachments_section = "## Attachments\n\n_No attachments._"

    return f"{header_block}\n\n---\n\n{body_section}\n\n---\n\n{attachments_section}"


def render_batch_markdown(entries: list[dict]) -> str:
    """Render a multi-message fetch as one markdown document.

    Same shape as the Gmail / M365 batch renderers: heading, counts line,
    ``## Batch errors`` first (each entry ``uid`` or ``id``, ``error_code``,
    ``error_message``), then success ``markdown`` documents separated by
    ``===``. Entry order is preserved.
    """
    successes = [e for e in entries if e.get("ok")]
    errors = [e for e in entries if not e.get("ok")]
    lines = [
        "# IMAP messages batch",
        "",
        f"Fetched {len(successes)} message(s). {len(errors)} error(s).",
    ]
    if errors:
        lines += ["", "## Batch errors", ""]
        for entry in errors:
            entry_id = entry.get("uid", entry.get("id", ""))
            code = entry.get("error_code", "fetch_error")
            message = _sanitize_header_value(str(entry.get("error_message", "")))
            lines.append(f"- **{entry_id}** -- `{code}`: {message}")
    document = "\n".join(lines)
    if successes:
        document += "\n\n===\n\n" + "\n\n===\n\n".join(
            entry.get("markdown", "") for entry in successes
        )
    return document
