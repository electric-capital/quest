"""End-to-end test of migration c4e8a2d17f63 (encrypt secrets at rest).

Builds a database at the previous head in a temp data dir, inserts
plaintext rows the way a pre-encryption release would have, upgrades to
the encryption revision, and checks: the pre-encryption backup exists, every secret is a
``qenc1:`` envelope, ``api_key_hash`` is populated, the ORM reads the
plaintext back, re-running is a no-op, and downgrade restores plaintext.

Alembic and the ORM read-back run in this process against the temp
deployment set up by ``tests/migration_support.py`` (env, paths, key cache
and store bindings are swapped for the test and restored afterwards). The
two ``init`` CLI tests at the bottom spawn ``python -m config.encryption``
once each, since the module entry point is what they test.
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
from tests.migration_support import PASSWORD, deployment, rows, run  # noqa: F401

PREVIOUS_HEAD = "f3a9c5d81b42"
# The revision under test. Later migrations (d7a1f3c9e2b4 drops
# users.telegram_session) change the schema these assertions read, so the
# upgrades stop at this revision instead of the current head.
ENCRYPTION_REV = "c4e8a2d17f63"


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


def _read_back_via_orm(db_path: Path) -> dict:
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
    from db.user_store import get_user_by_api_key, get_user_by_email, get_user_by_slack_user_id

    async def _read():
        a = await get_user_by_email("a@example.com")
        by_key = await get_user_by_api_key("key-a")
        by_slack = await get_user_by_slack_user_id("U1")
        return {
            "a": a,
            "by_key_email": by_key and by_key["email"],
            "by_slack_email": by_slack and by_slack["email"],
        }

    return run(_read())


def test_migration_encrypts_in_place_with_backup_and_downgrade(deployment):
    data_dir, db_path = deployment.data_dir, deployment.db_path
    encryption.create_key_file(PASSWORD, deployment.key_file)

    deployment.alembic("upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)

    deployment.alembic("upgrade", ENCRYPTION_REV)

    backups = list(data_dir.glob("quest.db.pre-encryption-*.bak"))
    assert len(backups) == 1
    assert rows(backups[0], "SELECT api_key FROM users ORDER BY id") == [("key-a",), ("key-b",)]

    users = rows(
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

    creds = rows(db_path, "SELECT service, secret, oauth_blob FROM user_service_credentials ORDER BY id")
    assert encryption.is_encrypted(creds[0][1]) and creds[0][2] is None
    assert creds[1][1] is None and encryption.is_encrypted(json.loads(creds[1][2]))

    # The ORM reads plaintext back, looks users up by hash, and resolves the
    # Slack user id from the decrypted blob.
    read = _read_back_via_orm(db_path)
    assert read["a"]["api_key"] == "key-a"
    assert read["a"]["google_oauth"] == {"access_token": "g-a"}
    # (telegram_session is checked in SQL above only: the current ORM model
    # no longer maps that column -- migration d7a1f3c9e2b4 moved it.)
    assert read["a"]["ramp_oauth"] == {"access_token": "ramp-a"}
    assert read["by_key_email"] == "a@example.com"
    assert read["by_slack_email"] == "a@example.com"

    # Downgrade restores plaintext and drops the hash column
    deployment.alembic("downgrade", PREVIOUS_HEAD)
    restored = rows(
        db_path,
        "SELECT api_key, google_oauth, telegram_session, airtable_token, ramp_oauth FROM users WHERE id = 1",
    )
    assert restored == [("key-a", json.dumps({"access_token": "g-a"}), "tg-a", "pat-a", json.dumps({"access_token": "ramp-a"}))]
    columns = {r[1] for r in rows(db_path, "PRAGMA table_info(users)")}
    assert "api_key_hash" not in columns
    assert rows(db_path, "SELECT secret FROM user_service_credentials WHERE service = 'acme'") == [("acme-secret",)]


def test_migration_is_idempotent_over_a_partial_run(deployment):
    """Rows already in the envelope are skipped; only plaintext rows change."""
    db_path = deployment.db_path
    encryption.create_key_file(PASSWORD, deployment.key_file)
    deployment.alembic("upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)
    deployment.alembic("upgrade", ENCRYPTION_REV)
    before = rows(db_path, "SELECT api_key, google_oauth FROM users ORDER BY id")

    # Simulate a downgrade-less re-run: stamp back and upgrade again.
    deployment.alembic("stamp", PREVIOUS_HEAD)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX ix_users_api_key_hash")
        conn.execute("ALTER TABLE users DROP COLUMN api_key_hash")
        conn.commit()
    finally:
        conn.close()
    deployment.alembic("upgrade", ENCRYPTION_REV)
    after = rows(db_path, "SELECT api_key, google_oauth FROM users ORDER BY id")
    assert after == before  # untouched ciphertext, no re-encryption
    assert rows(db_path, "SELECT api_key_hash FROM users WHERE id = 1") == [(encryption.hash_api_key("key-a"),)]


def test_migration_fails_closed_without_the_key(deployment):
    data_dir, db_path = deployment.data_dir, deployment.db_path
    encryption.create_key_file(PASSWORD, deployment.key_file)
    deployment.alembic("upgrade", PREVIOUS_HEAD)
    _seed_plaintext(db_path)

    with deployment.env(QUEST_ENCRYPTION_PASSWORD="wrong"):
        with pytest.raises(RuntimeError, match="does not unlock"):
            deployment.alembic("upgrade", ENCRYPTION_REV)
    # Nothing was touched, no backup was written
    assert rows(db_path, "SELECT api_key FROM users ORDER BY id") == [("key-a",), ("key-b",)]
    assert not list(data_dir.glob("*.bak"))

    with deployment.env(QUEST_ENCRYPTION_PASSWORD=None):
        with pytest.raises(RuntimeError, match="No encryption password available"):
            deployment.alembic("upgrade", ENCRYPTION_REV)


def test_fresh_database_needs_no_backup(deployment):
    data_dir = deployment.data_dir
    encryption.create_key_file(PASSWORD, deployment.key_file)
    deployment.alembic("upgrade", ENCRYPTION_REV)
    assert not list(data_dir.glob("*.bak"))
    columns = {r[1] for r in rows(data_dir / "quest.db", "PRAGMA table_info(users)")}
    assert "api_key_hash" in columns


# -- CLI ---------------------------------------------------------------------
#
# ``python -m config.encryption`` is the entry point run.py runs before the
# migrations. One spawn per test (``sys.executable``, the venv is already
# active) covers the ``__main__`` guard, argument parsing and the exit code;
# the remaining commands go through ``_cli()`` in this process, which is the
# function that guard calls with ``sys.exit(_cli(argv))``.


def _spawn_cli(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "config.encryption", *args], cwd=PROJECT_ROOT,
        env={**os.environ, **(env or {})}, capture_output=True, text=True, check=check,
    )


def _cli(capsys, *args: str) -> tuple[int, str, str]:
    code = encryption._cli(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_cli_creates_key_file_and_refuses_sentinel_outside_local(deployment, tmp_path, capsys):
    key_file = deployment.key_file
    assert not key_file.exists()
    result = _spawn_cli("init")
    assert key_file.exists() and "Encryption key unlocked" in result.stdout
    # check with the right password
    with deployment.env():
        assert _cli(capsys, "check")[0] == 0
    # wrong password
    with deployment.env(QUEST_ENCRYPTION_PASSWORD="x"):
        code, _out, err = _cli(capsys, "check")
    assert code == 1 and "does not unlock" in err
    # sentinel in prod
    with deployment.env(QUEST_ENCRYPTION_PASSWORD=encryption.LOCAL_DEV_PASSWORD):
        code, _out, err = _cli(capsys, "check")
    assert code == 1 and "outside local" in err
    # local mode: sentinel auto-creates without any password set
    local_key = tmp_path / "local_key.json"
    with deployment.env(
        QUEST_ENCRYPTION_PASSWORD=None, QUEST_ENV="local", QUEST_ENCRYPTION_KEY_FILE=str(local_key),
    ):
        assert _cli(capsys, "init")[0] == 0
    assert local_key.exists()


def test_init_cli_encrypts_plaintext_store_files(deployment):
    data_dir = deployment.data_dir
    store = data_dir / "service_credentials"
    store.mkdir()
    (store / "google_oauth.json").write_text(json.dumps({"web": {"client_id": "x", "client_secret": "s"}}))
    inf = data_dir / "inference_credentials"
    inf.mkdir()
    (inf / "openrouter.json").write_text(json.dumps({"api_key": "sk-or"}))
    # The store directories are import-time constants of the credential
    # modules, so the sweep has to run in a process whose QUEST_DATA_DIR is
    # this deployment's: the CLI spawn, as run.py does.
    result = _spawn_cli("init")
    assert "Encrypted credential file for google_oauth" in result.stdout
    assert "Encrypted credential file for openrouter" in result.stdout
    for path in (store / "google_oauth.json", inf / "openrouter.json"):
        on_disk = json.loads(path.read_text())
        assert set(on_disk) == {"encrypted"} and encryption.is_encrypted(on_disk["encrypted"])
    assert "sk-or" not in (inf / "openrouter.json").read_text()
