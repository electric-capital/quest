"""The DuckDB analytics store (analytics/store.py): opening, the numbered
migration runner, and the schema contract the ledger writes against."""

import shutil
from datetime import datetime

import duckdb
import pytest

from analytics import store as store_mod
from analytics.ledger import SKILL_LOADS_COLUMNS, TOOL_CALLS_COLUMNS
from analytics.store import (
    MIGRATIONS_DIR,
    AnalyticsStore,
    apply_migrations,
    list_migrations,
    table_columns,
)


def test_migrations_are_numbered_contiguously_from_one():
    versions = [v for v, _, _ in list_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_apply_migrations_is_idempotent_and_recorded():
    conn = duckdb.connect(":memory:")
    first = apply_migrations(conn)
    assert first == [v for v, _, _ in list_migrations()]
    assert apply_migrations(conn) == []
    recorded = conn.execute("SELECT version, name FROM _migrations ORDER BY version").fetchall()
    assert recorded == [(v, name) for v, name, _ in list_migrations()]
    assert all(isinstance(r[0], datetime) for r in conn.execute(
        "SELECT applied_at FROM _migrations").fetchall())


def test_skill_loads_schema_matches_what_the_ledger_writes():
    """A migration that renames or drops a ledger column must change
    SKILL_LOADS_COLUMNS in the same PR, and vice versa."""
    store = AnalyticsStore(":memory:").open()
    with store.cursor() as cur:
        columns = table_columns(cur, "skill_loads")
    assert tuple(name for name, _ in columns) == SKILL_LOADS_COLUMNS
    types = dict(columns)
    assert types["created_at"] == "TIMESTAMP"
    assert types["user_id"] == "INTEGER"
    store.close()


def test_tool_calls_schema_matches_what_the_ledger_writes():
    store = AnalyticsStore(":memory:").open()
    with store.cursor() as cur:
        columns = table_columns(cur, "tool_calls")
    assert tuple(name for name, _ in columns) == TOOL_CALLS_COLUMNS
    types = dict(columns)
    assert types["created_at"] == "TIMESTAMP"
    assert types["dynamic"] == "BOOLEAN"
    assert types["user_id"] == "INTEGER"
    store.close()


def test_later_migration_preserves_earlier_rows(tmp_path):
    """Applying the migrations one release at a time keeps the data: a
    store created at version 1 gets a row, then the rest apply over it."""
    staged = tmp_path / "migrations"
    staged.mkdir()
    files = list_migrations()
    shutil.copy(files[0][2], staged / files[0][2].name)
    conn = duckdb.connect(str(tmp_path / "a.duckdb"))
    assert apply_migrations(conn, staged) == [1]
    conn.execute(
        "INSERT INTO _backfills VALUES ('probe', ?, 7)", [datetime(2026, 1, 1)],
    )
    for _, _, path in files[1:]:
        shutil.copy(path, staged / path.name)
    assert apply_migrations(conn, staged) == [v for v, _, _ in files[1:]]
    assert conn.execute("SELECT name, row_count FROM _backfills").fetchall() == [("probe", 7)]
    conn.close()


def test_failed_migration_rolls_back_and_applies_nothing_after(tmp_path):
    staged = tmp_path / "migrations"
    staged.mkdir()
    shutil.copy(list_migrations()[0][2], staged / "0001_foundation.sql")
    (staged / "0002_bad.sql").write_text(
        "CREATE TABLE probe (x INTEGER);\nCREATE TABLE _backfills (dup INTEGER);\n"
    )
    (staged / "0003_after.sql").write_text("CREATE TABLE after_bad (x INTEGER);\n")
    conn = duckdb.connect(":memory:")
    with pytest.raises(duckdb.Error):
        apply_migrations(conn, staged)
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert "probe" not in tables and "after_bad" not in tables
    assert conn.execute("SELECT max(version) FROM _migrations").fetchone()[0] == 1


def test_invalid_migration_names_fail_loudly(tmp_path):
    (tmp_path / "0001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "0001_dup.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="share version 1"):
        list_migrations(tmp_path)
    (tmp_path / "0001_dup.sql").unlink()
    (tmp_path / "2_bad-name.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="invalid name"):
        list_migrations(tmp_path)


def test_file_store_persists_across_reopen(tmp_path):
    path = tmp_path / "analytics.duckdb"
    store = AnalyticsStore(path).open()
    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_loads (created_at, conversation_id, user_id, skill_id, source) "
            "VALUES (?, 'c', 1, 's', 'composer')", [datetime(2026, 1, 1)],
        )
    store.close()
    assert not store.is_open
    with pytest.raises(RuntimeError):
        store.cursor()
    again = AnalyticsStore(path).open()
    with again.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM skill_loads").fetchone()[0] == 1
    again.close()


def test_open_store_installs_process_wide_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_current", None)
    store = store_mod.open_store(tmp_path / "a.duckdb")
    assert store_mod.get_store() is store and store.is_open
    store_mod.close_store()
    assert store_mod.get_store() is None and not store.is_open
    # Closing twice is harmless.
    store_mod.close_store()


def test_migrations_dir_is_the_packaged_one():
    assert MIGRATIONS_DIR.is_dir()
    assert any(MIGRATIONS_DIR.glob("*.sql"))
