"""Unit tests for the Microsoft 365 plugin's upstream helpers.

Pure-function coverage (no network, no DB): admin config predicates,
granted-scope normalization / needs_reauth, token-expiry checks, and
token-blob construction (refresh-token rotation carry-over).
"""

from datetime import datetime, timedelta, timezone

from config.oauth_tokens import token_expires_within
from plugins.m365.upstream import (
    M365_SCOPES,
    build_token_blob,
    m365_connected,
    m365_is_configured,
    m365_needs_reauth,
    normalize_scopes,
    validate_m365_credentials,
)

_FULL_GRANT = "offline_access User.Read Mail.ReadWrite Mail.Send"


class TestAdminConfig:
    def test_is_configured_requires_all_three_fields(self):
        full = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
        assert m365_is_configured(full)
        for missing in ("tenant_id", "client_id", "client_secret"):
            partial = {**full, missing: ""}
            assert not m365_is_configured(partial)
        assert not m365_is_configured({})

    def test_validate_strips_whitespace(self):
        out = validate_m365_credentials({
            "tenant_id": "  t1  ", "client_id": "c\n", "client_secret": "s",
        })
        assert out == {"tenant_id": "t1", "client_id": "c", "client_secret": "s"}


class TestConnected:
    def test_connected_requires_access_token(self):
        assert m365_connected({"oauth_blob": {"access_token": "tok"}})
        assert not m365_connected({"oauth_blob": {}})
        assert not m365_connected({"oauth_blob": None})
        assert not m365_connected({})


class TestScopes:
    def test_normalize_from_string(self):
        assert normalize_scopes("User.Read Mail.Send") == {"user.read", "mail.send"}

    def test_normalize_from_list(self):
        assert normalize_scopes(["Mail.ReadWrite"]) == {"mail.readwrite"}

    def test_normalize_strips_graph_resource_uri(self):
        assert normalize_scopes(
            "https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/User.Read"
        ) == {"mail.send", "user.read"}

    def test_normalize_empty(self):
        assert normalize_scopes(None) == set()
        assert normalize_scopes("") == set()

    def test_needs_reauth_false_on_full_grant(self):
        row = {"oauth_blob": {"scopes": _FULL_GRANT.split()}}
        assert m365_needs_reauth(row) is False

    def test_needs_reauth_false_on_uri_prefixed_grant(self):
        granted = " ".join(
            f"https://graph.microsoft.com/{s}" for s in M365_SCOPES
        )
        row = {"oauth_blob": {"scopes": granted.split()}}
        assert m365_needs_reauth(row) is False

    def test_needs_reauth_true_when_scope_missing(self):
        row = {"oauth_blob": {"scopes": ["User.Read", "Mail.ReadWrite"]}}
        assert m365_needs_reauth(row) is True

    def test_needs_reauth_falls_back_to_scope_string(self):
        row = {"oauth_blob": {"scope": _FULL_GRANT}}
        assert m365_needs_reauth(row) is False

    def test_needs_reauth_true_on_empty_blob(self):
        assert m365_needs_reauth({"oauth_blob": {}}) is True
        assert m365_needs_reauth({}) is True

    def test_offline_access_not_required_in_grant(self):
        # Microsoft does not echo offline_access back consistently; its
        # absence must not flag re-auth.
        row = {"oauth_blob": {"scope": " ".join(M365_SCOPES)}}
        assert m365_needs_reauth(row) is False


class TestTokenExpiry:
    def test_missing_or_bad_expiry_reads_as_expired(self):
        assert token_expires_within({}) is True
        assert token_expires_within({"expires_at": "not-a-date"}) is True

    def test_future_token_is_not_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert token_expires_within({"expires_at": future}) is False

    def test_near_expiry_within_margin(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        assert token_expires_within({"expires_at": soon}) is True

    def test_naive_datetime_treated_as_utc(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)) \
            .replace(tzinfo=None).isoformat()
        assert token_expires_within({"expires_at": future}) is False


class TestBuildTokenBlob:
    def test_basic_fields(self):
        blob = build_token_blob({
            "access_token": "at", "refresh_token": "rt",
            "token_type": "Bearer", "expires_in": 3600,
            "scope": _FULL_GRANT,
        }, account={"email": "user@contoso.com"})
        assert blob["access_token"] == "at"
        assert blob["refresh_token"] == "rt"
        assert blob["scopes"] == _FULL_GRANT.split()
        assert blob["account"] == {"email": "user@contoso.com"}
        expires_at = datetime.fromisoformat(blob["expires_at"])
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 3500 < remaining <= 3600

    def test_refresh_carries_over_prior_fields(self):
        previous = {
            "refresh_token": "old-rt",
            "account": {"email": "user@contoso.com"},
            "authorized_at": "2026-01-01T00:00:00+00:00",
            "scope": _FULL_GRANT,
        }
        # Microsoft rotated only the access token (no refresh_token in the
        # refresh response): the prior refresh token must survive.
        blob = build_token_blob(
            {"access_token": "new-at", "expires_in": 3600},
            previous=previous,
        )
        assert blob["refresh_token"] == "old-rt"
        assert blob["account"] == {"email": "user@contoso.com"}
        assert blob["authorized_at"] == "2026-01-01T00:00:00+00:00"
        assert blob["scope"] == _FULL_GRANT

    def test_rotated_refresh_token_wins(self):
        blob = build_token_blob(
            {"access_token": "at", "refresh_token": "new-rt", "expires_in": 10},
            previous={"refresh_token": "old-rt"},
        )
        assert blob["refresh_token"] == "new-rt"

    def test_unparseable_expires_in_defaults(self):
        blob = build_token_blob({"access_token": "at", "expires_in": "soon"})
        expires_at = datetime.fromisoformat(blob["expires_at"])
        assert expires_at > datetime.now(timezone.utc)
