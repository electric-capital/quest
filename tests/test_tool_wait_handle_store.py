"""Tests for db.tool_wait_handle_store CRUD helpers.

Uses an isolated SQLite file so each test run starts with a fresh schema.
Async coroutines are driven via ``asyncio.run`` to match the existing
async-test convention in this repo.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.tool_wait_handle_store as store_mod

    return store_mod, isolated_db.models


@pytest.fixture()
def _seed_user(_isolated_db):
    _store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"test-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Test",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def test_create_handle_populates_fields(_isolated_db, _seed_user):
    store, _models = _isolated_db
    handle = _run(store.create_handle(
        user_id=_seed_user,
        conversation_id="conv-1",
        kind="slack_reply",
        tool_id="slack_reply_abc",
        payload={"content": "Likes tea"},
    ))
    assert handle["id"]
    assert handle["status"] == "pending"
    assert handle["payload"] == {"content": "Likes tea"}
    assert handle["resolved_at"] is None
    fetched = _run(store.get_handle(handle["id"]))
    assert fetched is not None
    assert fetched["id"] == handle["id"]


def test_resolve_handle_flips_status(_isolated_db, _seed_user):
    store, _models = _isolated_db
    handle = _run(store.create_handle(
        user_id=_seed_user, conversation_id="conv-1",
        kind="slack_reply", tool_id="t1", payload={"content": "x"},
    ))
    updated = _run(store.resolve_handle(
        handle["id"], new_status="accepted",
        response={"memory_id": "m-1"},
        correlation_kind="memory", correlation_id="m-1",
    ))
    assert updated is not None
    assert updated["status"] == "accepted"
    assert updated["response"] == {"memory_id": "m-1"}
    assert updated["resolved_at"] is not None

    again = _run(store.resolve_handle(handle["id"], new_status="rejected"))
    assert again is None


def test_bulk_get_filters_by_user(_isolated_db, _seed_user):
    store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create_other():
        async with AsyncSessionLocal() as db:
            u2 = models_mod.User(
                email=f"other-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Other",
            )
            db.add(u2)
            await db.commit()
            await db.refresh(u2)
            return u2.id

    other_user_id = _run(_create_other())

    h1 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t1", payload={},
    ))
    h2 = _run(store.create_handle(
        user_id=other_user_id, conversation_id="c2",
        kind="slack_reply", tool_id="t2", payload={},
    ))
    rows = _run(store.bulk_get_handles_by_ids(
        _seed_user, [h1["id"], h2["id"]],
    ))
    ids = {r["id"] for r in rows}
    assert h1["id"] in ids
    assert h2["id"] not in ids


def test_cancel_pending_for_conversation(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h1 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t1", payload={},
    ))
    h2 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t2", payload={},
    ))
    h3 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c-other",
        kind="slack_reply", tool_id="t3", payload={},
    ))
    cancelled = _run(store.cancel_pending_for_conversation(_seed_user, "c1"))
    cancelled_ids = {r["id"] for r in cancelled}
    assert cancelled_ids == {h1["id"], h2["id"]}
    untouched = _run(store.get_handle(h3["id"]))
    assert untouched["status"] == "pending"


def test_mark_timed_out_skips_resolved(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h1 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t1", payload={},
    ))
    h2 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t2", payload={},
    ))
    _run(store.resolve_handle(h2["id"], new_status="accepted"))
    rows = _run(store.mark_timed_out([h1["id"], h2["id"]], _seed_user))
    timed_out_ids = {r["id"] for r in rows}
    assert h1["id"] in timed_out_ids
    assert h2["id"] not in timed_out_ids


def test_get_handle_by_tool_id_scopes_user(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="slack_reply_xy", payload={},
    ))
    found = _run(store.get_handle_by_tool_id(_seed_user, "c1", "slack_reply_xy"))
    assert found is not None and found["id"] == h["id"]
    not_found = _run(store.get_handle_by_tool_id(
        _seed_user, "c1", "slack_reply_other",
    ))
    assert not_found is None


def _past(seconds=60):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _future(seconds=3600):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def test_mark_timed_out_expired_by_flips_only_overdue_rows(_isolated_db, _seed_user):
    store, _models = _isolated_db
    overdue = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1", kind="user_subagent",
        tool_id="", payload={}, expires_at=_past(),
    ))
    not_due = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1", kind="user_subagent",
        tool_id="", payload={}, expires_at=_future(),
    ))
    no_deadline = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1", kind="user_subagent",
        tool_id="", payload={},
    ))
    rows = _run(store.mark_timed_out(
        [overdue["id"], not_due["id"], no_deadline["id"]], _seed_user,
        expired_by=datetime.now(timezone.utc),
    ))
    assert [r["id"] for r in rows] == [overdue["id"]]
    assert rows[0]["status"] == "timed_out"
    assert rows[0]["resolved_at"] is not None
    assert _run(store.get_handle(not_due["id"]))["status"] == "pending"
    assert _run(store.get_handle(no_deadline["id"]))["status"] == "pending"


def test_mark_timed_out_scopes_user(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t1", payload={},
    ))
    assert _run(store.mark_timed_out([h["id"]], _seed_user + 1000)) == []
    assert _run(store.get_handle(h["id"]))["status"] == "pending"


def test_resolve_handle_loses_to_earlier_timeout(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="action_request", tool_id="t1", payload={},
    ))
    assert len(_run(store.mark_timed_out([h["id"]], _seed_user))) == 1
    assert _run(store.resolve_handle(
        h["id"], new_status="accepted", response={"late": True},
    )) is None
    row = _run(store.get_handle(h["id"]))
    assert row["status"] == "timed_out"
    assert row["response"] is None


def test_resolve_and_timeout_race_has_one_winner(_isolated_db, _seed_user):
    """Run resolve and time-out concurrently many times: every round exactly
    one writer wins, and the row ends in the winner's state."""
    store, _models = _isolated_db

    async def _round():
        h = await store.create_handle(
            user_id=_seed_user, conversation_id="c1",
            kind="action_request", tool_id="t1", payload={},
        )
        resolved, timed_out = await asyncio.gather(
            store.resolve_handle(
                h["id"], new_status="accepted", response={"ok": True},
                user_id=_seed_user,
            ),
            store.mark_timed_out([h["id"]], _seed_user),
        )
        final = await store.get_handle(h["id"])
        return resolved, timed_out, final

    for _ in range(10):
        resolved, timed_out, final = _run(_round())
        assert (resolved is not None) != bool(timed_out)
        if resolved is not None:
            assert final["status"] == "accepted"
            assert final["response"] == {"ok": True}
        else:
            assert final["status"] == "timed_out"


def test_cancel_pending_skips_rows_resolved_meanwhile(_isolated_db, _seed_user):
    store, _models = _isolated_db
    h1 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t1", payload={},
    ))
    h2 = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t2", payload={},
    ))
    _run(store.mark_timed_out([h2["id"]], _seed_user))
    cancelled = _run(store.cancel_pending_for_conversation(
        _seed_user, "c1", feedback="stop",
    ))
    assert [r["id"] for r in cancelled] == [h1["id"]]
    assert cancelled[0]["status"] == "cancelled"
    assert cancelled[0]["response"] == {"feedback": "stop"}
    assert _run(store.get_handle(h2["id"]))["status"] == "timed_out"


def test_set_expires_at_updates_pending_rows_only(_isolated_db, _seed_user):
    store, _models = _isolated_db
    pending = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={}, expires_at=_future(60),
    ))
    resolved = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={},
    ))
    _run(store.resolve_handle(resolved["id"], new_status="accepted"))

    deadline = _future(7200)
    count = _run(store.set_expires_at(
        [pending["id"], resolved["id"]], _seed_user, deadline,
    ))
    assert count == 1
    stored = datetime.fromisoformat(_run(store.get_handle(pending["id"]))["expires_at"])
    assert stored == deadline.replace(tzinfo=None)
    assert _run(store.get_handle(resolved["id"]))["expires_at"] is None


def test_set_expires_at_never_moves_a_passed_deadline(_isolated_db, _seed_user):
    store, _models = _isolated_db
    overdue = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={}, expires_at=_past(),
    ))
    before = _run(store.get_handle(overdue["id"]))["expires_at"]
    assert _run(store.set_expires_at([overdue["id"]], _seed_user, _future())) == 0
    assert _run(store.get_handle(overdue["id"]))["expires_at"] == before


def test_list_expired_pending(_isolated_db, _seed_user):
    store, _models = _isolated_db
    older = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={}, expires_at=_past(600),
    ))
    newer = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c2",
        kind="user_subagent", tool_id="", payload={}, expires_at=_past(60),
    ))
    _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={}, expires_at=_future(),
    ))
    _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="slack_reply", tool_id="t", payload={},
    ))
    done = _run(store.create_handle(
        user_id=_seed_user, conversation_id="c1",
        kind="user_subagent", tool_id="", payload={}, expires_at=_past(),
    ))
    _run(store.resolve_handle(done["id"], new_status="rejected"))

    rows = _run(store.list_expired_pending(datetime.now(timezone.utc)))
    assert [r["id"] for r in rows] == [older["id"], newer["id"]]
