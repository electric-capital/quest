"""Markdown rendering for IMAP messages (issue #84 phase 3, spec section 10.4).

Parses the raw RFC 5322 bytes fetched with ``BODY.PEEK[]`` using the stdlib
``email`` package (``policy=email.policy.default``) and renders them in the
same LLM-facing shape as the Gmail Simple and Microsoft 365 renderers
(``api/gmail/helpers.py``, ``plugins/m365/render.py``): a labelled header
block, ``---``, ``## Body``, ``---``, ``## Attachments``.

Decisions worth knowing:

* Body parts: a "body candidate" is a ``text/plain`` or ``text/html`` leaf
  without an ``attachment`` disposition and without a filename. Inside a
  ``multipart/alternative`` one alternative is chosen (the first whose plain
  text is non-blank, else the first with HTML; ``include_html=True`` prefers
  HTML); every other multipart (mixed, related, ...) contributes ALL its body
  candidates in order, so an Apple-Mail-style "text, inline image, text"
  message shows both text fragments and a filename-less HTML part next to a
  plain one is rendered too. HTML fragments go through the shared Gmail
  pipeline (``convert_html_to_markdown`` drops ``<script>``/``<style>``
  contents and turns markup into markdown text, so no raw HTML reaches the
  model; remote images become inert ``![alt](url)`` text that nothing
  fetches). ``include_html=True`` shows the raw HTML in a fenced block
  instead, like the other renderers. When no candidate exists, the first
  ``text/plain`` / ``text/html`` leaf without an attachment disposition is
  rendered even if it carries a filename (``Content-Type: ...; name=``).
* URL replacement runs for HTML-derived AND plain-text bodies (unlike Gmail /
  M365, which only replace URLs in HTML bodies): spec 10.4 asks for it and
  plain-text newsletters carry the same long tracking URLs. The mapping is
  cached under ``url_cache_key(account, folder, uidvalidity, uid)`` and also
  under ``latest_url_cache_key(account, folder, uid)`` so a lookup without a
  UIDVALIDITY can find the most recent rendering. The latest pointer is
  rewritten on EVERY replacement-mode render (an empty mapping when nothing
  was replaced), so it never points at a stale message after a UIDVALIDITY
  change. ``:`` and ``%`` in account / folder are percent-encoded in keys.
* Every body part is capped at ``MAX_BODY_BYTES`` of decoded payload
  BEFORE charset decoding / HTML conversion (and the fragments together at
  the same budget), so a huge body cannot blow up conversion time; a
  truncation note says how much was cut.
* Parse budget: before the MIME parser runs, ``message_too_complex`` refuses
  raw messages above ``MAX_RENDER_RAW_BYTES`` or with more than
  ``MAX_BOUNDARY_LINES`` boundary-like lines (the stdlib parser is
  super-linear in the part count); such messages render headers only, with
  a note.
* Header values, the identity lines, flags, filenames and MIME types are
  single-line safe: C0/C1 controls, U+2028/U+2029 and bidi controls become
  spaces. Headers are decoded once (``policy=default`` already decodes
  RFC 2047; only the raw fallback decodes by hand).
* Hostile or broken MIME never raises: unknown charsets fall back to UTF-8
  with replacement, unknown transfer encodings yield the raw payload, the
  part walk is iterative and bounded (``MAX_PARTS`` parts, ``MAX_DEPTH``
  nesting levels), and the HTML converter falls back to a flat text
  extraction on deep markup that would exhaust recursion.
* Attachments are enumerated by ``list_attachments`` in walk order with a
  1-based ``index``: every leaf that is not a body candidate (the same rule
  as ``protocol.is_attachment_part`` for BODYSTRUCTURE, so ``text/calendar``
  invites, ``text/csv`` and vCards count). The save tool MUST use the same
  function so ``attachment_index`` means the same part. Listed sizes are
  estimates from the encoded payload (shown with ``~``; nothing is decoded
  while rendering); the save tool reports the exact decoded size. An
  attached ``message/*`` part (e.g. a forwarded ``message/rfc822``) is one
  attachment and is not descended into -- its bytes are the attached
  message as an ``.eml`` (CRLF line endings), named ``<subject>.eml`` when
  it has no filename.
"""

from __future__ import annotations

import codecs
import email
import email.parser
import email.policy
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
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
# Parse budget (see the module docstring). tools.py refuses to download
# messages above MAX_RENDER_RAW_BYTES for rendering as well.
MAX_RENDER_RAW_BYTES = 25 * 1024 * 1024
MAX_BOUNDARY_LINES = 2000
_HEADER_ONLY_BYTES = 256 * 1024

_BODY_TYPES = ("text/plain", "text/html")

# Characters that must not reach a single-line markdown field: C0/C1
# controls (incl. CR, LF, TAB, VT, FF, ESC, NEL), line/paragraph
# separators and bidi embedding / override / isolate / mark controls.
_LINE_UNSAFE = {
    **{i: " " for i in range(0x00, 0x20)},
    **{i: " " for i in range(0x7F, 0xA0)},
    0x2028: " ", 0x2029: " ",
    **{i: " " for i in range(0x202A, 0x202F)},
    **{i: " " for i in range(0x2066, 0x206A)},
    0x200E: " ", 0x200F: " ", 0x061C: " ",
}
# Underscores stay unescaped: they are common in filenames and the model
# copies listed names into imap_save_attachment's ``filename``.
_NAME_ESCAPES = str.maketrans({"\\": "\\\\", "*": "\\*", "`": "\\`"})


@dataclass
class AttachmentInfo:
    """One attachment of a message, as listed to the model and saved by the tools."""

    index: int
    filename: str | None
    mime: str
    size: int  # estimate from the encoded payload (see estimated_size)
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


def safe_line(value) -> str:
    """Single-line, safe text: no controls, line separators or bidi controls."""
    text = _sanitize_header_value(_clean_text(value))
    return text.translate(_LINE_UNSAFE).strip()


def _header_text(value) -> str:
    """Single-line, safe header text (NOT RFC 2047-decoded again: values
    from ``policy=default`` are already decoded, and decoding twice would
    turn a literal ``=?utf-8?q?x?=`` the sender encoded into ``x``)."""
    return safe_line(value)


def _decode_rfc2047(text: str) -> str:
    if "=?" not in text:
        return text
    try:
        return _clean_text(str(make_header(decode_header(text))))
    except Exception:
        return text


def _get_header(msg: Message, name: str) -> str:
    """Decoded header value; never raises on a malformed header."""
    try:
        value = msg.get(name)
        if value is None:
            return ""
        return _header_text(value)
    except Exception:
        pass
    # Fall back to the unparsed raw value (decoded here, once).
    try:
        for key, raw in msg.raw_items():
            if key.lower() == name.lower():
                return _header_text(_decode_rfc2047(_clean_text(raw)))
    except Exception:
        pass
    return ""


def display_name(name) -> str:
    """A filename for the bold ``**name**`` slot of a listing: single-line
    and with the markdown-active ``\\``, ``*`` and backtick escaped
    (``imap_save_attachment`` also accepts this escaped spelling)."""
    return safe_line(name).translate(_NAME_ESCAPES)


def safe_mime(mime) -> str:
    """A MIME type safe inside a code span."""
    text = safe_line(mime).lower()
    if "`" in text or " " in text or text.count("/") != 1:
        return "application/octet-stream"
    return text


def _key_part(value) -> str:
    return str(value).replace("%", "%25").replace(":", "%3A")


def url_cache_key(account: str, folder: str, uidvalidity, uid) -> str:
    """Per-conversation URL-cache key of one message identity (``:`` and
    ``%`` in account / folder percent-encoded, so keys are unambiguous)."""
    return f"imap:{_key_part(account)}:{_key_part(folder)}:{uidvalidity}:{uid}"


def latest_url_cache_key(account: str, folder: str, uid) -> str:
    """Secondary key holding the most recently cached mapping for (account, folder, uid).

    ``latest`` can never collide with an integer UIDVALIDITY.
    """
    return f"imap:{_key_part(account)}:{_key_part(folder)}:latest:{uid}"


# --------------------------------------------------------------------------
# MIME walking
# --------------------------------------------------------------------------


def message_too_complex(raw, *, max_bytes: int | None = None) -> str | None:
    """Why ``raw`` must not go through the full MIME parser, or None.

    The stdlib parser's cost grows super-linearly with the number of parts
    (100k tiny parts in 4 MB take ~20 s), so besides a size cap the
    boundary-like lines (``\\n--``) are counted first.
    """
    data = raw or b""
    if max_bytes is None:
        max_bytes = MAX_RENDER_RAW_BYTES
    if len(data) > max_bytes:
        return (
            f"the message is {len(data):,} bytes, above the "
            f"{max_bytes // (1024 * 1024)} MB rendering limit"
        )
    if isinstance(data, (bytes, bytearray)):
        boundaries = data.count(b"\n--")
        if boundaries > MAX_BOUNDARY_LINES:
            return (
                f"the message has about {boundaries:,} MIME part boundaries "
                f"(limit {MAX_BOUNDARY_LINES:,})"
            )
    return None


def parse_message(raw: bytes) -> EmailMessage:
    """Parse raw message bytes; never raises (returns an empty message instead).

    Callers check :func:`message_too_complex` first."""
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        return email.message_from_bytes(raw or b"", policy=email.policy.default)
    except Exception:
        return EmailMessage(policy=email.policy.default)


def parse_headers_only(raw) -> EmailMessage:
    """Only the header block of ``raw`` (bounded), for messages too complex
    to parse in full."""
    data = bytes(raw or b"")[:_HEADER_ONLY_BYTES]
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = data.find(sep)
        if idx >= 0:
            data = data[: idx + len(sep)]
            break
    try:
        return email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(data)
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
    """Filename from Content-Disposition ``filename`` or Content-Type
    ``name`` (``policy=default`` decodes RFC 2231 and RFC 2047 already)."""
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return None
    name = safe_line(name)
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
        mime in _BODY_TYPES
        and _disposition(part) != "attachment"
        and not _filename(part)
    )


_EML_POLICY = email.policy.compat32.clone(linesep="\r\n")


def _message_bytes(item: Message) -> bytes:
    """An attached message re-serialised with CRLF line endings."""
    try:
        return item.as_bytes(policy=_EML_POLICY)
    except Exception:
        pass
    try:
        return item.as_string(policy=_EML_POLICY).encode("utf-8", "surrogateescape")
    except Exception:
        return b""


def _part_bytes(part: Message) -> bytes:
    """Decoded payload of a leaf (or the attached message for ``message/*``)."""
    try:
        if _is_message_container(part):
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                return b"\r\n".join(_message_bytes(item) for item in inner)
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


def _children(part: Message) -> list[Message]:
    try:
        payload = part.get_payload()
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return [child for child in payload if isinstance(child, Message)]


def _has_text(parts: list[Message], mime: str) -> bool:
    for part in parts:
        if _content_type(part) == mime:
            try:
                if _decode_text(part)[0].strip():
                    return True
            except Exception:
                continue
    return False


def _pick_alternative(options: list[list[Message]], prefer_html: bool) -> list[Message]:
    order = ("text/html", "text/plain") if prefer_html else ("text/plain", "text/html")
    for mime in order:
        for option in options:
            if _has_text(option, mime):
                return option
    return options[0]


def body_parts(msg: Message, *, prefer_html: bool = False) -> list[Message]:
    """The leaves rendered as the body, in order (see the module docstring)."""
    budget = [MAX_PARTS]

    def collect(part: Message, depth: int) -> list[Message]:
        if budget[0] <= 0:
            return []
        budget[0] -= 1
        if part is not msg and _is_message_container(part):
            return []
        if _safe_is_multipart(part):
            if depth >= MAX_DEPTH:
                return []
            groups = [collect(child, depth + 1) for child in _children(part)]
            if _content_type(part) == "multipart/alternative":
                options = [g for g in groups if g]
                return _pick_alternative(options, prefer_html) if options else []
            return [leaf for group in groups for leaf in group]
        return [part] if _is_body_candidate(part, _content_type(part)) else []

    parts = collect(msg, 0)
    if parts:
        return parts
    # No candidate: a text part that only carries a ``name=`` (or filename)
    # but no attachment disposition is still the body.
    for part in _walk_leaves(msg):
        if _content_type(part) in _BODY_TYPES and _disposition(part) != "attachment":
            return [part]
    return []


def estimated_size(part: Message) -> int:
    """Approximate decoded size of a leaf, from the encoded payload (base64
    = 3/4 of its non-whitespace length); nothing is decoded."""
    try:
        if _is_message_container(part):
            return len(_part_bytes(part))
        payload = part.get_payload(decode=False)
        if not isinstance(payload, str):
            return 0
        cte = str(part.get("Content-Transfer-Encoding", "") or "").strip().lower()
        if cte == "base64":
            data = len(payload) - sum(payload.count(c) for c in "\r\n \t")
            padding = payload.rstrip()[-2:].count("=")
            return max(0, max(0, data) * 3 // 4 - padding)
        if cte == "quoted-printable":
            return max(0, len(payload) - 2 * payload.count("="))
        return len(payload)
    except Exception:
        return 0


def _attached_message_name(part: Message) -> str:
    try:
        inner = part.get_payload()
        subject = inner[0].get("Subject") if isinstance(inner, list) and inner else None
    except Exception:
        subject = None
    text = safe_line(subject or "")[:120].strip()
    return f"{text or 'forwarded-message'}.eml"


def list_attachments(msg: Message) -> list[AttachmentInfo]:
    """Deterministic attachment enumeration (1-based ``index``).

    Every leaf that is not a body candidate -- i.e. the same rule as
    ``protocol.is_attachment_part``: an attachment disposition, a filename,
    or any type other than ``text/plain`` / ``text/html`` (``text/calendar``
    invites, ``text/csv``, vCards, images, attached messages). Unchosen
    body alternatives (e.g. the HTML twin of a plain body) are not
    attachments. The save tool must use this same function to resolve
    ``attachment_index``.
    """
    result: list[AttachmentInfo] = []
    for part in _walk_leaves(msg):
        mime = _content_type(part)
        if _is_body_candidate(part, mime):
            continue
        filename = _filename(part)
        if filename is None and mime.startswith("message/") and _is_message_container(part):
            filename = _attached_message_name(part)
        result.append(
            AttachmentInfo(
                index=len(result) + 1,
                filename=filename,
                mime=mime,
                size=estimated_size(part),
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
    if not _is_safe_charset(charset):
        charset = "utf-8"
    try:
        text = data.decode(charset, "replace")
    except Exception:
        text = data.decode("utf-8", "replace")
    text = _clean_text(text).replace("\x00", "")
    return text.replace("\r\n", "\n").replace("\r", "\n"), total


def _is_safe_charset(charset: str) -> bool:
    """A real text encoding: not a bytes-to-bytes codec (rot13, zlib) and
    not an escape codec (``unicode_escape`` would interpret ``\\x..``
    sequences written by the sender)."""
    try:
        info = codecs.lookup(charset)
    except (LookupError, TypeError, ValueError):
        return False
    if not getattr(info, "_is_text_encoding", True):
        return False
    return not info.name.endswith("escape")


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


_BODY_BUDGET_NOTE = "_[Further body parts omitted: the body exceeds the size limit.]_"


def _body_fragments(parts: list[Message], *, fenced_html: bool) -> tuple[list[str], bool]:
    """Rendered fragments of the body parts and whether HTML was fenced.

    Each part is capped at MAX_BODY_BYTES; once the decoded total passes
    the same budget the remaining parts are omitted with a note."""
    fragments: list[str] = []
    used = 0
    fenced = False
    for part in parts:
        if used >= MAX_BODY_BYTES:
            fragments.append(_BODY_BUDGET_NOTE)
            break
        text, total = _decode_text(part)
        used += min(total, MAX_BODY_BYTES)
        note = _truncation_note(total) if total > MAX_BODY_BYTES else ""
        if _content_type(part) == "text/html":
            if fenced_html:
                fence = _fence_for_html(text)
                fragments.append(f"{fence}html\n{text}\n{fence}{note}")
                fenced = True
                continue
            text = clean_email_markdown(_html_to_markdown(text)) or ""
        if text.strip():
            fragments.append(f"{text.strip(chr(10))}{note}")
    return fragments, fenced


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
    problem = message_too_complex(raw if isinstance(raw, (bytes, bytearray)) else b"")
    if problem is not None:
        msg = parse_headers_only(raw)
    else:
        msg = parse_message(raw)

    # --- Body ------------------------------------------------------------
    replaced_url_count: int | None = None
    body_section = "## Body\n\n_(empty body)_"
    mapping: dict = {}
    fenced = False
    if problem is not None:
        body_section = (
            f"## Body\n\n_[Not rendered: {problem}. Only the headers are shown; "
            "attachments cannot be listed or saved.]_"
        )
    else:
        try:
            fragments: list[str] = []
            if include_html:
                fragments, fenced = _body_fragments(
                    body_parts(msg, prefer_html=True), fenced_html=True,
                )
                if not fenced:
                    fragments = []
            if not fenced:
                fragments, _ = _body_fragments(body_parts(msg), fenced_html=False)
            body_text = "\n\n".join(fragments)
            if fenced:
                body_section = f"## Body (HTML)\n\n{body_text}"
            elif body_text.strip():
                if replace_urls and conversation_id:
                    replaced, mapping = replace_urls_with_identifiers(body_text)
                    if mapping:
                        body_text = replaced
                        replaced_url_count = len(mapping)
                # Plain bodies are NOT fenced: they can contain backticks.
                body_section = f"## Body\n\n{body_text}"
        except Exception:
            body_section = "## Body\n\n_(body could not be decoded)_"
            mapping, replaced_url_count, fenced = {}, None, False
    if replace_urls and conversation_id and not fenced:
        # Always (re)write both keys -- an empty mapping when nothing was
        # replaced -- so the latest pointer never outlives a re-rendering.
        keys = [
            cache_key or url_cache_key(account, folder, uidvalidity, uid),
            latest_url_cache_key(account, folder, uid),
        ]
        if not _cache_mapping(conversation_id, keys, mapping) and replaced_url_count:
            # Caching failed: show the original URLs rather than dead ids.
            return render_message_markdown(
                raw, folder=folder, uid=uid, uidvalidity=uidvalidity, account=account,
                flags=flags, replace_urls=False, conversation_id=None,
                include_html=include_html,
            )

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
    header_lines.append(f"Account: {safe_line(account)}")
    header_lines.append(f"Folder: {safe_line(folder)}")
    header_lines.append(f"UID: {_header_text(uid)}")
    header_lines.append(f"UIDVALIDITY: {_header_text(uidvalidity)}")
    if flags is not None:
        clean_flags = [f for f in (_header_text(x) for x in flags) if f]
        header_lines.append(f"Flags: {', '.join(clean_flags) if clean_flags else '(none)'}")
    if replaced_url_count is not None:
        header_lines.append(f"Replaced URL count: {replaced_url_count}")
    header_block = "\n".join(header_lines)

    # --- Attachments -------------------------------------------------------
    if problem is not None:
        attachments = []
    else:
        try:
            attachments = list_attachments(msg)
        except Exception:
            attachments = []
    if problem is not None:
        attachments_section = "## Attachments\n\n_Not listed (see the note above)._"
    elif attachments:
        lines = ["## Attachments", ""]
        for att in attachments:
            name = display_name(att.filename) if att.filename else "(unnamed)"
            inline = ", inline" if att.disposition == "inline" else ""
            lines.append(
                f"- **{name}** -- `{safe_mime(att.mime)}`, ~{att.size:,} bytes{inline}, "
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
            message = safe_line(str(entry.get("error_message", "")))
            lines.append(f"- **{safe_line(entry_id)}** -- `{safe_line(code)}`: {message}")
    document = "\n".join(lines)
    if successes:
        document += "\n\n===\n\n" + "\n\n===\n\n".join(
            entry.get("markdown", "") for entry in successes
        )
    return document
