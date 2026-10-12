"""Tests for the SEC EDGAR plugin manifest: the credential-free shape.

The plugin wraps a free, public API, so it declares no admin card and no
per-user connection. This suite pins what follows from that:

* The manifest validates and carries exactly the two service entries and
  the one ungated (``requires=None``) ``system:sec_edgar`` skill -- no
  tools, action requests, credential schema or user connection.
* The skill is visible and loadable with NOTHING connected (there is no
  connected_services key to gate on) and its content is the plugin's
  ``instructions.md``.
* ``get_user_connected_services()`` reports ``sec_edgar`` as true for every
  user, yet GET /me's ``has_any_service_connected`` does NOT count it
  (``config.plugins.connectionless_plugin_ids``), so the first-run
  "nothing connected" nudge still fires for a fresh user.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from api.instructions import get_user_connected_services
from chat.system_skills import CATALOG, list_system_skills, load_system_skills
from config import plugins as plugins_mod
from plugins.sec_edgar import manifest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent

NONE_CONNECTED = {
    "google_services": False,
    "airtable": False,
    "ramp": False,
}


def _run(coro):
    return asyncio.run(coro)


class TestManifest:
    def test_validates_as_the_credential_free_shape(self):
        plugin = manifest.get_plugin()
        plugins_mod.validate_plugin(plugin, set())
        assert plugin.id == "sec_edgar"
        assert plugin.credential_schema == ()
        assert plugin.user_connection is None
        assert plugin.tools == ()
        assert plugin.action_request_handlers == ()
        assert {s["key"] for s in plugin.services} == {"data.sec.gov", "www.sec.gov"}
        assert [s.id for s in plugin.system_skills] == ["system:sec_edgar"]
        assert plugins_mod.plugin_is_connectionless(plugin)

    def test_server_side_always_available(self):
        # No credential schema -> no admin switch -> always available.
        assert plugins_mod.plugin_server_available(manifest.get_plugin()) is True


class TestSkill:
    def test_skill_is_ungated_and_loadable_with_nothing_connected(self, sec_edgar_plugin):
        assert "system:sec_edgar" in CATALOG
        assert CATALOG["system:sec_edgar"].requires is None

        visible_ids = {
            s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)
        }
        assert "system:sec_edgar" in visible_ids

        results = load_system_skills(
            ["system:sec_edgar"], NONE_CONNECTED, "http://localhost:9000", "key",
        )
        assert len(results) == 1
        entry = results[0]
        assert entry["id"] == "system:sec_edgar"
        assert "error" not in entry
        assert entry["content"] == (_PLUGIN_DIR / "instructions.md").read_text()
        assert "CIK0000320193" in entry["content"]

    def test_skill_absent_while_plugin_is_not_loaded(self):
        assert "system:sec_edgar" not in CATALOG


class TestConnectedServices:
    def test_reported_true_for_every_user(self, sec_edgar_plugin):
        with patch("config.service_credentials.read_service_credentials",
                   return_value=None):
            assert get_user_connected_services({})["sec_edgar"] is True

    def test_does_not_count_as_a_connected_service_on_me(self, sec_edgar_plugin):
        import chat.routes.user as user_routes

        assert plugins_mod.connectionless_plugin_ids() == {"sec_edgar"}
        with patch("config.service_credentials.read_service_credentials",
                   return_value=None):
            info = _run(user_routes.get_current_user_info(
                user={"email": "u@example.com", "name": "U"},
            ))
        assert info["has_any_service_connected"] is False

        # A real connection still counts alongside the plugin.
        with patch("config.service_credentials.read_service_credentials",
                   return_value=None):
            info = _run(user_routes.get_current_user_info(
                user={"email": "u@example.com", "name": "U", "airtable_token": "pat"},
            ))
        assert info["has_any_service_connected"] is True
