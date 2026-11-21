"""Unit tests for the Twitter/X plugin's upstream helpers.

Pure-function coverage (no network, no DB): admin config predicates,
granted-scope needs_reauth (incl. the migrated pre-plugin blob shape),
token-expiry checks, and token-blob construction (rotating-refresh-token
carry-over).
"""

from datetime import datetime, timedelta, timezone

from config.oauth_tokens import token_expires_within
from plugins.twitter.upstream import (
    TWITTER_SCOPES,
    build_token_blob,
    twitter_connected,
    twitter_is_configured,
    twitter_needs_reauth,
    validate_twitter_credentials,
)

_FULL_GRANT = "dm.read dm.write tweet.read users.read bookmark.read offline.access"


class TestAdminConfig:
    def test_is_configured_requires_both_fields(self):
        full = {"client_id": "c", "client_secret": "s"}
        assert twitter_is_configured(full)
        for missing in ("client_id", "client_secret"):
            partial = {**full, missing: ""}
            assert not twitter_is_configured(partial)
        assert not twitter_is_configured({})

    def test_validate_strips_whitespace(self):
        out = validate_twitter_credentials({
            "client_id": "  c1  ", "client_secret": "s\n",
        })
        assert out == {"client_id": "c1", "client_secret": "s"}


class TestConnected:
    def test_connected_requires_access_token(self):
        assert twitter_connected({"oauth_blob": {"access_token": "tok"}})
        assert not twitter_connected({"oauth_blob": {}})
        assert not twitter_connected({"oauth_blob": None})
        assert not twitter_connected({})


class TestNeedsReauth:
    def test_full_grant_needs_no_reauth(self):
        row = {"oauth_blob": {"access_token": "t", "scope": _FULL_GRANT}}
        assert not twitter_needs_reauth(row)

    def test_pre_bookmark_grant_needs_reauth(self):
        # The historical case: blobs granted before bookmark.read was
        # added (incl. blobs migrated from users.twitter_oauth).
        row = {"oauth_blob": {
            "access_token": "t",
            "scope": "dm.read dm.write tweet.read users.read offline.access",
        }}
        assert twitter_needs_reauth(row)

    def test_scopes_list_shape_read_too(self):
        row = {"oauth_blob": {"access_token": "t", "scopes": list(TWITTER_SCOPES)}}
        assert not twitter_needs_reauth(row)

    def test_missing_scope_reads_as_reauth(self):
        assert twitter_needs_reauth({"oauth_blob": {"access_token": "t"}})
        assert twitter_needs_reauth({"oauth_blob": {}})


class TestTokenExpiry:
    def test_future_expiry_not_expiring(self):
        blob = {"expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()}
        assert not token_expires_within(blob)

    def test_within_margin_is_expiring(self):
        blob = {"expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat()}
        assert token_expires_within(blob)

    def test_naive_timestamp_read_as_utc(self):
        naive_future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).replace(tzinfo=None).isoformat()
        assert not token_expires_within({"expires_at": naive_future})

    def test_missing_or_unparseable_reads_as_expired(self):
        assert token_expires_within({})
        assert token_expires_within({"expires_at": None})
        assert token_expires_within({"expires_at": "not-a-date"})


class TestBuildTokenBlob:
    def test_fresh_grant(self):
        blob = build_token_blob({
            "access_token": "at",
            "refresh_token": "rt",
            "token_type": "bearer",
            "expires_in": 7200,
            "scope": _FULL_GRANT,
        })
        assert blob["access_token"] == "at"
        assert blob["refresh_token"] == "rt"
        assert blob["scope"] == _FULL_GRANT
        assert set(TWITTER_SCOPES).issubset(set(blob["scopes"]))
        assert not token_expires_within(blob)

    def test_refresh_rotates_token_and_carries_authorized_at(self):
        previous = {
            "refresh_token": "old-rt",
            "authorized_at": "2026-01-01T00:00:00+00:00",
            "scope": _FULL_GRANT,
        }
        blob = build_token_blob(
            {"access_token": "at2", "refresh_token": "new-rt", "expires_in": 7200},
            previous=previous,
        )
        assert blob["refresh_token"] == "new-rt"
        assert blob["authorized_at"] == "2026-01-01T00:00:00+00:00"
        # Scope not echoed on refresh -> carried from the previous blob.
        assert blob["scope"] == _FULL_GRANT

    def test_refresh_without_rotation_keeps_previous_token(self):
        blob = build_token_blob(
            {"access_token": "at2", "expires_in": 7200},
            previous={"refresh_token": "old-rt"},
        )
        assert blob["refresh_token"] == "old-rt"

    def test_missing_expires_in_defaults_to_a_finite_expiry(self):
        blob = build_token_blob({"access_token": "at"})
        assert blob["expires_at"]
        assert not token_expires_within(blob)
