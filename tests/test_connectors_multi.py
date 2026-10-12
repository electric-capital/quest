"""Multi-connection rows in GET /connectors and connected_services
(issue #84 spec 5 last paragraph and 8.1).

Uses the test-only ``multiconn`` form-kind plugin plus in-test plugins;
user dicts are hand-built in the db/user_store.py shape.
"""

import asyncio
import dataclasses
from unittest.mock import patch

import chat.routes.user as user_routes
from api.instructions import get_user_connected_services
from config import plugins as plugins_mod
from config.plugin_types import QuestPlugin, UserConnectionSpec

SECRET = "hunter2-very-secret"


def _row(row_id, account, *, is_default=False, host="mail.example.com", **extra):
    return {
        "id": row_id, "service": "multiconn", "account": account,
        "description": extra.pop("description", None),
        "is_default": is_default, "secret": None,
        "oauth_blob": {"values": {"host": host, "username": account, "password": SECRET}},
        "created_at": f"2026-10-0{row_id}T00:00:00", "updated_at": None,
        **extra,
    }


def _user(rows, service="multiconn"):
    user = {"email": "u@example.com"}
    if rows:
        default = next(r for r in rows if r.get("is_default"))
        user["service_credentials"] = {service: default}
        user["service_connections"] = {service: rows}
    return user


def _connectors(user, loaded):
    with patch.object(plugins_mod, "_LOADED", list(loaded)), \
            patch("config.service_credentials.read_service_credentials", return_value=None), \
            patch("auth.config.load_google_oauth_config", side_effect=RuntimeError):
        rows = asyncio.run(user_routes.get_connectors(user=user))["connectors"]
    return {r["service"]: r for r in rows}


def test_form_row_shape(multiconn_plugin):
    rows = [
        _row(2, "b@x.com", description="second"),
        _row(1, "a@x.com", is_default=True, host="gone.example.com"),
    ]
    row = _connectors(_user(rows), [multiconn_plugin])["multiconn"]
    assert row["kind"] == "form"
    assert row["label"] == "Multi Connection Fixture"
    assert row["available"] is True
    assert row["connected"] is True
    assert row["multi_connection"] is True
    assert row["connection_limit"] == 3
    assert row["connections_url"] == "/auth/connections/multiconn"
    # Default first; per-connection warning from row_warning.
    assert row["connections"] == [
        {"id": 1, "account": "a@x.com", "description": None, "is_default": True,
         "needs_reauth": False, "warning": "Server no longer allowed",
         "created_at": "2026-10-01T00:00:00"},
        {"id": 2, "account": "b@x.com", "description": "second", "is_default": False,
         "needs_reauth": False, "warning": None,
         "created_at": "2026-10-02T00:00:00"},
    ]
    assert row["form_fields"] == [
        {"key": "host", "label": "Server", "type": "select", "required": True,
         "placeholder": "", "options": ["mail.example.com", "mail.example.org"]},
        {"key": "port", "label": "Port", "type": "text", "required": False,
         "placeholder": "993", "options": []},
        {"key": "username", "label": "Username", "type": "text", "required": True,
         "placeholder": "", "options": []},
        {"key": "password", "label": "Password", "type": "secret", "required": True,
         "placeholder": "", "options": []},
    ]
    # Never any credential material, and no single-connection fields.
    assert SECRET not in repr(row)
    assert "oauth_blob" not in repr(row)
    for key in ("connect_url", "key_url", "key_hint", "disconnect_url"):
        assert key not in row


def test_form_row_not_connected(multiconn_plugin):
    row = _connectors(_user([]), [multiconn_plugin])["multiconn"]
    assert row["connected"] is False
    assert row["connections"] == []
    assert row["form_fields"]


def test_hook_exceptions_are_contained(multiconn_plugin):
    def _boom(*_a):
        raise RuntimeError("boom")

    spec = dataclasses.replace(
        multiconn_plugin.user_connection,
        field_options=_boom, row_warning=_boom, needs_reauth=_boom,
    )
    plugin = dataclasses.replace(multiconn_plugin, user_connection=spec)
    row = _connectors(_user([_row(1, "a@x.com", is_default=True)]), [plugin])["multiconn"]
    assert row["connections"][0]["warning"] is None
    assert row["connections"][0]["needs_reauth"] is False
    assert row["form_fields"][0]["options"] == []


def test_needs_reauth_per_connection(multiconn_plugin):
    spec = dataclasses.replace(
        multiconn_plugin.user_connection,
        needs_reauth=lambda row: row["account"].startswith("b"),
    )
    plugin = dataclasses.replace(multiconn_plugin, user_connection=spec)
    rows = [_row(1, "a@x.com", is_default=True), _row(2, "b@x.com")]
    conns = _connectors(_user(rows), [plugin])["multiconn"]["connections"]
    assert [c["needs_reauth"] for c in conns] == [False, True]


def _api_key_plugin(multi):
    return QuestPlugin(
        id="keysvc", label="Key Service",
        user_connection=UserConnectionSpec(
            kind="api_key",
            connected=lambda row: bool(row.get("secret")),
            multi_connection=multi,
            account_from=(lambda row: "acct") if multi else None,
        ),
    )


def test_single_connection_api_key_row_unchanged():
    row = {"id": 1, "service": "keysvc", "account": "", "is_default": True,
           "secret": "sk-abcd1234", "oauth_blob": None}
    user = {"email": "u@example.com", "service_credentials": {"keysvc": row},
            "service_connections": {"keysvc": [row]}}
    got = _connectors(user, [_api_key_plugin(False)])["keysvc"]
    assert got == {
        "service": "keysvc", "label": "Key Service", "kind": "api_key",
        "connected": True, "available": True, "key_hint": "1234",
        "key_url": "/auth/service-key/keysvc", "key_field": "api_key",
        "key_placeholder": "Paste Key Service API key",
        "disconnect_url": "/auth/service-key/keysvc/remove",
    }


def test_multi_api_key_row_drops_service_key_fields_and_adds_connections():
    rows = [
        {"id": 1, "service": "keysvc", "account": "a", "is_default": True,
         "secret": None, "oauth_blob": None, "created_at": None},
        {"id": 2, "service": "keysvc", "account": "b", "is_default": False,
         "secret": "sk-zzzz9999", "oauth_blob": None, "created_at": None},
    ]
    user = {"email": "u@example.com", "service_credentials": {"keysvc": rows[0]},
            "service_connections": {"keysvc": rows}}
    got = _connectors(user, [_api_key_plugin(True)])["keysvc"]
    # No service-wide key entry or Disconnect for a multi-connection
    # service (spec 11.6): keys are added per connection via connections_url.
    for key in ("key_url", "key_field", "key_placeholder", "key_hint", "disconnect_url"):
        assert key not in got
    assert got["connections_url"] == "/auth/connections/keysvc"
    assert got["kind"] == "api_key"
    # The default row has no secret, but the second connection is connected.
    assert got["connected"] is True
    assert got["multi_connection"] is True
    assert [c["account"] for c in got["connections"]] == ["a", "b"]
    assert "form_fields" not in got
    assert "sk-zzzz9999" not in repr(got["connections"])


# ---------------------------------------------------------------------------
# connected_services
# ---------------------------------------------------------------------------


def _connected(user, loaded):
    with patch.object(plugins_mod, "_LOADED", list(loaded)):
        return get_user_connected_services(user)


def test_connected_services_any_row(multiconn_plugin):
    dead = _row(1, "a@x.com", is_default=True)
    dead["oauth_blob"] = {}
    live = _row(2, "b@x.com")
    assert _connected(_user([dead, live]), [multiconn_plugin])["multiconn"] is True
    assert _connected(_user([dead]), [multiconn_plugin])["multiconn"] is False
    assert _connected(_user([]), [multiconn_plugin])["multiconn"] is False


def test_connected_services_falls_back_to_default_row(multiconn_plugin):
    user = {"email": "u@example.com",
            "service_credentials": {"multiconn": _row(1, "a@x.com", is_default=True)}}
    assert _connected(user, [multiconn_plugin])["multiconn"] is True


def test_connected_services_requires_server_available(multiconn_plugin):
    with patch.object(plugins_mod, "plugin_server_available", return_value=False):
        got = _connected(_user([_row(1, "a@x.com", is_default=True)]), [multiconn_plugin])
    assert got["multiconn"] is False


def test_connected_services_single_connection_reads_default_only():
    # A single-connection plugin is judged by its default row exactly as
    # before, even if a stray extra row were present.
    default = {"id": 1, "account": "", "is_default": True, "secret": None}
    extra = {"id": 2, "account": "x", "is_default": False, "secret": "sk-1"}
    user = {"email": "u@example.com", "service_credentials": {"keysvc": default},
            "service_connections": {"keysvc": [default, extra]}}
    assert _connected(user, [_api_key_plugin(False)])["keysvc"] is False
    user["service_credentials"]["keysvc"] = extra
    assert _connected(user, [_api_key_plugin(False)])["keysvc"] is True


def test_connected_predicate_raising_on_one_row(multiconn_plugin):
    def _pred(row):
        if row["account"] == "a@x.com":
            raise RuntimeError("bad row")
        return True

    spec = dataclasses.replace(multiconn_plugin.user_connection, connected=_pred)
    plugin = dataclasses.replace(multiconn_plugin, user_connection=spec)
    rows = [_row(1, "a@x.com", is_default=True), _row(2, "b@x.com")]
    assert _connected(_user(rows), [plugin])["multiconn"] is True
    assert _connected(_user(rows[:1]), [plugin])["multiconn"] is False
