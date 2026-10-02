"""Shaping Google Meet audit records into compact, model-friendly views.

Pure functions over Reports API ``activities.list`` items (no I/O), used by
plugins/google_admin/tools.py:

- ``call_ended`` records of the ``meet`` audit log become one endpoint row
  per participant session, with a curated metric subset and a
  good / fair / poor quality verdict (:data:`QUALITY_THRESHOLDS`);
- endpoint rows roll up into conferences and into a quality summary;
- ``meet_hardware`` audit records become event rows and a per-device
  health roster (there is no Meet hardware inventory API, so the roster
  is whatever the log saw in the window).

Reports API parameters arrive as ``{name, value | intValue | boolValue |
multiValue | multiIntValue | messageValue | multiMessageValue}`` with
int64 values encoded as strings; :func:`flatten_parameters` turns them
into a plain dict.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Parameters and events
# ---------------------------------------------------------------------------


def _to_int(raw: Any) -> Any:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def param_value(param: dict) -> Any:
    """The value of one Reports API event parameter."""
    if "intValue" in param:
        return _to_int(param["intValue"])
    if "boolValue" in param:
        return bool(param["boolValue"])
    if "value" in param:
        return param["value"]
    if "multiValue" in param:
        return list(param["multiValue"] or [])
    if "multiIntValue" in param:
        return [_to_int(v) for v in param["multiIntValue"] or []]
    if "messageValue" in param:
        return flatten_parameters((param["messageValue"] or {}).get("parameter"))
    if "multiMessageValue" in param:
        return [
            flatten_parameters((message or {}).get("parameter"))
            for message in param["multiMessageValue"] or []
        ]
    return None


def flatten_parameters(params: Optional[list]) -> dict:
    """``[{name, ...Value}, ...]`` -> ``{name: value}``."""
    out: dict[str, Any] = {}
    for param in params or []:
        name = param.get("name") if isinstance(param, dict) else None
        if name:
            out[name] = param_value(param)
    return out


def iter_events(items: Iterable[dict], event_name: Optional[str] = None):
    """Flatten activity items into ``{time, actor, name, type, params}``.

    One activity item can carry several events; order (newest first) is
    preserved.
    """
    for item in items:
        time = (item.get("id") or {}).get("time")
        actor = (item.get("actor") or {}).get("email")
        for event in item.get("events") or []:
            if event_name and event.get("name") != event_name:
                continue
            yield {
                "time": time,
                "actor": actor,
                "name": event.get("name"),
                "type": event.get("type"),
                "params": flatten_parameters(event.get("parameters")),
            }


def parse_time(value: Any) -> Optional[datetime]:
    """An RFC 3339 timestamp as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drop_empty(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None and v != [] and v != {}}


# ---------------------------------------------------------------------------
# Call quality
# ---------------------------------------------------------------------------

# Quest heuristics, not Google figures (Google publishes no per-call
# thresholds): common VoIP rules of thumb. Each row: metric, label, unit,
# fair-at, poor-at. A value at or above ``poor_at`` makes the endpoint
# poor, at or above ``fair_at`` fair.
QUALITY_THRESHOLDS = (
    ("audio_recv_packet_loss_mean", "audio receive packet loss", "%", 1, 5),
    ("audio_send_packet_loss_mean", "audio send packet loss", "%", 1, 5),
    ("video_recv_packet_loss_mean", "video receive packet loss", "%", 2, 8),
    ("video_send_packet_loss_mean", "video send packet loss", "%", 2, 8),
    ("network_recv_jitter_msec_mean", "receive jitter", " ms", 30, 50),
    ("network_send_jitter_msec_mean", "send jitter", " ms", 30, 50),
    ("network_rtt_msec_mean", "round-trip time", " ms", 150, 300),
    ("network_congestion", "network congestion", "%", 5, 20),
)

# ``end_call_reason`` values meaning the endpoint was dropped.
_DROP_REASONS = ("network_error", "system_error")

# An end-of-call rating at or below this counts against the endpoint.
_LOW_RATING = 2

QUALITY_RANK = {"poor": 0, "fair": 1, "good": 2, "unknown": 3}

# ``device_type`` values Google labels as Meet hardware. Android-based
# room kits may report another value; ``identifier_type == device_id`` is
# the reliable Meet hardware marker.
HARDWARE_DEVICE_TYPES = ("chromebox", "chromebase")

DEVICE_TYPES = (
    "android", "chromebase", "chromebox", "interop", "ios", "jamboard",
    "other_client", "pstn_in", "pstn_out", "smart_display", "web",
)

# The per-endpoint metrics returned by default (``include_all_metrics``
# returns every audio_/video_/screencast_/network_ value instead).
CORE_METRICS = (
    "network_rtt_msec_mean",
    "network_recv_jitter_msec_mean",
    "network_recv_jitter_msec_max",
    "network_send_jitter_msec_mean",
    "network_congestion",
    "network_estimated_download_kbps_mean",
    "network_estimated_upload_kbps_mean",
    "audio_recv_packet_loss_mean",
    "audio_recv_packet_loss_max",
    "audio_send_packet_loss_mean",
    "audio_send_packet_loss_max",
    "video_recv_packet_loss_mean",
    "video_send_packet_loss_mean",
    "video_recv_fps_mean",
    "video_send_fps_mean",
    "video_recv_long_side_median_pixels",
    "video_send_long_side_median_pixels",
)

_METRIC_PREFIXES = ("audio_", "video_", "screencast_", "network_")

# Metrics summarized (median / p90 / max) across endpoints.
SUMMARY_METRICS = (
    "network_rtt_msec_mean",
    "network_recv_jitter_msec_mean",
    "network_congestion",
    "audio_recv_packet_loss_mean",
    "audio_send_packet_loss_mean",
    "video_recv_packet_loss_mean",
)


def thresholds_view() -> list[dict]:
    """:data:`QUALITY_THRESHOLDS` as JSON-friendly rows for tool output."""
    return [
        {"metric": metric, "fair_at": fair, "poor_at": poor, "unit": unit.strip()}
        for metric, _label, unit, fair, poor in QUALITY_THRESHOLDS
    ]


def assess_quality(params: dict) -> tuple[str, list[dict]]:
    """``(verdict, issues)`` for one ``call_ended`` record.

    ``issues`` entries are ``{label, level, text}``. The verdict is
    ``unknown`` when the record carries none of the threshold metrics
    (dial-in phones, very short joins).
    """
    issues: list[dict] = []
    measured = False
    for metric, label, unit, fair_at, poor_at in QUALITY_THRESHOLDS:
        value = params.get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        measured = True
        if value >= poor_at:
            level = "poor"
        elif value >= fair_at:
            level = "fair"
        else:
            continue
        issues.append({"label": label, "level": level, "text": f"{label} {value}{unit}"})

    reason = params.get("end_call_reason")
    if reason in _DROP_REASONS:
        issues.append({
            "label": f"dropped ({reason})", "level": "poor",
            "text": f"left the call with end_call_reason={reason}",
        })
    rating = params.get("end_of_call_rating")
    if isinstance(rating, int) and not isinstance(rating, bool) and 0 < rating <= _LOW_RATING:
        issues.append({
            "label": "low call rating", "level": "fair",
            "text": f"participant rated the call {rating}/5",
        })

    levels = {issue["level"] for issue in issues}
    if "poor" in levels:
        verdict = "poor"
    elif "fair" in levels:
        verdict = "fair"
    elif measured:
        verdict = "good"
    else:
        verdict = "unknown"
    return verdict, issues


def endpoint_row(event: dict, *, include_all_metrics: bool = False) -> dict:
    """One ``call_ended`` event as a compact participant-session row.

    Carries the private key ``_issue_labels`` (for summaries); strip it
    with :func:`public_row` before output.
    """
    params = event["params"]
    left_at = parse_time(event.get("time"))
    duration = params.get("duration_seconds")
    joined_at = None
    if left_at is not None and isinstance(duration, int):
        joined_at = left_at - timedelta(seconds=duration)

    location = ", ".join(
        part for part in (params.get("location_region"), params.get("location_country"))
        if part
    ) or None

    if include_all_metrics:
        metrics = {
            name: value for name, value in sorted(params.items())
            if name.startswith(_METRIC_PREFIXES)
            and name != "network_transport_protocol"
        }
    else:
        metrics = {name: params[name] for name in CORE_METRICS if name in params}

    verdict, issues = assess_quality(params)
    reason = params.get("end_call_reason")
    row = {
        "conference_id": params.get("conference_id"),
        "meeting_code": params.get("meeting_code"),
        "organizer_email": params.get("organizer_email"),
        "participant": params.get("identifier") or event.get("actor"),
        "identifier_type": params.get("identifier_type"),
        "display_name": params.get("display_name"),
        "device_type": params.get("device_type"),
        "is_external": params.get("is_external") or None,
        "joined_at": format_time(joined_at),
        "left_at": format_time(left_at),
        "duration_seconds": duration,
        "location": location,
        "transport": params.get("network_transport_protocol"),
        "end_call_reason": reason if reason and reason != "normal" else None,
        "rating": params.get("end_of_call_rating"),
        "quality": verdict,
        "issues": [issue["text"] for issue in issues],
        "metrics": metrics,
    }
    if include_all_metrics:
        row["calendar_event_id"] = params.get("calendar_event_id")
        row["ip_address"] = params.get("ip_address")
        row["encryption_type"] = params.get("encryption_type")
        row["product_type"] = params.get("product_type")
    row = drop_empty(row)
    row["_issue_labels"] = [issue["label"] for issue in issues]
    return row


def public_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def is_hardware_row(row: dict) -> bool:
    return (
        row.get("identifier_type") == "device_id"
        or row.get("device_type") in HARDWARE_DEVICE_TYPES
    )


def sort_worst_first(rows: list[dict]) -> list[dict]:
    """Poor first, then fair, good, unknown; more issues first; newest first."""
    rows = sorted(rows, key=lambda r: r.get("left_at") or "", reverse=True)
    return sorted(
        rows,
        key=lambda r: (QUALITY_RANK.get(r.get("quality"), 9), -len(r.get("issues") or [])),
    )


def _quality_counts(rows: list[dict]) -> dict:
    counts = Counter(row.get("quality", "unknown") for row in rows)
    return {verdict: counts[verdict] for verdict in QUALITY_RANK if counts[verdict]}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _metric_stats(rows: list[dict]) -> dict:
    out = {}
    for metric in SUMMARY_METRICS:
        values = [
            row["metrics"][metric] for row in rows
            if isinstance((row.get("metrics") or {}).get(metric), (int, float))
        ]
        if not values:
            continue
        out[metric] = {
            "median": statistics.median(values),
            "p90": _percentile(values, 0.9),
            "max": max(values),
            "endpoints": len(values),
        }
    return out


def summarize_quality(rows: list[dict]) -> dict:
    """Aggregate view of endpoint rows: verdicts, splits, metric spread."""
    by_device: dict[str, list[dict]] = {}
    by_transport: dict[str, list[dict]] = {}
    for row in rows:
        by_device.setdefault(row.get("device_type") or "unknown", []).append(row)
        by_transport.setdefault(row.get("transport") or "unknown", []).append(row)

    issue_counts = Counter(label for row in rows for label in row.get("_issue_labels") or [])
    conferences = {row.get("conference_id") or row.get("meeting_code") for row in rows}
    participants = {row.get("participant") for row in rows if row.get("participant")}
    return drop_empty({
        "endpoints": len(rows),
        "conferences": len(conferences - {None}),
        "participants": len(participants),
        "by_quality": _quality_counts(rows),
        "by_device_type": {
            key: {"endpoints": len(group), **_quality_counts(group)}
            for key, group in sorted(by_device.items(), key=lambda kv: -len(kv[1]))
        },
        "by_transport": {
            key: {"endpoints": len(group), **_quality_counts(group)}
            for key, group in sorted(by_transport.items(), key=lambda kv: -len(kv[1]))
        },
        "metrics": _metric_stats(rows),
        "most_common_issues": [
            {"issue": label, "endpoints": count}
            for label, count in issue_counts.most_common(6)
        ],
    })


def summarize_conferences(rows: list[dict]) -> list[dict]:
    """Group endpoint rows into conferences, most recently ended first."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = row.get("conference_id") or row.get("meeting_code") or "unknown"
        groups.setdefault(key, []).append(row)

    conferences = []
    for group in groups.values():
        joined = [row["joined_at"] for row in group if row.get("joined_at")]
        left = [row["left_at"] for row in group if row.get("left_at")]
        first = group[0]
        participants = {row.get("participant") or row.get("display_name") for row in group}
        external = {
            row.get("participant") or row.get("display_name")
            for row in group if row.get("is_external")
        }
        hardware = sorted({
            row.get("display_name") or row.get("participant") or "unnamed device"
            for row in group if is_hardware_row(row)
        })
        worst = [
            f"{row.get('display_name') or row.get('participant') or 'unknown'}: "
            + "; ".join(row["issues"])
            for row in sort_worst_first(group)
            if row.get("quality") in ("poor", "fair") and row.get("issues")
        ][:3]
        conferences.append(drop_empty({
            "conference_id": first.get("conference_id"),
            "meeting_code": first.get("meeting_code"),
            "organizer_email": next(
                (row["organizer_email"] for row in group if row.get("organizer_email")), None,
            ),
            "started_at": min(joined) if joined else None,
            "ended_at": max(left) if left else None,
            "endpoints": len(group),
            "participants": len(participants - {None}),
            "external_participants": len(external - {None}) or None,
            "device_types": dict(Counter(row.get("device_type") or "unknown" for row in group)),
            "meet_hardware": hardware,
            "quality": _quality_counts(group),
            "worst_endpoints": worst,
        }))
    conferences.sort(key=lambda c: c.get("ended_at") or "", reverse=True)
    return conferences


# ---------------------------------------------------------------------------
# Meet hardware
# ---------------------------------------------------------------------------

PERIPHERALS = (
    "CAMERA", "ADD_ON_CAMERA", "MIC", "SPEAKER", "DISPLAY",
    "HANDHELD_CONTROLLER", "TOUCH_CONTROLLER", "VIDEO_CAPTURE_CONTENT_CAMERA",
)

CALL_PLATFORMS = ("MEET", "TEAMS_VIA_PEXIP", "TEAMS", "ZOOM", "WEBEX", "SIP")

_SOFTWARE_UPDATE_EVENTS = ("EVENT_BROWSER_UPDATE", "EVENT_CLIENT_APP_UPDATE", "EVENT_OS_UPDATE")


def hardware_event_row(event: dict) -> dict:
    """One ``meet_hardware`` event as a compact row."""
    params = event["params"]
    return drop_empty({
        "time": format_time(parse_time(event.get("time"))) or event.get("time"),
        "event": event.get("name"),
        "type": event.get("type"),
        "device_id": params.get("DEVICE_ID"),
        "device_name": params.get("DEVICE_DISPLAY_NAME"),
        "serial_number": params.get("SERIAL_NUMBER"),
        "data": params.get("EVENT_DATA"),
        "peripheral": params.get("AFFECTED_PERIPHERAL"),
    })


def device_key(row: dict) -> str:
    return row.get("device_id") or row.get("serial_number") or row.get("device_name") or "unknown"


def _call_event(name: str) -> Optional[tuple[str, str]]:
    """``EVENT_ZOOM_CALL_JOINED`` -> ``("ZOOM", "JOINED")``."""
    for platform in CALL_PLATFORMS:
        for state in ("JOINED", "DISCONNECTED"):
            if name == f"EVENT_{platform}_CALL_{state}":
                return platform, state
    return None


def _peripheral_event(name: str) -> Optional[tuple[str, str]]:
    """``EVENT_MIC_DETACHED`` -> ``("MIC", "detached")``."""
    for state in ("ATTACHED", "DETACHED"):
        suffix = f"_{state}"
        if name.startswith("EVENT_") and name.endswith(suffix):
            peripheral = name[len("EVENT_"):-len(suffix)]
            if peripheral in PERIPHERALS:
                return peripheral, state.lower()
    return None


def _stamp(row: dict) -> dict:
    return {"event": row.get("event"), "time": row.get("time")}


def summarize_hardware(rows: list[dict]) -> dict[str, dict]:
    """Per-device health from ``meet_hardware`` rows given newest first.

    Keyed by :func:`device_key`. Peripheral, presence and in-call states
    come from the newest relevant event in the window, so a device whose
    last camera event is a detach reads ``CAMERA: detached``.
    """
    devices: dict[str, dict] = {}
    for row in rows:
        key = device_key(row)
        device = devices.get(key)
        if device is None:
            device = devices[key] = {
                "device_id": row.get("device_id"),
                "display_name": None,
                "serial_number": None,
                "last_event": _stamp(row),
                "events": 0,
                "peripherals": {},
                "calls_joined": Counter(),
                "restarts": 0,
                "software_updates": 0,
                "app_load_errors": 0,
                "feedback_filed": 0,
            }
        device["events"] += 1
        device["display_name"] = device["display_name"] or row.get("device_name")
        device["serial_number"] = device["serial_number"] or row.get("serial_number")
        name = row.get("event") or ""

        call = _call_event(name)
        if call:
            platform, state = call
            if state == "JOINED":
                device["calls_joined"][platform] += 1
                device.setdefault("last_call_joined", {"platform": platform, "time": row.get("time")})
            device.setdefault("_call_state", (platform, state, row.get("time")))
            continue
        peripheral = _peripheral_event(name)
        if peripheral:
            part, state = peripheral
            if part not in device["peripherals"]:
                device["peripherals"][part] = {"state": state, "since": row.get("time")}
            continue
        if name in ("EVENT_DEVICE_FOUND", "EVENT_DEVICE_MISSING"):
            device.setdefault("presence", {
                "state": "missing" if name.endswith("MISSING") else "found",
                "since": row.get("time"),
            })
        elif name.startswith("EVENT_RESTART_"):
            device["restarts"] += 1
            device.setdefault("last_restart", _stamp(row))
        elif name in _SOFTWARE_UPDATE_EVENTS:
            device["software_updates"] += 1
            device.setdefault("last_software_update", _stamp(row))
        elif name.startswith("EVENT_FRONTEND_LOAD_") and (
            name.endswith("_ERROR") or name == "EVENT_FRONTEND_LOAD_WAITING_FOR_NETWORK"
        ):
            device["app_load_errors"] += 1
            device.setdefault("last_app_load_error", _stamp(row))
        elif name == "EVENT_FEEDBACK_FILED":
            device["feedback_filed"] += 1
            device.setdefault("last_feedback", {"time": row.get("time"), "data": row.get("data")})

    for device in devices.values():
        call_state = device.pop("_call_state", None)
        if call_state and call_state[1] == "JOINED":
            device["in_call"] = {"platform": call_state[0], "since": call_state[2]}
        device["calls_joined"] = dict(device["calls_joined"])
        device["concerns"] = _hardware_concerns(device)
    return devices


def _hardware_concerns(device: dict) -> list[str]:
    concerns = []
    presence = device.get("presence")
    if presence and presence["state"] == "missing":
        concerns.append(f"reported missing since {presence['since']}")
    for part, state in sorted(device["peripherals"].items()):
        if state["state"] == "detached":
            concerns.append(f"{part} detached since {state['since']}")
    if device["app_load_errors"]:
        concerns.append(f"{device['app_load_errors']} Meet app load error(s)")
    if device["restarts"] >= 3:
        concerns.append(f"{device['restarts']} restarts")
    if device["feedback_filed"]:
        concerns.append(f"{device['feedback_filed']} feedback report(s) filed")
    return concerns


def hardware_call_quality(rows: list[dict]) -> dict[str, dict]:
    """Per-device call quality from Meet hardware endpoint rows.

    Keyed by the device id (the ``identifier`` of a ``device_id``
    endpoint), or the display name when the record carries no id.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if not is_hardware_row(row):
            continue
        key = row.get("participant") if row.get("identifier_type") == "device_id" else None
        key = key or row.get("display_name") or "unknown"
        groups.setdefault(key, []).append(row)

    out = {}
    for key, group in groups.items():
        out[key] = drop_empty({
            "display_name": next((r["display_name"] for r in group if r.get("display_name")), None),
            "device_type": next((r["device_type"] for r in group if r.get("device_type")), None),
            "endpoints": len(group),
            "quality": _quality_counts(group),
            "metrics": _metric_stats(group),
            "most_common_issues": [
                {"issue": label, "endpoints": count}
                for label, count in Counter(
                    label for row in group for label in row.get("_issue_labels") or []
                ).most_common(3)
            ],
            "last_call_at": max((r["left_at"] for r in group if r.get("left_at")), default=None),
        })
    return out


def merge_hardware_call_quality(devices: dict[str, dict], quality: dict[str, dict]) -> None:
    """Attach :func:`hardware_call_quality` stats to the device roster.

    Matches on the device id first, then on the display name (the call log
    and the hardware log name devices independently). A device seen only
    in calls is added to the roster. Poor calls become a concern.
    """
    by_name = {
        (device.get("display_name") or "").lower(): key
        for key, device in devices.items() if device.get("display_name")
    }
    for key, stats in quality.items():
        target = key if key in devices else by_name.get((stats.get("display_name") or "").lower())
        if target is None:
            devices[key] = {
                "device_id": key if key != stats.get("display_name") else None,
                "display_name": stats.get("display_name"),
                "events": 0,
                "concerns": [],
                "note": "seen in Meet calls but no meet_hardware log events in the window",
            }
            target = key
        device = devices[target]
        device["call_quality"] = stats
        poor = (stats.get("quality") or {}).get("poor", 0)
        if poor:
            device["concerns"] = list(device.get("concerns") or []) + [
                f"{poor} of {stats['endpoints']} Meet call endpoint(s) had poor quality"
            ]


# ---------------------------------------------------------------------------
# Usage reports
# ---------------------------------------------------------------------------

# Customer usage ``meet:`` parameters returned when the caller names none.
DEFAULT_USAGE_PARAMETERS = (
    "num_meetings",
    "num_calls",
    "total_call_minutes",
    "average_meeting_minutes",
    "num_calls_by_external_users",
    "num_calls_chromebox",
    "num_calls_chromebase",
    "num_1day_active_users",
    "num_30day_active_users",
)

# Parameters that are not additive across days (excluded from totals).
_NON_ADDITIVE_PREFIXES = ("average_", "max_concurrent_", "num_1day_", "num_7day_", "num_30day_")


def usage_values(body: dict) -> dict:
    """``meet:`` parameter values from one customer usage report body."""
    values: dict[str, Any] = {}
    for report in body.get("usageReports") or []:
        for name, value in flatten_parameters(report.get("parameters")).items():
            values[name.split(":", 1)[-1]] = value
    return values


def usage_totals(days: list[dict], parameters: Iterable[str]) -> dict:
    totals = {}
    for name in parameters:
        if name.startswith(_NON_ADDITIVE_PREFIXES):
            continue
        values = [
            day["values"][name] for day in days
            if isinstance((day.get("values") or {}).get(name), int)
        ]
        if values:
            totals[name] = sum(values)
    return totals
