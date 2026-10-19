"""``ChatStorage.search_conversations`` over seeded chat_history.json files.

Covers the ``include_routines`` switch behind ``GET /app/api/search``: with
it off (the search modal's default) conversations a routine created are not
scanned at all, so a daily routine cannot repeat the same hit once per run;
with it on they are searched and each hit carries its ``routine_id`` so the
UI can badge it. Archived conversations are searched either way.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

import chat.storage as storage_mod


def _run(coro):
    return asyncio.run(coro)


BASE_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    import db.conversation_store as store

    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats", raising=True)

    async def _seed_user_and_routine():
        models = isolated_db.models
        async with store.AsyncSessionLocal() as db:
            db.add(models.User(id=1, email="a@example.com", api_key="key-1"))
            await db.commit()
        async with store.AsyncSessionLocal() as db:
            db.add(models.Project(
                id="proj", user_id=1, name="P",
                created_at=BASE_TS, updated_at=BASE_TS,
            ))
            await db.commit()
        async with store.AsyncSessionLocal() as db:
            db.add(models.Routine(
                id="rtn", project_id="proj", user_id=1, name="R", prompt="p",
            ))
            await db.commit()
    _run(_seed_user_and_routine())
    return store


def _seed_conversation(store, text, *, routine_id=None, archived=False):
    conv_id = str(uuid.uuid4())
    _run(store.create_conversation(
        1, conv_id, BASE_TS,
        project_id="proj" if routine_id else None, routine_id=routine_id,
    ))
    if archived:
        _run(store.archive_conversation(1, conv_id))
    conv_dir = storage_mod.ChatStorage.get_conversation_dir(conv_id)
    conv_dir.mkdir(parents=True)
    (conv_dir / "chat_history.json").write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "hello", "timestamp": BASE_TS.isoformat()},
            {"role": "assistant", "content": text, "timestamp": BASE_TS.isoformat()},
        ],
    }))
    return conv_id


def test_routine_runs_skipped_unless_included(env):
    store = env
    chat = _seed_conversation(store, "the quarterly report is ready")
    run = _seed_conversation(store, "quarterly report, run 1", routine_id="rtn")
    archived = _seed_conversation(store, "old quarterly notes", archived=True)
    _seed_conversation(store, "nothing relevant here")

    search = storage_mod.ChatStorage.search_conversations

    default = _run(search(1, "quarterly"))
    assert default["total_matches"] == 2
    assert {r["conversation_id"] for r in default["results"]} == {chat, archived}
    assert all(r["routine_id"] is None for r in default["results"])

    with_runs = _run(search(1, "quarterly", include_routines=True))
    assert with_runs["total_matches"] == 3
    by_id = {r["conversation_id"]: r for r in with_runs["results"]}
    assert set(by_id) == {chat, run, archived}
    assert by_id[run]["routine_id"] == "rtn"
    assert by_id[archived]["archived"] is True
