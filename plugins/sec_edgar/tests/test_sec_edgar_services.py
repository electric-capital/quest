"""Tests for the SEC EDGAR plugin's two ``authed_get`` service entries.

``data.sec.gov`` and ``www.sec.gov`` are registered by the plugin
(plugins/sec_edgar), so every test here runs with it registered via the
autouse fixture below.

The SEC EDGAR data API is a free, public, UNAUTHENTICATED US government API.
It is modelled as a no-auth service: a sync no-arg loader returning ``None``,
a no-op auth injector, ``requires_user`` left unset, and a plain-hostname
registry entry keyed ``data.sec.gov`` (no ``path_prefix`` -- path gating is
done entirely by ``allowed_endpoints``).

The single SEC-specific detail: SEC's fair-access policy returns HTTP 403 to
requests lacking a descriptive ``User-Agent``, so the entry carries a
``default_headers`` ``User-Agent`` (with a contact token).

Covers:
* The entry is present (key ``data.sec.gov``), name ``SEC EDGAR``, and does NOT
  set ``requires_user``, ``retry_on_401``, or ``missing_credentials_error``.
* The loader is sync/no-arg returning ``None``, and a non-empty contact-bearing
  ``User-Agent`` default header is present.
* The five allowed read-only path families match for a sample CIK; non-padded
  CIKs and missing ``.json`` are rejected without an HTTP call.
* A second plain-hostname entry keyed ``www.sec.gov`` allow-lists EXACTLY the two
  ticker->CIK map files (``/files/company_tickers.json`` and
  ``/files/company_tickers_exchange.json``); any other ``www.sec.gov`` path is
  denied. Both resolve via the plain-hostname fallback in ``_find_service``.
* A mocked request succeeds with exactly one HTTP call and injects NO
  ``Authorization`` header, while the ``User-Agent`` default header is present.
* Unregistered (plugin not loaded), neither host resolves at all.

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
from plugins.sec_edgar.manifest import (
    DATA_HOST,
    FILES_HOST,
    inject_no_auth,
    load_sec_edgar_credentials,
)


_SEC_KEY = DATA_HOST
_SEC_FILES_KEY = FILES_HOST


@pytest.fixture(autouse=True)
def _register_plugin(sec_edgar_plugin):
    """All tests here exercise the plugin-registered registry entries."""
    yield


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

class TestSecEdgarRegistryEntry:
    """Verify the static shape of the SEC EDGAR registry entry."""

    def test_entry_present_as_plain_hostname_key(self):
        assert _SEC_KEY in _SERVICE_REGISTRY
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        assert entry["name"] == "SEC EDGAR"
        # Plain hostname key: no path_prefix (gating is by allowed_endpoints).
        assert "path_prefix" not in entry
        # The plugin's own loader / injector.
        assert entry["load_credentials"] is load_sec_edgar_credentials
        assert entry["inject_auth"] is inject_no_auth

    def test_entry_has_no_post_allow_list(self):
        """Read-only by construction: GET paths only."""
        for key in (_SEC_KEY, _SEC_FILES_KEY):
            entry = _SERVICE_REGISTRY[key]
            assert "allowed_post_endpoints" not in entry
            assert "_allowed_post_endpoints" not in entry

    def test_entry_is_no_auth(self):
        """No per-user requirement and no token-refresh retry -- it's public."""
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        assert not entry.get("requires_user", False)
        assert "retry_on_401" not in entry or entry["retry_on_401"] is False
        assert "missing_credentials_error" not in entry

    def test_entry_loader_is_sync_no_arg_returning_none(self):
        """The loader must be sync/no-arg returning None (the no-auth shape)."""
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        loader = entry["load_credentials"]
        assert not asyncio.iscoroutinefunction(loader)
        assert loader() is None

    def test_entry_has_contact_bearing_user_agent_default_header(self):
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        defaults = entry.get("default_headers") or {}
        ua = defaults.get("User-Agent")
        # Assert non-empty + contact-like (SEC requires name + contact), rather
        # than hard-coding the exact string, to stay robust to the chosen value.
        assert ua
        assert ua.strip()
        assert ("@" in ua) or ("." in ua), f"UA should carry a contact token: {ua!r}"

    def test_allowed_endpoints_cover_core_paths(self):
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/submissions/CIK0000320193.json",
            "/submissions/CIK0000320193-submissions-001.json",
            "/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json",
            "/api/xbrl/companyconcept/CIK0000320193/dei/EntityCommonStockSharesOutstanding.json",
            "/api/xbrl/companyfacts/CIK0000320193.json",
            "/api/xbrl/frames/us-gaap/AccountsPayableCurrent/USD/CY2023.json",
            "/api/xbrl/frames/us-gaap/Revenues/USD/CY2023Q1.json",
            "/api/xbrl/frames/us-gaap/AccountsPayableCurrent/USD/CY2023Q1I.json",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected path to be allowed: {path}"

    def test_allowed_endpoints_reject_unknown_paths(self):
        entry = _SERVICE_REGISTRY[_SEC_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            "/submissions/CIK0000320193",            # missing .json
            "/submissions/CIK320193.json",           # CIK not zero-padded to 10 digits
            "/api/xbrl/companyfacts/CIK0000320193",  # missing .json
            "/api/xbrl/companyfacts/CIK320193.json",  # CIK not zero-padded
            "/files/company_tickers.json",           # /files/ surface intentionally excluded
            "/api/xbrl/frames/us-gaap/Revenues/USD/2023Q1.json",  # period missing CY prefix
            "/submissions/CIK0000320193.json/extra",  # trailing segment
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: the www.sec.gov ticker->CIK map-files entry
# ---------------------------------------------------------------------------

class TestSecEdgarFilesRegistryEntry:
    """Verify the static shape of the www.sec.gov ticker->CIK map-files entry."""

    def test_entry_present_as_plain_hostname_key(self):
        assert _SEC_FILES_KEY in _SERVICE_REGISTRY
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        # A descriptive name; just assert it references SEC EDGAR.
        assert "SEC EDGAR" in entry["name"]
        # Plain hostname key: no path_prefix (gating is by allowed_endpoints).
        assert "path_prefix" not in entry

    def test_entry_is_no_auth(self):
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        assert not entry.get("requires_user", False)
        assert "retry_on_401" not in entry or entry["retry_on_401"] is False
        assert "missing_credentials_error" not in entry

    def test_entry_loader_is_sync_no_arg_returning_none(self):
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        loader = entry["load_credentials"]
        assert not asyncio.iscoroutinefunction(loader)
        assert loader() is None

    def test_entry_has_contact_bearing_user_agent_default_header(self):
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        defaults = entry.get("default_headers") or {}
        ua = defaults.get("User-Agent")
        assert ua
        assert ua.strip()
        assert ("@" in ua) or ("." in ua), f"UA should carry a contact token: {ua!r}"

    def test_allowed_endpoints_cover_exactly_the_two_map_files(self):
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        assert matches("/files/company_tickers.json")
        assert matches("/files/company_tickers_exchange.json")

    def test_allowed_endpoints_reject_other_www_paths(self):
        entry = _SERVICE_REGISTRY[_SEC_FILES_KEY]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            "/files/something_else.json",          # unrelated /files/ file
            "/cgi-bin/browse-edgar",               # the human EDGAR browse endpoint
            "/files/company_tickers.json/extra",   # trailing segment
            "/files/company_tickersxjson",         # dot must be literal
            "/submissions/CIK0000320193.json",     # a data.sec.gov path, not a file
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: _find_service path scoping
# ---------------------------------------------------------------------------

class TestSecEdgarServiceLookup:
    """Both data.sec.gov and www.sec.gov resolve via the plain-hostname fallback."""

    def test_data_host_resolves_to_service(self):
        service = _find_service("data.sec.gov", "/submissions/CIK0000320193.json")
        assert service is not None
        assert service["name"] == "SEC EDGAR"

    def test_xbrl_path_resolves_to_service(self):
        service = _find_service(
            "data.sec.gov", "/api/xbrl/companyfacts/CIK0000320193.json",
        )
        assert service is not None
        assert service["name"] == "SEC EDGAR"

    def test_www_files_host_resolves_to_service(self):
        # The two ticker->CIK map files on www.sec.gov are now allow-listed and
        # resolve via the plain-hostname fallback.
        service = _find_service("www.sec.gov", "/files/company_tickers.json")
        assert service is not None
        assert "SEC EDGAR" in service["name"]

        service2 = _find_service("www.sec.gov", "/files/company_tickers_exchange.json")
        assert service2 is not None
        assert "SEC EDGAR" in service2["name"]

    def test_hosts_unreachable_while_plugin_is_not_loaded(self, sec_edgar_plugin):
        """The two hosts exist only through the plugin: unregister -> gone."""
        from config import plugins as plugins_mod
        plugins_mod.unregister_plugin(sec_edgar_plugin)
        try:
            assert _find_service("data.sec.gov", "/submissions/CIK0000320193.json") is None
            assert _find_service("www.sec.gov", "/files/company_tickers.json") is None
            assert _SEC_KEY not in _SERVICE_REGISTRY
            assert _SEC_FILES_KEY not in _SERVICE_REGISTRY
        finally:
            # Re-register so the fixture's own teardown unregisters cleanly.
            plugins_mod.register_plugin(sec_edgar_plugin)
            plugins_mod._LOADED.append(sec_edgar_plugin)


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for SEC EDGAR
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestSecEdgar:
    """Exercise _make_authed_request() against the SEC EDGAR entry."""

    def test_allowed_request_injects_no_auth_but_has_user_agent(self):
        calls: list = []
        body = {"cik": "320193", "name": "Apple Inc."}
        with _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://data.sec.gov/submissions/CIK0000320193.json",
                # No user supplied -- the API needs none.
            ))

        parsed = json.loads(result)
        assert parsed == body

        # Exactly one HTTP call.
        assert len(calls) == 1
        url, headers = calls[0]
        assert url.startswith("https://data.sec.gov/submissions/CIK0000320193.json")
        # The no-auth assertion: NO Authorization header injected.
        assert "Authorization" not in headers
        # The required SEC User-Agent is present.
        ua = headers.get("User-Agent")
        assert ua and ua.strip()

    def test_works_without_user_context(self):
        """A no-auth service must not require a user dict."""
        calls: list = []
        with _mock_httpx_client(calls, body={"taxonomy": "us-gaap"}):
            result = _run(_make_authed_request(
                "https://data.sec.gov/api/xbrl/companyconcept/"
                "CIK0000320193/us-gaap/Revenues.json",
                user=None,
            ))
        parsed = json.loads(result)
        assert parsed == {"taxonomy": "us-gaap"}
        assert len(calls) == 1
        _, headers = calls[0]
        assert "Authorization" not in headers

    def test_disallowed_path_returns_error_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://data.sec.gov/submissions/CIK320193.json",  # not zero-padded
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "SEC EDGAR" in parsed["error"]
        assert "/submissions/CIK320193.json" in parsed["error"]
        assert calls == []

    def test_www_files_map_request_injects_no_auth_but_has_user_agent(self):
        """The www.sec.gov ticker->CIK map file is reachable, no-auth, with a UA."""
        calls: list = []
        body = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        with _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://www.sec.gov/files/company_tickers.json",
            ))
        parsed = json.loads(result)
        assert parsed == body
        assert len(calls) == 1
        url, headers = calls[0]
        assert url.startswith("https://www.sec.gov/files/company_tickers.json")
        assert "Authorization" not in headers
        ua = headers.get("User-Agent")
        assert ua and ua.strip()

    def test_www_unrelated_path_denied_without_http_call(self):
        """An unrelated www.sec.gov path is rejected without an HTTP call."""
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://www.sec.gov/files/something_else.json",
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "/files/something_else.json" in parsed["error"]
        assert calls == []

    def test_multiple_allowed_paths_each_make_one_call(self):
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            _run(_make_authed_request(
                "https://data.sec.gov/submissions/CIK0000320193.json",
            ))
            _run(_make_authed_request(
                "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
            ))
            _run(_make_authed_request(
                "https://data.sec.gov/api/xbrl/frames/us-gaap/"
                "AccountsPayableCurrent/USD/CY2023Q1I.json",
            ))
        assert len(calls) == 3
        for _, headers in calls:
            assert "Authorization" not in headers
