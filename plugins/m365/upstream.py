"""Microsoft 365 upstream access helpers.

Server-side configuration (the admin ``m365`` credential-store entry with
the Entra ID app registration's tenant id / client id / client secret) and
per-user OAuth tokens (a ``user_service_credentials`` row with token JSON
in ``oauth_blob``) both resolve here, so the OAuth router, the authed_get
service entry, and the plugin tools share one implementation.

Unlike GitHub -- and like Ramp -- Microsoft identity platform access
tokens EXPIRE (~1 hour) and come with a refresh token (the
``offline_access`` scope). :func:`get_m365_token` proactively refreshes
near-expiry tokens and persists the rotated refresh token; the authed_get
registry entry additionally sets ``retry_on_401`` as a backstop, and
:func:`graph_request` retries once with a forced refresh on 401.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from config.oauth_tokens import (
    current_or_refreshed_token,
    get_user_oauth_blob,
    store_user_oauth_blob,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Delegated Microsoft Graph permissions the OAuth flow requests. The
# authorize request additionally asks for ``offline_access`` (refresh
# token); it is an OpenID Connect scope, not a Graph resource scope, so it
# is excluded from the needs_reauth subset check (Microsoft does not echo
# it back consistently).
M365_SCOPES = ("User.Read", "Mail.ReadWrite", "Mail.Send")

# The literal scope parameter sent on the authorize/token/refresh requests.
M365_SCOPE_REQUEST = "offline_access " + " ".join(M365_SCOPES)

_HTTP_TIMEOUT = 30.0

# Per-user refresh locks so concurrent tool calls in one turn do not race
# duplicate refresh requests (Microsoft rotates refresh tokens; a lost
# race would persist a stale rotated token).
_refresh_locks: dict[Any, asyncio.Lock] = {}


def login_base_url(tenant_id: str) -> str:
    """The Microsoft identity platform v2.0 endpoint base for a tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0"


def load_m365_client_config() -> dict:
    """Load the Entra ID app registration config (admin credential store).

    Raises a 500 HTTPException when unconfigured (mirrors
    ``load_github_client_config``). Store reads are fresh on every call so
    admin updates take effect without a restart.
    """
    from fastapi import HTTPException

    from config.service_credentials import read_service_credentials

    stored = read_service_credentials("m365")
    if stored and stored.get("client_id") and stored.get("client_secret") \
            and stored.get("tenant_id"):
        return stored
    raise HTTPException(
        status_code=500,
        detail=(
            "Microsoft 365 OAuth credentials not configured. Set the "
            "tenant ID, client ID, and client secret in Settings > "
            "Service Credentials (admin)."
        ),
    )


def m365_is_configured(config: dict) -> bool:
    """Admin ``is_configured`` predicate: all three app fields present."""
    return bool(
        config.get("tenant_id")
        and config.get("client_id")
        and config.get("client_secret")
    )


def validate_m365_credentials(values: dict) -> dict:
    """Admin-save normalization hook: strip stray whitespace from fields."""
    return {
        key: (value.strip() if isinstance(value, str) else value)
        for key, value in values.items()
    }


def get_user_m365_oauth(user: dict) -> Optional[dict]:
    """The user's stored Microsoft 365 OAuth blob, or None when not connected."""
    return get_user_oauth_blob(user, "m365")


def m365_connected(row: dict) -> bool:
    """``UserConnectionSpec.connected`` hook over the stored row."""
    return bool((row.get("oauth_blob") or {}).get("access_token"))


def normalize_scopes(raw: Any) -> set[str]:
    """Normalize a granted-scope value into a comparable set.

    Accepts either the raw space-separated ``scope`` string from the token
    response or an already-split list. Microsoft may return scopes as
    short names (``Mail.Send``) or full resource URIs
    (``https://graph.microsoft.com/Mail.Send``) depending on how they were
    requested; both are normalized to lowercase short names. The
    ``offline_access``/OpenID scopes pass through but are never part of
    :data:`M365_SCOPES`, so they do not affect the subset check.
    """
    if raw is None:
        parts: list[str] = []
    elif isinstance(raw, str):
        parts = raw.split()
    else:
        parts = [str(s) for s in raw]
    normalized = set()
    for scope in parts:
        scope = scope.strip().rstrip("/")
        if not scope:
            continue
        if "/" in scope:
            scope = scope.rsplit("/", 1)[1]
        normalized.add(scope.lower())
    return normalized


def m365_needs_reauth(row: dict) -> bool:
    """``UserConnectionSpec.needs_reauth`` hook: granted-scope check.

    The callback stores the scopes Microsoft actually GRANTED (not the
    ones requested), so widening :data:`M365_SCOPES` later flags existing
    connections with the "Update Available" re-authorize badge instead of
    failing at call time.
    """
    blob = row.get("oauth_blob") or {}
    granted = blob.get("scopes")
    if granted is None:
        granted = blob.get("scope")
    granted_set = normalize_scopes(granted)
    required = {s.lower() for s in M365_SCOPES}
    return not required.issubset(granted_set)


def build_token_blob(
    token_data: dict,
    *,
    previous: Optional[dict] = None,
    account: Optional[dict] = None,
) -> dict:
    """Build the ``oauth_blob`` JSON from a token-endpoint response.

    ``previous`` supplies carried-over fields on refresh (account info,
    original ``authorized_at``, and the prior refresh token when Microsoft
    does not rotate it in the response).
    """
    previous = previous or {}
    now = datetime.now(timezone.utc)
    expires_in = token_data.get("expires_in")
    try:
        expires_at = now + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        # Default to the common 1-hour lifetime minus a safety haircut.
        expires_at = now + timedelta(minutes=55)
    scope_str = token_data.get("scope", previous.get("scope", ""))
    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token")
        or previous.get("refresh_token"),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
        "scope": scope_str,
        "scopes": scope_str.split() if isinstance(scope_str, str) else [],
        "account": account or previous.get("account"),
        "authorized_at": previous.get("authorized_at") or now.isoformat(),
    }


async def _refresh_m365_token(user: dict, blob: dict) -> Optional[str]:
    """Refresh the user's access token and persist the rotated blob.

    Returns the new access token, or None when refresh is impossible
    (server unconfigured, no refresh token, revoked grant, transport
    error). Callers treat None as "not connected" so the standard
    reconnect message surfaces instead of a raw exception.
    """
    refresh_token = blob.get("refresh_token")
    if not refresh_token:
        return None

    try:
        config = load_m365_client_config()
    except Exception:
        logger.warning("[M365] Token refresh skipped: server credentials not configured")
        return None

    token_url = f"{login_base_url(config['tenant_id'])}/token"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(token_url, data={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": M365_SCOPE_REQUEST,
            })
    except httpx.HTTPError as exc:
        logger.warning("[M365] Token refresh transport error for %s: %s",
                       user.get("email", "unknown"), exc)
        return None

    if response.status_code != 200:
        # invalid_grant here means the refresh token was revoked or has
        # aged out; the user must reconnect in Settings > Data Connections.
        logger.warning(
            "[M365] Token refresh failed for %s (HTTP %s): %s",
            user.get("email", "unknown"), response.status_code,
            response.text[:300],
        )
        return None

    token_data = response.json()
    if not token_data.get("access_token"):
        logger.warning("[M365] Token refresh response had no access_token")
        return None

    new_blob = build_token_blob(token_data, previous=blob)

    # Persist, and update the in-memory user dict so later calls in the
    # same turn see the fresh token without a DB re-read.
    await store_user_oauth_blob(user, "m365", new_blob)

    logger.info("[M365] Refreshed access token for %s", user.get("email", "unknown"))
    return new_blob["access_token"]


async def get_m365_token(user: dict, force_refresh: bool = False) -> Optional[str]:
    """Get a valid Microsoft Graph access token, refreshing when needed.

    Returns None when Microsoft 365 is not connected or the refresh fails,
    so callers surface an actionable reconnect message.
    """
    return await current_or_refreshed_token(
        user, "m365", _refresh_m365_token, _refresh_locks, force_refresh=force_refresh,
    )


MISSING_CREDENTIALS_ERROR = {
    "error": "m365_oauth_required",
    "message": (
        "Microsoft 365 not connected (or the connection expired). "
        "Please connect Microsoft 365 in Settings > Data Connections."
    ),
}


async def load_m365_credentials(user: dict):
    """authed_get credential loader: a valid access token, or None."""
    return await get_m365_token(user)


def inject_m365_bearer_auth(token: str, headers: dict) -> None:
    """authed_get auth injector: Bearer token header."""
    headers["Authorization"] = f"Bearer {token}"


class GraphAuthError(Exception):
    """Raised by :func:`graph_request` when the user has no usable token."""


async def graph_request(
    user: dict,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> httpx.Response:
    """Make an authenticated Microsoft Graph request for the plugin tools.

    Injects a valid (proactively refreshed) Bearer token and retries once
    with a forced refresh on 401. Raises :class:`GraphAuthError` when the
    user is not connected; upstream HTTP errors are returned as the
    response for the caller to classify.
    """
    token = await get_m365_token(user)
    if not token:
        raise GraphAuthError(MISSING_CREDENTIALS_ERROR["message"])

    request_headers = dict(headers or {})
    request_headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method, url, params=params, json=json_body, headers=request_headers,
        )
        if response.status_code == 401:
            token = await get_m365_token(user, force_refresh=True)
            if not token:
                raise GraphAuthError(MISSING_CREDENTIALS_ERROR["message"])
            request_headers["Authorization"] = f"Bearer {token}"
            response = await client.request(
                method, url, params=params, json=json_body,
                headers=request_headers,
            )
    return response
