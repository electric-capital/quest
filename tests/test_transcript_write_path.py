"""chat_history.json write path (chat/storage.py, chat/_flush_helper.py).

* Concurrent appends to one conversation land in call order with
  consecutive seqs and never overlap; appends to another conversation,
  and the event loop itself, keep going while one append's write is
  parked in its worker thread.
* An append parses the transcript once: the seq high water comes from
  that parse, not from a second read.
* The synchronous writers (notice flags, guide snapshot, action-request
  status) wait for an in-flight threaded append instead of losing it.
* Files are written as compact JSON through an atomic replace.
* Each append records its metadata with one ``record_messages_appended``
  call.
* A flush cancelled mid-write still lands, and the driver's final flush
  does not append the same messages again.

DB writes are stubbed (``record_messages_appended``); the real-DB side
lives in tests/test_storage_seq.py.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import chat.storage as storage_mod


def _cs():
    # Other suites reload chat.storage; bind the class at call time.
    return storage_mod.ChatStorage


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def chats(tmp_path, monkeypatch):
    """Point CHATS_DIR at a temp dir, stub the DB update and the bus publish."""
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats_dir, raising=True)
    import db.conversation_store as conv_store

    meta_calls: list[tuple[str, dict]] = []

    async def _record(conversation_id, **kwargs):
        meta_calls.append((conversation_id, kwargs))

    monkeypatch.setattr(conv_store, "record_messages_appended", _record)
    monkeypatch.setattr(storage_mod, "_publish_appended_to_bus", lambda *a: None)
    return chats_dir, meta_calls


def _seed(chats_dir, cid, data=None):
    (chats_dir / cid).mkdir(parents=True, exist_ok=True)
    (chats_dir / cid / "chat_history.json").write_text(
        json.dumps(data if data is not None else {"id": cid, "messages": []})
    )


def _history(chats_dir, cid):
    return json.loads((chats_dir / cid / "chat_history.json").read_text())


def _text(content):
    return {"type": "text", "role": "assistant", "content": content}


@pytest.fixture()
def gate_c1_writes(monkeypatch):
    """Park every write of conversation ``c1`` until ``gate`` is set.

    ``entered`` is set once a write is parked: the writer is inside its
    read-modify-write, holding c1's file lock, in a worker thread.
    """
    entered = threading.Event()
    gate = threading.Event()
    real_write = storage_mod.write_json_atomic

    def _gated(path, data):
        if path.parent.name == "c1":
            entered.set()
            assert gate.wait(10), "test never released the parked write"
        real_write(path, data)

    monkeypatch.setattr(storage_mod, "write_json_atomic", _gated)
    return entered, gate


# ---------------------------------------------------------------------------
# Ordering and isolation
# ---------------------------------------------------------------------------


def test_concurrent_appends_land_in_call_order_and_never_overlap(chats, monkeypatch):
    chats_dir, _ = chats
    _seed(chats_dir, "c1")
    real_write = storage_mod.write_json_atomic
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    def _slow_write(path, data):
        # Slow, overlapping-prone writes: any missing serialization would
        # reorder or drop messages here.
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            threading.Event().wait(0.005)
            real_write(path, data)
        finally:
            with guard:
                active["now"] -= 1

    monkeypatch.setattr(storage_mod, "write_json_atomic", _slow_write)

    async def main():
        tasks = []
        for i in range(24):
            if i % 4 == 0:
                tasks.append(asyncio.create_task(
                    _cs().append_message("c1", "user", f"m{i}"),
                ))
            else:
                tasks.append(asyncio.create_task(
                    _cs().append_structured_messages("c1", [_text(f"m{i}")]),
                ))
        return await asyncio.gather(*tasks)

    results = _run(main())
    seqs = [r[0] if isinstance(r, tuple) else r[0][0] for r in results]
    assert seqs == list(range(1, 25))

    on_disk = _history(chats_dir, "c1")["messages"]
    assert [m["content"] for m in on_disk] == [f"m{i}" for i in range(24)]
    assert [m["seq"] for m in on_disk] == list(range(1, 25))
    assert active["max"] == 1


def test_other_conversations_and_the_loop_proceed_while_an_append_is_parked(
    chats, gate_c1_writes,
):
    chats_dir, _ = chats
    entered, gate = gate_c1_writes
    _seed(chats_dir, "c1")
    _seed(chats_dir, "c2")

    async def main():
        parked = asyncio.create_task(
            _cs().append_structured_messages("c1", [_text("slow")]),
        )
        await asyncio.to_thread(entered.wait, 10)
        # The loop is free while c1's write sits in its worker thread, and
        # c2 has its own locks.
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        other = await asyncio.wait_for(
            _cs().append_structured_messages("c2", [_text("fast")]), 10,
        )
        assert not parked.done()
        gate.set()
        return ticks, other, await parked

    ticks, other, parked = _run(main())
    assert ticks == 5
    assert [s for s, _ in other] == [1]
    assert [s for s, _ in parked] == [1]
    assert [m["content"] for m in _history(chats_dir, "c1")["messages"]] == ["slow"]
    assert [m["content"] for m in _history(chats_dir, "c2")["messages"]] == ["fast"]


# ---------------------------------------------------------------------------
# Seq high water from the same parse
# ---------------------------------------------------------------------------


def test_append_parses_the_transcript_once(chats, monkeypatch):
    chats_dir, _ = chats
    _seed(chats_dir, "c1", {"id": "c1", "messages": [
        {"role": "user", "content": "a", "seq": 1},
        {"role": "assistant", "content": "b", "seq": 7},
        {"role": "user", "content": "c", "seq": 3},
    ]})

    def _no_second_read(_cid):
        raise AssertionError("append re-read chat_history.json for the seq high water")

    monkeypatch.setattr(
        _cs(), "_read_seq_high_water", staticmethod(_no_second_read),
    )
    loads = []
    real_load = json.load

    def _counting_load(fp, *a, **kw):
        loads.append(getattr(fp, "name", None))
        return real_load(fp, *a, **kw)

    monkeypatch.setattr(storage_mod.json, "load", _counting_load)

    pairs = _run(_cs().append_structured_messages("c1", [_text("x"), _text("y")]))

    assert [s for s, _ in pairs] == [8, 9]  # max seq on disk + 1, not len + 1
    assert len(loads) == 1


@pytest.mark.parametrize(
    "messages, expected",
    [
        ([], 0),
        ([{"role": "user"}, {"role": "assistant"}], 2),  # pre-migration: len
        ([{"seq": 2}, {"seq": 5}, {"seq": 4}], 5),
        ([{"role": "user"}, {"seq": 3}, "junk", {"seq": "9"}], 3),
    ],
)
def test_seq_high_water_from_parsed_transcript(messages, expected):
    assert storage_mod._seq_high_water_from({"messages": messages}) == expected


def test_seq_high_water_from_tolerates_non_objects():
    assert storage_mod._seq_high_water_from(None) == 0
    assert storage_mod._seq_high_water_from({"messages": None}) == 0


# ---------------------------------------------------------------------------
# Synchronous writers vs a threaded append
# ---------------------------------------------------------------------------


def _set_flags():
    return _cs().set_conversation_flags("c1", {"own_workspace": True})


def _set_guide():
    return _cs().set_guide_snapshot("c1", "g1", "Guide", "Be brief.")


def _resolve_card():
    return _cs().update_action_request_message("c1", 42, "approved")


@pytest.mark.parametrize("writer", [_set_flags, _set_guide, _resolve_card])
def test_sync_writer_waits_for_an_in_flight_append(chats, gate_c1_writes, writer):
    chats_dir, _ = chats
    entered, gate = gate_c1_writes
    _seed(chats_dir, "c1", {"id": "c1", "messages": [
        {"type": "action_request", "request_id": 42, "status": "open", "seq": 1},
    ]})

    async def main():
        append = asyncio.create_task(
            _cs().append_structured_messages("c1", [_text("during")]),
        )
        await asyncio.to_thread(entered.wait, 10)
        # In production the writer runs on the loop; it runs in a thread
        # here only so the test's loop stays free to release the gate.
        sync_write = asyncio.create_task(asyncio.to_thread(writer))
        await asyncio.sleep(0.05)
        assert not sync_write.done(), "writer did not wait for the file lock"
        gate.set()
        await append
        await sync_write

    _run(main())
    data = _history(chats_dir, "c1")
    # Neither write lost the other's change.
    assert [m.get("content") for m in data["messages"]] == [None, "during"]
    if writer is _set_flags:
        assert data["own_workspace"] is True
    elif writer is _set_guide:
        assert data["guide_id"] == "g1"
    else:
        assert data["messages"][0]["status"] == "approved"


# ---------------------------------------------------------------------------
# Compact, atomic files
# ---------------------------------------------------------------------------


def test_transcript_is_compact_json_without_temp_leftovers(chats):
    chats_dir, _ = chats
    _seed(chats_dir, "c1")
    _run(_cs().append_structured_messages("c1", [
        {"type": "tool_use", "tool_name": "x", "tool_input": {"a": [1, 2]}, "tool_id": "t1"},
    ]))
    raw = (chats_dir / "c1" / "chat_history.json").read_text()
    assert "\n" not in raw
    assert ", " not in raw and '": ' not in raw
    assert sorted(p.name for p in (chats_dir / "c1").iterdir()) == ["chat_history.json"]


def test_failed_replace_keeps_the_old_file_and_removes_the_temp(chats, monkeypatch):
    chats_dir, _ = chats
    _seed(chats_dir, "c1")
    _run(_cs().append_structured_messages("c1", [_text("kept")]))
    before = (chats_dir / "c1" / "chat_history.json").read_text()

    def _boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(storage_mod.os, "replace", _boom)
    with pytest.raises(OSError):
        _run(_cs().append_structured_messages("c1", [_text("lost")]))

    assert (chats_dir / "c1" / "chat_history.json").read_text() == before
    assert sorted(p.name for p in (chats_dir / "c1").iterdir()) == ["chat_history.json"]


# ---------------------------------------------------------------------------
# One metadata update per append
# ---------------------------------------------------------------------------


def test_each_append_records_its_metadata_in_one_call(chats):
    chats_dir, meta_calls = chats
    _seed(chats_dir, "c1")

    _run(_cs().append_message(
        "c1", "user", "%%flags[nested_subagents]\nSummarize the report",
        timestamp="2026-10-14T10:00:00Z",
    ))
    _run(_cs().append_structured_messages("c1", [
        {"type": "tool_use", "tool_name": "x", "tool_input": {}, "tool_id": "t1",
         "timestamp": "2026-10-14T10:00:01Z"},
        {"type": "tool_result", "tool_id": "t1", "tool_output": "ok",
         "timestamp": "2026-10-14T10:00:02Z"},
    ]))

    assert len(meta_calls) == 2
    (cid1, first), (cid2, second) = meta_calls
    assert cid1 == cid2 == "c1"
    assert first["last_message_seq"] == 1
    assert first["auto_title"] == "Summarize the report"
    assert first["last_message_at"].isoformat().startswith("2026-10-14T10:00:00")
    assert second["last_message_seq"] == 3
    assert second["auto_title"] is None
    assert second["last_message_at"].isoformat().startswith("2026-10-14T10:00:02")


def test_metadata_failure_does_not_fail_the_append(chats, monkeypatch):
    chats_dir, _ = chats
    _seed(chats_dir, "c1")
    import db.conversation_store as conv_store

    async def _boom(*_a, **_kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(conv_store, "record_messages_appended", _boom)
    seq, _msg = _run(_cs().append_message("c1", "user", "hi"))
    assert seq == 1
    assert _history(chats_dir, "c1")["messages"][0]["seq"] == 1


# ---------------------------------------------------------------------------
# Cancellation during a flush
# ---------------------------------------------------------------------------


def test_flush_cancelled_mid_write_lands_once(chats, gate_c1_writes, monkeypatch):
    chats_dir, _ = chats
    entered, gate = gate_c1_writes
    _seed(chats_dir, "c1")
    import chat._flush_helper as flush_mod

    published: list[list[int]] = []
    monkeypatch.setattr(
        flush_mod, "_publish_appended_to_bus",
        lambda _cid, appended: published.append([s for s, _ in appended]),
    )
    messages_out: list[dict] = [
        {"type": "tool_use", "tool_name": "x", "tool_input": {}, "tool_id": "t1"},
    ]
    flush, _lock = flush_mod.make_flush_callback(
        "c1", messages_out, log_prefix="[test]",
    )

    async def main():
        run = asyncio.create_task(flush())
        await asyncio.to_thread(entered.wait, 10)
        run.cancel()  # the user pressed Stop mid-write
        await asyncio.sleep(0.05)
        assert not run.done(), "the cancelled flush abandoned its write"
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        # The driver's cancel path: interrupted marker + final flush.
        messages_out.append({"type": "interrupted"})
        await flush()

    _run(main())
    on_disk = _history(chats_dir, "c1")["messages"]
    assert [m["type"] for m in on_disk] == ["tool_use", "interrupted"]
    assert [m["seq"] for m in on_disk] == [1, 2]
    assert published == [[1], [2]]
