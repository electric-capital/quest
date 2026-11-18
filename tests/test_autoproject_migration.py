"""End-to-end test of migration 4b57cca1158c (AutoProjects).

Builds a database at the previous head (``origin/main``'s head when the
AutoProjects branch was cut) in a temp data dir, seeds users and ordinary
conversations, upgrades and checks: the three AutoProject tables with their
columns, server defaults, foreign keys and indexes; ``conversations``
gains a nullable ``autoproject_id`` (with its ON DELETE CASCADE inline key)
without a table rebuild, so every existing row and every row referencing a
conversation survives; the foreign-key actions fire; the ORM store reads
and writes the migrated schema; the downgrade removes everything again,
deleting run conversations but keeping ordinary ones; a fresh database
migrates from base to head and matches both the ORM metadata and an
ORM-created schema for the four objects.

Runs in this process against the temp deployment set up by
``tests/migration_support.py``.
"""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest
from sqlalchemy import create_engine

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

REV = "4b57cca1158c"
PREVIOUS = "a3d9e5c7f2b4"

TS = "2026-11-01 10:00:00.000000"
NEW_TABLES = ("autoprojects", "autoproject_runs", "autoproject_brief_versions")


@pytest.fixture()
def keyed(deployment):
    # Earlier revisions in the chain encrypt stored secrets.
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_info(db_path, table) -> dict:
    """name -> (type, notnull, default, pk) for every column of ``table``."""
    return {
        r[1]: (r[2], r[3], r[4], r[5]) for r in rows(db_path, f"PRAGMA table_info({table})")
    }


def _fks(db_path, table) -> set[tuple]:
    """(from, table, to, on_delete) for every foreign key of ``table``."""
    return {(r[3], r[2], r[4], r[6]) for r in rows(db_path, f"PRAGMA foreign_key_list({table})")}


def _named_indexes(db_path, table) -> dict:
    """name -> (columns, unique) for every created index of ``table``."""
    return {
        r[1]: ([c[2] for c in rows(db_path, f"PRAGMA index_info({r[1]})")], r[2])
        for r in rows(db_path, f"PRAGMA index_list({table})")
        if r[3] == "c"
    }


def _tables(db_path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _seed(db_path) -> None:
    """Users 1 and 2, an ordinary conversation of each and a memory
    suggestion pointing at one (a row a ``conversations`` rebuild would
    cascade away)."""
    conn = _connect(db_path)
    try:
        for uid, letter in ((1, "a"), (2, "b")):
            conn.execute(
                "INSERT INTO users (id, email, name, api_key, settings, created_at) "
                "VALUES (?, ?, ?, ?, '{}', ?)",
                (uid, f"{letter}@example.com", letter.upper(),
                 encryption.encrypt_str(f"key-{letter}", "users.api_key"), TS),
            )
        for cid, uid in (("c1", 1), ("c2", 2)):
            conn.execute(
                "INSERT INTO conversations (id, user_id, created_at, last_message_at, origin) "
                "VALUES (?, ?, ?, ?, 'web')", (cid, uid, TS, TS),
            )
        conn.execute(
            "INSERT INTO memory_suggestions (id, user_id, conversation_id, content, status, "
            "created_at) VALUES ('s1', 1, 'c1', 'Prefers tea', 'pending', ?)", (TS,),
        )
        conn.commit()
    finally:
        conn.close()


def _conversations(db_path):
    return rows(db_path, "SELECT id, user_id, origin FROM conversations ORDER BY id")


def _seed_autoproject(db_path) -> None:
    """User 1's AutoProject ``ap1`` with a run on conversation ``rc1`` and a
    brief version, plus an abandoned run conversation ``rc2``."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO autoprojects (id, user_id, title, created_at, updated_at, "
            "last_activity_at) VALUES ('ap1', 1, 'Plan', ?, ?, ?)", (TS, TS, TS),
        )
        for cid in ("rc1", "rc2"):
            conn.execute(
                "INSERT INTO conversations (id, user_id, created_at, last_message_at, origin, "
                "autoproject_id) VALUES (?, 1, ?, ?, 'autoproject', 'ap1')", (cid, TS, TS),
            )
        conn.execute(
            "INSERT INTO autoproject_runs (id, autoproject_id, seq, conversation_id, prompt, "
            "status, started_at) VALUES ('r1', 'ap1', 1, 'rc1', 'Do it', 'running', ?)", (TS,),
        )
        conn.execute(
            "INSERT INTO autoproject_brief_versions (id, autoproject_id, version, content, "
            "source, created_at) VALUES ('v1', 'ap1', 1, 'brief', 'ui', ?)", (TS,),
        )
        conn.commit()
    finally:
        conn.close()


def test_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    paths = list((PROJECT_ROOT / "alembic" / "versions").glob(f"{REV}_*.py"))
    assert len(paths) == 1
    spec = importlib.util.spec_from_file_location(f"migration_{REV}", paths[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REV
    assert module.down_revision == PREVIOUS  # memories.archive_reason
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    chain = [r.revision for r in script.walk_revisions(base="base", head=heads[0])]
    assert REV in chain
    # Nothing else branches off the previous head.
    assert [r.revision for r in script.walk_revisions() if r.down_revision == PREVIOUS] == [REV]


def test_upgrade_adds_the_tables_and_keeps_every_row(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    before = _conversations(db_path)
    assert "autoproject_id" not in columns(db_path, "conversations")
    assert not set(NEW_TABLES) & _tables(db_path)

    keyed.alembic("upgrade", REV)

    assert set(NEW_TABLES) <= _tables(db_path)
    assert _conversations(db_path) == before
    assert rows(db_path, "SELECT autoproject_id FROM conversations") == [(None,), (None,)]
    assert rows(db_path, "SELECT id, conversation_id FROM memory_suggestions") == [("s1", "c1")]

    info = _table_info(db_path, "conversations")
    assert info["autoproject_id"] == ("VARCHAR(36)", 0, None, 0)
    assert ("autoproject_id", "autoprojects", "id", "CASCADE") in _fks(db_path, "conversations")
    assert _named_indexes(db_path, "conversations")["ix_conversations_autoproject_id"] == (
        ["autoproject_id"], 0,
    )

    assert _fks(db_path, "autoprojects") == {("user_id", "users", "id", "CASCADE")}
    assert _named_indexes(db_path, "autoprojects") == {
        "ix_autoprojects_user_id_archived_last_activity": (
            ["user_id", "archived", "last_activity_at"], 0,
        ),
    }
    assert _fks(db_path, "autoproject_runs") == {
        ("autoproject_id", "autoprojects", "id", "CASCADE"),
        ("conversation_id", "conversations", "id", "CASCADE"),
    }
    assert _named_indexes(db_path, "autoproject_runs") == {
        "ix_autoproject_runs_autoproject_id_seq": (["autoproject_id", "seq"], 1),
        "ix_autoproject_runs_autoproject_id_status": (["autoproject_id", "status"], 0),
        "ix_autoproject_runs_conversation_id": (["conversation_id"], 1),
    }
    assert columns(db_path, "autoproject_runs") >= {
        "context", "context_brief_version", "brief_conflict_version",
    }
    assert _fks(db_path, "autoproject_brief_versions") == {
        ("autoproject_id", "autoprojects", "id", "CASCADE"),
    }
    assert _named_indexes(db_path, "autoproject_brief_versions") == {
        "ix_autoproject_brief_versions_autoproject_id_version": (
            ["autoproject_id", "version"], 1,
        ),
    }

    # A raw insert naming only the required columns gets the defaults.
    execute(
        db_path,
        "INSERT INTO autoprojects (id, user_id, title, created_at, updated_at, last_activity_at) "
        "VALUES ('raw', 1, 'Raw', ?, ?, ?)", (TS, TS, TS),
    )
    assert rows(
        db_path,
        "SELECT title_is_default, brief, brief_version, brief_updated_at, model, "
        "thinking_effort, archived, next_run_seq, event_seq, unread_seq, read_seq "
        "FROM autoprojects WHERE id = 'raw'",
    ) == [(1, "", 0, None, None, None, 0, 1, 0, 0, 0)]
    execute(
        db_path,
        "INSERT INTO conversations (id, user_id, created_at, last_message_at, autoproject_id) "
        "VALUES ('rc', 1, ?, ?, 'raw')", (TS, TS),
    )
    execute(
        db_path,
        "INSERT INTO autoproject_runs (id, autoproject_id, seq, conversation_id, prompt, status, "
        "started_at) VALUES ('rr', 'raw', 1, 'rc', 'p', 'running', ?)", (TS,),
    )
    assert rows(
        db_path,
        "SELECT summary, result, notes, handoff_missing, error, context, "
        "context_brief_version, brief_conflict_version FROM autoproject_runs",
    ) == [(None, None, None, 0, None, None, None, None)]


def test_constraints_and_foreign_key_actions(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)
    _seed_autoproject(db_path)

    conn = _connect(db_path)
    try:
        # (autoproject_id, seq) and conversation_id are unique.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO autoproject_runs (id, autoproject_id, seq, conversation_id, prompt, "
                "status, started_at) VALUES ('dup', 'ap1', 1, 'rc2', 'p', 'running', ?)", (TS,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO autoproject_runs (id, autoproject_id, seq, conversation_id, prompt, "
                "status, started_at) VALUES ('dup', 'ap1', 2, 'rc1', 'p', 'running', ?)", (TS,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO autoproject_brief_versions (id, autoproject_id, version, content, "
                "source, created_at) VALUES ('v2', 'ap1', 1, 'x', 'ui', ?)", (TS,),
            )
        # A conversation cannot point at a missing AutoProject.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO conversations (id, user_id, created_at, last_message_at, "
                "autoproject_id) VALUES ('bad', 1, ?, ?, 'nope')", (TS, TS),
            )

        # Deleting a run's conversation takes the run.
        conn.execute("DELETE FROM conversations WHERE id = 'rc1'")
        assert conn.execute("SELECT COUNT(*) FROM autoproject_runs").fetchone() == (0,)
        conn.execute(
            "INSERT INTO conversations (id, user_id, created_at, last_message_at, autoproject_id) "
            "VALUES ('rc3', 1, ?, ?, 'ap1')", (TS, TS),
        )
        conn.execute(
            "INSERT INTO autoproject_runs (id, autoproject_id, seq, conversation_id, prompt, "
            "status, started_at) VALUES ('r3', 'ap1', 3, 'rc3', 'p', 'completed', ?)", (TS,),
        )
        # Deleting the AutoProject takes its runs, versions and conversations,
        # and nothing else.
        conn.execute("DELETE FROM autoprojects WHERE id = 'ap1'")
        for table in ("autoproject_runs", "autoproject_brief_versions"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("SELECT id FROM conversations ORDER BY id").fetchall() == [
            ("c1",), ("c2",),
        ]
        assert conn.execute("SELECT id FROM memory_suggestions").fetchall() == [("s1",)]
        conn.commit()
    finally:
        conn.close()

    # Deleting the user takes every AutoProject row of theirs.
    _seed_autoproject(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM users WHERE id = 1")
        for table in NEW_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("SELECT id FROM conversations").fetchall() == [("c2",)]
        conn.commit()
    finally:
        conn.close()


def test_orm_reads_and_writes_the_migrated_schema(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", "head")

    import db.autoproject_store as store

    keyed.bind_orm([store])
    try:
        ap = run(store.create_autoproject(1, title="Through the ORM", title_is_default=True))
        execute(
            db_path,
            "INSERT INTO conversations (id, user_id, created_at, last_message_at, origin, "
            "autoproject_id) VALUES ('orm-c', 1, ?, ?, 'autoproject', ?)", (TS, TS, ap["id"]),
        )
        started = run(store.allocate_run(ap["id"], conversation_id="orm-c", prompt="Go"))
        assert started["seq"] == 1
        state = run(store.set_brief(
            ap["id"], "state", expected_brief_version=0, source=f"run:{started['id']}",
        ))
        assert state["brief_version"] == 1
        run(store.freeze_run_context(started["id"], context="ctx", brief_version=0))
        run(store.save_run_entry(
            started["id"], summary="s", result="r", notes="n", brief_conflict_version=1,
        ))
        done = run(store.complete_run(started["id"], summary="s", result="r", notes="n"))
        assert (done["status"], done["context_brief_version"], done["brief_conflict_version"]) == (
            "completed", 0, 1,
        )
        assert [v["source"] for v in run(store.list_brief_versions(ap["id"]))] == [
            f"run:{started['id']}",
        ]
        assert run(store.delete_autoproject(1, ap["id"])) == ["orm-c"]
        assert rows(db_path, "SELECT id FROM conversations ORDER BY id") == [("c1",), ("c2",)]
    finally:
        keyed.dispose()


def test_downgrade_removes_everything_and_keeps_ordinary_chats(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    before = _conversations(db_path)
    keyed.alembic("upgrade", REV)
    _seed_autoproject(db_path)

    keyed.alembic("downgrade", PREVIOUS)

    assert not set(NEW_TABLES) & _tables(db_path)
    assert "autoproject_id" not in columns(db_path, "conversations")
    assert "ix_conversations_autoproject_id" not in _named_indexes(db_path, "conversations")
    assert all(fk[1] != "autoprojects" for fk in _fks(db_path, "conversations"))
    # The run conversations are gone, the ordinary ones and their
    # dependents stay.
    assert _conversations(db_path) == before
    assert rows(db_path, "SELECT id, conversation_id FROM memory_suggestions") == [("s1", "c1")]

    # And it upgrades again cleanly.
    keyed.alembic("upgrade", REV)
    assert set(NEW_TABLES) <= _tables(db_path)


def _orm_schema(tmp_path):
    """A database created from the ORM metadata (what the models declare)."""
    from db.models import Base

    path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


def test_fresh_database_matches_the_orm(keyed, tmp_path):
    db_path = keyed.db_path
    keyed.alembic("upgrade", "head")
    assert set(NEW_TABLES) <= _tables(db_path)

    # Autogenerate reports nothing for the new tables. The inline key on
    # conversations.autoproject_id is the one known exception: reflection
    # cannot read the ON DELETE of an inline clause, so alembic/env.py
    # (_INLINE_FKS) skips it for autogenerate; it is checked from PRAGMA
    # below instead.
    mine = [
        d for d in metadata_drift(db_path)
        if "autoproject" in d and "'conversations'" not in d
    ]
    assert mine == []
    conv_drift = [d for d in metadata_drift(db_path) if "autoproject_id" in d
                  and "'conversations'" in d]
    assert all(d.startswith(("('remove_fk'", "('add_fk'")) for d in conv_drift)

    # Column for column (types, NOT NULL, primary keys), keys and indexes
    # equal an ORM-created schema.
    orm = _orm_schema(tmp_path)
    for table in NEW_TABLES:
        migrated = {k: (v[0], v[1], v[3]) for k, v in _table_info(db_path, table).items()}
        declared = {k: (v[0], v[1], v[3]) for k, v in _table_info(orm, table).items()}
        assert migrated == declared, table
        assert _fks(db_path, table) == _fks(orm, table), table
        assert _named_indexes(db_path, table) == _named_indexes(orm, table), table
    conv_migrated = _table_info(db_path, "conversations")["autoproject_id"]
    conv_declared = _table_info(orm, "conversations")["autoproject_id"]
    assert conv_migrated[:2] == conv_declared[:2]
    assert {fk for fk in _fks(db_path, "conversations") if fk[0] == "autoproject_id"} == {
        fk for fk in _fks(orm, "conversations") if fk[0] == "autoproject_id"
    } == {("autoproject_id", "autoprojects", "id", "CASCADE")}
    assert _named_indexes(db_path, "conversations")["ix_conversations_autoproject_id"] == (
        _named_indexes(orm, "conversations")["ix_conversations_autoproject_id"]
    )
