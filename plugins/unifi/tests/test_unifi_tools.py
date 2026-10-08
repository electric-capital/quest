"""Tests for the UniFi dynamic tools (plugins/unifi/tools.py) against the
in-memory controller, plus the plugin's registry wiring via the
``unifi_plugin`` fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import chat.gemini_api.tool_handlers as tool_handlers
import plugins.unifi.tools as tools_mod
from plugins.unifi.tests.fake_controller import (
    HOST,
    JPEG_BYTES,
    NETWORK_KEY,
    PROTECT_KEY,
    FakeController,
)


def _run(coro):
    return asyncio.run(coro)


_CONFIG = {"host": HOST, "verify_tls": False}


class _FakeProvider:
    """Records upload_file calls; returns an opaque ref or None."""

    def __init__(self, supports_upload=True):
        self.supports_upload = supports_upload
        self.uploads: list[dict] = []

    async def upload_file(self, file_path, mime_type, display_name="", model=""):
        self.uploads.append({"file_path": file_path, "mime_type": mime_type, "model": model})
        return {"ref": file_path} if self.supports_upload else None

    def make_file_part(self, ref):
        return {"part": ref}


def _ctx(*, network=True, protect=True, conversation_id="conv-1", provider=None):
    blob = {}
    if network:
        blob["network_api_key"] = NETWORK_KEY
    if protect:
        blob["protect_api_key"] = PROTECT_KEY
    user = {"id": 7, "email": "u@example.com"}
    if blob:
        user["service_credentials"] = {"unifi": {"oauth_blob": blob}}
    return SimpleNamespace(
        user=user, conversation_id=conversation_id, project_id=None,
        provider=provider, model="claude-opus-4-8",
    )


@pytest.fixture()
def controller():
    """Wire the tools module to the fake console for one test."""
    fake = FakeController()
    with patch.object(tools_mod, "load_unifi_config", return_value=_CONFIG), \
            patch.object(tools_mod, "make_client", fake.make_client):
        yield fake


def _call(handler, ctx, args=None):
    out = _run(handler(ctx, args or {}))
    if isinstance(out, tuple):
        result, parts = out
        return json.loads(result), parts
    return json.loads(out)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

class TestGating:
    def test_not_configured(self):
        with patch.object(
            tools_mod, "load_unifi_config",
            side_effect=HTTPException(status_code=500, detail="UniFi controller host not configured."),
        ):
            out = _call(tools_mod._tool_list_sites, _ctx())
        assert out["error"] == "unifi_not_configured"

    def test_missing_network_key(self, controller):
        out = _call(tools_mod._tool_list_devices, _ctx(network=False))
        assert out["error"] == "unifi_not_connected"
        assert out["application"] == "network"
        assert controller.calls == []

    def test_missing_protect_key(self, controller):
        out = _call(tools_mod._tool_list_cameras, _ctx(protect=False))
        assert out["error"] == "unifi_not_connected"
        assert out["application"] == "protect"

    def test_rejected_key_surfaces_auth_error(self, controller):
        ctx = _ctx()
        ctx.user["service_credentials"]["unifi"]["oauth_blob"]["network_api_key"] = "stale"
        out = _call(tools_mod._tool_list_sites, ctx)
        assert out["error"] == "unifi_auth_failed"
        assert out["status_code"] == 401


# ---------------------------------------------------------------------------
# Controller info / sites
# ---------------------------------------------------------------------------

class TestControllerInfo:
    def test_reports_both_applications(self, controller):
        out = _call(tools_mod._tool_get_controller_info, _ctx())
        assert out["host"] == HOST
        assert out["network"] == {
            "connected": True, "reachable": True, "application_version": "9.3.45",
            "sites": [{"id": "site-1", "internal_reference": "default", "name": "Default"}],
        }
        assert out["protect"] == {"connected": True, "reachable": True, "application_version": "6.0.1"}

    def test_only_protect_connected(self, controller):
        out = _call(tools_mod._tool_get_controller_info, _ctx(network=False))
        assert out["network"] == {"connected": False}
        assert out["protect"]["reachable"] is True
        assert all(not p.startswith("/proxy/network") for _, p in controller.calls)

    def test_no_keys_notes_how_to_connect(self, controller):
        out = _call(tools_mod._tool_get_controller_info, _ctx(network=False, protect=False))
        assert "Data Connections" in out["note"]

    def test_list_sites(self, controller):
        out = _call(tools_mod._tool_list_sites, _ctx())
        assert out["count"] == 1 and out["sites"][0]["internal_reference"] == "default"


# ---------------------------------------------------------------------------
# Network status
# ---------------------------------------------------------------------------

class TestNetworkStatus:
    def test_full_report_with_health(self, controller):
        out = _call(tools_mod._tool_get_network_status, _ctx())
        assert out["site"]["name"] == "Default"
        assert out["overall_status"] == "ok"
        assert out["subsystems"]["wan"]["wan_ip"] == "203.0.113.5"
        assert out["subsystems"]["wlan"]["status"] == "warning"
        gw = out["gateways"][0]
        assert gw["name"] == "Gateway" and gw["is_gateway"] is True
        assert gw["statistics"]["uplink"] == {"tx_rate_bps": 1000, "rx_rate_bps": 2000}
        assert gw["statistics"]["uptime_seconds"] == 86400
        assert gw["port_count"] == 2 and gw["ports_up"] == 1
        assert out["devices"]["total"] == 2
        assert out["devices"]["by_state"] == {"ONLINE": 1, "OFFLINE": 1}
        assert [d["name"] for d in out["devices"]["not_online"]] == ["Office AP"]
        assert out["clients"] == {"total": 3, "by_type": {"WIRELESS": 2, "WIRED": 1}}
        assert "subsystems_note" not in out

    def test_degrades_when_legacy_health_rejected(self):
        fake = FakeController(health_status=401)
        with patch.object(tools_mod, "load_unifi_config", return_value=_CONFIG), \
                patch.object(tools_mod, "make_client", fake.make_client):
            out = _call(tools_mod._tool_get_network_status, _ctx())
        assert "subsystems" not in out
        assert "unavailable" in out["subsystems_note"]
        # Gateway ONLINE -> still "ok" from device state alone.
        assert out["overall_status"] == "ok"

    def test_unknown_site(self, controller):
        out = _call(tools_mod._tool_get_network_status, _ctx(), {"site": "branch"})
        assert out["error"] == "unifi_unknown_site"
        assert "Default" in out["message"]

    def test_site_matched_by_name_or_ref(self, controller):
        for needle in ("default", "DEFAULT", "site-1"):
            out = _call(tools_mod._tool_get_network_status, _ctx(), {"site": needle})
            assert out["site"]["id"] == "site-1"


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class TestDevices:
    def test_list_devices(self, controller):
        out = _call(tools_mod._tool_list_devices, _ctx())
        assert out["count"] == 2
        names = {d["name"]: d for d in out["devices"]}
        assert names["Gateway"]["state"] == "ONLINE" and names["Gateway"]["is_gateway"]
        assert names["Office AP"]["radio_count"] == 2
        assert "statistics" not in names["Gateway"]
        assert "interfaces" not in names["Gateway"]  # bulky nested data trimmed

    def test_list_devices_state_filter_and_statistics(self, controller):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"state": "online", "include_statistics": True})
        assert out["state_filter"] == "ONLINE"
        assert [d["name"] for d in out["devices"]] == ["Gateway"]
        assert out["devices"][0]["statistics"]["cpu_utilization_pct"] == 12.5

    def test_get_device_by_name_ip_or_mac(self, controller):
        for needle in ("gateway", "192.168.1.1", "AA:BB:CC:00:00:01", "dev-gw"):
            out = _call(tools_mod._tool_get_device, _ctx(), {"device": needle})
            assert out["device"]["id"] == "dev-gw", needle
        assert out["device"]["detail"]["adoptedAt"] == "2025-01-01T00:00:00Z"
        assert out["device"]["statistics"]["uptime_seconds"] == 86400

    def test_get_device_offline_skips_statistics(self, controller):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "Office AP"})
        assert out["device"]["state"] == "OFFLINE"
        assert "statistics" not in out["device"]
        assert not any(p.endswith("statistics/latest") for _, p in controller.calls)

    def test_get_device_unknown_and_missing_arg(self, controller):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "nope"})
        assert out["error"] == "unifi_unknown_device"
        assert {d["name"] for d in out["available"]} == {"Gateway", "Office AP"}
        assert _call(tools_mod._tool_get_device, _ctx(), {})["error"] == "invalid_arguments"


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class TestClients:
    def test_list_clients_pages_through_controller_and_names_uplink(self, controller):
        out = _call(tools_mod._tool_list_clients, _ctx())
        assert out["total_connected"] == 3 and out["returned"] == 3
        laptop = next(c for c in out["clients"] if c["name"] == "Laptop")
        assert laptop["uplink_device_name"] == "Office AP"
        assert laptop["access"] == "DEFAULT"

    def test_type_and_search_filters(self, controller):
        out = _call(tools_mod._tool_list_clients, _ctx(), {"type": "wireless", "search": "phone"})
        assert out["type_filter"] == "WIRELESS"
        assert [c["name"] for c in out["clients"]] == ["Phone"]
        assert out["matched"] == 1 and out["total_connected"] == 3

    def test_invalid_type(self, controller):
        assert _call(tools_mod._tool_list_clients, _ctx(), {"type": "bogus"})["error"] == "invalid_arguments"

    def test_limit_offset_paging(self, controller):
        first = _call(tools_mod._tool_list_clients, _ctx(), {"limit": 2})
        assert first["returned"] == 2 and first["next_offset"] == 2
        second = _call(tools_mod._tool_list_clients, _ctx(), {"limit": 2, "offset": 2})
        assert second["returned"] == 1 and "next_offset" not in second

    def test_get_client_by_ip(self, controller):
        out = _call(tools_mod._tool_get_client, _ctx(), {"client": "192.168.1.60"})
        assert out["client"]["name"] == "NAS"
        assert out["client"]["detail"]["lastSeen"] == "2026-09-03T10:00:00Z"

    def test_get_client_unknown(self, controller):
        out = _call(tools_mod._tool_get_client, _ctx(), {"client": "toaster"})
        assert out["error"] == "unifi_unknown_client"


# ---------------------------------------------------------------------------
# Protect
# ---------------------------------------------------------------------------

class TestCameras:
    def test_list_cameras(self, controller):
        out = _call(tools_mod._tool_list_cameras, _ctx())
        assert out["count"] == 2 and out["connected"] == 1 and out["disconnected"] == 1
        front = next(c for c in out["cameras"] if c["name"] == "Front Door")
        assert front["state"] == "CONNECTED"
        assert front["feature_flags"] == {"hasHdr": True}
        assert front["smart_detect_object_types"] == ["person", "vehicle"]

    def test_get_camera_by_name(self, controller):
        out = _call(tools_mod._tool_get_camera, _ctx(), {"camera": "garage"})
        assert out["camera"]["id"] == "cam-2"
        assert out["camera"]["detail"]["osdSettings"] == {"isNameEnabled": True}

    def test_get_camera_unknown(self, controller):
        out = _call(tools_mod._tool_get_camera, _ctx(), {"camera": "attic"})
        assert out["error"] == "unifi_unknown_camera"
        assert [c["name"] for c in out["available"]] == ["Front Door", "Garage"]

    def test_list_protect_devices(self, controller):
        out = _call(tools_mod._tool_list_protect_devices, _ctx(), {"kind": "nvrs"})
        assert out["count"] == 1 and out["devices"][0]["name"] == "UNVR"
        assert _call(tools_mod._tool_list_protect_devices, _ctx(), {"kind": "sensors"})["count"] == 0
        assert _call(tools_mod._tool_list_protect_devices, _ctx(), {"kind": "toasters"})["error"] == "invalid_arguments"


class TestSnapshot:
    @pytest.fixture()
    def workspace(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()

        async def _dir(conversation_id, project_id=None):
            return root

        with patch.object(tool_handlers, "conversation_workspace_dir", _dir), \
                patch.object(tool_handlers, "_publish_file_list_changed") as published:
            yield root, published

    def test_saves_jpeg_and_attaches_image(self, controller, workspace):
        root, published = workspace
        provider = _FakeProvider()
        out, parts = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=provider), {"camera": "Front Door"})
        assert out["status"] == "success"
        assert out["camera"]["id"] == "cam-1"
        assert out["path"].startswith("unifi-snapshots/Front-Door-") and out["path"].endswith(".jpg")
        assert (root / out["path"]).read_bytes() == JPEG_BYTES
        assert out["size_bytes"] == len(JPEG_BYTES)
        assert out["attached_to_response"] is True
        assert parts == [{"part": {"ref": str(root / out["path"])}}]
        assert provider.uploads[0]["mime_type"] == "image/jpeg"
        assert f"![Front Door]({out['path']})" in out["message"]
        published.assert_called_once_with(7, "conversation", "conv-1", None)
        snapshot_calls = [c for c in controller.calls if c[1].endswith("/snapshot")]
        assert snapshot_calls == [("GET", "/proxy/protect/integration/v1/cameras/cam-1/snapshot")]

    def test_high_quality_flag_forwarded(self, controller, workspace):
        requests = []
        original = controller.handler

        def spy(request):
            requests.append(request)
            return original(request)

        controller.handler = spy
        out, _ = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()),
                       {"camera": "cam-1", "high_quality": True})
        assert out["high_quality"] is True
        snap = next(r for r in requests if r.url.path.endswith("/snapshot"))
        assert snap.url.params["highQuality"] == "true"

    def test_provider_without_upload_still_saves_file(self, controller, workspace):
        root, _ = workspace
        out, parts = _call(tools_mod._tool_get_camera_snapshot,
                           _ctx(provider=_FakeProvider(supports_upload=False)), {"camera": "cam-1"})
        assert out["attached_to_response"] is False and parts == []
        assert (root / out["path"]).exists()
        assert "get_workspace_file" in out["message"]

    def test_custom_path_directory_and_file(self, controller, workspace):
        root, _ = workspace
        out, _ = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()),
                       {"camera": "cam-1", "path": "shots/"})
        assert out["path"].startswith("shots/Front-Door-")
        out, _ = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()),
                       {"camera": "cam-1", "path": "front"})
        assert out["path"] == "front.jpg"
        assert (root / "front.jpg").exists()

    def test_path_traversal_rejected(self, controller, workspace):
        for bad in ("../x.jpg", "/tmp/x.jpg"):
            out = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()),
                        {"camera": "cam-1", "path": bad})
            assert out["error"] == "invalid_path", bad

    def test_offline_camera_refused(self, controller, workspace):
        out = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()), {"camera": "Garage"})
        assert out["error"] == "unifi_camera_offline"
        assert out["camera"]["state"] == "DISCONNECTED"
        assert not any(p.endswith("/snapshot") for _, p in controller.calls)

    def test_requires_workspace(self, controller):
        out = _call(tools_mod._tool_get_camera_snapshot, _ctx(conversation_id=None), {"camera": "cam-1"})
        assert out["error"] == "no_workspace"

    def test_upstream_snapshot_failure(self, workspace):
        fake = FakeController(snapshot_status=500)
        with patch.object(tools_mod, "load_unifi_config", return_value=_CONFIG), \
                patch.object(tools_mod, "make_client", fake.make_client):
            out = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=_FakeProvider()), {"camera": "cam-1"})
        assert out["error"] == "unifi_request_failed" and out["status_code"] == 500

    def test_attach_failure_keeps_file(self, controller, workspace):
        root, _ = workspace
        provider = _FakeProvider()
        provider.upload_file = AsyncMock(side_effect=RuntimeError("boom"))
        out, parts = _call(tools_mod._tool_get_camera_snapshot, _ctx(provider=provider), {"camera": "cam-1"})
        assert out["status"] == "success" and out["attached_to_response"] is False
        assert parts == [] and (root / out["path"]).exists()


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------

_TOOL_NAMES = {
    "unifi_get_controller_info", "unifi_list_sites", "unifi_get_network_status",
    "unifi_list_devices", "unifi_get_device", "unifi_list_clients", "unifi_get_client",
    "unifi_list_cameras", "unifi_get_camera", "unifi_get_camera_snapshot",
    "unifi_list_protect_devices",
}


class TestPluginWiring:
    def test_manifest_shape(self, unifi_plugin):
        assert unifi_plugin.id == "unifi"
        assert {t.spec["name"] for t in unifi_plugin.tools} == _TOOL_NAMES
        assert all(t.requires_service == "unifi" for t in unifi_plugin.tools)
        assert unifi_plugin.action_request_handlers == ()
        assert unifi_plugin.services == ()
        # Snapshot (needs a workspace) and the raw Protect dump stay tool-only.
        assert "unifi_get_camera_snapshot" not in unifi_plugin.script_tool_allowlist
        assert "unifi_list_protect_devices" not in unifi_plugin.script_tool_allowlist
        assert unifi_plugin.script_tool_allowlist <= _TOOL_NAMES
        assert unifi_plugin.user_connection.kind == "oauth"
        assert [f.key for f in unifi_plugin.credential_schema] == ["host", "verify_tls"]
        assert next(f for f in unifi_plugin.credential_schema if f.key == "verify_tls").type == "bool"
        assert Path(tools_mod.__file__).parent.joinpath("instructions.md").exists()

    def test_registered_into_core_registries(self, unifi_plugin):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST, TOOL_CALL_REGISTRY
        from chat.system_skills import CATALOG

        for name in _TOOL_NAMES:
            assert name in TOOL_CALL_REGISTRY
            assert TOOL_CALL_REGISTRY[name]["requires_service"] == "unifi"
            assert name in TOOL_CALL_HANDLERS
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST
        assert "unifi_list_devices" in SCRIPT_TOOL_CALL_ALLOWLIST
        assert "unifi_get_camera_snapshot" not in SCRIPT_TOOL_CALL_ALLOWLIST
        assert CATALOG["system:unifi"].requires == "unifi"
        assert "unifi_get_network_status" in CATALOG["system:unifi"].content_builder("", "")

    def test_connectors_row_uses_popup_convention(self, unifi_plugin):
        from config.plugins import plugin_server_available

        spec = unifi_plugin.user_connection
        assert spec.connected({"oauth_blob": {"protect_api_key": "p"}})
        assert not spec.connected({"oauth_blob": {}})
        assert {r.path for r in spec.oauth_router.routes} == {
            "/auth/unifi", "/auth/unifi/keys", "/auth/unifi/disconnect",
        }
        with patch("config.service_credentials.read_service_credentials", return_value=None):
            assert plugin_server_available(unifi_plugin) is False
        with patch("config.service_credentials.read_service_credentials", return_value=_CONFIG):
            assert plugin_server_available(unifi_plugin) is True
