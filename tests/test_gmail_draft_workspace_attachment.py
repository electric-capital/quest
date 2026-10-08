"""Tests for ``_resolve_workspace_attachment`` in ``api/gmail/draft_endpoints.py``.

Covers the resolver matrix called out in the dev plan:

* Standalone-conversation workspace happy path.
* Project-conversation happy path (the resolver reads the conversation
  workspace root and does not append ``"workspace"`` to it).
* Filename override — sanitised, with leading-dot stripping and
  separator replacement.
* Path-traversal rejections: absolute path, ``..`` parts, embedded
  ``../`` that resolves outside the workspace.
* Non-existent file -> 404.
* Directory (not a file) -> 400.
* MIME type fallback when ``mimetypes.guess_type`` returns ``None``.
* Empty ``workspace_path`` -> 400.

All tests stub ``ChatStorage.get_conversation_workspace_root`` so no DB or
filesystem layout is required outside of pytest's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api.gmail.draft_endpoints import _resolve_workspace_attachment


def _run(coro):
    return asyncio.run(coro)


def _patch_workspace_dir(workspace_dir: Path):
    """Patch the conversation-root resolver so the resolver reads from ``workspace_dir``."""
    return patch(
        "chat.storage.ChatStorage.get_conversation_workspace_root",
        side_effect=lambda conversation_id: workspace_dir,
    )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestWorkspaceAttachmentHappyPath:

    def test_reads_file_from_standalone_workspace(self, tmp_path):
        # Simulate ``data/chats/{conv}/workspace`` layout. The resolver
        # only sees the resolved workspace_dir; it does not care about
        # the parent directories.
        workspace = tmp_path / "chats" / "conv-1" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "notes.txt").write_text("hello world", encoding="utf-8")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment("conv-1", "notes.txt"))

        assert result["data"] == b"hello world"
        assert result["filename"] == "notes.txt"
        # ``mimetypes.guess_type("notes.txt")`` returns ``text/plain``.
        assert result["mime_type"] == "text/plain"

    def test_reads_file_from_project_workspace(self, tmp_path):
        # Project conversations read their own conversation workspace
        # root too. The resolver does not append an extra "workspace"
        # segment; it trusts whatever the root resolver returns.
        workspace = tmp_path / "chats" / "conv-2" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "report.pdf").write_bytes(b"%PDF-1.4 fake")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment(
                "conv-2", "report.pdf",
            ))

        assert result["data"] == b"%PDF-1.4 fake"
        assert result["filename"] == "report.pdf"
        assert result["mime_type"] == "application/pdf"

    def test_reads_nested_file(self, tmp_path):
        workspace = tmp_path / "workspace"
        nested = workspace / "out" / "sub"
        nested.mkdir(parents=True)
        (nested / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment(
                "conv-1", "out/sub/data.csv",
            ))

        assert result["data"] == b"a,b\n1,2\n"
        assert result["filename"] == "data.csv"
        assert result["mime_type"] == "text/csv"

    def test_filename_override_used(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "raw.bin").write_bytes(b"\x01\x02\x03")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment(
                "conv-1", "raw.bin", filename_override="cleaned.bin",
            ))

        assert result["filename"] == "cleaned.bin"

    def test_filename_override_sanitised(self, tmp_path):
        # ``_sanitize_workspace_filename`` strips path separators and
        # leading dots from the override.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src.txt").write_text("x", encoding="utf-8")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment(
                "conv-1", "src.txt",
                filename_override="..\\..\\evil/payload.exe",
            ))

        # Backslashes and forward-slashes become underscores; leading
        # dots are stripped. End result is the sanitised override.
        assert "/" not in result["filename"]
        assert "\\" not in result["filename"]
        assert not result["filename"].startswith(".")
        # Sanity check on the actual sanitised value: forward slashes
        # become underscores, backslashes become underscores, and the
        # leading dots are stripped one-by-one.
        assert result["filename"] == "_.._evil_payload.exe"

    def test_filename_override_falls_back_when_blank(self, tmp_path):
        # An override that sanitises down to an empty string falls back
        # to the resolved file's basename.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src.txt").write_text("x", encoding="utf-8")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment(
                "conv-1", "src.txt", filename_override="...",
            ))

        # "..." -> stripped to "" -> fall back to "src.txt"
        assert result["filename"] == "src.txt"

    def test_unknown_extension_uses_octet_stream(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Extensionless file -> mimetypes.guess_type returns None -> fallback
        (workspace / "blob").write_bytes(b"\x00\x01")

        with _patch_workspace_dir(workspace):
            result = _run(_resolve_workspace_attachment("conv-1", "blob"))

        assert result["mime_type"] == "application/octet-stream"


# ---------------------------------------------------------------------------
# Path-traversal and validation
# ---------------------------------------------------------------------------


class TestWorkspaceAttachmentValidation:

    def test_empty_path_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment("conv-1", ""))

        assert exc_info.value.status_code == 400
        assert "empty" in str(exc_info.value.detail).lower()

    def test_whitespace_only_path_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment("conv-1", "   "))

        assert exc_info.value.status_code == 400

    def test_absolute_path_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment(
                    "conv-1", "/etc/passwd",
                ))

        assert exc_info.value.status_code == 400
        assert "absolute" in str(exc_info.value.detail).lower()

    def test_dotdot_in_path_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment(
                    "conv-1", "../etc/passwd",
                ))

        assert exc_info.value.status_code == 400
        assert "traversal" in str(exc_info.value.detail).lower()

    def test_embedded_dotdot_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment(
                    "conv-1", "subdir/../../etc/passwd",
                ))

        # The ``..`` part rejection fires before the resolve check.
        assert exc_info.value.status_code == 400
        assert "traversal" in str(exc_info.value.detail).lower()

    def test_symlink_escape_rejected(self, tmp_path):
        # A symlink whose target is outside the workspace must be
        # rejected by the post-resolve relative_to() guard.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")

        link = workspace / "leaky"
        try:
            link.symlink_to(outside / "secret.txt")
        except OSError:
            pytest.skip("Symlinks not supported in this environment")

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment("conv-1", "leaky"))

        assert exc_info.value.status_code == 400
        assert "outside" in str(exc_info.value.detail).lower()

    def test_missing_file_returns_404(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment("conv-1", "nope.txt"))

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.detail).lower()

    def test_directory_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        sub = workspace / "subdir"
        sub.mkdir(parents=True)

        with _patch_workspace_dir(workspace):
            with pytest.raises(HTTPException) as exc_info:
                _run(_resolve_workspace_attachment("conv-1", "subdir"))

        assert exc_info.value.status_code == 400
        assert "regular file" in str(exc_info.value.detail).lower()
