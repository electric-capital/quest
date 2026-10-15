"""Tests for ``db.action_request_store.list_open_request_summaries``.

The cross-user aggregate the Slack pending-request notifier polls: one row
per user with open action requests, carrying the open count and the oldest
open row's created_at. Runs against an isolated temp SQLite database.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.action_request_store as store_mod

    return store_mod, isolated_db.models


def _create_user(models_mod):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"summaries-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Summary Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _create_request(store_mod, user_id, created_at=None):
    req = _run(store_mod.create_action_request(
        user_id=user_id,
        conversation_id=str(uuid.uuid4()),
        request_type="send_slack_message",
        params={"text": "hi"},
        reasoning="test",
    ))
    if created_at is not None:
        from db.engine import AsyncSessionLocal
        from db.models import ActionRequest

        async def _backdate():
            async with AsyncSessionLocal() as db:
                row = await db.get(ActionRequest, req["id"])
                row.created_at = created_at
                await db.commit()

        _run(_backdate())
    return req


def test_empty_db_returns_no_summaries(_isolated_db):
    store_mod, _ = _isolated_db
    assert _run(store_mod.list_open_request_summaries()) == []


def test_groups_open_requests_per_user_with_oldest(_isolated_db):
    store_mod, models_mod = _isolated_db
    user_a = _create_user(models_mod)
    user_b = _create_user(models_mod)

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    _create_request(store_mod, user_a, created_at=old)
    _create_request(store_mod, user_a)
    _create_request(store_mod, user_b)

    summaries = {s["user_id"]: s for s in _run(store_mod.list_open_request_summaries())}
    assert set(summaries) == {user_a, user_b}
    assert summaries[user_a]["open_count"] == 2
    assert summaries[user_b]["open_count"] == 1
    oldest = summaries[user_a]["oldest_created_at"]
    assert isinstance(oldest, datetime)
    assert abs((oldest - old).total_seconds()) < 1


def test_resolved_requests_are_excluded(_isolated_db):
    store_mod, models_mod = _isolated_db
    user_id = _create_user(models_mod)
    req = _create_request(store_mod, user_id)
    _run(store_mod.resolve_action_request(
        user_id, req["id"], models_mod.ActionRequestStatus.EXECUTED, result={},
    ))
    assert _run(store_mod.list_open_request_summaries()) == []
