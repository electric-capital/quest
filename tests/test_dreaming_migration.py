"""End-to-end test of migration b4e8d2c7a1f3 (dreaming).

Builds a database at the previous head in a temp data dir, seeds users, a
conversation, memories and the ``memory_suggestions`` rows the
``suggest_memory`` tool wrote, upgrades and checks: every existing
suggestion survives the table rebuild unchanged with ``source =
'conversation'`` and no dream run, ``conversation_id`` and ``content``
now accept NULL, the foreign-key actions (cascades and SET NULLs,
including the new one onto ``dream_runs``) fire, ``dream_runs`` carries
its indexes, the ORM reads and writes the migrated schema, the downgrade
restores the old shape while keeping the tool's rows, and a fresh
database migrates from base to head and matches the ORM.
"""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest

from config import encryption
from config.paths import PROJECT_ROOT
from tests.migration_support import (  # noqa: F401
    PASSWORD,
    columns,
    deployment,
    execute,
    metadata_drift,
    rows,
    run,
)

REV = "b4e8d2c7a1f3"
PREVIOUS = "7103d39f4efb"

TS = "2026-10-01 10:00:00.000000"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_info(db_path, table) -> dict:
    """name -> (type, notnull, default) for every column of ``table``."""
    return {r[1]: (r[2], r[3], r[4]) for r in rows(db_path, f"PRAGMA table_info({table})")}


def _fks(db_path, table) -> set[tuple]:
    """(from, table, to, on_delete) for every foreign key of ``table``."""
    return {
        (r[3], r[2], r[4], r[6])
        for r in rows(db_path, f"PRAGMA foreign_key_list({table})")
    }


def _named_indexes(db_path, table) -> dict:
    return {
        r[1]: [c[2] for c in rows(db_path, f"PRAGMA index_info({r[1]})")]
        for r in rows(db_path, f"PRAGMA index_list({table})")
        if r[3] == "c"
    }


def _tables(db_path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


OLD_SUGGESTION_COLUMNS = (
    "id", "user_id", "conversation_id", "content", "reasoning",
    "replaces_memory_id", "status", "memory_id", "created_at", "resolved_at",
)


def _seed(db_path) -> None:
    """Users 1 and 2, a conversation of each, two memories of user 1, and
    three suggestions as the tool wrote them: a pending replacement, an
    accepted one pointing at the memory it created, and user 2's."""
    conn = _connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
               "VALUES (?, ?, ?, ?, '{}', ?)")
        for uid, letter in ((1, "a"), (2, "b")):
            conn.execute(ins, (
                uid, f"{letter}@example.com", letter.upper(),
                encryption.encrypt_str(f"key-{letter}", "users.api_key"), TS,
            ))
        for cid, uid in (("c1", 1), ("c2", 2)):
            conn.execute(
                "INSERT INTO conversations (id, user_id, created_at, last_message_at) "
                "VALUES (?, ?, ?, ?)", (cid, uid, TS, TS),
            )
        mem_ins = ("INSERT INTO memories (id, user_id, content, created_at, archived) "
                   "VALUES (?, ?, ?, ?, 0)")
        conn.execute(mem_ins, ("m1", 1, "Prefers tea", TS))
        conn.execute(mem_ins, ("m2", 1, "Prefers green tea", TS))
        sug_ins = (
            "INSERT INTO memory_suggestions (id, user_id, conversation_id, content, "
            "reasoning, replaces_memory_id, status, memory_id, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(sug_ins, (
            "s1", 1, "c1", "Prefers green tea", "said so", "m1", "pending", None, TS, None,
        ))
        conn.execute(sug_ins, (
            "s2", 1, "c1", "Prefers green tea", None, None, "accepted", "m2", TS, TS,
        ))
        conn.execute(sug_ins, (
            "s3", 2, "c2", "Lives in Lisbon", None, None, "dismissed", None, TS, TS,
        ))
        conn.commit()
    finally:
        conn.close()


def _old_rows(db_path):
    cols = ", ".join(OLD_SUGGESTION_COLUMNS)
    return rows(db_path, f"SELECT {cols} FROM memory_suggestions ORDER BY id")


def test_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    path = PROJECT_ROOT / "alembic" / "versions" / f"{REV}_add_dreaming.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REV}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REV
    assert module.down_revision == PREVIOUS  # doc notes
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_upgrade_keeps_every_suggestion_and_relaxes_the_shape(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    before = _old_rows(db_path)
    assert not {"source", "dream_run_id"} & columns(db_path, "memory_suggestions")

    keyed.alembic("upgrade", REV)

    # Every row survived the rebuild, byte for byte, and reads as the
    # tool's own with no dream run.
    assert _old_rows(db_path) == before
    assert rows(
        db_path, "SELECT id, source, dream_run_id FROM memory_suggestions ORDER BY id",
    ) == [("s1", "conversation", None), ("s2", "conversation", None), ("s3", "conversation", None)]

    info = _table_info(db_path, "memory_suggestions")
    assert info["conversation_id"][1] == 0  # nullable now
    assert info["content"][1] == 0
    assert info["created_at"][1] == 1  # and this one no longer is
    assert info["source"] == ("VARCHAR(20)", 1, "'conversation'")
    assert info["dream_run_id"] == ("VARCHAR(36)", 0, None)
    assert info["status"] == ("VARCHAR(20)", 1, "'pending'")
    assert _fks(db_path, "memory_suggestions") == {
        ("user_id", "users", "id", "CASCADE"),
        ("conversation_id", "conversations", "id", "CASCADE"),
        ("replaces_memory_id", "memories", "id", "SET NULL"),
        ("memory_id", "memories", "id", "SET NULL"),
        ("dream_run_id", "dream_runs", "id", "SET NULL"),
    }
    assert _named_indexes(db_path, "memory_suggestions") == {
        "ix_memory_suggestions_user_id": ["user_id"],
        "ix_memory_suggestions_conversation_status": ["conversation_id", "status"],
    }

    assert "dream_runs" in _tables(db_path)
    assert _fks(db_path, "dream_runs") == {("user_id", "users", "id", "CASCADE")}
    assert _named_indexes(db_path, "dream_runs") == {
        "ix_dream_runs_user_id": ["user_id"],
        "ix_dream_runs_user_started": ["user_id", "started_at"],
    }

    # A raw insert naming none of the new columns gets the tool's source.
    execute(
        db_path,
        "INSERT INTO memory_suggestions (id, user_id, conversation_id, content, created_at) "
        "VALUES ('s-raw', 1, 'c1', 'fact', ?)", (TS,),
    )
    assert rows(
        db_path, "SELECT source, status, dream_run_id FROM memory_suggestions WHERE id = 's-raw'",
    ) == [("conversation", "pending", None)]


def test_foreign_key_actions(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)

    # A dream's rows: no conversation, one archive-only (no content).
    execute(
        db_path,
        "INSERT INTO dream_runs (id, user_id, trigger, status, memories_read, "
        "suggestions_created, started_at) VALUES ('r1', 1, 'scheduled', 'completed', 2, 2, ?)",
        (TS,),
    )
    execute(
        db_path,
        "INSERT INTO memory_suggestions (id, user_id, conversation_id, source, dream_run_id, "
        "content, replaces_memory_id, created_at) "
        "VALUES ('d1', 1, NULL, 'dream', 'r1', 'Prefers green tea (merged)', 'm1', ?)", (TS,),
    )
    execute(
        db_path,
        "INSERT INTO memory_suggestions (id, user_id, conversation_id, source, dream_run_id, "
        "content, replaces_memory_id, created_at) "
        "VALUES ('d2', 1, NULL, 'dream', 'r1', NULL, 'm2', ?)", (TS,),
    )

    conn = _connect(db_path)
    try:
        # Deleting the run nulls the pointer; the rows stay.
        conn.execute("DELETE FROM dream_runs WHERE id = 'r1'")
        assert conn.execute(
            "SELECT id, dream_run_id FROM memory_suggestions WHERE source = 'dream' ORDER BY id",
        ).fetchall() == [("d1", None), ("d2", None)]
        # Deleting a memory nulls the references, as before.
        conn.execute("DELETE FROM memories WHERE id = 'm1'")
        assert conn.execute(
            "SELECT id, replaces_memory_id FROM memory_suggestions WHERE id IN ('s1', 'd1') ORDER BY id",
        ).fetchall() == [("d1", None), ("s1", None)]
        # Deleting the conversation takes the tool's rows, not the dream's.
        conn.execute("DELETE FROM conversations WHERE id = 'c1'")
        assert conn.execute(
            "SELECT id FROM memory_suggestions WHERE user_id = 1 ORDER BY id",
        ).fetchall() == [("d1",), ("d2",)]
        # Deleting the user takes everything of theirs.
        conn.execute("DELETE FROM users WHERE id = 1")
        assert conn.execute(
            "SELECT id FROM memory_suggestions ORDER BY id",
        ).fetchall() == [("s3",)]
        conn.commit()
    finally:
        conn.close()


def test_orm_reads_and_writes_the_migrated_schema(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)

    import db.dream_run_store as run_store
    import db.memory_suggestion_store as store

    keyed.bind_orm([run_store, store])
    try:
        pending = run(store.list_pending_for_user(1))
        assert [(s["id"], s["source"], s["replaces_content"]) for s in pending] == [
            ("s1", "conversation", "Prefers tea"),
        ]
        dream = run(run_store.create_run(1, "manual"))
        archive = run(store.create_suggestion(
            1, None, None, reasoning="superseded", replaces_memory_id="m1",
            source="dream", dream_run_id=dream["id"],
        ))
        assert archive["content"] is None and archive["conversation_id"] is None
        accepted, memory = run(store.accept_suggestion(1, archive["id"]))
        assert memory is None and accepted["memory_id"] is None
        assert rows(db_path, "SELECT archived FROM memories WHERE id = 'm1'") == [(1,)]
        finished = run(run_store.finish_run(
            dream["id"], "completed", memories_read=2, suggestions_created=1,
        ))
        assert finished["status"] == "completed" and finished["finished_at"]
    finally:
        keyed.dispose()


def test_downgrade_restores_the_old_shape_and_keeps_the_tools_rows(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    before = _old_rows(db_path)
    keyed.alembic("upgrade", REV)
    execute(
        db_path,
        "INSERT INTO memory_suggestions (id, user_id, conversation_id, source, content, "
        "replaces_memory_id, created_at) VALUES ('d1', 1, NULL, 'dream', NULL, 'm1', ?)", (TS,),
    )

    keyed.alembic("downgrade", PREVIOUS)

    assert "dream_runs" not in _tables(db_path)
    assert not {"source", "dream_run_id"} & columns(db_path, "memory_suggestions")
    info = _table_info(db_path, "memory_suggestions")
    assert info["conversation_id"][1] == 1 and info["content"][1] == 1
    assert info["created_at"][1] == 0
    assert _old_rows(db_path) == before  # the dream's row is gone, the tool's stay
    assert _fks(db_path, "memory_suggestions") == {
        ("user_id", "users", "id", "CASCADE"),
        ("conversation_id", "conversations", "id", "CASCADE"),
        ("replaces_memory_id", "memories", "id", "SET NULL"),
        ("memory_id", "memories", "id", "SET NULL"),
    }


def test_fresh_database_matches_the_orm(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", "head")
    assert "dream_runs" in _tables(db_path)
    # (project_memory_suggestions has its own, older created_at drift.)
    mine = [
        d for d in metadata_drift(db_path)
        if "'dream_runs'" in d or "'memory_suggestions'" in d
    ]
    assert mine == []
