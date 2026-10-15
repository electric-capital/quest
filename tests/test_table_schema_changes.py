"""Quest Tables schema changes on live tables (storage.apply_schema_change)."""

from datetime import datetime, timezone
import json
import sqlite3

import pytest

import chat.tables.storage as storage_mod
from chat.tables.revert import restore_to
from chat.tables.schema import dump_schema, parse_columns
from chat.tables.schema_ops import SchemaOperationError, apply_schema_operations
from chat.tables.storage import (
    TABLE_DB_NAME,
    ChangeTooLargeError,
    RowsViolateError,
    SchemaChangeResult,
    SchemaMismatchError,
    TableStorageError,
    apply_schema_change,
    check_schema_violations,
    create_table_db,
    dump_rows,
    get_change,
    insert_rows,
    read_meta,
    unique_index_name,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
SOURCE = "action_request:7"
TITLE = "Inventory"

K_NAME = "c_00000001"
K_PRICE = "c_00000002"
K_SITE = "c_00000003"
K_DONE = "c_00000004"
K_STATUS = "c_00000005"
K_TAGS = "c_00000006"
K_SKU = "c_00000007"
RESERVED = ["_id", "_created_at", "_updated_at"]
BASE_DDL = RESERVED + [K_NAME, K_PRICE, K_SITE, K_DONE, K_STATUS, K_TAGS, K_SKU]


def make_schema():
    return parse_columns(
        [
            {"key": K_NAME, "name": "Name", "type": "text", "required": True},
            {"key": K_PRICE, "name": "Price", "type": "number", "precision": 2},
            {"key": K_SITE, "name": "Site", "type": "url"},
            {"key": K_DONE, "name": "Done", "type": "boolean", "default": False},
            {
                "key": K_STATUS,
                "name": "Status",
                "type": "enum",
                "options": ["Open", "Closed", "Hold"],
                "default": "Open",
            },
            {
                "key": K_TAGS,
                "name": "Tags",
                "type": "enum",
                "options": ["a", "b", "c", "d"],
                "multiple": True,
            },
            {"key": K_SKU, "name": "SKU", "type": "text", "unique": True},
        ],
        allow_key=True,
    )


ROWS = [
    {
        "Name": "A",
        "Price": "1.5",
        "Site": "https://a.example",
        "Done": True,
        "Status": "Open",
        "Tags": ["a", "b"],
        "SKU": "s1",
    },
    {"Name": "B", "Status": "Closed", "Tags": ["c"], "SKU": "s2"},
    {"Name": "C", "Status": None, "Tags": ["b"]},
    {"Name": "D", "Price": "2", "Tags": None, "SKU": None},
]


@pytest.fixture
def schema():
    return make_schema()


@pytest.fixture
def table_dir(tmp_path, schema):
    path = tmp_path / "tables" / "t1"
    create_table_db(
        path,
        table_id="t1",
        title=TITLE,
        schema=schema,
        source="ui:1",
        actor_user_id=1,
        now=NOW,
    )
    insert_rows(
        path, schema, schema_version=1, source="ui:1", actor_user_id=1,
        rows=ROWS, now=NOW,
    )
    return path


def plan_for(schema, ops):
    return apply_schema_operations(schema, ops, table_empty=False)


def apply(table_dir, schema, ops, *, version=1, title=TITLE, now=LATER):
    plan = plan_for(schema, ops)
    result = apply_schema_change(
        table_dir,
        schema,
        plan,
        schema_version=version,
        source=SOURCE,
        actor_user_id=3,
        title=title,
        now=now,
    )
    return plan, result


def raw_sql(table_dir, sql, params=()):
    conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def table_info(table_dir):
    return [r[1] for r in raw_sql(table_dir, "PRAGMA table_info(rows)")]


def table_types(table_dir):
    return {r[1]: r[2] for r in raw_sql(table_dir, "PRAGMA table_info(rows)")}


def indexes(table_dir):
    return sorted(
        (r[1], r[2]) for r in raw_sql(table_dir, "PRAGMA index_list(rows)")
    )


def snapshot(table_dir):
    """Everything a refused change must leave byte-identical."""
    return {
        "info": raw_sql(table_dir, "PRAGMA table_info(rows)"),
        "indexes": raw_sql(table_dir, "PRAGMA index_list(rows)"),
        "rows": raw_sql(table_dir, "SELECT * FROM rows ORDER BY _id"),
        "meta": raw_sql(table_dir, "SELECT key, value FROM meta ORDER BY key"),
        "changes": raw_sql(table_dir, "SELECT * FROM changes ORDER BY id"),
        "sql": raw_sql(table_dir, "SELECT name, sql FROM sqlite_master ORDER BY name"),
        "sequence": raw_sql(table_dir, "SELECT name, seq FROM sqlite_sequence ORDER BY name"),
    }


def stored(table_dir, key):
    return [r[0] for r in raw_sql(table_dir, f'SELECT "{key}" FROM rows ORDER BY _id')]


def assert_replays(table_dir):
    """Restore the table to its newest change, which replays the whole log
    forward from change #1 (chat.tables.revert), and check the rebuilt
    file equals the live one: the images hold every rewritten cell."""
    def state():
        return {
            "rows": raw_sql(table_dir, "SELECT * FROM rows ORDER BY _id"),
            "info": raw_sql(table_dir, "PRAGMA table_info(rows)"),
            "indexes": raw_sql(table_dir, "PRAGMA index_list(rows)"),
            "sql": raw_sql(table_dir, "SELECT type, name, sql FROM sqlite_master "
                           "ORDER BY rowid"),
            "meta": raw_sql(table_dir, "SELECT key, value FROM meta "
                            "WHERE key != 'schema_version' ORDER BY key"),
        }

    before = state()
    meta = read_meta(table_dir)
    newest = raw_sql(table_dir, "SELECT MAX(id) FROM changes")[0][0]
    restore_to(table_dir, meta.schema, newest, schema_version=meta.schema_version,
               source="ui:1", actor_user_id=1)
    assert state() == before


def check_common(table_dir, plan, result, *, version=1, title=TITLE, summary=None):
    """meta, result and change entry basics every successful change shares."""
    meta = read_meta(table_dir)
    assert meta.schema == plan.columns
    assert meta.schema_version == version + 1
    assert meta.title == (plan.new_title or title)
    assert isinstance(result, SchemaChangeResult)
    assert result.schema_version == version + 1
    assert result.columns == plan.columns
    assert result.title == meta.title
    assert result.row_count == 4
    change = get_change(table_dir, result.change_id)
    assert change["kind"] == "schema"
    assert change["op"] == "schema"
    assert change["source"] == SOURCE
    assert change["actor_user_id"] == 3
    assert change["schema_before"] == version
    assert change["schema_after"] == version + 1
    assert change["row_ids"] == result.row_ids
    assert change["after"]["operations"] == plan.operations
    assert change["after"]["columns"] == dump_schema(plan.columns)
    assert change["after"]["title"] == meta.title
    assert change["after"]["ddl_columns"] == table_info(table_dir)
    assert set(change["before"]) == {
        "columns", "title", "ddl_columns", "dropped_values", "rewrites"
    }
    assert set(change["after"]) == {
        "columns", "title", "ddl_columns", "unique_indexes", "created_indexes",
        "operations", "backfill",
    }
    assert change["after"]["unique_indexes"] == [
        r[0] for r in raw_sql(
            table_dir,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'rows' "
            "AND sql IS NOT NULL ORDER BY rowid",
        )
    ]
    if summary is not None:
        assert change["summary"] == summary
    return change


def check_before(change, schema, *, ddl=BASE_DDL, title=TITLE):
    assert change["before"]["columns"] == dump_schema(schema)
    assert change["before"]["title"] == title
    assert change["before"]["ddl_columns"] == ddl


# --- Add column ----------------------------------------------------------------


class TestAddColumn:
    def test_nullable_with_after(self, table_dir, schema):
        rows_before = dump_rows(table_dir, schema)
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"},
              "after": "Name"}],
        )
        key = plan.columns[1].key
        assert plan.columns[1].name == "Notes"
        # Physically appended; logically after Name.
        assert table_info(table_dir) == BASE_DDL + [key]
        assert table_types(table_dir)[key] == "TEXT"
        assert [c.name for c in read_meta(table_dir).schema][:2] == ["Name", "Notes"]
        rows = dump_rows(table_dir, plan.columns)
        assert [r[key] for r in rows] == [None] * 4
        # Untouched cells and timestamps stay.
        for old, new in zip(rows_before, rows):
            assert {k: v for k, v in new.items() if k != key} == old
        change = check_common(table_dir, plan, result, summary="Added column Notes")
        check_before(change, schema)
        assert result.row_ids == []
        assert change["before"]["dropped_values"] == {}
        assert change["after"]["backfill"] == {}
        assert change["before"]["rewrites"] == []

    def test_required_text_default_backfills(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Owner", "type": "text", "required": True,
                         "default": "ops"}}],
        )
        key = plan.columns[-1].key
        assert stored(table_dir, key) == ["ops"] * 4
        change = check_common(table_dir, plan, result, summary="Added column Owner")
        assert change["after"]["backfill"] == {
            key: {"value": "ops"}
        }
        assert result.row_ids == [1, 2, 3, 4]
        # Timestamps untouched by a schema change.
        assert [r["_updated_at"] for r in dump_rows(table_dir, plan.columns)] == [
            r["_updated_at"] for r in dump_rows(table_dir, schema)
        ]

    @pytest.mark.parametrize("default,expected", [(False, 0), (True, 1)])
    def test_required_boolean_default(self, table_dir, schema, default, expected):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Flag", "type": "boolean", "required": True,
                         "default": default}}],
        )
        key = plan.columns[-1].key
        assert table_types(table_dir)[key] == "INTEGER"
        assert stored(table_dir, key) == [expected] * 4
        assert [r[key] for r in dump_rows(table_dir, plan.columns)] == [default] * 4
        change = check_common(table_dir, plan, result)
        assert change["after"]["backfill"][key]["value"] == expected

    def test_required_multi_enum_default(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Labels", "type": "enum", "options": ["x", "y", "z"],
                         "multiple": True, "required": True, "default": ["z", "x"]}}],
        )
        key = plan.columns[-1].key
        assert stored(table_dir, key) == ['["x","z"]'] * 4
        assert [r[key] for r in dump_rows(table_dir, plan.columns)] == [["x", "z"]] * 4
        change = check_common(table_dir, plan, result)
        assert change["after"]["backfill"][key] == {"value": '["x","z"]'}
        assert result.row_ids == [1, 2, 3, 4]

    def test_unique_column_on_rows_gets_index(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Code", "type": "text", "unique": True}}],
        )
        key = plan.columns[-1].key
        assert (unique_index_name(key), 1) in indexes(table_dir)
        check_common(table_dir, plan, result)

    def test_required_unique_backfill_refused(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(
                table_dir, schema,
                [{"op": "add_column",
                  "column": {"name": "Code", "type": "text", "unique": True,
                             "required": True, "default": "same"}}],
            )
        assert exc.value.check == "unique"
        assert exc.value.column == "Code"
        assert exc.value.row_ids == [1, 2, 3, 4]
        assert exc.value.total == 4
        assert snapshot(table_dir) == before

    def test_added_then_required_is_checked(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(
                table_dir, schema,
                [{"op": "add_column", "column": {"name": "Notes", "type": "text"}},
                 {"op": "set_required", "name": "Notes", "required": True}],
            )
        assert (exc.value.check, exc.value.column, exc.value.total) == (
            "required", "Notes", 4
        )
        assert snapshot(table_dir) == before


# --- Rename, drop, reorder, metadata-only ops -----------------------------------


class TestRenameColumn:
    def test_no_ddl_change(self, table_dir, schema):
        rows_before = dump_rows(table_dir, schema)
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_column", "name": "Price", "new_name": "Cost"}],
        )
        assert table_info(table_dir) == BASE_DDL
        assert read_meta(table_dir).schema[1].name == "Cost"
        assert dump_rows(table_dir, plan.columns) == rows_before
        change = check_common(
            table_dir, plan, result, summary="Renamed column Price to Cost"
        )
        check_before(change, schema)
        assert result.row_ids == []


class TestDropColumn:
    def test_plain(self, table_dir, schema):
        plan, result = apply(table_dir, schema, [{"op": "drop_column", "name": "Site"}])
        assert table_info(table_dir) == [k for k in BASE_DDL if k != K_SITE]
        change = check_common(table_dir, plan, result, summary="Dropped column Site")
        check_before(change, schema)
        assert change["before"]["dropped_values"] == {
            K_SITE: {"1": "https://a.example"}
        }
        assert result.row_ids == [1]
        rows = dump_rows(table_dir, plan.columns)
        assert all(K_SITE not in r for r in rows)
        assert rows[0][K_NAME] == "A"

    def test_unique_indexed(self, table_dir, schema):
        assert (unique_index_name(K_SKU), 1) in indexes(table_dir)
        plan, result = apply(table_dir, schema, [{"op": "drop_column", "name": "SKU"}])
        assert indexes(table_dir) == []
        assert K_SKU not in table_info(table_dir)
        change = check_common(table_dir, plan, result)
        assert change["before"]["dropped_values"] == {K_SKU: {"1": "s1", "2": "s2"}}
        assert result.row_ids == [1, 2]

    def test_stored_forms_and_nulls(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "drop_column", "name": "Tags"},
             {"op": "drop_column", "name": "Done"},
             {"op": "drop_column", "name": "Price"}],
        )
        change = check_common(table_dir, plan, result, summary="3 schema changes")
        assert change["before"]["dropped_values"] == {
            K_TAGS: {"1": '["a","b"]', "2": '["c"]', "3": '["b"]'},
            K_DONE: {"1": 1, "2": 0, "3": 0, "4": 0},
            K_PRICE: {"1": "1.5", "4": "2"},
        }
        assert result.row_ids == [1, 2, 3, 4]
        assert table_info(table_dir) == RESERVED + [K_NAME, K_SITE, K_STATUS, K_SKU]


class TestMetadataOnly:
    @pytest.mark.parametrize(
        "op,summary,check",
        [
            ({"op": "reorder_columns",
              "names": ["SKU", "Name", "Price", "Site", "Done", "Status", "Tags"]},
             "Reordered columns",
             lambda cols: [c.key for c in cols][:2] == [K_SKU, K_NAME]),
            ({"op": "set_description", "name": "Price", "description": "In USD"},
             "Changed description of Price",
             lambda cols: cols[1].description == "In USD"),
            ({"op": "set_default", "name": "Status", "default": "Hold"},
             "Changed default of Status",
             lambda cols: cols[4].default == "Hold"),
            ({"op": "set_precision", "name": "Price", "precision": 0},
             "Changed precision of Price",
             lambda cols: cols[1].precision == 0),
            ({"op": "add_enum_option", "name": "Status", "option": "New",
              "after": "Open"},
             "Added option New to Status",
             lambda cols: cols[4].options == ("Open", "New", "Closed", "Hold")),
            ({"op": "set_required", "name": "Name", "required": False},
             "Made Name optional",
             lambda cols: not cols[0].required),
            ({"op": "rename_table", "title": " Stock "},
             "Renamed table to Stock",
             lambda cols: True),
        ],
    )
    def test_op(self, table_dir, schema, op, summary, check):
        rows_before = snapshot(table_dir)["rows"]
        plan, result = apply(table_dir, schema, [op])
        assert check(read_meta(table_dir).schema)
        assert table_info(table_dir) == BASE_DDL
        assert indexes(table_dir) == [(unique_index_name(K_SKU), 1)]
        # set_precision does not rewrite values; none of these touch rows.
        assert snapshot(table_dir)["rows"] == rows_before
        change = check_common(table_dir, plan, result, summary=summary)
        check_before(change, schema)
        assert result.row_ids == []

    def test_rename_table_title(self, table_dir, schema):
        plan, result = apply(table_dir, schema, [{"op": "rename_table", "title": "Stock"}])
        assert read_meta(table_dir).title == "Stock"
        assert result.title == "Stock"
        change = get_change(table_dir, result.change_id)
        assert (change["before"]["title"], change["after"]["title"]) == (TITLE, "Stock")


# --- Required / unique --------------------------------------------------------


class TestSetRequired:
    def test_ok(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema, [{"op": "set_required", "name": "Done", "required": True}]
        )
        assert read_meta(table_dir).schema[3].required
        check_common(table_dir, plan, result, summary="Made Done required")

    def test_refused(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "set_required", "name": "Price", "required": True}])
        err = exc.value
        assert err.code == "rows_violate"
        assert (err.check, err.column_key, err.column, err.option) == (
            "required", K_PRICE, "Price", None
        )
        assert (err.row_ids, err.total) == ([2, 3], 2)
        assert "'Price'" in err.message and "_id 2, 3" in err.message
        assert isinstance(err, TableStorageError)
        assert snapshot(table_dir) == before


class TestSetUnique:
    def test_on_creates_index_nulls_ignored(self, table_dir, schema):
        # Price: two values, two nulls (nulls do not count).
        plan, result = apply(
            table_dir, schema, [{"op": "set_unique", "name": "Price", "unique": True}]
        )
        assert (unique_index_name(K_PRICE), 1) in indexes(table_dir)
        check_common(table_dir, plan, result, summary="Made Price unique")

    def test_off_drops_index(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema, [{"op": "set_unique", "name": "SKU", "unique": False}]
        )
        assert indexes(table_dir) == []
        check_common(table_dir, plan, result, summary="Removed unique from SKU")

    def test_refused_on_duplicates(self, table_dir, schema):
        insert_rows(
            table_dir, schema, schema_version=1, source="ui:1", actor_user_id=1,
            rows=[{"Name": "E", "Price": "1.5"}, {"Name": "F", "Price": "2"},
                  {"Name": "G", "Price": "9"}],
            now=NOW,
        )
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "set_unique", "name": "Price", "unique": True}])
        assert (exc.value.check, exc.value.row_ids, exc.value.total) == (
            "unique", [1, 4, 5, 6], 4
        )
        assert snapshot(table_dir) == before

    def test_index_integrity_error_maps_to_rows_violate(
        self, table_dir, schema, monkeypatch
    ):
        insert_rows(
            table_dir, schema, schema_version=1, source="ui:1", actor_user_id=1,
            rows=[{"Name": "A"}], now=NOW,
        )
        plan = plan_for(schema, [{"op": "set_unique", "name": "Name", "unique": True}])
        # Simulate a check that missed the duplicate: drop the check effect.
        effects = [e for e in plan.effects if e.kind != "violation_check"]
        plan = type(plan)(plan.columns, effects, plan.new_title, plan.operations)
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply_schema_change(
                table_dir, schema, plan, schema_version=1, source=SOURCE,
                actor_user_id=None, title=TITLE,
            )
        assert (exc.value.check, exc.value.row_ids) == ("unique", [1, 5])
        assert snapshot(table_dir) == before


# --- Enum ops ------------------------------------------------------------------


class TestEnumOps:
    def test_rename_option_single(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_enum_option", "name": "Status", "option": "Open",
              "new_option": "Active"}],
        )
        assert stored(table_dir, K_STATUS) == ["Active", "Closed", None, "Active"]
        meta = read_meta(table_dir).schema[4]
        assert meta.options == ("Active", "Closed", "Hold")
        assert meta.default == "Active"
        change = check_common(
            table_dir, plan, result, summary="Renamed option Open to Active in Status"
        )
        assert change["before"]["rewrites"] == [
            {"kind": "enum_rename", "key": K_STATUS, "old": "Open", "new": "Active",
             "row_ids": [1, 4]}
        ]
        assert result.row_ids == [1, 4]
        assert table_info(table_dir) == BASE_DDL

    def test_rename_option_multi_keeps_option_order(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_enum_option", "name": "Tags", "option": "b",
              "new_option": "bee"}],
        )
        assert stored(table_dir, K_TAGS) == ['["a","bee"]', '["c"]', '["bee"]', None]
        change = check_common(table_dir, plan, result)
        assert change["before"]["rewrites"] == [
            {"kind": "enum_rename_multi", "key": K_TAGS, "old": "b", "new": "bee",
             "cells": {"1": '["a","b"]', "3": '["b"]'}}
        ]
        assert result.row_ids == [1, 3]

    def test_rename_chain_and_back(self, table_dir, schema):
        # a -> x -> a again: both steps are recorded; a restore replays both.
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_enum_option", "name": "Tags", "option": "a",
              "new_option": "x"},
             {"op": "rename_enum_option", "name": "Tags", "option": "x",
              "new_option": "a"}],
        )
        assert stored(table_dir, K_TAGS) == ['["a","b"]', '["c"]', '["b"]', None]
        change = check_common(table_dir, plan, result, summary="2 schema changes")
        assert [r["kind"] for r in change["before"]["rewrites"]] == [
            "enum_rename_multi", "enum_rename_multi"
        ]
        assert result.row_ids == [1]
        assert_replays(table_dir)

    def test_rename_onto_existing_refused_by_plan(self, schema):
        with pytest.raises(SchemaOperationError):
            plan_for(schema, [{"op": "rename_enum_option", "name": "Tags",
                               "option": "a", "new_option": "b"}])

    def test_remove_option_ok(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "remove_enum_option", "name": "Status", "option": "Hold"},
             {"op": "remove_enum_option", "name": "Tags", "option": "d"}],
        )
        meta = read_meta(table_dir).schema
        assert meta[4].options == ("Open", "Closed")
        assert meta[5].options == ("a", "b", "c")
        check_common(table_dir, plan, result)

    def test_remove_option_single_refused(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "remove_enum_option", "name": "Status", "option": "Closed"}])
        err = exc.value
        assert (err.check, err.column, err.option, err.row_ids, err.total) == (
            "enum_option", "Status", "Closed", [2], 1
        )
        assert "'Closed'" in err.message
        assert snapshot(table_dir) == before

    def test_remove_option_multi_refused(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "remove_enum_option", "name": "Tags", "option": "b"}])
        assert (exc.value.row_ids, exc.value.total) == ([1, 3], 2)
        assert snapshot(table_dir) == before

    def test_set_multiple_wraps(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "set_enum_multiple", "name": "Status", "multiple": True}],
        )
        assert stored(table_dir, K_STATUS) == ['["Open"]', '["Closed"]', None, '["Open"]']
        meta = read_meta(table_dir).schema[4]
        assert meta.multiple and meta.default == ("Open",)
        assert [r[K_STATUS] for r in dump_rows(table_dir, plan.columns)] == [
            ["Open"], ["Closed"], None, ["Open"]
        ]
        change = check_common(table_dir, plan, result, summary="Made Status multi-select")
        assert change["before"]["rewrites"] == [
            {"kind": "wrap", "key": K_STATUS, "row_ids": [1, 2, 4]}
        ]
        assert result.row_ids == [1, 2, 4]

    def test_wrap_then_rename_then_remove(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "set_enum_multiple", "name": "Status", "multiple": True},
             {"op": "rename_enum_option", "name": "Status", "option": "Closed",
              "new_option": "Done"},
             {"op": "remove_enum_option", "name": "Status", "option": "Hold"}],
        )
        assert stored(table_dir, K_STATUS) == ['["Open"]', '["Done"]', None, '["Open"]']
        change = check_common(table_dir, plan, result)
        assert change["before"]["rewrites"] == [
            {"kind": "wrap", "key": K_STATUS, "row_ids": [1, 2, 4]},
            {"kind": "enum_rename_multi", "key": K_STATUS, "old": "Closed",
             "new": "Done", "cells": {"2": '["Closed"]'}},
        ]
        assert_replays(table_dir)

    def test_single_rename_chain_replays_exactly(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_enum_option", "name": "Status", "option": "Open",
              "new_option": "X"},
             {"op": "rename_enum_option", "name": "Status", "option": "Closed",
              "new_option": "Open"},
             {"op": "rename_enum_option", "name": "Status", "option": "X",
              "new_option": "Closed"}],
        )
        # Open and Closed swapped.
        assert stored(table_dir, K_STATUS) == ["Closed", "Open", None, "Closed"]
        change = check_common(table_dir, plan, result)
        assert [(r["old"], r["new"], r["row_ids"]) for r in change["before"]["rewrites"]] == [
            ("Open", "X", [1, 4]), ("Closed", "Open", [2]), ("X", "Closed", [1, 4])
        ]
        assert_replays(table_dir)

    def test_backfilled_single_enum_wrapped(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Tier", "type": "enum", "options": ["Gold", "Silver"],
                         "required": True, "default": "Gold"}},
             {"op": "rename_enum_option", "name": "Tier", "option": "Gold",
              "new_option": "Platinum"},
             {"op": "set_enum_multiple", "name": "Tier", "multiple": True}],
        )
        key = plan.columns[-1].key
        assert stored(table_dir, key) == ['["Platinum"]'] * 4
        change = check_common(table_dir, plan, result)
        # The backfill records the value the rows ended up with.
        assert change["after"]["backfill"] == {
            key: {"value": '["Platinum"]'}
        }
        # Rewrites only cover columns that existed before the change.
        assert change["before"]["rewrites"] == []


# --- Batches -----------------------------------------------------------------


class TestBatches:
    def test_add_unique_drop(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"}},
             {"op": "set_unique", "name": "Name", "unique": True},
             {"op": "drop_column", "name": "Site"}],
        )
        notes = plan.columns[-1].key
        assert table_info(table_dir) == [k for k in BASE_DDL if k != K_SITE] + [notes]
        assert indexes(table_dir) == sorted(
            [(unique_index_name(K_SKU), 1), (unique_index_name(K_NAME), 1)]
        )
        change = check_common(table_dir, plan, result, summary="3 schema changes")
        check_before(change, schema)
        assert change["before"]["dropped_values"] == {
            K_SITE: {"1": "https://a.example"}
        }
        assert [o["op"] for o in change["after"]["operations"]] == [
            "add_column", "set_unique", "drop_column"
        ]

    def test_add_then_drop_cancels(self, table_dir, schema):
        rows_before = snapshot(table_dir)["rows"]
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Tmp", "type": "boolean", "required": True,
                         "default": True, "description": "x"}},
             {"op": "drop_column", "name": "Tmp"}],
        )
        assert table_info(table_dir) == BASE_DDL
        assert snapshot(table_dir)["rows"] == rows_before
        change = check_common(table_dir, plan, result)
        assert change["before"]["dropped_values"] == {}
        assert change["after"]["backfill"] == {}
        assert result.row_ids == []
        assert change["before"]["ddl_columns"] == change["after"]["ddl_columns"]

    def test_add_unique_then_drop_has_no_index(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "add_column",
              "column": {"name": "Code", "type": "text", "unique": True}},
             {"op": "drop_column", "name": "Code"}],
        )
        assert indexes(table_dir) == [(unique_index_name(K_SKU), 1)]
        assert table_info(table_dir) == BASE_DDL
        check_common(table_dir, plan, result)

    def test_rewrite_then_drop_records_original_values(self, table_dir, schema):
        plan, result = apply(
            table_dir, schema,
            [{"op": "rename_enum_option", "name": "Status", "option": "Open",
              "new_option": "Active"},
             {"op": "set_enum_multiple", "name": "Status", "multiple": True},
             {"op": "drop_column", "name": "Status"}],
        )
        change = check_common(table_dir, plan, result)
        assert change["before"]["dropped_values"] == {
            K_STATUS: {"1": "Open", "2": "Closed", "4": "Open"}
        }
        assert change["before"]["rewrites"] == []
        assert result.row_ids == [1, 2, 4]

    def test_second_change_uses_new_version(self, table_dir, schema):
        plan, _ = apply(table_dir, schema,
                        [{"op": "rename_column", "name": "Price", "new_name": "Cost"}])
        plan2, result2 = apply(
            table_dir, plan.columns, [{"op": "drop_column", "name": "Cost"}], version=2
        )
        check_common(table_dir, plan2, result2, version=2)
        assert get_change(table_dir, result2.change_id)["before"]["columns"] == (
            dump_schema(plan.columns)
        )


# --- Refusals ----------------------------------------------------------------


class TestRefusals:
    def _add_rows(self, table_dir, schema, rows):
        insert_rows(
            table_dir, schema, schema_version=1, source="ui:1", actor_user_id=1,
            rows=rows, now=NOW,
        )

    def test_required_ids_capped(self, table_dir, schema):
        self._add_rows(table_dir, schema, [{"Name": f"n{i}"} for i in range(25)])
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "set_required", "name": "Price", "required": True}])
        err = exc.value
        assert err.total == 27
        assert err.row_ids == [2, 3] + list(range(5, 23))
        assert len(err.row_ids) == 20
        assert "and 7 more" in err.message
        assert snapshot(table_dir) == before

    def test_unique_ids_capped(self, table_dir, schema):
        self._add_rows(table_dir, schema, [{"Name": "A"} for _ in range(24)])
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "set_unique", "name": "Name", "unique": True}])
        assert exc.value.total == 25
        assert exc.value.row_ids == [1] + list(range(5, 24))
        assert snapshot(table_dir) == before

    def test_enum_option_ids_capped(self, table_dir, schema):
        self._add_rows(table_dir, schema,
                       [{"Name": "x", "Tags": ["d", "a"]} for _ in range(22)])
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "remove_enum_option", "name": "Tags", "option": "d"}])
        assert exc.value.total == 22
        assert exc.value.row_ids == list(range(5, 25))
        assert snapshot(table_dir) == before

    def test_first_failing_check_reported(self, table_dir, schema):
        with pytest.raises(RowsViolateError) as exc:
            apply(table_dir, schema,
                  [{"op": "set_required", "name": "Site", "required": True},
                   {"op": "set_required", "name": "Price", "required": True}])
        assert exc.value.column == "Site"

    def test_violation_after_ddl_rolls_back(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError):
            apply(table_dir, schema,
                  [{"op": "drop_column", "name": "SKU"},
                   {"op": "add_column", "column": {"name": "N", "type": "text"}},
                   {"op": "rename_enum_option", "name": "Status", "option": "Open",
                    "new_option": "Z"},
                   {"op": "set_required", "name": "Price", "required": True}])
        assert snapshot(table_dir) == before

    def test_stale_schema(self, table_dir, schema):
        before = snapshot(table_dir)
        short = schema[:-1]
        with pytest.raises(SchemaMismatchError):
            apply(table_dir, short, [{"op": "drop_column", "name": "Site"}])
        assert snapshot(table_dir) == before

    def test_stale_version(self, table_dir, schema):
        before = snapshot(table_dir)
        with pytest.raises(SchemaMismatchError) as exc:
            apply(table_dir, schema, [{"op": "drop_column", "name": "Site"}], version=2)
        assert exc.value.code == "schema_mismatch"
        assert snapshot(table_dir) == before

    def test_change_too_large(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 300)
        before = snapshot(table_dir)
        with pytest.raises(ChangeTooLargeError) as exc:
            apply(table_dir, schema, [{"op": "drop_column", "name": "Site"}])
        assert exc.value.code == "change_too_large"
        assert "touches too many cells to record" in exc.value.message
        assert "row writes in smaller batches" in exc.value.message
        assert snapshot(table_dir) == before

    @pytest.mark.parametrize("target", ["write_meta", "append_change"])
    def test_failure_after_ddl_rolls_back_meta(
        self, table_dir, schema, monkeypatch, target
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(storage_mod, target, boom)
        before = snapshot(table_dir)
        with pytest.raises(RuntimeError):
            apply(table_dir, schema,
                  [{"op": "drop_column", "name": "SKU"},
                   {"op": "add_column",
                    "column": {"name": "N", "type": "text", "required": True,
                               "default": "x"}},
                   {"op": "rename_table", "title": "Other"}])
        assert snapshot(table_dir) == before
        assert read_meta(table_dir).schema_version == 1

    def test_plan_from_other_schema_is_caller_bug(self, table_dir, schema):
        other = parse_columns(
            [{"key": "c_0000ffff", "name": "X", "type": "text"}], allow_key=True
        )
        plan = plan_for(other, [{"op": "rename_column", "name": "X", "new_name": "Y"}])
        with pytest.raises(ValueError):
            apply_schema_change(
                table_dir, schema, plan, schema_version=1, source=SOURCE,
                actor_user_id=None, title=TITLE,
            )

    def test_bad_source(self, table_dir, schema):
        plan = plan_for(schema, [{"op": "rename_table", "title": "X"}])
        with pytest.raises(ValueError):
            apply_schema_change(
                table_dir, schema, plan, schema_version=1, source="nope",
                actor_user_id=None, title=TITLE,
            )

    def test_busy(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
        holder = sqlite3.connect(table_dir / TABLE_DB_NAME, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(TableStorageError) as exc:
                apply(table_dir, schema, [{"op": "rename_table", "title": "X"}])
            assert exc.value.code == "busy"
        finally:
            holder.close()


# --- Dry run -----------------------------------------------------------------


class TestDryRun:
    def test_ok_writes_nothing(self, table_dir, schema):
        before = snapshot(table_dir)
        plan = plan_for(schema, [{"op": "drop_column", "name": "SKU"},
                                 {"op": "rename_table", "title": "Other"}])
        assert check_schema_violations(table_dir, schema, plan, schema_version=1) is None
        assert snapshot(table_dir) == before

    def test_violation(self, table_dir, schema):
        before = snapshot(table_dir)
        plan = plan_for(schema, [{"op": "set_required", "name": "Price",
                                  "required": True}])
        with pytest.raises(RowsViolateError) as exc:
            check_schema_violations(table_dir, schema, plan, schema_version=1)
        assert exc.value.row_ids == [2, 3]
        assert snapshot(table_dir) == before

    def test_stale_and_too_large(self, table_dir, schema, monkeypatch):
        plan = plan_for(schema, [{"op": "drop_column", "name": "Site"}])
        with pytest.raises(SchemaMismatchError):
            check_schema_violations(table_dir, schema, plan, schema_version=5)
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 300)
        with pytest.raises(ChangeTooLargeError):
            check_schema_violations(table_dir, schema, plan, schema_version=1)


def test_images_are_json_round_trippable(table_dir, schema):
    _, result = apply(table_dir, schema, [{"op": "drop_column", "name": "Tags"}])
    change = get_change(table_dir, result.change_id)
    assert json.loads(json.dumps(change["before"])) == change["before"]


# --- Review fixes: compact images, table_empty guard ---------------------------


class TestCompactImages:
    def _medium(self, table_dir, schema, n=400):
        insert_rows(
            table_dir, schema, schema_version=1, source="ui:1", actor_user_id=1,
            rows=[{"Name": f"row number {i:04d}", "Status": "Closed"} for i in range(n)],
            now=NOW,
        )

    @pytest.mark.parametrize(
        "op",
        [
            {"op": "set_enum_multiple", "name": "Status", "multiple": True},
            {"op": "rename_enum_option", "name": "Status", "option": "Closed",
             "new_option": "Finished and archived"},
        ],
    )
    def test_fits_where_per_cell_images_would_not(
        self, table_dir, schema, monkeypatch, op
    ):
        self._medium(table_dir, schema)
        # A per-cell before/after map (the old format) needs at least this
        # many bytes: two quoted ids and two cells per touched row.
        # wrap: every non-null Status (403); rename: every Closed cell (401).
        touched = 403 if op["op"] == "set_enum_multiple" else 401
        old_format_floor = touched * 2 * (len('"123":') + len('"Closed",'))
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 5000)
        assert old_format_floor > 5000
        plan, result = apply(table_dir, schema, [op])
        change = get_change(table_dir, result.change_id)
        assert change["size_bytes"] <= 5000
        assert len(result.row_ids) == touched
        assert_replays(table_dir)

    def test_multi_op_too_large_hint_says_split(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 300)
        with pytest.raises(ChangeTooLargeError) as exc:
            apply(table_dir, schema, [{"op": "drop_column", "name": "Site"},
                                      {"op": "drop_column", "name": "Tags"}])
        assert "split it into smaller schema changes" in exc.value.message


class TestTableEmptyGuard:
    def test_empty_plan_on_populated_table_refused(self, table_dir, schema):
        plan = apply_schema_operations(
            schema,
            [{"op": "add_column",
              "column": {"name": "Owner", "type": "text", "required": True}}],
            table_empty=True,
        )
        before = snapshot(table_dir)
        with pytest.raises(RowsViolateError) as exc:
            apply_schema_change(
                table_dir, schema, plan, schema_version=1, source=SOURCE,
                actor_user_id=None, title=TITLE,
            )
        assert (exc.value.check, exc.value.column, exc.value.total) == (
            "required", "Owner", 4
        )
        assert exc.value.row_ids == [1, 2, 3, 4]
        assert snapshot(table_dir) == before
        with pytest.raises(RowsViolateError):
            check_schema_violations(table_dir, schema, plan, schema_version=1)

    def test_empty_plan_with_default_on_populated_table_refused(
        self, table_dir, schema
    ):
        # table_empty=True emits no backfill, so the default never reaches
        # the existing rows: still refused.
        plan = apply_schema_operations(
            schema,
            [{"op": "add_column",
              "column": {"name": "Owner", "type": "text", "required": True,
                         "default": "ops"}}],
            table_empty=True,
        )
        with pytest.raises(RowsViolateError):
            apply_schema_change(
                table_dir, schema, plan, schema_version=1, source=SOURCE,
                actor_user_id=None, title=TITLE,
            )

    def test_empty_plan_on_empty_table_ok(self, tmp_path, schema):
        path = tmp_path / "tables" / "t2"
        create_table_db(path, table_id="t2", title=TITLE, schema=schema,
                        source="ui:1", actor_user_id=1, now=NOW)
        plan = apply_schema_operations(
            schema,
            [{"op": "add_column",
              "column": {"name": "Owner", "type": "text", "required": True}}],
            table_empty=True,
        )
        result = apply_schema_change(
            path, schema, plan, schema_version=1, source=SOURCE,
            actor_user_id=None, title=TITLE,
        )
        assert result.row_count == 0 and result.row_ids == []
        assert get_change(path, result.change_id)["after"]["backfill"] == {}
