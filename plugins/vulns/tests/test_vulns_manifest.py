"""Tests for the vulnerability data plugin manifest: the credential-free
shape with tools.

The three sources are free and public, so the plugin declares no admin card
and no per-user connection. This suite pins what follows from that:

* The manifest validates with the four ``vulns_*`` tools and the one
  ungated ``system:vulns`` skill -- no services, action requests,
  credential schema or user connection.
* The tools register into the tool_call registry without a
  ``requires_service`` gate, are in the script bridge, and stay out of
  public projects.
* The skill is visible and loadable with NOTHING connected and documents
  every tool.
* GET /me's ``has_any_service_connected`` does not count the always-true
  ``vulns`` key.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from api.instructions import get_user_connected_services
from chat.system_skills import CATALOG, list_system_skills, load_system_skills
from config import plugins as plugins_mod
from plugins.vulns import manifest
from plugins.vulns.tools import SCRIPT_TOOL_ALLOWLIST

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_TOOL_NAMES = frozenset({
    "vulns_check_packages", "vulns_get_vulnerability", "vulns_search_kev", "vulns_get_epss",
})
NONE_CONNECTED = {"google_services": False, "airtable": False, "ramp": False}


class TestManifest:
    def test_validates_as_the_credential_free_shape_with_tools(self):
        plugin = manifest.get_plugin()
        plugins_mod.validate_plugin(plugin, set())
        assert plugin.id == "vulns"
        assert plugin.credential_schema == ()
        assert plugin.user_connection is None
        assert plugin.services == ()
        assert plugin.action_request_handlers == ()
        assert {t.spec["name"] for t in plugin.tools} == _TOOL_NAMES
        assert all(t.requires_service is None and not t.mutating for t in plugin.tools)
        assert plugin.script_tool_allowlist == SCRIPT_TOOL_ALLOWLIST == _TOOL_NAMES
        assert [s.id for s in plugin.system_skills] == ["system:vulns"]
        assert plugins_mod.plugin_is_connectionless(plugin)
        assert plugins_mod.plugin_server_available(plugin) is True

    def test_registered_into_core_registries(self, vulns_plugin):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST, TOOL_CALL_REGISTRY

        for name in _TOOL_NAMES:
            assert name in TOOL_CALL_REGISTRY
            assert "requires_service" not in TOOL_CALL_REGISTRY[name]
            assert TOOL_CALL_REGISTRY[name]["skill"] == "system:vulns"
            assert name in TOOL_CALL_HANDLERS
            assert name in SCRIPT_TOOL_CALL_ALLOWLIST
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST

    def test_tools_absent_while_plugin_is_not_loaded(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        assert not _TOOL_NAMES & set(TOOL_CALL_REGISTRY)


class TestSkill:
    def test_skill_is_ungated_and_loadable_with_nothing_connected(self, vulns_plugin):
        assert CATALOG["system:vulns"].requires is None
        visible_ids = {s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)}
        assert "system:vulns" in visible_ids

        (entry,) = load_system_skills(
            ["system:vulns"], NONE_CONNECTED, "http://localhost:9000", "key",
        )
        assert "error" not in entry
        assert entry["content"].startswith((_PLUGIN_DIR / "instructions.md").read_text())
        for name in _TOOL_NAMES:
            assert name in entry["content"]


class TestConnectedServices:
    def test_reported_true_but_not_counted_on_me(self, vulns_plugin):
        import chat.routes.user as user_routes

        with patch("config.service_credentials.read_service_credentials", return_value=None):
            assert get_user_connected_services({})["vulns"] is True
            info = asyncio.run(user_routes.get_current_user_info(
                user={"email": "u@example.com", "name": "U"},
            ))
        assert "vulns" in plugins_mod.connectionless_plugin_ids()
        assert info["has_any_service_connected"] is False
