"""Tests for the github_get_job_log plugin tool handler.

Covers the ``_handle_github_get_job_log`` flow in ``plugins/github/tools.py``:

* Happy path when GitHub returns the log body inline (HTTP 200).
* Happy path with the typical 302 -> signed URL redirect dance,
  confirming no Authorization header leaks to the signed URL host.
* Disallowed / invalid job_id is rejected before any HTTP call.
* Missing credentials returns ``github_oauth_required``.
* Response-size gate rejects large logs unless ``force_large_response``
  is set, and successful writes land in the workspace.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from types import SimpleNamespace

from chat.gemini_api.constants import AUTHED_GET_SIZE_LIMIT
from plugins.github.tools import _handle_github_get_job_log


def _call(user, conversation_id, **kwargs):
    """Invoke the plugin handler with the (ctx, args) tool signature."""
    ctx = SimpleNamespace(
        user=user,
        conversation_id=conversation_id,
        project_id=kwargs.pop("project_id", None),
    )
    return _handle_github_get_job_log(ctx, kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


USER_WITH_GITHUB = {
    "id": 1,
    "email": "u@example.com",
    "service_credentials": {
        "github": {
            "service": "github",
            "secret": None,
            "oauth_blob": {"access_token": "gho_testtoken123"},
        },
    },
}

USER_WITHOUT_GITHUB = {
    "id": 2,
    "email": "no-gh@example.com",
}


def _make_mock_response(status_code: int, content: bytes = b"", headers: dict | None = None):
    """Build a fake httpx.Response-like object for the initial (authed) call."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    resp.headers = headers or {}
    return resp


def _patch_initial_authed_request(response):
    """Patch ``_make_authed_request`` (imported inside the handler) to return
    a given response object or string.
    """
    return patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new_callable=AsyncMock,
        return_value=response,
    )


def _patch_signed_url_client(captured_calls: list, status_code: int, body: bytes):
    """Patch ``httpx.AsyncClient`` as used inside the handler for the signed
    URL fetch. Captures ``(url, headers)`` per request so we can assert no
    Authorization header was forwarded.
    """
    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.content = body
    response_mock.text = body.decode("utf-8", errors="replace") if body else ""

    async def fake_get(url, headers=None):
        captured_calls.append((url, dict(headers or {})))
        return response_mock

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=fake_get)

    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_mock)
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    return patch(
        "plugins.github.tools.httpx.AsyncClient",
        return_value=async_ctx,
    )


def _patch_workspace_dir(tmp_path: Path):
    """Patch ``conversation_workspace_dir`` so the handler writes to tmp_path."""
    async def fake_workspace(*args, **kwargs):
        return tmp_path

    return patch(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        side_effect=fake_workspace,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetGitHubJobLogHappyPath:
    """Success-path tests for _handle_github_get_job_log."""

    def test_inline_200_response_writes_file(self, tmp_path):
        """When GitHub responds 200 directly, write body to workspace."""
        body = b"log line 1\nlog line 2\n"
        initial_response = _make_mock_response(200, content=body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["filename"] == "github-job-42.log"
        assert parsed["path"] == "github-job-logs/github-job-42.log"
        assert parsed["size_bytes"] == len(body)
        assert "log line 1" in parsed["preview"]

        # File was written under the default github-job-logs/ folder
        written = tmp_path / "github-job-logs" / "github-job-42.log"
        assert written.exists()
        assert written.read_text(encoding="utf-8") == body.decode("utf-8")
        # And nothing was dropped at the workspace root
        assert not (tmp_path / "github-job-42.log").exists()

    def test_302_redirect_follows_signed_url_without_auth(self, tmp_path):
        """302 -> signed URL: second GET must not send Authorization."""
        signed_url = "https://productionresultssa.blob.core.windows.net/mock?sig=abc"
        initial_response = _make_mock_response(
            302, content=b"", headers={"Location": signed_url},
        )
        body = b"resolved log body from signed url\n"

        captured_signed_calls: list = []
        with _patch_initial_authed_request(initial_response), \
                _patch_signed_url_client(captured_signed_calls, 200, body), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="99",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["filename"] == "github-job-99.log"
        assert parsed["path"] == "github-job-logs/github-job-99.log"
        assert parsed["size_bytes"] == len(body)

        # One signed-URL GET, and it must NOT carry Authorization.
        assert len(captured_signed_calls) == 1
        signed_url_used, signed_headers = captured_signed_calls[0]
        assert signed_url_used == signed_url
        assert "Authorization" not in signed_headers
        assert "authorization" not in {k.lower() for k in signed_headers.keys()}

        # File was written with signed-URL body under github-job-logs/
        written = tmp_path / "github-job-logs" / "github-job-99.log"
        assert written.exists()
        assert written.read_text(encoding="utf-8") == body.decode("utf-8")


class TestGetGitHubJobLogErrors:
    """Failure-path tests for _handle_github_get_job_log."""

    def test_invalid_job_id_rejected_before_http(self, tmp_path):
        """Non-numeric job_id must be rejected without any HTTP call."""
        mock_authed = AsyncMock()
        with patch(
            "chat.gemini_api.authed_get._make_authed_request",
            mock_authed,
        ):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="not-a-number",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        assert "numeric" in parsed["error"].lower()
        mock_authed.assert_not_called()

    def test_missing_credentials_returns_github_oauth_required(self, tmp_path, github_plugin):
        """USER_WITHOUT_GITHUB must surface the github_oauth_required error."""
        # The real _make_authed_request returns the JSON error for missing
        # credentials, so we intentionally do not patch it here -- we want
        # to exercise the handler's error pass-through.  We do still need
        # to patch the workspace dir in case the handler tries to use it.
        with _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITHOUT_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        err = parsed["error"]
        # _make_authed_request stores the error dict from the registry entry
        if isinstance(err, dict):
            assert err.get("error") == "github_oauth_required"
        else:
            # Defensive: if it serialised to a string, confirm the keyword
            assert "github_oauth_required" in str(err)

    def test_302_with_no_location_header_returns_error(self, tmp_path):
        """A 302 without a Location header is an error, not a silent pass."""
        initial_response = _make_mock_response(302, content=b"", headers={})

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        assert "Location" in parsed["error"]


class TestGetGitHubJobLogSizeGate:
    """Size-gate tests mirroring the authed_get response_too_large path."""

    def test_large_log_rejected_without_force(self, tmp_path):
        """A log over AUTHED_GET_SIZE_LIMIT returns response_too_large."""
        big_body = b"x" * (AUTHED_GET_SIZE_LIMIT + 1024)
        initial_response = _make_mock_response(200, content=big_body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
            ))

        parsed = json.loads(result)
        assert parsed.get("error") == "response_too_large"
        assert parsed.get("response_size_bytes") == len(big_body)
        assert parsed.get("size_limit_bytes") == AUTHED_GET_SIZE_LIMIT
        # File should NOT exist yet (neither at root nor in default folder)
        assert not (tmp_path / "github-job-42.log").exists()
        assert not (tmp_path / "github-job-logs" / "github-job-42.log").exists()

    def test_large_log_written_with_force(self, tmp_path):
        """With force_large_response=True, the full log is persisted."""
        big_body = b"y" * (AUTHED_GET_SIZE_LIMIT + 2048)
        initial_response = _make_mock_response(200, content=big_body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                force_large_response=True,
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["size_bytes"] == len(big_body)
        assert parsed["path"] == "github-job-logs/github-job-42.log"

        written = tmp_path / "github-job-logs" / "github-job-42.log"
        assert written.exists()
        assert written.read_bytes() == big_body


class TestGetGitHubJobLogPathArg:
    """Tests for the optional ``path`` argument override."""

    def test_path_as_directory_with_trailing_slash(self, tmp_path):
        """path='logs/ci/' drops the default filename inside that dir."""
        body = b"log body for dir-with-slash\n"
        initial_response = _make_mock_response(200, content=body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                path="logs/ci/",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["filename"] == "github-job-42.log"
        assert parsed["path"] == "logs/ci/github-job-42.log"

        written = tmp_path / "logs" / "ci" / "github-job-42.log"
        assert written.exists()
        assert written.read_bytes() == body
        # Default folder must NOT be used when override is supplied
        assert not (tmp_path / "github-job-logs").exists()

    def test_path_as_existing_directory(self, tmp_path):
        """path='existing-dir' (no slash) is treated as a directory if it exists."""
        body = b"log body for existing dir\n"
        initial_response = _make_mock_response(200, content=body)

        # Pre-create the target directory inside the workspace
        (tmp_path / "existing-dir").mkdir()

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                path="existing-dir",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["filename"] == "github-job-42.log"
        assert parsed["path"] == "existing-dir/github-job-42.log"

        written = tmp_path / "existing-dir" / "github-job-42.log"
        assert written.exists()
        assert written.read_bytes() == body

    def test_path_as_full_filename(self, tmp_path):
        """path='logs/custom-name.log' uses the caller-chosen filename."""
        body = b"log body with custom filename\n"
        initial_response = _make_mock_response(200, content=body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                path="logs/custom-name.log",
            ))

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["filename"] == "custom-name.log"
        assert parsed["path"] == "logs/custom-name.log"

        written = tmp_path / "logs" / "custom-name.log"
        assert written.exists()
        assert written.read_bytes() == body
        # Default name must NOT be used when override is supplied
        assert not (tmp_path / "logs" / "github-job-42.log").exists()
        assert not (tmp_path / "github-job-logs").exists()

    def test_path_traversal_rejected(self, tmp_path):
        """path='../escape.log' must be rejected with a clear error."""
        body = b"should never be written\n"
        initial_response = _make_mock_response(200, content=body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                path="../escape.log",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        err = parsed["error"]
        assert isinstance(err, str)
        assert ".." in err or "traversal" in err.lower() or "outside" in err.lower()

        # Nothing must have been written anywhere under or above tmp_path
        assert not (tmp_path.parent / "escape.log").exists()
        assert not (tmp_path / "github-job-logs").exists()
        assert not (tmp_path / "github-job-42.log").exists()

    def test_absolute_path_rejected(self, tmp_path):
        """Absolute path='/tmp/absolute.log' must be rejected."""
        body = b"should never be written\n"
        initial_response = _make_mock_response(200, content=body)

        with _patch_initial_authed_request(initial_response), \
                _patch_workspace_dir(tmp_path):
            result = _run(_call(
                USER_WITH_GITHUB,
                "conv-123",
                owner="octo",
                repo="hello",
                job_id="42",
                path="/tmp/absolute.log",
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        err = parsed["error"]
        assert isinstance(err, str)
        assert "absolute" in err.lower()

        assert not (tmp_path / "github-job-logs").exists()
