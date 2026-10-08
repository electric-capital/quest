"""Tests for the project file tools (devplan 00009 section 6.2).

``list_project_files`` / ``get_project_file`` / ``write_project_file`` /
``edit_project_file`` in ``chat/gemini_api/tool_handlers/workspace.py``:
thin wrappers over the root-parameterised internals shared with the
``*_workspace_file`` tools, rooted at the project workspace.

Covers: success paths over a tmp data dir, the structured
``not_a_project_conversation`` refusal outside a project, the
read-before-edit gate under the ``project:`` sidecar prefix (and its
independence from conversation-space reads of the same path), the
``file_list_changed`` scope, the size pre-flight / upload path of
``get_project_file``, and dispatch through the ``tool_call`` meta tool.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import chat.storage as storage_mod
from chat.gemini_api.tool_dispatch import (
    DIRECT_TOOL_HANDLERS,
    TOOL_CALL_HANDLERS,
    _dispatch_tool_call,
)
from chat.gemini_api.tool_handlers import (
    _handle_edit_project_file,
    _handle_edit_workspace_file,
    _handle_get_project_file,
    _handle_get_workspace_file,
    _handle_list_project_files,
    _handle_list_workspace_files,
    _handle_write_project_file,
    _handle_write_workspace_file,
)

USER_ID = 7
CID = "conv"
PID = "proj"

PROJECT_TOOLS = (
    "list_project_files", "get_project_file", "write_project_file",
    "edit_project_file", "copy_file_to_project", "copy_project_file",
)


def _run(coro):
    return asyncio.run(coro)


def _cs():
    # Resolve from the live module: some suites reload chat.storage.
    return storage_mod.ChatStorage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Data roots under tmp_path, the conversation dir created (it always
    exists for a real conversation; it holds the read sidecar), and every
    ``file_list_changed`` event captured."""
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)
    (chats / CID).mkdir(parents=True)

    from chat.realtime import bus
    events = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: events.append((uid, ev)))
    return SimpleNamespace(
        conv_root=chats / CID / "workspace",
        project_root=projects / PID / "workspace" / "workspace",
        events=events,
    )


def _reads():
    return _cs().get_workspace_read_paths(CID)


def _put(root, rel, content="hello\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Standalone refusal
# ---------------------------------------------------------------------------


class TestStandaloneRefusal:
    @pytest.mark.parametrize("call", [
        lambda: _handle_list_project_files(USER_ID, CID, None),
        lambda: _handle_write_project_file(USER_ID, CID, "a.txt", "x", None),
        lambda: _handle_edit_project_file(USER_ID, CID, "a.txt", "x", "y", None),
    ])
    def test_string_handlers_refuse(self, env, call):
        out = json.loads(_run(call()))
        assert out["code"] == "not_a_project_conversation"
        assert "error" in out
        assert not env.project_root.exists()
        assert env.events == []

    def test_get_refuses(self, env):
        text, parts = _run(_handle_get_project_file(None, USER_ID, CID, "a.txt", None))
        assert json.loads(text)["code"] == "not_a_project_conversation"
        assert parts == []

    @pytest.mark.parametrize("tool", PROJECT_TOOLS)
    def test_dispatch_refuses_every_tool(self, env, tool):
        text, parts = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
            conversation_id=CID, timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": tool, "arguments": {
                "path": "a.txt", "content": "x", "old_string": "a", "new_string": "b",
            }},
            project_id=None,
        ))
        assert json.loads(text)["code"] == "not_a_project_conversation"
        assert parts == []


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


class TestProjectFileTools:
    def test_write_lands_in_project_root_only(self, env):
        out = json.loads(_run(_handle_write_project_file(
            USER_ID, CID, "reports/q3.md", "# Q3\n", PID,
        )))
        assert out == {"path": "reports/q3.md", "size_bytes": 5, "status": "written"}
        assert (env.project_root / "reports" / "q3.md").read_text() == "# Q3\n"
        assert not (env.conv_root / "reports").exists()
        assert _reads() == ["project:reports/q3.md"]
        [(uid, ev)] = env.events
        assert uid == USER_ID
        assert (ev["scope"], ev["conversation_id"], ev["project_id"]) == ("project", CID, PID)

    def test_list_shows_project_files_not_conversation_files(self, env):
        _put(env.project_root, "shared.md")
        _put(env.project_root, "sub/data.csv", "a,b\n")
        _put(env.conv_root, "scratch.txt")
        out = json.loads(_run(_handle_list_project_files(USER_ID, CID, PID)))
        assert out["file_count"] == 2
        assert [f["path"] for f in out["files"]] == ["shared.md", "sub/data.csv"]
        conv = json.loads(_run(_handle_list_workspace_files(USER_ID, CID, project_id=PID)))
        assert [f["path"] for f in conv["files"]] == ["scratch.txt"]

    def test_get_reads_inline_and_records_prefixed_key(self, env):
        _put(env.project_root, "notes.md", "project notes")
        text, parts = _run(_handle_get_project_file(None, USER_ID, CID, "/notes.md", PID))
        out = json.loads(text)
        assert out["content"] == "project notes" and parts == []
        assert _reads() == ["project:notes.md"]

    def test_get_missing_mentions_project_file(self, env):
        text, _ = _run(_handle_get_project_file(None, USER_ID, CID, "nope.md", PID))
        assert json.loads(text)["error"] == "Project file not found: nope.md"

    def test_get_rejects_traversal(self, env):
        text, _ = _run(_handle_get_project_file(None, USER_ID, CID, "../x", PID))
        assert "traversal" in json.loads(text)["error"]

    def test_get_does_not_fall_back_to_conversation_space(self, env):
        _put(env.conv_root, "only-here.md")
        text, _ = _run(_handle_get_project_file(None, USER_ID, CID, "only-here.md", PID))
        assert "not found" in json.loads(text)["error"]
        _put(env.project_root, "only-project.md")
        text, _ = _run(_handle_get_workspace_file(
            None, USER_ID, CID, "only-project.md", project_id=PID,
        ))
        assert "not found" in json.loads(text)["error"]

    def test_get_image_uploads_part(self, env):
        (env.project_root).mkdir(parents=True)
        (env.project_root / "chart.png").write_bytes(b"\x89PNG" + b"\x00" * 32)
        provider = MagicMock()
        provider.upload_file = AsyncMock(return_value=SimpleNamespace(mime_type="image/png"))
        provider.make_file_part = MagicMock(return_value="PART")
        text, parts = _run(_handle_get_project_file(
            provider, USER_ID, CID, "chart.png", PID, model="claude-opus-4-8",
        ))
        out = json.loads(text)
        assert out["uploaded"] is True and parts == ["PART"]
        assert provider.upload_file.await_args.kwargs["file_path"] == str(
            (env.project_root / "chart.png").resolve()
        )
        assert _reads() == ["project:chart.png"]

    def test_get_size_preflight_points_at_project_mount(self, env):
        env.project_root.mkdir(parents=True)
        big = env.project_root / "huge.pdf"
        with open(big, "wb") as f:
            f.truncate(64 * 1024 * 1024)
        provider = MagicMock()
        provider.upload_file = AsyncMock()
        text, parts = _run(_handle_get_project_file(
            provider, USER_ID, CID, "huge.pdf", PID, model="claude-opus-4-8",
        ))
        out = json.loads(text)
        assert "too large" in out["error"] and parts == []
        assert "/project/huge.pdf" in out["suggestion"]
        provider.upload_file.assert_not_awaited()
        assert _reads() == []

    def test_conversation_write_publishes_conversation_scope(self, env):
        _run(_handle_write_workspace_file(USER_ID, CID, "a.txt", "x", project_id=PID))
        assert (env.conv_root / "a.txt").exists()
        assert not env.project_root.exists()
        [(_uid, ev)] = env.events
        assert (ev["scope"], ev["project_id"]) == ("conversation", PID)
        assert _reads() == ["a.txt"]


# ---------------------------------------------------------------------------
# Read-before-edit gate with the project: prefix
# ---------------------------------------------------------------------------


class TestEditGate:
    def test_edit_refused_before_read(self, env):
        _put(env.project_root, "plan.md", "alpha beta")
        out = json.loads(_run(_handle_edit_project_file(
            USER_ID, CID, "plan.md", "beta", "BETA", PID,
        )))
        assert "has not been read" in out["error"]
        assert "get_project_file" in out["error"]
        assert (env.project_root / "plan.md").read_text() == "alpha beta"

    def test_edit_allowed_after_get(self, env):
        _put(env.project_root, "plan.md", "alpha beta")
        _run(_handle_get_project_file(None, USER_ID, CID, "plan.md", PID))
        out = json.loads(_run(_handle_edit_project_file(
            USER_ID, CID, "plan.md", "beta", "BETA", PID,
        )))
        assert out["status"] == "edited" and out["replacements"] == 1
        assert (env.project_root / "plan.md").read_text() == "alpha BETA"
        assert env.events[-1][1]["scope"] == "project"

    def test_edit_allowed_after_write_and_replace_all(self, env):
        _run(_handle_write_project_file(USER_ID, CID, "x.txt", "a a a", PID))
        out = json.loads(_run(_handle_edit_project_file(
            USER_ID, CID, "x.txt", "a", "b", PID, replace_all=True,
        )))
        assert out["replacements"] == 3
        assert (env.project_root / "x.txt").read_text() == "b b b"

    def test_conversation_read_does_not_license_project_edit(self, env):
        _put(env.conv_root, "notes.md", "conv text")
        _put(env.project_root, "notes.md", "proj text")
        _run(_handle_get_workspace_file(None, USER_ID, CID, "notes.md", project_id=PID))
        assert _reads() == ["notes.md"]
        out = json.loads(_run(_handle_edit_project_file(
            USER_ID, CID, "notes.md", "proj", "PROJ", PID,
        )))
        assert "has not been read" in out["error"]
        assert (env.project_root / "notes.md").read_text() == "proj text"

    def test_project_read_does_not_license_conversation_edit(self, env):
        _put(env.conv_root, "notes.md", "conv text")
        _put(env.project_root, "notes.md", "proj text")
        _run(_handle_get_project_file(None, USER_ID, CID, "notes.md", PID))
        out = json.loads(_run(_handle_edit_workspace_file(
            USER_ID, CID, "notes.md", "conv", "CONV", project_id=PID,
        )))
        assert "has not been read" in out["error"]
        assert "get_workspace_file" in out["error"]
        assert (env.conv_root / "notes.md").read_text() == "conv text"

    def test_sibling_conversation_read_does_not_license(self, env, tmp_path):
        _put(env.project_root, "plan.md", "alpha")
        (tmp_path / "chats" / "sibling").mkdir(parents=True)
        _run(_handle_get_project_file(None, USER_ID, "sibling", "plan.md", PID))
        out = json.loads(_run(_handle_edit_project_file(
            USER_ID, CID, "plan.md", "alpha", "ALPHA", PID,
        )))
        assert "has not been read" in out["error"]


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.parametrize("tool", PROJECT_TOOLS)
    def test_in_both_tables(self, tool):
        assert tool in TOOL_CALL_HANDLERS
        assert tool in DIRECT_TOOL_HANDLERS

    def test_tool_call_round_trip(self, env):
        def call(tool, arguments):
            text, _parts = _run(_dispatch_tool_call(
                app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
                conversation_id=CID, timezone="UTC", tool_name="tool_call",
                args={"tool_name": tool, "arguments": arguments},
                project_id=PID,
            ))
            return json.loads(text)

        assert call("write_project_file", {"path": "d.md", "content": "x x"})["status"] == "written"
        out = call("edit_project_file", {
            "path": "d.md", "old_string": "x", "new_string": "y", "replace_all": "true",
        })
        assert out["replacements"] == 2
        assert call("list_project_files", {})["file_count"] == 1
        assert call("get_project_file", {"path": "d.md"})["content"] == "y y"
        assert call("copy_project_file", {"path": "d.md"})["files_copied"] == 1
        assert (env.conv_root / "d.md").read_text() == "y y"
        _put(env.conv_root, "d.md", "new")
        assert call("copy_file_to_project", {"path": "d.md"})["code"] == "destination_exists"
        assert call("copy_file_to_project", {"path": "d.md", "overwrite": "true"})["type"] == "file"
        assert (env.project_root / "d.md").read_text() == "new"

    @pytest.mark.parametrize("project_id,listed", [(None, False), (PID, True)])
    def test_unknown_tool_error_lists_project_tools_only_in_projects(
        self, env, project_id, listed,
    ):
        text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
            conversation_id=CID, timezone="UTC", tool_name="tool_call",
            args={"tool_name": "no_such_tool", "arguments": {}},
            project_id=project_id,
        ))
        error = json.loads(text)["error"]
        assert error.startswith("Unknown tool: 'no_such_tool'")
        assert "list_workspace_files" in error
        for tool in PROJECT_TOOLS + ("project_db_query",):
            assert (tool in error) is listed, tool
