"""End-to-end test of migration c5e8a1d3f7b2 (AutoProject docs and tables).

Builds a database at the previous head, seeds a user doc and a user table
with a share each, upgrades and checks: ``docs`` and ``data_tables`` gain a
nullable ``autoproject_id`` with its ON DELETE CASCADE inline key and index,
without a table rebuild (every row and every share survives); deleting an
AutoProject removes its docs and tables and nothing else; the downgrade
deletes the AutoProject docs and tables and drops the column.
"""

from __future__ import annotations

import sqlite3

import pytest

from config import encryption
from tests.migration_support import (  # noqa: F401
    PASSWORD,
    columns,
    deployment,
    rows,
)

REV = "c5e8a1d3f7b2"
PREVIOUS = "4b57cca1158c"
TS = "2026-11-01 10:00:00.000000"
ARTIFACT_TABLES = ("docs", "data_tables")


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _fks(db_path, table) -> set[tuple]:
    return {(r[3], r[2], r[4], r[6]) for r in rows(db_path, f"PRAGMA foreign_key_list({table})")}


def _index_columns(db_path, name) -> list[str]:
    return [c[2] for c in rows(db_path, f"PRAGMA index_info({name})")]


def _insert_doc(conn, doc_id, title, autoproject_id=None) -> None:
    extra = ", autoproject_id" if autoproject_id is not None else ""
    marks = ", ?" if autoproject_id is not None else ""
    args = [doc_id, title, TS, TS] + ([autoproject_id] if autoproject_id is not None else [])
    conn.execute(
        "INSERT INTO docs (id, owner_id, title, description, mode, content_size, "
        "asset_count, require_approval, instructions, instructions_version, "
        f"memories_version, memory_count, created_at, updated_at{extra}) "
        f"VALUES (?, 1, ?, '', 'private', 0, 0, 0, '', 0, 0, 0, ?, ?{marks})",
        args,
    )


def _insert_table(conn, table_id, title, autoproject_id=None) -> None:
    extra = ", autoproject_id" if autoproject_id is not None else ""
    marks = ", ?" if autoproject_id is not None else ""
    args = [table_id, title, TS, TS] + ([autoproject_id] if autoproject_id is not None else [])
    conn.execute(
        "INSERT INTO data_tables (id, owner_id, title, description, mode, schema, "
        f"schema_version, row_count, require_approval, created_at, updated_at{extra}) "
        f"VALUES (?, 1, ?, '', 'private', '[]', 1, 0, 0, ?, ?{marks})",
        args,
    )


def _seed(db_path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, email, name, api_key, settings, created_at) "
            "VALUES (1, 'a@example.com', 'A', ?, '{}', ?)",
            (encryption.encrypt_str("key-a", "users.api_key"), TS),
        )
        conn.execute(
            "INSERT INTO autoprojects (id, user_id, title, created_at, updated_at, "
            "last_activity_at) VALUES ('ap1', 1, 'Research', ?, ?, ?)", (TS, TS, TS),
        )
        _insert_doc(conn, "d1", "Mine")
        _insert_table(conn, "t1", "Mine")
        conn.execute(
            "INSERT INTO doc_shares (doc_id, user_id, permission, created_at) "
            "VALUES ('d1', NULL, 'read', ?)", (TS,),
        )
        conn.execute(
            "INSERT INTO data_table_shares (table_id, user_id, permission, created_at) "
            "VALUES ('t1', NULL, 'read', ?)", (TS,),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_adds_the_columns_and_keeps_every_row(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    for table in ARTIFACT_TABLES:
        assert "autoproject_id" not in columns(db_path, table)

    keyed.alembic("upgrade", REV)

    for table in ARTIFACT_TABLES:
        assert "autoproject_id" in columns(db_path, table)
        assert ("autoproject_id", "autoprojects", "id", "CASCADE") in _fks(db_path, table)
        assert _index_columns(db_path, f"ix_{table}_autoproject_id") == ["autoproject_id"]
        assert rows(db_path, f"SELECT autoproject_id FROM {table}") == [(None,)]
    # No rebuild: the rows that reference docs and data_tables are intact.
    assert rows(db_path, "SELECT doc_id FROM doc_shares") == [("d1",)]
    assert rows(db_path, "SELECT table_id FROM data_table_shares") == [("t1",)]


def test_deleting_an_autoproject_cascades_to_its_docs_and_tables(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)
    conn = _connect(db_path)
    try:
        _insert_doc(conn, "d2", "Theirs", "ap1")
        _insert_table(conn, "t2", "Theirs", "ap1")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_doc(conn, "d3", "Orphan", "no-such-autoproject")
        conn.rollback()
        conn.execute("DELETE FROM autoprojects WHERE id = 'ap1'")
        conn.commit()
    finally:
        conn.close()
    assert rows(db_path, "SELECT id FROM docs") == [("d1",)]
    assert rows(db_path, "SELECT id FROM data_tables") == [("t1",)]


def test_downgrade_deletes_autoproject_artifacts_and_drops_the_column(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", REV)
    _seed(db_path)
    conn = _connect(db_path)
    try:
        _insert_doc(conn, "d2", "Theirs", "ap1")
        _insert_table(conn, "t2", "Theirs", "ap1")
        conn.commit()
    finally:
        conn.close()

    keyed.alembic("downgrade", PREVIOUS)

    for table in ARTIFACT_TABLES:
        assert "autoproject_id" not in columns(db_path, table)
    assert rows(db_path, "SELECT id FROM docs") == [("d1",)]
    assert rows(db_path, "SELECT id FROM data_tables") == [("t1",)]
    assert rows(db_path, "SELECT doc_id FROM doc_shares") == [("d1",)]
