"""End-to-end test of migration f4c8e1a7d2b9 (users.session_epoch).

Builds a database at the previous head in a temp data dir, seeds users,
upgrades and checks: existing rows start at epoch 0, the column is NOT NULL
with a server default (raw inserts that omit it get 0), the ORM reads and
bumps it through db/user_store.py, the ORM metadata matches the migrated
table, and the downgrade drops the column while keeping every user.

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
    deployment,
    execute,
    metadata_drift,
    rows,
    run,
)

REV = "f4c8e1a7d2b9"
PREVIOUS = "d4f8b2a6c1e9"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _columns(db_path):
    """name -> (type, notnull, default) for every users column."""
    return {
        r[1]: (r[2], r[3], r[4])
        for r in rows(db_path, "PRAGMA table_info(users)")
    }


def _seed(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
               "VALUES (?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)")
        conn.execute(ins, (1, 'a@example.com', 'A', encryption.encrypt_str('key-a', 'users.api_key')))
        conn.execute(ins, (2, 'b@example.com', 'B', encryption.encrypt_str('key-b', 'users.api_key')))
        conn.commit()
    finally:
        conn.close()


def _read_and_bump() -> dict:
    from db.user_store import bump_session_epoch, get_user_by_email

    async def _go():
        before = (await get_user_by_email('a@example.com'))["session_epoch"]
        bumped = await bump_session_epoch(1)
        after = (await get_user_by_email('a@example.com'))["session_epoch"]
        other = (await get_user_by_email('b@example.com'))["session_epoch"]
        return {"before": before, "bumped": bumped, "after": after, "other": other}

    return run(_go())


def test_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    path = PROJECT_ROOT / "alembic" / "versions" / f"{REV}_add_session_epoch_to_users.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REV}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REV
    assert module.down_revision == PREVIOUS  # add project_memories
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_upgrade_downgrade_roundtrip(keyed):
    db_path = keyed.db_path

    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    assert "session_epoch" not in _columns(db_path)
    before = rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id")

    keyed.alembic("upgrade", REV)

    col_type, notnull, default = _columns(db_path)["session_epoch"]
    assert col_type == "INTEGER" and notnull == 1
    assert default.strip("'\"") == "0"
    assert rows(db_path, "SELECT id, session_epoch FROM users ORDER BY id") == [(1, 0), (2, 0)]
    assert rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id") == before

    # A raw insert that does not name the column gets the server default.
    execute(
        db_path,
        "INSERT INTO users (id, email, name, api_key, settings, created_at) "
        "VALUES (3, 'c@example.com', 'C', 'plain', '{}', CURRENT_TIMESTAMP)",
    )
    assert rows(db_path, "SELECT session_epoch FROM users WHERE id = 3") == [(0,)]
    execute(db_path, "DELETE FROM users WHERE id = 3")

    # The ORM reads the column and bumps one account only.
    assert _read_and_bump() == {"before": 0, "bumped": 1, "after": 1, "other": 0}

    # ORM metadata matches the migrated column.
    assert [d for d in metadata_drift(db_path) if "session_epoch" in d] == []

    keyed.alembic("downgrade", PREVIOUS)
    assert "session_epoch" not in _columns(db_path)
    assert rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id") == before

    # And upgrades again cleanly, from epoch 0.
    keyed.alembic("upgrade", "head")
    assert rows(db_path, "SELECT id, session_epoch FROM users ORDER BY id") == [(1, 0), (2, 0)]
