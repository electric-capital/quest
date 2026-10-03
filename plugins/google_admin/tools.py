"""Google Workspace Admin Meet tools (plugin module).

Four read-only tools over the Admin SDK Reports API (client in
plugins/google_admin/reports.py, shaping in plugins/google_admin/meet.py):

- ``google_admin_meet_calls``: conferences in a window, grouped from the
  per-endpoint ``call_ended`` records, with metric medians / maxima each;
- ``google_admin_meet_call_quality``: per-participant-session rows carrying
  Google's quality metrics unchanged (optionally sorted by one metric)
  plus an aggregate summary;
- ``google_admin_meet_hardware``: a Meet hardware device roster with
  health signals derived from the ``meet_hardware`` audit log, joined
  with each device's call metrics, and optionally one device's events;
- ``google_admin_meet_usage``: daily Meet usage statistics.

They exist because the raw records do not fit through ``authed_get``: one
``call_ended`` record carries ~70 parameters (about the whole 3 KB inline
size gate), so the useful answers need paging, flattening and
aggregation server-side. The raw endpoints stay reachable through
``authed_get`` (allow-list in manifest.py) for anything the tools do not
shape.

Every handler converts :class:`GoogleAdminError` and argument errors into
a JSON error object instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config.plugin_types import PluginTool

from plugins.google_admin import meet
from plugins.google_admin.reports import (
    GoogleAdminError,
    get_customer_usage,
    list_activities,
    require_token,
    usage_client,
)
from plugins.google_admin.upstream import REPORTS_AUDIT_SCOPE, REPORTS_USAGE_SCOPE

logger = logging.getLogger(__name__)

# Google keeps Meet and Meet hardware audit records for 6 months.
MAX_LOOKBACK_DAYS = 180

# Pages of 1000 records collected per scan (newest first) before the
# result is flagged truncated.
CALL_SCAN_PAGES = 5
HARDWARE_SCAN_PAGES = 10

DEFAULT_CONFERENCE_LIMIT = 25
MAX_CONFERENCE_LIMIT = 100
DEFAULT_ENDPOINT_LIMIT = 50
MAX_ENDPOINT_LIMIT = 200
DEFAULT_DEVICE_LIMIT = 100
MAX_DEVICE_LIMIT = 300
DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 200

MAX_USAGE_DAYS = 92
_USAGE_CONCURRENCY = 5
# Customer usage days are Pacific-time days.
_USAGE_TZ = ZoneInfo("America/Los_Angeles")

# Pseudo device type: Meet hardware endpoints, selected upstream by
# ``identifier_type==device_id`` (covers room kits whatever device_type
# they report).
MEET_HARDWARE = "meet_hardware"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MEETING_URL_RE = re.compile(r"meet\.google\.com/(?:lookup/)?([A-Za-z0-9-]+)")
_USAGE_PARAM_RE = re.compile(r"^[a-z0-9_]+$")
_METRIC_NAME_RE = _USAGE_PARAM_RE

# Non-metric call_ended values google_admin_meet_call_quality can sort by.
_SORTABLE_FIELDS = ("duration_seconds", "end_of_call_rating")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _error(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message})


def _arg_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _arg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _arg_int(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer")
    if value < minimum:
        raise ValueError(f"'{name}' must be at least {minimum}")
    return min(value, maximum)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_bound(raw: str, name: str, *, end: bool) -> datetime:
    """``YYYY-MM-DD`` (UTC day; an end date includes the whole day) or RFC 3339."""
    if _DATE_RE.match(raw):
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return day + timedelta(days=1) if end else day
    parsed = meet.parse_time(raw)
    if parsed is None:
        raise ValueError(
            f"'{name}' must be a date (YYYY-MM-DD) or an RFC 3339 timestamp "
            "such as 2026-10-01T09:00:00Z"
        )
    return parsed


def resolve_window(args: dict, default_days: int) -> tuple[str, Optional[str]]:
    """``(startTime, endTime | None)`` for an activities scan.

    ``start_time`` / ``end_time`` win over ``days`` (the lookback from the
    end, default ``default_days``). An end in the future is dropped (the
    API defaults it to now).
    """
    now = _now()
    end_raw = _arg_str(args.get("end_time"))
    start_raw = _arg_str(args.get("start_time"))
    end = _parse_bound(end_raw, "end_time", end=True) if end_raw else None
    if end is not None and end >= now:
        end = None
    if start_raw:
        start = _parse_bound(start_raw, "start_time", end=False)
    else:
        days = _arg_int(args, "days", default_days, 1, MAX_LOOKBACK_DAYS)
        start = (end or now) - timedelta(days=days)
    if start >= (end or now):
        raise ValueError("start_time must be before end_time and in the past")
    if start < now - timedelta(days=MAX_LOOKBACK_DAYS):
        raise ValueError(
            f"Google keeps Meet audit records for 6 months; start_time must be "
            f"within the last {MAX_LOOKBACK_DAYS} days"
        )
    return meet.format_time(start), meet.format_time(end) if end else None


def meeting_code_variants(raw: str) -> list[str]:
    """Spellings of a meeting code to try against ``meeting_code==``.

    Accepts a code or a meet.google.com URL. The documented form is
    lowercase with hyphens (``abc-defg-hij``); the compact upper and lower
    case forms are tried after it because the filter is an exact match.
    """
    text = raw.strip()
    match = _MEETING_URL_RE.search(text)
    if match:
        text = match.group(1)
    letters = re.sub(r"[^A-Za-z]", "", text)
    if len(letters) == 10:
        candidates = [
            f"{letters[:3]}-{letters[3:7]}-{letters[7:]}".lower(),
            letters.upper(),
            letters.lower(),
        ]
    else:
        candidates = [text, text.lower(), text.upper()]
    seen: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def _filter_value(value: str, name: str) -> str:
    if "," in value:
        raise ValueError(f"'{name}' cannot contain a comma")
    return value


def _call_filters(args: dict) -> list[str]:
    """``filters`` clauses for a ``call_ended`` scan (all ANDed)."""
    clauses = []
    conference_id = _arg_str(args.get("conference_id"))
    if conference_id:
        clauses.append(f"conference_id=={_filter_value(conference_id, 'conference_id')}")
    organizer = _arg_str(args.get("organizer_email"))
    if organizer:
        clauses.append(f"organizer_email=={_filter_value(organizer, 'organizer_email')}")
    participant = _arg_str(args.get("participant"))
    if participant:
        clauses.append(f"identifier=={_filter_value(participant, 'participant')}")
    device_type = _arg_str(args.get("device_type"))
    if device_type:
        device_type = device_type.lower()
        if device_type == MEET_HARDWARE:
            clauses.append("identifier_type==device_id")
        elif device_type in meet.DEVICE_TYPES:
            clauses.append(f"device_type=={device_type}")
        else:
            raise ValueError(
                "'device_type' must be one of: "
                + ", ".join((MEET_HARDWARE,) + meet.DEVICE_TYPES)
            )
    return clauses


async def _scan_call_ended(
    token: str, args: dict, start: str, end: Optional[str],
) -> tuple[Any, Optional[str]]:
    """Collect ``call_ended`` records for the filters in ``args``.

    Returns ``(ActivityPage, meeting_code_used)``. A meeting code is tried
    in each spelling from :func:`meeting_code_variants` until one matches.
    """
    clauses = _call_filters(args)
    code = _arg_str(args.get("meeting_code"))
    variants = meeting_code_variants(code) if code else [None]
    page = None
    for variant in variants:
        filters = clauses + ([f"meeting_code=={variant}"] if variant else [])
        page = await list_activities(
            token, "meet",
            event_name="call_ended",
            filters=",".join(filters) or None,
            start_time=start,
            end_time=end,
            max_pages=CALL_SCAN_PAGES,
        )
        if page.items:
            return page, variant
    return page, None


def _window_view(start: str, end: Optional[str], page, rows_time_key: str, rows: list) -> dict:
    view = {"start_time": start, "end_time": end or meet.format_time(_now())}
    if page.truncated:
        times = [row[rows_time_key] for row in rows if row.get(rows_time_key)]
        view["truncated"] = True
        view["covered_from"] = min(times) if times else None
        view["note"] = (
            "Only the newest records were scanned; narrow the time window or "
            "add filters to cover the whole range."
        )
    return view


def _endpoint_rows(page, include_all_metrics: bool = False) -> list[dict]:
    return [
        meet.endpoint_row(event, include_all_metrics=include_all_metrics)
        for event in meet.iter_events(page.items, "call_ended")
    ]


def _applied_filters(args: dict, code_used: Optional[str]) -> dict:
    out = {
        name: _arg_str(args.get(name))
        for name in ("conference_id", "organizer_email", "participant", "device_type")
        if _arg_str(args.get(name))
    }
    if _arg_str(args.get("meeting_code")):
        out["meeting_code"] = code_used or _arg_str(args.get("meeting_code"))
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _tool_meet_calls(ctx, args: dict) -> str:
    try:
        limit = _arg_int(args, "limit", DEFAULT_CONFERENCE_LIMIT, 1, MAX_CONFERENCE_LIMIT)
        start, end = resolve_window(args, default_days=7)
        token = await require_token(ctx.user, REPORTS_AUDIT_SCOPE)
        page, code_used = await _scan_call_ended(token, args, start, end)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    except GoogleAdminError as exc:
        return json.dumps(exc.to_dict())

    rows = _endpoint_rows(page)
    conferences = meet.summarize_conferences(rows)
    out: dict[str, Any] = {
        "window": _window_view(start, end, page, "left_at", rows),
        "filters": _applied_filters(args, code_used),
        "endpoints_scanned": len(rows),
        "conferences_found": len(conferences),
        "conferences": conferences[:limit],
    }
    if len(conferences) > limit:
        out["conferences_omitted"] = len(conferences) - limit
    if not rows and _arg_str(args.get("meeting_code")):
        out["note"] = (
            "No call records matched the meeting code in any spelling tried "
            f"({', '.join(meeting_code_variants(args['meeting_code']))}). Records "
            "appear a few minutes after each participant leaves."
        )
    return json.dumps(out)


def _sort_metric(args: dict) -> tuple[Optional[str], bool]:
    """``(metric, descending)`` from ``sort_by`` / ``sort_order``."""
    sort_by = _arg_str(args.get("sort_by"))
    order = (_arg_str(args.get("sort_order")) or "desc").lower()
    if order not in ("asc", "desc"):
        raise ValueError("'sort_order' must be 'asc' or 'desc'")
    if sort_by is None:
        return None, True
    if not _METRIC_NAME_RE.match(sort_by) or not (
        meet.is_metric(sort_by) or sort_by in _SORTABLE_FIELDS
    ):
        raise ValueError(
            "'sort_by' must be a call_ended metric name (audio_*, video_*, "
            "screencast_*, network_*) or one of: " + ", ".join(_SORTABLE_FIELDS)
        )
    return sort_by, order == "desc"


async def _tool_meet_call_quality(ctx, args: dict) -> str:
    try:
        limit = _arg_int(args, "limit", DEFAULT_ENDPOINT_LIMIT, 1, MAX_ENDPOINT_LIMIT)
        sort_by, descending = _sort_metric(args)
        include_all = _arg_bool(args.get("include_all_metrics"), False)
        start, end = resolve_window(args, default_days=7)
        token = await require_token(ctx.user, REPORTS_AUDIT_SCOPE)
        page, code_used = await _scan_call_ended(token, args, start, end)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    except GoogleAdminError as exc:
        return json.dumps(exc.to_dict())

    # Newest first (the API order) unless sorted by a metric; endpoints
    # without that metric go last.
    events = list(meet.iter_events(page.items, "call_ended"))
    if sort_by:
        valued = [e for e in events if _is_number(e["params"].get(sort_by))]
        missing = [e for e in events if not _is_number(e["params"].get(sort_by))]
        valued.sort(key=lambda e: e["params"][sort_by], reverse=descending)
        events = valued + missing
    extra = (sort_by,) if sort_by and meet.is_metric(sort_by) else ()
    rows = [
        meet.endpoint_row(e, include_all_metrics=include_all, extra_metrics=extra)
        for e in events
    ]

    out: dict[str, Any] = {
        "window": _window_view(start, end, page, "left_at", rows),
        "filters": _applied_filters(args, code_used),
        "summary": meet.summarize_quality(rows),
        "endpoints": rows[:limit],
    }
    if sort_by:
        out["sorted_by"] = f"{sort_by} {'desc' if descending else 'asc'}"
        if events and not valued:
            out["note"] = f"No endpoint in the scan carried {sort_by}."
    if len(rows) > limit:
        out["endpoints_omitted"] = len(rows) - limit
    return json.dumps(out)


def _device_matches(device: dict, query: str) -> bool:
    lowered = query.lower()
    if query in (device.get("device_id"), device.get("serial_number")):
        return True
    name = device.get("display_name") or ""
    return lowered in name.lower()


async def _tool_meet_hardware(ctx, args: dict) -> str:
    try:
        limit = _arg_int(args, "limit", DEFAULT_DEVICE_LIMIT, 1, MAX_DEVICE_LIMIT)
        event_limit = _arg_int(args, "event_limit", DEFAULT_EVENT_LIMIT, 0, MAX_EVENT_LIMIT)
        device_query = _arg_str(args.get("device"))
        include_events = _arg_bool(args.get("include_events"), bool(device_query))
        include_quality = _arg_bool(args.get("include_call_quality"), True)
        start, end = resolve_window(args, default_days=7)
        token = await require_token(ctx.user, REPORTS_AUDIT_SCOPE)
        hardware_page = await list_activities(
            token, "meet_hardware",
            start_time=start, end_time=end, max_pages=HARDWARE_SCAN_PAGES,
        )
        call_page = None
        if include_quality:
            call_page = await list_activities(
                token, "meet",
                event_name="call_ended",
                filters="identifier_type==device_id",
                start_time=start, end_time=end,
                max_pages=CALL_SCAN_PAGES,
            )
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    except GoogleAdminError as exc:
        return json.dumps(exc.to_dict())

    event_rows = [meet.hardware_event_row(e) for e in meet.iter_events(hardware_page.items)]
    devices = meet.summarize_hardware(event_rows)

    if call_page is not None:
        meet.merge_hardware_call_quality(
            devices, meet.hardware_call_quality(_endpoint_rows(call_page)),
        )

    selected = list(devices.items())
    if device_query:
        selected = [(k, d) for k, d in selected if _device_matches(d, device_query)]
    selected.sort(key=lambda kd: (not kd[1].get("concerns"), (kd[1].get("display_name") or "").lower()))

    out: dict[str, Any] = {
        "window": _window_view(start, end, hardware_page, "time", event_rows),
        "hardware_events_scanned": len(event_rows),
        "devices_found": len(selected),
        "devices_with_concerns": sum(1 for _, d in selected if d.get("concerns")),
        "devices": [meet.drop_empty(d) for _, d in selected[:limit]],
    }
    if call_page is not None and call_page.truncated:
        out["call_quality_note"] = (
            "Call quality covers only the newest Meet hardware call records; "
            "narrow the window for complete per-device figures."
        )
    if len(selected) > limit:
        out["devices_omitted"] = len(selected) - limit
    if not selected:
        out["note"] = (
            "No matching device in the window." if device_query and devices else
            "No Meet hardware activity in the window. Either the organization "
            "has no Meet hardware or the devices were idle; widen the window."
        )
    if include_events and event_limit:
        keys = {key for key, _ in selected}
        events = [row for row in event_rows if meet.device_key(row) in keys]
        out["events"] = events[:event_limit]
        if len(events) > event_limit:
            out["events_omitted"] = len(events) - event_limit
    return json.dumps(out)


def _usage_parameters(raw: Any) -> list[str]:
    if raw in (None, "", []):
        return list(meet.DEFAULT_USAGE_PARAMETERS)
    items = raw if isinstance(raw, list) else str(raw).split(",")
    names = []
    for item in items:
        name = str(item).strip()
        if name.startswith("meet:"):
            name = name[len("meet:"):]
        if not name:
            continue
        if not _USAGE_PARAM_RE.match(name):
            raise ValueError(f"invalid usage parameter name: {item!r}")
        if name not in names:
            names.append(name)
    return names or list(meet.DEFAULT_USAGE_PARAMETERS)


def _usage_dates(args: dict) -> list[str]:
    yesterday = datetime.now(_USAGE_TZ).date() - timedelta(days=1)
    end_raw = _arg_str(args.get("end_date"))
    start_raw = _arg_str(args.get("start_date"))
    for name, raw in (("start_date", start_raw), ("end_date", end_raw)):
        if raw and not _DATE_RE.match(raw):
            raise ValueError(f"'{name}' must be a date in YYYY-MM-DD form")
    end = date.fromisoformat(end_raw) if end_raw else yesterday
    if end > yesterday:
        end = yesterday
    if start_raw:
        start = date.fromisoformat(start_raw)
    else:
        days = _arg_int(args, "days", 7, 1, MAX_USAGE_DAYS)
        start = end - timedelta(days=days - 1)
    if start > end:
        raise ValueError("start_date must not be after end_date (or yesterday)")
    if (end - start).days + 1 > MAX_USAGE_DAYS:
        raise ValueError(f"at most {MAX_USAGE_DAYS} days per call")
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


async def _tool_meet_usage(ctx, args: dict) -> str:
    try:
        parameters = _usage_parameters(args.get("parameters"))
        dates = _usage_dates(args)
        token = await require_token(ctx.user, REPORTS_USAGE_SCOPE)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    except GoogleAdminError as exc:
        return json.dumps(exc.to_dict())

    qualified = [f"meet:{name}" for name in parameters]
    semaphore = asyncio.Semaphore(_USAGE_CONCURRENCY)

    async with usage_client() as client:
        async def fetch(day: str):
            async with semaphore:
                return await get_customer_usage(client, token, day, qualified)

        results = await asyncio.gather(*(fetch(day) for day in dates), return_exceptions=True)

    # The same rejection for every day (an unknown parameter name, or a
    # range Google has not published yet) is one error, not N notes.
    if len(results) > 1 and all(isinstance(r, GoogleAdminError) for r in results) \
            and len({(r.code, str(r)) for r in results}) == 1:
        return json.dumps(results[0].to_dict())

    days = []
    for day, result in zip(dates, results):
        if isinstance(result, GoogleAdminError):
            if result.code != "google_admin_bad_request":
                # Privilege / API / connection problems apply to every day.
                return json.dumps(result.to_dict())
            days.append({"date": day, "note": str(result)})
        elif isinstance(result, Exception):
            logger.warning("[Google Admin] usage report for %s failed: %s", day, result)
            days.append({"date": day, "note": f"request failed: {result}"})
        else:
            values = meet.usage_values(result)
            entry: dict[str, Any] = {"date": day, "values": values}
            if not values and result.get("warnings"):
                entry["note"] = "; ".join(
                    str(w.get("message")) for w in result["warnings"] if w.get("message")
                )
            days.append(entry)

    return json.dumps({
        "parameters": parameters,
        "days": days,
        "totals": meet.usage_totals(days, parameters),
        "note": (
            "Days are Pacific-time days; a meeting counts on the day it ended. "
            "Only meetings organized by users in the organization are counted, "
            "and Google publishes each day 1-3 days later."
        ),
    })


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

_WINDOW_PROPERTIES = {
    "start_time": {
        "type": "string",
        "description": (
            "Window start: a UTC date (YYYY-MM-DD) or RFC 3339 timestamp. "
            "Default: 'days' before the end."
        ),
    },
    "end_time": {
        "type": "string",
        "description": "Window end (a date includes that whole UTC day). Default: now.",
    },
    "days": {
        "type": "integer",
        "description": "Lookback in days when start_time is not given (default 7, max 180).",
    },
}

_CALL_FILTER_PROPERTIES = {
    "meeting_code": {
        "type": "string",
        "description": (
            "Meeting code (abc-defg-hij) or meet.google.com link. Recurring "
            "meetings share a code, so this can span several conferences."
        ),
    },
    "conference_id": {
        "type": "string",
        "description": "One conference (a single occurrence of a meeting).",
    },
    "organizer_email": {
        "type": "string",
        "description": "Meetings created by this user.",
    },
    "participant": {
        "type": "string",
        "description": (
            "Endpoints of one participant: an email address, a phone number, "
            "or a Meet hardware device id."
        ),
    },
    "device_type": {
        "type": "string",
        "enum": [MEET_HARDWARE, *meet.DEVICE_TYPES],
        "description": (
            "Only endpoints of this client type. 'meet_hardware' selects "
            "every Meet hardware endpoint (room devices)."
        ),
    },
}


def _tool(name: str, description: str, properties: dict, handler) -> PluginTool:
    return PluginTool(
        spec={
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": []},
        },
        handler=handler,
        requires_service="google_admin",
    )


GOOGLE_ADMIN_TOOLS = (
    _tool(
        "google_admin_meet_calls",
        "List Google Meet calls (conferences) across the Workspace organization "
        "from the Meet audit log, most recently ended first: meeting code, "
        "organizer, start/end, participant and external counts, device types, "
        "Meet hardware rooms, end-call reasons, and the median / max of the key "
        "quality metrics (round-trip time, jitter, congestion, packet loss) per call. "
        "Filters are ANDed. Requires the 'Reports' admin privilege.",
        {
            **_WINDOW_PROPERTIES,
            **_CALL_FILTER_PROPERTIES,
            "limit": {
                "type": "integer",
                "description": f"Conferences to return (default {DEFAULT_CONFERENCE_LIMIT}, max {MAX_CONFERENCE_LIMIT}).",
            },
        },
        _tool_meet_calls,
    ),
    _tool(
        "google_admin_meet_call_quality",
        "Google Meet call quality per participant session (one row per time "
        "someone joined a call), as Google's raw metric values: packet loss "
        "(%), jitter and round-trip time (ms), congestion (%), estimated "
        "bandwidth (kbps), video resolution / frame rate, plus transport, "
        "location, end-call reason and rating. Rows are newest first, or "
        "sorted by one metric; the summary covers every scanned row (median / "
        "p90 / max of the key metrics, medians by device type and transport, "
        "end-call reason and rating counts). Narrow with a meeting code, "
        "conference, participant, organizer or device type. Requires the "
        "'Reports' admin privilege.",
        {
            **_WINDOW_PROPERTIES,
            **_CALL_FILTER_PROPERTIES,
            "sort_by": {
                "type": "string",
                "description": (
                    "Order rows by this call_ended metric instead of newest first, "
                    "e.g. audio_recv_packet_loss_mean, network_rtt_msec_mean, "
                    "network_recv_jitter_msec_max, network_estimated_download_kbps_mean, "
                    "or duration_seconds / end_of_call_rating. Rows without the "
                    "value go last; the metric is added to every row."
                ),
            },
            "sort_order": {
                "type": "string",
                "enum": ["desc", "asc"],
                "description": "Sort direction for sort_by (default desc, highest first).",
            },
            "include_all_metrics": {
                "type": "boolean",
                "description": (
                    "Return every audio/video/screencast/network metric plus IP "
                    "address, calendar event id and encryption (default false)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"Rows to return (default {DEFAULT_ENDPOINT_LIMIT}, max {MAX_ENDPOINT_LIMIT}).",
            },
        },
        _tool_meet_call_quality,
    ),
    _tool(
        "google_admin_meet_hardware",
        "Google Meet hardware (room devices) seen in the window, from the Meet "
        "hardware audit log: display name, device id, serial, last event, "
        "peripheral attach/detach state, missing/found, calls joined per "
        "platform (Meet, Zoom, Teams, Webex, SIP), restarts, software "
        "updates, app load errors, feedback, plus each device's Meet call "
        "metrics (median / p90 / max). Devices with concerns come first. There is no API listing "
        "Meet hardware: a device idle for the whole window does not appear. "
        "Requires the 'Reports' admin privilege.",
        {
            **_WINDOW_PROPERTIES,
            "device": {
                "type": "string",
                "description": "Only devices matching this device id, serial number, or display-name substring.",
            },
            "include_events": {
                "type": "boolean",
                "description": "Also return the matching devices' events, newest first (default: true when 'device' is set).",
            },
            "event_limit": {
                "type": "integer",
                "description": f"Events to return (default {DEFAULT_EVENT_LIMIT}, max {MAX_EVENT_LIMIT}).",
            },
            "include_call_quality": {
                "type": "boolean",
                "description": "Join each device's Meet call metrics from the call log (default true).",
            },
            "limit": {
                "type": "integer",
                "description": f"Devices to return (default {DEFAULT_DEVICE_LIMIT}, max {MAX_DEVICE_LIMIT}).",
            },
        },
        _tool_meet_hardware,
    ),
    _tool(
        "google_admin_meet_usage",
        "Daily Google Meet usage statistics for the organization (customer "
        "usage report): meetings, calls, call minutes, average meeting "
        "length, external and Meet hardware calls, active users, with totals "
        "over the range. Pacific-time days, published 1-3 days late. Requires "
        "the 'Reports' admin privilege.",
        {
            "start_date": {"type": "string", "description": "First day, YYYY-MM-DD."},
            "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD (default and maximum: yesterday)."},
            "days": {
                "type": "integer",
                "description": f"Number of days ending at end_date when start_date is not given (default 7, max {MAX_USAGE_DAYS}).",
            },
            "parameters": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "meet: usage parameter names, with or without the 'meet:' "
                    "prefix, e.g. num_calls_web, num_meetings_with_external_users, "
                    "total_call_minutes_chromebox. Default: "
                    + ", ".join(meet.DEFAULT_USAGE_PARAMETERS) + "."
                ),
            },
        },
        _tool_meet_usage,
    ),
)

GOOGLE_ADMIN_TOOL_NAMES = tuple(tool.spec["name"] for tool in GOOGLE_ADMIN_TOOLS)
