"""Tests for archiving projects.

An archived project is the project-level twin of an archived conversation:
hidden from the default list (``GET /projects?include_archived=true`` brings
it back), its scheduled routines paused, everything else kept. Covered here,
against an isolated SQLite file:

1. Store: the flag defaults off, flips both ways, leaves ``updated_at``
   alone, and ``list_projects`` filters on request.
2. Routes: the list endpoint hides archived projects unless asked, the
   archive / unarchive endpoints 404 on unknown and gated-off projects.
3. Scheduler: schedules of routines in an archived project are skipped
   without a run or ledger row (``next_due_at`` moved on) and resume at the
   next regular occurrence after unarchiving; interrupted runs are not
   retried while archived.

Same isolation pattern as tests/test_public_project_routines.py.
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
DUE = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)  # Mon 09:00 America/New_York


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")

    tmpdir = tempfile.mkdtemp(prefix="quest_project_archive_test_")
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

    spawned: list[str] = []
    monkeypatch.setattr(
        scheduler_mod, "_spawn_run",
        lambda app, schedule, routine, run_id: spawned.append(routine["id"]),
    )
    monkeypatch.setattr(scheduler_mod, "_last_reconversion_date", datetime.now(UTC).date())

    async def _seed():
        async with session_local() as db:
            u = models_mod.User(email=EMAIL, api_key=f"k-{uuid.uuid4().hex}")
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    user_id = _run(_seed())
    yield {
        "models": models_mod,
        "projects": project_store_mod,
        "routines": routine_store_mod,
        "schedules": schedule_store_mod,
        "scheduler": scheduler_mod,
        "session": session_local,
        "user": {"id": user_id, "email": EMAIL},
        "spawned": spawned,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _project(env, name="Research", **kw):
    return _run(env["projects"].create_project(env["user"]["id"], name, **kw))


def _archive(env, project, archived=True):
    return _run(env["projects"].set_project_archived(
        env["user"]["id"], project["id"], archived,
    ))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_defaults_to_not_archived(self, env):
        project = _project(env)
        assert project["archived"] is False
        assert _run(env["projects"].get_project(env["user"]["id"], project["id"]))["archived"] is False

    def test_flips_both_ways_without_touching_updated_at(self, env):
        project = _project(env)
        archived = _archive(env, project)
        assert archived["archived"] is True
        assert archived["updated_at"] == project["updated_at"]
        restored = _archive(env, project, archived=False)
        assert restored["archived"] is False
        assert restored["name"] == project["name"]

    def test_scoped_to_owner(self, env):
        project = _project(env)
        assert _run(env["projects"].set_project_archived(
            env["user"]["id"] + 1, project["id"], True,
        )) is None
        assert _run(env["projects"].set_project_archived(
            env["user"]["id"], "no-such-project", True,
        )) is None

    def test_list_filters_on_request(self, env):
        kept = _project(env, "Kept")
        gone = _project(env, "Gone")
        _archive(env, gone)
        store = env["projects"]
        uid = env["user"]["id"]

        # The default keeps everything (account deletion relies on it).
        names = {p["name"] for p in _run(store.list_projects(uid))}
        assert names == {"Kept", "Gone"}
        visible = _run(store.list_projects(uid, include_archived=False))
        assert [p["id"] for p in visible] == [kept["id"]]
        # The flag rides on list rows too.
        by_id = {p["id"]: p for p in _run(store.list_projects(uid))}
        assert by_id[gone["id"]]["archived"] is True
        assert by_id[kept["id"]]["archived"] is False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_list_hides_archived_unless_asked(self, env):
        from chat.project_routes import list_user_projects
        user = env["user"]
        _project(env, "Kept")
        gone = _project(env, "Gone")
        _archive(env, gone)

        names = [p["name"] for p in _run(list_user_projects(user=user))["projects"]]
        assert names == ["Kept"]
        names = {
            p["name"]
            for p in _run(list_user_projects(include_archived=True, user=user))["projects"]
        }
        assert names == {"Kept", "Gone"}

    def test_archive_and_unarchive_endpoints(self, env):
        from chat.project_routes import archive_user_project, unarchive_user_project
        user = env["user"]
        project = _project(env)

        archived = _run(archive_user_project(project["id"], user=user))
        assert archived["archived"] is True
        restored = _run(unarchive_user_project(project["id"], user=user))
        assert restored["archived"] is False

        with pytest.raises(HTTPException) as exc:
            _run(archive_user_project("no-such-project", user=user))
        assert exc.value.status_code == 404

    def test_gated_off_public_project_404s(self, env):
        from chat.project_routes import archive_user_project
        user = env["user"]
        fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECTS, True)
        public = _project(env, "Open", public=True)
        fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECTS, False)

        with pytest.raises(HTTPException) as exc:
            _run(archive_user_project(public["id"], user=user))
        assert exc.value.status_code == 404
        assert _run(env["projects"].get_project(user["id"], public["id"]))["archived"] is False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _routine(env, project, name="Digest"):
    return _run(env["routines"].create_routine(
        env["user"]["id"], project["id"], name, "go",
    ))


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
    def test_summary_carries_the_flag(self, env):
        project = _project(env)
        routine = _routine(env, project)
        _daily_at_due(env, routine)
        [sched] = _run(env["schedules"].list_enabled_schedules())
        assert sched["routine"]["project_archived"] is False
        _archive(env, project)
        [sched] = _run(env["schedules"].list_enabled_schedules())
        assert sched["routine"]["project_archived"] is True

    def test_archived_project_schedule_is_skipped_without_a_trace(self, env):
        project = _project(env)
        routine = _routine(env, project)
        sched = _daily_at_due(env, routine)
        _archive(env, project)

        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == []
        assert _run_rows(env) == []
        # The occurrence is passed over, not left to be caught up later.
        assert _stored_next_due(env, sched["id"]) == DUE + timedelta(days=1)

        # Unarchiving inside the catch-up grace does not run it late.
        _archive(env, project, archived=False)
        _poll(env, DUE + timedelta(hours=1))
        assert env["spawned"] == []
        assert _run_rows(env) == []

        # The next regular occurrence fires.
        _poll(env, DUE + timedelta(days=1, seconds=5))
        assert env["spawned"] == [routine["id"]]

    def test_unarchived_project_runs(self, env):
        project = _project(env)
        routine = _routine(env, project)
        _daily_at_due(env, routine)
        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]
        assert _run_rows(env) == [(DUE, "running")]

    def test_interval_schedule_waits_for_unarchive(self, env):
        project = _project(env)
        routine = _routine(env, project)
        _run(env["schedules"].create_schedule(
            user_id=env["user"]["id"], routine_id=routine["id"],
            schedule_type="every_n_minutes", interval_minutes=10,
        ))
        _archive(env, project)
        _poll(env, DUE)
        assert env["spawned"] == []
        assert _run_rows(env) == []

        _archive(env, project, archived=False)
        _poll(env, DUE + timedelta(minutes=1))
        assert env["spawned"] == [routine["id"]]

    def test_interrupted_run_is_not_retried_while_archived(self, env):
        project = _project(env)
        routine = _routine(env, project)
        sched = _daily_at_due(env, routine)

        # Created "last week", so the edited-after-the-occurrence retry
        # guard does not apply.
        async def _backdate():
            async with env["session"]() as db:
                row = await db.get(env["models"].RoutineSchedule, sched["id"])
                row.updated_at = DUE - timedelta(days=7)
                await db.commit()
        _run(_backdate())

        _poll(env, DUE + timedelta(seconds=5))
        assert env["spawned"] == [routine["id"]]
        _run(env["schedules"].mark_running_runs_interrupted())

        _archive(env, project)
        _poll(env, DUE + timedelta(minutes=5))
        assert env["spawned"] == [routine["id"]]

        _archive(env, project, archived=False)
        _poll(env, DUE + timedelta(minutes=6))
        assert env["spawned"] == [routine["id"], routine["id"]]
