"""The skill_loads ledger write path (analytics/ledger.py) and the sites
that record to it, plus the skill_reads.json backfill (analytics/backfill.py)."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from analytics import ledger as ledger_mod
from analytics.backfill import (
    SKILL_READS_BACKFILL,
    backfill_skill_reads,
    is_backfill_done,
)
from analytics.ledger import SKILL_LOAD_SOURCES, Ledger, record_skill_load
from analytics.store import AnalyticsStore


@pytest.fixture()
def mem_store():
    store = AnalyticsStore(":memory:").open()
    yield store
    store.close()


@pytest.fixture()
def installed_ledger(mem_store, monkeypatch):
    """An in-memory ledger installed as the process-wide one (no writer
    thread: tests flush explicitly)."""
    ledger = Ledger(mem_store)
    monkeypatch.setattr(ledger_mod, "_current", ledger)
    return ledger


def _rows(store, cols="conversation_id, user_id, skill_id, source, project_id, routine_id"):
    with store.cursor() as cur:
        return cur.execute(f"SELECT {cols} FROM skill_loads ORDER BY skill_id").fetchall()


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_record_and_flush_writes_one_row_per_skill(mem_store):
    ledger = Ledger(mem_store)
    assert ledger.record_skill_load(
        conversation_id="c1", user_id=7, skill_ids=["a", "", "system:b"],
        source="composer", project_id="p", routine_id=None,
    ) == 2
    assert ledger.pending == 2
    assert ledger.flush() == 2
    assert ledger.pending == 0
    assert _rows(mem_store) == [
        ("c1", 7, "a", "composer", "p", None),
        ("c1", 7, "system:b", "composer", "p", None),
    ]
    stamps = _rows(mem_store, "created_at")
    assert all(isinstance(s[0], datetime) and s[0].tzinfo is None for s in stamps)


def test_record_rejects_bad_calls(mem_store):
    ledger = Ledger(mem_store)
    with pytest.raises(ValueError, match="unknown skill load source"):
        ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=["a"], source="nope")
    with pytest.raises(ValueError):
        ledger.record_skill_load(conversation_id="", user_id=1, skill_ids=["a"], source="composer")
    assert ledger.record_skill_load(
        conversation_id="c", user_id=1, skill_ids=[], source="composer") == 0
    assert ledger.pending == 0


def test_writer_thread_flushes_and_stops_cleanly(mem_store):
    ledger = Ledger(mem_store, flush_interval=0.05)
    ledger.start()
    ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=["a"], source="tool_get_skill")
    for _ in range(100):
        if ledger.pending == 0 and _rows(mem_store):
            break
        asyncio.run(asyncio.sleep(0.02))
    assert len(_rows(mem_store)) == 1
    ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=["b"], source="tool_get_skill")
    ledger.stop()  # final flush
    assert len(_rows(mem_store)) == 2
    ledger.stop()  # idempotent


def test_queue_cap_drops_rather_than_grows(mem_store):
    ledger = Ledger(mem_store, max_queue=3)
    ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=list("abcde"), source="composer")
    assert ledger.pending == 3
    assert ledger.dropped == 2
    assert ledger.flush() == 3


def test_poison_batch_is_dropped_not_retried(mem_store):
    ledger = Ledger(mem_store)
    ledger._enqueue("no_such_table", ("x",), [(1,)])
    assert ledger.flush() == 0
    assert ledger.dropped == 1
    assert ledger.pending == 0
    # The writer is still usable afterwards.
    ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=["a"], source="composer")
    assert ledger.flush() == 1


def test_module_helper_is_inert_without_a_ledger(monkeypatch):
    monkeypatch.setattr(ledger_mod, "_current", None)
    record_skill_load(conversation_id="c", user_id=1, skill_ids=["a"], source="composer")


def test_module_helper_never_raises(installed_ledger, caplog):
    record_skill_load(conversation_id="c", user_id=1, skill_ids=["a"], source="bogus")
    assert installed_ledger.pending == 0
    assert "failed to record skill load" in caplog.text
    record_skill_load(conversation_id=None, user_id=1, skill_ids=["a"], source="composer")
    record_skill_load(conversation_id="c", user_id=None, skill_ids=["a"], source="composer")
    assert installed_ledger.pending == 0


def test_every_source_is_a_plain_string_the_schema_accepts(mem_store):
    ledger = Ledger(mem_store)
    for source in SKILL_LOAD_SOURCES:
        ledger.record_skill_load(conversation_id="c", user_id=1, skill_ids=["s"], source=source)
    assert ledger.flush() == len(SKILL_LOAD_SOURCES)


# ---------------------------------------------------------------------------
# Recording sites
# ---------------------------------------------------------------------------

@pytest.fixture()
def conv_dir(monkeypatch, tmp_path):
    from chat.storage import ChatStorage
    d = tmp_path / "conv"
    d.mkdir()
    monkeypatch.setattr(
        ChatStorage, "_get_conversation_dir", staticmethod(lambda conversation_id: d),
    )
    return d


def test_tool_handler_mark_splits_gate_and_ledger(installed_ledger, mem_store, conv_dir):
    from chat.gemini_api.tool_handlers.skills import _mark_skills_read
    from chat.storage import ChatStorage

    _mark_skills_read("c1", 3, ["db-1", "system:web_fetch"], "tool_load_skills")
    # The edit_skill read gate sees DB skills only ...
    assert ChatStorage.get_skill_read_ids("c1") == ["db-1"]
    # ... the ledger sees both.
    installed_ledger.flush()
    assert _rows(mem_store) == [
        ("c1", 3, "db-1", "tool_load_skills", None, None),
        ("c1", 3, "system:web_fetch", "tool_load_skills", None, None),
    ]
    # No conversation: nothing recorded anywhere, no error.
    _mark_skills_read(None, 3, ["db-2"], "tool_get_skill")
    assert installed_ledger.pending == 0


def test_create_skill_mark_records_create_source(installed_ledger, mem_store, conv_dir):
    from chat.action_request_types.create_skill import _mark_created_skill_read
    from chat.storage import ChatStorage

    _mark_created_skill_read("c9", 4, "new-skill", "proj-1")
    assert ChatStorage.get_skill_read_ids("c9") == ["new-skill"]
    installed_ledger.flush()
    assert _rows(mem_store) == [("c9", 4, "new-skill", "create_skill", "proj-1", None)]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _conv(cid, user_id, created, project_id=None, routine_id=None):
    return {"id": cid, "user_id": user_id, "project_id": project_id,
            "routine_id": routine_id, "created_at": created}


def test_backfill_imports_sidecars_once(mem_store):
    sidecars = {"c1": ["s1", "s2"], "c2": [], "c3": ["system:x"]}
    convs = [
        _conv("c1", 1, datetime(2026, 3, 1, 10, 0), project_id="p"),
        _conv("c2", 1, datetime(2026, 3, 2)),
        _conv("c3", 2, datetime(2026, 3, 3, tzinfo=timezone.utc), routine_id="r"),
        _conv("c4", 2, None),  # no sidecar at all
    ]
    written = backfill_skill_reads(mem_store, convs, lambda cid: sidecars.get(cid, []))
    assert written == 3
    assert _rows(mem_store, "conversation_id, user_id, skill_id, source, project_id, routine_id, created_at") == [
        ("c1", 1, "s1", "backfill", "p", None, datetime(2026, 3, 1, 10, 0)),
        ("c1", 1, "s2", "backfill", "p", None, datetime(2026, 3, 1, 10, 0)),
        ("c3", 2, "system:x", "backfill", None, "r", datetime(2026, 3, 3)),
    ]
    with mem_store.cursor() as cur:
        assert is_backfill_done(cur, SKILL_READS_BACKFILL)
        assert cur.execute("SELECT row_count FROM _backfills").fetchone() == (3,)
    # Second run: the marker short-circuits it.
    assert backfill_skill_reads(mem_store, convs, lambda cid: ["again"]) is None
    assert len(_rows(mem_store)) == 3


def test_backfill_failure_leaves_no_rows_and_no_marker(mem_store):
    def boom(cid):
        if cid == "c2":
            raise OSError("disk")
        return ["s"]
    convs = [_conv("c1", 1, datetime(2026, 1, 1)), _conv("c2", 1, datetime(2026, 1, 2))]
    with pytest.raises(OSError):
        backfill_skill_reads(mem_store, convs, boom)
    assert _rows(mem_store) == []
    with mem_store.cursor() as cur:
        assert not is_backfill_done(cur, SKILL_READS_BACKFILL)
    # And it works on the retry.
    assert backfill_skill_reads(mem_store, convs, lambda cid: ["s"]) == 2


def test_backfill_with_nothing_to_import_still_marks_done(mem_store):
    assert backfill_skill_reads(mem_store, [], lambda cid: []) == 0
    with mem_store.cursor() as cur:
        assert is_backfill_done(cur, SKILL_READS_BACKFILL)


def test_run_backfill_reads_real_sidecars(isolated_db, mem_store, monkeypatch, tmp_path):
    """The lifespan task end to end: conversations from quest.db, sidecars
    from each conversation directory."""
    from analytics import store as store_mod
    from analytics.backfill import run_skill_reads_backfill
    from chat.storage import ChatStorage
    from db.conversation_store import create_conversation
    from db.models import User

    monkeypatch.setattr(store_mod, "_current", mem_store)
    dirs = {}
    monkeypatch.setattr(
        ChatStorage, "_get_conversation_dir",
        staticmethod(lambda cid: dirs.setdefault(cid, tmp_path / cid)),
    )

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
    (tmp_path / "conv-a").mkdir()
    (tmp_path / "conv-a" / "skill_reads.json").write_text(json.dumps({"skill_ids": ["s1"]}))
    asyncio.run(run_skill_reads_backfill())
    assert _rows(mem_store, "conversation_id, user_id, skill_id, source, created_at") == [
        ("conv-a", uid, "s1", "backfill", datetime(2026, 2, 1)),
    ]
    # Idempotent across boots.
    asyncio.run(run_skill_reads_backfill())
    assert len(_rows(mem_store)) == 1
