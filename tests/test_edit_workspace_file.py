"""Tests for the ``edit_workspace_file`` tool (plan 00109).

Covers ``_handle_edit_workspace_file`` in ``chat/gemini_api/tool_handlers/workspace.py``:

* Exact string replacement semantics (Claude Code Edit-style): unique
  ``old_string`` match unless ``replace_all`` is true; errors for
  not-found, ambiguous match, identical strings, and empty ``old_string``.
* The read-before-edit gate backed by the per-conversation
  ``workspace_reads.json`` sidecar (``ChatStorage.get_workspace_read_paths``
  / ``add_workspace_read_paths``): a file must have been read with
  ``get_workspace_file`` or written with ``write_workspace_file`` in the
  same conversation before it may be edited.
* Path validation (traversal, empty path, absolute-ish normalization),
  missing-file / directory errors, binary-file rejection, and size caps.
* ``file_list_changed`` publishing on success only.
* Dispatch routing through ``_dispatch_tool_call`` (``tool_call`` inner
  arm) including ``replace_all`` string-to-bool coercion.
* Tool schema sanity in ``TOOL_CALL_REGISTRY``.

No DB, LLM, or network access -- workspace and sidecar paths are patched
to pytest tmp_path.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.constants import _WRITE_FILE_MAX_SIZE
from chat.gemini_api.tool_handlers import (
    _handle_edit_workspace_file,
    _handle_get_workspace_file,
    _handle_write_workspace_file,
)
from chat.storage import ChatStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


USER_ID = 42
CONV_ID = "conv-edit-1"


def _patch_workspace_dir(tmp_path: Path):
    """Patch ``conversation_workspace_dir`` so handlers operate under ``tmp_path``."""
    async def fake_workspace(*args, **kwargs):
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    return patch(
        "chat.gemini_api.tool_handlers.workspace.conversation_workspace_dir",
        new=fake_workspace,
    )


def _patch_conversation_dir(tmp_path: Path):
    """Patch ``ChatStorage._get_conversation_dir`` so the
    ``workspace_reads.json`` sidecar lands under ``tmp_path``."""
    return patch.object(
        ChatStorage,
        "_get_conversation_dir",
        new=lambda conversation_id: tmp_path,
    )


@pytest.fixture()
def workspace(tmp_path):
    """Workspace dir + sidecar both rooted at tmp_path; yields the
    workspace directory Path."""
    with _patch_workspace_dir(tmp_path), _patch_conversation_dir(tmp_path):
        yield tmp_path / "workspace"


def _make_file(workspace: Path, rel_path: str, content: str) -> Path:
    file_path = workspace / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _mark_read(rel_path: str, conversation_id: str = CONV_ID) -> None:
    ChatStorage.add_workspace_read_paths(conversation_id, [rel_path])


def _edit(path: str, old: str, new: str, *, replace_all: bool = False):
    return _run(_handle_edit_workspace_file(
        USER_ID, CONV_ID, path, old, new, replace_all=replace_all,
    ))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_single_unique_replacement(self, workspace):
        _make_file(workspace, "report.md", "alpha beta gamma\n")
        _mark_read("report.md")

        publish_mock = MagicMock()
        with patch(
            "chat.gemini_api.tool_handlers.workspace._publish_file_list_changed",
            new=publish_mock,
        ):
            result = _edit("report.md", "beta", "BETA")

        parsed = json.loads(result)
        assert parsed["status"] == "edited"
        assert parsed["path"] == "report.md"
        assert parsed["replacements"] == 1
        new_content = (workspace / "report.md").read_text(encoding="utf-8")
        assert new_content == "alpha BETA gamma\n"
        assert parsed["size_bytes"] == len(new_content.encode("utf-8"))
        publish_mock.assert_called_once_with(USER_ID, "conversation", CONV_ID, None)

    def test_empty_new_string_deletes_match(self, workspace):
        _make_file(workspace, "a.txt", "keep DELETE keep")
        _mark_read("a.txt")

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("a.txt", " DELETE", "")

        parsed = json.loads(result)
        assert parsed["status"] == "edited"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "keep keep"

    def test_successful_edit_marks_sidecar_idempotently(self, workspace):
        _make_file(workspace, "a.txt", "one two")
        _mark_read("a.txt")

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("a.txt", "one", "1")

        assert json.loads(result)["status"] == "edited"
        assert ChatStorage.get_workspace_read_paths(CONV_ID) == ["a.txt", "chat://a.txt"]

        # A previous successful edit licenses another edit.
        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("a.txt", "two", "2")
        assert json.loads(result)["status"] == "edited"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "1 2"


# ---------------------------------------------------------------------------
# Read-before-edit gate
# ---------------------------------------------------------------------------


class TestReadBeforeEditGate:
    def test_unread_file_is_rejected_and_untouched(self, workspace):
        original = "alpha beta gamma\n"
        _make_file(workspace, "report.md", original)

        publish_mock = MagicMock()
        with patch(
            "chat.gemini_api.tool_handlers.workspace._publish_file_list_changed",
            new=publish_mock,
        ):
            result = _edit("report.md", "beta", "BETA")

        parsed = json.loads(result)
        assert "error" in parsed
        assert "has not been read" in parsed["error"]
        assert "get_workspace_file" in parsed["error"]
        assert (workspace / "report.md").read_text(encoding="utf-8") == original
        publish_mock.assert_not_called()
        # The failed edit must not mark the sidecar either.
        assert ChatStorage.get_workspace_read_paths(CONV_ID) == []

    def test_get_workspace_file_licenses_edit(self, workspace):
        _make_file(workspace, "notes.txt", "hello world")

        read_result, extra = _run(_handle_get_workspace_file(
            MagicMock(), USER_ID, CONV_ID, "notes.txt",
        ))
        assert "error" not in json.loads(read_result)
        assert extra == []
        assert ChatStorage.get_workspace_read_paths(CONV_ID) == ["notes.txt", "chat://notes.txt"]

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("notes.txt", "world", "there")
        assert json.loads(result)["status"] == "edited"
        assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello there"

    def test_write_workspace_file_licenses_edit(self, workspace):
        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            write_result = _run(_handle_write_workspace_file(
                USER_ID, CONV_ID, "out/data.csv", "a,b\n1,2\n",
            ))
            assert json.loads(write_result)["status"] == "written"
            # Bare key + the chat:// key of the space-aware tools.
            assert ChatStorage.get_workspace_read_paths(CONV_ID) == [
                str(Path("out/data.csv")), "chat://out/data.csv",
            ]

            result = _edit("out/data.csv", "1,2", "3,4")
        assert json.loads(result)["status"] == "edited"
        assert (workspace / "out" / "data.csv").read_text(encoding="utf-8") == "a,b\n3,4\n"

    def test_read_in_other_conversation_does_not_license(self, tmp_path):
        # Project workspaces are shared, but tracking is per-conversation:
        # a read in conversation A must not license an edit in conversation B.
        def conv_dir(conversation_id):
            d = tmp_path / "convs" / conversation_id
            d.mkdir(parents=True, exist_ok=True)
            return d

        with _patch_workspace_dir(tmp_path), patch.object(
            ChatStorage, "_get_conversation_dir", new=conv_dir,
        ):
            workspace = tmp_path / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            original = "content here"
            _make_file(workspace, "shared.txt", original)
            _mark_read("shared.txt", conversation_id="some-other-conv")

            result = _edit("shared.txt", "content", "CONTENT")
            parsed = json.loads(result)
            assert "error" in parsed
            assert "has not been read" in parsed["error"]
            assert (workspace / "shared.txt").read_text(encoding="utf-8") == original

    def test_canonical_normalization_dot_slash_read_licenses_plain_edit(self, workspace):
        _make_file(workspace, "a.txt", "x y z")

        # Read with './a.txt' -- sidecar stores the canonical 'a.txt'.
        read_result, _ = _run(_handle_get_workspace_file(
            MagicMock(), USER_ID, CONV_ID, "./a.txt",
        ))
        assert "error" not in json.loads(read_result)
        assert ChatStorage.get_workspace_read_paths(CONV_ID) == ["a.txt", "chat://a.txt"]

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("a.txt", "y", "Y")
        assert json.loads(result)["status"] == "edited"

    def test_canonical_normalization_leading_slash_edit(self, workspace):
        _make_file(workspace, "a.txt", "x y z")
        _mark_read("a.txt")

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("/a.txt", "y", "Y")
        assert json.loads(result)["status"] == "edited"


# ---------------------------------------------------------------------------
# Matching errors
# ---------------------------------------------------------------------------


class TestMatchingErrors:
    def test_old_string_not_found(self, workspace):
        original = "alpha beta\n"
        _make_file(workspace, "a.txt", original)
        _mark_read("a.txt")

        result = _edit("a.txt", "missing", "x")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not found" in parsed["error"]
        assert (workspace / "a.txt").read_text(encoding="utf-8") == original

    def test_duplicate_old_string_without_replace_all(self, workspace):
        original = "dup x dup"
        _make_file(workspace, "a.txt", original)
        _mark_read("a.txt")

        result = _edit("a.txt", "dup", "DUP")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "2 times" in parsed["error"]
        assert "replace_all" in parsed["error"]
        assert (workspace / "a.txt").read_text(encoding="utf-8") == original

    def test_duplicate_old_string_with_replace_all(self, workspace):
        _make_file(workspace, "a.txt", "dup x dup")
        _mark_read("a.txt")

        with patch("chat.gemini_api.tool_handlers.workspace._publish_file_list_changed"):
            result = _edit("a.txt", "dup", "DUP", replace_all=True)
        parsed = json.loads(result)
        assert parsed["status"] == "edited"
        assert parsed["replacements"] == 2
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "DUP x DUP"

    def test_identical_old_and_new_strings(self, workspace):
        _make_file(workspace, "a.txt", "same same")
        _mark_read("a.txt")

        result = _edit("a.txt", "same", "same")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "identical" in parsed["error"]

    def test_empty_old_string(self, workspace):
        _make_file(workspace, "a.txt", "content")
        _mark_read("a.txt")

        result = _edit("a.txt", "", "x")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "old_string must not be empty" in parsed["error"]


# ---------------------------------------------------------------------------
# Path validation and existence errors
# ---------------------------------------------------------------------------


class TestPathErrors:
    def test_path_traversal_rejected(self, workspace):
        result = _edit("../escape.txt", "a", "b")
        parsed = json.loads(result)
        assert parsed["error"] == "Invalid path: path traversal not allowed"

    def test_empty_path_rejected(self, workspace):
        result = _edit("", "a", "b")
        parsed = json.loads(result)
        assert parsed["error"] == "Invalid path: path cannot be empty"

    def test_missing_file(self, workspace):
        result = _edit("nope.txt", "a", "b")
        parsed = json.loads(result)
        assert parsed["error"] == "File not found: nope.txt"

    def test_path_is_directory(self, workspace):
        (workspace / "subdir").mkdir(parents=True, exist_ok=True)
        result = _edit("subdir", "a", "b")
        parsed = json.loads(result)
        assert parsed["error"] == "Not a file: subdir"


# ---------------------------------------------------------------------------
# Binary files and size caps
# ---------------------------------------------------------------------------


class TestBinaryAndSizeGuards:
    def test_binary_file_rejected_with_suggestion(self, workspace):
        binary = b"\x00\xff\xfe\x01not-utf8\x80"
        file_path = workspace / "blob.bin"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(binary)
        _mark_read("blob.bin")

        result = _edit("blob.bin", "not", "still-not")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not valid UTF-8" in parsed["error"]
        assert "run_python" in parsed["suggestion"]
        assert file_path.read_bytes() == binary

    def test_oversized_file_rejected(self, workspace):
        big = "x" * (_WRITE_FILE_MAX_SIZE + 1)
        _make_file(workspace, "big.txt", big)
        _mark_read("big.txt")

        result = _edit("big.txt", "x", "y")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "File too large to edit" in parsed["error"]
        assert "run_python" in parsed["suggestion"]

    def test_oversized_post_replacement_content_rejected(self, workspace):
        original = "PLACEHOLDER"
        _make_file(workspace, "grow.txt", original)
        _mark_read("grow.txt")

        result = _edit("grow.txt", "PLACEHOLDER", "y" * (_WRITE_FILE_MAX_SIZE + 1))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Content too large" in parsed["error"]
        # No write happened.
        assert (workspace / "grow.txt").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Sidecar tracking helpers
# ---------------------------------------------------------------------------


class TestWorkspaceReadTracking:
    def test_getter_returns_empty_when_missing(self, tmp_path):
        with _patch_conversation_dir(tmp_path):
            assert ChatStorage.get_workspace_read_paths("c") == []

    def test_getter_returns_empty_on_corrupt_file(self, tmp_path):
        (tmp_path / "workspace_reads.json").write_text("{not json")
        with _patch_conversation_dir(tmp_path):
            assert ChatStorage.get_workspace_read_paths("c") == []

    def test_appender_dedups_preserving_order(self, tmp_path):
        with _patch_conversation_dir(tmp_path):
            ChatStorage.add_workspace_read_paths("c", ["a.txt", "b.txt"])
            ChatStorage.add_workspace_read_paths("c", ["b.txt", "c.txt", "a.txt"])
            assert ChatStorage.get_workspace_read_paths("c") == [
                "a.txt", "b.txt", "c.txt",
            ]

    def test_appender_noop_on_empty_input(self, tmp_path):
        with _patch_conversation_dir(tmp_path):
            ChatStorage.add_workspace_read_paths("c", [])
            assert not (tmp_path / "workspace_reads.json").exists()


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    def test_tool_call_routes_to_handler_with_replace_all_coercion(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call

        handler_mock = AsyncMock(return_value=json.dumps({"status": "edited"}))
        with patch(
            "chat.gemini_api.tool_dispatch._handle_edit_workspace_file",
            new=handler_mock,
        ):
            result, extra_parts = _run(_dispatch_tool_call(
                app=MagicMock(),
                provider=MagicMock(),
                user={"id": USER_ID, "email": "u@example.com"},
                conversation_id=CONV_ID,
                timezone="UTC",
                tool_name="tool_call",
                args={
                    "tool_name": "edit_workspace_file",
                    "arguments": {
                        "path": "a.txt",
                        "old_string": "old",
                        "new_string": "new",
                        "replace_all": "true",
                        "intent_message": "Fix typo in report",
                    },
                },
                project_id="proj-1",
            ))

        assert json.loads(result)["status"] == "edited"
        assert extra_parts == []
        handler_mock.assert_awaited_once_with(
            USER_ID, CONV_ID, "a.txt", "old", "new",
            replace_all=True, project_id="proj-1",
        )

    def test_tool_call_replace_all_defaults_false(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call

        handler_mock = AsyncMock(return_value=json.dumps({"status": "edited"}))
        with patch(
            "chat.gemini_api.tool_dispatch._handle_edit_workspace_file",
            new=handler_mock,
        ):
            _run(_dispatch_tool_call(
                app=MagicMock(),
                provider=MagicMock(),
                user={"id": USER_ID, "email": "u@example.com"},
                conversation_id=CONV_ID,
                timezone="UTC",
                tool_name="tool_call",
                args={
                    "tool_name": "edit_workspace_file",
                    "arguments": {
                        "path": "a.txt",
                        "old_string": "old",
                        "new_string": "new",
                    },
                },
            ))

        handler_mock.assert_awaited_once_with(
            USER_ID, CONV_ID, "a.txt", "old", "new",
            replace_all=False, project_id=None,
        )


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_registry_entry(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        spec = TOOL_CALL_REGISTRY["edit_workspace_file"]
        assert spec["name"] == "edit_workspace_file"
        params = spec["parameters"]
        assert set(params["required"]) == {"path", "old_string", "new_string"}
        for prop in ("path", "old_string", "new_string", "replace_all", "intent_message"):
            assert prop in params["properties"]
        assert params["properties"]["replace_all"]["type"] == "boolean"
        # The description must teach the read-before-edit gate.
        assert "get_workspace_file" in spec["description"]
