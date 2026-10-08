"""Tests for the copy tools between a project conversation's two file
spaces (devplan 00009 section 6.3): ``copy_file_to_project`` and
``copy_project_file`` in ``chat/gemini_api/tool_handlers/workspace_copy.py``.

Both run ``chat.file_storage.copy_entry`` (exhaustively tested with the copy
routes); these tests pin the tool contract on top of it: both directions,
``dest`` defaulting to ``path``, ``destination_exists`` without
``overwrite``, file replace / directory merge with it, hidden-entry
skipping and ``include_hidden``, the scratch-source refusal on
copy_file_to_project only, symlink / FIFO source refusals, structured
errors (never raised), never moving, and ``file_list_changed`` for the
destination scope only.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import chat.storage as storage_mod
from chat.gemini_api.tool_handlers import (
    _handle_copy_file_to_project,
    _handle_copy_project_file,
)

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


def _to_project(path, **kw):
    return json.loads(_run(_handle_copy_file_to_project(USER_ID, CID, PID, path, **kw)))


def _from_project(path, **kw):
    return json.loads(_run(_handle_copy_project_file(USER_ID, CID, PID, path, **kw)))


def _scopes(env):
    return [(e["scope"], e["conversation_id"], e["project_id"]) for e in env.events]


class TestDirections:
    def test_file_to_project_default_dest(self, env):
        _put(env.conv, "out/report.pdf", "pdf")
        out = _to_project("out/report.pdf")
        assert out["type"] == "file" and out["path"] == "/out/report.pdf"
        assert out["files_copied"] == 1 and out["moved"] is False
        assert "project workspace" in out["message"]
        assert (env.project / "out" / "report.pdf").read_text() == "pdf"
        assert (env.conv / "out" / "report.pdf").exists()  # never moved
        assert _scopes(env) == [("project", CID, PID)]

    def test_file_from_project_with_dest(self, env):
        _put(env.project, "etl.py", "print(1)")
        out = _from_project("etl.py", dest="scripts/etl.py")
        assert out["path"] == "/scripts/etl.py"
        assert (env.conv / "scripts" / "etl.py").read_text() == "print(1)"
        assert (env.project / "etl.py").exists()
        assert _scopes(env) == [("conversation", CID, PID)]

    def test_blank_dest_defaults_to_path(self, env):
        _put(env.conv, "a.txt")
        assert _to_project("a.txt", dest=" / ")["path"] == "/a.txt"


class TestOverwrite:
    def test_destination_exists_without_overwrite(self, env):
        _put(env.conv, "a.txt", "new")
        _put(env.project, "a.txt", "old")
        out = _to_project("a.txt")
        assert out["code"] == "destination_exists"
        assert "overwrite" in out["error"]
        assert (env.project / "a.txt").read_text() == "old"
        assert env.events == []

    def test_overwrite_replaces_file(self, env):
        _put(env.conv, "a.txt", "new")
        _put(env.project, "a.txt", "old")
        out = _to_project("a.txt", overwrite=True)
        assert out["type"] == "file"
        assert (env.project / "a.txt").read_text() == "new"

    def test_directory_merge(self, env):
        _put(env.project, "data/keep.csv", "keep")
        _put(env.project, "data/b.csv", "old")
        _put(env.conv, "data/b.csv", "new")
        _put(env.conv, "data/sub/c.csv", "c")
        assert _to_project("data")["code"] == "destination_exists"
        out = _to_project("data", overwrite=True)
        assert out["type"] == "folder" and out["files_copied"] == 2
        assert (env.project / "data" / "keep.csv").read_text() == "keep"
        assert (env.project / "data" / "b.csv").read_text() == "new"
        assert (env.project / "data" / "sub" / "c.csv").read_text() == "c"

    def test_type_mismatch_is_invalid_destination(self, env):
        _put(env.conv, "x", "file")
        (env.project / "x").mkdir()
        assert _to_project("x", overwrite=True)["code"] == "invalid_destination"


class TestHidden:
    def test_hidden_entries_skipped_by_default(self, env):
        _put(env.conv, "pkg/main.py")
        _put(env.conv, "pkg/.env", "secret")
        _put(env.conv, "pkg/.cache/x")
        out = _to_project("pkg")
        assert out["files_copied"] == 1 and out["skipped"] == 2
        assert "skipped" in out["message"]
        assert not (env.project / "pkg" / ".env").exists()

    def test_include_hidden(self, env):
        _put(env.conv, "pkg/main.py")
        _put(env.conv, "pkg/.env", "secret")
        out = _to_project("pkg", include_hidden=True)
        assert out["files_copied"] == 2
        assert (env.project / "pkg" / ".env").read_text() == "secret"

    def test_hidden_named_explicitly_is_copied(self, env):
        _put(env.project, ".config.json", "{}")
        assert _from_project(".config.json")["files_copied"] == 1
        assert (env.conv / ".config.json").exists()


class TestRefusals:
    @pytest.mark.parametrize("path", [
        ".responses/dump.json", ".responses", "/.subagent_responses/x/y.md",
        "pasted/image.png", "pasted",
    ])
    def test_scratch_sources_refused_to_project(self, env, path):
        _put(env.conv, path.strip("/") + ("" if "." in path.rsplit("/", 1)[-1] else "/f.txt"))
        out = _to_project(path)
        assert out["code"] == "forbidden_source"
        assert not any(env.project.iterdir())
        assert env.events == []

    def test_scratch_names_allowed_from_project(self, env):
        _put(env.project, "pasted/image.png", "png")
        assert _from_project("pasted/image.png")["type"] == "file"

    def test_not_found(self, env):
        assert _to_project("missing.txt")["code"] == "not_found"
        assert _from_project("missing.txt")["code"] == "not_found"

    @pytest.mark.parametrize("path", ["../escape.txt", "a/../../b"])
    def test_invalid_path(self, env, path):
        assert _to_project(path)["code"] == "invalid_path"

    def test_empty_path(self, env):
        assert _to_project("")["code"] == "invalid_path"
        assert _from_project("/")["code"] == "invalid_path"

    def test_symlink_source_refused(self, env):
        _put(env.conv, "real.txt", "data")
        os.symlink(env.conv / "real.txt", env.conv / "link.txt")
        out = _to_project("link.txt")
        assert out["code"] == "not_a_regular_file"
        assert not os.path.lexists(env.project / "link.txt")

    def test_symlink_escaping_root_refused(self, env, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        os.symlink(outside, env.conv / "link.txt")
        assert _to_project("link.txt")["code"] == "invalid_path"
        assert not os.path.lexists(env.project / "link.txt")

    def test_fifo_source_refused(self, env):
        os.mkfifo(env.project / "pipe")
        assert _from_project("pipe")["code"] == "not_a_regular_file"
        assert not os.path.lexists(env.conv / "pipe")

    def test_symlink_inside_directory_skipped(self, env, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        _put(env.conv, "dir/real.txt")
        os.symlink(outside, env.conv / "dir" / "link.txt")
        out = _to_project("dir")
        assert out["files_copied"] == 1 and out["skipped"] == 1
        assert not os.path.lexists(env.project / "dir" / "link.txt")

    @pytest.mark.parametrize("handler", [
        _handle_copy_file_to_project, _handle_copy_project_file,
    ])
    def test_standalone_refused(self, env, handler):
        out = json.loads(_run(handler(USER_ID, CID, None, "a.txt")))
        assert out["code"] == "not_a_project_conversation"
        assert env.events == []


class TestArgumentValidation:
    def test_traversal_dest_is_invalid_path(self, env):
        _put(env.conv, "a.txt")
        assert _to_project("a.txt", dest="../x")["code"] == "invalid_path"

    def test_root_dest_defaults_to_path(self, env):
        _put(env.conv, "a.txt")
        assert _to_project("a.txt", dest="/")["path"] == "/a.txt"

    def test_absolute_dest_is_root_relative(self, env):
        _put(env.conv, "a.txt")
        out = _to_project("a.txt", dest="/abs/b.txt")
        assert out["path"] == "/abs/b.txt"
        assert (env.project / "abs" / "b.txt").exists()

    @pytest.mark.parametrize("dest", [5, ["x"], {"a": 1}, True])
    def test_non_string_dest(self, env, dest):
        _put(env.conv, "a.txt")
        out = _to_project("a.txt", dest=dest)
        assert out["code"] == "invalid_destination"
        assert not (env.project / "a.txt").exists()

    @pytest.mark.parametrize("path", [None, 5, ["a.txt"]])
    def test_non_string_path(self, env, path):
        assert _to_project(path)["code"] == "invalid_path"
        assert _from_project(path)["code"] == "invalid_path"

    def test_dot_slash_scratch_source_refused(self, env):
        _put(env.conv, "pasted/x.png")
        assert _to_project("./pasted/x.png")["code"] == "forbidden_source"

    def test_os_error_is_copy_failed(self, env, monkeypatch):
        import chat.gemini_api.tool_handlers.workspace_copy as copy_mod

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(copy_mod, "copy_entry", boom)
        _put(env.conv, "a.txt")
        out = _to_project("a.txt")
        assert out["code"] == "copy_failed"
        assert "No space left" in out["error"]
        assert _scopes(env) == [("project", CID, PID)]

    def test_invalid_project_id(self, env):
        out = json.loads(_run(_handle_copy_file_to_project(
            USER_ID, CID, "../evil", "a.txt",
        )))
        assert out["code"] == "invalid_project"
