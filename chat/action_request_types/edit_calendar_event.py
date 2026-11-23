"""EditCalendarEventHandler -- approve-to-write action request for editing an
existing Google Calendar event.

The model issues a
``create_action_request(request_type="edit_calendar_event",
params={"event_id": "...", "expected_updated": "<event.updated>",
"start": "...", "end": "..."}, reasoning="...")`` call which appends an
inline approval card to the chat. On Approve, the handler PATCHes the
event via the Calendar v3 ``events.patch`` endpoint with only the
supplied fields.

Read-before-write enforcement: ``expected_updated`` must carry the
``updated`` timestamp of the event the model just read (a GET on
``/calendars/{calendarId}/events/{eventId}``). The handler compares it
against the live event twice -- at proposal time in
``validate_against_upstream()`` (same-turn rejection, no card) and again
at Approve time in ``execute()`` (TOCTOU close: the event may have
changed while the card sat open). A mismatch means the model is about to
overwrite a version of the event it never read, so the write is refused
with instructions to re-read the event.

Proposal-time validation also captures the live event (title, times,
attendees, location, description, recurrence scope) as the
server-injected ``current_event`` param so the preview card can render
before/after values and a line diff for description changes.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

import httpx
from dateutil import parser as dateutil_parser

from auth.google_credentials import make_authenticated_request
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._google import _extract_google_api_error, require_google_scope
from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types._skill_content_edit import build_content_diff
from chat.action_request_types.create_calendar_invite import (
    CALENDAR_API_BASE,
    _EMAIL_RE,
    _calendar_reauth_message,
    _format_preview_datetime,
    _normalize_timezone,
    _parse_event_datetime,
    _resolve_calendar_name,
)
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# `calendar_name` and `current_event` are server-injected after the
# model-supplied params validate (enrich_params_for_preview /
# validate_against_upstream); they are not model-suppliable and therefore
# not in the allow-list (parallels `calendar_name` on create_calendar_invite).
_ALLOWED_PARAMS = frozenset({
    "event_id",
    "calendar_id",
    "expected_updated",
    "summary",
    "start",
    "end",
    "time_zone",
    "attendees",
    "location",
    "description",
})

# The subset of params that actually change the event. At least one is
# required; the rest of the allow-list identifies the target.
_EDITABLE_FIELDS = (
    "summary",
    "start",
    "end",
    "time_zone",
    "attendees",
    "location",
    "description",
)

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{1,1024}$")

_FIELD_LABELS = {
    "summary": "Title",
    "start": "Start",
    "end": "End",
    "time_zone": "Time zone",
    "attendees": "Attendees",
    "location": "Location",
    "description": "Description",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _is_all_day(value: str | None) -> bool:
    return bool(value) and bool(_DATE_ONLY_RE.match(value))


def _parse_event_time(value: object, field_name: str, time_zone: str | None) -> str:
    """Normalize a model-supplied start/end.

    Accepts an all-day ``YYYY-MM-DD`` date (returned unchanged) or an
    RFC3339 datetime (returned as an aware ISO string, the naive case
    localized to ``time_zone``). Raises ValueError on anything else.
    """
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required parameter: {field_name}")
    raw = str(value).strip()
    if _DATE_ONLY_RE.match(raw):
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid YYYY-MM-DD date") from exc
        return raw
    return _parse_event_datetime(raw, field_name, time_zone).isoformat()


def _time_sort_key(value: str) -> datetime:
    """A comparable datetime for either an all-day date or an RFC3339 value."""
    if _is_all_day(value):
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    return dateutil_parser.isoparse(value)


def _check_order(start: str, end: str) -> None:
    if _is_all_day(start) != _is_all_day(end):
        raise ValueError(
            "start and end must both be all-day dates (YYYY-MM-DD) or both "
            "be RFC3339 datetimes. When switching an event between timed and "
            "all-day, supply both start and end."
        )
    if _time_sort_key(end) <= _time_sort_key(start):
        if _is_all_day(start):
            raise ValueError(
                "end must be after start (all-day end dates are exclusive: "
                "a one-day event on 2026-03-18 ends on 2026-03-19)"
            )
        raise ValueError("end must be after start")


def _same_instant(a: object, b: object) -> bool:
    """Whether two RFC3339 strings name the same instant (format-tolerant)."""
    a_str = str(a or "").strip()
    b_str = str(b or "").strip()
    if a_str == b_str:
        return True
    if not a_str or not b_str:
        return False
    try:
        return dateutil_parser.isoparse(a_str) == dateutil_parser.isoparse(b_str)
    except (ValueError, OverflowError):
        return False


def _live_time(bound: dict | None) -> str | None:
    """The comparable string form of a live event start/end object."""
    if not isinstance(bound, dict):
        return None
    if bound.get("date"):
        return str(bound["date"])
    if bound.get("dateTime"):
        return str(bound["dateTime"])
    return None


def _format_preview_time(value: str | None, time_zone: str | None) -> str:
    if not value:
        return ""
    if _is_all_day(value):
        parsed = date.fromisoformat(value)
        return parsed.strftime("%b %d, %Y").replace(" 0", " ") + " (all day)"
    try:
        return _format_preview_datetime(value, time_zone)
    except (ValueError, OverflowError):
        return value


def _format_preview_end(value: str | None, time_zone: str | None) -> str:
    """All-day end dates are exclusive in the API; show the inclusive day."""
    if value and _is_all_day(value):
        inclusive = date.fromisoformat(value) - timedelta(days=1)
        return inclusive.strftime("%b %d, %Y").replace(" 0", " ") + " (all day)"
    return _format_preview_time(value, time_zone)


# ---------------------------------------------------------------------------
# Live event helpers
# ---------------------------------------------------------------------------


def _event_url(params: dict) -> str:
    calendar_path = quote(str(params.get("calendar_id") or "primary"), safe="")
    event_path = quote(str(params["event_id"]), safe="")
    return f"{CALENDAR_API_BASE}/calendars/{calendar_path}/events/{event_path}"


async def _fetch_event(user: dict, params: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await make_authenticated_request(client, user, "GET", _event_url(params))


def _snapshot_event(event: dict) -> dict:
    """Reduce a live event resource to the fields the card and checks use."""
    attendees = []
    for attendee in event.get("attendees") or []:
        if isinstance(attendee, dict) and attendee.get("email"):
            attendees.append(str(attendee["email"]))
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    return {
        "summary": str(event.get("summary") or ""),
        "start": _live_time(start),
        "end": _live_time(end),
        "time_zone": start.get("timeZone") or end.get("timeZone"),
        "attendees": attendees,
        "location": str(event.get("location") or ""),
        "description": str(event.get("description") or ""),
        "html_link": event.get("htmlLink"),
        "updated": event.get("updated"),
        "status": event.get("status"),
        # A master event carries `recurrence`; an expanded instance carries
        # `recurringEventId`. Editing the former touches every occurrence.
        "recurring_series": bool(event.get("recurrence")),
        "recurring_instance": bool(event.get("recurringEventId")),
    }


def _changed_fields(params: dict, current: dict | None) -> list[str]:
    """Supplied editable fields that differ from the live snapshot.

    Without a snapshot every supplied field counts as a change.
    """
    changed = []
    for field in _EDITABLE_FIELDS:
        if field not in params:
            continue
        if current is None:
            changed.append(field)
            continue
        new_value = params[field]
        live_value = current.get(field)
        if field in ("start", "end"):
            same = (
                live_value is not None
                and _is_all_day(new_value) == _is_all_day(live_value)
                and (
                    new_value == live_value
                    if _is_all_day(new_value)
                    else _same_instant(new_value, live_value)
                )
            )
        elif field == "attendees":
            same = sorted(e.lower() for e in new_value) == sorted(
                e.lower() for e in (live_value or [])
            )
        else:
            same = str(new_value or "") == str(live_value or "")
        if not same:
            changed.append(field)
    return changed


def _reauth_or_api_error(response: httpx.Response, *, what: str) -> RuntimeError:
    if response.status_code in (401, 403):
        error_text = _extract_google_api_error(response).lower()
        if "insufficient" in error_text or "permission" in error_text or "scope" in error_text:
            return RuntimeError(_calendar_reauth_message())
    return RuntimeError(f"Google Calendar API error ({what}): {_extract_google_api_error(response)}")


class EditCalendarEventHandler(ActionRequestHandler):
    """Update fields on an existing Google Calendar event after approval.

    Params:
        event_id (str): The event id (from a Calendar API read).
        calendar_id (str, optional): Defaults to ``"primary"``.
        expected_updated (str): The event's ``updated`` timestamp as read
            by the model. Verified against the live event at proposal time
            and again at Approve time.
        summary / location / description (str, optional): Replacement
            values. An empty string clears location or description.
        start / end (str, optional): RFC3339 datetimes or all-day
            ``YYYY-MM-DD`` dates. Either may be supplied alone; the pair is
            validated against the live event's other bound.
        time_zone (str, optional): IANA zone applied to the event's start
            and end.
        attendees (list[str], optional): The COMPLETE replacement attendee
            list -- omitted addresses are removed. Google emails the
            attendees about the change after approval.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.EDIT_CALENDAR_EVENT

    @property
    def display_name(self) -> str:
        return "Edit Calendar Event"

    @property
    def approve_label(self) -> str:
        return "Update"

    @property
    def resolved_label(self) -> str:
        return "Updated"

    def summary_snippet(self, params: dict) -> str:
        current = params.get("current_event")
        if isinstance(current, dict) and current.get("summary"):
            return str(current["summary"])
        return str(params.get("summary") or params.get("event_id") or "")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        event_id = params.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("Missing required parameter: event_id")
        event_id = event_id.strip()
        if not _EVENT_ID_RE.match(event_id):
            raise ValueError(
                f"event_id does not look like a Google Calendar event id: "
                f"'{event_id[:80]}'. Pass the `id` field from a Calendar API "
                "event read, not the event URL."
            )

        expected_updated = params.get("expected_updated")
        if not isinstance(expected_updated, str) or not expected_updated.strip():
            raise ValueError(
                "Missing required parameter: expected_updated. Read the event "
                "first (authed_get on /calendars/{calendarId}/events/{eventId}) "
                "and pass its `updated` timestamp, so the edit can be verified "
                "against the live event."
            )
        expected_updated = expected_updated.strip()
        try:
            dateutil_parser.isoparse(expected_updated)
        except (ValueError, OverflowError) as exc:
            raise ValueError(
                "expected_updated must be the event's `updated` RFC3339 timestamp"
            ) from exc

        calendar_id = str(params.get("calendar_id") or "primary").strip() or "primary"

        validated: dict = {
            "event_id": event_id,
            "calendar_id": calendar_id,
            "expected_updated": expected_updated,
        }

        if "summary" in params:
            summary = params.get("summary")
            if summary is None or not str(summary).strip():
                raise ValueError("summary must be a non-empty string when supplied")
            validated["summary"] = str(summary).strip()

        time_zone = _normalize_timezone(params.get("time_zone"))
        if time_zone:
            validated["time_zone"] = time_zone

        for bound in ("start", "end"):
            if bound in params:
                validated[bound] = _parse_event_time(params.get(bound), bound, time_zone)
        if "start" in validated and "end" in validated:
            _check_order(validated["start"], validated["end"])

        if "attendees" in params:
            raw_attendees = params.get("attendees")
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
            validated["attendees"] = attendee_emails

        for optional_key in ("location", "description"):
            if optional_key in params:
                value = params.get(optional_key)
                validated[optional_key] = "" if value is None else str(value).strip()

        if not any(field in validated for field in _EDITABLE_FIELDS):
            raise ValueError(
                "No changes supplied. Pass at least one of: "
                + ", ".join(_EDITABLE_FIELDS)
            )

        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Verify expected_updated against the live event at proposal time.

        Also rejects unknown events, no-op edits, and start/end pairs that
        end up out of order once merged with the live event, and captures
        the ``current_event`` snapshot for the preview card. Transient
        failures (network, 401/403, 5xx) log a WARNING and fall through --
        execute() re-verifies at Approve time, so the safety check is
        never skipped, only deferred.
        """
        if not (user.get("google_services_oauth") or {}).get("access_token"):
            # No Google connection: the authoritative "connect Google
            # Services" error belongs to execute(); don't mask it as
            # Invalid parameters.
            return params

        try:
            response = await _fetch_event(user, params)
        except Exception:
            logger.warning(
                "[edit_calendar_event] event fetch failed during proposal "
                "validation; deferring verification to execute()",
                exc_info=True,
            )
            return params

        if response.status_code == 404:
            raise ValueError(
                f"Event not found: '{params['event_id']}' on calendar "
                f"'{params['calendar_id']}'. Check the event_id and calendar_id "
                "(list the calendar's events via authed_get to find it)."
            )
        if response.status_code >= 400:
            logger.warning(
                "[edit_calendar_event] event fetch returned %s during proposal "
                "validation; deferring verification to execute()",
                response.status_code,
            )
            return params

        event = response.json()
        current = _snapshot_event(event)

        if current.get("status") == "cancelled":
            raise ValueError(
                f"Event '{params['event_id']}' is cancelled (deleted); it cannot "
                "be edited. Propose a create_calendar_invite instead."
            )

        if not _same_instant(params["expected_updated"], current.get("updated")):
            raise ValueError(
                "expected_updated does not match the live event -- it changed "
                "since you read it (live `updated` is "
                f"{current.get('updated')!r}). Re-read the event via authed_get "
                f"{_event_url(params)} and pass its current `updated` value as "
                "expected_updated."
            )

        effective_start = params.get("start") or current.get("start")
        effective_end = params.get("end") or current.get("end")
        if ("start" in params or "end" in params) and effective_start and effective_end:
            _check_order(effective_start, effective_end)

        if not _changed_fields(params, current):
            raise ValueError(
                "No changes: every supplied field already matches the event. "
                "Nothing to update."
            )

        params = dict(params)
        params["current_event"] = current
        return params

    async def enrich_params_for_preview(self, params: dict, user: dict) -> None:
        calendar_name = await _resolve_calendar_name(params["calendar_id"], user)
        if calendar_name:
            params["calendar_name"] = calendar_name

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        current = params.get("current_event")
        if not isinstance(current, dict):
            current = None

        calendar_value = params.get("calendar_name") or params.get("calendar_id") or "primary"
        event_label = (current or {}).get("summary") or params.get("event_id", "")
        fields: list[dict] = [
            {"key": "Event", "value": str(event_label)},
            {"key": "Calendar", "value": str(calendar_value)},
        ]
        if current:
            if current.get("recurring_series"):
                fields.append({"key": "Scope", "value": "Entire recurring series"})
            elif current.get("recurring_instance"):
                fields.append({"key": "Scope", "value": "This occurrence only"})

        time_zone = params.get("time_zone") or (current or {}).get("time_zone")

        def _before_after(before: str, after: str) -> str:
            if current is None:
                return after
            if before == after:
                return after
            return f"{before or '(empty)'}  →  {after or '(empty)'}"

        for field in _EDITABLE_FIELDS:
            if field not in params:
                continue
            label = _FIELD_LABELS[field]
            new_value = params[field]
            old_value = (current or {}).get(field) if current else None
            if field == "description":
                if current is not None and str(old_value or "") != str(new_value or ""):
                    fields.append({
                        "key": label,
                        "value": "changed",
                        "type": "skill_content_diff",
                        "diff": build_content_diff(str(old_value or ""), str(new_value or "")),
                    })
                else:
                    fields.append({"key": label, "value": str(new_value or "(empty)")})
            elif field == "start":
                fields.append({
                    "key": label,
                    "value": _before_after(
                        _format_preview_time(old_value, time_zone),
                        _format_preview_time(new_value, time_zone),
                    ),
                })
            elif field == "end":
                fields.append({
                    "key": label,
                    "value": _before_after(
                        _format_preview_end(old_value, time_zone),
                        _format_preview_end(new_value, time_zone),
                    ),
                })
            elif field == "attendees":
                new_set = list(new_value)
                old_set = list(old_value or []) if current else []
                if current is None:
                    fields.append({"key": label, "value": ", ".join(new_set) or "(none)"})
                else:
                    added = [e for e in new_set if e.lower() not in {o.lower() for o in old_set}]
                    removed = [e for e in old_set if e.lower() not in {n.lower() for n in new_set}]
                    parts = []
                    if added:
                        parts.append("add " + ", ".join(added))
                    if removed:
                        parts.append("remove " + ", ".join(removed))
                    fields.append({
                        "key": label,
                        "value": "; ".join(parts) or ", ".join(new_set) or "(none)",
                    })
            else:
                fields.append({
                    "key": label,
                    "value": _before_after(str(old_value or ""), str(new_value or "")),
                })

        resulting_attendees = (
            params["attendees"] if "attendees" in params else (current or {}).get("attendees") or []
        )
        if resulting_attendees:
            fields.append({
                "key": "Notifications",
                "value": "Attendees will be emailed about the change",
            })
        return fields

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        require_google_scope(user, CALENDAR_EVENTS_SCOPE, _calendar_reauth_message())

        # Re-read the live event: the card may have sat open while the
        # event changed underneath it.
        try:
            live_resp = await _fetch_event(user, params)
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_calendar_reauth_message()) from exc
            raise
        if live_resp.status_code == 404:
            raise RuntimeError(
                f"Event '{params['event_id']}' no longer exists on calendar "
                f"'{params['calendar_id']}'."
            )
        if live_resp.status_code >= 400:
            raise _reauth_or_api_error(live_resp, what="reading the event")

        live_event = live_resp.json()
        current = _snapshot_event(live_event)
        if current.get("status") == "cancelled":
            raise RuntimeError(
                f"Event '{params['event_id']}' has been deleted since this "
                "request was created; nothing to update."
            )
        if not _same_instant(params["expected_updated"], current.get("updated")):
            raise RuntimeError(
                "The event changed since this request was created; refusing "
                "to overwrite a version that was never read. Re-read the "
                "event and issue a new edit_calendar_event request with its "
                "current `updated` value as expected_updated."
            )

        body: dict = {}
        if "summary" in params:
            body["summary"] = params["summary"]
        if "location" in params:
            body["location"] = params["location"]
        if "description" in params:
            body["description"] = params["description"]

        if any(key in params for key in ("start", "end", "time_zone")):
            effective_start = params.get("start") or current.get("start")
            effective_end = params.get("end") or current.get("end")
            if not effective_start or not effective_end:
                raise RuntimeError(
                    "The event has no readable start/end; supply both start and end."
                )
            _check_order(effective_start, effective_end)
            time_zone = params.get("time_zone") or current.get("time_zone")
            for key, value in (("start", effective_start), ("end", effective_end)):
                # Send the complete bound object so PATCH's merge semantics
                # cannot leave a stale `date` next to a new `dateTime` (or
                # vice versa) when the event switches between all-day and
                # timed.
                if _is_all_day(value):
                    body[key] = {"date": value, "dateTime": None, "timeZone": None}
                else:
                    body[key] = {"dateTime": value, "date": None}
                    if time_zone:
                        body[key]["timeZone"] = time_zone

        if "attendees" in params:
            # Preserve each kept attendee's existing record (responseStatus,
            # optional, organizer flags) by matching on email.
            live_by_email = {
                str(a.get("email", "")).lower(): a
                for a in (live_event.get("attendees") or [])
                if isinstance(a, dict) and a.get("email")
            }
            body["attendees"] = [
                live_by_email.get(email.lower(), {"email": email})
                for email in params["attendees"]
            ]

        resulting_attendees = (
            params["attendees"] if "attendees" in params else current.get("attendees") or []
        )
        request_params = {"sendUpdates": "all"} if resulting_attendees else None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await make_authenticated_request(
                    client,
                    user,
                    "PATCH",
                    _event_url(params),
                    json=body,
                    params=request_params,
                )
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_calendar_reauth_message()) from exc
            raise

        if response.status_code >= 400:
            raise _reauth_or_api_error(response, what="updating the event")

        payload = response.json()
        return {
            "success": True,
            "calendar_id": params["calendar_id"],
            "event_id": payload.get("id", params["event_id"]),
            "html_link": payload.get("htmlLink") or current.get("html_link"),
            "summary": payload.get("summary", params.get("summary") or current.get("summary")),
            "updated_fields": [f for f in _EDITABLE_FIELDS if f in params],
            "updated": payload.get("updated"),
        }
