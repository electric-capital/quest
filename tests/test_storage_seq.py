"""Tests for per-conversation ``seq`` stamping in ``ChatStorage``.

Verifies the Phase 2 contract:

* ``append_message`` stamps a monotonic seq and returns ``(seq, message)``.
* ``append_structured_messages`` stamps each message in order and returns
  ``[(seq, message), ...]`` covering only the freshly-appended slice.
* The DB cache (``conversations.last_message_seq``) is advanced atomically
  alongside the file write, so the persistent-WS subscribe handler can
  read it without scanning the JSON file.
* Pre-migration files (no ``seq`` field on existing messages) are handled
  -- the next append numbers from ``len(messages) + 1``.
"""

import asyncio
import json
import uuid
from datetime import datetime

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_storage(isolated_db, tmp_path, monkeypatch):
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()

    from config import paths
    import chat.storage as storage_mod

    # chat.storage binds CHATS_DIR at import time, so patch its copy too.
    monkeypatch.setattr(paths, "CHATS_DIR", chats_dir, raising=True)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats_dir, raising=True)

    import db.conversation_store as conv_store_mod

    return storage_mod, conv_store_mod, isolated_db.models


@pytest.fixture()
def _seed_conversation(_isolated_storage):
    storage_mod, conv_store_mod, models_mod = _isolated_storage
    from db.engine import AsyncSessionLocal

    async def _create() -> tuple[int, str]:
        # Need a real user FK to satisfy conversations.user_id.
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"seq-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Seq Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            user_id = u.id

        conversation_id, _created_at = await storage_mod.ChatStorage.create_conversation(user_id)
        return user_id, conversation_id

    return _run(_create())


def test_append_message_stamps_monotonic_seq(_isolated_storage, _seed_conversation):
    storage_mod, conv_store_mod, _ = _isolated_storage
    user_id, conversation_id = _seed_conversation

    seq1, msg1 = _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", "hello",
    ))
    seq2, msg2 = _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", "again",
    ))

    assert seq1 == 1
    assert seq2 == 2
    assert msg1["seq"] == 1
    assert msg2["seq"] == 2

    cached = _run(conv_store_mod.get_last_message_seq(conversation_id))
    assert cached == 2


def test_append_structured_messages_returns_pairs(_isolated_storage, _seed_conversation):
    storage_mod, conv_store_mod, _ = _isolated_storage
    user_id, conversation_id = _seed_conversation

    pairs = _run(storage_mod.ChatStorage.append_structured_messages(
        conversation_id,
        [
            {"type": "tool_use", "tool_name": "x", "tool_input": {}, "tool_id": "t1"},
            {"type": "tool_result", "tool_id": "t1", "tool_output": "done"},
        ],
    ))

    assert [s for (s, _) in pairs] == [1, 2]
    assert all(m["seq"] == s for (s, m) in pairs)

    # File reflects the same seqs.
    chat_file = storage_mod.ChatStorage._get_chat_history_file(conversation_id)
    with open(chat_file, "r") as f:
        on_disk = json.load(f)
    on_disk_seqs = [m.get("seq") for m in on_disk["messages"]]
    assert on_disk_seqs == [1, 2]

    # DB cache mirrors the high-water mark.
    cached = _run(conv_store_mod.get_last_message_seq(conversation_id))
    assert cached == 2


def test_append_continues_across_calls(_isolated_storage, _seed_conversation):
    storage_mod, conv_store_mod, _ = _isolated_storage
    _user_id, conversation_id = _seed_conversation

    _run(storage_mod.ChatStorage.append_structured_messages(
        conversation_id,
        [{"type": "tool_use", "tool_name": "x", "tool_input": {}, "tool_id": "t1"}],
    ))
    pairs2 = _run(storage_mod.ChatStorage.append_structured_messages(
        conversation_id,
        [{"type": "tool_result", "tool_id": "t1", "tool_output": "ok"}],
    ))
    assert pairs2 == [(2, pairs2[0][1])]
    assert pairs2[0][1]["seq"] == 2
    assert _run(conv_store_mod.get_last_message_seq(conversation_id)) == 2


def test_pre_migration_file_seq_starts_after_existing_count(_isolated_storage, _seed_conversation):
    """A chat_history.json without ``seq`` fields (legacy state) must
    continue numbering from ``len(messages) + 1`` so seq remains
    unique within the conversation."""
    storage_mod, conv_store_mod, _ = _isolated_storage
    user_id, conversation_id = _seed_conversation

    # Manually write a "pre-migration" file with no seq field on entries.
    chat_file = storage_mod.ChatStorage._get_chat_history_file(conversation_id)
    chat_data = {
        "id": conversation_id,
        "user_id": user_id,
        "created_at": "2026-01-01T00:00:00Z",
        "messages": [
            {"role": "user", "content": "old1", "timestamp": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "old2", "timestamp": "2026-01-01T00:00:02Z"},
            {"role": "user", "content": "old3", "timestamp": "2026-01-01T00:00:03Z"},
        ],
    }
    with open(chat_file, "w") as f:
        json.dump(chat_data, f)

    seq, msg = _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", "fresh",
    ))
    # Legacy three messages -> next seq is 4.
    assert seq == 4
    assert msg["seq"] == 4
    assert _run(conv_store_mod.get_last_message_seq(conversation_id)) == 4


def test_first_message_with_flags_line_persists_verbatim_clean_title(
    _isolated_storage, _seed_conversation,
):
    """The persisted/displayed first message keeps the ``%%flags[...]`` magic
    line intact, but the cached sidebar auto-title is derived from a
    flags-stripped copy (so the title never starts with the magic syntax)."""
    storage_mod, conv_store_mod, _ = _isolated_storage
    user_id, conversation_id = _seed_conversation

    original = "%%flags[nested_subagents]\nResearch the quarterly numbers"
    seq, msg = _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", original,
    ))
    assert seq == 1

    # The persisted message content (what the FE displays) is verbatim -- the
    # magic line is preserved so it survives copy-paste.
    chat_file = storage_mod.ChatStorage._get_chat_history_file(conversation_id)
    with open(chat_file, "r") as f:
        on_disk = json.load(f)
    assert on_disk["messages"][0]["content"] == original

    # The cached sidebar title is the flags-stripped body, never the magic line.
    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["auto_title"] == "Research the quarterly numbers"
    assert "%%flags" not in meta["auto_title"]


def test_each_append_updates_the_row_in_one_db_session(
    _isolated_storage, _seed_conversation, monkeypatch,
):
    """Sidebar timestamp, seq cache and auto title share one session per
    append instead of one session each."""
    storage_mod, conv_store_mod, _ = _isolated_storage
    user_id, conversation_id = _seed_conversation

    real_session = conv_store_mod.AsyncSessionLocal
    opened: list[int] = []

    def _counting_session():
        opened.append(1)
        return real_session()

    monkeypatch.setattr(conv_store_mod, "AsyncSessionLocal", _counting_session)

    _run(storage_mod.ChatStorage.append_message(
        conversation_id, "user", "Plan the offsite", timestamp="2026-10-14T09:30:00Z",
    ))
    assert len(opened) == 1
    _run(storage_mod.ChatStorage.append_structured_messages(conversation_id, [
        {"type": "tool_use", "tool_name": "x", "tool_input": {}, "tool_id": "t1",
         "timestamp": "2026-10-14T09:30:01Z"},
        {"type": "text", "role": "user", "content": "A later user row",
         "timestamp": "2026-10-14T09:30:02Z"},
    ]))
    assert len(opened) == 2

    monkeypatch.setattr(conv_store_mod, "AsyncSessionLocal", real_session)
    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["last_message_seq"] == 3
    assert meta["auto_title"] == "Plan the offsite"  # set once, never overwritten
    assert meta["last_message_at"].startswith("2026-10-14T09:30:02")
