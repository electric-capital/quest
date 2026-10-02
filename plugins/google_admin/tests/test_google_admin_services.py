"""Tests for the Google Workspace Admin plugin's ``authed_get`` service entry.

``admin.googleapis.com`` (Admin SDK Directory API) is registered by the
plugin, so every test here runs with it registered via the autouse
fixture below.

Covers:
* Registry shape: per-user, the plugin's own loader/injector (NOT the
  core Google Services credentials), its own missing-credentials error,
  and no POST allow-list.
* The GET allow-list: every directory / device read is reachable; the
  credential-bearing user sub-resources, write-shaped paths, custom
  methods, and the other Admin SDK APIs on the host are not.
* The Cloud Identity host is NOT registered (its scope cannot be granted
  through a user consent screen).
* ``_make_authed_request`` injects the stored admin token, refuses POSTs
  and disallowed paths without an HTTP call, and reports the reconnect
  error for a user without the connection -- even one who has Google
  Services connected.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _find_service,
    _make_authed_request,
)
from plugins.google_admin.upstream import (
    inject_google_admin_bearer_auth,
    load_google_admin_credentials,
)

_DIRECTORY_KEY = "admin.googleapis.com"

_DIR = "/admin/directory/v1"


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
def _register_plugin(google_admin_plugin):
    """All tests here exercise the plugin-registered registry entries."""
    yield


def _connected_user() -> dict:
    return {
        "id": 1,
        "email": "u@example.com",
        "service_credentials": {
            "google_admin": {
                "service": "google_admin",
                "secret": None,
                "oauth_blob": {
                    "access_token": "ya29.admin-token",
                    "refresh_token": "1//refresh",
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
                },
            },
        },
    }


def _matches(key: str, path: str) -> bool:
    return any(pat.match(path) for pat in _SERVICE_REGISTRY[key]["_allowed_endpoints"])


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

class TestRegistryEntries:
    def test_entry_is_per_user_with_the_plugins_own_credentials(self):
        entry = _SERVICE_REGISTRY[_DIRECTORY_KEY]
        assert entry["requires_user"] is True
        # A separate grant from Google Services: the core Google loader
        # must never feed this host.
        assert entry["load_credentials"] is load_google_admin_credentials
        assert entry["inject_auth"] is inject_google_admin_bearer_auth
        assert entry["missing_credentials_error"]["error"] == "google_admin_oauth_required"
        assert "path_prefix" not in entry

    def test_entry_has_no_post_allow_list(self):
        entry = _SERVICE_REGISTRY[_DIRECTORY_KEY]
        assert "allowed_post_endpoints" not in entry
        assert "_allowed_post_endpoints" not in entry

    def test_host_resolves_to_the_plugin_service(self):
        assert _find_service(_DIRECTORY_KEY, f"{_DIR}/users")["name"] \
            == "Google Workspace Admin (Directory)"

    def test_cloud_identity_host_is_not_registered(self):
        """Its scope cannot be granted by user consent, so no entry exists."""
        assert "cloudidentity.googleapis.com" not in _SERVICE_REGISTRY
        assert _find_service("cloudidentity.googleapis.com", "/v1/devices") is None


# ---------------------------------------------------------------------------
# Directory API allow-list
# ---------------------------------------------------------------------------

class TestDirectoryAllowList:
    @pytest.mark.parametrize("path", [
        f"{_DIR}/users",
        f"{_DIR}/users/jane@example.com",
        f"{_DIR}/users/103456789012345678901",
        f"{_DIR}/users/jane@example.com/aliases",
        f"{_DIR}/groups",
        f"{_DIR}/groups/eng@example.com",
        f"{_DIR}/groups/eng@example.com/aliases",
        f"{_DIR}/groups/eng@example.com/members",
        f"{_DIR}/groups/eng@example.com/members/jane@example.com",
        f"{_DIR}/groups/eng@example.com/hasMember/jane@example.com",
        f"{_DIR}/customer/my_customer/orgunits",
        f"{_DIR}/customer/my_customer/orgunits/corp",
        f"{_DIR}/customer/my_customer/orgunits/corp/sales/emea",
        f"{_DIR}/customer/my_customer/orgunits/id:03ph8a2z1enx4lx",
        f"{_DIR}/customer/my_customer/orgunits/Field%20Sales",
        f"{_DIR}/customer/C01abc234/domains",
        f"{_DIR}/customer/my_customer/domains/example.com",
        f"{_DIR}/customer/my_customer/domainaliases",
        f"{_DIR}/customer/my_customer/domainaliases/example.net",
        f"{_DIR}/customer/my_customer/schemas",
        f"{_DIR}/customer/my_customer/schemas/EmploymentData",
        f"{_DIR}/customers/my_customer",
        f"{_DIR}/customer/my_customer/devices/chromeos",
        f"{_DIR}/customer/my_customer/devices/chromeos:countChromeOsDevices",
        f"{_DIR}/customer/my_customer/devices/chromeos/5a3c1e7f-0000-1111-2222-333344445555",
        f"{_DIR}/customer/my_customer/devices/mobile",
        f"{_DIR}/customer/my_customer/devices/mobile/AFiQxQ8Qgd-rouSmcd2UnuvhYV__WXdacTgJhPEA1QoQJrK1hYbKJXm-8JFlhZOjBF4aVbhleS2FVQk5lI069K2GULpteTlLVpKLJFSLSL",
    ])
    def test_read_paths_are_allowed(self, path):
        assert _matches(_DIRECTORY_KEY, path), path

    @pytest.mark.parametrize("path", [
        # Credential-bearing user sub-resources (GETs upstream, never here).
        f"{_DIR}/users/jane@example.com/verificationCodes",
        f"{_DIR}/users/jane@example.com/asps",
        f"{_DIR}/users/jane@example.com/asps/1",
        f"{_DIR}/users/jane@example.com/tokens",
        f"{_DIR}/users/jane@example.com/tokens/client-id.apps.googleusercontent.com",
        f"{_DIR}/users/jane@example.com/photos/thumbnail",
        # Write-shaped paths.
        f"{_DIR}/users/jane@example.com/makeAdmin",
        f"{_DIR}/users/jane@example.com/signOut",
        f"{_DIR}/users/jane@example.com/undelete",
        f"{_DIR}/users/jane@example.com/verificationCodes/generate",
        f"{_DIR}/customer/my_customer/devices/chromeos/abc/action",
        f"{_DIR}/customer/my_customer/devices/chromeos/abc:issueCommand",
        f"{_DIR}/customer/my_customer/devices/chromeos/abc/commands/1",
        f"{_DIR}/customer/my_customer/devices/chromeos:batchChangeStatus",
        f"{_DIR}/customer/my_customer/devices/mobile/abc/action",
        # Resources outside the directory + devices scope of the plugin.
        f"{_DIR}/customer/my_customer/roles",
        f"{_DIR}/customer/my_customer/roleassignments",
        f"{_DIR}/customer/my_customer/resources/calendars",
        f"{_DIR}/customers/my_customer/chrome/printers",
        # Shape errors.
        f"{_DIR}/users/",
        f"{_DIR}/customer/my_customer/orgunits/",
        "/admin/directory/v2/users",
        "/admin/reports/v1/activity/users/all/applications/login",
        "/admin/datatransfer/v1/transfers",
    ])
    def test_everything_else_is_rejected(self, path):
        assert not _matches(_DIRECTORY_KEY, path), path

    @pytest.mark.parametrize("path", [
        f"{_DIR}/customer/my_customer/orgunits/..",
        f"{_DIR}/customer/my_customer/orgunits/../devices/chromeos/abc/action",
        f"{_DIR}/customer/my_customer/orgunits/corp/../../../users/jane@example.com/tokens",
        f"{_DIR}/customer/my_customer/orgunits/corp/./sales",
        f"{_DIR}/customer/my_customer/orgunits/%2e%2e/users",
        f"{_DIR}/customer/my_customer/orgunits/%2E%2E/users",
        f"{_DIR}/customer/my_customer/orgunits/.%2e/users",
        f"{_DIR}/customer/my_customer/orgunits/corp//sales",
    ])
    def test_org_unit_path_cannot_climb_out(self, path):
        """The multi-segment org unit pattern refuses dot and empty segments."""
        assert not _matches(_DIRECTORY_KEY, path), path

    def test_org_unit_names_starting_with_a_dot_still_match(self):
        assert _matches(_DIRECTORY_KEY, f"{_DIR}/customer/my_customer/orgunits/.hidden/team")
        assert _matches(_DIRECTORY_KEY, f"{_DIR}/customer/my_customer/orgunits/v1.2")


# ---------------------------------------------------------------------------
# _make_authed_request behavior
# ---------------------------------------------------------------------------

class TestMakeAuthedRequest:
    def test_allowed_request_injects_the_admin_token(self):
        calls: list = []
        body = {"users": [{"primaryEmail": "jane@example.com"}]}
        with _mock_httpx_client(calls, body=body):
            result = _run(_make_authed_request(
                "https://admin.googleapis.com/admin/directory/v1/users"
                "?customer=my_customer&query=isAdmin=true",
                user=_connected_user(),
            ))

        assert json.loads(result) == body
        assert len(calls) == 1
        method, url, headers = calls[0]
        assert method == "GET"
        assert "query=isAdmin=true" in url
        assert headers["Authorization"] == "Bearer ya29.admin-token"

    def test_disallowed_path_is_refused_without_http_call(self):
        calls: list = []
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://admin.googleapis.com/admin/directory/v1/users/"
                "jane@example.com/verificationCodes",
                user=_connected_user(),
            ))
        error = json.loads(result)["error"]
        assert "not allowed" in error and "verificationCodes" in error
        assert calls == []

    @pytest.mark.parametrize("url", [
        "https://admin.googleapis.com/admin/directory/v1/users",
        "https://admin.googleapis.com/admin/directory/v1/users/jane@example.com/makeAdmin",
        "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/devices/chromeos/abc/action",
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
        """Google Services credentials are not a substitute for the admin grant."""
        calls: list = []
        user = {
            "id": 2,
            "email": "plain@example.com",
            "google_services_oauth": {"access_token": "ya29.services-token"},
        }
        with _mock_httpx_client(calls):
            result = _run(_make_authed_request(
                "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer",
                user=user,
            ))
        assert json.loads(result)["error"]["error"] == "google_admin_oauth_required"
        assert calls == []


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class TestSkill:
    def test_skill_is_gated_on_the_connection(self):
        from chat.system_skills import CATALOG

        skill = CATALOG["system:google_admin"]
        assert skill.requires == "google_admin"
        content = skill.content_builder("http://localhost", "key")
        assert "https://admin.googleapis.com/admin/directory/v1" in content
        assert "cloudidentity.googleapis.com" not in content
