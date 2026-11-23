"""CreateCalendarInviteHandler and Google Calendar preview helpers."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from dateutil import parser as dateutil_parser

from auth.google_credentials import make_authenticated_request
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._google import _extract_google_api_error, require_google_scope
from chat.action_request_types._param_validation import reject_unknown_params
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# `calendar_name` is server-injected after validation by the dispatch
# arm at conversation.py:1259-1263; not model-supplied.
_ALLOWED_PARAMS = frozenset({
    "summary",
    "start",
    "end",
    "calendar_id",
    "time_zone",
    "attendees",
    "location",
    "description",
})


def _calendar_reauth_message() -> str:
    return (
        "Google Calendar write access requires reconnecting Google Services via "
        "Settings > Data Connections."
    )


def _normalize_timezone(time_zone: object) -> str | None:
    if time_zone is None:
        return None
    time_zone_str = str(time_zone).strip()
    if not time_zone_str:
        return None
    try:
        ZoneInfo(time_zone_str)
    except Exception as exc:
        raise ValueError(f"time_zone must be a valid IANA timezone: {time_zone_str}") from exc
    return time_zone_str


def _parse_event_datetime(value: object, field_name: str, time_zone: str | None) -> datetime:
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required parameter: {field_name}")

    raw_value = str(value).strip()
    try:
        parsed = dateutil_parser.isoparse(raw_value)
    except Exception as exc:
        raise ValueError(
            f"{field_name} must be a valid RFC3339 datetime"
        ) from exc

    if parsed.tzinfo is None:
        if not time_zone:
            raise ValueError(
                f"{field_name} must include a timezone offset or provide time_zone"
            )
        parsed = parsed.replace(tzinfo=ZoneInfo(time_zone))

    return parsed


def _format_preview_datetime(value: str, time_zone: str | None) -> str:
    parsed = dateutil_parser.isoparse(value)
    if time_zone:
        try:
            parsed = parsed.astimezone(ZoneInfo(time_zone))
        except Exception:
            logger.warning(
                "[calendar_invite] Failed to convert preview datetime to %s",
                time_zone,
                exc_info=True,
            )
    month_day = parsed.strftime("%b %d, %Y").replace(" 0", " ")
    time_part = parsed.strftime("%I:%M %p").lstrip("0")
    zone_part = parsed.strftime("%Z")
    return f"{month_day}, {time_part} {zone_part}".strip()


async def _resolve_calendar_name(calendar_id: str, user: dict | None) -> str | None:
    if not user or not user.get("google_services_oauth"):
        return None

    path = quote(calendar_id, safe="")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await make_authenticated_request(
                client,
                user,
                "GET",
                f"{CALENDAR_API_BASE}/users/me/calendarList/{path}",
            )
        if response.status_code >= 400:
            return None
        payload = response.json()
        summary = payload.get("summary")
        if summary and str(summary).strip():
            return str(summary).strip()
    except Exception:
        logger.warning(
            "[calendar_invite] Failed to resolve calendar name for %s",
            calendar_id,
            exc_info=True,
        )
    return None


class CreateCalendarInviteHandler(ActionRequestHandler):
    """Create a Google Calendar event that can optionally invite attendees."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_CALENDAR_INVITE

    @property
    def display_name(self) -> str:
        return "Create Calendar Invite"

    @property
    def approve_label(self) -> str:
        return "Create"

    @property
    def resolved_label(self) -> str:
        return "Created"

    def summary_snippet(self, params: dict) -> str:
        return str(params.get("summary") or "")

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        calendar_value = params.get("calendar_name") or params.get("calendar_id") or "primary"
        if calendar_value == params.get("calendar_id") and user:
            resolved = await _resolve_calendar_name(str(calendar_value), user)
            if resolved:
                calendar_value = resolved

        fields = [
            {"key": "Title", "value": params.get("summary", "")},
            {"key": "Calendar", "value": str(calendar_value)},
            {"key": "Start", "value": _format_preview_datetime(params["start"], params.get("time_zone"))},
            {"key": "End", "value": _format_preview_datetime(params["end"], params.get("time_zone"))},
        ]

        attendees = params.get("attendees") or []
        if attendees:
            fields.append({"key": "Attendees", "value": ", ".join(attendees)})

        if params.get("location"):
            fields.append({"key": "Location", "value": params["location"]})

        if params.get("description"):
            fields.append({"key": "Description", "value": params["description"]})

        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        summary = params.get("summary")
        if not summary or not str(summary).strip():
            raise ValueError("Missing required parameter: summary")
        summary_str = str(summary).strip()

        time_zone = _normalize_timezone(params.get("time_zone"))
        start_dt = _parse_event_datetime(params.get("start"), "start", time_zone)
        end_dt = _parse_event_datetime(params.get("end"), "end", time_zone)
        if end_dt <= start_dt:
            raise ValueError("end must be after start")

        calendar_id = str(params.get("calendar_id") or "primary").strip() or "primary"

        validated: dict = {
            "summary": summary_str,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "calendar_id": calendar_id,
        }

        if time_zone:
            validated["time_zone"] = time_zone

        raw_attendees = params.get("attendees")
        if raw_attendees is not None:
            if not isinstance(raw_attendees, list):
                raise ValueError("attendees must be a list of email addresses")
            attendee_emails: list[str] = []
            for attendee in raw_attendees:
                email = str(attendee).strip()
                if not email:
                    continue
                if not _EMAIL_RE.match(email):
                    raise ValueError(f"attendees must contain valid email addresses: {email}")
                if email not in attendee_emails:
                    attendee_emails.append(email)
            if attendee_emails:
                validated["attendees"] = attendee_emails

        for optional_key in ("location", "description"):
            value = params.get(optional_key)
            if value is not None and str(value).strip():
                validated[optional_key] = str(value).strip()

        return validated

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        require_google_scope(user, "https://www.googleapis.com/auth/calendar.events", _calendar_reauth_message())

        body: dict = {
            "summary": params["summary"],
            "start": {"dateTime": params["start"]},
            "end": {"dateTime": params["end"]},
        }

        if params.get("time_zone"):
            body["start"]["timeZone"] = params["time_zone"]
            body["end"]["timeZone"] = params["time_zone"]

        if params.get("location"):
            body["location"] = params["location"]

        if "description" in params:
            body["description"] = params["description"]

        attendees = params.get("attendees") or []
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]

        request_params = {"sendUpdates": "all"} if attendees else None
        calendar_path = quote(params["calendar_id"], safe="")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await make_authenticated_request(
                    client,
                    user,
                    "POST",
                    f"{CALENDAR_API_BASE}/calendars/{calendar_path}/events",
                    json=body,
                    params=request_params,
                )
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_calendar_reauth_message()) from exc
            raise

        if response.status_code in (401, 403):
            error_text = _extract_google_api_error(response).lower()
            if "insufficient" in error_text or "permission" in error_text or "scope" in error_text:
                raise RuntimeError(_calendar_reauth_message())

        if response.status_code >= 400:
            raise RuntimeError(
                f"Google Calendar API error: {_extract_google_api_error(response)}"
            )

        payload = response.json()
        return {
            "success": True,
            "calendar_id": params["calendar_id"],
            "event_id": payload.get("id"),
            "html_link": payload.get("htmlLink"),
            "summary": payload.get("summary", params["summary"]),
        }
