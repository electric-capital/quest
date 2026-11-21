"""Tests for the per-connection subscription TTL on the persistent WS.

Subscriptions on the multiplexed WebSocket are time-bounded by
``SUBSCRIPTION_TTL_SECONDS``. Each ``subscribe`` op (initial or refresh)
extends the deadline; the sweep helper evicts entries whose deadline has
elapsed and emits ``subscription_expired`` per evicted conversation.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from chat.realtime import socket as socket_mod
from chat.realtime.bus import Bus, SubscriberQueue


class _FakeWebSocket:
    """Minimal stand-in for the FastAPI ``WebSocket`` used by socket.py."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: bool = False
        self.cookies: dict[str, str] = {}

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def _make_conn(monkeypatch: pytest.MonkeyPatch) -> tuple[
    socket_mod._Connection, Bus, _FakeWebSocket,
]:
    """Build a fresh ``_Connection`` with a private ``Bus`` swapped in."""
    ws = _FakeWebSocket()
    user = {"id": 7, "email": "test@example.com"}
    conn = socket_mod._Connection(ws, user)
    test_bus = Bus()
    monkeypatch.setattr(socket_mod, "bus", test_bus, raising=True)
    return conn, test_bus, ws


def _patch_meta(
    monkeypatch: pytest.MonkeyPatch, *, last_seq: int = 0,
) -> None:
    """Stub ``db.conversation_store.get_conversation_meta`` so subscribe
    bypasses the SQL ownership check.
    """

    async def _meta(_user_id: int, _conv_id: str) -> dict:
        return {"last_message_seq": last_seq}

    fake_module = types.SimpleNamespace(get_conversation_meta=_meta)
    monkeypatch.setitem(
        sys.modules, "db.conversation_store", fake_module,
    )


def _run(coro):
    return asyncio.run(coro)


def test_subscribe_extends_deadline(monkeypatch):
    """Two ``subscribe`` calls in a row do not double-add the queue but
    bump the deadline forward.
    """
    _patch_meta(monkeypatch)
    conn, test_bus, _ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))
    deadline_first = conn.conversation_subs["conv-1"]
    assert test_bus.conversation_subscriber_count("conv-1") == 1

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1060.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))
    deadline_second = conn.conversation_subs["conv-1"]

    assert deadline_second > deadline_first
    assert test_bus.conversation_subscriber_count("conv-1") == 1


def test_sweep_evicts_expired_subscription(monkeypatch):
    """A subscription past its deadline is unsubscribed from the bus and
    a ``subscription_expired`` envelope is sent to the client.
    """
    _patch_meta(monkeypatch)
    conn, test_bus, ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))
    ws.sent.clear()  # drop the initial ``subscribed`` envelope

    # Jump well past the 5 min TTL.
    expired = _run(socket_mod._sweep_expired_subscriptions(
        conn, now=1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS + 1.0,
    ))

    assert expired == ["conv-1"]
    assert "conv-1" not in conn.conversation_subs
    assert test_bus.conversation_subscriber_count("conv-1") == 0
    assert any(
        msg.get("type") == "subscription_expired"
        and msg.get("conversation_id") == "conv-1"
        for msg in ws.sent
    )


def test_sweep_keeps_active_subscription(monkeypatch):
    """A subscription whose deadline has not yet passed is left alone."""
    _patch_meta(monkeypatch)
    conn, test_bus, ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))
    ws.sent.clear()

    # Halfway through the TTL.
    expired = _run(socket_mod._sweep_expired_subscriptions(
        conn, now=1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS / 2,
    ))

    assert expired == []
    assert "conv-1" in conn.conversation_subs
    assert test_bus.conversation_subscriber_count("conv-1") == 1
    assert not any(
        msg.get("type") == "subscription_expired" for msg in ws.sent
    )


def test_refresh_then_sweep_keeps_subscription(monkeypatch):
    """Refreshing after the original deadline-but-before-sweep keeps the
    subscription alive across the next sweep tick.
    """
    _patch_meta(monkeypatch)
    conn, test_bus, _ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))

    # 60 s later, still well inside the TTL window: refresh.
    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1060.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))

    # First sweep: the original 1000+TTL deadline is past, but the refresh
    # bumped it to 1060+TTL, which has not yet elapsed.
    expired = _run(socket_mod._sweep_expired_subscriptions(
        conn, now=1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS + 1.0,
    ))

    assert expired == []
    assert test_bus.conversation_subscriber_count("conv-1") == 1


def test_resubscribe_after_expiry_re_adds_to_bus(monkeypatch):
    """After expiry, a fresh subscribe goes through the first-time branch
    and re-registers the queue with the bus.
    """
    _patch_meta(monkeypatch)
    conn, test_bus, _ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))

    _run(socket_mod._sweep_expired_subscriptions(
        conn, now=1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS + 1.0,
    ))
    assert test_bus.conversation_subscriber_count("conv-1") == 0

    # Resubscribe: should treat as first-time and re-add to the bus.
    monkeypatch.setattr(
        socket_mod.time, "monotonic",
        lambda: 1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS + 5.0,
    )
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))

    assert "conv-1" in conn.conversation_subs
    assert test_bus.conversation_subscriber_count("conv-1") == 1


def test_unsubscribe_op_still_works_immediately(monkeypatch):
    """The explicit ``unsubscribe`` wire op continues to remove the
    subscription synchronously (used for explicit teardown flows).
    """
    _patch_meta(monkeypatch)
    conn, test_bus, ws = _make_conn(monkeypatch)

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))
    ws.sent.clear()

    _run(socket_mod._handle_unsubscribe(conn, {"conversation_id": "conv-1"}))

    assert "conv-1" not in conn.conversation_subs
    assert test_bus.conversation_subscriber_count("conv-1") == 0
    assert any(msg.get("type") == "unsubscribed" for msg in ws.sent)


def test_replay_buffer_eviction_respects_ttl_subscriber(monkeypatch):
    """While a TTL'd subscription holds the queue in the bus, the replay
    buffer's idle-eviction sweep refuses to drop the conversation's buffer.
    Once the sweep evicts the subscription, the buffer becomes evictable.
    """
    import time as _time

    from chat.realtime.replay_buffer import ReplayBuffer

    _patch_meta(monkeypatch)
    conn, test_bus, _ws = _make_conn(monkeypatch)
    buf = ReplayBuffer(maxlen=10)
    buf.append("conv-1", 1, {"seq": 1})
    buf._last_append_at["conv-1"] = 0.0  # mark as old

    monkeypatch.setattr(socket_mod.time, "monotonic", lambda: 1000.0)
    _run(socket_mod._handle_subscribe(conn, {
        "conversation_id": "conv-1",
        "last_seq": 0,
    }))

    rb_mod = sys.modules["chat.realtime.replay_buffer"]
    original_bus = rb_mod.bus
    rb_mod.bus = test_bus
    try:
        # Subscriber present -> buffer must NOT be evicted.
        evicted = buf.evict_idle(idle_seconds=1.0, now=_time.monotonic())
        assert evicted == 0
        assert buf.latest_seq("conv-1") == 1

        # After TTL expires the sweep removes the bus subscription, and
        # the replay buffer's next sweep can drop the buffer.
        _run(socket_mod._sweep_expired_subscriptions(
            conn, now=1000.0 + socket_mod.SUBSCRIPTION_TTL_SECONDS + 1.0,
        ))
        evicted = buf.evict_idle(idle_seconds=1.0, now=_time.monotonic())
        assert evicted == 1
        assert buf.latest_seq("conv-1") is None
    finally:
        rb_mod.bus = original_bus
