"""Tests for the GitHub plugin's ``authed_get`` service registry entry.

The ``api.github.com`` entry is registered by the github plugin
(plugins/github), so every test in this module runs with the plugin
registered via the autouse fixture below.

Covers:
* GitHub is registered with the expected shape (401 retry backstop for
  expiring GitHub App tokens, has ``default_headers``, ``requires_user``).
* ``default_headers`` are merged into outgoing requests with the
  documented precedence (defaults < caller < inject_auth).
* Allowed-endpoint gating accepts known paths and rejects unknown ones.
* Missing-credentials errors are returned when ``github_oauth`` is absent.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _make_authed_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _mock_httpx_client(captured_calls: list, status_code: int = 200, body: dict | None = None):
    """Build a patch target that captures client.get() calls.

    Returns a context manager that patches ``httpx.AsyncClient`` inside
    ``chat.gemini_api.authed_get`` so every ``client.get(url, headers=...)``
    is recorded to ``captured_calls`` as ``(url, headers)`` tuples, and
    returns an ``httpx.Response``-like object with the given status code
    and body.
    """
    if body is None:
        body = {"ok": True}
    response_text = json.dumps(body)

    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.text = response_text
    response_mock.json = MagicMock(return_value=body)

    async def fake_get(url, headers=None):
        captured_calls.append((url, dict(headers or {})))
        return response_mock

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=fake_get)

    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_mock)
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    return patch(
        "chat.gemini_api.authed_get.httpx.AsyncClient",
        return_value=async_ctx,
    )


@pytest.fixture(autouse=True)
def _register_github_plugin(github_plugin):
    """All tests here exercise the plugin-registered registry entry."""
    yield


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


# ---------------------------------------------------------------------------
# Tests: registry shape
# ---------------------------------------------------------------------------

class TestGitHubRegistryEntry:
    """Verify the static shape of the GitHub registry entry."""

    def test_github_entry_present(self):
        assert "api.github.com" in _SERVICE_REGISTRY

    def test_github_entry_requires_user(self):
        entry = _SERVICE_REGISTRY["api.github.com"]
        assert entry["requires_user"] is True

    def test_github_entry_has_retry_on_401(self):
        """GitHub App user tokens expire, so a 401 re-runs the loader."""
        entry = _SERVICE_REGISTRY["api.github.com"]
        assert entry["retry_on_401"] is True

    def test_github_entry_has_expected_default_headers(self):
        entry = _SERVICE_REGISTRY["api.github.com"]
        defaults = entry.get("default_headers") or {}
        assert defaults.get("User-Agent") == "Quest/1.0"
        assert defaults.get("Accept") == "application/vnd.github+json"

    def test_github_entry_has_missing_credentials_error(self):
        entry = _SERVICE_REGISTRY["api.github.com"]
        err = entry.get("missing_credentials_error") or {}
        assert err.get("error") == "github_oauth_required"
        assert "Settings" in err.get("message", "")

    def test_github_allowed_endpoints_cover_core_paths(self):
        """Spot-check that all the paths exposed by the old proxy are covered."""
        entry = _SERVICE_REGISTRY["api.github.com"]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/user",
            "/user/repos",
            "/user/orgs",
            "/repos/owner/repo",
            "/repos/owner/repo/branches",
            "/repos/owner/repo/stargazers",
            "/repos/owner/repo/issues",
            "/repos/owner/repo/issues/42",
            "/repos/owner/repo/issues/42/comments",
            "/repos/owner/repo/pulls",
            "/repos/owner/repo/pulls/123",
            "/repos/owner/repo/pulls/123/files",
            "/repos/owner/repo/pulls/123/reviews",
            "/repos/owner/repo/commits",
            "/repos/owner/repo/commits/abc123",
            "/repos/owner/repo/contents",                       # directory root
            "/repos/owner/repo/contents/README.md",
            "/repos/owner/repo/contents/src/deep/nested.py",
            "/orgs/my-org/repos",
            "/search/repositories",
            "/search/issues",
            "/search/code",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected path to be allowed: {path}"

    def test_github_allowed_endpoints_cover_actions_paths(self):
        """Verify GitHub Actions (CI/CD) read paths are in the allow-list."""
        entry = _SERVICE_REGISTRY["api.github.com"]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/repos/owner/repo/actions/runs",
            "/repos/owner/repo/actions/runs/12345",
            "/repos/owner/repo/actions/runs/12345/jobs",
            "/repos/owner/repo/actions/runs/12345/attempts/2/jobs",
            "/repos/owner/repo/actions/jobs/67890",
            "/repos/owner/repo/actions/jobs/67890/logs",
            "/repos/owner/repo/actions/runs/12345/logs",
            "/repos/owner/repo/actions/workflows",
            "/repos/owner/repo/actions/workflows/ci.yml",
            "/repos/owner/repo/actions/workflows/12345",
            "/repos/owner/repo/actions/workflows/ci.yml/runs",
            "/repos/owner/repo/actions/workflows/12345/runs",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected Actions path to be allowed: {path}"

    def test_github_allowed_endpoints_reject_unknown_paths(self):
        entry = _SERVICE_REGISTRY["api.github.com"]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            "/gists",                                        # not in allow-list
            "/users/someone",                                # per plan, not exposed
            "/repos/owner/repo/collaborators",               # write-ish -- not exposed
            "/repos/owner/repo/stargazers/extra",            # no sub-paths under stargazers
            "/repos/owner/repo/issues/abc",                  # non-numeric issue number
            # --- Actions negative cases ---
            "/repos/owner/repo/actions/runs/abc",            # non-numeric run_id
            "/repos/owner/repo/actions/runs/abc/jobs",       # non-numeric run_id
            "/repos/owner/repo/actions/jobs/abc",            # non-numeric job_id
            "/repos/owner/repo/actions/jobs/abc/logs",       # non-numeric job_id
            "/repos/owner/repo/actions/artifacts",           # artifacts not allow-listed
            "/repos/owner/repo/actions/secrets",             # secrets not allow-listed
            "/repos/owner/repo/actions/runners",             # runners not allow-listed
            "/repos/owner/repo/actions/caches",              # caches not allow-listed
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for GitHub
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestGitHub:
    """Exercise _make_authed_request() against the GitHub service entry."""

    def test_allowed_request_injects_default_headers_and_bearer(self):
        calls: list = []
        with _mock_httpx_client(calls, body={"login": "octocat"}):
            result = _run(_make_authed_request(
                "https://api.github.com/user",
                user=USER_WITH_GITHUB,
            ))

        # Success returns the raw response body text (JSON string).
        parsed = json.loads(result)
        assert parsed == {"login": "octocat"}

        assert len(calls) == 1
        url, headers = calls[0]
        assert url == "https://api.github.com/user"
        # Service default headers merged in
        assert headers.get("User-Agent") == "Quest/1.0"
        assert headers.get("Accept") == "application/vnd.github+json"
        # Auth injected
        assert headers.get("Authorization") == "Bearer gho_testtoken123"

    def test_caller_accept_header_overrides_default_accept(self):
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            _run(_make_authed_request(
                "https://api.github.com/user",
                headers={"Accept": "application/json"},
                user=USER_WITH_GITHUB,
            ))

        _, headers = calls[0]
        # Caller overrode the default Accept header (allow-listed)
        assert headers.get("Accept") == "application/json"
        # Default User-Agent still present (caller did not override it)
        assert headers.get("User-Agent") == "Quest/1.0"
        # Auth still injected
        assert headers.get("Authorization") == "Bearer gho_testtoken123"

    def test_non_allow_listed_caller_header_is_rejected_without_http_call(self):
        """Only content-negotiation headers may be caller-supplied."""
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            result = _run(_make_authed_request(
                "https://api.github.com/user",
                headers={"Accept": "application/json", "X-Custom": "yes"},
                user=USER_WITH_GITHUB,
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: X-Custom" in parsed["error"]
        assert calls == []

    def test_caller_authorization_header_is_rejected_without_http_call(self):
        """A caller Authorization header is rejected outright, never merged."""
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            result = _run(_make_authed_request(
                "https://api.github.com/user",
                headers={"Authorization": "Bearer hacker-token"},
                user=USER_WITH_GITHUB,
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: Authorization" in parsed["error"]
        assert calls == []

    def test_disallowed_path_returns_error_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.github.com/gists",
                user=USER_WITH_GITHUB,
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        # Error message references GitHub and the offending path
        assert "GitHub" in parsed["error"]
        assert "/gists" in parsed["error"]
        # No HTTP call should have been made
        assert calls == []

    def test_missing_credentials_returns_github_oauth_required(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.github.com/user",
                user=USER_WITHOUT_GITHUB,
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        err = parsed["error"]
        # The error is stored as the dict defined in the registry.
        assert isinstance(err, dict)
        assert err.get("error") == "github_oauth_required"
        assert "Settings" in err.get("message", "")
        # No HTTP call should have been made
        assert calls == []

    def test_contents_root_and_nested_paths_allowed(self):
        calls: list = []
        with _mock_httpx_client(calls, body=[]):
            _run(_make_authed_request(
                "https://api.github.com/repos/owner/repo/contents",
                user=USER_WITH_GITHUB,
            ))
            _run(_make_authed_request(
                "https://api.github.com/repos/owner/repo/contents/src/deep/file.py",
                user=USER_WITH_GITHUB,
            ))

        assert len(calls) == 2
        assert calls[0][0].endswith("/contents")
        assert calls[1][0].endswith("/contents/src/deep/file.py")
