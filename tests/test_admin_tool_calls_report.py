"""The System Reports "Tool Calls" section: the ledger query and the pure
fold in analytics/reports.py, and the endpoint's gating and wiring."""

import asyncio
from datetime import datetime, timezone

import pytest

from analytics import store as store_mod
from analytics.ledger import Ledger
from analytics.reports import build_tool_calls_report, query_backfill, query_tool_calls
from analytics.store import AnalyticsStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def mem_store(monkeypatch):
    store = AnalyticsStore(":memory:").open()
    monkeypatch.setattr(store_mod, "_current", store)
    yield store
    store.close()


def _call(ledger, conv, user, tool, at, call_type="top_level", dynamic=False, **kw):
    ledger.record_tool_call(
        user_id=user, tool_name=tool, dynamic=dynamic, call_type=call_type,
        conversation_id=conv, created_at=at, **kw,
    )


def _seed(store):
    ledger = Ledger(store)
    t = lambda d, h=12: datetime(2026, 10, d, h)  # noqa: E731
    _call(ledger, "c1", 1, "get_gmail_messages", t(1), dynamic=True)
    _call(ledger, "c1", 1, "get_gmail_messages", t(2), dynamic=True)
    _call(ledger, "c1", 1, "agent_task", t(2, 13))
    _call(ledger, "c1", 1, "get_workspace_file", t(2, 13), call_type="sub_agent",
          agent_name="Reader", sub_agent_level=1, parent_tool_id="t1")
    _call(ledger, "c2", 2, "get_gmail_messages", t(5), dynamic=True, routine_id="r1")
    _call(ledger, None, 2, "list_quest_docs", t(6), call_type="script", dynamic=True)
    _call(ledger, "c4", 3, "memory_search", t(12), source="backfill")
    ledger.flush()
    return ledger


# ---------------------------------------------------------------------------
# query_tool_calls
# ---------------------------------------------------------------------------

def test_query_groups_per_tool_conversation_user_and_clips_window(mem_store):
    _seed(mem_store)
    with mem_store.cursor() as cur:
        rows = query_tool_calls(cur)
        windowed = query_tool_calls(
            cur,
            datetime(2026, 10, 2, tzinfo=timezone.utc),
            datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        assert query_backfill(cur, "tool_calls_transcripts") is None
        cur.execute(
            "INSERT INTO _backfills VALUES ('tool_calls_transcripts', ?, 5)",
            [datetime(2026, 10, 1)],
        )
        backfill = query_backfill(cur, "tool_calls_transcripts")

    by_key = {(r["tool_name"], r["conversation_id"]): r for r in rows}
    assert set(by_key) == {
        ("get_gmail_messages", "c1"), ("agent_task", "c1"), ("get_workspace_file", "c1"),
        ("get_gmail_messages", "c2"), ("list_quest_docs", None), ("memory_search", "c4"),
    }
    gmail_c1 = by_key[("get_gmail_messages", "c1")]
    assert (gmail_c1["call_count"], gmail_c1["dynamic_count"], gmail_c1["sub_agent_count"]) == (2, 2, 0)
    assert gmail_c1["user_id"] == 1 and gmail_c1["routine_id"] is None
    assert gmail_c1["last_called_at"] == datetime(2026, 10, 2, 12)
    assert by_key[("get_workspace_file", "c1")]["sub_agent_count"] == 1
    assert by_key[("get_gmail_messages", "c2")]["routine_id"] == "r1"
    assert by_key[("list_quest_docs", None)]["script_count"] == 1
    assert by_key[("memory_search", "c4")]["backfill_count"] == 1

    # Oct 2 .. Oct 5 inclusive (half-open at Oct 6 midnight UTC).
    w = {(r["tool_name"], r["conversation_id"]): r for r in windowed}
    assert set(w) == {
        ("get_gmail_messages", "c1"), ("agent_task", "c1"), ("get_workspace_file", "c1"),
        ("get_gmail_messages", "c2"),
    }
    assert w[("get_gmail_messages", "c1")]["call_count"] == 1
    assert backfill == {"completed_at": "2026-10-01T00:00:00", "row_count": 5}


# ---------------------------------------------------------------------------
# build_tool_calls_report
# ---------------------------------------------------------------------------

def _usage(tool, conv, user, calls=1, routine_id=None, last=datetime(2026, 10, 1), **split):
    row = {
        "tool_name": tool, "conversation_id": conv, "user_id": user,
        "call_count": calls, "sub_agent_count": 0, "script_count": 0,
        "dynamic_count": 0, "backfill_count": 0, "routine_id": routine_id,
        "last_called_at": last,
    }
    row.update(split)
    return row


def test_build_report_folds_splits_and_attributes_routines():
    usage = [
        _usage("get_gmail_messages", "c1", 1, calls=2, dynamic_count=2,
               last=datetime(2026, 10, 2, 12)),
        _usage("get_gmail_messages", "c2", 2, calls=1, dynamic_count=1, routine_id="r1",
               last=datetime(2026, 10, 5)),
        _usage("get_gmail_messages", "c5", 1, calls=3, dynamic_count=3,
               last=datetime(2026, 10, 1)),
        _usage("agent_task", "c1", 1, last=datetime(2026, 10, 2, 13)),
        _usage("get_workspace_file", "c1", 1, calls=4, sub_agent_count=4,
               last=datetime(2026, 10, 2, 13)),
        _usage("list_quest_docs", None, 2, script_count=1, dynamic_count=1,
               last=datetime(2026, 10, 6)),
        _usage("memory_search", "c4", 3, backfill_count=1, last=datetime(2026, 10, 12)),
    ]
    report = build_tool_calls_report(
        usage, routine_conversations={"c5"},
        backfill={"completed_at": "2026-10-01T00:00:00", "row_count": 5},
    )
    assert report["call_count"] == 13
    assert report["conversation_count"] == 4   # the script row has no conversation
    assert report["backfill"] == {"completed_at": "2026-10-01T00:00:00", "row_count": 5}

    rows = {r["tool"]: r for r in report["tools"]}
    gmail = rows["get_gmail_messages"]
    assert gmail["call_count"] == 6 and gmail["dynamic_count"] == 6
    # c2 via the row's routine_id (1 call), c5 via the conversations table (3).
    assert gmail["routine_call_count"] == 4
    assert gmail["conversation_count"] == 3 and gmail["user_count"] == 2
    assert gmail["last_called_at"] == "2026-10-05T00:00:00"
    assert rows["get_workspace_file"]["sub_agent_call_count"] == 4
    script = rows["list_quest_docs"]
    assert (script["script_call_count"], script["conversation_count"], script["user_count"]) == (1, 0, 1)
    assert rows["memory_search"]["backfill_count"] == 1

    # Sorted by calls, then conversations, then name.
    assert [r["tool"] for r in report["tools"]][:2] == ["get_gmail_messages", "get_workspace_file"]
    one_call = [r["tool"] for r in report["tools"] if r["call_count"] == 1]
    assert one_call == sorted(one_call, key=lambda n: (-rows[n]["conversation_count"], n))


def test_build_report_empty():
    report = build_tool_calls_report([], set(), None)
    assert report == {"tools": [], "call_count": 0, "conversation_count": 0, "backfill": None}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_endpoint_rejects_non_admin_bad_ranges_and_missing_store(monkeypatch, isolated_db):
    from fastapi import HTTPException
    import chat.routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: False)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_tool_calls_report(user={"email": "nobody@example.com"}))
    assert exc.value.status_code == 403

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_tool_calls_report(
            start="2026-10-09", end="2026-10-01", user={"email": "admin@example.com"},
        ))
    assert exc.value.status_code == 400

    monkeypatch.setattr(store_mod, "_current", None)
    with pytest.raises(HTTPException) as exc:
        _run(admin_mod.admin_tool_calls_report(user={"email": "admin@example.com"}))
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "analytics_unavailable"


def test_endpoint_attributes_routine_conversations_from_quest_db(isolated_db, mem_store, monkeypatch):
    import chat.routes.admin as admin_mod
    from db.conversation_store import create_conversation
    from db.models import User
    from db.project_store import create_project
    from db.routine_store import create_routine

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as db:
            u = User(email="a@example.com", api_key="k", name="Ada")
            db.add(u)
            await db.commit()
            await db.refresh(u)
        project = await create_project(u.id, name="P")
        routine = await create_routine(u.id, project["id"], name="R", prompt="go")
        await create_conversation(u.id, "c1", datetime(2026, 10, 1, tzinfo=timezone.utc))
        await create_conversation(
            u.id, "c2", datetime(2026, 10, 2, tzinfo=timezone.utc),
            project_id=project["id"], routine_id=routine["id"],
        )
        return u.id

    uid = _run(_seed())
    ledger = Ledger(mem_store)
    _call(ledger, "c1", uid, "get_gmail_messages", datetime(2026, 10, 1, 9), dynamic=True)
    _call(ledger, "c2", uid, "get_gmail_messages", datetime(2026, 10, 2, 9), dynamic=True)  # routine conv, per quest.db
    _call(ledger, "c9", uid, "memory_search", datetime(2026, 10, 20, 9))   # outside the range
    ledger.flush()

    resp = _run(admin_mod.admin_tool_calls_report(
        start="2026-10-01", end="2026-10-10", user={"email": "admin@example.com"},
    ))
    assert resp["conversation_count"] == 2
    assert resp["call_count"] == 2
    assert resp["backfill"] is None
    assert [r["tool"] for r in resp["tools"]] == ["get_gmail_messages"]
    row = resp["tools"][0]
    assert (row["call_count"], row["routine_call_count"], row["dynamic_count"]) == (2, 1, 2)
    assert (row["conversation_count"], row["user_count"]) == (2, 1)
