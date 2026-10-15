"""Pre-card arm of the ``change_table_schema`` action request (Quest Tables).

Called through :func:`chat.action_request_types.table_precard.table_precard_check`
(imported lazily, only when a ``change_table_schema`` card is proposed).

:func:`precard_change_table_schema` runs ``service.preview_schema_change``
(a dry run under the table lock: gate, verdict, ``expected_schema_version``,
every operation validated in order against a working copy of the schema,
the ``rows_violate`` checks against the live rows) and injects the
server-only preview keys (``CHANGE_TABLE_SCHEMA_INJECTED_KEYS``). Column
keys (``c_xxxxxxxx``) never reach the card: the injected ``schema_diff``
names columns only.

The message helpers (:func:`stale_schema_message`,
:func:`operation_error_message`, :func:`operation_summaries`) are shared
with the handler (``change_table_schema.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from chat.action_request_types.table_precard import (
    precard_caller,
    precard_errors,
    refuse,
    strip_column_key,
    strip_column_keys,
    table_card_fields,
)
from chat.tables import service
from chat.tables.schema import load_schema
from chat.tables.schema_ops import EnumRewrite, SchemaOperationError
from chat.tables.storage import schema_summary

__all__ = [
    "CHANGE_TABLE_SCHEMA_INJECTED_KEYS",
    "keyless_schema_diff",
    "operation_error_message",
    "operation_summaries",
    "precard_change_table_schema",
    "stale_schema_message",
]

# Server-only keys written into params by the pre-card (the handler's
# SERVER_INJECTED_KEYS).
CHANGE_TABLE_SCHEMA_INJECTED_KEYS = frozenset({
    "table_title",
    "table_mode",
    "table_scope",
    "share_summary",
    "require_approval",
    "schema_diff",
    "operations_count",
    "rows_backfilled",
    "dropped_values",
    "operation_summaries",
})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


# Longest column description quoted in the stale_schema message.
_STALE_DESCRIPTION_CHARS = 80


def _compact_spec(column: Mapping) -> dict:
    """A key-less column spec without null / false / empty fields (the
    defaults), its description clipped: keeps the stale_schema message
    bounded."""
    out: dict = {}
    for field, value in column.items():
        if field == "key" or value is None or value is False or value in ("", [], ()):
            continue
        if field == "description" and isinstance(value, str) and (
            len(value) > _STALE_DESCRIPTION_CHARS
        ):
            value = value[:_STALE_DESCRIPTION_CHARS] + "\u2026"
        out[field] = value
    return out


def _current_columns(current: Mapping) -> list[dict]:
    """The current table's columns as compact key-less specs (best effort)."""
    try:
        columns = strip_column_keys(load_schema(current.get("schema")))
    except Exception:  # noqa: BLE001 -- a message helper never raises
        raw = current.get("schema")
        if not isinstance(raw, list):
            return []
        columns = strip_column_keys([c for c in raw if isinstance(c, Mapping)])
    return [_compact_spec(c) for c in columns]


def stale_schema_message(current: Mapping) -> str:
    """The pre-card ``stale_schema`` text: the current ``schema_version``
    and the current columns as compact key-less JSON specs (default fields
    left out, descriptions clipped), so the model can re-propose against
    them."""
    columns = json.dumps(_current_columns(current), ensure_ascii=False)
    return (
        f"The table's columns changed since you read them; the current "
        f"schema_version is {current.get('schema_version')}. Current columns: "
        f"{columns}. Re-propose the operations against these columns with "
        f"expected_schema_version {current.get('schema_version')}."
    )


def execute_stale_schema_message(current: Mapping) -> str:
    """The approve-time ``stale_schema`` text (shown on the card)."""
    return (
        "The table's columns were changed after this card was proposed "
        f"(current schema_version {current.get('schema_version')}); nothing "
        "was changed. Propose the change again against the current columns."
    )


def operation_error_message(exc: SchemaOperationError) -> str:
    """The operation error's text, naming the failing operation (1-based,
    "Operation 2 ...") when the text does not already."""
    message = str(exc)
    index = exc.op_index
    if index is not None and not message.startswith(f"Operation {index + 1}"):
        label = f" ({exc.op})" if exc.op else ""
        return f"Operation {index + 1}{label}: {message}"
    return message


def operation_summaries(operations: Any) -> list[str]:
    """One human line per operation ("Renamed column A to B"); never raises.
    Works on the plan's normalized operations and on the raw name-based
    ones (an odd operation reads "1 schema change")."""
    if not isinstance(operations, (list, tuple)):
        return []
    lines: list[str] = []
    for op in operations:
        try:
            lines.append(schema_summary([op]) if isinstance(op, Mapping) else "1 schema change")
        except Exception:  # noqa: BLE001 -- odd params from old history
            lines.append("1 schema change")
    return lines


# ---------------------------------------------------------------------------
# schema_diff without keys
# ---------------------------------------------------------------------------


def keyless_schema_diff(
    diff: Mapping, stats: Mapping, effects: Sequence = (),
) -> dict:
    """``schema_ops.schema_diff`` output with every column key removed.

    Each entry also gets ``cells_rewritten`` (the dry run's
    ``cells_changed`` for its column: distinct cells whose stored value
    differs after the change, 0 when none) and ``options_renamed`` (the
    ``rename_enum_option`` rewrites of its column, in order, as
    ``{"option", "new_option"}``); the top level gets
    ``cells_rewritten_total``."""
    # Distinct cells whose value actually changes (a chained or round-trip
    # rename counts a cell once, or not at all); the per-effect
    # "cells_rewritten" stat would overcount them.
    rewritten = stats.get("cells_changed") or {}
    renames: dict[str, list[dict]] = {}
    for effect in effects:
        if isinstance(effect, EnumRewrite):
            renames.setdefault(effect.column_key, []).append(
                {"option": effect.old_option, "new_option": effect.new_option}
            )
    entries = []
    total = 0
    for entry in diff.get("columns", []):
        key = entry.get("key")
        out = {k: v for k, v in entry.items() if k != "key"}
        for side in ("before", "after"):
            if isinstance(out.get(side), Mapping):
                out[side] = strip_column_key(out[side])
        count = int(rewritten.get(key, 0) or 0)
        out["cells_rewritten"] = count
        out["options_renamed"] = renames.get(key, [])
        total += count
        entries.append(out)
    result = {k: v for k, v in diff.items() if k != "columns"}
    result["columns"] = entries
    result["cells_rewritten_total"] = total
    return result


# ---------------------------------------------------------------------------
# The arm
# ---------------------------------------------------------------------------


async def precard_change_table_schema(
    validated_params: dict,
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
    *,
    is_slack_origin: bool = False,
) -> None:
    """Pre-card arm of ``change_table_schema``.

    Order: gate (``tables_disabled``, no DB work) -> the conversation's
    project (``project_not_found``) -> ``service.preview_schema_change``:
    a missing or hidden table (``table_not_found``, byte-equal), the
    schema verdict (``denied`` with the schema deny reason: a read share, a
    public table from a private conversation, a public conversation, a
    Slack run), the version pin (``stale_schema`` naming the current
    ``schema_version`` and the current key-less columns), each operation in
    order (its ``SchemaOperationError`` code, naming the operation), the
    title collision of ``rename_table`` (``duplicate_title``) and the live
    rows (``rows_violate`` with the row ids).

    On success injects ``table_title``, ``table_mode``, ``table_scope``,
    ``share_summary``, ``require_approval``, ``schema_diff`` (key-less, with
    the per-column and total ``cells_rewritten``), ``operations_count``,
    ``rows_backfilled``, ``dropped_values`` (column name -> non-null cells
    dropped) and ``operation_summaries``.
    """
    caller = await precard_caller(
        user, project_id, conversation_id, is_slack_origin=is_slack_origin,
    )
    with precard_errors():
        try:
            preview = await service.preview_schema_change(
                caller,
                validated_params.get("table_id"),
                validated_params.get("operations"),
                expected_schema_version=validated_params.get("expected_schema_version"),
            )
        except service.StaleSchema as exc:
            raise refuse("stale_schema", stale_schema_message(exc.current)) from None
        except SchemaOperationError as exc:
            raise refuse(exc.code, operation_error_message(exc)) from None

    plan = preview["plan"]
    stats = preview["stats"] or {}
    names = {c.key: c.name for c in preview["columns_before"]}
    dropped = {
        names[key]: int(count or 0)
        for key, count in (stats.get("dropped_values") or {}).items()
        if key in names  # never a key on the card
    }

    validated_params.update(table_card_fields(preview["table"]))
    validated_params["schema_diff"] = keyless_schema_diff(
        preview["schema_diff"], stats, plan.effects,
    )
    validated_params["operations_count"] = len(plan.operations)
    validated_params["rows_backfilled"] = int(stats.get("rows_backfilled") or 0)
    validated_params["dropped_values"] = dropped
    validated_params["operation_summaries"] = operation_summaries(plan.operations)
