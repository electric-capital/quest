"""Tests for chat.wait_handles.wait_timer: the per-handle in-memory timers,
the wait-site overdue check, and the durable ``expires_at`` sweep.

Uses an isolated SQLite file per test (the same pattern as
tests/test_tool_wait_handle_store.py). The resume kick, the realtime bus
and the user lookup are replaced with recorders so the tests observe the
side effects without driving a model run or touching the real data dir.
"""

import ast
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture()
def env(isolated_db, monkeypatch):
    import db.tool_wait_handle_store as store_mod

    from chat.realtime import bus as rt_bus
    from chat.wait_handles import resume as wait_resume
    from chat.wait_handles import wait_timer
    import db.user_store as user_store

    kicks: list[tuple] = []
    events: list[tuple] = []
    monkeypatch.setattr(
        wait_resume, "maybe_kick_resume",
        lambda app, user, cid: kicks.append((app, user["id"], cid)),
    )
    monkeypatch.setattr(
        rt_bus, "publish_to_user",
        lambda user_id, event: events.append((user_id, event)),
    )

    async def _fake_get_user_by_id(user_id):
        return {"id": user_id, "email": f"u{user_id}@example.com"}

    monkeypatch.setattr(user_store, "get_user_by_id", _fake_get_user_by_id)

    yield {
        "store": store_mod,
        "models": isolated_db.models,
        "wait_timer": wait_timer,
        "kicks": kicks,
        "events": events,
    }

    wait_timer._active_timers.clear()


def _make_user(env) -> int:
    from db.engine import AsyncSessionLocal

    models_mod = env["models"]

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"timer-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Timer Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _handle(env, user_id, conversation_id="c1", expires_at=None, kind="user_subagent"):
    return _run(env["store"].create_handle(
        user_id=user_id,
        conversation_id=conversation_id,
        kind=kind,
        tool_id="",
        payload={},
        expires_at=expires_at,
    ))


def _status(env, handle_id):
    return _run(env["store"].get_handle(handle_id))["status"]


APP = object()


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------


def test_sweep_times_out_only_expired_pending_rows(env):
    user_id = _make_user(env)
    past = _now() - timedelta(minutes=5)
    future = _now() + timedelta(hours=1)

    expired = _handle(env, user_id, expires_at=past)
    unexpired = _handle(env, user_id, expires_at=future)
    no_deadline = _handle(env, user_id)
    resolved = _handle(env, user_id, expires_at=past)
    _run(env["store"].resolve_handle(
        resolved["id"], new_status="accepted", response={"ok": True},
    ))

    flipped = _run(env["wait_timer"].sweep_expired_handles(APP))

    assert [row["id"] for row in flipped] == [expired["id"]]
    assert _status(env, expired["id"]) == "timed_out"
    assert _status(env, unexpired["id"]) == "pending"
    assert _status(env, no_deadline["id"]) == "pending"
    resolved_row = _run(env["store"].get_handle(resolved["id"]))
    assert resolved_row["status"] == "accepted"
    assert resolved_row["response"] == {"ok": True}


def test_sweep_wakes_each_expired_handles_conversation_once(env):
    user_a = _make_user(env)
    user_b = _make_user(env)
    past = _now() - timedelta(seconds=1)
    h1 = _handle(env, user_a, conversation_id="conv-a1", expires_at=past)
    h2 = _handle(env, user_a, conversation_id="conv-a2", expires_at=past)
    h3 = _handle(env, user_b, conversation_id="conv-b", expires_at=past)

    _run(env["wait_timer"].sweep_expired_handles(APP))

    # One resume kick per expired handle, each with the handle owner and the
    # app the sweep was started with.
    assert sorted(env["kicks"], key=lambda k: k[2]) == [
        (APP, user_a, "conv-a1"),
        (APP, user_a, "conv-a2"),
        (APP, user_b, "conv-b"),
    ]
    # One wait_handle_resolved per handle, on the owner's channel.
    published = {
        (user_id, ev["handle_id"], ev["status"], ev["conversation_id"])
        for user_id, ev in env["events"]
    }
    assert published == {
        (user_a, h1["id"], "timed_out", "conv-a1"),
        (user_a, h2["id"], "timed_out", "conv-a2"),
        (user_b, h3["id"], "timed_out", "conv-b"),
    }
    assert all(ev["type"] == "wait_handle_resolved" for _u, ev in env["events"])
    assert all(ev["kind"] == "user_subagent" for _u, ev in env["events"])


def test_sweep_kicks_a_conversation_once_for_several_handles(env):
    """Two overdue handles in one conversation wake it once: a second kick
    would re-run the resume after the first one suspends again."""
    user_id = _make_user(env)
    past = _now() - timedelta(seconds=1)
    _handle(env, user_id, conversation_id="conv", expires_at=past)
    _handle(env, user_id, conversation_id="conv", expires_at=past)

    flipped = _run(env["wait_timer"].sweep_expired_handles(APP))

    assert len(flipped) == 2
    assert env["kicks"] == [(APP, user_id, "conv")]
    assert len(env["events"]) == 2


def test_sweep_is_idempotent(env):
    user_id = _make_user(env)
    _handle(env, user_id, expires_at=_now() - timedelta(seconds=1))

    first = _run(env["wait_timer"].sweep_expired_handles(APP))
    second = _run(env["wait_timer"].sweep_expired_handles(APP))

    assert len(first) == 1
    assert second == []
    assert len(env["kicks"]) == 1
    assert len(env["events"]) == 1


def test_sweep_loses_to_a_resolve_after_its_read(env, monkeypatch):
    """The DB row decides: a handle resolved between the sweep's candidate
    read and its write stays resolved, and nothing is announced for it."""
    store = env["store"]
    user_id = _make_user(env)
    h = _handle(env, user_id, expires_at=_now() - timedelta(seconds=1))

    stale_view = _run(store.list_expired_pending(_now()))
    assert [row["id"] for row in stale_view] == [h["id"]]
    _run(store.resolve_handle(h["id"], new_status="accepted", response={"v": 1}))

    async def _stale_list(_now_arg):
        return stale_view

    monkeypatch.setattr(store, "list_expired_pending", _stale_list)

    flipped = _run(env["wait_timer"].sweep_expired_handles(APP))

    assert flipped == []
    assert _status(env, h["id"]) == "accepted"
    assert env["kicks"] == []
    assert env["events"] == []


def test_sweep_honours_a_deadline_moved_after_its_read(env, monkeypatch):
    """A wait that pushed the deadline out after the sweep's read wins."""
    store = env["store"]
    user_id = _make_user(env)
    h = _handle(env, user_id, expires_at=_now() - timedelta(seconds=1))
    stale_view = _run(store.list_expired_pending(_now()))

    # Simulate a later deadline landing in the DB after the sweep's read
    # (set_expires_at itself never moves a passed deadline).
    from db.engine import AsyncSessionLocal
    from sqlalchemy import update

    async def _push_deadline():
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(env["models"].ToolWaitHandle)
                .where(env["models"].ToolWaitHandle.id == h["id"])
                .values(expires_at=_now() + timedelta(hours=1))
            )
            await db.commit()

    _run(_push_deadline())

    async def _stale_list(_now_arg):
        return stale_view

    monkeypatch.setattr(store, "list_expired_pending", _stale_list)

    assert _run(env["wait_timer"].sweep_expired_handles(APP)) == []
    assert _status(env, h["id"]) == "pending"
    assert env["kicks"] == []


def test_sweep_without_user_still_times_out_but_does_not_kick(env, monkeypatch):
    import db.user_store as user_store

    async def _missing(_user_id):
        return None

    monkeypatch.setattr(user_store, "get_user_by_id", _missing)
    user_id = _make_user(env)
    h = _handle(env, user_id, expires_at=_now() - timedelta(seconds=1))

    flipped = _run(env["wait_timer"].sweep_expired_handles(APP))

    assert [row["id"] for row in flipped] == [h["id"]]
    assert env["kicks"] == []
    assert len(env["events"]) == 1


def test_sweep_loop_runs_at_startup_then_on_interval(env, monkeypatch):
    wait_timer = env["wait_timer"]
    calls: list = []

    async def _fake_sweep(app):
        calls.append(app)
        if len(calls) == 1:
            raise RuntimeError("one bad pass must not stop the loop")

    monkeypatch.setattr(wait_timer, "sweep_expired_handles", _fake_sweep)

    async def _drive():
        task = asyncio.create_task(
            wait_timer.expiry_sweep_loop(APP, interval_seconds=0.01)
        )
        # The first pass runs before any sleep (startup sweep).
        await asyncio.sleep(0)
        assert calls == [APP]
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(_drive())
    assert len(calls) >= 3
    assert all(app is APP for app in calls)


def test_sweep_interval_is_a_module_constant():
    from chat.wait_handles import wait_timer

    assert wait_timer.EXPIRY_SWEEP_INTERVAL_SECONDS == 60


def test_lifespan_starts_and_stops_the_sweep():
    """quest.py's lifespan starts expiry_sweep_loop(app) and cancels it on
    shutdown. Checked on the source: importing quest loads every plugin
    into process-global registries."""
    source = (Path(__file__).resolve().parents[1] / "quest.py").read_text()
    tree = ast.parse(source)
    lifespan = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    body = ast.get_source_segment(source, lifespan)
    assert "asyncio.create_task(expiry_sweep_loop(app))" in body
    assert "wait_expiry_task.cancel()" in body


# ---------------------------------------------------------------------------
# Wait-site overdue check
# ---------------------------------------------------------------------------


def test_expire_overdue_handles_skips_the_live_conversation(env):
    user_id = _make_user(env)
    user = {"id": user_id}
    past = _now() - timedelta(seconds=1)
    here = _handle(env, user_id, conversation_id="live", expires_at=past)
    elsewhere = _handle(env, user_id, conversation_id="other", expires_at=past)
    not_due = _handle(env, user_id, conversation_id="live",
                      expires_at=_now() + timedelta(hours=1))
    no_deadline = _handle(env, user_id, conversation_id="live")

    flipped = _run(env["wait_timer"].expire_overdue_handles(
        APP, user,
        [here["id"], elsewhere["id"], not_due["id"], no_deadline["id"]],
        current_conversation_id="live",
    ))

    assert {row["id"] for row in flipped} == {here["id"], elsewhere["id"]}
    assert _status(env, not_due["id"]) == "pending"
    assert _status(env, no_deadline["id"]) == "pending"
    # The live run reads the status itself; only the other one is woken.
    assert env["kicks"] == [(APP, user_id, "other")]
    assert len(env["events"]) == 2


# ---------------------------------------------------------------------------
# In-memory timers
# ---------------------------------------------------------------------------


def test_timer_times_out_pending_handle_and_kicks_resume(env):
    wait_timer = env["wait_timer"]
    user_id = _make_user(env)
    h = _handle(env, user_id, conversation_id="conv", kind="action_request")

    async def _drive():
        wait_timer.schedule_timeout_for_handles(
            user_id, "conv", [h["id"]], 0, app=APP, user={"id": user_id},
        )
        task = wait_timer._active_timers[h["id"]]
        await task

    _run(_drive())

    assert _status(env, h["id"]) == "timed_out"
    assert env["kicks"] == [(APP, user_id, "conv")]
    [(event_user, event)] = env["events"]
    assert event_user == user_id
    assert event["handle_id"] == h["id"]
    assert event["status"] == "timed_out"
    assert event["kind"] == "action_request"
    assert h["id"] not in wait_timer._active_timers


def test_timer_noops_when_handle_resolved_before_deadline(env):
    wait_timer = env["wait_timer"]
    user_id = _make_user(env)
    h = _handle(env, user_id)
    _run(env["store"].resolve_handle(h["id"], new_status="accepted"))

    async def _drive():
        wait_timer.schedule_timeout_for_handles(
            user_id, "c1", [h["id"]], 0, app=APP, user={"id": user_id},
        )
        await wait_timer._active_timers[h["id"]]

    _run(_drive())

    assert _status(env, h["id"]) == "accepted"
    assert env["kicks"] == []
    assert env["events"] == []


def test_timer_without_app_times_out_but_does_not_kick(env):
    wait_timer = env["wait_timer"]
    user_id = _make_user(env)
    h = _handle(env, user_id)

    async def _drive():
        wait_timer.schedule_timeout_for_handles(user_id, "c1", [h["id"]], 0)
        await wait_timer._active_timers[h["id"]]

    _run(_drive())

    assert _status(env, h["id"]) == "timed_out"
    assert env["kicks"] == []
    assert len(env["events"]) == 1


def test_rescheduling_replaces_the_existing_timer(env):
    wait_timer = env["wait_timer"]
    user_id = _make_user(env)
    h = _handle(env, user_id)

    async def _drive():
        wait_timer.schedule_timeout_for_handles(
            user_id, "c1", [h["id"]], 3600, app=APP, user={"id": user_id},
        )
        first = wait_timer._active_timers[h["id"]]
        wait_timer.schedule_timeout_for_handles(
            user_id, "c1", [h["id"]], 0, app=APP, user={"id": user_id},
        )
        second = wait_timer._active_timers[h["id"]]
        assert second is not first
        await second
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()

    _run(_drive())

    assert _status(env, h["id"]) == "timed_out"
    assert env["kicks"] == [(APP, user_id, "c1")]


def test_schedule_without_running_loop_is_a_noop(env):
    wait_timer = env["wait_timer"]
    wait_timer.schedule_timeout_for_handles(1, "c1", ["h-1"], 5)
    assert "h-1" not in wait_timer._active_timers
