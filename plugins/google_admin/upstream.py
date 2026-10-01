"""Google Workspace Admin upstream access helpers.

Server-side configuration and per-user OAuth tokens both resolve here, so
the OAuth router and the authed_get service entries share one
implementation.

The plugin has no OAuth client of its own: it reuses the deployment's
core Google OAuth client (the ``google_oauth`` credential-store entry
that already serves sign-in and the Google Services connector). Its admin
card is a single ``enabled`` switch, because the connection only works
once the admin has added the plugin's callback URL to that client and
enabled the Admin SDK / Cloud Identity APIs in its GCP project.

Per-user tokens are a SEPARATE grant from the user's Google Services
connection (a ``user_service_credentials`` row with token JSON in
``oauth_blob``): only Workspace administrators can use the admin scopes,
so they are not added to ``GOOGLE_SERVICE_SCOPES`` where every user would
have to re-consent to them. Google access tokens expire after ~1 hour and
the refresh token does not rotate; :func:`get_google_admin_token`
refreshes near-expiry tokens under a per-user lock and persists the new
access token.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SERVICE_ID = "google_admin"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

_SCOPE_PREFIX = "https://www.googleapis.com/auth/"

# Every scope is a read-only variant: the token itself cannot change the
# directory, on top of the GET-only authed_get allow-lists in manifest.py.
# ``admin.directory.user.security`` (third-party app grants, app passwords,
# backup verification codes) is deliberately absent -- Google offers no
# read-only variant of it.
GOOGLE_ADMIN_SCOPES = tuple(_SCOPE_PREFIX + name for name in (
    "admin.directory.user.readonly",             # users, aliases
    "admin.directory.group.readonly",            # groups, aliases, members
    "admin.directory.orgunit.readonly",          # organizational units
    "admin.directory.domain.readonly",           # domains, domain aliases
    "admin.directory.customer.readonly",         # the Workspace account itself
    "admin.directory.userschema.readonly",       # custom user attribute schemas
    "admin.directory.device.chromeos.readonly",  # ChromeOS devices
    "admin.directory.device.mobile.readonly",    # mobile devices
    "cloud-identity.devices.readonly",           # endpoints (laptops/desktops) + device users
))

# Identity scopes requested alongside the admin scopes so the callback can
# record WHICH Google account was connected (it may be a dedicated admin
# account, not the Quest login). Excluded from the needs_reauth check.
# "openid" is listed because Google adds it to the grant whenever a
# userinfo.* scope is requested.
GOOGLE_ADMIN_IDENTITY_SCOPES = ("openid", _SCOPE_PREFIX + "userinfo.email")

# The literal scope parameter sent on the authorize request.
GOOGLE_ADMIN_SCOPE_REQUEST = " ".join(
    GOOGLE_ADMIN_SCOPES + GOOGLE_ADMIN_IDENTITY_SCOPES
)

# Safety margin: refresh the token if it expires within 5 minutes.
_REFRESH_MARGIN_SECONDS = 300

_HTTP_TIMEOUT = 30.0

# Per-user refresh locks so concurrent tool calls in one turn share a
# single refresh request.
_refresh_locks: dict[Any, asyncio.Lock] = {}

MISSING_CREDENTIALS_ERROR = {
    "error": "google_admin_oauth_required",
    "message": (
        "Google Workspace Admin not connected (or the connection expired). "
        "Please connect Google Workspace Admin in Settings > Data Connections."
    ),
}


# ---------------------------------------------------------------------------
# Server-side configuration
# ---------------------------------------------------------------------------

def _google_client_config() -> Optional[dict]:
    """The core Google OAuth client config, or None when unconfigured."""
    from auth.config import load_client_config

    try:
        config = load_client_config()
    except Exception:
        return None
    if config.get("client_id") and config.get("client_secret"):
        return config
    return None


def google_admin_is_configured(config: dict) -> bool:
    """Admin ``is_configured`` predicate.

    Available only when the admin switched the plugin on AND the core
    Google OAuth client it borrows is configured.
    """
    return bool(config.get("enabled")) and _google_client_config() is not None


def validate_google_admin_credentials(values: dict) -> dict:
    """Admin-save hook: refuse to enable without a Google OAuth client."""
    if values.get("enabled") and _google_client_config() is None:
        raise ValueError(
            "Configure the Google OAuth client first (Settings > Service "
            "Credentials > Google): Google Workspace Admin reuses it."
        )
    return values


def load_google_admin_client_config() -> dict:
    """The OAuth client config for the admin flow.

    Raises a 500 HTTPException when the plugin is switched off or the
    core Google OAuth client is missing (mirrors the other OAuth
    client-config loaders). Store reads are fresh on every call so admin
    updates take effect without a restart.
    """
    from fastapi import HTTPException

    from config.service_credentials import read_service_credentials

    stored = read_service_credentials(SERVICE_ID) or {}
    if not stored.get("enabled"):
        raise HTTPException(
            status_code=500,
            detail=(
                "Google Workspace Admin is not enabled. An admin can enable "
                "it in Settings > Service Credentials."
            ),
        )
    config = _google_client_config()
    if config is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth credentials not configured. Set them in "
                "Settings > Service Credentials (admin)."
            ),
        )
    return config


# ---------------------------------------------------------------------------
# Per-user connection
# ---------------------------------------------------------------------------

def get_user_google_admin_oauth(user: dict) -> Optional[dict]:
    """The user's stored OAuth blob, or None when not connected."""
    rows = user.get("service_credentials") or {}
    return (rows.get(SERVICE_ID) or {}).get("oauth_blob")


def google_admin_connected(row: dict) -> bool:
    """``UserConnectionSpec.connected`` hook over the stored row."""
    return bool((row.get("oauth_blob") or {}).get("access_token"))


def google_admin_needs_reauth(row: dict) -> bool:
    """``UserConnectionSpec.needs_reauth`` hook: granted-scope check.

    The callback stores the scopes Google actually GRANTED (the token
    response's ``scope`` field), so a user who unticked a scope on the
    consent screen -- or a later widening of :data:`GOOGLE_ADMIN_SCOPES`
    -- shows the "Update Available" re-authorize badge instead of failing
    at call time with ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``.
    """
    granted = (row.get("oauth_blob") or {}).get("scopes") or []
    return not set(GOOGLE_ADMIN_SCOPES).issubset(set(granted))


def token_expires_within(blob: dict, margin_seconds: int = _REFRESH_MARGIN_SECONDS) -> bool:
    """Whether the blob's access token expires within ``margin_seconds``.

    An unparseable or missing ``expires_at`` reads as expired: Google
    access tokens always expire, so with no usable expiry the safe move is
    to refresh rather than send a possibly-dead token.
    """
    expires_at_str = blob.get("expires_at")
    if not expires_at_str:
        return True
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except (TypeError, ValueError):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (expires_at - now).total_seconds() < margin_seconds


def build_token_blob(
    token_data: dict,
    *,
    previous: Optional[dict] = None,
    account: Optional[dict] = None,
) -> dict:
    """Build the ``oauth_blob`` JSON from a token-endpoint response.

    ``previous`` supplies the fields a refresh response does not repeat:
    the refresh token (Google does not rotate it), the connected account,
    and the original ``authorized_at``.
    """
    previous = previous or {}
    now = datetime.now(timezone.utc)
    try:
        expires_at = now + timedelta(seconds=int(token_data.get("expires_in")))
    except (TypeError, ValueError):
        # Default to the usual 1-hour lifetime minus a safety haircut.
        expires_at = now + timedelta(minutes=55)
    scope_str = token_data.get("scope") or previous.get("scope") or ""
    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token")
        or previous.get("refresh_token"),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
        "scope": scope_str,
        "scopes": scope_str.split(),
        "account": account or previous.get("account"),
        "authorized_at": previous.get("authorized_at") or now.isoformat(),
    }


async def _refresh_google_admin_token(user: dict, blob: dict) -> Optional[str]:
    """Refresh the user's access token and persist the new blob.

    Returns the new access token, or None when refresh is impossible
    (plugin switched off, no refresh token, revoked grant, transport
    error). Callers treat None as "not connected" so the standard
    reconnect message surfaces instead of a raw exception.
    """
    email = user.get("email", "unknown")
    refresh_token = blob.get("refresh_token")
    if not refresh_token:
        return None

    try:
        config = load_google_admin_client_config()
    except Exception:
        logger.warning("[Google Admin] Token refresh skipped: not enabled / no Google OAuth client")
        return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
    except httpx.HTTPError as exc:
        logger.warning("[Google Admin] Token refresh transport error for %s: %s", email, exc)
        return None

    if response.status_code != 200:
        # invalid_grant here means the grant was revoked (or the admin
        # account's password / session policy invalidated it); the user
        # must reconnect in Settings > Data Connections.
        logger.warning(
            "[Google Admin] Token refresh failed for %s (HTTP %s): %s",
            email, response.status_code, response.text[:300],
        )
        return None

    token_data = response.json()
    if not token_data.get("access_token"):
        logger.warning("[Google Admin] Token refresh response had no access_token")
        return None

    new_blob = build_token_blob(token_data, previous=blob)

    from db.user_service_credential_store import upsert_credential
    await upsert_credential(user["id"], SERVICE_ID, oauth_blob=new_blob)

    # Mutate the in-memory user dict so later calls in the same turn see
    # the fresh token without a DB re-read.
    rows = user.setdefault("service_credentials", {})
    rows.setdefault(SERVICE_ID, {})["oauth_blob"] = new_blob

    logger.info("[Google Admin] Refreshed access token for %s", email)
    return new_blob["access_token"]


async def get_google_admin_token(user: dict) -> Optional[str]:
    """Get a valid access token for the user, refreshing when needed.

    Returns None when Google Workspace Admin is not connected or the
    refresh fails, so callers surface an actionable reconnect message.
    """
    blob = get_user_google_admin_oauth(user)
    if not blob or not blob.get("access_token"):
        return None

    if not token_expires_within(blob):
        return blob["access_token"]

    lock = _refresh_locks.setdefault(user.get("id"), asyncio.Lock())
    async with lock:
        # Another coroutine may have refreshed while we waited on the lock.
        blob = get_user_google_admin_oauth(user) or blob
        if not token_expires_within(blob):
            return blob.get("access_token")
        return await _refresh_google_admin_token(user, blob)


async def load_google_admin_credentials(user: dict):
    """authed_get credential loader: a valid access token, or None."""
    return await get_google_admin_token(user)


def inject_google_admin_bearer_auth(token: str, headers: dict) -> None:
    """authed_get auth injector: Bearer token header."""
    headers["Authorization"] = f"Bearer {token}"
