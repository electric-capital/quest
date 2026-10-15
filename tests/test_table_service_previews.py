"""Quest Tables service previews and approve-time pins for the action
requests (chat/tables/service.py, chat/tables/storage.py):

1. Dry-run row images per op (``preview_row_write``'s ``before_rows`` /
   ``after_rows``), including the upsert split and a filter delete.
2. ``rows_digest``: stable over unchanged rows, different after a value
   change, a vanished row or a changed set of affected rows.
3. ``apply_row_write(expected_schema_version=, expected_rows_digest=)``:
   StaleSchema, RowsChanged (rows mutated between preview and apply,
   through a UI caller), live-schema validation errors unchanged, and the
   success path writing exactly one change.
4. ``preview_schema_change``: payload incl. ``stats.cells_rewritten`` and
   ``stats.cells_changed`` for enum renames, StaleSchema, TableDenied, rows_violate, duplicate_title,
   and nothing written.
"""

import asyncio

import pytest

from chat.tables import service, storage
from chat.tables.schema import load_schema
from chat.tables.service import (
    RowsChanged,
    StaleSchema,
    TableDenied,
    TableError,
    rows_digest,
)
from tests.test_table_service import (  # noqa: F401 - env is a fixture
    _run,
    changes,
    convo,
    env,
    fetch,
    make_table,
    table_dir,
    ui,
)

ENUM_COLUMNS = [
    {"name": "Name", "type": "text", "required": True},
    {"name": "Status", "type": "enum", "options": ["Open", "Done", "Hold"]},
    {"name": "Tags", "type": "enum", "options": ["a", "b", "c"], "multiple": True},
    {"name": "Price", "type": "number"},
]


def seed(env, table, rows=None):
    """Insert rows as the owner's UI (ids 1.. in order)."""
    rows = rows or [{"Name": "a", "SKU": "x"}, {"Name": "b", "SKU": "y"}]
    _run(service.apply_row_write(ui(env), table["id"], "insert", {"rows": rows}))
    env.published.clear()


def preview(env, table, op, params, caller=None):
    return _run(service.preview_row_write(caller or convo(env), table["id"], op, params))[2]


def snapshot(env, table):
    return (
        storage.dump_rows(table_dir(env, table), load_schema(fetch(env, table)["schema"])),
        storage.list_changes(table_dir(env, table)),
        fetch(env, table)["schema_version"],
    )


def make_enum_table(env, title="Tasks"):
    table = _run(service.create_table(ui(env), title=title, columns=ENUM_COLUMNS))
    _run(service.apply_row_write(ui(env), table["id"], "insert", {"rows": [
        {"Name": "a", "Status": "Open", "Tags": ["a", "b"]},
        {"Name": "b", "Status": "Open", "Tags": ["b"]},
        {"Name": "c", "Status": "Done", "Tags": ["c"]},
        {"Name": "d"},
    ]}))
    env.published.clear()
    return table


def key_of(table, name):
    return next(c["key"] for c in table["schema"] if c["name"] == name)


# ---------------------------------------------------------------------------
# Row images
# ---------------------------------------------------------------------------


class TestRowImages:
    def test_insert(self, env):
        table = make_table(env)
        seed(env, table)
        before = snapshot(env, table)
        out = preview(env, table, "insert", {"rows": [{"Name": "c", "Price": 2}]})
        assert out["before_rows"] == []
        assert out["after_rows"] == [{"_id": 3, "Name": "c", "Price": "2", "SKU": None}]
        assert out["inserted_ids"] == [3] and out["change_id"] is None
        assert out["rows_digest"] == rows_digest([])
        assert snapshot(env, table) == before

    def test_update(self, env):
        table = make_table(env)
        seed(env, table)
        out = preview(env, table, "update", {"updates": [{"_id": 1, "Price": 5}]})
        assert out["before_rows"] == [{"_id": 1, "Name": "a", "Price": None, "SKU": "x"}]
        assert out["after_rows"] == [{"_id": 1, "Name": "a", "Price": "5", "SKU": "x"}]
        for row in out["before_rows"] + out["after_rows"]:
            assert "_updated_at" not in row and "_created_at" not in row

    def test_upsert_splits_matched_and_inserted(self, env):
        table = make_table(env)
        seed(env, table)
        out = preview(env, table, "upsert", {"key_column": "SKU", "rows": [
            {"SKU": "x", "Price": 7}, {"SKU": "z", "Name": "z"},
        ]})
        assert out["before_rows"] == [{"_id": 1, "Name": "a", "Price": None, "SKU": "x"}]
        assert out["after_rows"] == [
            {"_id": 1, "Name": "a", "Price": "7", "SKU": "x"},
            {"_id": 3, "Name": "z", "Price": None, "SKU": "z"},
        ]
        assert out["updated_ids"] == [1] and out["inserted_ids"] == [3]

    def test_filter_delete(self, env):
        table = make_table(env)
        seed(env, table)
        before = snapshot(env, table)
        out = preview(env, table, "delete",
                      {"filter": {"column": "Name", "op": "eq", "value": "b"}})
        assert out["before_rows"] == [{"_id": 2, "Name": "b", "Price": None, "SKU": "y"}]
        assert out["after_rows"] == []
        assert out["deleted"] == 1
        assert snapshot(env, table) == before

    def test_filter_delete_matching_nothing(self, env):
        table = make_table(env)
        seed(env, table)
        out = preview(env, table, "delete",
                      {"filter": {"column": "Name", "op": "eq", "value": "zzz"}})
        assert out["before_rows"] == [] and out["after_rows"] == []
        assert out["rows_digest"] == rows_digest([])

    def test_enum_values_are_logical(self, env):
        table = make_enum_table(env)
        out = preview(env, table, "update", {"updates": [{"_id": 1, "Tags": ["c"]}]})
        assert out["before_rows"][0]["Tags"] == ["a", "b"]
        assert out["after_rows"][0]["Tags"] == ["c"]


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


class TestDigest:
    def test_ignores_timestamps_and_order(self):
        a = {"_id": 1, "_created_at": "t1", "_updated_at": "t1", "c_00000001": "x"}
        b = {"_id": 2, "_created_at": "t1", "_updated_at": "t1", "c_00000001": None}
        same = {**a, "_updated_at": "t9"}
        assert rows_digest([a, b]) == rows_digest([b, same])
        assert rows_digest([a, b]) != rows_digest([a])
        assert rows_digest([a]) != rows_digest([{**a, "c_00000001": "y"}])

    def test_stable_over_unchanged_rows(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        first = preview(env, table, "update", params)["rows_digest"]
        # An unrelated row's change leaves it alone.
        _run(service.apply_row_write(ui(env), table["id"], "update",
                                     {"updates": [{"_id": 2, "Price": 1}]}))
        assert preview(env, table, "update", params)["rows_digest"] == first

    def test_changes_on_value_change(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        first = preview(env, table, "update", params)["rows_digest"]
        _run(service.apply_row_write(ui(env), table["id"], "update",
                                     {"updates": [{"_id": 1, "Name": "A"}]}))
        assert preview(env, table, "update", params)["rows_digest"] != first

    def test_changes_when_an_affected_row_vanishes(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"filter": {"column": "Name", "op": "ne", "value": "q"}}
        first = preview(env, table, "delete", params)["rows_digest"]
        _run(service.apply_row_write(ui(env), table["id"], "delete", {"row_ids": [2]}))
        assert preview(env, table, "delete", params)["rows_digest"] != first

    def test_changes_when_the_match_set_changes(self, env):
        table = make_table(env)
        seed(env, table)
        delete = {"filter": {"column": "Name", "op": "eq", "value": "a"}}
        upsert = {"key_column": "SKU", "rows": [{"SKU": "z", "Name": "z"}]}
        first_delete = preview(env, table, "delete", delete)["rows_digest"]
        first_upsert = preview(env, table, "upsert", upsert)["rows_digest"]
        _run(service.apply_row_write(ui(env), table["id"], "insert",
                                     {"rows": [{"Name": "a", "SKU": "z"}]}))
        assert preview(env, table, "delete", delete)["rows_digest"] != first_delete
        assert preview(env, table, "upsert", upsert)["rows_digest"] != first_upsert


# ---------------------------------------------------------------------------
# apply_row_write pins
# ---------------------------------------------------------------------------


def execute(env, table, op, params, **pins):
    """The write_table_rows executor's call: proposal-context caller,
    bypass_approval, an action_request source; a digest pin comes with the
    version pin (version 1 unless given)."""
    if "expected_rows_digest" in pins:
        pins.setdefault("expected_schema_version", 1)
    return _run(execute_async(env, table, op, params, **pins))


def execute_async(env, table, op, params, **pins):
    return service.apply_row_write(
        convo(env), table["id"], op, params, bypass_approval=True,
        write_source="action_request:9", **pins,
    )


class TestApplyPins:
    def test_success_writes_exactly_one_change(self, env):
        table = make_table(env, shares=[("bob", "write")])
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        out = preview(env, table, "update", params)
        seq = len(changes(env, table))
        result = execute(env, table, "update", params,
                         expected_schema_version=1,
                         expected_rows_digest=out["rows_digest"])
        assert result["updated_ids"] == [1]
        entries = changes(env, table)
        assert len(entries) == seq + 1
        assert entries[-1]["source"] == "action_request:9"
        rows = storage.dump_rows(table_dir(env, table), load_schema(table["schema"]))
        assert rows[0][key_of(table, "Price")] == "5"

    def test_stale_schema(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        out = preview(env, table, "update", params)
        _run(service.apply_schema_change(
            ui(env), table["id"],
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"}}],
            expected_schema_version=1,
        ))
        before = snapshot(env, table)
        with pytest.raises(StaleSchema) as exc:
            execute(env, table, "update", params, expected_schema_version=1,
                    expected_rows_digest=out["rows_digest"])
        assert exc.value.code == "stale_schema"
        assert exc.value.current["schema_version"] == 2
        assert snapshot(env, table) == before

    def test_rows_changed_on_value_change(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        out = preview(env, table, "update", params)
        _run(service.apply_row_write(ui(env), table["id"], "update",
                                     {"updates": [{"_id": 1, "Name": "Changed"}]}))
        before = snapshot(env, table)
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "update", params, expected_schema_version=1,
                    expected_rows_digest=out["rows_digest"])
        assert exc.value.code == "rows_changed"
        assert isinstance(exc.value, TableError)
        assert exc.value.current_rows == [
            {"_id": 1, "Name": "Changed", "Price": None, "SKU": "x"},
        ]
        assert 'Row 1 now: {"Name": "Changed", "Price": null, "SKU": "x"}' in str(exc.value)
        assert "c_" not in str(exc.value)
        assert snapshot(env, table) == before

    def test_rows_changed_when_a_named_row_vanished(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}, {"_id": 2, "Price": 6}]}
        out = preview(env, table, "update", params)
        _run(service.apply_row_write(ui(env), table["id"], "delete", {"row_ids": [2]}))
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "update", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.vanished_ids == [2]
        assert "Row 2 no longer exists" in str(exc.value)
        assert [r["_id"] for r in exc.value.current_rows] == [1]

    def test_rows_changed_when_filter_matches_another_row(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"filter": {"column": "Name", "op": "eq", "value": "a"}}
        out = preview(env, table, "delete", params)
        _run(service.apply_row_write(ui(env), table["id"], "insert",
                                     {"rows": [{"Name": "a"}]}))
        before = snapshot(env, table)
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "delete", params, expected_rows_digest=out["rows_digest"])
        assert [r["_id"] for r in exc.value.current_rows] == [1, 3]
        assert snapshot(env, table) == before

    def test_rows_changed_when_upsert_key_now_matches(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"key_column": "SKU", "rows": [{"SKU": "z", "Name": "z"}]}
        out = preview(env, table, "upsert", params)
        _run(service.apply_row_write(ui(env), table["id"], "insert",
                                     {"rows": [{"Name": "q", "SKU": "z"}]}))
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "upsert", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.current_rows == [{"_id": 3, "Name": "q", "Price": None, "SKU": "z"}]

    def test_message_lists_at_most_ten_rows(self, env):
        table = make_table(env)
        seed(env, table, [{"Name": f"r{i}"} for i in range(12)])
        params = {"filter": {"column": "Name", "op": "ne", "value": "q"}}
        out = preview(env, table, "delete", params)
        _run(service.apply_row_write(ui(env), table["id"], "insert",
                                     {"rows": [{"Name": "new"}]}))
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "delete", params, expected_rows_digest=out["rows_digest"])
        assert len(exc.value.current_rows) == 13
        assert str(exc.value).count("now: ") == 10
        assert "and 3 more" in str(exc.value)

    def test_live_validation_error_unchanged(self, env):
        """A unique clash that appeared after the preview: the insert's
        digest (no before rows) still matches, so the real write's own
        validation error surfaces unchanged."""
        table = make_table(env)
        seed(env, table)
        params = {"rows": [{"Name": "e", "SKU": "z"}]}
        out = preview(env, table, "insert", params)
        _run(service.apply_row_write(ui(env), table["id"], "insert",
                                     {"rows": [{"Name": "f", "SKU": "z"}]}))
        with pytest.raises(storage.InvalidRowsError) as exc:
            execute(env, table, "insert", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.problems[0]["code"] == "duplicate_value"

    def test_schema_change_between_is_stale_schema(self, env):
        """An enum option removed by the UI after the preview: the version
        pin refuses before any row validation."""
        table = make_enum_table(env)
        params = {"rows": [{"Name": "e", "Status": "Hold"}]}
        out = preview(env, table, "insert", params)
        _run(service.apply_schema_change(
            ui(env), table["id"],
            [{"op": "remove_enum_option", "name": "Status", "option": "Hold"}],
            expected_schema_version=1,
        ))
        with pytest.raises(StaleSchema):
            execute(env, table, "insert", params, expected_rows_digest=out["rows_digest"])

    def test_digest_needs_schema_version(self, env):
        table = make_table(env)
        out = preview(env, table, "insert", {"rows": [{"Name": "a"}]})
        with pytest.raises(TableError) as exc:
            _run(execute_async(env, table, "insert", {"rows": [{"Name": "a"}]},
                               expected_rows_digest=out["rows_digest"]))
        assert exc.value.code == "invalid_args"
        assert "expected_schema_version" in str(exc.value)
        assert len(changes(env, table)) == 1

    @pytest.mark.parametrize("op,params", [
        ("update", {"updates": [{"_id": 2, "Price": 6}]}),
        ("delete", {"row_ids": [2]}),
    ])
    def test_vanished_row_without_digest_is_unknown_row(self, env, op, params):
        table = make_table(env)
        seed(env, table)
        _run(service.apply_row_write(ui(env), table["id"], "delete", {"row_ids": [2]}))
        with pytest.raises(storage.InvalidRowsError) as exc:
            execute(env, table, op, params)
        assert [p["code"] for p in exc.value.problems] == ["unknown_row"]

    def test_rows_changed_when_a_deleted_row_vanished(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"row_ids": [1, 2]}
        out = preview(env, table, "delete", params)
        _run(service.apply_row_write(ui(env), table["id"], "delete", {"row_ids": [1]}))
        before = snapshot(env, table)
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "delete", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.op == "delete" and exc.value.vanished_ids == [1]
        assert "Row 1 no longer exists" in str(exc.value)
        assert 'Row 2 now: {"Name": "b"' in str(exc.value)
        assert snapshot(env, table) == before

    def test_message_when_upsert_matches_nothing(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"key_column": "SKU", "rows": [{"SKU": "x", "Name": "a", "Price": 1}]}
        out = preview(env, table, "upsert", params)
        _run(service.apply_row_write(ui(env), table["id"], "delete", {"row_ids": [1]}))
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "upsert", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.op == "upsert" and exc.value.current_rows == []
        assert ("The upsert now matches no stored rows (it would insert them "
                "instead).") in str(exc.value)

    def test_message_when_filter_matches_nothing(self, env):
        table = make_table(env)
        seed(env, table)
        params = {"filter": {"column": "Name", "op": "eq", "value": "a"}}
        out = preview(env, table, "delete", params)
        _run(service.apply_row_write(ui(env), table["id"], "update",
                                     {"updates": [{"_id": 1, "Name": "A"}]}))
        with pytest.raises(RowsChanged) as exc:
            execute(env, table, "delete", params, expected_rows_digest=out["rows_digest"])
        assert exc.value.by_filter is True
        assert "The filter now matches no rows." in str(exc.value)

    def test_check_and_write_share_one_lock_hold(self, env, monkeypatch):
        """A UI update started right after the digest check queues on the
        lock and lands after the card's change, never between."""
        table = make_table(env)
        seed(env, table)
        params = {"updates": [{"_id": 1, "Price": 5}]}
        out = preview(env, table, "update", params)
        original = service._check_rows_digest
        state = {}

        async def check_then_race(*args, **kwargs):
            await original(*args, **kwargs)
            state["ui"] = asyncio.ensure_future(service.apply_row_write(
                ui(env), table["id"], "update",
                {"updates": [{"_id": 1, "Price": 9, "Name": "UI"}]},
            ))
            await asyncio.sleep(0.2)  # let it run up to the lock
            state["ui_done_before_write"] = state["ui"].done()

        monkeypatch.setattr(service, "_check_rows_digest", check_then_race)

        async def scenario():
            card = await execute_async(
                env, table, "update", params, expected_schema_version=1,
                expected_rows_digest=out["rows_digest"],
            )
            return card, await state["ui"]

        card, ui_result = _run(scenario())
        assert state["ui_done_before_write"] is False
        assert ui_result["change_id"] == card["change_id"] + 1
        entries = changes(env, table)
        assert [e["source"].split(":")[0] for e in entries[-2:]] == ["action_request", "ui"]
        rows = storage.dump_rows(table_dir(env, table), load_schema(table["schema"]))
        assert rows[0][key_of(table, "Price")] == "9"

    def test_bad_pin_types(self, env):
        table = make_table(env)
        with pytest.raises(TableError) as exc:
            execute(env, table, "insert", {"rows": [{"Name": "a"}]},
                    expected_schema_version=True)
        assert exc.value.code == "invalid_args"
        with pytest.raises(TableError) as exc:
            execute(env, table, "insert", {"rows": [{"Name": "a"}]},
                    expected_rows_digest=5)
        assert exc.value.code == "invalid_args"


# ---------------------------------------------------------------------------
# preview_schema_change
# ---------------------------------------------------------------------------


class TestPreviewSchemaChange:
    def test_payload_and_nothing_written(self, env):
        table = make_enum_table(env)
        before = snapshot(env, table)
        published = list(env.published)
        out = _run(service.preview_schema_change(
            convo(env), table["id"],
            [{"op": "rename_enum_option", "name": "Status", "option": "Open",
              "new_option": "Todo"},
             {"op": "rename_enum_option", "name": "Tags", "option": "b",
              "new_option": "B"},
             {"op": "rename_enum_option", "name": "Status", "option": "Todo",
              "new_option": "Next"},
             {"op": "drop_column", "name": "Price"}],
            expected_schema_version=1,
        ))
        assert out["table"]["id"] == table["id"]
        assert out["access"].schema == "approval"
        assert out["row_count"] == 4
        assert out["new_title"] is None
        assert [c.name for c in out["columns_before"]] == ["Name", "Status", "Tags", "Price"]
        assert [c.name for c in out["columns_after"]] == ["Name", "Status", "Tags"]
        assert out["columns_after"] == list(out["plan"].columns)
        status = next(c for c in out["columns_after"] if c.name == "Status")
        assert list(status.options) == ["Next", "Done", "Hold"]
        diff = out["schema_diff"]
        assert {e["status"] for e in diff["columns"]} >= {"dropped", "changed"}
        assert out["stats"] == {
            # Status: 2 cells Open -> Todo, then the same 2 Todo -> Next.
            "cells_rewritten": {key_of(table, "Status"): 4, key_of(table, "Tags"): 2},
            # ... but only 2 distinct cells of each column changed.
            "cells_changed": {key_of(table, "Status"): 2, key_of(table, "Tags"): 2},
            "rows_backfilled": 0,
            "dropped_values": {key_of(table, "Price"): 0},
        }
        assert snapshot(env, table) == before
        assert env.published == published

    def test_stats_for_a_renamed_then_dropped_column(self, env):
        table = make_enum_table(env)
        out = _run(service.preview_schema_change(
            ui(env), table["id"],
            [{"op": "rename_enum_option", "name": "Status", "option": "Open",
              "new_option": "Todo"},
             {"op": "drop_column", "name": "Status"}],
            expected_schema_version=1,
        ))
        # The rewrite is not counted for a column that is gone; its dropped
        # values are its 3 non-null cells (Open, Open, Done).
        assert out["stats"] == {
            "cells_rewritten": {},
            "cells_changed": {},
            "rows_backfilled": 0,
            "dropped_values": {key_of(table, "Status"): 3},
        }

    def test_rename_table(self, env):
        table = make_enum_table(env)
        out = _run(service.preview_schema_change(
            ui(env), table["id"], [{"op": "rename_table", "title": "Chores"}],
            expected_schema_version=1,
        ))
        assert out["new_title"] == "Chores"
        assert fetch(env, table)["title"] == "Tasks"

    def test_duplicate_title(self, env):
        table = make_enum_table(env)
        make_table(env, "Other")
        before = snapshot(env, table)
        with pytest.raises(TableError) as exc:
            _run(service.preview_schema_change(
                ui(env), table["id"], [{"op": "rename_table", "title": "other"}],
                expected_schema_version=1,
            ))
        assert exc.value.code == "duplicate_title"
        assert "already exists" in str(exc.value)
        assert snapshot(env, table) == before

    def test_stale_schema(self, env):
        table = make_enum_table(env)
        with pytest.raises(StaleSchema) as exc:
            _run(service.preview_schema_change(
                ui(env), table["id"], [{"op": "rename_table", "title": "X"}],
                expected_schema_version=2,
            ))
        assert exc.value.current["schema_version"] == 1

    def test_denied_for_non_approval_verdict(self, env):
        table = make_table(env, shares=[("bob", "write")])
        with pytest.raises(TableDenied):
            _run(service.preview_schema_change(
                ui(env, "bob"), table["id"], [{"op": "rename_table", "title": "X"}],
                expected_schema_version=1,
            ))
        public = make_table(env, "Pub", project_id=env.public_project)
        with pytest.raises(TableDenied):
            _run(service.preview_schema_change(
                convo(env, project=env.public_project, public=True), public["id"],
                [{"op": "rename_table", "title": "X"}], expected_schema_version=1,
            ))

    def test_rows_violate(self, env):
        table = make_enum_table(env)
        before = snapshot(env, table)
        with pytest.raises(storage.RowsViolateError) as exc:
            _run(service.preview_schema_change(
                ui(env), table["id"],
                [{"op": "set_required", "name": "Status", "required": True}],
                expected_schema_version=1,
            ))
        assert exc.value.code == "rows_violate"
        assert exc.value.row_ids == [4]
        assert snapshot(env, table) == before
