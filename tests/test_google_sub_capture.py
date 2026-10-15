"""Tests for capturing the Google ``sub`` claim on login.

The v2 userinfo endpoint's ``id`` field is the Google account's stable
OAuth ``sub``; the login callback must persist it on both the new-user
and existing-user paths (so users who logged in before the column
existed pick it up on their next login), and local dev logins must seed
deterministic fake subs so local flows exercise the column.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import auth.google_login as google_login
import auth.oauth_state as oauth_state
from auth.config import ALLOWED_DOMAIN


def _run(coro):
    return asyncio.run(coro)


EMAIL = f"alice@{ALLOWED_DOMAIN}"
USERINFO = {"id": "1234567890", "email": EMAIL, "name": "Alice"}


@pytest.fixture(autouse=True)
def _fixed_secret_key():
    """Sign state cookies with a fixed key so tests never touch the data dir."""
    with patch.object(oauth_state, "get_secret_key", return_value="test-secret"):
        yield


STATE = "st4te"


def _client():
    """Test client carrying a valid signed login-state cookie for STATE."""
    app = FastAPI()
    app.include_router(google_login.router)
    client = TestClient(app)
    client.cookies.set(
        google_login._STATE_COOKIE,
        oauth_state._serializer().dumps({"csrf": STATE, "uid": None, "popup": False}),
    )
    return client


CALLBACK_URL = f"/auth/callback?code=abc&state={STATE}"


def _patch_flow():
    """Patch the OAuth flow so fetch_token yields fake credentials."""
    creds = MagicMock(token="tok", refresh_token="ref", expiry=None)
    flow = MagicMock()
    flow.credentials = creds
    flow.fetch_token = MagicMock()
    return patch.object(google_login, "get_login_oauth_flow", return_value=flow)


def _patch_userinfo(payload=USERINFO):
    """Patch the callback's userinfo fetch."""
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value=payload)
    client_mock = MagicMock()
    client_mock.get = AsyncMock(return_value=resp)
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_mock)
    async_ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(
        google_login.httpx, "AsyncClient", return_value=async_ctx,
    )


def _patch_serializer():
    """Sign the session cookie with a fixed key (never the data dir's)."""
    import auth.session as session_mod
    return patch.object(session_mod, "get_secret_key", return_value="test-secret")


class TestLoginCallbackStoresSub:
    def test_new_user_created_with_sub(self):
        create_mock = AsyncMock(return_value={"id": 1})
        lookup_mock = AsyncMock(side_effect=[None, {"id": 1}])
        with _patch_flow(), _patch_userinfo(), _patch_serializer(), \
                patch.object(google_login, "get_user_by_email", lookup_mock), \
                patch.object(google_login, "create_user", create_mock):
            response = _client().get(
                CALLBACK_URL, follow_redirects=False,
            )
        assert response.status_code == 303
        assert create_mock.await_args.kwargs["google_sub"] == "1234567890"

    def test_existing_user_backfilled_on_login(self):
        update_mock = AsyncMock(return_value={"id": 1})
        lookup_mock = AsyncMock(side_effect=[
            {"id": 1, "email": EMAIL}, {"id": 1},
        ])
        with _patch_flow(), _patch_userinfo(), _patch_serializer(), \
                patch.object(google_login, "get_user_by_email", lookup_mock), \
                patch.object(google_login, "update_user_field", update_mock):
            response = _client().get(
                CALLBACK_URL, follow_redirects=False,
            )
        assert response.status_code == 303
        assert update_mock.await_args.kwargs["google_sub"] == "1234567890"

    def test_missing_sub_does_not_clobber_existing_value(self):
        update_mock = AsyncMock(return_value={"id": 1})
        lookup_mock = AsyncMock(side_effect=[
            {"id": 1, "email": EMAIL}, {"id": 1},
        ])
        payload = {"email": EMAIL, "name": "Alice"}  # no "id" field
        with _patch_flow(), _patch_userinfo(payload), _patch_serializer(), \
                patch.object(google_login, "get_user_by_email", lookup_mock), \
                patch.object(google_login, "update_user_field", update_mock):
            _client().get(CALLBACK_URL, follow_redirects=False)
        assert "google_sub" not in update_mock.await_args.kwargs


class TestDevLoginSubs:
    def test_deterministic_and_prefixed(self):
        from auth.dev_login import _generate_dev_google_sub

        sub = _generate_dev_google_sub("alice@quest.local")
        assert sub == _generate_dev_google_sub("alice@quest.local")
        assert sub.startswith("dev-")
        assert sub != _generate_dev_google_sub("bob@quest.local")


class TestUserModel:
    def test_to_dict_includes_sub_only_when_set(self):
        from db.models import User

        user = User(email="a@b.c", name="A", api_key="k")
        assert "google_sub" not in user.to_dict()
        user.google_sub = "sub-1"
        assert user.to_dict()["google_sub"] == "sub-1"
