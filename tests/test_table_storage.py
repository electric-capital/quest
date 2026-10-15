"""Quest Tables table file storage (chat/tables/storage.py)."""

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

import pytest

import chat.tables.storage as storage_mod
from chat.tables.schema import Column, dump_schema, parse_columns
from chat.tables.storage import (
    TABLE_DB_NAME,
    ChangeTooLargeError,
    InvalidRowsError,
    RowWriteResult,
    SchemaMismatchError,
    TableFullError,
    TableMeta,
    TableStorageError,
    TooManyRowsError,
    append_change,
    connect,
    copy_for_download,
    create_table_db,
    dump_rows,
    get_change,
    get_rows_by_ids,
    insert_rows,
    list_changes,
    read_meta,
    row_count,
    sql_type,
    unique_index_name,
    write_meta,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
SOURCE = "conversation:abc"

K_NAME = "c_00000001"
K_PRICE = "c_00000002"
K_SITE = "c_00000003"
K_DONE = "c_00000004"
K_STATUS = "c_00000005"
K_TAGS = "c_00000006"
K_SKU = "c_00000007"


def make_schema() -> list[Column]:
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
                "options": ["Open", "Closed"],
                "default": "Open",
            },
            {
                "key": K_TAGS,
                "name": "Tags",
                "type": "enum",
                "options": ["a", "b", "c"],
                "multiple": True,
            },
            {"key": K_SKU, "name": "SKU", "type": "text", "unique": True},
        ],
        allow_key=True,
    )


@pytest.fixture
def schema() -> list[Column]:
    return make_schema()


@pytest.fixture
def table_dir(tmp_path, schema):
    path = tmp_path / "tables" / "t1"
    create_table_db(
        path,
        table_id="t1",
        title="Inventory",
        schema=schema,
        source="ui:1",
        actor_user_id=1,
        now=NOW,
    )
    return path


def insert(table_dir, schema, rows, *, now=NOW, **kw) -> RowWriteResult:
    return insert_rows(
        table_dir,
        schema,
        schema_version=kw.pop("schema_version", 1),
        source=kw.pop("source", SOURCE),
        actor_user_id=kw.pop("actor_user_id", None),
        rows=rows,
        now=now,
    )


def raw_rows(table_dir) -> list[tuple]:
    conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
    try:
        return conn.execute("SELECT * FROM rows ORDER BY _id").fetchall()
    finally:
        conn.close()


def raw_sql(table_dir, sql, params=()):
    conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def change_count(table_dir) -> int:
    return raw_sql(table_dir, "SELECT COUNT(*) FROM changes")[0][0]


def assert_nothing_written(table_dir, rows_before: int, changes_before: int):
    assert row_count(table_dir) == rows_before
    assert change_count(table_dir) == changes_before


# --- Create ------------------------------------------------------------------


class TestCreate:
    def test_ddl_shape(self, table_dir, schema):
        info = raw_sql(table_dir, "PRAGMA table_info(rows)")
        cols = [(r[1], r[2], r[3], r[5]) for r in info]
        assert cols[:3] == [
            ("_id", "INTEGER", 0, 1),
            ("_created_at", "TEXT", 1, 0),
            ("_updated_at", "TEXT", 1, 0),
        ]
        assert [(c[0], c[1], c[2]) for c in cols[3:]] == [
            (c.key, sql_type(c), 0) for c in schema
        ]
        assert sql_type(schema[3]) == "INTEGER"
        assert {sql_type(c) for c in schema if c.type != "boolean"} == {"TEXT"}

    def test_rows_uses_autoincrement(self, table_dir):
        sql = raw_sql(table_dir, "SELECT sql FROM sqlite_master WHERE name = 'rows'")[0][0]
        assert "AUTOINCREMENT" in sql

    def test_unique_index_per_unique_column(self, table_dir):
        indexes = raw_sql(table_dir, "PRAGMA index_list(rows)")
        unique = {r[1] for r in indexes if r[2] == 1 and r[3] == "c"}
        assert unique == {unique_index_name(K_SKU)} == {"ux_rows_" + K_SKU}
        assert raw_sql(table_dir, f'PRAGMA index_info("ux_rows_{K_SKU}")')[0][2] == K_SKU

    def test_changes_and_meta_tables(self, table_dir):
        cols = [r[1] for r in raw_sql(table_dir, "PRAGMA table_info(changes)")]
        assert cols == [
            "id", "created_at", "source", "actor_user_id", "kind", "op", "summary",
            "schema_before", "schema_after", "row_ids", "before", "after",
            "size_bytes",
        ]
        assert [r[1] for r in raw_sql(table_dir, "PRAGMA table_info(meta)")] == [
            "key", "value",
        ]

    def test_meta_written_and_read(self, table_dir, schema):
        meta = read_meta(table_dir)
        assert meta == TableMeta(
            table_id="t1", title="Inventory", schema=schema, schema_version=1
        )
        stored = dict(raw_sql(table_dir, "SELECT key, value FROM meta"))
        assert stored["schema"] == json.dumps(
            dump_schema(schema), ensure_ascii=False, separators=(",", ":")
        )
        assert stored["schema_version"] == "1"

    def test_change_one(self, table_dir, schema):
        change = get_change(table_dir, 1)
        assert change["created_at"] == "2026-10-01T12:00:00.000000+00:00"
        assert change["source"] == "ui:1"
        assert change["actor_user_id"] == 1
        assert (change["kind"], change["op"], change["summary"]) == (
            "schema", "schema", "Created table",
        )
        assert (change["schema_before"], change["schema_after"]) == (0, 1)
        assert change["row_ids"] == [] and change["before"] == {}
        assert change["after"] == {
            "op": "create_table",
            "title": "Inventory",
            "columns": dump_schema(schema),
        }
        assert change["size_bytes"] == len("{}") + len(
            json.dumps(change["after"], ensure_ascii=False, separators=(",", ":")).encode()
        )

    def test_returns_change_id_and_schema_version(self, tmp_path, schema):
        path = tmp_path / "t"
        assert create_table_db(
            path, table_id="t", title="T", schema=schema, schema_version=4,
            source="action_request:9", actor_user_id=None, now=NOW,
        ) == 1
        assert read_meta(path).schema_version == 4
        assert get_change(path, 1)["schema_after"] == 4

    def test_storage_exists(self, table_dir, schema, tmp_path):
        with pytest.raises(TableStorageError) as exc:
            create_table_db(
                table_dir, table_id="t1", title="X", schema=schema,
                source="ui:1", actor_user_id=1,
            )
        assert exc.value.code == "storage_exists"
        orphan = tmp_path / "orphan"
        orphan.write_text("x")
        with pytest.raises(TableStorageError) as exc:
            create_table_db(
                orphan, table_id="o", title="X", schema=schema,
                source="ui:1", actor_user_id=1,
            )
        assert exc.value.code == "storage_exists"

    def test_bad_schema_is_value_error(self, tmp_path, schema):
        for bad in ([], [schema[0], schema[0]], [Column(key="x", name="X", type="text")]):
            with pytest.raises(ValueError):
                create_table_db(
                    tmp_path / "t", table_id="t", title="T", schema=bad,
                    source="ui:1", actor_user_id=1,
                )
        assert not (tmp_path / "t").exists()

    def test_too_many_columns(self, tmp_path, monkeypatch, schema):
        monkeypatch.setattr(storage_mod, "TABLE_MAX_COLUMNS", 3)
        with pytest.raises(ValueError):
            create_table_db(
                tmp_path / "t", table_id="t", title="T", schema=schema,
                source="ui:1", actor_user_id=1,
            )

    def test_bad_source(self, tmp_path, schema):
        for source in ("ui:", "bogus:1", "", None):
            with pytest.raises(ValueError):
                create_table_db(
                    tmp_path / "t", table_id="t", title="T", schema=schema,
                    source=source, actor_user_id=1,
                )

    def test_failure_cleans_up(self, tmp_path, schema, monkeypatch):
        def boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(storage_mod, "append_change", boom)
        path = tmp_path / "t"
        with pytest.raises(RuntimeError):
            create_table_db(
                path, table_id="t", title="T", schema=schema,
                source="ui:1", actor_user_id=1,
            )
        assert not path.exists()
        monkeypatch.undo()
        assert create_table_db(
            path, table_id="t", title="T", schema=schema,
            source="ui:1", actor_user_id=1,
        ) == 1


class TestWriteMeta:
    def test_upserts_only_given_keys(self, table_dir, schema):
        with connect(table_dir) as conn:
            write_meta(conn, title="Renamed", schema_version=2)
        meta = read_meta(table_dir)
        assert (meta.title, meta.schema_version, meta.table_id) == ("Renamed", 2, "t1")
        assert meta.schema == schema

    def test_corrupt_meta(self, table_dir):
        conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
        conn.execute("DELETE FROM meta WHERE key = 'title'")
        conn.commit()
        conn.close()
        with pytest.raises(TableStorageError) as exc:
            read_meta(table_dir)
        assert exc.value.code == "corrupt_storage"


# --- Guarded open ------------------------------------------------------------


class TestGuardedOpen:
    def test_pragmas(self, table_dir):
        with connect(table_dir) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.isolation_level is None

    def test_attach_denied(self, table_dir, tmp_path):
        with connect(table_dir) as conn:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("ATTACH DATABASE ? AS other", (str(tmp_path / "x.db"),))
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("/* c */ attach ? as other", (str(tmp_path / "x.db"),))
        assert not (tmp_path / "x.db").exists()

    def test_storage_missing(self, tmp_path, table_dir):
        with pytest.raises(TableStorageError) as exc:
            row_count(tmp_path / "nope")
        assert exc.value.code == "storage_missing"
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(TableStorageError) as exc:
            row_count(empty)
        assert exc.value.code == "storage_missing"
        assert not (empty / TABLE_DB_NAME).exists()

    def test_symlinked_table_db_refused(self, table_dir, tmp_path):
        other = tmp_path / "other.db"
        os.replace(table_dir / TABLE_DB_NAME, other)
        os.symlink(other, table_dir / TABLE_DB_NAME)
        with pytest.raises(TableStorageError) as exc:
            row_count(table_dir)
        assert exc.value.code == "unsafe_path"
        assert str(tmp_path) not in exc.value.message

    def test_symlinked_table_dir_refused(self, table_dir, tmp_path):
        link = tmp_path / "link"
        os.symlink(table_dir, link)
        with pytest.raises(TableStorageError) as exc:
            row_count(link)
        assert exc.value.code == "unsafe_path"

    def test_fifo_refused(self, tmp_path):
        path = tmp_path / "t"
        path.mkdir()
        os.mkfifo(path / TABLE_DB_NAME)
        with pytest.raises(TableStorageError) as exc:
            row_count(path)
        assert exc.value.code == "unsafe_path"

    def test_directory_named_table_db_refused(self, tmp_path):
        path = tmp_path / "t"
        (path / TABLE_DB_NAME).mkdir(parents=True)
        with pytest.raises(TableStorageError) as exc:
            row_count(path)
        assert exc.value.code == "unsafe_path"

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_symlinked_side_file_refused(self, table_dir, tmp_path, suffix):
        side = table_dir / (TABLE_DB_NAME + suffix)
        side.unlink(missing_ok=True)
        target = tmp_path / "target"
        target.write_bytes(b"")
        os.symlink(target, side)
        with pytest.raises(TableStorageError) as exc:
            row_count(table_dir)
        assert exc.value.code == "unsafe_path"
        assert side.name in exc.value.message

    def test_swapped_file_refused(self, table_dir, monkeypatch, tmp_path):
        real_connect = sqlite3.connect
        other = tmp_path / "swap.db"
        real_connect(other).close()

        def swapping_connect(path, *a, **kw):
            os.replace(other, path)
            return real_connect(path, *a, **kw)

        monkeypatch.setattr(storage_mod.sqlite3, "connect", swapping_connect)
        with pytest.raises(TableStorageError) as exc:
            row_count(table_dir)
        assert exc.value.code == "unsafe_path"


# --- Insert --------------------------------------------------------------------


class TestInsertRoundTrip:
    def test_every_type(self, table_dir, schema):
        result = insert(
            table_dir,
            schema,
            [
                {
                    "Name": "Café\r\nline",
                    "Price": "1.105",
                    "Site": "  https://example.com/x  ",
                    "Done": True,
                    "Status": "Closed",
                    "Tags": ["c", "a", "c"],
                    "SKU": "A-1",
                },
                {"Name": "plain", "Price": 3, "Done": False, "Tags": []},
            ],
        )
        assert result.row_ids == [1, 2]
        rows = dump_rows(table_dir, schema)
        assert rows[0] == {
            "_id": 1,
            "_created_at": "2026-10-01T12:00:00.000000+00:00",
            "_updated_at": "2026-10-01T12:00:00.000000+00:00",
            K_NAME: "Café\nline",
            K_PRICE: "1.1",
            K_SITE: "https://example.com/x",
            K_DONE: True,
            K_STATUS: "Closed",
            K_TAGS: ["a", "c"],
            K_SKU: "A-1",
        }
        assert rows[1][K_PRICE] == "3"
        assert rows[1][K_DONE] is False
        assert rows[1][K_TAGS] is None
        assert rows[1][K_SITE] is None and rows[1][K_SKU] is None

    def test_stored_form(self, table_dir, schema):
        insert(
            table_dir,
            schema,
            [{"Name": "x", "Price": "10.50", "Done": True, "Tags": ["b", "a"]}],
        )
        row = raw_rows(table_dir)[0]
        assert row[3:] == ("x", "10.5", None, 1, "Open", '["a","b"]', None)
        types = raw_sql(
            table_dir,
            f'SELECT typeof("{K_PRICE}"), typeof("{K_DONE}"), typeof("{K_TAGS}") FROM rows',
        )[0]
        assert types == ("text", "integer", "text")

    def test_number_precision_and_zero(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "Price": "-0.001"}, {"Name": "b", "Price": 2.675}])
        rows = dump_rows(table_dir, schema)
        assert [r[K_PRICE] for r in rows] == ["0", "2.68"]

    def test_get_rows_by_ids(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": n} for n in "abc"])
        got = get_rows_by_ids(table_dir, schema, [3, 1, 99, 1])
        assert [r["_id"] for r in got] == [1, 3]
        assert got[1][K_NAME] == "c"
        with pytest.raises(ValueError):
            get_rows_by_ids(table_dir, schema, [True])

    def test_ids_never_reused(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a"}, {"Name": "b"}])
        conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
        conn.execute("DELETE FROM rows WHERE _id = 2")
        conn.commit()
        conn.close()
        assert insert(table_dir, schema, [{"Name": "c"}]).row_ids == [3]


class TestInsertDefaults:
    def test_omitted_takes_default(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a"}])
        row = dump_rows(table_dir, schema)[0]
        assert row[K_DONE] is False and row[K_STATUS] == "Open"

    def test_explicit_null_skips_default(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "Status": None, "Done": None}])
        row = dump_rows(table_dir, schema)[0]
        assert row[K_STATUS] is None and row[K_DONE] is None

    def test_required_without_default(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, [{"Price": 1}, {"Name": None}])
        assert [(p["row"], p["column"], p["code"]) for p in exc.value.problems] == [
            (0, "Name", "required"),
            (1, "Name", "required"),
        ]

    def test_required_with_default(self, tmp_path):
        schema = parse_columns(
            [{"key": K_NAME, "name": "Name", "type": "text", "required": True,
              "default": "anon"}],
            allow_key=True,
        )
        path = tmp_path / "t"
        create_table_db(path, table_id="t", title="T", schema=schema,
                        source="ui:1", actor_user_id=1)
        insert(path, schema, [{}])
        assert dump_rows(path, schema)[0][K_NAME] == "anon"
        with pytest.raises(InvalidRowsError):
            insert(path, schema, [{"Name": None}])


class TestInsertValidation:
    def test_one_bad_row_writes_nothing(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "seed"}])
        rows = [{"Name": f"r{i}"} for i in range(10)]
        rows[6]["Price"] = "abc"
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, rows)
        assert exc.value.code == "invalid_rows"
        assert exc.value.problems == [
            {
                "row": 6,
                "column": "Price",
                "code": "invalid_number",
                "reason": exc.value.problems[0]["reason"],
            }
        ]
        assert "Column" not in exc.value.problems[0]["reason"]
        assert_nothing_written(table_dir, 1, 2)

    def test_all_problems_collected_in_order(self, table_dir, schema):
        rows = [
            {"Name": "ok"},
            {"Tags": ["zz"], "Price": "x", "Bogus": 1, "Name": 5},
            "not a row",
            {"Name": "y", "_id": 3, "_created_at": "x", "Done": "yes", "Site": "ftp://x"},
        ]
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, rows)
        assert [(p["row"], p["column"], p["code"]) for p in exc.value.problems] == [
            (1, "Name", "wrong_type"),
            (1, "Price", "invalid_number"),
            (1, "Tags", "unknown_option"),
            (1, "Bogus", "unknown_column"),
            (2, None, "invalid_row"),
            (3, "_id", "read_only_column"),
            (3, "Site", "invalid_url"),
            (3, "Done", "wrong_type"),
            (3, "_created_at", "read_only_column"),
        ]
        assert str(exc.value) == "The rows have 9 problems; nothing was written."
        assert_nothing_written(table_dir, 0, 1)

    def test_column_names_are_case_sensitive(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, [{"name": "x"}])
        codes = {(p["column"], p["code"]) for p in exc.value.problems}
        assert codes == {("name", "unknown_column"), ("Name", "required")}

    def test_no_rows(self, table_dir, schema):
        with pytest.raises(TableStorageError) as exc:
            insert(table_dir, schema, [])
        assert exc.value.code == "no_rows"
        with pytest.raises(ValueError):
            insert(table_dir, schema, {"Name": "x"})

    def test_bad_source(self, table_dir, schema):
        with pytest.raises(ValueError):
            insert(table_dir, schema, [{"Name": "x"}], source="conversation:")


class TestInsertUnique:
    def test_existing_value_names_the_row(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "SKU": "X"}, {"Name": "b", "SKU": "Y"}])
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, [{"Name": "c", "SKU": "Z"}, {"Name": "d", "SKU": "Y"}])
        (problem,) = exc.value.problems
        assert (problem["row"], problem["column"], problem["code"]) == (1, "SKU", "duplicate_value")
        assert problem["reason"] == "value 'Y' is already used by row _id 2."
        assert_nothing_written(table_dir, 2, 2)

    def test_duplicate_inside_call(self, table_dir, schema):
        with pytest.raises(InvalidRowsError) as exc:
            insert(
                table_dir,
                schema,
                [{"Name": "a", "SKU": "Q"}, {"Name": "b"}, {"Name": "c", "SKU": "Q"}],
            )
        (problem,) = exc.value.problems
        assert problem["row"] == 2
        assert "row 0 of this call" in problem["reason"]

    def test_canonical_form_compared(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "SKU": "Café"}])
        with pytest.raises(InvalidRowsError):
            insert(table_dir, schema, [{"Name": "b", "SKU": "Café"}])

    def test_nulls_never_conflict(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a"}, {"Name": "b", "SKU": None}])
        insert(table_dir, schema, [{"Name": "c"}])
        assert row_count(table_dir) == 3

    def test_unique_and_value_problems_together(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "SKU": "X"}])
        with pytest.raises(InvalidRowsError) as exc:
            insert(table_dir, schema, [{"Price": "bad", "SKU": "X"}])
        assert [(p["column"], p["code"]) for p in exc.value.problems] == [
            ("Name", "required"),
            ("Price", "invalid_number"),
            ("SKU", "duplicate_value"),
        ]


class TestInsertChange:
    def test_change_entry(self, table_dir, schema):
        later = NOW + timedelta(minutes=5)
        result = insert(
            table_dir, schema, [{"Name": "a", "Done": True}, {"Name": "b"}],
            now=later, actor_user_id=7,
        )
        assert result == RowWriteResult(
            op="insert", inserted=2, updated=0, deleted=0, row_ids=[1, 2],
            inserted_ids=[1, 2], updated_ids=[], row_count=2, change_id=2,
        )
        change = get_change(table_dir, 2)
        stamp = "2026-10-01T12:05:00.000000+00:00"
        assert change["created_at"] == stamp
        assert (change["kind"], change["op"], change["summary"]) == (
            "rows", "insert", "Inserted 2 rows",
        )
        assert (change["schema_before"], change["schema_after"]) == (1, 1)
        assert change["source"] == SOURCE and change["actor_user_id"] == 7
        assert change["row_ids"] == [1, 2]
        assert change["before"] == []
        assert change["after"][0] == {
            "_id": 1, "_created_at": stamp, "_updated_at": stamp,
            K_NAME: "a", K_PRICE: None, K_SITE: None, K_DONE: 1,
            K_STATUS: "Open", K_TAGS: None, K_SKU: None,
        }
        assert [r["_id"] for r in change["after"]] == [1, 2]
        assert change["size_bytes"] == len("[]") + len(
            json.dumps(change["after"], ensure_ascii=False, separators=(",", ":")).encode()
        )

    def test_images_use_stored_form(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "Tags": ["b", "a"]}])
        assert get_change(table_dir, 2)["after"][0][K_TAGS] == '["a","b"]'

    def test_singular_summary(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a"}])
        assert get_change(table_dir, 2)["summary"] == "Inserted 1 row"

    def test_list_changes(self, table_dir, schema):
        for name in "abc":
            insert(table_dir, schema, [{"Name": name}])
        changes = list_changes(table_dir)
        assert [c["id"] for c in changes] == [4, 3, 2, 1]
        assert "before" not in changes[0] and "row_ids" not in changes[0]
        assert changes[0]["summary"] == "Inserted 1 row"
        assert [c["id"] for c in list_changes(table_dir, limit=2, before_id=4)] == [3, 2]
        assert get_change(table_dir, 99) is None


class TestSummary:
    @pytest.mark.parametrize(
        "op, counts, text",
        [
            ("insert", {"inserted": 1}, "Inserted 1 row"),
            ("insert", {"inserted": 12}, "Inserted 12 rows"),
            ("update", {"updated": 3}, "Updated 3 rows"),
            ("update", {"updated": 1}, "Updated 1 row"),
            ("upsert", {"inserted": 2, "updated": 3}, "Upserted 5 rows (2 inserted, 3 updated)"),
            ("delete", {"deleted": 7}, "Deleted 7 rows"),
            ("delete", {"deleted": 1}, "Deleted 1 row"),
        ],
    )
    def test_summary(self, op, counts, text):
        assert storage_mod._summary(op, **counts) == text


# --- Caps and refusals ---------------------------------------------------------


class TestCaps:
    def test_too_many_rows(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_WRITE_MAX_ROWS", 3)
        insert(table_dir, schema, [{"Name": n} for n in "abc"])
        with pytest.raises(TooManyRowsError) as exc:
            insert(table_dir, schema, [{"Name": n} for n in "abcd"])
        assert (exc.value.code, exc.value.count, exc.value.cap) == ("too_many_rows", 4, 3)
        assert_nothing_written(table_dir, 3, 2)

    def test_default_write_cap(self, table_dir, schema):
        rows = [{"Name": str(i)} for i in range(storage_mod.TABLE_WRITE_MAX_ROWS + 1)]
        with pytest.raises(TooManyRowsError):
            insert(table_dir, schema, rows)

    def test_table_full(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_MAX_ROWS", 3)
        insert(table_dir, schema, [{"Name": "a"}, {"Name": "b"}])
        with pytest.raises(TableFullError) as exc:
            insert(table_dir, schema, [{"Name": "c"}, {"Name": "d"}])
        err = exc.value
        assert (err.code, err.cap, err.row_count, err.adding) == ("table_full", 3, 2, 2)
        assert "3" in err.message and "2 rows" in err.message
        assert_nothing_written(table_dir, 2, 2)
        insert(table_dir, schema, [{"Name": "c"}])
        assert row_count(table_dir) == 3

    def test_change_too_large(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 400)
        with pytest.raises(ChangeTooLargeError) as exc:
            insert(table_dir, schema, [{"Name": "x" * 200}, {"Name": "y" * 200}])
        assert exc.value.code == "change_too_large"
        assert exc.value.size_bytes > 400 and exc.value.cap == 400
        assert "Split" in exc.value.message
        assert_nothing_written(table_dir, 0, 1)

    def test_schema_mismatch_version(self, table_dir, schema):
        with pytest.raises(SchemaMismatchError) as exc:
            insert(table_dir, schema, [{"Name": "a"}], schema_version=2)
        assert exc.value.code == "schema_mismatch"
        assert_nothing_written(table_dir, 0, 1)

    def test_schema_mismatch_columns(self, table_dir, schema):
        extra = schema + [Column(key="c_0000000f", name="Extra", type="text")]
        with pytest.raises(SchemaMismatchError):
            insert(table_dir, extra, [{"Name": "a"}])
        with pytest.raises(SchemaMismatchError):
            insert(table_dir, schema[:-1], [{"Name": "a"}])
        assert_nothing_written(table_dir, 0, 1)

    def test_residual_integrity_error(self, table_dir, schema, monkeypatch):
        insert(table_dir, schema, [{"Name": "a", "SKU": "X"}])
        monkeypatch.setattr(storage_mod, "_check_unique", lambda *a, **kw: None)
        with pytest.raises(TableStorageError) as exc:
            insert(table_dir, schema, [{"Name": "b"}, {"Name": "c", "SKU": "X"}])
        assert exc.value.code == "constraint_failed"
        assert_nothing_written(table_dir, 1, 2)


# --- Change log ----------------------------------------------------------------


def _append(conn, now, **kw):
    return append_change(
        conn, now=now, source="ui:1", actor_user_id=None, kind="rows", op="insert",
        summary="x", schema_before=1, schema_after=1, row_ids=kw.get("row_ids", []),
        before=[], after=kw.get("after", []),
    )


class TestChangeLog:
    def test_row_ids_sorted(self, table_dir):
        with connect(table_dir) as conn, storage_mod._write_txn(conn):
            cid = _append(conn, NOW, row_ids=[5, 2, 9])
        assert get_change(table_dir, cid)["row_ids"] == [2, 5, 9]

    def test_too_large_inserts_nothing(self, table_dir, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 10)
        with connect(table_dir) as conn:
            with pytest.raises(ChangeTooLargeError):
                _append(conn, NOW, after=["x" * 20])
        assert change_count(table_dir) == 1

    def test_log_is_never_pruned(self, table_dir):
        with connect(table_dir) as conn, storage_mod._write_txn(conn):
            for days in (4000, 900, 1):
                _append(conn, NOW - timedelta(days=days))
        with connect(table_dir) as conn, storage_mod._write_txn(conn):
            for _ in range(30):
                _append(conn, NOW)
        assert change_count(table_dir) == 34
        assert [c["id"] for c in list_changes(table_dir, limit=200)] == list(
            range(34, 0, -1)
        )

    def test_list_changes_restore_to(self, table_dir):
        with connect(table_dir) as conn, storage_mod._write_txn(conn):
            storage_mod.append_change(
                conn, now=NOW, source="ui:1", actor_user_id=1, kind="restore",
                op="restore", summary="Restored table to change #1",
                schema_before=1, schema_after=2, row_ids=[], before={},
                after={"restore_to": 1},
            )
        newest, first = list_changes(table_dir)
        assert (newest["kind"], newest["restore_to"]) == ("restore", 1)
        assert first["restore_to"] is None


# --- Download copy -------------------------------------------------------------


class TestCopyForDownload:
    def test_without_history(self, table_dir, schema, tmp_path):
        insert(table_dir, schema, [{"Name": "a", "Tags": ["a"]}, {"Name": "b"}])
        dest = tmp_path / "out" / "copy.sqlite"
        dest.parent.mkdir()
        assert copy_for_download(table_dir, dest) == dest
        assert sorted(p.name for p in dest.parent.iterdir()) == ["copy.sqlite"]
        conn = sqlite3.connect(dest)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "changes" not in names and {"rows", "meta"} <= names
            assert conn.execute("SELECT name FROM sqlite_sequence").fetchall() == [("rows",)]
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 2
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            assert meta["title"] == "Inventory"
        finally:
            conn.close()
        assert change_count(table_dir) == 2

    def test_with_history(self, table_dir, schema, tmp_path):
        insert(table_dir, schema, [{"Name": "a"}])
        dest = tmp_path / "copy.sqlite"
        copy_for_download(table_dir, dest, include_history=True)
        conn = sqlite3.connect(dest)
        try:
            assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()
        assert not os.path.exists(str(dest) + "-wal")

    def test_dest_exists(self, table_dir, tmp_path):
        dest = tmp_path / "copy.sqlite"
        dest.write_text("keep")
        with pytest.raises(TableStorageError) as exc:
            copy_for_download(table_dir, dest)
        assert exc.value.code == "copy_exists"
        assert dest.read_text() == "keep"
        link = tmp_path / "link.sqlite"
        os.symlink(tmp_path / "missing", link)
        with pytest.raises(TableStorageError) as exc:
            copy_for_download(table_dir, link)
        assert exc.value.code == "copy_exists"
        assert not (tmp_path / "missing").exists()

    def test_parent_must_be_real_dir(self, table_dir, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(real, tmp_path / "linkdir")
        with pytest.raises(TableStorageError) as exc:
            copy_for_download(table_dir, tmp_path / "linkdir" / "c.sqlite")
        assert exc.value.code == "unsafe_path"
        assert list(real.iterdir()) == []

    def test_attach_still_denied_after_copy(self, table_dir, tmp_path):
        with connect(table_dir) as conn:
            conn.execute("SELECT 1")
        copy_for_download(table_dir, tmp_path / "c.sqlite")
        with connect(table_dir) as conn:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("ATTACH DATABASE ? AS x", (str(tmp_path / "x.db"),))

    def test_vacuum_into_other_path_denied(self, table_dir, tmp_path):
        with connect(table_dir) as conn:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("VACUUM INTO ?", (str(tmp_path / "sneaky.db"),))
        assert not (tmp_path / "sneaky.db").exists()

    def test_attach_allowed_is_scoped(self, table_dir, tmp_path):
        target = str(tmp_path / "ok.db")
        with connect(table_dir) as conn:
            with storage_mod._attach_allowed(conn, target):
                with pytest.raises(sqlite3.DatabaseError):
                    conn.execute("ATTACH DATABASE ? AS x", (str(tmp_path / "other.db"),))
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("ATTACH DATABASE ? AS x", (target,))
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("ATTACH DATABASE '' AS x")
        assert not os.path.exists(target)


# --- Shared helpers for the other row ops --------------------------------------


class TestSharedHelpers:
    def test_patch_values_only_named_columns(self, schema):
        problems = storage_mod._Problems(schema)
        values = storage_mod._patch_values(
            problems, 0, schema, {K_PRICE: "2.50", K_STATUS: None, K_NAME: None}
        )
        assert values == {K_PRICE: "2.5", K_STATUS: None}
        with pytest.raises(InvalidRowsError) as exc:
            problems.raise_if_any()
        assert [(p["column"], p["code"]) for p in exc.value.problems] == [("Name", "required")]

    def test_unique_check_excludes_touched_ids(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "SKU": "X"}, {"Name": "b", "SKU": "Y"}])
        with connect(table_dir) as conn:
            problems = storage_mod._Problems(schema)
            # A swap between rows 1 and 2 is fine against the final state.
            storage_mod._check_unique(
                conn, problems, schema, [(0, {K_SKU: "Y"}), (1, {K_SKU: "X"})], {1, 2}
            )
            assert not problems
            # Row 1 alone taking Y collides with untouched row 2.
            storage_mod._check_unique(conn, problems, schema, [(0, {K_SKU: "Y"})], {1})
            with pytest.raises(InvalidRowsError) as exc:
                problems.raise_if_any()
        assert exc.value.problems[0]["reason"] == "value 'Y' is already used by row _id 2."

    def test_row_images_full_stored_rows(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a", "Done": True}, {"Name": "b"}])
        with connect(table_dir) as conn:
            images = storage_mod._row_images(conn, schema, [2, 1, 2, 42])
        assert [i["_id"] for i in images] == [1, 2]
        assert images[0][K_DONE] == 1
        assert set(images[0]) == {"_id", "_created_at", "_updated_at", *(c.key for c in schema)}

    def test_write_txn_rolls_back(self, table_dir):
        with connect(table_dir) as conn:
            with pytest.raises(RuntimeError):
                with storage_mod._write_txn(conn):
                    conn.execute("DELETE FROM changes")
                    raise RuntimeError("x")
            assert not conn.in_transaction
        assert change_count(table_dir) == 1

    def test_utc_iso_fixed_width(self):
        local = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))
        assert storage_mod._utc_iso(local) == "2026-01-02T01:04:05.000000+00:00"


# --- Review fixes: guarded open, reads, errors ---------------------------------


class TestDoubleQuotedStrings:
    def test_dqs_off_on_every_connection(self, table_dir):
        with connect(table_dir) as conn:
            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                conn.execute('SELECT "c_0000000f" FROM rows')

    def test_dump_rows_with_extra_column_is_schema_mismatch(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "a"}])
        extra = schema + [Column(key="c_0000000f", name="Extra", type="text")]
        with pytest.raises(SchemaMismatchError) as exc:
            dump_rows(table_dir, extra)
        assert exc.value.code == "schema_mismatch"
        assert "c_0000000f" not in exc.value.message
        with pytest.raises(SchemaMismatchError):
            get_rows_by_ids(table_dir, extra, [1])


class TestBusy:
    def test_locked_table_at_begin_is_busy(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
        holder = sqlite3.connect(table_dir / TABLE_DB_NAME, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(TableStorageError) as exc:
                insert(table_dir, schema, [{"Name": "a"}])
            assert (exc.value.code, exc.value.message) == (
                "busy", "The table is busy; try again."
            )
            assert isinstance(exc.value.__cause__, sqlite3.OperationalError)
            # Reads still work in WAL mode while a writer holds the lock.
            assert row_count(table_dir) == 0
        finally:
            holder.close()
        assert insert(table_dir, schema, [{"Name": "a"}]).row_ids == [1]

    def test_locked_file_at_open_is_busy(self, table_dir, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
        holder = sqlite3.connect(table_dir / TABLE_DB_NAME, isolation_level=None)
        try:
            holder.execute("PRAGMA locking_mode=EXCLUSIVE")
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("DELETE FROM meta WHERE key = 'none'")
            holder.execute("COMMIT")
            with pytest.raises(TableStorageError) as exc:
                row_count(table_dir)
            assert exc.value.code == "busy"
        finally:
            holder.close()

    def test_opener_uses_patched_timeout(self, table_dir, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_DB_BUSY_TIMEOUT_S", 0.05)
        with connect(table_dir) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50

    def test_other_operational_errors_pass_through(self, table_dir):
        with connect(table_dir) as conn:
            with pytest.raises(sqlite3.OperationalError):
                with storage_mod._write_txn(conn):
                    conn.execute("SELECT * FROM no_such_table")


def test_side_file_rechecked_after_pragmas(table_dir, tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    target = tmp_path / "target"
    target.write_bytes(b"")
    side = table_dir / (TABLE_DB_NAME + "-journal")

    def planting_connect(path, *a, **kw):
        os.symlink(target, side)
        return real_connect(path, *a, **kw)

    monkeypatch.setattr(storage_mod.sqlite3, "connect", planting_connect)
    with pytest.raises(TableStorageError) as exc:
        row_count(table_dir)
    assert exc.value.code == "unsafe_path" and side.name in exc.value.message


def test_table_dir_that_is_a_file_refused(tmp_path):
    path = tmp_path / "t"
    path.write_text("x")
    with pytest.raises(TableStorageError) as exc:
        row_count(path)
    assert exc.value.code == "unsafe_path"


def test_constraint_failed_hides_column_key(table_dir, schema, monkeypatch, caplog):
    insert(table_dir, schema, [{"Name": "a", "SKU": "X"}])
    monkeypatch.setattr(storage_mod, "_check_unique", lambda *a, **kw: None)
    with caplog.at_level("WARNING", logger="chat.tables.storage"):
        with pytest.raises(TableStorageError) as exc:
            insert(table_dir, schema, [{"Name": "b", "SKU": "X"}])
    assert exc.value.code == "constraint_failed"
    assert K_SKU not in exc.value.message
    assert exc.value.message == "The write broke a table constraint; nothing was written."
    assert isinstance(exc.value.__cause__, sqlite3.IntegrityError)
    assert K_SKU in caplog.text


class TestChangeLogExtras:
    def test_too_large_hint(self, table_dir, monkeypatch):
        monkeypatch.setattr(storage_mod, "TABLE_CHANGE_MAX_BYTES", 10)
        with connect(table_dir) as conn:
            with pytest.raises(ChangeTooLargeError) as exc:
                append_change(
                    conn, now=NOW, source="ui:1", actor_user_id=None, kind="schema",
                    op="schema", summary="x", schema_before=1, schema_after=2,
                    row_ids=[], before=["x" * 20], after=[],
                    too_large_hint="Drop fewer columns at once.",
                )
        assert exc.value.message.endswith(" Drop fewer columns at once.")
        assert ChangeTooLargeError(99, 10).message.endswith(" Split it into smaller writes.")

    def test_size_bytes_counts_utf8(self, table_dir, schema):
        insert(table_dir, schema, [{"Name": "é" * 10}])
        change = get_change(table_dir, 2)
        expected = len(json.dumps(change["before"], ensure_ascii=False,
                                  separators=(",", ":")).encode()) + len(
            json.dumps(change["after"], ensure_ascii=False,
                       separators=(",", ":")).encode())
        assert change["size_bytes"] == expected
        ascii_len = len(json.dumps(change["after"], ensure_ascii=False,
                                   separators=(",", ":")))
        assert change["size_bytes"] == 2 + ascii_len + 10  # "[]" + 1 extra byte per é

    def test_list_changes_limit_clamped(self, table_dir, schema, monkeypatch):
        monkeypatch.setattr(storage_mod, "_LIST_CHANGES_MAX", 3)
        for name in "abcd":
            insert(table_dir, schema, [{"Name": name}])
        assert len(list_changes(table_dir, limit=1000)) == 3
        assert len(list_changes(table_dir, limit=0)) == 1
        assert len(list_changes(table_dir, limit=-5)) == 1
        assert storage_mod._LIST_CHANGES_MAX == 3

    def test_list_changes_max_is_200(self):
        assert storage_mod._LIST_CHANGES_MAX == 200

    @pytest.mark.parametrize("kw", [{"before_id": "5"}, {"before_id": True},
                                    {"before_id": 1.5}, {"limit": "10"}, {"limit": None}])
    def test_list_changes_bad_args(self, table_dir, kw):
        with pytest.raises(ValueError):
            list_changes(table_dir, **kw)


class TestNaiveNow:
    def test_insert_refuses_naive_now(self, table_dir, schema):
        with pytest.raises(ValueError, match="timezone-aware"):
            insert(table_dir, schema, [{"Name": "a"}], now=datetime(2026, 10, 1, 12))
        assert_nothing_written(table_dir, 0, 1)

    def test_create_refuses_naive_now(self, tmp_path, schema):
        path = tmp_path / "t"
        with pytest.raises(ValueError):
            create_table_db(path, table_id="t", title="T", schema=schema, source="ui:1",
                            actor_user_id=1, now=datetime(2026, 10, 1, 12))
        assert not path.exists()

    def test_append_change_refuses_naive_now(self, table_dir):
        with connect(table_dir) as conn:
            with pytest.raises(ValueError):
                _append(conn, datetime(2026, 10, 1, 12))
        assert change_count(table_dir) == 1


@pytest.mark.parametrize("value", ['{"a": 1}', '"text"', "5", "[5]", "[{\"key\": 1}]"])
def test_read_meta_bad_schema_json_is_corrupt(table_dir, value):
    raw_conn = sqlite3.connect(table_dir / TABLE_DB_NAME)
    raw_conn.execute("UPDATE meta SET value = ? WHERE key = 'schema'", (value,))
    raw_conn.commit()
    raw_conn.close()
    with pytest.raises(TableStorageError) as exc:
        read_meta(table_dir)
    assert exc.value.code == "corrupt_storage"


class TestCopyExtras:
    @pytest.mark.parametrize("history", [True, False])
    def test_copy_leaves_no_side_files(self, table_dir, schema, tmp_path, history,
                                       monkeypatch):
        insert(table_dir, schema, [{"Name": "a"}])
        out = tmp_path / "out"
        out.mkdir()
        dest = out / "copy.sqlite"
        seen: set[str] = set()
        modes: list[str] = []
        real_open = storage_mod._open_db_file

        class Watching:
            """Records the files next to dest after every statement."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, *a):
                try:
                    return self._conn.execute(*a)
                finally:
                    seen.update(p.name for p in out.iterdir())

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def watching_open(db, **kw):
            conn = real_open(db, **kw)
            if db != dest:
                return conn
            modes.append(conn.execute("PRAGMA journal_mode").fetchone()[0])
            return Watching(conn)

        monkeypatch.setattr(storage_mod, "_open_db_file", watching_open)
        copy_for_download(table_dir, dest, include_history=history)
        assert modes == ["delete"]
        assert not {n for n in seen if n.endswith(("-wal", "-shm"))}
        assert sorted(p.name for p in out.iterdir()) == ["copy.sqlite"]

    def test_failure_partway_removes_dest(self, table_dir, tmp_path, monkeypatch):
        dest = tmp_path / "copy.sqlite"
        real_open = storage_mod._open_db_file

        def failing_open(db, **kw):
            if db == dest:
                raise RuntimeError("boom")
            return real_open(db, **kw)

        monkeypatch.setattr(storage_mod, "_open_db_file", failing_open)
        with pytest.raises(RuntimeError):
            copy_for_download(table_dir, dest)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["tables"]
