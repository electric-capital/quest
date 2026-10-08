"""Per-conversation workspace storage primitives (issue #70, phase 1).

* ``file_storage.validate_path(root, rel)`` takes the browsable root itself,
  for both the conversation and the project workspace root;
* ``ChatStorage.copy_workspace_files(src_root, dst_root)`` copies between two
  roots, skipping the root ``.responses`` dir, symlinks and special files;
* ``ChatStorage.get_conversation_flags`` / ``set_conversation_flag`` over the
  top level of chat_history.json.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import chat.storage as storage_mod
from chat.file_storage import list_workspace_files, save_uploaded_file, validate_path


def _cs():
    # Other suites reload chat.storage; bind the class at call time.
    return storage_mod.ChatStorage


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)
    return chats, projects


def _both_roots():
    return [
        lambda: _cs().get_conversation_workspace_root("conv"),
        lambda: _cs().get_project_workspace_root("proj"),
    ]


# ---------------------------------------------------------------------------
# validate_path against both roots
# ---------------------------------------------------------------------------


class TestValidatePathRoots:
    @pytest.mark.parametrize("make_root", _both_roots(), ids=["conversation", "project"])
    def test_accepts_paths_inside_the_root(self, roots, make_root):
        root = make_root()
        for rel in ("", "/", "."):
            ok, resolved = validate_path(root, rel)
            assert ok and resolved == root
        assert root.is_dir()  # created on first use

        ok, resolved = validate_path(root, "sub/notes.md")
        assert ok and resolved == (root / "sub" / "notes.md").resolve()
        # A leading slash means "the root", not the filesystem root.
        ok, resolved = validate_path(root, "/notes.md")
        assert ok and resolved == (root / "notes.md").resolve()

    @pytest.mark.parametrize("make_root", _both_roots(), ids=["conversation", "project"])
    def test_rejects_traversal_and_escapes(self, roots, make_root, tmp_path):
        root = make_root()
        for rel in ("..", "../x", "sub/../../x", "a/..", "//../etc/passwd"):
            assert validate_path(root, rel) == (False, None), rel

        # A symlink inside the root pointing outside it is refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        assert validate_path(root, "escape/secret.txt") == (False, None)

    def test_nothing_is_appended_to_the_root(self, roots):
        chats, projects = roots
        conv_root = _cs().get_conversation_workspace_root("conv")
        _run(save_uploaded_file(conv_root, "a.txt", b"conv"))
        assert (chats / "conv" / "workspace" / "a.txt").read_bytes() == b"conv"
        assert not (chats / "conv" / "workspace" / "workspace").exists()

        project_root = _cs().get_project_workspace_root("proj")
        _run(save_uploaded_file(project_root, "b.txt", b"proj"))
        assert (projects / "proj" / "workspace" / "workspace" / "b.txt").read_bytes() == b"proj"
        listing = list_workspace_files(project_root)
        assert listing["currentPath"] == "/"
        assert [f["name"] for f in listing["files"]] == ["b.txt"]


# ---------------------------------------------------------------------------
# copy_workspace_files
# ---------------------------------------------------------------------------


class TestCopyWorkspaceFiles:
    def test_copies_tree_and_skips_responses_symlinks_and_fifos(self, roots, tmp_path):
        src = _cs().get_conversation_workspace_root("src")
        dst = _cs().get_conversation_workspace_root("dst")
        (src / "sub" / ".responses").mkdir(parents=True)
        (src / ".responses").mkdir()
        (src / ".responses" / "blob.json").write_text("{}")
        (src / "sub" / ".responses" / "kept.json").write_text("[]")
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("b")
        (src / ".hidden").write_text("h")
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        (src / "leak.txt").symlink_to(secret)
        os.mkfifo(src / "sub" / "pipe")

        _run(_cs().copy_workspace_files(src, dst))

        assert (dst / "a.txt").read_text() == "a"
        assert (dst / "sub" / "b.txt").read_text() == "b"
        assert (dst / ".hidden").read_text() == "h"
        # Only the ROOT .responses is skipped.
        assert not (dst / ".responses").exists()
        assert (dst / "sub" / ".responses" / "kept.json").read_text() == "[]"
        assert not (dst / "leak.txt").exists() and not (dst / "leak.txt").is_symlink()
        assert not (dst / "sub" / "pipe").exists()

    def test_merges_into_existing_destination(self, roots):
        src = _cs().get_conversation_workspace_root("src")
        dst = _cs().get_project_workspace_root("proj")
        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (src / "new.txt").write_text("new")
        (dst / "old.txt").write_text("old")

        _run(_cs().copy_workspace_files(src, dst))

        assert sorted(p.name for p in dst.iterdir()) == ["new.txt", "old.txt"]

    def test_missing_source_is_a_noop(self, roots):
        dst = _cs().get_conversation_workspace_root("dst")
        _run(_cs().copy_workspace_files(_cs().get_conversation_workspace_root("src"), dst))
        assert not dst.exists()


# ---------------------------------------------------------------------------
# Conversation notice flags
# ---------------------------------------------------------------------------


def _write_history(chats, cid, data):
    conv = chats / cid
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "chat_history.json").write_text(json.dumps(data))


class TestConversationFlags:
    def test_round_trip_preserves_messages_and_other_keys(self, roots):
        chats, _ = roots
        messages = [{"role": "user", "content": "hi", "seq": 1}]
        _write_history(chats, "c1", {
            "id": "c1", "user_id": 7, "project_id": "p1", "messages": messages,
        })
        assert _cs().get_conversation_flags("c1") == {}

        _cs().set_conversation_flag("c1", "converted_from_standalone", True)
        _cs().set_conversation_flag("c1", "legacy_shared_workspace", True)
        assert _cs().get_conversation_flags("c1") == {
            "converted_from_standalone": True,
            "legacy_shared_workspace": True,
        }

        data = json.loads((chats / "c1" / "chat_history.json").read_text())
        assert data["messages"] == messages
        assert data["user_id"] == 7 and data["project_id"] == "p1"

        _cs().set_conversation_flag("c1", "legacy_shared_workspace", False)
        assert _cs().get_conversation_flags("c1")["legacy_shared_workspace"] is False

    def test_messages_appended_after_a_flag_keep_it(self, roots, monkeypatch):
        import db.conversation_store as conv_store

        async def _noop(*args, **kwargs):
            return None

        for name in (
            "create_conversation", "update_last_message_at",
            "update_last_message_seq", "set_conversation_auto_title",
        ):
            monkeypatch.setattr(conv_store, name, _noop)
        monkeypatch.setattr(storage_mod, "_publish_appended_to_bus", lambda *a: None)

        cid, _ = _run(_cs().create_conversation(1))
        _cs().set_conversation_flag(cid, "converted_from_standalone", True)
        _run(_cs().append_message(cid, "user", "later"))
        assert _cs().get_conversation_flags(cid) == {"converted_from_standalone": True}
        assert [m["content"] for m in _cs().get_conversation(cid)["messages"]] == ["later"]

    def test_unknown_flag_rejected(self, roots):
        chats, _ = roots
        _write_history(chats, "c1", {"id": "c1", "messages": []})
        for name in ("messages", "guide_id", "nested_subagents", ""):
            with pytest.raises(ValueError):
                _cs().set_conversation_flag("c1", name, True)
        data = json.loads((chats / "c1" / "chat_history.json").read_text())
        assert data == {"id": "c1", "messages": []}

    def test_unknown_top_level_keys_are_not_reported(self, roots):
        chats, _ = roots
        _write_history(chats, "c1", {"id": "c1", "messages": [], "guide_id": "g"})
        assert _cs().get_conversation_flags("c1") == {}

    def test_missing_history(self, roots):
        chats, _ = roots
        assert _cs().get_conversation_flags("nope") == {}
        _cs().set_conversation_flag("nope", "legacy_shared_workspace", True)
        assert not (chats / "nope").exists()

    def test_unreadable_history_reads_as_no_flags(self, roots):
        chats, _ = roots
        (chats / "c1").mkdir(parents=True)
        (chats / "c1" / "chat_history.json").write_text("{not json")
        assert _cs().get_conversation_flags("c1") == {}

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]", "null"])
    def test_set_on_corrupt_history_is_a_noop(self, roots, content):
        chats, _ = roots
        (chats / "c1").mkdir(parents=True)
        chat_file = chats / "c1" / "chat_history.json"
        chat_file.write_text(content)
        _cs().set_conversation_flag("c1", "own_workspace", True)
        assert chat_file.read_text() == content


# ---------------------------------------------------------------------------
# Root resolver containment
# ---------------------------------------------------------------------------


class TestRootContainment:
    def test_conversation_root_symlinked_to_another_conversation_is_refused(self, roots):
        chats, _ = roots
        victim = chats / "B" / "workspace"
        victim.mkdir(parents=True)
        (chats / "A").mkdir()
        (chats / "A" / "workspace").symlink_to(victim, target_is_directory=True)
        with pytest.raises(storage_mod.InvalidStorageIdError):
            _cs().get_conversation_workspace_root("A")
        assert _cs().get_conversation_workspace_root("B") == victim

    @pytest.mark.parametrize("level", ["outer", "inner"])
    def test_project_root_symlinked_to_another_project_is_refused(self, roots, level):
        _, projects = roots
        victim = projects / "Q" / "workspace" / "workspace"
        victim.mkdir(parents=True)
        (projects / "P").mkdir()
        if level == "outer":
            (projects / "P" / "workspace").symlink_to(
                projects / "Q" / "workspace", target_is_directory=True,
            )
        else:
            (projects / "P" / "workspace").mkdir()
            (projects / "P" / "workspace" / "workspace").symlink_to(
                victim, target_is_directory=True,
            )
        with pytest.raises(storage_mod.InvalidStorageIdError):
            _cs().get_project_workspace_root("P")
        assert _cs().get_project_workspace_root("Q") == victim


# ---------------------------------------------------------------------------
# Tool-side resolution (conversation_workspace_dir / project_workspace_dir)
# ---------------------------------------------------------------------------


class TestToolWorkspaceDir:
    def test_conversation_dir_is_conversation_root(self, roots):
        from chat.gemini_api.tool_handlers._common import conversation_workspace_dir

        chats, projects = roots
        path = _run(conversation_workspace_dir("conv"))
        assert path == chats / "conv" / "workspace" and path.is_dir()
        assert not projects.exists()

    def test_project_dir_is_project_root(self, roots):
        from chat.gemini_api.tool_handlers._common import project_workspace_dir

        chats, projects = roots
        path = _run(project_workspace_dir("proj"))
        assert path == projects / "proj" / "workspace" / "workspace" and path.is_dir()
        assert not chats.exists()

    def test_file_list_changed_scope_is_explicit(self, roots, monkeypatch):
        from chat.gemini_api.tool_handlers import _common
        from chat.realtime import bus

        published = []
        monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: published.append(ev))
        _common._publish_file_list_changed(1, "conversation", "conv", "proj")
        _common._publish_file_list_changed(1, "project", "conv", "proj")
        _common._publish_file_list_changed(1, "conversation", "solo", None)
        assert [(e["scope"], e["conversation_id"], e["project_id"]) for e in published] == [
            ("conversation", "conv", "proj"),
            ("project", "conv", "proj"),
            ("conversation", "solo", None),
        ]
