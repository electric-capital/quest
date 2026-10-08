"""Tests for the space-aware file tools ``list_files`` / ``read_file`` /
``write_file`` / ``edit_file`` (chat/gemini_api/tool_handlers/workspace.py;
devplan 00009, revised 2026-10-08: scheme-qualified file tools).

Covers: success per space over a tmp data dir, ``proj://`` in a standalone
conversation -> ``no_project``, bare / malformed paths -> ``invalid_path``,
an unresolvable project id -> ``invalid_project``, strict addressing (no
fallback to the other space), the read-before-edit sidecar keys incl.
cross-family licensing between the ``*_workspace_file`` tools and the
``chat://`` paths (but never toward ``proj://``), sub-agent reads, the
``file_list_changed`` scope, ``read_file``'s upload and size pre-flight,
and dispatch through the ``tool_call`` meta tool.
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
    _handle_edit_file,
    _handle_edit_workspace_file,
    _handle_get_workspace_file,
    _handle_list_files,
    _handle_list_workspace_files,
    _handle_read_file,
    _handle_write_file,
    _handle_write_workspace_file,
)

USER_ID = 7
CID = "conv"
PID = "proj"

NEW_TOOLS = ("list_files", "read_file", "write_file", "edit_file", "copy_file")


def _run(coro):
    return asyncio.run(coro)


def _cs():
    # Resolve from the live module: some suites reload chat.storage.
    return storage_mod.ChatStorage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Data roots under tmp_path, the conversation dir created (it holds
    the read sidecar), every ``file_list_changed`` event captured."""
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


def _j(text):
    return json.loads(text)


def _read(path, *, project_id=PID, provider=None, model=""):
    text, parts = _run(_handle_read_file(
        provider, USER_ID, CID, project_id, path, model=model,
    ))
    return _j(text), parts


def _write(path, content, *, project_id=PID):
    return _j(_run(_handle_write_file(USER_ID, CID, project_id, path, content)))


def _edit(path, old, new, *, project_id=PID, replace_all=False):
    return _j(_run(_handle_edit_file(
        USER_ID, CID, project_id, path, old, new, replace_all=replace_all,
    )))


def _list(path, *, project_id=PID):
    return _j(_run(_handle_list_files(USER_ID, CID, project_id, path)))


# ---------------------------------------------------------------------------
# Path errors, no_project, invalid_project
# ---------------------------------------------------------------------------


class TestPathErrors:
    @pytest.mark.parametrize("path", [
        "notes.md", "/notes.md", "project://x", "chat://../x", "chat:///abs", None, 3,
        "proj://a..b/c", " chat://x",
    ])
    def test_invalid_path_everywhere(self, env, path):
        for out in (
            _list(path), _read(path)[0], _write(path, "x"), _edit(path, "a", "b"),
        ):
            assert out["code"] == "invalid_path"
            assert "chat://" in out["error"] and "proj://" in out["error"]
        assert env.events == []

    @pytest.mark.parametrize("space,attr", [("chat", "conv_root"), ("proj", "project_root")])
    def test_symlink_escaping_root(self, env, tmp_path, space, attr):
        root = getattr(env, attr)
        root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (root / "link").symlink_to(outside)
        (root / "flink.txt").symlink_to(outside / "secret.txt")
        _cs().add_workspace_read_paths(CID, [f"{space}://flink.txt", "flink.txt"])
        for out in (
            _list(f"{space}://link"),
            _read(f"{space}://flink.txt")[0],
            _read(f"{space}://link/secret.txt")[0],
            _edit(f"{space}://flink.txt", "secret", "PWNED"),
            _write(f"{space}://link/new.txt", "x"),
        ):
            assert out["code"] == "invalid_path", out
            assert "outside" in out["error"]
        assert (outside / "secret.txt").read_text() == "secret"
        assert not (outside / "new.txt").exists()

    def test_root_is_not_a_file(self, env):
        assert _read("chat://")[0]["code"] == "invalid_path"
        assert _write("proj://", "x")["code"] == "invalid_path"

    def test_standalone_proj_is_no_project(self, env):
        for out in (
            _list("proj://", project_id=None),
            _read("proj://a.txt", project_id=None)[0],
            _write("proj://a.txt", "x", project_id=None),
            _edit("proj://a.txt", "a", "b", project_id=None),
        ):
            assert out["code"] == "no_project"
        assert not env.project_root.exists()
        assert env.events == []

    def test_standalone_chat_works(self, env):
        assert _write("chat://a.txt", "x", project_id=None)["status"] == "written"
        assert (env.conv_root / "a.txt").read_text() == "x"

    def test_invalid_project_id(self, env):
        out = _list("proj://", project_id="../evil")
        assert out["code"] == "invalid_project"
        assert _read("proj://a", project_id="../evil")[0]["code"] == "invalid_project"

    @pytest.mark.parametrize("tool", NEW_TOOLS)
    def test_dispatch_never_raises(self, env, tool):
        text, parts = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
            conversation_id=CID, timezone="UTC", tool_name="tool_call",
            args={"tool_name": tool, "arguments": {
                "path": "proj://a.txt", "src": "proj://a.txt", "dest": "chat://a.txt",
                "content": "x", "old_string": "a", "new_string": "b",
            }},
            project_id=None,
        ))
        assert _j(text)["code"] == "no_project"
        assert parts == []


# ---------------------------------------------------------------------------
# Success per space
# ---------------------------------------------------------------------------


class TestPerSpace:
    def test_write_proj_lands_in_project_root_only(self, env):
        out = _write("proj://reports/q3.md", "# Q3\n")
        assert out == {"path": "proj://reports/q3.md", "size_bytes": 5, "status": "written"}
        assert (env.project_root / "reports" / "q3.md").read_text() == "# Q3\n"
        assert not (env.conv_root / "reports").exists()
        assert _reads() == ["proj://reports/q3.md"]
        [(uid, ev)] = env.events
        assert (uid, ev["scope"], ev["conversation_id"], ev["project_id"]) == (
            USER_ID, "project", CID, PID,
        )

    def test_write_chat_lands_in_conversation_root_only(self, env):
        out = _write("chat://out/a.txt", "x")
        assert out["path"] == "chat://out/a.txt"
        assert (env.conv_root / "out" / "a.txt").read_text() == "x"
        assert not env.project_root.exists()
        assert _reads() == ["chat://out/a.txt", "out/a.txt"]
        [(_uid, ev)] = env.events
        assert (ev["scope"], ev["project_id"]) == ("conversation", PID)

    def test_list_whole_space_matches_list_workspace_files(self, env):
        _put(env.conv_root, "b.txt")
        _put(env.conv_root, "sub/a.txt")
        assert _list("chat://") == _j(_run(
            _handle_list_workspace_files(USER_ID, CID, project_id=PID)
        ))

    def test_list_project_space_and_subtree(self, env):
        _put(env.project_root, "shared.md")
        _put(env.project_root, "reports/q3.md")
        _put(env.project_root, "reports/old/q2.md")
        _put(env.conv_root, "scratch.txt")
        whole = _list("proj://")
        assert [f["path"] for f in whole["files"]] == [
            "reports/old/q2.md", "reports/q3.md", "shared.md",
        ]
        sub = _list("proj://reports")
        assert sub["file_count"] == 2
        assert [f["path"] for f in sub["files"]] == ["reports/old/q2.md", "reports/q3.md"]
        assert set(sub["files"][0]) == {"path", "size_bytes", "modified"}

    def test_list_missing_and_file(self, env):
        _put(env.project_root, "a.md")
        assert _list("proj://nope")["code"] == "not_found"
        assert _list("proj://a.md")["code"] == "not_a_folder"

    def test_read_inline(self, env):
        _put(env.project_root, "notes.md", "project notes")
        out, parts = _read("proj://notes.md")
        assert out == {"path": "proj://notes.md", "size_bytes": 13, "content": "project notes"}
        assert parts == []
        assert _reads() == ["proj://notes.md"]

    def test_read_missing_names_the_scheme_path(self, env):
        out, _ = _read("proj://nope.md")
        assert out["error"] == "Project file not found: proj://nope.md"
        out, _ = _read("chat://nope.md")
        assert out["error"] == "File not found: chat://nope.md"

    def test_strict_addressing_no_fallback(self, env):
        _put(env.conv_root, "only-chat.md")
        _put(env.project_root, "only-proj.md")
        assert "not found" in _read("proj://only-chat.md")[0]["error"]
        assert "not found" in _read("chat://only-proj.md")[0]["error"]
        bare, _ = _run(_handle_get_workspace_file(
            None, USER_ID, CID, "only-proj.md", project_id=PID,
        ))
        assert "not found" in _j(bare)["error"]

    def test_read_image_uploads_part(self, env):
        env.project_root.mkdir(parents=True)
        (env.project_root / "chart.png").write_bytes(b"\x89PNG" + b"\x00" * 32)
        provider = MagicMock()
        provider.upload_file = AsyncMock(return_value=SimpleNamespace(mime_type="image/png"))
        provider.make_file_part = MagicMock(return_value="PART")
        out, parts = _read("proj://chart.png", provider=provider, model="claude-opus-4-8")
        assert out["uploaded"] is True and parts == ["PART"]
        assert out["path"] == "proj://chart.png"
        assert provider.upload_file.await_args.kwargs["file_path"] == str(
            (env.project_root / "chart.png").resolve()
        )

    def test_read_size_preflight_points_at_project_mount(self, env):
        env.project_root.mkdir(parents=True)
        with open(env.project_root / "huge.pdf", "wb") as f:
            f.truncate(64 * 1024 * 1024)
        provider = MagicMock()
        provider.upload_file = AsyncMock()
        out, parts = _read("proj://huge.pdf", provider=provider, model="claude-opus-4-8")
        assert "too large" in out["error"] and parts == []
        assert "/project/huge.pdf" in out["suggestion"]
        assert "read_file" in out["suggestion"]
        assert "get_workspace_file" not in out["suggestion"]
        provider.upload_file.assert_not_awaited()
        assert _reads() == []

    def test_write_rejects_directory_and_non_string_content(self, env):
        (env.project_root / "dir").mkdir(parents=True)
        assert "is a directory" in _write("proj://dir", "x")["error"]
        assert _write("proj://a.txt", 5)["code"] == "invalid_content"


# ---------------------------------------------------------------------------
# Read-before-edit sidecar
# ---------------------------------------------------------------------------


class TestEditGate:
    def test_proj_edit_refused_before_read(self, env):
        _put(env.project_root, "plan.md", "alpha beta")
        out = _edit("proj://plan.md", "beta", "BETA")
        assert "has not been read" in out["error"]
        assert "proj://plan.md" in out["error"] and "read_file" in out["error"]
        assert (env.project_root / "plan.md").read_text() == "alpha beta"

    def test_proj_edit_after_read(self, env):
        _put(env.project_root, "plan.md", "alpha beta")
        _read("proj://plan.md")
        out = _edit("proj://plan.md", "beta", "BETA")
        assert out == {"path": "proj://plan.md", "size_bytes": 10, "status": "edited",
                       "replacements": 1}
        assert (env.project_root / "plan.md").read_text() == "alpha BETA"
        assert env.events[-1][1]["scope"] == "project"

    def test_proj_edit_after_write_replace_all(self, env):
        _write("proj://x.txt", "a a a")
        assert _edit("proj://x.txt", "a", "b", replace_all=True)["replacements"] == 3

    @pytest.mark.parametrize("reader", ["get_workspace_file", "write_workspace_file"])
    def test_old_family_read_licenses_chat_edit(self, env, reader):
        _put(env.conv_root, "notes.md", "conv text")
        if reader == "get_workspace_file":
            _run(_handle_get_workspace_file(None, USER_ID, CID, "notes.md", project_id=PID))
        else:
            _run(_handle_write_workspace_file(USER_ID, CID, "notes.md", "conv text"))
        assert set(_reads()) == {"notes.md", "chat://notes.md"}
        assert _edit("chat://notes.md", "conv", "CONV")["status"] == "edited"

    @pytest.mark.parametrize("reader", ["read_file", "write_file"])
    def test_chat_read_licenses_old_family_edit(self, env, reader):
        _put(env.conv_root, "notes.md", "conv text")
        if reader == "read_file":
            _read("chat://notes.md")
        else:
            _write("chat://notes.md", "conv text")
        out = _j(_run(_handle_edit_workspace_file(
            USER_ID, CID, "notes.md", "conv", "CONV", project_id=PID,
        )))
        assert out["status"] == "edited"

    def test_legacy_bare_only_sidecar_licenses_chat_edit(self, env):
        _put(env.conv_root, "old.md", "legacy")
        _cs().add_workspace_read_paths(CID, ["old.md"])
        assert _edit("chat://old.md", "legacy", "LEGACY")["status"] == "edited"

    def test_chat_read_does_not_license_proj_edit(self, env):
        _put(env.conv_root, "notes.md", "conv text")
        _put(env.project_root, "notes.md", "proj text")
        _read("chat://notes.md")
        _run(_handle_get_workspace_file(None, USER_ID, CID, "notes.md", project_id=PID))
        assert "has not been read" in _edit("proj://notes.md", "proj", "PROJ")["error"]
        assert (env.project_root / "notes.md").read_text() == "proj text"

    def test_proj_read_does_not_license_chat_edits(self, env):
        _put(env.conv_root, "notes.md", "conv text")
        _put(env.project_root, "notes.md", "proj text")
        _read("proj://notes.md")
        assert "has not been read" in _edit("chat://notes.md", "conv", "CONV")["error"]
        out = _j(_run(_handle_edit_workspace_file(
            USER_ID, CID, "notes.md", "conv", "CONV", project_id=PID,
        )))
        assert "has not been read" in out["error"]
        assert "get_workspace_file" in out["error"]

    def test_file_named_like_a_scheme_key_cannot_collide(self, env):
        # A bare conversation file literally named "proj:/plan.md" cannot
        # exist (slash), and a bare key never starts with "proj://".
        _put(env.project_root, "plan.md", "proj")
        _put(env.conv_root, "proj:/plan.md", "conv")
        _run(_handle_get_workspace_file(None, USER_ID, CID, "proj:/plan.md", project_id=PID))
        assert "proj://plan.md" not in _reads()
        assert "has not been read" in _edit("proj://plan.md", "proj", "P")["error"]

    def test_sibling_conversation_read_does_not_license(self, env, tmp_path):
        _put(env.project_root, "plan.md", "alpha")
        (tmp_path / "chats" / "sibling").mkdir(parents=True)
        _run(_handle_read_file(None, USER_ID, "sibling", PID, "proj://plan.md"))
        assert "has not been read" in _edit("proj://plan.md", "alpha", "ALPHA")["error"]

    def test_sub_agent_read_licenses_parent_edit(self, env):
        _put(env.project_root, "plan.md", "alpha")
        text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
            conversation_id=CID, timezone="UTC", tool_name="tool_call",
            args={"tool_name": "read_file", "arguments": {"path": "proj://plan.md"}},
            project_id=PID, is_sub_agent=True, agent_name="helper",
        ))
        assert _j(text)["content"] == "alpha"
        assert _edit("proj://plan.md", "alpha", "ALPHA")["status"] == "edited"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.parametrize("tool", NEW_TOOLS)
    def test_in_both_tables(self, tool):
        assert tool in TOOL_CALL_HANDLERS
        assert tool in DIRECT_TOOL_HANDLERS

    def test_old_workspace_tools_stay_dispatchable(self):
        for tool in ("list_workspace_files", "get_workspace_file",
                     "write_workspace_file", "edit_workspace_file"):
            assert tool in TOOL_CALL_HANDLERS

    def test_tool_call_round_trip(self, env):
        def call(tool, arguments):
            text, _parts = _run(_dispatch_tool_call(
                app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
                conversation_id=CID, timezone="UTC", tool_name="tool_call",
                args={"tool_name": tool, "arguments": arguments},
                project_id=PID,
            ))
            return _j(text)

        assert call("write_file", {"path": "proj://d.md", "content": "x x"})["status"] == "written"
        out = call("edit_file", {
            "path": "proj://d.md", "old_string": "x", "new_string": "y", "replace_all": "true",
        })
        assert out["replacements"] == 2
        assert call("list_files", {"path": "proj://"})["file_count"] == 1
        assert call("read_file", {"path": "proj://d.md"})["content"] == "y y"
        assert call("copy_file", {"src": "proj://d.md", "dest": "chat://d.md"})["files_copied"] == 1
        assert (env.conv_root / "d.md").read_text() == "y y"
        _put(env.conv_root, "d.md", "new")
        assert call("copy_file", {"src": "chat://d.md", "dest": "proj://d.md"})["code"] == \
            "destination_exists"
        assert call("copy_file", {
            "src": "chat://d.md", "dest": "proj://d.md", "overwrite": "true",
        })["type"] == "file"
        assert (env.project_root / "d.md").read_text() == "new"
        assert call("list_files", {})["code"] == "invalid_path"

    @pytest.mark.parametrize("project_id", [None, PID])
    def test_unknown_tool_error_lists_what_the_prompt_offers(self, env, project_id):
        text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user={"id": USER_ID, "email": "u@x"},
            conversation_id=CID, timezone="UTC", tool_name="tool_call",
            args={"tool_name": "no_such_tool", "arguments": {}},
            project_id=project_id,
        ))
        error = _j(text)["error"]
        assert error.startswith("Unknown tool: 'no_such_tool'")
        listed = set(error.split("Available dynamic tools: ", 1)[1].rstrip(".").split(", "))
        in_project = project_id is not None
        for tool in NEW_TOOLS + ("project_db_query",):
            assert (tool in listed) is in_project, tool
        for tool in ("list_workspace_files", "get_workspace_file",
                     "write_workspace_file", "edit_workspace_file"):
            assert (tool in listed) is not in_project, tool
