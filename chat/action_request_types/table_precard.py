"""Pre-card checks + shared helpers for the Quest Tables action requests.

The three table request types (:data:`TABLE_REQUEST_TYPES`) share one
pre-card entry point, :func:`table_precard_check`, called from the
``create_action_request`` dispatch arm (chat/gemini_api/turn_tools.py,
after ``validate_params``, before the request row is written), like
``doc_precard_check``. It dispatches to one arm per type; every arm has the
signature ``(validated_params, user, project_id, conversation_id, *,
is_slack_origin)``:

- ``create_table`` -> :func:`precard_create_table` (this module);
- ``change_table_schema`` ->
  ``chat.action_request_types.table_schema_precard.precard_change_table_schema``;
- ``write_table_rows`` ->
  ``chat.action_request_types.table_rows_precard.precard_write_table_rows``.

The other two arms live in their own modules, imported only when their type
is requested, and reuse the helpers below (:func:`precard_caller`,
:func:`execute_caller`, :func:`refuse`, :func:`precard_errors` /
:func:`execute_errors`, :func:`table_card_fields`,
:func:`common_preview_fields`, :func:`strip_column_keys`,
:func:`write_source`).

Conventions (shared by the three handlers):

- A refusal raises ``ValueError(f"{code}: {message}")`` (:func:`refuse`), so
  the dispatch arm returns ``Invalid parameters: <code>: <message>`` the same
  turn, with no card. Errors from the tables layers (``TableError``,
  storage / filter / schema / value errors: anything with a ``.code``) map
  through :func:`precard_errors` with their own code and text, so a hidden
  table keeps the byte-equal ``table_not_found`` text.
- The feature gate is checked first, before any DB work (``tables_disabled``).
- The pre-card caller is the proposal's conversation: ``run_kind`` "slack"
  for a Slack-driven run, else "top_level", ``is_public`` from the
  conversation's project. Public conversations and Slack runs never reach
  here (``_PUBLIC_BLOCKED_LOOP_TOOLS`` / ``ctx.is_slack_origin``); the arms
  still refuse them as defense in depth.
- Execute re-resolves everything with the PROPOSAL's conversation context
  (:func:`execute_caller`, never a UI caller: invariant 1), records
  ``write_source(request_id)`` = ``action_request:<id>`` and the approver as
  actor, and fails with ``RuntimeError(f"{code}: {message}")``
  (:func:`execute_errors`).
- Preview payloads never carry column keys (``c_xxxxxxxx``).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from chat.action_request_types.doc_precard import share_summary
from chat.action_request_types.write_doc import project_is_public
from chat.tables import service
from chat.tables.access import create_table_deny_reason
from chat.tables.constants import TABLE_MAX_PER_OWNER
from chat.tables.filters import FilterError
from chat.tables.schema import (
    Column,
    ColumnSpecError,
    TableSchemaError,
    TableValueError,
    clip_repr,
    parse_columns,
)
from chat.tables.storage import TableStorageError

__all__ = [
    "TABLE_ERRORS",
    "TABLE_REQUEST_TYPES",
    "common_preview_fields",
    "execute_caller",
    "execute_errors",
    "precard_caller",
    "precard_create_table",
    "precard_errors",
    "refuse",
    "strip_column_key",
    "strip_column_keys",
    "table_card_fields",
    "table_precard_check",
    "write_source",
]

TABLE_REQUEST_TYPES = ("create_table", "change_table_schema", "write_table_rows")

# Arms living in their own modules: imported lazily, only when that type is
# requested (a type without a registered handler never reaches the pre-card).
_LAZY_ARMS: dict[str, tuple[str, str]] = {
    "change_table_schema": (
        "chat.action_request_types.table_schema_precard",
        "precard_change_table_schema",
    ),
    "write_table_rows": (
        "chat.action_request_types.table_rows_precard",
        "precard_write_table_rows",
    ),
}

# Every tables-layer refusal carries ``.code`` and model-facing ``str(e)``.
TABLE_ERRORS: tuple[type[Exception], ...] = (
    service.TableError,
    TableStorageError,
    FilterError,
    TableSchemaError,
    TableValueError,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def table_precard_check(
    req_type: str,
    validated_params: dict,
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
    *,
    is_slack_origin: bool = False,
) -> None:
    """Verify a table action-request proposal and enrich ``validated_params``
    with its server-only preview keys.

    Raises:
        ValueError: ``"<code>: <message>"``, surfaced as ``Invalid
            parameters`` by the dispatch arm (no card).
    """
    if req_type == "create_table":
        arm = precard_create_table
    elif req_type in _LAZY_ARMS:
        module_name, attr = _LAZY_ARMS[req_type]
        arm = getattr(importlib.import_module(module_name), attr)
    else:
        raise ValueError(f"Not a table request type: {req_type!r}")
    await arm(
        validated_params, user, project_id, conversation_id,
        is_slack_origin=is_slack_origin,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def refuse(code: str, message: str) -> ValueError:
    """The pre-card refusal: ``ValueError("<code>: <message>")``."""
    return ValueError(f"{code}: {message}")


def _code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "invalid_request"


@contextmanager
def precard_errors() -> Iterator[None]:
    """Map a tables-layer error (:data:`TABLE_ERRORS`) to :func:`refuse`
    with its own code and text."""
    try:
        yield
    except TABLE_ERRORS as exc:
        raise refuse(_code(exc), str(exc)) from None


@contextmanager
def execute_errors() -> Iterator[None]:
    """Map a tables-layer error (:data:`TABLE_ERRORS`) to
    ``RuntimeError("<code>: <message>")`` (the approve route shows it on
    the card, which stays open)."""
    try:
        yield
    except TABLE_ERRORS as exc:
        raise RuntimeError(f"{_code(exc)}: {exc}") from None


def _gate_caller(user: dict, project_id: str | None, conversation_id: str | None,
                 run_kind: str) -> service.Caller:
    # The gate reads only the user; no DB work.
    return service.Caller(
        user=user, conversation_id=conversation_id, project_id=project_id,
        is_public=False, run_kind=run_kind,
    )


async def precard_caller(
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
    *,
    is_slack_origin: bool,
) -> service.Caller:
    """The proposal's caller, gate first.

    Checks the ``tables`` gate before any DB work (``tables_disabled``),
    then derives ``is_public`` from the conversation's project (a missing
    or foreign project refuses with ``project_not_found``). ``run_kind`` is
    ``"slack"`` for a Slack-driven run, else ``"top_level"``.
    """
    run_kind = "slack" if is_slack_origin else "top_level"
    with precard_errors():
        service.require_enabled(_gate_caller(user, project_id, conversation_id, run_kind))
    try:
        is_public = await project_is_public(user, project_id)
    except RuntimeError as exc:
        raise refuse("project_not_found", str(exc)) from None
    return service.Caller(
        user=user, conversation_id=conversation_id, project_id=project_id,
        is_public=is_public, run_kind=run_kind,
    )


async def execute_caller(
    user: dict, project_id: str | None, conversation_id: str | None,
) -> service.Caller:
    """The approve-time caller: the PROPOSAL's conversation context
    (``run_kind="top_level"``), never a UI caller (invariant 1).

    Raises:
        RuntimeError: ``tables_disabled: ...`` (gate closed, before any DB
            work) or ``project_not_found: ...`` (the conversation's project
            row is gone or not the user's).
    """
    with execute_errors():
        service.require_enabled(_gate_caller(user, project_id, conversation_id, "top_level"))
    try:
        is_public = await project_is_public(user, project_id)
    except RuntimeError as exc:
        raise RuntimeError(f"project_not_found: {exc}") from None
    return service.Caller(
        user=user, conversation_id=conversation_id, project_id=project_id,
        is_public=is_public, run_kind="top_level",
    )


def write_source(request_id: int | None) -> str:
    """``action_request:<id>``: the change source and ``last_write_source``
    of an approved table card (credited to the card's proposer through
    ``get_action_request_owners``). The table storage needs the id."""
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise RuntimeError("invalid_args: the approve path must pass the request id.")
    return f"action_request:{request_id}"


def table_card_fields(table: Mapping) -> dict:
    """The server-injected values every card about an existing table shows:
    ``table_title``, ``table_mode``, ``table_scope`` ("user" / "project"),
    ``share_summary`` (counts only) and ``require_approval``."""
    return {
        "table_title": table["title"],
        "table_mode": table["mode"],
        "table_scope": "project" if table.get("project_id") else "user",
        "share_summary": share_summary(table.get("shares") or []),
        "require_approval": bool(table.get("require_approval", False)),
    }


def strip_column_key(column: Column | Mapping) -> dict:
    """A column (:class:`Column` or its dict) as a dict without ``key``."""
    data = column.to_dict() if isinstance(column, Column) else dict(column)
    data.pop("key", None)
    return data


def strip_column_keys(columns: Sequence[Column | Mapping]) -> list[dict]:
    """Columns as key-less dicts (preview payloads never carry keys)."""
    return [strip_column_key(column) for column in columns]


def common_preview_fields(params: Mapping) -> list[dict]:
    """The Table / Mode / Scope / Shares / Approval rows of a card about an
    existing table, from :func:`table_card_fields`' injected keys (write_doc
    style: each row only when its value is present). Never raises."""
    fields: list[dict] = []
    title = params.get("table_title") or params.get("table_id")
    if title:
        fields.append({"key": "Table", "value": str(title)})
    if params.get("table_mode"):
        fields.append({"key": "Mode", "value": str(params["table_mode"])})
    if params.get("table_scope"):
        fields.append({"key": "Scope", "value": str(params["table_scope"])})
    if params.get("share_summary"):
        fields.append({"key": "Shares", "value": str(params["share_summary"])})
    if params.get("require_approval") is True:
        fields.append({"key": "Approval", "value": "required by the owner for every change"})
    return fields


# ---------------------------------------------------------------------------
# create_table
# ---------------------------------------------------------------------------

_MODE_MISMATCH = (
    "This conversation's mode does not match the table's; the table cannot "
    "be created here."
)


def _column_spec_message(exc: ColumnSpecError) -> str:
    """The spec error's text, naming the column when the text does not."""
    message = str(exc)
    if exc.column and clip_repr(exc.column) not in message:
        return f"Column {clip_repr(exc.column)}: {message}"
    return message


async def precard_create_table(
    validated_params: dict,
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
    *,
    is_slack_origin: bool = False,
) -> None:
    """Pre-card arm of ``create_table``.

    Order: gate (``tables_disabled``, no DB work) -> the conversation may
    propose a table (``denied``: public conversations and Slack runs) ->
    ``target: "project"`` needs a project conversation (``invalid_args``)
    and the user's project (``project_not_found``) -> the mode (private;
    a public result refuses ``mode_mismatch``, unreachable because public
    conversations are refused, kept fail-closed) -> title / description
    (``invalid_request``) -> column specs (the ``ColumnSpecError`` code,
    naming the column) -> the title collision in the same ``(owner,
    project, mode)`` scope (``duplicate_title``) -> the per-owner cap
    (``table_limit``), the store's order.

    On success normalizes ``title`` / ``description`` (stripped) and injects
    ``table_mode``, ``project_title`` (None for a user table) and
    ``column_specs`` (the parsed columns as key-less dicts).
    """
    from db import project_store, table_store

    caller = await precard_caller(
        user, project_id, conversation_id, is_slack_origin=is_slack_origin,
    )
    reason = create_table_deny_reason(is_public=caller.is_public, run_kind=caller.run_kind)
    if reason:
        raise refuse("denied", reason)

    target = validated_params.get("target") or "user"
    table_project_id: str | None = None
    project_title: str | None = None
    mode = "private"
    if target == "project":
        if not project_id:
            raise refuse(
                "invalid_args",
                "target 'project' needs a project conversation; use target "
                "'user' for a table of your own.",
            )
        project = await project_store.get_project(user["id"], project_id)
        if project is None:
            raise refuse("project_not_found", "Project not found.")
        table_project_id = project_id
        project_title = project.get("name")
        mode = "public" if project.get("public") else "private"
    if mode == "public":
        raise refuse("mode_mismatch", _MODE_MISMATCH)

    try:
        title = table_store._validate_title(validated_params.get("title"))
        description = table_store._validate_description(validated_params.get("description"))
    except table_store.TableValidationError as exc:
        raise refuse("invalid_request", str(exc)) from None

    try:
        columns = parse_columns(validated_params.get("columns"))
    except ColumnSpecError as exc:
        raise refuse(exc.code, _column_spec_message(exc)) from None

    # Same order as table_store.create_table: the collision, then the cap.
    owner_id = user["id"]
    if await table_store.title_taken(owner_id, table_project_id, mode, title):
        raise refuse(
            "duplicate_title",
            str(table_store._duplicate_title_error(title, table_project_id)),
        )
    if len(await table_store.list_table_ids_for_user(owner_id)) >= TABLE_MAX_PER_OWNER:
        raise refuse(
            "table_limit",
            f"Table limit reached: an owner can have at most "
            f"{TABLE_MAX_PER_OWNER} tables (TABLE_MAX_PER_OWNER).",
        )

    validated_params["title"] = title
    validated_params["description"] = description
    validated_params["table_mode"] = mode
    validated_params["project_title"] = project_title
    validated_params["column_specs"] = strip_column_keys(columns)


def column_summary(columns: Any) -> str:
    """``"<N> columns: Name (type), ..."`` for key-less column dicts; never
    raises on odd input."""
    if not isinstance(columns, list):
        return ""
    parts = []
    for column in columns:
        if isinstance(column, Mapping):
            parts.append(f"{column.get('name', '')} ({column.get('type', '')})")
    count = len(columns)
    noun = "column" if count == 1 else "columns"
    return f"{count} {noun}: {', '.join(parts)}" if parts else f"{count} {noun}"
