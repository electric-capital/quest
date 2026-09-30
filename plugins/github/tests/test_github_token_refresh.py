"""Tests for GitHub token refresh (plugins/github/upstream.py).

GitHub App user tokens (``ghu_``) expire after 8 hours and come with a
rotating refresh token; classic OAuth App tokens (``gho_``) never expire.
Covers the expiry check, token-blob construction for both shapes, and
``get_github_token``: pass-through for non-expiring tokens, adopting a
token another turn already refreshed, refreshing near expiry (persisted +
in-memory), failure fallbacks, and one refresh for concurrent callers.

The credential store and GitHub's token endpoint are faked -- no DB, no
network.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import plugins.github.upstream as upstream
from plugins.github.upstream import (
    GITHUB_TOKEN_URL,
    build_token_blob,
    get_github_token,
    token_expires_within,
)


def _run(coro):
    return asyncio.run(coro)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _app_blob(access: str, refresh: str, expires_in: timedelta) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "scope": "",
        "scopes": [],
        "expires_at": _iso(expires_in),
        "authorized_at": "2026-01-01T00:00:00+00:00",
    }


def _user(blob: dict | None) -> dict:
    user = {"id": 7, "email": "u@example.com"}
    if blob is not None:
        user["service_credentials"] = {
            "github": {"service": "github", "secret": None, "oauth_blob": blob},
        }
    return user


class _FakeStore:
    """Stands in for db.user_service_credential_store (one user's rows)."""

    def __init__(self, blob: dict | None):
        self.blob = blob
        self.upserts: list[dict] = []

    async def get_credential(self, user_id, service):
        assert service == "github"
        if self.blob is None:
            return None
        return {"service": "github", "secret": None, "oauth_blob": dict(self.blob)}

    async def upsert_credential(self, user_id, service, *, secret=None, oauth_blob=None):
        assert service == "github"
        self.blob = oauth_blob
        self.upserts.append(oauth_blob)
        return {"service": service, "secret": secret, "oauth_blob": oauth_blob}


@pytest.fixture
def store():
    return _FakeStore(None)


@pytest.fixture(autouse=True)
def _patch_store_and_config(store):
    # Each test runs its own event loop; a lock contended in an earlier
    # test's loop cannot be reused.
    upstream._refresh_locks.clear()
    with patch(
        "db.user_service_credential_store.get_credential", store.get_credential,
    ), patch(
        "db.user_service_credential_store.upsert_credential", store.upsert_credential,
    ), patch.object(
        upstream, "load_github_client_config",
        return_value={"client_id": "cid", "client_secret": "csec"},
    ):
        yield


def _patch_token_endpoint(*, status_code=200, body=None, exc=None):
    """Patch httpx.AsyncClient in the upstream module; returns the client
    mock so tests can inspect the refresh POST."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=body if body is not None else {})
    response.text = str(body)

    client = MagicMock()
    if exc is not None:
        client.post = AsyncMock(side_effect=exc)
    else:
        client.post = AsyncMock(return_value=response)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(upstream.httpx, "AsyncClient", return_value=ctx), client


_REFRESHED = {
    "access_token": "ghu_new",
    "expires_in": 28800,
    "refresh_token": "ghr_new",
    "refresh_token_expires_in": 15897600,
    "scope": "",
    "token_type": "bearer",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestTokenExpiry:
    def test_no_expiry_never_expires(self):
        # Classic OAuth App tokens (and GitHub Apps with expiration off).
        assert not token_expires_within({"access_token": "gho_x"})

    def test_future_expiry_not_expiring(self):
        assert not token_expires_within({"expires_at": _iso(timedelta(hours=1))})

    def test_within_margin_is_expiring(self):
        assert token_expires_within({"expires_at": _iso(timedelta(seconds=60))})

    def test_zero_margin_checks_actual_expiry(self):
        blob = {"expires_at": _iso(timedelta(seconds=60))}
        assert not token_expires_within(blob, 0)
        assert token_expires_within({"expires_at": _iso(-timedelta(seconds=1))}, 0)

    def test_unparseable_reads_as_expired(self):
        assert token_expires_within({"expires_at": "not-a-date"})


class TestBuildTokenBlob:
    def test_classic_oauth_app_token_has_no_expiry(self):
        blob = build_token_blob({
            "access_token": "gho_x", "token_type": "bearer", "scope": "repo,read:org",
        })
        assert blob["access_token"] == "gho_x"
        assert blob["scopes"] == ["repo", "read:org"]
        assert blob["authorized_at"]
        assert "refresh_token" not in blob
        assert "expires_at" not in blob
        assert "refresh_token_expires_at" not in blob

    def test_github_app_token_keeps_refresh_token_and_expiries(self):
        blob = build_token_blob(_REFRESHED)
        assert blob["refresh_token"] == "ghr_new"
        expires_at = datetime.fromisoformat(blob["expires_at"])
        remaining = expires_at - datetime.now(timezone.utc)
        assert timedelta(hours=7, minutes=59) < remaining <= timedelta(hours=8)
        refresh_expires_at = datetime.fromisoformat(blob["refresh_token_expires_at"])
        assert refresh_expires_at - datetime.now(timezone.utc) > timedelta(days=180)

    def test_refresh_carries_authorized_at_and_scope(self):
        previous = {
            "access_token": "ghu_old", "refresh_token": "ghr_old",
            "scope": "repo", "authorized_at": "2026-01-01T00:00:00+00:00",
        }
        blob = build_token_blob({"access_token": "ghu_new", "expires_in": 28800}, previous=previous)
        assert blob["authorized_at"] == "2026-01-01T00:00:00+00:00"
        assert blob["scope"] == "repo"
        # GitHub invalidates a refresh token on use: never carry it over.
        assert "refresh_token" not in blob


# ---------------------------------------------------------------------------
# get_github_token
# ---------------------------------------------------------------------------

class TestGetGitHubToken:
    def test_not_connected(self):
        assert _run(get_github_token(_user(None))) is None

    def test_non_expiring_token_skips_store_and_refresh(self, store):
        store.get_credential = AsyncMock(side_effect=AssertionError("no DB read"))
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher, patch(
            "db.user_service_credential_store.get_credential", store.get_credential,
        ):
            token = _run(get_github_token(_user({"access_token": "gho_classic"})))
        assert token == "gho_classic"
        client.post.assert_not_called()

    def test_valid_expiring_token_returned_without_refresh(self, store):
        blob = _app_blob("ghu_a", "ghr_a", timedelta(hours=4))
        store.blob = blob
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            token = _run(get_github_token(_user(blob)))
        assert token == "ghu_a"
        client.post.assert_not_called()

    def test_adopts_token_refreshed_by_another_turn(self, store):
        # This turn's user dict predates a refresh done by a routine: its
        # tokens are dead on GitHub's side. The stored row wins.
        stale = _app_blob("ghu_old", "ghr_old", timedelta(minutes=2))
        store.blob = _app_blob("ghu_fresh", "ghr_fresh", timedelta(hours=8))
        user = _user(stale)
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            token = _run(get_github_token(user))
        assert token == "ghu_fresh"
        client.post.assert_not_called()
        assert user["service_credentials"]["github"]["oauth_blob"]["access_token"] == "ghu_fresh"

    def test_near_expiry_refreshes_and_persists(self, store):
        blob = _app_blob("ghu_old", "ghr_old", timedelta(minutes=2))
        store.blob = blob
        user = _user(blob)
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            token = _run(get_github_token(user))

        assert token == "ghu_new"
        client.post.assert_awaited_once()
        args, kwargs = client.post.await_args
        assert args == (GITHUB_TOKEN_URL,)
        assert kwargs["data"] == {
            "client_id": "cid",
            "client_secret": "csec",
            "grant_type": "refresh_token",
            "refresh_token": "ghr_old",
        }
        assert kwargs["headers"]["Accept"] == "application/json"

        assert len(store.upserts) == 1
        saved = store.upserts[0]
        assert saved["access_token"] == "ghu_new"
        assert saved["refresh_token"] == "ghr_new"
        assert saved["authorized_at"] == "2026-01-01T00:00:00+00:00"
        assert not token_expires_within(saved)
        # The in-memory dict follows, so later calls in the turn reuse it.
        assert user["service_credentials"]["github"]["oauth_blob"] == saved

    def test_refresh_error_body_on_expired_token_returns_none(self, store):
        # GitHub reports refresh failures as HTTP 200 with an error body.
        blob = _app_blob("ghu_old", "ghr_old", -timedelta(minutes=1))
        store.blob = blob
        patcher, _ = _patch_token_endpoint(body={
            "error": "bad_refresh_token",
            "error_description": "The refresh token passed is incorrect or expired.",
        })
        with patcher:
            assert _run(get_github_token(_user(blob))) is None
        assert store.upserts == []

    def test_refresh_transport_error_falls_back_to_unexpired_token(self, store):
        blob = _app_blob("ghu_old", "ghr_old", timedelta(minutes=2))
        store.blob = blob
        patcher, _ = _patch_token_endpoint(exc=httpx.ConnectError("boom"))
        with patcher:
            assert _run(get_github_token(_user(blob))) == "ghu_old"
        assert store.upserts == []

    def test_disconnected_since_turn_start_returns_none(self, store):
        store.blob = None
        user = _user(_app_blob("ghu_old", "ghr_old", timedelta(hours=4)))
        assert _run(get_github_token(user)) is None

    def test_concurrent_callers_share_one_refresh(self, store):
        # Refresh tokens rotate: a second refresh with the same token
        # would fail, so concurrent callers must serialize on the lock.
        blob = _app_blob("ghu_old", "ghr_old", timedelta(minutes=2))
        store.blob = blob

        async def _both():
            # Separate user dicts, like a chat turn and a routine.
            return await asyncio.gather(
                get_github_token(_user(dict(blob))),
                get_github_token(_user(dict(blob))),
            )

        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            tokens = _run(_both())
        assert tokens == ["ghu_new", "ghu_new"]
        client.post.assert_awaited_once()
        assert len(store.upserts) == 1
