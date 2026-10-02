"""Tests for the Tailscale plugin's ``authed_get`` service entry.

``api.tailscale.com`` is registered by the plugin, so every test here runs
with it registered via the autouse fixture below.

Covers:
* Registry shape: per-user, the plugin's own loader/injector, its own
  missing-credentials error, and no POST allow-list.
* The GET allow-list: every tailnet-configuration read is reachable; the
  invite and log-streaming-destination rows, write-shaped paths, the ACL
  POST verbs, the OAuth token endpoint and shape errors are not.
* ``_make_authed_request`` injects the stored API access token (and the
  exchanged access token for an OAuth client secret), passes the HuJSON
  ``Accept`` override through, refuses POSTs and disallowed paths without
  an HTTP call, and reports the reconnect error for a user without the
  connection.

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
from plugins.tailscale import upstream
from plugins.tailscale.upstream import (
    inject_tailscale_bearer_auth,
    load_tailscale_credentials,
)

_KEY = "api.tailscale.com"
_V2 = "/api/v2"
_API_TOKEN = "tskey-api-kAbCdE1CNTRL-0123456789abcdef0123456789abcdef"
_CLIENT_SECRET = "tskey-client-kXyZ123CNTRL-fedcba9876543210fedcba9876543210"


def _run(coro):
    return asyncio.run(coro)


def _mock_httpx_client(captured_calls: list, body: dict | None = None):
    """Patch ``httpx.AsyncClient`` in authed_get; record GET and POST calls."""
    body = body if body is not None else {"ok": True}
    response = MagicMock()
    response.status_code = 200
    response.text = json.dumps(body)
    response.json = MagicMock(return_value=body)

    async def fake_get(url, headers=None):
        captured_calls.append(("GET", url, dict(headers or {})))
        return response

    async def fake_post(url, headers=None, json=None):
        captured_calls.append(("POST", url, dict(headers or {})))
        return response

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=fake_post)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("chat.gemini_api.authed_get.httpx.AsyncClient", return_value=ctx)


@pytest.fixture(autouse=True)
def _register_plugin(tailscale_plugin):
    """All tests here exercise the plugin-registered registry entries."""
    upstream.clear_token_cache()
    yield
    upstream.clear_token_cache()


def _connected_user(secret: str = _API_TOKEN) -> dict:
    return {
        "id": 1,
        "email": "u@example.com",
        "service_credentials": {
            "tailscale": {
                "service": "tailscale",
                "secret": secret,
                "oauth_blob": None,
            },
        },
    }


def _matches(path: str) -> bool:
    return any(pat.match(path) for pat in _SERVICE_REGISTRY[_KEY]["_allowed_endpoints"])


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

class TestRegistryEntry:
    def test_entry_is_per_user_with_the_plugins_own_credentials(self):
        entry = _SERVICE_REGISTRY[_KEY]
        assert entry["requires_user"] is True
        assert entry["load_credentials"] is load_tailscale_credentials
        assert entry["inject_auth"] is inject_tailscale_bearer_auth
        assert entry["missing_credentials_error"]["error"] == "tailscale_token_required"
        assert "path_prefix" not in entry
        assert entry["default_headers"]["User-Agent"]

    def test_entry_has_no_post_allow_list(self):
        entry = _SERVICE_REGISTRY[_KEY]
        assert "allowed_post_endpoints" not in entry
        assert "_allowed_post_endpoints" not in entry

    def test_host_resolves_to_the_plugin_service(self):
        assert _find_service(_KEY, f"{_V2}/tailnet/-/devices")["name"] == "Tailscale"

    def test_no_other_tailscale_host_is_registered(self):
        assert _find_service("login.tailscale.com", "/admin") is None
        assert _find_service("tailscale.com", "/api") is None


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------

class TestAllowList:
    @pytest.mark.parametrize("path", [
        # devices
        f"{_V2}/tailnet/-/devices",
        f"{_V2}/tailnet/example.com/devices",
        f"{_V2}/tailnet/alice@gmail.com/devices",
        f"{_V2}/tailnet/alice.github/devices",
        f"{_V2}/tailnet/tail1234.ts.net/devices",
        f"{_V2}/device/nABC123CNTRL",
        f"{_V2}/device/12345678901234",
        f"{_V2}/device/nABC123CNTRL/routes",
        f"{_V2}/device/nABC123CNTRL/attributes",
        # policy file
        f"{_V2}/tailnet/-/acl",
        # dns
        f"{_V2}/tailnet/-/dns/nameservers",
        f"{_V2}/tailnet/-/dns/preferences",
        f"{_V2}/tailnet/-/dns/searchpaths",
        f"{_V2}/tailnet/-/dns/split-dns",
        f"{_V2}/tailnet/-/dns/configuration",
        # keys
        f"{_V2}/tailnet/-/keys",
        f"{_V2}/tailnet/-/keys/kAbCdE1CNTRL",
        # users
        f"{_V2}/tailnet/-/users",
        f"{_V2}/users/123456",
        # tailnet
        f"{_V2}/tailnet/-/settings",
        f"{_V2}/tailnet/-/contacts",
        f"{_V2}/organizations/-/tailnets",
        # webhooks
        f"{_V2}/tailnet/-/webhooks",
        f"{_V2}/webhooks/abc123",
        # posture integrations
        f"{_V2}/tailnet/-/posture/integrations",
        f"{_V2}/posture/integrations/abc123",
        # services
        f"{_V2}/tailnet/-/vip-services",
        f"{_V2}/tailnet/-/vip-services/svc:web",
        # logs
        f"{_V2}/tailnet/-/logging/network",
    ])
    def test_read_paths_are_allowed(self, path):
        assert _matches(path), path

    @pytest.mark.parametrize("path", [
        # Invites carry the accept URL.
        f"{_V2}/tailnet/-/user-invites",
        f"{_V2}/user-invites/abc123",
        f"{_V2}/device/nABC123CNTRL/device-invites",
        f"{_V2}/device-invites/abc123",
        # Log streaming destinations may carry destination credentials.
        f"{_V2}/tailnet/-/logging/configuration/stream",
        f"{_V2}/tailnet/-/logging/network/stream",
        f"{_V2}/tailnet/-/logging/network/stream/status",
        f"{_V2}/tailnet/-/aws-external-id",
        # Write-shaped / POST-only paths.
        f"{_V2}/tailnet/-/acl/validate",
        f"{_V2}/tailnet/-/acl/preview",
        f"{_V2}/device/nABC123CNTRL/authorized",
        f"{_V2}/device/nABC123CNTRL/tags",
        f"{_V2}/device/nABC123CNTRL/key",
        f"{_V2}/device/nABC123CNTRL/name",
        f"{_V2}/device/nABC123CNTRL/ip",
        f"{_V2}/device/nABC123CNTRL/attributes/custom:foo",
        f"{_V2}/webhooks/abc123/test",
        f"{_V2}/webhooks/abc123/rotate",
        f"{_V2}/oauth/token",
        # Shape errors / other resources.
        f"{_V2}/tailnet/-",
        f"{_V2}/tailnet/-/",
        f"{_V2}/tailnet/-/devices/",
        f"{_V2}/tailnet/-/devices/nABC123CNTRL",
        f"{_V2}/tailnet//devices",
        f"{_V2}/devices",
        f"{_V2}/tailnet/-/dns",
        f"{_V2}/tailnet/-/dns/other",
        f"{_V2}/tailnet/-/keys/kAbCdE1CNTRL/extra",
        "/api/v1/tailnet/-/devices",
        "/tailnet/-/devices",
    ])
    def test_everything_else_is_rejected(self, path):
        assert not _matches(path), path

    @pytest.mark.parametrize("path", [
        f"{_V2}/tailnet/../devices",
        f"{_V2}/tailnet/./devices",
        f"{_V2}/tailnet/%2e%2e/devices",
        f"{_V2}/tailnet/%2E%2E/devices",
        f"{_V2}/tailnet/.%2e/devices",
        f"{_V2}/device/../routes",
        f"{_V2}/users/..",
        f"{_V2}/tailnet/-/keys/..",
        f"{_V2}/tailnet/-/vip-services/..",
    ])
    def test_dot_segments_are_refused(self, path):
        assert not _matches(path), path

    def test_names_merely_starting_with_a_dot_still_match(self):
        assert _matches(f"{_V2}/tailnet/.hidden.example/devices")
        assert _matches(f"{_V2}/tailnet/-/vip-services/svc:v1.2")


# ---------------------------------------------------------------------------
# _make_authed_request behavior
# ---------------------------------------------------------------------------

class TestMakeAuthedRequest:
    def test_allowed_request_injects_the_api_access_token(self):
        calls: list = []
        body = {"devices": [{"nodeId": "nABC123CNTRL", "name": "laptop.example.ts.net"}]}
        with _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/devices?fields=all",
                user=_connected_user(),
            ))

        assert json.loads(result) == body
        assert len(calls) == 1
        method, url, headers = calls[0]
        assert method == "GET"
        assert url.endswith("/devices?fields=all")
        assert headers["Authorization"] == f"Bearer {_API_TOKEN}"
        assert headers["User-Agent"] == "Quest/1.0"

    def test_oauth_client_secret_is_exchanged_and_the_access_token_injected(self):
        calls: list = []
        with patch.object(
            upstream, "_exchange_client_secret",
            AsyncMock(return_value=("tskey-exchanged-access", 9e12)),
        ) as exchange, _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/settings",
                user=_connected_user(_CLIENT_SECRET),
            ))
        assert json.loads(result) == {"ok": True}
        exchange.assert_awaited_once()
        assert calls[0][2]["Authorization"] == "Bearer tskey-exchanged-access"

    def test_hujson_accept_header_passes_through_below_auth(self):
        calls: list = []
        with _mock_httpx_client(calls):
            _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/acl",
                headers={"Accept": "application/hujson"},
                user=_connected_user(),
            ))
        _, _, headers = calls[0]
        assert headers["Accept"] == "application/hujson"
        assert headers["Authorization"] == f"Bearer {_API_TOKEN}"

    def test_disallowed_path_is_refused_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/user-invites",
                user=_connected_user(),
            ))
        error = json.loads(result)["error"]
        assert "not allowed" in error and "user-invites" in error
        assert calls == []

    @pytest.mark.parametrize("url", [
        "https://api.tailscale.com/api/v2/tailnet/-/acl",
        "https://api.tailscale.com/api/v2/tailnet/-/acl/validate",
        "https://api.tailscale.com/api/v2/tailnet/-/keys",
        "https://api.tailscale.com/api/v2/device/nABC123CNTRL/authorized",
        "https://api.tailscale.com/api/v2/oauth/token",
    ])
    def test_every_post_is_refused_without_http_call(self, url):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                url, user=_connected_user(), method="POST", json_body={},
            ))
        assert "POST requests are not allowed" in json.loads(result)["error"]
        assert calls == []

    def test_user_without_the_connection_gets_the_reconnect_error(self):
        calls: list = []
        user = {"id": 2, "email": "plain@example.com", "service_credentials": {}}
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/devices", user=user,
            ))
        assert json.loads(result)["error"]["error"] == "tailscale_token_required"
        assert calls == []

    def test_failed_oauth_exchange_reads_as_not_connected(self):
        calls: list = []
        with patch.object(
            upstream, "_exchange_client_secret", AsyncMock(return_value=None),
        ), _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://api.tailscale.com/api/v2/tailnet/-/devices",
                user=_connected_user(_CLIENT_SECRET),
            ))
        assert json.loads(result)["error"]["error"] == "tailscale_token_required"
        assert calls == []


# ---------------------------------------------------------------------------
# Skill + connection spec
# ---------------------------------------------------------------------------

class TestManifestSurface:
    def test_skill_is_gated_on_the_connection(self):
        from chat.system_skills import CATALOG

        skill = CATALOG["system:tailscale"]
        assert skill.requires == "tailscale"
        content = skill.content_builder("http://localhost", "key")
        assert "https://api.tailscale.com/api/v2" in content
        assert "application/hujson" in content
        # Every allow-listed resource is documented in the skill.
        for fragment in ("/devices", "/acl", "/dns/", "/keys", "/users",
                         "/settings", "/webhooks", "/vip-services", "/logging/network"):
            assert fragment in content, fragment

    def test_connection_is_api_key_kind_without_an_admin_card(self, tailscale_plugin):
        from config.plugins import plugin_server_available

        assert tailscale_plugin.credential_schema == ()
        assert tailscale_plugin.user_connection.kind == "api_key"
        assert tailscale_plugin.tools == ()
        assert tailscale_plugin.action_request_handlers == ()
        # No server-side switch: available on every deployment.
        assert plugin_server_available(tailscale_plugin) is True
