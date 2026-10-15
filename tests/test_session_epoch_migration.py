"""End-to-end test of migration f4c8e1a7d2b9 (users.session_epoch).

Builds a database at the previous head in a temp data dir, seeds users,
upgrades and checks: existing rows start at epoch 0, the column is NOT NULL
with a server default (raw inserts that omit it get 0), the ORM reads and
bumps it through db/user_store.py, the ORM metadata matches the migrated
table, and the downgrade drops the column while keeping every user.

Alembic, the seeding and the ORM read-back run in subprocesses (``uv run``)
so the env is exactly what a deployment would have, without disturbing this
process's engine or encryption cache (same approach as
tests/test_multi_connection_migration.py).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess

import pytest

from config import encryption
from config.paths import PROJECT_ROOT

REV = "f4c8e1a7d2b9"
PREVIOUS = "d4f8b2a6c1e9"
PASSWORD = "migration-test-password"


@pytest.fixture()
def deployment(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_file = data_dir / "encryption_key.json"
    env = {
        **os.environ,
        "QUEST_ENV": "prod",
        "QUEST_DATA_DIR": str(data_dir),
        "QUEST_ENCRYPTION_KEY_FILE": str(key_file),
        "QUEST_ENCRYPTION_PASSWORD": PASSWORD,
    }
    env.pop("QUEST_ENCRYPTION_PASSWORD_FILE", None)
    encryption.create_key_file(PASSWORD, key_file)
    return data_dir, env


def _run(args, env, check=True):
    return subprocess.run(
        ["uv", "run", *args], cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, check=check,
    )


def _python(env, script):
    return _run(["python", "-c", script], env).stdout.strip().splitlines()[-1]


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _columns(db_path):
    """name -> (type, notnull, default) for every users column."""
    return {
        r[1]: (r[2], r[3], r[4])
        for r in _rows(db_path, "PRAGMA table_info(users)")
    }


_SEED = """
import sqlite3
from config import encryption
from config.paths import DATABASE_PATH
conn = sqlite3.connect(DATABASE_PATH)
ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
       "VALUES (?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)")
conn.execute(ins, (1, 'a@example.com', 'A', encryption.encrypt_str('key-a', 'users.api_key')))
conn.execute(ins, (2, 'b@example.com', 'B', encryption.encrypt_str('key-b', 'users.api_key')))
conn.commit(); conn.close()
print("seeded")
"""

_READ_AND_BUMP = """
import asyncio, json
from db.user_store import bump_session_epoch, get_user_by_email
before = asyncio.run(get_user_by_email('a@example.com'))["session_epoch"]
bumped = asyncio.run(bump_session_epoch(1))
after = asyncio.run(get_user_by_email('a@example.com'))["session_epoch"]
other = asyncio.run(get_user_by_email('b@example.com'))["session_epoch"]
print(json.dumps({"before": before, "bumped": bumped, "after": after, "other": other}))
"""

_DRIFT = """
import json
from sqlalchemy import create_engine
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from config.paths import DATABASE_PATH
from db.models import Base
engine = create_engine(f"sqlite:///{DATABASE_PATH}")
with engine.connect() as conn:
    diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
print(json.dumps([repr(d) for d in diffs if "session_epoch" in repr(d)]))
"""


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


def test_upgrade_downgrade_roundtrip(deployment):
    data_dir, env = deployment
    db_path = data_dir / "quest.db"

    _run(["alembic", "upgrade", PREVIOUS], env)
    _python(env, _SEED)
    assert "session_epoch" not in _columns(db_path)
    before = _rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id")

    _run(["alembic", "upgrade", REV], env)

    col_type, notnull, default = _columns(db_path)["session_epoch"]
    assert col_type == "INTEGER" and notnull == 1
    assert default.strip("'\"") == "0"
    assert _rows(db_path, "SELECT id, session_epoch FROM users ORDER BY id") == [(1, 0), (2, 0)]
    assert _rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id") == before

    # A raw insert that does not name the column gets the server default.
    _execute(
        db_path,
        "INSERT INTO users (id, email, name, api_key, settings, created_at) "
        "VALUES (3, 'c@example.com', 'C', 'plain', '{}', CURRENT_TIMESTAMP)",
    )
    assert _rows(db_path, "SELECT session_epoch FROM users WHERE id = 3") == [(0,)]
    _execute(db_path, "DELETE FROM users WHERE id = 3")

    # The ORM reads the column and bumps one account only.
    read = json.loads(_python(env, _READ_AND_BUMP))
    assert read == {"before": 0, "bumped": 1, "after": 1, "other": 0}

    # ORM metadata matches the migrated column.
    assert json.loads(_python(env, _DRIFT)) == []

    _run(["alembic", "downgrade", PREVIOUS], env)
    assert "session_epoch" not in _columns(db_path)
    assert _rows(db_path, "SELECT id, email, api_key FROM users ORDER BY id") == before

    # And upgrades again cleanly, from epoch 0.
    _run(["alembic", "upgrade", "head"], env)
    assert _rows(db_path, "SELECT id, session_epoch FROM users ORDER BY id") == [(1, 0), (2, 0)]
