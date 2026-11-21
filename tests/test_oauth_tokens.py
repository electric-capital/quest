"""Shared per-user OAuth token helpers (config/oauth_tokens.py).

Covers the near-expiry check, the blob accessors on the user dict, the
persist-and-update step, and the locked refresh path that the expiring-token
plugins (Twitter/X, Microsoft 365, Google Workspace Admin, GitHub) build on.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from config.oauth_tokens import (
    current_or_refreshed_token,
    get_user_oauth_blob,
    set_user_oauth_blob,
    store_user_oauth_blob,
    token_expires_within,
)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _user(blob: dict | None = None) -> dict:
    user = {"id": 7, "email": "u@example.com"}
    if blob is not None:
        user["service_credentials"] = {"acme": {"oauth_blob": blob}}
    return user


def _live(token: str = "live") -> dict:
    return {"access_token": token, "refresh_token": "r", "expires_at": _iso(timedelta(hours=1))}


def _stale(token: str = "stale") -> dict:
    return {"access_token": token, "refresh_token": "r", "expires_at": _iso(timedelta(seconds=30))}


class TestTokenExpiresWithin:
    def test_far_future_is_not_expiring(self):
        assert token_expires_within({"expires_at": _iso(timedelta(hours=1))}) is False

    def test_inside_the_margin_is_expiring(self):
        assert token_expires_within({"expires_at": _iso(timedelta(seconds=60))}) is True

    def test_explicit_margin(self):
        blob = {"expires_at": _iso(timedelta(seconds=60))}
        assert token_expires_within(blob, 0) is False
        assert token_expires_within({"expires_at": _iso(-timedelta(seconds=1))}, 0) is True

    def test_naive_timestamp_is_read_as_utc(self):
        naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
        assert token_expires_within({"expires_at": naive.isoformat()}) is False

    def test_unparseable_reads_as_expired(self):
        assert token_expires_within({"expires_at": "not-a-date"}) is True
        assert token_expires_within({"expires_at": "not-a-date"}, missing_means_expired=False) is True

    def test_missing_reads_as_expired_by_default(self):
        assert token_expires_within({}) is True
        assert token_expires_within({"expires_at": None}) is True

    def test_missing_can_mean_never_expires(self):
        assert token_expires_within({}, missing_means_expired=False) is False


class TestUserBlob:
    def test_get_returns_none_when_not_connected(self):
        assert get_user_oauth_blob({"id": 1}, "acme") is None
        assert get_user_oauth_blob({"id": 1, "service_credentials": {"acme": None}}, "acme") is None

    def test_set_creates_the_row_and_keeps_other_fields(self):
        user = {"id": 1}
        set_user_oauth_blob(user, "acme", {"access_token": "a"})
        assert get_user_oauth_blob(user, "acme") == {"access_token": "a"}

        user["service_credentials"]["acme"]["description"] = "work"
        set_user_oauth_blob(user, "acme", {"access_token": "b"})
        assert user["service_credentials"]["acme"] == {
            "oauth_blob": {"access_token": "b"}, "description": "work",
        }

    def test_store_persists_and_updates_the_user_dict(self):
        writes = []

        async def fake_upsert(user_id, service, *, secret=None, oauth_blob=None):
            writes.append((user_id, service, oauth_blob))

        user = _user(_stale())
        new_blob = _live("new")
        with patch("db.user_service_credential_store.upsert_credential", fake_upsert):
            asyncio.run(store_user_oauth_blob(user, "acme", new_blob))

        assert writes == [(7, "acme", new_blob)]
        assert get_user_oauth_blob(user, "acme") is new_blob


class TestCurrentOrRefreshedToken:
    @staticmethod
    def _refresher(calls: list, result: str | None = "new"):
        async def refresh(user, blob):
            calls.append(blob["access_token"])
            await asyncio.sleep(0)
            if result is not None:
                set_user_oauth_blob(user, "acme", _live(result))
            return result
        return refresh

    def test_not_connected_returns_none(self):
        calls: list = []
        refresh = self._refresher(calls)
        assert asyncio.run(current_or_refreshed_token(_user(), "acme", refresh, {})) is None
        assert asyncio.run(current_or_refreshed_token(_user({}), "acme", refresh, {})) is None
        assert calls == []

    def test_live_token_is_returned_without_refresh(self):
        calls: list = []
        token = asyncio.run(current_or_refreshed_token(
            _user(_live()), "acme", self._refresher(calls), {},
        ))
        assert token == "live"
        assert calls == []

    def test_near_expiry_token_is_refreshed(self):
        calls: list = []
        token = asyncio.run(current_or_refreshed_token(
            _user(_stale()), "acme", self._refresher(calls), {},
        ))
        assert token == "new"
        assert calls == ["stale"]

    def test_failed_refresh_returns_none(self):
        calls: list = []
        token = asyncio.run(current_or_refreshed_token(
            _user(_stale()), "acme", self._refresher(calls, result=None), {},
        ))
        assert token is None
        assert calls == ["stale"]

    def test_force_refresh_skips_the_expiry_check(self):
        calls: list = []
        token = asyncio.run(current_or_refreshed_token(
            _user(_live()), "acme", self._refresher(calls), {}, force_refresh=True,
        ))
        assert token == "new"
        assert calls == ["live"]

    def test_concurrent_callers_refresh_once(self):
        calls: list = []
        refresh = self._refresher(calls)
        user = _user(_stale())
        locks: dict = {}

        async def both():
            return await asyncio.gather(
                current_or_refreshed_token(user, "acme", refresh, locks),
                current_or_refreshed_token(user, "acme", refresh, locks),
            )

        assert asyncio.run(both()) == ["new", "new"]
        assert calls == ["stale"]
        assert list(locks) == [7]
