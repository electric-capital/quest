"""Twitter/X upstream access helpers.

Server-side configuration (the admin ``twitter`` credential-store entry
with the OAuth 2.0 app's client id/secret) and per-user OAuth tokens (a
``user_service_credentials`` row with token JSON in ``oauth_blob``) both
resolve here, so the OAuth router, the authed_get service entry, and the
send_twitter_dm action-request handler share one implementation.

Like Microsoft 365 (and Ramp) -- and unlike GitHub -- Twitter/X access
tokens EXPIRE (~2 hours) and refresh tokens ROTATE on every use (the
``offline.access`` scope), so :func:`get_twitter_token` proactively
refreshes near-expiry tokens under a per-user lock and persists the
rotated refresh token; the ``api.twitter.com`` authed_get registry entry
additionally sets ``retry_on_401`` as a backstop, and
:func:`twitter_request` retries once with a forced refresh on 401.
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

TWITTER_API_BASE = "https://api.twitter.com/2"

# OAuth 2.0 scopes the flow requests. dm.read / dm.write require at
# minimum the Basic API tier ($100/month as of 2024). ``offline.access``
# (refresh token) is requested alongside but excluded from the reauth
# subset check below, mirroring the M365 ``offline_access`` handling.
TWITTER_SCOPES = ("dm.read", "dm.write", "tweet.read", "users.read", "bookmark.read")

# The literal scope parameter sent on the authorize request.
TWITTER_SCOPE_REQUEST = " ".join(TWITTER_SCOPES + ("offline.access",))

_HTTP_TIMEOUT = 30.0

# Per-user refresh locks so concurrent tool calls in one turn do not race
# duplicate refresh requests (X rotates refresh tokens on every use; a
# lost race would persist a stale rotated token and permanently break the
# connection).
_refresh_locks: dict[Any, asyncio.Lock] = {}

# The authorize page must live on x.com, NOT the legacy twitter.com domain:
# X's consent page calls api.x.com, and twitter.com -> api.x.com is a
# CROSS-SITE request, so browsers that block third-party cookies drop the
# session's ct0 CSRF cookie and the Authorize click 403s with X error code
# 353 ("This request requires a matching csrf cookie and header").
TWITTER_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TWITTER_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"


def load_twitter_client_config() -> dict:
    """Load the Twitter/X OAuth 2.0 app config (admin credential store).

    Prefers the per-service credential store and falls back to the legacy
    standalone twitter_credentials.json (see
    config/service_credentials.py's ``read_legacy_service_credentials``
    special case). Store reads are fresh on every call so admin updates
    take effect without a restart. Raises a 500 HTTPException when
    unconfigured (mirrors ``load_github_client_config``).
    """
    from fastapi import HTTPException

    from config.service_credentials import (
        read_legacy_service_credentials,
        read_service_credentials,
    )

    stored = read_service_credentials("twitter")
    if stored:
        return stored
    legacy = read_legacy_service_credentials("twitter")
    if legacy:
        return legacy
    raise HTTPException(
        status_code=500,
        detail=(
            "Twitter OAuth credentials not configured. Set them in "
            "Settings > Service Credentials (admin) or place "
            "twitter_credentials.json in the project root."
        ),
    )


def twitter_is_configured(config: dict) -> bool:
    """Admin ``is_configured`` predicate: both client fields present."""
    return bool(config.get("client_id") and config.get("client_secret"))


def validate_twitter_credentials(values: dict) -> dict:
    """Admin-save normalization hook: strip stray whitespace from fields."""
    return {
        key: (value.strip() if isinstance(value, str) else value)
        for key, value in values.items()
    }


def get_user_twitter_oauth(user: dict) -> Optional[dict]:
    """The user's stored Twitter/X OAuth blob, or None when not connected.

    Reads the ``user_service_credentials`` row attached to the user dict
    by db/user_store.py (service key ``twitter``).
    """
    return get_user_oauth_blob(user, "twitter")


def twitter_connected(row: dict) -> bool:
    """``UserConnectionSpec.connected`` hook over the stored row."""
    return bool((row.get("oauth_blob") or {}).get("access_token"))


def twitter_needs_reauth(row: dict) -> bool:
    """``UserConnectionSpec.needs_reauth`` hook: granted-scope check.

    The callback stores the raw space-separated ``scope`` string Twitter
    actually GRANTED (blobs migrated from the pre-plugin
    ``users.twitter_oauth`` column carry the same shape), so widening
    :data:`TWITTER_SCOPES` later -- as historically happened when
    ``bookmark.read`` was added -- flags existing connections with the
    "Update Available" re-authorize badge instead of failing at call time.
    """
    blob = row.get("oauth_blob") or {}
    granted = blob.get("scopes")
    if granted is None:
        granted = (blob.get("scope") or "").split()
    granted_set = {str(s).strip() for s in granted if str(s).strip()}
    return not set(TWITTER_SCOPES).issubset(granted_set)


def build_token_blob(token_data: dict, *, previous: Optional[dict] = None) -> dict:
    """Build the ``oauth_blob`` JSON from a token-endpoint response.

    ``previous`` supplies carried-over fields on refresh (original
    ``authorized_at``, and the prior refresh token when the response does
    not rotate it -- X normally rotates on every use).
    """
    previous = previous or {}
    now = datetime.now(timezone.utc)
    expires_in = token_data.get("expires_in")
    try:
        expires_at = now + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        # Default to the documented 2-hour lifetime minus a safety haircut.
        expires_at = now + timedelta(minutes=115)
    scope_str = token_data.get("scope", previous.get("scope", ""))
    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token")
        or previous.get("refresh_token"),
        "token_type": token_data.get("token_type", "bearer"),
        "expires_at": expires_at.isoformat(),
        "scope": scope_str,
        "scopes": scope_str.split() if isinstance(scope_str, str) else [],
        "authorized_at": previous.get("authorized_at") or now.isoformat(),
    }


async def _refresh_twitter_token(user: dict, blob: dict) -> Optional[str]:
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
        config = load_twitter_client_config()
    except Exception:
        logger.warning("[Twitter] Token refresh skipped: server credentials not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                TWITTER_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": config["client_id"],
                },
                auth=(config["client_id"], config["client_secret"]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.warning("[Twitter] Token refresh transport error for %s: %s",
                       user.get("email", "unknown"), exc)
        return None

    if response.status_code != 200:
        # invalid_grant here means the refresh token was revoked or has
        # aged out; the user must reconnect in Settings > Data Connections.
        logger.warning(
            "[Twitter] Token refresh failed for %s (HTTP %s): %s",
            user.get("email", "unknown"), response.status_code,
            response.text[:300],
        )
        return None

    token_data = response.json()
    if token_data.get("error") or not token_data.get("access_token"):
        logger.warning(
            "[Twitter] Token refresh response unusable: %s",
            token_data.get("error_description", token_data.get("error", "no access_token")),
        )
        return None

    new_blob = build_token_blob(token_data, previous=blob)

    # Persist, and update the in-memory user dict so later calls in the
    # same turn see the fresh token without a DB re-read.
    await store_user_oauth_blob(user, "twitter", new_blob)

    logger.info("[Twitter] Refreshed access token for %s", user.get("email", "unknown"))
    return new_blob["access_token"]


async def get_twitter_token(user: dict, force_refresh: bool = False) -> Optional[str]:
    """Get a valid Twitter/X access token, refreshing when needed.

    Returns None when Twitter is not connected or the refresh fails, so
    callers surface an actionable reconnect message.
    """
    return await current_or_refreshed_token(
        user, "twitter", _refresh_twitter_token, _refresh_locks, force_refresh=force_refresh,
    )


MISSING_CREDENTIALS_ERROR = {
    "error": "twitter_oauth_required",
    "message": (
        "Twitter/X not connected (or the connection expired). "
        "Please connect Twitter in Settings > Data Connections."
    ),
}


async def load_twitter_credentials(user: dict):
    """authed_get credential loader: a valid access token, or None."""
    return await get_twitter_token(user)


def inject_twitter_bearer_auth(token: str, headers: dict) -> None:
    """authed_get auth injector: Bearer token header."""
    headers["Authorization"] = f"Bearer {token}"


class TwitterAuthError(Exception):
    """Raised by :func:`twitter_request` when the user has no usable token."""


async def twitter_request(
    user: dict,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> httpx.Response:
    """Make an authenticated X API v2 request for the DM handler.

    ``path`` is relative to :data:`TWITTER_API_BASE` (e.g. ``users/me``).
    Injects a valid (proactively refreshed) Bearer token and retries once
    with a forced refresh on 401. Raises :class:`TwitterAuthError` when
    the user is not connected; upstream HTTP errors are returned as the
    response for the caller to classify.
    """
    token = await get_twitter_token(user)
    if not token:
        raise TwitterAuthError(MISSING_CREDENTIALS_ERROR["message"])

    url = f"{TWITTER_API_BASE}/{path}"
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        headers["Authorization"] = f"Bearer {token}"
        response = await client.request(
            method, url, params=params, json=json_body, headers=headers,
        )
        if response.status_code == 401:
            token = await get_twitter_token(user, force_refresh=True)
            if not token:
                raise TwitterAuthError(MISSING_CREDENTIALS_ERROR["message"])
            headers["Authorization"] = f"Bearer {token}"
            response = await client.request(
                method, url, params=params, json=json_body, headers=headers,
            )
    return response
