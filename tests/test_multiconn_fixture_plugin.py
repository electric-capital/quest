"""The test-only multi-connection fixture plugin loads and resolves (issue #84)."""

import asyncio
import json
from types import SimpleNamespace

from config.plugins import get_user_connection_spec
from config.user_connections import resolve_connection


def test_fixture_plugin_registers(multiconn_plugin):
    spec = get_user_connection_spec("multiconn")
    assert spec is not None
    assert spec.kind == "form" and spec.multi_connection


def test_fixture_tool_resolves_named_connection(multiconn_plugin):
    rows = [
        {"id": 1, "service": "multiconn", "account": "a@x", "is_default": True,
         "oauth_blob": {"values": {"host": "mail.example.com"}}},
        {"id": 2, "service": "multiconn", "account": "b@x", "is_default": False,
         "oauth_blob": {"values": {"host": "mail.example.com"}}},
    ]
    user = {"service_credentials": {"multiconn": rows[0]},
            "service_connections": {"multiconn": rows}}
    assert resolve_connection(user, "multiconn", "B@X")["id"] == 2
    tool = multiconn_plugin.tools[0]
    ctx = SimpleNamespace(user=user)
    assert json.loads(asyncio.run(tool.handler(ctx, {})))["account"] == "a@x"
    assert json.loads(asyncio.run(tool.handler(ctx, {"connection": "b@x"})))["account"] == "b@x"
    err = json.loads(asyncio.run(tool.handler(ctx, {"connection": "c@x"})))
    assert err["error"] == "connection_not_found" and err["available"] == ["a@x", "b@x"]
