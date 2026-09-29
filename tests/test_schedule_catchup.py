"""Tests for restart-resilient routine scheduling and the weekly schedule type.

Covers:

1. db/schedule_timing.py occurrence math: hourly / daily / weekly, DST gap
   and fold handling, backlog enumeration.
2. The scheduler poll against an isolated SQLite file (spawned runs are
   captured, not executed): on-time fire, no double fire, catch-up inside the
   grace, missed past the grace, backlog coalescing, NULL next_due_at
   initialization, interval schedules, and interrupted-run recovery + retry.
3. Store validation and next_due_at recomputation on edit.

Same monkeypatched-``AsyncSessionLocal`` isolation pattern as
tests/test_routine_costs.py.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db import schedule_timing as st

UTC = timezone.utc


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Occurrence math
# ---------------------------------------------------------------------------


def _daily(time_local="09:00", tz="America/New_York"):
    return {"schedule_type": "daily", "daily_time_local": time_local, "timezone": tz}


def _weekly(days, time_local="09:00", tz="America/New_York"):
    return {
        "schedule_type": "weekly", "daily_time_local": time_local,
        "timezone": tz, "weekly_days": days,
    }


def test_hourly_occurrences():
    sched = {"schedule_type": "hourly", "hourly_minute": 15}
    t = datetime(2026, 9, 28, 10, 20, tzinfo=UTC)
    assert st.occurrence_at_or_before(sched, t) == datetime(2026, 9, 28, 10, 15, tzinfo=UTC)
    assert st.occurrence_after(sched, t) == datetime(2026, 9, 28, 11, 15, tzinfo=UTC)
    exact = datetime(2026, 9, 28, 10, 15, tzinfo=UTC)
    assert st.occurrence_at_or_before(sched, exact) == exact
    assert st.occurrence_after(sched, exact) == datetime(2026, 9, 28, 11, 15, tzinfo=UTC)


def test_daily_occurrence_uses_local_time_across_dst():
    sched = _daily("09:00")
    # EDT (UTC-4) in September, EST (UTC-5) in December.
    assert st.occurrence_after(sched, datetime(2026, 9, 28, 0, 0, tzinfo=UTC)) == \
        datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    assert st.occurrence_after(sched, datetime(2026, 12, 1, 0, 0, tzinfo=UTC)) == \
        datetime(2026, 12, 1, 14, 0, tzinfo=UTC)


def test_daily_dst_gap_shifts_forward_and_fold_uses_first():
    # 2026-03-08 02:30 does not exist in New York (clocks jump 02:00 -> 03:00).
    gap = _daily("02:30")
    occ = st.occurrence_after(gap, datetime(2026, 3, 8, 0, 0, tzinfo=UTC))
    assert occ == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # 03:30 EDT
    # The day is never skipped: next occurrence is the following day.
    assert st.occurrence_after(gap, occ) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)

    # 2026-11-01 01:30 happens twice; the first one (EDT, UTC-4) is used.
    fold = _daily("01:30")
    occ = st.occurrence_after(fold, datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert occ == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert st.occurrence_after(fold, occ) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_weekly_occurrences_follow_local_weekday():
    # 2026-09-28 is a Monday.
    sched = _weekly([0, 3], "09:00")  # Mon + Thu
    t = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)  # Mon 10:00 EDT
    assert st.occurrence_at_or_before(sched, t) == datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    assert st.occurrence_after(sched, t) == datetime(2026, 10, 1, 13, 0, tzinfo=UTC)  # Thu

    # Local weekday, not UTC weekday: Sunday 20:00 in LA is Monday 03:00 UTC.
    la = _weekly([6], "20:00", "America/Los_Angeles")
    assert st.occurrence_after(la, datetime(2026, 9, 27, 0, 0, tzinfo=UTC)) == \
        datetime(2026, 9, 28, 3, 0, tzinfo=UTC)


def test_occurrences_between_is_half_open():
    sched = {"schedule_type": "hourly", "hourly_minute": 0}
    start = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    end = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
    assert st.occurrences_between(sched, start, end) == [
        datetime(2026, 9, 28, h, 0, tzinfo=UTC) for h in (1, 2, 3)
    ]


def test_validate_weekly_days():
    assert st.validate_weekly_days([4, 0, 0]) == [0, 4]
    for bad in ([], None, [7], [-1], ["mon"], [True]):
        with pytest.raises(ValueError):
            st.validate_weekly_days(bad)
    assert st.describe_weekly_days([0, 1, 2, 3, 4]) == "weekdays"
    assert st.describe_weekly_days("1,3") == "Tue, Thu"


# ---------------------------------------------------------------------------
# Scheduler against an isolated DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_schedule_catchup_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod
    import db.schedule_store as schedule_store_mod
    import chat.scheduler as scheduler_mod

    models_mod.Base.metadata.create_all(sync_engine)
    for mod in (project_store_mod, routine_store_mod, schedule_store_mod):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    spawned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler_mod, "_spawn_run",
        lambda app, schedule, routine, run_id: spawned.append((schedule["id"], run_id)),
    )
    # The daily DST reconversion is unrelated to these tests.
    monkeypatch.setattr(scheduler_mod, "_last_reconversion_date", datetime.now(UTC).date())

    notices: list[tuple[str, list]] = []

    async def _fake_persist(conversation_id, messages):
        notices.append((conversation_id, messages))

    monkeypatch.setattr(scheduler_mod, "_persist_scheduler_messages", _fake_persist)

    async def _seed():
        async with session_local() as db:
            u = models_mod.User(email="alice@example.com", api_key=f"k-{uuid.uuid4().hex}")
            db.add(u)
            await db.commit()
            await db.refresh(u)
            user_id = u.id
        project = await project_store_mod.create_project(user_id, name="P")
        routine = await routine_store_mod.create_routine(user_id, project["id"], "Digest", "go")
        return user_id, routine

    user_id, routine = _run(_seed())
    yield {
        "models": models_mod,
        "store": schedule_store_mod,
        "scheduler": scheduler_mod,
        "session": session_local,
        "user_id": user_id,
        "routine": routine,
        "spawned": spawned,
        "notices": notices,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _create(env, **kwargs):
    return _run(env["store"].create_schedule(
        user_id=env["user_id"], routine_id=env["routine"]["id"], **kwargs,
    ))


def _poll(env, now):
    _run(env["scheduler"]._poll_and_execute(None, now=now))


def _runs(env):
    async def _q():
        async with env["session"]() as db:
            rows = (await db.execute(
                select(env["models"].RoutineScheduleRun)
                .order_by(env["models"].RoutineScheduleRun.occurrence_at)
            )).scalars().all()
            return [(st.as_utc(r.occurrence_at), r.status, r.attempt) for r in rows]
    return _run(_q())


def _schedule(env):
    return _run(env["store"].get_schedule_for_routine(env["routine"]["id"], include_runs=True))


def _stored_next_due(env, schedule_id):
    async def _q():
        async with env["session"]() as db:
            row = await db.get(env["models"].RoutineSchedule, schedule_id)
            return st.as_utc(row.next_due_at)
    return _run(_q())


DUE = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)  # Mon 09:00 America/New_York


def _daily_at_due(env, **extra):
    sched = _create(
        env, schedule_type="daily", daily_time_local="09:00",
        timezone_str="America/New_York", **extra,
    )
    _run(env["store"].set_next_due(sched["id"], DUE))

    # Created "last week", so the edited-after-the-occurrence retry guard
    # does not apply.
    async def _backdate():
        async with env["session"]() as db:
            row = await db.get(env["models"].RoutineSchedule, sched["id"])
            row.updated_at = DUE - timedelta(days=7)
            await db.commit()
    _run(_backdate())
    return sched


def test_create_sets_next_due_in_the_future(env):
    sched = _create(
        env, schedule_type="weekly", daily_time_local="09:00",
        timezone_str="America/New_York", weekly_days=[0, 4],
    )
    due = datetime.fromisoformat(sched["next_due_at"])
    assert due > datetime.now(UTC)
    assert sched["weekly_days"] == [0, 4]


def test_on_time_fire_happens_once(env):
    sched = _daily_at_due(env)
    _poll(env, DUE - timedelta(seconds=40))
    assert env["spawned"] == []
    _poll(env, DUE + timedelta(seconds=5))
    _poll(env, DUE + timedelta(seconds=35))
    assert len(env["spawned"]) == 1
    assert _runs(env) == [(DUE, "running", 1)]
    assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)


def test_already_claimed_occurrence_never_fires_twice(env):
    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    # Rewind next_due_at to the occurrence that already has a ledger row:
    # the unique index rejects the second claim and the schedule moves on.
    _run(env["store"].set_next_due(sched["id"], DUE))
    _poll(env, DUE + timedelta(minutes=1))
    assert len(env["spawned"]) == 1
    assert _runs(env) == [(DUE, "running", 1)]
    assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)

    # Same for an occurrence that is past the grace (record_missed path).
    _run(env["store"].set_next_due(sched["id"], DUE))
    _poll(env, DUE + timedelta(hours=7))
    assert _runs(env) == [(DUE, "running", 1)]
    assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)


def test_late_poll_within_grace_catches_up(env):
    _daily_at_due(env)
    # Server was down 08:55-12:00 local; the 09:00 run still happens.
    _poll(env, DUE + timedelta(hours=3))
    assert len(env["spawned"]) == 1
    assert _runs(env) == [(DUE, "running", 1)]


def test_past_grace_is_recorded_missed(env):
    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(hours=7))
    assert env["spawned"] == []
    assert _runs(env) == [(DUE, "missed", 0)]
    assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)
    recent = _schedule(env)["recent_runs"]
    assert recent[0]["status"] == "missed"


def test_backlog_collapses_into_one_run_of_the_newest(env):
    sched = _create(env, schedule_type="hourly", hourly_minute=0)
    start = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    _run(env["store"].set_next_due(sched["id"], start))
    _poll(env, datetime(2026, 9, 28, 4, 10, tzinfo=UTC))
    assert len(env["spawned"]) == 1
    assert _runs(env) == [
        (datetime(2026, 9, 28, 1, 0, tzinfo=UTC), "missed", 0),
        (datetime(2026, 9, 28, 2, 0, tzinfo=UTC), "missed", 0),
        (datetime(2026, 9, 28, 3, 0, tzinfo=UTC), "missed", 0),
        (datetime(2026, 9, 28, 4, 0, tzinfo=UTC), "running", 1),
    ]
    assert _stored_next_due(env, sched["id"]) == datetime(2026, 9, 28, 5, 0, tzinfo=UTC)


def test_null_next_due_initializes_without_firing(env):
    sched = _daily_at_due(env)
    _run(env["store"].set_next_due(sched["id"], None))
    now = DUE + timedelta(minutes=1)
    _poll(env, now)
    assert env["spawned"] == []
    assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)


def test_retiming_recomputes_next_due(env):
    sched = _daily_at_due(env)
    updated = _run(env["store"].update_schedule(
        schedule_id=sched["id"], user_id=env["user_id"], daily_time_local="18:30",
    ))
    due = datetime.fromisoformat(updated["next_due_at"])
    local = due.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (18, 30)
    assert due > datetime.now(UTC)


def test_weekly_requires_days(env):
    with pytest.raises(ValueError, match="weekly_days"):
        _create(
            env, schedule_type="weekly", daily_time_local="09:00",
            timezone_str="America/New_York",
        )
    sched = _create(
        env, schedule_type="weekly", daily_time_local="09:00",
        timezone_str="America/New_York", weekly_days=[2],
    )
    with pytest.raises(ValueError, match="weekly_days"):
        _run(env["store"].update_schedule(
            schedule_id=sched["id"], user_id=env["user_id"], weekly_days=[],
        ))


def test_interval_schedule_fires_and_skips_while_running(env):
    _create(env, schedule_type="every_n_minutes", interval_minutes=10)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    _poll(env, now)
    assert len(env["spawned"]) == 1
    # is_running is set at claim time, so the next poll can't double-fire.
    _poll(env, now + timedelta(minutes=20))
    assert len(env["spawned"]) == 1


def test_restart_interrupts_running_run_and_retries_once(env, monkeypatch):
    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    (_, run_id), = env["spawned"]
    _run(env["store"].set_run_conversation(sched["id"], run_id, "conv-1"))

    # Hard kill + restart ten minutes later: the startup sweep marks the row
    # interrupted, clears is_running and leaves a notice in the conversation.
    restart = DUE + timedelta(minutes=10)
    monkeypatch.setattr(env["scheduler"], "datetime", _FrozenDatetime(restart))
    # The poll's once-a-day DST reconversion gate reads the (now frozen)
    # clock; keep it suppressed for the frozen date too, otherwise it
    # re-saves the schedule with a real-clock updated_at after the
    # occurrence and the edited-after-occurrence guard blocks the retry.
    monkeypatch.setattr(env["scheduler"], "_last_reconversion_date", restart.date())
    _run(env["scheduler"]._recover_interrupted_runs())
    assert _runs(env) == [(DUE, "interrupted", 1)]
    assert _schedule(env)["is_running"] is False
    (conv, msgs), = env["notices"]
    assert conv == "conv-1"
    assert msgs[0]["type"] == "error" and "retry" in msgs[0]["error"]

    # The next poll retries it (attempt 2) without re-firing the occurrence.
    _poll(env, restart + timedelta(seconds=30))
    assert len(env["spawned"]) == 2
    assert env["spawned"][1][1] == run_id
    assert _runs(env) == [(DUE, "running", 2)]

    # A second interruption exhausts the attempts: no further retry.
    _run(env["scheduler"]._recover_interrupted_runs())
    _poll(env, restart + timedelta(minutes=5))
    assert len(env["spawned"]) == 2
    assert _runs(env) == [(DUE, "interrupted", 2)]


def test_interrupted_run_is_not_retried_after_an_edit(env):
    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    _run(env["scheduler"]._recover_interrupted_runs())
    # Any edit after the occurrence (here: pause + resume) cancels the retry.
    async def _touch():
        async with env["session"]() as db:
            row = await db.get(env["models"].RoutineSchedule, sched["id"])
            row.updated_at = DUE + timedelta(minutes=1)
            await db.commit()
    _run(_touch())
    _poll(env, DUE + timedelta(minutes=10))
    assert len(env["spawned"]) == 1


def test_interrupted_run_past_grace_is_not_retried(env):
    _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    _run(env["scheduler"]._recover_interrupted_runs())
    _poll(env, DUE + timedelta(hours=7))
    assert len(env["spawned"]) == 1
    assert _runs(env)[0][1] == "interrupted"


def test_finish_run_completed_updates_schedule(env):
    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    (_, run_id), = env["spawned"]
    _run(env["store"].finish_run(sched["id"], run_id, "completed"))
    s = _schedule(env)
    assert s["is_running"] is False
    assert s["last_run_completed_at"] is not None
    assert s["recent_runs"][0]["status"] == "completed"


class _FrozenDatetime:
    """Stand-in for the ``datetime`` class in chat.scheduler with a fixed now()."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now

    def __getattr__(self, name):
        return getattr(datetime, name)


def test_shutdown_cancellation_marks_run_interrupted(env, monkeypatch):
    import chat.gemini_api as gemini_api_mod
    import db.user_store as user_store_mod
    from chat.storage import ChatStorage

    sched = _daily_at_due(env)
    _poll(env, DUE + timedelta(seconds=5))
    (_, run_id), = env["spawned"]

    async def _user(user_id):
        return {"id": user_id, "email": "alice@example.com"}

    async def _create_conv(*args, **kwargs):
        return "conv-9", None

    async def _append(**kwargs):
        return None

    started = asyncio.Event()

    async def _forever(**kwargs):
        kwargs["messages_out"].append({"type": "text", "content": "partial"})
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(user_store_mod, "get_user_by_id", _user)
    monkeypatch.setattr(ChatStorage, "create_project_conversation", staticmethod(_create_conv))
    monkeypatch.setattr(ChatStorage, "append_message", staticmethod(_append))
    monkeypatch.setattr(gemini_api_mod, "run_conversation_turn", _forever)

    schedules = _run(env["store"].list_enabled_schedules())

    async def _go():
        task = asyncio.create_task(env["scheduler"]._execute_scheduled_run(
            None, schedules[0], schedules[0]["routine"], run_id,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(_go())
    assert _runs(env) == [(DUE, "interrupted", 1)]
    assert _schedule(env)["is_running"] is False
    (conv, msgs), = env["notices"]
    assert conv == "conv-9"
    # Partial output is kept, followed by the interruption notice.
    assert msgs[0]["content"] == "partial"
    assert msgs[-1]["type"] == "error"
    assert "interrupted by a server restart" in msgs[-1]["error"]


def test_routine_action_request_weekly_spec():
    from chat.action_request_types._routine_validation import (
        describe_schedule, validate_schedule_spec,
    )

    spec = validate_schedule_spec({
        "schedule_type": "weekly", "daily_time_local": "08:00",
        "timezone": "Europe/London", "weekly_days": [4, 0],
    })
    assert spec["weekly_days"] == [0, 4]
    assert describe_schedule(spec) == "Weekly on Mon, Fri at 08:00 (Europe/London)"
    with pytest.raises(ValueError, match="weekly_days"):
        validate_schedule_spec({
            "schedule_type": "weekly", "daily_time_local": "08:00",
            "timezone": "Europe/London",
        })
