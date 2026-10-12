"""Stdlib-only IMAP response parsing and argument encoding.

``imaplib`` hands back raw response data; this module turns it into
Python values:

- :func:`parse_fetch_response` -- FETCH data in imaplib's shape (bytes
  lines and ``(prefix, literal)`` tuples, several literals per message
  allowed) into ``{uid: {ITEM: value}}``.
- :func:`parse_list_line` -- one LIST response line.
- :func:`encode_folder_name` / :func:`decode_folder_name` -- RFC 3501
  modified UTF-7 mailbox names.
- :func:`quote_imap_string` -- a safe quoted string (rejects CR/LF/NUL
  and non-ASCII; callers send non-ASCII as a literal).
- :func:`bodystructure_parts` / :func:`bodystructure_has_attachments` --
  walk a parsed BODYSTRUCTURE.
- :func:`decode_envelope` -- a parsed ENVELOPE into readable strings
  (RFC 2047 decoded).

Value mapping of the parser: ``NIL`` -> None, quoted strings -> str,
digit atoms -> int, other atoms -> str, parenthesised lists -> list,
literals -> bytes at the top level of a FETCH item (``BODY[...]``,
``BINARY[...]``, ``RFC822.*``) and str (UTF-8, replacement on errors)
inside nested lists (e.g. an ENVELOPE subject sent as a literal).
"""

from __future__ import annotations

import base64
import email.header
import email.utils
import re
from email.message import Message
from typing import Any, Iterable, Optional

# -- tokenizer ----------------------------------------------------------


class _Literal:
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data


class _Atom(str):
    """An unquoted atom (distinguishes ``"123"`` from ``123``)."""


_LITERAL_MARK_RE = re.compile(rb"\{(\d+)\+?\}$")


def _segments(data: Iterable[Any]) -> list:
    """Flatten imaplib response data into text segments and literals."""
    out: list = []
    for item in data or ():
        if item is None:
            continue
        if isinstance(item, tuple):
            prefix = item[0] if item else b""
            literal = item[1] if len(item) > 1 else b""
            if isinstance(prefix, str):
                prefix = prefix.encode("utf-8", "replace")
            if isinstance(literal, str):
                literal = literal.encode("utf-8", "replace")
            prefix = _LITERAL_MARK_RE.sub(b"", prefix.rstrip())
            out.append(prefix)
            out.append(_Literal(literal or b""))
        else:
            if isinstance(item, str):
                item = item.encode("utf-8", "replace")
            out.append(item)
            out.append(b"\n")  # end of a response line
    return out


_SPECIAL = b"()\"{ \t\r\n"


def _tokenize(data: Iterable[Any]) -> list:
    """Tokens: '(' ')' '\\n' (line end), _Atom, str (quoted), _Literal."""
    tokens: list = []
    for seg in _segments(data):
        if isinstance(seg, _Literal):
            tokens.append(seg)
            continue
        if seg == b"\n":
            tokens.append("\n")
            continue
        i, n = 0, len(seg)
        while i < n:
            ch = seg[i:i + 1]
            if ch in (b" ", b"\t", b"\r", b"\n"):
                i += 1
            elif ch == b"(":
                tokens.append("(")
                i += 1
            elif ch == b")":
                tokens.append(")")
                i += 1
            elif ch == b'"':
                i += 1
                buf = bytearray()
                while i < n and seg[i:i + 1] != b'"':
                    if seg[i:i + 1] == b"\\" and i + 1 < n:
                        i += 1
                    buf += seg[i:i + 1]
                    i += 1
                i += 1  # closing quote
                tokens.append(bytes(buf).decode("utf-8", "replace"))
            elif ch == b"{":
                # An inline literal marker without a tuple (should not
                # happen with imaplib); skip it.
                end = seg.find(b"}", i)
                i = n if end < 0 else end + 1
            else:
                start = i
                depth = 0
                while i < n:
                    c = seg[i:i + 1]
                    if c == b"[":
                        depth += 1
                    elif c == b"]":
                        depth = max(0, depth - 1)
                    elif depth == 0 and c in (b" ", b"(", b")", b"\t", b"\r", b"\n", b'"'):
                        break
                    i += 1
                tokens.append(_Atom(seg[start:i].decode("utf-8", "replace")))
    return tokens


def _atom_value(tok: _Atom):
    if tok.upper() == "NIL":
        return None
    if tok.isdigit():
        return int(tok)
    return str(tok)


def _parse_value(tokens: list, pos: int, *, top_literal_bytes: bool = False):
    """Parse one value at ``tokens[pos]``; returns (value, next_pos)."""
    while pos < len(tokens) and tokens[pos] == "\n":
        pos += 1
    if pos >= len(tokens):
        return None, pos
    tok = tokens[pos]
    if isinstance(tok, _Literal):
        if top_literal_bytes:
            return tok.data, pos + 1
        return tok.data.decode("utf-8", "replace"), pos + 1
    if tok == "(" and not isinstance(tok, _Atom):
        items: list = []
        pos += 1
        while pos < len(tokens):
            t = tokens[pos]
            if t == "\n":
                pos += 1
                continue
            if t == ")" and not isinstance(t, _Atom):
                return items, pos + 1
            value, pos = _parse_value(tokens, pos)
            items.append(value)
        return items, pos
    if isinstance(tok, _Atom):
        return _atom_value(tok), pos + 1
    if tok == ")":
        return None, pos + 1
    return tok, pos + 1  # quoted string


def _normalize_item_name(name: str) -> str:
    upper = name.upper()
    bracket = upper.find("[")
    head = upper if bracket < 0 else upper[:bracket]
    if head.endswith(".PEEK"):
        upper = head[:-5] + upper[len(head):]
    return upper


_LITERAL_VALUE_ITEMS = ("BODY[", "BINARY[", "RFC822")


def parse_fetch_messages(data: Iterable[Any]) -> list[tuple[int, dict]]:
    """Every FETCH response in ``data`` as ``(sequence number, items)``."""
    tokens = _tokenize(data)
    out: list[tuple[int, dict]] = []
    pos = 0
    n = len(tokens)
    while pos < n:
        tok = tokens[pos]
        if not (isinstance(tok, _Atom) and tok.isdigit()):
            pos += 1
            continue
        seq = int(tok)
        pos += 1
        # Tolerate a stray "FETCH" keyword (raw untagged lines).
        if pos < n and isinstance(tokens[pos], _Atom) and tokens[pos].upper() == "FETCH":
            pos += 1
        if pos >= n or tokens[pos] != "(" or isinstance(tokens[pos], _Atom):
            continue
        pos += 1
        items: dict = {}
        while pos < n:
            t = tokens[pos]
            if t == "\n":
                pos += 1
                continue
            if t == ")" and not isinstance(t, _Atom):
                pos += 1
                break
            if not isinstance(t, _Atom):
                # Malformed: skip the value-less token.
                pos += 1
                continue
            key = _normalize_item_name(str(t))
            pos += 1
            literal_bytes = key.startswith(_LITERAL_VALUE_ITEMS)
            value, pos = _parse_value(tokens, pos, top_literal_bytes=literal_bytes)
            if literal_bytes and isinstance(value, str):
                value = value.encode("utf-8", "replace")
            items[key] = value
        out.append((seq, items))
    return out


def parse_fetch_response(data: Iterable[Any]) -> dict[int, dict]:
    """FETCH data keyed by UID (responses without a UID item -- e.g.
    unsolicited flag updates -- are dropped). A UID repeated across
    responses merges its items."""
    result: dict[int, dict] = {}
    for _seq, items in parse_fetch_messages(data):
        uid = items.get("UID")
        if not isinstance(uid, int):
            continue
        result.setdefault(uid, {}).update(items)
    return result


def parse_status_response(data: Iterable[Any]) -> dict[str, int]:
    """``STATUS`` data into ``{"MESSAGES": n, ...}`` (upper-case keys)."""
    tokens = _tokenize(data)
    # The attribute list is the last parenthesised group.
    last_open = None
    for i, t in enumerate(tokens):
        if t == "(" and not isinstance(t, _Atom):
            last_open = i
    if last_open is None:
        return {}
    values, _ = _parse_value(tokens, last_open)
    out: dict[str, int] = {}
    if isinstance(values, list):
        for i in range(0, len(values) - 1, 2):
            key, value = values[i], values[i + 1]
            if isinstance(key, str) and isinstance(value, int):
                out[key.upper()] = value
    return out


# -- LIST ---------------------------------------------------------------

SPECIAL_USE_FLAGS = ("\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Junk", "\\Sent", "\\Trash")
_SPECIAL_USE_BY_UPPER = {f.upper(): f for f in SPECIAL_USE_FLAGS}


def parse_list_line(line: Any) -> Optional[dict]:
    """One LIST response (bytes, or imaplib's ``(prefix, literal)`` tuple
    when the name is a literal) into ``{name, raw_name, delimiter, flags,
    special_use}``; None when the line is not a LIST response."""
    tokens = [t for t in _tokenize([line]) if t != "\n"]
    if not tokens:
        return None
    flags_value, pos = _parse_value(tokens, 0)
    if not isinstance(flags_value, list):
        return None
    delimiter, pos = _parse_value(tokens, pos)
    name, pos = _parse_value(tokens, pos)
    if name is None:
        return None
    raw_name = str(name)
    flags = [str(f) for f in flags_value if f is not None]
    special_use = None
    for flag in flags:
        if flag.upper() in _SPECIAL_USE_BY_UPPER:
            special_use = _SPECIAL_USE_BY_UPPER[flag.upper()]
            break
    if raw_name.upper() == "INBOX":
        raw_name = "INBOX"
    return {
        "name": decode_folder_name(raw_name),
        "raw_name": raw_name,
        "delimiter": None if delimiter is None else str(delimiter),
        "flags": flags,
        "special_use": special_use,
    }


# -- modified UTF-7 (RFC 3501 5.1.3) ----------------------------------


def _b64_encode_utf16(chunk: str) -> str:
    raw = chunk.encode("utf-16-be")
    return base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")


def encode_folder_name(name: str) -> str:
    """Encode a folder name into modified UTF-7 (ASCII)."""
    out: list[str] = []
    pending: list[str] = []

    def flush():
        if pending:
            out.append("&" + _b64_encode_utf16("".join(pending)) + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def decode_folder_name(name: str) -> str:
    """Decode a modified UTF-7 folder name; malformed runs are kept as-is."""
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    out: list[str] = []
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end < 0:
            out.append(name[i:])
            break
        run = name[i + 1:end]
        if not run:
            out.append("&")
        else:
            b64 = run.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64, validate=True).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


# -- quoting ------------------------------------------------------------


def check_safe_text(value: str, what: str = "value") -> str:
    """Reject CR, LF and NUL (command injection)."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    if any(c in value for c in ("\r", "\n", "\0")):
        raise ValueError(f"{what} must not contain line breaks or NUL characters")
    return value


def quote_imap_string(value: str) -> str:
    """An IMAP quoted string. ASCII only; rejects CR/LF/NUL."""
    check_safe_text(value)
    if not value.isascii():
        raise ValueError("non-ASCII text must be sent as a literal")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_folder(name: str) -> str:
    """A folder name encoded (modified UTF-7) and quoted for a command."""
    check_safe_text(name, "folder name")
    return quote_imap_string(encode_folder_name(name))


# -- BODYSTRUCTURE ------------------------------------------------------


def _decode_words(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    try:
        return str(email.header.make_header(email.header.decode_header(text)))
    except Exception:
        return text


def _params_from_header(value: str) -> dict[str, str]:
    """Parameters of a ``type; key=value; ...`` header value via the
    stdlib parser (RFC 2231 continuations / charsets decoded)."""
    msg = Message()
    msg["Content-Disposition"] = value.replace("\r", " ").replace("\n", " ")
    out: dict[str, str] = {}
    try:
        params = msg.get_params(header="content-disposition") or []
    except Exception:
        return out
    for key, raw in params[1:]:
        try:
            text = email.utils.collapse_rfc2231_value(raw)
        except Exception:
            text = raw if isinstance(raw, str) else ""
        out[str(key).lower()] = email.utils.unquote(text)
    return out


def _param_dict(params: Any) -> dict[str, str]:
    """``("NAME" "x.pdf" ...)`` into a lower-cased key dict; RFC 2231
    ``name*`` / ``name*0*`` continuations are joined and decoded."""
    if not isinstance(params, list):
        return {}
    pieces = ["x"]
    for i in range(0, len(params) - 1, 2):
        key, value = params[i], params[i + 1]
        if key is None or value is None:
            continue
        key = re.sub(r"[^A-Za-z0-9*_.-]", "", str(key)).lower()
        if not key:
            continue
        value = str(value)
        if key.endswith("*") and re.fullmatch(r"[^\s;\"]*", value):
            pieces.append(f"{key}={value}")
        else:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            pieces.append(f'{key}="{escaped}"')
    return _params_from_header("; ".join(pieces))


def _disposition(value: Any) -> tuple[Optional[str], dict]:
    """A BODYSTRUCTURE disposition: the RFC 3501 list form
    ``("attachment" ("filename" "x.pdf"))`` or, as some servers send it,
    the raw header string ``attachment; filename="x.pdf"``."""
    if isinstance(value, list) and value and isinstance(value[0], str):
        params = _param_dict(value[1]) if len(value) > 1 else {}
        return value[0].lower(), params
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        kind = text.split(";", 1)[0].strip().lower()
        if not kind or not re.fullmatch(r"[a-z0-9._+-]+", kind):
            return None, {}
        return kind, _params_from_header(text)
    return None, {}


def _leaf_info(part: list, section: str) -> dict:
    maintype = str(part[0] or "").lower() if len(part) > 0 else ""
    subtype = str(part[1] or "").lower() if len(part) > 1 else ""
    params = _param_dict(part[2]) if len(part) > 2 else {}
    size = part[6] if len(part) > 6 and isinstance(part[6], int) else 0
    if maintype == "text":
        disp_index = 9
    elif maintype == "message" and subtype in ("rfc822", "global"):
        disp_index = 11
    else:
        disp_index = 8
    disposition, disp_params = _disposition(part[disp_index] if len(part) > disp_index else None)
    filename = disp_params.get("filename") or params.get("name")
    return {
        "section": section,
        "mime": f"{maintype}/{subtype}" if maintype else "application/octet-stream",
        "filename": _decode_words(filename) if filename else None,
        "size": size,
        "disposition": disposition,
    }


def _is_multipart(part: Any) -> bool:
    return isinstance(part, list) and bool(part) and isinstance(part[0], list)


def bodystructure_parts(bs: Any) -> list[dict]:
    """Every leaf part of a parsed BODYSTRUCTURE, depth first, as
    ``{"section", "mime", "filename", "size", "disposition"}``. Sections
    follow RFC 3501 numbering (a single-part message is section "1"); an
    attached ``message/rfc822`` is one leaf and is not descended into."""
    out: list[dict] = []

    def walk(part: Any, prefix: str) -> None:
        if not isinstance(part, list) or not part:
            return
        if _is_multipart(part):
            index = 0
            for child in part:
                if not isinstance(child, list):
                    break
                index += 1
                walk(child, f"{prefix}.{index}" if prefix else str(index))
            return
        out.append(_leaf_info(part, prefix or "1"))

    walk(bs, "")
    return out


def bodystructure_has_attachments(bs: Any) -> bool:
    """Whether any leaf looks like an attachment: an ``attachment``
    disposition, a filename, or a non-text type."""
    for part in bodystructure_parts(bs):
        if part["disposition"] == "attachment" or part["filename"]:
            return True
        if not part["mime"].startswith("text/"):
            return True
    return False


# -- ENVELOPE -----------------------------------------------------------


def _format_addresses(value: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for addr in value:
        if not isinstance(addr, list) or len(addr) < 4:
            continue
        name, _adl, mailbox, host = addr[:4]
        if host is None:
            # RFC 3501 group syntax markers; skip.
            continue
        address = f"{mailbox}@{host}" if mailbox else str(host)
        display = _decode_words(name) if name else None
        out.append(f"{display} <{address}>" if display else address)
    return out


def decode_envelope(env: Any) -> dict:
    """A parsed ENVELOPE as ``{date, subject, from, to, cc, message_id}``;
    address fields are lists of ``Name <addr>`` strings."""
    if not isinstance(env, list):
        env = []
    env = list(env) + [None] * (10 - len(env))
    return {
        "date": _decode_words(env[0]),
        "subject": _decode_words(env[1]),
        "from": _format_addresses(env[2]),
        "to": _format_addresses(env[5]),
        "cc": _format_addresses(env[6]),
        "message_id": None if env[9] is None else str(env[9]),
    }
