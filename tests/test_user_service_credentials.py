"""Tests for the per-user plugin credential table and store.

Covers db/user_service_credential_store.py CRUD, the FK cascade on user
deletion, and db/user_store.py attaching the rows to user dicts as
``service_credentials`` (only while a loaded plugin declares a per-user
connection). Uses an isolated SQLite file per test, driven via
``asyncio.run`` to match the repo's async-test convention.
"""

import asyncio
import uuid

import pytest

from config.plugin_types import QuestPlugin, UserConnectionSpec


def _run(coro):
    return asyncio.run(coro)


def _api_key_plugin(pid: str = "acme") -> QuestPlugin:
    return QuestPlugin(
        id=pid,
        label=pid.title(),
        user_connection=UserConnectionSpec(
            kind="api_key",
            connected=lambda row: bool(row.get("secret")),
        ),
    )


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.user_service_credential_store as store_mod
    import db.user_store as user_store_mod

    return store_mod, user_store_mod


@pytest.fixture()
def _seed_user(_isolated_db):
    _store, user_store = _isolated_db
    user = _run(user_store.create_user(
        email=f"test-{uuid.uuid4().hex}@example.com",
        name="Test",
        api_key=f"k-{uuid.uuid4().hex}",
    ))
    return user


def test_upsert_get_list_delete_roundtrip(_isolated_db, _seed_user):
    store, _ = _isolated_db
    uid = _seed_user["id"]

    assert _run(store.get_credential(uid, "acme")) is None
    assert _run(store.list_credentials(uid)) == {}

    row = _run(store.upsert_credential(uid, "acme", secret="sk-1234"))
    assert row["service"] == "acme"
    assert row["secret"] == "sk-1234"
    assert row["oauth_blob"] is None
    assert row["created_at"] is not None

    fetched = _run(store.get_credential(uid, "acme"))
    assert fetched["secret"] == "sk-1234"

    listed = _run(store.list_credentials(uid))
    assert set(listed) == {"acme"}

    assert _run(store.delete_credential(uid, "acme")) is True
    assert _run(store.get_credential(uid, "acme")) is None
    # Deleting again reports nothing removed.
    assert _run(store.delete_credential(uid, "acme")) is False


def test_upsert_is_full_replacement(_isolated_db, _seed_user):
    store, _ = _isolated_db
    uid = _seed_user["id"]

    _run(store.upsert_credential(uid, "acme", oauth_blob={"access_token": "t"}))
    row = _run(store.upsert_credential(uid, "acme", secret="sk-new"))
    # Saving a key clears a stale oauth blob (and vice versa).
    assert row["secret"] == "sk-new"
    assert row["oauth_blob"] is None

    # Still one row per (user, service).
    assert list(_run(store.list_credentials(uid))) == ["acme"]


def test_rows_are_per_user_and_per_service(_isolated_db, _seed_user):
    store, user_store = _isolated_db
    uid = _seed_user["id"]
    other = _run(user_store.create_user(
        email=f"other-{uuid.uuid4().hex}@example.com",
        name="Other",
        api_key=f"k-{uuid.uuid4().hex}",
    ))

    _run(store.upsert_credential(uid, "acme", secret="a"))
    _run(store.upsert_credential(uid, "globex", secret="b"))
    _run(store.upsert_credential(other["id"], "acme", secret="c"))

    assert set(_run(store.list_credentials(uid))) == {"acme", "globex"}
    assert _run(store.get_credential(other["id"], "acme"))["secret"] == "c"

    assert _run(store.delete_all_credentials(uid)) == 2
    assert _run(store.list_credentials(uid)) == {}
    # The other user's rows survive a disconnect-all.
    assert _run(store.get_credential(other["id"], "acme")) is not None


def test_user_delete_cascades_rows(_isolated_db, _seed_user):
    store, user_store = _isolated_db
    uid = _seed_user["id"]
    _run(store.upsert_credential(uid, "acme", secret="a"))

    assert _run(user_store.delete_user(_seed_user["email"])) is True
    assert _run(store.list_credentials(uid)) == {}


def test_logout_and_disconnect_clears_plugin_credentials(
    _isolated_db, _seed_user, monkeypatch,
):
    store, _user_store = _isolated_db
    uid = _seed_user["id"]
    _run(store.upsert_credential(uid, "acme", secret="sk-1"))

    import chat.routes.user as user_routes
    result = _run(user_routes.logout_and_disconnect(
        request=None, user=_seed_user,
    ))
    assert result.status_code == 200
    assert _run(store.list_credentials(uid)) == {}


def test_user_dict_attach_gated_on_loaded_plugins(
    _isolated_db, _seed_user, monkeypatch,
):
    store, user_store = _isolated_db
    uid = _seed_user["id"]
    email = _seed_user["email"]
    _run(store.upsert_credential(uid, "acme", secret="sk-1234"))

    from config import plugins as plugins_mod

    # No plugin declares a user connection -> the extra query is skipped
    # and the key is absent entirely.
    monkeypatch.setattr(plugins_mod, "_LOADED", [])
    user = _run(user_store.get_user_by_email(email))
    assert "service_credentials" not in user

    # With an api_key plugin loaded the rows ride on the user dict.
    monkeypatch.setattr(plugins_mod, "_LOADED", [_api_key_plugin("acme")])
    for load in (
        lambda: user_store.get_user_by_email(email),
        lambda: user_store.get_user_by_id(uid),
        lambda: user_store.get_user_by_api_key(_seed_user["api_key"]),
        lambda: user_store.update_user_field(email, name="Renamed"),
    ):
        user = _run(load())
        assert user["service_credentials"]["acme"]["secret"] == "sk-1234"

    # A user with no rows keeps the omit-when-absent style.
    other = _run(user_store.create_user(
        email=f"empty-{uuid.uuid4().hex}@example.com",
        name="Empty",
        api_key=f"k-{uuid.uuid4().hex}",
    ))
    fetched = _run(user_store.get_user_by_email(other["email"]))
    assert "service_credentials" not in fetched
