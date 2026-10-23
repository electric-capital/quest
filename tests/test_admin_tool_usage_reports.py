"""Tests for the admin System Reports "Tool Calls" and "Skills" sections.

1. ``ChatStorage.scan_transcript_tool_usage``: tool_use extraction from a
   chat_history.json (meta-tool unwrapping, skill id capture, day clipping,
   the "user wrote in range" flag) against a temp chats dir.
2. The transcript walk ``collect_transcript_usage`` (bound pruning, missing
   transcripts) and the pure folds ``build_tool_calls_report`` /
   ``build_skills_report`` in chat/tool_usage_report.py.
3. ``skill_store.list_skill_labels`` against a throwaway sqlite file, and
   the two endpoints' admin gating, range parsing and wiring.
"""

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.tool_usage_report import (
    build_skills_report,
    build_tool_calls_report,
    collect_transcript_usage,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ChatStorage.scan_transcript_tool_usage
# ---------------------------------------------------------------------------

def _msg(ts, **fields):
    return {"timestamp": ts, **fields}


TRANSCRIPT = {
    "messages": [
        _msg("2026-10-01T09:00:00Z", role="user", content="hi"),
        _msg("2026-10-01T09:00:01Z", type="tool_use", role="assistant",
             tool_name="tool_call",
             tool_input={"tool_name": "set_conversation_name", "arguments": {"name": "x"}},
             tool_id="c1"),
        _msg("2026-10-01T09:00:02Z", type="tool_result", role="assistant",
             tool_id="c1", tool_output="{}"),
        _msg("2026-10-05T10:00:00Z", role="user", content="load it"),
        _msg("2026-10-05T10:00:01Z", type="tool_use", role="assistant",
             tool_name="load_skills", tool_input={"skill_ids": ["s1", "system:gmail"]},
             tool_id="c2"),
        _msg("2026-10-05T10:00:02Z", type="tool_use", role="assistant",
             tool_name="get_skill", tool_input={"skill_id": "s2"}, tool_id="c3"),
        _msg("2026-10-05T10:00:03Z", type="tool_use", role="assistant",
             tool_name="run_script", tool_input={"script": "ls"}, tool_id="c4"),
        # A tool_call whose inner name is missing keeps the meta-tool name.
        _msg("2026-10-05T10:00:04Z", type="tool_use", role="assistant",
             tool_name="tool_call", tool_input={"arguments": {}}, tool_id="c5"),
        # Garbage rows are skipped.
        "not a dict",
        _msg(None, type="tool_use", tool_name="read_file", tool_input={}),
        _msg("2026-10-09T10:00:00Z", type="tool_use", role="assistant",
             tool_name=None, tool_input={}),
    ],
}


@pytest.fixture()
def _chats_dir(monkeypatch):
    import chat.storage as storage_mod
    tmp = Path(tempfile.mkdtemp(prefix="quest_tool_usage_test_"))
    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp, raising=True)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _write_transcript(chats_dir: Path, conv_id: str, data: dict) -> None:
    d = chats_dir / conv_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "chat_history.json").write_text(json.dumps(data))


def test_scan_unwraps_meta_tool_and_captures_skill_ids(_chats_dir):
    from chat.storage import ChatStorage
    conv = str(uuid.uuid4())
    _write_transcript(_chats_dir, conv, TRANSCRIPT)

    scanned = ChatStorage.scan_transcript_tool_usage(conv)
    assert scanned["active"] is True
    assert [(c["tool"], c["dynamic"], c["skill_ids"]) for c in scanned["tool_calls"]] == [
        ("set_conversation_name", True, []),
        ("load_skills", False, ["s1", "system:gmail"]),
        ("get_skill", False, ["s2"]),
        ("run_script", False, []),
        ("tool_call", False, []),
    ]
    assert scanned["tool_calls"][0]["timestamp"] == "2026-10-01T09:00:01Z"


def test_scan_clips_to_day_window_and_flags_user_activity(_chats_dir):
    from chat.storage import ChatStorage
    conv = str(uuid.uuid4())
    _write_transcript(_chats_dir, conv, TRANSCRIPT)

    only_first = ChatStorage.scan_transcript_tool_usage(conv, "2026-10-01", "2026-10-01")
    assert [c["tool"] for c in only_first["tool_calls"]] == ["set_conversation_name"]
    assert only_first["active"] is True

    # Days with tool calls but no user message: not active.
    later = ChatStorage.scan_transcript_tool_usage(conv, "2026-10-05T", None)
    assert later["active"] is False
    none = ChatStorage.scan_transcript_tool_usage(conv, "2026-10-06", "2026-10-31")
    assert none == {"tool_calls": [], "active": False}


def test_scan_returns_none_for_missing_or_broken_transcript(_chats_dir):
    from chat.storage import ChatStorage
    assert ChatStorage.scan_transcript_tool_usage(str(uuid.uuid4())) is None
    conv = str(uuid.uuid4())
    (_chats_dir / conv).mkdir()
    (_chats_dir / conv / "chat_history.json").write_text("{not json")
    assert ChatStorage.scan_transcript_tool_usage(conv) is None


# ---------------------------------------------------------------------------
# collect_transcript_usage / build_* folds
# ---------------------------------------------------------------------------

D1 = datetime(2026, 10, 1, 12, 0)
D5 = datetime(2026, 10, 5, 12, 0)
D9 = datetime(2026, 10, 9, 12, 0)


def _conv(conv_id, user_id, *, routine=None, created=D1, last=D9):
    return {"id": conv_id, "user_id": user_id, "routine_id": routine,
            "created_at": created, "last_message_at": last}


def _call(tool, ts="2026-10-05T10:00:00Z", dynamic=False, skill_ids=()):
    return {"tool": tool, "dynamic": dynamic, "timestamp": ts, "skill_ids": list(skill_ids)}


def test_collect_prunes_by_bounds_and_skips_missing_transcripts():
    rows = [
        _conv("in", 1),
        _conv("too-old", 1, created=datetime(2026, 9, 1), last=datetime(2026, 9, 2)),
        _conv("too-new", 1, created=datetime(2026, 10, 20), last=datetime(2026, 10, 21)),
        _conv("no-bounds", 2, created=None, last=None),
        _conv("gone", 2),
    ]
    scans = {
        "in": {"tool_calls": [_call("read_file")], "active": True},
        "no-bounds": {"tool_calls": [], "active": False},
    }
    seen = []

    def scan(conv_id, start_day, end_day):
        seen.append((conv_id, start_day, end_day))
        return scans.get(conv_id)

    records = collect_transcript_usage(
        rows, date(2026, 10, 1), date(2026, 10, 9), scan,
        lambda conv_id: ["s1"] if conv_id == "in" else [],
    )
    assert [s[0] for s in seen] == ["in", "no-bounds", "gone"]
    assert seen[0][1:] == ("2026-10-01", "2026-10-09")
    assert [(r["conversation_id"], r["active"], r["skill_ids"]) for r in records] == [
        ("in", True, ["s1"]), ("no-bounds", False, []),
    ]
    assert records[0]["last_message_at"] == D9


def test_tool_calls_report_counts_calls_conversations_and_users():
    records = [
        {"conversation_id": "a", "user_id": 1, "routine_id": None, "last_message_at": D9,
         "active": True, "skill_ids": [],
         "tool_calls": [_call("read_file", "2026-10-05T10:00:00Z"),
                        _call("read_file", "2026-10-06T10:00:00Z"),
                        _call("set_conversation_name", dynamic=True)]},
        {"conversation_id": "b", "user_id": 2, "routine_id": "r1", "last_message_at": D9,
         "active": True, "skill_ids": [],
         "tool_calls": [_call("read_file", "2026-10-04T10:00:00Z"),
                        _call("run_script")]},
        {"conversation_id": "c", "user_id": 2, "routine_id": None, "last_message_at": D5,
         "active": False, "skill_ids": ["s1"], "tool_calls": []},
    ]
    report = build_tool_calls_report(records)
    assert report["call_count"] == 5
    assert report["conversation_count"] == 2
    assert report["scanned_conversations"] == 3
    assert [(r["tool"], r["call_count"]) for r in report["tools"]] == [
        ("read_file", 3), ("run_script", 1), ("set_conversation_name", 1),
    ]
    read_file = report["tools"][0]
    assert read_file == {
        "tool": "read_file", "dynamic": False, "call_count": 3,
        "routine_call_count": 1, "conversation_count": 2, "user_count": 2,
        "last_called_at": "2026-10-06T10:00:00Z",
    }
    assert report["tools"][2]["dynamic"] is True


def test_tool_calls_report_empty():
    assert build_tool_calls_report([]) == {
        "tools": [], "call_count": 0, "conversation_count": 0, "scanned_conversations": 0,
    }


LABELS = {
    "s1": {"name": "Weekly digest", "visibility": "private", "project_id": None,
           "creator_email": "alice@example.com", "creator_name": "Alice"},
    "s2": {"name": "Deploy", "visibility": "public", "project_id": None,
           "creator_email": "bob@example.com", "creator_name": None},
    "p1": {"name": "Project notes", "visibility": "private", "project_id": "proj",
           "creator_email": "alice@example.com", "creator_name": "Alice"},
    "system:gmail": {"name": "Gmail"},
}


def test_skills_report_attributes_reads_to_active_conversations_and_counts_loads():
    records = [
        # Active in range, two skills in context, one explicit load.
        {"conversation_id": "a", "user_id": 1, "routine_id": None, "last_message_at": D9,
         "active": True, "skill_ids": ["s1", "system:gmail"],
         "tool_calls": [_call("load_skills", skill_ids=["system:gmail"])]},
        # Routine conversation, active, same skill as a plus a project skill.
        {"conversation_id": "b", "user_id": 2, "routine_id": "r1", "last_message_at": D5,
         "active": True, "skill_ids": ["s1", "p1"], "tool_calls": []},
        # NOT active in range: its skill reads do not count...
        {"conversation_id": "c", "user_id": 3, "routine_id": None, "last_message_at": D1,
         "active": False, "skill_ids": ["s2", "s1"],
         # ...but an explicit in-range load does.
         "tool_calls": [_call("get_skill", skill_ids=["gone"])]},
    ]
    report = build_skills_report(records, LABELS)
    assert report["conversation_count"] == 2
    assert report["scanned_conversations"] == 3
    assert [(r["skill_id"], r["conversation_count"], r["load_count"]) for r in report["skills"]] == [
        ("s1", 2, 0), ("gone", 1, 1), ("system:gmail", 1, 1), ("p1", 1, 0),
    ]
    by_id = {r["skill_id"]: r for r in report["skills"]}
    assert by_id["s1"] == {
        "skill_id": "s1", "name": "Weekly digest", "kind": "private",
        "owner_email": "alice@example.com", "owner_name": "Alice", "project_id": None,
        "conversation_count": 2, "routine_conversation_count": 1, "user_count": 2,
        "load_count": 0, "last_used_at": D9.isoformat(),
    }
    assert by_id["system:gmail"]["kind"] == "system"
    assert by_id["system:gmail"]["owner_email"] is None
    assert by_id["p1"]["kind"] == "project"
    assert by_id["p1"]["project_id"] == "proj"
    assert by_id["gone"] == {
        "skill_id": "gone", "name": "(deleted skill)", "kind": "deleted",
        "owner_email": None, "owner_name": None, "project_id": None,
        "conversation_count": 1, "routine_conversation_count": 0, "user_count": 1,
        "load_count": 1, "last_used_at": D1.isoformat(),
    }
    # s2 was only in an inactive conversation: no row.
    assert "s2" not in by_id


def test_skills_report_names_unlabeled_system_skills_from_their_id():
    records = [
        {"conversation_id": "a", "user_id": 1, "routine_id": None, "last_message_at": None,
         "active": True, "skill_ids": ["system:unknown"], "tool_calls": []},
    ]
    report = build_skills_report(records, {})
    assert report["skills"][0]["name"] == "unknown"
    assert report["skills"][0]["kind"] == "system"
    assert report["skills"][0]["last_used_at"] is None


# ---------------------------------------------------------------------------
# skill_store.list_skill_labels
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_tool_usage_db_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.skill_store as skill_store_mod
    import db.conversation_store as conversation_store_mod

    models_mod.Base.metadata.create_all(sync_engine)
    for mod in (skill_store_mod, conversation_store_mod):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    yield {"models": models_mod, "skill_store": skill_store_mod, "session": session_local}

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


def test_list_skill_labels_returns_any_owner_without_content(_isolated_db):
    mods = _isolated_db
    store = mods["skill_store"]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        bob = await _create_user(mods, "bob@example.com")
        a = await store.create_skill(alice, "Digest", "d", "SECRET BODY", visibility="private")
        b = await store.create_skill(bob, "Deploy", "d", "SECRET BODY", visibility="public")
        return alice, bob, a["id"], b["id"]

    alice, bob, a_id, b_id = _run(_seed())
    labels = _run(store.list_skill_labels([a_id, b_id, "missing", "system:gmail", ""]))
    assert set(labels) == {a_id, b_id}
    assert labels[a_id] == {
        "id": a_id, "name": "Digest", "visibility": "private", "project_id": None,
        "creator_id": alice, "creator_email": "alice@example.com", "creator_name": "Alice",
    }
    assert labels[b_id]["creator_id"] == bob
    assert not labels[b_id]["creator_name"]
    assert all("content" not in label for label in labels.values())
    assert _run(store.list_skill_labels([])) == {}
    assert _run(store.list_skill_labels(["system:gmail"])) == {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def _admin(monkeypatch):
    import chat.routes.admin as admin_mod
    monkeypatch.setattr(admin_mod, "is_admin", lambda email: True)
    return admin_mod


def test_endpoints_wire_scan_range_and_labels(_isolated_db, _chats_dir, _admin, monkeypatch):
    mods = _isolated_db
    store = mods["skill_store"]

    async def _seed():
        alice = await _create_user(mods, "alice@example.com", "Alice")
        skill = await store.create_skill(alice, "Digest", "d", "BODY", visibility="shared")
        return alice, skill["id"]

    alice, skill_id = _run(_seed())
    conv = str(uuid.uuid4())
    _write_transcript(_chats_dir, conv, {
        "messages": [
            _msg("2026-10-05T10:00:00Z", role="user", content="go"),
            _msg("2026-10-05T10:00:01Z", type="tool_use", role="assistant",
                 tool_name="load_skills", tool_input={"skill_ids": [skill_id]}, tool_id="c1"),
            _msg("2026-10-05T10:00:02Z", type="tool_use", role="assistant",
                 tool_name="tool_call",
                 tool_input={"tool_name": "list_routines", "arguments": {}}, tool_id="c2"),
        ],
    })
    (_chats_dir / conv / "skill_reads.json").write_text(
        json.dumps({"skill_ids": [skill_id, "system:gmail"]})
    )

    async def fake_rows():
        return [_conv(conv, alice, created=D1, last=D9)]

    monkeypatch.setattr(_admin, "list_conversation_activity_rows", fake_rows)

    tools = _run(_admin.admin_tool_calls_report(
        start="2026-10-05", end="2026-10-05", user={"email": "admin@example.com"},
    ))
    assert tools["call_count"] == 2
    assert tools["conversation_count"] == 1
    assert tools["scanned_conversations"] == 1
    assert [(r["tool"], r["dynamic"]) for r in tools["tools"]] == [
        ("list_routines", True), ("load_skills", False),
    ]

    skills = _run(_admin.admin_skills_report(
        start="2026-10-05", end="2026-10-05", user={"email": "admin@example.com"},
    ))
    assert skills["conversation_count"] == 1
    rows = {r["skill_id"]: r for r in skills["skills"]}
    assert set(rows) == {skill_id, "system:gmail"}
    assert rows[skill_id]["name"] == "Digest"
    assert rows[skill_id]["kind"] == "shared"
    assert rows[skill_id]["owner_email"] == "alice@example.com"
    assert rows[skill_id]["load_count"] == 1
    assert rows[skill_id]["conversation_count"] == 1
    assert rows[skill_id]["user_count"] == 1
    assert rows["system:gmail"]["kind"] == "system"
    assert rows["system:gmail"]["load_count"] == 0
    # The catalog names the system skill (never its content).
    assert rows["system:gmail"]["name"] and rows["system:gmail"]["name"] != "system:gmail"

    # A range the conversation did not see: nothing counted, no scan either.
    outside = _run(_admin.admin_tool_calls_report(
        start="2026-10-20", end=None, user={"email": "admin@example.com"},
    ))
    assert outside == {
        "tools": [], "call_count": 0, "conversation_count": 0, "scanned_conversations": 0,
    }


@pytest.mark.parametrize("handler", ["admin_tool_calls_report", "admin_skills_report"])
def test_endpoints_reject_non_admin_and_bad_ranges(monkeypatch, handler):
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
