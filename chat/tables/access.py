"""The Quest Tables access rule -- the ONE place the table matrix lives.

Invariant 3 of the Quest Tables spec: every access decision about a table
goes through :func:`resolve_table_access`. The model-facing tools, the HTTP
routes (``run_kind="ui"``), the ``create_table`` / ``change_table_schema`` /
``write_table_rows`` action requests (pre-card AND approve time),
``list_tables`` and the prompts consume the returned :class:`TableAccess`;
none of them re-derive any part of the rule below.

The visibility and row-write procedure is the Quest Docs one, shared code
in ``chat/artifact_access.py`` (rules 1-7 of ``chat/docs/access.py`` apply
word for word with "table" for "doc", ``write_table_rows`` for
``write_doc``, and "Project Access" for the project's doc sources,
still ``project_doc_sources`` / ``doc_source_project_ids``). This module
adds the table wording and the second verdict, ``schema``. The matrix
(columns as in docs; the last column covers sub_agent, inference_api,
user_subagent and script runs)::

    | Table             | Private owner  | Private recipient | Public owner   | Public recipient | Read-only |
    |-------------------|----------------|-------------------|----------------|------------------|-----------|
    | Private, unshared | rows Free      | n/a (Hidden)      | Hidden         | n/a (Hidden)     | Read      |
    |                   | schema Appr.   |                   |                |                  |           |
    | Private, shared   | rows Appr.     | read: Read        | Hidden         | Hidden           | Read      |
    |                   | schema Appr.   | write: rows Appr. |                |                  |           |
    |                   |                |   schema Appr.    |                |                  |           |
    | Public, unshared  | Read           | n/a (Hidden)      | rows Free      | n/a (Hidden)     | Read (*)  |
    |                   |                |                   | schema Appr.   |                  |           |
    | Public, shared    | Read           | Read              | rows Free      | read: Read       | Read (*)  |
    |                   |                |                   | schema Appr.   | write: rows Free |           |
    |                   |                |                   |                |   schema Appr.   |           |

(*) except ``script``: scripts see user tables only (never a project
table), and public tables are always project tables, so Hidden.

As for docs (rule 2), the owner's standalone conversations see the tables
of all of the owner's projects read-only ("Read" in both verdicts, with
:data:`DENY_OUTSIDE_PROJECT`, or the public-from-private wording for a
public table).

"Read" = rows AND schema denied, both with the same read-only reason.

The ``schema`` verdict is never ``"free"`` (invariant 5: no model-facing
tool mutates a schema; the only schema writers are the card executors and
the UI routes). It is decided after the rows verdict, in this order:

1. Hidden -> denied, no reason (the whole verdict is :data:`HIDDEN`).
2. A read-only rows verdict (read-only run kind, read share, public table
   from a private conversation, the owner's project table from a
   standalone conversation, UI without a write relationship) -> denied
   with the rows reason.
3. ``ui`` -> ``"approval"`` for the owner (the owner's own Save in the
   schema editor is the approval; the UI routes write the schema
   directly), denied (:data:`DENY_SCHEMA_UI_NOT_OWNER`) for a write-share
   recipient: schema edits are owner-only (spec section 7). Routes still
   check ownership themselves for every owner-only action (rename,
   ``require_approval``, delete, shares); ``schema != "denied"`` in the UI
   is equivalent to "is the owner". The UI restore route (phase A16:
   restore-to-point, open to editors = owner or write share, and
   rewinding schema changes too, spec 4.5 and 7) gates on ``rows ==
   "free"`` with ``run_kind="ui"``, NOT on ``schema``: a write-share
   recipient may restore past a schema change it cannot make.
4. Slack-driven runs -> denied (:data:`DENY_SCHEMA_SLACK`): no cards there;
   the model asks the user to make the change in the Quest UI.
5. Otherwise (a top-level conversation with a write relationship, private
   or public) -> ``"approval"`` (a ``change_table_schema`` card). A public
   conversation opens the table cards: they are public-safe action
   request types (``PUBLIC_ACTION_REQUEST_TYPES`` in
   ``chat/llm/tool_schemas.py``), and a card proposed from a public
   conversation only ever reaches a public table (invariant 1).

The owner's ``require_approval`` switch changes the rows verdict only
(docs rule 7: Free -> Approval tagged ``required_by_owner``; denied in
Slack runs, where no card can open); the schema is gated already.

Unlike :class:`chat.docs.access.DocAccess`, a verdict carries two deny
reasons, ``deny_reason`` for rows and ``schema_deny_reason`` for the
schema, because the two can differ (rows Free with schema Denied in a
Slack run). ``approval_reason`` (``"shared"`` or ``"required_by_owner"``)
tags a rows ``"approval"``; a schema approval needs no reason (every
schema change is gated).

Creation (``create_table`` cards) is for top-level conversations only,
private or public: :func:`create_table_deny_reason` (a public conversation
creates only public tables, i.e. tables of its own project; the pre-card
refuses ``target="user"`` there). A hidden table and a missing id
produce the same text: :func:`not_found_message`, built on
``chat.tables.constants.table_not_found_message``.

Callers pass ``is_public=False`` and ``project_id=None`` for the ``ui`` and
``script`` run kinds, and the empty default ``doc_source_project_ids`` for
everything but a private project conversation.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from chat import artifact_access
from chat.docs.access import READ_ONLY_RUN_KINDS, RUN_KINDS, WRITE_VERDICTS, WriteVerdict
from chat.tables.constants import TABLE_MODES, table_not_found_message

__all__ = [
    "APPROVAL_REASONS",
    "APPROVAL_REQUIRED_BY_OWNER_MESSAGE",
    "APPROVAL_REQUIRED_MESSAGE",
    "APPROVAL_REQUIRED_WRITE_NOTE",
    "APPROVAL_WRITE_NOTE",
    "DENY_INFERENCE_API",
    "DENY_OUTSIDE_PROJECT",
    "DENY_PUBLIC_TABLE_FROM_PRIVATE",
    "DENY_READ_ONLY_SHARE",
    "DENY_SCHEMA_SLACK",
    "DENY_SCHEMA_UI_NOT_OWNER",
    "DENY_SCRIPT",
    "DENY_SLACK_APPROVAL_REQUIRED",
    "DENY_SLACK_NEEDS_APPROVAL",
    "DENY_SUB_AGENT",
    "DENY_UI_READ_ONLY",
    "DENY_USER_SUBAGENT",
    "HIDDEN",
    "READ_ONLY_RUN_KINDS",
    "RUN_KINDS",
    "SCHEMA_APPROVAL_MESSAGE",
    "SCHEMA_APPROVAL_NOTE",
    "SCHEMA_VERDICTS",
    "TableAccess",
    "approval_required_message",
    "create_table_deny_reason",
    "effective_share",
    "not_found_message",
    "resolve_table_access",
    "schema_note",
    "write_note",
]

SchemaVerdict = Literal["approval", "denied"]
SCHEMA_VERDICTS = ("approval", "denied")
ApprovalReason = Literal["shared", "required_by_owner"]
APPROVAL_REASONS = ("shared", "required_by_owner")

assert tuple(TABLE_MODES) == artifact_access.MODES

# --- Rows deny reasons (model/user-facing; never name a hidden table) ------

DENY_SUB_AGENT = "Only the top-level agent writes tables; return the content to it."
DENY_INFERENCE_API = "Inference API runs are read-only; tables cannot be written here."
DENY_USER_SUBAGENT = "Cross-user subagent runs are read-only; return the content to the caller."
DENY_SCRIPT = (
    "Sandbox scripts have read-only access to tables; the agent writes tables "
    "with the table tools."
)
DENY_READ_ONLY_SHARE = "You have read-only access to this shared table."
DENY_PUBLIC_TABLE_FROM_PRIVATE = (
    "Public tables are written only from public-project conversations."
)
DENY_OUTSIDE_PROJECT = (
    "This table belongs to a project; its rows and columns are changed only "
    "from that project's conversations or in the Quest UI. Here it is read-only."
)
DENY_SLACK_NEEDS_APPROVAL = (
    "This table is shared, so changes need an approval card, which Slack-driven "
    "conversations cannot open. Continue from the Quest web UI."
)
DENY_SLACK_APPROVAL_REQUIRED = (
    "The owner requires approval for every change to this table, and "
    "Slack-driven conversations cannot open approval cards. Continue from "
    "the Quest web UI."
)
DENY_UI_READ_ONLY = "You have read-only access to this table."

# --- Schema deny reasons (beyond the read-only ones above) -----------------

DENY_SCHEMA_SLACK = (
    "Slack-driven conversations cannot open approval cards, so they cannot "
    "create tables or change a table's columns. Ask the user to make the "
    "change in the Quest web UI (Tables)."
)
DENY_SCHEMA_UI_NOT_OWNER = "Only the table's owner can change its columns."

# --- Notes and approval_required messages ----------------------------------

APPROVAL_WRITE_NOTE = "shared private table: use create_action_request(write_table_rows)"
APPROVAL_REQUIRED_WRITE_NOTE = (
    "owner requires approval for every change: use create_action_request(write_table_rows)"
)
SCHEMA_APPROVAL_NOTE = (
    "column changes need approval: use create_action_request(change_table_schema)"
)

# spec 6.2: the message of every approval_required row-write tool result
# (the second wording when the owner's switch produced the verdict).
APPROVAL_REQUIRED_MESSAGE = (
    'This table is shared; propose the change with '
    'create_action_request(request_type="write_table_rows", ...)'
)
APPROVAL_REQUIRED_BY_OWNER_MESSAGE = (
    'The owner requires approval for every change to this table; propose the '
    'change with create_action_request(request_type="write_table_rows", ...)'
)
SCHEMA_APPROVAL_MESSAGE = (
    "Column changes need approval; propose them with "
    'create_action_request(request_type="change_table_schema", ...)'
)

# artifact_access deny cause -> table wording.
_DENY_TEXT = {
    "sub_agent": DENY_SUB_AGENT,
    "inference_api": DENY_INFERENCE_API,
    "user_subagent": DENY_USER_SUBAGENT,
    "script": DENY_SCRIPT,
    "read_only_share": DENY_READ_ONLY_SHARE,
    "public_from_private": DENY_PUBLIC_TABLE_FROM_PRIVATE,
    "outside_project": DENY_OUTSIDE_PROJECT,
    "slack_needs_approval": DENY_SLACK_NEEDS_APPROVAL,
    "slack_approval_required": DENY_SLACK_APPROVAL_REQUIRED,
    "ui_read_only": DENY_UI_READ_ONLY,
}
assert set(_DENY_TEXT) == set(artifact_access.DENY_CAUSES)

# Causes that make the caller read-only on the table as a whole (schema
# rule 2). The Slack approval causes are row-only: the schema verdict
# there comes from rule 4.
_READ_ONLY_CAUSES = frozenset({
    *READ_ONLY_RUN_KINDS,
    "read_only_share",
    "public_from_private",
    "outside_project",
    "ui_read_only",
})


@dataclass(frozen=True)
class TableAccess:
    """What one caller may do with one table.

    ``visible=False`` means "behave as if the table does not exist"; such a
    verdict grants nothing and carries no reasons (:data:`HIDDEN`).
    For a visible table: ``deny_reason`` is set exactly when ``rows`` is
    ``"denied"``, ``schema_deny_reason`` exactly when ``schema`` is
    ``"denied"``, and ``approval_reason`` exactly when ``rows`` is
    ``"approval"``.
    """

    visible: bool
    can_read: bool
    rows: WriteVerdict
    schema: SchemaVerdict
    deny_reason: str | None
    approval_reason: ApprovalReason | None
    schema_deny_reason: str | None

    def __post_init__(self) -> None:
        if self.rows not in WRITE_VERDICTS:
            raise ValueError(f"unknown rows verdict: {self.rows!r}")
        if self.schema not in SCHEMA_VERDICTS:
            # Invariant 5: "free" is never a schema verdict.
            raise ValueError(f"unknown schema verdict: {self.schema!r}")
        if self.approval_reason is not None and self.approval_reason not in APPROVAL_REASONS:
            raise ValueError(f"unknown approval_reason: {self.approval_reason!r}")
        if not self.visible:
            if (
                self.can_read
                or self.rows != "denied"
                or self.schema != "denied"
                or self.deny_reason is not None
                or self.schema_deny_reason is not None
                or self.approval_reason is not None
            ):
                raise ValueError("a hidden table grants nothing and carries no reasons")
            return
        if not self.can_read:
            raise ValueError("a visible table is always readable")
        if (self.deny_reason is not None) != (self.rows == "denied"):
            raise ValueError("deny_reason is set exactly when rows is denied")
        if (self.schema_deny_reason is not None) != (self.schema == "denied"):
            raise ValueError("schema_deny_reason is set exactly when schema is denied")
        if (self.approval_reason is not None) != (self.rows == "approval"):
            raise ValueError("approval_reason is set exactly when rows is approval")


HIDDEN = TableAccess(
    visible=False, can_read=False, rows="denied", schema="denied",
    deny_reason=None, approval_reason=None, schema_deny_reason=None,
)


def effective_share(table: Mapping[str, Any], user_id: int) -> str | None:
    """The permission ``user_id`` holds through ``table["shares"]``
    (own row or the everyone row, higher wins); None without one. A missing
    or None ``shares`` key raises ValueError (fail closed)."""
    return artifact_access.effective_share(table, user_id, kind="table")


def resolve_table_access(
    table: Mapping[str, Any],
    *,
    user_id: int,
    is_public: bool,
    project_id: str | None,
    run_kind: str,
    doc_source_project_ids: Collection[str] = (),
) -> TableAccess:
    """Decide visibility, readability and the rows and schema verdicts.

    ``table`` is the ``db.table_store`` dict including ``shares`` (missing
    or None ``shares`` raises -- never fail open on a partial dict).
    ``is_public`` / ``project_id`` describe the calling conversation;
    ``run_kind`` is one of :data:`RUN_KINDS`; ``doc_source_project_ids``
    are the public projects whose public tables the calling (private)
    project may read, empty unless the caller is a private project
    conversation.

    Raises ValueError for an unknown ``run_kind`` or table mode, or a table
    dict without its ``shares`` list.
    """
    core = artifact_access.resolve_core(
        table,
        user_id=user_id,
        is_public=is_public,
        project_id=project_id,
        run_kind=run_kind,
        source_project_ids=doc_source_project_ids,
        kind="table",
    )
    if not core.visible:
        return HIDDEN

    deny_reason = _DENY_TEXT[core.cause] if core.write == "denied" else None
    approval_reason: ApprovalReason | None = None
    if core.write == "approval":
        approval_reason = "required_by_owner" if core.required_by_owner else "shared"

    # Schema rules 2-5 (module docstring).
    schema: SchemaVerdict = "denied"
    if core.write == "denied" and core.cause in _READ_ONLY_CAUSES:
        schema_deny_reason: str | None = deny_reason
    elif run_kind == "ui":
        if core.is_owner:
            schema, schema_deny_reason = "approval", None
        else:
            schema_deny_reason = DENY_SCHEMA_UI_NOT_OWNER
    elif run_kind == "slack":
        schema_deny_reason = DENY_SCHEMA_SLACK
    else:
        schema, schema_deny_reason = "approval", None

    return TableAccess(
        visible=True,
        can_read=True,
        rows=core.write,
        schema=schema,
        deny_reason=deny_reason,
        approval_reason=approval_reason,
        schema_deny_reason=schema_deny_reason,
    )


def write_note(access: TableAccess) -> str | None:
    """Short rows ``write_note`` for list/get results: None when writes are
    free (or the table is hidden)."""
    if access.rows == "free":
        return None
    if access.rows == "approval":
        if access.approval_reason == "required_by_owner":
            return APPROVAL_REQUIRED_WRITE_NOTE
        return APPROVAL_WRITE_NOTE
    return access.deny_reason


def schema_note(access: TableAccess) -> str | None:
    """Short note for the ``schema_change`` field: the card to propose, or
    the reason column changes are denied (None for a hidden table)."""
    if access.schema == "approval":
        return SCHEMA_APPROVAL_NOTE
    return access.schema_deny_reason


def approval_required_message(
    access: TableAccess, request_type: str = "write_table_rows",
) -> str:
    """The ``approval_required`` message (spec 6.2).

    ``write_table_rows``: worded by ``access.approval_reason`` (shared vs
    the owner's switch). ``change_table_schema``: the schema wording (every
    schema change is gated, whatever the rows verdict). Raises ValueError
    for any other request type.
    """
    if request_type == "write_table_rows":
        if access.approval_reason == "required_by_owner":
            return APPROVAL_REQUIRED_BY_OWNER_MESSAGE
        return APPROVAL_REQUIRED_MESSAGE
    if request_type == "change_table_schema":
        return SCHEMA_APPROVAL_MESSAGE
    raise ValueError(f"no approval_required message for request_type {request_type!r}")


def not_found_message(table_id: str, access: TableAccess | None) -> str | None:
    """The not-found text for a missing (``access=None``) or hidden table,
    else None.

    Invariant 2: a hidden table and a nonexistent id produce byte-identical
    text (``table_not_found_message``). Callers resolve access only when
    the store returned a table, and do the same lookups (doc sources) on
    both paths.
    """
    if access is None or not access.visible:
        return table_not_found_message(table_id)
    return None


def create_table_deny_reason(*, is_public: bool, run_kind: str) -> str | None:
    """Why this caller may not propose a ``create_table`` card, or None.

    Only top-level conversations propose them (spec 5.1 "Creation"); the
    table is created in the conversation's mode (private from a standalone
    conversation or one of a private project, public from a public
    project's conversation, where only ``target="project"`` is possible
    since user tables are private). ``is_public`` is accepted for the
    signature's symmetry with the rows rule; it never denies. The UI
    creates tables through ``POST /tables``, not a card, so
    ``run_kind="ui"`` raises ValueError, as does an unknown run kind.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError(f"unknown run_kind: {run_kind!r}")
    if run_kind == "ui":
        raise ValueError("the UI creates tables directly, not through a card")
    if run_kind in READ_ONLY_RUN_KINDS:
        return _DENY_TEXT[run_kind]
    if run_kind == "slack":
        return DENY_SCHEMA_SLACK
    return None
