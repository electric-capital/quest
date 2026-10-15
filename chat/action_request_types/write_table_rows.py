"""WriteTableRowsHandler -- approve-to-write row changes on a Quest Table.

Params: exactly the ``write_table_rows`` shape of
``chat/tables/write_request.py`` (``{table_id, operation, rows? |
updates? | key_column? | row_ids? | filter?}``), which is what a row write
tool returns as ``suggested_request.params`` on an ``approval`` verdict
(a table shared by or with the user, or the owner's ``require_approval``
switch).

``validate_params`` drops the server-only keys (:data:`SERVER_INJECTED_KEYS`)
and checks the shape (``validate_write_request_params``). The pre-card arm
(:func:`chat.action_request_types.table_rows_precard.precard_write_table_rows`)
dry-runs the write against the live schema and rows, refuses a write that
needs no card (``use_tool_directly``) or would fail, and injects the
preview keys, including ``schema_version`` and ``rows_digest``.

``execute`` re-resolves access with the PROPOSAL's conversation context
(never a UI caller: invariant 1, a private conversation never writes a
public table) and calls ``service.apply_row_write(..., bypass_approval=True,
expected_schema_version=, expected_rows_digest=)`` with ``write_source =
action_request:<id>`` and the approver as actor. A schema change since the
proposal fails the approve with ``stale_schema``; any change to the rows
the card showed (a value, a vanished row, a filter now matching other rows,
an upsert key now matching a stored row) with ``rows_changed`` naming the
current values. The digest covers the set of rows an upsert matches, so a
changed insert/update split (``upsert_split``) always surfaces as
``rows_changed`` before anything is written; the split is not re-compared
separately.
"""

from __future__ import annotations

from collections.abc import Mapping

from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types.table_precard import (
    common_preview_fields,
    execute_caller,
    execute_errors,
    write_source,
)
from chat.action_request_types.table_rows_precard import filter_text, invalid_rows_message
from chat.tables import service
from chat.tables.service import TableError
from chat.tables.storage import InvalidRowsError, TableStorageError
from chat.tables.write_request import validate_write_request_params
from db.models import ActionRequestType

# Server-only preview keys written by the pre-card check; a model-supplied
# copy is dropped by validate_params.
SERVER_INJECTED_KEYS = frozenset({
    "table_title",
    "table_mode",
    "table_scope",
    "share_summary",
    "require_approval",
    "approval_reason",
    "schema_version",
    "rows_digest",
    "row_counts",
    "upsert_split",
    "rows_diff",
})

OPERATION_LABELS = {
    "insert": "Insert",
    "update": "Update",
    "upsert": "Upsert",
    "delete": "Delete",
}


def _rows_noun(count: int) -> str:
    return "row" if count == 1 else "rows"


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _affected(params: Mapping) -> int | None:
    """Rows the write touches: the injected counts, else the diff total,
    else the length of the op's list; None when unknown (a filter delete
    without counts)."""
    counts = params.get("row_counts")
    if isinstance(counts, Mapping):
        values = [counts.get(k) for k in ("inserted", "updated", "deleted")]
        if all(_is_count(v) for v in values):
            return sum(values)
    diff = params.get("rows_diff")
    if isinstance(diff, Mapping) and _is_count(diff.get("total")):
        return diff["total"]
    for key in ("rows", "updates", "row_ids"):
        value = params.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _op_label(params: Mapping) -> str:
    op = params.get("operation")
    return OPERATION_LABELS.get(op, "Write") if isinstance(op, str) else "Write"


def _rows_text(params: Mapping) -> str:
    """``"<Op> N rows"`` (+ ``" (M inserted, K updated)"`` for an upsert)."""
    label = _op_label(params)
    count = _affected(params)
    text = f"{label} rows" if count is None else f"{label} {count} {_rows_noun(count)}"
    split = params.get("upsert_split")
    if params.get("operation") == "upsert" and isinstance(split, Mapping):
        ins, upd = split.get("insert"), split.get("update")
        if _is_count(ins) and _is_count(upd):
            text += f" ({ins} inserted, {upd} updated)"
    return text


def _strip_injected(params: Mapping) -> dict:
    return {k: v for k, v in params.items() if k not in SERVER_INJECTED_KEYS}


class WriteTableRowsHandler(ActionRequestHandler):
    """Write rows of a Quest Table after approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.WRITE_TABLE_ROWS

    @property
    def display_name(self) -> str:
        return "Write Table Rows"

    @property
    def approve_label(self) -> str:
        return "Write"

    @property
    def resolved_label(self) -> str:
        return "Rows written"

    def summary_snippet(self, params: dict) -> str:
        if not isinstance(params, Mapping):
            return ""
        title = params.get("table_title") or params.get("table_id")
        text = _rows_text({k: v for k, v in params.items() if k != "upsert_split"})
        return f"{text} in {title}" if isinstance(title, str) and title else text

    # ------------------------------------------------------------------
    # Validation (shape only; the rest is the pre-card's)
    # ------------------------------------------------------------------

    def validate_params(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        params = _strip_injected(params)
        try:
            table_id, operation, op_params = validate_write_request_params(params)
        except (TableError, TableStorageError) as exc:
            raise ValueError(f"{exc.code}: {exc}") from None
        return {"table_id": table_id, "operation": operation, **op_params}

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        if not isinstance(params, Mapping):
            return [{"key": "Rows", "value": ""}]
        fields = common_preview_fields(params)
        op = params.get("operation")
        label = _op_label(params)
        if op == "upsert" and isinstance(params.get("key_column"), str):
            label = f"Upsert on {params['key_column']}"
        fields.append({"key": "Operation", "value": label})
        diff = params.get("rows_diff")
        rows_field = {"key": "Rows", "value": _rows_text(params)}
        if isinstance(diff, Mapping):
            # Structured payload for the card's before/after row grid (B7);
            # unknown field types fall back to the plain value string.
            rows_field["type"] = "table_rows_diff"
            rows_field["rows_diff"] = dict(diff)
        fields.append(rows_field)
        if op == "delete" and params.get("filter") is not None:
            fields.append({"key": "Filter", "value": filter_text(params["filter"])})
        return fields

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
        request_id: int | None = None,
    ) -> dict:
        if not isinstance(params, Mapping):
            raise RuntimeError("invalid_request: Malformed write_table_rows request.")
        source = write_source(request_id)
        schema_version = params.get("schema_version")
        digest = params.get("rows_digest")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or not isinstance(digest, str)
            or not digest
        ):
            raise RuntimeError(
                "stale_request: This card does not record the table state it "
                "was proposed against; propose the write again."
            )
        with execute_errors():
            table_id, operation, op_params = validate_write_request_params(
                _strip_injected(params),
            )
        caller = await execute_caller(user, project_id, conversation_id)
        with execute_errors():
            try:
                out = await service.apply_row_write(
                    caller, table_id, operation, op_params,
                    write_source=source,
                    actor_user_id=user["id"],
                    bypass_approval=True,
                    expected_schema_version=schema_version,
                    expected_rows_digest=digest,
                )
            except InvalidRowsError as exc:
                # Name every row / column problem on the card.
                raise RuntimeError(f"{exc.code}: {invalid_rows_message(exc)}") from None
        return {
            "table_id": table_id,
            "title": params.get("table_title") or table_id,
            "operation": operation,
            "inserted": out["inserted"],
            "updated": out["updated"],
            "deleted": out["deleted"],
            "row_ids": out["row_ids"],
            "change_id": out["change_id"],
            "row_count": out["row_count"],
        }
