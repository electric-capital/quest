"""Quest Docs access rule: every cell of the spec's 5.1 matrix, plus the
project, public-conversation, share-precedence and run_kind edge rules
pinned on :func:`chat.docs.access.resolve_doc_access`.

User docs are always private: the matrix's public rows are public-project
docs, and a user doc whose stored mode still says "public" (pre-migration
leftover) gets exactly the private user doc's verdicts.
"""

import itertools

import pytest

from chat.docs.access import (
    APPROVAL_REQUIRED_WRITE_NOTE,
    APPROVAL_WRITE_NOTE,
    DENY_INFERENCE_API,
    DENY_PUBLIC_APPROVAL_REQUIRED,
    DENY_PUBLIC_DOC_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCRIPT,
    DENY_SLACK_APPROVAL_REQUIRED,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
    DENY_UI_READ_ONLY,
    DENY_USER_SUBAGENT,
    HIDDEN,
    READ_ONLY_RUN_KINDS,
    RUN_KINDS,
    DocAccess,
    creation_mode,
    effective_share,
    resolve_doc_access,
    write_note,
)
from chat.docs.constants import DOC_MODES, DOC_SHARE_PERMISSIONS

OWNER = 1
RECIPIENT = 2
STRANGER = 3
PROJECT = "11111111-1111-4111-8111-111111111111"
OTHER_PROJECT = "22222222-2222-4222-8222-222222222222"


def _doc(mode, shares=(), *, project_id=None, owner_id=OWNER):
    return {
        "id": "33333333-3333-4333-8333-333333333333",
        "owner_id": owner_id,
        "project_id": project_id,
        "title": "Notes",
        "description": "",
        "mode": mode,
        "content_size": 0,
        "asset_count": 0,
        "last_write_source": None,
        "created_at": "2026-10-05T00:00:00",
        "updated_at": "2026-10-05T00:00:00",
        "shares": [
            {"id": i, "user_id": user_id, "permission": permission,
             "created_at": "2026-10-05T00:00:00"}
            for i, (user_id, permission) in enumerate(shares, start=1)
        ],
    }


FREE = DocAccess(visible=True, can_read=True, write="free", deny_reason=None)
AR = DocAccess(visible=True, can_read=True, write="approval", deny_reason=None)
# The owner's require-approval switch produced the approval (rule 7).
AR_REQ = DocAccess(
    visible=True, can_read=True, write="approval", deny_reason=None, required_by_owner=True,
)
H = HIDDEN


def RO(reason):
    return DocAccess(visible=True, can_read=True, write="denied", deny_reason=reason)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

# The "private" rows are user docs (always private). User docs can never be
# public: the "public" rows are docs of the public project PROJECT, the only
# kind of public doc there is. The "legacy_public_user" rows are user docs
# whose stored mode still says "public" (rows from before migration
# e1b7c4d9a2f6): the rule evaluates them as private, so they expect exactly
# what the matching private row expects -- in particular Hidden from every
# public conversation.
DOC_ROWS = {
    "private_unshared": _doc("private"),
    "private_shared_read": _doc("private", [(RECIPIENT, "read")]),
    "private_shared_write": _doc("private", [(RECIPIENT, "write")]),
    "private_shared_everyone_read": _doc("private", [(None, "read")]),
    "public_unshared": _doc("public", project_id=PROJECT),
    "public_shared_read": _doc("public", [(RECIPIENT, "read")], project_id=PROJECT),
    "public_shared_write": _doc("public", [(RECIPIENT, "write")], project_id=PROJECT),
    "legacy_public_user_unshared": _doc("public"),
    "legacy_public_user_shared_read": _doc("public", [(RECIPIENT, "read")]),
    "legacy_public_user_shared_write": _doc("public", [(RECIPIENT, "write")]),
    "legacy_public_user_shared_everyone_read": _doc("public", [(None, "read")]),
}

# column -> (user_id, is_public, run_kind, project_id). Conversation runs
# are conversations of PROJECT (a public conversation is always one in a
# public project; user docs are visible from any project); script and ui
# carry no conversation context (project_id=None), as their callers pass.
COLUMNS = {
    "owner_private": (OWNER, False, "top_level", PROJECT),
    "recipient_private": (RECIPIENT, False, "top_level", PROJECT),
    "owner_public": (OWNER, True, "top_level", PROJECT),
    "recipient_public": (RECIPIENT, True, "top_level", PROJECT),
    "stranger_private": (STRANGER, False, "top_level", PROJECT),
    "owner_sub_agent": (OWNER, False, "sub_agent", PROJECT),
    "owner_inference_api": (OWNER, False, "inference_api", PROJECT),
    "owner_user_subagent": (OWNER, False, "user_subagent", PROJECT),
    "owner_script": (OWNER, False, "script", None),
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

# Public docs are project docs, and sandbox scripts (no conversation
# context) never see a project doc (rule 2).
PUBLIC_DOC_READ_ONLY_COLUMNS = {**READ_ONLY_COLUMNS, "owner_script": H}

EXPECTED = {
    "private_unshared": {
        "owner_private": FREE,
        "recipient_private": H,
        "owner_public": H,
        "recipient_public": H,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        "owner_slack": FREE,
        "recipient_slack": H,
        "ui_owner": FREE,
        "ui_recipient": H,
    },
    "private_shared_read": {
        "owner_private": AR,
        "recipient_private": RO(DENY_READ_ONLY_SHARE),
        "owner_public": H,
        "recipient_public": H,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": RO(DENY_READ_ONLY_SHARE),
        "ui_owner": FREE,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "private_shared_write": {
        "owner_private": AR,
        "recipient_private": AR,
        "owner_public": H,
        "recipient_public": H,
        "stranger_private": H,
        **READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": RO(DENY_SLACK_NEEDS_APPROVAL),
        "ui_owner": FREE,
        "ui_recipient": FREE,
    },
    "private_shared_everyone_read": {
        "owner_private": AR,
        "recipient_private": RO(DENY_READ_ONLY_SHARE),
        "owner_public": H,
        "recipient_public": H,
        # The everyone row makes every user on the install a read recipient.
        "stranger_private": RO(DENY_READ_ONLY_SHARE),
        **READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_SLACK_NEEDS_APPROVAL),
        "recipient_slack": RO(DENY_READ_ONLY_SHARE),
        "ui_owner": FREE,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "public_unshared": {
        "owner_private": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_private": H,
        "owner_public": FREE,
        "recipient_public": H,
        "stranger_private": H,
        **PUBLIC_DOC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_slack": H,
        "ui_owner": FREE,
        "ui_recipient": H,
    },
    "public_shared_read": {
        "owner_private": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_private": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "owner_public": FREE,
        "recipient_public": RO(DENY_READ_ONLY_SHARE),
        "stranger_private": H,
        **PUBLIC_DOC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_slack": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "ui_owner": FREE,
        "ui_recipient": RO(DENY_UI_READ_ONLY),
    },
    "public_shared_write": {
        "owner_private": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_private": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "owner_public": FREE,
        "recipient_public": FREE,
        "stranger_private": H,
        **PUBLIC_DOC_READ_ONLY_COLUMNS,
        "owner_slack": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "recipient_slack": RO(DENY_PUBLIC_DOC_FROM_PRIVATE),
        "ui_owner": FREE,
        "ui_recipient": FREE,
    },
}

# A leftover public user doc is a private user doc for every decision.
EXPECTED.update({
    "legacy_public_user_unshared": EXPECTED["private_unshared"],
    "legacy_public_user_shared_read": EXPECTED["private_shared_read"],
    "legacy_public_user_shared_write": EXPECTED["private_shared_write"],
    "legacy_public_user_shared_everyone_read": EXPECTED["private_shared_everyone_read"],
})


def test_expected_table_covers_every_cell():
    assert set(EXPECTED) == set(DOC_ROWS)
    for row in EXPECTED.values():
        assert set(row) == set(COLUMNS)


def test_public_conversation_never_sees_a_user_doc():
    """The 'public conversation x user doc' cells are all Hidden: user docs
    are always private, whatever their stored mode."""
    for row, doc in DOC_ROWS.items():
        if doc["project_id"] is not None:
            continue
        for column, (_uid, is_public, _kind, _pid) in COLUMNS.items():
            if is_public:
                assert EXPECTED[row][column] == H, (row, column)


@pytest.mark.parametrize(
    "row,column",
    [(r, c) for r in DOC_ROWS for c in COLUMNS],
)
def test_matrix_cell(row, column):
    user_id, is_public, run_kind, project_id = COLUMNS[column]
    access = resolve_doc_access(
        DOC_ROWS[row],
        user_id=user_id,
        is_public=is_public,
        project_id=project_id,
        run_kind=run_kind,
    )
    assert access == EXPECTED[row][column]


@pytest.mark.parametrize("run_kind", sorted(READ_ONLY_RUN_KINDS))
@pytest.mark.parametrize("row", sorted(DOC_ROWS))
def test_read_only_run_kinds_in_public_context(run_kind, row):
    """Read-only kinds read public (project) docs of their project and still
    never see private ones -- every user doc included."""
    doc = DOC_ROWS[row]
    project_id = None if run_kind == "script" else PROJECT
    access = resolve_doc_access(
        doc, user_id=OWNER, is_public=True, project_id=project_id, run_kind=run_kind
    )
    if doc["project_id"] is None or doc["mode"] == "private":
        assert access == HIDDEN
    else:
        assert access == PUBLIC_DOC_READ_ONLY_COLUMNS[f"owner_{run_kind}"]


# ---------------------------------------------------------------------------
# Project docs
# ---------------------------------------------------------------------------


def _access(doc, *, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level"):
    return resolve_doc_access(
        doc, user_id=user_id, is_public=is_public, project_id=project_id, run_kind=run_kind
    )


def test_project_doc_visible_from_same_project():
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=PROJECT) == FREE


def test_public_project_doc_writable_from_its_public_project():
    doc = _doc("public", project_id=PROJECT)
    assert _access(doc, project_id=PROJECT, is_public=True) == FREE


def test_project_doc_hidden_from_other_project():
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=OTHER_PROJECT) == HIDDEN


def test_project_doc_hidden_from_standalone_conversation():
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=None) == HIDDEN


@pytest.mark.parametrize("run_kind", [k for k in RUN_KINDS if k != "ui"])
def test_project_doc_hidden_outside_project_for_every_conversation_kind(run_kind):
    doc = _doc("private", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        assert _access(doc, user_id=user_id, project_id=None, run_kind=run_kind) == HIDDEN
        assert (
            _access(doc, user_id=user_id, project_id=OTHER_PROJECT, run_kind=run_kind)
            == HIDDEN
        )


def test_script_never_sees_project_docs():
    """Scripts carry no conversation context (project_id=None)."""
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=None, run_kind="script") == HIDDEN


def test_read_only_kinds_read_project_docs_inside_the_project():
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=PROJECT, run_kind="sub_agent") == RO(DENY_SUB_AGENT)


@pytest.mark.parametrize("project_id", [None, PROJECT, OTHER_PROJECT])
def test_ui_sees_project_docs_regardless_of_project_id(project_id):
    doc = _doc("private", [(RECIPIENT, "read")], project_id=PROJECT)
    assert _access(doc, project_id=project_id, run_kind="ui") == FREE
    assert _access(doc, user_id=RECIPIENT, project_id=project_id, run_kind="ui") == RO(
        DENY_UI_READ_ONLY
    )
    # The UI still requires a relationship.
    assert _access(doc, user_id=STRANGER, project_id=project_id, run_kind="ui") == HIDDEN


def test_user_doc_visible_from_project_conversation():
    doc = _doc("private")
    assert _access(doc, project_id=PROJECT) == FREE


# ---------------------------------------------------------------------------
# Public docs are readable from a private project that lists them as a source
# ---------------------------------------------------------------------------

SOURCES = frozenset({PROJECT})


def _source_access(doc, *, user_id=OWNER, project_id=OTHER_PROJECT, run_kind="top_level",
                   is_public=False, sources=SOURCES):
    return resolve_doc_access(
        doc, user_id=user_id, is_public=is_public, project_id=project_id,
        run_kind=run_kind, doc_source_project_ids=sources,
    )


@pytest.mark.parametrize("run_kind", ["top_level", "slack"])
def test_public_doc_readable_from_a_private_project_with_the_source(run_kind):
    """Rule 2's exception: a private project listing PROJECT as a doc source
    reads its public docs (owner and recipients alike), and the taint rule
    keeps every one of them from writing."""
    expected = RO(DENY_PUBLIC_DOC_FROM_PRIVATE)
    unshared = _doc("public", project_id=PROJECT)
    assert _source_access(unshared, run_kind=run_kind) == expected
    for permission in ("read", "write"):
        shared = _doc("public", [(RECIPIENT, permission)], project_id=PROJECT)
        assert _source_access(shared, run_kind=run_kind) == expected
        assert _source_access(shared, user_id=RECIPIENT, run_kind=run_kind) == expected
    everyone = _doc("public", [(None, "read")], project_id=PROJECT)
    assert _source_access(everyone, user_id=STRANGER, run_kind=run_kind) == expected


@pytest.mark.parametrize("run_kind", ["sub_agent", "inference_api", "user_subagent"])
def test_public_doc_readable_by_read_only_kinds_of_the_private_project(run_kind):
    doc = _doc("public", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        assert (
            _source_access(doc, user_id=user_id, run_kind=run_kind)
            == READ_ONLY_COLUMNS[f"owner_{run_kind}"]
        )


def test_public_doc_still_needs_a_relationship():
    doc = _doc("public", [(RECIPIENT, "write")], project_id=PROJECT)
    assert _source_access(doc, user_id=STRANGER) == HIDDEN


def test_public_doc_hidden_without_the_source():
    doc = _doc("public", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        # No sources at all, or sources that do not name the doc's project.
        assert _source_access(doc, user_id=user_id, sources=frozenset()) == HIDDEN
        assert (
            _source_access(doc, user_id=user_id, sources=frozenset({OTHER_PROJECT}))
            == HIDDEN
        )
        # A standalone conversation has no project, hence no sources.
        assert _source_access(doc, user_id=user_id, project_id=None) == HIDDEN


def test_public_doc_hidden_from_scripts_and_other_public_projects_even_with_source():
    doc = _doc("public", [(RECIPIENT, "write")], project_id=PROJECT)
    # Scripts carry no conversation context and see no project doc.
    assert _source_access(doc, project_id=None, run_kind="script") == HIDDEN
    # A public conversation stays confined to its own project's docs.
    for user_id in (OWNER, RECIPIENT):
        assert _source_access(doc, user_id=user_id, is_public=True) == HIDDEN


def test_source_grants_nothing_on_private_project_docs():
    """The exception is for public docs only."""
    doc = _doc("private", [(RECIPIENT, "write")], project_id=PROJECT)
    for user_id in (OWNER, RECIPIENT):
        assert _source_access(doc, user_id=user_id) == HIDDEN


def test_source_grants_nothing_in_the_ui_or_the_same_project():
    """The UI never passes sources and sees project docs anyway; inside
    the doc's own project the verdict is the same with or without."""
    doc = _doc("public", project_id=PROJECT)
    assert _source_access(doc, project_id=None, run_kind="ui") == FREE
    assert _source_access(doc, project_id=PROJECT, is_public=True) == FREE
    assert _source_access(doc, project_id=PROJECT) == RO(DENY_PUBLIC_DOC_FROM_PRIVATE)


# ---------------------------------------------------------------------------
# Public conversations never learn of private docs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shares,user_id",
    [
        ((), OWNER),
        ([(RECIPIENT, "write")], OWNER),
        ([(RECIPIENT, "write")], RECIPIENT),
        ([(None, "write")], STRANGER),
        ([(RECIPIENT, "read"), (None, "write")], RECIPIENT),
    ],
)
def test_public_conversation_never_sees_private_doc(shares, user_id):
    assert _access(_doc("private", shares), user_id=user_id, is_public=True) == HIDDEN


def test_public_project_conversation_never_sees_private_project_doc():
    doc = _doc("private", project_id=PROJECT)
    assert _access(doc, project_id=PROJECT, is_public=True) == HIDDEN


# ---------------------------------------------------------------------------
# Taint sweep over every combination
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
def test_taint_and_invisibility_hold_everywhere(run_kind):
    for mode, shares, user_id, is_public, project_id, doc_project_id, sources in itertools.product(
        DOC_MODES,
        _SHARE_SETS,
        (OWNER, RECIPIENT, STRANGER),
        (False, True),
        (None, PROJECT, OTHER_PROJECT),
        (None, PROJECT),
        (frozenset(), SOURCES),
    ):
        doc = _doc(mode, shares, project_id=doc_project_id)
        access = resolve_doc_access(
            doc, user_id=user_id, is_public=is_public, project_id=project_id,
            run_kind=run_kind, doc_source_project_ids=sources,
        )
        # A user doc is private whatever its stored mode says.
        effective_mode = "private" if doc_project_id is None else mode
        # Hidden verdicts are exactly HIDDEN: no deny_reason leaks.
        if not access.visible:
            assert access == HIDDEN
        else:
            assert access.can_read
        # Invisibility: a public conversation never learns of a private doc
        # -- nor of any user doc.
        if is_public and (effective_mode == "private" or doc_project_id is None):
            assert access == HIDDEN
        # Taint: a private conversation never writes a public doc.
        if effective_mode == "public" and not is_public and run_kind != "ui":
            assert access.write == "denied"
        # Reach: outside the doc's project, only a private project
        # conversation whose sources name the doc's project sees a public
        # doc; a public conversation only its own project's docs.
        related = user_id == OWNER or effective_share(doc, user_id) is not None
        if effective_mode == "public" and related and run_kind not in ("script", "ui"):
            if project_id == doc_project_id:
                assert access.visible
            else:
                assert access.visible == (
                    not is_public and project_id is not None and doc_project_id in sources
                )
        # Read-only kinds never write.
        if run_kind in READ_ONLY_RUN_KINDS:
            assert access.write == "denied"
        # Slack never gets an approval verdict (it cannot open a card).
        if run_kind == "slack":
            assert access.write != "approval"


@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_legacy_public_user_doc_is_exactly_a_private_user_doc(run_kind):
    """Every verdict on a user doc with stored mode "public" equals the
    verdict on the same doc stored "private" (defensive: migration
    e1b7c4d9a2f6 flips such rows)."""
    for shares, user_id, is_public, project_id in itertools.product(
        _SHARE_SETS, (OWNER, RECIPIENT, STRANGER), (False, True),
        (None, PROJECT, OTHER_PROJECT),
    ):
        kwargs = dict(
            user_id=user_id, is_public=is_public, project_id=project_id, run_kind=run_kind,
        )
        assert resolve_doc_access(_doc("public", shares), **kwargs) == resolve_doc_access(
            _doc("private", shares), **kwargs
        )


@pytest.mark.parametrize(
    "shares,user_id",
    [
        ((), OWNER),
        ([(RECIPIENT, "write")], OWNER),
        ([(RECIPIENT, "write")], RECIPIENT),
        ([(None, "write")], STRANGER),
    ],
)
def test_legacy_public_user_doc_hidden_from_public_conversations(shares, user_id):
    for project_id in (None, PROJECT):
        assert _access(
            _doc("public", shares), user_id=user_id, is_public=True, project_id=project_id,
        ) == HIDDEN


def test_legacy_public_user_doc_treated_as_private_in_private_conversations():
    assert _access(_doc("public")) == FREE
    shared_read = _doc("public", [(RECIPIENT, "read")])
    assert _access(shared_read) == AR
    assert _access(shared_read, user_id=RECIPIENT) == RO(DENY_READ_ONLY_SHARE)
    shared_write = _doc("public", [(RECIPIENT, "write")])
    assert _access(shared_write, user_id=RECIPIENT) == AR
    assert _access(shared_write, user_id=RECIPIENT, run_kind="slack") == RO(
        DENY_SLACK_NEEDS_APPROVAL
    )


# ---------------------------------------------------------------------------
# Missing shares, unknown inputs
# ---------------------------------------------------------------------------


def test_missing_shares_key_fails_closed():
    # A dict fetched without shares must never read as "unshared": the owner
    # of a shared private doc would get a free write instead of approval.
    doc = _doc("private")
    del doc["shares"]
    with pytest.raises(ValueError):
        _access(doc)
    with pytest.raises(ValueError):
        _access(doc, user_id=RECIPIENT)
    with pytest.raises(ValueError):
        effective_share(doc, OWNER)


def test_none_shares_fails_closed():
    doc = _doc("private")
    doc["shares"] = None
    with pytest.raises(ValueError):
        _access(doc)


@pytest.mark.parametrize("run_kind", ["", "UI", "top-level", "web", None])
def test_unknown_run_kind_raises(run_kind):
    with pytest.raises(ValueError):
        _access(_doc("private"), run_kind=run_kind)
    # Even when the doc would be hidden from the caller.
    with pytest.raises(ValueError):
        _access(_doc("private"), user_id=STRANGER, run_kind=run_kind)


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        _access(_doc("internal"))


# ---------------------------------------------------------------------------
# effective_share
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shares,expected",
    [
        ((), None),
        ([(RECIPIENT, "read")], "read"),
        ([(RECIPIENT, "write")], "write"),
        ([(None, "read")], "read"),
        ([(None, "write")], "write"),
        # Both rows match: the higher permission wins either way round.
        ([(RECIPIENT, "write"), (None, "read")], "write"),
        ([(RECIPIENT, "read"), (None, "write")], "write"),
        ([(None, "write"), (RECIPIENT, "read")], "write"),
        ([(RECIPIENT, "read"), (None, "read")], "read"),
        # Another user's row grants nothing.
        ([(STRANGER, "write")], None),
        ([(STRANGER, "write"), (None, "read")], "read"),
        # An unknown permission grants nothing.
        ([(RECIPIENT, "admin")], None),
        ([(RECIPIENT, "admin"), (None, "read")], "read"),
    ],
)
def test_effective_share(shares, expected):
    assert effective_share(_doc("private", shares), RECIPIENT) == expected


@pytest.mark.parametrize("permission", DOC_SHARE_PERMISSIONS)
def test_effective_share_knows_every_permission(permission):
    assert effective_share(_doc("private", [(RECIPIENT, permission)]), RECIPIENT) == permission


def test_everyone_write_share_with_user_read_row_still_writes():
    doc = _doc("private", [(RECIPIENT, "read"), (None, "write")])
    assert _access(doc, user_id=RECIPIENT) == AR


def test_owner_with_only_an_unknown_permission_row_still_needs_approval():
    """Any share row (even a malformed one) makes owner writes gated."""
    doc = _doc("private", [(RECIPIENT, "admin")])
    assert _access(doc) == AR
    assert _access(doc, user_id=RECIPIENT) == HIDDEN


# ---------------------------------------------------------------------------
# write_note, creation_mode, DocAccess, constants
# ---------------------------------------------------------------------------


def test_write_note_mapping():
    assert write_note(FREE) is None
    assert write_note(AR) == APPROVAL_WRITE_NOTE
    assert write_note(AR_REQ) == APPROVAL_REQUIRED_WRITE_NOTE
    assert write_note(RO(DENY_READ_ONLY_SHARE)) == DENY_READ_ONLY_SHARE
    assert write_note(RO(DENY_PUBLIC_DOC_FROM_PRIVATE)) == DENY_PUBLIC_DOC_FROM_PRIVATE
    assert write_note(HIDDEN) is None


def test_creation_mode():
    assert creation_mode(False) == "private"
    assert creation_mode(True) == "public"
    assert {creation_mode(False), creation_mode(True)} == set(DOC_MODES)


def test_hidden_carries_no_deny_reason():
    assert HIDDEN == DocAccess(visible=False, can_read=False, write="denied", deny_reason=None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"visible": False, "can_read": False, "write": "denied", "deny_reason": "x"},
        {"visible": False, "can_read": True, "write": "denied", "deny_reason": None},
        {"visible": False, "can_read": False, "write": "free", "deny_reason": None},
        {"visible": True, "can_read": True, "write": "free", "deny_reason": "x"},
        {"visible": True, "can_read": True, "write": "approval", "deny_reason": "x"},
        {"visible": True, "can_read": True, "write": "denied", "deny_reason": None},
        {"visible": True, "can_read": True, "write": "maybe", "deny_reason": None},
    ],
)
def test_doc_access_rejects_inconsistent_verdicts(kwargs):
    with pytest.raises(ValueError):
        DocAccess(**kwargs)


def test_run_kinds():
    assert RUN_KINDS == (
        "top_level", "sub_agent", "inference_api", "user_subagent", "script", "slack", "ui",
    )
    assert READ_ONLY_RUN_KINDS == {"sub_agent", "inference_api", "user_subagent", "script"}
    assert READ_ONLY_RUN_KINDS <= set(RUN_KINDS)


def test_deny_reason_texts_are_pinned():
    assert DENY_SUB_AGENT == "Only the top-level agent writes docs; return the content to it."
    assert DENY_INFERENCE_API == "Inference API runs are read-only; docs cannot be written here."
    assert DENY_USER_SUBAGENT == (
        "Cross-user subagent runs are read-only; return the content to the caller."
    )
    assert DENY_SCRIPT == (
        "Sandbox scripts have read-only access to docs; the agent writes docs with the doc tools."
    )
    assert DENY_READ_ONLY_SHARE == "You have read-only access to this shared doc."
    assert DENY_PUBLIC_DOC_FROM_PRIVATE == (
        "Public docs are written only from public-project conversations."
    )
    assert DENY_SLACK_NEEDS_APPROVAL == (
        "This doc is shared, so changes need an approval card, which Slack-driven "
        "conversations cannot open. Continue from the Quest web UI."
    )
    assert DENY_UI_READ_ONLY == "You have read-only access to this doc."
    assert APPROVAL_WRITE_NOTE == "shared private doc: use create_action_request(write_doc)"
    assert DENY_SLACK_APPROVAL_REQUIRED == (
        "The owner requires approval for every change to this doc, and "
        "Slack-driven conversations cannot open approval cards. Continue from "
        "the Quest web UI."
    )
    assert DENY_PUBLIC_APPROVAL_REQUIRED == (
        "The owner requires approval for every change to this doc, and "
        "public-project conversations cannot open approval cards."
    )
    assert APPROVAL_REQUIRED_WRITE_NOTE == (
        "owner requires approval for every change: use create_action_request(write_doc)"
    )


# ---------------------------------------------------------------------------
# Rule 7: the owner's require-approval switch
# ---------------------------------------------------------------------------


def _with_switch(verdict, column):
    """What a cell becomes once ``require_approval`` is on.

    Every Free verdict a conversation would get becomes Approval (marked
    ``required_by_owner``), a share-based Approval keeps the verdict but
    takes the switch's wording, Slack and public conversations (no cards)
    are denied with the switch's reasons, and every other cell -- reads,
    Hidden, the read-only run kinds and the UI -- is untouched.
    """
    _uid, is_public, run_kind, _pid = COLUMNS[column]
    if run_kind == "ui" or run_kind in READ_ONLY_RUN_KINDS:
        return verdict
    if verdict == AR:
        return AR_REQ
    if verdict == RO(DENY_SLACK_NEEDS_APPROVAL):
        return RO(DENY_SLACK_APPROVAL_REQUIRED)
    if verdict == FREE:
        if run_kind == "slack":
            return RO(DENY_SLACK_APPROVAL_REQUIRED)
        if is_public:
            return RO(DENY_PUBLIC_APPROVAL_REQUIRED)
        return AR_REQ
    return verdict


@pytest.mark.parametrize(
    "row,column",
    [(r, c) for r in DOC_ROWS for c in COLUMNS],
)
def test_matrix_cell_with_require_approval(row, column):
    user_id, is_public, run_kind, project_id = COLUMNS[column]
    doc = {**DOC_ROWS[row], "require_approval": True}
    access = resolve_doc_access(
        doc, user_id=user_id, is_public=is_public, project_id=project_id, run_kind=run_kind,
    )
    assert access == _with_switch(EXPECTED[row][column], column), (row, column)


def test_require_approval_changes_the_expected_cells_only():
    """The derivation above touches exactly the conversation columns' Free
    and Approval cells (sanity check on the helper, so a silent no-op
    cannot pass the matrix test)."""
    changed = {
        (row, column)
        for row in DOC_ROWS for column in COLUMNS
        if _with_switch(EXPECTED[row][column], column) != EXPECTED[row][column]
    }
    assert ("private_unshared", "owner_private") in changed
    assert ("private_unshared", "owner_slack") in changed
    assert ("private_shared_write", "recipient_private") in changed
    assert ("public_unshared", "owner_public") in changed
    assert ("public_shared_write", "recipient_public") in changed
    assert ("private_unshared", "ui_owner") not in changed
    assert ("private_shared_read", "recipient_private") not in changed
    assert ("private_unshared", "owner_sub_agent") not in changed
    assert ("public_unshared", "owner_private") not in changed
    for row, column in changed:
        _uid, _pub, run_kind, _pid = COLUMNS[column]
        assert run_kind in ("top_level", "slack")


def test_require_approval_owner_unshared_private_doc():
    doc = {**_doc("private"), "require_approval": True}
    access = resolve_doc_access(
        doc, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    )
    assert access == AR_REQ
    assert access.required_by_owner is True
    assert write_note(access) == APPROVAL_REQUIRED_WRITE_NOTE
    # The UI (a person editing) is never approval-gated.
    assert resolve_doc_access(
        doc, user_id=OWNER, is_public=False, project_id=None, run_kind="ui",
    ) == FREE


def test_require_approval_off_or_absent_is_the_plain_matrix():
    for value in (False, 0, None):
        doc = {**_doc("private"), "require_approval": value}
        assert resolve_doc_access(
            doc, user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
        ) == FREE
    assert "require_approval" not in _doc("private")
    assert resolve_doc_access(
        _doc("private"), user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    ) == FREE


def test_required_by_owner_only_on_approval_verdicts():
    for write, deny in (("free", None), ("denied", "x")):
        with pytest.raises(ValueError):
            DocAccess(
                visible=True, can_read=True, write=write, deny_reason=deny,
                required_by_owner=True,
            )
    with pytest.raises(ValueError):
        DocAccess(
            visible=False, can_read=False, write="denied", deny_reason=None,
            required_by_owner=True,
        )
    assert AR_REQ != AR
