"""IMAP mailbox dynamic tools (issue #84 phase 3, spec section 10.3).

Five read-only ``imap_*`` plugin tools over the transport in
``plugins/imap/upstream.py`` (one verified-TLS session per call, folders
opened with ``EXAMINE``, FETCH items checked so nothing can set
``\\Seen``):

- ``imap_list_folders`` -- LIST + STATUS (MESSAGES UNSEEN UIDVALIDITY) per
  folder, capped at 200 folders.
- ``imap_search_messages`` -- server-side ``UID SEARCH`` from structured
  criteria; envelope rows for the newest ``limit`` matches.
- ``imap_get_messages`` -- 1-20 messages by UID fetched with
  ``BODY.PEEK[]`` and rendered as markdown (plugins/imap/render.py).
- ``imap_get_message_urls`` -- resolve ``(#N#)`` identifiers from the
  per-conversation URL cache (no network).
- ``imap_save_attachment`` -- write one attachment into the conversation
  workspace under ``imap-attachments/<account>/`` (tool-only).

Every tool is a multi-connection tool: an optional ``connection``
argument (account address) picks the row through
``config.user_connections.resolve_connection``; omitted = the default
connection. Message identity is ``(folder, uidvalidity, uid)``; the
optional ``uidvalidity`` argument makes a stale UID fail loudly with
``imap_uidvalidity_changed`` instead of silently addressing another
message.

Results are JSON strings, except ``imap_get_messages`` which returns a
markdown document like ``m365_get_mail_messages``. Errors are JSON
``{"error": <code>, "message": ...}`` (``ImapError.to_dict()`` for
transport failures).
"""

from __future__ import annotations

import asyncio
import imaplib
import json
import mimetypes
import os
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Optional

from config.plugin_types import PluginTool
from config.user_connections import (
    ConnectionNotFound,
    ConnectionNotSupported,
    resolve_connection,
)

from plugins.imap import protocol
from plugins.imap.render import (
    attachment_payload,
    latest_url_cache_key,
    list_attachments,
    parse_message,
    render_batch_markdown,
    render_message_markdown,
    url_cache_key,
)
from plugins.imap.settings import SERVICE_ID, imap_connected
from plugins.imap.upstream import ImapError, ImapSession, run_imap, settings_from_row

# -- limits ---------------------------------------------------------------

MAX_FOLDERS = 200
MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20
MAX_GET_UIDS = 20
MAX_UID = 2**32 - 1
MAX_TEXT_ARG = 500
MAX_FOLDER_ARG = 1000
MAX_URL_IDS = 200

# Messages larger than this are not downloaded by imap_get_messages: the
# headers are rendered plus a BODYSTRUCTURE-based attachment list.
MAX_RENDER_MESSAGE_BYTES = 25 * 1024 * 1024
# Total raw bytes one imap_get_messages call downloads; further UIDs are
# reported as batch errors (fetch them in another call).
MAX_BATCH_BYTES = 60 * 1024 * 1024

# imap_save_attachment: cap on the decoded attachment, and the message size
# above which the attachment cannot possibly fit (base64 inflates ~4/3,
# plus headers / other parts), so the download is refused up front.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_SAVE_MESSAGE_BYTES = int(MAX_ATTACHMENT_BYTES * 1.4) + 1024 * 1024

ATTACHMENTS_DIR = "imap-attachments"
_MAX_FILENAME_CHARS = 180

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOSELECT_FLAGS = ("\\NOSELECT", "\\NONEXISTENT")

_SEARCH_FETCH_ITEMS = "(UID FLAGS RFC822.SIZE INTERNALDATE ENVELOPE BODYSTRUCTURE)"


# -- small helpers --------------------------------------------------------


class _ArgError(ValueError):
    """An invalid tool argument (rendered as ``invalid_arguments``)."""


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _invalid(message: str) -> str:
    return _json({"error": "invalid_arguments", "message": message})


def _opt_str(args: dict, key: str, *, max_len: int = MAX_TEXT_ARG) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArgError(f"{key} must be a string.")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_len:
        raise _ArgError(f"{key} must be at most {max_len} characters.")
    if any(c in value for c in ("\r", "\n", "\0")):
        raise _ArgError(f"{key} must not contain line breaks or NUL characters.")
    return value


def _folder_arg(args: dict, *, required: bool) -> str:
    folder = _opt_str(args, "folder", max_len=MAX_FOLDER_ARG)
    if folder is None:
        if required:
            raise _ArgError("folder is required (e.g. \"INBOX\"; see imap_list_folders).")
        folder = "INBOX"
    # INBOX is case-insensitive in IMAP; one spelling keeps URL-cache keys stable.
    if folder.upper() == "INBOX":
        folder = "INBOX"
    return folder


def _int_value(value: Any, key: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise _ArgError(f"{key} must be an integer.")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise _ArgError(f"{key} must be an integer.")
    if not lo <= value <= hi:
        raise _ArgError(f"{key} must be between {lo} and {hi}.")
    return value


def _opt_int(args: dict, key: str, lo: int, hi: int, default: Optional[int] = None) -> Optional[int]:
    value = args.get(key)
    if value is None or value == "":
        return default
    return _int_value(value, key, lo, hi)


def _opt_bool(args: dict, key: str) -> bool:
    value = args.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise _ArgError(f"{key} must be true or false.")


def _opt_date(args: dict, key: str) -> Optional[date]:
    value = args.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value.strip()):
        raise _ArgError(f"{key} must be a date in YYYY-MM-DD format (e.g. 2026-10-05).")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _ArgError(f"{key} is not a valid calendar date: {value!r}.") from None


def imap_date(day: date) -> str:
    """An ISO date as an IMAP search date (``05-Oct-2026``), locale-free."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _uid_list(args: dict) -> list[int]:
    raw = args.get("uids")
    if isinstance(raw, (int, str)) and not isinstance(raw, bool):
        raw = [p for p in str(raw).split(",") if p.strip()] if isinstance(raw, str) else [raw]
    if not isinstance(raw, list) or not raw:
        raise _ArgError(f"uids is required: a list of 1-{MAX_GET_UIDS} message UIDs.")
    uids: list[int] = []
    for item in raw:
        uid = _int_value(item, "each uid", 1, MAX_UID)
        if uid not in uids:
            uids.append(uid)
    if len(uids) > MAX_GET_UIDS:
        raise _ArgError(
            f"At most {MAX_GET_UIDS} UIDs per call (got {len(uids)}); split them into "
            "several calls."
        )
    return uids


def _resolve(ctx, args: dict):
    """(row, error_json): the connection row to use, or a structured error."""
    connection = args.get("connection")
    if connection is not None and not isinstance(connection, str):
        return None, _invalid("connection must be a string (an account address).")
    try:
        row = resolve_connection(ctx.user or {}, SERVICE_ID, connection)
    except ConnectionNotFound as exc:
        return None, _json({
            "error": "connection_not_found",
            "service": SERVICE_ID,
            "requested": exc.requested,
            "available": exc.available,
            "message": (
                f"No IMAP connection named {exc.requested!r}. Use one of the "
                "available accounts, or omit connection for the default."
            ),
        })
    except ConnectionNotSupported as exc:
        return None, _json({
            "error": "connection_not_supported", "service": SERVICE_ID,
            "requested": exc.requested,
        })
    if row is None or not imap_connected(row):
        return None, _json({
            "error": "imap_not_connected",
            "service": SERVICE_ID,
            "message": "No IMAP mailbox is connected. Add one in Settings > Data Connections.",
        })
    return row, None


def _settings(row: dict):
    """(settings, error_json)."""
    try:
        return settings_from_row(row), None
    except ImapError as exc:
        return None, _json(exc.to_dict())


async def _run(settings, work):
    """run_imap with ImapError / bad-argument ValueError rendered as JSON."""
    try:
        return await run_imap(settings, work), None
    except ImapError as exc:
        return None, _json(exc.to_dict())
    except ValueError as exc:
        return None, _invalid(str(exc))


def _join(values) -> str:
    return ", ".join(v for v in (values or []) if v)


def _flags(value) -> list[str]:
    return [str(f) for f in value] if isinstance(value, list) else []


# -- imap_list_folders --------------------------------------------------


async def _handle_list_folders(ctx, args: dict) -> str:
    row, error = _resolve(ctx, args)
    if error:
        return error
    settings, error = _settings(row)
    if error:
        return error

    def work(session: ImapSession) -> dict:
        listed = session.list_folders(limit=MAX_FOLDERS + 1)
        truncated = len(listed) > MAX_FOLDERS
        folders = []
        for entry in listed[:MAX_FOLDERS]:
            item = {
                "name": entry["name"],
                "delimiter": entry.get("delimiter"),
                "flags": entry.get("flags") or [],
                "special_use": entry.get("special_use"),
                "messages": None,
                "unseen": None,
                "uidvalidity": None,
            }
            if any(f.upper() in _NOSELECT_FLAGS for f in item["flags"]):
                item["selectable"] = False
            else:
                try:
                    status = session.status(entry["name"])
                except ImapError as exc:
                    item["status_error"] = exc.message
                except imaplib.IMAP4.abort:
                    raise
                except (imaplib.IMAP4.error, ValueError) as exc:
                    # e.g. BAD on an odd folder: keep listing the rest.
                    item["status_error"] = str(exc)[:300]
                else:
                    item.update(status)
            folders.append(item)
        return {"folders": folders, "truncated": truncated}

    result, error = await _run(settings, work)
    if error:
        return error
    payload = {
        "account": settings.account,
        "count": len(result["folders"]),
        "folders": result["folders"],
    }
    if result["truncated"]:
        payload["truncated"] = True
        payload["note"] = f"Only the first {MAX_FOLDERS} folders are listed."
    return _json(payload)


# -- imap_search_messages -------------------------------------------------


def _search_terms(args: dict) -> list:
    terms: list = []
    for key in ("from", "to", "subject", "text"):
        value = _opt_str(args, key)
        if value is not None:
            terms.append((key.upper(), value))
    since = _opt_date(args, "since")
    before = _opt_date(args, "before")
    if since and before and since >= before:
        raise _ArgError("before must be later than since (before is exclusive).")
    if since:
        terms.append(("SINCE", imap_date(since)))
    if before:
        terms.append(("BEFORE", imap_date(before)))
    if _opt_bool(args, "unseen"):
        terms.append("UNSEEN")
    return terms or ["ALL"]


def _search_row(uid: int, item: dict) -> dict:
    env = protocol.decode_envelope(item.get("ENVELOPE"))
    bs = item.get("BODYSTRUCTURE")
    return {
        "uid": uid,
        "date": env.get("date") or item.get("INTERNALDATE"),
        "from": _join(env.get("from")),
        "to": _join(env.get("to")),
        "subject": env.get("subject") or "",
        "size": item.get("RFC822.SIZE"),
        "flags": _flags(item.get("FLAGS")),
        "has_attachments": protocol.bodystructure_has_attachments(bs) if bs else False,
    }


async def _handle_search_messages(ctx, args: dict) -> str:
    try:
        folder = _folder_arg(args, required=False)
        terms = _search_terms(args)
        limit = _opt_int(args, "limit", 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)
    except _ArgError as exc:
        return _invalid(str(exc))
    row, error = _resolve(ctx, args)
    if error:
        return error
    settings, error = _settings(row)
    if error:
        return error

    def work(session: ImapSession) -> dict:
        uidvalidity = session.examine(folder)
        matches = session.uid_search(terms)
        newest = sorted(matches, reverse=True)[:limit]
        fetched = session.uid_fetch(newest, _SEARCH_FETCH_ITEMS) if newest else {}
        rows = [_search_row(uid, fetched[uid]) for uid in newest if uid in fetched]
        return {"uidvalidity": uidvalidity, "total_matches": len(matches), "messages": rows}

    result, error = await _run(settings, work)
    if error:
        return error
    payload = {
        "account": settings.account,
        "folder": folder,
        "uidvalidity": result["uidvalidity"],
        "total_matches": result["total_matches"],
        "returned": len(result["messages"]),
        "messages": result["messages"],
    }
    if result["total_matches"] > len(result["messages"]):
        payload["note"] = (
            f"Showing the newest {len(result['messages'])} of {result['total_matches']} "
            "matches; narrow the search (dates, from, subject) to see older ones."
        )
    return _json(payload)


# -- imap_get_messages ----------------------------------------------------


def _huge_message_markdown(item: dict, *, folder: str, uid: int, uidvalidity: int,
                           account: str) -> str:
    """Headers + a BODYSTRUCTURE attachment list for a message too large to fetch."""
    header = item.get("BODY[HEADER]") or b""
    rendered = render_message_markdown(
        bytes(header) + b"\r\n", folder=folder, uid=uid, uidvalidity=uidvalidity,
        account=account, flags=_flags(item.get("FLAGS")), replace_urls=False,
    )
    header_block = rendered.split("\n\n---\n\n", 1)[0]
    size = item.get("RFC822.SIZE") or 0
    body = (
        "## Body\n\n"
        f"_[Message is {size:,} bytes, above the {MAX_RENDER_MESSAGE_BYTES // (1024 * 1024)} MB "
        "rendering limit: only the headers and the attachment list are shown.]_"
    )
    parts = [
        p for p in protocol.bodystructure_parts(item.get("BODYSTRUCTURE"))
        if p["disposition"] == "attachment" or p["filename"] or not p["mime"].startswith("text/")
    ]
    if parts:
        lines = ["## Attachments", "",
                 "_Sizes are encoded (transfer) sizes. Save one with imap_save_attachment "
                 "using its `filename`._", ""]
        for part in parts:
            name = part["filename"] or "(unnamed)"
            lines.append(f"- **{name}** -- `{part['mime']}`, ~{part['size']:,} bytes")
        attachments = "\n".join(lines)
    else:
        attachments = "## Attachments\n\n_No attachments._"
    return f"{header_block}\n\n---\n\n{body}\n\n---\n\n{attachments}"


async def _handle_get_messages(ctx, args: dict) -> str:
    try:
        folder = _folder_arg(args, required=True)
        uids = _uid_list(args)
        expected = _opt_int(args, "uidvalidity", 1, MAX_UID)
        include_html = _opt_bool(args, "include_html")
        include_urls = _opt_bool(args, "include_urls")
    except _ArgError as exc:
        return _invalid(str(exc))
    row, error = _resolve(ctx, args)
    if error:
        return error
    settings, error = _settings(row)
    if error:
        return error
    account = settings.account
    conversation_id = getattr(ctx, "conversation_id", None)

    def work(session: ImapSession) -> tuple[int, list[dict]]:
        # Network only: raw bytes are collected here and rendered after the
        # session closes (rendering is CPU-heavy and must not eat the call
        # timeout or hold the connection open).
        uidvalidity = session.check_uidvalidity(folder, expected)
        meta = session.uid_fetch(uids, "(UID FLAGS RFC822.SIZE)")
        fetched: list[dict] = []
        budget = MAX_BATCH_BYTES
        for uid in uids:
            item = meta.get(uid)
            if item is None:
                fetched.append({
                    "uid": uid, "ok": False, "error_code": "imap_message_not_found",
                    "error_message": f"No message with UID {uid} in {folder!r}.",
                })
                continue
            size = item.get("RFC822.SIZE") or 0
            if size > MAX_RENDER_MESSAGE_BYTES:
                # Never handed to the full MIME parser: headers + BODYSTRUCTURE.
                big = session.uid_fetch(
                    [uid], "(UID FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER])",
                ).get(uid)
                fetched.append({"uid": uid, "kind": "huge", "item": big or item})
                continue
            if size > budget:
                fetched.append({
                    "uid": uid, "ok": False, "error_code": "imap_batch_too_large",
                    "error_message": (
                        f"Skipped: this batch already fetched about "
                        f"{(MAX_BATCH_BYTES - budget) // (1024 * 1024)} MB; fetch UID {uid} "
                        "in a separate call."
                    ),
                })
                continue
            got = session.uid_fetch([uid], "(UID FLAGS BODY.PEEK[])").get(uid) or {}
            raw = got.get("BODY[]")
            if raw is None:
                fetched.append({
                    "uid": uid, "ok": False, "error_code": "imap_message_not_found",
                    "error_message": f"Message UID {uid} disappeared while fetching.",
                })
                continue
            budget -= len(raw)
            fetched.append({
                "uid": uid, "kind": "full", "raw": bytes(raw),
                "flags": _flags(got.get("FLAGS", item.get("FLAGS"))),
            })
        return uidvalidity, fetched

    result, error = await _run(settings, work)
    if error:
        return error
    uidvalidity, fetched = result

    def render_all() -> list[dict]:
        entries: list[dict] = []
        for entry in fetched:
            kind = entry.get("kind")
            if kind == "huge":
                markdown = _huge_message_markdown(
                    entry["item"], folder=folder, uid=entry["uid"],
                    uidvalidity=uidvalidity, account=account,
                )
            elif kind == "full":
                markdown = render_message_markdown(
                    entry.pop("raw"), folder=folder, uid=entry["uid"],
                    uidvalidity=uidvalidity, account=account, flags=entry["flags"],
                    replace_urls=not include_urls, conversation_id=conversation_id,
                    include_html=include_html,
                )
            else:
                entries.append(entry)
                continue
            entries.append({"uid": entry["uid"], "ok": True, "markdown": markdown})
        return entries

    entries = await asyncio.to_thread(render_all)
    if len(entries) == 1:
        entry = entries[0]
        if entry["ok"]:
            return entry["markdown"]
        return _json({
            "error": entry["error_code"], "message": entry["error_message"],
            "uid": entry["uid"], "folder": folder,
        })
    return render_batch_markdown(entries)


# -- imap_get_message_urls -----------------------------------------------


async def _handle_get_message_urls(ctx, args: dict) -> str:
    from api.gmail.helpers import _get_cached_url_mapping

    try:
        folder = _folder_arg(args, required=True)
        uid = _opt_int(args, "uid", 1, MAX_UID)
        if uid is None:
            raise _ArgError("uid is required.")
        uidvalidity = _opt_int(args, "uidvalidity", 1, MAX_UID)
        raw_ids = args.get("url_ids")
        url_ids: Optional[list[int]] = None
        if raw_ids is not None and raw_ids != []:
            if not isinstance(raw_ids, list):
                raw_ids = [raw_ids]
            if len(raw_ids) > MAX_URL_IDS:
                raise _ArgError(f"At most {MAX_URL_IDS} url_ids per call.")
            url_ids = [_int_value(v, "each url_id", 1, 10**9) for v in raw_ids]
    except _ArgError as exc:
        return _invalid(str(exc))
    row, error = _resolve(ctx, args)
    if error:
        return error
    conversation_id = getattr(ctx, "conversation_id", None)
    if not conversation_id:
        return _json({
            "error": "no_conversation_context",
            "message": (
                "URL lookup needs conversation context (the mappings are cached per "
                "conversation); script calls have none."
            ),
        })
    account = row.get("account") or ""
    key = (
        url_cache_key(account, folder, uidvalidity, uid) if uidvalidity is not None
        else latest_url_cache_key(account, folder, uid)
    )
    try:
        mapping = _get_cached_url_mapping(conversation_id, key)
    except ValueError:
        mapping = None
    if not mapping:
        return _json({
            "error": "url_mapping_not_found",
            "message": (
                f"No cached URL mappings for UID {uid} in {folder!r} of {account}"
                + (f" (UIDVALIDITY {uidvalidity})" if uidvalidity is not None else "")
                + ". Mappings exist only for messages fetched with imap_get_messages "
                "in this conversation with URL replacement on."
            ),
        })
    base = {"account": account, "folder": folder, "uid": uid}
    if uidvalidity is not None:
        base["uidvalidity"] = uidvalidity
    if url_ids is None:
        return _json({**base, "urls": {str(k): v for k, v in sorted(mapping.items())}})
    urls: dict[str, str] = {}
    errors: dict[str, str] = {}
    for ident in url_ids:
        url = mapping.get(ident)
        if url is None:
            errors[str(ident)] = f"No URL mapping found for identifier {ident}."
        else:
            urls[str(ident)] = url
    payload = {**base, "urls": urls}
    if errors:
        payload["errors"] = errors
    return _json(payload)


# -- imap_save_attachment ------------------------------------------------


def safe_filename(name: Optional[str], fallback: str) -> str:
    """A single workspace path component: no separators, no leading dots,
    no control characters, bounded length (extension kept)."""
    from chat.gemini_api.tool_handlers import _sanitize_workspace_filename

    text = "".join(
        "_" if unicodedata.category(c).startswith("C") else c for c in (name or "")
    )
    text = _sanitize_workspace_filename(text).strip()
    if text in ("", ".", ".."):
        text = fallback
    if len(text) > _MAX_FILENAME_CHARS:
        stem, ext = os.path.splitext(text)
        ext = ext[:20]
        text = stem[: _MAX_FILENAME_CHARS - len(ext)] + ext
    return text


def _default_attachment_name(index: int, mime: str) -> str:
    ext = mimetypes.guess_extension(mime or "") or ".bin"
    if mime == "message/rfc822":
        ext = ".eml"
    return f"attachment-{index}{ext}"


def _write_unique(directory: Path, filename: str, data: bytes) -> Path:
    """Create ``directory/filename`` exclusively, adding ``-2``, ``-3``, ...
    on collision; never follows a symlink at the leaf."""
    stem, ext = os.path.splitext(filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1, 1000):
        candidate = directory / (filename if n == 1 else f"{stem}-{n}{ext}")
        try:
            fd = os.open(candidate, flags, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return candidate
    raise FileExistsError("too many files with this name")


def _pick_attachment(attachments, index: Optional[int], filename: Optional[str]):
    """(attachment, error_message)."""
    available = [
        {"attachment_index": a.index, "filename": a.filename, "mime": a.mime, "size": a.size}
        for a in attachments
    ]
    if not attachments:
        return None, ("This message has no attachments.", available)
    if index is not None:
        for att in attachments:
            if att.index == index:
                return att, None
        return None, (f"No attachment with attachment_index {index}.", available)
    wanted = filename.casefold()
    matches = [a for a in attachments if (a.filename or "").casefold() == wanted]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, (f"No attachment named {filename!r}.", available)
    return None, (
        f"Several attachments are named {filename!r}; pass attachment_index instead.",
        available,
    )


async def _handle_save_attachment(ctx, args: dict) -> str:
    from chat.gemini_api import tool_handlers

    try:
        folder = _folder_arg(args, required=True)
        uid = _opt_int(args, "uid", 1, MAX_UID)
        if uid is None:
            raise _ArgError("uid is required.")
        expected = _opt_int(args, "uidvalidity", 1, MAX_UID)
        index = _opt_int(args, "attachment_index", 1, 10_000)
        filename = _opt_str(args, "filename", max_len=1000)
        if (index is None) == (filename is None):
            raise _ArgError("Pass exactly one of attachment_index or filename.")
        save_as = _opt_str(args, "save_as", max_len=1000)
    except _ArgError as exc:
        return _invalid(str(exc))
    row, error = _resolve(ctx, args)
    if error:
        return error
    conversation_id = getattr(ctx, "conversation_id", None)
    if not conversation_id:
        return _json({
            "error": "no_conversation_context",
            "message": "imap_save_attachment needs a conversation workspace.",
        })
    settings, error = _settings(row)
    if error:
        return error

    def work(session: ImapSession) -> tuple[int, bytes]:
        uidvalidity = session.check_uidvalidity(folder, expected)
        meta = session.uid_fetch([uid], "(UID RFC822.SIZE)").get(uid)
        if meta is None:
            raise ImapError(
                "imap_message_not_found", f"No message with UID {uid} in {folder!r}.",
                folder=folder, uid=uid,
            )
        size = meta.get("RFC822.SIZE") or 0
        if size > MAX_SAVE_MESSAGE_BYTES:
            raise ImapError(
                "imap_attachment_too_large",
                f"The message is {size:,} bytes; attachments above "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB cannot be saved.",
                size=size,
            )
        raw = (session.uid_fetch([uid], "(UID BODY.PEEK[])").get(uid) or {}).get("BODY[]")
        if raw is None:
            raise ImapError(
                "imap_message_not_found", f"Message UID {uid} disappeared while fetching.",
                folder=folder, uid=uid,
            )
        return uidvalidity, bytes(raw)

    fetched, error = await _run(settings, work)
    if error:
        return error
    uidvalidity, raw = fetched

    def extract() -> dict:
        # MIME parsing is CPU-bound: off the event loop, after the session.
        attachments = list_attachments(parse_message(raw))
        att, problem = _pick_attachment(attachments, index, filename)
        if att is None:
            message, available = problem
            raise ImapError("imap_attachment_not_found", message, available=available)
        data = attachment_payload(att)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ImapError(
                "imap_attachment_too_large",
                f"The attachment is {len(data):,} bytes, above the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit.",
                size=len(data),
            )
        return {
            "uidvalidity": uidvalidity, "data": data, "index": att.index,
            "filename": att.filename, "mime": att.mime,
        }

    try:
        result = await asyncio.to_thread(extract)
    except ImapError as exc:
        return _json(exc.to_dict())

    default_name = result["filename"] or _default_attachment_name(result["index"], result["mime"])
    target_name = safe_filename(save_as or default_name, _default_attachment_name(
        result["index"], result["mime"]))
    account_dir = safe_filename(settings.account, "account")
    try:
        root = (await tool_handlers.conversation_workspace_dir(conversation_id)).resolve()
        directory = root / ATTACHMENTS_DIR / account_dir
        for component in (root / ATTACHMENTS_DIR, directory):
            if component.is_symlink():
                raise ValueError("symlink in the destination path")
        directory.mkdir(parents=True, exist_ok=True)
        resolved_dir = directory.resolve()
        resolved_dir.relative_to(root)  # ValueError if a symlink escaped
        written = await asyncio.to_thread(
            _write_unique, resolved_dir, target_name, result["data"],
        )
    except ValueError:
        return _json({
            "error": "invalid_destination",
            "message": "The destination resolves outside the conversation workspace.",
        })
    except OSError as exc:
        return _json({
            "error": "save_failed",
            "message": f"Could not save the attachment to the workspace: {exc.strerror or exc}",
        })

    tool_handlers._publish_file_list_changed(
        ctx.user["id"], "conversation", conversation_id, getattr(ctx, "project_id", None),
    )
    rel = written.relative_to(root).as_posix()
    size = len(result["data"])
    return _json({
        "status": "success",
        "path": rel,
        "filename": written.name,
        "size_bytes": size,
        "content_type": result["mime"],
        "account": settings.account,
        "folder": folder,
        "uid": uid,
        "uidvalidity": result["uidvalidity"],
        "attachment_index": result["index"],
        "message": (
            f"Attachment saved to the workspace as '{rel}' ({size:,} bytes). "
            "Read it with get_workspace_file or process it with run_python."
        ),
    })


# -- tool specs -----------------------------------------------------------


def _intent_param(example: str) -> dict:
    return {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent "
            f"(max 50 characters). Example: '{example}'."
        ),
    }


_CONNECTION_PARAM = {
    "type": "string",
    "description": (
        "Account address of the IMAP connection to use when you have more than "
        "one (see Connected accounts in your instructions). Omit to use the default."
    ),
}

_FOLDER_PARAM = {
    "type": "string",
    "description": (
        "Folder (mailbox) name as returned by imap_list_folders, e.g. \"INBOX\" or "
        "\"Archive/2026\"."
    ),
}

_UIDVALIDITY_PARAM = {
    "type": "integer",
    "description": (
        "Optional UIDVALIDITY returned with the UIDs (by imap_search_messages or a "
        "message header). When it no longer matches the folder, the call fails with "
        "imap_uidvalidity_changed instead of addressing a different message."
    ),
}

_REQUIRES = "Requires an IMAP mailbox connected in Settings > Data Connections."

LIST_FOLDERS_TOOL = PluginTool(
    spec={
        "name": "imap_list_folders",
        "description": (
            "List the folders (mailboxes) of an IMAP account with message and unread "
            f"counts, flags, delimiter and special-use role (\\Sent, \\Trash, ...). Capped "
            f"at {MAX_FOLDERS} folders. {_REQUIRES}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connection": _CONNECTION_PARAM,
                "intent_message": _intent_param("List mail folders"),
            },
            "required": [],
        },
    },
    handler=_handle_list_folders,
    requires_service=SERVICE_ID,
)

SEARCH_MESSAGES_TOOL = PluginTool(
    spec={
        "name": "imap_search_messages",
        "description": (
            "Search one IMAP folder on the server and list the newest matching messages "
            "(uid, date, from, to, subject, size, flags, has_attachments), newest first, "
            "plus the folder's uidvalidity and total_matches. Criteria combine with AND; "
            "text criteria are case-insensitive substring matches on most servers. Fetch "
            f"full messages with imap_get_messages. Never marks messages read. {_REQUIRES}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connection": _CONNECTION_PARAM,
                "folder": {**_FOLDER_PARAM, "description": _FOLDER_PARAM["description"]
                           + " Default \"INBOX\"."},
                "from": {"type": "string", "description": "Text in the From header."},
                "to": {"type": "string", "description": "Text in the To header."},
                "subject": {"type": "string", "description": "Text in the Subject."},
                "text": {"type": "string",
                         "description": "Text anywhere in the headers or body."},
                "since": {"type": "string",
                          "description": "Only messages on or after this date (YYYY-MM-DD)."},
                "before": {"type": "string",
                           "description": "Only messages before this date (YYYY-MM-DD, exclusive)."},
                "unseen": {"type": "boolean",
                           "description": "true = only unread messages. Omit for read and unread."},
                "limit": {"type": "integer",
                          "description": f"Rows to return, 1-{MAX_SEARCH_LIMIT} "
                                         f"(default {DEFAULT_SEARCH_LIMIT})."},
                "intent_message": _intent_param("Find invoices from ACME"),
            },
            "required": [],
        },
    },
    handler=_handle_search_messages,
    requires_service=SERVICE_ID,
)

GET_MESSAGES_TOOL = PluginTool(
    spec={
        "name": "imap_get_messages",
        "description": (
            f"Fetch 1-{MAX_GET_UIDS} messages of one IMAP folder by UID, rendered as a "
            "markdown document (headers, body as markdown, numbered attachment list). "
            "Long URLs are replaced with (#N#) identifiers (resolve with "
            "imap_get_message_urls) unless include_urls=true. Never marks messages read. "
            f"Find UIDs with imap_search_messages. {_REQUIRES}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connection": _CONNECTION_PARAM,
                "folder": _FOLDER_PARAM,
                "uids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": f"Message UIDs in the folder (1-{MAX_GET_UIDS}).",
                },
                "uidvalidity": _UIDVALIDITY_PARAM,
                "include_html": {
                    "type": "boolean",
                    "description": "Show the raw HTML body in a fenced block instead of "
                                   "the markdown conversion (default false).",
                },
                "include_urls": {
                    "type": "boolean",
                    "description": "Keep full URLs inline instead of (#N#) identifiers "
                                   "(default false).",
                },
                "intent_message": _intent_param("Read the latest emails"),
            },
            "required": ["folder", "uids"],
        },
    },
    handler=_handle_get_messages,
    requires_service=SERVICE_ID,
)

GET_MESSAGE_URLS_TOOL = PluginTool(
    spec={
        "name": "imap_get_message_urls",
        "description": (
            "Look up the full URLs behind the (#N#) identifiers that imap_get_messages put "
            "into a message body in this conversation. Omit url_ids to get every mapping "
            "of the message. No server access."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connection": _CONNECTION_PARAM,
                "folder": _FOLDER_PARAM,
                "uid": {"type": "integer", "description": "The message UID."},
                "uidvalidity": {
                    "type": "integer",
                    "description": "Optional UIDVALIDITY of the rendering to look up; omit "
                                   "for the most recent rendering of this UID.",
                },
                "url_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Identifiers to resolve, e.g. [1, 2].",
                },
                "intent_message": _intent_param("Look up email links"),
            },
            "required": ["folder", "uid"],
        },
    },
    handler=_handle_get_message_urls,
    requires_service=SERVICE_ID,
)

SAVE_ATTACHMENT_TOOL = PluginTool(
    spec={
        "name": "imap_save_attachment",
        "description": (
            "Save one attachment of an IMAP message into this conversation's workspace "
            f"under '{ATTACHMENTS_DIR}/<account>/'. Pick it by attachment_index (from the "
            "'## Attachments' list of imap_get_messages) or by exact filename. "
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit. Never marks the message "
            "read. Read the file afterwards with get_workspace_file or run_python."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connection": _CONNECTION_PARAM,
                "folder": _FOLDER_PARAM,
                "uid": {"type": "integer", "description": "The message UID."},
                "uidvalidity": _UIDVALIDITY_PARAM,
                "attachment_index": {
                    "type": "integer",
                    "description": "1-based attachment_index from imap_get_messages.",
                },
                "filename": {
                    "type": "string",
                    "description": "Exact attachment filename (instead of attachment_index).",
                },
                "save_as": {
                    "type": "string",
                    "description": "Optional file name to save as (a name only, no "
                                   "folders). An existing file is never overwritten: a "
                                   "numeric suffix is added.",
                },
                "intent_message": _intent_param("Save invoice PDF"),
            },
            "required": ["folder", "uid"],
        },
    },
    handler=_handle_save_attachment,
    requires_service=SERVICE_ID,
)

ALL_TOOLS = (
    LIST_FOLDERS_TOOL,
    SEARCH_MESSAGES_TOOL,
    GET_MESSAGES_TOOL,
    GET_MESSAGE_URLS_TOOL,
    SAVE_ATTACHMENT_TOOL,
)

# Read-only tools reachable from sandbox scripts via POST /api/tool-call.
# Script calls carry no conversation context: bodies keep full URLs and
# the URL lookup reports missing context. imap_save_attachment is
# tool-only (it writes into the conversation workspace).
SCRIPT_TOOL_NAMES = frozenset({
    "imap_list_folders",
    "imap_search_messages",
    "imap_get_messages",
    "imap_get_message_urls",
})
