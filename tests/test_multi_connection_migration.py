"""End-to-end test of migration e3c9b7a1d5f2 (multi-connection
user_service_credentials: account / description / is_default + index swap).

Builds a database at the previous head in a temp data dir, seeds legacy
credential rows whose secret / oauth_blob are encrypted envelopes, upgrades
to head, and checks: legacy rows become account '' / default, ciphertext is
untouched and still decrypts through the ORM store, the index swap landed
(incl. the partial default index), the ORM metadata matches the migrated
table, and the downgrade refuses while a (user, service) holds several rows
but succeeds -- restoring the old unique index -- once each has one.

Alembic, the seeding and the ORM read-back run in this process against the
temp deployment set up by ``tests/migration_support.py``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from config import encryption
from tests.migration_support import (  # noqa: F401
    PASSWORD,
    deployment,
    execute,
    metadata_drift,
    rows,
    run,
)

REV = "e3c9b7a1d5f2"
PREVIOUS = "b4d7e2a9c6f1"


@pytest.fixture()
def keyed(deployment):
    encryption.create_key_file(PASSWORD, deployment.key_file)
    return deployment


def _indexes(db_path):
    return dict(rows(
        db_path,
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'user_service_credentials'",
    ))


def _seed(db_path) -> None:
    sec = lambda s: encryption.encrypt_str(s, "user_service_credentials.secret")  # noqa: E731
    blob = lambda d: json.dumps(encryption.encrypt_json(d, "user_service_credentials.oauth_blob"))  # noqa: E731
    conn = sqlite3.connect(db_path)
    try:
        ins = ("INSERT INTO users (id, email, name, api_key, settings, created_at) "
               "VALUES (?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)")
        conn.execute(ins, (1, 'a@example.com', 'A', 'key-a'))
        conn.execute(ins, (2, 'b@example.com', 'B', 'key-b'))
        row = ("INSERT INTO user_service_credentials (user_id, service, secret, oauth_blob, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        conn.execute(row, (1, 'acme', sec('sk-a'), None))
        conn.execute(row, (1, 'github', None, blob({"access_token": "gho-a"})))
        conn.execute(row, (2, 'acme', sec('sk-b'), None))
        conn.commit()
    finally:
        conn.close()


def _read() -> dict:
    from db.user_service_credential_store import get_credential, list_connections

    async def _read_all():
        return {
            "a_acme": await get_credential(1, "acme"),
            "a_github": await get_credential(1, "github"),
            "b_acme": await list_connections(2, "acme"),
        }

    return run(_read_all())


def test_upgrade_downgrade_roundtrip(keyed):
    db_path = keyed.db_path

    keyed.alembic("upgrade", PREVIOUS)
    _seed(db_path)
    before = rows(db_path, "SELECT id, secret, oauth_blob FROM user_service_credentials ORDER BY id")

    keyed.alembic("upgrade", REV)

    # Legacy rows: account '', no description, default; ciphertext verbatim.
    after = rows(
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
    fks = rows(db_path, "PRAGMA foreign_key_list(user_service_credentials)")
    assert [(fk[2], fk[3], fk[4], fk[6]) for fk in fks] == [("users", "user_id", "id", "CASCADE")]

    # The ORM reads the migrated rows back through the store.
    read = _read()
    assert read["a_acme"]["secret"] == "sk-a"
    assert read["a_acme"]["account"] == "" and read["a_acme"]["is_default"] is True
    assert read["a_github"]["oauth_blob"] == {"access_token": "gho-a"}
    assert [r["secret"] for r in read["b_acme"]] == ["sk-b"]

    # ORM metadata matches the migrated table's columns and index columns /
    # uniqueness. compare_metadata does NOT compare sqlite_where or server
    # defaults; the index-SQL and legacy-row assertions above cover those.
    assert [d for d in metadata_drift(db_path) if "user_service_credentials" in d] == []

    # A second connection and a second default.
    execute(
        db_path,
        "INSERT INTO user_service_credentials (user_id, service, account, is_default, created_at, updated_at) "
        "VALUES (1, 'acme', 'second', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    )
    with pytest.raises(sqlite3.IntegrityError):
        execute(
            db_path,
            "INSERT INTO user_service_credentials (user_id, service, account, is_default, created_at, updated_at) "
            "VALUES (1, 'acme', 'third', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        )

    # Downgrade refuses while (1, acme) has two rows; nothing changes.
    with pytest.raises(RuntimeError, match=r"user 1 / acme \(2 rows\)"):
        keyed.alembic("downgrade", PREVIOUS)
    assert "is_default" in {r[1] for r in rows(db_path, "PRAGMA table_info(user_service_credentials)")}
    assert rows(db_path, "SELECT version_num FROM alembic_version") == [(REV,)]

    # With one row per (user, service) it succeeds.
    execute(db_path, "DELETE FROM user_service_credentials WHERE account = 'second'")
    keyed.alembic("downgrade", PREVIOUS)
    columns = {r[1] for r in rows(db_path, "PRAGMA table_info(user_service_credentials)")}
    assert not columns & {"account", "description", "is_default"}
    assert rows(db_path, "SELECT id, secret, oauth_blob FROM user_service_credentials ORDER BY id") == before
    indexes = _indexes(db_path)
    assert "UNIQUE" in indexes["ix_user_service_credentials_user_id_service"]
    assert "ix_user_service_credentials_user_id" in indexes
    assert "ix_user_service_credentials_user_service_account" not in indexes
    assert "ix_user_service_credentials_user_service_default" not in indexes
    fks = rows(db_path, "PRAGMA foreign_key_list(user_service_credentials)")
    assert [(fk[2], fk[3], fk[4], fk[6]) for fk in fks] == [("users", "user_id", "id", "CASCADE")]
    # Users survived the table rebuild (no cascade in the wrong direction).
    assert rows(db_path, "SELECT COUNT(*) FROM users") == [(2,)]

    # And upgrades again cleanly.
    keyed.alembic("upgrade", "head")
    assert rows(db_path, "SELECT COUNT(*) FROM user_service_credentials WHERE is_default") == [(3,)]
