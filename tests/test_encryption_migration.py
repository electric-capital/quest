"""End-to-end test of migration c4e8a2d17f63 (encrypt secrets at rest).

Builds a database at the previous head in a temp data dir, inserts
plaintext rows the way a pre-encryption release would have, upgrades to
the encryption revision, and checks: the pre-encryption backup exists, every secret is a
``qenc1:`` envelope, ``api_key_hash`` is populated, the ORM reads the
plaintext back, re-running is a no-op, and downgrade restores plaintext.

Alembic and the ORM read-back run in subprocesses (``uv run``) so the env
(QUEST_DATA_DIR, the key file, the password) is exactly what a deployment
would have, without disturbing this process's engine.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from config import encryption
from config.paths import PROJECT_ROOT

PREVIOUS_HEAD = "f3a9c5d81b42"
# The revision under test. Later migrations (d7a1f3c9e2b4 drops
# users.telegram_session) change the schema these assertions read, so the
# upgrades stop at this revision instead of the current head.
ENCRYPTION_REV = "c4e8a2d17f63"
PASSWORD = "migration-test-password"


@pytest.fixture()
def deployment(tmp_path):
    """A temp data dir with its own key file and the env to address it."""
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
    return data_dir, env


def _run(args: list[str], env: dict, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", *args], cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, check=check,
    )


def _alembic(env: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["alembic", *args], env, check=check)


def _seed_plaintext(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, email, name, api_key, settings, google_oauth, "
            "google_services_oauth, telegram_session, airtable_token, ramp_oauth, created_at) "
            "VALUES (1, 'a@example.com', 'A', 'key-a', '{}', ?, ?, 'tg-a', 'pat-a', ?, CURRENT_TIMESTAMP)",
            (json.dumps({"access_token": "g-a"}), json.dumps({"access_token": "gs-a"}),
             json.dumps({"access_token": "ramp-a"})),
        )
        conn.execute(
            "INSERT INTO users (id, email, name, api_key, settings, google_oauth, created_at) "
            "VALUES (2, 'b@example.com', 'B', 'key-b', '{}', NULL, CURRENT_TIMESTAMP)",
        )
        conn.execute(
            "INSERT INTO user_service_credentials (user_id, service, secret, oauth_blob, created_at, updated_at) "
            "VALUES (1, 'acme', 'acme-secret', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        )
        conn.execute(
            "INSERT INTO user_service_credentials (user_id, service, secret, oauth_blob, created_at, updated_at) "
            "VALUES (1, 'slack', NULL, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (json.dumps({"access_token": "xoxp-a", "user_id": "U1"}),),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# Columns added by migrations after ENCRYPTION_REV that the current ORM
# models map (they SELECT every mapped column, so the read-back needs them);
# added by hand, with their server defaults, for the read-back only.
_LATER_COLUMNS = {
    "users": (
        ("password_hash", "VARCHAR(255)"),
        ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "user_service_credentials": (
        ("account", "VARCHAR(320) NOT NULL DEFAULT ''"),
        ("description", "TEXT"),
        ("is_default", "BOOLEAN NOT NULL DEFAULT 1"),
    ),
}


def _read_back_via_orm(env: dict, db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        for table, columns in _LATER_COLUMNS.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        conn.commit()
    finally:
        conn.close()
    script = (
        "import asyncio, json\n"
        "from db.user_store import get_user_by_email, get_user_by_api_key, get_user_by_slack_user_id\n"
        "a = asyncio.run(get_user_by_email('a@example.com'))\n"
        "by_key = asyncio.run(get_user_by_api_key('key-a'))\n"
        "by_slack = asyncio.run(get_user_by_slack_user_id('U1'))\n"
        "print(json.dumps({'a': a, 'by_key_email': by_key and by_key['email'],"
        " 'by_slack_email': by_slack and by_slack['email']}))\n"
    )
    result = _run(["python", "-c", script], env)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_migration_encrypts_in_place_with_backup_and_downgrade(deployment):
    data_dir, env = deployment
    db_path = data_dir / "quest.db"
    encryption.create_key_file(PASSWORD, Path(env["QUEST_ENCRYPTION_KEY_FILE"]))

    _alembic(env, "upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)

    _alembic(env, "upgrade", ENCRYPTION_REV)

    backups = list(data_dir.glob("quest.db.pre-encryption-*.bak"))
    assert len(backups) == 1
    assert _rows(backups[0], "SELECT api_key FROM users ORDER BY id") == [("key-a",), ("key-b",)]

    users = _rows(
        db_path,
        "SELECT api_key, api_key_hash, google_oauth, google_services_oauth, "
        "telegram_session, airtable_token, ramp_oauth FROM users ORDER BY id",
    )
    a, b = users
    assert encryption.is_encrypted(a[0]) and a[1] == encryption.hash_api_key("key-a")
    assert encryption.is_encrypted(json.loads(a[2]))
    assert encryption.is_encrypted(json.loads(a[3]))
    assert encryption.is_encrypted(a[4]) and encryption.is_encrypted(a[5])
    assert encryption.is_encrypted(json.loads(a[6]))
    assert encryption.is_encrypted(b[0]) and b[1] == encryption.hash_api_key("key-b")
    assert b[2:] == (None, None, None, None, None)
    dump = " ".join(str(v) for row in users for v in row)
    for secret in ("key-a", "key-b", "g-a", "gs-a", "tg-a", "pat-a", "ramp-a"):
        assert secret not in dump

    creds = _rows(db_path, "SELECT service, secret, oauth_blob FROM user_service_credentials ORDER BY id")
    assert encryption.is_encrypted(creds[0][1]) and creds[0][2] is None
    assert creds[1][1] is None and encryption.is_encrypted(json.loads(creds[1][2]))

    # The ORM reads plaintext back, looks users up by hash, and resolves the
    # Slack user id from the decrypted blob.
    read = _read_back_via_orm(env, db_path)
    assert read["a"]["api_key"] == "key-a"
    assert read["a"]["google_oauth"] == {"access_token": "g-a"}
    # (telegram_session is checked in SQL above only: the current ORM model
    # no longer maps that column -- migration d7a1f3c9e2b4 moved it.)
    assert read["a"]["ramp_oauth"] == {"access_token": "ramp-a"}
    assert read["by_key_email"] == "a@example.com"
    assert read["by_slack_email"] == "a@example.com"

    # Downgrade restores plaintext and drops the hash column
    _alembic(env, "downgrade", PREVIOUS_HEAD)
    restored = _rows(
        db_path,
        "SELECT api_key, google_oauth, telegram_session, airtable_token, ramp_oauth FROM users WHERE id = 1",
    )
    assert restored == [("key-a", json.dumps({"access_token": "g-a"}), "tg-a", "pat-a", json.dumps({"access_token": "ramp-a"}))]
    columns = {r[1] for r in _rows(db_path, "PRAGMA table_info(users)")}
    assert "api_key_hash" not in columns
    assert _rows(db_path, "SELECT secret FROM user_service_credentials WHERE service = 'acme'") == [("acme-secret",)]


def test_migration_is_idempotent_over_a_partial_run(deployment):
    """Rows already in the envelope are skipped; only plaintext rows change."""
    data_dir, env = deployment
    db_path = data_dir / "quest.db"
    encryption.create_key_file(PASSWORD, Path(env["QUEST_ENCRYPTION_KEY_FILE"]))
    _alembic(env, "upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)
    _alembic(env, "upgrade", ENCRYPTION_REV)
    before = _rows(db_path, "SELECT api_key, google_oauth FROM users ORDER BY id")

    # Simulate a downgrade-less re-run: stamp back and upgrade again.
    _alembic(env, "stamp", PREVIOUS_HEAD)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX ix_users_api_key_hash")
        conn.execute("ALTER TABLE users DROP COLUMN api_key_hash")
        conn.commit()
    finally:
        conn.close()
    _alembic(env, "upgrade", ENCRYPTION_REV)
    after = _rows(db_path, "SELECT api_key, google_oauth FROM users ORDER BY id")
    assert after == before  # untouched ciphertext, no re-encryption
    assert _rows(db_path, "SELECT api_key_hash FROM users WHERE id = 1") == [(encryption.hash_api_key("key-a"),)]


def test_migration_fails_closed_without_the_key(deployment):
    data_dir, env = deployment
    db_path = data_dir / "quest.db"
    encryption.create_key_file(PASSWORD, Path(env["QUEST_ENCRYPTION_KEY_FILE"]))
    _alembic(env, "upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)

    bad_env = {**env, "QUEST_ENCRYPTION_PASSWORD": "wrong"}
    result = _alembic(bad_env, "upgrade", ENCRYPTION_REV, check=False)
    assert result.returncode != 0
    assert "does not unlock" in result.stderr
    # Nothing was touched, no backup was written
    assert _rows(db_path, "SELECT api_key FROM users ORDER BY id") == [("key-a",), ("key-b",)]
    assert not list(data_dir.glob("*.bak"))

    no_pw_env = {k: v for k, v in env.items() if k != "QUEST_ENCRYPTION_PASSWORD"}
    result = _alembic(no_pw_env, "upgrade", ENCRYPTION_REV, check=False)
    assert result.returncode != 0
    assert "No encryption password available" in result.stderr


def test_fresh_database_needs_no_backup(deployment):
    data_dir, env = deployment
    encryption.create_key_file(PASSWORD, Path(env["QUEST_ENCRYPTION_KEY_FILE"]))
    _alembic(env, "upgrade", ENCRYPTION_REV)
    assert not list(data_dir.glob("*.bak"))
    columns = {r[1] for r in _rows(data_dir / "quest.db", "PRAGMA table_info(users)")}
    assert "api_key_hash" in columns


def test_init_cli_creates_key_file_and_refuses_sentinel_outside_local(deployment, tmp_path):
    data_dir, env = deployment
    key_file = Path(env["QUEST_ENCRYPTION_KEY_FILE"])
    assert not key_file.exists()
    result = _run(["python", "-m", "config.encryption", "init"], env)
    assert key_file.exists() and "Encryption key unlocked" in result.stdout
    # check with the right password
    _run(["python", "-m", "config.encryption", "check"], env)
    # wrong password
    result = _run(["python", "-m", "config.encryption", "check"], {**env, "QUEST_ENCRYPTION_PASSWORD": "x"}, check=False)
    assert result.returncode == 1 and "does not unlock" in result.stderr
    # sentinel in prod
    result = _run(
        ["python", "-m", "config.encryption", "check"],
        {**env, "QUEST_ENCRYPTION_PASSWORD": encryption.LOCAL_DEV_PASSWORD}, check=False,
    )
    assert result.returncode == 1 and "outside local" in result.stderr
    # local mode: sentinel auto-creates without any password set
    local_key = tmp_path / "local_key.json"
    local_env = {k: v for k, v in env.items() if k != "QUEST_ENCRYPTION_PASSWORD"}
    local_env.update({"QUEST_ENV": "local", "QUEST_ENCRYPTION_KEY_FILE": str(local_key)})
    _run(["python", "-m", "config.encryption", "init"], local_env)
    assert local_key.exists()


def test_init_cli_encrypts_plaintext_store_files(deployment):
    data_dir, env = deployment
    store = data_dir / "service_credentials"
    store.mkdir()
    (store / "google_oauth.json").write_text(json.dumps({"web": {"client_id": "x", "client_secret": "s"}}))
    inf = data_dir / "inference_credentials"
    inf.mkdir()
    (inf / "openrouter.json").write_text(json.dumps({"api_key": "sk-or"}))
    result = _run(["python", "-m", "config.encryption", "init"], env)
    assert "Encrypted credential file for google_oauth" in result.stdout
    assert "Encrypted credential file for openrouter" in result.stdout
    for path in (store / "google_oauth.json", inf / "openrouter.json"):
        on_disk = json.loads(path.read_text())
        assert set(on_disk) == {"encrypted"} and encryption.is_encrypted(on_disk["encrypted"])
    assert "sk-or" not in (inf / "openrouter.json").read_text()
