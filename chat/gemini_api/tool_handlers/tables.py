"""Quest Tables tool handlers: thin wrappers over chat/tables/service.py.

Each handler takes the dispatch-built :class:`chat.tables.service.Caller`
and the raw tool arguments, coerces loosely-typed arguments (``"10"``,
``"true"``, a ``filter`` / ``sort`` / ``columns`` sent as a JSON string),
calls the service and returns a JSON string. Nothing here opens a
``table.db`` or calls the storage / filter layers directly: every read goes
through the service, which holds the per-table lock.

Every error is ``{"error": <code>, "message": <text>, ...}``:

- ``tables_disabled`` -- the ``tables`` feature gate is closed for the user
  (checked before anything else, no DB work);
- ``table_not_found`` -- a missing id AND a table this conversation may not
  see, with byte-equal text (spec invariant 2);
- other :class:`chat.tables.service.TableError` codes (``invalid_args``,
  ``denied`` ...), with ``suggested_request`` added for
  ``approval_required``;
- :class:`chat.tables.filters.FilterError` -- its payload, which adds
  ``path`` (the offending filter / sort / columns node);
- storage and schema refusals -- their code and message plus their
  structured fields (``problems``, ``row_ids``, ``cap`` ...). A column's
  internal ``key`` never appears in any tool output.
"""

import json
from typing import Any, Awaitable, Callable, Optional

from chat.tables import access as table_access
from chat.tables import service
from chat.tables.constants import TABLE_QUERY_MAX_BYTES
from chat.tables.filters import FilterError
from chat.tables.schema import Column, TableSchemaError, load_schema
from chat.tables.service import Caller, TableApprovalRequired, TableError
from chat.tables.storage import TableStorageError

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
SAMPLE_ROWS = 5

# Structured fields copied from a storage / schema refusal into its error
# payload when set. ``column_key`` (RowsViolateError) is deliberately
# absent: column keys are never shown to models.
_ERROR_FIELDS = (
    "problems", "count", "cap", "row_count", "adding", "row_ids", "total",
    "column", "check", "option", "field",
)


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


def _invalid(message: str) -> TableError:
    return TableError("invalid_args", message)


def _int_arg(args: dict, key: str, default=None):
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        raise _invalid(f"{key} must be an integer.")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            pass
    raise _invalid(f"{key} must be an integer.")


def _bool_arg(args: dict, key: str, default: bool) -> bool:
    raw = args.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    if isinstance(raw, int):
        return bool(raw)
    raise _invalid(f"{key} must be a boolean.")


def _json_arg(args: dict, key: str):
    """A structured argument (object or list) that a model may have sent
    as a JSON string; an empty string means omitted. The parsed value is
    validated by the service (filter DSL, sort keys, column names). A
    bare non-JSON ``columns`` string is taken as one column name."""
    raw = args.get(key)
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            if key == "columns":
                return [raw.strip()]  # one bare column name
            raise _invalid(f"{key} must be JSON (an object or a list), not a plain string.")
    return raw


def _str_arg(args: dict, key: str, default: Optional[str] = None) -> Optional[str]:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if not isinstance(raw, str):
        raise _invalid(f"{key} must be a string.")
    return raw.strip()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _error_payload(exc: Exception) -> dict:
    payload = {"error": exc.code, "message": str(exc)}
    for field in _ERROR_FIELDS:
        value = getattr(exc, field, None)
        if value is not None:
            payload[field] = value
    return payload


async def _run(caller: Caller, op: Callable[[], Awaitable[Any]]) -> str:
    """Gate first, then the operation; map every refusal to an error JSON."""
    try:
        service.require_enabled(caller)
        result = await op()
    except TableApprovalRequired as exc:
        return json.dumps({
            "error": exc.code,
            "message": str(exc),
            "suggested_request": exc.suggested_request,
        })
    except TableError as exc:  # incl. TableDisabled, TableNotFound
        return json.dumps({"error": exc.code, "message": str(exc)})
    except FilterError as exc:
        return json.dumps(exc.to_payload())
    except (TableStorageError, TableSchemaError) as exc:
        return json.dumps(_error_payload(exc), default=str)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def _column_label(column: Column) -> str:
    """``Name:type``; a multi-choice enum is ``Name:enum[]``."""
    kind = "enum[]" if column.type == "enum" and column.multiple else column.type
    return f"{column.name}:{kind}"


def _column_info(column: Column) -> dict:
    """Every column field except the internal ``key``, in schema order."""
    data = column.to_dict()
    data.pop("key", None)
    return data


def _scope(table: dict) -> str:
    return "project" if table.get("project_id") else "user"


def _summary(table: dict, verdict: table_access.TableAccess, columns) -> dict:
    return {
        "id": table["id"],
        "title": table["title"],
        "description": table.get("description") or "",
        "mode": table["mode"],
        "scope": _scope(table),
        "project_id": table.get("project_id"),
        "row_count": table["row_count"],
        "column_count": len(columns),
        "columns": [_column_label(c) for c in columns],
        "updated_at": table["updated_at"],
        "writable": verdict.rows,
        "write_note": table_access.write_note(verdict),
        "schema_change": verdict.schema,
    }


def _detail(table: dict, verdict: table_access.TableAccess, columns, sample: list) -> dict:
    return {
        "id": table["id"],
        "title": table["title"],
        "description": table.get("description") or "",
        "mode": table["mode"],
        "scope": _scope(table),
        "project_id": table.get("project_id"),
        "columns": [_column_info(c) for c in columns],
        "row_count": table["row_count"],
        "schema_version": table["schema_version"],
        "updated_at": table["updated_at"],
        "writable": verdict.rows,
        "write_note": table_access.write_note(verdict),
        "schema_change": verdict.schema,
        "schema_note": table_access.schema_note(verdict),
        "sample_rows": sample,
    }


def _fit_rows(rows: list, total: int, offset: int, limit: int) -> dict:
    """The query_rows payload, halving the rows until the JSON fits in
    ``TABLE_QUERY_MAX_BYTES`` (the project_db_query convention). Always
    terminates: an empty page is far below the cap."""

    def build(page: list, truncated: bool) -> dict:
        out = {"rows": page, "total": total, "truncated": truncated,
               "offset": offset, "limit": limit}
        if truncated:
            if page:
                out["hint"] = (
                    f"Response truncated to {len(page)} rows to fit the size "
                    f"limit; continue with offset={offset + len(page)}, or "
                    "select fewer columns with `columns`."
                )
            else:
                out["hint"] = (
                    "A single row is larger than the response size limit; "
                    "select fewer columns with `columns`."
                )
        return out

    payload = build(rows, False)
    if len(json.dumps(payload).encode("utf-8")) <= TABLE_QUERY_MAX_BYTES:
        return payload
    page = rows
    while True:
        page = page[:len(page) // 2]
        payload = build(page, True)
        if not page or len(json.dumps(payload).encode("utf-8")) <= TABLE_QUERY_MAX_BYTES:
            return payload


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


async def _handle_list_tables(caller: Caller, args: dict) -> str:
    async def op():
        limit = _int_arg(args, "limit", LIST_DEFAULT_LIMIT)
        limit = max(1, min(LIST_MAX_LIMIT, limit))
        found = await service.list_visible_tables(
            caller, scope=_str_arg(args, "scope", "all"), limit=limit,
        )
        tables = [
            _summary(table, verdict, load_schema(table["schema"]))
            for table, verdict in found
        ]
        return {"tables": tables, "count": len(tables)}
    return await _run(caller, op)


async def _handle_get_table(caller: Caller, args: dict) -> str:
    async def op():
        table, verdict, sample = await service.sample_rows(
            caller, args.get("table_id"), SAMPLE_ROWS,
        )
        return _detail(table, verdict, load_schema(table["schema"]), sample)
    return await _run(caller, op)


async def _handle_query_rows(caller: Caller, args: dict) -> str:
    async def op():
        count_only = _bool_arg(args, "count_only", False)
        _table, _verdict, result = await service.query_rows(
            caller,
            args.get("table_id"),
            filter=_json_arg(args, "filter"),
            sort=_json_arg(args, "sort"),
            limit=_int_arg(args, "limit"),
            offset=_int_arg(args, "offset"),
            columns=_json_arg(args, "columns"),
            count_only=count_only,
        )
        if count_only:
            return {"total": result.total}
        return _fit_rows(result.rows, result.total, result.offset, result.limit)
    return await _run(caller, op)
