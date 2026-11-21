"""Tests for the caller header allow-list in ``_make_authed_request``.

Caller-supplied headers (the model's ``headers`` tool argument, or the sandbox
proxy body) ride on requests that carry the user's upstream credential, so
only plain content-negotiation headers (``_ALLOWED_CALLER_HEADERS``) are
accepted. Anything else -- ``X-HTTP-Method-Override``, ``X-Goog-*`` metadata,
``Authorization`` -- is rejected BEFORE credential loading and network I/O,
on every service. Regression coverage for the finding that model-controlled
Google headers reached the provider under the user's OAuth bearer.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api import authed_get
from chat.gemini_api.authed_get import (
    _ALLOWED_CALLER_HEADERS,
    _make_authed_request,
    _reject_disallowed_caller_headers,
    handle_authed_get,
    handle_authed_post,
)
from tests.authed_get_support import mock_httpx_client


_COMPUTE_INSTANCE_URL = (
    "https://compute.googleapis.com/compute/v1/projects/victim-project/"
    "zones/us-central1-a/instances/vm-1"
)
_LOGGING_ENTRIES_URL = "https://logging.googleapis.com/v2/entries:list"
_USER = {"id": "u1", "email": "victim@example.com"}


def _run(coro):
    return asyncio.run(coro)


def _mock_credentials_loader():
    creds = MagicMock()
    creds.token = "ya29.test-token"
    loader = AsyncMock(return_value=creds)
    return patch.object(authed_get, "get_valid_service_credentials", new=loader), loader


class TestRejectHelper:
    def test_none_and_empty_pass(self):
        assert _reject_disallowed_caller_headers(None) is None
        assert _reject_disallowed_caller_headers({}) is None

    def test_allow_list_is_content_negotiation_only(self):
        assert _ALLOWED_CALLER_HEADERS == frozenset({"accept", "accept-language"})

    def test_allowed_names_are_case_insensitive(self):
        assert _reject_disallowed_caller_headers({
            "Accept": "application/vnd.github.raw+json",
            "ACCEPT-LANGUAGE": "en",
        }) is None

    @pytest.mark.parametrize("name", [
        "X-HTTP-Method-Override",
        "x-http-method-override",
        "X-Goog-Request-Reason",
        "X-Goog-User-Project",
        "Authorization",
        "Cookie",
        "Host",
    ])
    def test_disallowed_names_are_rejected_and_named(self, name):
        result = _reject_disallowed_caller_headers({"Accept": "a", name: "v"})
        parsed = json.loads(result)
        assert f"Header(s) not allowed: {name}" in parsed["error"]
        assert "accept, accept-language" in parsed["error"]


class TestGateRunsBeforeIO:
    def test_method_override_on_allowed_google_get_is_rejected_before_credentials(self):
        gets: list = []
        posts: list = []
        creds_patch, loader = _mock_credentials_loader()
        with creds_patch, mock_httpx_client(gets, posts):
            result = _run(handle_authed_get(
                _COMPUTE_INSTANCE_URL,
                headers={"X-HTTP-Method-Override": "DELETE"},
                user=_USER,
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: X-HTTP-Method-Override" in parsed["error"]
        # Neither the user's OAuth credential nor the network was touched.
        loader.assert_not_awaited()
        assert gets == [] and posts == []

    def test_goog_request_reason_metadata_is_rejected_before_credentials(self):
        gets: list = []
        posts: list = []
        creds_patch, loader = _mock_credentials_loader()
        with creds_patch, mock_httpx_client(gets, posts):
            result = _run(_make_authed_request(
                "https://compute.googleapis.com/compute/v1/projects/"
                "attacker-owned-project/zones/us-central1-a/instances",
                headers={"X-Goog-Request-Reason": "quest_api_key_here"},
                user=_USER,
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: X-Goog-Request-Reason" in parsed["error"]
        loader.assert_not_awaited()
        assert gets == []

    def test_authed_post_path_is_gated_too(self):
        gets: list = []
        posts: list = []
        creds_patch, loader = _mock_credentials_loader()
        with creds_patch, mock_httpx_client(gets, posts):
            result = _run(handle_authed_post(
                _LOGGING_ENTRIES_URL,
                body={"resourceNames": ["projects/p"]},
                headers={"X-HTTP-Method-Override": "PUT"},
                user=_USER,
            ))

        parsed = json.loads(result)
        assert "Header(s) not allowed: X-HTTP-Method-Override" in parsed["error"]
        loader.assert_not_awaited()
        assert posts == []

    def test_header_gate_precedes_endpoint_validation(self):
        # A disallowed header on a disallowed path reports the header error:
        # the gate sits before path validation so nothing else runs first.
        gets: list = []
        posts: list = []
        with mock_httpx_client(gets, posts):
            result = _run(_make_authed_request(
                "https://compute.googleapis.com/compute/v1/projects/p/"
                "zones/z/instances/vm:stop",
                headers={"X-HTTP-Method-Override": "POST"},
                user=_USER,
            ))
        assert "Header(s) not allowed" in json.loads(result)["error"]
        assert gets == []


class TestAllowedHeadersStillForwarded:
    def test_accept_override_is_forwarded_with_injected_auth(self):
        gets: list = []
        posts: list = []
        creds_patch, _ = _mock_credentials_loader()
        with creds_patch, mock_httpx_client(gets, posts):
            result = _run(_make_authed_request(
                _COMPUTE_INSTANCE_URL,
                headers={"Accept": "application/json", "Accept-Language": "en"},
                user=_USER,
            ))

        assert json.loads(result) == {"ok": True}
        assert len(gets) == 1
        url, headers = gets[0]
        assert url == _COMPUTE_INSTANCE_URL
        assert headers["Accept"] == "application/json"
        assert headers["Accept-Language"] == "en"
        assert headers["Authorization"] == "Bearer ya29.test-token"

    def test_no_headers_still_works(self):
        gets: list = []
        posts: list = []
        creds_patch, _ = _mock_credentials_loader()
        with creds_patch, mock_httpx_client(gets, posts):
            result = _run(_make_authed_request(_COMPUTE_INSTANCE_URL, user=_USER))
        assert json.loads(result) == {"ok": True}
        assert len(gets) == 1
