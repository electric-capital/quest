"""Tests for the per-routine cost breakdown of the admin Users report.

Covers the two pieces the ``/admin/system-monitor/user-report`` endpoint
adds on top of ``get_usage_by_user()`` (whose per-routine fold is tested in
test_system_reports_analytics.py):

1. ``get_routine_labels()`` in db/routine_store.py -- batched routine name +
   owning-project name lookup across owners.
2. ``_routine_cost_view()`` in chat/routes/admin.py -- the row projection,
   incl. the placeholder for a routine that vanished mid-request.

Each store's ``AsyncSessionLocal`` is monkeypatched onto a fresh sqlite file.
"""

import asyncio
import os
import shutil
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_user_report_routines_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    test_session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod

    models_mod.Base.metadata.create_all(sync_engine)

    for mod in (project_store_mod, routine_store_mod):
        monkeypatch.setattr(mod, "AsyncSessionLocal", test_session_local)

    yield models_mod, project_store_mod, routine_store_mod

    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _create_user(models_mod, session_factory, email):
    async with session_factory() as db:
        u = models_mod.User(email=email, api_key=f"k-{uuid.uuid4().hex}")
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


def test_get_routine_labels_resolves_names_across_owners(_isolated_db):
    models_mod, project_store, routine_store = _isolated_db
    factory = routine_store.AsyncSessionLocal

    async def _seed():
        alice = await _create_user(models_mod, factory, "alice@example.com")
        bob = await _create_user(models_mod, factory, "bob@example.com")
        a_project = await project_store.create_project(alice, name="Research")
        b_project = await project_store.create_project(bob, name="Ops")
        # Same routine name in two projects: the project name disambiguates.
        a_daily = await routine_store.create_routine(alice, a_project["id"], "Daily", "run")
        b_daily = await routine_store.create_routine(bob, b_project["id"], "Daily", "run")
        a_weekly = await routine_store.create_routine(alice, a_project["id"], "Weekly", "run")
        return a_project, b_project, a_daily, b_daily, a_weekly

    a_project, b_project, a_daily, b_daily, a_weekly = _run(_seed())

    labels = _run(routine_store.get_routine_labels(
        {a_daily["id"], b_daily["id"], "no-such-routine"}
    ))

    assert labels == {
        a_daily["id"]: {
            "name": "Daily", "project_id": a_project["id"], "project_name": "Research",
        },
        b_daily["id"]: {
            "name": "Daily", "project_id": b_project["id"], "project_name": "Ops",
        },
    }
    # Only the requested ids come back, and an unknown id is simply absent.
    assert a_weekly["id"] not in labels


def test_get_routine_labels_empty_input_short_circuits(_isolated_db):
    _, _, routine_store = _isolated_db
    assert _run(routine_store.get_routine_labels(set())) == {}


def test_routine_cost_view_labels_row_and_placeholders_missing_routine():
    from chat.routes.admin import _routine_cost_view

    usage = {
        "routine_id": "r-1",
        "conversation_count": 3,
        "cost_usd": 1.25,
        "cost_source": "reported",
        "known_cost_usd": 1.25,
    }
    label = {"name": "Daily digest", "project_id": "p-1", "project_name": "Research"}

    assert _routine_cost_view(usage, label) == {
        "routine_id": "r-1",
        "routine_name": "Daily digest",
        "project_id": "p-1",
        "project_name": "Research",
        "conversation_count": 3,
        "cost_usd": 1.25,
        "cost_source": "reported",
    }
    # The internal ranking key never leaks to the API row.
    assert "known_cost_usd" not in _routine_cost_view(usage, label)

    assert _routine_cost_view(
        {**usage, "cost_usd": None, "cost_source": None}, None
    ) == {
        "routine_id": "r-1",
        "routine_name": "(deleted routine)",
        "project_id": None,
        "project_name": None,
        "conversation_count": 3,
        "cost_usd": None,
        "cost_source": None,
    }
