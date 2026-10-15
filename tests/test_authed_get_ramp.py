"""Tests for the Ramp entry in the ``authed_get`` service registry.

Covers:
* Ramp is registered with the expected shape (``requires_user``,
  ``retry_on_401`` set because Ramp access tokens expire, and a
  ``missing_credentials_error``).
* Allowed-endpoint gating accepts read paths across the Ramp developer API
  and rejects the card-vault endpoints, token endpoints, and webhooks.
* ``_make_authed_request`` injects the Bearer token, returns the
  ``ramp_oauth_required`` error when Ramp is not connected, and never makes
  an HTTP call for disallowed paths.
* ``get_ramp_token`` proactively refreshes expired / near-expiry tokens.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.ramp import get_ramp_token
from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _load_ramp_credentials,
    _make_authed_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _mock_httpx_client(captured_calls: list, status_code: int = 200, body: dict | None = None):
    """Patch ``httpx.AsyncClient`` inside authed_get, capturing GET calls."""
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


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _user_with_ramp(expires_in_seconds: int = 3600) -> dict:
    return {
        "id": 1,
        "email": "u@example.com",
        "ramp_oauth": {
            "access_token": "ramp_testtoken123",
            "refresh_token": "ramp_refresh456",
            "expires_at": _future_iso(expires_in_seconds),
        },
    }


USER_WITHOUT_RAMP = {
    "id": 2,
    "email": "no-ramp@example.com",
}


# ---------------------------------------------------------------------------
# Tests: registry shape
# ---------------------------------------------------------------------------

class TestRampRegistryEntry:
    """Verify the static shape of the Ramp registry entry."""

    def test_ramp_entry_present(self):
        assert "api.ramp.com" in _SERVICE_REGISTRY

    def test_ramp_entry_requires_user(self):
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        assert entry["requires_user"] is True

    def test_ramp_entry_has_retry_on_401(self):
        """Ramp access tokens expire, so the 401-refresh backstop is on."""
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        assert entry["retry_on_401"] is True

    def test_ramp_entry_has_missing_credentials_error(self):
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        err = entry.get("missing_credentials_error") or {}
        assert err.get("error") == "ramp_oauth_required"
        assert "Settings" in err.get("message", "")

    def test_ramp_entry_has_no_post_allow_list(self):
        """Ramp is read-only: no POST allow-list means all POSTs rejected."""
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        assert "allowed_post_endpoints" not in entry

    def test_ramp_allowed_endpoints_cover_core_read_paths(self):
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        expected_allowed = [
            "/developer/v1/business",
            "/developer/v1/business/balance",
            "/developer/v1/users",
            "/developer/v1/users/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/transactions",
            "/developer/v1/transactions/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/cards/physical",
            "/developer/v1/cards/physical/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/cards/virtual",
            "/developer/v1/cards/virtual/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/limits",
            "/developer/v1/spend-programs",
            "/developer/v1/bills",
            "/developer/v1/bills/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/reimbursements",
            "/developer/v1/receipts",
            "/developer/v1/memos",
            "/developer/v1/vendors",
            "/developer/v1/vendors/3e5a2b8c-1234-4abc-9def-000000000000/contacts",
            "/developer/v1/purchase-orders",
            "/developer/v1/merchants",
            "/developer/v1/statements",
            "/developer/v1/transfers",
            "/developer/v1/cashbacks",
            "/developer/v1/departments",
            "/developer/v1/locations",
            "/developer/v1/entities",
            "/developer/v1/accounting/accounts",
            "/developer/v1/accounting/tax/code/options",
            "/developer/v1/audit-logs/events",
        ]
        for path in expected_allowed:
            assert matches(path), f"expected path to be allowed: {path}"

    def test_ramp_allowed_endpoints_reject_sensitive_paths(self):
        entry = _SERVICE_REGISTRY["api.ramp.com"]
        compiled = entry["_allowed_endpoints"]

        def matches(path: str) -> bool:
            return any(pat.match(path) for pat in compiled)

        denied = [
            # Card vault (full card numbers) is deliberately unreachable.
            "/developer/v1/cards/vault/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/vault/cards/3e5a2b8c-1234-4abc-9def-000000000000",
            "/developer/v1/cards",                 # bare cards root not exposed
            # OAuth token endpoints must never be proxied.
            "/developer/v1/token",
            "/developer/v1/token/revoke",
            # Webhook config is not exposed.
            "/developer/v1/webhooks",
            # Outside the developer API entirely.
            "/v1/authorize",
            "/developer/v2/transactions",
        ]
        for path in denied:
            assert not matches(path), f"path should not be allowed: {path}"


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for Ramp
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestRamp:
    """Exercise _make_authed_request() against the Ramp service entry."""

    def test_allowed_request_injects_bearer(self):
        calls: list = []
        with _mock_httpx_client(calls, body={"data": []}):
            result = _run(_make_authed_request(
                "https://api.ramp.com/developer/v1/transactions",
                user=_user_with_ramp(),
            ))

        parsed = json.loads(result)
        assert parsed == {"data": []}

        assert len(calls) == 1
        url, headers = calls[0]
        assert url == "https://api.ramp.com/developer/v1/transactions"
        assert headers.get("Authorization") == "Bearer ramp_testtoken123"

    def test_caller_authorization_header_is_rejected_without_http_call(self):
        # A caller-supplied Authorization header is not in the caller header
        # allow-list, so the request is rejected before credential loading
        # and any HTTP call (it can never clobber the injected bearer).
        calls: list = []
        with _mock_httpx_client(calls, body={"ok": True}):
            result = _run(_make_authed_request(
                "https://api.ramp.com/developer/v1/business",
                headers={"Authorization": "Bearer hacker-token"},
                user=_user_with_ramp(),
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: Authorization" in parsed["error"]
        assert calls == []

    def test_disallowed_path_returns_error_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.ramp.com/developer/v1/cards/vault/abc-123",
                user=_user_with_ramp(),
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        assert "Ramp" in parsed["error"]
        assert "/developer/v1/cards/vault/abc-123" in parsed["error"]
        assert calls == []

    def test_missing_credentials_returns_ramp_oauth_required(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.ramp.com/developer/v1/transactions",
                user=USER_WITHOUT_RAMP,
            ))

        parsed = json.loads(result)
        assert "error" in parsed
        err = parsed["error"]
        assert isinstance(err, dict)
        assert err.get("error") == "ramp_oauth_required"
        assert "Settings" in err.get("message", "")
        assert calls == []


# ---------------------------------------------------------------------------
# Tests: token refresh behavior
# ---------------------------------------------------------------------------

class TestGetRampToken:
    """get_ramp_token: proactive refresh of expired / near-expiry tokens."""

    def test_returns_stored_token_when_not_near_expiry(self):
        user = _user_with_ramp(expires_in_seconds=3600)
        token = _run(get_ramp_token(user))
        assert token == "ramp_testtoken123"

    def test_refreshes_when_near_expiry(self):
        # Expires inside the 5-minute refresh margin -> refresh path taken.
        user = _user_with_ramp(expires_in_seconds=60)
        with patch(
            "auth.ramp.refresh_ramp_token",
            new=AsyncMock(return_value="ramp_newtoken789"),
        ) as refresh_mock:
            token = _run(get_ramp_token(user))
        assert token == "ramp_newtoken789"
        refresh_mock.assert_awaited_once_with(user)

    def test_refreshes_when_already_expired(self):
        user = _user_with_ramp(expires_in_seconds=-100)
        with patch(
            "auth.ramp.refresh_ramp_token",
            new=AsyncMock(return_value="ramp_newtoken789"),
        ):
            token = _run(get_ramp_token(user))
        assert token == "ramp_newtoken789"

    def test_raises_401_when_not_connected(self):
        with pytest.raises(HTTPException) as exc_info:
            _run(get_ramp_token(USER_WITHOUT_RAMP))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "ramp_oauth_required"

    def test_loader_returns_none_when_not_connected(self):
        """The authed_get loader swallows the HTTPException into None so the
        registry's missing_credentials_error is what the model sees."""
        assert _run(_load_ramp_credentials(USER_WITHOUT_RAMP)) is None

    def test_loader_returns_none_when_refresh_fails(self):
        user = _user_with_ramp(expires_in_seconds=-100)
        failure = HTTPException(status_code=401, detail={"error": "ramp_token_expired"})
        with patch(
            "auth.ramp.refresh_ramp_token",
            new=AsyncMock(side_effect=failure),
        ):
            assert _run(_load_ramp_credentials(user)) is None
