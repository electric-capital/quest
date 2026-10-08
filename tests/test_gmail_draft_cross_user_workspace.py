"""Regression tests for security finding #279216.

``POST /api/gmail-simple/drafts`` accepted a caller-supplied ``conversation_id``
and resolved workspace attachments against that conversation's workspace
without checking the authenticated user owned it. Path validation confined
the file to the selected workspace but never authorized selecting it, so a
user who learned another user's conversation id (for example the
``subagent_conversation_id`` in a ``run_user_subagent`` launch receipt)
could attach that user's workspace files to a draft in their own Gmail.

These tests lock in the ownership guard: an unowned conversation is a 404
before any file is read or any draft is created, while owned conversations
still attach files as before.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.policy import default

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client(monkeypatch, tmp_path, owned_ids):
    """Sandbox-app client authenticated as user 123 owning ``owned_ids``.

    Every conversation id maps to ``tmp_path / <id>`` as its workspace so the
    test can stage a file in a "victim" workspace the caller does not own.
    """
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.storage import ChatStorage
    import db.conversation_store as conversation_store
    from auth.session import get_current_user
    from chat.sandbox_api import register_sandbox_api_routes

    async def get_meta(user_id, conversation_id):
        if conversation_id in owned_ids:
            return {"id": conversation_id, "user_id": user_id, "project_id": None}
        return None

    monkeypatch.setattr(conversation_store, "get_conversation_meta", get_meta, raising=True)

    def workspace_root(conversation_id):
        return tmp_path / conversation_id

    monkeypatch.setattr(
        ChatStorage, "get_conversation_workspace_root", workspace_root, raising=True,
    )

    captured = {}

    class _Call:
        def execute(self):
            return {"id": "draft-1", "message": {"id": "m1", "threadId": "t1", "labelIds": ["DRAFT"]}}

    class _Drafts:
        def create(self, *, userId, body):
            captured["body"] = body
            return _Call()

    class _Users:
        def drafts(self):
            return _Drafts()

    class _Service:
        def users(self):
            return _Users()

    async def fake_credentials(user):
        return object()

    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda credentials: _Service())

    app = FastAPI()
    register_sandbox_api_routes(app)

    async def current_user():
        return {"id": 123, "email": "attacker@example.test"}

    app.dependency_overrides[get_current_user] = current_user
    return TestClient(app), captured


def _draft_payload(conversation_id):
    return {
        "to": "attacker@example.test",
        "subject": "report",
        "body": "see attached",
        "conversation_id": conversation_id,
        "attachments": [
            {"type": "workspace", "workspace_path": "exports/secret.txt", "filename": "copy.txt"}
        ],
    }


def test_workspace_attachment_rejects_unowned_conversation(tmp_path, monkeypatch):
    client, captured = _build_client(monkeypatch, tmp_path, owned_ids={"mine"})
    staged = tmp_path / "victim" / "exports" / "secret.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("victim api key")

    response = client.post("/api/gmail-simple/drafts", json=_draft_payload("victim"))

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"] == "conversation_not_found"
    assert captured == {}, "no draft may be created for an unowned conversation"


def test_workspace_attachment_succeeds_for_owned_conversation(tmp_path, monkeypatch):
    client, captured = _build_client(monkeypatch, tmp_path, owned_ids={"mine"})
    staged = tmp_path / "mine" / "exports" / "secret.txt"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"my own file")

    response = client.post("/api/gmail-simple/drafts", json=_draft_payload("mine"))

    assert response.status_code == 200, response.text
    raw = base64.urlsafe_b64decode(captured["body"]["message"]["raw"])
    parsed = message_from_bytes(raw, policy=default)
    attachments = [part for part in parsed.walk() if part.get_filename()]
    assert [a.get_filename() for a in attachments] == ["copy.txt"]
    assert attachments[0].get_payload(decode=True) == b"my own file"


def test_ownership_check_not_applied_without_workspace_attachments(tmp_path, monkeypatch):
    """Drafts with no workspace attachments never needed a conversation and still don't."""
    client, captured = _build_client(monkeypatch, tmp_path, owned_ids=set())

    response = client.post(
        "/api/gmail-simple/drafts",
        json={"to": "attacker@example.test", "subject": "s", "body": "b", "conversation_id": "victim"},
    )

    assert response.status_code == 200, response.text
    assert "body" in captured
