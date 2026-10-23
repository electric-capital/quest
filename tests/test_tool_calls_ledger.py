"""The tool_calls ledger write path (analytics/ledger.py), the sandbox
script recording site, the transcript reader (chat/storage.py) and the
chat_history.json backfill (analytics/backfill.py)."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from analytics import ledger as ledger_mod
from analytics.backfill import (
    TOOL_CALLS_BACKFILL,
    backfill_tool_calls,
    is_backfill_done,
)
from analytics.ledger import (
    TOOL_CALL_SOURCES,
    TOOL_CALL_TYPES,
    Ledger,
    effective_tool_name,
    record_tool_call,
)
from analytics.store import AnalyticsStore


@pytest.fixture()
def mem_store():
    store = AnalyticsStore(":memory:").open()
    yield store
    store.close()


@pytest.fixture()
def installed_ledger(mem_store, monkeypatch):
    ledger = Ledger(mem_store)
    monkeypatch.setattr(ledger_mod, "_current", ledger)
    return ledger


COLS = "conversation_id, user_id, tool_name, dynamic, call_type, source, tool_id"


def _rows(store, cols=COLS, order="tool_name, created_at"):
    with store.cursor() as cur:
        return cur.execute(f"SELECT {cols} FROM tool_calls ORDER BY {order}").fetchall()


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_effective_tool_name_unwraps_the_meta_tool():
    assert effective_tool_name("tool_call", {"tool_name": "get_gmail_messages"}) == (
        "get_gmail_messages", True)
    assert effective_tool_name("tool_call", {"arguments": {}}) == ("tool_call", False)
    assert effective_tool_name("tool_call", {"tool_name": ""}) == ("tool_call", False)
    assert effective_tool_name("tool_call", None) == ("tool_call", False)
    assert effective_tool_name("write_workspace_file", {"tool_name": "x"}) == (
        "write_workspace_file", False)


def test_record_and_flush_writes_one_row_per_call(mem_store):
    ledger = Ledger(mem_store)
    ledger.record_tool_call(
        user_id=7, tool_name="agent_task", dynamic=False, call_type="top_level",
        conversation_id="c1", tool_id="t1", project_id="p", routine_id="r",
    )
    ledger.record_tool_call(
        user_id=7, tool_name="get_workspace_file", dynamic=False, call_type="sub_agent",
        conversation_id="c1", tool_id="sub_t2", agent_name="Reader", sub_agent_level=2,
        parent_tool_id="t1",
    )
    ledger.record_tool_call(
        user_id=7, tool_name="list_quest_docs", dynamic=True, call_type="script",
    )
    assert ledger.pending == 3
    assert ledger.flush() == 3
    assert _rows(mem_store) == [
        ("c1", 7, "agent_task", False, "top_level", "live", "t1"),
        ("c1", 7, "get_workspace_file", False, "sub_agent", "live", "sub_t2"),
        (None, 7, "list_quest_docs", True, "script", "live", None),
    ]
    assert _rows(
        mem_store, "agent_name, sub_agent_level, parent_tool_id, project_id, routine_id",
    ) == [
        (None, None, None, "p", "r"),
        ("Reader", 2, "t1", None, None),
        (None, None, None, None, None),
    ]
    stamps = _rows(mem_store, "created_at")
    assert all(isinstance(s[0], datetime) and s[0].tzinfo is None for s in stamps)


def test_record_rejects_bad_calls(mem_store):
    ledger = Ledger(mem_store)
    with pytest.raises(ValueError, match="unknown tool call type"):
        ledger.record_tool_call(user_id=1, tool_name="x", dynamic=False, call_type="nope")
    with pytest.raises(ValueError, match="unknown tool call source"):
        ledger.record_tool_call(
            user_id=1, tool_name="x", dynamic=False, call_type="top_level", source="disk")
    with pytest.raises(ValueError):
        ledger.record_tool_call(user_id=1, tool_name="", dynamic=False, call_type="top_level")
    with pytest.raises(ValueError):
        ledger.record_tool_call(user_id=None, tool_name="x", dynamic=False, call_type="top_level")
    assert ledger.pending == 0


def test_every_discriminator_is_a_plain_string_the_schema_accepts(mem_store):
    ledger = Ledger(mem_store)
    for call_type in TOOL_CALL_TYPES:
        for source in TOOL_CALL_SOURCES:
            ledger.record_tool_call(
                user_id=1, tool_name="x", dynamic=False, call_type=call_type, source=source)
    assert ledger.flush() == len(TOOL_CALL_TYPES) * len(TOOL_CALL_SOURCES)


def test_module_helper_resolves_the_effective_name(installed_ledger, mem_store):
    record_tool_call(
        user_id=3, tool_name="tool_call", args={"tool_name": "get_gmail_messages", "arguments": {}},
        call_type="top_level", conversation_id="c1", tool_id="t1",
    )
    record_tool_call(
        user_id=3, tool_name="memory_search", args={"query": "q"},
        call_type="top_level", conversation_id="c1", tool_id="t2",
    )
    installed_ledger.flush()
    assert _rows(mem_store) == [
        ("c1", 3, "get_gmail_messages", True, "top_level", "live", "t1"),
        ("c1", 3, "memory_search", False, "top_level", "live", "t2"),
    ]


def test_module_helper_is_inert_without_a_ledger_and_never_raises(
    installed_ledger, monkeypatch, caplog,
):
    record_tool_call(user_id=1, tool_name="x", args=None, call_type="bogus", conversation_id="c")
    assert installed_ledger.pending == 0
    assert "failed to record tool call" in caplog.text
    record_tool_call(user_id=None, tool_name="x", args=None, call_type="top_level")
    record_tool_call(user_id=1, tool_name="", args=None, call_type="top_level")
    assert installed_ledger.pending == 0
    monkeypatch.setattr(ledger_mod, "_current", None)
    record_tool_call(user_id=1, tool_name="x", args=None, call_type="top_level")


# ---------------------------------------------------------------------------
# Recording site: the sandbox script bridge
# ---------------------------------------------------------------------------

def test_script_bridge_records_a_dynamic_script_call(installed_ledger, mem_store, monkeypatch):
    from chat.gemini_api import tool_dispatch
    from chat.gemini_api.script_tool_call import (
        SCRIPT_TOOL_CALL_ALLOWLIST,
        ScriptToolCallRequest,
        script_tool_call_endpoint,
    )
    from chat.sandbox_tokens import SandboxLease

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return json.dumps({"ok": True}), []

    monkeypatch.setattr(tool_dispatch, "_dispatch_tool_call", fake_dispatch)
    tool = sorted(SCRIPT_TOOL_CALL_ALLOWLIST)[0]
    lease = SandboxLease(user_id=5, conversation_id="conv-x", expires_at=0.0, project_id="p1")
    resp = asyncio.run(script_tool_call_endpoint(
        ScriptToolCallRequest(tool_name=tool, arguments={}),
        user={"id": 5, "email": "a@example.com"}, lease=lease,
    ))
    assert resp.status_code == 200
    assert seen["tool_name"] == "tool_call" and seen["args"]["tool_name"] == tool
    installed_ledger.flush()
    assert _rows(mem_store, COLS + ", project_id") == [
        ("conv-x", 5, tool, True, "script", "live", None, "p1"),
    ]

    # A tool outside the allow-list is refused before anything is recorded.
    resp = asyncio.run(script_tool_call_endpoint(
        ScriptToolCallRequest(tool_name="definitely_not_a_tool", arguments={}),
        user={"id": 5, "email": "a@example.com"}, lease=lease,
    ))
    assert resp.status_code == 400
    assert installed_ledger.pending == 0


# ---------------------------------------------------------------------------
# Transcript reader
# ---------------------------------------------------------------------------

@pytest.fixture()
def conv_dirs(monkeypatch, tmp_path):
    from chat.storage import ChatStorage
    dirs = {}
    monkeypatch.setattr(
        ChatStorage, "get_conversation_dir",
        staticmethod(lambda cid: dirs.setdefault(cid, tmp_path / cid)),
    )
    return dirs


def _write_history(conv_dir, messages):
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / "chat_history.json").write_text(json.dumps({
        "id": conv_dir.name, "user_id": 1, "created_at": "2026-01-01T00:00:00Z",
        "messages": messages,
    }))


TRANSCRIPT = [
    {"role": "user", "content": "hi", "timestamp": "2026-03-01T10:00:00Z"},
    {"type": "tool_use", "role": "assistant", "tool_name": "tool_call",
     "tool_input": {"tool_name": "get_gmail_messages", "arguments": {}},
     "tool_id": "t1", "timestamp": "2026-03-01T10:00:05Z"},
    {"type": "tool_result", "role": "assistant", "tool_id": "t1", "tool_output": "...",
     "timestamp": "2026-03-01T10:00:06Z"},
    {"type": "tool_use", "role": "assistant", "tool_name": "agent_task",
     "tool_input": {"prompt": "x"}, "tool_id": "t2", "timestamp": "2026-03-01T10:01:00Z"},
    {"type": "tool_result", "role": "assistant", "tool_id": "t2", "tool_output": "done",
     "timestamp": "2026-03-01T10:02:00Z",
     "sub_agent_tool_calls": [
         {"type": "sub_agent_tool_use", "parent_tool_id": "t2", "agent_name": "Reader",
          "tool_name": "get_workspace_file", "tool_input": {"path": "a"}, "tool_id": "sub_a"},
         {"type": "sub_agent_tool_result", "parent_tool_id": "t2", "tool_id": "sub_a"},
         {"type": "sub_agent_tool_use", "parent_tool_id": "t2", "agent_name": "Reader",
          "tool_name": "tool_call", "tool_input": {"tool_name": "list_quest_docs"},
          "tool_id": "sub_b", "nested_parent_id": "nested_1"},
         {"type": "sub_agent_finished", "parent_tool_id": "t2"},
     ]},
    {"type": "tool_use", "role": "assistant", "tool_input": {}, "tool_id": "t3",
     "timestamp": "2026-03-01T10:03:00Z"},   # no tool_name: skipped
    {"type": "tool_use", "role": "assistant", "tool_name": "memory_search",
     "tool_input": {"query": "q"}, "tool_id": "t4"},   # no timestamp
    {"type": "stats", "stats": {}},
]


def test_transcript_reader_lists_top_level_and_sub_agent_calls(conv_dirs, tmp_path):
    from chat.storage import ChatStorage
    _write_history(tmp_path / "c1", TRANSCRIPT)
    calls = ChatStorage.list_transcript_tool_calls("c1")
    assert [(c["call_type"], c["tool_name"], c["tool_id"], c["timestamp"]) for c in calls] == [
        ("top_level", "tool_call", "t1", "2026-03-01T10:00:05Z"),
        ("top_level", "agent_task", "t2", "2026-03-01T10:01:00Z"),
        ("sub_agent", "get_workspace_file", "sub_a", "2026-03-01T10:02:00Z"),
        ("sub_agent", "tool_call", "sub_b", "2026-03-01T10:02:00Z"),
        ("top_level", "memory_search", "t4", None),
    ]
    sub = [c for c in calls if c["call_type"] == "sub_agent"]
    assert [(c["agent_name"], c["sub_agent_level"], c["parent_tool_id"]) for c in sub] == [
        ("Reader", 1, "t2"), ("Reader", 2, "t2"),
    ]
    # Missing file, unreadable file: empty, never raises.
    assert ChatStorage.list_transcript_tool_calls("nope") == []
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "chat_history.json").write_text("{not json")
    assert ChatStorage.list_transcript_tool_calls("bad") == []


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _conv(cid, user_id, created, project_id=None, routine_id=None):
    return {"id": cid, "user_id": user_id, "project_id": project_id,
            "routine_id": routine_id, "created_at": created}


def _call(call_type, tool_name, tool_id, timestamp, tool_input=None, **extra):
    return {"call_type": call_type, "tool_name": tool_name, "tool_input": tool_input,
            "tool_id": tool_id, "timestamp": timestamp, "agent_name": None,
            "sub_agent_level": None, "parent_tool_id": None, **extra}


def test_backfill_imports_transcripts_once_and_honours_the_cutoff(mem_store):
    transcripts = {
        "c1": [
            _call("top_level", "tool_call", "t1", "2026-03-01T10:00:05Z",
                  {"tool_name": "get_gmail_messages"}),
            _call("sub_agent", "get_workspace_file", "sub_a", "2026-03-01T10:02:00Z",
                  agent_name="Reader", sub_agent_level=1, parent_tool_id="t2"),
            _call("top_level", "memory_search", "t4", None),           # takes created_at
            _call("top_level", "late_call", "t5", "2026-09-01T00:00:00Z"),  # after cutoff
        ],
        "c2": [],
    }
    convs = [
        _conv("c1", 1, datetime(2026, 3, 1, 9, 0), project_id="p", routine_id="r"),
        _conv("c2", 1, datetime(2026, 3, 2)),
        _conv("c3", 2, datetime(2026, 3, 3, tzinfo=timezone.utc)),   # no transcript
    ]
    written = backfill_tool_calls(
        mem_store, convs, lambda cid: transcripts.get(cid, []), cutoff=datetime(2026, 6, 1),
    )
    assert written == 3
    assert _rows(mem_store, COLS + ", created_at, project_id, routine_id", "created_at") == [
        ("c1", 1, "memory_search", False, "top_level", "backfill", "t4",
         datetime(2026, 3, 1, 9, 0), "p", "r"),
        ("c1", 1, "get_gmail_messages", True, "top_level", "backfill", "t1",
         datetime(2026, 3, 1, 10, 0, 5), "p", "r"),
        ("c1", 1, "get_workspace_file", False, "sub_agent", "backfill", "sub_a",
         datetime(2026, 3, 1, 10, 2), "p", "r"),
    ]
    assert _rows(mem_store, "agent_name, sub_agent_level, parent_tool_id", "created_at")[2] == (
        "Reader", 1, "t2")
    with mem_store.cursor() as cur:
        assert is_backfill_done(cur, TOOL_CALLS_BACKFILL)
        assert cur.execute(
            "SELECT row_count FROM _backfills WHERE name = ?", [TOOL_CALLS_BACKFILL],
        ).fetchone() == (3,)
    # Second run: the marker short-circuits it.
    assert backfill_tool_calls(mem_store, convs, lambda cid: transcripts["c1"]) is None
    assert len(_rows(mem_store)) == 3


def test_backfill_failure_leaves_no_rows_and_no_marker(mem_store):
    def boom(cid):
        if cid == "c2":
            raise OSError("disk")
        return [_call("top_level", "x", "t", "2026-01-01T00:00:00Z")]
    convs = [_conv("c1", 1, datetime(2026, 1, 1)), _conv("c2", 1, datetime(2026, 1, 2))]
    with pytest.raises(OSError):
        backfill_tool_calls(mem_store, convs, boom)
    assert _rows(mem_store) == []
    with mem_store.cursor() as cur:
        assert not is_backfill_done(cur, TOOL_CALLS_BACKFILL)
    assert backfill_tool_calls(
        mem_store, convs, lambda cid: [_call("top_level", "x", "t", "2026-01-01T00:00:00Z")],
    ) == 2


def test_backfill_with_nothing_to_import_still_marks_done(mem_store):
    assert backfill_tool_calls(mem_store, [], lambda cid: []) == 0
    with mem_store.cursor() as cur:
        assert is_backfill_done(cur, TOOL_CALLS_BACKFILL)


def test_run_backfills_reads_real_transcripts(isolated_db, mem_store, conv_dirs, tmp_path, monkeypatch):
    """The lifespan task end to end: conversations from quest.db, transcripts
    from each conversation directory, both imports marked."""
    from analytics import store as store_mod
    from analytics.backfill import SKILL_READS_BACKFILL, run_backfills
    from db.conversation_store import create_conversation
    from db.models import User

    monkeypatch.setattr(store_mod, "_current", mem_store)

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as db:
            u = User(email="a@example.com", api_key="k", name="A")
            db.add(u)
            await db.commit()
            await db.refresh(u)
        await create_conversation(u.id, "conv-a", datetime(2026, 2, 1, tzinfo=timezone.utc))
        await create_conversation(u.id, "conv-b", datetime(2026, 2, 2, tzinfo=timezone.utc))
        return u.id

    uid = asyncio.run(_seed())
    _write_history(tmp_path / "conv-a", TRANSCRIPT)
    asyncio.run(run_backfills())
    rows = _rows(mem_store, "conversation_id, user_id, tool_name, call_type, created_at", "created_at")
    assert rows == [
        ("conv-a", uid, "memory_search", "top_level", datetime(2026, 2, 1)),
        ("conv-a", uid, "get_gmail_messages", "top_level", datetime(2026, 3, 1, 10, 0, 5)),
        ("conv-a", uid, "agent_task", "top_level", datetime(2026, 3, 1, 10, 1)),
        ("conv-a", uid, "get_workspace_file", "sub_agent", datetime(2026, 3, 1, 10, 2)),
        ("conv-a", uid, "list_quest_docs", "sub_agent", datetime(2026, 3, 1, 10, 2)),
    ]
    with mem_store.cursor() as cur:
        assert is_backfill_done(cur, TOOL_CALLS_BACKFILL)
        assert is_backfill_done(cur, SKILL_READS_BACKFILL)
    # Idempotent across boots.
    asyncio.run(run_backfills())
    assert len(_rows(mem_store)) == 5
