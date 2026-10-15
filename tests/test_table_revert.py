"""Quest Tables restore by forward replay (chat.tables.revert): restore to
a point after every row and schema operation, nested restores, the replay
path and in-effect rule, the file swap and WAL side files, refusals, and a
randomized property test."""

from datetime import datetime, timedelta, timezone
import itertools
import os
import random
import sqlite3

import pytest

import chat.tables.revert as revert_mod
import chat.tables.storage as storage_mod
from chat.tables.revert import (
    ChangeRef,
    RestoreResult,
    RevertRefusal,
    UnknownChangeError,
    in_effect_ids,
    in_effect_ids_from,
    read_change_refs,
    replay_path,
    restore_to,
)
from chat.tables.schema import parse_columns
from chat.tables.schema_ops import SchemaOperationError, apply_schema_operations
from chat.tables.storage import (
    TABLE_DB_NAME,
    SchemaMismatchError,
    TableStorageError,
    apply_schema_change,
    create_table_db,
    delete_rows,
    dump_rows,
    get_change,
    insert_rows,
    list_changes,
    read_meta,
    update_rows,
    upsert_rows,
)

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
TITLE = "Inventory"

K_NAME = "c_00000001"
K_PRICE = "c_00000002"
K_SITE = "c_00000003"
K_DONE = "c_00000004"
K_STATUS = "c_00000005"
K_TAGS = "c_00000006"
K_SKU = "c_00000007"


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


class Table:
    """A live table plus helpers that always use the current meta schema
    and a strictly increasing clock."""

    def __init__(self, path):
        self.path = path
        self._tick = itertools.count(1)

    def now(self):
        return T0 + timedelta(minutes=next(self._tick))

    @property
    def meta(self):
        return read_meta(self.path)

    def _ctx(self):
        meta = self.meta
        return meta.schema, meta.schema_version

    def insert(self, rows):
        schema, version = self._ctx()
        return insert_rows(self.path, schema, schema_version=version, source="ui:1",
                           actor_user_id=1, rows=rows, now=self.now()).change_id

    def update(self, updates):
        schema, version = self._ctx()
        return update_rows(self.path, schema, schema_version=version, source="ui:1",
                           actor_user_id=1, updates=updates, now=self.now()).change_id

    def upsert(self, key, rows):
        schema, version = self._ctx()
        return upsert_rows(self.path, schema, schema_version=version, source="ui:1",
                           actor_user_id=1, key_column=key, rows=rows,
                           now=self.now()).change_id

    def delete(self, row_ids=None, where=None):
        schema, version = self._ctx()
        return delete_rows(self.path, schema, schema_version=version, source="ui:1",
                           actor_user_id=1, row_ids=row_ids, where=where,
                           now=self.now()).change_id

    def schema_change(self, ops):
        meta = self.meta
        plan = apply_schema_operations(meta.schema, ops, table_empty=False)
        return apply_schema_change(
            self.path, meta.schema, plan, schema_version=meta.schema_version,
            source="action_request:7", actor_user_id=3, title=meta.title,
            now=self.now(),
        ).change_id

    def restore(self, change_id) -> RestoreResult:
        schema, version = self._ctx()
        return restore_to(self.path, schema, change_id, schema_version=version,
                          source="ui:2", actor_user_id=2, now=self.now())

    def newest(self):
        return list_changes(self.path, limit=1)[0]["id"]

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.path / TABLE_DB_NAME)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def snapshot(self):
        """The table state a restore must reproduce byte-equal: everything
        but ``meta.schema_version``, the change log and the AUTOINCREMENT
        sequence. Index lists and ``sqlite_master`` keep their own order."""
        meta = self.meta
        info = self.raw("PRAGMA table_info(rows)")
        index_list = self.raw("PRAGMA index_list(rows)")
        return {
            "dump": dump_rows(self.path, meta.schema),
            "rows": self.raw("SELECT * FROM rows ORDER BY _id"),
            "types": self.raw(
                "SELECT " + ", ".join(f'typeof("{c[1]}")' for c in info)
                + " FROM rows ORDER BY _id"
            ),
            "info": info,
            "indexes": index_list,
            "index_columns": {
                row[1]: self.raw(f'PRAGMA index_info("{row[1]}")')
                for row in index_list
            },
            "sql": self.raw(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
            ),
            "meta": self.raw(
                "SELECT key, value FROM meta WHERE key != 'schema_version' ORDER BY key"
            ),
        }

    def changes(self):
        return self.raw("SELECT * FROM changes ORDER BY id")

    def file_state(self):
        """Everything a refusal must leave identical."""
        return {
            "sql": self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"),
            "rows": self.raw("SELECT * FROM rows ORDER BY _id"),
            "meta": self.raw("SELECT key, value FROM meta ORDER BY key"),
            "changes": self.changes(),
            "sequence": self.raw("SELECT name, seq FROM sqlite_sequence ORDER BY name"),
        }

    def file_bytes(self):
        return (self.path / TABLE_DB_NAME).read_bytes()

    def dir_names(self):
        return sorted(os.listdir(self.path))

    def seq(self, name="rows"):
        row = self.raw("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,))
        return row[0][0] if row else 0


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "tables" / "t1"
    create_table_db(path, table_id="t1", title=TITLE, schema=make_schema(),
                    source="ui:1", actor_user_id=1, now=T0)
    t = Table(path)
    t.insert(ROWS)  # change #2
    return t


def check_restore_round_trip(table, apply, *, changes_state=True):
    """Snapshot at the newest entry P, apply (one or more changes), restore
    to P (byte-equal to the snapshot), then restore to the entry just
    before that restore (byte-equal to the applied state)."""
    point = table.newest()
    before = table.snapshot()
    version = table.meta.schema_version
    apply()
    applied_point = table.newest()
    after = table.snapshot()
    assert (after != before) == changes_state
    applied_version = table.meta.schema_version
    assert applied_version >= version
    log = table.changes()
    names = table.dir_names()

    result = table.restore(point)
    assert table.snapshot() == before
    assert table.dir_names() == names
    assert result.restored_to == point
    assert result.change_id == applied_point + 1
    assert result.schema_version == applied_version + 1 == table.meta.schema_version
    assert result.columns == table.meta.schema
    assert result.title == table.meta.title
    assert result.row_count == len(before["dump"])
    new_log = table.changes()
    assert new_log[:-1] == log and len(new_log) == len(log) + 1
    entry = get_change(table.path, result.change_id)
    assert (entry["kind"], entry["op"]) == ("restore", "restore")
    assert entry["summary"] == f"Restored table to change #{point}"
    assert entry["before"] == {} and entry["after"] == {"restore_to": point}
    assert entry["row_ids"] == []
    assert (entry["schema_before"], entry["schema_after"]) == (
        applied_version, applied_version + 1,
    )
    assert (entry["source"], entry["actor_user_id"]) == ("ui:2", 2)

    again = table.restore(applied_point)
    assert table.snapshot() == after
    assert again.schema_version == applied_version + 2
    assert table.raw("PRAGMA integrity_check") == [("ok",)]
    return result, again


# --- Every row op ----------------------------------------------------------------


class TestRowOps:
    def test_insert(self, table):
        check_restore_round_trip(table, lambda: table.insert([{"Name": "E", "SKU": "s5"}]))

    def test_update_including_null_clear(self, table):
        check_restore_round_trip(
            table,
            lambda: table.update([
                {"_id": 1, "Price": None, "Tags": ["d", "a"], "Done": False},
                {"_id": 3, "Site": "https://c.example", "Status": "Hold"},
            ]),
        )

    def test_update_swaps_unique_values(self, table):
        check_restore_round_trip(
            table, lambda: table.update([{"_id": 1, "SKU": "s2"}, {"_id": 2, "SKU": "s1"}])
        )

    def test_upsert_inserts_and_updates(self, table):
        check_restore_round_trip(
            table,
            lambda: table.upsert("SKU", [
                {"SKU": "s2", "Name": "B2", "Tags": ["a"]},
                {"SKU": "s9", "Name": "Z"},
            ]),
        )

    def test_delete_by_ids(self, table):
        check_restore_round_trip(table, lambda: table.delete(row_ids=[1, 3]))

    def test_delete_by_where(self, table):
        check_restore_round_trip(
            table, lambda: table.delete(where=(f'"{K_SKU}" IS NULL', []))
        )

    def test_delete_everything(self, table):
        check_restore_round_trip(table, lambda: table.delete(row_ids=[1, 2, 3, 4]))


# --- Every schema operation --------------------------------------------------------


def add(spec):
    return {"op": "add_column", "column": spec}


SCHEMA_CASES = {
    "add_nullable": [[add({"name": "Notes", "type": "text"})]],
    "add_required_default": [
        [add({"name": "Owner", "type": "text", "required": True, "default": "ops"})]
    ],
    "add_boolean_true": [
        [add({"name": "Flag", "type": "boolean", "required": True, "default": True})]
    ],
    "add_boolean_false": [
        [add({"name": "Flag", "type": "boolean", "required": True, "default": False})]
    ],
    "add_multi_default": [[add({
        "name": "Labels", "type": "enum", "options": ["x", "y", "z"],
        "multiple": True, "required": True, "default": ["y", "x"],
    })]],
    "add_single_enum_default": [[add({
        "name": "Level", "type": "enum", "options": ["lo", "hi"],
        "required": True, "default": "hi",
    })]],
    "add_unique": [[add({"name": "Code", "type": "text", "unique": True})]],
    "add_after": [[{"op": "add_column", "column": {"name": "Mid", "type": "number"},
                    "after": "Name"}]],
    "rename_column": [[{"op": "rename_column", "name": "Price", "new_name": "Cost"}]],
    "drop_with_nulls": [[{"op": "drop_column", "name": "Price"}]],
    "drop_unique_indexed": [[{"op": "drop_column", "name": "SKU"}]],
    "drop_multi": [[{"op": "drop_column", "name": "Tags"}]],
    "reorder": [[{"op": "reorder_columns",
                  "names": ["SKU", "Tags", "Status", "Done", "Site", "Price", "Name"]}]],
    "set_description": [[{"op": "set_description", "name": "Site",
                          "description": "Home page"}]],
    "set_required_on": [[{"op": "set_required", "name": "Done", "required": True}]],
    "set_required_off": [[{"op": "set_required", "name": "Name", "required": False}]],
    "set_unique_on": [[{"op": "set_unique", "name": "Name", "unique": True}]],
    "set_unique_off": [[{"op": "set_unique", "name": "SKU", "unique": False}]],
    "set_default": [[{"op": "set_default", "name": "Status", "default": "Hold"}]],
    "set_precision": [[{"op": "set_precision", "name": "Price", "precision": 0}]],
    "add_option": [[{"op": "add_enum_option", "name": "Tags", "option": "aa",
                     "after": "a"}]],
    "rename_option_single": [[{"op": "rename_enum_option", "name": "Status",
                               "option": "Open", "new_option": "Active"}]],
    "rename_option_multi": [[{"op": "rename_enum_option", "name": "Tags",
                              "option": "b", "new_option": "bee"}]],
    "rename_option_chain_single": [[
        {"op": "rename_enum_option", "name": "Status", "option": "Open",
         "new_option": "X"},
        {"op": "rename_enum_option", "name": "Status", "option": "X",
         "new_option": "Y"},
    ]],
    "rename_option_chain_multi_back": [[
        {"op": "rename_enum_option", "name": "Tags", "option": "a", "new_option": "x"},
        {"op": "rename_enum_option", "name": "Tags", "option": "x", "new_option": "a"},
    ]],
    "swap_options_single": [[
        {"op": "rename_enum_option", "name": "Status", "option": "Open",
         "new_option": "tmp"},
        {"op": "rename_enum_option", "name": "Status", "option": "Closed",
         "new_option": "Open"},
        {"op": "rename_enum_option", "name": "Status", "option": "tmp",
         "new_option": "Closed"},
    ]],
    "swap_options_multi_reorders": [[
        {"op": "rename_enum_option", "name": "Tags", "option": "a", "new_option": "t"},
        {"op": "rename_enum_option", "name": "Tags", "option": "b", "new_option": "a"},
        {"op": "rename_enum_option", "name": "Tags", "option": "t", "new_option": "b"},
    ]],
    "remove_option": [[{"op": "remove_enum_option", "name": "Status",
                        "option": "Hold"}]],
    "set_multiple": [[{"op": "set_enum_multiple", "name": "Status", "multiple": True}]],
    "wrap_then_rename": [[
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
        {"op": "rename_enum_option", "name": "Status", "option": "Open",
         "new_option": "Live"},
        {"op": "add_enum_option", "name": "Status", "option": "First", "after": None},
    ]],
    "rename_then_wrap": [[
        {"op": "rename_enum_option", "name": "Status", "option": "Closed",
         "new_option": "Done"},
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
    ]],
    "rename_table": [[{"op": "rename_table", "title": "Stock"}]],
    "batch": [[
        add({"name": "Owner", "type": "text", "required": True, "default": "ops"}),
        {"op": "rename_column", "name": "Site", "new_name": "URL"},
        {"op": "drop_column", "name": "Price"},
        {"op": "set_unique", "name": "Name", "unique": True},
        {"op": "rename_enum_option", "name": "Tags", "option": "c", "new_option": "cc"},
        {"op": "reorder_columns",
         "names": ["Owner", "Name", "URL", "Done", "Status", "Tags", "SKU"]},
        {"op": "rename_table", "title": "Stock"},
    ]],
    "add_then_drop_in_batch": [[
        add({"name": "Tmp", "type": "text", "required": True, "default": "t"}),
        {"op": "drop_column", "name": "Tmp"},
        {"op": "drop_column", "name": "SKU"},
    ]],
    "two_unique_indexes_in_one_batch": [[
        {"op": "set_unique", "name": "Site", "unique": True},
        {"op": "set_unique", "name": "Name", "unique": True},
    ]],
    "unique_off_and_on_moves_index": [[
        {"op": "set_unique", "name": "Name", "unique": True},
        {"op": "set_unique", "name": "SKU", "unique": False},
        {"op": "set_unique", "name": "SKU", "unique": True},
    ]],
    "two_batches": [
        [add({"name": "Owner", "type": "boolean", "required": True, "default": True})],
        [{"op": "drop_column", "name": "Done"},
         {"op": "set_enum_multiple", "name": "Status", "multiple": True}],
    ],
}


@pytest.mark.parametrize("case", sorted(SCHEMA_CASES))
def test_schema_operation_restores(table, case):
    def apply():
        for ops in SCHEMA_CASES[case]:
            table.schema_change(ops)

    # Renaming an option and back again rewrites cells to what they were.
    check_restore_round_trip(
        table, apply, changes_state=case != "rename_option_chain_multi_back"
    )


def test_schema_then_rows_then_schema(table):
    def apply():
        table.schema_change([add({"name": "Owner", "type": "text", "unique": True})])
        table.insert([{"Name": "E", "Owner": "o1"}])
        table.update([{"_id": 1, "Owner": "o2"}])
        table.schema_change([{"op": "drop_column", "name": "Price"},
                             {"op": "set_enum_multiple", "name": "Status",
                              "multiple": True}])
        table.delete(row_ids=[2])

    check_restore_round_trip(table, apply)


# --- Mixed histories and nested restores ---------------------------------------------


def record(table, snaps):
    snaps[table.newest()] = table.snapshot()


def test_mixed_history_every_point(table):
    snaps = {}
    record(table, snaps)  # 2
    table.update([{"_id": 2, "Price": "7"}]); record(table, snaps)
    table.schema_change([add({"name": "Owner", "type": "text", "required": True,
                              "default": "x"})]); record(table, snaps)
    table.insert([{"Name": "E", "Owner": "y"}]); record(table, snaps)
    table.schema_change([{"op": "rename_enum_option", "name": "Tags",
                          "option": "b", "new_option": "z"}]); record(table, snaps)
    table.delete(row_ids=[1]); record(table, snaps)
    table.schema_change([{"op": "drop_column", "name": "SKU"}]); record(table, snaps)
    for point in random.Random(1).sample(sorted(snaps), len(snaps)):
        table.restore(point)
        assert table.snapshot() == snaps[point], point


def test_restore_to_change_one_gives_empty_table(table):
    created = table.snapshot()
    table.restore(1)
    snap = table.snapshot()
    assert snap["rows"] == [] and snap["dump"] == []
    assert snap["info"] == created["info"] and snap["meta"] == created["meta"]


def test_nested_restores(table):
    snaps = {}
    record(table, snaps)                                          # 2
    table.insert([{"Name": "E"}]); record(table, snaps)           # 3
    table.update([{"_id": 1, "Name": "A2"}]); record(table, snaps)  # 4
    table.schema_change([{"op": "drop_column", "name": "Site"}])  # 5
    record(table, snaps)
    r6 = table.restore(3)                                         # 6 -> 3
    assert r6.change_id == 6 and table.snapshot() == snaps[3]
    snaps[6] = snaps[3]
    table.insert([{"Name": "F"}]); record(table, snaps)           # 7
    # Restore to a point inside the range 6 rewound.
    table.restore(4)                                              # 8 -> 4
    assert table.snapshot() == snaps[4]
    snaps[8] = snaps[4]
    # Restore to a restore entry: equals its target.
    table.restore(6)                                              # 9 -> 6
    assert table.snapshot() == snaps[3]
    snaps[9] = snaps[3]
    # Reverse the last restore: restore to the entry before it.
    table.restore(8)                                              # 10 -> 8
    assert table.snapshot() == snaps[4]
    snaps[10] = snaps[4]
    table.restore(7)                                              # 11 -> 7
    assert table.snapshot() == snaps[7]
    snaps[11] = snaps[7]
    table.restore(5)                                              # 12 -> 5
    assert table.snapshot() == snaps[5]
    snaps[12] = snaps[5]
    refs = read_change_refs(table.path)
    assert [r.restore_to for r in refs[5:]] == [3, None, 4, 6, 8, 7, 5]
    assert replay_path(refs) == [1, 2, 3, 4, 5, 12]
    assert replay_path(refs, 11) == [1, 2, 3, 6, 7, 11]
    assert replay_path(refs, 10) == [1, 2, 3, 4, 8, 10]
    assert replay_path(refs, 9) == [1, 2, 3, 6, 9]
    assert in_effect_ids(table.path) == {1, 2, 3, 4, 5, 12}
    for point, snap in snaps.items():
        table.restore(point)
        assert table.snapshot() == snap, point


def test_restore_to_newest_is_a_no_op_rebuild(table):
    table.update([{"_id": 1, "Price": "3"}])
    before = table.snapshot()
    result = table.restore(3)
    assert table.snapshot() == before
    assert get_change(table.path, result.change_id)["after"] == {"restore_to": 3}
    # Restoring to that restore entry is fine too.
    table.restore(result.change_id)
    assert table.snapshot() == before


def test_ids_never_reused_after_rewinding_inserts(table):
    table.insert([{"Name": "E"}, {"Name": "F"}])  # ids 5, 6
    table.delete(row_ids=[6])
    assert table.seq() == 6
    table.restore(2)
    assert table.seq() == 6
    table.insert([{"Name": "G"}])
    assert [r["_id"] for r in dump_rows(table.path, table.meta.schema)] == [1, 2, 3, 4, 7]


def test_sequence_covers_ids_beyond_the_live_sequence(table):
    """The rows sequence is the max of the live one and every logged id."""
    conn = sqlite3.connect(table.path / TABLE_DB_NAME)
    conn.execute("UPDATE sqlite_sequence SET seq = 2 WHERE name = 'rows'")
    conn.commit()
    conn.close()
    table.restore(2)
    assert table.seq() == 4


def test_changes_sequence_continues(table):
    conn = sqlite3.connect(table.path / TABLE_DB_NAME)
    conn.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'changes'")
    conn.commit()
    conn.close()
    assert table.restore(1).change_id == 41


def test_meta_and_header(table):
    table.schema_change([{"op": "rename_table", "title": "Stock"}])
    table.restore(2)
    meta = table.meta
    assert (meta.table_id, meta.title, meta.schema_version) == ("t1", TITLE, 3)
    header = table.file_bytes()[:100]
    assert header[18:20] == b"\x02\x02"  # WAL mode
    assert table.dir_names() == [TABLE_DB_NAME]


# --- Replay path (pure) ----------------------------------------------------------------


def refs(*kinds):
    """``refs("r", "r", 1, "r")``: an int is a restore entry to that id."""
    out = []
    for i, kind in enumerate(kinds, start=1):
        if isinstance(kind, int):
            out.append(ChangeRef(i, "restore", kind))
        else:
            out.append(ChangeRef(i, "schema" if i == 1 else "rows", None))
    return out


class TestReplayPath:
    def test_linear(self):
        log = refs("s", "r", "r")
        assert replay_path(log) == [1, 2, 3]
        assert replay_path(log, 2) == [1, 2]
        assert in_effect_ids_from(log) == {1, 2, 3}

    def test_restore_redirects(self):
        log = refs("s", "r", "r", "r", 2, "r")
        assert replay_path(log) == [1, 2, 5, 6]
        assert replay_path(log, 4) == [1, 2, 3, 4]
        assert in_effect_ids_from(log) == {1, 2, 5, 6}

    def test_reverse_a_restore(self):
        log = refs("s", "r", "r", 2, 3)
        assert replay_path(log) == [1, 2, 3, 5]
        assert in_effect_ids_from(log) == {1, 2, 3, 5}

    def test_restore_to_a_restore(self):
        log = refs("s", "r", "r", "r", 2, "r", 5, "r")
        assert replay_path(log) == [1, 2, 5, 7, 8]
        assert in_effect_ids_from(log) == {1, 2, 5, 7, 8}

    def test_restore_into_rewound_range(self):
        log = refs("s", "r", "r", "r", "r", 2, "r", 4)
        assert replay_path(log) == [1, 2, 3, 4, 8]
        assert in_effect_ids_from(log) == {1, 2, 3, 4, 8}

    def test_chain_of_restores(self):
        log = refs("s", "r", 1, 2, 3, 4)
        assert replay_path(log) == [1, 2, 4, 6]
        assert in_effect_ids_from(log) == {1, 2, 4, 6}

    def test_gaps_follow_the_previous_id(self):
        log = [ChangeRef(1, "schema", None), ChangeRef(4, "rows", None),
               ChangeRef(9, "restore", 1), ChangeRef(10, "rows", None)]
        assert replay_path(log) == [1, 9, 10]
        assert replay_path(log, 4) == [1, 4]

    def test_long_log_is_iterative(self):
        log = refs("s", *(["r"] * 50_000), 3, *(["r"] * 1000))
        path = replay_path(log)
        assert path[:3] == [1, 2, 3] and len(path) == 3 + 1 + 1000

    def test_empty_and_unknown(self):
        assert replay_path([]) == [] and in_effect_ids_from([]) == set()
        with pytest.raises(UnknownChangeError):
            replay_path(refs("s", "r"), 3)
        with pytest.raises(UnknownChangeError):
            replay_path([], 1)

    @pytest.mark.parametrize("target", [None, 5, 0, True, "2"])
    def test_bad_restore_target_is_corrupt(self, target):
        log = refs("s", "r", "r", "r")[:4] + [ChangeRef(5, "restore", target)]
        with pytest.raises(TableStorageError) as exc:
            replay_path(log)
        assert exc.value.code == "corrupt_storage"

    def test_ids_must_ascend(self):
        with pytest.raises(ValueError):
            replay_path([ChangeRef(2, "rows", None), ChangeRef(1, "schema", None)])


# --- Refusals and failure safety --------------------------------------------------------


@pytest.mark.parametrize("change_id", [0, -1, 3, 99])
def test_unknown_change(table, change_id):
    state, data = table.file_state(), table.file_bytes()
    with pytest.raises(UnknownChangeError) as exc:
        table.restore(change_id)
    assert isinstance(exc.value, RevertRefusal)
    assert exc.value.code == "unknown_change"
    assert exc.value.change_id == change_id
    assert table.file_state() == state and table.file_bytes() == data
    assert table.dir_names() == [TABLE_DB_NAME]


def test_non_integer_change_id_is_a_caller_bug(table):
    with pytest.raises(ValueError):
        table.restore("2")


def test_stale_schema_refused_first(table):
    meta = table.meta
    state = table.file_state()
    with pytest.raises(SchemaMismatchError):
        restore_to(table.path, meta.schema, 99, schema_version=meta.schema_version + 1,
                   source="ui:2", actor_user_id=2)
    with pytest.raises(SchemaMismatchError):
        restore_to(table.path, meta.schema[:-1], 1, schema_version=meta.schema_version,
                   source="ui:2", actor_user_id=2)
    assert table.file_state() == state
    assert table.dir_names() == [TABLE_DB_NAME]


def corrupt(table, change_id, field, value):
    conn = sqlite3.connect(table.path / TABLE_DB_NAME)
    conn.execute(f"UPDATE changes SET {field} = ? WHERE id = ?", (value, change_id))
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "setup",
    [
        lambda t: corrupt(t, 2, "after", "not json"),
        lambda t: corrupt(t, 2, "after", '[{"_id": 1}]'),
        lambda t: corrupt(t, 2, "after", '{"x": 1}'),
        lambda t: corrupt(t, 2, "row_ids", "[]"),
        lambda t: corrupt(t, 1, "after", '{"op": "create_table"}'),
        lambda t: corrupt(t, 1, "kind", "rows"),
        lambda t: corrupt(t, 3, "after", '{"columns": []}'),
        lambda t: corrupt(t, 3, "before", '{"rewrites": [{"kind": "zap", "key": "x"}]}'),
        lambda t: corrupt(t, 3, "kind", "weird"),
    ],
)
def test_corrupt_image_leaves_live_untouched(table, setup):
    table.schema_change([{"op": "rename_enum_option", "name": "Status",
                          "option": "Open", "new_option": "Go"}])  # 3
    setup(table)
    state, data = table.file_state(), table.file_bytes()
    with pytest.raises(TableStorageError) as exc:
        table.restore(3)
    assert exc.value.code == "corrupt_storage"
    assert table.file_bytes() == data and table.file_state() == state
    assert table.dir_names() == [TABLE_DB_NAME]


def test_rewrite_on_diverged_cell_is_corrupt(table):
    table.schema_change([{"op": "rename_enum_option", "name": "Tags",
                          "option": "b", "new_option": "z"}])  # 3
    # Tamper with the insert image so replay reaches a cell the rename
    # step did not record.
    change = get_change(table.path, 2)
    change["after"][0][K_TAGS] = '["a","c"]'
    corrupt(table, 2, "after", storage_mod._json(change["after"]))
    with pytest.raises(TableStorageError) as exc:
        table.restore(3)
    assert exc.value.code == "corrupt_storage"


def test_corrupt_restore_entry_target(table):
    table.restore(1)  # 3
    corrupt(table, 3, "after", '{"restore_to": 7}')
    with pytest.raises(TableStorageError) as exc:
        table.restore(2)
    assert exc.value.code == "corrupt_storage"


def test_failure_right_before_rename(table, monkeypatch):
    table.update([{"_id": 1, "Price": "5"}])
    state, data = table.file_state(), table.file_bytes()

    def boom(src, dst):
        assert src.name.startswith(revert_mod.TEMP_PREFIX) and src.exists()
        raise OSError("disk on fire")

    monkeypatch.setattr(revert_mod, "_replace_file", boom)
    with pytest.raises(OSError):
        table.restore(2)
    assert table.file_state() == state and table.file_bytes() == data
    assert table.dir_names() == [TABLE_DB_NAME]


def test_failure_mid_build_removes_temp(table, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("mid build")

    monkeypatch.setattr(revert_mod, "write_meta", boom)
    data = table.file_bytes()
    with pytest.raises(RuntimeError):
        table.restore(2)
    assert table.file_bytes() == data
    assert table.dir_names() == [TABLE_DB_NAME]


def test_writer_holding_the_lock_is_busy(table, monkeypatch):
    monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
    other = sqlite3.connect(table.path / TABLE_DB_NAME, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(TableStorageError) as exc:
            table.restore(2)
        assert exc.value.code == "busy"
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert table.dir_names() == [TABLE_DB_NAME]


def test_other_open_connection_is_busy(table, monkeypatch):
    """An idle connection keeps the side files alive: refuse rather than
    rename under it."""
    monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
    other = sqlite3.connect(table.path / TABLE_DB_NAME, isolation_level=None)
    other.execute("SELECT COUNT(*) FROM rows").fetchone()
    state = table.file_state()
    try:
        with pytest.raises(TableStorageError) as exc:
            table.restore(1)
        assert exc.value.code == "busy"
    finally:
        other.close()
    assert table.file_state() == state
    assert not any(n.startswith(revert_mod.TEMP_PREFIX) for n in table.dir_names())


def test_open_reader_is_busy(table, monkeypatch):
    monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
    other = sqlite3.connect(table.path / TABLE_DB_NAME, isolation_level=None)
    other.execute("BEGIN")
    other.execute("SELECT COUNT(*) FROM rows").fetchone()
    try:
        with pytest.raises(TableStorageError) as exc:
            table.restore(1)
        assert exc.value.code == "busy"
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert len(dump_rows(table.path, table.meta.schema)) == 4


# --- WAL swap safety ---------------------------------------------------------------------


def write_leaving_wal(table, sql, params=()):
    """Write through a connection that does not checkpoint on close, so the
    frames stay in ``table.db-wal`` (as after a crash)."""
    conn = sqlite3.connect(table.path / TABLE_DB_NAME, isolation_level=None)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(sql, params)
    conn.close()
    assert os.path.getsize(table.path / (TABLE_DB_NAME + "-wal")) > 0


def test_stale_wal_would_be_replayed_into_a_renamed_file(tmp_path):
    """Why the swap checkpoints and removes the side files first: SQLite
    applies a leftover WAL to whatever file now has the name."""
    live, new = tmp_path / "live.db", tmp_path / "new.db"
    for path, value in ((live, "old"), (new, "new")):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (a)")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
        conn.close()
    conn = sqlite3.connect(live, isolation_level=None)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    conn.execute("UPDATE t SET a = 'stale'")
    conn.close()
    os.replace(new, live)
    conn = sqlite3.connect(live)
    assert conn.execute("SELECT a FROM t").fetchall() == [("stale",)]
    conn.close()


def test_restore_with_leftover_wal(table):
    target = table.snapshot()
    write_leaving_wal(table, f'UPDATE rows SET "{K_NAME}" = ? WHERE _id = 1', ("wal",))
    write_leaving_wal(table, f'UPDATE rows SET "{K_NAME}" = ? WHERE _id = 2', ("wal2",))
    assert table.dir_names() == sorted([TABLE_DB_NAME, TABLE_DB_NAME + "-shm",
                                        TABLE_DB_NAME + "-wal"])
    table.restore(2)
    assert table.dir_names() == [TABLE_DB_NAME]
    # Reopen from scratch: the restored content, not the WAL's.
    assert table.snapshot() == target
    assert table.raw("PRAGMA integrity_check") == [("ok",)]
    names = [r[f"{K_NAME}"] for r in dump_rows(table.path, table.meta.schema)]
    assert names == ["A", "B", "C", "D"]


# --- Randomized property test -------------------------------------------------------------


def _value(column, rnd, serial):
    if column.type == "boolean":
        return rnd.random() < 0.5
    if column.type == "number":
        return str(rnd.randrange(100)) if not column.unique else str(serial)
    if column.type == "url":
        return f"https://h{serial if column.unique else rnd.randrange(5)}.example"
    if column.type == "enum":
        options = list(column.options or ())
        if column.multiple:
            return rnd.sample(options, rnd.randrange(0, min(3, len(options)) + 1))
        return rnd.choice(options)
    return f"v{serial}" if column.unique else rnd.choice(["x", "y", "z", "long text"])


def _payload(schema, rnd, serial):
    row = {}
    for column in schema:
        if column.required or rnd.random() < 0.7:
            row[column.name] = _value(column, rnd, next(serial))
        elif rnd.random() < 0.3:
            row[column.name] = None
    return row


def _schema_op(cols, rnd):
    names = [c.name for c in cols]
    enums = [c for c in cols if c.type == "enum"]
    kind = rnd.choice(["add", "add", "drop", "rename", "req", "uniq", "default",
                       "addopt", "renopt", "renopt", "rmopt", "multi", "title",
                       "reorder", "precision", "desc"])
    name = rnd.choice(names)
    if kind == "add":
        t = rnd.choice(["text", "boolean", "enum", "enum", "number"])
        spec = {"name": f"N{rnd.randrange(10000)}", "type": t}
        if t == "enum":
            spec["options"] = ["x", "y", "z"]
            if rnd.random() < 0.5:
                spec["multiple"] = True
            if rnd.random() < 0.6:
                spec["required"] = True
                spec["default"] = ["y", "x"] if spec.get("multiple") else "y"
        elif t == "boolean" and rnd.random() < 0.6:
            spec["required"] = True
            spec["default"] = rnd.random() < 0.5
        elif t == "text":
            if rnd.random() < 0.5:
                spec["required"] = True
                spec["default"] = "d"
            elif rnd.random() < 0.3:
                spec["unique"] = True
        return add(spec)
    if kind == "drop":
        return {"op": "drop_column", "name": name}
    if kind == "rename":
        return {"op": "rename_column", "name": name, "new_name": name + "r"}
    if kind == "req":
        return {"op": "set_required", "name": name, "required": rnd.random() < 0.5}
    if kind == "uniq":
        return {"op": "set_unique", "name": name, "unique": rnd.random() < 0.5}
    if kind == "title":
        return {"op": "rename_table", "title": f"T{rnd.randrange(99)}"}
    if kind == "reorder":
        shuffled = names[:]
        rnd.shuffle(shuffled)
        return {"op": "reorder_columns", "names": shuffled}
    if kind == "precision":
        return {"op": "set_precision", "name": name, "precision": rnd.randrange(4)}
    if kind == "desc":
        return {"op": "set_description", "name": name, "description": "d"}
    if kind == "default":
        return {"op": "set_default", "name": name, "default": None}
    if not enums:
        return {"op": "rename_table", "title": "T"}
    enum = rnd.choice(enums)
    options = list(enum.options)
    if kind == "addopt":
        return {"op": "add_enum_option", "name": enum.name,
                "option": f"o{rnd.randrange(99)}", "after": rnd.choice(options + [None])}
    if kind == "renopt":
        return {"op": "rename_enum_option", "name": enum.name,
                "option": rnd.choice(options),
                "new_option": rnd.choice(["a", "b", "x", "y", "Open", "Q", "R", "tmp"])}
    if kind == "rmopt":
        return {"op": "remove_enum_option", "name": enum.name,
                "option": rnd.choice(options)}
    return {"op": "set_enum_multiple", "name": enum.name, "multiple": True}


def _random_step(table, rnd, serial):
    """One random write (refusals write nothing and are skipped)."""
    meta = table.meta
    schema = meta.schema
    ids = [r["_id"] for r in dump_rows(table.path, schema)]
    kind = rnd.choice(["insert", "insert", "update", "upsert", "delete", "schema",
                       "schema"])
    try:
        if kind == "insert":
            table.insert([_payload(schema, rnd, serial) for _ in range(rnd.randrange(1, 4))])
        elif kind == "update" and ids:
            chosen = rnd.sample(ids, min(len(ids), rnd.randrange(1, 3)))
            updates = []
            for row_id in chosen:
                patch = _payload(schema, rnd, serial)
                if not patch:
                    patch = {schema[0].name: _value(schema[0], rnd, next(serial))}
                updates.append({"_id": row_id, **patch})
            table.update(updates)
        elif kind == "upsert":
            keys = [c for c in schema if c.unique]
            if keys:
                key = rnd.choice(keys)
                rows = [_payload(schema, rnd, serial) for _ in range(2)]
                if ids:
                    existing = dump_rows(table.path, schema)[0][key.key]
                    if existing is not None:
                        rows[0][key.name] = existing
                for row in rows:
                    row.setdefault(key.name, _value(key, rnd, next(serial)))
                table.upsert(key.name, rows)
        elif kind == "delete" and ids:
            table.delete(row_ids=rnd.sample(ids, min(len(ids), rnd.randrange(1, 3))))
        elif kind == "schema":
            ops, cols = [], schema
            for _ in range(rnd.randrange(1, 5)):
                op = _schema_op(cols, rnd)
                try:
                    cols = apply_schema_operations(schema, ops + [op],
                                                   table_empty=not ids).columns
                    ops.append(op)
                except SchemaOperationError:
                    pass
            if ops:
                plan = apply_schema_operations(schema, ops, table_empty=not ids)
                apply_schema_change(table.path, schema, plan,
                                    schema_version=meta.schema_version, source="ui:1",
                                    actor_user_id=1, title=meta.title, now=table.now())
    except TableStorageError as exc:
        assert exc.code in {"invalid_rows", "rows_violate", "table_full", "no_rows"}, exc


@pytest.mark.parametrize("seed", range(40))
def test_random_histories(tmp_path, seed):
    rnd = random.Random(seed)
    path = tmp_path / "t"
    create_table_db(path, table_id="t", title=TITLE, schema=make_schema(),
                    source="ui:1", actor_user_id=1, now=T0)
    table = Table(path)
    serial = itertools.count(1)
    snaps = {1: table.snapshot()}
    for _ in range(14):
        if len(snaps) > 2 and rnd.random() < 0.2:
            point = rnd.choice(sorted(snaps))
            result = table.restore(point)
            assert table.snapshot() == snaps[point], (seed, point)
            snaps[result.change_id] = snaps[point]
        else:
            _random_step(table, rnd, serial)
            snaps[table.newest()] = table.snapshot()
    refs_now = read_change_refs(path)
    assert [r.id for r in refs_now] == sorted(snaps)
    # Expected in-effect set, computed independently by recursion.
    by_id = {r.id: r for r in refs_now}

    def path_of(q):
        r = by_id[q]
        if q == 1:
            return [1]
        return path_of(r.restore_to if r.kind == "restore" else q - 1) + [q]

    expected_effect = set(path_of(refs_now[-1].id))
    assert in_effect_ids(path) == expected_effect
    for point in rnd.sample(sorted(snaps), min(6, len(snaps))):
        log = table.changes()
        table.restore(point)
        assert table.snapshot() == snaps[point], (seed, point)
        assert table.changes()[:-1] == log
        assert table.raw("PRAGMA integrity_check") == [("ok",)]


# --- Review fixes: index order, stale temps, fsync, in-effect pages ---------------


def test_unique_index_creation_order_is_replayed(table):
    """A batch creating two unique indexes: restore keeps their order, so
    sqlite_master and index_list match unsorted."""
    table.schema_change([{"op": "set_unique", "name": "Site", "unique": True},
                         {"op": "set_unique", "name": "Name", "unique": True}])
    change = get_change(table.path, 3)
    assert change["after"]["unique_indexes"] == [
        f"ux_rows_{K_SKU}", f"ux_rows_{K_SITE}", f"ux_rows_{K_NAME}",
    ]
    assert change["after"]["created_indexes"] == [f"ux_rows_{K_SITE}", f"ux_rows_{K_NAME}"]
    snap = table.snapshot()
    table.restore(3)
    assert table.snapshot() == snap
    assert [r[1] for r in table.raw("PRAGMA index_list(rows)")] == [
        r[1] for r in snap["indexes"]
    ]


def test_stale_temp_files_are_removed(table):
    stale = [table.path / (revert_mod.TEMP_PREFIX + "dead" + suffix)
             for suffix in ("", "-journal", "-wal", "-shm")]
    for path in stale:
        path.write_bytes(b"junk")
    table.restore(2)
    assert table.dir_names() == [TABLE_DB_NAME]


def test_stale_temps_removed_even_when_refused(table):
    stale = table.path / (revert_mod.TEMP_PREFIX + "dead")
    stale.write_bytes(b"junk")
    with pytest.raises(UnknownChangeError):
        table.restore(99)
    assert table.dir_names() == [TABLE_DB_NAME]


def test_directory_fsync_failure_after_swap_is_logged(table, monkeypatch, caplog):
    table.update([{"_id": 1, "Price": "4"}])
    target = table.snapshot()
    table.update([{"_id": 1, "Price": "5"}])
    real = revert_mod._fsync_path

    def fsync(path, *, directory=False):
        if directory:
            raise OSError("no fsync for you")
        return real(path, directory=directory)

    monkeypatch.setattr(revert_mod, "_fsync_path", fsync)
    with caplog.at_level("WARNING", logger="chat.tables.revert"):
        result = table.restore(3)
    assert result.change_id == 5
    assert table.snapshot() == target
    assert "fsync" in caplog.text


def test_list_changes_in_effect(table):
    table.insert([{"Name": "E"}])  # 3
    table.update([{"_id": 1, "Name": "A2"}])  # 4
    table.restore(2)  # 5
    table.insert([{"Name": "F"}])  # 6
    page = revert_mod.list_changes_in_effect(table.path, limit=3)
    assert [(c["id"], c["in_effect"], c["restore_to"]) for c in page] == [
        (6, True, None), (5, True, 2), (4, False, None),
    ]
    older = revert_mod.list_changes_in_effect(table.path, limit=10, before_id=4)
    assert [(c["id"], c["in_effect"]) for c in older] == [
        (3, False), (2, True), (1, True),
    ]
    assert {k for k in page[0]} == set(list_changes(table.path)[0]) | {"in_effect"}


def test_list_changes_malformed_restore_entry_is_corrupt(table):
    table.restore(1)  # 3
    corrupt(table, 3, "after", "{not json")
    with pytest.raises(TableStorageError) as exc:
        list_changes(table.path)
    assert exc.value.code == "corrupt_storage"
    with pytest.raises(TableStorageError) as exc:
        revert_mod.list_changes_in_effect(table.path)
    assert exc.value.code == "corrupt_storage"


def test_recreated_index_moves_to_the_end_of_sqlite_master(table):
    """Unique off then on in one batch re-creates the index after every
    other sqlite_master entry; replay re-creates it too."""
    table.schema_change([{"op": "set_unique", "name": "SKU", "unique": False},
                         {"op": "set_unique", "name": "SKU", "unique": True},
                         {"op": "rename_column", "name": "Name", "new_name": "N"}])
    names = [r[0] for r in table.raw("SELECT name FROM sqlite_master ORDER BY rowid")]
    created = get_change(table.path, 3)["after"]["created_indexes"]
    assert created == [f"ux_rows_{K_SKU}"]
    assert names[-1] == f"ux_rows_{K_SKU}"
    snap = table.snapshot()
    table.restore(3)
    assert table.snapshot() == snap
