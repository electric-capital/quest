"""Tests for the generic per-user API key routes (auth/service_key.py).

POST /auth/service-key/{service} and .../remove serve every loaded plugin
with an ``api_key`` user connection; unknown services (and oauth-kind
connections, which use the plugin's own /auth/<id> router instead) 404.
Keys land in the user_service_credentials table via the store.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import auth.service_key as service_key_mod
from config import plugins as plugins_mod
from config.plugin_types import QuestPlugin, UserConnectionSpec


def _run(coro):
    return asyncio.run(coro)


class _FakeRequest:
    def __init__(self, body=None, cookies=None):
        self._body = body
        self.cookies = cookies if cookies is not None else {"quest_session": "x"}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _plugin(pid: str = "acme", *, kind: str = "api_key", validate_key=None):
    return QuestPlugin(
        id=pid,
        label=pid.title(),
        user_connection=UserConnectionSpec(
            kind=kind,
            connected=lambda row: bool(row.get("secret")),
            validate_key=validate_key,
        ),
    )


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_service_key_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    from importlib import reload
    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.user_service_credential_store as store_mod
    reload(store_mod)
    import db.user_store as user_store_mod
    reload(user_store_mod)

    models_mod.Base.metadata.create_all(engine_mod.engine)

    yield store_mod, user_store_mod

    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def _seed_user(_isolated_db):
    _store, user_store = _isolated_db
    return _run(user_store.create_user(
        email=f"test-{uuid.uuid4().hex}@example.com",
        name="Test",
        api_key=f"k-{uuid.uuid4().hex}",
    ))


@pytest.fixture()
def _as_user(_seed_user, monkeypatch):
    async def _fake_get_user_from_cookie(_cookie):
        return _seed_user

    monkeypatch.setattr(
        service_key_mod, "get_user_from_cookie", _fake_get_user_from_cookie,
    )
    monkeypatch.setattr(service_key_mod, "COOKIE_NAME", "quest_session")
    return _seed_user


def _save(service, body):
    return _run(service_key_mod.save_service_key(service, _FakeRequest(body)))


def _remove(service):
    return _run(service_key_mod.remove_service_key(service, _FakeRequest({})))


def test_unknown_service_404s(_as_user):
    with patch.object(plugins_mod, "_LOADED", []):
        with pytest.raises(HTTPException) as exc:
            _save("nope", {"api_key": "sk-12345678"})
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            _remove("nope")
        assert exc.value.status_code == 404


def test_oauth_kind_connection_404s(_as_user):
    with patch.object(plugins_mod, "_LOADED", [_plugin(kind="oauth")]):
        with pytest.raises(HTTPException) as exc:
            _save("acme", {"api_key": "sk-12345678"})
        assert exc.value.status_code == 404


def test_multi_connection_api_key_plugin_404s(_isolated_db, _as_user):
    # Multi-connection keys are managed per connection (/auth/connections);
    # the service-wide routes would bypass the account binding.
    store, _ = _isolated_db
    plugin = QuestPlugin(
        id="acme", label="Acme",
        user_connection=UserConnectionSpec(
            kind="api_key", connected=lambda row: bool(row.get("secret")),
            multi_connection=True, account_from=lambda row: "acct",
        ),
    )
    with patch.object(plugins_mod, "_LOADED", [plugin]):
        for call in (lambda: _save("acme", {"api_key": "sk-12345678"}),
                     lambda: _remove("acme")):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 404
            assert exc.value.detail["error"] == "unknown_service"
    assert _run(store.get_credential(_as_user["id"], "acme")) is None


def test_missing_cookie_is_401(_isolated_db):
    with patch.object(plugins_mod, "_LOADED", [_plugin()]):
        with pytest.raises(HTTPException) as exc:
            _run(service_key_mod.save_service_key(
                "acme", _FakeRequest({"api_key": "sk-1"}, cookies={}),
            ))
        assert exc.value.status_code == 401


def test_empty_key_is_400(_as_user):
    with patch.object(plugins_mod, "_LOADED", [_plugin()]):
        with pytest.raises(HTTPException) as exc:
            _save("acme", {"api_key": "   "})
        assert exc.value.status_code == 400


def test_validate_key_hook_rejects(_as_user):
    plugin = _plugin(
        validate_key=lambda key: None if key.startswith("sk-") else "Bad key format",
    )
    with patch.object(plugins_mod, "_LOADED", [plugin]):
        with pytest.raises(HTTPException) as exc:
            _save("acme", {"api_key": "nope"})
        assert exc.value.status_code == 400
        assert exc.value.detail == "Bad key format"


def test_save_and_remove_roundtrip(_isolated_db, _as_user):
    store, _ = _isolated_db
    uid = _as_user["id"]
    with patch.object(plugins_mod, "_LOADED", [_plugin()]):
        assert _save("acme", {"api_key": " sk-12345678 "})["success"] is True
        row = _run(store.get_credential(uid, "acme"))
        assert row["secret"] == "sk-12345678"  # stripped

        # Saving again replaces the key.
        _save("acme", {"api_key": "sk-87654321"})
        assert _run(store.get_credential(uid, "acme"))["secret"] == "sk-87654321"

        assert _remove("acme")["success"] is True
        assert _run(store.get_credential(uid, "acme")) is None
        # Removing an absent key stays a success (idempotent disconnect).
        assert _remove("acme")["success"] is True
