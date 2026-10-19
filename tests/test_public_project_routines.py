"""Tests for routines in public projects.

Routines are available in every project, public ones included (the
``public_project_routines`` feature gate that once covered them was removed
in October 2026). Covered here, against an isolated SQLite file:

1. The old gate key is gone from the registry.
2. Routine / schedule routes and the one-click run endpoint work in public
   projects exactly as in private ones; the public-only restriction that
   remains (no skill auto-loads) still holds.
3. The scheduler runs public-project routines.
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


def _routine(env, project, name="Digest"):
    """Create a routine straight in the store."""
    return _run(env["routines"].create_routine(
        env["user"]["id"], project["id"], name, "go",
    ))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestGateRegistry:
    def test_gate_is_gone(self):
        # Routines in public projects are a default feature; a stale key in
        # an older feature_gates.json is ignored like any unknown key.
        assert "public_project_routines" not in fg.KNOWN_FEATURES
        assert "public_project_routines" not in fg.FEATURE_LABELS
        assert "public_project_routines" not in fg.PER_USER_ACCESS_FEATURES
        assert not hasattr(fg, "FEATURE_PUBLIC_PROJECT_ROUTINES")
        assert not hasattr(fg, "public_project_routines_enabled_for")


# ---------------------------------------------------------------------------
# Routine / schedule routes
# ---------------------------------------------------------------------------


class TestRoutineRoutes:
    def test_create_in_public_project(self, env):
        from chat.routine_routes import CreateRoutineRequest, create_project_routine
        body = CreateRoutineRequest(name="Digest", prompt="go")
        created = _run(create_project_routine(env["public"]["id"], body, user=env["user"]))
        assert created["project_id"] == env["public"]["id"]

    def test_create_in_private_project(self, env):
        from chat.routine_routes import CreateRoutineRequest, create_project_routine
        created = _run(create_project_routine(
            env["private"]["id"], CreateRoutineRequest(name="Digest", prompt="go"),
            user=env["user"],
        ))
        assert created["project_id"] == env["private"]["id"]

    def test_public_routines_are_listed_and_editable(self, env):
        from chat.routine_routes import (
            UpdateRoutineRequest, delete_project_routine, get_project_routine,
            list_project_routines, update_project_routine,
        )
        routine = _routine(env, env["public"])
        pid, rid, user = env["public"]["id"], routine["id"], env["user"]

        listed = _run(list_project_routines(pid, user=user))["routines"]
        assert [r["id"] for r in listed] == [rid]
        assert _run(get_project_routine(pid, rid, user=user))["name"] == "Digest"
        renamed = _run(update_project_routine(
            pid, rid, UpdateRoutineRequest(name="Renamed"), user=user,
        ))
        assert renamed["name"] == "Renamed"
        _run(delete_project_routine(pid, rid, user=user))
        assert _run(list_project_routines(pid, user=user)) == {"routines": []}

    def test_schedule_routes_in_public_project(self, env):
        from chat.schedule_routes import (
            CreateScheduleRequest, create_routine_schedule, get_routine_schedule,
        )
        routine = _routine(env, env["public"])
        pid, rid, user = env["public"]["id"], routine["id"], env["user"]
        body = CreateScheduleRequest(schedule_type="hourly", hourly_minute=0)
        _run(create_routine_schedule(pid, rid, body, user=user))
        assert _run(get_routine_schedule(pid, rid, user=user))["schedule_type"] == "hourly"

    def test_public_routine_cannot_autoload_skills(self, env):
        from chat.routine_routes import (
            RoutineAutoloadRequest, toggle_routine_skill_autoload,
        )
        routine = _routine(env, env["public"])
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

    def test_public_routine_run_starts(self, env):
        routine = _routine(env, env["public"])
        assert self._start(env, env["public"], routine)["id"]

    def test_plain_public_conversation(self, env):
        from chat.project_routes import create_project_conversation
        assert _run(create_project_conversation(env["public"]["id"], user=env["user"]))["id"]

    def test_private_routine_run_starts(self, env):
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


class TestScheduler:
    def test_public_routine_runs(self, env):
        routine = _routine(env, env["public"])
        _daily_at_due(env, routine)
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]
        assert _run_rows(env) == [(DUE, "running")]

    def test_private_routine_runs(self, env):
        routine = _routine(env, env["private"])
        _daily_at_due(env, routine)
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]

    def test_public_interval_schedule_runs(self, env):
        routine = _routine(env, env["public"])
        _run(env["schedules"].create_schedule(
            user_id=env["user"]["id"], routine_id=routine["id"],
            schedule_type="every_n_minutes", interval_minutes=10,
        ))
        _poll(env, DUE)
        assert env["spawned"] == [routine["id"]]

    def test_summary_has_no_gate_fields(self, env):
        routine = _routine(env, env["public"])
        _daily_at_due(env, routine)
        [sched] = _run(env["schedules"].list_enabled_schedules())
        assert "project_public" not in sched["routine"]
        assert "user_email" not in sched["routine"]
        assert sched["routine"]["project_archived"] is False


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
        assert "cannot propose writes to external services" in routine
