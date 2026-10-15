"""ChangeTableSchemaHandler -- approve-to-apply schema change for Quest Tables.

Params: ``{table_id, operations, expected_schema_version}``. ``operations``
is the name-based operation list of ``chat.tables.schema_ops`` (spec
section 4.4: ``add_column``, ``rename_column``, ``drop_column``,
``reorder_columns``, ``set_*``, the enum-option ops, ``rename_table``),
applied atomically in order as one change. ``expected_schema_version`` pins
the schema the operations were written against.

``validate_params`` checks the shape only. The pre-card arm
(:func:`chat.action_request_types.table_schema_precard.precard_change_table_schema`)
dry-runs the change (access, version pin, every operation, the
``rows_violate`` checks against the live rows) and injects the server-only
preview keys (:data:`SERVER_INJECTED_KEYS`), among them the key-less
``schema_diff``.

``execute`` re-resolves access with the PROPOSAL's conversation context
(never a UI context: invariant 1) and calls
``chat.tables.service.apply_schema_change`` with the same version pin
(a UI edit while the card sat open fails the approve with
``stale_schema``), ``write_source = action_request:<id>`` and the approver
as actor. The service re-runs the ``rows_violate`` checks under the lock.
"""

from __future__ import annotations

from collections.abc import Mapping

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types.table_precard import (
    common_preview_fields,
    execute_caller,
    execute_errors,
    write_source,
)
from chat.action_request_types.table_schema_precard import (
    CHANGE_TABLE_SCHEMA_INJECTED_KEYS,
    execute_stale_schema_message,
    operation_error_message,
    operation_summaries,
)
from chat.tables import service
from chat.tables.constants import TABLE_SCHEMA_MAX_OPERATIONS
from chat.tables.schema import load_schema
from chat.tables.schema_ops import SchemaOperationError
from db.models import ActionRequestType

_ALL_PARAMS = frozenset({"table_id", "operations", "expected_schema_version"})

# Server-only preview keys written by the pre-card check; a model-supplied
# copy is dropped by validate_params.
SERVER_INJECTED_KEYS = CHANGE_TABLE_SCHEMA_INJECTED_KEYS


def _summaries(params: Mapping) -> list[str]:
    injected = params.get("operation_summaries")
    if isinstance(injected, list) and all(isinstance(s, str) for s in injected):
        return injected
    return operation_summaries(params.get("operations"))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class ChangeTableSchemaHandler(ActionRequestHandler):
    """Change a Quest Table's columns after approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CHANGE_TABLE_SCHEMA

    @property
    def display_name(self) -> str:
        return "Change Table Schema"

    @property
    def approve_label(self) -> str:
        return "Apply"

    @property
    def resolved_label(self) -> str:
        return "Schema changed"

    def summary_snippet(self, params: dict) -> str:
        if not isinstance(params, Mapping):
            return ""
        summaries = _summaries(params)
        if summaries:
            first = summaries[0]
            return f"{first} and {len(summaries) - 1} more" if len(summaries) > 1 else first
        title = params.get("table_title")
        return title if isinstance(title, str) else ""

    # ------------------------------------------------------------------
    # Validation (shape only; the content is the pre-card's)
    # ------------------------------------------------------------------

    def validate_params(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        params = {k: v for k, v in params.items() if k not in SERVER_INJECTED_KEYS}
        reject_unknown_params(self.type_name.value, params, _ALL_PARAMS)

        table_id = params.get("table_id")
        if table_id is None:
            raise ValueError("Missing required parameter: table_id")
        if not isinstance(table_id, str) or not table_id.strip():
            raise ValueError("table_id must be a non-empty string.")

        operations = params.get("operations")
        if operations is None:
            raise ValueError("Missing required parameter: operations")
        if not isinstance(operations, list):
            raise ValueError("operations must be a list of operation objects.")
        if not operations:
            raise ValueError("operations must contain at least one operation.")
        if len(operations) > TABLE_SCHEMA_MAX_OPERATIONS:
            raise ValueError(
                f"At most {TABLE_SCHEMA_MAX_OPERATIONS} operations per schema change "
                f"(got {len(operations)})."
            )
        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                raise ValueError(f"operations[{i}] must be an object.")

        version = params.get("expected_schema_version")
        if version is None:
            raise ValueError("Missing required parameter: expected_schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError(
                "expected_schema_version must be a positive integer (the "
                "schema_version get_table reported)."
            )

        return {
            "table_id": table_id,
            "operations": operations,
            "expected_schema_version": version,
        }

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        if not isinstance(params, Mapping):
            return [{"key": "Changes", "value": ""}]
        fields = common_preview_fields(params)
        summaries = _summaries(params)
        count = params.get("operations_count")
        if isinstance(count, bool) or not isinstance(count, int):
            ops = params.get("operations")
            count = len(ops) if isinstance(ops, list) else len(summaries)
        value = _count(count, "operation")
        if summaries:
            value += ": " + "; ".join(summaries)
        diff = params.get("schema_diff")
        changes: dict = {"key": "Changes", "value": value}
        if isinstance(diff, Mapping):
            # Structured payload for the before/after list (B7); unknown
            # field types fall back to the plain value string.
            changes["type"] = "table_schema_diff"
            changes["schema_diff"] = dict(diff)
        fields.append(changes)

        rewritten = diff.get("cells_rewritten_total") if isinstance(diff, Mapping) else None
        if isinstance(rewritten, int) and not isinstance(rewritten, bool) and rewritten > 0:
            fields.append({"key": "Cells rewritten", "value": str(rewritten)})
        dropped = params.get("dropped_values")
        if isinstance(dropped, Mapping):
            parts = [
                f"{name}: {_count(n, 'value')}"
                for name, n in dropped.items()
                if isinstance(n, int) and not isinstance(n, bool) and n > 0
            ]
            if parts:
                fields.append({"key": "Dropped values", "value": ", ".join(parts)})
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
        table_id = params.get("table_id") if isinstance(params, Mapping) else None
        operations = params.get("operations") if isinstance(params, Mapping) else None
        version = params.get("expected_schema_version") if isinstance(params, Mapping) else None
        if (
            not isinstance(table_id, str)
            or not isinstance(operations, list)
            or isinstance(version, bool)
            or not isinstance(version, int)
        ):
            raise RuntimeError("invalid_request: Malformed change_table_schema request.")
        source = write_source(request_id)
        caller = await execute_caller(user, project_id, conversation_id)
        with execute_errors():
            try:
                result = await service.apply_schema_change(
                    caller,
                    table_id,
                    operations,
                    expected_schema_version=version,
                    write_source=source,
                    actor_user_id=user["id"],
                )
            except service.StaleSchema as exc:
                raise RuntimeError(
                    f"stale_schema: {execute_stale_schema_message(exc.current)}"
                ) from None
            except SchemaOperationError as exc:
                raise RuntimeError(f"{exc.code}: {operation_error_message(exc)}") from None
        return {
            "table_id": table_id,
            "title": result["title"],
            "schema_version": result["schema_version"],
            "change_id": result["change_id"],
            "columns": [
                {"name": c.name, "type": c.type} for c in load_schema(result["columns"])
            ],
        }
