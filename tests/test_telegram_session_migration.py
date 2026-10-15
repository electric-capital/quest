"""End-to-end test of migration d7a1f3c9e2b4 (users.telegram_session ->
user_service_credentials rows for the Telegram plugin).

Builds a database at the encryption revision in a temp data dir, seeds
users whose ``telegram_session`` is encrypted under the old column label
(plus one legacy plaintext row, one without a session, and one that
already has a ``telegram`` credential row), upgrades to head, and checks:
the column is gone, each session landed as ``{"session": ...}`` in an
encrypted ``oauth_blob`` readable through the ORM store, the existing row
won, re-running is a no-op, and downgrade restores the column with the
value re-encrypted under the old label.

Alembic, the seeding and the ORM read-back run in this process against the
temp deployment set up by ``tests/migration_support.py``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from config import encryption
from tests.migration_support import PASSWORD, columns, deployment, rows, run  # noqa: F401

ENCRYPTION_REV = "c4e8a2d17f63"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _seed(db_path) -> None:
    enc = lambda s: encryption.encrypt_str(s, "users.telegram_session")  # noqa: E731
    blob = lambda d: json.dumps(encryption.encrypt_json(d, "user_service_credentials.oauth_blob"))  # noqa: E731
    conn = sqlite3.connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, telegram_session, created_at) "
               "VALUES (?, ?, ?, ?, '{}', ?, CURRENT_TIMESTAMP)")
        conn.execute(ins, (1, 'a@example.com', 'A', 'key-a', enc('tg-a')))
        conn.execute(ins, (2, 'b@example.com', 'B', 'key-b', 'tg-b-plaintext'))
        conn.execute(ins, (3, 'c@example.com', 'C', 'key-c', None))
        conn.execute(ins, (4, 'd@example.com', 'D', 'key-d', enc('tg-d-stale')))
        conn.execute(
            "INSERT INTO user_service_credentials (user_id, service, oauth_blob, created_at, updated_at) "
            "VALUES (4, 'telegram', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (blob({"session": "tg-d-existing"}),),
        )
        conn.execute(
            "INSERT INTO user_service_credentials (user_id, service, oauth_blob, created_at, updated_at) "
            "VALUES (1, 'slack', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (blob({"access_token": "xoxp-a"}),),
        )
        conn.commit()
    finally:
        conn.close()


def _read_rows() -> dict:
    """Each user's telegram blob (and user 1's slack blob) through the store."""
    from db.user_service_credential_store import get_credential

    async def _read():
        out = {}
        for uid in (1, 2, 3, 4):
            row = await get_credential(uid, "telegram")
            out[str(uid)] = row and row["oauth_blob"]
        out["slack"] = (await get_credential(1, "slack"))["oauth_blob"]
        return out

    return run(_read())


def _read_column(db_path) -> dict:
    """users.telegram_session decrypted under the old column label."""
    out = {}
    for uid, raw in rows(db_path, "SELECT id, telegram_session FROM users ORDER BY id"):
        if raw is None:
            out[str(uid)] = None
        else:
            assert encryption.is_encrypted(raw), raw
            out[str(uid)] = encryption.decrypt_str(raw, "users.telegram_session")
    return out


def test_sessions_move_into_credential_rows_and_back(keyed):
    db_path = keyed.db_path

    keyed.alembic("upgrade", ENCRYPTION_REV)
    _seed(db_path)

    keyed.alembic("upgrade", "head")

    assert "telegram_session" not in columns(db_path, "users")
    # Everything in the table is ciphertext: no session string is readable.
    raw = " ".join(str(v) for row in rows(db_path, "SELECT oauth_blob FROM user_service_credentials") for v in row)
    for secret in ("tg-a", "tg-b-plaintext", "tg-d-existing"):
        assert secret not in raw

    read = _read_rows()
    assert read["1"] == {"session": "tg-a"}
    assert read["2"] == {"session": "tg-b-plaintext"}  # legacy plaintext row accepted
    assert read["3"] is None
    assert read["4"] == {"session": "tg-d-existing"}  # existing row wins
    assert read["slack"] == {"access_token": "xoxp-a"}  # untouched
    assert rows(db_path, "SELECT COUNT(*) FROM user_service_credentials WHERE service = 'telegram'") == [(3,)]

    # Re-running at head is a no-op.
    keyed.alembic("upgrade", "head")
    assert _read_rows() == read

    # Downgrade restores the column (re-encrypted under the old label) and
    # removes the rows.
    keyed.alembic("downgrade", ENCRYPTION_REV)
    assert "telegram_session" in columns(db_path, "users")
    assert _read_column(db_path) == {
        "1": "tg-a", "2": "tg-b-plaintext", "3": None, "4": "tg-d-existing",
    }
    assert rows(db_path, "SELECT COUNT(*) FROM user_service_credentials WHERE service = 'telegram'") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM user_service_credentials WHERE service = 'slack'") == [(1,)]


def test_upgrade_refuses_without_the_key(keyed):
    keyed.alembic("upgrade", ENCRYPTION_REV)
    with keyed.env(QUEST_ENCRYPTION_PASSWORD="wrong-password"):
        with pytest.raises(RuntimeError, match="encryption-at-rest key"):
            keyed.alembic("upgrade", "head")
    assert "telegram_session" in columns(keyed.db_path, "users")
