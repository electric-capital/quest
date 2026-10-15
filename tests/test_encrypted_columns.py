"""Tests for the encrypted ORM column types (db/encrypted_types.py) as used
by db/models.py: ciphertext on disk, plaintext through the ORM, hash-based
API-key lookup, the decrypt-in-Python Slack reverse lookup, and legacy
plaintext tolerance.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest

from config import encryption
from config.plugin_types import QuestPlugin, UserConnectionSpec


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(isolated_db):
    """The shared fresh-sqlite fixture plus the stores this file drives and
    the raw file path for ciphertext inspection."""
    import db.user_service_credential_store as usc_mod
    import db.user_store as user_store_mod

    return {
        "db_path": str(isolated_db.path),
        "models": isolated_db.models,
        "user_store": user_store_mod,
        "usc_store": usc_mod,
    }


def _raw(db_path: str, sql: str, *params):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_user_secret_columns_are_ciphertext_on_disk(isolated_db):
    user_store = isolated_db["user_store"]
    email = f"u-{uuid.uuid4().hex}@example.com"
    api_key = f"k-{uuid.uuid4().hex}"
    _run(user_store.create_user(email=email, name="U", api_key=api_key))
    _run(user_store.update_user_field(
        email,
        google_oauth={"access_token": "g-token", "refresh_token": "g-refresh"},
        google_services_oauth={"access_token": "svc-token"},
        airtable_token="pat-airtable",
        ramp_oauth={"access_token": "ramp-token"},
    ))

    (row,) = _raw(
        isolated_db["db_path"],
        "SELECT api_key, api_key_hash, google_oauth, google_services_oauth, "
        "airtable_token, ramp_oauth FROM users WHERE email = ?",
        email,
    )
    stored_api_key, stored_hash, g, gs, at, ramp = row
    assert encryption.is_encrypted(stored_api_key) and api_key not in stored_api_key
    assert stored_hash == encryption.hash_api_key(api_key)
    assert encryption.is_encrypted(at)
    for json_value in (g, gs, ramp):
        # JSON columns hold the JSON string literal of the envelope
        assert encryption.is_encrypted(json.loads(json_value))
    for secret in ("g-token", "g-refresh", "svc-token", "pat-airtable", "ramp-token"):
        assert secret not in " ".join(str(v) for v in row)

    # ... and plaintext through the ORM
    user = _run(user_store.get_user_by_email(email))
    assert user["api_key"] == api_key
    assert user["google_oauth"] == {"access_token": "g-token", "refresh_token": "g-refresh"}
    assert user["google_services_oauth"] == {"access_token": "svc-token"}
    assert user["airtable_token"] == "pat-airtable"
    assert user["ramp_oauth"] == {"access_token": "ramp-token"}
    assert "api_key_hash" not in user


def test_api_key_lookup_uses_the_hash_and_follows_resets(isolated_db):
    user_store = isolated_db["user_store"]
    email = f"u-{uuid.uuid4().hex}@example.com"
    api_key = f"k-{uuid.uuid4().hex}"
    _run(user_store.create_user(email=email, name="U", api_key=api_key))
    assert _run(user_store.get_user_by_api_key(api_key))["email"] == email
    assert _run(user_store.get_user_by_api_key("nope")) is None
    assert _run(user_store.get_user_by_api_key("")) is None

    new_key = f"k-{uuid.uuid4().hex}"
    _run(user_store.update_user_field(email, api_key=new_key))
    assert _run(user_store.get_user_by_api_key(api_key)) is None
    assert _run(user_store.get_user_by_api_key(new_key))["email"] == email
    (row,) = _raw(isolated_db["db_path"], "SELECT api_key_hash FROM users WHERE email = ?", email)
    assert row[0] == encryption.hash_api_key(new_key)


def test_removing_a_secret_field_stores_null(isolated_db):
    user_store = isolated_db["user_store"]
    email = f"u-{uuid.uuid4().hex}@example.com"
    _run(user_store.create_user(email=email, name="U", api_key=f"k-{uuid.uuid4().hex}"))
    _run(user_store.update_user_field(email, airtable_token="pat", ramp_oauth={"a": 1}))
    _run(user_store.remove_user_fields(email, ["airtable_token", "ramp_oauth"]))
    user = _run(user_store.get_user_by_email(email))
    assert "airtable_token" not in user and "ramp_oauth" not in user


def test_service_credential_rows_are_ciphertext_and_slack_lookup_decrypts(isolated_db, monkeypatch):
    user_store = isolated_db["user_store"]
    usc = isolated_db["usc_store"]
    # A loaded plugin with a user connection makes _user_dict attach rows.
    from config import plugins as plugins_mod
    plugin = QuestPlugin(
        id="acme", label="Acme",
        user_connection=UserConnectionSpec(kind="api_key", connected=lambda row: bool(row.get("secret"))),
    )
    monkeypatch.setattr(plugins_mod, "get_loaded_plugins", lambda: [plugin])

    email = f"u-{uuid.uuid4().hex}@example.com"
    user = _run(user_store.create_user(email=email, name="U", api_key=f"k-{uuid.uuid4().hex}"))
    _run(usc.upsert_credential(user["id"], "acme", secret="acme-api-key"))
    _run(usc.upsert_credential(user["id"], "slack", oauth_blob={"access_token": "xoxp-1", "user_id": "U123"}))

    rows = _raw(isolated_db["db_path"], "SELECT service, secret, oauth_blob FROM user_service_credentials")
    for service, secret, blob in rows:
        if service == "acme":
            assert encryption.is_encrypted(secret) and "acme-api-key" not in secret
        else:
            assert encryption.is_encrypted(json.loads(blob)) and "xoxp-1" not in blob

    loaded = _run(user_store.get_user_by_email(email))
    assert loaded["service_credentials"]["acme"]["secret"] == "acme-api-key"
    assert loaded["service_credentials"]["slack"]["oauth_blob"]["user_id"] == "U123"

    assert _run(user_store.get_user_by_slack_user_id("U123"))["email"] == email
    assert _run(user_store.get_user_by_slack_user_id("U999")) is None
    assert _run(user_store.get_user_by_slack_user_id("")) is None


def test_legacy_plaintext_rows_are_still_readable(isolated_db):
    """A row written by a pre-encryption process reads back as-is."""
    user_store = isolated_db["user_store"]
    email = f"u-{uuid.uuid4().hex}@example.com"
    api_key = f"k-{uuid.uuid4().hex}"
    _run(user_store.create_user(email=email, name="U", api_key=api_key))
    conn = sqlite3.connect(isolated_db["db_path"])
    try:
        conn.execute(
            "UPDATE users SET google_oauth = ?, airtable_token = ? WHERE email = ?",
            (json.dumps({"access_token": "plain"}), "plain-token", email),
        )
        conn.commit()
    finally:
        conn.close()
    user = _run(user_store.get_user_by_email(email))
    assert user["google_oauth"] == {"access_token": "plain"}
    assert user["airtable_token"] == "plain-token"


def test_ciphertext_from_another_column_is_rejected(isolated_db):
    user_store = isolated_db["user_store"]
    email = f"u-{uuid.uuid4().hex}@example.com"
    _run(user_store.create_user(email=email, name="U", api_key=f"k-{uuid.uuid4().hex}"))
    _run(user_store.update_user_field(email, ramp_oauth={"access_token": "ramp"}))
    conn = sqlite3.connect(isolated_db["db_path"])
    try:
        conn.execute("UPDATE users SET google_oauth = ramp_oauth WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(encryption.EncryptionError, match="failed authentication"):
        _run(user_store.get_user_by_email(email))
