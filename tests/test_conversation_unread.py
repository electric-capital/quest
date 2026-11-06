"""Tests for the unread state of conversations.

A conversation is unread while ``unread_seq > read_seq``:

1. Store: ``mark_conversation_unread`` advances ``unread_seq`` to the row's
   high-water message seq (web-origin conversations only), and
   ``mark_conversation_read`` advances ``read_seq`` monotonically, clamped
   to that high-water seq and owner-checked.
2. ``chat.unread.mark_unread_after_run`` publishes
   ``conversation_unread_changed`` to the owner and never raises.
3. ``POST /conversations/{id}/read`` publishes only when ``read_seq`` moved
   and changes nothing under admin impersonation.
4. Run paths: the WS send task and the headless resume mark the
   conversation unread before their ``send_message_finished`` publish, and
   not at all when the run was stopped.
5. The ``subscribed`` ack carries both seqs.

Store tests run against the shared ``isolated_db`` fixture with asyncio.run.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from chat import unread as unread_mod


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(isolated_db):
    """The conversation store on a fresh database holding users 1, 7, 8."""
    import db.conversation_store as store_mod

    async def _add_users():
        async with store_mod.AsyncSessionLocal() as db:
            for user_id in (1, 7, 8):
                db.add(isolated_db.models.User(
                    id=user_id, email=f"u{user_id}@example.com",
                    api_key=f"key-{user_id}",
                ))
            await db.commit()

    _run(_add_users())
    return store_mod


TS = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_conversation(store, user_id=1, *, origin=None, last_seq=0):
    conv_id = str(uuid.uuid4())
    _run(store.create_conversation(user_id, conv_id, TS, origin=origin))
    if last_seq:
        _run(store.record_messages_appended(conv_id, last_message_seq=last_seq))
    return conv_id


class _RecordingBus:
    def __init__(self):
        self.user_events: list[tuple[int, dict]] = []

    def publish_to_user(self, user_id, event):
        self.user_events.append((user_id, event))


@pytest.fixture()
def bus(monkeypatch):
    recording = _RecordingBus()
    monkeypatch.setattr(unread_mod, "bus", recording)
    return recording


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_new_conversation_is_read(store):
    conv_id = _make_conversation(store)
    meta = _run(store.get_conversation_meta(1, conv_id))
    assert (meta["unread_seq"], meta["read_seq"]) == (0, 0)


def test_mark_unread_advances_to_high_water_seq(store):
    conv_id = _make_conversation(store, last_seq=4)

    meta = _run(store.mark_conversation_unread(conv_id))

    assert (meta["unread_seq"], meta["read_seq"]) == (4, 0)
    stored = _run(store.get_conversation_meta(1, conv_id))
    assert stored["unread_seq"] == 4


def test_mark_unread_is_a_noop_without_new_messages(store):
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.mark_conversation_unread(conv_id))

    assert _run(store.mark_conversation_unread(conv_id)) is None
    assert _run(store.mark_conversation_unread("missing")) is None


@pytest.mark.parametrize("origin", ["slack", "inference_api", "user_subagent"])
def test_mark_unread_skips_conversations_read_elsewhere(store, origin):
    conv_id = _make_conversation(store, origin=origin, last_seq=4)

    assert _run(store.mark_conversation_unread(conv_id)) is None
    assert _run(store.get_conversation_meta(1, conv_id))["unread_seq"] == 0


def test_mark_unread_applies_to_explicit_web_origin(store):
    conv_id = _make_conversation(store, origin="web", last_seq=2)
    assert _run(store.mark_conversation_unread(conv_id))["unread_seq"] == 2


def test_mark_read_clears_unread(store):
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.mark_conversation_unread(conv_id))

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 4))

    assert changed is True
    assert (meta["unread_seq"], meta["read_seq"]) == (4, 4)


def test_mark_read_never_moves_backwards(store):
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.mark_conversation_read(1, conv_id, 4))

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 2))

    assert changed is False
    assert meta["read_seq"] == 4


def test_mark_read_is_clamped_to_the_high_water_seq(store):
    conv_id = _make_conversation(store, last_seq=4)

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 999))

    assert changed is True
    assert meta["read_seq"] == 4


def test_stale_read_ack_leaves_newer_output_unread(store):
    """An acknowledgement that predates the latest run must not hide it."""
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.record_messages_appended(conv_id, last_message_seq=9))
    _run(store.mark_conversation_unread(conv_id))

    meta, _ = _run(store.mark_conversation_read(1, conv_id, 4))

    assert meta["unread_seq"] > meta["read_seq"]


def test_mark_read_checks_ownership(store):
    conv_id = _make_conversation(store, user_id=1, last_seq=4)

    assert _run(store.mark_conversation_read(2, conv_id, 4)) is None
    assert _run(store.mark_conversation_read(1, "missing", 4)) is None
    assert _run(store.get_conversation_meta(1, conv_id))["read_seq"] == 0


def test_list_rows_carry_the_unread_seqs(store):
    from chat.storage import ChatStorage

    conv_id = _make_conversation(store, last_seq=4)
    _run(store.mark_conversation_unread(conv_id))

    rows = _run(ChatStorage.list_conversations(1))

    row = next(r for r in rows if r["id"] == conv_id)
    assert (row["unread_seq"], row["read_seq"]) == (4, 0)


# ---------------------------------------------------------------------------
# Run-end hook
# ---------------------------------------------------------------------------


def test_mark_unread_after_run_publishes_to_the_owner(store, bus):
    conv_id = _make_conversation(store, user_id=7, last_seq=3)

    _run(unread_mod.mark_unread_after_run(conv_id))

    assert bus.user_events == [(7, {
        "type": "conversation_unread_changed",
        "conversation_id": conv_id,
        "unread_seq": 3,
        "read_seq": 0,
    })]


def test_mark_unread_after_run_is_silent_when_nothing_changed(store, bus):
    conv_id = _make_conversation(store, origin="slack", last_seq=3)

    _run(unread_mod.mark_unread_after_run(conv_id))

    assert bus.user_events == []


def test_mark_unread_after_run_never_raises(monkeypatch, bus):
    async def boom(_conversation_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        unread_mod.conversation_store, "mark_conversation_unread", boom,
    )

    _run(unread_mod.mark_unread_after_run("conv"))

    assert bus.user_events == []


# ---------------------------------------------------------------------------
# POST /conversations/{id}/read
# ---------------------------------------------------------------------------


def test_read_endpoint_marks_read_and_publishes(store, bus):
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)
    _run(store.mark_conversation_unread(conv_id))

    body = _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5), user={"id": 7},
    ))

    assert body == {"unread_seq": 5, "read_seq": 5}
    assert bus.user_events == [(7, {
        "type": "conversation_unread_changed",
        "conversation_id": conv_id,
        "unread_seq": 5,
        "read_seq": 5,
    })]

    # A repeat changes nothing and publishes nothing.
    bus.user_events.clear()
    _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5), user={"id": 7},
    ))
    assert bus.user_events == []


def test_read_endpoint_is_a_noop_under_impersonation(store, bus):
    """An admin viewing as the owner must not clear the owner's unread dot."""
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)
    _run(store.mark_conversation_unread(conv_id))

    body = _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5),
        user={"id": 7, "_impersonator_uid": 1},
    ))

    assert body == {"unread_seq": 5, "read_seq": 0}
    assert bus.user_events == []
    assert _run(store.get_conversation_meta(7, conv_id))["read_seq"] == 0


def test_read_endpoint_404s_for_another_users_conversation(store, bus):
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)

    with pytest.raises(HTTPException) as excinfo:
        _run(mark_conversation_read_endpoint(
            conv_id, MarkReadRequest(seq=5), user={"id": 8},
        ))

    assert excinfo.value.status_code == 404
    assert bus.user_events == []


# ---------------------------------------------------------------------------
# Run paths
# ---------------------------------------------------------------------------


class _ConversationBus:
    def __init__(self, log):
        self._log = log

    def publish_to_conversation(self, _conversation_id, event):
        self._log.append(event["type"])


def _patch_send_run(monkeypatch, run_impl):
    """Stub everything ``_run_send_message`` touches; returns the ordered
    log of unread marks and conversation-channel publishes."""
    from chat.realtime import socket as socket_mod

    log: list[str] = []

    async def fake_mark(conversation_id):
        log.append(f"unread:{conversation_id}")

    def fake_make_flush(_conversation_id, _messages_out, *, log_prefix):
        async def flush():
            return None

        return flush, asyncio.Lock()

    async def fake_cancel_handles(_user_id, _conversation_id):
        return None

    monkeypatch.setattr(unread_mod, "mark_unread_after_run", fake_mark)
    monkeypatch.setattr(socket_mod, "make_flush_callback", fake_make_flush)
    monkeypatch.setattr(socket_mod, "run_conversation_turn", run_impl)
    monkeypatch.setattr(socket_mod, "bus", _ConversationBus(log))
    monkeypatch.setattr(
        socket_mod, "_save_interrupted_sdk_history", lambda *a, **k: None,
    )
    monkeypatch.setattr(socket_mod, "remove_chat_session", lambda *a, **k: None)
    monkeypatch.setattr(
        socket_mod, "cancel_pending_wait_handles_for_conversation",
        fake_cancel_handles,
    )
    return socket_mod, log


def _send_kwargs(conversation_id):
    return dict(
        app=None, user={"id": 1, "email": "u@example.com"},
        conversation_id=conversation_id, user_message="hi",
        timezone_str="UTC", selected_model=None, project_id=None,
        routine_id=None, skill_ids=None,
    )


def test_send_run_marks_unread_before_finishing(monkeypatch):
    async def fake_run(**_kwargs):
        return None

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["unread:c1", "send_message_finished"]


def test_failed_send_run_still_marks_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise RuntimeError("boom")

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["unread:c1", "send_message_finished"]


def test_stopped_send_run_does_not_mark_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise asyncio.CancelledError()

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["send_message_finished"]


def _patch_resume(monkeypatch, run_impl):
    import chat.gemini_api as gemini_mod
    import db.conversation_store as conv_store_mod
    from chat.realtime.bus import bus as live_bus
    from chat.wait_handles import resume as resume_mod

    log: list[str] = []

    async def no_handles(_user_id, _conversation_id):
        return []

    async def fake_meta(_user_id, _conversation_id):
        return {"origin": "web"}

    async def fake_flags(_conversation_id):
        return []

    async def fake_mark(conversation_id):
        log.append(f"unread:{conversation_id}")

    def fake_make_flush(_conversation_id, _messages_out, *, log_prefix):
        async def flush():
            return None

        return flush, asyncio.Lock()

    monkeypatch.setattr(resume_mod, "_unresolved_blocking_handle_ids", no_handles)
    monkeypatch.setattr(resume_mod, "_stopped_handle_ids", no_handles)
    monkeypatch.setattr(resume_mod, "_streaming_resumes", set())
    monkeypatch.setattr(resume_mod, "make_flush_callback", fake_make_flush)
    monkeypatch.setattr(conv_store_mod, "get_conversation_meta", fake_meta)
    monkeypatch.setattr(conv_store_mod, "get_conversation_flags", fake_flags)
    monkeypatch.setattr(gemini_mod, "run_conversation_turn", run_impl)
    monkeypatch.setattr(unread_mod, "mark_unread_after_run", fake_mark)
    monkeypatch.setattr(
        live_bus, "publish_to_conversation",
        lambda _cid, event: log.append(event["type"]),
    )
    return resume_mod, log


def test_resume_marks_unread_before_finishing(monkeypatch):
    async def fake_run(**_kwargs):
        return None

    resume_mod, log = _patch_resume(monkeypatch, fake_run)

    _run(resume_mod._run_resume(None, {"id": 1}, "c2"))

    assert log == ["resume_started", "unread:c2", "send_message_finished"]


def test_stopped_resume_does_not_mark_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise asyncio.CancelledError()

    resume_mod, log = _patch_resume(monkeypatch, fake_run)

    with pytest.raises(asyncio.CancelledError):
        _run(resume_mod._run_resume(None, {"id": 1}, "c2"))

    assert log == ["resume_started", "send_message_finished"]


# ---------------------------------------------------------------------------
# Subscribe ack
# ---------------------------------------------------------------------------


def test_subscribed_ack_carries_the_unread_seqs(monkeypatch, store):
    from chat.realtime import socket as socket_mod
    from chat.realtime.bus import Bus
    from chat.realtime.replay_buffer import ReplayBuffer

    class _FakeWebSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

    conv_id = _make_conversation(store, user_id=7, last_seq=6)
    _run(store.mark_conversation_unread(conv_id))
    _run(store.mark_conversation_read(7, conv_id, 2))

    ws = _FakeWebSocket()
    conn = socket_mod._Connection(ws, {"id": 7, "email": "u@example.com"})
    monkeypatch.setattr(socket_mod, "bus", Bus())
    monkeypatch.setattr(socket_mod, "replay_buffer", ReplayBuffer())

    _run(socket_mod._handle_subscribe(
        conn, {"op": "subscribe", "conversation_id": conv_id, "last_seq": 6},
    ))

    ack = ws.sent[-1]
    assert ack["type"] == "subscribed"
    assert ack["mode"] == "up_to_date"
    assert (ack["unread_seq"], ack["read_seq"]) == (6, 2)
