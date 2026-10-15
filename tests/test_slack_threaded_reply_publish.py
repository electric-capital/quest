"""Tests that the Slack-driven threaded-reply path publishes
``message_appended`` to subscribers of the matching Quest conversation.

The web UI subscribes to a Slack-driven conversation by its Quest
``conversation_id`` (the same UUID used for web conversations); the
Socket Mode worker resolves a threaded reply to that conversation_id via
the ``slack_conversations`` table and then calls
``ChatStorage.append_message`` for the user's message. Without a publish
hook on that path, an open web tab would never see the user's slack
reply land in chat_history -- which is the regression this test guards
against.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

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

    import chat.slack_conversation_store as slack_store_mod

    return storage_mod, slack_store_mod, isolated_db.models


@pytest.fixture()
def _seed_slack_conversation(_isolated_storage):
    storage_mod, slack_store_mod, models_mod = _isolated_storage
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"slack-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Slack Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            user_id = u.id

        channel = "D" + uuid.uuid4().hex[:8]
        thread_ts = "1234567890.000100"
        conversation_id = await storage_mod.ChatStorage.create_slack_conversation(
            user_id=user_id,
            slack_channel_id=channel,
            slack_thread_ts=thread_ts,
            slack_user_id="USLACKUSER",
        )
        return user_id, conversation_id, channel, thread_ts

    return _run(_create())


def test_threaded_reply_with_pending_handle_publishes_user_message(
    _isolated_storage, _seed_slack_conversation,
):
    """When ``_handle_threaded_reply`` finds a pending slack_reply handle
    (via ``enqueue_user_reply`` returning True), the user message must be
    appended via ``ChatStorage.append_message`` AND that append must
    publish ``message_appended`` to the conversation channel.
    """
    storage_mod, _slack_store_mod, _ = _isolated_storage
    user_id, conversation_id, channel, thread_ts = _seed_slack_conversation

    from chat.realtime.bus import bus as _live_bus, SubscriberQueue

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    # Patch enqueue_user_reply to simulate "pending handle exists" path.
    # Patch get_slack_conversation to return our seeded row.
    # Patch get_user_by_slack_user_id (used elsewhere) -- not needed for
    # the threaded-reply path which gets user from db lookup via
    # ownership check.
    from chat import slack_socket_mode

    async def _fake_enqueue(channel, thread_ts, text):
        return True  # pending handle exists -> branch we care about

    with patch.object(
        slack_socket_mode.slack_driven_runtime,
        "enqueue_user_reply",
        side_effect=_fake_enqueue,
    ):
        _run(slack_socket_mode._handle_threaded_reply(
            user={"id": user_id, "email": "slack-tester@example.com"},
            channel=channel,
            ts="1234567890.000200",
            thread_ts=thread_ts,
            text="hello from slack",
            slack_user_id="USLACKUSER",
        ))

    # The user-message append should publish message_appended.
    seqs_seen: list[int] = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seqs_seen.append(ev["seq"])
    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)

    assert seqs_seen, (
        "_handle_threaded_reply with a pending slack_reply handle must "
        "publish message_appended for the user's text so onlooker web tabs "
        "see the new bubble immediately."
    )


def test_threaded_reply_no_pending_handle_publishes_user_message(
    _isolated_storage, _seed_slack_conversation,
):
    """When ``_handle_threaded_reply`` finds NO pending slack_reply handle
    AND the run is not active (the resume path), the user message is
    persisted via the same ``ChatStorage.append_message`` and must
    publish ``message_appended`` to the conversation channel.
    """
    storage_mod, _slack_store_mod, _ = _isolated_storage
    user_id, conversation_id, channel, thread_ts = _seed_slack_conversation

    from chat.realtime.bus import bus as _live_bus, SubscriberQueue

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    from chat import slack_socket_mode

    async def _fake_enqueue(channel, thread_ts, text):
        return False  # no pending handle -> fall through

    with patch.object(
        slack_socket_mode.slack_driven_runtime,
        "enqueue_user_reply",
        side_effect=_fake_enqueue,
    ), patch.object(
        slack_socket_mode.slack_driven_runtime,
        "is_run_active",
        return_value=False,
    ), patch.object(
        slack_socket_mode.slack_driven_runtime,
        "set_typing",
        new=AsyncMock(),
    ), patch.object(
        slack_socket_mode,
        "_start_model_run",
        return_value=None,
    ):
        _run(slack_socket_mode._handle_threaded_reply(
            user={"id": user_id, "email": "slack-tester@example.com"},
            channel=channel,
            ts="1234567890.000300",
            thread_ts=thread_ts,
            text="continue this conversation",
            slack_user_id="USLACKUSER",
        ))

    seqs_seen: list[int] = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seqs_seen.append(ev["seq"])
    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)

    assert seqs_seen, (
        "_handle_threaded_reply on the no-pending-handle path must publish "
        "message_appended for the user's text so onlooker web tabs see the "
        "new bubble immediately."
    )


def test_resume_flush_callback_publishes(_isolated_storage, _seed_slack_conversation):
    """The headless resume path uses ``make_flush_callback`` to persist new
    structured messages. After resume produces a tool_use / tool_result /
    text turn, those messages must publish ``message_appended`` so the web
    UI watching the Slack-driven conversation sees the model's reply.

    This is a focused unit test on the publish hook, not a full
    run_conversation_turn integration test: we drive the closure directly with
    a small messages_out list, mirroring what the resume task would
    accumulate before / between flush boundaries.
    """
    storage_mod, _slack_store_mod, _ = _isolated_storage
    _user_id, conversation_id, _channel, _thread_ts = _seed_slack_conversation

    from chat.realtime.bus import bus as _live_bus, SubscriberQueue
    from chat._flush_helper import make_flush_callback

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    messages_out: list[dict] = []
    flush_fn, _lock = make_flush_callback(
        conversation_id, messages_out, log_prefix="[wait-resume]",
    )

    # Simulate the resume's run_conversation_turn appending tool_use + tool_result
    # for send_slack_reply_and_get_response close-out, then a text reply.
    messages_out.append({
        "type": "tool_use",
        "role": "assistant",
        "tool_name": "send_slack_reply_and_get_response",
        "tool_input": {"text": "Got it, working on that."},
        "tool_id": "tool_resume_1",
        "intent_message": "",
    })
    _run(flush_fn())
    messages_out.append({
        "type": "tool_result",
        "tool_id": "tool_resume_1",
        "tool_output": '{"user_reply": "thanks", "posted_ts": "1.23"}',
    })
    _run(flush_fn())

    seqs_seen: list[int] = []
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            seqs_seen.append(ev["seq"])
    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)

    assert seqs_seen == [1, 2], (
        "Resume's flush callback must publish message_appended for each "
        "newly persisted structured message."
    )


def test_full_slack_threaded_reply_to_subscribed_tab(
    _isolated_storage, _seed_slack_conversation,
):
    """End-to-end: a web tab subscribes to a Slack-driven conversation
    via the same bus key the slack worker publishes to. When
    ``_handle_threaded_reply`` runs (with a pending slack_reply handle),
    the tab receives a ``message_appended`` event whose ``conversation_id``
    matches the subscription -- proving the namespace agrees.

    This is the regression scenario from the bug report: open a
    Slack-driven conversation in the web UI, send a Slack message, the
    web UI must receive the update.
    """
    storage_mod, _slack_store_mod, _ = _isolated_storage
    user_id, conversation_id, channel, thread_ts = _seed_slack_conversation

    # Subscribe with the EXACT conversation_id the Slack worker resolves
    # via the slack_conversations table (which is the Quest UUID).
    from chat.realtime.bus import bus as _live_bus, SubscriberQueue

    sub_queue = SubscriberQueue()
    _live_bus.subscribe_conversation(conversation_id, sub_queue)

    # Drive the Slack worker exactly like a real DM would.
    from chat import slack_socket_mode

    async def _fake_enqueue(channel, thread_ts, text):
        return True

    with patch.object(
        slack_socket_mode.slack_driven_runtime,
        "enqueue_user_reply",
        side_effect=_fake_enqueue,
    ):
        _run(slack_socket_mode._handle_threaded_reply(
            user={"id": user_id, "email": "slack-tester@example.com"},
            channel=channel,
            ts="1234567890.000400",
            thread_ts=thread_ts,
            text="this is the user's slack reply",
            slack_user_id="USLACKUSER",
        ))

    # The subscribed queue receives a message_appended with the same
    # conversation_id we subscribed to. If the slack worker published
    # under a DIFFERENT key (e.g. the slack_conversations.id row id, or
    # the slack_thread_ts), this would fail.
    found_event = None
    while not sub_queue.empty():
        ev = sub_queue.get_nowait()
        if ev.get("type") == "message_appended":
            found_event = ev
            break
    _live_bus.unsubscribe_conversation(conversation_id, sub_queue)

    assert found_event is not None
    assert found_event["conversation_id"] == conversation_id, (
        "Slack worker must publish message_appended with the same "
        f"conversation_id ({conversation_id}) that the web tab "
        f"subscribes to. Saw conversation_id={found_event.get('conversation_id')!r}."
    )
