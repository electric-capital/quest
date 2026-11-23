"""Tests for the per-routine inference cost report (Routine Settings > Costs).

Covers:

1. ``build_cost_report()`` in chat/routine_costs.py -- the pure fold: rolling
   7/28-day windows with their preceding period, lifetime total, the
   null-on-unpriced convention per window, no-call runs, and the recent-runs
   table (newest first, capped).
2. ``get_routine_cost_report()`` end to end against an isolated SQLite file:
   conversation rows linked by ``routine_id`` feed the ``IN (subquery)``
   usage aggregation (``get_usage_by_model_for_conversation_query``), and
   another routine's runs stay out.
3. The ``GET /projects/{project_id}/routines/{routine_id}/costs`` route's
   ownership checks.

Same monkeypatched-``AsyncSessionLocal`` isolation pattern as
tests/test_admin_user_report_routines.py.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.routine_costs import RECENT_RUNS_LIMIT, build_cost_report


def _iso_z(dt):
    """The endpoint's wire form: naive-UTC ISO with a ``Z`` suffix."""
    return dt.replace(tzinfo=None).isoformat() + "Z"


def _run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _row(conv_id, started_at, custom_name="Daily digest", model="claude-opus-4-8"):
    return {
        "id": conv_id,
        "created_at": started_at,
        "custom_name": custom_name,
        "auto_title": None,
        "last_message_seq": 0,
        "model": model,
    }


def _usage(cost, source="estimated", model="claude-opus-4-8", tokens=1000, calls=2):
    return {
        "models": [
            {
                "model": model,
                "provider": "anthropic",
                "call_count": calls,
                "total_tokens": tokens,
                "estimated_cost_usd": cost,
                "cost_source": source,
                "metrics": {},
            }
        ],
        "total": {
            "call_count": calls,
            "total_tokens": tokens,
            "estimated_cost_usd": cost,
            "cost_source": source,
        },
    }


ROUTINE = {"id": "r-1", "created_at": "2026-01-01T00:00:00"}


# --------------------------------------------------------------------------
# build_cost_report (pure)
# --------------------------------------------------------------------------


def test_windows_bucket_runs_by_start_time_with_previous_period():
    rows = [
        _row("c-today", NOW - timedelta(hours=1)),
        _row("c-6d", NOW - timedelta(days=6)),
        _row("c-10d", NOW - timedelta(days=10)),      # previous 7d, current 28d
        _row("c-27d", NOW - timedelta(days=27)),      # current 28d only
        _row("c-40d", NOW - timedelta(days=40)),      # previous 28d only
        _row("c-70d", NOW - timedelta(days=70)),      # lifetime only
    ]
    usage = {
        "c-today": _usage(1.0),
        "c-6d": _usage(2.0),
        "c-10d": _usage(4.0),
        "c-27d": _usage(8.0),
        "c-40d": _usage(16.0),
        "c-70d": _usage(32.0),
    }

    report = build_cost_report(ROUTINE, rows, usage, now=NOW)

    seven, twenty_eight = report["windows"]
    assert seven["days"] == 7
    assert seven["current"]["run_count"] == 2
    assert seven["current"]["cost_usd"] == 3.0
    assert seven["current"]["cost_source"] == "estimated"
    assert seven["previous"]["run_count"] == 1
    assert seven["previous"]["cost_usd"] == 4.0

    assert twenty_eight["days"] == 28
    assert twenty_eight["current"]["run_count"] == 4
    assert twenty_eight["current"]["cost_usd"] == 15.0
    assert twenty_eight["previous"]["run_count"] == 1
    assert twenty_eight["previous"]["cost_usd"] == 16.0

    assert report["lifetime"]["run_count"] == 6
    assert report["lifetime"]["cost_usd"] == 63.0
    assert report["lifetime"]["call_count"] == 12
    assert report["lifetime"]["total_tokens"] == 6000
    assert report["routine_id"] == "r-1"
    assert report["routine_created_at"] == "2026-01-01T00:00:00"
    assert report["generated_at"] == _iso_z(NOW)


def test_window_boundaries_are_half_open_on_the_old_side():
    # Exactly 7 days old falls into the previous period; just under stays
    # current. Exactly 14 days old drops out of the 7d pair entirely.
    rows = [
        _row("c-under", NOW - timedelta(days=7) + timedelta(seconds=1)),
        _row("c-exact", NOW - timedelta(days=7)),
        _row("c-14", NOW - timedelta(days=14)),
    ]
    usage = {"c-under": _usage(1.0), "c-exact": _usage(2.0), "c-14": _usage(4.0)}

    seven = build_cost_report(ROUTINE, rows, usage, now=NOW)["windows"][0]

    assert seven["current"]["cost_usd"] == 1.0
    assert seven["previous"]["cost_usd"] == 2.0
    assert seven["previous"]["run_count"] == 1


def test_unpriced_run_nulls_only_the_windows_containing_it():
    rows = [
        _row("c-priced", NOW - timedelta(days=1)),
        _row("c-unpriced", NOW - timedelta(days=20), model="mystery-model"),
    ]
    usage = {
        "c-priced": _usage(1.5),
        "c-unpriced": _usage(None, source=None, model="mystery-model"),
    }

    report = build_cost_report(ROUTINE, rows, usage, now=NOW)

    seven, twenty_eight = report["windows"]
    assert seven["current"]["cost_usd"] == 1.5
    assert seven["current"]["cost_source"] == "estimated"
    # The unpriced run sits in the 28d window and the lifetime total: both
    # null (a partial sum would read as the whole figure), run counts kept.
    assert twenty_eight["current"]["cost_usd"] is None
    assert twenty_eight["current"]["cost_source"] is None
    assert twenty_eight["current"]["run_count"] == 2
    assert report["lifetime"]["cost_usd"] is None
    assert report["lifetime"]["run_count"] == 2
    unpriced_row = next(
        r for r in report["recent_runs"] if r["conversation_id"] == "c-unpriced"
    )
    assert unpriced_row["cost_usd"] is None


def test_mixed_sources_degrade_to_mixed():
    rows = [
        _row("c-a", NOW - timedelta(days=1)),
        _row("c-b", NOW - timedelta(days=2)),
    ]
    usage = {
        "c-a": _usage(1.0, source="reported"),
        "c-b": _usage(1.0, source="estimated"),
    }

    seven = build_cost_report(ROUTINE, rows, usage, now=NOW)["windows"][0]

    assert seven["current"]["cost_usd"] == 2.0
    assert seven["current"]["cost_source"] == "mixed"


def test_run_without_calls_costs_a_known_zero_and_falls_back_to_row_model():
    rows = [_row("c-empty", NOW - timedelta(days=1), model="gemini-3.5-flash-lite")]

    report = build_cost_report(ROUTINE, rows, {}, now=NOW)

    (run,) = report["recent_runs"]
    assert run["cost_usd"] == 0.0
    assert run["cost_source"] is None
    assert run["call_count"] == 0
    assert run["total_tokens"] == 0
    assert run["models"] == ["gemini-3.5-flash-lite"]
    assert run["title"] == "Daily digest"
    assert run["started_at"] == _iso_z(NOW - timedelta(days=1))
    # A known zero keeps the window priced (it is not an unpriced run).
    assert report["windows"][0]["current"]["cost_usd"] == 0.0
    assert report["windows"][0]["current"]["run_count"] == 1
    assert report["lifetime"]["cost_usd"] == 0.0


def test_recent_runs_keep_input_order_and_are_capped():
    rows = [
        _row(f"c-{i}", NOW - timedelta(days=i)) for i in range(RECENT_RUNS_LIMIT + 5)
    ]
    usage = {row["id"]: _usage(0.5) for row in rows}

    report = build_cost_report(ROUTINE, rows, usage, now=NOW)

    assert len(report["recent_runs"]) == RECENT_RUNS_LIMIT
    assert [r["conversation_id"] for r in report["recent_runs"]] == [
        f"c-{i}" for i in range(RECENT_RUNS_LIMIT)
    ]
    # The cap only trims the table; the totals still cover every run.
    assert report["lifetime"]["run_count"] == RECENT_RUNS_LIMIT + 5


def test_recent_run_lists_every_model_heaviest_first():
    rows = [_row("c-1", NOW - timedelta(days=1))]
    usage = {
        "c-1": {
            "models": [
                {"model": "claude-opus-4-8", "provider": "anthropic", "call_count": 1,
                 "total_tokens": 900, "estimated_cost_usd": 0.3,
                 "cost_source": "estimated", "metrics": {}},
                {"model": "claude-haiku-4-5", "provider": "anthropic", "call_count": 3,
                 "total_tokens": 100, "estimated_cost_usd": 0.01,
                 "cost_source": "estimated", "metrics": {}},
            ],
            "total": {"call_count": 4, "total_tokens": 1000,
                      "estimated_cost_usd": 0.31, "cost_source": "estimated"},
        }
    }

    (run,) = build_cost_report(ROUTINE, rows, usage, now=NOW)["recent_runs"]

    assert run["models"] == ["claude-opus-4-8", "claude-haiku-4-5"]
    assert run["call_count"] == 4
    assert run["cost_usd"] == 0.31


def test_timestamps_serialize_with_z_suffix_not_offset():
    # parseUTCTimestamp on the FE appends "Z" to anything not ending in one,
    # so a "+00:00" offset would render as "Invalid Date".
    rows = [_row("c-1", NOW - timedelta(days=1))]

    report = build_cost_report(ROUTINE, rows, {"c-1": _usage(1.0)}, now=NOW)

    for value in (report["generated_at"], report["recent_runs"][0]["started_at"]):
        assert value.endswith("Z")
        assert "+" not in value


def test_naive_conversation_timestamps_are_treated_as_utc():
    # conversations.created_at is stored naive; the fold must not mix naive
    # and aware datetimes (TypeError) nor shift the instant.
    rows = [_row("c-naive", (NOW - timedelta(days=1)).replace(tzinfo=None))]

    report = build_cost_report(ROUTINE, rows, {"c-naive": _usage(1.0)}, now=NOW)

    assert report["windows"][0]["current"]["run_count"] == 1
    assert report["recent_runs"][0]["started_at"] == _iso_z(NOW - timedelta(days=1))


# --------------------------------------------------------------------------
# Store integration
# --------------------------------------------------------------------------


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_routine_costs_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    test_session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.conversation_store as conversation_store_mod
    import db.llm_call_store as llm_call_store_mod
    import db.models as models_mod
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod

    models_mod.Base.metadata.create_all(sync_engine)

    for mod in (
        conversation_store_mod, llm_call_store_mod, project_store_mod, routine_store_mod,
    ):
        monkeypatch.setattr(mod, "AsyncSessionLocal", test_session_local)

    yield {
        "models": models_mod,
        "conversations": conversation_store_mod,
        "calls": llm_call_store_mod,
        "projects": project_store_mod,
        "routines": routine_store_mod,
        "session": test_session_local,
    }

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


async def _record_anthropic(calls, models_mod, conversation_id, user_id, output_tokens):
    return await calls.record_api_call(
        conversation_id=conversation_id,
        user_id=user_id,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=100,
        output_tokens=output_tokens,
        duration_ms=10,
        provider="anthropic",
        raw_usage={
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )


def test_get_routine_cost_report_scopes_usage_to_the_routines_runs(_isolated_db):
    env = _isolated_db
    models_mod = env["models"]
    from chat.routine_costs import get_routine_cost_report

    async def _seed():
        user_id = await _create_user(models_mod, env["session"], "alice@example.com")
        project = await env["projects"].create_project(user_id, name="Research")
        daily = await env["routines"].create_routine(user_id, project["id"], "Daily", "run")
        weekly = await env["routines"].create_routine(user_id, project["id"], "Weekly", "run")
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Two runs of Daily (newest first expected), one of Weekly, one
        # standalone conversation -- only the Daily runs may count.
        run_old = str(uuid.uuid4())
        run_new = str(uuid.uuid4())
        other = str(uuid.uuid4())
        standalone = str(uuid.uuid4())
        await env["conversations"].create_conversation(
            user_id, run_old, now - timedelta(days=3), project_id=project["id"],
            routine_id=daily["id"], custom_name="Daily",
        )
        await env["conversations"].create_conversation(
            user_id, run_new, now - timedelta(hours=2), project_id=project["id"],
            routine_id=daily["id"], custom_name="Daily",
        )
        await env["conversations"].create_conversation(
            user_id, other, now - timedelta(hours=1), project_id=project["id"],
            routine_id=weekly["id"], custom_name="Weekly",
        )
        await env["conversations"].create_conversation(
            user_id, standalone, now - timedelta(hours=1),
        )
        await _record_anthropic(env["calls"], models_mod, run_old, user_id, 1000)
        await _record_anthropic(env["calls"], models_mod, run_old, user_id, 1000)
        await _record_anthropic(env["calls"], models_mod, run_new, user_id, 3000)
        await _record_anthropic(env["calls"], models_mod, other, user_id, 50000)
        await _record_anthropic(env["calls"], models_mod, standalone, user_id, 50000)
        return daily, run_old, run_new

    daily, run_old, run_new = _run(_seed())

    report = _run(get_routine_cost_report(daily))

    assert [r["conversation_id"] for r in report["recent_runs"]] == [run_new, run_old]
    new_row, old_row = report["recent_runs"]
    assert new_row["call_count"] == 1
    assert old_row["call_count"] == 2
    assert old_row["total_tokens"] == 2 * (100 + 1000)
    assert old_row["title"] == "Daily"
    assert old_row["models"] == ["claude-opus-4-8"]
    # Opus 4.8 is priced: every figure is a known estimate.
    assert new_row["cost_source"] == "estimated"
    assert new_row["cost_usd"] > 0
    seven = report["windows"][0]
    assert seven["current"]["run_count"] == 2
    assert seven["current"]["cost_usd"] == pytest.approx(
        round(new_row["cost_usd"] + old_row["cost_usd"], 6)
    )
    assert seven["previous"]["run_count"] == 0
    assert seven["previous"]["cost_usd"] == 0.0
    assert report["lifetime"]["run_count"] == 2
    assert report["lifetime"]["call_count"] == 3
    assert report["lifetime"]["cost_usd"] == seven["current"]["cost_usd"]


def test_get_routine_cost_report_for_a_never_run_routine_is_empty(_isolated_db):
    env = _isolated_db
    from chat.routine_costs import get_routine_cost_report

    async def _seed():
        user_id = await _create_user(env["models"], env["session"], "alice@example.com")
        project = await env["projects"].create_project(user_id, name="Research")
        return await env["routines"].create_routine(user_id, project["id"], "Daily", "run")

    routine = _run(_seed())

    report = _run(get_routine_cost_report(routine))

    assert report["recent_runs"] == []
    assert report["lifetime"] == {
        "run_count": 0, "call_count": 0, "total_tokens": 0,
        "cost_usd": 0.0, "cost_source": None,
    }
    assert report["routine_created_at"] == routine["created_at"]


# --------------------------------------------------------------------------
# Route ownership checks
# --------------------------------------------------------------------------


def _client(monkeypatch, user_id, routine, project_id, report):
    import chat.routine_routes as routes_mod
    from chat.auth import get_current_user_cookie_or_apikey_checked

    async def fake_get_project(uid, pid):
        return {"id": pid} if uid == user_id and pid == project_id else None

    async def fake_get_routine(uid, rid):
        return routine if uid == user_id and rid == routine["id"] else None

    async def fake_report(r):
        assert r is routine
        return report

    monkeypatch.setattr("db.project_store.get_project", fake_get_project)
    monkeypatch.setattr(routes_mod, "get_routine", fake_get_routine)
    monkeypatch.setattr(routes_mod, "get_routine_cost_report", fake_report)

    app = FastAPI()
    app.include_router(routes_mod.router)
    app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = (
        lambda: {"id": user_id, "email": "alice@example.com"}
    )
    return TestClient(app)


def test_costs_route_returns_report_for_owned_routine(monkeypatch):
    routine = {"id": "r-1", "project_id": "p-1", "created_at": "2026-01-01T00:00:00"}
    report = {"routine_id": "r-1", "windows": [], "lifetime": {}, "recent_runs": []}
    client = _client(monkeypatch, 7, routine, "p-1", report)

    resp = client.get("/app/api/projects/p-1/routines/r-1/costs")

    assert resp.status_code == 200
    assert resp.json() == report


def test_costs_route_404s_on_foreign_project_or_mismatched_routine(monkeypatch):
    routine = {"id": "r-1", "project_id": "p-1", "created_at": None}
    client = _client(monkeypatch, 7, routine, "p-1", {})

    assert client.get("/app/api/projects/p-other/routines/r-1/costs").status_code == 404
    assert client.get("/app/api/projects/p-1/routines/r-other/costs").status_code == 404

    # Routine exists but belongs to another project of the same user.
    routine_elsewhere = {"id": "r-2", "project_id": "p-2", "created_at": None}
    client = _client(monkeypatch, 7, routine_elsewhere, "p-1", {})
    assert client.get("/app/api/projects/p-1/routines/r-2/costs").status_code == 404
