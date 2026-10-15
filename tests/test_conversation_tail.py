"""Tests for ``GET /conversations/{id}/tail`` (``get_conversation_tail``).

Every open tab calls the route on each ``message_appended`` event, so it
answers from the realtime replay buffer when the buffer provably holds the
requested rows and reads ``chat_history.json`` in a worker thread otherwise.
Covers:

* the buffer path answers without reading the file;
* both paths return identical rows for every ``after_seq`` on the same data;
* the file fallback (cold buffer, ``after_seq`` older than the buffer, a seq
  the buffer never saw, durable rows newer than the buffer, a rewritten
  ``action_request`` card) runs off the event loop and is correct;
* ownership is enforced before either source is consulted.
"""

import asyncio
import importlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(monkeypatch):
    """Throwaway sqlite + chats dir, and a fresh replay buffer.

    Patches attributes instead of reloading modules (see
    ``tests/test_convert_conversation_to_project.py`` for why). The fresh
    buffer replaces both the module singleton (which the storage append
    paths import at call time) and the route module's own reference.
    """
    tmpdir = tempfile.mkdtemp(prefix="quest_conversation_tail_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    chats_dir = os.path.join(tmpdir, "chats")
    os.makedirs(chats_dir, exist_ok=True)

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.conversation_store as conv_store_mod
    import chat.storage as storage_mod
    import chat.routes.conversations as routes_mod
    # The package re-exports the singleton under the submodule's name, so
    # ``import chat.realtime.replay_buffer as m`` would bind the instance.
    rb_mod = importlib.import_module("chat.realtime.replay_buffer")

    models_mod.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(conv_store_mod, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", Path(chats_dir), raising=True)

    buffer = rb_mod.ReplayBuffer()
    monkeypatch.setattr(rb_mod, "replay_buffer", buffer, raising=True)
    monkeypatch.setattr(routes_mod, "replay_buffer", buffer, raising=True)

    # Record every chat_history.json read the route makes, and the thread
    # it ran on, so tests can tell the buffer path from the file path.
    reads: list[str] = []
    original_get = storage_mod.ChatStorage.get_conversation

    def spy_get_conversation(conversation_id):
        reads.append(threading.current_thread().name)
        return original_get(conversation_id)

    monkeypatch.setattr(
        storage_mod.ChatStorage, "get_conversation",
        staticmethod(spy_get_conversation),
    )

    class Env:
        storage = storage_mod
        routes = routes_mod
        models = models_mod
        conv_store = conv_store_mod

    e = Env()
    e.buffer = buffer
    e.reads = reads
    e.original_get = original_get
    e.rb_mod = rb_mod
    e.monkeypatch = monkeypatch

    yield e

    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _create_user(env) -> int:
    async with env.conv_store.AsyncSessionLocal() as db:
        u = env.models.User(
            email=f"tail-{uuid.uuid4().hex}@example.com",
            api_key=f"k-{uuid.uuid4().hex}",
            name="Tail Tester",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


def _seed(env) -> tuple[int, str, list[dict]]:
    """A user, a conversation, and six rows written through the real
    append paths (single-message + flush callback), as a turn does.

    Returns ``(user_id, conversation_id, messages_out)``; the caller may
    mutate ``messages_out`` afterwards the way the turn loop's shared list
    outlives a flush.
    """
    from chat._flush_helper import make_flush_callback

    async def _go():
        user_id = await _create_user(env)
        cid, _ = await env.storage.ChatStorage.create_conversation(user_id)
        await env.storage.ChatStorage.append_message(cid, "user", "hello")

        messages_out: list[dict] = []
        flush, _lock = make_flush_callback(cid, messages_out, log_prefix="[test]")
        messages_out.append({
            "type": "tool_use", "tool_name": "lookup", "tool_id": "t1",
            # A tuple is stored as a JSON list; both paths must agree.
            "tool_input": {"ids": (1, 2), "query": "café"},
        })
        await flush()
        messages_out.append({
            "type": "tool_result", "tool_id": "t1",
            "tool_output": {"rows": [{"a": 1}], "note": None},
        })
        messages_out.append({"type": "text", "role": "assistant", "content": "done"})
        await flush()
        messages_out.append({"type": "stats", "role": "assistant", "input_tokens": 10})
        await flush()
        await env.storage.ChatStorage.append_message(cid, "user", "again")
        return user_id, cid, messages_out

    return _run(_go())


def _tail(env, user_id: int, cid: str, after_seq: int) -> dict:
    return _run(env.routes.get_conversation_tail(
        cid, after_seq=after_seq, user={"id": user_id},
    ))


def _file_rows(env, cid: str, after_seq: int) -> list[dict]:
    """The ``seq > after_seq`` rows straight from chat_history.json."""
    data = env.original_get(cid)
    return [m for m in data["messages"] if m["seq"] > after_seq]


# ---------------------------------------------------------------------------
# Buffer path
# ---------------------------------------------------------------------------

def test_tail_served_from_buffer_without_reading_file(env):
    user_id, cid, _ = _seed(env)

    resp = _tail(env, user_id, cid, after_seq=2)

    assert [m["seq"] for m in resp["messages"]] == [3, 4, 5, 6]
    assert resp["last_message_seq"] == 6
    assert resp["messages"] == _file_rows(env, cid, 2)
    assert env.reads == [], "a covered tail must not read chat_history.json"


def test_tail_up_to_date_served_from_buffer(env):
    user_id, cid, _ = _seed(env)

    resp = _tail(env, user_id, cid, after_seq=6)

    assert resp == {"messages": [], "last_message_seq": 6}
    assert env.reads == []


def test_buffer_and_file_paths_return_identical_responses(env):
    """Same data, every after_seq: the buffer answer equals the file answer.

    The turn loop's dicts are mutated after the flush to prove the buffer
    holds what was written, not a live reference.
    """
    user_id, cid, messages_out = _seed(env)
    messages_out[0]["tool_input"]["query"] = "mutated after flush"
    messages_out[2]["content"] = "mutated after flush"

    from_buffer = {s: _tail(env, user_id, cid, s) for s in range(0, 8)}
    assert env.reads == []

    # Drop the buffer: every request now reads the file.
    cold = env.rb_mod.ReplayBuffer()
    env.monkeypatch.setattr(env.routes, "replay_buffer", cold, raising=True)
    from_file = {s: _tail(env, user_id, cid, s) for s in range(0, 8)}
    assert len(env.reads) == 8

    for after_seq in range(0, 8):
        assert from_buffer[after_seq] == from_file[after_seq], after_seq
        # Byte-identical once serialised, as the client receives it.
        assert json.dumps(from_buffer[after_seq]) == json.dumps(from_file[after_seq])
    assert from_buffer[0]["messages"][1]["tool_input"] == {
        "ids": [1, 2], "query": "café",
    }


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------

def test_cold_buffer_reads_file_in_worker_thread(env):
    user_id, cid, _ = _seed(env)
    cold = env.rb_mod.ReplayBuffer()
    env.monkeypatch.setattr(env.routes, "replay_buffer", cold, raising=True)

    resp = _tail(env, user_id, cid, after_seq=4)

    assert resp == {"messages": _file_rows(env, cid, 4), "last_message_seq": 6}
    assert len(env.reads) == 1
    assert env.reads[0] != threading.main_thread().name, (
        "the fallback read must run off the event loop"
    )


def test_after_seq_older_than_buffer_reads_file(env):
    small = env.rb_mod.ReplayBuffer(maxlen=3)
    env.monkeypatch.setattr(env.rb_mod, "replay_buffer", small, raising=True)
    env.monkeypatch.setattr(env.routes, "replay_buffer", small, raising=True)
    user_id, cid, _ = _seed(env)  # buffer keeps seqs 4..6

    resp = _tail(env, user_id, cid, after_seq=1)
    assert [m["seq"] for m in resp["messages"]] == [2, 3, 4, 5, 6]
    assert resp["messages"] == _file_rows(env, cid, 1)
    assert len(env.reads) == 1

    # Inside the window the buffer still answers.
    resp = _tail(env, user_id, cid, after_seq=3)
    assert [m["seq"] for m in resp["messages"]] == [4, 5, 6]
    assert len(env.reads) == 1


def test_unpublished_append_forces_file_read(env):
    """An append that writes the file without publishing (the scheduler's
    path) leaves a hole in the buffer; a tail spanning it must not skip it."""
    user_id, cid, _ = _seed(env)
    ChatStorage = env.storage.ChatStorage

    # Durable row newer than the buffer: DB high-water 7, buffer ends at 6.
    _run(ChatStorage.append_structured_messages(
        cid, [{"type": "text", "role": "assistant", "content": "unpublished"}],
    ))
    resp = _tail(env, user_id, cid, after_seq=5)
    assert [m["seq"] for m in resp["messages"]] == [6, 7]
    assert resp["last_message_seq"] == 7
    assert len(env.reads) == 1

    # A published append after it: the buffer holds 6 and 8 but not 7.
    _run(ChatStorage.append_message(cid, "user", "published"))
    resp = _tail(env, user_id, cid, after_seq=5)
    assert [m["seq"] for m in resp["messages"]] == [6, 7, 8]
    assert resp["messages"] == _file_rows(env, cid, 5)
    assert len(env.reads) == 2

    # A tail that starts past the hole is served from the buffer.
    resp = _tail(env, user_id, cid, after_seq=7)
    assert [m["seq"] for m in resp["messages"]] == [8]
    assert len(env.reads) == 2


def test_rewritten_action_request_card_read_from_file(env):
    """Resolving an action request rewrites its card on disk; the buffered
    copy still says pending, so a tail holding the card reads the file."""
    from chat._flush_helper import make_flush_callback

    user_id, cid, _ = _seed(env)

    async def _card():
        messages_out = [{
            "type": "action_request", "role": "assistant",
            "request_id": 41, "status": "pending",
        }]
        flush, _ = make_flush_callback(cid, messages_out, log_prefix="[test]")
        await flush()

    _run(_card())
    env.storage.ChatStorage.update_action_request_message(
        cid, 41, status="denied", feedback="not now",
    )

    resp = _tail(env, user_id, cid, after_seq=6)
    assert len(resp["messages"]) == 1
    card = resp["messages"][0]
    assert card["seq"] == 7
    assert card["status"] == "denied"
    assert card["feedback"] == "not now"
    assert len(env.reads) == 1

    # Later rows that do not include the card come from the buffer again.
    _run(env.storage.ChatStorage.append_message(cid, "user", "after"))
    resp = _tail(env, user_id, cid, after_seq=7)
    assert [m["seq"] for m in resp["messages"]] == [8]
    assert len(env.reads) == 1


def test_torn_threaded_read_is_retried_on_the_loop(env):
    """A parse error in the worker thread (a concurrent writer caught
    mid-write) is retried once on the event loop."""
    user_id, cid, _ = _seed(env)
    cold = env.rb_mod.ReplayBuffer()
    env.monkeypatch.setattr(env.routes, "replay_buffer", cold, raising=True)
    main = threading.main_thread().name

    def flaky_get(conversation_id):
        env.reads.append(threading.current_thread().name)
        if threading.current_thread().name != main:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return env.original_get(conversation_id)

    env.monkeypatch.setattr(
        env.storage.ChatStorage, "get_conversation", staticmethod(flaky_get),
    )

    resp = _tail(env, user_id, cid, after_seq=5)

    assert resp == {"messages": _file_rows(env, cid, 5), "last_message_seq": 6}
    assert len(env.reads) == 2 and env.reads[0] != main and env.reads[1] == main


def test_missing_transcript_file_is_404(env):
    user_id, cid, _ = _seed(env)
    cold = env.rb_mod.ReplayBuffer()
    env.monkeypatch.setattr(env.routes, "replay_buffer", cold, raising=True)
    env.storage.ChatStorage._get_chat_history_file(cid).unlink()

    with pytest.raises(HTTPException) as exc:
        _tail(env, user_id, cid, after_seq=0)
    assert exc.value.status_code == 404
    assert exc.value.detail["error"] == "conversation_not_found"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_other_users_conversation_is_404_before_buffer_or_file(env):
    _owner, cid, _ = _seed(env)
    intruder = _run(_create_user(env))
    assert env.buffer.latest_seq(cid) == 6  # the buffer could answer

    consulted: list[str] = []
    original_slice = env.buffer.contiguous_slice

    def spy_slice(*args, **kwargs):
        consulted.append("buffer")
        return original_slice(*args, **kwargs)

    env.monkeypatch.setattr(env.buffer, "contiguous_slice", spy_slice)

    with pytest.raises(HTTPException) as exc:
        _tail(env, intruder, cid, after_seq=0)

    assert exc.value.status_code == 404
    assert exc.value.detail["error"] == "conversation_not_found"
    assert consulted == [], "the buffer must not be consulted for a non-owner"
    assert env.reads == []


def test_unknown_conversation_is_404(env):
    user_id, _cid, _ = _seed(env)

    with pytest.raises(HTTPException) as exc:
        _tail(env, user_id, str(uuid.uuid4()), after_seq=0)
    assert exc.value.status_code == 404
    assert env.reads == []
