"""Quest Tables access rule: every cell of the spec's 5.1 matrix for both
verdict columns (rows and schema), every run_kind, the owner's
require-approval switch, project and doc-source visibility, and the
hidden-vs-missing parity, pinned on
:func:`chat.tables.access.resolve_table_access`.
"""

import itertools

import pytest

from chat.docs import access as doc_access
from chat.docs.access import resolve_doc_access
from chat.tables.access import (
    APPROVAL_REQUIRED_BY_OWNER_MESSAGE,
    APPROVAL_REQUIRED_MESSAGE,
    APPROVAL_REQUIRED_WRITE_NOTE,
    APPROVAL_WRITE_NOTE,
    DENY_INFERENCE_API,
    DENY_OUTSIDE_PROJECT,
    DENY_PUBLIC_APPROVAL_REQUIRED,
    DENY_PUBLIC_TABLE_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCHEMA_PUBLIC,
    DENY_SCHEMA_SLACK,
    DENY_SCHEMA_UI_NOT_OWNER,
    DENY_SCRIPT,
    DENY_SLACK_APPROVAL_REQUIRED,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
    DENY_UI_READ_ONLY,
    DENY_USER_SUBAGENT,
    HIDDEN,
    READ_ONLY_RUN_KINDS,
    RUN_KINDS,
    SCHEMA_APPROVAL_MESSAGE,
    SCHEMA_APPROVAL_NOTE,
    TableAccess,
    approval_required_message,
    create_table_deny_reason,
    effective_share,
    not_found_message,
    resolve_table_access,
    schema_note,
    write_note,
)
from chat.tables.constants import TABLE_MODES, table_not_found_message

OWNER = 1
RECIPIENT = 2
STRANGER = 3
PROJECT = "11111111-1111-4111-8111-111111111111"
OTHER_PROJECT = "22222222-2222-4222-8222-222222222222"
TABLE_ID = "33333333-3333-4333-8333-333333333333"


def _table(mode, shares=(), *, project_id=None, owner_id=OWNER, require_approval=False):
    """A ``db.table_store._table_to_dict`` shaped dict."""
    return {
        "id": TABLE_ID,
        "owner_id": owner_id,
        "project_id": project_id,
        "title": "Leads",
        "description": "",
        "mode": mode,
        "schema": [],
        "schema_version": 1,
        "row_count": 0,
        "last_write_source": None,
        "require_approval": require_approval,
        "created_at": "2026-10-15T00:00:00",
        "updated_at": "2026-10-15T00:00:00",
        "shares": [
            {"id": i, "user_id": user_id, "permission": permission,
             "created_at": "2026-10-15T00:00:00"}
            for i, (user_id, permission) in enumerate(shares, start=1)
        ],
    }


def _v(rows, schema, *, deny=None, approval=None, schema_deny=None):
    return TableAccess(
        visible=True, can_read=True, rows=rows, schema=schema, deny_reason=deny,
        approval_reason=approval, schema_deny_reason=schema_deny,
    )


H = HIDDEN
# Private convo, owner of an unshared private table.
FREE_AR = _v("free", "approval")
# Shared private table: rows card (shared), schema card.
AR_AR = _v("approval", "approval", approval="shared")
AR_REQ_AR = _v("approval", "approval", approval="required_by_owner")
# Public convo with a write relationship: rows free, schema denied.
FREE_PUB = _v("free", "denied", schema_deny=DENY_SCHEMA_PUBLIC)
# Slack with a write relationship on an unshared private table.
FREE_SLACK = _v("free", "denied", schema_deny=DENY_SCHEMA_SLACK)
# UI editors.
UI_OWNER = _v("free", "approval")
UI_WRITER = _v("free", "denied", schema_deny=DENY_SCHEMA_UI_NOT_OWNER)


def RO(reason):
    """Read: rows and schema both denied with the same reason."""
    return _v("denied", "denied", deny=reason, schema_deny=reason)


def SLACK_DENIED(reason):
    """Slack rows denial (no cards) with the schema's Slack reason."""
    return _v("denied", "denied", deny=reason, schema_deny=DENY_SCHEMA_SLACK)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

# "private" rows are user tables (always private); "public" rows are tables
# of the public project PROJECT (the only kind of public table).
TABLE_ROWS = {
    "private_unshared": _table("private"),
    "private_shared_read": _table("private", [(RECIPIENT, "read")]),
    "private_shared_write": _table("private", [(RECIPIENT, "write")]),
    "private_shared_everyone_read": _table("private", [(None, "read")]),
    "private_shared_everyone_write": _table("private", [(None, "write")]),
    "public_unshared": _table("public", project_id=PROJECT),
    "public_shared_read": _table("public", [(RECIPIENT, "read")], project_id=PROJECT),
    "public_shared_write": _table("public", [(RECIPIENT, "write")], project_id=PROJECT),
    "public_shared_everyone_write": _table("public", [(None, "write")], project_id=PROJECT),
    "legacy_public_user_shared_write": _table("public", [(RECIPIENT, "write")]),
}

# column -> (user_id, is_public, run_kind, project_id)
COLUMNS = {
    "owner_private": (OWNER, False, "top_level", PROJECT),
    "recipient_private": (RECIPIENT, False, "top_level", PROJECT),
    "owner_public": (OWNER, True, "top_level", PROJECT),
    "recipient_public": (RECIPIENT, True, "top_level", PROJECT),
    "stranger_private": (STRANGER, False, "top_level", PROJECT),
    "stranger_public": (STRANGER, True, "top_level", PROJECT),
    "owner_sub_agent": (OWNER, False, "sub_agent", PROJECT),
    "owner_inference_api": (OWNER, False, "inference_api", PROJECT),
    "owner_user_subagent": (OWNER, False, "user_subagent", PROJECT),
    "owner_script": (OWNER, False, "script", None),
    "recipient_sub_agent": (RECIPIENT, False, "sub_agent", PROJECT),
    "recipient_inference_api": (RECIPIENT, False, "inference_api", PROJECT),
    "recipient_user_subagent": (RECIPIENT, False, "user_subagent", PROJECT),
    "recipient_script": (RECIPIENT, False, "script", None),
    "owner_slack": (OWNER, False, "slack", PROJECT),
    "recipient_slack": (RECIPIENT, False, "slack", PROJECT),
    "ui_owner": (OWNER, False, "ui", None),
    "ui_recipient": (RECIPIENT, False, "ui", None),
}

READ_ONLY_COLUMNS = {
    "owner_sub_agent": RO(DENY_SUB_AGENT),
    "owner_inference_api": RO(DENY_INFERENCE_API),
    "owner_user_subagent": RO(DENY_USER_SUBAGENT),
    "owner_script": RO(DENY_SCRIPT),
}
# Scripts see user tables only; public tables are project tables.
PUBLIC_READ_ONLY_COLUMNS = {**READ_ONLY_COLUMNS, "owner_script": H}

# A share recipient (read or write share alike) in a read-only run kind.
RECIPIENT_READ_ONLY_COLUMNS = {
    "recipient_sub_agent": RO(DENY_SUB_AGENT),
    "recipient_inference_api": RO(DENY_INFERENCE_API),
    "recipient_user_subagent": RO(DENY_USER_SUBAGENT),
    "recipient_script": RO(DENY_SCRIPT),
}
RECIPIENT_PUBLIC_READ_ONLY_COLUMNS = {
    "recipient_sub_agent": RO(DENY_SUB_AGENT),
    "recipient_inference_api": RO(DENY_INFERENCE_API),
    "recipient_user_subagent": RO(DENY_USER_SUBAGENT),
    "recipient_script": H,
}
# No share: the recipient has no relationship at all.
RECIPIENT_READ_ONLY_HIDDEN = {
    "recipient_sub_agent": H,
    "recipient_inference_api": H,
    "recipient_user_subagent": H,
    "recipient_script": H,
}

_PRIVATE_HIDDEN_PUBLIC = {"owner_public": H, "recipient_public": H, "stranger_public": H}

EXPECTED = {
    "private_unshared": {
        "owner_private": FREE_AR,
        "recipient_private": H,
        **_PRIVATE_HIDDEN_PUBLIC,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_HIDDEN,
        "owner_slack": FREE_SLACK,
        "recipient_slack": H,
        "ui_owner": UI_OWNER,
        "ui_recipient": H,
    },
    "private_shared_read": {
        "owner_private": AR_AR,
        "recipient_private": RO(DENY_READ_ONLY_SHARE),
        **_PRIVATE_HIDDEN_PUBLIC,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_COLUMNS,
        "owner_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": RO(DENY_READ_ONLY_SHARE),
        "ui_owner": UI_OWNER,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "private_shared_write": {
        "owner_private": AR_AR,
        "recipient_private": AR_AR,
        **_PRIVATE_HIDDEN_PUBLIC,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_COLUMNS,
        "owner_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "ui_owner": UI_OWNER,
        "ui_recipient": UI_WRITER,
    },
    "private_shared_everyone_read": {
        "owner_private": AR_AR,
        "recipient_private": RO(DENY_READ_ONLY_SHARE),
        **_PRIVATE_HIDDEN_PUBLIC,
        # The everyone row makes every user a read recipient.
        "stranger_private": RO(DENY_READ_ONLY_SHARE),
        **READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_COLUMNS,
        "owner_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": RO(DENY_READ_ONLY_SHARE),
        "ui_owner": UI_OWNER,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "private_shared_everyone_write": {
        "owner_private": AR_AR,
        "recipient_private": AR_AR,
        **_PRIVATE_HIDDEN_PUBLIC,
        "stranger_private": AR_AR,
        **READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_COLUMNS,
        "owner_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": SLACK_DENIED(DENY_SLACK_NEEDS_APPROVAL),
        "ui_owner": UI_OWNER,
        "ui_recipient": UI_WRITER,
    },
    # Public rows x private-conversation columns (project_id=PROJECT,
    # is_public=False) cannot occur in practice: a public project has no
    # private conversations. The real path, a private project reading a
    # doc-source public table, is test_doc_source_public_tables_read_only_*.
    "public_unshared": {
        "owner_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_private": H,
        "owner_public": FREE_PUB,
        "recipient_public": H,
        "stranger_public": H,
        "stranger_private": H,
        **PUBLIC_READ_ONLY_COLUMNS,
        **RECIPIENT_READ_ONLY_HIDDEN,
        "owner_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_slack": H,
        "ui_owner": UI_OWNER,
        "ui_recipient": H,
    },
    "public_shared_read": {
        "owner_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "owner_public": FREE_PUB,
        "recipient_public": RO(DENY_READ_ONLY_SHARE),
        "stranger_public": H,
        "stranger_private": H,
        **PUBLIC_READ_ONLY_COLUMNS,
        **RECIPIENT_PUBLIC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "ui_owner": UI_OWNER,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "public_shared_write": {
        "owner_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "owner_public": FREE_PUB,
        "recipient_public": FREE_PUB,
        "stranger_public": H,
        "stranger_private": H,
        **PUBLIC_READ_ONLY_COLUMNS,
        **RECIPIENT_PUBLIC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "ui_owner": UI_OWNER,
        "ui_recipient": UI_WRITER,
    },
    "public_shared_everyone_write": {
        "owner_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "owner_public": FREE_PUB,
        "recipient_public": FREE_PUB,
        "stranger_public": FREE_PUB,
        "stranger_private": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        **PUBLIC_READ_ONLY_COLUMNS,
        **RECIPIENT_PUBLIC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "recipient_slack": RO(DENY_PUBLIC_TABLE_FROM_PRIVATE),
        "ui_owner": UI_OWNER,
        "ui_recipient": UI_WRITER,
    },
}
# A user table stored "public" (cannot happen; defensive) is private.
EXPECTED["legacy_public_user_shared_write"] = EXPECTED["private_shared_write"]


def test_expected_table_covers_every_cell():
    assert set(EXPECTED) == set(TABLE_ROWS)
    for row in EXPECTED.values():
        assert set(row) == set(COLUMNS)


def test_columns_cover_every_run_kind():
    assert {kind for _u, _p, kind, _pid in COLUMNS.values()} == set(RUN_KINDS)


@pytest.mark.parametrize("row,column", [(r, c) for r in TABLE_ROWS for c in COLUMNS])
def test_matrix_cell(row, column):
    user_id, is_public, run_kind, project_id = COLUMNS[column]
    access = resolve_table_access(
        TABLE_ROWS[row], user_id=user_id, is_public=is_public,
        project_id=project_id, run_kind=run_kind,
    )
    assert access == EXPECTED[row][column], (row, column)


@pytest.mark.parametrize("row,column", [(r, c) for r in TABLE_ROWS for c in COLUMNS])
def test_rows_verdict_matches_the_docs_rule(row, column):
    """The rows column is the docs matrix (shared procedure): same
    visibility, same verdict, same approval split."""
    user_id, is_public, run_kind, project_id = COLUMNS[column]
    kwargs = dict(user_id=user_id, is_public=is_public, project_id=project_id,
                  run_kind=run_kind)
    table = resolve_table_access(TABLE_ROWS[row], **kwargs)
    doc = resolve_doc_access(TABLE_ROWS[row], **kwargs)
    assert table.visible == doc.visible
    assert table.rows == doc.write
    assert (table.approval_reason == "required_by_owner") == doc.required_by_owner


# ---------------------------------------------------------------------------
# Every run kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_every_run_kind_on_owner_private_table(run_kind):
    table = _table("private", project_id=PROJECT)
    project_id = None if run_kind in ("script", "ui") else PROJECT
    access = resolve_table_access(
        table, user_id=OWNER, is_public=False, project_id=project_id, run_kind=run_kind,
    )
    if run_kind == "script":
        # Scripts never see a project table.
        assert access == HIDDEN
    elif run_kind in READ_ONLY_RUN_KINDS:
        assert access == READ_ONLY_COLUMNS[f"owner_{run_kind}"]
    elif run_kind == "slack":
        assert access == FREE_SLACK
    elif run_kind == "ui":
        assert access == UI_OWNER
    else:
        assert access == FREE_AR


@pytest.mark.parametrize("run_kind", sorted(READ_ONLY_RUN_KINDS))
@pytest.mark.parametrize("row", sorted(TABLE_ROWS))
def test_read_only_run_kinds_in_public_context(run_kind, row):
    """Read-only kinds read public tables of their project and never see
    private ones (every user table included)."""
    table = TABLE_ROWS[row]
    project_id = None if run_kind == "script" else PROJECT
    access = resolve_table_access(
        table, user_id=OWNER, is_public=True, project_id=project_id, run_kind=run_kind,
    )
    if table["project_id"] is None or table["mode"] == "private":
        assert access == HIDDEN
    else:
        assert access == PUBLIC_READ_ONLY_COLUMNS[f"owner_{run_kind}"]


def test_script_reads_user_tables_only():
    assert resolve_table_access(
        _table("private"), user_id=OWNER, is_public=False, project_id=None,
        run_kind="script",
    ) == RO(DENY_SCRIPT)
    for mode in TABLE_MODES:
        assert resolve_table_access(
            _table(mode, project_id=PROJECT), user_id=OWNER, is_public=False,
            project_id=None, run_kind="script", doc_source_project_ids={PROJECT},
        ) == HIDDEN


# ---------------------------------------------------------------------------
# The owner's require_approval switch
# ---------------------------------------------------------------------------


# The owner's require_approval switch, written out: rows Free -> Approval
# (required_by_owner) in private conversations and denied in public and
# Slack ones; a share-based Approval takes the switch's tag. Every cell not
# listed here (reads, Hidden, read-only kinds, the UI) is unchanged.
AR_SW = AR_REQ_AR
SLACK_SW = SLACK_DENIED(DENY_SLACK_APPROVAL_REQUIRED)
PUB_SW = _v("denied", "denied", deny=DENY_PUBLIC_APPROVAL_REQUIRED,
            schema_deny=DENY_SCHEMA_PUBLIC)
SWITCH_OVERRIDES = {
    ("private_unshared", "owner_private"): AR_SW,
    ("private_unshared", "owner_slack"): SLACK_SW,
    ("private_shared_read", "owner_private"): AR_SW,
    ("private_shared_read", "owner_slack"): SLACK_SW,
    ("private_shared_write", "owner_private"): AR_SW,
    ("private_shared_write", "recipient_private"): AR_SW,
    ("private_shared_write", "owner_slack"): SLACK_SW,
    ("private_shared_write", "recipient_slack"): SLACK_SW,
    ("private_shared_everyone_read", "owner_private"): AR_SW,
    ("private_shared_everyone_read", "owner_slack"): SLACK_SW,
    ("private_shared_everyone_write", "owner_private"): AR_SW,
    ("private_shared_everyone_write", "recipient_private"): AR_SW,
    ("private_shared_everyone_write", "stranger_private"): AR_SW,
    ("private_shared_everyone_write", "owner_slack"): SLACK_SW,
    ("private_shared_everyone_write", "recipient_slack"): SLACK_SW,
    ("public_unshared", "owner_public"): PUB_SW,
    ("public_shared_read", "owner_public"): PUB_SW,
    ("public_shared_write", "owner_public"): PUB_SW,
    ("public_shared_write", "recipient_public"): PUB_SW,
    ("public_shared_everyone_write", "owner_public"): PUB_SW,
    ("public_shared_everyone_write", "recipient_public"): PUB_SW,
    ("public_shared_everyone_write", "stranger_public"): PUB_SW,
    ("legacy_public_user_shared_write", "owner_private"): AR_SW,
    ("legacy_public_user_shared_write", "recipient_private"): AR_SW,
    ("legacy_public_user_shared_write", "owner_slack"): SLACK_SW,
    ("legacy_public_user_shared_write", "recipient_slack"): SLACK_SW,
}


@pytest.mark.parametrize("row,column", [(r, c) for r in TABLE_ROWS for c in COLUMNS])
def test_matrix_cell_with_require_approval(row, column):
    user_id, is_public, run_kind, project_id = COLUMNS[column]
    table = {**TABLE_ROWS[row], "require_approval": True}
    access = resolve_table_access(
        table, user_id=user_id, is_public=is_public, project_id=project_id,
        run_kind=run_kind,
    )
    expected = SWITCH_OVERRIDES.get((row, column), EXPECTED[row][column])
    assert access == expected, (row, column)


def test_switch_overrides_are_real_changes_in_conversation_columns():
    for (row, column), verdict in SWITCH_OVERRIDES.items():
        assert row in TABLE_ROWS and column in COLUMNS
        assert verdict != EXPECTED[row][column], (row, column)
        assert COLUMNS[column][2] in ("top_level", "slack")


def test_require_approval_private_public_slack():
    table = _table("private", require_approval=True)
    private = resolve_table_access(
        table, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    )
    assert private.rows == "approval"
    assert private.approval_reason == "required_by_owner"
    assert private.schema == "approval"
    slack = resolve_table_access(
        table, user_id=OWNER, is_public=False, project_id=None, run_kind="slack",
    )
    assert (slack.rows, slack.deny_reason) == ("denied", DENY_SLACK_APPROVAL_REQUIRED)
    public = resolve_table_access(
        _table("public", project_id=PROJECT, require_approval=True),
        user_id=OWNER, is_public=True, project_id=PROJECT, run_kind="top_level",
    )
    assert (public.rows, public.deny_reason) == ("denied", DENY_PUBLIC_APPROVAL_REQUIRED)
    assert (public.schema, public.schema_deny_reason) == ("denied", DENY_SCHEMA_PUBLIC)
    # A person in the UI is never approval-gated.
    assert resolve_table_access(
        table, user_id=OWNER, is_public=False, project_id=None, run_kind="ui",
    ) == UI_OWNER


def test_require_approval_off_or_absent_is_the_plain_matrix():
    for value in (False, 0, None):
        table = {**_table("private"), "require_approval": value}
        assert resolve_table_access(
            table, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
        ) == FREE_AR
    table = _table("private")
    del table["require_approval"]
    assert resolve_table_access(
        table, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    ) == FREE_AR


# ---------------------------------------------------------------------------
# Project tables and doc sources
# ---------------------------------------------------------------------------


def _access(table, *, user_id=OWNER, is_public=False, project_id=None,
            run_kind="top_level", sources=()):
    return resolve_table_access(
        table, user_id=user_id, is_public=is_public, project_id=project_id,
        run_kind=run_kind, doc_source_project_ids=sources,
    )


def test_project_table_outside_its_project():
    """Docs rule 2: other projects never see it; from a standalone
    conversation the owner reads it (both verdicts denied), a recipient
    sees nothing, scripts see no project table."""
    table = _table("private", [(RECIPIENT, "write")], project_id=PROJECT)
    assert _access(table, project_id=PROJECT) == AR_AR
    read_only_reason = {
        "sub_agent": DENY_SUB_AGENT, "inference_api": DENY_INFERENCE_API,
        "user_subagent": DENY_USER_SUBAGENT,
    }
    for run_kind in [k for k in RUN_KINDS if k != "ui"]:
        for user_id in (OWNER, RECIPIENT):
            assert _access(table, user_id=user_id, project_id=OTHER_PROJECT,
                           run_kind=run_kind) == HIDDEN
        assert _access(table, user_id=RECIPIENT, project_id=None,
                       run_kind=run_kind) == HIDDEN
        if run_kind == "script":
            expected = HIDDEN
        else:
            expected = RO(read_only_reason.get(run_kind, DENY_OUTSIDE_PROJECT))
        assert _access(table, user_id=OWNER, project_id=None, run_kind=run_kind) == expected


def test_owner_standalone_reads_every_own_project_table():
    """Shares, the owner's switch and the mode never make a project table
    writable from a standalone conversation."""
    expected = RO(DENY_OUTSIDE_PROJECT)
    assert _access(_table("private", project_id=PROJECT)) == expected
    assert _access(_table("private", [(None, "write")], project_id=PROJECT)) == expected
    assert _access(_table("private", project_id=PROJECT, require_approval=True)) == expected
    assert _access(_table("private", project_id=PROJECT), run_kind="slack") == expected
    assert _access(_table("public", project_id=PROJECT)) == RO(DENY_PUBLIC_TABLE_FROM_PRIVATE)
    # A stranger with no relationship still sees nothing.
    assert _access(_table("private", project_id=PROJECT), user_id=STRANGER) == HIDDEN


def test_public_project_table_from_its_public_project():
    assert _access(_table("public", project_id=PROJECT), project_id=PROJECT,
                   is_public=True) == FREE_PUB


def test_private_project_table_hidden_from_its_public_conversation():
    assert _access(_table("private", project_id=PROJECT), project_id=PROJECT,
                   is_public=True) == HIDDEN


@pytest.mark.parametrize("run_kind", ["top_level", "slack"])
def test_doc_source_public_tables_read_only_from_a_private_project(run_kind):
    expected = RO(DENY_PUBLIC_TABLE_FROM_PRIVATE)
    for shares, user_id in [((), OWNER), ([(RECIPIENT, "write")], RECIPIENT),
                            ([(None, "write")], STRANGER)]:
        table = _table("public", shares, project_id=PROJECT)
        assert _access(table, user_id=user_id, project_id=OTHER_PROJECT,
                       run_kind=run_kind, sources={PROJECT}) == expected


def test_doc_source_needs_the_source_and_a_relationship():
    table = _table("public", [(RECIPIENT, "write")], project_id=PROJECT)
    assert _access(table, project_id=OTHER_PROJECT) == HIDDEN
    assert _access(table, project_id=OTHER_PROJECT, sources={OTHER_PROJECT}) == HIDDEN
    assert _access(table, user_id=RECIPIENT, project_id=None, sources={PROJECT}) == HIDDEN
    assert _access(table, user_id=STRANGER, project_id=OTHER_PROJECT,
                   sources={PROJECT}) == HIDDEN
    # The owner's standalone conversation reads it by ownership, not sources.
    assert _access(table, project_id=None, sources={PROJECT}) == RO(
        DENY_PUBLIC_TABLE_FROM_PRIVATE
    )
    assert _access(table, project_id=None) == RO(DENY_PUBLIC_TABLE_FROM_PRIVATE)


def test_doc_source_private_tables_hidden():
    table = _table("private", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        assert _access(table, user_id=user_id, project_id=OTHER_PROJECT,
                       sources={PROJECT}) == HIDDEN


def test_doc_sources_ignored_in_public_conversations():
    table = _table("public", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        assert _access(table, user_id=user_id, is_public=True,
                       project_id=OTHER_PROJECT, sources={PROJECT}) == HIDDEN


@pytest.mark.parametrize("project_id", [None, PROJECT, OTHER_PROJECT])
def test_ui_sees_project_tables_regardless_of_project_id(project_id):
    table = _table("private", [(RECIPIENT, "read")], project_id=PROJECT)
    assert _access(table, project_id=project_id, run_kind="ui") == UI_OWNER
    assert _access(table, user_id=RECIPIENT, project_id=project_id,
                   run_kind="ui") == RO(DENY_UI_READ_ONLY)
    assert _access(table, user_id=STRANGER, project_id=project_id,
                   run_kind="ui") == HIDDEN


def test_user_table_visible_from_any_private_conversation():
    shared = _table("private", [(None, "read")])
    for project_id in (None, PROJECT, OTHER_PROJECT):
        assert _access(_table("private"), project_id=project_id) == FREE_AR
        assert _access(shared, user_id=STRANGER, project_id=project_id) == RO(
            DENY_READ_ONLY_SHARE
        )


# ---------------------------------------------------------------------------
# Sweep: invariants over every combination
# ---------------------------------------------------------------------------

_SHARE_SETS = [
    (),
    [(RECIPIENT, "read")],
    [(RECIPIENT, "write")],
    [(None, "read")],
    [(None, "write")],
    [(RECIPIENT, "read"), (None, "write")],
]


@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_invariants_hold_everywhere(run_kind):
    for (mode, shares, user_id, is_public, project_id, table_project_id, sources,
         require_approval) in itertools.product(
        TABLE_MODES, _SHARE_SETS, (OWNER, RECIPIENT, STRANGER), (False, True),
        (None, PROJECT, OTHER_PROJECT), (None, PROJECT), (frozenset(), frozenset({PROJECT})),
        (False, True),
    ):
        table = _table(mode, shares, project_id=table_project_id,
                       require_approval=require_approval)
        access = _access(table, user_id=user_id, is_public=is_public,
                         project_id=project_id, run_kind=run_kind, sources=sources)
        effective_mode = "private" if table_project_id is None else mode
        # Invariant 5: the schema is never free.
        assert access.schema in ("approval", "denied")
        if not access.visible:
            assert access == HIDDEN
            continue
        assert access.can_read
        # Invariant 2: public conversations never learn of private tables.
        assert not (is_public and effective_mode == "private")
        # Taint: a private conversation never writes a public table.
        if effective_mode == "public" and not is_public and run_kind != "ui":
            assert access.rows == "denied" and access.schema == "denied"
        # Reach outside the table's project (docs rule 2): read-only, and
        # only for the owner from a standalone conversation or through a
        # source naming a public table's project.
        if table_project_id is not None and run_kind != "ui" and project_id != table_project_id:
            assert not is_public and run_kind != "script"
            assert (project_id is None and user_id == OWNER) or (
                effective_mode == "public" and project_id is not None
                and table_project_id in sources
            )
            assert access.rows == "denied" and access.schema == "denied"
        # Schema approval only for a top-level private conversation or the
        # owner in the UI.
        if access.schema == "approval":
            assert (run_kind == "top_level" and not is_public) or (
                run_kind == "ui" and user_id == OWNER
            )
        # Public conversations and Slack runs never get a schema card (the
        # UI carries no conversation; callers pass is_public=False).
        if (is_public and run_kind != "ui") or run_kind == "slack":
            assert access.schema == "denied"
            assert access.schema_deny_reason is not None
        if run_kind in READ_ONLY_RUN_KINDS:
            assert access.rows == "denied" and access.schema == "denied"
        if run_kind == "slack":
            assert access.rows != "approval"
        if require_approval and run_kind not in ("ui",):
            assert access.rows != "free"


# ---------------------------------------------------------------------------
# Hidden vs missing parity
# ---------------------------------------------------------------------------


def test_hidden_and_missing_not_found_bodies_are_byte_identical():
    hidden = _access(_table("private"), user_id=OWNER, is_public=True, project_id=PROJECT)
    assert hidden == HIDDEN
    from_hidden = not_found_message(TABLE_ID, hidden)
    from_missing = not_found_message(TABLE_ID, None)
    assert from_hidden is not None
    assert from_hidden.encode() == from_missing.encode()
    assert from_hidden == table_not_found_message(TABLE_ID)
    # A stranger's view of a private table is the same text too.
    stranger = _access(_table("private"), user_id=STRANGER)
    assert not_found_message(TABLE_ID, stranger) == from_missing


def test_not_found_message_none_for_visible_table():
    assert not_found_message(TABLE_ID, FREE_AR) is None
    assert not_found_message(TABLE_ID, RO(DENY_READ_ONLY_SHARE)) is None


def test_hidden_verdict_leaks_nothing():
    assert HIDDEN.deny_reason is None
    assert HIDDEN.schema_deny_reason is None
    assert HIDDEN.approval_reason is None
    assert write_note(HIDDEN) is None
    assert schema_note(HIDDEN) is None


# ---------------------------------------------------------------------------
# Notes and messages
# ---------------------------------------------------------------------------


def test_write_note_mapping():
    assert write_note(FREE_AR) is None
    assert write_note(AR_AR) == APPROVAL_WRITE_NOTE
    assert write_note(AR_REQ_AR) == APPROVAL_REQUIRED_WRITE_NOTE
    assert write_note(RO(DENY_READ_ONLY_SHARE)) == DENY_READ_ONLY_SHARE
    assert write_note(FREE_PUB) is None


def test_schema_note_mapping():
    assert schema_note(FREE_AR) == SCHEMA_APPROVAL_NOTE
    assert schema_note(FREE_PUB) == DENY_SCHEMA_PUBLIC
    assert schema_note(FREE_SLACK) == DENY_SCHEMA_SLACK
    assert schema_note(RO(DENY_SUB_AGENT)) == DENY_SUB_AGENT


def test_approval_required_message():
    assert approval_required_message(AR_AR) == APPROVAL_REQUIRED_MESSAGE
    assert approval_required_message(AR_AR, "write_table_rows") == APPROVAL_REQUIRED_MESSAGE
    assert approval_required_message(AR_REQ_AR) == APPROVAL_REQUIRED_BY_OWNER_MESSAGE
    assert approval_required_message(FREE_AR, "change_table_schema") == SCHEMA_APPROVAL_MESSAGE
    with pytest.raises(ValueError):
        approval_required_message(AR_AR, "write_doc")


def test_texts_are_pinned():
    assert APPROVAL_REQUIRED_MESSAGE == (
        'This table is shared; propose the change with '
        'create_action_request(request_type="write_table_rows", ...)'
    )
    assert APPROVAL_REQUIRED_BY_OWNER_MESSAGE == (
        'The owner requires approval for every change to this table; propose the '
        'change with create_action_request(request_type="write_table_rows", ...)'
    )
    assert APPROVAL_WRITE_NOTE == (
        "shared private table: use create_action_request(write_table_rows)"
    )
    assert APPROVAL_REQUIRED_WRITE_NOTE == (
        "owner requires approval for every change: use create_action_request(write_table_rows)"
    )
    assert DENY_READ_ONLY_SHARE == "You have read-only access to this shared table."
    assert DENY_OUTSIDE_PROJECT == (
        "This table belongs to a project; its rows and columns are changed only "
        "from that project's conversations or in the Quest UI. Here it is read-only."
    )
    assert DENY_PUBLIC_TABLE_FROM_PRIVATE == (
        "Public tables are written only from public-project conversations."
    )
    assert "Quest UI" in DENY_SCHEMA_PUBLIC and "Ask the user" in DENY_SCHEMA_PUBLIC
    assert "Quest web UI" in DENY_SCHEMA_SLACK and "Ask the user" in DENY_SCHEMA_SLACK
    assert DENY_SCHEMA_UI_NOT_OWNER == "Only the table's owner can change its columns."


def test_deny_texts_never_say_doc():
    import chat.tables.access as mod
    for name in dir(mod):
        value = getattr(mod, name)
        if name.isupper() and isinstance(value, str):
            assert " doc" not in value and "write_doc" not in value, name


# ---------------------------------------------------------------------------
# create_table, TableAccess, shares, inputs
# ---------------------------------------------------------------------------


def test_create_table_deny_reason():
    assert create_table_deny_reason(is_public=False, run_kind="top_level") is None
    assert create_table_deny_reason(is_public=True, run_kind="top_level") == DENY_SCHEMA_PUBLIC
    assert create_table_deny_reason(is_public=False, run_kind="slack") == DENY_SCHEMA_SLACK
    assert create_table_deny_reason(is_public=False, run_kind="sub_agent") == DENY_SUB_AGENT
    assert create_table_deny_reason(is_public=False, run_kind="script") == DENY_SCRIPT
    for kind in ("ui", "web", None):
        with pytest.raises(ValueError):
            create_table_deny_reason(is_public=False, run_kind=kind)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(visible=True, can_read=True, rows="free", schema="free",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=False, can_read=False, rows="denied", schema="denied",
             deny_reason="x", approval_reason=None, schema_deny_reason=None),
        dict(visible=False, can_read=False, rows="denied", schema="approval",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=True, can_read=False, rows="free", schema="approval",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=True, can_read=True, rows="denied", schema="approval",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=True, can_read=True, rows="free", schema="denied",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=True, can_read=True, rows="approval", schema="approval",
             deny_reason=None, approval_reason=None, schema_deny_reason=None),
        dict(visible=True, can_read=True, rows="free", schema="approval",
             deny_reason=None, approval_reason="shared", schema_deny_reason=None),
        dict(visible=True, can_read=True, rows="approval", schema="approval",
             deny_reason=None, approval_reason="because", schema_deny_reason=None),
    ],
)
def test_table_access_rejects_inconsistent_verdicts(kwargs):
    with pytest.raises(ValueError):
        TableAccess(**kwargs)


def test_effective_share():
    assert effective_share(_table("private", [(RECIPIENT, "read"), (None, "write")]),
                           RECIPIENT) == "write"
    assert effective_share(_table("private", [(STRANGER, "write")]), RECIPIENT) is None


def test_missing_shares_fails_closed():
    table = _table("private")
    del table["shares"]
    with pytest.raises(ValueError, match="table dict"):
        _access(table)
    table["shares"] = None
    with pytest.raises(ValueError):
        _access(table, user_id=STRANGER)


@pytest.mark.parametrize("run_kind", ["", "UI", "web", None])
def test_unknown_run_kind_raises(run_kind):
    with pytest.raises(ValueError):
        _access(_table("private"), run_kind=run_kind)


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="table mode"):
        _access(_table("internal"))


def test_every_deny_cause_has_table_and_doc_wording():
    """Holds under python -O too (the modules' import-time asserts don't)."""
    import typing

    from chat import artifact_access
    from chat.tables import access as table_access

    assert artifact_access.DENY_CAUSES == typing.get_args(artifact_access.DenyCause)
    assert set(table_access._DENY_TEXT) == set(artifact_access.DENY_CAUSES)
    assert set(doc_access._DENY_TEXT) == set(artifact_access.DENY_CAUSES)
    assert tuple(TABLE_MODES) == artifact_access.MODES


def test_run_kinds_are_the_docs_ones():
    assert RUN_KINDS is doc_access.RUN_KINDS
    assert READ_ONLY_RUN_KINDS is doc_access.READ_ONLY_RUN_KINDS
