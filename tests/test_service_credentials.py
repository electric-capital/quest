"""Tests for the per-service upstream credential store.

Covers ``config/service_credentials.py`` (read/write with restrictive
permissions, legacy fallback reads, startup migration semantics), the
``load_google_oauth_config()`` precedence in ``auth/config.py`` (store wins
over server_credentials.json), and the admin service-credentials endpoints
in ``chat/routes/admin.py`` (masked reads, keep-secret-on-empty writes,
admin gating). Everything runs against tmp_path -- no real data directory
or network involved.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest
from fastapi import HTTPException

import config.service_credentials as sc


GOOGLE_CONFIG = {
    "web": {
        "client_id": "abc.apps.googleusercontent.com",
        "client_secret": "topsecret",
        "project_id": "my-project",
        "redirect_uris": ["https://example.com/auth/callback"],
    }
}

SLACK_CONFIG = {
    "client_id": "slack-id",
    "client_secret": "slack-secret",
    "bot_token": "xoxb-token",
    "socket_mode_token": "xapp-token",
}

GITHUB_CONFIG = {"client_id": "gh-id", "client_secret": "gh-secret"}

TWITTER_CONFIG = {"client_id": "tw-id", "client_secret": "tw-secret"}

COINGECKO_CONFIG = {"api_key": "cg-store-key"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store and the legacy files at tmp_path."""
    store_dir = tmp_path / "service_credentials"
    legacy_file = tmp_path / "server_credentials.json"
    monkeypatch.setattr(sc, "SERVICE_CREDENTIALS_DIR", store_dir)
    monkeypatch.setattr(sc, "LEGACY_CREDENTIALS_FILE", legacy_file)
    monkeypatch.setattr(
        sc, "LEGACY_TWITTER_CREDENTIALS_FILE", tmp_path / "twitter_credentials.json"
    )
    return store_dir, legacy_file


# ---------------------------------------------------------------------------
# Store read/write
# ---------------------------------------------------------------------------

def test_write_then_read_round_trip(store):
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    assert sc.read_service_credentials("google_oauth") == GOOGLE_CONFIG


def test_write_sets_restrictive_permissions(store):
    store_dir, _ = store
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    file_mode = stat.S_IMODE(os.stat(store_dir / "google_oauth.json").st_mode)
    dir_mode = stat.S_IMODE(os.stat(store_dir).st_mode)
    assert file_mode == 0o600
    assert dir_mode == 0o700


def test_write_leaves_no_temp_files(store):
    store_dir, _ = store
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    assert sorted(p.name for p in store_dir.iterdir()) == ["google_oauth.json"]


def test_write_overwrites_existing(store):
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    updated = {"web": {"client_id": "new", "client_secret": "s"}}
    sc.write_service_credentials("google_oauth", updated)
    assert sc.read_service_credentials("google_oauth") == updated


def test_read_missing_returns_none(store):
    assert sc.read_service_credentials("google_oauth") is None


def test_read_malformed_json_returns_none(store):
    store_dir, _ = store
    store_dir.mkdir(parents=True)
    (store_dir / "google_oauth.json").write_text("{not json")
    assert sc.read_service_credentials("google_oauth") is None


def test_read_non_object_returns_none(store):
    store_dir, _ = store
    store_dir.mkdir(parents=True)
    (store_dir / "google_oauth.json").write_text('["a list"]')
    assert sc.read_service_credentials("google_oauth") is None


def test_unknown_service_rejected(store):
    with pytest.raises(ValueError):
        sc.read_service_credentials("nope")
    with pytest.raises(ValueError):
        sc.write_service_credentials("nope", {})
    with pytest.raises(ValueError):
        sc.read_legacy_service_credentials("nope")


# ---------------------------------------------------------------------------
# Legacy reads and migration
# ---------------------------------------------------------------------------

def test_read_legacy_section(store):
    _, legacy_file = store
    legacy_file.write_text(json.dumps({"google_oauth": GOOGLE_CONFIG, "slack": {}}))
    assert sc.read_legacy_service_credentials("google_oauth") == GOOGLE_CONFIG


def test_read_legacy_missing_file_or_section(store):
    _, legacy_file = store
    assert sc.read_legacy_service_credentials("google_oauth") is None
    legacy_file.write_text(json.dumps({"slack": {"bot_token": "x"}}))
    assert sc.read_legacy_service_credentials("google_oauth") is None


def test_migrate_copies_legacy_into_store(store):
    store_dir, legacy_file = store
    legacy_file.write_text(json.dumps({"google_oauth": GOOGLE_CONFIG}))
    assert sc.migrate_legacy_credentials() == ["google_oauth"]
    assert sc.read_service_credentials("google_oauth") == GOOGLE_CONFIG
    file_mode = stat.S_IMODE(os.stat(store_dir / "google_oauth.json").st_mode)
    assert file_mode == 0o600
    # Legacy file untouched
    assert json.loads(legacy_file.read_text()) == {"google_oauth": GOOGLE_CONFIG}


def test_migrate_copies_all_known_sections(store, github_plugin, slack_plugin, twitter_plugin):
    # The github, slack, and twitter plugins' services are in
    # KNOWN_SERVICES after plugin load, and migrate_legacy_credentials()
    # runs in the app lifespan (post-load), so their legacy locations (the
    # github/slack server_credentials.json sections, the standalone
    # twitter_credentials.json) still migrate.
    _, legacy_file = store
    legacy_file.write_text(json.dumps({
        "google_oauth": GOOGLE_CONFIG,
        "slack": SLACK_CONFIG,
        "github": GITHUB_CONFIG,
        "coingecko": COINGECKO_CONFIG,
        "gemini": {"api_key": "not-a-store-service"},
    }))
    sc.LEGACY_TWITTER_CREDENTIALS_FILE.write_text(json.dumps(TWITTER_CONFIG))
    assert sc.migrate_legacy_credentials() == [
        "google_oauth", "coingecko", "github", "slack", "twitter",
    ]
    assert sc.read_service_credentials("slack") == SLACK_CONFIG
    assert sc.read_service_credentials("github") == GITHUB_CONFIG
    assert sc.read_service_credentials("twitter") == TWITTER_CONFIG
    assert sc.read_service_credentials("coingecko") == COINGECKO_CONFIG


def test_read_legacy_twitter_uses_standalone_file(store, twitter_plugin):
    _, legacy_file = store
    # A "twitter" section in server_credentials.json is NOT the legacy
    # location for twitter -- only the standalone file is.
    legacy_file.write_text(json.dumps({"twitter": {"client_id": "wrong"}}))
    assert sc.read_legacy_service_credentials("twitter") is None
    sc.LEGACY_TWITTER_CREDENTIALS_FILE.write_text(json.dumps(TWITTER_CONFIG))
    assert sc.read_legacy_service_credentials("twitter") == TWITTER_CONFIG


def test_migrate_never_overwrites_store(store):
    _, legacy_file = store
    store_config = {"web": {"client_id": "from-store", "client_secret": "s"}}
    sc.write_service_credentials("google_oauth", store_config)
    legacy_file.write_text(json.dumps({"google_oauth": GOOGLE_CONFIG}))
    assert sc.migrate_legacy_credentials() == []
    assert sc.read_service_credentials("google_oauth") == store_config


def test_migrate_with_nothing_to_do(store):
    assert sc.migrate_legacy_credentials() == []


# ---------------------------------------------------------------------------
# Loader precedence (auth/config.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def oauth_loader(store, monkeypatch):
    import auth.config as auth_config
    _, legacy_file = store
    monkeypatch.setattr(auth_config, "SERVER_CREDENTIALS_FILE", legacy_file)
    monkeypatch.setattr(auth_config, "_server_credentials_cache", None)
    return auth_config, legacy_file


def test_load_google_oauth_prefers_store(oauth_loader):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"google_oauth": {"web": {"client_id": "legacy"}}}))
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    assert auth_config.load_google_oauth_config() == GOOGLE_CONFIG


def test_load_google_oauth_falls_back_to_legacy(oauth_loader):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"google_oauth": GOOGLE_CONFIG}))
    assert auth_config.load_google_oauth_config() == GOOGLE_CONFIG


def test_load_google_oauth_unconfigured_raises(oauth_loader):
    auth_config, _ = oauth_loader
    with pytest.raises(HTTPException) as exc_info:
        auth_config.load_google_oauth_config()
    assert exc_info.value.status_code == 500


def test_load_slack_prefers_store(oauth_loader, slack_plugin):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"slack": {"client_id": "legacy"}}))
    sc.write_service_credentials("slack", SLACK_CONFIG)
    assert auth_config.load_slack_client_config() == SLACK_CONFIG
    assert auth_config.load_slack_bot_token() == "xoxb-token"
    assert auth_config.load_slack_socket_mode_token() == "xapp-token"


def test_load_slack_falls_back_to_legacy(oauth_loader):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"slack": SLACK_CONFIG}))
    assert auth_config.load_slack_client_config() == SLACK_CONFIG


def test_load_slack_unconfigured(oauth_loader):
    auth_config, _ = oauth_loader
    with pytest.raises(HTTPException) as exc_info:
        auth_config.load_slack_client_config()
    assert exc_info.value.status_code == 500
    # Socket Mode is optional plumbing: missing credentials mean disabled,
    # not a crash.
    assert auth_config.load_slack_socket_mode_token() is None


def test_load_coingecko_prefers_store(oauth_loader):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"coingecko": {"api_key": "legacy-key"}}))
    sc.write_service_credentials("coingecko", COINGECKO_CONFIG)
    assert auth_config.load_coingecko_api_key() == "cg-store-key"


def test_load_coingecko_falls_back_to_legacy(oauth_loader):
    auth_config, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"coingecko": {"api_key": "legacy-key"}}))
    assert auth_config.load_coingecko_api_key() == "legacy-key"


def test_load_coingecko_unconfigured_raises(oauth_loader):
    auth_config, legacy_file = oauth_loader
    with pytest.raises(HTTPException) as exc_info:
        auth_config.load_coingecko_api_key()
    assert exc_info.value.status_code == 500
    # An empty key in either location is still unconfigured.
    legacy_file.write_text(json.dumps({"coingecko": {"api_key": ""}}))
    with pytest.raises(HTTPException):
        auth_config.load_coingecko_api_key()


def test_load_github_prefers_store(oauth_loader, github_plugin):
    # The github loader lives in the plugin now (plugins/github/upstream.py)
    # but keeps the same store-then-legacy precedence.
    from plugins.github.upstream import load_github_client_config
    _, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"github": {"client_id": "legacy"}}))
    sc.write_service_credentials("github", GITHUB_CONFIG)
    assert load_github_client_config() == GITHUB_CONFIG


def test_load_github_falls_back_to_legacy(oauth_loader, github_plugin):
    from plugins.github.upstream import load_github_client_config
    _, legacy_file = oauth_loader
    legacy_file.write_text(json.dumps({"github": GITHUB_CONFIG}))
    assert load_github_client_config() == GITHUB_CONFIG


def test_load_github_unconfigured_raises(oauth_loader, github_plugin):
    from plugins.github.upstream import load_github_client_config
    with pytest.raises(HTTPException) as exc_info:
        load_github_client_config()
    assert exc_info.value.status_code == 500


def test_load_twitter_prefers_store(oauth_loader, twitter_plugin):
    from plugins.twitter.upstream import load_twitter_client_config
    sc.LEGACY_TWITTER_CREDENTIALS_FILE.write_text(
        json.dumps({"client_id": "legacy"})
    )
    sc.write_service_credentials("twitter", TWITTER_CONFIG)
    assert load_twitter_client_config() == TWITTER_CONFIG


def test_load_twitter_falls_back_to_legacy_file(oauth_loader, twitter_plugin):
    from plugins.twitter.upstream import load_twitter_client_config
    sc.LEGACY_TWITTER_CREDENTIALS_FILE.write_text(json.dumps(TWITTER_CONFIG))
    assert load_twitter_client_config() == TWITTER_CONFIG


def test_load_twitter_unconfigured_raises(oauth_loader, twitter_plugin):
    from plugins.twitter.upstream import load_twitter_client_config
    with pytest.raises(HTTPException) as exc_info:
        load_twitter_client_config()
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Admin endpoints (chat/routes/admin.py)
# ---------------------------------------------------------------------------

ADMIN_USER = {"id": 1, "email": "admin@example.com"}


@pytest.fixture
def admin_routes(store, monkeypatch):
    import chat.routes.admin as admin
    monkeypatch.setattr(admin, "is_admin", lambda email: email == ADMIN_USER["email"])
    return admin


def _run(coro):
    return asyncio.run(coro)


def _put(admin, service, body, user=ADMIN_USER):
    return _run(admin.admin_update_service_credentials(service, body, user=user))


def test_admin_endpoints_require_admin(admin_routes):
    admin = admin_routes
    nobody = {"id": 2, "email": "user@example.com"}
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_get_service_credentials("google_oauth", user=nobody))
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        _put(admin, "google_oauth", {"client_id": "x", "client_secret": "y"}, user=nobody)
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_list_service_credentials(user=nobody))
    assert exc_info.value.status_code == 403


def test_admin_get_unknown_service_404(admin_routes):
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_get_service_credentials("nope", user=ADMIN_USER))
    assert exc_info.value.status_code == 404
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "nope", {})
    assert exc_info.value.status_code == 404


def test_admin_list_returns_schema_for_all_core_services(admin_routes):
    result = _run(admin_routes.admin_list_service_credentials(user=ADMIN_USER))
    services = {row["service"]: row for row in result["services"]}
    assert set(services) >= {
        "google_oauth", "ramp", "coingecko",
    }
    assert "github" not in services  # plugin-registered, not core
    assert "slack" not in services  # plugin-registered, not core
    assert "twitter" not in services  # plugin-registered, not core
    assert "telegram" not in services  # plugin-registered, not core
    for row in services.values():
        assert row["configured"] is False
        assert isinstance(row["fields"], list) and row["fields"]
        assert isinstance(row["credentials"], dict)
        for field in row["fields"]:
            assert set(field) == {
                "key", "label", "type", "placeholder", "help",
                "required", "required_if", "visible_if", "default",
            }


def test_admin_get_masks_secret(admin_routes):
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    detail = _run(
        admin_routes.admin_get_service_credentials("google_oauth", user=ADMIN_USER)
    )
    assert detail["configured"] is True
    assert detail["source"] == "store"
    creds = detail["credentials"]
    assert creds["client_id"] == "abc.apps.googleusercontent.com"
    assert creds["client_secret_set"] is True
    assert "client_secret" not in creds
    # The redirect_uris list flattens to a newline-joined textarea value.
    assert creds["redirect_uris"] == "https://example.com/auth/callback"
    assert "topsecret" not in json.dumps(detail)


def test_admin_get_reports_legacy_source(admin_routes, store):
    _, legacy_file = store
    legacy_file.write_text(json.dumps({"google_oauth": GOOGLE_CONFIG}))
    detail = _run(
        admin_routes.admin_get_service_credentials("google_oauth", user=ADMIN_USER)
    )
    assert detail["configured"] is True
    assert detail["source"] == "legacy"


def test_admin_put_writes_store(admin_routes):
    detail = _put(admin_routes, "google_oauth", {
        "client_id": "new-id",
        "client_secret": "new-secret",
        "project_id": "proj",
        "redirect_uris": "https://a.example/cb\n  \nhttps://b.example/cb",
    })
    assert detail["source"] == "store"
    stored = sc.read_service_credentials("google_oauth")
    web = stored["web"]
    assert web["client_id"] == "new-id"
    assert web["client_secret"] == "new-secret"
    assert web["project_id"] == "proj"
    assert web["redirect_uris"] == ["https://a.example/cb", "https://b.example/cb"]
    # Standard Google endpoints filled in
    assert web["auth_uri"] == "https://accounts.google.com/o/oauth2/auth"
    assert web["token_uri"] == "https://oauth2.googleapis.com/token"


def test_admin_put_empty_secret_keeps_stored_one(admin_routes):
    sc.write_service_credentials("google_oauth", GOOGLE_CONFIG)
    _put(admin_routes, "google_oauth", {
        "client_id": "rotated-id", "client_secret": "",
    })
    web = sc.read_service_credentials("google_oauth")["web"]
    assert web["client_id"] == "rotated-id"
    assert web["client_secret"] == "topsecret"


def test_admin_put_requires_client_id_and_some_secret(admin_routes):
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "google_oauth", {"client_id": "  ", "client_secret": "s"})
    assert exc_info.value.status_code == 400

    # No stored secret and none provided -> 400
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "google_oauth", {"client_id": "x", "client_secret": ""})
    assert exc_info.value.status_code == 400


def test_admin_put_coingecko_writes_store_and_reports_configured(admin_routes):
    result = _put(admin_routes, "coingecko", {"api_key": "  cg-new-key  "})
    assert result["configured"] is True
    assert result["credentials"] == {"api_key_set": True}
    assert sc.read_service_credentials("coingecko") == {"api_key": "cg-new-key"}
    # Empty secret keeps the stored one; nothing stored + empty is rejected.
    _put(admin_routes, "coingecko", {"api_key": ""})
    assert sc.read_service_credentials("coingecko") == {"api_key": "cg-new-key"}


def test_admin_put_coingecko_requires_key_when_none_stored(admin_routes):
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "coingecko", {"api_key": ""})
    assert exc_info.value.status_code == 400
    detail = _run(
        admin_routes.admin_get_service_credentials("coingecko", user=ADMIN_USER)
    )
    assert detail["configured"] is False
    assert detail["credentials"] == {"api_key_set": False}


def test_admin_put_rejects_unknown_fields(admin_routes, twitter_plugin):
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "twitter", {
            "client_id": "id", "client_secret": "s", "bogus": "nope",
        })
    assert exc_info.value.status_code == 400
    assert "bogus" in exc_info.value.detail["message"]


def test_admin_get_masks_slack_tokens(admin_routes, slack_plugin):
    sc.write_service_credentials("slack", SLACK_CONFIG)
    detail = _run(
        admin_routes.admin_get_service_credentials("slack", user=ADMIN_USER)
    )
    creds = detail["credentials"]
    assert creds == {
        "client_id": "slack-id",
        "client_secret_set": True,
        "bot_token_set": True,
        "socket_mode_token_set": True,
    }
    dumped = json.dumps(detail)
    assert "slack-secret" not in dumped
    assert "xoxb-token" not in dumped
    assert "xapp-token" not in dumped


def test_admin_get_flat_service_view(admin_routes, twitter_plugin):
    sc.write_service_credentials("twitter", TWITTER_CONFIG)
    detail = _run(
        admin_routes.admin_get_service_credentials("twitter", user=ADMIN_USER)
    )
    assert detail["credentials"] == {"client_id": "tw-id", "client_secret_set": True}
    assert "tw-secret" not in json.dumps(detail)


def test_admin_put_slack_writes_store_and_keeps_empty_secrets(admin_routes, slack_plugin):
    sc.write_service_credentials("slack", {**SLACK_CONFIG, "extra": "kept"})
    detail = _put(admin_routes, "slack", {
        "client_id": "new-id",
        "client_secret": "",
        "bot_token": "",
        "socket_mode_token": "xapp-rotated",
    })
    assert detail["source"] == "store"
    stored = sc.read_service_credentials("slack")
    assert stored == {
        "client_id": "new-id",
        "client_secret": "slack-secret",  # empty kept the stored one
        "bot_token": "xoxb-token",  # empty kept the stored one
        "socket_mode_token": "xapp-rotated",
        "extra": "kept",
    }


def test_admin_put_slack_optional_tokens_stay_absent(admin_routes, slack_plugin):
    _put(admin_routes, "slack", {"client_id": "id", "client_secret": "s"})
    assert sc.read_service_credentials("slack") == {
        "client_id": "id",
        "client_secret": "s",
    }


def test_admin_put_slack_requires_secret_when_none_stored(admin_routes, slack_plugin):
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "slack", {"client_id": "id", "client_secret": ""})
    assert exc_info.value.status_code == 400


def test_admin_put_github_and_twitter(admin_routes, github_plugin, twitter_plugin):
    # github's and twitter's cards are plugin-registered but save through
    # the same generic PUT endpoint as the core specs.
    detail = _put(admin_routes, "github", {
        "client_id": "gh-id", "client_secret": "gh-secret",
    })
    assert detail["source"] == "store"
    assert sc.read_service_credentials("github") == GITHUB_CONFIG

    detail = _put(admin_routes, "twitter", {
        "client_id": "tw-id", "client_secret": "tw-secret",
    })
    assert detail["source"] == "store"
    assert sc.read_service_credentials("twitter") == TWITTER_CONFIG


def test_admin_put_twitter_empty_secret_keeps_legacy_one(
        admin_routes, store, twitter_plugin):
    sc.LEGACY_TWITTER_CREDENTIALS_FILE.write_text(json.dumps(TWITTER_CONFIG))
    detail = _put(admin_routes, "twitter", {
        "client_id": "rotated-id", "client_secret": "",
    })
    assert detail["source"] == "store"
    assert sc.read_service_credentials("twitter") == {
        "client_id": "rotated-id",
        "client_secret": "tw-secret",
    }


def test_admin_put_migrates_legacy_and_preserves_extra_keys(admin_routes, store):
    _, legacy_file = store
    legacy = {
        "google_oauth": {
            "web": {
                **GOOGLE_CONFIG["web"],
                "custom_key": "kept",
            }
        }
    }
    legacy_file.write_text(json.dumps(legacy))
    detail = _put(admin_routes, "google_oauth", {
        "client_id": "from-ui", "client_secret": "", "redirect_uris": "https://x/cb",
    })
    assert detail["source"] == "store"
    web = sc.read_service_credentials("google_oauth")["web"]
    assert web["client_id"] == "from-ui"
    assert web["client_secret"] == "topsecret"  # kept from legacy
    assert web["custom_key"] == "kept"


# ---------------------------------------------------------------------------
# Plugin credential services (config/service_specs.py registration)
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin_specs(store, monkeypatch):
    """Isolate the plugin spec registry and the KNOWN_SERVICES roster."""
    import config.service_specs as specs
    monkeypatch.setattr(specs, "_PLUGIN_SPECS", {})
    monkeypatch.setattr(sc, "KNOWN_SERVICES", sc.KNOWN_SERVICES)
    return specs


def _acme_plugin():
    from config.plugin_types import CredentialField, QuestPlugin
    return QuestPlugin(
        id="acme",
        label="Acme Tracker",
        credential_schema=(
            CredentialField(key="enabled", label="Enabled", type="bool"),
            CredentialField(
                key="api_token", label="API Token", type="secret",
                required_if="enabled",
            ),
        ),
        is_configured=lambda creds: bool(creds.get("enabled")),
    )


def test_plugin_credentials_register_and_round_trip(plugin_specs, admin_routes):
    plugin_specs.register_plugin_credentials(_acme_plugin())
    assert "acme" in sc.KNOWN_SERVICES

    result = _run(admin_routes.admin_list_service_credentials(user=ADMIN_USER))
    row = next(r for r in result["services"] if r["service"] == "acme")
    assert row["label"] == "Acme Tracker"
    assert row["configured"] is False

    detail = _put(admin_routes, "acme", {"enabled": True, "api_token": "tok"})
    assert detail["configured"] is True
    assert detail["credentials"] == {"enabled": True, "api_token_set": True}
    assert sc.read_service_credentials("acme") == {
        "enabled": True, "api_token": "tok",
    }

    # required_if: enabling without a token (and none stored) is rejected.
    sc.service_credentials_path("acme").unlink()
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "acme", {"enabled": True, "api_token": ""})
    assert exc_info.value.status_code == 400


def test_plugin_credentials_id_collision_rejected(plugin_specs):
    from config.plugin_types import CredentialField, QuestPlugin
    plugin = QuestPlugin(
        id="ramp", label="Fake Ramp",
        credential_schema=(CredentialField(key="k", label="K", type="text"),),
    )
    with pytest.raises(ValueError):
        plugin_specs.register_plugin_credentials(plugin)


# ---------------------------------------------------------------------------
# list fields (entries with per-entry sub-forms)
# ---------------------------------------------------------------------------

def _relay_plugin(**overrides):
    """A plugin whose card is a list of mail relays plus a flat field."""
    from config.plugin_types import CredentialField, QuestPlugin
    relay_fields = (
        CredentialField(key="host", label="Host", type="text", required=True),
        CredentialField(
            key="mode", label="Mode", type="select", options=("tls", "starttls"),
            default="tls", required=True,
        ),
        CredentialField(key="port", label="Port", type="text"),
        CredentialField(key="auth", label="Authenticated", type="bool"),
        CredentialField(
            key="user", label="User", type="text", required_if="auth", visible_if="auth",
        ),
    )
    kwargs = dict(
        id="relay",
        label="Mail relays",
        credential_schema=(
            CredentialField(key="label", label="Label", type="text", default="Relays"),
            CredentialField(
                key="relays", label="Relays", type="list", required=True,
                item_label="relay", max_items=3, item_fields=relay_fields,
                help="Where mail goes.",
            ),
        ),
        is_configured=lambda creds: bool(creds.get("relays")),
    )
    kwargs.update(overrides)
    return QuestPlugin(**kwargs)


def test_list_field_schema_view(plugin_specs, admin_routes):
    plugin_specs.register_plugin_credentials(_relay_plugin())
    result = _run(admin_routes.admin_get_service_credentials("relay", user=ADMIN_USER))
    label_field, relays = result["fields"]
    assert label_field["default"] == "Relays"
    assert relays["type"] == "list"
    assert relays["item_label"] == "relay"
    assert relays["max_items"] == 3
    assert relays["help"] == "Where mail goes."
    assert [f["key"] for f in relays["item_fields"]] == ["host", "mode", "port", "auth", "user"]
    mode = relays["item_fields"][1]
    assert mode["options"] == ["tls", "starttls"] and mode["default"] == "tls"
    assert relays["item_fields"][4]["required_if"] == "auth"
    # Nothing stored: the list reads as no entries.
    assert result["credentials"] == {"label": "", "relays": []}
    assert result["configured"] is False


def test_list_field_round_trip(plugin_specs, admin_routes):
    plugin_specs.register_plugin_credentials(_relay_plugin())
    detail = _put(admin_routes, "relay", {
        "label": "",
        "relays": [
            {"host": " a.example ", "mode": "tls", "port": "", "auth": False},
            {"host": "b.example", "mode": "starttls", "port": "587", "auth": True, "user": "u"},
        ],
    })
    assert detail["configured"] is True
    assert detail["credentials"]["relays"] == [
        {"host": "a.example", "mode": "tls", "port": "", "auth": False, "user": ""},
        {"host": "b.example", "mode": "starttls", "port": "587", "auth": True, "user": "u"},
    ]
    assert sc.read_service_credentials("relay")["relays"][1]["user"] == "u"

    # A later save replaces the whole list (entries removed client-side go).
    detail = _put(admin_routes, "relay", {"relays": [{"host": "c.example", "mode": "tls"}]})
    assert [e["host"] for e in detail["credentials"]["relays"]] == ["c.example"]


@pytest.mark.parametrize("body, message", [
    ({"relays": []}, "Relays: add at least one relay."),
    ({}, "Relays: add at least one relay."),
    ({"relays": "a.example"}, "Relays must be a list of entries."),
    ({"relays": ["a.example"]}, "Relays, entry 1: Expected a JSON object of fields."),
    ({"relays": [{"host": "a", "mode": "tls"}, {"mode": "tls"}]}, "Relays, entry 2: Host is required."),
    ({"relays": [{"host": "a", "mode": "plain"}]}, "Relays, entry 1: Mode must be one of: tls, starttls."),
    ({"relays": [{"host": "a", "mode": "tls", "extra": 1}]}, "Relays, entry 1: Unknown field(s): extra"),
    ({"relays": [{"host": "a", "mode": "tls", "port": 25}]}, "Relays, entry 1: Port must be a string."),
    ({"relays": [{"host": "a", "mode": "tls", "auth": True}]}, "Relays, entry 1: User is required."),
    ({"relays": [{"host": h, "mode": "tls"} for h in "abcd"]}, "Relays: at most 3 relays allowed."),
])
def test_list_field_rejections(plugin_specs, admin_routes, body, message):
    plugin_specs.register_plugin_credentials(_relay_plugin())
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "relay", body)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["message"] == message


def test_list_field_validate_hook_sees_entries(plugin_specs, admin_routes):
    seen = {}

    def validate(values):
        seen.update(values)
        if len({e["host"] for e in values["relays"]}) < len(values["relays"]):
            raise ValueError("Relays, entry 2: duplicate host")
        return {**values, "relays": [{**e, "host": e["host"].upper()} for e in values["relays"]]}

    plugin_specs.register_plugin_credentials(_relay_plugin(credential_validate=validate))
    detail = _put(admin_routes, "relay", {"relays": [{"host": "a", "mode": "tls"}]})
    assert seen["relays"] == [{"host": "a", "mode": "tls", "port": "", "auth": False, "user": ""}]
    assert detail["credentials"]["relays"][0]["host"] == "A"
    with pytest.raises(HTTPException) as exc_info:
        _put(admin_routes, "relay", {"relays": [{"host": "a", "mode": "tls"}, {"host": "a", "mode": "tls"}]})
    assert exc_info.value.detail["message"] == "Relays, entry 2: duplicate host"


def test_list_field_flatten_hook_projects_legacy_shape(plugin_specs, admin_routes):
    def flatten(stored):
        text = stored.get("relays")
        if isinstance(text, str):
            return {**stored, "relays": [{"host": h, "mode": "tls"} for h in text.split()]}
        return stored

    plugin_specs.register_plugin_credentials(_relay_plugin(credential_flatten=flatten))
    sc.write_service_credentials("relay", {"relays": "a.example b.example"})
    result = _run(admin_routes.admin_get_service_credentials("relay", user=ADMIN_USER))
    assert result["credentials"]["relays"] == [
        {"host": "a.example", "mode": "tls", "port": "", "auth": False, "user": ""},
        {"host": "b.example", "mode": "tls", "port": "", "auth": False, "user": ""},
    ]
    # A non-list stored value without a flatten hook reads as no entries.
    plain = _relay_plugin()
    spec = plugin_specs.ServiceSpec(
        service="relay2", label=plain.label, fields=plain.credential_schema,
    )
    assert plugin_specs.form_view(spec, {"relays": "a.example b.example"}) == {
        "label": "", "relays": [],
    }


@pytest.mark.parametrize("field_kwargs, message", [
    (dict(type="list", item_label="r"), "needs item_fields"),
    (dict(type="list", max_items=-1, item_fields=(("host", "text"),)), "max_items"),
    (dict(type="list", item_fields=(("key", "secret"),)), "entries allow only"),
    (dict(type="list", item_fields=(("inner", "list"),)), "entries allow only"),
    (dict(type="list", item_fields=(("a", "text"), ("a", "text"))), "duplicate relays entry field keys"),
    (dict(type="list", item_fields=(("m", "select"),)), "relays entry field 'm' needs options"),
    (dict(type="text", item_label="x"), "sets list options but is not a list field"),
    (dict(type="bool", default="yes"), "default is only for text, textarea and select"),
    (dict(type="list", item_fields=(("m", "select", dict(options=("a",), default="b")),)),
     "relays entry field 'm' default must be one of its options"),
    (dict(type="list", item_fields=(("m", "text"), ("n", "text", dict(visible_if="m")))),
     "visible_if='m' must name a bool field"),
])
def test_list_field_schema_rejections(plugin_specs, field_kwargs, message):
    from config.plugin_types import CredentialField, QuestPlugin
    kwargs = dict(field_kwargs)
    if "item_fields" in kwargs:
        kwargs["item_fields"] = tuple(
            CredentialField(key=item[0], label=item[0], type=item[1], **(item[2] if len(item) > 2 else {}))
            for item in kwargs["item_fields"]
        )
    plugin = QuestPlugin(
        id="relay", label="Relays",
        credential_schema=(CredentialField(key="relays", label="Relays", **kwargs),),
    )
    with pytest.raises(ValueError, match=message):
        plugin_specs.register_plugin_credentials(plugin)
    assert "relay" not in sc.KNOWN_SERVICES


def test_list_field_refused_in_user_connection_forms():
    """Per-user form connections render no list fields (no editor there)."""
    from config.plugin_types import CredentialField
    from config.service_specs import _validate_fields
    fields = (CredentialField(
        key="hosts", label="Hosts", type="list",
        item_fields=(CredentialField(key="h", label="H", type="text"),),
    ),)
    with pytest.raises(ValueError, match="unknown type 'list'"):
        _validate_fields("x", fields, allow_select=True)
    _validate_fields("x", fields, allow_list=True)
