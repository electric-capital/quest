"""Tests for the Google Workspace Admin plugin's upstream helpers.

Covers the admin-card predicates (the ``enabled`` switch AND the borrowed
core Google OAuth client), the granted-scope ``needs_reauth`` check,
token-blob construction, and ``get_google_admin_token``: pass-through for
a live token, refresh near expiry (persisted + in-memory, refresh token
carried over), failure handling, and one refresh for concurrent callers.

The credential store and Google's token endpoint are faked -- no DB, no
network.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import plugins.google_admin.upstream as upstream
from plugins.google_admin.upstream import (
    GOOGLE_ADMIN_IDENTITY_SCOPES,
    GOOGLE_ADMIN_SCOPES,
    GOOGLE_TOKEN_URL,
    build_token_blob,
    get_google_admin_token,
    google_admin_connected,
    google_admin_is_configured,
    google_admin_needs_reauth,
    load_google_admin_client_config,
    token_expires_within,
    validate_google_admin_credentials,
)

_CLIENT = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "csec"}
_FULL_GRANT = " ".join(GOOGLE_ADMIN_SCOPES + GOOGLE_ADMIN_IDENTITY_SCOPES)


def _run(coro):
    return asyncio.run(coro)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _patch_google_client(config):
    return patch.object(upstream, "_google_client_config", return_value=config)


def _patch_store_config(config):
    return patch(
        "config.service_credentials.read_service_credentials", return_value=config,
    )


# ---------------------------------------------------------------------------
# Admin configuration
# ---------------------------------------------------------------------------

class TestAdminConfig:
    def test_scopes_are_all_read_only(self):
        assert GOOGLE_ADMIN_SCOPES
        assert all(scope.endswith(".readonly") for scope in GOOGLE_ADMIN_SCOPES)

    def test_configured_needs_the_switch_and_the_google_client(self):
        with _patch_google_client(_CLIENT):
            assert google_admin_is_configured({"enabled": True})
            assert not google_admin_is_configured({"enabled": False})
            assert not google_admin_is_configured({})
        with _patch_google_client(None):
            assert not google_admin_is_configured({"enabled": True})

    def test_enabling_without_a_google_client_is_rejected(self):
        with _patch_google_client(None):
            with pytest.raises(ValueError, match="Google OAuth client"):
                validate_google_admin_credentials({"enabled": True})
            # Switching off is always allowed.
            assert validate_google_admin_credentials({"enabled": False}) \
                == {"enabled": False}
        with _patch_google_client(_CLIENT):
            assert validate_google_admin_credentials({"enabled": True}) \
                == {"enabled": True}

    def test_client_config_loader_requires_the_switch(self):
        from fastapi import HTTPException

        with _patch_google_client(_CLIENT), _patch_store_config({"enabled": True}):
            assert load_google_admin_client_config() == _CLIENT
        for stored in (None, {}, {"enabled": False}):
            with _patch_google_client(_CLIENT), _patch_store_config(stored):
                with pytest.raises(HTTPException, match="not enabled"):
                    load_google_admin_client_config()
        with _patch_google_client(None), _patch_store_config({"enabled": True}):
            with pytest.raises(HTTPException, match="not configured"):
                load_google_admin_client_config()


# ---------------------------------------------------------------------------
# Connection predicates
# ---------------------------------------------------------------------------

class TestConnection:
    def test_connected_requires_access_token(self):
        assert google_admin_connected({"oauth_blob": {"access_token": "tok"}})
        assert not google_admin_connected({"oauth_blob": {}})
        assert not google_admin_connected({"oauth_blob": None})
        assert not google_admin_connected({})

    def test_needs_reauth_false_on_full_grant(self):
        assert google_admin_needs_reauth(
            {"oauth_blob": {"scopes": _FULL_GRANT.split()}}
        ) is False

    def test_identity_scopes_are_not_required(self):
        assert google_admin_needs_reauth(
            {"oauth_blob": {"scopes": list(GOOGLE_ADMIN_SCOPES)}}
        ) is False

    def test_needs_reauth_true_when_a_scope_was_unticked(self):
        partial = [s for s in GOOGLE_ADMIN_SCOPES if "device.mobile" not in s]
        assert google_admin_needs_reauth({"oauth_blob": {"scopes": partial}}) is True

    def test_needs_reauth_true_on_empty_blob(self):
        assert google_admin_needs_reauth({"oauth_blob": {}}) is True
        assert google_admin_needs_reauth({}) is True


# ---------------------------------------------------------------------------
# Token blob
# ---------------------------------------------------------------------------

class TestTokenExpiry:
    def test_missing_or_bad_expiry_reads_as_expired(self):
        assert token_expires_within({}) is True
        assert token_expires_within({"expires_at": "not-a-date"}) is True

    def test_future_token_is_not_expiring(self):
        assert token_expires_within({"expires_at": _iso(timedelta(hours=1))}) is False

    def test_near_expiry_within_margin(self):
        assert token_expires_within({"expires_at": _iso(timedelta(seconds=60))}) is True


class TestBuildTokenBlob:
    def test_code_exchange_blob(self):
        blob = build_token_blob({
            "access_token": "at", "refresh_token": "rt", "token_type": "Bearer",
            "expires_in": 3599, "scope": _FULL_GRANT,
        }, account={"email": "admin@example.com", "domain": "example.com"})
        assert blob["access_token"] == "at"
        assert blob["refresh_token"] == "rt"
        assert blob["scopes"] == _FULL_GRANT.split()
        assert blob["account"] == {"email": "admin@example.com", "domain": "example.com"}
        assert blob["authorized_at"]
        remaining = (
            datetime.fromisoformat(blob["expires_at"]) - datetime.now(timezone.utc)
        ).total_seconds()
        assert 3500 < remaining <= 3599

    def test_refresh_carries_over_what_google_does_not_repeat(self):
        previous = {
            "refresh_token": "rt",
            "account": {"email": "admin@example.com", "domain": "example.com"},
            "authorized_at": "2026-01-01T00:00:00+00:00",
            "scope": _FULL_GRANT,
            "scopes": _FULL_GRANT.split(),
        }
        # Google's refresh response has no refresh_token.
        blob = build_token_blob(
            {"access_token": "new-at", "expires_in": 3599}, previous=previous,
        )
        assert blob["access_token"] == "new-at"
        assert blob["refresh_token"] == "rt"
        assert blob["account"] == previous["account"]
        assert blob["authorized_at"] == "2026-01-01T00:00:00+00:00"
        assert blob["scopes"] == _FULL_GRANT.split()

    def test_unparseable_expires_in_defaults(self):
        blob = build_token_blob({"access_token": "at", "expires_in": "soon"})
        assert datetime.fromisoformat(blob["expires_at"]) > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# get_google_admin_token
# ---------------------------------------------------------------------------

def _blob(access: str, expires_in: timedelta, refresh: str | None = "rt") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "scope": _FULL_GRANT,
        "scopes": _FULL_GRANT.split(),
        "expires_at": _iso(expires_in),
        "account": {"email": "admin@example.com", "domain": "example.com"},
        "authorized_at": "2026-01-01T00:00:00+00:00",
    }


def _user(blob: dict | None) -> dict:
    user = {"id": 7, "email": "u@example.com"}
    if blob is not None:
        user["service_credentials"] = {
            "google_admin": {"service": "google_admin", "secret": None, "oauth_blob": blob},
        }
    return user


@pytest.fixture
def upserts():
    """Capture credential-store writes; server config is enabled."""
    # Each test runs its own event loop; a lock created in an earlier
    # test's loop cannot be reused.
    upstream._refresh_locks.clear()
    writes: list = []

    async def fake_upsert(user_id, service, *, secret=None, oauth_blob=None):
        writes.append((user_id, service, oauth_blob))

    with patch("db.user_service_credential_store.upsert_credential", fake_upsert), \
            patch.object(upstream, "load_google_admin_client_config", return_value=_CLIENT):
        yield writes


def _patch_token_endpoint(*, status_code=200, body=None, exc=None):
    """Patch httpx.AsyncClient in the upstream module; returns the client
    mock so tests can inspect the refresh POST."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=body if body is not None else {})
    response.text = str(body)

    client = MagicMock()
    client.post = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=response)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(upstream.httpx, "AsyncClient", return_value=ctx), client


_REFRESHED = {
    "access_token": "ya29.new",
    "expires_in": 3599,
    "scope": _FULL_GRANT,
    "token_type": "Bearer",
}


class TestGetToken:
    def test_not_connected_returns_none(self, upserts):
        assert _run(get_google_admin_token(_user(None))) is None

    def test_live_token_is_returned_without_refresh(self, upserts):
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            token = _run(get_google_admin_token(_user(_blob("ya29.live", timedelta(hours=1)))))
        assert token == "ya29.live"
        client.post.assert_not_awaited()
        assert upserts == []

    def test_expiring_token_is_refreshed_and_persisted(self, upserts):
        user = _user(_blob("ya29.old", timedelta(seconds=30)))
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            token = _run(get_google_admin_token(user))

        assert token == "ya29.new"
        url = client.post.await_args.args[0]
        data = client.post.await_args.kwargs["data"]
        assert url == GOOGLE_TOKEN_URL
        assert data == {
            "client_id": _CLIENT["client_id"],
            "client_secret": _CLIENT["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": "rt",
        }

        assert len(upserts) == 1
        user_id, service, stored = upserts[0]
        assert (user_id, service) == (7, "google_admin")
        assert stored["access_token"] == "ya29.new"
        # Google does not rotate the refresh token: the old one survives.
        assert stored["refresh_token"] == "rt"
        assert stored["account"]["email"] == "admin@example.com"
        # Later calls in the same turn see the new token.
        assert user["service_credentials"]["google_admin"]["oauth_blob"] is stored

    def test_revoked_grant_returns_none_without_persisting(self, upserts):
        user = _user(_blob("ya29.old", -timedelta(minutes=5)))
        patcher, _ = _patch_token_endpoint(
            status_code=400, body={"error": "invalid_grant"},
        )
        with patcher:
            assert _run(get_google_admin_token(user)) is None
        assert upserts == []

    def test_transport_error_returns_none(self, upserts):
        user = _user(_blob("ya29.old", -timedelta(minutes=5)))
        patcher, _ = _patch_token_endpoint(exc=httpx.ConnectError("boom"))
        with patcher:
            assert _run(get_google_admin_token(user)) is None
        assert upserts == []

    def test_no_refresh_token_returns_none(self, upserts):
        user = _user(_blob("ya29.old", -timedelta(minutes=5), refresh=None))
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher:
            assert _run(get_google_admin_token(user)) is None
        client.post.assert_not_awaited()

    def test_disabled_plugin_does_not_refresh(self, upserts):
        from fastapi import HTTPException

        user = _user(_blob("ya29.old", -timedelta(minutes=5)))
        patcher, client = _patch_token_endpoint(body=_REFRESHED)
        with patcher, patch.object(
            upstream, "load_google_admin_client_config",
            side_effect=HTTPException(status_code=500, detail="not enabled"),
        ):
            assert _run(get_google_admin_token(user)) is None
        client.post.assert_not_awaited()

    def test_concurrent_callers_share_one_refresh(self, upserts):
        user = _user(_blob("ya29.old", timedelta(seconds=30)))
        patcher, client = _patch_token_endpoint(body=_REFRESHED)

        async def both():
            return await asyncio.gather(
                get_google_admin_token(user), get_google_admin_token(user),
            )

        with patcher:
            tokens = _run(both())
        assert tokens == ["ya29.new", "ya29.new"]
        assert client.post.await_count == 1
        assert len(upserts) == 1
