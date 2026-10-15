"""Pre-card arm of the ``write_table_rows`` action request (Quest Tables).

Dispatched lazily from
:func:`chat.action_request_types.table_precard.table_precard_check`. The
arm dry-runs the proposed row write against the live schema and rows
(``chat.tables.service.preview_row_write``: nothing written) and either
refuses the proposal (``ValueError("<code>: <message>")``, no card) or
injects the server-only preview keys the card shows and the executor pins
(see :data:`chat.action_request_types.write_table_rows.SERVER_INJECTED_KEYS`):

- :func:`table_card_fields` (``table_title``, ``table_mode``,
  ``table_scope``, ``share_summary``, ``require_approval``) and
  ``approval_reason`` (``"shared"`` / ``"required_by_owner"``);
- ``schema_version`` (the version at proposal) and ``rows_digest`` (the
  digest of the rows the write touches), passed back to
  ``apply_row_write(expected_schema_version=, expected_rows_digest=)`` at
  approve time (``stale_schema`` / ``rows_changed``);
- ``row_counts`` ``{inserted, updated, deleted}``, ``upsert_split``
  ``{insert, update}`` (upsert only) and ``rows_diff`` (:func:`build_rows_diff`).

Refusals, in order: ``tables_disabled`` (gate, before any DB work),
``project_not_found``; then the dry run's own errors with their code and
text: ``table_not_found`` (hidden and missing byte-equal), ``denied`` (the
verdict's deny reason: read share, a public table from a private
conversation, Slack), ``invalid_rows`` (with the problem list),
``too_many_rows``, ``table_full``, ``change_too_large``, filter errors,
``unknown_key_column`` / ``key_not_unique``; then on the verdict:
``use_tool_directly`` (the write needs no card) and ``no_rows`` (a filter
delete matching nothing).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from chat.action_request_types.table_precard import (
    precard_caller,
    precard_errors,
    refuse,
    table_card_fields,
)
from chat.tables import service
from chat.tables.schema import load_schema
from chat.tables.storage import InvalidRowsError
from chat.tables.write_request import validate_write_request_params

__all__ = [
    "OPERATION_TOOLS",
    "ROWS_DIFF_MAX_ROWS",
    "build_rows_diff",
    "filter_text",
    "invalid_rows_message",
    "precard_write_table_rows",
]

# Most rows the card's rows_diff carries (``truncated`` beyond).
ROWS_DIFF_MAX_ROWS = 50
# Most invalid_rows problems a refusal spells out.
_PROBLEMS_SHOWN = 50

OPERATION_TOOLS = {
    "insert": "insert_rows",
    "update": "update_rows",
    "upsert": "upsert_rows",
    "delete": "delete_rows",
}

# The params keys a model may send (write_request's shape).
_OWN_KEYS = frozenset({
    "table_id", "operation", "rows", "updates", "key_column", "row_ids", "filter",
})


def invalid_rows_message(exc: InvalidRowsError) -> str:
    """``str(exc)`` followed by every problem (row index, column, reason),
    at most :data:`_PROBLEMS_SHOWN`."""
    parts = []
    for problem in exc.problems[:_PROBLEMS_SHOWN]:
        where = f"row {problem.get('row')}"
        if problem.get("column") is not None:
            where += f", column {problem['column']!r}"
        parts.append(f"{where}: {problem.get('reason')}")
    if len(exc.problems) > _PROBLEMS_SHOWN:
        parts.append(f"and {len(exc.problems) - _PROBLEMS_SHOWN} more")
    if not parts:
        return str(exc)
    return f"{exc} Problems: " + "; ".join(parts) + "."


def _values(row: Mapping, columns: Sequence[str]) -> dict:
    """A row image's column values by name (no ``_id``, no timestamps)."""
    return {name: row.get(name) for name in columns}


def build_rows_diff(operation: str, columns: Sequence[str], result: Mapping) -> dict:
    """The card's ``rows_diff`` from ``preview_row_write``'s result.

    ``{"columns": [names in schema order], "rows": [{"_id", "status",
    "before", "after", "changed_columns"}], "total": n, "truncated": bool}``
    with at most :data:`ROWS_DIFF_MAX_ROWS` rows. insert: ``added`` rows
    (``_id`` None, ``after`` with defaults applied); update: ``changed``
    rows (before / after by ``_id``, ``changed_columns`` the names whose
    value differs); upsert: matched rows ``changed``, new rows ``added``
    (``_id`` None); delete: ``deleted`` rows (``before`` only). Rows are
    keyed by column name; no timestamps, no column keys.
    """
    columns = list(columns)
    before_rows = [r for r in result.get("before_rows") or [] if isinstance(r, Mapping)]
    after_rows = [r for r in result.get("after_rows") or [] if isinstance(r, Mapping)]
    before_by_id = {r.get("_id"): r for r in before_rows}
    entries: list[dict] = []

    def added(after: Mapping) -> dict:
        return {"_id": None, "status": "added", "before": None,
                "after": _values(after, columns), "changed_columns": []}

    if operation == "delete":
        for before in before_rows:
            entries.append({"_id": before.get("_id"), "status": "deleted",
                            "before": _values(before, columns), "after": None,
                            "changed_columns": []})
    elif operation == "insert":
        entries = [added(after) for after in after_rows]
    else:  # update / upsert
        for after in after_rows:
            before = before_by_id.get(after.get("_id"))
            if before is None:
                entries.append(added(after))
                continue
            before_values = _values(before, columns)
            after_values = _values(after, columns)
            entries.append({
                "_id": after.get("_id"),
                "status": "changed",
                "before": before_values,
                "after": after_values,
                "changed_columns": [
                    name for name in columns if before_values[name] != after_values[name]
                ],
            })
    total = len(entries)
    return {
        "columns": columns,
        "rows": entries[:ROWS_DIFF_MAX_ROWS],
        "total": total,
        "truncated": total > ROWS_DIFF_MAX_ROWS,
    }


async def precard_write_table_rows(
    validated_params: dict,
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
    *,
    is_slack_origin: bool = False,
) -> None:
    """Verify a ``write_table_rows`` proposal and inject its server-only
    preview keys (module docstring).

    Raises:
        ValueError: ``"<code>: <message>"`` (no card).
    """
    caller = await precard_caller(
        user, project_id, conversation_id, is_slack_origin=is_slack_origin,
    )
    clean = {k: v for k, v in validated_params.items() if k in _OWN_KEYS}
    with precard_errors():
        table_id, operation, op_params = validate_write_request_params(clean)
    with precard_errors():
        try:
            table, access, result = await service.preview_row_write(
                caller, table_id, operation, op_params,
            )
        except InvalidRowsError as exc:
            # The problem list goes into the refusal so the model can fix
            # every row in one go (refuse() is not a tables error).
            raise refuse(exc.code, invalid_rows_message(exc)) from None

    if access.rows == "free":
        tool = OPERATION_TOOLS[operation]
        raise refuse(
            "use_tool_directly",
            f"This table is writable directly; call {tool} instead of "
            "proposing an action request.",
        )
    if access.rows != "approval":
        # The service raises TableDenied for a denied verdict; fail closed
        # on anything else that is not an approval verdict.
        raise refuse("denied", access.deny_reason or "This table cannot be written here.")
    if operation == "delete" and "filter" in op_params and not result.get("deleted"):
        raise refuse("no_rows", "The filter matches no rows; nothing to delete.")

    columns = [column.name for column in load_schema(table["schema"])]
    validated_params.update(table_card_fields(table))
    validated_params["approval_reason"] = access.approval_reason
    validated_params["schema_version"] = table["schema_version"]
    validated_params["rows_digest"] = result["rows_digest"]
    validated_params["row_counts"] = {
        "inserted": result["inserted"],
        "updated": result["updated"],
        "deleted": result["deleted"],
    }
    if operation == "upsert":
        validated_params["upsert_split"] = {
            "insert": result["inserted"], "update": result["updated"],
        }
    validated_params["rows_diff"] = build_rows_diff(operation, columns, result)


# Longest filter text the card shows (clipped with "...").
FILTER_TEXT_MAX_CHARS = 200


def filter_text(value: Any) -> str:
    """Compact JSON of a delete filter for the card, clipped to
    :data:`FILTER_TEXT_MAX_CHARS` with ``"..."``; never raises."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > FILTER_TEXT_MAX_CHARS:
        text = text[:FILTER_TEXT_MAX_CHARS] + "..."
    return text
