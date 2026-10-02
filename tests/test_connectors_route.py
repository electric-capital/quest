"""Tests for the GET /connectors list shape (chat/routes/user.py).

The endpoint returns a LIST of generically renderable rows (kind "oauth"
or "api_key") instead of the old fixed-key dict; these tests pin the row
contract the frontend maps over.
"""

import asyncio
from unittest.mock import patch

import chat.routes.user as user_routes


def _run(coro):
    return asyncio.run(coro)


class _Unconfigured(Exception):
    pass


def _patch_core_loaders(configured=False):
    """Patch the per-service server-credential loaders get_connectors probes.

    ``configured=False`` makes every loader raise (no integration
    credentials anywhere -- including the legacy repo-root fallback files a
    dev machine might have); ``configured=True`` makes them all succeed.
    """
    def _loader():
        if not configured:
            raise _Unconfigured()
        return {"client_id": "x"}

    return [
        patch("auth.config.load_google_oauth_config", side_effect=_loader),
    ]


def _get_connectors(user, core_configured=False):
    from config import plugins as plugins_mod
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.object(plugins_mod, "_LOADED", []))
        stack.enter_context(
            patch("config.service_credentials.read_service_credentials",
                  return_value=None))
        for p in _patch_core_loaders(configured=core_configured):
            stack.enter_context(p)
        return _run(user_routes.get_connectors(user=user))["connectors"]


def test_connectors_is_a_list_of_rows():
    rows = _get_connectors({"email": "u@example.com"})
    assert isinstance(rows, list)
    services = [row["service"] for row in rows]
    # Slack and Telegram are plugin-provided now (plugins/slack,
    # plugins/telegram); with no plugins loaded there are no such rows.
    assert services == [
        "google_services",
        "airtable", "ramp",
    ]
    for row in rows:
        assert row["kind"] in ("oauth", "api_key")
        assert isinstance(row["label"], str) and row["label"]
        assert isinstance(row["connected"], bool)
        if row["kind"] == "oauth":
            assert row["connect_url"].startswith("/auth/")
        else:
            assert row["key_url"].startswith("/auth/")
            assert row["key_field"]
            assert row["disconnect_url"].startswith("/auth/")


def test_core_rows_available_tracks_server_credentials():
    # No server-side integration credentials anywhere -> every core row
    # that needs them is unavailable (hidden by the FE).
    rows = {r["service"]: r for r in _get_connectors({"email": "u@example.com"})}
    for service in ("google_services",):
        assert rows[service]["available"] is False
    # Airtable needs no server-side integration credentials (the user
    # supplies their own PAT), so it carries no available flag at all.
    assert "available" not in rows["airtable"]

    # All loaders succeed -> the same rows are available.
    rows = {
        r["service"]: r
        for r in _get_connectors({"email": "u@example.com"}, core_configured=True)
    }
    for service in ("google_services",):
        assert rows[service]["available"] is True


def test_unconfigured_ramp_is_unavailable():
    rows = {r["service"]: r for r in _get_connectors({"email": "u@example.com"})}
    assert rows["ramp"]["available"] is False


def test_connected_flags_and_key_hints():
    from config import plugins as plugins_mod
    user = {
        "email": "u@example.com",
        "google_services_oauth": {"scopes": []},
        "airtable_token": "patXYZ1234",
    }

    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.object(plugins_mod, "_LOADED", []))
        stack.enter_context(
            patch("config.service_credentials.read_service_credentials",
                  return_value={"client_id": "x"}))
        for p in _patch_core_loaders(configured=True):
            stack.enter_context(p)
        rows = {
            r["service"]: r
            for r in _run(user_routes.get_connectors(user=user))["connectors"]
        }
    assert rows["google_services"]["connected"] is True
    # Empty granted-scope set no longer covers the required scopes.
    assert rows["google_services"]["needs_reauth"] is True
    assert rows["airtable"]["key_hint"] == "1234"
    assert rows["ramp"]["available"] is True


# ---------------------------------------------------------------------------
# Plugin rows (api_key user connections)
# ---------------------------------------------------------------------------

def _api_key_plugin(pid="acme", key_hint=True):
    from config.plugin_types import (
        CredentialField, QuestPlugin, UserConnectionSpec,
    )
    return QuestPlugin(
        id=pid,
        label=pid.title(),
        credential_schema=(
            CredentialField(key="enabled", label="Enabled", type="bool"),
        ),
        is_configured=lambda config: bool(config.get("enabled")),
        user_connection=UserConnectionSpec(
            kind="api_key",
            connected=lambda row: bool(row.get("secret")),
            key_hint=key_hint,
        ),
    )


def _get_connectors_with_plugin(user, plugin, server_config):
    from config import plugins as plugins_mod
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.object(plugins_mod, "_LOADED", [plugin]))
        stack.enter_context(
            patch("config.service_credentials.read_service_credentials",
                  return_value=server_config))
        for p in _patch_core_loaders(configured=False):
            stack.enter_context(p)
        return {
            r["service"]: r
            for r in _run(user_routes.get_connectors(user=user))["connectors"]
        }


def test_plugin_api_key_row_appended_generically():
    user = {
        "email": "u@example.com",
        "service_credentials": {
            "acme": {"service": "acme", "secret": "sk-12345678"},
        },
    }
    rows = _get_connectors_with_plugin(user, _api_key_plugin(), {"enabled": True})
    row = rows["acme"]
    assert row["kind"] == "api_key"
    assert row["label"] == "Acme"
    assert row["connected"] is True
    assert row["available"] is True
    assert row["key_hint"] == "5678"
    assert row["key_url"] == "/auth/service-key/acme"
    assert row["key_field"] == "api_key"
    assert row["disconnect_url"] == "/auth/service-key/acme/remove"


def test_plugin_row_unavailable_and_disconnected_states():
    # Server config disabled -> row hidden via available: false.
    rows = _get_connectors_with_plugin(
        {"email": "u@example.com"}, _api_key_plugin(), {"enabled": False},
    )
    assert rows["acme"]["available"] is False
    assert rows["acme"]["connected"] is False
    assert rows["acme"]["key_hint"] is None

    # key_hint: False plugins never expose the last-4.
    user = {
        "email": "u@example.com",
        "service_credentials": {"acme": {"service": "acme", "secret": "sk-1"}},
    }
    rows = _get_connectors_with_plugin(
        user, _api_key_plugin(key_hint=False), {"enabled": True},
    )
    assert rows["acme"]["connected"] is True
    assert rows["acme"]["key_hint"] is None


# ---------------------------------------------------------------------------
# Plugin rows (oauth user connections)
# ---------------------------------------------------------------------------

def test_oauth_kind_plugin_row_appended_generically():
    from config.plugin_types import QuestPlugin, UserConnectionSpec
    plugin = QuestPlugin(
        id="acme", label="Acme",
        user_connection=UserConnectionSpec(
            kind="oauth",
            connected=lambda row: bool(
                (row.get("oauth_blob") or {}).get("access_token")
            ),
            needs_reauth=lambda row: (
                (row.get("oauth_blob") or {}).get("scope") != "full"
            ),
        ),
    )

    # Disconnected: no stored row -> connected False, needs_reauth False
    # (the hook only runs against a stored row).
    rows = _get_connectors_with_plugin({"email": "u@example.com"}, plugin, None)
    row = rows["acme"]
    assert row["kind"] == "oauth"
    assert row["connect_url"] == "/auth/acme?popup=1"
    assert row["connected"] is False
    assert row["needs_reauth"] is False
    # No credential schema -> no server-side gating, but plugin rows always
    # carry an explicit available flag.
    assert row["available"] is True
    assert "key_url" not in row and "disconnect_url" not in row

    # Connected with a stale grant -> needs_reauth True.
    user = {
        "email": "u@example.com",
        "service_credentials": {
            "acme": {
                "service": "acme",
                "oauth_blob": {"access_token": "tok", "scope": "partial"},
            },
        },
    }
    rows = _get_connectors_with_plugin(user, plugin, None)
    assert rows["acme"]["connected"] is True
    assert rows["acme"]["needs_reauth"] is True


def test_oauth_kind_plugin_connect_url_hyphenates_the_id():
    """An underscore in the plugin id is a hyphen in its /auth namespace."""
    from config.plugin_types import QuestPlugin, UserConnectionSpec
    plugin = QuestPlugin(
        id="acme_admin", label="Acme Admin",
        user_connection=UserConnectionSpec(
            kind="oauth", connected=lambda row: True,
        ),
    )
    rows = _get_connectors_with_plugin({"email": "u@example.com"}, plugin, None)
    assert rows["acme_admin"]["connect_url"] == "/auth/acme-admin?popup=1"


def test_github_plugin_oauth_row(github_plugin):
    """The in-tree github plugin's row: same URLs as the old core row."""
    user = {
        "email": "u@example.com",
        "service_credentials": {
            "github": {
                "service": "github",
                "oauth_blob": {"access_token": "gho_x", "scope": "repo,read:org"},
            },
        },
    }
    rows = _get_connectors_with_plugin(
        user, github_plugin, {"client_id": "id", "client_secret": "sec"},
    )
    row = rows["github"]
    assert row["kind"] == "oauth"
    assert row["connect_url"] == "/auth/github?popup=1"
    assert row["connected"] is True
    assert row["available"] is True
    # Granted scopes cover GITHUB_SCOPES -> no re-auth badge.
    assert row["needs_reauth"] is False

    # A pre-plugin blob granted before read:org existed -> re-auth badge.
    user["service_credentials"]["github"]["oauth_blob"] = {
        "access_token": "gho_x", "scope": "repo",
    }
    rows = _get_connectors_with_plugin(
        user, github_plugin, {"client_id": "id", "client_secret": "sec"},
    )
    assert rows["github"]["needs_reauth"] is True

    # Unconfigured server side -> row hidden via available: False.
    rows = _get_connectors_with_plugin(user, github_plugin, None)
    assert rows["github"]["available"] is False


def test_twitter_plugin_oauth_row(twitter_plugin):
    """The in-tree twitter plugin's row: same URLs as the old core row."""
    user = {
        "email": "u@example.com",
        "service_credentials": {
            "twitter": {
                "service": "twitter",
                "oauth_blob": {
                    "access_token": "tok",
                    "scope": "dm.read dm.write tweet.read users.read "
                             "bookmark.read offline.access",
                },
            },
        },
    }
    rows = _get_connectors_with_plugin(
        user, twitter_plugin, {"client_id": "id", "client_secret": "sec"},
    )
    row = rows["twitter"]
    assert row["kind"] == "oauth"
    assert row["connect_url"] == "/auth/twitter?popup=1"
    assert row["connected"] is True
    assert row["available"] is True
    # Granted scopes cover TWITTER_SCOPES -> no re-auth badge.
    assert row["needs_reauth"] is False

    # A blob granted before bookmark.read existed -> re-auth badge.
    user["service_credentials"]["twitter"]["oauth_blob"] = {
        "access_token": "tok",
        "scope": "dm.read dm.write tweet.read users.read offline.access",
    }
    rows = _get_connectors_with_plugin(
        user, twitter_plugin, {"client_id": "id", "client_secret": "sec"},
    )
    assert rows["twitter"]["needs_reauth"] is True

    # Unconfigured server side -> row hidden via available: False.
    rows = _get_connectors_with_plugin(user, twitter_plugin, None)
    assert rows["twitter"]["available"] is False


# ---------------------------------------------------------------------------
# The real Slack plugin's row (oauth kind, credential-row connection)
# ---------------------------------------------------------------------------

def test_slack_plugin_row(slack_plugin):
    from config import plugins as plugins_mod
    from contextlib import ExitStack

    user = {
        "email": "u@example.com",
        "service_credentials": {
            "slack": {"service": "slack",
                      "oauth_blob": {"access_token": "tok"}},
        },
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(plugins_mod, "_LOADED", [slack_plugin]))
        stack.enter_context(
            patch("config.service_credentials.read_service_credentials",
                  return_value={"client_id": "x", "client_secret": "y"}))
        for p in _patch_core_loaders(configured=False):
            stack.enter_context(p)
        rows = {
            r["service"]: r
            for r in _run(user_routes.get_connectors(user=user))["connectors"]
        }

    row = rows["slack"]
    assert row["kind"] == "oauth"
    assert row["label"] == "Slack"
    assert row["connect_url"] == "/auth/slack?popup=1"
    assert row["connected"] is True
    assert row["available"] is True


# ---------------------------------------------------------------------------
# The real Telegram plugin's row (oauth kind, non-OAuth login router)
# ---------------------------------------------------------------------------

def test_telegram_plugin_row(telegram_plugin):
    from config import plugins as plugins_mod
    from contextlib import ExitStack

    def _rows(user):
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugins_mod, "_LOADED", [telegram_plugin]))
            stack.enter_context(
                patch("config.service_credentials.read_service_credentials",
                      return_value={"api_id": "123456", "api_hash": "abcdef"}))
            for p in _patch_core_loaders(configured=False):
                stack.enter_context(p)
            return {
                r["service"]: r
                for r in _run(user_routes.get_connectors(user=user))["connectors"]
            }

    connected = {
        "email": "u@example.com",
        "service_credentials": {
            "telegram": {"service": "telegram",
                         "oauth_blob": {"session": "1ApWapzMBu...", "phone": "+15551234567"}},
        },
    }
    row = _rows(connected)["telegram"]
    assert row["kind"] == "oauth"
    assert row["label"] == "Telegram"
    assert row["connect_url"] == "/auth/telegram?popup=1"
    assert row["connected"] is True
    assert row["available"] is True
    assert row["needs_reauth"] is False

    # A pending login (code sent, not verified) is not a connection.
    pending = {
        "email": "u@example.com",
        "service_credentials": {
            "telegram": {"service": "telegram",
                         "oauth_blob": {"pending": {"phone": "+15551234567", "stage": "code"}}},
        },
    }
    assert _rows(pending)["telegram"]["connected"] is False
    assert _rows({"email": "u@example.com"})["telegram"]["connected"] is False
