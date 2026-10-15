"""HTTP routes for Quest Tables: tables, schema, rows (spec section 7, phase A15).

Everything is under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` and refused with 403
``tables_disabled`` while the ``tables`` feature gate is closed for the user
(the first of each handler's own checks, before any DB work; FastAPI's
422 for an ill-typed body or query parameter -- e.g. a non-boolean
``require_approval`` -- comes earlier, before the handler runs, as in the
docs routes). History, download and
share routes (phase A16) reuse the helpers below (``_http_error``,
``_require_tables_enabled``, ``_get_table_for_ui``, ``_require_owner``,
``_ui_row`` / ``_ui_rows``, ``_stale_conflict``, ``_raise_table_error``,
``_download_filename``, ``_attachment_disposition``, ``_user_ref``).

Every read and write goes through ``chat.tables.service`` with a ``ui``
caller (``service.caller_for_ui``), so visibility is the one access rule
(``chat.tables.access.resolve_table_access`` with ``run_kind="ui"``): a
table the user cannot see is a 404 ``table_not_found`` whose body is
byte-equal to a nonexistent id's (``{"detail": {"error":
"table_not_found", "message": "Table not found: <id>"}}``; also for a
malformed id). Metadata edits, schema edits and delete are owner-only (403
``forbidden`` for a visible table the caller does not own, checked before
the request body is looked at); rows are readable by every viewer.

Routes
------

- ``GET /tables`` -- three keyset-paged streams like docs: the caller's OWN
  user tables (default), one of the caller's projects' tables
  (``project_id``; 404 ``project_not_found`` unless the caller owns the
  project), tables shared WITH the caller (``shared=true``, user and project
  tables; with ``project_id`` 400 ``invalid_request``). ``limit`` 1..200
  (422 outside; omitted = the whole stream, as docs), ``cursor`` =
  ``<updated_at>|<id>`` from ``next_cursor`` (400 ``invalid_cursor``).
  Returns ``{"tables": [list row], "has_more", "next_cursor"}``.
- ``POST /tables`` ``{title, description?, project_id?, columns}`` -> 201
  detail row. Change #1 "Created table" with source ``ui:<user_id>``.
- ``GET /tables/{id}`` -> detail row (+ ``last_write_conversation``).
- ``PUT /tables/{id}`` ``{title?, description?, require_approval?
  (strict bool), expected_updated_at?}`` (owner) -> detail row; flat 409
  ``{"error": "stale_update", "message", "current": list row}``.
- ``PUT /tables/{id}/schema`` ``{operations, expected_schema_version}``
  (owner) -> detail row + ``change_id``.
- ``GET /tables/{id}/rows`` ``?filter=<json>&sort=<json>&limit=&offset=
  &count_only=`` -> ``{"rows": [{"_id", "_updated_at", <column KEY>:
  value}], "total", "limit", "offset", "truncated", "schema_version"}``.
  ``limit`` defaults to 100 and is REFUSED above ``TABLE_GRID_MAX_LIMIT``
  (200) with 400 ``invalid_limit`` (not clamped). ``truncated`` is true
  when more rows match past this page (``offset + len(rows) < total``);
  rows are never cut for size. With ``count_only=true`` ``rows`` is ``[]``
  and ``truncated`` false.
- ``DELETE /tables/{id}`` (owner) -> 204; waits for the table lock.

Row shape (:func:`_ui_row`)
---------------------------

The store's table dict WITHOUT ``schema`` and ``shares`` (``id``,
``owner_id``, ``project_id``, ``title``, ``description``, ``mode``,
``schema_version``, ``row_count``, ``last_write_source``,
``require_approval``, ``created_at``, ``updated_at``) plus

- ``scope`` (``user`` / ``project``), ``column_count`` (len of the schema),
  ``shared`` (has any share row), ``shared_with_me`` (caller is not the
  owner), ``permission`` (the caller's effective share ``read`` /
  ``write``; null for the owner);
- ``owner {id, name, email}`` for non-owners (null for the owner);
- ``shares``: owner only (null for non-owners), each share dict plus
  ``user {id, name, email}`` (null for the everyone row);
- ``last_write_user {id, name, email}`` owner only, docs rules: the user of
  a ``ui:<id>`` source, or the proposer of an ``action_request:<id>``
  source when that is not the owner (a write-share recipient); null
  otherwise. ``last_write_source`` is null for non-owners;
- ``access``: ``{can_read, rows, schema (the raw UI verdicts), can_edit
  and can_restore (rows == "free": owner or write share), can_rename,
  can_delete, can_share, can_require_approval (owner), can_edit_schema
  (schema != "denied", i.e. owner), can_download_history (= can_edit),
  deny_reason, schema_deny_reason}``.

The detail shape (GET/POST/PUT one table) is the row plus ``schema``: the
full stored column list (``key``, ``name``, ``type``, ``required``,
``unique``, ``default``, ``options``, ``multiple``, ``precision``,
``description``). ``PUT /schema`` adds ``change_id``.

Errors
------

``HTTPException(detail={"error": code, "message": ..., **extra})`` (body
``{"detail": {...}}``), except the flat 409 ``stale_update``. Status per
code in :data:`_ERROR_STATUS` (:func:`_raise_table_error`): 403
``tables_disabled`` / ``forbidden`` / ``denied``; 404 ``table_not_found``
/ ``project_not_found`` / ``unknown_change`` / ``user_not_found`` /
``share_not_found``; 409 ``duplicate_title`` / ``stale_schema`` /
``rows_changed`` / ``schema_mismatch`` (quest.db and table.db disagreed;
the service heals a lost bump, so a reload + retry works); 503 ``busy``;
500 ``corrupt_storage`` and the other storage-integrity codes; 400
everything else (``invalid_request``, ``invalid_args``, ``table_limit``,
``change_too_large``, ``rows_violate``, column-spec / schema-operation /
filter codes...). Extras inside ``detail``: ``stale_schema`` ->
``schema_version`` + ``schema`` of the current table; ``rows_violate`` ->
``column``, ``check``, ``option``, ``row_ids``, ``total``; schema
operation errors -> ``op_index``, ``op``, ``column``, ``field``; column
spec errors -> ``column``, ``field``; filter errors -> ``path``;
``invalid_rows`` -> ``problems``; ``rows_changed`` -> ``vanished_ids``.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.tables import filters, service, storage
from chat.tables.access import TableAccess, effective_share, resolve_table_access
from chat.tables.constants import (
    TABLE_GRID_MAX_LIMIT,
    table_not_found_message,
    tables_disabled_message,
)
from chat.tables.schema import TableSchemaError
from chat.tables.schema_ops import SchemaOperationError
from config.feature_gates import tables_enabled_for
from db import action_request_store, project_store, table_store, user_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["tables"],
)

# Error code -> HTTP status; anything absent is a 400.
_ERROR_STATUS = {
    "tables_disabled": 403,
    "forbidden": 403,
    "denied": 403,
    "table_not_found": 404,
    "project_not_found": 404,
    "unknown_change": 404,
    "user_not_found": 404,
    "share_not_found": 404,
    "duplicate_title": 409,
    "stale_schema": 409,
    "stale_update": 409,
    "rows_changed": 409,
    "schema_mismatch": 409,
    "busy": 503,
    "corrupt_storage": 500,
    "unsafe_path": 500,
    "storage_missing": 500,
    "storage_exists": 500,
    "copy_exists": 500,
}

_FILENAME_UNSAFE_RE = re.compile(r'[/\\:*?"<>|]+')
_FILENAME_MAX_LEN = 120

_UI_SOURCE_RE = re.compile(r"ui:([0-9]{1,18})")
_ACTION_REQUEST_SOURCE_RE = re.compile(r"action_request:([0-9]{1,18})")
_INT_PARAM_RE = re.compile(r"-?[0-9]{1,18}")


class CreateTableRequest(BaseModel):
    # Loose so malformed values get this API's own 400 codes (the service
    # validates title / description / columns).
    title: Optional[Any] = None
    description: Optional[Any] = None
    project_id: Optional[Any] = None
    columns: Optional[Any] = None


class UpdateTableRequest(BaseModel):
    title: Optional[Any] = None
    description: Optional[Any] = None
    # The owner's require-approval switch; strict so a string never flips it.
    require_approval: Optional[StrictBool] = None
    expected_updated_at: Optional[str] = None


class SchemaChangeRequest(BaseModel):
    # Loose: the service validates both (400 invalid_args /
    # SchemaOperationError codes).
    operations: Optional[Any] = None
    expected_schema_version: Optional[Any] = None


# ---------------------------------------------------------------------------
# Helpers (A16 imports these)
# ---------------------------------------------------------------------------


def _http_error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"error": code, "message": message, **extra},
    )


def _require_tables_enabled(user: dict) -> None:
    """403 ``tables_disabled`` unless the tables gate is open for this user."""
    if not tables_enabled_for(user.get("email") or ""):
        raise _http_error(403, "tables_disabled", tables_disabled_message())


def _ui_access(user: dict, table: dict) -> TableAccess:
    return resolve_table_access(
        table, user_id=user["id"], is_public=False, project_id=None, run_kind="ui",
    )


def _table_not_found(table_id: str) -> HTTPException:
    # The one body for a missing, hidden or malformed id (invariant 2).
    return _http_error(404, "table_not_found", table_not_found_message(table_id))


def _error_extra(exc: Exception) -> dict:
    """Machine-readable extras of an error, carried inside ``detail``."""
    if isinstance(exc, storage.TableStorageError) and exc.code == "rows_violate":
        return {
            "column": getattr(exc, "column", None),
            "check": getattr(exc, "check", None),
            "option": getattr(exc, "option", None),
            "row_ids": list(getattr(exc, "row_ids", []) or []),
            "total": getattr(exc, "total", None),
        }
    if isinstance(exc, storage.InvalidRowsError):
        return {"problems": exc.problems}
    if isinstance(exc, SchemaOperationError):
        return {"op_index": exc.op_index, "op": exc.op,
                "column": exc.column, "field": exc.field}
    if isinstance(exc, TableSchemaError):
        return {"column": exc.column, "field": exc.field}
    if isinstance(exc, filters.FilterError):
        return {"path": exc.path}
    if isinstance(exc, service.StaleSchema):
        return {"schema_version": exc.current.get("schema_version"),
                "schema": exc.current.get("schema")}
    if isinstance(exc, service.RowsChanged):
        return {"vanished_ids": list(exc.vanished_ids)}
    return {}


def _raise_table_error(exc: Exception, table_id: Optional[str] = None):
    """Raise the HTTPException for a service-layer refusal.

    Handles :class:`service.TableError` (and subclasses),
    :class:`storage.TableStorageError` (incl. revert refusals),
    :class:`filters.FilterError` and :class:`TableSchemaError`; anything
    else is re-raised unchanged (a 500). ``table_not_found`` always gets
    the byte-equal not-found body for ``table_id``. ``StaleUpdate`` should
    be answered with :func:`_stale_conflict` by the caller (it needs the
    user); here it falls back to a ``detail`` 409.
    """
    if isinstance(exc, filters.FilterError):
        raise HTTPException(status_code=400, detail=exc.to_payload())
    if not isinstance(exc, (service.TableError, storage.TableStorageError, TableSchemaError)):
        raise exc
    code = exc.code
    if code == "table_not_found":
        raise _table_not_found(table_id if table_id is not None else getattr(exc, "table_id", ""))
    status = _ERROR_STATUS.get(code, 400)
    if status == 500:
        logger.warning("[tables] storage error on %s: %s", table_id, code, exc_info=exc)
    raise _http_error(status, code, str(exc), **_error_extra(exc))


async def _get_table_for_ui(user: dict, table_id: str) -> tuple[dict, TableAccess]:
    """``(table with shares, access)`` for a table the user may see, else
    404 ``table_not_found`` (missing, hidden and malformed ids alike)."""
    try:
        return await service.get_visible_table(service.caller_for_ui(user), table_id)
    except service.TableDisabled:
        raise _http_error(403, "tables_disabled", tables_disabled_message())
    except service.TableError:
        raise _table_not_found(table_id)


def _require_owner(user: dict, table: dict, action: str) -> None:
    if table["owner_id"] != user["id"]:
        raise _http_error(403, "forbidden", f"Only the table's owner can {action}.")


def _source_id(pattern: re.Pattern, source) -> Optional[int]:
    if not isinstance(source, str):
        return None
    match = pattern.fullmatch(source)
    return int(match.group(1)) if match else None


def _ui_source_user_id(source) -> Optional[int]:
    return _source_id(_UI_SOURCE_RE, source)


def _last_writer_id(table: dict, card_owners: dict) -> Optional[int]:
    """Who wrote the table's current state, for the owner's
    ``last_write_user``: the ``ui:<id>`` user, or the proposer of an
    ``action_request:<id>`` card when that is not the owner (docs rule)."""
    source = table.get("last_write_source")
    writer = _ui_source_user_id(source)
    if writer is not None:
        return writer
    request_id = _source_id(_ACTION_REQUEST_SOURCE_RE, source)
    if request_id is not None:
        proposer = card_owners.get(request_id)
        if proposer is not None and proposer != table["owner_id"]:
            return proposer
    return None


def _row_user_ids(user: dict, table: dict, card_owners: dict) -> set[int]:
    if table["owner_id"] != user["id"]:
        return {table["owner_id"]}
    ids = {
        share["user_id"] for share in table.get("shares") or []
        if share.get("user_id") is not None
    }
    writer = _last_writer_id(table, card_owners)
    if writer is not None:
        ids.add(writer)
    return ids


def _user_ref(users: dict, user_id: int) -> dict:
    """``{id, name, email}``; a user missing from the lookup keeps its id."""
    found = users.get(user_id)
    if found is not None:
        return {"id": found["id"], "name": found.get("name"), "email": found.get("email")}
    return {"id": user_id, "name": None, "email": None}


def _table_row(
    user: dict, table: dict, access: TableAccess, users: dict, card_owners: dict,
) -> dict:
    """The UI list row (module docstring) for ``user``."""
    is_owner = table["owner_id"] == user["id"]
    shares = table.get("shares") or []
    row = {k: v for k, v in table.items() if k not in ("shares", "schema")}
    row["scope"] = "project" if table.get("project_id") else "user"
    row["column_count"] = len(table.get("schema") or [])
    row["shared"] = bool(shares)
    row["shared_with_me"] = not is_owner
    row["permission"] = None if is_owner else effective_share(table, user["id"])
    if is_owner:
        row["owner"] = None
        row["shares"] = [
            {
                **share,
                "user": (
                    None if share.get("user_id") is None
                    else _user_ref(users, share["user_id"])
                ),
            }
            for share in shares
        ]
        writer = _last_writer_id(table, card_owners)
        if writer is None:
            row["last_write_user"] = None
        elif writer in users or _ui_source_user_id(table.get("last_write_source")) is None:
            row["last_write_user"] = _user_ref(users, writer)
        else:
            row["last_write_user"] = None
    else:
        row["owner"] = _user_ref(users, table["owner_id"])
        row["shares"] = None
        row["last_write_source"] = None
        row["last_write_user"] = None
    can_edit = access.rows == "free"
    row["access"] = {
        "can_read": access.can_read,
        "rows": access.rows,
        "schema": access.schema,
        "can_edit": can_edit,
        "can_restore": can_edit,
        "can_rename": is_owner,
        "can_delete": is_owner,
        "can_share": is_owner,
        "can_edit_schema": is_owner and access.schema != "denied",
        "can_require_approval": is_owner,
        "can_download_history": can_edit,
        "deny_reason": access.deny_reason,
        "schema_deny_reason": access.schema_deny_reason,
    }
    return row


async def _ui_rows(
    user: dict,
    pairs: list[tuple[dict, Optional[TableAccess]]],
    known_users: Optional[dict] = None,
) -> list[dict]:
    """:func:`_table_row` for many tables: at most one
    ``get_action_request_owners`` and one ``get_users_by_ids`` call.
    ``access`` None = the UI verdict for ``user``."""
    request_ids = {
        request_id
        for table, _access in pairs
        if table["owner_id"] == user["id"]
        and (request_id := _source_id(
            _ACTION_REQUEST_SOURCE_RE, table.get("last_write_source"),
        )) is not None
    }
    card_owners = (
        await action_request_store.get_action_request_owners(request_ids)
        if request_ids else {}
    )
    users = dict(known_users or {})
    ids: set[int] = set()
    for table, _access in pairs:
        ids |= _row_user_ids(user, table, card_owners)
    missing = ids - users.keys()
    if missing:
        users.update(await user_store.get_users_by_ids(missing))
    return [
        _table_row(
            user, table, access if access is not None else _ui_access(user, table),
            users, card_owners,
        )
        for table, access in pairs
    ]


async def _ui_row(user: dict, table: dict, access: Optional[TableAccess] = None) -> dict:
    """The list row for one table (``table`` with shares)."""
    return (await _ui_rows(user, [(table, access)]))[0]


async def _ui_detail(user: dict, table: dict, access: Optional[TableAccess] = None) -> dict:
    """The detail shape: :func:`_ui_row` plus the full ``schema``."""
    row = await _ui_row(user, table, access)
    row["schema"] = table.get("schema") or []
    return row


async def _stale_conflict(
    user: dict, current: dict, message: Optional[str] = None,
) -> JSONResponse:
    """The flat 409 ``stale_update`` body carrying the current row;
    ``message`` defaults to the :class:`service.StaleUpdate` text."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "stale_update",
            "message": message or service.StaleUpdate({}).message,
            "current": await _ui_row(user, current),
        },
    )


def _download_filename(title: str, ext: str) -> str:
    """A file name derived from the table title, safe for any file system."""
    name = unicodedata.normalize("NFC", title or "")
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = _FILENAME_UNSAFE_RE.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:_FILENAME_MAX_LEN].rstrip(" .-") or "table"
    return f"{name}.{ext}"


def _attachment_disposition(filename: str) -> str:
    """``attachment`` Content-Disposition: ASCII fallback + RFC 5987 UTF-8."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a ``<updated_at_iso>|<table_id>`` keyset cursor (400 on garbage)."""
    ts_part, sep, id_part = cursor.partition("|")
    if not sep or not id_part:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    return ts, id_part


async def _visible_tables_page(
    user: dict,
    project_id: Optional[str],
    limit: Optional[int],
    before: Optional[tuple[datetime, str]],
    *,
    shared: bool = False,
) -> tuple[list[tuple[dict, TableAccess]], bool, dict]:
    """Visible tables of one stream, newest-updated first, ``has_more``
    and the owners looked up on the way (for ``_ui_rows``' known_users).
    Gathers ``limit + 1`` visible rows, paging the store past hidden ones."""
    want = None if limit is None else limit + 1
    owners: dict = {}
    out: list[tuple[dict, TableAccess]] = []
    while True:
        if shared:
            rows = await table_store.list_tables_shared_with(
                user["id"], limit=want, before=before,
            )
        else:
            rows = await table_store.list_accessible_tables(
                user["id"],
                project_id=project_id,
                include_user_tables=project_id is None,
                include_project_tables=project_id is not None,
                owned_only=project_id is None,
                limit=want,
                before=before,
            )
        missing = {
            t["owner_id"] for t in rows if t["owner_id"] != user["id"]
        } - owners.keys()
        if missing:
            owners.update(await user_store.get_users_by_ids(missing))
        for table in rows:
            access = _ui_access(user, table)
            if not access.visible:
                continue
            out.append((table, access))
            if want is not None and len(out) >= want:
                break
        if want is None or len(out) >= want or len(rows) < want:
            break
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])
    has_more = limit is not None and len(out) > limit
    if has_more:
        out = out[:limit]
    return out, has_more, owners


def _parse_json_param(raw: Optional[str], code: str, path: str) -> Any:
    """A JSON query parameter (None when absent or empty); bad JSON is a
    400 in the FilterError payload shape."""
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=filters.FilterError(code, f"{path} is not valid JSON.", path).to_payload(),
        )


def _parse_int_param(raw: Optional[str], code: str, path: str) -> Optional[int]:
    if raw is None or raw == "":
        return None
    # ASCII digits only (int() would take "１２", "1_0", " 7 "); a leading
    # "-" parses so a negative offset gets check_page's range error.
    if raw.isascii() and _INT_PARAM_RE.fullmatch(raw):
        return int(raw, 10)
    raise HTTPException(
            status_code=400,
        detail=filters.FilterError(code, f"{path} must be a whole number.", path).to_payload(),
    )


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@router.get("/tables")
async def list_ui_tables(
    project_id: Optional[str] = Query(None),
    shared: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List tables, newest-updated first (module docstring, ``GET /tables``)."""
    _require_tables_enabled(user)
    project_id = project_id or None
    if shared and project_id is not None:
        raise _http_error(
            400, "invalid_request",
            "shared=true lists tables shared with you across all projects; "
            "it cannot be combined with project_id.",
        )
    if project_id is not None:
        if await project_store.get_project(user["id"], project_id) is None:
            raise _http_error(404, "project_not_found", "Project not found.")
    before = _parse_cursor(cursor) if cursor else None
    page, has_more, owners = await _visible_tables_page(
        user, project_id, limit, before, shared=shared,
    )
    rows = await _ui_rows(user, page, known_users=owners)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last['updated_at']}|{last['id']}"
    return {"tables": rows, "has_more": has_more, "next_cursor": next_cursor}


@router.post("/tables", status_code=201)
async def create_ui_table(
    body: CreateTableRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a table owned by the user (a user table, or a table in one of
    the user's projects, taking the project's mode). 400 column-spec codes
    (with ``column`` / ``field``), ``invalid_request``, ``table_limit``; 404
    ``project_not_found``; 409 ``duplicate_title``. Returns the detail (201)."""
    _require_tables_enabled(user)
    project_id = None if body.project_id in (None, "") else body.project_id
    if project_id is not None:
        if not isinstance(project_id, str):
            raise _http_error(400, "invalid_request", "project_id must be a string.")
        if await project_store.get_project(user["id"], project_id) is None:
            raise _http_error(404, "project_not_found", "Project not found.")
    uid = user["id"]
    try:
        table = await service.create_table(
            service.caller_for_ui(user),
            title=body.title,
            description=body.description if body.description is not None else "",
            project_id=project_id,
            columns=body.columns,
            write_source=f"ui:{uid}",
            actor_user_id=uid,
        )
    except (service.TableError, storage.TableStorageError, TableSchemaError) as exc:
        _raise_table_error(exc)
    return await _ui_detail(user, table)


# ---------------------------------------------------------------------------
# One table
# ---------------------------------------------------------------------------


@router.get("/tables/{table_id}")
async def get_ui_table(
    table_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The detail shape plus ``last_write_conversation`` (``{id, title,
    project_id}`` of a ``conversation:<id>`` last write the caller can see,
    else null)."""
    _require_tables_enabled(user)
    table, access = await _get_table_for_ui(user, table_id)
    row = await _ui_detail(user, table, access)
    # Shared with the docs viewer footer (generic over a row's source).
    from chat.docs.routes import _last_write_conversation

    row["last_write_conversation"] = await _last_write_conversation(user, row)
    return row


@router.put("/tables/{table_id}")
async def update_ui_table(
    table_id: str,
    body: Optional[UpdateTableRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Rename / describe / flip ``require_approval`` (owner only). Flat 409
    ``stale_update`` with ``current``; 409 ``duplicate_title``; 400
    ``invalid_request``. Returns the detail."""
    _require_tables_enabled(user)
    table, _access = await _get_table_for_ui(user, table_id)
    _require_owner(user, table, "change its settings")
    body = body or UpdateTableRequest()
    if body.title is not None and not isinstance(body.title, str):
        raise _http_error(400, "invalid_request", "title must be a string.")
    if body.description is not None and not isinstance(body.description, str):
        raise _http_error(400, "invalid_request", "description must be a string.")
    try:
        updated = await service.update_metadata(
            service.caller_for_ui(user),
            table["id"],
            title=body.title,
            description=body.description,
            require_approval=body.require_approval,
            expected_updated_at=body.expected_updated_at,
        )
    except service.StaleUpdate as exc:
        return await _stale_conflict(user, exc.current, exc.message)
    except (service.TableError, storage.TableStorageError, TableSchemaError) as exc:
        _raise_table_error(exc, table_id)
    return await _ui_detail(user, updated)


@router.put("/tables/{table_id}/schema")
async def change_ui_table_schema(
    table_id: str,
    body: Optional[SchemaChangeRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Apply a list of schema operations as one change (owner only).

    409 ``stale_schema`` (``detail.schema_version`` / ``detail.schema``
    = the current table's); 400 schema-operation codes with ``op_index``;
    400 ``rows_violate`` with ``column`` / ``check`` / ``option`` /
    ``row_ids`` / ``total``; 409 ``duplicate_title`` (``rename_table``);
    400 ``change_too_large``; 409 ``schema_mismatch``; 503 ``busy``.
    Returns the detail plus ``change_id``; the detail is re-fetched after
    the change released the table lock, so it may already reflect a later
    change (its ``schema_version`` can be past the one this change made).
    """
    _require_tables_enabled(user)
    table, _access = await _get_table_for_ui(user, table_id)
    _require_owner(user, table, "change its columns")
    body = body or SchemaChangeRequest()
    caller = service.caller_for_ui(user)
    uid = user["id"]
    try:
        result = await service.apply_schema_change(
            caller,
            table["id"],
            body.operations,
            expected_schema_version=body.expected_schema_version,
            write_source=f"ui:{uid}",
            actor_user_id=uid,
        )
    except (service.TableError, storage.TableStorageError, TableSchemaError) as exc:
        _raise_table_error(exc, table_id)
    fresh, access = await _get_table_for_ui(user, table_id)
    row = await _ui_detail(user, fresh, access)
    row["change_id"] = result["change_id"]
    return row


@router.get("/tables/{table_id}/rows")
async def query_ui_table_rows(
    table_id: str,
    filter: Optional[str] = Query(None),  # noqa: A002 - the DSL's own name
    sort: Optional[str] = Query(None),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
    count_only: bool = Query(False),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One page of rows, cells keyed by column KEY (module docstring).

    ``filter`` / ``sort`` are the query_rows DSL as JSON; bad JSON is 400
    ``invalid_filter`` / ``invalid_sort`` and every DSL refusal is 400 with
    the FilterError payload ``{error, message, path}`` as ``detail``.
    """
    _require_tables_enabled(user)
    filter_value = _parse_json_param(filter, "invalid_filter", "filter")
    sort_value = _parse_json_param(sort, "invalid_sort", "sort")
    limit_value = _parse_int_param(limit, "invalid_limit", "limit")
    offset_value = _parse_int_param(offset, "invalid_offset", "offset")
    if not table_id.strip():
        # query_rows refuses a blank id with invalid_args; keep the 404
        # parity body of every other per-table route.
        raise _table_not_found(table_id)
    try:
        table, _access, result = await service.query_rows(
            service.caller_for_ui(user),
            table_id,
            filter=filter_value,
            sort=sort_value,
            limit=limit_value,
            offset=offset_value,
            count_only=count_only,
            max_limit=TABLE_GRID_MAX_LIMIT,
        )
    except (service.TableError, storage.TableStorageError, TableSchemaError,
            filters.FilterError) as exc:
        _raise_table_error(exc, table_id)
    keys = {col["name"]: col["key"] for col in table.get("schema") or []}
    rows = []
    for item in result.rows:
        out = {"_id": item["_id"], "_updated_at": item["_updated_at"]}
        for name, value in item.items():
            if name in ("_id", "_updated_at"):
                continue
            out[keys.get(name, name)] = value
        rows.append(out)
    return {
        "rows": rows,
        "total": result.total,
        "limit": result.limit,
        "offset": result.offset,
        "truncated": (not count_only) and result.offset + len(rows) < result.total,
        "schema_version": table["schema_version"],
    }


@router.delete("/tables/{table_id}", status_code=204)
async def delete_ui_table(
    table_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a table (owner only) under its lock: the row (shares go with
    it), the directory, then ``table_changed {updated_at: null}`` and
    ``table_list_changed`` to the audience. 204."""
    _require_tables_enabled(user)
    table, _access = await _get_table_for_ui(user, table_id)
    _require_owner(user, table, "delete it")
    try:
        await service.delete_table(service.caller_for_ui(user), table["id"])
    except (service.TableError, storage.TableStorageError) as exc:
        _raise_table_error(exc, table_id)
    return Response(status_code=204)
