"""Google Services checks shared by the action-request types that write to
a Google API (Calendar, Drive, Sheets, Gmail, Compute).

Each of those handlers starts ``execute`` the same way -- the user must
have Google Services connected and must have granted the scope the write
needs -- and reports an upstream failure with the message Google put in
the error body. Those three pieces live here.
"""

from __future__ import annotations

from typing import Iterable

import httpx

GOOGLE_NOT_CONNECTED_MESSAGE = (
    "Google Services not connected. Please connect Google Services via "
    "Settings > Data Connections."
)


def _get_authorized_google_scopes(user: dict) -> set[str]:
    google_services_oauth = user.get("google_services_oauth") or {}
    return {scope for scope in google_services_oauth.get("scopes", []) if isinstance(scope, str)}


def require_google_scope(user: dict, scopes: str | Iterable[str], reauth_message: str) -> None:
    """Raise ``RuntimeError`` unless the user can make this Google write.

    The user must have Google Services connected (else the standard
    "not connected" message) and must have granted ``scopes`` -- one scope,
    or several of which any one is enough (else ``reauth_message``).
    """
    google_services_oauth = user.get("google_services_oauth") or {}
    if not google_services_oauth.get("access_token"):
        raise RuntimeError(GOOGLE_NOT_CONNECTED_MESSAGE)
    wanted = {scopes} if isinstance(scopes, str) else set(scopes)
    if not (wanted & _get_authorized_google_scopes(user)):
        raise RuntimeError(reauth_message)


def _extract_google_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text or f"HTTP {response.status_code}"

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
        errors = error.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"])
    if isinstance(error, str):
        return error
    return response.text or f"HTTP {response.status_code}"
