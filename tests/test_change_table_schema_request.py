"""The ``change_table_schema`` action request: handler, pre-card arm and wiring.

Runs against the isolated ``env`` fixture of tests/test_table_service.py
(tmp SQLite file, TABLES_DIR, ``tables`` gate open, alice owning a private
and a public project, realtime events captured). Covers:

1. ``ChangeTableSchemaHandler.validate_params``: shape errors, unknown keys,
   server-injected preview keys dropped.
2. Pre-card refusals: gate closed (before any DB work), missing vs hidden
   byte-equal ``table_not_found``, each schema deny reason (read share,
   public table from a private conversation, Slack), a public
   conversation's card on its own public table,
   ``stale_schema`` (current version + key-less columns), a
   ``SchemaOperationError`` naming its operation, ``rows_violate``
   (set_required, set_unique, remove_enum_option), ``duplicate_title``.
3. The preview payload of a multi-operation batch (no column key anywhere),
   preview fields, labels, summary snippet, registration.
4. ``execute``: one change, version bump, ``action_request:<id>`` source,
   approver as actor; the TOCTOU on a UI schema edit; ``rows_violate`` at
   approve; a write-share recipient's card credited to the recipient; a
   share revoked before approval; invariant 1 (a public table is never
   changed through this card).
5. End to end through the ``create_action_request`` dispatch arm and the
   resolve route.
"""

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import pytest

from chat.tables import service, storage
from chat.tables.access import (
    DENY_PUBLIC_TABLE_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCHEMA_SLACK,
)
from chat.tables.constants import table_not_found_message, tables_disabled_message
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    convo,
    env,
    table_dir,
    ui,
    uid,
)

COLUMNS = [
    {"name": "Name", "type": "text", "required": True},
    {"name": "Status", "type": "enum", "options": ["open", "done", "blocked"]},
    {"name": "Tags", "type": "enum", "options": ["a", "b", "c"], "multiple": True},
    {"name": "Notes", "type": "text"},
]
ROWS = [
    {"Name": "x", "Status": "open", "Tags": ["a", "b"], "Notes": "n1"},
    {"Name": "y", "Status": "open", "Tags": ["a"]},
    {"Name": "z", "Status": "done", "Tags": ["c"], "Notes": "n3"},
]
INJECTED = (
    "table_title", "table_mode", "table_scope", "share_summary", "require_approval",
    "schema_diff", "operations_count", "rows_backfilled", "dropped_values",
    "operation_summaries",
)
KEY_RE = re.compile(r"c_[0-9a-f]{8}")


def _run(coro):
    return asyncio.run(coro)


def handler():
    from chat.action_request_types.change_table_schema import ChangeTableSchemaHandler
    return ChangeTableSchemaHandler()


def make(env, title="Inventory", *, owner="alice", project_id=None, rows=ROWS, shares=()):
    table = _run(service.create_table(
        ui(env, owner), title=title, columns=COLUMNS, project_id=project_id,
    ))
    if rows:
        _run(service.apply_row_write(
            ui(env, owner), table["id"], "insert", {"rows": [dict(r) for r in rows]},
        ))
    for who, permission in shares:
        _run(env.store.add_share(table["id"], env.users[who]["id"], permission))
    return _run(env.store.get_table(table["id"], with_shares=True))


def params(table, operations, version=None):
    return {
        "table_id": table["id"] if isinstance(table, dict) else table,
        "operations": operations,
        "expected_schema_version": table["schema_version"] if version is None else version,
    }


RENAME = [{"op": "rename_column", "name": "Notes", "new_name": "Comments"}]


def precard(validated, caller, *, is_slack_origin=False):
    from chat.action_request_types.table_precard import table_precard_check

    _run(table_precard_check(
        "change_table_schema", validated, caller.user, caller.project_id,
        caller.conversation_id, is_slack_origin=is_slack_origin,
    ))
    return validated


def propose(caller, raw, **kwargs):
    return precard(handler().validate_params(raw), caller, **kwargs)


def execute(validated, caller, request_id=7):
    return _run(handler().execute(
        validated, caller.user,
        conversation_id=caller.conversation_id,
        project_id=caller.project_id,
        request_id=request_id,
    ))


def refusal(caller, raw, **kwargs) -> str:
    with pytest.raises(ValueError) as exc:
        propose(caller, raw, **kwargs)
    return str(exc.value)


def has_key_anywhere(value) -> bool:
    if isinstance(value, dict):
        return "key" in value or any(has_key_anywhere(v) for v in value.values())
    if isinstance(value, list):
        return any(has_key_anywhere(v) for v in value)
    return False


def current(env, table):
    return _run(env.store.get_table(table["id"], with_shares=True))


def change_count(env, table):
    return len(storage.list_changes(table_dir(env, table)))


def ui_edit(env, table):
    """The owner's own Save in the Columns panel (a UI schema change)."""
    return _run(service.apply_schema_change(
        ui(env), table["id"],
        [{"op": "set_description", "name": "Name", "description": "the name"}],
        expected_schema_version=current(env, table)["schema_version"],
    ))


def make_request(env, caller, p, who="alice"):
    """A real action_requests row (so get_action_request_owners maps it)."""
    from db import action_request_store, conversation_store

    _run(conversation_store.create_conversation(
        user_id=uid(env, who), conversation_id=caller.conversation_id,
        created_at=datetime.now(timezone.utc), project_id=caller.project_id,
    ))
    return _run(action_request_store.create_action_request(
        uid(env, who), caller.conversation_id, "change_table_schema", p, "r",
    ))["id"]


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


class TestValidateParams:
    def test_normalized(self):
        raw = {"table_id": "t1", "operations": RENAME, "expected_schema_version": 3}
        assert handler().validate_params(raw) == raw

    @pytest.mark.parametrize("raw, match", [
        ({"operations": RENAME, "expected_schema_version": 1},
         "Missing required parameter: table_id"),
        ({"table_id": 5, "operations": RENAME, "expected_schema_version": 1},
         "table_id must be a non-empty string"),
        ({"table_id": "  ", "operations": RENAME, "expected_schema_version": 1},
         "table_id must be a non-empty string"),
        ({"table_id": "t", "expected_schema_version": 1},
         "Missing required parameter: operations"),
        ({"table_id": "t", "operations": {"op": "x"}, "expected_schema_version": 1},
         "operations must be a list"),
        ({"table_id": "t", "operations": [], "expected_schema_version": 1},
         "at least one operation"),
        ({"table_id": "t", "operations": [RENAME[0], "x"], "expected_schema_version": 1},
         r"operations\[1\] must be an object"),
        ({"table_id": "t", "operations": RENAME * 201, "expected_schema_version": 1},
         "At most 200 operations"),
        ({"table_id": "t", "operations": RENAME},
         "Missing required parameter: expected_schema_version"),
        ({"table_id": "t", "operations": RENAME, "expected_schema_version": True},
         "positive integer"),
        ({"table_id": "t", "operations": RENAME, "expected_schema_version": 0},
         "positive integer"),
        ({"table_id": "t", "operations": RENAME, "expected_schema_version": "1"},
         "positive integer"),
        ({"table_id": "t", "operations": RENAME, "expected_schema_version": 1.0},
         "positive integer"),
    ])
    def test_shape_errors(self, raw, match):
        with pytest.raises(ValueError, match=match):
            handler().validate_params(raw)

    def test_non_dict(self):
        with pytest.raises(ValueError, match="params must be an object"):
            handler().validate_params(["x"])

    @pytest.mark.parametrize("extra", [{"title": "t"}, {"columns": []}, {"mode": "public"}])
    def test_unknown_keys(self, extra):
        with pytest.raises(ValueError, match="Unknown parameter for change_table_schema"):
            handler().validate_params({
                "table_id": "t", "operations": RENAME, "expected_schema_version": 1, **extra,
            })

    def test_injected_keys_dropped(self):
        raw = {"table_id": "t", "operations": RENAME, "expected_schema_version": 1}
        raw.update({k: "forged" for k in INJECTED})
        out = handler().validate_params(raw)
        assert not set(INJECTED) & set(out)

    def test_injected_keys_match_handler(self):
        from chat.action_request_types.change_table_schema import SERVER_INJECTED_KEYS

        assert SERVER_INJECTED_KEYS == frozenset(INJECTED)


# ---------------------------------------------------------------------------
# Pre-card refusals
# ---------------------------------------------------------------------------


class TestPrecardRefusals:
    def test_gate_closed_before_any_db_work(self, env, monkeypatch):
        import chat.action_request_types.table_precard as table_precard
        import db.project_store as project_store

        table = make(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(env.store, "get_table", boom)
        monkeypatch.setattr(project_store, "get_project", boom)
        monkeypatch.setattr(project_store, "list_doc_source_project_ids", boom)
        monkeypatch.setattr(table_precard, "project_is_public", boom)
        for caller in (convo(env), convo(env, project=env.private_project)):
            assert refusal(caller, params(table, RENAME)) == (
                f"tables_disabled: {tables_disabled_message()}"
            )

    def test_missing_and_hidden_byte_equal(self, env):
        table = make(env)
        missing = str(uuid.uuid4())
        hidden = refusal(convo(env, "bob"), params(table, RENAME))
        assert hidden == f"table_not_found: {table_not_found_message(table['id'])}"
        absent = refusal(convo(env, "bob"), params(missing, RENAME, version=1))
        assert absent == f"table_not_found: {table_not_found_message(missing)}"
        # Same text modulo the id itself.
        assert hidden.replace(table["id"], "<id>") == absent.replace(missing, "<id>")
        # A private table from a public conversation is hidden too.
        public = convo(env, project=env.public_project, public=True)
        assert refusal(public, params(table, RENAME)) == hidden

    def test_read_share_denied(self, env):
        table = make(env, shares=[("bob", "read")])
        assert refusal(convo(env, "bob"), params(table, RENAME)) == (
            f"denied: {DENY_READ_ONLY_SHARE}"
        )

    def test_slack_denied(self, env):
        table = make(env)
        assert refusal(convo(env), params(table, RENAME), is_slack_origin=True) == (
            f"denied: {DENY_SCHEMA_SLACK}"
        )

    def test_public_conversation_proposes_a_card_on_its_public_table(self, env):
        # change_table_schema is a public-safe action request: a public
        # conversation gets the card for its own project's public table,
        # and the card shows the table's mode.
        table = make(env, project_id=env.public_project)
        caller = convo(env, project=env.public_project, public=True)
        out = propose(caller, params(table, RENAME))
        assert (out["table_mode"], out["table_scope"]) == ("public", "project")
        assert out["operation_summaries"] == ["Renamed column Notes to Comments"]
        result = execute(out, caller, request_id=9)
        assert result["columns"][-1]["name"] == "Comments"

    def test_public_table_from_private_conversation_denied(self, env):
        """Invariant 1: a private conversation that reads a public table
        (Project Access) can never propose changing it."""
        import db.project_store as project_store

        table = make(env, project_id=env.public_project)
        _run(project_store.set_doc_source_projects(
            uid(env, "alice"), env.private_project, [env.public_project],
        ))
        caller = convo(env, project=env.private_project)
        assert refusal(caller, params(table, RENAME)) == (
            f"denied: {DENY_PUBLIC_TABLE_FROM_PRIVATE}"
        )

    def test_stale_schema_names_current_version_and_columns(self, env):
        table = make(env)
        ui_edit(env, table)
        msg = refusal(convo(env), params(table, RENAME, version=1))
        assert msg.startswith("stale_schema: ")
        assert "schema_version is 2" in msg
        assert "expected_schema_version 2" in msg
        assert not KEY_RE.search(msg) and '"key"' not in msg
        listed = json.loads(msg.split("Current columns: ", 1)[1].split("]. ", 1)[0] + "]")
        # Compact specs: default (null / false / empty) fields left out.
        assert listed == [
            {"name": "Name", "type": "text", "required": True, "description": "the name"},
            {"name": "Status", "type": "enum", "options": ["open", "done", "blocked"]},
            {"name": "Tags", "type": "enum", "options": ["a", "b", "c"], "multiple": True},
            {"name": "Notes", "type": "text"},
        ]

    def test_stale_schema_message_clips_descriptions(self, env):
        table = make(env)
        _run(service.apply_schema_change(
            ui(env), table["id"],
            [{"op": "set_description", "name": "Notes", "description": "d" * 400}],
            expected_schema_version=1,
        ))
        msg = refusal(convo(env), params(table, RENAME, version=1))
        listed = json.loads(msg.split("Current columns: ", 1)[1].split("]. ", 1)[0] + "]")
        assert listed[3]["description"] == "d" * 80 + "\u2026"
        assert len(msg) < 800

    def test_operation_error_names_its_operation(self, env):
        table = make(env)
        ops = [RENAME[0], {"op": "drop_column", "name": "Nope"}]
        assert refusal(convo(env), params(table, ops)) == (
            "unknown_column: Operation 2 (drop_column): no column named 'Nope'."
        )
        ops = [RENAME[0], RENAME[0], {"op": "set_type", "name": "Name"}]
        assert refusal(convo(env), params(table, ops)).startswith(
            "unknown_column: Operation 2 (rename_column): no column named 'Notes'",
        )
        ops = [{"op": "add_column", "column": {"name": "Qty", "type": "number",
                                                 "required": True}}]
        assert refusal(convo(env), params(table, ops)).startswith(
            "required_needs_default: Operation 1 (add_column): ",
        )

    def test_operation_error_message_prefixes_when_missing(self):
        from chat.action_request_types.table_schema_precard import operation_error_message
        from chat.tables.schema_ops import SchemaOperationError

        exc = SchemaOperationError("x", "bad thing", op_index=2, op="drop_column")
        assert operation_error_message(exc) == "Operation 3 (drop_column): bad thing"
        exc = SchemaOperationError("x", "Operation 3 (drop_column): bad", op_index=2)
        assert operation_error_message(exc) == "Operation 3 (drop_column): bad"
        exc = SchemaOperationError("too_many_operations", "list level")
        assert operation_error_message(exc) == "list level"

    def test_rows_violate_set_required(self, env):
        table = make(env)
        msg = refusal(convo(env), params(table, [
            {"op": "set_required", "name": "Notes", "required": True},
        ]))
        assert msg.startswith("rows_violate: Column 'Notes' cannot be made required")
        assert "(_id 2)" in msg

    def test_rows_violate_set_unique(self, env):
        table = make(env, rows=[*ROWS, {"Name": "x"}])
        msg = refusal(convo(env), params(table, [
            {"op": "set_unique", "name": "Name", "unique": True},
        ]))
        assert msg.startswith("rows_violate: Column 'Name' cannot be made unique")
        assert "_id 1, 4" in msg

    def test_rows_violate_remove_enum_option(self, env):
        table = make(env)
        msg = refusal(convo(env), params(table, [
            {"op": "remove_enum_option", "name": "Tags", "option": "a"},
        ]))
        assert msg.startswith("rows_violate: Option 'a' cannot be removed from column 'Tags'")
        assert "_id 1, 2" in msg
        # An unused option is fine.
        propose(convo(env), params(table, [
            {"op": "remove_enum_option", "name": "Status", "option": "blocked"},
        ]))

    def test_duplicate_title(self, env):
        table = make(env)
        make(env, "Other", rows=())
        assert refusal(convo(env), params(table, [{"op": "rename_table", "title": "OTHER"}])) == (
            "duplicate_title: A table titled 'OTHER' already exists."
        )

    def test_nothing_written_by_the_precard(self, env):
        table = make(env)
        before = change_count(env, table)
        propose(convo(env), params(table, [
            {"op": "drop_column", "name": "Notes"},
            {"op": "rename_table", "title": "Renamed"},
        ]))
        fresh = current(env, table)
        assert (fresh["schema_version"], fresh["title"]) == (1, "Inventory")
        assert change_count(env, table) == before


# ---------------------------------------------------------------------------
# Preview payload
# ---------------------------------------------------------------------------

BATCH = [
    {"op": "add_column", "column": {"name": "Qty", "type": "number", "required": True,
                                    "default": 1}},
    {"op": "rename_column", "name": "Name", "new_name": "Title"},
    {"op": "drop_column", "name": "Notes"},
    {"op": "rename_enum_option", "name": "Status", "option": "open", "new_option": "todo"},
    {"op": "rename_enum_option", "name": "Tags", "option": "a", "new_option": "alpha"},
    {"op": "reorder_columns", "names": ["Title", "Status", "Tags", "Qty"]},
]


CHAIN = [
    {"op": "rename_enum_option", "name": "Status", "option": "open", "new_option": "done2"},
    {"op": "rename_enum_option", "name": "Status", "option": "done", "new_option": "fin"},
    {"op": "rename_enum_option", "name": "Status", "option": "done2", "new_option": "open"},
]
ROUND_TRIP = [
    {"op": "rename_enum_option", "name": "Status", "option": "open", "new_option": "x"},
    {"op": "rename_enum_option", "name": "Status", "option": "x", "new_option": "open"},
]


def _preview(p):
    return _run(handler().render_preview(p))


def _entry(out, name):
    return next(
        e for e in out["schema_diff"]["columns"] if (e["after"] or e["before"])["name"] == name
    )


class TestPreview:
    def test_batch_payload(self, env):
        table = make(env)
        out = propose(convo(env), params(table, BATCH))
        assert not has_key_anywhere(out)
        assert not KEY_RE.search(json.dumps(out))
        assert (out["table_title"], out["table_mode"], out["table_scope"]) == (
            "Inventory", "private", "user",
        )
        assert (out["share_summary"], out["require_approval"]) == ("", False)
        assert out["operations_count"] == 6
        assert out["rows_backfilled"] == 3
        assert out["dropped_values"] == {"Notes": 2}
        assert out["operation_summaries"] == [
            "Added column Qty",
            "Renamed column Name to Title",
            "Dropped column Notes",
            "Renamed option open to todo in Status",
            "Renamed option a to alpha in Tags",
            "Reordered columns",
        ]
        diff = out["schema_diff"]
        assert diff["before_order"] == ["Name", "Status", "Tags", "Notes"]
        assert diff["after_order"] == ["Title", "Status", "Tags", "Qty"]
        assert diff["changed"] is True
        by_name = {(e["after"] or e["before"])["name"]: e for e in diff["columns"]}
        assert [(e["after"] or e["before"])["name"] for e in diff["columns"]] == [
            "Title", "Status", "Tags", "Notes", "Qty",
        ]
        assert by_name["Qty"]["status"] == "added" and by_name["Qty"]["before"] is None
        assert by_name["Qty"]["after"]["default"] == "1"
        assert by_name["Title"]["status"] == "renamed"
        assert by_name["Title"]["changes"] == {"name": {"before": "Name", "after": "Title"}}
        assert by_name["Notes"]["status"] == "dropped" and by_name["Notes"]["after"] is None
        assert by_name["Status"]["status"] == "changed"
        # Single enum: two cells held "open"; multi enum: two lists held "a".
        assert by_name["Status"]["cells_rewritten"] == 2
        assert by_name["Status"]["options_renamed"] == [{"option": "open", "new_option": "todo"}]
        assert by_name["Tags"]["cells_rewritten"] == 2
        assert by_name["Tags"]["options_renamed"] == [{"option": "a", "new_option": "alpha"}]
        assert by_name["Title"]["cells_rewritten"] == 0
        assert diff["cells_rewritten_total"] == 4
        json.dumps(out)

    def test_chained_renames_count_distinct_changed_cells(self, env):
        """open x2, done x1; open->done2, done->fin, done2->open: only the
        "done" cell ends up different (5 effect hits, 1 changed cell)."""
        table = make(env)
        out = propose(convo(env), params(table, CHAIN))
        entry = _entry(out, "Status")
        assert entry["cells_rewritten"] == 1
        assert out["schema_diff"]["cells_rewritten_total"] == 1
        fields = _preview(out)
        assert {"key": "Cells rewritten", "value": "1"} in fields

    def test_round_trip_rename_counts_nothing(self, env):
        table = make(env)
        out = propose(convo(env), params(table, ROUND_TRIP))
        entry = _entry(out, "Status")
        assert entry["status"] == "unchanged"
        assert entry["cells_rewritten"] == 0
        assert out["schema_diff"]["cells_rewritten_total"] == 0
        assert "Cells rewritten" not in [f["key"] for f in _preview(out)]

    def test_storage_stats_cells_changed(self, env):
        from chat.tables import schema_ops
        from chat.tables.schema import load_schema

        table = make(env)
        fresh = current(env, table)
        columns = load_schema(fresh["schema"])
        status_key = columns[1].key

        def stats(ops):
            plan = schema_ops.apply_schema_operations(columns, ops, table_empty=False)
            return storage.check_schema_violations(
                table_dir(env, fresh), columns, plan,
                schema_version=fresh["schema_version"],
            )

        chained = stats(CHAIN)
        assert chained["cells_rewritten"] == {status_key: 5}
        assert chained["cells_changed"] == {status_key: 1}
        round_trip = stats(ROUND_TRIP)
        assert round_trip["cells_rewritten"] == {status_key: 4}
        assert round_trip["cells_changed"] == {}
        # A multi enum: a counts once per cell.
        tags_key = columns[2].key
        multi = stats([
            {"op": "rename_enum_option", "name": "Tags", "option": "a", "new_option": "x"},
            {"op": "rename_enum_option", "name": "Tags", "option": "x", "new_option": "y"},
        ])
        assert multi["cells_changed"] == {tags_key: 2}
        # A dropped column is not counted (its values are dropped_values).
        dropped = stats([*CHAIN, {"op": "drop_column", "name": "Status"}])
        assert dropped["cells_changed"] == {}

    def test_reorder_flags_moved(self, env):
        table = make(env)
        out = propose(convo(env), params(table, [
            {"op": "reorder_columns", "names": ["Name", "Tags", "Notes", "Status"]},
        ]))
        diff = out["schema_diff"]
        assert diff["order_changed"] is True
        moved = [e["after"]["name"] for e in diff["columns"] if e["moved"]]
        assert moved == ["Status"]
        assert out["rows_backfilled"] == 0 and out["dropped_values"] == {}

    def test_preview_fields(self, env):
        table = make(env, shares=[("bob", "write")])
        fields = _preview(propose(convo(env), params(table, BATCH)))
        assert [f["key"] for f in fields] == [
            "Table", "Mode", "Scope", "Shares", "Changes", "Cells rewritten", "Dropped values",
        ]
        assert fields[0] == {"key": "Table", "value": "Inventory"}
        assert fields[1] == {"key": "Mode", "value": "private"}
        assert fields[2] == {"key": "Scope", "value": "user"}
        changes = fields[4]
        assert changes["type"] == "table_schema_diff"
        assert changes["value"] == (
            "6 operations: Added column Qty; Renamed column Name to Title; Dropped "
            "column Notes; Renamed option open to todo in Status; Renamed option a to "
            "alpha in Tags; Reordered columns"
        )
        assert changes["schema_diff"]["cells_rewritten_total"] == 4
        assert not has_key_anywhere(changes["schema_diff"])
        assert fields[5] == {"key": "Cells rewritten", "value": "4"}
        assert fields[6] == {"key": "Dropped values", "value": "Notes: 2 values"}
        assert all(isinstance(f["value"], str) for f in fields)
        assert not KEY_RE.search(json.dumps(fields))

    def test_preview_single_op_no_extras(self, env):
        table = make(env)
        fields = _preview(propose(convo(env), params(table, RENAME)))
        assert [f["key"] for f in fields] == ["Table", "Mode", "Scope", "Changes"]
        assert fields[-1]["value"] == "1 operation: Renamed column Notes to Comments"

    def test_project_scope_and_approval_rows(self, env):
        table = make(env, project_id=env.private_project)
        _run(env.store.update_table_metadata(table["id"], require_approval=True))
        fields = _preview(propose(convo(env, project=env.private_project), params(table, RENAME)))
        values = {f["key"]: f["value"] for f in fields}
        assert values["Scope"] == "project"
        assert values["Approval"] == "required by the owner for every change"

    @pytest.mark.parametrize("p", [
        {}, {"operations": "x"}, {"operations": [None, 3, {"op": "add_column"}]},
        {"schema_diff": "junk", "dropped_values": [1], "operations_count": True},
        {"operation_summaries": [1, 2], "table_title": 7},
    ])
    def test_odd_params_never_raise(self, p):
        fields = _preview(p)
        assert fields and all(isinstance(f["value"], str) for f in fields)
        json.dumps(fields)

    @pytest.mark.parametrize("p, expected", [
        ({"operation_summaries": ["A"]}, "A"),
        ({"operation_summaries": ["A", "B", "C"]}, "A and 2 more"),
        ({"operations": RENAME}, "Renamed column Notes to Comments"),
        ({"operations": RENAME * 2}, "Renamed column Notes to Comments and 1 more"),
        ({"operations": [{"op": "add_column"}]}, "1 schema change"),
        ({"operations": [], "table_title": "Inventory"}, "Inventory"),
        ({"table_title": "Inventory"}, "Inventory"),
        ({"operations": "x", "table_title": 5}, ""),
        ({}, ""),
    ])
    def test_summary_snippet(self, p, expected):
        assert handler().summary_snippet(p) == expected

    def test_summary_snippet_non_dict(self):
        assert handler().summary_snippet(None) == ""  # type: ignore[arg-type]

    def test_labels_and_registration(self):
        from chat.action_request_types import get_handler, get_summary_snippet
        from chat.action_request_types.change_table_schema import ChangeTableSchemaHandler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        from db.models import ActionRequestType

        h = handler()
        assert (h.display_name, h.approve_label, h.resolved_label) == (
            "Change Table Schema", "Apply", "Schema changed",
        )
        assert isinstance(get_handler("change_table_schema"), ChangeTableSchemaHandler)
        assert get_handler("change_table_schema").type_name == (
            ActionRequestType.CHANGE_TABLE_SCHEMA
        )
        assert "change_table_schema" in ACTION_REQUEST_TYPE_ENUM
        assert get_summary_snippet(
            "change_table_schema", {"operation_summaries": ["A", "B"]},
        ) == "A and 1 more"

    def test_registered_table_arms_import(self):
        import importlib

        from chat.action_request_types import get_handler
        from chat.action_request_types.table_precard import (
            _LAZY_ARMS,
            TABLE_REQUEST_TYPES,
        )

        checked = []
        for req_type in TABLE_REQUEST_TYPES:
            try:
                get_handler(req_type)
            except Exception:  # noqa: BLE001 -- not registered (yet)
                continue
            if req_type in _LAZY_ARMS:
                module_name, attr = _LAZY_ARMS[req_type]
                assert callable(getattr(importlib.import_module(module_name), attr))
            checked.append(req_type)
        assert "change_table_schema" in checked


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


class TestExecute:
    def test_success(self, env):
        table = make(env)
        caller = convo(env)
        validated = propose(caller, params(table, BATCH))
        before = change_count(env, table)
        result = execute(validated, caller, request_id=41)
        assert result == {
            "table_id": table["id"], "title": "Inventory", "schema_version": 2,
            "change_id": before + 1,
            "columns": [{"name": "Title", "type": "text"},
                        {"name": "Status", "type": "enum"},
                        {"name": "Tags", "type": "enum"},
                        {"name": "Qty", "type": "number"}],
        }
        fresh = current(env, table)
        assert fresh["schema_version"] == 2
        assert fresh["last_write_source"] == "action_request:41"
        assert change_count(env, table) == before + 1
        change = storage.get_change(table_dir(env, table), result["change_id"])
        assert change["source"] == "action_request:41"
        assert change["actor_user_id"] == uid(env, "alice")
        data = _run(service.query_rows(convo(env), table["id"]))[2].rows
        assert [r["Status"] for r in data] == ["todo", "todo", "done"]
        assert [r["Tags"] for r in data] == [["alpha", "b"], ["alpha"], ["c"]]
        assert [r["Qty"] for r in data] == ["1", "1", "1"]

    def test_rename_table(self, env):
        table = make(env)
        caller = convo(env)
        result = execute(
            propose(caller, params(table, [{"op": "rename_table", "title": "Stock"}])), caller,
        )
        assert result["title"] == "Stock"
        assert current(env, table)["title"] == "Stock"

    def test_rename_table_collision_at_approve(self, env):
        table = make(env)
        other = make(env, "Other", rows=())
        caller = convo(env)
        validated = propose(caller, params(table, [{"op": "rename_table", "title": "Stock"}]))
        _run(env.store.update_table_metadata(other["id"], title="STOCK"))
        count = change_count(env, table)
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value).startswith("duplicate_title: ")
        fresh = current(env, table)
        assert (fresh["schema_version"], fresh["title"]) == (1, "Inventory")
        assert change_count(env, table) == count

    def test_toctou_ui_edit_between_proposal_and_approve(self, env):
        table = make(env)
        caller = convo(env)
        validated = propose(caller, params(table, RENAME))
        ui_edit(env, table)
        count = change_count(env, table)
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        msg = str(exc.value)
        assert msg.startswith("stale_schema: ")
        assert "current schema_version 2" in msg
        assert not KEY_RE.search(msg)
        fresh = current(env, table)
        assert fresh["schema_version"] == 2
        assert [c["name"] for c in fresh["schema"]] == ["Name", "Status", "Tags", "Notes"]
        assert change_count(env, table) == count

    def test_rows_violate_at_approve(self, env):
        table = make(env, rows=[ROWS[0]])
        caller = convo(env)
        validated = propose(caller, params(table, [
            {"op": "set_required", "name": "Notes", "required": True},
        ]))
        _run(service.apply_row_write(caller, table["id"], "insert", {"rows": [{"Name": "q"}]}))
        count = change_count(env, table)
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value).startswith(
            "rows_violate: Column 'Notes' cannot be made required"
        )
        assert "(_id 2)" in str(exc.value)
        assert current(env, table)["schema_version"] == 1
        assert change_count(env, table) == count

    def test_write_share_recipient_credited(self, env):
        from db import action_request_store

        table = make(env, shares=[("bob", "write")])
        caller = convo(env, "bob")
        raw = params(table, RENAME)
        validated = propose(caller, raw)
        assert validated["share_summary"]
        request_id = make_request(env, caller, validated, who="bob")
        result = execute(validated, caller, request_id=request_id)
        change = storage.get_change(table_dir(env, table), result["change_id"])
        assert change["actor_user_id"] == uid(env, "bob")
        assert change["source"] == f"action_request:{request_id}"
        owners = _run(action_request_store.get_action_request_owners([request_id]))
        assert owners == {request_id: uid(env, "bob")}
        assert current(env, table)["owner_id"] == uid(env, "alice")

    def test_share_revoked_before_approve(self, env):
        table = make(env, shares=[("bob", "write")])
        caller = convo(env, "bob")
        validated = propose(caller, params(table, RENAME))
        share_id = current(env, table)["shares"][0]["id"]
        assert _run(env.store.remove_share(table["id"], share_id))
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == f"table_not_found: {table_not_found_message(table['id'])}"
        assert current(env, table)["schema_version"] == 1

    def test_share_downgraded_before_approve(self, env):
        table = make(env, shares=[("bob", "write")])
        caller = convo(env, "bob")
        validated = propose(caller, params(table, RENAME))
        _run(env.store.add_share(table["id"], uid(env, "bob"), "read"))
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == f"denied: {DENY_READ_ONLY_SHARE}"

    def test_public_table_never_changed_from_private_context(self, env):
        """Invariant 1 at approve: the executor re-resolves with the
        proposal's private context, so a public table is refused even when
        the params name it."""
        import db.project_store as project_store

        table = make(env, project_id=env.public_project)
        _run(project_store.set_doc_source_projects(
            uid(env, "alice"), env.private_project, [env.public_project],
        ))
        caller = convo(env, project=env.private_project)
        validated = handler().validate_params(params(table, RENAME))
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == f"denied: {DENY_PUBLIC_TABLE_FROM_PRIVATE}"
        assert current(env, table)["schema_version"] == 1

    def test_gate_closed_at_approve(self, env):
        table = make(env)
        caller = convo(env)
        validated = propose(caller, params(table, RENAME))
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == f"tables_disabled: {tables_disabled_message()}"

    def test_operation_error_at_approve(self, env):
        table = make(env)
        validated = handler().validate_params(params(table, [
            RENAME[0], {"op": "drop_column", "name": "Nope"},
        ]))
        with pytest.raises(RuntimeError) as exc:
            execute(validated, convo(env))
        assert str(exc.value) == (
            "unknown_column: Operation 2 (drop_column): no column named 'Nope'."
        )

    def test_request_id_required(self, env):
        table = make(env)
        caller = convo(env)
        with pytest.raises(RuntimeError, match="^invalid_args: "):
            execute(propose(caller, params(table, RENAME)), caller, request_id=None)
        assert current(env, table)["schema_version"] == 1

    @pytest.mark.parametrize("bad", [
        {}, {"table_id": 5, "operations": [], "expected_schema_version": 1},
        {"table_id": "t", "operations": "x", "expected_schema_version": 1},
        {"table_id": "t", "operations": [], "expected_schema_version": True},
    ])
    def test_malformed_params(self, env, bad):
        with pytest.raises(RuntimeError, match="^invalid_request: Malformed"):
            execute(bad, convo(env))


# ---------------------------------------------------------------------------
# End to end: create_action_request arm -> card -> resolve route
# ---------------------------------------------------------------------------


@pytest.fixture()
def ar_env(env, monkeypatch):
    """env + the wait-handle store on the same DB."""
    import db.action_request_store as action_request_store
    import db.tool_wait_handle_store as tool_wait_handle_store
    from chat.storage import ChatStorage
    from chat.wait_handles import resume as wait_resume

    monkeypatch.setattr(
        tool_wait_handle_store, "AsyncSessionLocal", env.store.AsyncSessionLocal,
    )
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )
    monkeypatch.setattr(wait_resume, "maybe_kick_resume", lambda *a, **k: None)
    env.action_request_store = action_request_store
    return env


def _arm(caller, p):
    from chat.gemini_api.turn_tools import TurnState, _handle_create_action_request
    from tests.test_turn_tools_registry import _call, _make_ctx

    ctx, _events = _make_ctx(
        user=caller.user, conversation_id=caller.conversation_id,
        project_id=caller.project_id,
    )
    call = _call("create_action_request", {
        "request_type": "change_table_schema", "params": p, "reasoning": "columns",
    })
    return _run(_handle_create_action_request(ctx, call, TurnState()))


class _Req:
    app = None


class TestEndToEnd:
    def test_arm_refusal(self, env):
        table = make(env)
        ui_edit(env, table)
        error = json.loads(_arm(convo(env), params(table, RENAME, version=1)))["error"]
        assert error.startswith("Invalid parameters: stale_schema: ")
        assert "schema_version is 2" in error

    def _open_card(self, env, caller, p):
        from chat.gemini_api.turn_tools import SuspendForActionRequest
        from db import conversation_store

        _run(conversation_store.create_conversation(
            user_id=caller.user["id"], conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc), project_id=caller.project_id,
        ))
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, p)
        return suspended.value.request_id

    def test_card_then_approve(self, ar_env):
        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request

        table = make(ar_env)
        caller = convo(ar_env)
        # A forged injected key is dropped and recomputed.
        request_id = self._open_card(
            ar_env, caller, {**params(table, BATCH), "table_mode": "public"},
        )
        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["request_type"] == "change_table_schema" and row["status"] == "open"
        assert row["params"]["table_mode"] == "private"
        assert row["params"]["schema_diff"]["cells_rewritten_total"] == 4
        assert not has_key_anywhere(row["params"])
        assert current(ar_env, table)["schema_version"] == 1

        resolved = _run(resolve_user_action_request(
            request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
        ))
        assert resolved["status"] == "executed"
        assert resolved["resolved_label"] == "Schema changed"
        assert resolved["summary_snippet"] == "Added column Qty and 5 more"
        assert resolved["result"]["schema_version"] == 2
        fresh = current(ar_env, table)
        assert fresh["schema_version"] == 2
        assert fresh["last_write_source"] == f"action_request:{request_id}"

    def test_approve_after_ui_edit_keeps_request_open(self, ar_env):
        from fastapi import HTTPException

        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request

        table = make(ar_env)
        caller = convo(ar_env)
        request_id = self._open_card(ar_env, caller, params(table, RENAME))
        ui_edit(ar_env, table)
        count = change_count(ar_env, table)
        with pytest.raises(HTTPException) as exc:
            _run(resolve_user_action_request(
                request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
            ))
        assert exc.value.status_code == 500
        assert exc.value.detail["message"].startswith(
            "Failed to execute request: stale_schema: "
        )
        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["status"] == "open"
        assert change_count(ar_env, table) == count
