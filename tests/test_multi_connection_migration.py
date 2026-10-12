"""End-to-end test of migration e3c9b7a1d5f2 (multi-connection
user_service_credentials: account / description / is_default + index swap).

Builds a database at the previous head in a temp data dir, seeds legacy
credential rows whose secret / oauth_blob are encrypted envelopes, upgrades
to head, and checks: legacy rows become account '' / default, ciphertext is
untouched and still decrypts through the ORM store, the index swap landed
(incl. the partial default index), the ORM metadata matches the migrated
table, and the downgrade refuses while a (user, service) holds several rows
but succeeds -- restoring the old unique index -- once each has one.

Alembic, the seeding and the ORM read-back run in subprocesses (``uv run``)
so the env is exactly what a deployment would have, without disturbing this
process's engine or encryption cache.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from config import encryption
from config.paths import PROJECT_ROOT

REV = "e3c9b7a1d5f2"
PREVIOUS = "b4d7e2a9c6f1"
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


def _indexes(db_path):
    return dict(_rows(
        db_path,
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'user_service_credentials'",
    ))


_SEED = """
import json, sqlite3
from config import encryption
from config.paths import DATABASE_PATH
sec = lambda s: encryption.encrypt_str(s, "user_service_credentials.secret")
blob = lambda d: json.dumps(encryption.encrypt_json(d, "user_service_credentials.oauth_blob"))
conn = sqlite3.connect(DATABASE_PATH)
ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
       "VALUES (?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)")
conn.execute(ins, (1, 'a@example.com', 'A', 'key-a'))
conn.execute(ins, (2, 'b@example.com', 'B', 'key-b'))
row = ("INSERT INTO user_service_credentials (user_id, service, secret, oauth_blob, created_at, updated_at) "
       "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
conn.execute(row, (1, 'acme', sec('sk-a'), None))
conn.execute(row, (1, 'github', None, blob({"access_token": "gho-a"})))
conn.execute(row, (2, 'acme', sec('sk-b'), None))
conn.commit(); conn.close()
print("seeded")
"""

_READ = """
import asyncio, json
from db.user_service_credential_store import get_credential, list_connections
out = {
    "a_acme": asyncio.run(get_credential(1, "acme")),
    "a_github": asyncio.run(get_credential(1, "github")),
    "b_acme": asyncio.run(list_connections(2, "acme")),
}
print(json.dumps(out))
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
def touches(d):
    return "user_service_credentials" in repr(d)
print(json.dumps([repr(d) for d in diffs if touches(d)]))
"""


def test_upgrade_downgrade_roundtrip(deployment):
    data_dir, env = deployment
    db_path = data_dir / "quest.db"

    _run(["alembic", "upgrade", PREVIOUS], env)
    _python(env, _SEED)
    before = _rows(db_path, "SELECT id, secret, oauth_blob FROM user_service_credentials ORDER BY id")

    _run(["alembic", "upgrade", REV], env)

    # Legacy rows: account '', no description, default; ciphertext verbatim.
    after = _rows(
        db_path,
        "SELECT id, secret, oauth_blob, account, description, is_default "
        "FROM user_service_credentials ORDER BY id",
    )
    assert [r[:3] for r in after] == before
    assert all(r[3:] == ("", None, 1) for r in after)

    indexes = _indexes(db_path)
    assert "ix_user_service_credentials_user_id_service" not in indexes
    assert "ix_user_service_credentials_user_id" in indexes
    assert "UNIQUE" in indexes["ix_user_service_credentials_user_service_account"]
    default_sql = indexes["ix_user_service_credentials_user_service_default"]
    assert "UNIQUE" in default_sql and "WHERE is_default" in default_sql
    fks = _rows(db_path, "PRAGMA foreign_key_list(user_service_credentials)")
    assert [(fk[2], fk[3], fk[4], fk[6]) for fk in fks] == [("users", "user_id", "id", "CASCADE")]

    # The ORM reads the migrated rows back through the store.
    read = json.loads(_python(env, _READ))
    assert read["a_acme"]["secret"] == "sk-a"
    assert read["a_acme"]["account"] == "" and read["a_acme"]["is_default"] is True
    assert read["a_github"]["oauth_blob"] == {"access_token": "gho-a"}
    assert [r["secret"] for r in read["b_acme"]] == ["sk-b"]

    # ORM metadata matches the migrated table (autogenerate stays quiet).
    assert json.loads(_python(env, _DRIFT)) == []

    # A second connection and a second default.
    _execute(
        db_path,
        "INSERT INTO user_service_credentials (user_id, service, account, is_default, created_at, updated_at) "
        "VALUES (1, 'acme', 'second', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            db_path,
            "INSERT INTO user_service_credentials (user_id, service, account, is_default, created_at, updated_at) "
            "VALUES (1, 'acme', 'third', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        )

    # Downgrade refuses while (1, acme) has two rows; nothing changes.
    result = _run(["alembic", "downgrade", PREVIOUS], env, check=False)
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "user 1 / acme (2 rows)" in result.stderr
    assert "is_default" in {r[1] for r in _rows(db_path, "PRAGMA table_info(user_service_credentials)")}
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(REV,)]

    # With one row per (user, service) it succeeds.
    _execute(db_path, "DELETE FROM user_service_credentials WHERE account = 'second'")
    _run(["alembic", "downgrade", PREVIOUS], env)
    columns = {r[1] for r in _rows(db_path, "PRAGMA table_info(user_service_credentials)")}
    assert not columns & {"account", "description", "is_default"}
    assert _rows(db_path, "SELECT id, secret, oauth_blob FROM user_service_credentials ORDER BY id") == before
    indexes = _indexes(db_path)
    assert "UNIQUE" in indexes["ix_user_service_credentials_user_id_service"]
    assert "ix_user_service_credentials_user_id" in indexes
    assert "ix_user_service_credentials_user_service_account" not in indexes
    assert "ix_user_service_credentials_user_service_default" not in indexes
    fks = _rows(db_path, "PRAGMA foreign_key_list(user_service_credentials)")
    assert [(fk[2], fk[3], fk[4], fk[6]) for fk in fks] == [("users", "user_id", "id", "CASCADE")]
    # Users survived the table rebuild (no cascade in the wrong direction).
    assert _rows(db_path, "SELECT COUNT(*) FROM users") == [(2,)]

    # And upgrades again cleanly.
    _run(["alembic", "upgrade", "head"], env)
    assert _rows(db_path, "SELECT COUNT(*) FROM user_service_credentials WHERE is_default") == [(3,)]
