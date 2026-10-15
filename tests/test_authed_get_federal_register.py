"""Tests for the Federal Register entry in the ``authed_get`` registry.

The Federal Register API is a free, public, UNAUTHENTICATED US government API.
It is modelled as a no-auth service: a sync no-arg loader returning ``None``,
a no-op auth injector, ``requires_user`` left unset, and a path-prefix-scoped
registry entry keyed ``www.federalregister.gov/api/v1``.

Covers:
* The entry is present with ``path_prefix == "/api/v1"`` and does NOT set
  ``requires_user`` or ``retry_on_401``.
* A mocked request succeeds with exactly one HTTP call and injects NO
  ``Authorization`` header (the no-auth assertion), while the polite
  ``User-Agent`` default header is present.
* Allowed read-only paths match; unknown / wrong-version paths are rejected
  without an HTTP call.
* Non-``/api/v1`` paths on the same host resolve to no service (the human
  website is not proxied).

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _find_service,
    _make_authed_request,
)


_FR_KEY = "www.federalregister.gov/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _mock_httpx_client(captured_calls: list, status_code: int = 200, body: dict | None = None):
    """Build a patch target that captures client.get() calls.

    Patches ``httpx.AsyncClient`` inside ``chat.gemini_api.authed_get`` so each
    ``client.get(url, headers=...)`` is recorded to ``captured_calls`` as
    ``(url, headers)`` tuples and returns an ``httpx.Response``-like object with
    the given status code and body.
    """
    if body is None:
        body = {"ok": True}
    response_text = json.dumps(body)

    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.text = response_text
    response_mock.json = MagicMock(return_value=body)

    async def fake_get(url, headers=None):
        captured_calls.append((str(url), dict(headers or {})))
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


# ---------------------------------------------------------------------------
# Tests: registry shape
# ---------------------------------------------------------------------------

class TestFederalRegisterRegistryEntry:
    """Verify the static shape of the Federal Register registry entry."""

    def test_entry_present_with_path_prefix(self):
        assert _FR_KEY in _SERVICE_REGISTRY
        entry = _SERVICE_REGISTRY[_FR_KEY]
        assert entry["path_prefix"] == "/api/v1"
        assert entry["name"] == "Federal Register"

    def test_entry_is_no_auth(self):
        """No per-user requirement and no token-refresh retry -- it's public."""
        entry = _SERVICE_REGISTRY[_FR_KEY]
        # requires_user defaults to False; must NOT be truthy.
        assert not entry.get("requires_user", False)
        assert "retry_on_401" not in entry or entry["retry_on_401"] is False
        # No missing-credentials error (there are no credentials).
        assert "missing_credentials_error" not in entry

    def test_entry_loader_is_sync_no_arg_returning_none(self):
        """The loader must be sync/no-arg returning None (the no-auth shape)."""
        entry = _SERVICE_REGISTRY[_FR_KEY]
        loader = entry["load_credentials"]
        assert not asyncio.iscoroutinefunction(loader)
        assert loader() is None

    def test_entry_has_user_agent_default_header(self):
        entry = _SERVICE_REGISTRY[_FR_KEY]
        defaults = entry.get("default_headers") or {}
        assert defaults.get("User-Agent") == "Quest/1.0"

    def test_allowed_endpoints_cover_core_paths(self):
        entry = _SERVICE_REGISTRY[_FR_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/api/v1/documents.json",
            "/api/v1/documents.csv",
            "/api/v1/documents",
            "/api/v1/documents/2026-10606.json",
            "/api/v1/documents/2026-10606,2026-10607.json",
            "/api/v1/documents/facets/daily",
            "/api/v1/public-inspection-documents.json",
            "/api/v1/public-inspection-documents/current.json",
            "/api/v1/public-inspection-documents/2026-10606.json",
            "/api/v1/agencies",
            "/api/v1/agencies.json",
            "/api/v1/agencies/environmental-protection-agency",
            "/api/v1/agencies/44",
            "/api/v1/suggested_searches",
            "/api/v1/suggested_searches/business-and-industry",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected path to be allowed: {path}"

    def test_allowed_endpoints_reject_unknown_paths(self):
        entry = _SERVICE_REGISTRY[_FR_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            "/api/v2/documents",                  # wrong API version
            "/api/v1/some-other-path",            # not in allow-list
            "/api/v1/documents/2026/05/28",       # too deep for single-doc
            "/api/v1/images/abc123",              # binary images intentionally excluded
            "/api/v1/agencies/44/extra",          # too deep for single agency
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: _find_service path scoping
# ---------------------------------------------------------------------------

class TestFederalRegisterServiceLookup:
    """The host is path-scoped so the human website is not proxied."""

    def test_api_v1_path_resolves_to_service(self):
        service = _find_service("www.federalregister.gov", "/api/v1/documents.json")
        assert service is not None
        assert service["name"] == "Federal Register"

    def test_non_api_path_resolves_to_no_service(self):
        # A human-website path (no /api/v1 prefix) must NOT resolve -- there is
        # no plain hostname key, so the path-prefix lookup returns None.
        service = _find_service(
            "www.federalregister.gov", "/documents/2026/05/28/some-rule",
        )
        assert service is None


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for Federal Register
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestFederalRegister:
    """Exercise _make_authed_request() against the Federal Register entry."""

    def test_allowed_request_injects_no_auth_but_has_user_agent(self):
        calls: list = []
        body = {"count": 1, "results": [{"document_number": "2026-10606"}]}
        with _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/documents.json"
                "?per_page=1&fields%5B%5D=document_number",
                # No user supplied -- the API needs none.
            ))

        parsed = json.loads(result)
        assert parsed == body

        # Exactly one HTTP call.
        assert len(calls) == 1
        url, headers = calls[0]
        assert url.startswith(
            "https://www.federalregister.gov/api/v1/documents.json"
        )
        # The no-auth assertion: NO Authorization header injected.
        assert "Authorization" not in headers
        # Polite default UA still present.
        assert headers.get("User-Agent") == "Quest/1.0"

    def test_works_without_user_context(self):
        """A no-auth service must not require a user dict."""
        calls: list = []
        with _mock_httpx_client(calls, body={"name": "EPA"}):
            result = _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/agencies/"
                "environmental-protection-agency",
                user=None,
            ))
        parsed = json.loads(result)
        assert parsed == {"name": "EPA"}
        assert len(calls) == 1
        _, headers = calls[0]
        assert "Authorization" not in headers

    def test_disallowed_path_returns_error_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/images/abc123",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Federal Register" in parsed["error"]
        assert "/api/v1/images/abc123" in parsed["error"]
        assert calls == []

    def test_non_api_path_returns_unknown_host_error(self):
        """Human-website paths are not a known service -> unknown-host error."""
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://www.federalregister.gov/documents/2026/05/28/some-rule",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown service host" in parsed["error"]
        assert calls == []

    def test_multiple_allowed_paths_each_make_one_call(self):
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/documents/facets/daily",
            ))
            _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/"
                "public-inspection-documents/current.json",
            ))
            _run(_make_authed_request(
                "https://www.federalregister.gov/api/v1/suggested_searches",
            ))
        assert len(calls) == 3
        for _, headers in calls:
            assert "Authorization" not in headers
