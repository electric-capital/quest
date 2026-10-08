"""Tests for the ``output_file`` parameter on ``handle_authed_get``.

Covers the "write upstream response body directly to the conversation
workspace" branch added in plan 00073, with the plan-00103 re-rooting of
``output_file`` under the hidden ``.responses/`` workspace subdirectory:

* Happy path: small JSON body lands in ``<workspace>/.responses/`` and a
  receipt (whose ``path`` starts with ``.responses/``) is returned (not the
  body).
* Happy path with ``alt=media`` binary content: the alt=media block is
  lifted when ``output_file`` is set so binary bytes go straight to disk.
* Path-resolution edges: absolute paths and ``..`` segments are rejected;
  a trailing ``/`` triggers the synthesized-filename behaviour.
* Upstream HTTP error short-circuit: no file is written when the upstream
  returned 4xx/5xx.
* Missing ``conversation_id`` is rejected before any HTTP call.
* ``output_file`` wins over ``force_large_response`` (no blob written,
  workspace file written instead).
* ``_publish_file_list_changed`` is invoked best-effort.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.authed_get import handle_authed_get
from chat.gemini_api.constants import AUTHED_GET_SIZE_LIMIT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


USER = {"id": 42, "email": "u@example.com"}


def _make_response(
    *,
    status_code: int = 200,
    content: bytes = b"",
    text: str | None = None,
    headers: dict | None = None,
):
    """Build a fake httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = text if text is not None else content.decode("utf-8", errors="replace")
    resp.headers = headers or {"content-type": "application/json"}
    return resp


def _patch_workspace_dir(tmp_path: Path):
    """Patch ``conversation_workspace_dir`` so the handler writes to ``tmp_path``."""
    async def fake_workspace(*args, **kwargs):
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    return patch(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        new=fake_workspace,
    )


def _patch_make_request(response):
    """Patch ``_make_authed_request`` to return a given value.

    For the ``output_file`` branch the handler always passes
    ``raw_response=True`` so ``response`` should be either an
    ``httpx.Response``-like object (success path) or a JSON error string
    (failure / validation path).
    """
    return patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new_callable=AsyncMock,
        return_value=response,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutputFileHappyPath:
    """Body lands in the workspace; receipt is returned to the LLM."""

    def test_writes_response_to_workspace_and_returns_receipt(self, tmp_path):
        body = b'{"hello":"world"}'
        response = _make_response(
            content=body,
            headers={"content-type": "application/json; charset=utf-8"},
        )

        publish_mock = MagicMock()
        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
            new=publish_mock,
        ):
            result = _run(handle_authed_get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                user=USER,
                conversation_id="conv-1",
                output_file="dump.json",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        # output_file is transparently re-rooted under the hidden .responses/ dir.
        assert parsed["path"] == ".responses/dump.json"
        assert parsed["filename"] == "dump.json"
        assert parsed["bytes_written"] == len(body)
        assert parsed["status_code"] == 200
        assert parsed["content_type"].startswith("application/json")

        written = tmp_path / "workspace" / ".responses" / "dump.json"
        assert written.exists()
        assert written.read_bytes() == body

        publish_mock.assert_called_once_with(USER["id"], "conversation", "conv-1", None)

    def test_writes_into_subdirectory(self, tmp_path):
        body = b"hello"
        response = _make_response(
            content=body, headers={"content-type": "text/plain"},
        )

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="conv-2",
                output_file="dumps/gh.txt",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["path"] == ".responses/dumps/gh.txt"
        written = tmp_path / "workspace" / ".responses" / "dumps" / "gh.txt"
        assert written.read_bytes() == body

    def test_errors_on_existing_file(self, tmp_path):
        # Collisions in .responses/ must not clobber a previously saved body.
        ws = tmp_path / "workspace"
        responses_dir = ws / ".responses"
        responses_dir.mkdir(parents=True, exist_ok=True)
        existing = responses_dir / "x.json"
        existing.write_text("old")

        body = b'{"new":true}'
        response = _make_response(content=body)

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                output_file="x.json",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        assert "already exists" in parsed["error"]
        # The pre-existing file is left untouched.
        assert existing.read_text() == "old"


class TestAltMediaUnlocked:
    """``alt=media`` is blocked normally but allowed when output_file is set."""

    def test_alt_media_blocked_without_output_file(self, tmp_path):
        # No need to mock the request -- the alt=media block short-circuits
        # before anything else.
        with _patch_workspace_dir(tmp_path):
            result = _run(handle_authed_get(
                "https://www.googleapis.com/drive/v3/files/abc?alt=media",
                user=USER,
                conversation_id="conv-3",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "alt=media" in parsed["error"]

    def test_alt_media_unlocked_when_output_file_set(self, tmp_path):
        binary = b"\x00\x01\x02\x03binary-bytes"
        response = _make_response(
            content=binary,
            headers={"content-type": "application/pdf"},
        )

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://www.googleapis.com/drive/v3/files/abc?alt=media",
                user=USER,
                conversation_id="conv-3",
                output_file="downloads/file.pdf",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["bytes_written"] == len(binary)
        assert parsed["content_type"] == "application/pdf"
        written = tmp_path / "workspace" / ".responses" / "downloads" / "file.pdf"
        assert written.read_bytes() == binary


class TestPathResolution:
    """Absolute paths, traversal, directory-vs-file resolution."""

    def test_rejects_absolute_path(self, tmp_path):
        # No request is issued -- pre-validation runs first.
        with _patch_workspace_dir(tmp_path):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                output_file="/etc/passwd",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "absolute" in parsed["error"].lower()

    def test_rejects_parent_traversal(self, tmp_path):
        with _patch_workspace_dir(tmp_path):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                output_file="../escape.json",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert ".." in parsed["error"] or "traversal" in parsed["error"].lower()

    def test_trailing_slash_synthesizes_filename(self, tmp_path):
        body = b'{"a":1}'
        response = _make_response(
            content=body,
            headers={"content-type": "application/json"},
        )

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                output_file="dumps/",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        # Synthesized name shape: authed-get-<16hex>.json, re-rooted under .responses/.
        assert parsed["path"].startswith(".responses/dumps/authed-get-")
        assert parsed["path"].endswith(".json")
        assert parsed["filename"].startswith("authed-get-")
        written = tmp_path / "workspace" / parsed["path"]
        assert written.read_bytes() == body

    def test_existing_directory_synthesizes_filename(self, tmp_path):
        ws = tmp_path / "workspace"
        # The existing-directory probe runs under .responses/, so the dir
        # must already exist there for the synth-filename branch to trigger.
        target_dir = ws / ".responses" / "existing"
        target_dir.mkdir(parents=True, exist_ok=True)

        body = b"<html>hi</html>"
        response = _make_response(
            content=body, headers={"content-type": "text/html"},
        )

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                output_file="existing",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["path"].startswith(".responses/existing/authed-get-")
        assert parsed["path"].endswith(".html")


class TestUpstreamErrorShortCircuit:
    """4xx/5xx upstream errors must NOT write to disk."""

    def test_http_error_does_not_write_file(self, tmp_path):
        # _make_authed_request maps upstream 4xx/5xx into a JSON error
        # string. With raw_response=True, the same mapping still applies.
        error_str = json.dumps({
            "error": {
                "status_code": 404,
                "service": "GitHub",
                "response": {"message": "Not Found"},
            },
        })

        publish_mock = MagicMock()
        with _patch_make_request(error_str), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
            new=publish_mock,
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/repos/foo/bar",
                user=USER,
                conversation_id="c",
                output_file="response.json",
            ))

        # The error envelope is passed through unchanged.
        assert result == error_str
        # No file should have been written.
        ws = tmp_path / "workspace"
        if ws.exists():
            assert not (ws / "response.json").exists()
        publish_mock.assert_not_called()


class TestMissingConversationId:
    def test_returns_error_when_conversation_id_is_none(self, tmp_path):
        # Should error before any HTTP call -- patch the request to assert
        # it is never invoked.
        request_mock = AsyncMock()
        with patch(
            "chat.gemini_api.authed_get._make_authed_request",
            new=request_mock,
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id=None,
                output_file="x.json",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "conversation" in parsed["error"].lower()
        request_mock.assert_not_awaited()


class TestPrecedenceOverForceLargeResponse:
    """``output_file`` wins over ``force_large_response``."""

    def test_output_file_wins_over_force_large_response(self, tmp_path):
        # Body large enough that the inline path would route to the blob
        # store. With output_file set we expect the workspace write
        # instead and no blob file.
        body = b"x" * (AUTHED_GET_SIZE_LIMIT + 5_000)
        response = _make_response(
            content=body, headers={"content-type": "application/json"},
        )

        with _patch_make_request(response), _patch_workspace_dir(tmp_path), patch(
            "chat.gemini_api.tool_handlers._publish_file_list_changed",
        ):
            result = _run(handle_authed_get(
                "https://api.github.com/user",
                user=USER,
                conversation_id="c",
                project_id=None,
                output_file="big.json",
                force_large_response=True,
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["bytes_written"] == len(body)
        assert "stored" not in parsed
        assert "hash" not in parsed
        # Workspace write happened (under the hidden .responses/ dir).
        assert (tmp_path / "workspace" / ".responses" / "big.json").read_bytes() == body
        # No blob path under tmp_path/responses/ because we never went
        # down the blob branch. (Distinct from the in-workspace
        # workspace/.responses/ dir above.)
        assert not (tmp_path / "responses").exists()
