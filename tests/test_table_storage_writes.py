"""Quest Tables row writes beyond insert: update_rows, upsert_rows and
delete_rows (chat/tables/storage.py). Insert and the storage core are in
tests/test_table_storage.py, whose schema and helpers this file reuses."""

from datetime import timedelta
import sqlite3

import pytest

import chat.tables.storage as storage_mod
from chat.tables.schema import Column
from chat.tables.storage import (
    ChangeTooLargeError,
    InvalidRowsError,
    RowWriteResult,
    SchemaMismatchError,
    TableFullError,
    TableStorageError,
    TooManyRowsError,
    UpsertKeyError,
    create_table_db,
    delete_rows,
    dump_rows,
    get_change,
    row_count,
    update_rows,
    upsert_rows,
)
from tests.test_table_storage import (
    K_DONE,
    K_NAME,
    K_PRICE,
    K_SITE,
    K_SKU,
    K_STATUS,
    K_TAGS,
    NOW,
    SOURCE,
    assert_nothing_written,
    change_count,
    insert,
    make_schema,
    raw_sql,
)

LATER = NOW + timedelta(minutes=5)
STAMP = "2026-10-01T12:00:00.000000+00:00"
LATER_STAMP = "2026-10-01T12:05:00.000000+00:00"


@pytest.fixture
def schema():
    return make_schema()


@pytest.fixture
def table_dir(tmp_path, schema):
    """A table with rows 1 (a, SKU X, Price 1.5), 2 (b, SKU Y, Tags [a]) and
    3 (c, no SKU), written by change 2 at NOW."""
    path = tmp_path / "tables" / "t1"
    create_table_db(
        path, table_id="t1", title="Inventory", schema=schema,
        source="ui:1", actor_user_id=1, now=NOW,
    )
    insert(
        path,
        schema,
        [
            {"Name": "a", "SKU": "X", "Price": 1.5},
            {"Name": "b", "SKU": "Y", "Tags": ["a"]},
            {"Name": "c"},
        ],
    )
    return path


def update(table_dir, schema, updates, *, now=LATER, **kw) -> RowWriteResult:
    return update_rows(
        table_dir,
        schema,
        schema_version=kw.pop("schema_version", 1),
        source=kw.pop("source", SOURCE),
        actor_user_id=kw.pop("actor_user_id", None),
        updates=updates,
        now=now,
    )


def upsert(table_dir, schema, rows, *, key_column="SKU", now=LATER, **kw) -> RowWriteResult:
    return upsert_rows(
        table_dir,
        schema,
        schema_version=kw.pop("schema_version", 1),
        source=kw.pop("source", SOURCE),
        actor_user_id=kw.pop("actor_user_id", None),
        key_column=key_column,
        rows=rows,
        now=now,
    )


def delete(table_dir, schema, *, row_ids=None, where=None, now=LATER, **kw) -> RowWriteResult:
    return delete_rows(
        table_dir,
        schema,
        schema_version=kw.pop("schema_version", 1),
        source=kw.pop("source", SOURCE),
        actor_user_id=kw.pop("actor_user_id", None),
        row_ids=row_ids,
        where=where,
        now=now,
    )


def stored(table_dir, row_id) -> dict:
    """The stored row (change-image form) straight from SQL."""
    conn_rows = raw_sql(table_dir, "SELECT * FROM rows WHERE _id = ?", (row_id,))
    names = [r[1] for r in raw_sql(table_dir, "PRAGMA table_info(rows)")]
    return dict(zip(names, conn_rows[0]))


def codes(exc) -> list[tuple]:
    return [(p["row"], p["column"], p["code"]) for p in exc.value.problems]


# --- update_rows -----------------------------------------------------------------


class TestUpdate:
    def test_round_trip(self, table_dir, schema):
        result = update(
            table_dir, schema,
            [{"_id": 3, "Price": "2.50", "Done": True}, {"_id": 1, "Tags": ["c", "a"]}],
            actor_user_id=5,
        )
        assert result == RowWriteResult(
            op="update", inserted=0, updated=2, deleted=0, row_ids=[1, 3],
            inserted_ids=[], updated_ids=[3, 1], row_count=3, change_id=3,
        )
        rows = {r["_id"]: r for r in dump_rows(table_dir, schema)}
        assert rows[3][K_PRICE] == "2.5" and rows[3][K_DONE] is True
        assert rows[1][K_TAGS] == ["a", "c"]
        assert stored(table_dir, 1)[K_TAGS] == '["a","c"]'

    def test_partial_patch_leaves_other_cells_and_bumps_updated_at(self, table_dir, schema):
        before = {r["_id"]: r for r in dump_rows(table_dir, schema)}
        update(table_dir, schema, [{"_id": 1, "Price": 9}])
        after = {r["_id"]: r for r in dump_rows(table_dir, schema)}
        assert after[1] == {**before[1], K_PRICE: "9", "_updated_at": LATER_STAMP}
        assert after[1]["_created_at"] == STAMP
        assert after[2] == before[2] and after[3] == before[3]

    def test_explicit_null_clears(self, table_dir, schema):
        update(table_dir, schema, [{"_id": 1, "SKU": None, "Price": None}])
        row = stored(table_dir, 1)
        assert row[K_SKU] is None and row[K_PRICE] is None and row[K_NAME] == "a"

    def test_required_clear_refused(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, [{"_id": 1, "Name": None}])
        assert codes(exc) == [(0, "Name", "required")]
        assert_nothing_written(table_dir, 3, 2)

    def test_one_bad_update_writes_nothing(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": f"r{i}"} for i in range(7)])
        before = dump_rows(table_dir, schema)
        updates = [{"_id": i, "Price": i} for i in range(1, 11)]
        updates[6]["Price"] = "abc"
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, updates)
        assert codes(exc) == [(6, "Price", "invalid_number")]
        assert dump_rows(table_dir, schema) == before
        assert_nothing_written(table_dir, 10, 3)

    def test_every_problem_code(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "d"}, {"Name": "e"}])
        updates = [
            {"Price": 1},                       # 0: no _id
            {"_id": "1", "Price": 1},           # 1: _id not an int
            {"_id": True, "Price": 1},          # 2: bool is not an int
            {"_id": 99, "Price": 1},            # 3: unknown
            {"_id": 1, "Price": 1},             # 4: fine
            {"_id": 1, "Price": 2},             # 5: duplicate of 4
            {"_id": 2},                         # 6: empty patch
            {"_id": 3, "_created_at": "x", "_updated_at": "y"},  # 7: read-only
            {"_id": 4, "Bogus": 1},             # 8: unknown column
            "nope",                             # 9: not an object
            {"_id": 5, "Done": "yes"},          # 10: value problem
        ]
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, updates)
        assert codes(exc) == [
            (0, "_id", "invalid_row_id"),
            (1, "_id", "invalid_row_id"),
            (2, "_id", "invalid_row_id"),
            (3, "_id", "unknown_row"),
            (5, "_id", "duplicate_row_id"),
            (6, None, "empty_patch"),
            (7, "_created_at", "read_only_column"),
            (7, "_updated_at", "read_only_column"),
            (8, "Bogus", "unknown_column"),
            (9, None, "invalid_row"),
            (10, "Done", "wrong_type"),
        ]
        reasons = {p["row"]: p["reason"] for p in exc.value.problems}
        assert reasons[0] == "_id is required."
        assert reasons[3] == "there is no row with _id 99."
        assert reasons[5] == "_id 1 is also updated by row 4 of this call."
        assert_nothing_written(table_dir, 5, 3)

    def test_unique_violation_names_existing_row(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, [{"_id": 3, "SKU": "Y"}])
        (problem,) = exc.value.problems
        assert (problem["row"], problem["column"], problem["code"]) == (0, "SKU", "duplicate_value")
        assert problem["reason"] == "value 'Y' is already used by row _id 2."
        assert_nothing_written(table_dir, 3, 2)

    def test_unchanged_unique_cell_of_touched_row_still_counts(self, table_dir, schema):
        # Row 1 keeps X (not patched); row 3 taking X must collide with it.
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, [{"_id": 1, "Price": 3}, {"_id": 3, "SKU": "X"}])
        assert exc.value.problems == [
            {
                "row": 1,
                "column": "SKU",
                "code": "duplicate_value",
                "reason": "value 'X' is already used by row _id 1.",
            }
        ]

    def test_unchanged_unique_cell_problem_lands_on_changing_row(self, table_dir, schema):
        # The changing row comes first; the problem must not move to row 1.
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, [{"_id": 3, "SKU": "X"}, {"_id": 1, "Price": 3}])
        assert exc.value.problems == [
            {
                "row": 0,
                "column": "SKU",
                "code": "duplicate_value",
                "reason": "value 'X' is already used by row _id 1.",
            }
        ]

    @pytest.mark.parametrize("order", [1, -1])
    def test_clear_unique_while_another_row_takes_it(self, table_dir, schema, order):
        update(table_dir, schema, [{"_id": 1, "SKU": None}, {"_id": 3, "SKU": "X"}][::order])
        assert stored(table_dir, 1)[K_SKU] is None
        assert stored(table_dir, 3)[K_SKU] == "X"

    def test_clear_boolean_and_multi_enum(self, table_dir, schema):
        update(table_dir, schema, [{"_id": 2, "Done": None, "Tags": []}])
        row = stored(table_dir, 2)
        assert (row[K_DONE], row[K_TAGS]) == (None, None)

    def test_problem_order_row_level_then_id(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            update(table_dir, schema, [{"_id": "z"}, {"Bogus": 1, "Price": "x"}])
        assert codes(exc) == [
            (0, None, "empty_patch"),
            (0, "_id", "invalid_row_id"),
            (1, "_id", "invalid_row_id"),
            (1, "Price", "invalid_number"),
            (1, "Bogus", "unknown_column"),
        ]

    def test_unique_swap_in_one_call(self, table_dir, schema):
        update(table_dir, schema, [{"_id": 1, "SKU": "Y"}, {"_id": 2, "SKU": "X"}])
        assert stored(table_dir, 1)[K_SKU] == "Y"
        assert stored(table_dir, 2)[K_SKU] == "X"

    def test_unique_rotation_and_freed_value(self, table_dir, schema):
        # 1: X -> Z (new), 3 takes X freed by 1, 2 keeps Y.
        update(table_dir, schema, [{"_id": 3, "SKU": "X"}, {"_id": 1, "SKU": "Z"}])
        assert [r[K_SKU] for r in dump_rows(table_dir, schema)] == ["Z", "Y", "X"]

    def test_same_value_rewritten(self, table_dir, schema):
        update(table_dir, schema, [{"_id": 1, "SKU": "X"}])
        assert stored(table_dir, 1)[K_SKU] == "X"

    def test_change_entry(self, table_dir, schema):
        with storage_mod.connect(table_dir) as conn:
            images_before = storage_mod._row_images(conn, schema, [1, 3])
        result = update(
            table_dir, schema, [{"_id": 3, "Price": 4}, {"_id": 1, "Status": "Closed"}],
            actor_user_id=9,
        )
        change = get_change(table_dir, result.change_id)
        assert (change["kind"], change["op"], change["summary"]) == (
            "rows", "update", "Updated 2 rows",
        )
        assert (change["schema_before"], change["schema_after"]) == (1, 1)
        assert change["created_at"] == LATER_STAMP and change["actor_user_id"] == 9
        assert change["row_ids"] == [1, 3]
        assert change["before"] == images_before
        assert change["after"] == [stored(table_dir, 1), stored(table_dir, 3)]
        assert change["after"][0] == {
            **images_before[0], K_STATUS: "Closed", "_updated_at": LATER_STAMP,
        }
        assert change["after"][1] == {
            **images_before[1], K_PRICE: "4", "_updated_at": LATER_STAMP,
        }

    def test_singular_summary(self, table_dir, schema):
        result = update(table_dir, schema, [{"_id": 2, "Price": 1}])
        assert get_change(table_dir, result.change_id)["summary"] == "Updated 1 row"

    def test_replaying_before_image_restores_dump(self, table_dir, schema):
        before_dump = dump_rows(table_dir, schema)
        result = update(
            table_dir, schema,
            [{"_id": 1, "SKU": "Y", "Tags": ["b"]}, {"_id": 2, "SKU": "X", "Done": True}],
        )
        assert dump_rows(table_dir, schema) != before_dump
        change = get_change(table_dir, result.change_id)
        conn = sqlite3.connect(table_dir / storage_mod.TABLE_DB_NAME)
        try:
            # Clear the unique cells first, as a revert would, then restore.
            conn.execute(
                f'UPDATE rows SET "{K_SKU}" = NULL WHERE _id IN (1, 2)'
            )
            for image in change["before"]:
                cols = [k for k in image if k != "_id"]
                conn.execute(
                    f"UPDATE rows SET {', '.join(f'{c} = ?' for c in cols)} WHERE _id = ?",
                    (*(image[c] for c in cols), image["_id"]),
                )
            conn.commit()
        finally:
            conn.close()
        assert dump_rows(table_dir, schema) == before_dump

    def test_change_too_large_rolls_back(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 300)
        before = dump_rows(table_dir, schema)
        with pytest.raises(ChangeTooLargeError):
            update(table_dir, schema, [{"_id": 1, "Name": "x" * 300}])
        assert dump_rows(table_dir, schema) == before
        assert_nothing_written(table_dir, 3, 2)

    def test_too_many_rows(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_WRITE_MAX_ROWS", 2)
        with pytest.raises(TooManyRowsError) as exc:
            update(table_dir, schema, [{"_id": i, "Price": 1} for i in (1, 2, 3)])
        assert exc.value.count == 3
        assert_nothing_written(table_dir, 3, 2)

    def test_no_rows_and_schema_mismatch(self, table_dir, schema):
        with pytest.raises(TableStorageError) as exc:
            update(table_dir, schema, [])
        assert exc.value.code == "no_rows"
        with pytest.raises(SchemaMismatchError):
            update(table_dir, schema, [{"_id": 1, "Price": 1}], schema_version=2)
        assert_nothing_written(table_dir, 3, 2)


# --- upsert_rows -----------------------------------------------------------------


class TestUpsert:
    def test_split_counts_and_ids(self, table_dir, schema):
        result = upsert(
            table_dir, schema,
            [
                {"SKU": "N1", "Name": "new1"},
                {"SKU": "Y", "Price": 7},
                {"SKU": "N2", "Name": "new2"},
                {"SKU": "X", "Name": "a2"},
            ],
        )
        assert result == RowWriteResult(
            op="upsert", inserted=2, updated=2, deleted=0, row_ids=[1, 2, 4, 5],
            inserted_ids=[4, 5], updated_ids=[2, 1], row_count=5, change_id=3,
        )
        rows = {r["_id"]: r for r in dump_rows(table_dir, schema)}
        assert rows[4][K_NAME] == "new1" and rows[5][K_SKU] == "N2"
        assert rows[2][K_PRICE] == "7" and rows[2][K_NAME] == "b"
        assert rows[1][K_NAME] == "a2"

    def test_update_side_partial_insert_side_defaults(self, table_dir, schema):
        update(table_dir, schema, [{"_id": 2, "Status": "Closed", "Done": True}], now=NOW)
        upsert(table_dir, schema, [{"SKU": "Y", "Price": 3}, {"SKU": "N", "Name": "n"}])
        old = stored(table_dir, 2)
        assert old[K_STATUS] == "Closed" and old[K_DONE] == 1  # no defaults applied
        assert old[K_TAGS] == '["a"]' and old[K_PRICE] == "3"
        assert old["_created_at"] == STAMP and old["_updated_at"] == LATER_STAMP
        new = stored(table_dir, 4)
        assert new[K_STATUS] == "Open" and new[K_DONE] == 0  # defaults applied
        assert new["_created_at"] == new["_updated_at"] == LATER_STAMP

    def test_insert_side_required(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            upsert(table_dir, schema, [{"SKU": "Y", "Price": 1}, {"SKU": "N"}])
        assert codes(exc) == [(1, "Name", "required")]
        assert_nothing_written(table_dir, 3, 2)

    def test_key_canonicalized_for_lookup(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "d", "SKU": "Café"}])
        result = upsert(table_dir, schema, [{"SKU": "Café", "Price": 1}])
        assert result.updated_ids == [4] and result.inserted == 0

    def test_unknown_key_column(self, table_dir, schema):
        with pytest.raises(UpsertKeyError) as exc:
            upsert(table_dir, schema, [{"SKU": "X"}], key_column="Nope")
        assert exc.value.code == "unknown_key_column"
        assert "'Nope'" in exc.value.message
        assert_nothing_written(table_dir, 3, 2)

    def test_key_not_unique(self, table_dir, schema):
        with pytest.raises(UpsertKeyError) as exc:
            upsert(table_dir, schema, [{"Name": "a"}], key_column="Name")
        assert exc.value.code == "key_not_unique"
        assert_nothing_written(table_dir, 3, 2)

    def test_key_checked_before_rows(self, table_dir, schema):
        with pytest.raises(UpsertKeyError):
            upsert(table_dir, schema, [], key_column="Nope")

    def test_key_problems(self, table_dir, schema):
        rows = [
            {"Name": "m"},                      # 0: no key
            {"SKU": None, "Name": "m"},         # 1: null key
            {"SKU": 5, "Name": "m"},            # 2: bad key value
            {"SKU": "K", "Name": "k1"},         # 3: fine
            {"SKU": "K", "Name": "k2"},         # 4: duplicate of 3
            {"SKU": "Y", "_id": 2},             # 5: read-only _id
        ]
        with pytest.raises(InvalidRowsError) as exc:
            upsert(table_dir, schema, rows)
        assert codes(exc) == [
            (0, "SKU", "missing_key"),
            (1, "SKU", "missing_key"),
            (2, "SKU", "wrong_type"),
            (4, "SKU", "duplicate_key"),
            (5, "_id", "read_only_column"),
        ]
        dup = exc.value.problems[3]
        assert dup["reason"] == "key value 'K' is also used by row 3 of this call."
        assert_nothing_written(table_dir, 3, 2)

    def test_duplicate_key_after_canonicalization(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            upsert(
                table_dir, schema,
                [{"SKU": "Café", "Name": "a"}, {"SKU": "Café", "Name": "b"}],
            )
        assert codes(exc) == [(1, "SKU", "duplicate_key")]

    def test_other_unique_column_violation_names_row(self, tmp_path):
        schema = make_schema()
        schema = [
            *schema,
            Column(key="c_00000008", name="Code", type="text", unique=True),
        ]
        path = tmp_path / "t2"
        create_table_db(
            path, table_id="t2", title="T", schema=schema, source="ui:1",
            actor_user_id=1, now=NOW,
        )
        insert(path, schema, [{"Name": "a", "SKU": "X", "Code": "c1"},
                              {"Name": "b", "SKU": "Y", "Code": "c2"}])
        with pytest.raises(InvalidRowsError) as exc:
            upsert(path, schema, [{"SKU": "X", "Code": "c2"}])
        (problem,) = exc.value.problems
        assert (problem["column"], problem["code"]) == ("Code", "duplicate_value")
        assert problem["reason"] == "value 'c2' is already used by row _id 2."
        with pytest.raises(InvalidRowsError) as exc:
            upsert(path, schema, [{"SKU": "N", "Name": "n", "Code": "c1"}])
        assert exc.value.problems[0]["reason"] == "value 'c1' is already used by row _id 1."
        # Swapping Code between the two existing rows in one upsert is fine.
        upsert(path, schema, [{"SKU": "X", "Code": "c2"}, {"SKU": "Y", "Code": "c1"}])
        assert [r["c_00000008"] for r in dump_rows(path, schema)] == ["c2", "c1"]

    def test_table_full_counts_inserts_only(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_MAX_ROWS", 4)
        result = upsert(
            table_dir, schema,
            [{"SKU": "X", "Price": 1}, {"SKU": "Y", "Price": 2}, {"SKU": "N", "Name": "n"}],
        )
        assert (result.inserted, result.updated, result.row_count) == (1, 2, 4)
        with pytest.raises(TableFullError) as exc:
            upsert(table_dir, schema, [{"SKU": "X", "Price": 3}, {"SKU": "M", "Name": "m"}])
        assert (exc.value.row_count, exc.value.adding) == (4, 1)
        assert_nothing_written(table_dir, 4, 3)
        upsert(table_dir, schema, [{"SKU": "X", "Price": 3}, {"SKU": "N", "Price": 4}])

    def test_change_entry(self, table_dir, schema):
        with storage_mod.connect(table_dir) as conn:
            images_before = storage_mod._row_images(conn, schema, [2])
        result = upsert(table_dir, schema, [{"SKU": "N", "Name": "n"}, {"SKU": "Y", "Price": 2}])
        change = get_change(table_dir, result.change_id)
        assert (change["kind"], change["op"], change["summary"]) == (
            "rows", "upsert", "Upserted 2 rows (1 inserted, 1 updated)",
        )
        assert change["row_ids"] == [2, 4]
        assert change["before"] == images_before
        assert change["after"] == [stored(table_dir, 2), stored(table_dir, 4)]
        assert change["after"][0] == {
            **images_before[0], K_PRICE: "2", "_updated_at": LATER_STAMP,
        }

    def test_all_inserts_before_empty(self, table_dir, schema):
        result = upsert(table_dir, schema, [{"SKU": "N", "Name": "n"}])
        change = get_change(table_dir, result.change_id)
        assert change["before"] == [] and change["summary"] == (
            "Upserted 1 row (1 inserted, 0 updated)"
        )

    def test_atomic_failure(self, table_dir, schema):
        before = dump_rows(table_dir, schema)
        rows = [{"SKU": f"N{i}", "Name": f"n{i}"} for i in range(8)]
        rows.insert(3, {"SKU": "X", "Price": "bad"})
        with pytest.raises(InvalidRowsError) as exc:
            upsert(table_dir, schema, rows)
        assert codes(exc) == [(3, "Price", "invalid_number")]
        assert dump_rows(table_dir, schema) == before
        assert_nothing_written(table_dir, 3, 2)

    def test_too_many_rows(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_WRITE_MAX_ROWS", 1)
        with pytest.raises(TooManyRowsError) as exc:
            upsert(table_dir, schema, [{"SKU": "X"}, {"SKU": "Y"}])
        assert exc.value.count == 2
        assert_nothing_written(table_dir, 3, 2)

    def test_missing_key_row_gets_insert_rules(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            upsert(table_dir, schema, [{"Price": 1}, {"SKU": 5}])
        assert codes(exc) == [
            (0, "Name", "required"),
            (0, "SKU", "missing_key"),
            (1, "Name", "required"),
            (1, "SKU", "wrong_type"),
        ]
        assert_nothing_written(table_dir, 3, 2)

    def test_repeated_key_row_validated_like_first(self, table_dir, schema):
        # N inserts, so its repeat gets insert rules; X patches, so its
        # repeat gets patch rules (Name not needed).
        with pytest.raises(InvalidRowsError) as exc:
            upsert(table_dir, schema, [{"SKU": "N", "Name": "n"}, {"SKU": "N"},
                                       {"SKU": "X"}, {"SKU": "X"}])
        assert codes(exc) == [
            (1, "Name", "required"),
            (1, "SKU", "duplicate_key"),
            (3, "SKU", "duplicate_key"),
        ]


# --- delete_rows -----------------------------------------------------------------


class TestDelete:
    def test_by_ids(self, table_dir, schema):
        with storage_mod.connect(table_dir) as conn:
            images = storage_mod._row_images(conn, schema, [1, 3])
        result = delete(table_dir, schema, row_ids=[3, 1], actor_user_id=4)
        assert result == RowWriteResult(
            op="delete", inserted=0, updated=0, deleted=2, row_ids=[1, 3],
            inserted_ids=[], updated_ids=[], row_count=1, change_id=3,
        )
        assert [r["_id"] for r in dump_rows(table_dir, schema)] == [2]
        change = get_change(table_dir, 3)
        assert (change["kind"], change["op"], change["summary"]) == (
            "rows", "delete", "Deleted 2 rows",
        )
        assert change["row_ids"] == [1, 3] and change["actor_user_id"] == 4
        assert change["before"] == images and change["after"] == []
        assert change["created_at"] == LATER_STAMP

    def test_singular_summary(self, table_dir, schema):
        result = delete(table_dir, schema, row_ids=[2])
        assert get_change(table_dir, result.change_id)["summary"] == "Deleted 1 row"

    def test_id_problems_atomic(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            delete(table_dir, schema, row_ids=[1, "2", True, 99, 1, 3])
        assert codes(exc) == [
            (1, None, "invalid_row_id"),
            (2, None, "invalid_row_id"),
            (3, None, "unknown_row"),
            (4, None, "duplicate_row_id"),
        ]
        assert exc.value.problems[2]["reason"] == "there is no row with _id 99."
        assert_nothing_written(table_dir, 3, 2)

    def test_empty_ids(self, table_dir, schema):
        with pytest.raises(TableStorageError) as exc:
            delete(table_dir, schema, row_ids=[])
        assert exc.value.code == "no_rows"

    def test_too_many_ids(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_WRITE_MAX_ROWS", 2)
        with pytest.raises(TooManyRowsError) as exc:
            delete(table_dir, schema, row_ids=[1, 2, 3])
        assert exc.value.count == 3
        assert_nothing_written(table_dir, 3, 2)

    @pytest.mark.parametrize("selector", [{}, {"row_ids": [1], "where": ("1 = 1", ())}])
    def test_invalid_selector(self, table_dir, schema, selector):
        with pytest.raises(TableStorageError) as exc:
            delete(table_dir, schema, **selector)
        assert exc.value.code == "invalid_selector"
        assert_nothing_written(table_dir, 3, 2)

    def test_by_where_with_params(self, table_dir, schema):
        with storage_mod.connect(table_dir) as conn:
            images = storage_mod._row_images(conn, schema, [1, 2])
        result = delete(table_dir, schema, where=(f'"{K_SKU}" IN (?, ?)', ["X", "Y"]))
        assert (result.deleted, result.row_ids, result.row_count) == (2, [1, 2], 1)
        change = get_change(table_dir, result.change_id)
        assert change["before"] == images and change["after"] == []

    def test_where_zero_matches_writes_nothing(self, table_dir, schema):
        result = delete(table_dir, schema, where=(f'"{K_SKU}" = ?', ["nope"]))
        assert result == RowWriteResult(
            op="delete", inserted=0, updated=0, deleted=0, row_ids=[],
            inserted_ids=[], updated_ids=[], row_count=3, change_id=None,
        )
        assert_nothing_written(table_dir, 3, 2)

    def test_where_over_cap_names_matching_count(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_WRITE_MAX_ROWS", 2)
        with pytest.raises(TooManyRowsError) as exc:
            delete(table_dir, schema, where=(f'"{K_NAME}" IS NOT NULL', ()))
        assert (exc.value.code, exc.value.count, exc.value.cap) == ("too_many_rows", 3, 2)
        assert_nothing_written(table_dir, 3, 2)

    def test_where_shape(self, table_dir, schema):
        with pytest.raises(ValueError):
            delete(table_dir, schema, where="1 = 1")
        for params in ({"a": 1}, "ab", None):
            with pytest.raises(ValueError):
                delete(table_dir, schema, where=("1 = ?", params))
        assert_nothing_written(table_dir, 3, 2)

    def test_deleted_id_not_reused(self, table_dir, schema):
        delete(table_dir, schema, row_ids=[3])
        assert insert(table_dir, schema, [{"Name": "d"}]).row_ids == [4]

    def test_replaying_before_image_restores_dump(self, table_dir, schema):
        before_dump = dump_rows(table_dir, schema)
        result = delete(table_dir, schema, row_ids=[1, 2])
        change = get_change(table_dir, result.change_id)
        with storage_mod.connect(table_dir) as conn, storage_mod._write_txn(conn):
            for image in change["before"]:
                storage_mod._insert_image_row(conn, schema, image)
        assert dump_rows(table_dir, schema) == before_dump

    def test_change_too_large_rolls_back(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 50)
        with pytest.raises(ChangeTooLargeError):
            delete(table_dir, schema, row_ids=[1, 2, 3])
        assert_nothing_written(table_dir, 3, 2)

    def test_schema_mismatch(self, table_dir, schema):
        with pytest.raises(SchemaMismatchError):
            delete(table_dir, schema, row_ids=[1], schema_version=3)
        assert_nothing_written(table_dir, 3, 2)


def test_upsert_replay_restores_dump(table_dir, schema):
    """An upsert's images are complete: deleting the inserted rows and
    writing back the updated rows' before images gives the old dump."""
    before_dump = dump_rows(table_dir, schema)
    result = upsert(table_dir, schema, [{"SKU": "Y", "Price": 2}, {"SKU": "N", "Name": "n"}])
    change = get_change(table_dir, result.change_id)
    conn = sqlite3.connect(table_dir / storage_mod.TABLE_DB_NAME)
    try:
        before_ids = {image["_id"] for image in change["before"]}
        for row_id in set(change["row_ids"]) - before_ids:
            conn.execute("DELETE FROM rows WHERE _id = ?", (row_id,))
        for image in change["before"]:
            cols = [k for k in image if k != "_id"]
            conn.execute(
                f"UPDATE rows SET {', '.join(f'{c} = ?' for c in cols)} WHERE _id = ?",
                (*(image[c] for c in cols), image["_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    assert dump_rows(table_dir, schema) == before_dump
    assert row_count(table_dir) == 3 and change_count(table_dir) == 3
    assert K_SITE in change["after"][0]


def test_failure_after_append_rolls_back(table_dir, schema, monkeypatch):
    """A failure after append_change in the same transaction leaves neither
    the write nor its change entry."""
    before_dump = dump_rows(table_dir, schema)

    def boom(conn):
        raise RuntimeError("after append")

    monkeypatch.setattr(storage_mod, "_count", boom)
    with pytest.raises(RuntimeError):
        update(table_dir, schema, [{"_id": 1, "Price": 9}])
    monkeypatch.undo()
    assert change_count(table_dir) == 2
    assert dump_rows(table_dir, schema) == before_dump


def test_insert_image_row_keeps_id_and_timestamps(table_dir, schema):
    image = {"_id": 77, "_created_at": "2020-01-01T00:00:00.000000+00:00",
             "_updated_at": "2021-01-01T00:00:00.000000+00:00", K_NAME: "z",
             "c_0000ffff": "ignored"}
    with storage_mod.connect(table_dir) as conn, storage_mod._write_txn(conn):
        storage_mod._insert_image_row(conn, schema, image)
    row = storage_mod.get_rows_by_ids(table_dir, schema, [77])[0]
    assert (row["_id"], row["_created_at"], row["_updated_at"], row[K_NAME]) == (
        77, image["_created_at"], image["_updated_at"], "z")
    assert row[K_SKU] is None


# --- A second unique column ------------------------------------------------------

K_CODE = "c_00000008"


@pytest.fixture
def coded(tmp_path):
    """Rows 1 (a, SKU X, Code c1), 2 (b, SKU Y) and 3 (c) in a table whose
    Code column is unique too."""
    schema = make_schema() + [Column(key=K_CODE, name="Code", type="text", unique=True)]
    path = tmp_path / "tables" / "t2"
    create_table_db(
        path, table_id="t2", title="Coded", schema=schema,
        source="ui:1", actor_user_id=1, now=NOW,
    )
    insert(path, schema, [{"Name": "a", "SKU": "X", "Code": "c1"},
                          {"Name": "b", "SKU": "Y"}, {"Name": "c"}])
    return path, schema


def _dup(row, reason):
    return [{"row": row, "column": "Code", "code": "duplicate_value", "reason": reason}]


@pytest.mark.parametrize("order", [1, -1])
def test_update_duplicate_lands_on_changing_row(coded, order):
    path, schema = coded
    updates = [{"_id": 2, "Code": "c1"}, {"_id": 1, "Name": "zz"}][::order]
    with pytest.raises(InvalidRowsError) as exc:
        update(path, schema, updates)
    changing = 0 if order == 1 else 1
    assert exc.value.problems == _dup(changing, "value 'c1' is already used by row _id 1.")
    assert_nothing_written(path, 3, 2)


@pytest.mark.parametrize("order", [1, -1])
def test_upsert_duplicate_lands_on_changing_row(coded, order):
    path, schema = coded
    rows = [{"SKU": "Q", "Name": "q", "Code": "c1"}, {"SKU": "X", "Name": "aa"}][::order]
    with pytest.raises(InvalidRowsError) as exc:
        upsert(path, schema, rows)
    changing = 0 if order == 1 else 1
    assert exc.value.problems == _dup(changing, "value 'c1' is already used by row _id 1.")
    assert_nothing_written(path, 3, 2)


def test_duplicate_between_changing_rows_names_call_row(coded):
    path, schema = coded
    with pytest.raises(InvalidRowsError) as exc:
        update(path, schema, [{"_id": 2, "Code": "c7"}, {"_id": 3, "Code": "c7"}])
    assert exc.value.problems == _dup(1, "value 'c7' is also used by row 0 of this call.")


@pytest.mark.parametrize("order", [1, -1])
def test_upsert_insert_takes_value_an_update_gives_up(coded, order):
    path, schema = coded
    rows = [{"SKU": "X", "Code": "c9"}, {"SKU": "N", "Name": "n", "Code": "c1"}][::order]
    result = upsert(path, schema, rows)
    assert (result.inserted, result.updated) == (1, 1)
    assert stored(path, 1)[K_CODE] == "c9"
    assert stored(path, result.inserted_ids[0])[K_CODE] == "c1"


def test_existing_ids(table_dir):
    with storage_mod.connect(table_dir) as conn:
        assert storage_mod._existing_ids(conn, [3, 1, 99, 1]) == {1, 3}
