"""Tests for the ``public_project_routines`` feature gate.

Public projects have no routines unless an admin opens the gate (Settings >
Features) for the user. Covered here, against an isolated SQLite file and a
per-test feature-gate store:

1. Registry: the gate is known, off by default and per-user capable
   (public projects themselves are ungated).
2. Routine / schedule routes and the one-click run endpoint refuse public
   projects while the gate is closed and work while it is open; private
   projects are never affected; nothing is deleted.
3. The scheduler skips gated public-project routines (no run, no ledger
   row, ``next_due_at`` moved on) and runs them once the gate is open.
4. The public system prompt for a routine run drops the naming step.

Same monkeypatched-``AsyncSessionLocal`` isolation pattern as
tests/test_schedule_catchup.py.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config.feature_gates as fg
from db import schedule_timing as st

UTC = timezone.utc
EMAIL = "alice@example.com"
DUE = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)  # Mon 09:00 America/New_York


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")

    tmpdir = tempfile.mkdtemp(prefix="quest_public_routines_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    chats_dir = os.path.join(tmpdir, "chats")
    projects_dir = os.path.join(tmpdir, "projects")
    os.makedirs(chats_dir)
    os.makedirs(projects_dir)
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.conversation_store as conv_store_mod
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod
    import db.schedule_store as schedule_store_mod
    import db.skill_store as skill_store_mod
    import chat.scheduler as scheduler_mod
    import chat.storage as storage_mod

    models_mod.Base.metadata.create_all(sync_engine)
    for mod in (
        conv_store_mod, project_store_mod, routine_store_mod,
        schedule_store_mod, skill_store_mod,
    ):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", Path(chats_dir), raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", Path(projects_dir), raising=True)

    from chat.project_routes import bus
    monkeypatch.setattr(bus, "publish_to_user", lambda *_a, **_k: None)

    spawned: list[str] = []
    monkeypatch.setattr(
        scheduler_mod, "_spawn_run",
        lambda app, schedule, routine, run_id: spawned.append(routine["id"]),
    )
    # The daily DST reconversion is unrelated to these tests.
    monkeypatch.setattr(scheduler_mod, "_last_reconversion_date", datetime.now(UTC).date())

    async def _seed():
        async with session_local() as db:
            u = models_mod.User(email=EMAIL, api_key=f"k-{uuid.uuid4().hex}")
            db.add(u)
            await db.commit()
            await db.refresh(u)
            user_id = u.id
        public = await project_store_mod.create_project(user_id, name="Open", public=True)
        private = await project_store_mod.create_project(user_id, name="Closed")
        return user_id, public, private

    user_id, public, private = _run(_seed())
    yield {
        "models": models_mod,
        "routines": routine_store_mod,
        "schedules": schedule_store_mod,
        "scheduler": scheduler_mod,
        "session": session_local,
        "user": {"id": user_id, "email": EMAIL},
        "public": public,
        "private": private,
        "spawned": spawned,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _open_gates(routines: bool = True):
    fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECT_ROUTINES, routines)


def _routine(env, project, name="Digest"):
    """Create a routine straight in the store (bypassing the route gate)."""
    return _run(env["routines"].create_routine(
        env["user"]["id"], project["id"], name, "go",
    ))


def _assert_gated(exc):
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "public_project_routines_disabled"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestGateRegistry:
    def test_registered_off_by_default_and_per_user(self, env):
        feature = fg.FEATURE_PUBLIC_PROJECT_ROUTINES
        assert feature in fg.KNOWN_FEATURES
        assert feature in fg.FEATURE_LABELS
        assert feature in fg.PER_USER_ACCESS_FEATURES
        assert not fg.is_feature_enabled(feature)
        assert not fg.public_project_routines_enabled_for(EMAIL)

    def test_not_a_conversation_flag(self, env):
        from chat.conversation_flags import KNOWN_FLAGS
        assert fg.FEATURE_PUBLIC_PROJECT_ROUTINES not in KNOWN_FLAGS

    def test_only_its_own_gate_is_needed(self, env):
        # Public projects are ungated, so this is the sole switch.
        fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECT_ROUTINES, True)
        assert fg.public_project_routines_enabled_for(EMAIL)

    def test_per_user_access(self, env):
        _open_gates()
        fg.set_feature_allowed_users(
            fg.FEATURE_PUBLIC_PROJECT_ROUTINES, ["bob@example.com"]
        )
        assert not fg.public_project_routines_enabled_for(EMAIL)
        assert fg.public_project_routines_enabled_for("Bob@Example.com")


# ---------------------------------------------------------------------------
# Routine / schedule routes
# ---------------------------------------------------------------------------


class TestRoutineRoutes:
    def test_create_in_public_project_follows_the_gate(self, env):
        from chat.routine_routes import CreateRoutineRequest, create_project_routine
        body = CreateRoutineRequest(name="Digest", prompt="go")

        _open_gates(routines=False)
        with pytest.raises(HTTPException) as exc:
            _run(create_project_routine(env["public"]["id"], body, user=env["user"]))
        _assert_gated(exc)

        _open_gates()
        created = _run(create_project_routine(env["public"]["id"], body, user=env["user"]))
        assert created["project_id"] == env["public"]["id"]

    def test_private_project_never_gated(self, env):
        from chat.routine_routes import CreateRoutineRequest, create_project_routine
        created = _run(create_project_routine(
            env["private"]["id"], CreateRoutineRequest(name="Digest", prompt="go"),
            user=env["user"],
        ))
        assert created["project_id"] == env["private"]["id"]

    def test_closing_the_gate_hides_and_locks_existing_routines(self, env):
        from chat.routine_routes import (
            UpdateRoutineRequest, delete_project_routine, get_project_routine,
            list_project_routines, update_project_routine,
        )
        routine = _routine(env, env["public"])
        pid, rid, user = env["public"]["id"], routine["id"], env["user"]

        _open_gates()
        listed = _run(list_project_routines(pid, user=user))["routines"]
        assert [r["id"] for r in listed] == [rid]

        _open_gates(routines=False)
        assert _run(list_project_routines(pid, user=user)) == {"routines": []}
        for call in (
            lambda: get_project_routine(pid, rid, user=user),
            lambda: update_project_routine(
                pid, rid, UpdateRoutineRequest(name="Renamed"), user=user,
            ),
            lambda: delete_project_routine(pid, rid, user=user),
        ):
            with pytest.raises(HTTPException) as exc:
                _run(call())
            _assert_gated(exc)

        # Nothing was deleted or renamed: reopening the gate restores it.
        _open_gates()
        assert _run(get_project_routine(pid, rid, user=user))["name"] == "Digest"

    def test_user_outside_the_allowed_list_is_gated(self, env):
        from chat.routine_routes import get_project_routine
        routine = _routine(env, env["public"])
        _open_gates()
        fg.set_feature_allowed_users(
            fg.FEATURE_PUBLIC_PROJECT_ROUTINES, ["bob@example.com"]
        )
        with pytest.raises(HTTPException) as exc:
            _run(get_project_routine(env["public"]["id"], routine["id"], user=env["user"]))
        _assert_gated(exc)

    def test_schedule_routes_follow_the_gate(self, env):
        from chat.schedule_routes import (
            CreateScheduleRequest, create_routine_schedule, get_routine_schedule,
        )
        routine = _routine(env, env["public"])
        pid, rid, user = env["public"]["id"], routine["id"], env["user"]
        body = CreateScheduleRequest(schedule_type="hourly", hourly_minute=0)

        _open_gates(routines=False)
        with pytest.raises(HTTPException) as exc:
            _run(create_routine_schedule(pid, rid, body, user=user))
        _assert_gated(exc)

        _open_gates()
        _run(create_routine_schedule(pid, rid, body, user=user))
        assert _run(get_routine_schedule(pid, rid, user=user))["schedule_type"] == "hourly"

        _open_gates(routines=False)
        with pytest.raises(HTTPException) as exc:
            _run(get_routine_schedule(pid, rid, user=user))
        _assert_gated(exc)

    def test_public_routine_cannot_autoload_skills(self, env):
        from chat.routine_routes import (
            RoutineAutoloadRequest, toggle_routine_skill_autoload,
        )
        routine = _routine(env, env["public"])
        _open_gates()
        with pytest.raises(HTTPException) as exc:
            _run(toggle_routine_skill_autoload(
                env["public"]["id"], routine["id"], "any-skill",
                RoutineAutoloadRequest(enabled=True), user=env["user"],
            ))
        assert exc.value.status_code == 400
        assert exc.value.detail["error"] == "public_project_no_skills"


class TestOneClickRun:
    def _start(self, env, project, routine):
        from chat.project_routes import (
            CreateProjectConversationRequest, create_project_conversation,
        )
        return _run(create_project_conversation(
            project["id"],
            CreateProjectConversationRequest(routine_id=routine["id"]),
            user=env["user"],
        ))

    def test_refused_while_gate_closed(self, env):
        routine = _routine(env, env["public"])
        _open_gates(routines=False)
        with pytest.raises(HTTPException) as exc:
            self._start(env, env["public"], routine)
        _assert_gated(exc)

    def test_allowed_while_gate_open(self, env):
        routine = _routine(env, env["public"])
        _open_gates()
        assert self._start(env, env["public"], routine)["id"]

    def test_plain_public_conversation_unaffected(self, env):
        from chat.project_routes import create_project_conversation
        _open_gates(routines=False)
        assert _run(create_project_conversation(env["public"]["id"], user=env["user"]))["id"]

    def test_private_routine_unaffected(self, env):
        routine = _routine(env, env["private"])
        assert self._start(env, env["private"], routine)["id"]


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _daily_at_due(env, routine):
    sched = _run(env["schedules"].create_schedule(
        user_id=env["user"]["id"], routine_id=routine["id"],
        schedule_type="daily", daily_time_local="09:00",
        timezone_str="America/New_York",
    ))
    _run(env["schedules"].set_next_due(sched["id"], DUE))
    return sched


def _poll(env, now):
    _run(env["scheduler"]._poll_and_execute(None, now=now))


def _run_rows(env):
    async def _q():
        async with env["session"]() as db:
            rows = (await db.execute(
                select(env["models"].RoutineScheduleRun)
            )).scalars().all()
            return [(st.as_utc(r.occurrence_at), r.status) for r in rows]
    return _run(_q())


def _stored_next_due(env, schedule_id):
    async def _q():
        async with env["session"]() as db:
            row = await db.get(env["models"].RoutineSchedule, schedule_id)
            return st.as_utc(row.next_due_at)
    return _run(_q())


class TestScheduler:
    def test_gated_public_routine_is_skipped_without_a_trace(self, env):
        routine = _routine(env, env["public"])
        sched = _daily_at_due(env, routine)
        _open_gates(routines=False)

        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == []
        assert _run_rows(env) == []
        # The occurrence is passed over, not left to be caught up later.
        assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)

        # Reopening the gate inside the catch-up grace does not run it late.
        _open_gates()
        _poll(env, DUE + timedelta(hours=1))
        assert env["spawned"] == []
        assert _run_rows(env) == []

        # The next regular occurrence fires.
        _poll(env, DUE + timedelta(days=1, seconds=5))
        assert env["spawned"] == [routine["id"]]

    def test_open_gate_runs_public_routine(self, env):
        routine = _routine(env, env["public"])
        _daily_at_due(env, routine)
        _open_gates()
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]
        assert _run_rows(env) == [(DUE, "running")]

    def test_owner_outside_the_allowed_list_is_skipped(self, env):
        routine = _routine(env, env["public"])
        _daily_at_due(env, routine)
        _open_gates()
        fg.set_feature_allowed_users(
            fg.FEATURE_PUBLIC_PROJECT_ROUTINES, ["bob@example.com"]
        )
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == []

    def test_private_routine_runs_with_gates_closed(self, env):
        routine = _routine(env, env["private"])
        _daily_at_due(env, routine)
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]

    def test_gated_interval_schedule_waits_for_the_gate(self, env):
        routine = _routine(env, env["public"])
        _run(env["schedules"].create_schedule(
            user_id=env["user"]["id"], routine_id=routine["id"],
            schedule_type="every_n_minutes", interval_minutes=10,
        ))
        _open_gates(routines=False)
        _poll(env, DUE)
        assert env["spawned"] == []
        assert _run_rows(env) == []

        _open_gates()
        _poll(env, DUE + timedelta(minutes=1))
        assert env["spawned"] == [routine["id"]]

    def test_interrupted_run_is_not_retried_while_gated(self, env):
        routine = _routine(env, env["public"])
        sched = _daily_at_due(env, routine)

        # Created "last week", so the edited-after-the-occurrence retry
        # guard does not apply.
        async def _backdate():
            async with env["session"]() as db:
                row = await db.get(env["models"].RoutineSchedule, sched["id"])
                row.updated_at = DUE - timedelta(days=7)
                await db.commit()
        _run(_backdate())

        _open_gates()
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]
        _run(env["schedules"].mark_running_runs_interrupted())

        _open_gates(routines=False)
        _poll(env, DUE + timedelta(minutes=5))
        assert env["spawned"] == [routine["id"]]

        _open_gates()
        _poll(env, DUE + timedelta(minutes=6))
        assert env["spawned"] == [routine["id"], routine["id"]]


# ---------------------------------------------------------------------------
# Public system prompt for routine runs
# ---------------------------------------------------------------------------


class TestPublicRoutinePrompt:
    def test_routine_run_skips_the_naming_step(self):
        from chat.gemini_api.system_prompt import get_public_project_system_prompt
        regular = get_public_project_system_prompt(user_email=EMAIL)
        routine = get_public_project_system_prompt(user_email=EMAIL, is_routine=True)
        assert "set_conversation_name" in regular
        assert "set_conversation_name" not in routine
        assert "already named after its routine" in routine
        # Still the public prompt: no proxy preamble, boundaries intact.
        assert "PUBLIC project" in routine
        assert "create_action_request` is unavailable" in routine
