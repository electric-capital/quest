"""Tests for plugins/tailscale/upstream.py: credential shape validation,
the per-user row helpers, and the OAuth client-secret exchange (token
cache, per-user lock, failure modes). All HTTP is mocked."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from plugins.tailscale import upstream
from plugins.tailscale.upstream import (
    OAUTH_TOKEN_URL,
    credential_kind,
    get_tailscale_token,
    get_user_secret,
    inject_tailscale_bearer_auth,
    oauth_client_id,
    tailscale_connected,
    validate_credential,
)

_API_TOKEN = "tskey-api-kAbCdE1CNTRL-0123456789abcdef0123456789abcdef"
_CLIENT_SECRET = "tskey-client-kXyZ123CNTRL-fedcba9876543210fedcba9876543210"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_cache():
    upstream.clear_token_cache()
    yield
    upstream.clear_token_cache()


def _user(secret: str | None, user_id: int = 7) -> dict:
    rows = {} if secret is None else {
        "tailscale": {"service": "tailscale", "secret": secret, "oauth_blob": None},
    }
    return {"id": user_id, "email": "u@example.com", "service_credentials": rows}


# ---------------------------------------------------------------------------
# Credential shapes
# ---------------------------------------------------------------------------

class TestValidateCredential:
    @pytest.mark.parametrize("secret", [_API_TOKEN, _CLIENT_SECRET])
    def test_accepts_api_tokens_and_oauth_client_secrets(self, secret):
        assert validate_credential(secret) is None

    def test_rejects_whitespace(self):
        assert "spaces" in validate_credential(_API_TOKEN[:20] + " " + _API_TOKEN[20:])
        assert "spaces" in validate_credential(_API_TOKEN + "\n")

    def test_rejects_device_auth_keys_with_a_specific_hint(self):
        error = validate_credential("tskey-auth-kAbCdE1CNTRL-0123456789abcdef0123456789abcdef")
        assert "device auth key" in error
        assert "tskey-api-" in error

    @pytest.mark.parametrize("secret", [
        "ghp_0123456789abcdef0123456789abcdef",   # some other vendor's token
        "kAbCdE1CNTRL",                            # a bare key id
        "tskey-0123456789abcdef0123456789abcdef",  # no kind segment
        "",
    ])
    def test_rejects_unrecognised_prefixes(self, secret):
        assert "tskey-api-" in validate_credential(secret)

    def test_rejects_malformed_oauth_secret(self):
        assert "expected" in validate_credential("tskey-client-nodash").lower()
        assert "expected" in validate_credential("tskey-client--secretpart0123456789").lower()

    def test_rejects_too_short_and_too_long(self):
        assert "too short" in validate_credential("tskey-api-k-ab")
        assert "too long" in validate_credential("tskey-api-kAbCdE1CNTRL-" + "x" * 300)


class TestShapeHelpers:
    def test_credential_kind(self):
        assert credential_kind(_API_TOKEN) == "api_token"
        assert credential_kind(_CLIENT_SECRET) == "oauth_client"
        assert credential_kind("tskey-auth-abc-def") is None
        assert credential_kind("nope") is None

    def test_oauth_client_id_is_the_middle_segment(self):
        assert oauth_client_id(_CLIENT_SECRET) == "kXyZ123CNTRL"
        assert oauth_client_id(_API_TOKEN) is None
        assert oauth_client_id("tskey-client-nodash") is None
        assert oauth_client_id("tskey-client--x") is None

    def test_user_row_helpers(self):
        assert get_user_secret(_user(_API_TOKEN)) == _API_TOKEN
        assert get_user_secret(_user("  " + _API_TOKEN + " ")) == _API_TOKEN
        assert get_user_secret(_user(None)) is None
        assert get_user_secret(_user("   ")) is None
        assert get_user_secret({"id": 1}) is None
        assert tailscale_connected({"secret": _API_TOKEN}) is True
        assert tailscale_connected({"secret": "  "}) is False
        assert tailscale_connected({"secret": None}) is False
        assert tailscale_connected({}) is False

    def test_inject_sets_bearer_header(self):
        headers: dict = {"Accept": "application/hujson"}
        inject_tailscale_bearer_auth("tok", headers)
        assert headers == {"Accept": "application/hujson", "Authorization": "Bearer tok"}


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

def _mock_token_endpoint(captured: list, *, status: int = 200, body=None, raise_exc=None):
    """Patch ``httpx.AsyncClient`` in upstream; record POSTs to the token URL."""
    body = body if body is not None else {"access_token": "tskey-exchanged", "expires_in": 3600}
    response = MagicMock()
    response.status_code = status
    response.text = json.dumps(body) if isinstance(body, dict) else str(body)
    if isinstance(body, dict):
        response.json = MagicMock(return_value=body)
    else:
        response.json = MagicMock(side_effect=ValueError("not json"))

    async def fake_post(url, data=None, headers=None):
        if raise_exc is not None:
            raise raise_exc
        captured.append((url, dict(data or {}), dict(headers or {})))
        return response

    client = MagicMock()
    client.post = AsyncMock(side_effect=fake_post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("plugins.tailscale.upstream.httpx.AsyncClient", return_value=ctx)


class TestGetTailscaleToken:
    def test_api_token_is_returned_verbatim_without_http(self):
        posts: list = []
        with _mock_token_endpoint(posts):
            assert _run(get_tailscale_token(_user(_API_TOKEN))) == _API_TOKEN
        assert posts == []

    def test_not_connected_returns_none(self):
        assert _run(get_tailscale_token(_user(None))) is None

    def test_unrecognised_stored_value_returns_none(self):
        posts: list = []
        with _mock_token_endpoint(posts):
            assert _run(get_tailscale_token(_user("tskey-auth-abc-def0123456789"))) is None
        assert posts == []

    def test_oauth_secret_is_exchanged_with_the_embedded_client_id(self):
        posts: list = []
        with _mock_token_endpoint(posts):
            token = _run(get_tailscale_token(_user(_CLIENT_SECRET)))
        assert token == "tskey-exchanged"
        assert len(posts) == 1
        url, data, headers = posts[0]
        assert url == OAUTH_TOKEN_URL
        assert data == {"client_id": "kXyZ123CNTRL", "client_secret": _CLIENT_SECRET}
        assert "Authorization" not in headers

    def test_exchanged_token_is_cached_per_user_until_near_expiry(self):
        posts: list = []
        with _mock_token_endpoint(posts):
            first = _run(get_tailscale_token(_user(_CLIENT_SECRET)))
            second = _run(get_tailscale_token(_user(_CLIENT_SECRET)))
        assert first == second == "tskey-exchanged"
        assert len(posts) == 1

        # Force the cached entry close to expiry: the next call re-exchanges.
        upstream._token_cache[7].expires_at = time.time() + 10
        with _mock_token_endpoint(posts):
            _run(get_tailscale_token(_user(_CLIENT_SECRET)))
        assert len(posts) == 2

    def test_cache_is_keyed_by_user_and_invalidated_by_a_new_secret(self):
        posts: list = []
        other_secret = "tskey-client-kOther99CNTRL-00000000000000000000000000000000"
        with _mock_token_endpoint(posts):
            _run(get_tailscale_token(_user(_CLIENT_SECRET, user_id=1)))
            _run(get_tailscale_token(_user(_CLIENT_SECRET, user_id=2)))
            # Same user pastes a different client secret: no stale token reuse.
            _run(get_tailscale_token(_user(other_secret, user_id=1)))
        assert len(posts) == 3
        assert posts[2][1]["client_id"] == "kOther99CNTRL"

    def test_concurrent_calls_share_one_exchange(self):
        posts: list = []

        async def go():
            user = _user(_CLIENT_SECRET)
            return await asyncio.gather(*(get_tailscale_token(user) for _ in range(5)))

        with _mock_token_endpoint(posts):
            tokens = _run(go())
        assert tokens == ["tskey-exchanged"] * 5
        assert len(posts) == 1

    @pytest.mark.parametrize("kwargs", [
        {"status": 401, "body": {"message": "invalid client"}},
        {"status": 500, "body": {"message": "boom"}},
        {"body": {"token_type": "Bearer"}},          # no access_token
        {"body": "<html>not json</html>"},
        {"raise_exc": httpx.ConnectError("no route")},
    ])
    def test_exchange_failures_read_as_not_connected(self, kwargs):
        posts: list = []
        with _mock_token_endpoint(posts, **kwargs):
            assert _run(get_tailscale_token(_user(_CLIENT_SECRET))) is None
        assert 7 not in upstream._token_cache

    def test_missing_expires_in_falls_back_to_an_hour(self):
        posts: list = []
        before = time.time()
        with _mock_token_endpoint(posts, body={"access_token": "t"}):
            assert _run(get_tailscale_token(_user(_CLIENT_SECRET))) == "t"
        cached = upstream._token_cache[7]
        assert before + 3600 - 5 <= cached.expires_at <= time.time() + 3600 + 5
