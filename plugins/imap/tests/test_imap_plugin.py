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
    # The server-list field explains the host[:port] [ssl|starttls] format.
    (hosts_field,) = [f for f in fields_view(spec) if f["key"] == "allowed_hosts"]
    assert hosts_field["type"] == "textarea"
    assert hosts_field["label"] == "IMAP servers"
    assert "host[:port] [ssl|starttls]" in hosts_field["help"]
    assert "993" in hosts_field["help"] and "143 starttls" in hosts_field["help"]
    stored = resolve_update(
        spec, {"allowed_hosts": "IMAP.Example.com\n\nimap.example.com:143\n[::1] StartTLS"}, None,
    )
    assert stored["allowed_hosts"] == (
        "imap.example.com:993 ssl\nimap.example.com:143 starttls\n[::1]:143 starttls"
    )
    with pytest.raises(ValueError, match="Line 1"):
        resolve_update(spec, {"allowed_hosts": "imaps://x"}, None)
    with pytest.raises(ValueError, match="Line 2: duplicate of line 1"):
        resolve_update(spec, {"allowed_hosts": "x.example\nx.example:993 ssl"}, None)
    with pytest.raises(ValueError):
        resolve_update(spec, {"allowed_hosts": ""}, None)
