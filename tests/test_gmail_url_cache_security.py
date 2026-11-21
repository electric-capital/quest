"""Regression tests for security finding #279217.

An authenticated user could upload a crafted ``url_mappings.sqlite`` into
their own workspace, then point the Gmail Simple URL-lookup route at it via a
path-fragment ``conversation_id`` (e.g. ``<id>/workspace/cache``). The cache
was read through SqliteDict's default pickle codec, executing code in the
server process. These tests lock in the two-part fix: (1) the cache path
resolver rejects non-canonical conversation ids, and (2) values are stored and
read as JSON, never pickled, so a planted database cannot execute code. They
also cover the ownership guard on the lookup routes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlitedict import SqliteDict


def _build_client(monkeypatch, tmp_path, owned_ids):
    import api.gmail.helpers as gmail_helpers
    import chat.storage as storage_mod
    from auth.session import get_current_user
    from chat.file_routes import router as file_router
    from chat.sandbox_api import register_sandbox_api_routes

    chats_dir = tmp_path / "chats"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats_dir, raising=True)

    async def get_meta(user_id, conversation_id):
        if conversation_id in owned_ids:
            return {"id": conversation_id, "user_id": user_id, "project_id": None}
        return None

    import db.conversation_store as conversation_store

    monkeypatch.setattr(conversation_store, "get_conversation_meta", get_meta, raising=True)

    app = FastAPI()
    app.include_router(file_router)
    register_sandbox_api_routes(app)

    async def current_user():
        return {"id": 123, "email": "attacker@example.test"}

    app.dependency_overrides[get_current_user] = current_user
    import chat.file_routes as file_routes

    app.dependency_overrides[file_routes.get_current_user_cookie_or_apikey_checked] = (
        current_user
    )
    return TestClient(app), chats_dir


def test_uploaded_pickle_database_cannot_execute_code(tmp_path, monkeypatch):
    client, _ = _build_client(monkeypatch, tmp_path, owned_ids={"owned"})
    marker = tmp_path / "code-executed"

    class ExecuteOnUnpickle:
        def __reduce__(self):
            return (Path.write_text, (marker, "pwned"))

    crafted = tmp_path / "url_mappings.sqlite"
    with SqliteDict(str(crafted), tablename="url_mappings", autocommit=True) as db:
        db["m1"] = ExecuteOnUnpickle()

    uploaded = client.post(
        "/app/api/conversations/owned/files/upload",
        params={"path": "cache"},
        files={"files": ("url_mappings.sqlite", crafted.read_bytes(), "application/octet-stream")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["errors"] == []

    response = client.get(
        "/api/gmail-simple/urls/m1",
        params={"conversation_id": "owned/workspace/cache"},
    )

    assert not marker.exists(), "pickle payload executed -- RCE still live"
    # Non-canonical id is rejected before any file is opened.
    assert response.status_code == 404, response.text


def test_path_fragment_conversation_id_is_rejected():
    from api.gmail.helpers import _get_url_cache_path

    for bad in ["owned/workspace/cache", "../etc/passwd", "a/b", "", "."]:
        with pytest.raises(ValueError):
            _get_url_cache_path(bad)


def test_legacy_pickle_cache_degrades_to_miss(tmp_path, monkeypatch):
    import api.gmail.helpers as gmail_helpers
    import chat.storage as storage_mod

    chats_dir = tmp_path / "chats"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats_dir, raising=True)

    marker = tmp_path / "legacy-executed"

    class ExecuteOnUnpickle:
        def __reduce__(self):
            return (Path.write_text, (marker, "pwned"))

    legacy = chats_dir / "conv" / "url_mappings.sqlite"
    legacy.parent.mkdir(parents=True)
    with SqliteDict(str(legacy), tablename="url_mappings", autocommit=True) as db:
        db["m1"] = ExecuteOnUnpickle()  # default pickle codec

    result = gmail_helpers._get_cached_url_mapping("conv", "m1")
    assert result is None
    assert not marker.exists()


def test_json_roundtrip_preserves_int_keys(tmp_path, monkeypatch):
    import api.gmail.helpers as gmail_helpers
    import chat.storage as storage_mod

    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats", raising=True)
    gmail_helpers._cache_url_mapping("conv", "m1", {1: "https://example.com/a", 2: "https://example.com/b"})
    got = gmail_helpers._get_cached_url_mapping("conv", "m1")
    assert got == {1: "https://example.com/a", 2: "https://example.com/b"}
    assert all(isinstance(k, int) for k in got)


def test_lookup_rejects_unowned_conversation(tmp_path, monkeypatch):
    import api.gmail.helpers as gmail_helpers

    client, _ = _build_client(monkeypatch, tmp_path, owned_ids={"mine"})
    gmail_helpers._cache_url_mapping("victim", "m1", {1: "https://secret/url"})

    response = client.get(
        "/api/gmail-simple/urls/m1", params={"conversation_id": "victim"}
    )
    assert response.status_code == 404, response.text


def test_lookup_succeeds_for_owned_conversation(tmp_path, monkeypatch):
    import api.gmail.helpers as gmail_helpers

    client, _ = _build_client(monkeypatch, tmp_path, owned_ids={"mine"})
    gmail_helpers._cache_url_mapping("mine", "m1", {1: "https://ok/url"})

    response = client.get(
        "/api/gmail-simple/urls/m1", params={"conversation_id": "mine"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["urls"] == {"1": "https://ok/url"}
