"""Tests for the Google Slides entry in the ``authed_get`` registry.

The Google Slides REST API (host ``slides.googleapis.com``) is a per-user
OAuth Google Workspace service, modelled exactly like Google Docs / Sheets: it
reuses the shared ``_load_google_services_credentials`` loader and
``_inject_google_bearer_auth`` injector, sets ``requires_user`` and
``retry_on_401`` to ``True``, and is keyed by a plain hostname (no
``path_prefix`` -- path gating is done entirely by ``allowed_endpoints``).

Covers:
* The entry is present (key ``slides.googleapis.com``), name ``Google Slides``,
  and is a plain hostname key (no ``path_prefix``).
* The per-user OAuth model: ``requires_user`` and ``retry_on_401`` are True, and
  it reuses the shared Google loader/injector (no Slides-specific helpers).
* The two allowed read-only path families (get presentation, get one page) match;
  the list/no-id, no-page-id, batchUpdate, extra-segment, and unrelated paths are
  rejected without an HTTP call.
* ``_find_service`` resolves the host via the plain-hostname fallback.
* A mocked request with resolvable credentials injects an
  ``Authorization: Bearer ...`` header and makes exactly one HTTP call.
* A request with no user / no credentials returns the
  ``google_services_auth_required`` missing-credentials error without an HTTP
  call.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api import authed_get
from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _find_service,
    _inject_google_bearer_auth,
    _load_google_services_credentials,
    _make_authed_request,
)


_SLIDES_KEY = "slides.googleapis.com"


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


def _mock_credentials_loader(token: str | None = "ya29.test-token"):
    """Patch the underlying Google credential resolver.

    The registry entry captures the ``_load_google_services_credentials``
    function object at module-load time, so patching the module-level name has
    no effect on the stored reference. Instead patch the underlying
    ``get_valid_service_credentials`` (which the loader awaits) so the loader
    resolves to creds with a ``.token`` (or ``None`` for the missing-creds case).
    """
    creds = None
    if token is not None:
        creds = MagicMock()
        creds.token = token
    return patch.object(
        authed_get,
        "get_valid_service_credentials",
        new=AsyncMock(return_value=creds),
    )


# ---------------------------------------------------------------------------
# Tests: registry shape
# ---------------------------------------------------------------------------

class TestSlidesRegistryEntry:
    """Verify the static shape of the Google Slides registry entry."""

    def test_entry_present_as_plain_hostname_key(self):
        assert _SLIDES_KEY in _SERVICE_REGISTRY
        entry = _SERVICE_REGISTRY[_SLIDES_KEY]
        assert entry["name"] == "Google Slides"
        # Plain hostname key: no path_prefix (gating is by allowed_endpoints).
        assert "path_prefix" not in entry

    def test_entry_is_per_user_oauth(self):
        """Per-user OAuth with token-refresh retry -- like Docs / Sheets."""
        entry = _SERVICE_REGISTRY[_SLIDES_KEY]
        assert entry["requires_user"] is True
        assert entry["retry_on_401"] is True
        # Uses the default google_services_auth_required error.
        assert "missing_credentials_error" not in entry

    def test_entry_reuses_shared_google_helpers(self):
        """Slides must reuse the shared Google loader/injector, not bespoke ones."""
        entry = _SERVICE_REGISTRY[_SLIDES_KEY]
        assert entry["load_credentials"] is _load_google_services_credentials
        assert entry["inject_auth"] is _inject_google_bearer_auth

    def test_allowed_endpoints_cover_read_only_paths(self):
        entry = _SERVICE_REGISTRY[_SLIDES_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/v1/presentations/ABC123",
            "/v1/presentations/1aBc-_xyz",
            "/v1/presentations/ABC123/pages/p1",
            "/v1/presentations/ABC123/pages/g123_0_1",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected path to be allowed: {path}"

    def test_allowed_endpoints_reject_unknown_paths(self):
        entry = _SERVICE_REGISTRY[_SLIDES_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            "/v1/presentations",                       # list -- no list endpoint exposed
            "/v1/presentations/",                       # trailing slash, no id
            "/v1/presentations/ABC123/pages",           # missing page id
            "/v1/presentations/ABC123:batchUpdate",     # write surface
            "/v1/presentations/ABC/extra/seg",          # extra segment
            "/v1/presentations/ABC123/pages/p1/thumbnail",  # thumbnail excluded
            "/v2/presentations/ABC123",                 # wrong version
            "/v1/documents/ABC123",                     # unrelated path
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: _find_service path scoping
# ---------------------------------------------------------------------------

class TestSlidesServiceLookup:
    """slides.googleapis.com resolves via the plain-hostname fallback."""

    def test_host_resolves_to_service(self):
        service = _find_service("slides.googleapis.com", "/v1/presentations/ABC123")
        assert service is not None
        assert service["name"] == "Google Slides"

    def test_page_path_resolves_to_service(self):
        service = _find_service(
            "slides.googleapis.com", "/v1/presentations/ABC123/pages/p1",
        )
        assert service is not None
        assert service["name"] == "Google Slides"


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for Google Slides
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestSlides:
    """Exercise _make_authed_request() against the Google Slides entry."""

    def test_allowed_request_injects_bearer_auth(self):
        calls: list = []
        body = {"presentationId": "ABC123", "title": "Q3 Deck"}
        with _mock_credentials_loader("ya29.live-token"), \
                _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://slides.googleapis.com/v1/presentations/ABC123",
                user={"email": "user@example.com", "google_services_oauth": {}},
            ))

        parsed = json.loads(result)
        assert parsed == body

        # Exactly one HTTP call.
        assert len(calls) == 1
        url, headers = calls[0]
        assert url.startswith("https://slides.googleapis.com/v1/presentations/ABC123")
        assert headers.get("Authorization") == "Bearer ya29.live-token"

    def test_page_request_injects_bearer_auth(self):
        calls: list = []
        body = {"objectId": "p1"}
        with _mock_credentials_loader("ya29.page-token"), \
                _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://slides.googleapis.com/v1/presentations/ABC123/pages/p1",
                user={"email": "user@example.com", "google_services_oauth": {}},
            ))
        parsed = json.loads(result)
        assert parsed == body
        assert len(calls) == 1
        _, headers = calls[0]
        assert headers.get("Authorization") == "Bearer ya29.page-token"

    def test_disallowed_path_returns_error_without_http_call(self):
        calls: list = []
        with _mock_credentials_loader(), _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://slides.googleapis.com/v1/presentations/ABC123:batchUpdate",
                user={"email": "user@example.com", "google_services_oauth": {}},
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Google Slides" in parsed["error"]
        assert "batchUpdate" in parsed["error"]
        assert calls == []

    def test_no_user_returns_error_without_http_call(self):
        """A requires_user service with no user context errors before any HTTP call."""
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://slides.googleapis.com/v1/presentations/ABC123",
                user=None,
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert calls == []

    def test_missing_credentials_returns_google_auth_required(self):
        """A user with no resolvable creds gets google_services_auth_required, no call."""
        calls: list = []
        with _mock_credentials_loader(token=None), _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://slides.googleapis.com/v1/presentations/ABC123",
                user={"email": "user@example.com"},
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        # Default Google missing-credentials error.
        assert parsed["error"]["error"] == "google_services_auth_required"
        assert calls == []
