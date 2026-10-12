"""The IMAP plugin loads through the real loader; connectors row gating."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import chat.routes.user as user_routes
from config import plugins as plugins_mod
from config.service_specs import get_service_spec, resolve_update

PLUGIN_PY = Path(__file__).resolve().parent.parent / "plugin.py"


def test_manifest_passes_loader_validation():
    plugin = plugins_mod._import_plugin_module(PLUGIN_PY).get_plugin()
    plugins_mod.validate_plugin(plugin, set())
    spec = plugin.user_connection
    assert plugin.id == "imap" and plugin.label == "IMAP mailbox"
    assert spec.kind == "form" and spec.multi_connection and spec.connection_limit == 10
    assert [f.key for f in spec.fields] == ["host", "port", "security", "username", "password"]
    assert next(f for f in spec.fields if f.key == "security").options == ("ssl", "starttls")
    assert next(f for f in spec.fields if f.key == "password").type == "secret"


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
    host_field = next(f for f in row["form_fields"] if f["key"] == "host")
    assert host_field["options"] == []

    imap_admin_hosts.set("  \n")
    assert _connectors(user, imap_plugin)["available"] is False
    imap_admin_hosts.set("http://bad")
    assert _connectors(user, imap_plugin)["available"] is False

    imap_admin_hosts.set("imap.example.com\nmail.example.org:993")
    row = _connectors(user, imap_plugin)
    assert row["available"] is True
    fields = {f["key"]: f for f in row["form_fields"]}
    assert fields["host"]["options"] == ["imap.example.com", "mail.example.org"]
    assert fields["security"]["options"] == ["ssl", "starttls"]
    assert fields["port"]["placeholder"] == "993"


def test_connectors_row_lists_connections_with_warning(imap_plugin, fake_imap):
    rows = [
        fake_imap.row(row_id=1, is_default=True),
        fake_imap.row(row_id=2, is_default=False, host="gone.example.com",
                      username="x@gone.example.com"),
    ]
    row = _connectors(fake_imap.user(rows), imap_plugin)
    assert row["connected"] is True
    assert [(c["account"], c["warning"]) for c in row["connections"]] == [
        ("alice@example.com", None),
        ("x@gone.example.com", "Server no longer allowed"),
    ]
    assert "s3cret-pass" not in repr(row)


def test_admin_card_save_normalises_and_rejects(imap_plugin):
    spec = get_service_spec("imap")
    assert spec is not None
    stored = resolve_update(spec, {"allowed_hosts": "IMAP.Example.com\n\nimap.example.com"}, None)
    assert stored["allowed_hosts"] == "imap.example.com"
    with pytest.raises(ValueError, match="Line 1"):
        resolve_update(spec, {"allowed_hosts": "imaps://x"}, None)
    with pytest.raises(ValueError):
        resolve_update(spec, {"allowed_hosts": ""}, None)
