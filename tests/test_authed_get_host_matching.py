"""Regression tests: path-scoped registry keys require an EXACT hostname match.

``_find_service`` used to test ``key.startswith(hostname)`` for path-scoped
keys such as ``www.googleapis.com/calendar/v3``. Any proper string prefix of
the registered host (``www.googleapis.co``, ``www.googleapis``, a bare
``www``) therefore selected the Google Calendar / Drive service, and
``_make_authed_request`` then injected the user's Google bearer token into a
request sent to that attacker-controlled hostname. The host component of the
key must equal the request hostname exactly.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat.gemini_api import authed_get
from chat.gemini_api.authed_get import _find_service, handle_authed_get


@pytest.mark.parametrize(
    "hostname",
    [
        "www.googleapis.co",
        "www.googleapis.c",
        "www.googleapis",
        "www",
        "w",
        "www.federalregister.go",
    ],
)
def test_proper_prefix_of_registered_host_does_not_match(hostname):
    assert _find_service(hostname, "/calendar/v3/users/me/calendarList") is None
    assert _find_service(hostname, "/drive/v3/files") is None
    assert _find_service(hostname, "/api/v1/documents.json") is None


def test_longer_host_sharing_prefix_does_not_match():
    assert _find_service("www.googleapis.com.evil.example", "/calendar/v3/users/me/calendarList") is None
    assert _find_service("xwww.googleapis.com", "/calendar/v3/users/me/calendarList") is None


def test_exact_host_still_matches_path_scoped_entries():
    assert _find_service("www.googleapis.com", "/calendar/v3/users/me/calendarList")["name"] == "Google Calendar"
    assert _find_service("www.googleapis.com", "/drive/v3/files")["name"] == "Google Drive"
    assert _find_service("www.federalregister.gov", "/api/v1/documents.json")["name"] == "Federal Register"


def test_exact_host_with_unscoped_path_falls_through():
    # www.googleapis.com has only path-scoped entries; an unscoped path must
    # not resolve to any of them.
    assert _find_service("www.googleapis.com", "/oauth2/v1/userinfo") is None


def test_plain_hostname_fallback_unaffected():
    assert _find_service("docs.googleapis.com", "/v1/documents/abc")["name"] == "Google Docs"
    assert _find_service("api.airtable.com", "/v0/meta/bases")["name"] == "Airtable"


class _CapturingAsyncClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        raise AssertionError("no upstream request should be issued")


def test_prefix_host_never_receives_google_bearer(monkeypatch):
    """End-to-end: the token must not be attached to a prefix-host URL."""
    credentials = SimpleNamespace(token="ya29.secret")
    user = {"id": "user-1", "email": "victim@example.com"}
    _CapturingAsyncClient.calls = []
    monkeypatch.setattr(
        authed_get, "get_valid_service_credentials", AsyncMock(return_value=credentials),
    )
    monkeypatch.setattr(authed_get.httpx, "AsyncClient", _CapturingAsyncClient)

    url = "https://www.googleapis.co/calendar/v3/users/me/calendarList?x=1"
    result = json.loads(asyncio.run(handle_authed_get(url, user=user)))

    assert "error" in result
    assert "Unknown service host: 'www.googleapis.co'" in result["error"]
    assert _CapturingAsyncClient.calls == []
