"""The ``write_table_rows`` action request: handler, pre-card arm and wiring.

Runs against the ``world`` fixture of tests/test_table_tools_write.py
(isolated DB and TABLES_DIR, ``tables`` gate open; alice owns ``mine``
(unshared), ``read_shared`` / ``write_shared`` (shared with bob), ``open``
/ ``open_write`` / ``open_read`` (public project), each holding the SEED
rows Apple (1, S1) and Pear (2, S2)). Covers:

1. ``validate_params``: shape per op, unknown and injected keys, the
   write tool's ``suggested_request`` passing unchanged.
2. Pre-card refusals: gate closed before any DB work, hidden vs missing
   byte-equal, a free table -> ``use_tool_directly`` naming each tool,
   denied (read share, a public table from a private conversation, Slack),
   invalid_rows with the problem list, too_many_rows, a filter delete
   matching nothing, unknown key column, filter errors.
3. The injected keys and the ``rows_diff`` payload per op, the 50-row cap,
   no column key anywhere.
4. ``execute`` per op (one change, ``action_request:<id>``, actor the
   approver, rows as previewed), ``stale_schema``, ``rows_changed``
   (update, delete, upsert split), nothing written on a refusal, the
   recipient's approval credit, a revoked share, the owner's
   ``require_approval`` switch, invariant 1 at approve time.
5. Preview fields, labels, summary snippet, registration.
6. End to end through the ``create_action_request`` arm and the resolve
   route.
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
    DENY_SLACK_NEEDS_APPROVAL,
)
from chat.tables.constants import (
    TABLE_WRITE_MAX_ROWS,
    table_not_found_message,
    tables_disabled_message,
)
from tests.test_table_service import convo, make_table, table_dir, ui, uid
from tests.test_table_tools_write import (  # noqa: F401  (env, world are fixtures)
    _dispatch,
    _seed,
    _set_require_approval,
    _snapshot,
    _state,
    env,
    world,
)

TOOLS = {"insert": "insert_rows", "update": "update_rows",
         "upsert": "upsert_rows", "delete": "delete_rows"}
INJECTED = (
    "table_title", "table_mode", "table_scope", "share_summary", "require_approval",
    "approval_reason", "schema_version", "rows_digest", "row_counts", "upsert_split",
    "rows_diff",
)
COLUMN_KEY = re.compile(r"^c_[0-9a-f]{8}$")


def _run(coro):
    return asyncio.run(coro)


def handler():
    from chat.action_request_types.write_table_rows import WriteTableRowsHandler
    return WriteTableRowsHandler()


def op_params(op, table_id):
    """Valid params per op against a SEED-ed table."""
    base = {"table_id": table_id, "operation": op}
    if op == "insert":
        return {**base, "rows": [{"Name": "Plum", "SKU": "S3"}]}
    if op == "update":
        return {**base, "updates": [{"_id": 1, "Price": 7}]}
    if op == "upsert":
        return {**base, "key_column": "SKU", "rows": [
            {"SKU": "S2", "Price": 9}, {"SKU": "S4", "Name": "Fig"},
        ]}
    return {**base, "row_ids": [2]}


def precard(validated, caller, *, is_slack_origin=False):
    from chat.action_request_types.table_precard import table_precard_check

    _run(table_precard_check(
        "write_table_rows", validated, caller.user, caller.project_id,
        caller.conversation_id, is_slack_origin=is_slack_origin,
    ))
    return validated


def propose(caller, raw, **kwargs):
    return precard(handler().validate_params(raw), caller, **kwargs)


def refusal(caller, raw, **kwargs) -> str:
    with pytest.raises(ValueError) as exc:
        propose(caller, raw, **kwargs)
    return str(exc.value)


def execute(validated, caller, request_id=7):
    return _run(handler().execute(
        validated, caller.user,
        conversation_id=caller.conversation_id,
        project_id=caller.project_id,
        request_id=request_id,
    ))


def exec_error(validated, caller, request_id=7) -> str:
    with pytest.raises(RuntimeError) as exc:
        execute(validated, caller, request_id)
    return str(exc.value)


def has_column_key(value, *, key_field=True) -> bool:
    """Any ``c_xxxxxxxx`` key or string value anywhere, or (``key_field``)
    a ``key`` key (preview fields use ``key`` as their label)."""
    if isinstance(value, dict):
        return any(
            (key_field and k == "key")
            or (isinstance(k, str) and COLUMN_KEY.match(k))
            or has_column_key(v, key_field=key_field)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(has_column_key(v, key_field=key_field) for v in value)
    return isinstance(value, str) and bool(COLUMN_KEY.match(value))


def changes(env, table):
    return storage.list_changes(table_dir(env, table))


def latest_change(env, table):
    return storage.get_change(table_dir(env, table), changes(env, table)[0]["id"])


def ui_write(env, table, op, params, who="alice"):
    return _run(service.apply_row_write(ui(env, who), table["id"], op, params))


# ---------------------------------------------------------------------------
# 1. validate_params
# ---------------------------------------------------------------------------


class TestValidateParams:
    @pytest.mark.parametrize("op", list(TOOLS))
    def test_shape_per_op(self, op):
        raw = op_params(op, "t1")
        assert handler().validate_params(raw) == raw

    def test_filter_delete_and_coercion(self):
        f = {"column": "Price", "op": "gt", "value": "1"}
        assert handler().validate_params(
            {"table_id": "t", "operation": "delete", "filter": f},
        ) == {"table_id": "t", "operation": "delete", "filter": f}
        assert handler().validate_params(
            {"table_id": "t", "operation": "delete", "row_ids": ["2", 3.0]},
        )["row_ids"] == [2, 3]

    @pytest.mark.parametrize("raw, match", [
        (["x"], "params must be an object"),
        ({"operation": "insert", "rows": [{}]}, "^invalid_args: table_id"),
        ({"table_id": "t", "operation": "merge"}, "^invalid_args: operation must be one of"),
        ({"table_id": "t", "operation": "insert", "rows": []}, "^invalid_args: rows must be"),
        ({"table_id": "t", "operation": "update", "updates": [{"Price": 1}]},
         "^invalid_args: each update needs an integer _id"),
        ({"table_id": "t", "operation": "upsert", "rows": [{}]}, "^invalid_args: key_column"),
        ({"table_id": "t", "operation": "delete"}, "^invalid_args: Give exactly one"),
        ({"table_id": "t", "operation": "delete", "row_ids": [1], "filter": {}},
         "^invalid_args: Give exactly one"),
        ({"table_id": "t", "operation": "insert", "rows": [{}], "row_ids": [1]},
         "^invalid_args: Unexpected key"),
        ({"table_id": "t", "operation": "insert", "rows": [{}], "owner": 1},
         "^invalid_args: Unexpected key"),
    ])
    def test_shape_errors(self, raw, match):
        with pytest.raises(ValueError, match=match):
            handler().validate_params(raw)

    def test_too_many_rows(self):
        with pytest.raises(ValueError, match="^too_many_rows: This write would touch"):
            handler().validate_params({
                "table_id": "t", "operation": "insert",
                "rows": [{"Name": "x"}] * (TABLE_WRITE_MAX_ROWS + 1),
            })

    def test_injected_keys_dropped(self):
        raw = op_params("update", "t")
        echoed = {**raw, **{k: "junk" for k in INJECTED}}
        assert handler().validate_params(echoed) == raw

    @pytest.mark.parametrize("op", list(TOOLS))
    def test_suggested_request_round_trip(self, world, op):
        args = {k: v for k, v in op_params(op, world.t["write_shared"]["id"]).items()
                if k != "operation"}
        got = _dispatch(world, TOOLS[op], args)
        assert got["error"] == "approval_required"
        params = got["suggested_request"]["params"]
        assert got["suggested_request"]["request_type"] == "write_table_rows"
        assert handler().validate_params(params) == params


# ---------------------------------------------------------------------------
# 2. Pre-card refusals
# ---------------------------------------------------------------------------


class TestPrecardRefusals:
    def test_gate_closed_before_any_db_work(self, world, monkeypatch):
        import chat.action_request_types.table_precard as table_precard

        world.fg.set_feature_enabled(world.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(table_precard, "project_is_public", boom)
        monkeypatch.setattr(service, "preview_row_write", boom)
        monkeypatch.setattr(world.store, "get_table", boom)
        for op in TOOLS:
            assert refusal(convo(world), op_params(op, world.t["write_shared"]["id"])) == (
                f"tables_disabled: {tables_disabled_message()}"
            )

    @pytest.mark.parametrize("op", list(TOOLS))
    def test_hidden_and_missing_byte_equal(self, world, op):
        hidden = world.t["mine"]["id"]
        missing = str(uuid.uuid4())
        got_hidden = refusal(convo(world, "bob"), op_params(op, hidden))
        got_missing = refusal(convo(world, "bob"), op_params(op, missing))
        assert got_hidden == f"table_not_found: {table_not_found_message(hidden)}"
        assert got_missing.replace(missing, hidden) == got_hidden

    @pytest.mark.parametrize("op", list(TOOLS))
    def test_free_table_use_tool_directly(self, world, op):
        table = world.t["mine"]
        before = _snapshot(world, table)
        assert refusal(convo(world), op_params(op, table["id"])) == (
            f"use_tool_directly: This table is writable directly; call {TOOLS[op]} "
            "instead of proposing an action request."
        )
        assert _snapshot(world, table) == before

    def test_public_table_free_from_its_public_conversation(self, world):
        # Defense in depth: public conversations never reach the pre-card,
        # and their free verdict refuses the card anyway.
        caller = convo(world, project=world.public_project, public=True)
        assert refusal(caller, op_params("insert", world.t["open"]["id"])).startswith(
            "use_tool_directly: ",
        )

    @pytest.mark.parametrize("op", list(TOOLS))
    def test_read_share_denied(self, world, op):
        assert refusal(convo(world, "bob"), op_params(op, world.t["read_shared"]["id"])) == (
            f"denied: {DENY_READ_ONLY_SHARE}"
        )

    @pytest.mark.parametrize("key", ["open", "open_write"])
    def test_public_table_from_private_conversation_denied(self, world, key):
        # Invariant 1: never a card writing a public table from a private
        # conversation. The owner's private project reads it through its
        # doc sources and the owner's standalone chat by ownership (both
        # denied); a recipient's standalone chat sees nothing.
        reader = convo(world, project=world.private_project)
        for op in TOOLS:
            tid = world.t[key]["id"]
            for caller in (reader, convo(world)):
                assert refusal(caller, op_params(op, tid)) == (
                    f"denied: {DENY_PUBLIC_TABLE_FROM_PRIVATE}"
                )
            assert refusal(convo(world, "bob"), op_params(op, tid)) == (
                f"table_not_found: {table_not_found_message(tid)}"
            )

    def test_slack_denied(self, world):
        assert refusal(
            convo(world), op_params("insert", world.t["write_shared"]["id"]),
            is_slack_origin=True,
        ) == f"denied: {DENY_SLACK_NEEDS_APPROVAL}"

    def test_invalid_rows_lists_problems(self, world):
        tid = world.t["write_shared"]["id"]
        msg = refusal(convo(world), {"table_id": tid, "operation": "insert", "rows": [
            {"Name": "ok", "SKU": "S9"},
            {"Name": "x", "Price": "abc", "Colour": "red"},
            {"Price": 1, "SKU": "S1"},
        ]})
        assert msg.startswith("invalid_rows: The rows have ")
        assert "Problems: " in msg
        assert "row 1, column 'Price': " in msg
        assert "row 1, column 'Colour': " in msg
        assert "row 2, column 'Name': " in msg
        assert "row 2, column 'SKU': " in msg

    def test_update_unknown_id_is_invalid_rows(self, world):
        tid = world.t["write_shared"]["id"]
        msg = refusal(convo(world), {"table_id": tid, "operation": "update",
                                     "updates": [{"_id": 99, "Price": 1}]})
        assert msg.startswith("invalid_rows: ") and "row 0, column '_id'" in msg

    def test_too_many_rows_filter_delete(self, world, monkeypatch):
        monkeypatch.setattr(storage, "TABLE_WRITE_MAX_ROWS", 1)
        tid = world.t["write_shared"]["id"]
        msg = refusal(convo(world), {"table_id": tid, "operation": "delete",
                                     "filter": {"column": "Price", "op": "gt", "value": "0"}})
        assert msg.startswith("too_many_rows: This write would touch 2 rows")

    def test_filter_delete_matching_nothing(self, world):
        tid = world.t["write_shared"]["id"]
        assert refusal(convo(world), {
            "table_id": tid, "operation": "delete",
            "filter": {"column": "Price", "op": "gt", "value": "100"},
        }) == "no_rows: The filter matches no rows; nothing to delete."

    def test_filter_error(self, world):
        tid = world.t["write_shared"]["id"]
        msg = refusal(convo(world), {"table_id": tid, "operation": "delete",
                                     "filter": {"column": "Nope", "op": "eq", "value": 1}})
        code, text = msg.split(": ", 1)
        assert code.isidentifier() and code != "invalid_request"
        assert "Nope" in text

    def test_unknown_key_column(self, world):
        tid = world.t["write_shared"]["id"]
        msg = refusal(convo(world), {"table_id": tid, "operation": "upsert",
                                     "key_column": "Nope", "rows": [{"Name": "x"}]})
        assert msg.startswith("unknown_key_column: ")
        msg = refusal(convo(world), {"table_id": tid, "operation": "upsert",
                                     "key_column": "Name", "rows": [{"Name": "x"}]})
        assert msg.startswith("key_not_unique: ")

    def test_foreign_project(self, world):
        caller = convo(world, "bob", project=world.private_project)
        assert refusal(caller, op_params("insert", world.t["write_shared"]["id"])).startswith(
            "project_not_found: ",
        )


# ---------------------------------------------------------------------------
# 3. Injected keys and rows_diff
# ---------------------------------------------------------------------------


class TestInjection:
    def test_card_fields_and_pins(self, world):
        table = world.t["write_shared"]
        out = propose(convo(world), op_params("update", table["id"]))
        assert out["table_title"] == "Write shared"
        assert (out["table_mode"], out["table_scope"]) == ("private", "user")
        assert out["share_summary"]
        assert out["require_approval"] is False
        assert out["approval_reason"] == "shared"
        assert out["schema_version"] == table["schema_version"]
        assert re.fullmatch(r"[0-9a-f]{64}", out["rows_digest"])
        assert out["row_counts"] == {"inserted": 0, "updated": 1, "deleted": 0}
        assert "upsert_split" not in out
        assert not has_column_key(out)
        json.dumps(out)

    def test_insert_diff(self, world):
        out = propose(convo(world), op_params("insert", world.t["write_shared"]["id"]))
        assert out["row_counts"] == {"inserted": 1, "updated": 0, "deleted": 0}
        assert out["rows_diff"] == {
            "columns": ["Name", "Price", "SKU"],
            "rows": [{"_id": None, "status": "added", "before": None,
                      "after": {"Name": "Plum", "Price": None, "SKU": "S3"},
                      "changed_columns": []}],
            "total": 1, "truncated": False,
        }

    def test_update_diff(self, world):
        tid = world.t["write_shared"]["id"]
        out = propose(convo(world), {"table_id": tid, "operation": "update", "updates": [
            {"_id": 2, "Price": 5, "Name": "Pear"}, {"_id": 1, "Name": "Green apple"},
        ]})
        assert out["rows_diff"] == {
            "columns": ["Name", "Price", "SKU"],
            "rows": [
                {"_id": 1, "status": "changed",
                 "before": {"Name": "Apple", "Price": "1", "SKU": "S1"},
                 "after": {"Name": "Green apple", "Price": "1", "SKU": "S1"},
                 "changed_columns": ["Name"]},
                {"_id": 2, "status": "changed",
                 "before": {"Name": "Pear", "Price": "2", "SKU": "S2"},
                 "after": {"Name": "Pear", "Price": "5", "SKU": "S2"},
                 "changed_columns": ["Price"]},
            ],
            "total": 2, "truncated": False,
        }

    def test_upsert_diff_and_split(self, world):
        out = propose(convo(world), op_params("upsert", world.t["write_shared"]["id"]))
        assert out["upsert_split"] == {"insert": 1, "update": 1}
        assert out["row_counts"] == {"inserted": 1, "updated": 1, "deleted": 0}
        assert out["rows_diff"] == {
            "columns": ["Name", "Price", "SKU"],
            "rows": [
                {"_id": 2, "status": "changed",
                 "before": {"Name": "Pear", "Price": "2", "SKU": "S2"},
                 "after": {"Name": "Pear", "Price": "9", "SKU": "S2"},
                 "changed_columns": ["Price"]},
                {"_id": None, "status": "added", "before": None,
                 "after": {"Name": "Fig", "Price": None, "SKU": "S4"},
                 "changed_columns": []},
            ],
            "total": 2, "truncated": False,
        }

    def test_delete_by_ids_diff(self, world):
        out = propose(convo(world), op_params("delete", world.t["write_shared"]["id"]))
        assert out["row_counts"] == {"inserted": 0, "updated": 0, "deleted": 1}
        assert out["rows_diff"]["rows"] == [
            {"_id": 2, "status": "deleted",
             "before": {"Name": "Pear", "Price": "2", "SKU": "S2"},
             "after": None, "changed_columns": []},
        ]

    def test_delete_by_filter_diff(self, world):
        tid = world.t["write_shared"]["id"]
        out = propose(convo(world), {"table_id": tid, "operation": "delete",
                                     "filter": {"column": "Price", "op": "gt", "value": "0"}})
        assert [r["_id"] for r in out["rows_diff"]["rows"]] == [1, 2]
        assert {r["status"] for r in out["rows_diff"]["rows"]} == {"deleted"}
        assert out["rows_diff"]["total"] == 2

    def test_cap_at_50_rows(self, world):
        table = make_table(world, "Big", shares=[("bob", "write")])
        ui_write(world, table, "insert",
                 {"rows": [{"Name": f"n{i}", "SKU": f"K{i}", "Price": i} for i in range(60)]})
        out = propose(convo(world), {"table_id": table["id"], "operation": "update",
                                     "updates": [{"_id": i, "Price": 1000 + i}
                                                 for i in range(1, 61)]})
        diff = out["rows_diff"]
        assert (diff["total"], diff["truncated"], len(diff["rows"])) == (60, True, 50)
        assert [r["_id"] for r in diff["rows"]] == list(range(1, 51))
        assert out["row_counts"]["updated"] == 60
        assert not has_column_key(out)

    def test_exactly_50_rows_not_truncated(self, world):
        table = make_table(world, "Fifty", shares=[("bob", "write")])
        ui_write(world, table, "insert",
                 {"rows": [{"Name": f"n{i}", "SKU": f"K{i}", "Price": i} for i in range(50)]})
        out = propose(convo(world), {"table_id": table["id"], "operation": "update",
                                     "updates": [{"_id": i, "Price": 1000 + i}
                                                 for i in range(1, 51)]})
        diff = out["rows_diff"]
        assert (diff["total"], diff["truncated"], len(diff["rows"])) == (50, False, 50)

    def test_no_column_keys_any_op(self, world):
        tid = world.t["write_shared"]["id"]
        for op in TOOLS:
            out = propose(convo(world), op_params(op, tid))
            assert not has_column_key(out)
            fields = _run(handler().render_preview(out))
            assert not has_column_key(fields, key_field=False)
            assert not has_column_key([f.get("rows_diff") for f in fields])

    def test_require_approval_on_unshared(self, world):
        table = world.t["mine"]
        _set_require_approval(world, table)
        out = propose(convo(world), op_params("insert", table["id"]))
        assert out["approval_reason"] == "required_by_owner"
        assert out["require_approval"] is True
        assert out["share_summary"] == ""
        fields = _run(handler().render_preview(out))
        assert {"key": "Approval", "value": "required by the owner for every change"} in fields
        result = execute(out, convo(world))
        assert result["inserted"] == 1


# ---------------------------------------------------------------------------
# 4. Execute
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.parametrize("op", list(TOOLS) + ["delete_filter"])
    def test_success_per_op(self, world, op):
        table = world.t["write_shared"]
        if op == "delete_filter":
            raw = {"table_id": table["id"], "operation": "delete",
                   "filter": {"column": "SKU", "op": "eq", "value": "S1"}}
        else:
            raw = op_params(op, table["id"])
        caller = convo(world)
        card = propose(caller, raw)
        n_changes = len(changes(world, table))
        result = execute(card, caller, request_id=41)

        counts = card["row_counts"]
        assert (result["inserted"], result["updated"], result["deleted"]) == (
            counts["inserted"], counts["updated"], counts["deleted"],
        )
        assert result["table_id"] == table["id"]
        assert result["title"] == "Write shared"
        assert result["operation"] == raw["operation"]
        assert result["change_id"] is not None
        assert len(changes(world, table)) == n_changes + 1
        change = latest_change(world, table)
        assert change["source"] == "action_request:41"
        assert change["actor_user_id"] == uid(world, "alice")
        fresh = world.store
        assert _run(fresh.get_table(table["id"]))["last_write_source"] == "action_request:41"
        assert result["row_count"] == len(_state(world, table))

        # The table now holds exactly what the card showed.
        state = {r["_id"]: {k: v for k, v in r.items() if k != "_id"}
                 for r in _state(world, table)}
        for entry in card["rows_diff"]["rows"]:
            if entry["status"] == "deleted":
                assert entry["_id"] not in state
            elif entry["status"] == "changed":
                assert state[entry["_id"]] == entry["after"]
            else:
                assert entry["after"] in state.values()

    def test_stale_schema(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("update", table["id"]))
        _run(service.apply_schema_change(
            ui(world), table["id"],
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"}}],
            expected_schema_version=card["schema_version"],
        ))
        before = _snapshot(world, table)
        assert exec_error(card, caller).startswith(
            "stale_schema: The table's columns were changed by someone else",
        )
        assert _snapshot(world, table) == before

    @pytest.mark.parametrize("op", ["update", "delete"])
    def test_rows_changed_value(self, world, op):
        table = world.t["write_shared"]
        caller = convo(world)
        raw = (op_params("update", table["id"]) if op == "update"
               else {"table_id": table["id"], "operation": "delete", "row_ids": [1]})
        card = propose(caller, raw)
        ui_write(world, table, "update", {"updates": [{"_id": 1, "Name": "Quince"}]})
        before = _snapshot(world, table)
        msg = exec_error(card, caller)
        assert msg.startswith("rows_changed: The rows changed since this write was proposed")
        assert "Row 1 now:" in msg and "Quince" in msg
        assert _snapshot(world, table) == before

    def test_rows_changed_row_vanished(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("update", table["id"]))
        ui_write(world, table, "delete", {"row_ids": [1]})
        before = _snapshot(world, table)
        assert "Row 1 no longer exists" in exec_error(card, caller)
        assert _snapshot(world, table) == before

    def test_rows_changed_filter_delete_matches_more(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, {"table_id": table["id"], "operation": "delete",
                                "filter": {"column": "Price", "op": "gt", "value": "1"}})
        ui_write(world, table, "insert", {"rows": [{"Name": "Kiwi", "Price": 3, "SKU": "S7"}]})
        before = _snapshot(world, table)
        assert exec_error(card, caller).startswith("rows_changed: ")
        assert _snapshot(world, table) == before

    def test_rows_changed_upsert_split(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("upsert", table["id"]))
        assert card["upsert_split"] == {"insert": 1, "update": 1}
        # S4 now exists: the upsert would update two rows, insert none.
        ui_write(world, table, "insert", {"rows": [{"Name": "Fig", "SKU": "S4"}]})
        before = _snapshot(world, table)
        msg = exec_error(card, caller)
        assert msg.startswith("rows_changed: ")
        assert "S4" in msg
        assert _snapshot(world, table) == before

    def test_unique_clash_meanwhile(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("insert", table["id"]))
        ui_write(world, table, "insert", {"rows": [{"Name": "Other", "SKU": "S3"}]})
        before = _snapshot(world, table)
        msg = exec_error(card, caller)
        assert msg.startswith("invalid_rows: The rows have 1 problem")
        assert "row 0, column 'SKU': " in msg
        assert _snapshot(world, table) == before

    def test_missing_pins_fail_closed(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("insert", table["id"]))
        before = _snapshot(world, table)
        for drop in ("schema_version", "rows_digest"):
            tampered = {k: v for k, v in card.items() if k != drop}
            assert exec_error(tampered, caller).startswith("stale_request: ")
        assert exec_error({**card, "schema_version": True}, caller).startswith("stale_request: ")
        assert _snapshot(world, table) == before

    def test_request_id_required(self, world):
        caller = convo(world)
        card = propose(caller, op_params("insert", world.t["write_shared"]["id"]))
        assert exec_error(card, caller, request_id=None).startswith("invalid_args: ")

    def test_gate_closed_at_approve(self, world):
        table = world.t["write_shared"]
        caller = convo(world)
        card = propose(caller, op_params("insert", table["id"]))
        world.fg.set_feature_enabled(world.fg.FEATURE_TABLES, False)
        assert exec_error(card, caller) == f"tables_disabled: {tables_disabled_message()}"

    def test_recipient_approval_credit(self, world):
        from db import action_request_store, conversation_store

        table = world.t["write_shared"]
        caller = convo(world, "bob")
        _run(conversation_store.create_conversation(
            user_id=uid(world, "bob"), conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc),
        ))
        card = propose(caller, op_params("update", table["id"]))
        request_id = _run(action_request_store.create_action_request(
            uid(world, "bob"), caller.conversation_id, "write_table_rows", card, "r",
        ))["id"]
        execute(card, caller, request_id=request_id)
        change = latest_change(world, table)
        assert change["actor_user_id"] == uid(world, "bob")
        assert change["source"] == f"action_request:{request_id}"
        rid = int(change["source"].split(":", 1)[1])
        assert _run(action_request_store.get_action_request_owners([rid])) == {
            request_id: uid(world, "bob"),
        }

    def test_share_revoked_before_approve(self, world):
        table = world.t["write_shared"]
        caller = convo(world, "bob")
        card = propose(caller, op_params("insert", table["id"]))
        share = next(s for s in table["shares"] if s["user_id"] == uid(world, "bob"))
        _run(world.store.remove_share(table["id"], share["id"]))
        before = _snapshot(world, table)
        assert exec_error(card, caller) == (
            f"table_not_found: {table_not_found_message(table['id'])}"
        )
        assert _snapshot(world, table) == before

    def test_share_downgraded_before_approve(self, world):
        table = world.t["write_shared"]
        caller = convo(world, "bob")
        card = propose(caller, op_params("insert", table["id"]))
        share = next(s for s in table["shares"] if s["user_id"] == uid(world, "bob"))
        _run(world.store.remove_share(table["id"], share["id"]))
        _run(world.store.add_share(table["id"], uid(world, "bob"), "read"))
        assert exec_error(card, caller) == f"denied: {DENY_READ_ONLY_SHARE}"

    @pytest.mark.parametrize("key", ["open", "open_write"])
    def test_public_table_never_written_from_private_conversation(self, world, key):
        # Invariant 1 at approve time: a card forged around the pre-card
        # still re-resolves with the private proposal context.
        table = world.t[key]
        card = {**handler().validate_params(op_params("insert", table["id"])),
                "schema_version": table["schema_version"], "rows_digest": "0" * 64}
        before = _snapshot(world, table)
        for caller in (convo(world, project=world.private_project), convo(world)):
            assert exec_error(card, caller) == f"denied: {DENY_PUBLIC_TABLE_FROM_PRIVATE}"
        assert exec_error(card, convo(world, "bob")) == (
            f"table_not_found: {table_not_found_message(table['id'])}"
        )
        assert _snapshot(world, table) == before


# ---------------------------------------------------------------------------
# 5. Preview, labels, registration
# ---------------------------------------------------------------------------


def _preview(p):
    return _run(handler().render_preview(p))


class TestPreviewAndLabels:
    def test_update_fields(self, world):
        out = propose(convo(world), op_params("update", world.t["write_shared"]["id"]))
        fields = _preview(out)
        assert [f["key"] for f in fields] == [
            "Table", "Mode", "Scope", "Shares", "Operation", "Rows",
        ]
        assert fields[0] == {"key": "Table", "value": "Write shared"}
        assert fields[4] == {"key": "Operation", "value": "Update"}
        rows = fields[5]
        assert (rows["value"], rows["type"]) == ("Update 1 row", "table_rows_diff")
        assert rows["rows_diff"] == out["rows_diff"]
        assert all(isinstance(f["value"], str) for f in fields)

    def test_upsert_and_filter_fields(self, world):
        tid = world.t["write_shared"]["id"]
        fields = {f["key"]: f for f in _preview(propose(convo(world), op_params("upsert", tid)))}
        assert fields["Operation"]["value"] == "Upsert on SKU"
        assert fields["Rows"]["value"] == "Upsert 2 rows (1 inserted, 1 updated)"
        f = {"column": "Price", "op": "gt", "value": "0"}
        fields = {x["key"]: x for x in _preview(propose(
            convo(world), {"table_id": tid, "operation": "delete", "filter": f},
        ))}
        assert fields["Operation"]["value"] == "Delete"
        assert fields["Rows"]["value"] == "Delete 2 rows"
        assert fields["Filter"]["value"] == '{"column":"Price","op":"gt","value":"0"}'

    def test_filter_value_clipped(self):
        f = {"or": [{"column": "Name", "op": "eq", "value": "x" * 50}] * 10}
        fields = {x["key"]: x for x in _preview(
            {"table_id": "t", "operation": "delete", "filter": f},
        )}
        value = fields["Filter"]["value"]
        assert len(value) == 203 and value.endswith("...")

    def test_insert_value(self, world):
        tid = world.t["write_shared"]["id"]
        fields = {f["key"]: f for f in _preview(propose(convo(world), op_params("insert", tid)))}
        assert fields["Operation"]["value"] == "Insert"
        assert fields["Rows"]["value"] == "Insert 1 row"

    @pytest.mark.parametrize("p", [
        {}, {"operation": 5}, {"operation": "upsert", "upsert_split": "x"},
        {"rows_diff": "junk", "row_counts": {"inserted": "a"}},
        {"operation": "delete", "filter": object()},
    ])
    def test_odd_params_never_raise(self, p):
        fields = _preview(p)
        assert fields and all(isinstance(f["value"], str) for f in fields)

    @pytest.mark.parametrize("p, expected", [
        ({"operation": "insert", "table_title": "T",
          "row_counts": {"inserted": 3, "updated": 0, "deleted": 0}}, "Insert 3 rows in T"),
        ({"operation": "upsert", "table_title": "T", "upsert_split": {"insert": 1, "update": 1},
          "row_counts": {"inserted": 1, "updated": 1, "deleted": 0}}, "Upsert 2 rows in T"),
        ({"operation": "update", "table_id": "abc", "updates": [{"_id": 1}]},
         "Update 1 row in abc"),
        ({"operation": "delete", "table_title": "T", "filter": {}}, "Delete rows in T"),
        ({"operation": "delete", "row_ids": [1, 2]}, "Delete 2 rows"),
        ({}, "Write rows"),
        ({"operation": ["x"], "table_title": 5}, "Write rows"),
    ])
    def test_summary_snippet(self, p, expected):
        assert handler().summary_snippet(p) == expected

    def test_summary_snippet_non_dict(self):
        assert handler().summary_snippet(None) == ""

    def test_labels_and_registration(self):
        from chat.action_request_types import get_handler
        from chat.action_request_types.write_table_rows import WriteTableRowsHandler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        from db.models import ActionRequestType

        h = handler()
        assert (h.display_name, h.approve_label, h.resolved_label) == (
            "Write Table Rows", "Write", "Rows written",
        )
        assert isinstance(get_handler("write_table_rows"), WriteTableRowsHandler)
        assert get_handler("write_table_rows").type_name == ActionRequestType.WRITE_TABLE_ROWS
        assert "write_table_rows" in ACTION_REQUEST_TYPE_ENUM

    def test_injected_keys_constant(self):
        from chat.action_request_types.write_table_rows import SERVER_INJECTED_KEYS

        assert SERVER_INJECTED_KEYS == frozenset(INJECTED)


# ---------------------------------------------------------------------------
# 6. End to end: create_action_request arm -> card -> resolve route
# ---------------------------------------------------------------------------


@pytest.fixture()
def ar_world(world, monkeypatch):
    import db.action_request_store as action_request_store
    import db.tool_wait_handle_store as tool_wait_handle_store
    from chat.storage import ChatStorage
    from chat.wait_handles import resume as wait_resume

    monkeypatch.setattr(
        tool_wait_handle_store, "AsyncSessionLocal", world.store.AsyncSessionLocal,
    )
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )
    monkeypatch.setattr(wait_resume, "maybe_kick_resume", lambda *a, **k: None)
    world.action_request_store = action_request_store
    return world


def _arm(caller, p):
    from chat.gemini_api.turn_tools import TurnState, _handle_create_action_request
    from tests.test_turn_tools_registry import _call, _make_ctx

    ctx, _events = _make_ctx(
        user=caller.user, conversation_id=caller.conversation_id,
        project_id=caller.project_id,
    )
    call = _call("create_action_request", {
        "request_type": "write_table_rows", "params": p, "reasoning": "rows",
    })
    return _run(_handle_create_action_request(ctx, call, TurnState()))


class _Req:
    app = None


def _open_card(world, caller, p):
    from chat.gemini_api.turn_tools import SuspendForActionRequest
    from db import conversation_store

    _run(conversation_store.create_conversation(
        user_id=caller.user["id"], conversation_id=caller.conversation_id,
        created_at=datetime.now(timezone.utc), project_id=caller.project_id,
    ))
    with pytest.raises(SuspendForActionRequest) as suspended:
        _arm(caller, p)
    return suspended.value.request_id


def _resolve(caller, request_id):
    from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request

    return _run(resolve_user_action_request(
        request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
    ))


class TestEndToEnd:
    def test_arm_refusals(self, world):
        assert json.loads(_arm(convo(world), op_params("insert", world.t["mine"]["id"]))) == {
            "error": "Invalid parameters: use_tool_directly: This table is writable "
                     "directly; call insert_rows instead of proposing an action request.",
        }
        got = json.loads(_arm(convo(world, "bob"), op_params("insert", world.t["read_shared"]["id"])))
        assert got == {"error": f"Invalid parameters: denied: {DENY_READ_ONLY_SHARE}"}

    def test_recipient_card_then_approve(self, ar_world):
        table = ar_world.t["write_shared"]
        caller = convo(ar_world, "bob")
        # A model echoing a forged diff gets the server's own.
        request_id = _open_card(ar_world, caller, {
            **op_params("upsert", table["id"]), "rows_diff": {"rows": []},
            "schema_version": 99,
        })
        row = _run(ar_world.action_request_store.get_action_request(
            caller.user["id"], request_id,
        ))
        assert row["request_type"] == "write_table_rows" and row["status"] == "open"
        assert row["params"]["schema_version"] == table["schema_version"]
        assert row["params"]["rows_diff"]["total"] == 2
        assert not has_column_key(row["params"])
        n_changes = len(changes(ar_world, table))

        resolved = _resolve(caller, request_id)
        assert resolved["status"] == "executed"
        assert resolved["resolved_label"] == "Rows written"
        assert resolved["summary_snippet"] == "Upsert 2 rows in Write shared"
        assert (resolved["result"]["inserted"], resolved["result"]["updated"]) == (1, 1)
        assert len(changes(ar_world, table)) == n_changes + 1
        change = latest_change(ar_world, table)
        assert change["source"] == f"action_request:{request_id}"
        assert change["actor_user_id"] == uid(ar_world, "bob")
        assert _run(ar_world.action_request_store.get_action_request_owners([request_id])) == {
            request_id: uid(ar_world, "bob"),
        }

    def test_rows_changed_keeps_card_open(self, ar_world):
        from fastapi import HTTPException

        table = ar_world.t["write_shared"]
        caller = convo(ar_world)
        request_id = _open_card(ar_world, caller, op_params("update", table["id"]))
        ui_write(ar_world, table, "update", {"updates": [{"_id": 1, "Price": 42}]})
        before = _snapshot(ar_world, table)
        with pytest.raises(HTTPException) as exc:
            _resolve(caller, request_id)
        assert exc.value.status_code == 500
        message = exc.value.detail["message"]
        assert message.startswith("Failed to execute request: rows_changed: ")
        assert '"Price": "42"' in message
        row = _run(ar_world.action_request_store.get_action_request(
            caller.user["id"], request_id,
        ))
        assert row["status"] == "open"
        assert _snapshot(ar_world, table) == before
