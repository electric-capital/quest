"""The ``write_table_rows`` params shape, shared by the row write tools and
the ``write_table_rows`` action request.

A row write tool that gets an ``approval`` verdict returns
``suggested_request = {"request_type": "write_table_rows", "params":
build_write_request_params(...)}``; the model forwards it unchanged through
``create_action_request``, and the action request (pre-card and approve
time) validates the card's params with :func:`validate_write_request_params`,
then calls ``chat.tables.service.apply_row_write(caller, table_id,
operation, op_params, bypass_approval=True)``. The round trip tool args ->
params -> validate -> ``apply_row_write`` is lossless: the params are
exactly the tool's own arguments plus ``operation``.

Shape (no other keys)::

    {"table_id": str,
     "operation": "insert" | "update" | "upsert" | "delete",
     "rows":       [{<column name>: value, ...}, ...],    # insert, upsert
     "updates":    [{"_id": int, <column name>: value}],  # update
     "key_column": str,                                   # upsert
     "row_ids":    [int, ...],                            # delete, or
     "filter":     {...filter DSL...}}                    # delete

``rows`` / ``updates`` / ``row_ids`` are non-empty lists of at most
``TABLE_WRITE_MAX_ROWS`` entries; ``rows`` and ``updates`` entries are
objects and every update names an integer ``_id``; a delete carries exactly
one of ``row_ids`` / ``filter``. An integer-valued string or float ``_id``
or row id (``"5"``, ``5.0``) is taken as that integer. Only the shape is
checked here: column names, values, unique keys and the filter DSL are
validated against the live schema by the storage layer (``invalid_rows``,
``unknown_key_column`` / ``key_not_unique``, FilterError ...).
"""

from __future__ import annotations

from typing import Any

from chat.tables.constants import TABLE_WRITE_MAX_ROWS
from chat.tables.service import ROW_OPS, TableError
from chat.tables.storage import TooManyRowsError

__all__ = [
    "OPERATION_KEYS",
    "WRITE_TABLE_ROWS",
    "WriteRequestError",
    "build_write_request_params",
    "validate_write_request_params",
]

WRITE_TABLE_ROWS = "write_table_rows"

# operation -> the op-specific keys it may carry (delete: exactly one).
OPERATION_KEYS: dict[str, tuple[str, ...]] = {
    "insert": ("rows",),
    "update": ("updates",),
    "upsert": ("key_column", "rows"),
    "delete": ("row_ids", "filter"),
}
assert tuple(OPERATION_KEYS) == ROW_OPS

_ENVELOPE_KEYS = ("table_id", "operation")


class WriteRequestError(TableError):
    """The params do not have the ``write_table_rows`` shape (``invalid_args``)."""

    def __init__(self, message: str):
        super().__init__("invalid_args", message)


def _indexes(bad: list[int]) -> str:
    shown = ", ".join(str(i) for i in bad[:20])
    return shown + (" ..." if len(bad) > 20 else "")


def _as_int(value: Any) -> Any:
    """An integer-valued string or float as int; anything else unchanged
    (a bool stays a bool, so it is refused as an id)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_list(name: str, value: Any) -> list:
    if not isinstance(value, (list, tuple)) or not value:
        raise WriteRequestError(f"{name} must be a non-empty list.")
    if len(value) > TABLE_WRITE_MAX_ROWS:
        raise TooManyRowsError(len(value))
    return list(value)


def _objects(name: str, value: Any) -> list[dict]:
    entries = _entry_list(name, value)
    bad = [i for i, entry in enumerate(entries) if not isinstance(entry, dict)]
    if bad:
        raise WriteRequestError(
            f"each entry of {name} must be an object; not an object at index "
            f"{_indexes(bad)}."
        )
    return [dict(entry) for entry in entries]


def _clean_op_params(operation: str, params: dict) -> dict:
    if operation in ("insert", "upsert"):
        out: dict[str, Any] = {}
        if operation == "upsert":
            key_column = params.get("key_column")
            if not isinstance(key_column, str) or not key_column.strip():
                raise WriteRequestError("key_column must be a column name.")
            out["key_column"] = key_column
        out["rows"] = _objects("rows", params.get("rows"))
        return out
    if operation == "update":
        updates = _objects("updates", params.get("updates"))
        bad = []
        for i, update in enumerate(updates):
            if "_id" in update:
                update["_id"] = _as_int(update["_id"])
            if not _is_int(update.get("_id")):
                bad.append(i)
        if bad:
            raise WriteRequestError(
                "each update needs an integer _id (from query_rows); missing or "
                f"not an integer at index {_indexes(bad)}."
            )
        return {"updates": updates}
    has_ids = params.get("row_ids") is not None
    has_filter = params.get("filter") is not None
    if has_ids == has_filter:
        raise WriteRequestError("Give exactly one of row_ids or filter.")
    if has_filter:
        if not isinstance(params["filter"], dict):
            raise WriteRequestError("filter must be an object (the query_rows filter DSL).")
        return {"filter": params["filter"]}
    row_ids = [_as_int(v) for v in _entry_list("row_ids", params["row_ids"])]
    bad = [i for i, v in enumerate(row_ids) if not _is_int(v)]
    if bad:
        raise WriteRequestError(f"row_ids must be integers; not an integer at index {_indexes(bad)}.")
    return {"row_ids": row_ids}


def validate_write_request_params(params: Any) -> tuple[str, str, dict]:
    """Check a ``write_table_rows`` params dict (module docstring).

    Returns:
        ``(table_id, operation, op_params)``: ``op_params`` is what
        ``service.apply_row_write(caller, table_id, operation, op_params)``
        takes (the op's keys only; a delete's absent selector left out;
        ids coerced to int; ``rows`` / ``updates`` entries copied).

    Raises:
        WriteRequestError (``invalid_args``): not an object, unknown or
        misplaced keys, a missing / empty ``table_id``, an unknown
        ``operation``, a missing or non-list ``rows`` / ``updates`` /
        ``row_ids``, an entry that is not an object, an update without an
        integer ``_id``, a non-integer row id, a missing ``key_column``, a
        delete with neither or both of ``row_ids`` / ``filter``, a
        non-object ``filter``.
        chat.tables.storage.TooManyRowsError (``too_many_rows``, with
        ``count`` and ``cap``): more than ``TABLE_WRITE_MAX_ROWS`` entries.
    """
    if not isinstance(params, dict):
        raise WriteRequestError("params must be an object.")
    table_id = params.get("table_id")
    if not isinstance(table_id, str) or not table_id.strip():
        raise WriteRequestError("table_id must be a non-empty string.")
    operation = params.get("operation")
    if operation not in OPERATION_KEYS:
        raise WriteRequestError(f"operation must be one of: {', '.join(ROW_OPS)}.")
    allowed = set(_ENVELOPE_KEYS) | set(OPERATION_KEYS[operation])
    extra = sorted(str(k) for k in params if k not in allowed)
    if extra:
        raise WriteRequestError(
            f"Unexpected key(s) for operation {operation!r}: {', '.join(extra)}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return table_id, operation, _clean_op_params(operation, params)


def build_write_request_params(table_id: Any, operation: Any, params: Any) -> dict:
    """The ``write_table_rows`` params for a row write: ``{"table_id",
    "operation", **op_params}`` with keys whose value is None dropped (an
    omitted delete selector). ``params`` are the tool args minus
    ``table_id`` (``apply_row_write``'s params).

    The result is validated (:func:`validate_write_request_params`; same
    errors) and returned in its cleaned form, so
    ``validate_write_request_params(build_write_request_params(t, op, p))``
    returns ``(t, op, <the same op params>)``.
    """
    if not isinstance(params, dict):
        raise WriteRequestError("params must be an object.")
    if set(params) & set(_ENVELOPE_KEYS):
        raise WriteRequestError("params must not repeat table_id or operation.")
    merged = {"table_id": table_id, "operation": operation}
    merged.update({k: v for k, v in params.items() if v is not None})
    clean_id, clean_op, op_params = validate_write_request_params(merged)
    return {"table_id": clean_id, "operation": clean_op, **op_params}
