"""Tests for the Telegram read tools (plugins/telegram/tools.py) and plugin wiring.

Registry/dispatch fan-out via the ``telegram_plugin`` fixture,
``requires_service`` prompt gating, the script-bridge allow-list, the
public-project exclusion, the proxy block, and the handlers' argument
coercion + error rendering (no Telethon network access: a user without a
stored session fails inside ``TelegramClientManager`` before any
connection is attempted).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS, _dispatch_tool_call
from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST, TOOL_CALL_REGISTRY

from plugins.telegram.tools import (
    TELEGRAM_TOOL_NAMES,
    _tool_get_me,
    _tool_get_messages,
    _tool_list_contacts,
    _tool_list_dialogs,
)

TELEGRAM_TOOLS = (
    "telegram_get_me",
    "telegram_list_dialogs",
    "telegram_get_messages",
    "telegram_list_contacts",
)


def _run(coro):
    return asyncio.run(coro)


def _ctx(*, connected=True):
    user = {"id": 1, "email": "t@example.com"}
    if connected:
        user["service_credentials"] = {"telegram": {"oauth_blob": {"session": "sess"}}}
    return SimpleNamespace(user=user, conversation_id="c1", project_id=None)


def _client_patch(client):
    return patch(
        "plugins.telegram.upstream.TelegramClientManager.get_client",
        AsyncMock(return_value=client),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_tool_names(self):
        assert TELEGRAM_TOOL_NAMES == frozenset(TELEGRAM_TOOLS)

    def test_specs_registered_and_gated_on_telegram(self, telegram_plugin):
        for name in TELEGRAM_TOOLS:
            assert name in TOOL_CALL_REGISTRY, name
            assert TOOL_CALL_REGISTRY[name]["requires_service"] == "telegram"
            assert name in TOOL_CALL_HANDLERS, name

    def test_get_messages_requires_dialog_id(self, telegram_plugin):
        assert TOOL_CALL_REGISTRY["telegram_get_messages"]["parameters"]["required"] == ["dialog_id"]

    def test_hidden_from_prompt_until_connected(self, telegram_plugin):
        # Plugin tools are skill-documented: a prompt names them on the
        # system:telegram enumeration line, gated with the skill.
        from chat.system_skills import build_system_skills_enumeration
        from tests.prompt_support import named_under_skill

        hidden = build_system_skills_enumeration(connected_services={"telegram": False})
        shown = build_system_skills_enumeration(connected_services={"telegram": True})
        for name in TELEGRAM_TOOLS:
            assert name not in hidden, name
            assert named_under_skill(shown, "system:telegram", name), name

    def test_script_bridge_allowlisted(self, telegram_plugin):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST

        for name in TELEGRAM_TOOLS:
            assert name in SCRIPT_TOOL_CALL_ALLOWLIST, name

    def test_not_in_public_project_allowlist(self):
        for name in TELEGRAM_TOOLS:
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST, name

    def test_unregistered_without_plugin(self):
        for name in TELEGRAM_TOOLS:
            assert name not in TOOL_CALL_REGISTRY, name
            assert name not in TOOL_CALL_HANDLERS, name

    def test_manifest_shape(self, telegram_plugin):
        assert telegram_plugin.id == "telegram"
        assert telegram_plugin.user_connection.kind == "oauth"
        assert telegram_plugin.unprefixed_action_types == frozenset({"send_telegram_message"})
        assert telegram_plugin.on_shutdown is not None
        assert telegram_plugin.post_load is not None
        assert [s.id for s in telegram_plugin.system_skills] == ["system:telegram"]

    def test_on_shutdown_closes_clients(self, telegram_plugin):
        from plugins.telegram.upstream import TelegramClientManager

        manager = TelegramClientManager.get_instance()
        client = SimpleNamespace(
            is_connected=lambda: True, disconnect=AsyncMock(),
        )
        manager._clients[99] = client
        _run(telegram_plugin.on_shutdown())
        client.disconnect.assert_awaited_once()
        assert manager._clients == {}


# ---------------------------------------------------------------------------
# Proxy blocking: /api/telegram is tool-only
# ---------------------------------------------------------------------------

class TestTelegramProxyBlocked:
    def test_telegram_prefix_in_blocked_paths(self):
        from chat.route_dispatch import _BLOCKED_PROXY_PATHS

        assert "/api/telegram" in _BLOCKED_PROXY_PATHS

    def test_curl_proxy_get_on_telegram_read_returns_blocked_error(self):
        from chat.route_dispatch import execute_tool_call

        result, notices = _run(execute_tool_call(
            app=object(),  # blocked-path check fires before route resolution
            user=_ctx().user,
            tool_name="curl_proxy_get",
            tool_args={"url": "http://localhost:8000/api/telegram/dialogs?limit=5"},
        ))
        payload = json.loads(result)
        assert "dedicated tool" in payload["error"]
        assert notices == []

    def test_main_app_has_no_telegram_api_routes(self):
        """quest.py mounts no /api/telegram/*; only the plugin's /auth/telegram
        login routes exist. Subprocess: importing quest loads plugins into
        the process-global registries."""
        import subprocess
        import sys
        from pathlib import Path

        script = (
            "import json, quest\n"
            "from chat.route_table import iter_app_routes\n"
            "paths = sorted({r.path for r in iter_app_routes(quest.app) if r.path})\n"
            "print(json.dumps({'api': [p for p in paths if p.startswith('/api/telegram')],"
            " 'auth': [p for p in paths if p.startswith('/auth/telegram')]}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[3],
        )
        assert proc.returncode == 0, proc.stderr
        routes = json.loads(proc.stdout.strip().splitlines()[-1])
        assert routes["api"] == []
        assert routes["auth"] == [
            "/auth/telegram", "/auth/telegram/2fa", "/auth/telegram/disconnect",
            "/auth/telegram/send-code", "/auth/telegram/verify",
        ]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class _FakeDialog:
    id = 42
    name = "Alice"
    is_user = True
    is_group = False
    is_channel = False
    unread_count = 3
    date = None


class TestHandlers:
    def test_not_connected_renders_structured_error(self):
        ctx = _ctx(connected=False)
        for coro in (
            _tool_get_me(ctx, {}),
            _tool_list_dialogs(ctx, {}),
            _tool_get_messages(ctx, {"dialog_id": 42}),
            _tool_list_contacts(ctx, {}),
        ):
            payload = json.loads(_run(coro))
            assert payload["error"] == "telegram_not_connected"

    def test_list_dialogs_serializes_endpoint_result(self):
        client = AsyncMock()
        client.get_dialogs = AsyncMock(return_value=[_FakeDialog()])
        with _client_patch(client):
            payload = json.loads(_run(_tool_list_dialogs(_ctx(), {"limit": "5"})))
        client.get_dialogs.assert_awaited_once_with(limit=5)
        assert payload == {"dialogs": [{
            "id": 42, "name": "Alice", "is_user": True, "is_group": False,
            "is_channel": False, "unread_count": 3, "last_message_date": None,
        }]}

    def test_get_messages_coerces_args(self):
        client = AsyncMock()
        client.get_messages = AsyncMock(return_value=[])
        with _client_patch(client):
            payload = json.loads(_run(_tool_get_messages(_ctx(), {
                "dialog_id": "-1001234", "limit": "10", "offset_id": "99",
                "offset_date": "2024-01-15T10:30:00Z", "reverse": "true",
            })))
        assert payload == {"messages": []}
        kwargs = client.get_messages.await_args.kwargs
        assert client.get_messages.await_args.args == (-1001234,)
        assert kwargs["limit"] == 10
        assert kwargs["offset_id"] == 99
        assert kwargs["reverse"] is True
        assert kwargs["offset_date"].isoformat() == "2024-01-15T10:30:00+00:00"

    def test_get_messages_rejects_bad_dialog_id_before_connecting(self):
        for bad in (None, "", "@alice", True):
            payload = json.loads(_run(_tool_get_messages(_ctx(connected=False), {"dialog_id": bad})))
            assert "dialog_id" in payload["error"], bad
            assert payload["error"] != "telegram_not_connected"

    def test_get_messages_invalid_offset_date(self):
        # Date parsing runs before the client lookup, so the disconnected
        # user still gets the date error.
        payload = json.loads(_run(_tool_get_messages(
            _ctx(connected=False), {"dialog_id": 42, "offset_date": "yesterday"},
        )))
        assert payload["error"] == "invalid_date"

    def test_telethon_failure_becomes_telegram_api_error(self):
        client = AsyncMock()
        client.get_me = AsyncMock(side_effect=RuntimeError("boom"))
        with _client_patch(client):
            payload = json.loads(_run(_tool_get_me(_ctx(), {})))
        assert payload["error"] == "telegram_api_error"
        assert "boom" in payload["message"]

    def test_dispatch_via_tool_call(self, telegram_plugin):
        result_text, extra_parts = _run(_dispatch_tool_call(
            app=None, provider=None, user=_ctx(connected=False).user,
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "telegram_list_contacts", "arguments": {}},
        ))
        assert json.loads(result_text)["error"] == "telegram_not_connected"
        assert extra_parts == []


# ---------------------------------------------------------------------------
# Skill prose
# ---------------------------------------------------------------------------

def test_instructions_point_at_tools_not_proxy(telegram_plugin):
    from chat.system_skills import CATALOG

    text = CATALOG["system:telegram"].content_builder("http://localhost:9200", "key")
    for name in TELEGRAM_TOOLS:
        assert name in text, name
    for route in ("/api/telegram/me", "/api/telegram/dialogs", "/api/telegram/messages", "/api/telegram/contacts"):
        assert route not in text, route
    assert "curl_proxy_get` on" not in text
    assert "/api/tool-call" in text and "QUEST_PORT" in text
    assert "send_telegram_message" in text
    assert "Settings > Data" in text
