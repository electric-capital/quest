"""Tests for converting a standalone conversation into a project.

Covers ``POST /projects/from-conversation`` and its building blocks:

* the route leaves the conversation's files in its own conversation
  workspace (``data/chats/{id}/workspace/``), creates an empty project
  workspace, flips ``conversations.project_id`` and sets the
  ``converted_from_standalone`` + ``own_workspace`` notice flags in
  chat_history.json (a flag write failure never fails the conversion);
* ``conversation_store.set_conversation_project`` -- attaches a standalone
  conversation to a project, refusing wrong-owner and already-in-project rows.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_storage(monkeypatch):
    """Point the store modules at a throwaway sqlite file + data dirs.

    Deliberately avoids importlib.reload(): reloading replaces class/function
    objects inside the shared modules, which breaks ``patch("chat.storage...")``
    targets for unrelated tests that run later in the same pytest session.
    Instead, swap the session factory and path constants on the already-loaded
    modules -- monkeypatch restores the originals on teardown.
    """
    tmpdir = tempfile.mkdtemp(prefix="quest_convert_project_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    chats_dir = os.path.join(tmpdir, "chats")
    projects_dir = os.path.join(tmpdir, "projects")
    os.makedirs(chats_dir, exist_ok=True)
    os.makedirs(projects_dir, exist_ok=True)

    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    test_session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.conversation_store as conv_store_mod
    import db.project_store as project_store_mod
    import chat.storage as storage_mod

    models_mod.Base.metadata.create_all(sync_engine)

    monkeypatch.setattr(conv_store_mod, "AsyncSessionLocal", test_session_local)
    monkeypatch.setattr(project_store_mod, "AsyncSessionLocal", test_session_local)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", Path(chats_dir), raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", Path(projects_dir), raising=True)

    yield storage_mod, conv_store_mod, project_store_mod, models_mod

    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _create_user(conv_store_mod, models_mod) -> int:
    # conv_store_mod.AsyncSessionLocal is the patched test factory.
    async with conv_store_mod.AsyncSessionLocal() as db:
        u = models_mod.User(
            email=f"conv-{uuid.uuid4().hex}@example.com",
            api_key=f"k-{uuid.uuid4().hex}",
            name="Convert Tester",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


@pytest.fixture()
def _seed(_isolated_storage):
    """Create a user, a standalone conversation, and a project row."""
    storage_mod, conv_store_mod, project_store_mod, models_mod = _isolated_storage

    async def _create():
        user_id = await _create_user(conv_store_mod, models_mod)
        conversation_id, _ = await storage_mod.ChatStorage.create_conversation(user_id)
        project = await project_store_mod.create_project(user_id, name="Converted")
        return user_id, conversation_id, project["id"]

    return _run(_create())


def _convert(user_id: int, conversation_id: str, name: str = "From Chat") -> dict:
    from chat.project_routes import (
        CreateProjectFromConversationRequest,
        create_project_from_conversation,
    )

    return _run(create_project_from_conversation(
        CreateProjectFromConversationRequest(name=name, conversation_id=conversation_id),
        {"id": user_id, "email": "convert@example.com"},
    ))


def test_from_conversation_leaves_files_in_conversation_workspace(_isolated_storage, _seed):
    storage_mod, conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, conversation_id, _unused_project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    _run(ChatStorage.append_message(conversation_id, "user", "hi"))
    ws = ChatStorage.get_conversation_workspace_root(conversation_id)
    (ws / "sub").mkdir(parents=True)
    (ws / "notes.md").write_text("hello")
    (ws / "sub" / "data.csv").write_text("a,b\n1,2")

    project = _convert(user_id, conversation_id)

    # Nothing moved: the files are still in the conversation workspace.
    assert (ws / "notes.md").read_text() == "hello"
    assert (ws / "sub" / "data.csv").read_text() == "a,b\n1,2"
    # The project workspace exists and is empty.
    project_root = ChatStorage.get_project_workspace_root(project["id"])
    assert project_root.is_dir()
    assert list(project_root.iterdir()) == []

    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["project_id"] == project["id"]
    assert ChatStorage.get_conversation_flags(conversation_id) == {
        "converted_from_standalone": True,
        "own_workspace": True,
    }
    # The flag write kept the history intact.
    history = ChatStorage.get_conversation(conversation_id)
    assert [m["content"] for m in history["messages"]] == ["hi"]


def test_from_conversation_creates_missing_conversation_workspace(_isolated_storage, _seed):
    storage_mod, _conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, conversation_id, _unused_project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    ws = ChatStorage.get_conversation_workspace_root(conversation_id)
    assert not ws.exists()  # a fresh standalone conversation has none yet

    _convert(user_id, conversation_id)

    assert ws.is_dir()
    assert ChatStorage.get_conversation_flags(conversation_id) == {
        "converted_from_standalone": True,
        "own_workspace": True,
    }


def test_set_conversation_project_keeps_conversation_workspace(_isolated_storage, _seed):
    storage_mod, conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, conversation_id, project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    updated = _run(conv_store_mod.set_conversation_project(
        user_id, conversation_id, project_id,
    ))
    assert updated is not None
    assert updated["project_id"] == project_id

    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["project_id"] == project_id

    # Project membership does not change where the conversation's files live.
    assert ChatStorage.get_conversation_workspace_root(conversation_id) == (
        storage_mod.CHATS_DIR / conversation_id / "workspace"
    )


def test_set_conversation_project_refuses_wrong_owner_and_reattach(_isolated_storage, _seed):
    storage_mod, conv_store_mod, project_store_mod, _ = _isolated_storage
    user_id, conversation_id, project_id = _seed

    # Wrong owner -> None, row untouched.
    assert _run(conv_store_mod.set_conversation_project(
        user_id + 999, conversation_id, project_id,
    )) is None
    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["project_id"] is None

    # Attach once, then a second attach (even to another project) is refused.
    assert _run(conv_store_mod.set_conversation_project(
        user_id, conversation_id, project_id,
    )) is not None
    other = _run(project_store_mod.create_project(user_id, name="Other"))
    assert _run(conv_store_mod.set_conversation_project(
        user_id, conversation_id, other["id"],
    )) is None
    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["project_id"] == project_id


def test_create_project_conversation_creates_conversation_workspace(_isolated_storage, _seed):
    storage_mod, conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, _conversation_id, project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    cid, _ = _run(ChatStorage.create_project_conversation(user_id, project_id))

    ws = ChatStorage.get_conversation_workspace_root(cid)
    assert ws == storage_mod.CHATS_DIR / cid / "workspace"
    assert ws.is_dir() and list(ws.iterdir()) == []
    # The explicit post-cutover marker, not the dir's presence.
    assert ChatStorage.get_conversation_flags(cid) == {"own_workspace": True}


def test_standalone_conversation_has_no_workspace_flags(_isolated_storage, _seed):
    storage_mod, _conv_store_mod, _project_store_mod, _ = _isolated_storage
    _user_id, conversation_id, _project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    assert ChatStorage.get_conversation_flags(conversation_id) == {}
    # Opening the conversation's files mkdirs its workspace as a side effect;
    # that must not read as a post-cutover marker.
    ChatStorage.get_conversation_workspace_root(conversation_id).mkdir(parents=True)
    assert ChatStorage.get_conversation_flags(conversation_id) == {}


def test_from_conversation_survives_corrupt_chat_history(_isolated_storage, _seed):
    storage_mod, conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, conversation_id, _unused_project_id = _seed
    ChatStorage = storage_mod.ChatStorage

    chat_file = storage_mod.CHATS_DIR / conversation_id / "chat_history.json"
    chat_file.write_text("{not json")

    project = _convert(user_id, conversation_id)

    meta = _run(conv_store_mod.get_conversation_meta(user_id, conversation_id))
    assert meta["project_id"] == project["id"]
    assert ChatStorage.get_conversation_workspace_root(conversation_id).is_dir()
    # The flag write was skipped; the file was left as it was.
    assert chat_file.read_text() == "{not json"
    assert ChatStorage.get_conversation_flags(conversation_id) == {}


def test_duplicate_workspace_of_project_conversation_copies_only_its_files(
    _isolated_storage, _seed,
):
    storage_mod, _conv_store_mod, _project_store_mod, _ = _isolated_storage
    user_id, _conversation_id, project_id = _seed
    ChatStorage = storage_mod.ChatStorage
    from chat.routes.conversations import duplicate_conversation_workspace

    src, _ = _run(ChatStorage.create_project_conversation(user_id, project_id))
    src_root = ChatStorage.get_conversation_workspace_root(src)
    (src_root / "mine.txt").write_text("conversation file")
    (src_root / ".responses").mkdir()
    (src_root / ".responses" / "blob.json").write_text("{}")
    project_root = ChatStorage.create_project_workspace(project_id)
    (project_root / "shared.txt").write_text("project file")

    result = _run(duplicate_conversation_workspace(
        src, {"id": user_id, "email": "convert@example.com"},
    ))

    dst_root = ChatStorage.get_conversation_workspace_root(result["id"])
    assert result["project_id"] is None
    assert sorted(p.name for p in dst_root.iterdir()) == ["mine.txt"]
    assert (dst_root / "mine.txt").read_text() == "conversation file"
