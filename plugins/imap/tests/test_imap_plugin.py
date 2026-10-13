"""The IMAP plugin loads through the real loader; connectors row gating."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import chat.routes.user as user_routes
from config import plugins as plugins_mod
from config.service_specs import fields_view, get_service_spec, resolve_update

PLUGIN_PY = Path(__file__).resolve().parent.parent / "plugin.py"


def test_manifest_passes_loader_validation():
    plugin = plugins_mod._import_plugin_module(PLUGIN_PY).get_plugin()
    plugins_mod.validate_plugin(plugin, set())
    spec = plugin.user_connection
    assert plugin.id == "imap" and plugin.label == "IMAP mailbox"
    assert spec.kind == "form" and spec.multi_connection and spec.connection_limit == 10
    assert [(f.key, f.type, f.required) for f in spec.fields] == [
        ("server", "select", True),
        ("username", "text", True),
        ("password", "secret", True),
    ]


def _connectors(user, plugin):
    with patch.object(plugins_mod, "_LOADED", [plugin]), \
            patch("auth.config.load_google_oauth_config", side_effect=RuntimeError):
        rows = asyncio.run(user_routes.get_connectors(user=user))["connectors"]
    return {r["service"]: r for r in rows}["imap"]


def test_connectors_row_unavailable_until_admin_lists_a_host(imap_plugin, imap_admin_hosts):
    user = {"email": "u@example.com"}
    imap_admin_hosts.clear()
    row = _connectors(user, imap_plugin)
    assert row["available"] is False
    assert row["kind"] == "form" and row["multi_connection"] is True
    server_field = next(f for f in row["form_fields"] if f["key"] == "server")
    assert server_field["options"] == []

    imap_admin_hosts.set("  \n")
    assert _connectors(user, imap_plugin)["available"] is False
    imap_admin_hosts.set("http://bad")
    assert _connectors(user, imap_plugin)["available"] is False

    imap_admin_hosts.set("imap.example.com\nmail.example.org:143\nmail.example.org:993")
    row = _connectors(user, imap_plugin)
    assert row["available"] is True
    assert [f["key"] for f in row["form_fields"]] == ["server", "username", "password"]
    fields = {f["key"]: f for f in row["form_fields"]}
    assert fields["server"]["type"] == "select" and fields["server"]["required"] is True
    assert fields["server"]["options"] == [
        "imap.example.com:993 ssl", "mail.example.org:143 starttls", "mail.example.org:993 ssl",
    ]
    assert fields["password"]["type"] == "secret"


def test_connectors_row_lists_connections_with_warning(imap_plugin, fake_imap):
    rows = [
        fake_imap.row(row_id=1, is_default=True),
        fake_imap.row(row_id=2, is_default=False, server="gone.example.com:993 ssl",
                      username="x@gone.example.com"),
        fake_imap.row(row_id=3, is_default=False, username="old@example.com", values={
            "host": "imap.example.com", "port": "", "security": "ssl",
            "username": "old@example.com", "password": "s3cret-pass",
        }),
    ]
    row = _connectors(fake_imap.user(rows), imap_plugin)
    assert row["connected"] is True
    assert [(c["account"], c["warning"]) for c in row["connections"]] == [
        ("alice@example.com", None),
        ("x@gone.example.com", "Server no longer allowed"),
        ("old@example.com", "Server no longer allowed"),
    ]
    assert "s3cret-pass" not in repr(row)


def test_admin_card_save_normalises_and_rejects(imap_plugin):
    spec = get_service_spec("imap")
    assert spec is not None
    # The server list is a list field of host / security / port entries.
    (hosts_field,) = [f for f in fields_view(spec) if f["key"] == "allowed_hosts"]
    assert hosts_field["type"] == "list"
    assert hosts_field["label"] == "IMAP servers"
    assert hosts_field["item_label"] == "server"
    assert hosts_field["required"] is True
    assert "removing a server disables" in hosts_field["help"]
    items = {f["key"]: f for f in hosts_field["item_fields"]}
    assert list(items) == ["host", "security", "port"]
    assert items["host"]["required"] is True
    assert items["security"] == {
        **items["security"], "type": "select", "options": ["ssl", "starttls"],
        "default": "ssl", "required": True,
    }
    assert items["port"]["required"] is False
    assert "993" in items["port"]["placeholder"] and "143" in items["port"]["placeholder"]

    stored = resolve_update(spec, {"allowed_hosts": [
        {"host": "IMAP.Example.com", "security": "ssl", "port": ""},
        {"host": "imap.example.com", "security": "starttls", "port": "143"},
        {"host": "::1", "security": "starttls", "port": ""},
    ]}, None)
    assert stored["allowed_hosts"] == [
        {"host": "imap.example.com", "port": "993", "security": "ssl"},
        {"host": "imap.example.com", "port": "143", "security": "starttls"},
        {"host": "::1", "port": "143", "security": "starttls"},
    ]
    # The generic entry rules run first (required host, select choices)...
    with pytest.raises(ValueError, match="IMAP servers, entry 1: Host is required"):
        resolve_update(spec, {"allowed_hosts": [{"host": "", "security": "ssl"}]}, None)
    with pytest.raises(ValueError, match="entry 1: Security must be one of: ssl, starttls"):
        resolve_update(spec, {"allowed_hosts": [{"host": "x", "security": "plain"}]}, None)
    # ...then the plugin's own validation.
    with pytest.raises(ValueError, match="Entry 1: Host: no scheme"):
        resolve_update(spec, {"allowed_hosts": [{"host": "imaps://x", "security": "ssl"}]}, None)
    with pytest.raises(ValueError, match="Entry 2: duplicate of entry 1"):
        resolve_update(spec, {"allowed_hosts": [
            {"host": "x.example", "security": "ssl"},
            {"host": "x.example", "security": "ssl", "port": "993"},
        ]}, None)
    with pytest.raises(ValueError, match="add at least one server"):
        resolve_update(spec, {"allowed_hosts": []}, None)


def test_plugin_post_load_migrates_legacy_text_card(imap_plugin, tmp_path, monkeypatch):
    """The manifest's post_load hook rewrites a pre-list-field text card
    into entries, so the card reads configured and the per-user select
    offers the lines."""
    from config import service_credentials as sc
    from config.service_specs import form_view
    monkeypatch.setattr(sc, "SERVICE_CREDENTIALS_DIR", tmp_path / "service_credentials")
    sc.write_service_credentials("imap", {"allowed_hosts": "imap.example.com\nmail.example.org:143"})
    spec = get_service_spec("imap")
    assert spec.is_configured(sc.read_service_credentials("imap")) is False
    assert form_view(spec, sc.read_service_credentials("imap")) == {"allowed_hosts": []}

    assert imap_plugin.post_load is not None
    imap_plugin.post_load()
    stored = sc.read_service_credentials("imap")
    assert stored == {"allowed_hosts": [
        {"host": "imap.example.com", "port": "993", "security": "ssl"},
        {"host": "mail.example.org", "port": "143", "security": "starttls"},
    ]}
    assert spec.is_configured(stored) is True
    assert form_view(spec, stored) == stored
    assert imap_field_options_for_test() == ["imap.example.com:993 ssl", "mail.example.org:143 starttls"]


def imap_field_options_for_test():
    from plugins.imap.settings import imap_field_options
    return imap_field_options()["server"]
