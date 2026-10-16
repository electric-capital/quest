"""Tests for the admin System Reports "Docs", "Tables" and "Memories" sections.

1. The pure folds in chat/feature_usage_report.py (``build_docs_report``,
   ``build_tables_report``, ``build_memories_report``): ownership splits,
   range clipping, model-written detection, sort order, unknown owners.
2. The narrow all-rows store projections behind them (``list_all_doc_rows``,
   ``list_all_table_rows``, ``list_all_memory_rows`` and the suggestion /
   project-memory twins), against a throwaway sqlite file.
3. The three endpoints' admin gating, range parsing and wiring.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.feature_usage_report import (
    build_docs_report,
    build_memories_report,
    build_tables_report,
)


def _run(coro):
    return asyncio.run(coro)


USERS = [
    {"id": 1, "email": "alice@example.com", "name": "Alice"},
    {"id": 2, "email": "bob@example.com", "name": None},
    {"id": 3, "email": "carol@example.com", "name": "Carol"},
]

D1 = datetime(2026, 10, 1, 12, 0)
D5 = datetime(2026, 10, 5, 12, 0)
D9 = datetime(2026, 10, 9, 23, 59)
D12 = datetime(2026, 10, 12, 0, 0)


def _doc(owner, *, project=None, mode="private", size=100, assets=0, source=None,
         shares=0, created=D1, updated=D1):
    return {
        "id": uuid.uuid4().hex, "owner_id": owner, "project_id": project,
        "mode": mode, "content_size": size, "asset_count": assets,
        "last_write_source": source, "share_count": shares,
        "created_at": created, "updated_at": updated,
    }


def _table(owner, *, project=None, mode="private", rows=0, columns=1, source=None,
           shares=0, created=D1, updated=D1):
    return {
        "id": uuid.uuid4().hex, "owner_id": owner, "project_id": project,
        "mode": mode, "row_count": rows, "column_count": columns,
        "last_write_source": source, "share_count": shares,
        "created_at": created, "updated_at": updated,
    }


# ---------------------------------------------------------------------------
# build_docs_report / build_tables_report
# ---------------------------------------------------------------------------

def test_docs_report_folds_ownership_and_clips_range():
    rows = [
        _doc(1, size=100, assets=2, source="conversation:c1", shares=2,
             created=D1, updated=D9),
        _doc(1, project="p1", mode="public", size=50, source="ui",
             created=D5, updated=D5),
        _doc(1, project="p1", mode="public", size=10,
             source="action_request:7", created=D12, updated=D12),
        _doc(2, size=1, source="ui", created=D1, updated=D1),
    ]
    report = build_docs_report(rows, USERS, date(2026, 10, 5), date(2026, 10, 9))

    assert report["total_users"] == 3
    assert [r["user_id"] for r in report["users"]] == [1, 2]
    alice, bob = report["users"]
    assert alice["user_email"] == "alice@example.com"
    assert alice["user_name"] == "Alice"
    assert alice["doc_count"] == 3
    assert alice["user_doc_count"] == 1
    assert alice["project_doc_count"] == 2
    assert alice["public_doc_count"] == 2
    assert alice["shared_doc_count"] == 1
    assert alice["model_written_count"] == 2
    assert alice["content_bytes"] == 160
    assert alice["asset_count"] == 2
    # Range Oct 5..9: the project doc created on the 5th, the user doc
    # updated on the 9th; the doc created on the 12th is outside.
    assert alice["created_in_range"] == 1
    assert alice["updated_in_range"] == 2
    assert alice["last_updated_at"] == D12.isoformat()

    assert bob["user_name"] == ""
    assert bob["doc_count"] == 1
    assert bob["created_in_range"] == 0
    assert bob["updated_in_range"] == 0
    assert bob["model_written_count"] == 0


def test_docs_report_open_range_counts_everything_and_sorts_by_activity():
    rows = [
        _doc(2, created=D1, updated=D1),
        _doc(2, created=D5, updated=D5),
        _doc(1, created=D1, updated=D12),
        _doc(1, created=D1, updated=D12),
        _doc(1, created=D1, updated=D12),
    ]
    report = build_docs_report(rows, USERS)
    assert [(r["user_id"], r["updated_in_range"]) for r in report["users"]] == [
        (1, 3), (2, 2),
    ]

    # Same in-range activity: more docs owned first, then email.
    tied = build_docs_report(
        [_doc(2), _doc(1), _doc(1), _doc(3), _doc(3)], USERS,
        date(2027, 1, 1), date(2027, 1, 2),
    )
    assert [r["user_id"] for r in tied["users"]] == [1, 3, 2]


def test_docs_report_unknown_owner_gets_placeholder():
    report = build_docs_report([_doc(99)], USERS)
    row = report["users"][0]
    assert row["user_id"] == 99
    assert row["user_email"] == ""
    assert row["user_name"] == "(unknown user)"


def test_tables_report_sums_rows_and_columns():
    rows = [
        _table(1, rows=10, columns=3, source="conversation:c", created=D1, updated=D9),
        _table(1, project="p", mode="public", rows=5, columns=2,
               source="revert:4", shares=1, created=D9, updated=D9),
        _table(3, rows=0, columns=1, source="ui:3", created=D12, updated=D12),
    ]
    report = build_tables_report(rows, USERS, date(2026, 10, 9), None)
    alice, carol = report["users"]
    assert alice["user_id"] == 1
    assert alice["table_count"] == 2
    assert alice["user_table_count"] == 1
    assert alice["project_table_count"] == 1
    assert alice["public_table_count"] == 1
    assert alice["shared_table_count"] == 1
    # A History restore is a human write, not a model write.
    assert alice["model_written_count"] == 1
    assert alice["row_total"] == 15
    assert alice["column_total"] == 5
    assert alice["created_in_range"] == 1
    assert alice["updated_in_range"] == 2
    assert carol["table_count"] == 1
    assert carol["updated_in_range"] == 1
    assert set(alice) == {
        "user_id", "user_email", "user_name", "table_count", "user_table_count",
        "project_table_count", "public_table_count", "shared_table_count",
        "model_written_count", "row_total", "column_total", "created_in_range",
        "updated_in_range", "last_updated_at",
    }


# ---------------------------------------------------------------------------
# build_memories_report
# ---------------------------------------------------------------------------

def _memory(user, *, archived=False, length=10, created=D1, updated=None):
    return {"user_id": user, "archived": archived, "content_length": length,
            "created_at": created, "updated_at": updated}


def _suggestion(user, status, *, created=D1, resolved=None, project=None):
    row = {"user_id": user, "status": status, "created_at": created,
           "resolved_at": resolved}
    if project is not None:
        row["project_id"] = project
    return row


def _project_memory(user, project, *, archived=False, length=10, created=D1, updated=None):
    return {"user_id": user, "project_id": project, "archived": archived,
            "content_length": length, "created_at": created, "updated_at": updated}


def test_memories_report_counts_memories_and_suggestion_funnel():
    memories = [
        _memory(1, length=100, created=D1),
        _memory(1, length=20, created=D5),
        _memory(1, archived=True, length=999, created=D1, updated=D9),
        _memory(2, created=D12),
    ]
    suggestions = [
        _suggestion(1, "accepted", created=D5, resolved=D5),
        _suggestion(1, "accepted", created=D5, resolved=D9),
        _suggestion(1, "dismissed", created=D9, resolved=D9),
        _suggestion(1, "pending", created=D9),
        _suggestion(1, "accepted", created=D12, resolved=D12),  # outside
        _suggestion(2, "pending", created=D1),  # outside
    ]
    project_memories = [
        _project_memory(1, "p1", created=D5),
        _project_memory(1, "p1", created=D1),
        _project_memory(1, "p2", archived=True, created=D1),
        _project_memory(3, "p3", created=D12),
    ]
    project_suggestions = [
        _suggestion(1, "dismissed", created=D5, resolved=D5, project="p1"),
        _suggestion(3, "pending", created=D5, project="p3"),
    ]
    report = build_memories_report(
        memories, suggestions, project_memories, project_suggestions, USERS,
        date(2026, 10, 5), date(2026, 10, 9),
    )
    assert report["total_users"] == 3
    by_id = {r["user_id"]: r for r in report["users"]}
    assert list(by_id) == [1, 3, 2]  # by in-range activity, then holdings

    alice = by_id[1]
    assert alice["memory_count"] == 2
    assert alice["archived_count"] == 1
    assert alice["content_chars"] == 120  # active memories only
    assert alice["created_in_range"] == 1
    assert alice["suggested_in_range"] == 4
    assert alice["accepted_in_range"] == 2
    assert alice["dismissed_in_range"] == 1
    assert alice["pending_in_range"] == 1
    assert alice["project_memory_count"] == 2
    assert alice["project_archived_count"] == 1
    assert alice["project_count"] == 2
    assert alice["project_created_in_range"] == 1
    assert alice["project_suggested_in_range"] == 1
    assert alice["project_dismissed_in_range"] == 1
    assert alice["project_accepted_in_range"] == 0
    assert alice["project_pending_in_range"] == 0
    assert alice["last_activity_at"] == D12.isoformat()

    bob = by_id[2]
    assert bob["memory_count"] == 1
    assert bob["created_in_range"] == 0
    assert bob["suggested_in_range"] == 0
    assert bob["pending_in_range"] == 0
    assert bob["last_activity_at"] == D12.isoformat()

    carol = by_id[3]
    assert carol["memory_count"] == 0
    assert carol["project_memory_count"] == 1
    assert carol["project_suggested_in_range"] == 1
    assert carol["project_pending_in_range"] == 1


def test_memories_report_skips_users_without_any_rows():
    report = build_memories_report([], [], [], [], USERS)
    assert report == {"users": [], "total_users": 3}


# ---------------------------------------------------------------------------
# Store projections (isolated sqlite)
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(monkeypatch):
    """Swap every involved store onto a fresh sqlite file (same no-reload
    pattern as tests/test_admin_guides_report.py)."""
    tmpdir = tempfile.mkdtemp(prefix="quest_feature_usage_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    test_session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.conversation_store as conversation_store
    import db.doc_store as doc_store
    import db.memory_store as memory_store
    import db.memory_suggestion_store as suggestion_store
    import db.project_memory_store as project_memory_store
    import db.project_memory_suggestion_store as project_suggestion_store
    import db.project_store as project_store
    import db.table_store as table_store
    import db.user_store as user_store

    models_mod.Base.metadata.create_all(sync_engine)
    mods = {
        "models": models_mod,
        "conversation_store": conversation_store,
        "doc_store": doc_store,
        "memory_store": memory_store,
        "suggestion_store": suggestion_store,
        "project_memory_store": project_memory_store,
        "project_suggestion_store": project_suggestion_store,
        "project_store": project_store,
        "table_store": table_store,
        "user_store": user_store,
        "session": test_session_local,
    }
    for name, mod in mods.items():
        if name not in ("models", "session"):
            monkeypatch.setattr(mod, "AsyncSessionLocal", test_session_local)

    yield mods

    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _create_user(mods, email, name=None):
    async with mods["session"]() as db:
        u = mods["models"].User(email=email, api_key=f"k-{uuid.uuid4().hex}", name=name)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


async def _create_conversation(mods, user_id):
    conv_id = str(uuid.uuid4())
    await mods["conversation_store"].create_conversation(
        user_id, conv_id, datetime.now(timezone.utc),
    )
    return conv_id


def test_list_all_doc_rows_projects_metadata_and_share_counts(_isolated_db):
    mods = _isolated_db
    doc_store = mods["doc_store"]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        bob = await _create_user(mods, "bob@example.com")
        project = await mods["project_store"].create_project(alice, name="Pub", public=True)
        d1 = await doc_store.create_doc(
            alice, "Notes", mode="private", content_size=120,
            last_write_source="conversation:c1",
        )
        d2 = await doc_store.create_doc(
            alice, "Plan", mode="public", project_id=project["id"], content_size=30,
        )
        await doc_store.update_after_write(
            d1["id"], content_size=150, asset_count=2, last_write_source="ui",
        )
        await doc_store.add_share(d1["id"], bob, "read")
        await doc_store.add_share(d1["id"], None, "read")
        await doc_store.add_share(d1["id"], bob, "write")  # upsert, still 2 rows
        return alice, project["id"], d1["id"], d2["id"]

    alice, project_id, d1, d2 = _run(_seed())
    rows = {r["id"]: r for r in _run(doc_store.list_all_doc_rows())}
    assert set(rows) == {d1, d2}
    assert rows[d1]["owner_id"] == alice
    assert rows[d1]["project_id"] is None
    assert rows[d1]["mode"] == "private"
    assert rows[d1]["content_size"] == 150
    assert rows[d1]["asset_count"] == 2
    assert rows[d1]["last_write_source"] == "ui"
    assert rows[d1]["share_count"] == 2
    assert isinstance(rows[d1]["created_at"], datetime)
    assert rows[d1]["updated_at"] >= rows[d1]["created_at"]
    assert rows[d2]["project_id"] == project_id
    assert rows[d2]["mode"] == "public"
    assert rows[d2]["share_count"] == 0
    assert "title" not in rows[d1]
    assert "description" not in rows[d1]


def test_list_all_table_rows_projects_metadata(_isolated_db):
    mods = _isolated_db
    table_store = mods["table_store"]
    schema = [
        {"key": "c1", "name": "Name", "type": "text"},
        {"key": "c2", "name": "Qty", "type": "number"},
    ]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        bob = await _create_user(mods, "bob@example.com")
        t1 = await table_store.create_table(
            alice, "Inventory", mode="private", schema=schema,
            last_write_source="conversation:c1",
        )
        await table_store.update_after_write(
            t1["id"], row_count=42, last_write_source="action_request:9",
        )
        await table_store.add_share(t1["id"], bob, "read")
        t2 = await table_store.create_table(
            bob, "Empty", mode="private", schema=schema[:1],
        )
        return alice, bob, t1["id"], t2["id"]

    alice, bob, t1, t2 = _run(_seed())
    rows = {r["id"]: r for r in _run(table_store.list_all_table_rows())}
    assert set(rows) == {t1, t2}
    assert rows[t1]["owner_id"] == alice
    assert rows[t1]["row_count"] == 42
    assert rows[t1]["column_count"] == 2
    assert rows[t1]["last_write_source"] == "action_request:9"
    assert rows[t1]["share_count"] == 1
    assert rows[t2]["owner_id"] == bob
    assert rows[t2]["row_count"] == 0
    assert rows[t2]["column_count"] == 1
    assert rows[t2]["share_count"] == 0
    assert "schema" not in rows[t1] and "table_schema" not in rows[t1]
    assert "title" not in rows[t1]


def test_memory_projections_cover_all_users_and_hide_text(_isolated_db):
    mods = _isolated_db
    memory_store = mods["memory_store"]
    suggestion_store = mods["suggestion_store"]
    project_memory_store = mods["project_memory_store"]
    project_suggestion_store = mods["project_suggestion_store"]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        bob = await _create_user(mods, "bob@example.com")
        conv = await _create_conversation(mods, alice)
        project = await mods["project_store"].create_project(bob, name="Proj")

        m1 = await memory_store.create_memory(alice, "Likes terse answers")
        m2 = await memory_store.create_memory(alice, "Old preference")
        await memory_store.archive_memory(alice, m2["id"])
        await memory_store.create_memory(bob, "Bob fact")

        s1 = await suggestion_store.create_suggestion(alice, conv, "Suggested one")
        await suggestion_store.accept_suggestion(alice, s1["id"])
        s2 = await suggestion_store.create_suggestion(alice, conv, "Suggested two")
        await suggestion_store.dismiss_suggestion(alice, s2["id"])
        await suggestion_store.create_suggestion(alice, conv, "Suggested three")

        await project_memory_store.create_project_memory(project["id"], "Project fact")
        pm = await project_memory_store.create_project_memory(project["id"], "Stale")
        await project_memory_store.set_project_memory_archived(project["id"], pm["id"], True)

        ps = await project_suggestion_store.create_suggestion(
            alice, project["id"], conv, "Project suggestion",
        )
        await project_suggestion_store.accept_suggestion(alice, ps["id"])
        return alice, bob, project["id"], m1["id"]

    alice, bob, project_id, m1 = _run(_seed())

    memories = _run(memory_store.list_all_memory_rows())
    # Alice: two created + one from the accepted suggestion; Bob: one.
    assert sorted((r["user_id"], r["archived"]) for r in memories) == sorted([
        (alice, False), (alice, True), (alice, False), (bob, False),
    ])
    by_len = {r["content_length"] for r in memories if r["user_id"] == alice}
    assert len("Likes terse answers") in by_len
    assert all("content" not in r for r in memories)
    assert all(isinstance(r["created_at"], datetime) for r in memories)

    suggestions = _run(suggestion_store.list_all_suggestion_rows())
    assert sorted(r["status"] for r in suggestions) == ["accepted", "dismissed", "pending"]
    assert all(r["user_id"] == alice for r in suggestions)
    assert all("content" not in r and "reasoning" not in r for r in suggestions)
    resolved = [r for r in suggestions if r["status"] != "pending"]
    assert all(isinstance(r["resolved_at"], datetime) for r in resolved)
    assert next(r for r in suggestions if r["status"] == "pending")["resolved_at"] is None

    project_memories = _run(project_memory_store.list_all_project_memory_rows())
    # Attributed to the project owner (bob), the accepted suggestion included.
    assert sorted((r["user_id"], r["project_id"], r["archived"]) for r in project_memories) == [
        (bob, project_id, False), (bob, project_id, False), (bob, project_id, True),
    ]
    assert all("content" not in r for r in project_memories)

    project_suggestions = _run(project_suggestion_store.list_all_suggestion_rows())
    assert [(r["user_id"], r["project_id"], r["status"]) for r in project_suggestions] == [
        (alice, project_id, "accepted"),
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def _admin(monkeypatch):
    import chat.routes.admin as admin_mod
    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    return admin_mod


def test_docs_report_endpoint_wires_range_and_roster(_isolated_db, _admin):
    mods = _isolated_db
    doc_store = mods["doc_store"]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        await _create_user(mods, "bob@example.com")
        d = await doc_store.create_doc(alice, "Notes", mode="private", content_size=5)
        async with mods["session"]() as db:
            await db.execute(
                update(mods["models"].Doc).where(mods["models"].Doc.id == d["id"])
                .values(created_at=D1, updated_at=D9)
            )
            await db.commit()
        return alice

    alice = _run(_seed())
    resp = _run(_admin.admin_docs_report(
        start="2026-10-09", end="2026-10-09", user={"email": "admin@example.com"},
    ))
    assert resp["total_users"] == 2
    assert [r["user_id"] for r in resp["users"]] == [alice]
    row = resp["users"][0]
    assert row["doc_count"] == 1
    assert row["created_in_range"] == 0
    assert row["updated_in_range"] == 1
    assert row["last_updated_at"] == D9.isoformat()

    outside = _run(_admin.admin_docs_report(
        start="2026-10-10", end=None, user={"email": "admin@example.com"},
    ))
    assert outside["users"][0]["updated_in_range"] == 0


def test_tables_and_memories_endpoints_return_rosters(_isolated_db, _admin):
    mods = _isolated_db

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        await _create_user(mods, "bob@example.com")
        await mods["table_store"].create_table(
            alice, "T", mode="private", schema=[{"key": "c1", "name": "A", "type": "text"}],
        )
        await mods["memory_store"].create_memory(alice, "fact")
        return alice

    alice = _run(_seed())
    tables = _run(_admin.admin_tables_report(user={"email": "admin@example.com"}))
    assert tables["total_users"] == 2
    assert [(r["user_id"], r["table_count"], r["column_total"]) for r in tables["users"]] == [
        (alice, 1, 1),
    ]
    memories = _run(_admin.admin_memories_report(user={"email": "admin@example.com"}))
    assert memories["total_users"] == 2
    assert [(r["user_id"], r["memory_count"]) for r in memories["users"]] == [(alice, 1)]
    # Today falls in the open range, so the fresh memory counts as created.
    assert memories["users"][0]["created_in_range"] == 1


@pytest.mark.parametrize("handler", ["admin_docs_report", "admin_tables_report",
                                     "admin_memories_report"])
def test_feature_usage_endpoints_reject_non_admin_and_bad_ranges(
    _isolated_db, monkeypatch, handler,
):
    from fastapi import HTTPException
    import chat.routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: False)
    with pytest.raises(HTTPException) as exc:
        _run(getattr(admin_mod, handler)(user={"email": "nobody@example.com"}))
    assert exc.value.status_code == 403

    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    with pytest.raises(HTTPException) as exc:
        _run(getattr(admin_mod, handler)(
            start="2026-10-09", end="2026-10-01", user={"email": "admin@example.com"},
        ))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        _run(getattr(admin_mod, handler)(start="nope", user={"email": "admin@example.com"}))
    assert exc.value.status_code == 400
