"""The System Reports "Skills" section: the ledger query and the pure fold
in analytics/reports.py, and the endpoint's gating and wiring."""

import asyncio
from datetime import datetime, timezone

import pytest

from analytics import ledger as ledger_mod
from analytics import store as store_mod
from analytics.ledger import Ledger
from analytics.reports import build_skills_report, query_backfill, query_skill_usage
from analytics.store import AnalyticsStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def mem_store(monkeypatch):
    store = AnalyticsStore(":memory:").open()
    monkeypatch.setattr(store_mod, "_current", store)
    yield store
    store.close()


def _load(ledger, conv, user, skills, source, at, **kw):
    ledger.record_skill_load(
        conversation_id=conv, user_id=user, skill_ids=skills, source=source,
        created_at=at, **kw,
    )


def _seed(store):
    ledger = Ledger(store)
    t = lambda d, h=12: datetime(2026, 10, d, h)  # noqa: E731
    _load(ledger, "c1", 1, ["s-a", "s-b"], "autoload_user", t(1))
    _load(ledger, "c1", 1, ["s-a", "s-b"], "autoload_user", t(2))      # second turn
    _load(ledger, "c1", 1, ["s-a"], "tool_get_skill", t(2, 13))
    _load(ledger, "c2", 2, ["s-a"], "composer", t(5), routine_id="r1")
    _load(ledger, "c3", 2, ["system:gmail"], "tool_load_skills", t(9))
    _load(ledger, "c4", 3, ["s-gone"], "backfill", t(12))
    ledger.flush()
    return ledger


# ---------------------------------------------------------------------------
# query_skill_usage
# ---------------------------------------------------------------------------

def test_query_groups_per_skill_conversation_user_and_clips_window(mem_store):
    _seed(mem_store)
    with mem_store.cursor() as cur:
        rows = query_skill_usage(cur)
        windowed = query_skill_usage(
            cur,
            datetime(2026, 10, 2, tzinfo=timezone.utc),
            datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        assert query_backfill(cur, "skill_reads") is None
        cur.execute("INSERT INTO _backfills VALUES ('skill_reads', ?, 5)", [datetime(2026, 10, 1)])
        backfill = query_backfill(cur, "skill_reads")

    by_key = {(r["skill_id"], r["conversation_id"]): r for r in rows}
    assert set(by_key) == {
        ("s-a", "c1"), ("s-b", "c1"), ("s-a", "c2"), ("system:gmail", "c3"), ("s-gone", "c4"),
    }
    a_c1 = by_key[("s-a", "c1")]
    assert (a_c1["load_count"], a_c1["autoload_count"], a_c1["model_load_count"]) == (3, 2, 1)
    assert a_c1["composer_count"] == 0 and a_c1["backfill_count"] == 0
    assert a_c1["user_id"] == 1 and a_c1["routine_id"] is None
    assert a_c1["last_used_at"] == datetime(2026, 10, 2, 13)
    assert by_key[("s-a", "c2")]["routine_id"] == "r1"
    assert by_key[("s-gone", "c4")]["backfill_count"] == 1

    # Oct 2 .. Oct 5 inclusive (half-open at Oct 6 midnight UTC).
    w = {(r["skill_id"], r["conversation_id"]): r for r in windowed}
    assert set(w) == {("s-a", "c1"), ("s-b", "c1"), ("s-a", "c2")}
    assert w[("s-a", "c1")]["load_count"] == 2
    assert w[("s-b", "c1")]["load_count"] == 1
    assert backfill == {"completed_at": "2026-10-01T00:00:00", "row_count": 5}


# ---------------------------------------------------------------------------
# build_skills_report
# ---------------------------------------------------------------------------

def _usage(skill, conv, user, loads=1, routine_id=None, last=datetime(2026, 10, 1), **split):
    row = {
        "skill_id": skill, "conversation_id": conv, "user_id": user,
        "load_count": loads, "autoload_count": 0, "composer_count": 0,
        "model_load_count": 0, "backfill_count": 0, "routine_id": routine_id,
        "last_used_at": last,
    }
    row.update(split)
    return row


def test_build_report_labels_kinds_and_attributes_routines():
    usage = [
        _usage("s-a", "c1", 1, loads=3, autoload_count=2, model_load_count=1,
               last=datetime(2026, 10, 2, 13)),
        _usage("s-a", "c2", 2, loads=1, composer_count=1, routine_id="r1",
               last=datetime(2026, 10, 5)),
        _usage("s-a", "c5", 1, loads=1, composer_count=1, last=datetime(2026, 10, 1)),
        _usage("s-b", "c1", 1, loads=2, autoload_count=2, last=datetime(2026, 10, 2)),
        _usage("system:gmail", "c3", 2, model_load_count=1, last=datetime(2026, 10, 9)),
        _usage("system:unknown", "c3", 2, model_load_count=1),
        _usage("s-gone", "c4", 3, backfill_count=1, last=datetime(2026, 10, 12)),
        _usage("s-proj", "c6", 4),
        _usage("s-weird", "c7", 4),
    ]
    labels = {
        "s-a": {"name": "Alpha", "visibility": "shared", "project_id": None,
                "creator_id": 1, "creator_email": "a@example.com", "creator_name": "A"},
        "s-b": {"name": "Beta", "visibility": "private", "project_id": None,
                "creator_id": 1, "creator_email": "a@example.com", "creator_name": None},
        "s-proj": {"name": "Proj", "visibility": "project", "project_id": "p1",
                   "creator_id": None, "creator_email": None, "creator_name": None},
        "s-weird": {"name": "Weird", "visibility": "legacy", "project_id": None,
                    "creator_id": 9, "creator_email": "z@example.com", "creator_name": "Z"},
    }
    report = build_skills_report(
        usage, labels, {"system:gmail": "Web fetch"},
        routine_conversations={"c5"},   # from quest.db
        backfill={"completed_at": "2026-10-01T00:00:00", "row_count": 5},
    )
    assert report["conversation_count"] == 7
    assert report["load_count"] == 12
    assert report["backfill"] == {"completed_at": "2026-10-01T00:00:00", "row_count": 5}

    rows = {r["skill_id"]: r for r in report["skills"]}
    a = rows["s-a"]
    assert (a["name"], a["kind"], a["owner_email"], a["owner_name"]) == (
        "Alpha", "shared", "a@example.com", "A")
    assert a["conversation_count"] == 3
    # c2 via the row's routine_id, c5 via the conversations table.
    assert a["routine_conversation_count"] == 2
    assert a["user_count"] == 2
    assert (a["load_count"], a["autoload_count"], a["composer_count"],
            a["model_load_count"], a["backfill_count"]) == (5, 2, 2, 1, 0)
    assert a["last_used_at"] == "2026-10-05T00:00:00"

    assert rows["system:gmail"]["kind"] == "system"
    assert rows["system:gmail"]["name"] == "Web fetch"
    assert rows["system:gmail"]["owner_email"] is None
    # A system id missing from the catalog keeps its id as the name.
    assert rows["system:unknown"] == {**rows["system:unknown"], "kind": "system", "name": "system:unknown"}
    assert rows["s-gone"]["kind"] == "deleted" and rows["s-gone"]["name"] == "s-gone"
    assert rows["s-gone"]["backfill_count"] == 1
    assert rows["s-proj"]["kind"] == "project" and rows["s-proj"]["project_id"] == "p1"
    # An unexpected visibility value degrades to private rather than leaking.
    assert rows["s-weird"]["kind"] == "private"

    # Sorted by conversations, then loads, then name.
    order = [r["skill_id"] for r in report["skills"]]
    assert order[0] == "s-a"
    assert order.index("s-b") < order.index("s-gone")  # 2 loads beats 1 at one conversation


def test_build_report_empty():
    report = build_skills_report([], {}, {}, set(), None)
    assert report == {"skills": [], "conversation_count": 0, "load_count": 0, "backfill": None}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def _admin(monkeypatch):
    import chat.routes.admin as admin_mod
    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    return admin_mod


def test_endpoint_rejects_non_admin_bad_ranges_and_missing_store(monkeypatch, isolated_db):
    from fastapi import HTTPException
    import chat.routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: False)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_skills_report(user={"email": "nobody@example.com"}))
    assert exc.value.status_code == 403

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_skills_report(
            start="2026-10-09", end="2026-10-01", user={"email": "admin@example.com"},
        ))
    assert exc.value.status_code == 400

    monkeypatch.setattr(store_mod, "_current", None)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_skills_report(user={"email": "admin@example.com"}))
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "analytics_unavailable"


def test_endpoint_joins_labels_catalog_and_routine_conversations(isolated_db, mem_store, _admin):
    from db.conversation_store import create_conversation, list_conversation_activity_rows
    from db.models import User
    from db.project_store import create_project
    from db.routine_store import create_routine
    from db.skill_store import create_skill

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as db:
            u = User(email="a@example.com", api_key="k", name="Ada")
            db.add(u)
            await db.commit()
            await db.refresh(u)
        skill = await create_skill(u.id, name="Alpha", content="body", visibility="public")
        project = await create_project(u.id, name="P")
        routine = await create_routine(u.id, project["id"], name="R", prompt="go")
        await create_conversation(u.id, "c1", datetime(2026, 10, 1, tzinfo=timezone.utc))
        await create_conversation(
            u.id, "c2", datetime(2026, 10, 2, tzinfo=timezone.utc),
            project_id=project["id"], routine_id=routine["id"],
        )
        return u.id, skill["id"], project["id"]

    uid, skill_id, project_id = _run(_seed())
    ledger = Ledger(mem_store)
    _load(ledger, "c1", uid, [skill_id, "system:gmail"], "autoload_user", datetime(2026, 10, 1, 9))
    _load(ledger, "c2", uid, [skill_id], "composer", datetime(2026, 10, 2, 9))  # routine conv, per quest.db
    _load(ledger, "c9", uid, ["gone"], "tool_get_skill", datetime(2026, 10, 20, 9))
    ledger.flush()

    resp = _run(_admin.admin_skills_report(
        start="2026-10-01", end="2026-10-10", user={"email": "admin@example.com"},
    ))
    assert resp["conversation_count"] == 2
    assert resp["load_count"] == 3
    assert resp["backfill"] is None
    rows = {r["skill_id"]: r for r in resp["skills"]}
    assert set(rows) == {skill_id, "system:gmail"}
    alpha = rows[skill_id]
    assert (alpha["name"], alpha["kind"], alpha["owner_name"]) == ("Alpha", "public", "Ada")
    assert alpha["conversation_count"] == 2
    assert alpha["routine_conversation_count"] == 1
    assert (alpha["autoload_count"], alpha["composer_count"]) == (1, 1)
    assert rows["system:gmail"]["kind"] == "system"
    assert rows["system:gmail"]["name"] != "system:gmail"  # resolved from the catalog

    # The activity projection carries project_id for the backfill.
    activity = {c["id"]: c for c in _run(list_conversation_activity_rows())}
    assert activity["c2"]["project_id"] == project_id
    assert activity["c1"]["project_id"] is None
