"""End-to-end test of migration c5a9e3f7d2b1 (guides become skills).

Builds a database at the previous head in a temp data dir, seeds users
with guides (a default with content, an empty default, name collisions
with existing skills, a guide attached to a scheduled routine), upgrades
and checks: every guide with content is a private skill of its owner with
the same content and none is auto-loaded, each owner has one
``guides_converted`` notification naming the skills and the routines that
lost a guide, users without convertible guides have none, the ``guides``
table and ``routines.guide_id`` are gone while every routine, schedule,
routine skill auto-load and conversation link survives with its foreign
keys still enforced, the ORM stores read and write the result, the
downgrade restores the empty schema, and a fresh database migrates from
base to head and matches the ORM.
"""

from __future__ import annotations

import importlib.util
import json
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

REV = "c5a9e3f7d2b1"
PREVIOUS = "b4e8d2c7a1f3"

TS = "2026-10-01 10:00:00.000000"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tables(db_path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


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


def _seed(db_path) -> None:
    """Three users.

    User 1 (a): a default guide with content, the guides "Analyst" and
    "Writer", and an empty guide; already owns skills named "Analyst" and
    "Analyst (converted)". Two routines in project p1: r1 with the Analyst
    guide, a schedule, a routine skill auto-load and a conversation; r2
    with no guide.
    User 2 (b): only the empty default guide every user was given.
    User 3 (c): a guide named "Analyst" too (names are per user), and
    routine r3 with the EMPTY guide g-c-empty attached.
    """
    conn = _connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
               "VALUES (?, ?, ?, ?, '{}', ?)")
        for uid, letter in ((1, "a"), (2, "b"), (3, "c")):
            conn.execute(ins, (
                uid, f"{letter}@example.com", letter.upper(),
                encryption.encrypt_str(f"key-{letter}", "users.api_key"), TS,
            ))
        guide_ins = ("INSERT INTO guides (id, user_id, name, content, is_default, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)")
        for guide in (
            ("g-a-default", 1, "Default", "Always answer in English.", 1),
            ("g-a-analyst", 1, "Analyst", "Lead with the number.", 0),
            ("g-a-writer", 1, "Writer", "Short sentences.\n\nNo jargon.", 0),
            ("g-a-empty", 1, "Blank", "  \n\t", 0),
            ("g-b-default", 2, "Default", "", 1),
            ("g-c-analyst", 3, "Analyst", "Cite sources.", 0),
            ("g-c-empty", 3, "Empty", "", 0),
        ):
            conn.execute(guide_ins, (*guide, TS))
        skill_ins = ("INSERT INTO skills (id, creator_id, project_id, name, description, "
                     "content, visibility, created_at) VALUES (?, ?, NULL, ?, '', ?, 'private', ?)")
        conn.execute(skill_ins, ("s-old-1", 1, "Analyst", "an older skill", TS))
        conn.execute(skill_ins, ("s-old-2", 1, "Analyst (converted)", "another", TS))
        project_ins = ("INSERT INTO projects (id, user_id, name, guide, public, archived, "
                       "created_at) VALUES (?, ?, ?, '', 0, 0, ?)")
        conn.execute(project_ins, ("p1", 1, "Reports", TS))
        conn.execute(project_ins, ("p3", 3, "Research", TS))
        routine_ins = ("INSERT INTO routines (id, project_id, user_id, name, prompt, guide_id, "
                       "model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        conn.execute(routine_ins, (
            "r1", "p1", 1, "Weekly numbers", "Summarize the week.", "g-a-analyst",
            "gemini-x", TS, TS,
        ))
        conn.execute(routine_ins, ("r2", "p1", 1, "Standup", "List blockers.", None, None, TS, TS))
        conn.execute(routine_ins, ("r3", "p3", 3, "Digest", "Digest it.", "g-c-empty", None, TS, TS))
        conn.execute(
            "INSERT INTO routine_schedules (id, routine_id, user_id, schedule_type, "
            "hourly_minute, is_enabled, is_running, created_at) "
            "VALUES ('sch1', 'r1', 1, 'hourly', 15, 1, 0, ?)", (TS,),
        )
        conn.execute(
            "INSERT INTO routine_skill_autoloads (id, routine_id, skill_id, created_at) "
            "VALUES ('rsa1', 'r1', 's-old-1', ?)", (TS,),
        )
        conn.execute(
            "INSERT INTO conversations (id, user_id, project_id, routine_id, created_at, "
            "last_message_at) VALUES ('c1', 1, 'p1', 'r1', ?, ?)", (TS, TS),
        )
        conn.commit()
    finally:
        conn.close()


def _converted_skills(db_path, user_id) -> list[tuple]:
    return rows(
        db_path,
        "SELECT name, description, content, visibility, project_id FROM skills "
        "WHERE creator_id = ? AND id NOT LIKE 's-old-%' ORDER BY name",
        (user_id,),
    )


def _notifications(db_path) -> dict[int, dict]:
    out = {}
    for user_id, kind, title, body, data, dismissed_at in rows(
        db_path,
        "SELECT user_id, kind, title, body, data, dismissed_at FROM user_notifications",
    ):
        assert user_id not in out, "one notification per user"
        out[user_id] = {
            "kind": kind, "title": title, "body": body,
            "data": json.loads(data), "dismissed_at": dismissed_at,
        }
    return out


def test_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    path = PROJECT_ROOT / "alembic" / "versions" / f"{REV}_convert_guides_to_skills.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REV}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REV
    assert module.down_revision == PREVIOUS  # dreaming
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_guides_with_content_become_private_skills(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)

    keyed.alembic("upgrade", REV)

    # User 1: three skills. "Analyst" and "Analyst (converted)" were taken,
    # and the default guide is not called "Default" any more.
    assert _converted_skills(db_path, 1) == [
        ("Analyst (converted 2)", "Converted from the 'Analyst' guide.",
         "Lead with the number.", "private", None),
        ("Default guide", "Converted from the 'Default' guide.",
         "Always answer in English.", "private", None),
        ("Writer", "Converted from the 'Writer' guide.",
         "Short sentences.\n\nNo jargon.", "private", None),
    ]
    # The skills the user already had are untouched.
    assert rows(db_path, "SELECT id, name, content FROM skills WHERE id LIKE 's-old-%' ORDER BY id") == [
        ("s-old-1", "Analyst", "an older skill"),
        ("s-old-2", "Analyst (converted)", "another"),
    ]
    # User 2 had only an empty default guide: nothing to convert.
    assert _converted_skills(db_path, 2) == []
    # User 3's "Analyst" collides with nobody (names are per creator).
    assert _converted_skills(db_path, 3) == [
        ("Analyst", "Converted from the 'Analyst' guide.", "Cite sources.", "private", None),
    ]
    # No auto-load of any tier was created for a converted skill.
    assert rows(db_path, "SELECT COUNT(*) FROM user_skill_autoloads") == [(0,)]
    assert rows(db_path, "SELECT id, skill_id FROM routine_skill_autoloads") == [("rsa1", "s-old-1")]
    assert rows(db_path, "SELECT COUNT(*) FROM project_skill_autoloads") == [(0,)]
    # Timestamps are stored the way the ORM stores them.
    created = rows(db_path, "SELECT created_at FROM skills WHERE name = 'Writer'")[0][0]
    assert len(created) == len(TS) and created[4] == "-" and created[10] == " "


def test_each_owner_gets_one_notification(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)

    keyed.alembic("upgrade", REV)

    notes = _notifications(db_path)
    assert set(notes) == {1, 3}  # user 2 had nothing converted

    first = notes[1]
    assert first["kind"] == "guides_converted"
    assert first["title"] == "Your guides were converted to skills"
    assert first["dismissed_at"] is None
    skill_ids = dict(rows(db_path, "SELECT name, id FROM skills WHERE creator_id = 1"))
    assert first["data"]["skills"] == [
        {"skill_id": skill_ids["Default guide"], "skill_name": "Default guide",
         "guide_name": "Default", "was_default": True},
        {"skill_id": skill_ids["Analyst (converted 2)"], "skill_name": "Analyst (converted 2)",
         "guide_name": "Analyst", "was_default": False},
        {"skill_id": skill_ids["Writer"], "skill_name": "Writer",
         "guide_name": "Writer", "was_default": False},
    ]
    assert first["data"]["routines"] == [{
        "routine_id": "r1", "routine_name": "Weekly numbers",
        "project_id": "p1", "project_name": "Reports",
        "skill_id": skill_ids["Analyst (converted 2)"],
        "skill_name": "Analyst (converted 2)",
    }]
    body = first["body"]
    assert "Your 3 guides are now private skills" in body
    assert '- Default, now the skill "Default guide"' in body
    assert '- Analyst, now the skill "Analyst (converted 2)"' in body
    assert "\n- Writer\n" in body
    assert "not loaded automatically" in body
    assert "1 routine had a guide attached and now runs without it" in body
    assert '- Weekly numbers in Reports (skill "Analyst (converted 2)")' in body
    assert "Blank" not in body  # the empty guide is not mentioned

    # User 3: one guide, and the routine whose guide was empty is not listed.
    third = notes[3]
    assert "Your guide is now a private skill" in third["body"]
    assert third["data"]["routines"] == []
    assert "routine" not in third["body"]


def test_guides_schema_is_dropped_and_routines_keep_everything(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    routine_cols = "id, project_id, user_id, name, prompt, model, created_at, updated_at"
    before = rows(db_path, f"SELECT {routine_cols} FROM routines ORDER BY id")
    schedules = rows(db_path, "SELECT * FROM routine_schedules ORDER BY id")
    assert "guide_id" in columns(db_path, "routines")

    keyed.alembic("upgrade", REV)

    assert "guides" not in _tables(db_path)
    assert "guide_id" not in columns(db_path, "routines")
    # Every routine and everything hanging off a routine survived: the
    # table was edited in place, not rebuilt (a rebuild would have cascaded).
    assert rows(db_path, f"SELECT {routine_cols} FROM routines ORDER BY id") == before
    assert rows(db_path, "SELECT * FROM routine_schedules ORDER BY id") == schedules
    assert rows(db_path, "SELECT id, routine_id FROM routine_skill_autoloads") == [("rsa1", "r1")]
    assert rows(db_path, "SELECT id, routine_id FROM conversations") == [("c1", "r1")]

    assert _fks(db_path, "routines") == {
        ("project_id", "projects", "id", "CASCADE"),
        ("user_id", "users", "id", "CASCADE"),
    }
    assert _named_indexes(db_path, "routines") == {
        "ix_routines_project_id": ["project_id"],
        "ix_routines_user_id": ["user_id"],
        "ix_routines_project_id_name": ["project_id", "name"],
    }
    assert _fks(db_path, "user_notifications") == {("user_id", "users", "id", "CASCADE")}
    assert _named_indexes(db_path, "user_notifications") == {
        "ix_user_notifications_user_id": ["user_id"],
        "ix_user_notifications_user_created": ["user_id", "created_at"],
    }
    assert rows(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(db_path, "PRAGMA foreign_key_check") == []

    # The foreign keys onto and off routines are still enforced.
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO routines (id, project_id, user_id, name, prompt, created_at) "
            "VALUES ('r-new', 'p1', 1, 'New', 'Go.', ?)", (TS,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO routines (id, project_id, user_id, name, prompt, created_at) "
                "VALUES ('r-bad', 'no-such-project', 1, 'Bad', 'Go.', ?)", (TS,),
            )
        conn.execute("DELETE FROM routines WHERE id = 'r1'")
        assert conn.execute("SELECT COUNT(*) FROM routine_schedules").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM routine_skill_autoloads").fetchone() == (0,)
        assert conn.execute("SELECT routine_id FROM conversations WHERE id = 'c1'").fetchone() == (None,)
        # Deleting a user takes their notification with them.
        conn.execute("DELETE FROM users WHERE id = 3")
        assert conn.execute("SELECT user_id FROM user_notifications").fetchall() == [(1,)]
        conn.commit()
    finally:
        conn.close()


def test_upgrade_with_no_guides_writes_nothing(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    keyed.alembic("upgrade", REV)
    assert rows(db_path, "SELECT COUNT(*) FROM skills") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM user_notifications") == [(0,)]
    assert "guides" not in _tables(db_path)


def test_a_long_guide_name_still_fits_the_skill_name_cap(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    long_name = "N" * 100
    execute(
        db_path,
        "INSERT INTO guides (id, user_id, name, content, is_default, created_at) "
        "VALUES ('g-long', 2, ?, 'text', 0, ?)", (long_name, TS),
    )
    execute(
        db_path,
        "INSERT INTO skills (id, creator_id, project_id, name, description, content, "
        "visibility, created_at) VALUES ('s-old-3', 2, NULL, ?, '', 'x', 'private', ?)",
        (long_name, TS),
    )

    keyed.alembic("upgrade", REV)

    (name,), = rows(db_path, "SELECT name FROM skills WHERE creator_id = 2 AND id != 's-old-3'")
    assert name == "N" * 88 + " (converted)" and len(name) == 100


def test_orm_reads_and_writes_the_migrated_schema(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)

    import db.notification_store as notification_store
    import db.routine_store as routine_store
    import db.skill_store as skill_store

    keyed.bind_orm([notification_store, routine_store, skill_store])
    try:
        (note,) = run(notification_store.list_for_user(1))
        assert note["kind"] == "guides_converted" and note["dismissed_at"] is None
        assert [s["skill_name"] for s in note["data"]["skills"]] == [
            "Default guide", "Analyst (converted 2)", "Writer",
        ]
        assert run(notification_store.count_open(1)) == 1
        dismissed = run(notification_store.dismiss(1, note["id"]))
        assert dismissed["dismissed_at"] is not None
        assert run(notification_store.count_open(1)) == 0

        # A converted skill is an ordinary private skill of its owner.
        skill = run(skill_store.get_skill(note["data"]["skills"][0]["skill_id"]))
        assert skill["name"] == "Default guide" and skill["visibility"] == "private"
        assert skill["content"] == "Always answer in English."
        assert run(skill_store.list_user_autoloaded_skill_ids(1)) == []

        routine = run(routine_store.get_routine(1, "r1"))
        assert routine["name"] == "Weekly numbers" and "guide_id" not in routine
        created = run(routine_store.create_routine(1, "p1", "Fresh", "Do it."))
        updated = run(routine_store.update_routine(1, created["id"], model="gemini-y"))
        assert updated["model"] == "gemini-y"
    finally:
        keyed.dispose()


def test_downgrade_restores_an_empty_guides_schema(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    keyed.alembic("upgrade", REV)

    keyed.alembic("downgrade", PREVIOUS)

    assert "user_notifications" not in _tables(db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM guides") == [(0,)]
    assert _named_indexes(db_path, "guides") == {
        "ix_guides_user_id": ["user_id"],
        "ix_guides_user_id_name": ["user_id", "name"],
    }
    assert ("guide_id", "guides", "id", "SET NULL") in _fks(db_path, "routines")
    assert rows(db_path, "SELECT id, guide_id FROM routines ORDER BY id") == [
        ("r1", None), ("r2", None), ("r3", None),
    ]
    # The converted skills stay, and the schedule never left.
    assert len(_converted_skills(db_path, 1)) == 3
    assert rows(db_path, "SELECT id FROM routine_schedules") == [("sch1",)]

    # And the upgrade applies again to the downgraded schema.
    keyed.alembic("upgrade", REV)
    assert "guides" not in _tables(db_path)
    assert "guide_id" not in columns(db_path, "routines")


def test_fresh_database_matches_the_orm(keyed):
    db_path = keyed.db_path
    keyed.alembic("upgrade", "head")
    assert "guides" not in _tables(db_path)
    assert "guide_id" not in columns(db_path, "routines")
    mine = [
        d for d in metadata_drift(db_path)
        if "'guides'" in d or "'routines'" in d or "'user_notifications'" in d
        or "guide_id" in d
    ]
    assert mine == []
