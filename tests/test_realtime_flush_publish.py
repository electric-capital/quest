"""Tests that the shared flush callback publishes ``message_appended`` to
the in-process bus and pushes new (seq, message) pairs into the replay
buffer after a successful disk write.

This is the single fan-out point that the persistent WS subscribe handler
relies on (durable events) and that the per-conversation channel forwards
to live subscribers.
"""

import asyncio
import uuid

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_storage(isolated_db, tmp_path, monkeypatch):
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()

    from config import paths
    import chat.storage as storage_mod

    # chat.storage binds CHATS_DIR at import time, so patch its copy too.
    monkeypatch.setattr(paths, "CHATS_DIR", chats_dir, raising=True)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats_dir, raising=True)

    import chat._flush_helper as flush_mod

    return storage_mod, flush_mod, isolated_db.models


@pytest.fixture()
def _seed_conversation(_isolated_storage):
    storage_mod, _flush_mod, models_mod = _isolated_storage
    from db.engine import AsyncSessionLocal

    async def _create() -> str:
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"flush-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Flush Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            user_id = u.id
        conversation_id, _created_at = await storage_mod.ChatStorage.create_conversation(user_id)
        return conversation_id

    return _run(_create())


def test_flush_publishes_message_appended(_isolated_storage, _seed_conversation):
    storage_mod, flush_mod, _ = _isolated_storage
    conversation_id = _seed_conversation

    # Subscribe a queue against the bus + reset the replay buffer.
    from chat.realtime.bus import bus as _live_bus, SubscriberQueue
    from chat.realtime.replay_buffer import replay_buffer as _live_buf

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    messages_out: list[dict] = []
    flush_fn, _lock = flush_mod.make_flush_callback(
        conversation_id, messages_out, log_prefix="[test]",
    )

    messages_out.append({"type": "tool_use", "tool_name": "x", "tool_id": "t1"})
    messages_out.append({"type": "tool_result", "tool_id": "t1", "tool_output": "ok"})

    _run(flush_fn())

    # Bus received two ``message_appended`` envelopes in order.
    seqs_seen: list[int] = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seqs_seen.append(ev["seq"])
    assert seqs_seen == [1, 2]

    # Replay buffer has the same window.
    sliced = _live_buf.slice(conversation_id, 0)
    assert sliced is not None
    assert [s for (s, _msg) in sliced] == [1, 2]

    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)


def test_append_message_publishes_message_appended(
    _isolated_storage, _seed_conversation,
):
    """``ChatStorage.append_message`` (the single-message path used at
    turn-start for the user bubble) must also publish ``message_appended``
    so onlooker tabs subscribed to the same conversation render the user
    message immediately. Without this hook, a second tab watching the same
    conversation only sees the assistant's reply (the durable flush
    callback path) and not the user's prompt.
    """
    storage_mod, _flush_mod, _ = _isolated_storage
    conversation_id = _seed_conversation

    from chat.realtime.bus import bus as _live_bus, SubscriberQueue
    from chat.realtime.replay_buffer import replay_buffer as _live_buf

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    seq, _msg = _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", "hello",
    ))
    assert seq == 1

    seqs_seen: list[int] = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seqs_seen.append(ev["seq"])
    assert seqs_seen == [1], (
        "append_message must fan out message_appended to onlooker tabs"
    )

    sliced = _live_buf.slice(conversation_id, 0)
    assert sliced is not None
    assert [s for (s, _m) in sliced] == [1]

    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)


def test_flush_idempotent_no_double_publish(_isolated_storage, _seed_conversation):
    """Calling the flush twice with no new messages must not republish."""
    storage_mod, flush_mod, _ = _isolated_storage
    conversation_id = _seed_conversation

    from chat.realtime.bus import bus as _live_bus, SubscriberQueue

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    messages_out: list[dict] = []
    flush_fn, _lock = flush_mod.make_flush_callback(
        conversation_id, messages_out, log_prefix="[test]",
    )

    messages_out.append({"type": "tool_use", "tool_name": "x", "tool_id": "t1"})
    _run(flush_fn())
    _run(flush_fn())  # idempotent: nothing new to publish

    seen = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seen.append(ev["seq"])
    assert seen == [1]

    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)
