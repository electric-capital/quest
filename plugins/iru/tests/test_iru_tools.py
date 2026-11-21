"""Tests for the Iru dynamic tools (plugins/iru/tools.py) against the
in-memory tenant, plus the plugin's registry wiring via the ``iru_plugin``
fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import plugins.iru.tools as tools_mod
from plugins.iru import fleet
from plugins.iru.tests.fake_tenant import (
    API_URL,
    BP_ENG,
    BP_OPS,
    IPAD_LOBBY,
    LIBRARY_ITEM_ZOOM,
    MAC_ADA,
    MAC_BUILD,
    TOKEN,
    USER_ADA,
    FakeTenant,
)


def _run(coro):
    return asyncio.run(coro)


_CONFIG = {"api_url": API_URL}


def _ctx(*, connected=True, token=TOKEN):
    user = {"id": 7, "email": "u@example.com"}
    if connected:
        user["service_credentials"] = {"iru": {"secret": token}}
    return SimpleNamespace(
        user=user, conversation_id="conv-1", project_id=None,
        provider=None, model="claude-opus-4-8",
    )


@pytest.fixture()
def tenant():
    """Wire the tools module to the fake tenant for one test."""
    fake = FakeTenant()
    with patch.object(tools_mod, "load_iru_config", return_value=_CONFIG), \
            patch.object(tools_mod, "make_client", fake.make_client):
        yield fake


def _use(fake: FakeTenant):
    return patch.multiple(tools_mod, load_iru_config=lambda: _CONFIG, make_client=fake.make_client)


def _call(handler, ctx, args=None):
    return json.loads(_run(handler(ctx, args or {})))


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

class TestGating:
    def test_not_configured(self):
        with patch.object(
            tools_mod, "load_iru_config",
            side_effect=HTTPException(status_code=500, detail="Iru tenant API URL not configured."),
        ):
            out = _call(tools_mod._tool_list_devices, _ctx())
        assert out["error"] == "iru_not_configured"
        assert "not configured" in out["message"]

    def test_not_connected(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(connected=False))
        assert out["error"] == "iru_not_connected"
        assert tenant.calls == []

    def test_rejected_token_surfaces_auth_error(self, tenant):
        out = _call(tools_mod._tool_list_blueprints, _ctx(token="stale-token-1234567"))
        assert out["error"] == "iru_auth_failed"
        assert out["status_code"] == 401

    def test_missing_permission_is_403(self):
        fake = FakeTenant(forbidden_paths={"/api/v1/threat-details"})
        with _use(fake):
            out = _call(tools_mod._tool_list_threats, _ctx())
        assert out["error"] == "iru_auth_failed"
        assert out["status_code"] == 403
        assert "permission" in out["message"]

    def test_rate_limit_reports_retry_after(self):
        fake = FakeTenant(rate_limited=True)
        with _use(fake):
            out = _call(tools_mod._tool_list_tags, _ctx())
        assert out["error"] == "iru_rate_limited"
        assert out["retry_after_seconds"] == 42


# ---------------------------------------------------------------------------
# Tenant info
# ---------------------------------------------------------------------------

class TestTenantInfo:
    def test_reports_licensing_and_blueprints(self, tenant):
        out = _call(tools_mod._tool_get_tenant_info, _ctx())
        assert out["api_url"] == API_URL
        assert out["connected"] is True
        assert out["reachable"] is True
        assert out["licensing"]["counts"]["macos_count"] == 2
        assert out["blueprint_count"] == 2

    def test_not_connected_note(self, tenant):
        out = _call(tools_mod._tool_get_tenant_info, _ctx(connected=False))
        assert out["connected"] is False
        assert "note" in out
        assert tenant.calls == []

    def test_licensing_forbidden_falls_back_to_device_probe(self):
        fake = FakeTenant(licensing_status=403)
        with _use(fake):
            out = _call(tools_mod._tool_get_tenant_info, _ctx())
        assert out["reachable"] is True
        assert "licensing" not in out
        assert "permission" in out["licensing_note"]
        assert ("GET", "/api/v1/devices") in fake.calls

    def test_bad_token_reported_inline(self, tenant):
        out = _call(tools_mod._tool_get_tenant_info, _ctx(token="stale-token-1234567"))
        assert out["reachable"] is False
        assert out["error"] == "iru_auth_failed"


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class TestListDevices:
    def test_compact_rows_and_paging_hint(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"limit": 2})
        assert out["count"] == 2
        assert out["next_offset"] == 2
        row = out["devices"][0]
        assert row["device_name"] == MAC_ADA["device_name"]
        assert row["user"] == {"id": USER_ADA["id"], "name": USER_ADA["name"],
                               "email": USER_ADA["email"], "is_archived": False}
        assert "last_enrollment" not in row  # trimmed
        assert "lost_mode_status" not in row  # empty dropped

        out2 = _call(tools_mod._tool_list_devices, _ctx(), {"limit": 2, "offset": 2})
        assert out2["count"] == 1
        assert out2["next_offset"] is None
        assert out2["devices"][0]["lost_mode_status"] == "enabled"

    def test_filters_forwarded(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {
            "platform": "mac", "filevault_enabled": False, "ordering": "-last_check_in",
        })
        assert [d["serial_number"] for d in out["devices"]] == [MAC_BUILD["serial_number"]]
        sent = tenant.last_params["/api/v1/devices"]
        assert sent["platform"] == "Mac"
        assert sent["filevault_enabled"] == "false"
        assert sent["ordering"] == "-last_check_in"
        assert out["filters"]["platform"] == "Mac"

    def test_invalid_platform(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"platform": "Linux"})
        assert out["error"] == "invalid_arguments"
        assert tenant.calls == []

    def test_blueprint_by_name_resolves_id(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"blueprint": "engineering"})
        assert {d["device_name"] for d in out["devices"]} == {MAC_ADA["device_name"], IPAD_LOBBY["device_name"]}
        assert tenant.last_params["/api/v1/devices"]["blueprint_id"] == BP_ENG["id"]
        assert out["filters"]["blueprint_name"] == "Engineering"

    def test_unknown_blueprint(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"blueprint": "Marketing"})
        assert out["error"] == "iru_unknown_blueprint"

    def test_user_email_filter(self, tenant):
        out = _call(tools_mod._tool_list_devices, _ctx(), {"user_email": "ada@"})
        assert [d["serial_number"] for d in out["devices"]] == [MAC_ADA["serial_number"]]


class TestGetDevice:
    def test_by_serial_default_details(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": MAC_ADA["serial_number"]})
        assert out["device"]["device_id"] == MAC_ADA["device_id"]
        assert out["sections"]["details"]["security"]["filevault_enabled"] is True
        paths = [c[1] for c in tenant.calls]
        assert paths == ["/api/v1/devices", f"/api/v1/devices/{MAC_ADA['device_id']}/details"]

    def test_by_uuid(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": IPAD_LOBBY["device_id"], "sections": []})
        assert out["device"]["device_name"] == IPAD_LOBBY["device_name"]
        assert tenant.calls[0] == ("GET", f"/api/v1/devices/{IPAD_LOBBY['device_id']}")

    def test_by_exact_name_case_insensitive(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "build mac", "sections": ["lost_mode"]})
        assert out["device"]["serial_number"] == MAC_BUILD["serial_number"]
        assert out["sections"]["lost_mode"] == {"lost_mode_status": ""}

    def test_single_partial_name_match_accepted(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "Lobby", "sections": []})
        assert out["device"]["device_id"] == IPAD_LOBBY["device_id"]

    def test_ambiguous_name_lists_candidates(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "Mac"})
        assert out["error"] == "iru_ambiguous_device"
        assert {c["serial_number"] for c in out["candidates"]} == {
            MAC_ADA["serial_number"], MAC_BUILD["serial_number"],
        }

    def test_unknown_device(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "ZZZ"})
        assert out["error"] == "iru_unknown_device"
        out = _call(tools_mod._tool_get_device, _ctx(),
                    {"device": "00000000-0000-0000-0000-000000000000"})
        assert out["error"] == "iru_unknown_device"

    def test_missing_and_unknown_sections(self, tenant):
        assert _call(tools_mod._tool_get_device, _ctx(), {})["error"] == "invalid_arguments"
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": "x", "sections": ["secrets"]})
        assert out["error"] == "invalid_arguments"
        assert "secrets" in out["message"]
        assert tenant.calls == []

    def test_status_and_library_items_sections_are_compact(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {
            "device": MAC_ADA["device_id"], "sections": ["status", "library_items"],
        })
        status = out["sections"]["status"]
        zoom = status["library_items"][0]
        assert zoom["status"] == "PASS"
        assert zoom["item_id"] == LIBRARY_ITEM_ZOOM
        assert "log" not in zoom
        assert zoom["log_tail"].startswith("[") and zoom["log_tail"].endswith("line\n")
        wifi = status["library_items"][1]
        assert wifi["last_audit_log"].endswith("more chars]")
        assert "log_tail" not in wifi
        assert status["parameters"] == [
            {"item_id": "param-1", "status": "PASS"}, {"item_id": "param-2", "status": "ERROR"},
        ]
        items = out["sections"]["library_items"]
        assert items["count"] == 2
        assert items["by_status"] == {"PASS": 1, "ERROR": 1}
        assert items["items"][0]["blueprint_name"] == "Engineering"

    def test_apps_section_with_search(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {
            "device": MAC_ADA["device_id"], "sections": ["apps"], "app_search": "ZOOM",
        })
        apps = out["sections"]["apps"]
        assert apps["count"] == 1
        assert apps["truncated"] is False
        assert apps["apps"][0] == {
            "name": "Zoom", "version": "6.1.0", "bundle_id": "us.zoom.xos",
            "path": "/Applications/zoom.us.app", "source": "Kandji", "app_store_vendable": "no",
        }
        out = _call(tools_mod._tool_get_device, _ctx(), {"device": MAC_ADA["device_id"], "sections": ["apps"]})
        assert [a["name"] for a in out["sections"]["apps"]["apps"]] == ["1Password", "Safari", "Zoom"]

    def test_activity_and_commands_sections(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {
            "device": MAC_ADA["device_id"], "sections": ["activity", "commands"], "activity_limit": 1,
        })
        activity = out["sections"]["activity"]
        assert activity["total"] == 2
        assert activity["returned"] == 1
        assert activity["events"][0]["action_type"] == "enrollment"
        assert activity["events"][0]["blueprint_name"] == "Engineering"
        assert tenant.last_params[f"/api/v1/devices/{MAC_ADA['device_id']}/activity"]["limit"] == "1"
        commands = out["sections"]["commands"]
        assert commands["total"] == 1
        assert commands["commands"][0]["command"] == "DeviceInformation"

    def test_big_activity_details_truncated(self, tenant):
        out = _call(tools_mod._tool_get_device, _ctx(), {
            "device": MAC_ADA["device_id"], "sections": ["activity"],
        })
        big = out["sections"]["activity"]["events"][1]["details"]
        assert isinstance(big, str) and "truncated" in big

    def test_forbidden_section_reported_inline(self):
        fake = FakeTenant(forbidden_paths={f"/api/v1/devices/{MAC_ADA['device_id']}/apps"})
        with _use(fake):
            out = _call(tools_mod._tool_get_device, _ctx(), {
                "device": MAC_ADA["device_id"], "sections": ["apps", "parameters"],
            })
        assert out["sections"]["apps"]["error"] == "iru_auth_failed"
        assert out["sections"]["apps"]["status_code"] == 403
        assert out["sections"]["parameters"] == {"parameters": [{"item_id": "param-1", "status": "PASS"}]}


# ---------------------------------------------------------------------------
# Blueprints / library items
# ---------------------------------------------------------------------------

class TestBlueprints:
    def test_list(self, tenant):
        out = _call(tools_mod._tool_list_blueprints, _ctx(), {"name": "ops"})
        assert out["blueprints"] == [{
            "id": BP_OPS["id"], "name": "Ops", "type": "map", "computers_count": 1,
            "enrollment_code_active": False,
        }]
        assert out["total"] == 1
        assert out["next_offset"] is None

    def test_get_by_name_with_items(self, tenant):
        out = _call(tools_mod._tool_get_blueprint, _ctx(), {"blueprint": "Engineering"})
        assert out["blueprint"]["id"] == BP_ENG["id"]
        assert [i["name"] for i in out["library_items"]] == ["Zoom", "Office Wi-Fi"]
        assert out["library_item_count"] == 2

    def test_get_by_id(self, tenant):
        out = _call(tools_mod._tool_get_blueprint, _ctx(), {"blueprint": BP_OPS["id"]})
        assert out["blueprint"]["name"] == "Ops"
        assert tenant.calls[0] == ("GET", f"/api/v1/blueprints/{BP_OPS['id']}")

    def test_get_unknown_and_missing(self, tenant):
        assert _call(tools_mod._tool_get_blueprint, _ctx(), {})["error"] == "invalid_arguments"
        assert _call(tools_mod._tool_get_blueprint, _ctx(), {"blueprint": "nope"})["error"] == "iru_unknown_blueprint"
        out = _call(tools_mod._tool_get_blueprint, _ctx(),
                    {"blueprint": "00000000-0000-0000-0000-000000000000"})
        assert out["error"] == "iru_unknown_blueprint"


class TestLibraryItemStatus:
    def test_single_page(self, tenant):
        out = _call(tools_mod._tool_get_library_item_status, _ctx(), {
            "library_item_id": LIBRARY_ITEM_ZOOM, "limit": 2,
        })
        assert out["count"] == 2
        assert out["total"] == 3
        assert out["next_offset"] == 2
        assert out["by_status_in_page"] == {"PASS": 1, "ERROR": 1}
        row = out["statuses"][1]
        assert row["device_name"] == MAC_BUILD["device_name"]
        assert row["blueprint_name"] == "Ops"
        assert row["log_tail"] == "Downloading zoom\nfailed"

    def test_status_filter_scans_all_pages(self, tenant):
        out = _call(tools_mod._tool_get_library_item_status, _ctx(), {
            "library_item_id": LIBRARY_ITEM_ZOOM, "status": "error",
        })
        assert out["status_filter"] == "ERROR"
        assert out["scanned"] == 3
        assert out["total"] == 1
        assert [r["device_name"] for r in out["statuses"]] == [MAC_BUILD["device_name"]]
        # page size 2 -> two upstream pages were walked
        assert [c[1] for c in tenant.calls].count(f"/api/v1/library/library-items/{LIBRARY_ITEM_ZOOM}/status") == 2

    def test_device_restriction_resolves_serial(self, tenant):
        out = _call(tools_mod._tool_get_library_item_status, _ctx(), {
            "library_item_id": LIBRARY_ITEM_ZOOM, "device": MAC_ADA["serial_number"],
        })
        assert out["count"] == 1
        assert out["statuses"][0]["status"] == "PASS"
        sent = tenant.last_params[f"/api/v1/library/library-items/{LIBRARY_ITEM_ZOOM}/status"]
        assert sent["computer_id"] == MAC_ADA["device_id"]

    def test_requires_uuid(self, tenant):
        out = _call(tools_mod._tool_get_library_item_status, _ctx(), {"library_item_id": "Zoom"})
        assert out["error"] == "invalid_arguments"
        assert tenant.calls == []


# ---------------------------------------------------------------------------
# Users / tags
# ---------------------------------------------------------------------------

class TestUsers:
    def test_list_with_cursor(self, tenant):
        out = _call(tools_mod._tool_list_users, _ctx())
        assert out["count"] == 2
        assert out["next_cursor"] == "cD0y"
        assert out["users"][0] == {
            "id": USER_ADA["id"], "name": "Ada Lovelace", "email": "ada@acme.example",
            "active": True, "archived": False, "device_count": 1, "department": "Engineering",
            "job_title": "Engineer", "integration": {"name": "acme-okta", "type": "okta"},
            "created_at": "2024-09-06T21:00:04Z", "updated_at": "2024-09-06T21:00:04Z",
        }
        out2 = _call(tools_mod._tool_list_users, _ctx(), {"cursor": "cD0y"})
        assert [u["email"] for u in out2["users"]] == ["old@acme.example"]
        assert out2["next_cursor"] is None

    def test_archived_filter(self, tenant):
        out = _call(tools_mod._tool_list_users, _ctx(), {"archived": True})
        assert [u["email"] for u in out["users"]] == ["old@acme.example"]
        assert tenant.last_params["/api/v1/users"]["archived"] == "true"

    def test_get_by_email_with_devices(self, tenant):
        out = _call(tools_mod._tool_get_user, _ctx(), {"user": "ADA@acme.example"})
        assert out["user"]["id"] == USER_ADA["id"]
        assert [d["serial_number"] for d in out["devices"]] == [MAC_ADA["serial_number"]]
        assert out["device_count"] == 1
        assert tenant.last_params["/api/v1/devices"]["user_id"] == USER_ADA["id"]

    def test_get_by_id_and_unknown(self, tenant):
        out = _call(tools_mod._tool_get_user, _ctx(), {"user": USER_ADA["id"]})
        assert out["user"]["email"] == "ada@acme.example"
        assert _call(tools_mod._tool_get_user, _ctx(), {"user": "nobody@acme.example"})["error"] == "iru_unknown_user"
        assert _call(tools_mod._tool_get_user, _ctx(), {})["error"] == "invalid_arguments"

    def test_partial_email_is_ambiguous(self, tenant):
        out = _call(tools_mod._tool_get_user, _ctx(), {"user": "acme.example"})
        assert out["error"] == "iru_ambiguous_user"
        # Only the first cursor page (2 rows in the fake) is consulted.
        assert [c["email"] for c in out["candidates"]] == ["ada@acme.example", "bob@acme.example"]


class TestTags:
    def test_list_and_search(self, tenant):
        out = _call(tools_mod._tool_list_tags, _ctx())
        assert [t["name"] for t in out["tags"]] == ["engineering", "kiosk"]
        assert tenant.last_params["/api/v1/tags"] == {"search": ""}
        out = _call(tools_mod._tool_list_tags, _ctx(), {"search": "kio"})
        assert [t["name"] for t in out["tags"]] == ["kiosk"]


# ---------------------------------------------------------------------------
# Prism
# ---------------------------------------------------------------------------

class TestPrism:
    def test_unfiltered_first_page_reports_total(self, tenant):
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "FileVault"})
        assert out["category"] == "filevault"
        assert out["count"] == 2
        assert out["total_unfiltered"] == 2
        assert out["fields_available"] is None
        assert ("GET", "/api/v1/prism/count") in tenant.calls

    def test_filter_object_and_fields(self, tenant):
        out = _call(tools_mod._tool_prism_query, _ctx(), {
            "category": "filevault",
            "filter": {"filevault_enabled": {"eq": False}},
            "fields": ["device__name", "serial_number", "nonexistent"],
            "blueprint_ids": [BP_OPS["id"], BP_ENG["id"]],
            "device_families": "Mac",
            "sort_by": "-last_collected_at",
        })
        assert out["rows"] == [{"device__name": "Build Mac", "serial_number": "C02BBB222222"}]
        assert "filevault_enabled" in out["fields_available"]
        assert out["total_unfiltered"] is None
        sent = tenant.last_params["/api/v1/prism/filevault"]
        assert json.loads(sent["filter"]) == {"filevault_enabled": {"eq": False}}
        assert sent["blueprint_ids"] == f"{BP_OPS['id']},{BP_ENG['id']}"
        assert sent["device_families"] == "Mac"
        assert sent["sort_by"] == "-last_collected_at"
        assert ("GET", "/api/v1/prism/count") not in tenant.calls

    def test_filter_as_json_string(self, tenant):
        out = _call(tools_mod._tool_prism_query, _ctx(), {
            "category": "filevault", "filter": '{"filevault_enabled": {"eq": true}}',
        })
        assert [r["device__name"] for r in out["rows"]] == [MAC_ADA["device_name"]]

    def test_invalid_filter_and_category(self, tenant):
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "filevault", "filter": "not json"})
        assert out["error"] == "invalid_arguments"
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "filevault", "filter": "[1]"})
        assert out["error"] == "invalid_arguments"
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "secrets"})
        assert out["error"] == "invalid_arguments"
        assert tenant.calls == []

    def test_paging(self, tenant):
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "filevault", "limit": 1})
        assert out["count"] == 1
        assert out["next_offset"] == 1
        out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "filevault", "limit": 1, "offset": 1})
        assert out["rows"][0]["device__name"] == "Build Mac"
        assert out["next_offset"] == 1 + 1  # a full page: another may exist
        assert out["total_unfiltered"] is None

    def test_count_forbidden_is_tolerated(self):
        fake = FakeTenant(forbidden_paths={"/api/v1/prism/count"})
        with _use(fake):
            out = _call(tools_mod._tool_prism_query, _ctx(), {"category": "filevault"})
        assert out["count"] == 2
        assert out["total_unfiltered"] is None


# ---------------------------------------------------------------------------
# Threats / vulnerabilities / audit / ADE
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_threats(self, tenant):
        out = _call(tools_mod._tool_list_threats, _ctx(), {"status": "Quarantined", "date_range": "30"})
        assert out["count"] == 1
        assert out["total"] == 1
        assert out["malware_count"] == 1
        assert out["threats"][0]["threat_name"] == "malware_5"
        sent = tenant.last_params["/api/v1/threat-details"]
        assert sent["status"] == "quarantined"
        assert sent["date_range"] == "30"

    def test_vulnerabilities_paging(self, tenant):
        out = _call(tools_mod._tool_list_vulnerabilities, _ctx(), {"size": 1, "sort_by": "-cvss_score"})
        assert out["count"] == 1
        assert out["total"] == 2
        assert out["next_page"] == 2
        out2 = _call(tools_mod._tool_list_vulnerabilities, _ctx(), {"size": 1, "page": 2})
        assert out2["vulnerabilities"][0]["cve_id"] == "CVE-2025-1111"
        assert out2["next_page"] is None

    def test_vulnerabilities_filter(self, tenant):
        _call(tools_mod._tool_list_vulnerabilities, _ctx(), {"filter": {"severity": {"in": ["Critical"]}}})
        sent = tenant.last_params["/api/v1/vulnerability-management/vulnerabilities"]
        assert json.loads(sent["filter"]) == {"severity": {"in": ["Critical"]}}
        out = _call(tools_mod._tool_list_vulnerabilities, _ctx(), {"filter": "oops"})
        assert out["error"] == "invalid_arguments"

    def test_get_vulnerability(self, tenant):
        out = _call(tools_mod._tool_get_vulnerability, _ctx(), {"cve_id": "cve-2024-24795"})
        assert out["cve_id"] == "CVE-2024-24795"
        assert out["vulnerability"]["description"] == "A bad bug."
        assert out["devices"][0]["name"] == "Build Mac"
        assert out["devices_total"] == 1
        assert out["software"][0]["name"] == "macOS 13 Ventura"

    def test_get_vulnerability_bad_or_unknown(self, tenant):
        assert _call(tools_mod._tool_get_vulnerability, _ctx(), {"cve_id": "zoom"})["error"] == "invalid_arguments"
        out = _call(tools_mod._tool_get_vulnerability, _ctx(), {"cve_id": "CVE-1999-0001"})
        assert out["error"] == "iru_not_found"

    @pytest.mark.parametrize("cve_id", [
        "CVE-/../../devices",
        "CVE-2024-24795/../../../devices",
        "CVE-2024-24795?x=1",
        "CVE-2024-24795#frag",
        "CVE-2024-24795/devices",
        "CVE-2024-24795 x",
        "CVE-2024-24795\n/devices",
        "CVE-2024-",
        "CVE-24-24795",
    ])
    def test_get_vulnerability_rejects_ids_that_are_not_a_cve(self, tenant, cve_id):
        """The id is interpolated into the request path: nothing but a
        well-formed CVE id may reach the tenant."""
        tenant.last_params.clear()
        out = _call(tools_mod._tool_get_vulnerability, _ctx(), {"cve_id": cve_id})
        assert out["error"] == "invalid_arguments"
        assert tenant.last_params == {}


class TestAuditAndAde:
    def test_audit_events_cursor_and_compaction(self, tenant):
        out = _call(tools_mod._tool_list_audit_events, _ctx(), {"limit": 1, "start_date": "2026-09-01"})
        assert out["count"] == 1
        assert out["next_cursor"] == "page2"
        assert isinstance(out["events"][0]["new_state"], str)  # oversized -> truncated JSON
        sent = tenant.last_params["/api/v1/audit/events"]
        assert sent["sort_by"] == "-occurred_at"
        assert sent["start_date"] == "2026-09-01"
        out2 = _call(tools_mod._tool_list_audit_events, _ctx(), {"cursor": "page2"})
        assert out2["events"][0]["new_state"] == {"name": "Ops", "type": "map"}
        assert out2["next_cursor"] is None

    def test_ade_devices(self, tenant):
        out = _call(tools_mod._tool_list_ade_devices, _ctx(), {"profile_status": "Assigned"})
        assert out["count"] == 1
        assert out["total"] == 1
        assert out["next_page"] is None
        assert out["ade_devices"][0]["serial_number"] == "C02DDD444444"
        assert tenant.last_params["/api/v1/integrations/apple/ade/devices"]["profile_status"] == "assigned"
        out = _call(tools_mod._tool_list_ade_devices, _ctx(), {"profile_status": "pushed"})
        assert out["count"] == 0


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class TestViews:
    def test_app_row_android_shape(self):
        row = fleet.app_row({"display_name": "Slack", "package_name": "com.Slack",
                             "version_name": "25.1", "application_source": "INSTALLED_FROM_PLAY_STORE"})
        assert row == {"name": "Slack", "version": "25.1", "bundle_id": "com.Slack",
                       "source": "INSTALLED_FROM_PLAY_STORE"}

    def test_user_ref_shapes(self):
        assert fleet.user_ref("") is None
        assert fleet.user_ref("legacy string") == "legacy string"
        assert fleet.user_ref({"id": "1", "email": "", "name": None}) == {"id": "1"}
        assert fleet.user_ref({}) is None

    def test_truncation_helpers(self):
        assert fleet.truncate_text("abc", 5) == "abc"
        assert fleet.truncate_text("abcdefgh", 3).startswith("abc... [5 more chars]")
        assert fleet.tail_text("abcdefgh", 3).endswith("fgh")
        assert fleet.compact_json({"a": 1}) == {"a": 1}
        assert fleet.compact_json(None) is None
        assert fleet.compact_json(True) is True
        assert "truncated" in fleet.compact_json({"a": "x" * 5000})

    def test_project_fields(self):
        assert fleet.project_fields({"a": 1, "b": 2}, ["b", "zz"]) == {"b": 2}
        assert fleet.project_fields({"a": 1}, None) == {"a": 1}


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------

_TOOL_NAMES = {
    "iru_get_tenant_info",
    "iru_list_devices",
    "iru_get_device",
    "iru_list_blueprints",
    "iru_get_blueprint",
    "iru_get_library_item_status",
    "iru_list_users",
    "iru_get_user",
    "iru_list_tags",
    "iru_prism_query",
    "iru_list_threats",
    "iru_list_vulnerabilities",
    "iru_get_vulnerability",
    "iru_list_audit_events",
    "iru_list_ade_devices",
}


class TestPluginWiring:
    def test_manifest_shape(self, iru_plugin):
        assert iru_plugin.id == "iru"
        assert {t.spec["name"] for t in iru_plugin.tools} == _TOOL_NAMES
        assert all(t.requires_service == "iru" for t in iru_plugin.tools)
        # Read side only: no action requests, no authed_get entry, no secrets tool.
        assert iru_plugin.action_request_handlers == ()
        assert iru_plugin.services == ()
        assert not any("secret" in name for name in _TOOL_NAMES)
        assert iru_plugin.script_tool_allowlist == _TOOL_NAMES
        assert iru_plugin.user_connection.kind == "api_key"
        assert iru_plugin.user_connection.oauth_router is None
        assert iru_plugin.user_connection.key_placeholder == "Paste your Iru API token"
        assert [f.key for f in iru_plugin.credential_schema] == ["api_url"]
        assert iru_plugin.credential_schema[0].required is True
        assert Path(tools_mod.__file__).parent.joinpath("instructions.md").exists()

    def test_registered_into_core_registries(self, iru_plugin):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST, TOOL_CALL_REGISTRY
        from chat.system_skills import CATALOG

        for name in _TOOL_NAMES:
            assert name in TOOL_CALL_REGISTRY
            assert TOOL_CALL_REGISTRY[name]["requires_service"] == "iru"
            assert name in TOOL_CALL_HANDLERS
            assert name in SCRIPT_TOOL_CALL_ALLOWLIST
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST
        assert CATALOG["system:iru"].requires == "iru"
        body = CATALOG["system:iru"].content_builder("", "")
        for name in _TOOL_NAMES:
            assert name in body

    def test_admin_and_user_hooks(self, iru_plugin):
        from config.plugins import plugin_server_available

        assert iru_plugin.credential_validate({"api_url": "acme.api.iru.com"}) == {"api_url": API_URL}
        assert iru_plugin.user_connection.connected({"secret": TOKEN})
        assert not iru_plugin.user_connection.connected({"secret": ""})
        assert iru_plugin.user_connection.validate_key("short") is not None
        assert iru_plugin.user_connection.validate_key(TOKEN) is None
        with patch("config.service_credentials.read_service_credentials", return_value=None):
            assert plugin_server_available(iru_plugin) is False
        with patch("config.service_credentials.read_service_credentials", return_value=_CONFIG):
            assert plugin_server_available(iru_plugin) is True

    def test_tool_specs_are_well_formed(self, iru_plugin):
        for tool in iru_plugin.tools:
            spec = tool.spec
            params = spec["parameters"]
            assert params["type"] == "object"
            assert "intent_message" in params["properties"]
            for name in params["required"]:
                assert name in params["properties"]
            assert spec["description"]
