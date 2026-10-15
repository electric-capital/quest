"""Quest Tables history routes: change list, change detail, restore (spec
sections 4.5, 7 and 8.4, phase A16).

Same conventions as chat/tables/routes.py: under ``/app/api``,
authenticated by ``get_current_user_cookie_or_apikey_checked``, 403
``tables_disabled`` first, visibility through ``routes._get_table_for_ui``
(404 ``table_not_found`` with one body for a missing, hidden or malformed
id). Every route here is for EDITORS only (the ``ui`` rows verdict
``free``: the owner or a write-share recipient): right after visibility a
reader gets 403 ``forbidden`` ("History is available to people who can
edit this table."), before the query string or body is looked at --
before images can hold rows the owner deleted before sharing. Order: gate
-> 404 -> 403 -> the rest. The service re-checks the verdict under the
table lock.

Routes
------

- ``GET /tables/{id}/changes?limit=&before_id=`` -- newest first.
  ``limit`` 1..200 (default 50; else 400 ``invalid_request``),
  ``before_id`` a positive integer (the previous page's
  ``next_before_id``; else 400 ``invalid_cursor``). Returns ``{"changes":
  [Entry], "has_more", "next_before_id"}`` (``next_before_id`` = the last
  entry's id when ``has_more``, else null).
- ``GET /tables/{id}/changes/{change_id}`` -- one Entry plus the detail
  fields below; the change and its entry come from one locked read
  (``service.get_change_entry``). A malformed id and a missing one are the same 404
  ``unknown_change`` body shape (``{"detail": {"error": "unknown_change",
  "message": "There is no change #<id>."}}``).
- ``POST /tables/{id}/restore`` ``{"change_id": int}`` -- rebuild the table
  as it was right after that change (``service.apply_restore``, source
  ``revert:<change_id>``, actor the caller; the service publishes the
  events). 400 ``invalid_request`` for a missing or non-integer
  ``change_id``, 404 ``unknown_change``; edit access lost while the
  request waited for the lock is the same 403 ``forbidden``. Returns
  ``{"change": Entry (the new restore entry), "table": detail shape
  (routes._ui_detail), "schema_version", "row_count", "restored_to"}``.
  ``change`` is null when the caller stopped being an editor between the
  restore and the follow-up read of its entry (the restore stands).

Entry
-----

``{id, created_at, source, source_label, conversation, actor, kind, op,
summary, row_count, in_effect, restore_to, schema_before, schema_after,
size_bytes}``:

- ``source``: the raw write source for the OWNER only, null for anyone
  else (docs rule: non-owners never get raw sources and the owner's
  conversation ids), and null for the owner too when it is a
  ``conversation:<id>`` the owner cannot open (a write-share recipient's
  conversation).
- ``source_label`` (phrased for the caller): ``conversation:<id>`` -> that
  conversation's title when the CALLER owns it, else "a conversation" (a
  share recipient never learns the owner's conversation titles; a deleted
  conversation also reads "a conversation"); ``action_request:<id>`` ->
  "Request #<id>" for every editor (card numbers are not sensitive); ``ui:<user id>`` -> "you" for the caller, else the
  user's name (else email; "a user" once deleted); ``revert:<id>`` ->
  "Restored to #<id>"; anything else "unknown".
- ``conversation``: ``{id, title, project_id}`` when the source is a
  conversation the caller owns (the link hint), else null.
- ``actor``: ``{user_id, name}`` for the recorded ``actor_user_id`` (name
  as above, "a user" once deleted), null when none was recorded.
- ``row_count``: how many row ids the change records; ``in_effect``: the
  replay path to the newest entry applies it (spec 4.5); ``restore_to``:
  a restore entry's target id, else null.

Names are batched: per response one ``user_store.get_users_by_ids`` and
one ``conversation_store.get_conversations_meta`` (plus one thread hop for
the titles).

Detail fields
-------------

- ``columns``: ``[{key, name, type, multiple, options, precision,
  current}]`` to label cells and schema images. A schema change: the union
  of its before and after columns (after order, then the dropped ones). A
  rows or restore change: the table's current schema, plus every key of
  the images missing from it as ``{key, name: key, type: null, ...}``.
  ``current`` is true for a key in the table's current schema.
- ``before`` / ``after``: a ``rows`` change's row images (lists of
  ``{"_id", "_created_at", "_updated_at", <column KEY>: value}``, values
  converted to logical form with the table's current column definitions
  (a rows change records no schema): booleans true/false, multi-enums
  lists; an unknown key or an undecodable cell stays raw), each cut to the first
  ``TABLE_CHANGE_DETAIL_MAX_ROWS`` rows and ``TABLE_CHANGE_DETAIL_MAX_BYTES``
  of serialized JSON; a ``schema`` change's ``before`` ``{columns, title,
  dropped_values: {key: {"<_id>": value}}}`` (dropped values bounded the
  same way, per key, decoded with the before columns) and ``after``
  ``{columns, title, operations, backfill: {key: {"value"}}}`` (backfill
  values decoded with the after columns) (change #1: ``{op: "create_table", title, columns}``); a
  restore's ``{}`` / ``{"restore_to": id}``. Internal replay data
  (``ddl_columns``, ``rewrites``, index lists) is never sent.
- ``truncated``: ``{"before": bool, "after": bool}``; ``before_total`` /
  ``after_total``: the full image row counts of a rows change (null for
  other kinds).
- ``row_ids``: the first ``_DETAIL_MAX_ROW_IDS`` (1000) recorded ids;
  ``row_ids_total`` all of them.
- ``schema_diff``: ``schema_ops.schema_diff(before columns, after
  columns)`` for a schema change, else null; ``operations``: its
  normalized operations list (null for change #1 and other kinds).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.tables import service, storage
from chat.tables.access import TableAccess
from chat.tables.constants import (
    TABLE_CHANGE_DETAIL_MAX_BYTES,
    TABLE_CHANGE_DETAIL_MAX_ROWS,
)
from chat.tables.routes import (
    _get_table_for_ui,
    _http_error,
    _raise_table_error,
    _require_tables_enabled,
    _ui_detail,
)
from chat.tables.schema import Column, TableSchemaError, from_storage, load_schema
from chat.tables.schema_ops import schema_diff
from db import user_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["tables"],
)

CHANGES_DEFAULT_LIMIT = 50
CHANGES_MAX_LIMIT = 200  # = storage._LIST_CHANGES_MAX (the store clamps there)
_DETAIL_MAX_ROW_IDS = 1000

_INT_RE = re.compile(r"[0-9]{1,18}")
_SOURCE_RE = re.compile(r"(conversation|action_request|ui|revert):(.+)")

_SCHEMA_BEFORE_FIELDS = ("columns", "title", "dropped_values")
_SCHEMA_AFTER_FIELDS = ("op", "columns", "title", "operations", "backfill")
_COLUMN_LABEL_FIELDS = ("key", "name", "type", "multiple", "options", "precision")

_SERVICE_ERRORS = (service.TableError, storage.TableStorageError, TableSchemaError)


class RestoreRequest(BaseModel):
    # Loose so a malformed value is this API's own 400, not a 422.
    change_id: Optional[Any] = None


def _is_editor(access: TableAccess) -> bool:
    return access.rows == "free"


async def _editable_table(user: dict, table_id: str) -> tuple[dict, TableAccess]:
    """``(table, access)`` for an editor: 404 (one body), then 403."""
    table, access = await _get_table_for_ui(user, table_id)
    if not _is_editor(access):
        raise _http_error(403, "forbidden", service.HISTORY_FORBIDDEN_MESSAGE)
    return table, access


def _unknown_change(raw_id) -> Exception:
    return _http_error(404, "unknown_change", f"There is no change #{raw_id}.")


async def _service_call(table_id: str, coro):
    try:
        return await coro
    except _SERVICE_ERRORS as exc:
        _raise_table_error(exc, table_id)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _classify(source) -> tuple[str, Optional[str]]:
    if isinstance(source, str):
        match = _SOURCE_RE.fullmatch(source)
        if match:
            kind, ref = match.group(1), match.group(2)
            if kind != "conversation" and not _INT_RE.fullmatch(ref):
                return "unknown", None
            return kind, ref
    return "unknown", None


def _user_name(found: Optional[dict]) -> str:
    if not found:
        return "a user"
    name = (found.get("name") or "").strip()
    return name or (found.get("email") or "").strip() or "a user"


async def _label_entries(user: dict, table: dict, entries: list[dict]) -> list[dict]:
    """Entries (storage summary dicts with ``in_effect``) as API Entries for
    ``user`` (module docstring), with one user and one conversation lookup."""
    from chat.docs.history import _conversation_refs

    viewer_id = user["id"]
    is_owner = table["owner_id"] == viewer_id
    classified = [(entry, *_classify(entry.get("source"))) for entry in entries]

    user_ids = {
        int(ref) for _e, kind, ref in classified if kind == "ui" and int(ref) != viewer_id
    }
    user_ids |= {
        entry["actor_user_id"] for entry in entries
        if isinstance(entry.get("actor_user_id"), int)
    }
    users = await user_store.get_users_by_ids(user_ids) if user_ids else {}

    conversation_ids = {ref for _e, kind, ref in classified if kind == "conversation"}
    conversations = (
        await _conversation_refs(viewer_id, conversation_ids) if conversation_ids else {}
    )

    out = []
    for entry, kind, ref in classified:
        conversation = None
        if kind == "conversation":
            found = conversations.get(ref)
            conversation = dict(found) if found else None
            label = found["title"] if found else "a conversation"
        elif kind == "action_request":
            label = f"Request #{int(ref)}"
        elif kind == "ui":
            label = "you" if int(ref) == viewer_id else _user_name(users.get(int(ref)))
        elif kind == "revert":
            label = f"Restored to #{int(ref)}"
        else:
            label = "unknown"
        actor_id = entry.get("actor_user_id")
        actor = None
        if isinstance(actor_id, int):
            actor = {"user_id": actor_id, "name": _user_name(users.get(actor_id))}
        out.append({
            "id": entry["id"],
            "created_at": entry["created_at"],
            "source": (
                entry.get("source")
                if is_owner and (kind != "conversation" or conversation is not None)
                else None
            ),
            "source_label": label,
            "conversation": conversation,
            "actor": actor,
            "kind": entry["kind"],
            "op": entry["op"],
            "summary": entry["summary"],
            "row_count": entry.get("row_count"),
            "in_effect": entry.get("in_effect"),
            "restore_to": entry.get("restore_to"),
            "schema_before": entry["schema_before"],
            "schema_after": entry["schema_after"],
            "size_bytes": entry["size_bytes"],
        })
    return out


async def _restore_entry(user: dict, table_id: str, change_id: int) -> Optional[dict]:
    """The listed Entry of a restore entry just written, or None when it
    cannot be read: the log has no such id, or the caller stopped being an
    editor between the restore and this read (the restore itself stands)."""
    try:
        table, _access, page = await service.list_changes(
            service.caller_for_ui(user), table_id, limit=1, before_id=change_id + 1,
        )
    except service.TableForbidden:
        return None
    except _SERVICE_ERRORS as exc:
        _raise_table_error(exc, table_id)
    if not page or page[0]["id"] != change_id:
        return None
    return (await _label_entries(user, table, page))[0]


# ---------------------------------------------------------------------------
# Detail shaping
# ---------------------------------------------------------------------------


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _column_label(raw: dict, current_keys: set) -> dict:
    out = {field: raw.get(field) for field in _COLUMN_LABEL_FIELDS}
    out["multiple"] = bool(out["multiple"])
    out["current"] = raw.get("key") in current_keys
    return out


def _columns_by_key(raw_columns) -> dict[str, Column]:
    """Column objects of stored column dicts, skipping any that do not load."""
    out: dict[str, Column] = {}
    for raw in raw_columns or []:
        try:
            column = Column.from_dict(raw)
        except (TableSchemaError, TypeError, AttributeError, KeyError, ValueError):
            continue
        out[column.key] = column
    return out


def _logical(column: Optional[Column], stored):
    """``stored`` in logical form for ``column``; the raw value when the
    column is unknown or the value does not decode (a multi-enum cell
    that is not a JSON list of strings stays the raw string)."""
    if column is None:
        return stored
    try:
        value = from_storage(column, stored)
    except (TypeError, ValueError):
        return stored
    if column.type == "enum" and column.multiple and value is not None and not (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    ):
        return stored
    return value


def _logical_row(row: dict, columns: dict[str, Column]) -> dict:
    return {
        k: (v if k.startswith("_") else _logical(columns.get(k), v))
        for k, v in row.items()
    }


def _bounded_rows(rows, columns: dict[str, Column]) -> tuple[list, bool]:
    """The first rows that fit ``TABLE_CHANGE_DETAIL_MAX_ROWS`` and
    ``TABLE_CHANGE_DETAIL_MAX_BYTES`` (serialized), and whether any were cut."""
    if not isinstance(rows, list):
        return [], bool(rows)
    out: list = []
    size = 2
    for row in rows:
        if len(out) >= TABLE_CHANGE_DETAIL_MAX_ROWS:
            return out, True
        shaped = _logical_row(row, columns) if isinstance(row, dict) else row
        cost = _json_size(shaped) + 1
        if size + cost > TABLE_CHANGE_DETAIL_MAX_BYTES:
            return out, True
        out.append(shaped)
        size += cost
    return out, False


def _bounded_dropped(dropped, columns: dict[str, Column]) -> tuple[dict, bool]:
    """``dropped_values`` cut to ``TABLE_CHANGE_DETAIL_MAX_ROWS`` cells per
    column and ``TABLE_CHANGE_DETAIL_MAX_BYTES`` overall."""
    if not isinstance(dropped, dict):
        return {}, False
    out: dict = {}
    truncated = False
    size = 2
    for key, cells in dropped.items():
        if not isinstance(cells, dict):
            continue
        kept: dict = {}
        out[key] = kept
        ordered = sorted(cells.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
        for row_id, stored in ordered:
            if len(kept) >= TABLE_CHANGE_DETAIL_MAX_ROWS:
                truncated = True
                break
            value = _logical(columns.get(key), stored)
            cost = _json_size({row_id: value}) + len(key)
            if size + cost > TABLE_CHANGE_DETAIL_MAX_BYTES:
                truncated = True
                break
            kept[row_id] = value
            size += cost
    return out, truncated


def _image_keys(rows) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        for key in row:
            if not key.startswith("_") and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _schema_detail(change: dict, current_keys: set) -> dict:
    before = change["before"] if isinstance(change["before"], dict) else {}
    after = change["after"] if isinstance(change["after"], dict) else {}
    before_raw = before.get("columns") or []
    after_raw = after.get("columns") or []

    columns: list[dict] = []
    seen: set = set()
    for raw in [*after_raw, *before_raw]:
        if isinstance(raw, dict) and raw.get("key") not in seen:
            seen.add(raw.get("key"))
            columns.append(_column_label(raw, current_keys))

    dropped, dropped_cut = _bounded_dropped(
        before.get("dropped_values"), _columns_by_key(before_raw),
    )
    before_out = {f: before[f] for f in _SCHEMA_BEFORE_FIELDS if f in before}
    if "dropped_values" in before_out:
        before_out["dropped_values"] = dropped
    after_out = {f: after[f] for f in _SCHEMA_AFTER_FIELDS if f in after}
    if isinstance(after_out.get("backfill"), dict):
        after_columns = _columns_by_key(after_raw)
        after_out["backfill"] = {
            key: (
                {**fill, "value": _logical(after_columns.get(key), fill.get("value"))}
                if isinstance(fill, dict) else fill
            )
            for key, fill in after_out["backfill"].items()
        }

    try:
        diff = schema_diff(load_schema(before_raw), load_schema(after_raw))
    except (TableSchemaError, TypeError, AttributeError, KeyError, ValueError):
        logger.warning("[tables] change %s has unreadable columns", change.get("id"))
        diff = None
    return {
        "columns": columns,
        "before": before_out,
        "after": after_out,
        "truncated": {"before": dropped_cut, "after": False},
        "before_total": None,
        "after_total": None,
        "schema_diff": diff,
        "operations": after.get("operations"),
    }


def _rows_detail(change: dict, table: dict, current_keys: set) -> dict:
    current_raw = table.get("schema") or []
    columns = [_column_label(raw, current_keys) for raw in current_raw]
    known = _columns_by_key(current_raw)
    for key in _image_keys(change["before"]) + _image_keys(change["after"]):
        if key not in current_keys and all(c["key"] != key for c in columns):
            columns.append({
                "key": key, "name": key, "type": None, "multiple": False,
                "options": None, "precision": None, "current": False,
            })
    if change["kind"] == "restore":
        return {
            "columns": columns,
            "before": change["before"],
            "after": change["after"],
            "truncated": {"before": False, "after": False},
            "before_total": None,
            "after_total": None,
            "schema_diff": None,
            "operations": None,
        }
    before, before_cut = _bounded_rows(change["before"], known)
    after, after_cut = _bounded_rows(change["after"], known)
    return {
        "columns": columns,
        "before": before,
        "after": after,
        "truncated": {"before": before_cut, "after": after_cut},
        "before_total": len(change["before"]) if isinstance(change["before"], list) else 0,
        "after_total": len(change["after"]) if isinstance(change["after"], list) else 0,
        "schema_diff": None,
        "operations": None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tables/{table_id}/changes")
async def list_table_changes(
    table_id: str,
    limit: Optional[str] = Query(None),
    before_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """A page of the change log, newest first (module docstring)."""
    _require_tables_enabled(user)
    table, _access = await _editable_table(user, table_id)
    if limit is None or limit == "":
        page_size = CHANGES_DEFAULT_LIMIT
    else:
        raw = limit.strip()
        if not _INT_RE.fullmatch(raw) or not 1 <= int(raw) <= CHANGES_MAX_LIMIT:
            raise _http_error(
                400, "invalid_request",
                f"limit must be a whole number from 1 to {CHANGES_MAX_LIMIT}.",
            )
        page_size = int(raw)
    cursor = None
    if before_id is not None and before_id != "":
        raw = before_id.strip()
        if not _INT_RE.fullmatch(raw) or int(raw) < 1:
            raise _http_error(400, "invalid_cursor", "before_id must be a positive whole number.")
        cursor = int(raw)

    fetch = min(page_size + 1, CHANGES_MAX_LIMIT)
    fresh, _access, page = await _service_call(
        table_id,
        service.list_changes(
            service.caller_for_ui(user), table["id"], limit=fetch, before_id=cursor,
        ),
    )
    if len(page) > page_size:
        has_more = True
        page = page[:page_size]
    else:
        # At the store's clamp the extra row cannot be fetched; change ids
        # start at 1 (create_table) and are never removed, so an older
        # entry exists when the last id is above 1.
        has_more = len(page) == page_size == fetch and page[-1]["id"] > 1
    entries = await _label_entries(user, fresh, page)
    return {
        "changes": entries,
        "has_more": has_more,
        "next_before_id": entries[-1]["id"] if has_more and entries else None,
    }


@router.get("/tables/{table_id}/changes/{change_id}")
async def get_table_change(
    table_id: str,
    change_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One change with bounded images (module docstring, Detail fields)."""
    _require_tables_enabled(user)
    table, _access = await _editable_table(user, table_id)
    if not _INT_RE.fullmatch(change_id) or int(change_id) < 1:
        raise _unknown_change(change_id)
    cid = int(change_id)
    fresh, _access, change, raw_entry = await _service_call(
        table_id, service.get_change_entry(service.caller_for_ui(user), table["id"], cid),
    )
    (entry,) = await _label_entries(user, fresh, [raw_entry])
    current_keys = {raw.get("key") for raw in fresh.get("schema") or []}
    if change["kind"] == "schema":
        shaped = _schema_detail(change, current_keys)
    else:
        shaped = _rows_detail(change, fresh, current_keys)
    row_ids = change["row_ids"] if isinstance(change["row_ids"], list) else []
    return {
        **entry,
        **shaped,
        "row_ids": row_ids[:_DETAIL_MAX_ROW_IDS],
        "row_ids_total": len(row_ids),
    }


@router.post("/tables/{table_id}/restore")
async def restore_table(
    table_id: str,
    body: Optional[RestoreRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Restore the table to the state right after ``change_id`` (editors
    only; module docstring)."""
    _require_tables_enabled(user)
    table, _access = await _editable_table(user, table_id)
    change_id = body.change_id if body is not None else None
    if isinstance(change_id, bool) or not isinstance(change_id, int):
        raise _http_error(400, "invalid_request", "change_id must be an integer.")
    if change_id < 1:
        raise _unknown_change(change_id)
    uid = user["id"]
    try:
        result = await service.apply_restore(
            service.caller_for_ui(user), table["id"], change_id, actor_user_id=uid,
        )
    except service.TableDenied:
        # Edit access lost after the check above: the same 403 as up front.
        raise _http_error(403, "forbidden", service.HISTORY_FORBIDDEN_MESSAGE)
    except _SERVICE_ERRORS as exc:
        _raise_table_error(exc, table_id)
    entry = await _restore_entry(user, table["id"], result["change_id"])
    fresh, access = await _get_table_for_ui(user, table["id"])
    return {
        "change": entry,
        "table": await _ui_detail(user, fresh, access),
        "schema_version": result["schema_version"],
        "row_count": result["row_count"],
        "restored_to": result["restored_to"],
    }
