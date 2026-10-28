"""End-to-end test of migration 7103d39f4efb (doc notes).

Builds a database at the previous head in a temp data dir, seeds users, a
project, a user doc and a project doc with per-user and everyone shares,
upgrades and checks: existing docs get empty notes (empty instructions,
both versions 0, no memories) with every other value unchanged, every
share survives (the columns are added in place, never by a table rebuild),
the new columns and tables carry the declared defaults, foreign keys and
indexes, the foreign key actions fire, the ORM reads and writes the
migrated schema, and the downgrade drops it all while keeping every doc and
share. A fresh database migrates from base to head and matches the ORM.

Alembic, the seeding and the ORM read-back run in this process against the
temp deployment set up by ``tests/migration_support.py``.
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

REV = "7103d39f4efb"
PREVIOUS = "a9d3e7f1c5b2"

NEW_DOC_COLUMNS = {
    "instructions", "instructions_version", "instructions_updated_at",
    "instructions_updated_by", "memories_version", "memory_count",
}
NEW_TABLES = {"doc_memories", "doc_note_suggestions"}
NEW_INDEXES = {
    "doc_memories": {
        "ix_doc_memories_doc_id_created_at": ["doc_id", "created_at"],
    },
    "doc_note_suggestions": {
        "ix_doc_note_suggestions_conversation_status": ["conversation_id", "status"],
        "ix_doc_note_suggestions_doc_status": ["doc_id", "status"],
        "ix_doc_note_suggestions_user_status": ["user_id", "status"],
    },
}

TS = "2026-10-01 10:00:00.000000"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _connect(db_path) -> sqlite3.Connection:
    """A raw connection that enforces foreign keys, as the app's does."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_info(db_path, table) -> dict:
    """name -> (type, notnull, default) for every column of ``table``."""
    return {
        r[1]: (r[2], r[3], r[4])
        for r in rows(db_path, f"PRAGMA table_info({table})")
    }


def _fks(db_path, table) -> set[tuple]:
    """(from, table, to, on_delete) for every foreign key of ``table``."""
    return {
        (r[3], r[2], r[4], r[6])
        for r in rows(db_path, f"PRAGMA foreign_key_list({table})")
    }


def _named_indexes(db_path, table) -> dict:
    """name -> columns of every explicitly created index of ``table``."""
    return {
        r[1]: [c[2] for c in rows(db_path, f"PRAGMA index_info({r[1]})")]
        for r in rows(db_path, f"PRAGMA index_list({table})")
        if r[3] == "c"
    }


def _tables(db_path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _seed(db_path) -> None:
    """Users 1 (owner), 2 (share recipient) and 3 (owns nothing, no
    shares), a project of user 1, a user doc and a project doc, a
    conversation of user 1, and the shares the docs could have had."""
    conn = _connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
               "VALUES (?, ?, ?, ?, '{}', ?)")
        for uid, letter in ((1, "a"), (2, "b"), (3, "c")):
            conn.execute(ins, (
                uid, f"{letter}@example.com", letter.upper(),
                encryption.encrypt_str(f"key-{letter}", "users.api_key"), TS,
            ))
        conn.execute(
            "INSERT INTO projects (id, user_id, name, created_at, public) "
            "VALUES ('p1', 1, 'Launch', ?, 1)", (TS,),
        )
        doc_ins = (
            "INSERT INTO docs (id, owner_id, project_id, title, description, mode, "
            "content_size, asset_count, last_write_source, created_at, updated_at, "
            "require_approval) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(doc_ins, (
            "d-user", 1, None, "Plan", "Quarter plan", "private", 120, 2, "ui",
            TS, "2026-10-02 09:30:00.000000", 1,
        ))
        conn.execute(doc_ins, (
            "d-proj", 1, "p1", "Spec", "", "public", 40, 0, "conversation:c1",
            TS, TS, 0,
        ))
        share_ins = (
            "INSERT INTO doc_shares (id, doc_id, user_id, permission, created_at) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        conn.execute(share_ins, (1, "d-user", 2, "write", TS))
        conn.execute(share_ins, (2, "d-user", None, "read", TS))
        conn.execute(share_ins, (3, "d-proj", 2, "read", TS))
        conn.execute(
            "INSERT INTO conversations (id, user_id, created_at, last_message_at) "
            "VALUES ('c1', 1, ?, ?)", (TS, TS),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(db_path, old_doc_columns) -> dict:
    cols = ", ".join(sorted(old_doc_columns))
    return {
        "docs": rows(db_path, f"SELECT {cols} FROM docs ORDER BY id"),
        "doc_shares": rows(db_path, "SELECT * FROM doc_shares ORDER BY id"),
    }


def _at_previous_seeded(dep):
    """Migrate to the previous head, seed, and return the old docs columns
    with a snapshot of the docs and doc_shares rows."""
    dep.alembic("upgrade", PREVIOUS)
    _seed(dep.db_path)
    old_doc_columns = columns(dep.db_path, "docs")
    assert not old_doc_columns & NEW_DOC_COLUMNS
    return old_doc_columns, _snapshot(dep.db_path, old_doc_columns)


def test_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    path = PROJECT_ROOT / "alembic" / "versions" / f"{REV}_add_doc_notes.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REV}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REV
    assert module.down_revision == PREVIOUS  # add thinking_effort to conversations
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_upgrade_gives_existing_docs_empty_notes(keyed):
    db_path = keyed.db_path
    old_doc_columns, before = _at_previous_seeded(keyed)

    keyed.alembic("upgrade", REV)

    # Every existing doc starts with no notes; nothing else moved.
    assert rows(
        db_path,
        "SELECT id, instructions, instructions_version, instructions_updated_at, "
        "instructions_updated_by, memories_version, memory_count "
        "FROM docs ORDER BY id",
    ) == [
        ("d-proj", "", 0, None, None, 0, 0),
        ("d-user", "", 0, None, None, 0, 0),
    ]
    assert _snapshot(db_path, old_doc_columns) == before
    assert len(before["doc_shares"]) == 3  # every share survived

    # Declared types, NOT NULL and server defaults of the new docs columns.
    info = _table_info(db_path, "docs")
    assert {n: info[n] for n in NEW_DOC_COLUMNS} == {
        "instructions": ("TEXT", 1, "''"),
        "instructions_version": ("INTEGER", 1, "'0'"),
        "instructions_updated_at": ("DATETIME", 0, None),
        "instructions_updated_by": ("INTEGER", 0, None),
        "memories_version": ("INTEGER", 1, "'0'"),
        "memory_count": ("INTEGER", 1, "'0'"),
    }
    assert _fks(db_path, "docs") == {
        ("owner_id", "users", "id", "CASCADE"),
        ("project_id", "projects", "id", "CASCADE"),
        ("instructions_updated_by", "users", "id", "SET NULL"),
    }

    # The two new tables, their foreign keys and their indexes.
    assert NEW_TABLES <= _tables(db_path)
    assert _fks(db_path, "doc_memories") == {
        ("doc_id", "docs", "id", "CASCADE"),
        ("created_by", "users", "id", "SET NULL"),
        ("updated_by", "users", "id", "SET NULL"),
    }
    assert _fks(db_path, "doc_note_suggestions") == {
        ("user_id", "users", "id", "CASCADE"),
        ("doc_id", "docs", "id", "CASCADE"),
        ("conversation_id", "conversations", "id", "CASCADE"),
        ("replaces_memory_id", "doc_memories", "id", "SET NULL"),
        ("memory_id", "doc_memories", "id", "SET NULL"),
    }
    for table, expected in NEW_INDEXES.items():
        assert _named_indexes(db_path, table) == expected
    memories = _table_info(db_path, "doc_memories")
    assert {n: memories[n][1] for n in memories} == {
        "id": 1, "doc_id": 1, "content": 1, "source": 1, "created_by": 0,
        "updated_by": 0, "created_at": 1, "updated_at": 1,
    }
    suggestions = _table_info(db_path, "doc_note_suggestions")
    assert {n: suggestions[n][1] for n in suggestions} == {
        "id": 1, "user_id": 1, "doc_id": 1, "conversation_id": 1, "kind": 1,
        "content": 1, "reasoning": 0, "base_instructions_version": 0,
        "replaces_memory_id": 0, "status": 1, "memory_id": 0, "created_at": 1,
        "resolved_at": 0,
    }
    assert suggestions["status"] == ("VARCHAR(20)", 1, "'pending'")

    # A raw insert that names none of the new columns gets the defaults.
    execute(
        db_path,
        "INSERT INTO docs (id, owner_id, title, mode, content_size, created_at, "
        "updated_at) VALUES ('d-raw', 1, 'Raw', 'private', 0, ?, ?)", (TS, TS),
    )
    assert rows(
        db_path,
        "SELECT instructions, instructions_version, instructions_updated_at, "
        "instructions_updated_by, memories_version, memory_count "
        "FROM docs WHERE id = 'd-raw'",
    ) == [("", 0, None, None, 0, 0)]
    # So does a raw suggestion insert that leaves out the status.
    execute(
        db_path,
        "INSERT INTO doc_note_suggestions (id, user_id, doc_id, conversation_id, "
        "kind, content, created_at) VALUES ('s-raw', 1, 'd-raw', 'c1', 'memory', "
        "'fact', ?)", (TS,),
    )
    assert rows(
        db_path, "SELECT status FROM doc_note_suggestions WHERE id = 's-raw'",
    ) == [("pending",)]


def test_foreign_key_actions(keyed):
    db_path = keyed.db_path
    _at_previous_seeded(keyed)
    keyed.alembic("upgrade", REV)

    conn = _connect(db_path)
    try:
        mem_ins = (
            "INSERT INTO doc_memories (id, doc_id, content, source, created_by, "
            "updated_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        sug_ins = (
            "INSERT INTO doc_note_suggestions (id, user_id, doc_id, "
            "conversation_id, kind, content, replaces_memory_id, memory_id, "
            "created_at) VALUES (?, ?, ?, 'c1', ?, ?, ?, ?, ?)"
        )
        conn.execute(
            "UPDATE docs SET instructions = 'USD thousands', instructions_version = 1, "
            "instructions_updated_at = ?, instructions_updated_by = 3 "
            "WHERE id = 'd-user'", (TS,),
        )
        conn.execute(mem_ins, ("m1", "d-user", "Board reads it Fridays", "ui:3", 3, 3, TS, TS))
        conn.execute(mem_ins, ("m2", "d-user", "Numbers from finance", "ui:1", 1, 1, TS, TS))
        conn.execute(mem_ins, ("m3", "d-proj", "Public launch doc", "ui:1", 1, 1, TS, TS))
        conn.execute(sug_ins, ("s1", 1, "d-user", "memory", "Board reads it Mondays", "m1", "m1", TS))
        conn.execute(sug_ins, ("s2", 1, "d-user", "instructions", "Newest first", None, None, TS))
        conn.execute(sug_ins, ("s3", 1, "d-proj", "memory", "Launch is in May", "m3", None, TS))
        # A suggestion reviewed by user 2, to see the reviewer cascade alone.
        conn.execute(sug_ins, ("s4", 2, "d-proj", "memory", "Ask B first", None, None, TS))
        conn.commit()

        # Deleting a user who wrote notes nulls the author columns only.
        conn.execute("DELETE FROM users WHERE id = 3")
        conn.commit()
        assert conn.execute(
            "SELECT id, instructions, instructions_updated_by FROM docs WHERE id = 'd-user'"
        ).fetchall() == [("d-user", "USD thousands", None)]
        assert conn.execute(
            "SELECT id, created_by, updated_by FROM doc_memories ORDER BY id"
        ).fetchall() == [("m1", None, None), ("m2", 1, 1), ("m3", 1, 1)]
        assert conn.execute("SELECT count(*) FROM doc_shares").fetchone() == (3,)

        # Deleting a memory nulls the suggestion columns pointing at it.
        conn.execute("DELETE FROM doc_memories WHERE id = 'm1'")
        conn.commit()
        assert conn.execute(
            "SELECT id, replaces_memory_id, memory_id FROM doc_note_suggestions ORDER BY id"
        ).fetchall() == [("s1", None, None), ("s2", None, None), ("s3", "m3", None), ("s4", None, None)]

        # Deleting the reviewer drops their suggestions only.
        conn.execute("DELETE FROM users WHERE id = 2")
        conn.commit()
        assert [r[0] for r in conn.execute(
            "SELECT id FROM doc_note_suggestions ORDER BY id"
        )] == ["s1", "s2", "s3"]

        # Deleting a doc drops its memories and suggestions, not another doc's.
        conn.execute("DELETE FROM docs WHERE id = 'd-user'")
        conn.commit()
        assert conn.execute("SELECT id, doc_id FROM doc_memories").fetchall() == [("m3", "d-proj")]
        assert conn.execute(
            "SELECT id, doc_id, replaces_memory_id FROM doc_note_suggestions"
        ).fetchall() == [("s3", "d-proj", "m3")]

        # Deleting the conversation drops its suggestions; memories stay.
        conn.execute("DELETE FROM conversations WHERE id = 'c1'")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM doc_note_suggestions").fetchone() == (0,)
        assert conn.execute("SELECT id FROM doc_memories").fetchall() == [("m3",)]
    finally:
        conn.close()


def _orm_roundtrip(db_path) -> dict:
    """Read docs and write one memory and one suggestion through the ORM."""
    from sqlalchemy import exc, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import undefer
    from sqlalchemy.pool import NullPool

    from db.models import Doc, DocMemory, DocNoteSuggestion

    async def _go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            out = {}
            async with factory() as db:
                docs = (await db.execute(select(Doc).order_by(Doc.id))).scalars().all()
                out["listed"] = [
                    (d.id, d.has_instructions, d.instructions_version,
                     d.memories_version, d.memory_count)
                    for d in docs
                ]
                # The text is never loaded by a plain select, and touching
                # it raises instead of lazy-loading.
                try:
                    docs[0].instructions
                    out["raised"] = False
                except exc.InvalidRequestError:
                    out["raised"] = True
                loaded = (await db.execute(
                    select(Doc).options(undefer(Doc.instructions)).order_by(Doc.id)
                )).scalars().all()
                out["undeferred"] = [(d.id, d.instructions) for d in loaded]

                memory = DocMemory(doc_id="d-user", content="Fact", source="ui:1", created_by=1)
                db.add(memory)
                await db.flush()
                suggestion = DocNoteSuggestion(
                    user_id=1, doc_id="d-user", conversation_id="c1", kind="memory",
                    content="Another fact", replaces_memory_id=memory.id,
                )
                db.add(suggestion)
                await db.commit()
                out["memory"] = (
                    len(memory.id), memory.created_at is not None,
                    memory.updated_at is not None, memory.updated_by,
                )
                out["suggestion"] = (len(suggestion.id), suggestion.status, suggestion.memory_id)
            return out
        finally:
            await engine.dispose()

    return run(_go())


def test_orm_reads_and_writes_the_migrated_schema(keyed):
    db_path = keyed.db_path
    _at_previous_seeded(keyed)
    keyed.alembic("upgrade", REV)
    execute(db_path, "UPDATE docs SET instructions = 'Newest first' WHERE id = 'd-user'")

    out = _orm_roundtrip(db_path)

    assert out["listed"] == [("d-proj", False, 0, 0, 0), ("d-user", True, 0, 0, 0)]
    assert out["raised"] is True
    assert out["undeferred"] == [("d-proj", ""), ("d-user", "Newest first")]
    assert out["memory"] == (36, True, True, None)
    assert out["suggestion"] == (36, "pending", None)
    # The ORM write left the doc row alone (the store bumps the counters).
    assert rows(
        db_path, "SELECT updated_at, memories_version, memory_count FROM docs WHERE id = 'd-user'",
    ) == [("2026-10-02 09:30:00.000000", 0, 0)]


def test_downgrade_drops_notes_and_keeps_docs_and_shares(keyed):
    db_path = keyed.db_path
    old_doc_columns, before = _at_previous_seeded(keyed)
    keyed.alembic("upgrade", REV)

    # Give the doc some notes the downgrade has to drop.
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE docs SET instructions = 'Newest first', instructions_version = 2, "
            "instructions_updated_at = ?, instructions_updated_by = 2, "
            "memories_version = 1, memory_count = 1 WHERE id = 'd-user'", (TS,),
        )
        conn.execute(
            "INSERT INTO doc_memories (id, doc_id, content, source, created_by, "
            "updated_by, created_at, updated_at) "
            "VALUES ('m1', 'd-user', 'Fact', 'ui:2', 2, 2, ?, ?)", (TS, TS),
        )
        conn.execute(
            "INSERT INTO doc_note_suggestions (id, user_id, doc_id, conversation_id, "
            "kind, content, memory_id, status, created_at) "
            "VALUES ('s1', 1, 'd-user', 'c1', 'memory', 'Fact', 'm1', 'accepted', ?)",
            (TS,),
        )
        conn.commit()
    finally:
        conn.close()

    keyed.alembic("downgrade", PREVIOUS)

    assert columns(db_path, "docs") == old_doc_columns
    assert not NEW_TABLES & _tables(db_path)
    assert _fks(db_path, "docs") == {
        ("owner_id", "users", "id", "CASCADE"),
        ("project_id", "projects", "id", "CASCADE"),
    }
    assert _snapshot(db_path, old_doc_columns) == before

    # And upgrades again cleanly, back to empty notes.
    keyed.alembic("upgrade", "head")
    assert rows(
        db_path,
        "SELECT id, instructions, instructions_version, instructions_updated_by, "
        "memories_version, memory_count FROM docs ORDER BY id",
    ) == [("d-proj", "", 0, None, 0, 0), ("d-user", "", 0, None, 0, 0)]
    assert rows(db_path, "SELECT count(*) FROM doc_memories") == [(0,)]
    assert _snapshot(db_path, old_doc_columns) == before


def test_fresh_database_matches_the_orm(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", "head")

    assert NEW_TABLES <= _tables(db_path)
    assert NEW_DOC_COLUMNS <= columns(db_path, "docs")

    names = (*NEW_TABLES, *NEW_DOC_COLUMNS)
    mine = [d for d in metadata_drift(db_path) if any(n in d for n in names)]
    # SQLite only lets ALTER TABLE add a foreign key inline on the column
    # (``REFERENCES users(id) ON DELETE SET NULL``), and SQLAlchemy's
    # SQLite reflection parses ON DELETE from table-level ``FOREIGN KEY``
    # clauses only, so autogenerate sees this one FK without its ondelete
    # and proposes re-creating it. PRAGMA foreign_key_list (asserted in the
    # upgrade test) and test_foreign_key_actions show the database has
    # SET NULL.
    fk_pair = [
        d for d in mine
        if "instructions_updated_by" in d and d.startswith(("('add_fk'", "('remove_fk'"))
    ]
    assert len(fk_pair) == 2
    (added,) = [d for d in fk_pair if d.startswith("('add_fk'")]
    (removed,) = [d for d in fk_pair if d.startswith("('remove_fk'")]
    assert "ondelete='SET NULL'" in added and "ondelete" not in removed
    assert [d for d in mine if d not in fk_pair] == []
