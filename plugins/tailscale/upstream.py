"""Tailscale upstream access helpers (plugin module).

The per-user credential (a ``user_service_credentials`` row whose ``secret``
is written by the generic ``POST /auth/service-key/tailscale`` route) resolves
here into the Bearer token the ``authed_get`` service entry injects.

Two credential shapes are accepted in that single field, told apart by
their prefix:

``tskey-api-...`` -- a personal **API access token** from the admin
    console (Settings > Keys). Used as the Bearer token verbatim. Carries
    the full rights of the admin who minted it and expires after at most
    90 days, after which the user pastes a new one.

``tskey-client-...`` -- an **OAuth client secret** (Settings > Trust
    credentials / OAuth clients). OAuth clients never expire and are the
    least-privilege option: the admin picks read-only scopes when creating
    the client, so the token Quest holds *cannot* write even if the
    allow-list were wrong. The secret is exchanged for a short-lived access
    token at ``POST /api/v2/oauth/token`` (client-credentials grant) and
    the result is cached in memory per user until shortly before it
    expires. The client id is embedded in the secret (the segment after
    ``tskey-client-``), so one pasted value is enough.

The plugin has no server-side configuration: ``api.tailscale.com`` is a
fixed public host and every credential is the user's own.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SERVICE_ID = "tailscale"

API_HOST = "api.tailscale.com"
API_BASE_URL = f"https://{API_HOST}/api/v2"
OAUTH_TOKEN_URL = f"{API_BASE_URL}/oauth/token"

API_TOKEN_PREFIX = "tskey-api-"
OAUTH_SECRET_PREFIX = "tskey-client-"
# Prefixes a user may paste by mistake, each with a specific hint.
_AUTH_KEY_PREFIX = "tskey-auth-"

# OAuth access tokens live 1 hour; refresh when this close to expiry.
_REFRESH_MARGIN_SECONDS = 300
# Fallback lifetime when the token response carries no ``expires_in``.
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600

_HTTP_TIMEOUT = 30.0

# Key / client ids are short alphanumeric segments (``kAbCdE1CNTRL``).
_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")

MISSING_CREDENTIALS_ERROR = {
    "error": "tailscale_token_required",
    "message": (
        "Tailscale is not connected (or the stored credential no longer "
        "works). Add a Tailscale API access token or OAuth client secret in "
        "Settings > Data Connections > Tailscale."
    ),
}


# ---------------------------------------------------------------------------
# Per-user connection (api_key kind)
# ---------------------------------------------------------------------------

def credential_kind(secret: str) -> Optional[str]:
    """``"api_token"`` / ``"oauth_client"`` for a supported secret, else None."""
    if secret.startswith(API_TOKEN_PREFIX):
        return "api_token"
    if secret.startswith(OAUTH_SECRET_PREFIX):
        return "oauth_client"
    return None


def oauth_client_id(secret: str) -> Optional[str]:
    """The client id embedded in an OAuth client secret.

    Tailscale secrets are ``tskey-client-<client id>-<random>``; the
    middle segment is the id the token endpoint expects as ``client_id``.
    Returns None when the secret does not have that shape.
    """
    if not secret.startswith(OAUTH_SECRET_PREFIX):
        return None
    rest = secret[len(OAUTH_SECRET_PREFIX):]
    client_id, sep, _tail = rest.partition("-")
    if not sep or not _ID_SEGMENT_RE.match(client_id):
        return None
    return client_id


def validate_credential(secret: str) -> Optional[str]:
    """``UserConnectionSpec.validate_key`` hook: shape check only.

    Accepts an API access token (``tskey-api-...``) or an OAuth client
    secret (``tskey-client-...``). Rejects whitespace, device auth keys
    (``tskey-auth-...``, which enrol machines and are useless against the
    API), and anything without a recognised prefix. Whether Tailscale
    accepts the value is found out on the first call (an API token that
    was revoked or expired answers 401; a bad client secret fails the
    token exchange).
    """
    if any(ch.isspace() for ch in secret):
        return "The credential must not contain spaces or line breaks."
    if secret.startswith(_AUTH_KEY_PREFIX):
        return (
            "That is a device auth key (tskey-auth-...), which enrols machines "
            "and cannot call the API. Paste an API access token (tskey-api-...) "
            "or an OAuth client secret (tskey-client-...) instead."
        )
    kind = credential_kind(secret)
    if kind is None:
        return (
            "Expected a Tailscale API access token starting with "
            "'tskey-api-' or an OAuth client secret starting with "
            "'tskey-client-'."
        )
    if kind == "oauth_client" and oauth_client_id(secret) is None:
        return (
            "That OAuth client secret is not in the expected "
            "'tskey-client-<client id>-<secret>' form."
        )
    if len(secret) < len(API_TOKEN_PREFIX) + 16:
        return "That is too short to be a Tailscale credential."
    if len(secret) > 256:
        return "That is too long to be a Tailscale credential."
    return None


def get_user_secret(user: dict) -> Optional[str]:
    """The user's stored Tailscale credential, or None when no row / empty.

    Reads the ``user_service_credentials`` row attached to the user dict
    by db/user_store.py (service key ``tailscale``).
    """
    rows = user.get("service_credentials") or {}
    secret = (rows.get(SERVICE_ID) or {}).get("secret")
    return secret.strip() if isinstance(secret, str) and secret.strip() else None


def tailscale_connected(row: dict) -> bool:
    """``UserConnectionSpec.connected`` hook: a credential is stored."""
    secret = row.get("secret")
    return isinstance(secret, str) and bool(secret.strip())


# ---------------------------------------------------------------------------
# OAuth client-credentials exchange (tskey-client-... secrets)
# ---------------------------------------------------------------------------

class _CachedToken:
    __slots__ = ("access_token", "expires_at", "secret")

    def __init__(self, access_token: str, expires_at: float, secret: str):
        self.access_token = access_token
        self.expires_at = expires_at
        self.secret = secret

    def usable_for(self, secret: str, now: float) -> bool:
        return (
            self.secret == secret
            and now < self.expires_at - _REFRESH_MARGIN_SECONDS
        )


# user id -> cached access token (process-local; tokens die with the process).
_token_cache: dict[Any, _CachedToken] = {}
# Per-user locks so parallel tool calls in one turn share a single exchange.
_exchange_locks: dict[Any, asyncio.Lock] = {}


def clear_token_cache() -> None:
    """Drop every cached OAuth access token (test seam)."""
    _token_cache.clear()
    _exchange_locks.clear()


async def _exchange_client_secret(secret: str, *, email: str) -> Optional[tuple[str, float]]:
    """Trade an OAuth client secret for an access token.

    Returns ``(access_token, expires_at_epoch)`` or None when Tailscale
    rejects the client (revoked / mistyped secret) or the request fails;
    callers treat None as "not connected" so the reconnect message
    surfaces instead of a raw exception.
    """
    client_id = oauth_client_id(secret)
    if client_id is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                OAUTH_TOKEN_URL,
                data={"client_id": client_id, "client_secret": secret},
                headers={"Accept": "application/json", "User-Agent": "Quest/1.0"},
            )
    except httpx.HTTPError as exc:
        logger.warning("[Tailscale] OAuth token exchange transport error for %s: %s", email, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "[Tailscale] OAuth token exchange failed for %s (HTTP %s): %s",
            email, response.status_code, response.text[:300],
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("[Tailscale] OAuth token response was not JSON")
        return None
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        logger.warning("[Tailscale] OAuth token response had no access_token")
        return None
    try:
        lifetime = int(payload.get("expires_in") or _DEFAULT_TOKEN_LIFETIME_SECONDS)
    except (TypeError, ValueError):
        lifetime = _DEFAULT_TOKEN_LIFETIME_SECONDS
    return access_token, time.time() + lifetime


async def get_tailscale_token(user: dict) -> Optional[str]:
    """A Bearer token for the user, or None when not connected.

    An API access token is returned as stored. An OAuth client secret is
    exchanged (once per hour per user) for an access token.
    """
    secret = get_user_secret(user)
    if secret is None:
        return None
    kind = credential_kind(secret)
    if kind == "api_token":
        return secret
    if kind != "oauth_client":
        # A stored value that predates / bypassed validate_credential.
        return None

    user_id = user.get("id")
    now = time.time()
    cached = _token_cache.get(user_id)
    if cached is not None and cached.usable_for(secret, now):
        return cached.access_token

    lock = _exchange_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        cached = _token_cache.get(user_id)
        now = time.time()
        if cached is not None and cached.usable_for(secret, now):
            return cached.access_token
        result = await _exchange_client_secret(secret, email=user.get("email", "unknown"))
        if result is None:
            _token_cache.pop(user_id, None)
            return None
        access_token, expires_at = result
        _token_cache[user_id] = _CachedToken(access_token, expires_at, secret)
        logger.info("[Tailscale] Exchanged OAuth client secret for %s", user.get("email", "unknown"))
        return access_token


# ---------------------------------------------------------------------------
# authed_get service hooks
# ---------------------------------------------------------------------------

async def load_tailscale_credentials(user: dict):
    """authed_get credential loader: a Bearer token, or None."""
    return await get_tailscale_token(user)


def inject_tailscale_bearer_auth(token: str, headers: dict) -> None:
    """authed_get auth injector: Bearer token header."""
    headers["Authorization"] = f"Bearer {token}"
