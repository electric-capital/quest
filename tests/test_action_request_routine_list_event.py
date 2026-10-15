"""Regression tests: executing a create_routine / edit_routine action
request must publish a ``routine_list_changed`` per-user event.

Before the fix, approving a ``create_routine`` request created the routine
but published only ``request_count_changed`` / ``wait_handle_resolved``, so
the sidebar's cached per-project routine list stayed stale until a full
page reload.

These tests call the route function directly with an isolated DB and a
captured bus.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db, monkeypatch):
    import db.action_request_store as action_request_store_mod
    import db.conversation_store as conversation_store_mod
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod

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
        "routine_store": routine_store_mod,
        "action_request_store": action_request_store_mod,
    }


@pytest.fixture()
def _captured_bus(monkeypatch):
    from chat.action_request_routes import bus

    published: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        bus, "publish_to_user",
        lambda user_id, event: published.append((user_id, event)),
    )
    return published


class _FakeRequest:
    app = None


def _make_user(models_mod) -> dict:
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"ar-routine-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Routine Event Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {"id": u.id, "email": u.email, "name": u.name}

    return _run(_create())


def _make_project_conversation(stores, user) -> tuple[str, str]:
    project = _run(stores["project_store"].create_project(user["id"], "Proj"))
    conv_id = str(uuid.uuid4())
    _run(stores["conversation_store"].create_conversation(
        user_id=user["id"], conversation_id=conv_id,
        created_at=datetime.now(timezone.utc), project_id=project["id"],
    ))
    return project["id"], conv_id


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


def _routine_events(published, user_id):
    return [
        event for uid, event in published
        if uid == user_id and event.get("type") == "routine_list_changed"
    ]


def test_execute_create_routine_publishes_routine_list_changed(
    _isolated_db, _captured_bus,
):
    stores = _isolated_db
    user = _make_user(stores["models"])
    project_id, conv_id = _make_project_conversation(stores, user)

    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="create_routine",
        params={"name": "Daily Digest", "prompt": "Summarize the day."},
        reasoning="automate the digest",
    ))

    resolved = _resolve(req["id"], user)

    assert resolved["status"] == "executed"
    events = _routine_events(_captured_bus, user["id"])
    assert [e["project_id"] for e in events] == [project_id]


def test_execute_edit_routine_publishes_routine_list_changed(
    _isolated_db, _captured_bus,
):
    stores = _isolated_db
    user = _make_user(stores["models"])
    project_id, conv_id = _make_project_conversation(stores, user)
    routine = _run(stores["routine_store"].create_routine(
        user_id=user["id"], project_id=project_id,
        name="Daily Digest", prompt="v1",
    ))

    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="edit_routine",
        params={"routine_id": routine["id"], "prompt": "v2"},
        reasoning="update the prompt",
    ))

    resolved = _resolve(req["id"], user)

    assert resolved["status"] == "executed"
    events = _routine_events(_captured_bus, user["id"])
    assert [e["project_id"] for e in events] == [project_id]


def test_execute_non_routine_request_does_not_publish(
    _isolated_db, _captured_bus,
):
    stores = _isolated_db
    user = _make_user(stores["models"])
    project_id, conv_id = _make_project_conversation(stores, user)

    req = _run(stores["action_request_store"].create_action_request(
        user_id=user["id"],
        conversation_id=conv_id,
        request_type="create_skill",
        params={"target": "project", "name": "Port Rows", "content": "steps"},
        reasoning="save workflow",
    ))

    resolved = _resolve(req["id"], user)

    assert resolved["status"] == "executed"
    assert _routine_events(_captured_bus, user["id"]) == []
