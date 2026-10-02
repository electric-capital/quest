"""Admin SDK Reports API client for the Google Workspace Admin Meet tools.

The Meet tools read two Reports API surfaces with the user's Google
Workspace Admin token (see plugins/google_admin/upstream.py):

- ``activities.list`` for the ``meet`` audit log (one ``call_ended``
  record per participant endpoint, carrying the call-quality metrics) and
  the ``meet_hardware`` audit log (device joins/leaves, peripherals,
  restarts, app load errors). Pages are collected newest first up to a
  cap, since a busy organization produces thousands of endpoints a day.
- ``customerUsageReports.get`` for the daily Meet usage statistics.

Upstream failures become :class:`GoogleAdminError` with a stable code and
an actionable message (missing Reports privilege, API disabled, a grant
that predates the Reports scopes), so tool handlers can hand them to the
model as JSON instead of raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from plugins.google_admin.upstream import (
    MISSING_CREDENTIALS_ERROR,
    get_google_admin_token,
    has_granted_scope,
)

logger = logging.getLogger(__name__)

REPORTS_BASE_URL = "https://admin.googleapis.com/admin/reports/v1"

# activities.list allows up to 1000 records per page.
ACTIVITY_PAGE_SIZE = 1000

_HTTP_TIMEOUT = 60.0

# Partial response: drop etag/kind/ownerDomain and the per-item ipAddress
# (the Meet record repeats it as the ``ip_address`` parameter).
_ACTIVITY_FIELDS = "nextPageToken,items(id/time,actor/email,events)"


class GoogleAdminError(Exception):
    """An upstream or connection failure with a stable error code."""

    def __init__(self, code: str, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"error": self.code, "message": str(self)}
        if self.status_code is not None:
            out["status_code"] = self.status_code
        return out


@dataclass
class ActivityPage:
    """Activity records collected across pages, newest first."""

    items: list = field(default_factory=list)
    # True when the scan stopped at the page cap with more records left:
    # the items then cover only the newest part of the window.
    truncated: bool = False


def _reconnect_error() -> GoogleAdminError:
    return GoogleAdminError(
        "google_admin_reconnect_required",
        "The Google Workspace Admin connection was made before the Meet "
        "report scopes were added. Ask the user to reconnect it in "
        "Settings > Data Connections (the row shows Update Available).",
    )


async def require_token(user: dict, scope: str) -> str:
    """The user's access token, or a :class:`GoogleAdminError`.

    Checks the recorded grant for ``scope`` first, so a pre-Meet
    connection gets a reconnect hint without an upstream round trip.
    """
    token = await get_google_admin_token(user)
    if not token:
        raise GoogleAdminError(
            MISSING_CREDENTIALS_ERROR["error"], MISSING_CREDENTIALS_ERROR["message"],
        )
    if not has_granted_scope(user, scope):
        raise _reconnect_error()
    return token


def _google_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text[:500]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:500]
    return str(body)[:500]


def _error_for_response(response: httpx.Response) -> GoogleAdminError:
    status = response.status_code
    message = _google_error_message(response)
    text = response.text or ""
    if status == 401:
        return GoogleAdminError(
            MISSING_CREDENTIALS_ERROR["error"], MISSING_CREDENTIALS_ERROR["message"], status,
        )
    if status == 403:
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in text.lower():
            err = _reconnect_error()
            err.status_code = status
            return err
        if "SERVICE_DISABLED" in text or "has not been used in project" in text:
            return GoogleAdminError(
                "google_admin_api_disabled",
                "The Admin SDK API is not enabled in this deployment's Google "
                f"Cloud project (an admin must enable it). Google said: {message}",
                status,
            )
        return GoogleAdminError(
            "google_admin_forbidden",
            "The connected Google account is not allowed to read Workspace "
            "reports: it needs the Admin console 'Reports' privilege (super "
            f"admins have it). Google said: {message}",
            status,
        )
    if status == 429:
        return GoogleAdminError(
            "google_admin_rate_limited",
            f"Google rate-limited the request; wait and retry. Google said: {message}",
            status,
        )
    if status == 400:
        return GoogleAdminError("google_admin_bad_request", message, status)
    return GoogleAdminError(
        "google_admin_upstream_error",
        f"Google answered HTTP {status}: {message}",
        status,
    )


async def _get_json(client: httpx.AsyncClient, url: str, token: str, params: dict) -> dict:
    try:
        response = await client.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.TimeoutException:
        raise GoogleAdminError(
            "google_admin_timeout",
            "The Reports API request timed out; narrow the time window.",
        )
    except httpx.HTTPError as exc:
        raise GoogleAdminError(
            "google_admin_upstream_error", f"Reports API request failed: {exc}",
        )
    if response.status_code != 200:
        raise _error_for_response(response)
    try:
        return response.json()
    except ValueError:
        raise GoogleAdminError(
            "google_admin_upstream_error", "The Reports API returned a non-JSON body.",
        )


async def list_activities(
    token: str,
    application: str,
    *,
    event_name: Optional[str] = None,
    filters: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_pages: int = 5,
) -> ActivityPage:
    """Collect ``application`` audit records (all users), newest first.

    ``filters`` is the Reports API ``filters`` string (``param==value``
    clauses, comma-separated, ANDed); Google ignores it unless
    ``event_name`` is set. Stops after ``max_pages`` pages of
    :data:`ACTIVITY_PAGE_SIZE` and flags the result ``truncated``.
    """
    url = f"{REPORTS_BASE_URL}/activity/users/all/applications/{application}"
    params: dict[str, Any] = {
        "maxResults": ACTIVITY_PAGE_SIZE,
        "fields": _ACTIVITY_FIELDS,
    }
    if event_name:
        params["eventName"] = event_name
    if filters:
        params["filters"] = filters
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time

    page = ActivityPage()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for page_number in range(max_pages):
            body = await _get_json(client, url, token, params)
            page.items.extend(body.get("items") or [])
            next_token = body.get("nextPageToken")
            if not next_token:
                return page
            if page_number == max_pages - 1:
                page.truncated = True
                return page
            params["pageToken"] = next_token
    return page


async def get_customer_usage(
    client: httpx.AsyncClient, token: str, date: str, parameters: list[str],
) -> dict:
    """One day's customer usage report restricted to ``parameters``.

    Returns the raw report body; a day whose data Google has not produced
    yet raises :class:`GoogleAdminError` ``google_admin_bad_request`` with
    Google's "not yet available" message.
    """
    url = f"{REPORTS_BASE_URL}/usage/dates/{date}"
    return await _get_json(client, url, token, {"parameters": ",".join(parameters)})


def usage_client() -> httpx.AsyncClient:
    """A shared client for a run of :func:`get_customer_usage` calls."""
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
