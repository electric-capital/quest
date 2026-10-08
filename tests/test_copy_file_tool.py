"""Tests for ``copy_file`` (chat/gemini_api/tool_handlers/workspace_copy.py).

``copy_entry`` itself is exhaustively tested with the copy routes; these
pin the tool contract on top of it: every direction incl. same-space
copies, mandatory schemes on ``src`` and ``dest``, ``destination_exists``
without ``overwrite``, file replace / folder merge with it, hidden-entry
skipping and ``include_hidden``, the scratch-root ban only toward
``proj://``, symlink / FIFO source refusals, ``no_project`` /
``invalid_project``, ``copy_failed`` on I/O errors, structured errors
(never raised), never moving, and ``file_list_changed`` for the
destination scope only.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import chat.storage as storage_mod
from chat.gemini_api.tool_handlers import _handle_copy_file

USER_ID = 7
CID = "conv"
PID = "proj"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)
    conv_root = chats / CID / "workspace"
    project_root = projects / PID / "workspace" / "workspace"
    conv_root.mkdir(parents=True)
    project_root.mkdir(parents=True)

    from chat.realtime import bus
    events = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: events.append(ev))
    return SimpleNamespace(conv=conv_root, project=project_root, events=events)


def _put(root, rel, content="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _copy(src, dest, *, project_id=PID, **kw):
    return json.loads(_run(_handle_copy_file(USER_ID, CID, project_id, src, dest, **kw)))


def _scopes(env):
    return [(e["scope"], e["conversation_id"], e["project_id"]) for e in env.events]


class TestDirections:
    def test_chat_to_proj(self, env):
        _put(env.conv, "out/report.pdf", "pdf")
        out = _copy("chat://out/report.pdf", "proj://out/report.pdf")
        assert out["type"] == "file" and out["path"] == "proj://out/report.pdf"
        assert out["files_copied"] == 1 and out["moved"] is False
        assert "project workspace" in out["message"]
        assert (env.project / "out" / "report.pdf").read_text() == "pdf"
        assert (env.conv / "out" / "report.pdf").exists()  # never moved
        assert _scopes(env) == [("project", CID, PID)]

    def test_proj_to_chat_renamed(self, env):
        _put(env.project, "etl.py", "print(1)")
        out = _copy("proj://etl.py", "chat://scripts/etl.py")
        assert out["path"] == "chat://scripts/etl.py"
        assert "conversation workspace" in out["message"]
        assert (env.conv / "scripts" / "etl.py").read_text() == "print(1)"
        assert (env.project / "etl.py").exists()
        assert _scopes(env) == [("conversation", CID, PID)]

    def test_same_space_chat(self, env):
        _put(env.conv, "a.txt", "a")
        assert _copy("chat://a.txt", "chat://b/a.txt")["path"] == "chat://b/a.txt"
        assert (env.conv / "b" / "a.txt").read_text() == "a"
        assert _scopes(env) == [("conversation", CID, PID)]

    def test_same_space_proj(self, env):
        _put(env.project, "d/x.txt")
        out = _copy("proj://d", "proj://e")
        assert out["type"] == "folder" and out["path"] == "proj://e"
        assert (env.project / "e" / "x.txt").exists()
        assert _scopes(env) == [("project", CID, PID)]

    def test_standalone_chat_to_chat(self, env):
        _put(env.conv, "a.txt")
        assert _copy("chat://a.txt", "chat://c.txt", project_id=None)["type"] == "file"


class TestOverwrite:
    def test_destination_exists_without_overwrite(self, env):
        _put(env.conv, "a.txt", "new")
        _put(env.project, "a.txt", "old")
        out = _copy("chat://a.txt", "proj://a.txt")
        assert out["code"] == "destination_exists" and "overwrite" in out["error"]
        assert (env.project / "a.txt").read_text() == "old"
        assert env.events == []

    def test_overwrite_replaces_file(self, env):
        _put(env.conv, "a.txt", "new")
        _put(env.project, "a.txt", "old")
        assert _copy("chat://a.txt", "proj://a.txt", overwrite=True)["type"] == "file"
        assert (env.project / "a.txt").read_text() == "new"

    def test_directory_merge(self, env):
        _put(env.project, "data/keep.csv", "keep")
        _put(env.project, "data/b.csv", "old")
        _put(env.conv, "data/b.csv", "new")
        _put(env.conv, "data/sub/c.csv", "c")
        assert _copy("chat://data", "proj://data")["code"] == "destination_exists"
        out = _copy("chat://data", "proj://data", overwrite=True)
        assert out["type"] == "folder" and out["files_copied"] == 2
        assert (env.project / "data" / "keep.csv").read_text() == "keep"
        assert (env.project / "data" / "b.csv").read_text() == "new"
        assert (env.project / "data" / "sub" / "c.csv").read_text() == "c"

    def test_type_mismatch(self, env):
        _put(env.conv, "x", "file")
        (env.project / "x").mkdir()
        assert _copy("chat://x", "proj://x", overwrite=True)["code"] == "invalid_destination"

    def test_self_and_subtree(self, env):
        _put(env.conv, "d/x.txt")
        assert _copy("chat://d", "chat://d", overwrite=True)["code"] == "invalid_destination"
        assert _copy("chat://d", "chat://d/inner")["code"] == "invalid_destination"


class TestHidden:
    def test_hidden_entries_skipped_by_default(self, env):
        _put(env.conv, "pkg/main.py")
        _put(env.conv, "pkg/.env", "secret")
        _put(env.conv, "pkg/.cache/x")
        out = _copy("chat://pkg", "proj://pkg")
        assert out["files_copied"] == 1 and out["skipped"] == 2
        assert "skipped" in out["message"]
        assert not (env.project / "pkg" / ".env").exists()

    def test_include_hidden(self, env):
        _put(env.conv, "pkg/main.py")
        _put(env.conv, "pkg/.env", "secret")
        assert _copy("chat://pkg", "proj://pkg", include_hidden=True)["files_copied"] == 2
        assert (env.project / "pkg" / ".env").read_text() == "secret"

    def test_hidden_named_explicitly(self, env):
        _put(env.project, ".config.json", "{}")
        assert _copy("proj://.config.json", "chat://.config.json")["files_copied"] == 1


class TestScratchBan:
    @pytest.mark.parametrize("src", [
        "chat://.responses/dump.json", "chat://.responses",
        "chat://.subagent_responses/x/y.md", "chat://pasted/image.png",
        "chat://pasted", "chat://./pasted/x.png",
    ])
    def test_scratch_source_toward_proj_refused(self, env, src):
        _put(env.conv, ".responses/dump.json")
        _put(env.conv, ".subagent_responses/x/y.md")
        _put(env.conv, "pasted/image.png")
        _put(env.conv, "pasted/x.png")
        _put(env.project, "pasted/x.png")
        out = _copy(src, "proj://kept")
        assert out["code"] == "forbidden_source"
        assert not (env.project / "kept").exists()
        assert env.events == []

    def test_project_scratch_named_folders_copy_within_project(self, env):
        _put(env.project, "pasted/x.png", "png")
        _put(env.project, ".responses/r.json", "{}")
        assert _copy("proj://pasted/x.png", "proj://y.png")["type"] == "file"
        assert _copy("proj://.responses", "proj://kept")["type"] == "folder"
        assert (env.project / "kept" / "r.json").exists()

    def test_standalone_scratch_toward_proj_is_no_project(self, env):
        _put(env.conv, "pasted/x.png")
        assert _copy("chat://pasted/x.png", "proj://y", project_id=None)["code"] == \
            "no_project"

    def test_scratch_source_toward_chat_allowed(self, env):
        _put(env.conv, "pasted/image.png", "png")
        _put(env.project, "pasted/p.png", "png")
        assert _copy("chat://pasted/image.png", "chat://keep.png")["type"] == "file"
        assert _copy("proj://pasted/p.png", "chat://p.png")["type"] == "file"


class TestRefusals:
    @pytest.mark.parametrize("src,dest", [
        ("a.txt", "proj://a.txt"), ("chat://a.txt", "a.txt"),
        ("chat://a.txt", None), (None, "proj://a.txt"), ("chat://a.txt", 5),
        ("chat://../x", "proj://x"), ("chat://a.txt", "proj://../x"),
        ("chat://a.txt", "proj://"), ("chat://", "proj://x"),
        ("chat://a.txt", "proj:///abs"), ("project://a.txt", "chat://b"),
    ])
    def test_invalid_path(self, env, src, dest):
        _put(env.conv, "a.txt")
        out = _copy(src, dest)
        assert out["code"] == "invalid_path"
        assert "chat://" in out["error"] and "proj://" in out["error"]
        assert out["error"].startswith(("src: ", "dest: "))
        assert env.events == []

    def test_not_found(self, env):
        assert _copy("chat://missing.txt", "proj://m.txt")["code"] == "not_found"
        assert _copy("proj://missing.txt", "chat://m.txt")["code"] == "not_found"

    def test_symlink_source(self, env):
        _put(env.conv, "real.txt", "data")
        os.symlink(env.conv / "real.txt", env.conv / "link.txt")
        assert _copy("chat://link.txt", "proj://link.txt")["code"] == "not_a_regular_file"
        assert not os.path.lexists(env.project / "link.txt")

    def test_symlink_escaping_root(self, env, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        os.symlink(outside, env.conv / "link.txt")
        assert _copy("chat://link.txt", "proj://link.txt")["code"] == "invalid_path"

    def test_fifo_source(self, env):
        os.mkfifo(env.project / "pipe")
        assert _copy("proj://pipe", "chat://pipe")["code"] == "not_a_regular_file"
        assert not os.path.lexists(env.conv / "pipe")

    def test_symlink_inside_directory_skipped(self, env, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        _put(env.conv, "dir/real.txt")
        os.symlink(outside, env.conv / "dir" / "link.txt")
        out = _copy("chat://dir", "proj://dir")
        assert out["files_copied"] == 1 and out["skipped"] == 1
        assert not os.path.lexists(env.project / "dir" / "link.txt")

    @pytest.mark.parametrize("src,dest", [
        ("proj://a.txt", "chat://a.txt"), ("chat://a.txt", "proj://a.txt"),
    ])
    def test_no_project(self, env, src, dest):
        _put(env.conv, "a.txt")
        assert _copy(src, dest, project_id=None)["code"] == "no_project"
        assert env.events == []

    def test_invalid_project(self, env):
        _put(env.conv, "a.txt")
        assert _copy("chat://a.txt", "proj://a.txt", project_id="../evil")["code"] == \
            "invalid_project"

    def test_os_error_is_copy_failed(self, env, monkeypatch):
        import chat.gemini_api.tool_handlers.workspace_copy as copy_mod

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(copy_mod, "copy_entry", boom)
        _put(env.conv, "a.txt")
        out = _copy("chat://a.txt", "proj://a.txt")
        assert out["code"] == "copy_failed" and "No space left" in out["error"]
        assert _scopes(env) == [("project", CID, PID)]
