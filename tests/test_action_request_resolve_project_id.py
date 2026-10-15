"""Regression tests: the action-request approve route must pass the
conversation's project_id to handler.execute.

Before the fix, ``POST /action-requests/{id}/resolve`` called
``handler.execute(...)`` without ``project_id``, so approving a
``create_skill`` request with ``target="project"`` (or an ``edit_skill``
against a project skill) always failed with "This conversation is not in
a project ..." even though the proposal-time pre-card check had passed.

These tests call the route function directly with an isolated DB.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db, monkeypatch):
    import db.action_request_store as action_request_store_mod
    import db.conversation_store as conversation_store_mod
    import db.project_store as project_store_mod
    import db.skill_store as skill_store_mod

    # The route's best-effort on-disk chat-history mirror is irrelevant
    # here and would touch the real data dir; make it a no-op.
    from chat.storage import ChatStorage
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )

    return {
        "models": isolated_db.models,
        "project_store": project_store_mod,
        "conversation_store": conversation_store_mod,
        "skill_store": skill_store_mod,
        "action_request_store": action_request_store_mod,
    }


class _FakeRequest:
    app = None


def _make_user(models_mod) -> dict:
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"ar-resolve-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Resolve Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {"id": u.id, "email": u.email, "name": u.name}

    return _run(_create())


def _resolve(request_id: int, user: dict):
    from chat.action_request_routes import (
        ResolveRequestBody,
        resolve_user_action_request,
    )
    return _run(resolve_user_action_request(
        request_id,
        ResolveRequestBody(action="execute"),
        _FakeRequest(),
        user,
    ))


def test_approve_create_project_skill_in_project_conversation(_isolated_db):
    """Approving create_skill(target=project) succeeds when the request's
    conversation belongs to a project the user owns (the regression)."""
    stores = _isolated_db
    user = _make_user(stores["models"])

    project = _run(stores["project_store"].create_project(user["id"], "Proj"))
    conv_id = str(uuid.uuid4())
    _run(stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc), project_id=project["id"],
    ))
    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="create_skill",
        params={"target": "project", "name": "Port Rows", "content": "steps"},
        reasoning="save workflow",
    ))

    resolved = _resolve(req["id"], user)

    assert resolved["status"] == "executed"
    assert resolved["result"]["success"] is True
    skills = _run(stores["skill_store"].list_project_skills(project["id"]))
    assert [s["name"] for s in skills] == ["Port Rows"]


def test_approve_edit_project_skill_in_project_conversation(_isolated_db):
    """Approving edit_skill against a project skill succeeds from a
    conversation in that project (same regression, edit path)."""
    stores = _isolated_db
    user = _make_user(stores["models"])

    project = _run(stores["project_store"].create_project(user["id"], "Proj"))
    conv_id = str(uuid.uuid4())
    _run(stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc), project_id=project["id"],
    ))
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Port Rows", description="",
        content="v1", project_id=project["id"],
    ))
    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="edit_skill",
        params={"skill_id": skill["id"], "content": "v2"},
        reasoning="update steps",
    ))

    resolved = _resolve(req["id"], user)

    assert resolved["status"] == "executed"
    updated = _run(stores["skill_store"].get_skill(skill["id"]))
    assert updated["content"] == "v2"


def test_approve_create_project_skill_outside_project_still_fails(_isolated_db):
    """The execute-time guard still rejects target=project when the
    conversation is standalone (project_id resolves to None)."""
    stores = _isolated_db
    user = _make_user(stores["models"])

    conv_id = str(uuid.uuid4())
    _run(stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc),
    ))
    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="create_skill",
        params={"target": "project", "name": "Port Rows", "content": "steps"},
        reasoning="save workflow",
    ))

    with pytest.raises(HTTPException) as exc_info:
        _resolve(req["id"], user)

    assert exc_info.value.status_code == 500
    assert "not in a project" in exc_info.value.detail["message"]
    # The request stays open for retry.
    row = _run(stores["action_request_store"].get_action_request(
        user["id"], req["id"],
    ))
    assert row["status"] == "open"
