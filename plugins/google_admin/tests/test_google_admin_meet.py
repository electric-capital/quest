"""Tests for the Google Workspace Admin plugin's Google Meet support.

Covers:
* ``meet.py`` shaping: Reports API parameter flattening, the good / fair
  / poor quality verdict, endpoint rows, conference and quality summaries,
  the Meet hardware roster and its call-quality merge, usage totals.
* ``tools.py`` argument handling: time windows, meeting-code spellings,
  ``filters`` clauses.
* The four tool handlers against a faked Reports API: request shape
  (eventName, filters, window, partial-response fields, bearer token),
  paging and truncation, meeting-code retry, min_quality selection, the
  hardware roster + call-quality join, daily usage, and error mapping
  (not connected, pre-Meet grant, missing privilege, API disabled).

All HTTP calls are faked -- no real network traffic.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.google_admin import meet, tools
from plugins.google_admin.reports import REPORTS_BASE_URL
from plugins.google_admin.upstream import (
    GOOGLE_ADMIN_SCOPES,
    REPORTS_AUDIT_SCOPE,
    REPORTS_USAGE_SCOPE,
)

_ACTIVITY_MEET = f"{REPORTS_BASE_URL}/activity/users/all/applications/meet"
_ACTIVITY_HARDWARE = f"{REPORTS_BASE_URL}/activity/users/all/applications/meet_hardware"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _param(name, value):
    if isinstance(value, bool):
        return {"name": name, "boolValue": value}
    if isinstance(value, int):
        return {"name": name, "intValue": str(value)}
    if isinstance(value, dict):
        return {"name": name, "messageValue": {"parameter": [_param(k, v) for k, v in value.items()]}}
    return {"name": name, "value": value}


def _call_ended(time, **params):
    return {
        "id": {"time": time},
        "actor": {"email": params.get("identifier")} if params.get("identifier_type") == "email_address" else {},
        "events": [{
            "type": "call",
            "name": "call_ended",
            "parameters": [_param(k, v) for k, v in params.items()],
        }],
    }


def _hw_event(time, name, device_id="dev-1", display="Boardroom", serial="SN1", event_type="ISSUE", **extra):
    params = {"DEVICE_ID": device_id, "DEVICE_DISPLAY_NAME": display, "SERIAL_NUMBER": serial, **extra}
    return {
        "id": {"time": time},
        "events": [{
            "type": event_type,
            "name": name,
            "parameters": [_param(k, v) for k, v in params.items()],
        }],
    }


def _good_web(time="2026-10-01T10:30:00.000Z", **overrides):
    params = dict(
        conference_id="conf-1", meeting_code="abc-defg-hij", organizer_email="jane@example.com",
        identifier="jane@example.com", identifier_type="email_address", display_name="Jane",
        device_type="web", duration_seconds=1800, network_rtt_msec_mean=60,
        network_recv_jitter_msec_mean=8, audio_recv_packet_loss_mean=0,
        audio_send_packet_loss_mean=0, network_congestion=0, location_country="US",
        location_region="New York", network_transport_protocol="udp", end_call_reason="normal",
        ip_address="203.0.113.7",
    )
    params.update(overrides)
    return _call_ended(time, **params)


def _poor_room(time="2026-10-01T10:29:00.000Z", **overrides):
    params = dict(
        conference_id="conf-1", meeting_code="abc-defg-hij", organizer_email="jane@example.com",
        identifier="dev-1", identifier_type="device_id", display_name="Boardroom",
        device_type="chromebox", duration_seconds=1700, network_rtt_msec_mean=320,
        network_recv_jitter_msec_mean=12, audio_recv_packet_loss_mean=7,
        network_transport_protocol="tcp", end_call_reason="network_error",
    )
    params.update(overrides)
    return _call_ended(time, **params)


# ---------------------------------------------------------------------------
# Fake Reports API
# ---------------------------------------------------------------------------

def _response(status, body):
    response = MagicMock()
    response.status_code = status
    response.text = json.dumps(body) if not isinstance(body, str) else body
    response.json = MagicMock(return_value=body)
    return response


class FakeGoogle:
    """Answers ``client.get(url, params=, headers=)`` from a router."""

    def __init__(self, router):
        self.router = router
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None):
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "headers": dict(headers or {})})
        status, body = self.router(url, params)
        return _response(status, body)

    def patch(self):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=self)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return patch("plugins.google_admin.reports.httpx.AsyncClient", return_value=ctx)


def _user(scopes=GOOGLE_ADMIN_SCOPES):
    return {
        "id": 7,
        "email": "admin@example.com",
        "service_credentials": {
            "google_admin": {
                "oauth_blob": {
                    "access_token": "ya29.admin",
                    "refresh_token": "1//r",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "scopes": list(scopes),
                },
            },
        },
    }


def _ctx(user=None):
    return SimpleNamespace(user=user or _user())


def _call_tool(handler, args, fake=None, user=None):
    if fake is None:
        return json.loads(_run(handler(_ctx(user), args)))
    with fake.patch():
        return json.loads(_run(handler(_ctx(user), args)))


# ---------------------------------------------------------------------------
# Parameter flattening
# ---------------------------------------------------------------------------

class TestFlattenParameters:
    def test_every_value_kind(self):
        flat = meet.flatten_parameters([
            {"name": "n", "intValue": "42"},
            {"name": "b", "boolValue": True},
            {"name": "s", "value": "web"},
            {"name": "mv", "multiValue": ["a", "b"]},
            {"name": "mi", "multiIntValue": ["1", "2"]},
            {"name": "msg", "messageValue": {"parameter": [{"name": "inner", "intValue": "3"}]}},
            {"name": "mm", "multiMessageValue": [{"parameter": [{"name": "x", "value": "y"}]}]},
            {"name": "empty"},
            {"intValue": "1"},
        ])
        assert flat == {
            "n": 42, "b": True, "s": "web", "mv": ["a", "b"], "mi": [1, 2],
            "msg": {"inner": 3}, "mm": [{"x": "y"}], "empty": None,
        }

    def test_iter_events_keeps_order_and_filters_by_name(self):
        items = [
            {"id": {"time": "t2"}, "actor": {"email": "a@x"}, "events": [
                {"name": "call_ended", "type": "call", "parameters": []},
                {"name": "presentation_started", "parameters": []},
            ]},
            {"id": {"time": "t1"}, "events": [{"name": "call_ended", "parameters": []}]},
        ]
        events = list(meet.iter_events(items, "call_ended"))
        assert [(e["time"], e["actor"]) for e in events] == [("t2", "a@x"), ("t1", None)]


# ---------------------------------------------------------------------------
# Quality verdict
# ---------------------------------------------------------------------------

class TestAssessQuality:
    def test_clean_metrics_are_good(self):
        verdict, issues = meet.assess_quality({"network_rtt_msec_mean": 40, "audio_recv_packet_loss_mean": 0})
        assert verdict == "good" and issues == []

    @pytest.mark.parametrize("metric,value,expected", [
        ("audio_recv_packet_loss_mean", 1, "fair"),
        ("audio_recv_packet_loss_mean", 4, "fair"),
        ("audio_recv_packet_loss_mean", 5, "poor"),
        ("video_recv_packet_loss_mean", 1, "good"),
        ("video_recv_packet_loss_mean", 8, "poor"),
        ("network_recv_jitter_msec_mean", 30, "fair"),
        ("network_recv_jitter_msec_mean", 50, "poor"),
        ("network_rtt_msec_mean", 149, "good"),
        ("network_rtt_msec_mean", 150, "fair"),
        ("network_rtt_msec_mean", 300, "poor"),
        ("network_congestion", 20, "poor"),
    ])
    def test_thresholds(self, metric, value, expected):
        verdict, _ = meet.assess_quality({metric: value})
        assert verdict == expected

    def test_worst_issue_wins_and_every_issue_is_listed(self):
        verdict, issues = meet.assess_quality({
            "network_recv_jitter_msec_mean": 35, "audio_send_packet_loss_mean": 9,
        })
        assert verdict == "poor"
        assert {i["level"] for i in issues} == {"fair", "poor"}
        assert "audio send packet loss 9%" in [i["text"] for i in issues]

    @pytest.mark.parametrize("reason", ["network_error", "system_error"])
    def test_dropped_endpoint_is_poor(self, reason):
        verdict, issues = meet.assess_quality({"network_rtt_msec_mean": 20, "end_call_reason": reason})
        assert verdict == "poor"
        assert issues[0]["label"] == f"dropped ({reason})"

    def test_low_rating_is_fair(self):
        assert meet.assess_quality({"network_rtt_msec_mean": 20, "end_of_call_rating": 2})[0] == "fair"
        assert meet.assess_quality({"network_rtt_msec_mean": 20, "end_of_call_rating": 4})[0] == "good"

    def test_no_metrics_is_unknown(self):
        assert meet.assess_quality({"device_type": "pstn_in", "end_call_reason": "normal"}) == ("unknown", [])

    def test_thresholds_view_matches_table(self):
        view = meet.thresholds_view()
        assert len(view) == len(meet.QUALITY_THRESHOLDS)
        assert {"metric": "network_rtt_msec_mean", "fair_at": 150, "poor_at": 300, "unit": "ms"} in view


# ---------------------------------------------------------------------------
# Endpoint rows and summaries
# ---------------------------------------------------------------------------

def _rows(*items, include_all_metrics=False):
    return [
        meet.endpoint_row(e, include_all_metrics=include_all_metrics)
        for e in meet.iter_events(items, "call_ended")
    ]


class TestEndpointRow:
    def test_compact_row(self):
        (row,) = _rows(_good_web())
        public = meet.public_row(row)
        assert public["participant"] == "jane@example.com"
        assert public["joined_at"] == "2026-10-01T10:00:00Z"
        assert public["left_at"] == "2026-10-01T10:30:00Z"
        assert public["location"] == "New York, US"
        assert public["transport"] == "udp"
        assert public["quality"] == "good"
        # Defaults stay quiet: normal end, internal participant, no IP.
        for key in ("end_call_reason", "is_external", "issues", "ip_address", "_issue_labels"):
            assert key not in public
        assert set(public["metrics"]) <= set(meet.CORE_METRICS)

    def test_all_metrics_adds_every_metric_and_ip(self):
        (row,) = _rows(_good_web(screencast_send_seconds=30), include_all_metrics=True)
        assert row["ip_address"] == "203.0.113.7"
        assert row["metrics"]["screencast_send_seconds"] == 30
        assert "network_transport_protocol" not in row["metrics"]

    def test_poor_row_lists_reasons(self):
        (row,) = _rows(_poor_room())
        assert row["quality"] == "poor"
        assert row["end_call_reason"] == "network_error"
        assert "round-trip time 320 ms" in row["issues"]
        assert meet.is_hardware_row(row)

    def test_participant_falls_back_to_actor(self):
        item = _good_web()
        params = item["events"][0]["parameters"]
        item["events"][0]["parameters"] = [p for p in params if p["name"] != "identifier"]
        (row,) = _rows(item)
        assert row["participant"] == "jane@example.com"

    def test_sort_worst_first(self):
        rows = _rows(
            _good_web(time="2026-10-01T11:00:00Z"),
            _good_web(time="2026-10-01T10:00:00Z", network_recv_jitter_msec_mean=40),
            _poor_room(),
            _call_ended("2026-10-01T09:00:00Z", conference_id="c2", device_type="pstn_in"),
        )
        assert [r["quality"] for r in meet.sort_worst_first(rows)] == ["poor", "fair", "good", "unknown"]


class TestSummaries:
    def test_conferences_group_and_sort(self):
        rows = _rows(
            _good_web(),
            _poor_room(),
            _good_web(time="2026-10-01T09:00:00Z", conference_id="conf-0", identifier="ext@other.com",
                      is_external=True),
        )
        conferences = meet.summarize_conferences(rows)
        assert [c["conference_id"] for c in conferences] == ["conf-1", "conf-0"]
        first = conferences[0]
        assert first["endpoints"] == 2
        assert first["organizer_email"] == "jane@example.com"
        assert first["started_at"] == "2026-10-01T10:00:00Z"
        assert first["ended_at"] == "2026-10-01T10:30:00Z"
        assert first["device_types"] == {"web": 1, "chromebox": 1}
        assert first["meet_hardware"] == ["Boardroom"]
        assert first["quality"] == {"poor": 1, "good": 1}
        assert first["worst_endpoints"][0].startswith("Boardroom: ")
        assert conferences[1]["external_participants"] == 1

    def test_quality_summary(self):
        rows = _rows(_good_web(), _poor_room(), _good_web(identifier="b@example.com", network_rtt_msec_mean=100))
        summary = meet.summarize_quality(rows)
        assert summary["endpoints"] == 3
        assert summary["conferences"] == 1
        assert summary["participants"] == 3
        assert summary["by_quality"] == {"poor": 1, "good": 2}
        assert summary["by_device_type"]["chromebox"] == {"endpoints": 1, "poor": 1}
        assert summary["by_transport"]["udp"]["endpoints"] == 2
        rtt = summary["metrics"]["network_rtt_msec_mean"]
        assert rtt == {"median": 100, "p90": 320, "max": 320, "endpoints": 3}
        issues = {i["issue"] for i in summary["most_common_issues"]}
        assert {"round-trip time", "audio receive packet loss", "dropped (network_error)"} <= issues

    def test_empty_summary(self):
        assert meet.summarize_quality([]) == {"endpoints": 0, "conferences": 0, "participants": 0}


# ---------------------------------------------------------------------------
# Meet hardware
# ---------------------------------------------------------------------------

def _hw_rows(*items):
    return [meet.hardware_event_row(e) for e in meet.iter_events(items)]


class TestHardwareRoster:
    def test_newest_state_wins(self):
        rows = _hw_rows(
            # newest first, as the API returns them
            _hw_event("2026-10-02T09:00:00Z", "EVENT_MEET_CALL_JOINED", event_type="ACTIVITY",
                      EVENT_DATA={"meeting_code": "abc-defg-hij"}),
            _hw_event("2026-10-02T08:00:00Z", "EVENT_CAMERA_DETACHED", AFFECTED_PERIPHERAL={"name": "Huddly"}),
            _hw_event("2026-10-02T07:00:00Z", "EVENT_MIC_ATTACHED"),
            _hw_event("2026-10-01T07:00:00Z", "EVENT_CAMERA_ATTACHED"),
            _hw_event("2026-10-01T06:00:00Z", "EVENT_DEVICE_MISSING"),
            _hw_event("2026-10-01T05:00:00Z", "EVENT_RESTART_APP", event_type="RESTART"),
            _hw_event("2026-10-01T04:00:00Z", "EVENT_OS_UPDATE", event_type="SOFTWARE_UPDATE"),
            _hw_event("2026-10-01T03:00:00Z", "EVENT_FRONTEND_LOAD_TIMEOUT_ERROR"),
            _hw_event("2026-10-01T02:00:00Z", "EVENT_ZOOM_CALL_JOINED", event_type="ACTIVITY"),
            _hw_event("2026-10-01T01:00:00Z", "EVENT_FEEDBACK_FILED", event_type="FEEDBACK_FILED"),
        )
        assert rows[0]["data"] == {"meeting_code": "abc-defg-hij"}
        assert rows[1]["peripheral"] == {"name": "Huddly"}

        device = meet.summarize_hardware(rows)["dev-1"]
        assert device["display_name"] == "Boardroom"
        assert device["serial_number"] == "SN1"
        assert device["events"] == 10
        assert device["last_event"] == {"event": "EVENT_MEET_CALL_JOINED", "time": "2026-10-02T09:00:00Z"}
        assert device["peripherals"]["CAMERA"] == {"state": "detached", "since": "2026-10-02T08:00:00Z"}
        assert device["peripherals"]["MIC"]["state"] == "attached"
        assert device["presence"]["state"] == "missing"
        assert device["in_call"] == {"platform": "MEET", "since": "2026-10-02T09:00:00Z"}
        assert device["calls_joined"] == {"MEET": 1, "ZOOM": 1}
        assert device["restarts"] == 1
        assert device["software_updates"] == 1
        assert device["app_load_errors"] == 1
        assert device["feedback_filed"] == 1
        concerns = " | ".join(device["concerns"])
        assert "reported missing" in concerns
        assert "CAMERA detached" in concerns
        assert "MIC" not in concerns
        assert "app load error" in concerns

    def test_found_after_missing_is_not_a_concern(self):
        rows = _hw_rows(
            _hw_event("2026-10-02T09:00:00Z", "EVENT_DEVICE_FOUND"),
            _hw_event("2026-10-02T08:00:00Z", "EVENT_DEVICE_MISSING"),
            _hw_event("2026-10-02T07:00:00Z", "EVENT_MEET_CALL_DISCONNECTED", event_type="ACTIVITY"),
        )
        device = meet.summarize_hardware(rows)["dev-1"]
        assert device["presence"]["state"] == "found"
        assert "in_call" not in device
        assert device["concerns"] == []

    def test_devices_are_keyed_separately(self):
        rows = _hw_rows(
            _hw_event("t2", "EVENT_RESTART_APP", device_id="dev-2", display="Huddle"),
            _hw_event("t1", "EVENT_RESTART_APP"),
        )
        assert set(meet.summarize_hardware(rows)) == {"dev-1", "dev-2"}

    def test_call_quality_merge_by_id_name_and_call_only(self):
        devices = meet.summarize_hardware(_hw_rows(
            _hw_event("t2", "EVENT_RESTART_APP"),
            _hw_event("t1", "EVENT_RESTART_APP", device_id="hw-9", display="Huddle"),
        ))
        quality = meet.hardware_call_quality(_rows(
            _poor_room(),                                                     # dev-1 by id
            _poor_room(identifier="other-id-9", display_name="huddle",       # Huddle by name
                       end_call_reason="normal", audio_recv_packet_loss_mean=0, network_rtt_msec_mean=50),
            _poor_room(identifier="dev-3", display_name="Lobby"),            # only in calls
            _good_web(),                                                      # not hardware
        ))
        assert set(quality) == {"dev-1", "other-id-9", "dev-3"}

        meet.merge_hardware_call_quality(devices, quality)
        assert devices["dev-1"]["call_quality"]["quality"] == {"poor": 1}
        assert any("poor quality" in c for c in devices["dev-1"]["concerns"])
        assert devices["hw-9"]["call_quality"]["quality"] == {"good": 1}
        assert not any("poor quality" in c for c in devices["hw-9"]["concerns"])
        assert devices["dev-3"]["display_name"] == "Lobby"
        assert devices["dev-3"]["device_id"] == "dev-3"
        assert "no meet_hardware log events" in devices["dev-3"]["note"]


class TestUsage:
    def test_values_strip_prefix_and_parse_ints(self):
        body = {"usageReports": [{"date": "2026-09-30", "parameters": [
            {"name": "meet:num_calls", "intValue": "120"},
            {"name": "meet:average_meeting_minutes", "intValue": "31"},
        ]}]}
        assert meet.usage_values(body) == {"num_calls": 120, "average_meeting_minutes": 31}

    def test_totals_skip_non_additive(self):
        days = [
            {"date": "d1", "values": {"num_calls": 10, "average_meeting_minutes": 30, "num_30day_active_users": 5}},
            {"date": "d2", "values": {"num_calls": 5, "average_meeting_minutes": 20, "num_30day_active_users": 6}},
            {"date": "d3", "note": "not yet available"},
        ]
        totals = meet.usage_totals(days, ["num_calls", "average_meeting_minutes", "num_30day_active_users"])
        assert totals == {"num_calls": 15}


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def fixed_now():
    with patch.object(tools, "_now", return_value=_FIXED_NOW):
        yield


class TestResolveWindow:
    def test_default_lookback(self, fixed_now):
        assert tools.resolve_window({}, default_days=7) == ("2026-09-25T12:00:00Z", None)

    def test_days(self, fixed_now):
        assert tools.resolve_window({"days": 2}, default_days=7)[0] == "2026-09-30T12:00:00Z"

    def test_dates_cover_whole_days(self, fixed_now):
        start, end = tools.resolve_window(
            {"start_time": "2026-09-01", "end_time": "2026-09-30"}, default_days=7,
        )
        assert (start, end) == ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")

    def test_days_counts_back_from_end(self, fixed_now):
        start, end = tools.resolve_window({"end_time": "2026-09-30T10:00:00Z", "days": 1}, default_days=7)
        assert (start, end) == ("2026-09-29T10:00:00Z", "2026-09-30T10:00:00Z")

    def test_future_end_is_dropped(self, fixed_now):
        assert tools.resolve_window({"end_time": "2026-10-02"}, default_days=1)[1] is None

    @pytest.mark.parametrize("args", [
        {"start_time": "2026-10-03"},
        {"start_time": "2026-09-30", "end_time": "2026-09-29"},
        {"start_time": "2026-01-01"},
        {"start_time": "yesterday"},
        {"days": "seven"},
        {"days": 0},
    ])
    def test_invalid_windows(self, fixed_now, args):
        with pytest.raises(ValueError):
            tools.resolve_window(args, default_days=7)

    def test_days_capped(self, fixed_now):
        assert tools.resolve_window({"days": 999}, default_days=7)[0] == "2026-04-05T12:00:00Z"


class TestMeetingCodeVariants:
    @pytest.mark.parametrize("raw", [
        "abc-defg-hij", "ABCDEFGHIJ", "abc defg hij", "https://meet.google.com/abc-defg-hij?authuser=1",
    ])
    def test_ten_letter_codes(self, raw):
        assert tools.meeting_code_variants(raw) == ["abc-defg-hij", "ABCDEFGHIJ", "abcdefghij"]

    def test_other_codes_are_tried_as_given(self):
        assert tools.meeting_code_variants("my-room") == ["my-room", "MY-ROOM"]


class TestCallFilters:
    def test_clauses(self):
        clauses = tools._call_filters({
            "conference_id": "conf-1", "organizer_email": "jane@example.com",
            "participant": "sam@example.com", "device_type": "WEB",
        })
        assert clauses == [
            "conference_id==conf-1", "organizer_email==jane@example.com",
            "identifier==sam@example.com", "device_type==web",
        ]

    def test_meet_hardware_pseudo_type(self):
        assert tools._call_filters({"device_type": "meet_hardware"}) == ["identifier_type==device_id"]

    @pytest.mark.parametrize("args", [
        {"device_type": "laptop"},
        {"participant": "a@x.com,b@x.com"},
    ])
    def test_rejects(self, args):
        with pytest.raises(ValueError):
            tools._call_filters(args)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

class TestMeetCallsTool:
    def test_request_shape_and_grouping(self, fixed_now):
        fake = FakeGoogle(lambda url, params: (200, {"items": [_good_web(), _poor_room()]}))
        out = _call_tool(tools._tool_meet_calls, {"organizer_email": "jane@example.com", "days": 1}, fake)

        (call,) = fake.calls
        assert call["url"] == _ACTIVITY_MEET
        assert call["headers"]["Authorization"] == "Bearer ya29.admin"
        assert call["params"]["eventName"] == "call_ended"
        assert call["params"]["filters"] == "organizer_email==jane@example.com"
        assert call["params"]["startTime"] == "2026-10-01T12:00:00Z"
        assert "endTime" not in call["params"]
        assert call["params"]["maxResults"] == 1000
        assert "items(" in call["params"]["fields"] and "nextPageToken" in call["params"]["fields"]

        assert out["endpoints_scanned"] == 2
        assert out["conferences_found"] == 1
        assert out["conferences"][0]["meeting_code"] == "abc-defg-hij"
        assert out["filters"] == {"organizer_email": "jane@example.com"}
        assert "truncated" not in out["window"]

    def test_meeting_code_spellings_are_tried_until_one_matches(self, fixed_now):
        def router(url, params):
            if params["filters"] == "meeting_code==ABCDEFGHIJ":
                return 200, {"items": [_good_web(meeting_code="ABCDEFGHIJ")]}
            return 200, {}
        fake = FakeGoogle(router)
        out = _call_tool(tools._tool_meet_calls, {"meeting_code": "meet.google.com/abc-defg-hij"}, fake)
        assert [c["params"]["filters"] for c in fake.calls] == [
            "meeting_code==abc-defg-hij", "meeting_code==ABCDEFGHIJ",
        ]
        assert out["filters"]["meeting_code"] == "ABCDEFGHIJ"
        assert out["conferences_found"] == 1

    def test_unmatched_meeting_code_explains(self, fixed_now):
        fake = FakeGoogle(lambda url, params: (200, {}))
        out = _call_tool(tools._tool_meet_calls, {"meeting_code": "abc-defg-hij"}, fake)
        assert len(fake.calls) == 3
        assert out["conferences"] == [] and "No call records matched" in out["note"]

    def test_pages_are_followed_then_truncated(self, fixed_now):
        def router(url, params):
            number = int(params.get("pageToken", "0"))
            minute = 59 - number
            return 200, {
                "items": [_good_web(time=f"2026-10-02T11:{minute:02d}:00Z", conference_id=f"c{number}")],
                "nextPageToken": str(number + 1),
            }
        fake = FakeGoogle(router)
        out = _call_tool(tools._tool_meet_calls, {}, fake)
        assert len(fake.calls) == tools.CALL_SCAN_PAGES
        assert [c["params"].get("pageToken") for c in fake.calls] == [None, "1", "2", "3", "4"]
        assert out["window"]["truncated"] is True
        assert out["window"]["covered_from"] == "2026-10-02T11:55:00Z"
        assert out["conferences_found"] == tools.CALL_SCAN_PAGES

    def test_limit(self, fixed_now):
        items = [_good_web(conference_id=f"c{i}") for i in range(4)]
        fake = FakeGoogle(lambda url, params: (200, {"items": items}))
        out = _call_tool(tools._tool_meet_calls, {"limit": 2}, fake)
        assert len(out["conferences"]) == 2 and out["conferences_omitted"] == 2

    def test_invalid_arguments_make_no_request(self, fixed_now):
        fake = FakeGoogle(lambda url, params: (200, {}))
        out = _call_tool(tools._tool_meet_calls, {"device_type": "laptop"}, fake)
        assert out["error"] == "invalid_arguments"
        assert fake.calls == []


class TestCallQualityTool:
    def _fake(self):
        items = [
            _good_web(),
            _good_web(identifier="b@example.com", network_recv_jitter_msec_mean=40),
            _poor_room(),
        ]
        return FakeGoogle(lambda url, params: (200, {"items": items}))

    def test_rows_worst_first_with_summary_and_thresholds(self, fixed_now):
        out = _call_tool(tools._tool_meet_call_quality, {"conference_id": "conf-1"}, self._fake())
        assert [r["quality"] for r in out["endpoints"]] == ["poor", "fair", "good"]
        assert all("_issue_labels" not in r for r in out["endpoints"])
        assert out["summary"]["by_quality"] == {"poor": 1, "fair": 1, "good": 1}
        assert out["thresholds"] == meet.thresholds_view()
        assert out["filters"] == {"conference_id": "conf-1"}

    def test_min_quality_keeps_summary_over_all_rows(self, fixed_now):
        out = _call_tool(tools._tool_meet_call_quality, {"min_quality": "poor"}, self._fake())
        assert [r["participant"] for r in out["endpoints"]] == ["dev-1"]
        assert out["summary"]["endpoints"] == 3

        out = _call_tool(tools._tool_meet_call_quality, {"min_quality": "fair"}, self._fake())
        assert len(out["endpoints"]) == 2

    def test_participant_and_device_filters(self, fixed_now):
        fake = self._fake()
        _call_tool(tools._tool_meet_call_quality,
                   {"participant": "sam@example.com", "device_type": "meet_hardware"}, fake)
        assert fake.calls[0]["params"]["filters"] == "identifier==sam@example.com,identifier_type==device_id"

    def test_include_all_metrics(self, fixed_now):
        out = _call_tool(tools._tool_meet_call_quality, {"include_all_metrics": True}, self._fake())
        assert any(r.get("ip_address") for r in out["endpoints"])

    def test_bad_min_quality(self, fixed_now):
        out = _call_tool(tools._tool_meet_call_quality, {"min_quality": "awful"})
        assert out["error"] == "invalid_arguments"


class TestHardwareTool:
    def _router(self, url, params):
        if url == _ACTIVITY_HARDWARE:
            return 200, {"items": [
                _hw_event("2026-10-02T09:00:00Z", "EVENT_CAMERA_DETACHED"),
                _hw_event("2026-10-02T08:00:00Z", "EVENT_MEET_CALL_JOINED", device_id="dev-2",
                          display="Huddle", serial="SN2", event_type="ACTIVITY"),
                _hw_event("2026-10-02T07:00:00Z", "EVENT_RESTART_APP"),
            ]}
        assert url == _ACTIVITY_MEET
        return 200, {"items": [_poor_room(), _poor_room(identifier="dev-2", display_name="Huddle",
                                                        end_call_reason="normal", audio_recv_packet_loss_mean=0,
                                                        network_rtt_msec_mean=40)]}

    def test_roster_with_call_quality(self, fixed_now):
        fake = FakeGoogle(self._router)
        out = _call_tool(tools._tool_meet_hardware, {}, fake)

        hw_call, meet_call = fake.calls
        assert hw_call["url"] == _ACTIVITY_HARDWARE
        assert "eventName" not in hw_call["params"] and "filters" not in hw_call["params"]
        assert meet_call["params"]["eventName"] == "call_ended"
        assert meet_call["params"]["filters"] == "identifier_type==device_id"

        assert out["devices_found"] == 2
        assert out["devices_with_concerns"] == 1
        boardroom, huddle = out["devices"]
        assert boardroom["display_name"] == "Boardroom"
        assert any("CAMERA detached" in c for c in boardroom["concerns"])
        assert boardroom["call_quality"]["quality"] == {"poor": 1}
        assert huddle["call_quality"]["quality"] == {"good": 1}
        assert "concerns" not in huddle
        assert "events" not in out

    def test_device_filter_returns_its_events(self, fixed_now):
        fake = FakeGoogle(self._router)
        out = _call_tool(tools._tool_meet_hardware, {"device": "board", "include_call_quality": False}, fake)
        assert len(fake.calls) == 1
        assert [d["display_name"] for d in out["devices"]] == ["Boardroom"]
        assert [e["event"] for e in out["events"]] == ["EVENT_CAMERA_DETACHED", "EVENT_RESTART_APP"]

    def test_device_filter_by_serial(self, fixed_now):
        out = _call_tool(tools._tool_meet_hardware, {"device": "SN2"}, FakeGoogle(self._router))
        assert [d["display_name"] for d in out["devices"]] == ["Huddle"]

    def test_no_activity(self, fixed_now):
        out = _call_tool(tools._tool_meet_hardware, {}, FakeGoogle(lambda url, params: (200, {})))
        assert out["devices"] == [] and "No Meet hardware activity" in out["note"]


class TestUsageTool:
    def test_daily_values_and_totals(self):
        def router(url, params):
            day = url.rsplit("/", 1)[1]
            if day == "2026-09-30":
                return 400, {"error": {"code": 400, "message": "Data for dates later than 2026-09-29 is not yet available."}}
            return 200, {"usageReports": [{"date": day, "parameters": [
                {"name": "meet:num_calls", "intValue": "10"},
                {"name": "meet:average_meeting_minutes", "intValue": "25"},
            ]}]}
        fake = FakeGoogle(router)
        out = _call_tool(tools._tool_meet_usage, {
            "start_date": "2026-09-28", "end_date": "2026-09-30",
            "parameters": ["num_calls", "meet:average_meeting_minutes"],
        }, fake)

        assert sorted(c["url"].rsplit("/", 1)[1] for c in fake.calls) == ["2026-09-28", "2026-09-29", "2026-09-30"]
        assert all(c["params"] == {"parameters": "meet:num_calls,meet:average_meeting_minutes"} for c in fake.calls)
        assert out["parameters"] == ["num_calls", "average_meeting_minutes"]
        assert [d["date"] for d in out["days"]] == ["2026-09-28", "2026-09-29", "2026-09-30"]
        assert out["days"][0]["values"] == {"num_calls": 10, "average_meeting_minutes": 25}
        assert "not yet available" in out["days"][2]["note"]
        assert out["totals"] == {"num_calls": 20}

    def test_default_parameters_and_window(self):
        fake = FakeGoogle(lambda url, params: (200, {"usageReports": []}))
        out = _call_tool(tools._tool_meet_usage, {"days": 3}, fake)
        assert len(fake.calls) == 3
        assert out["parameters"] == list(meet.DEFAULT_USAGE_PARAMETERS)

    def test_privilege_error_is_reported_once(self):
        fake = FakeGoogle(lambda url, params: (403, {"error": {"code": 403, "message": "Not Authorized to access this resource/api"}}))
        out = _call_tool(tools._tool_meet_usage, {"days": 2}, fake)
        assert out["error"] == "google_admin_forbidden"
        assert "Reports" in out["message"]

    def test_identical_rejection_for_every_day_is_one_error(self):
        fake = FakeGoogle(lambda url, params: (400, {"error": {"message": "Invalid parameter meet:num_bogus"}}))
        out = _call_tool(tools._tool_meet_usage, {"days": 3, "parameters": ["num_bogus"]}, fake)
        assert out == {"error": "google_admin_bad_request", "message": "Invalid parameter meet:num_bogus",
                       "status_code": 400}

    def test_days_are_capped(self):
        fake = FakeGoogle(lambda url, params: (200, {}))
        _call_tool(tools._tool_meet_usage, {"days": 200}, fake)
        assert len(fake.calls) == tools.MAX_USAGE_DAYS

    @pytest.mark.parametrize("args", [
        {"start_date": "2026/09/01"},
        {"start_date": "2026-01-01", "end_date": "2026-09-01"},
        {"parameters": ["num calls"]},
    ])
    def test_invalid_arguments(self, args):
        out = _call_tool(tools._tool_meet_usage, args)
        assert out["error"] == "invalid_arguments"


class TestErrors:
    @pytest.mark.parametrize("handler", [
        tools._tool_meet_calls, tools._tool_meet_call_quality, tools._tool_meet_hardware,
    ])
    def test_not_connected(self, fixed_now, handler):
        fake = FakeGoogle(lambda url, params: (200, {}))
        out = _call_tool(handler, {}, fake, user={"id": 1, "email": "u@example.com"})
        assert out["error"] == "google_admin_oauth_required"
        assert fake.calls == []

    def test_pre_meet_grant_gets_reconnect_hint_without_a_request(self, fixed_now):
        old_grant = [s for s in GOOGLE_ADMIN_SCOPES if s not in (REPORTS_AUDIT_SCOPE, REPORTS_USAGE_SCOPE)]
        fake = FakeGoogle(lambda url, params: (200, {}))
        out = _call_tool(tools._tool_meet_call_quality, {}, fake, user=_user(scopes=old_grant))
        assert out["error"] == "google_admin_reconnect_required"
        assert fake.calls == []
        out = _call_tool(tools._tool_meet_usage, {}, fake, user=_user(scopes=old_grant))
        assert out["error"] == "google_admin_reconnect_required"

    @pytest.mark.parametrize("status,body,code", [
        (403, {"error": {"message": "Request had insufficient authentication scopes.",
                         "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}},
         "google_admin_reconnect_required"),
        (403, {"error": {"message": "Admin SDK API has not been used in project 123",
                         "details": [{"reason": "SERVICE_DISABLED"}]}},
         "google_admin_api_disabled"),
        (403, {"error": {"message": "Not Authorized to access this resource/api"}}, "google_admin_forbidden"),
        (401, {"error": {"message": "Invalid Credentials"}}, "google_admin_oauth_required"),
        (429, {"error": {"message": "Quota exceeded"}}, "google_admin_rate_limited"),
        (400, {"error": {"message": "Bad filter"}}, "google_admin_bad_request"),
        (503, "upstream down", "google_admin_upstream_error"),
    ])
    def test_upstream_errors_are_mapped(self, fixed_now, status, body, code):
        fake = FakeGoogle(lambda url, params: (status, body))
        out = _call_tool(tools._tool_meet_calls, {}, fake)
        assert out["error"] == code
        assert out["status_code"] == status


class TestRegistration:
    def test_tools_are_registered_and_gated(self, google_admin_plugin):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        for name in tools.GOOGLE_ADMIN_TOOL_NAMES:
            assert name in TOOL_CALL_REGISTRY, name
        assert all(tool.requires_service == "google_admin" for tool in tools.GOOGLE_ADMIN_TOOLS)
        assert not any(tool.mutating for tool in tools.GOOGLE_ADMIN_TOOLS)

    def test_tools_are_in_the_script_bridge(self, google_admin_plugin):
        assert google_admin_plugin.script_tool_allowlist == frozenset(tools.GOOGLE_ADMIN_TOOL_NAMES)
