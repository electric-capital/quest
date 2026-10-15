"""CreateTableHandler -- approve-to-create action request for Quest Tables.

Params: ``{title, description?, target?: "user"|"project", columns}``.
``target`` defaults to ``"user"`` (as the ``create_doc`` tool),
``description`` to ``""``. ``columns`` is the list of column specs
(``chat.tables.schema.parse_column_spec``: name, type, required, unique,
default, options, multiple, precision, description; never a ``key``).

``validate_params`` checks the shape only. The pre-card arm
(:func:`chat.action_request_types.table_precard.precard_create_table`)
checks the gate, that the conversation may propose a table (top-level
private conversations only), the target and project, the mode, title and
description, the column specs, the title collision and then the per-owner cap (the store's order),
then injects the server-only preview keys (:data:`SERVER_INJECTED_KEYS`).

``execute`` re-resolves the proposal's conversation context, re-checks
that it may create a table and calls ``chat.tables.service.create_table``
with the proposer as owner, ``write_source = action_request:<id>`` and the
approver as actor: change #1 "Created table". The store re-checks the
title collision under the per-owner lock (a same-title table created
while the card sat open fails the approve with ``duplicate_title``).
"""

from __future__ import annotations

from collections.abc import Mapping

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types.table_precard import (
    column_summary,
    execute_caller,
    execute_errors,
    strip_column_keys,
    write_source,
)
from chat.tables import service
from chat.tables.access import create_table_deny_reason
from chat.tables.constants import TABLE_MAX_COLUMNS
from chat.tables.schema import load_schema, parse_columns
from db.models import ActionRequestType

TARGETS = ("user", "project")

_ALL_PARAMS = frozenset({"title", "description", "target", "columns"})

# Server-only preview keys written by the pre-card check; a model-supplied
# copy is dropped by validate_params.
SERVER_INJECTED_KEYS = frozenset({"table_mode", "project_title", "column_specs"})


def _preview_columns(params: Mapping) -> list[dict]:
    """The injected key-less column specs, else the raw specs (key-less)."""
    specs = params.get("column_specs")
    if isinstance(specs, list):
        return [dict(c) for c in specs if isinstance(c, Mapping)]
    raw = params.get("columns")
    if isinstance(raw, list):
        return strip_column_keys([c for c in raw if isinstance(c, Mapping)])
    return []


def _column_count(params: Mapping) -> int | None:
    for key in ("column_specs", "columns"):
        value = params.get(key)
        if isinstance(value, list):
            return len(value)
    return None


class CreateTableHandler(ActionRequestHandler):
    """Create a Quest Table after approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_TABLE

    @property
    def display_name(self) -> str:
        return "Create Table"

    @property
    def approve_label(self) -> str:
        return "Create"

    @property
    def resolved_label(self) -> str:
        return "Created"

    def summary_snippet(self, params: dict) -> str:
        if not isinstance(params, Mapping):
            return ""
        title = params.get("title")
        title = title.strip() if isinstance(title, str) else ""
        count = _column_count(params)
        if count is None:
            return title
        noun = "column" if count == 1 else "columns"
        suffix = f"({count} {noun})"
        return f"{title} {suffix}" if title else suffix

    # ------------------------------------------------------------------
    # Validation (shape only; the rest is the pre-card's)
    # ------------------------------------------------------------------

    def validate_params(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        params = {k: v for k, v in params.items() if k not in SERVER_INJECTED_KEYS}
        reject_unknown_params(self.type_name.value, params, _ALL_PARAMS)

        title = params.get("title")
        if title is None:
            raise ValueError("Missing required parameter: title")
        if not isinstance(title, str):
            raise ValueError("title must be a string.")
        if not title.strip():
            raise ValueError("title must be a non-empty string.")

        description = params.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise ValueError("description must be a string.")

        target = params.get("target")
        if target is None:
            target = "user"
        if not isinstance(target, str) or target not in TARGETS:
            raise ValueError(
                f"Invalid target {target!r}. Must be one of: {', '.join(TARGETS)}."
            )

        columns = params.get("columns")
        if columns is None:
            raise ValueError("Missing required parameter: columns")
        if not isinstance(columns, list):
            raise ValueError("columns must be a list of column objects.")
        if not columns:
            raise ValueError("columns must contain at least one column.")
        if len(columns) > TABLE_MAX_COLUMNS:
            raise ValueError(
                f"A table can have at most {TABLE_MAX_COLUMNS} columns (got {len(columns)})."
            )
        for i, column in enumerate(columns):
            if not isinstance(column, dict):
                raise ValueError(f"columns[{i}] must be an object.")

        return {
            "title": title,
            "description": description,
            "target": target,
            "columns": columns,
        }

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        if not isinstance(params, Mapping):
            return [{"key": "Title", "value": ""}]
        fields: list[dict] = [{"key": "Title", "value": str(params.get("title") or "")}]
        if params.get("description"):
            fields.append({"key": "Description", "value": str(params["description"])})
        # table_mode / project_title / column_specs are server-injected by
        # the pre-card check (table_precard.precard_create_table).
        if params.get("table_mode"):
            fields.append({"key": "Mode", "value": str(params["table_mode"])})
        target = params.get("target") or "user"
        fields.append({"key": "Scope", "value": str(target)})
        if target == "project" and params.get("project_title"):
            fields.append({"key": "Project", "value": str(params["project_title"])})
        columns = _preview_columns(params)
        fields.append({
            "key": "Columns",
            "value": column_summary(columns),
            # Structured payload for the card's column table (B7); unknown
            # field types fall back to the plain value string.
            "type": "table_columns",
            "columns": columns,
        })
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
        title = params.get("title")
        target = params.get("target") or "user"
        columns = params.get("columns")
        if not isinstance(title, str) or target not in TARGETS or not isinstance(columns, list):
            raise RuntimeError("invalid_request: Malformed create_table request.")
        if target == "project" and not project_id:
            raise RuntimeError(
                "invalid_args: target 'project' needs a project conversation."
            )
        source = write_source(request_id)
        caller = await execute_caller(user, project_id, conversation_id)
        reason = create_table_deny_reason(is_public=caller.is_public, run_kind=caller.run_kind)
        if reason:
            raise RuntimeError(f"denied: {reason}")
        with execute_errors():
            # The table must be exactly the one the card showed: the
            # columns as parsed now must equal the injected column_specs.
            specs = params.get("column_specs")
            if specs is not None and strip_column_keys(parse_columns(columns)) != specs:
                raise RuntimeError(
                    "stale_request: The columns no longer match the ones shown "
                    "on the card; propose the table again."
                )
            table = await service.create_table(
                caller,
                title=title,
                description=params.get("description") or "",
                project_id=project_id if target == "project" else None,
                columns=columns,
                write_source=source,
                actor_user_id=user["id"],
            )
        return {
            "table_id": table["id"],
            "title": table["title"],
            "mode": table["mode"],
            "scope": "project" if table.get("project_id") else "user",
            "project_id": table.get("project_id"),
            "columns": [
                {"name": c.name, "type": c.type} for c in load_schema(table["schema"])
            ],
            "schema_version": table["schema_version"],
        }
