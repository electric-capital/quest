"""GitHub upstream access helpers.

Server-side configuration (the admin ``github`` credential-store entry
with the OAuth app's client id/secret) and per-user OAuth tokens (a
``user_service_credentials`` row with token JSON in ``oauth_blob``,
attached to user dicts as ``user["service_credentials"]["github"]``)
both resolve here, so the OAuth router, the authed_get service entry,
and the plugin tool share one implementation.

Two token shapes arrive depending on what the admin registered:

* **Classic OAuth App** (``gho_``): the token never expires and the
  exchange returns no refresh token, so the loader hands it out as-is.
* **GitHub App** with "Expire user authorization tokens" on (GitHub's
  default): the ``ghu_`` access token lives 8 hours and comes with a
  ``ghr_`` refresh token good for 6 months. :func:`get_github_token`
  refreshes near-expiry tokens under a per-user lock and persists the
  rotated pair, so routines keep working unattended. Every refresh
  invalidates BOTH the old access token and the old refresh token, so
  the loader reads the stored row rather than trusting a user dict that
  another turn or routine may have outdated.

A token revoked on GitHub's side surfaces as an upstream 401 (or a
failed refresh, reported as the reconnect message).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# OAuth scopes requested from GitHub. ``repo`` is required for private
# repository read access (GitHub has no read-only repo scope for OAuth
# apps); ``read:org`` covers org membership and org repo listings.
GITHUB_SCOPES = ("repo", "read:org")

# Code exchange and refresh share one endpoint.
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Safety margin: refresh the token if it expires within 5 minutes.
_REFRESH_MARGIN_SECONDS = 300

_HTTP_TIMEOUT = 30.0

# Per-user refresh locks so concurrent tool calls do not race duplicate
# refreshes (the loser would present an already-rotated refresh token).
_refresh_locks: dict[Any, asyncio.Lock] = {}

MISSING_CREDENTIALS_ERROR = {
    "error": "github_oauth_required",
    "message": (
        "GitHub not connected (or the connection expired). "
        "Please connect GitHub in Settings > Data Connections."
    ),
}


def load_github_client_config() -> dict:
    """Load the GitHub OAuth app client config (admin credential store).

    Prefers the per-service credential store and falls back to the
    "github" section of the legacy server_credentials.json. Store reads
    are fresh on every call so admin updates take effect without a
    restart. Raises a 500 HTTPException when unconfigured (mirrors the
    other OAuth client-config loaders in auth/config.py).
    """
    from fastapi import HTTPException

    from config.service_credentials import (
        read_legacy_service_credentials,
        read_service_credentials,
    )

    stored = read_service_credentials("github")
    if stored:
        return stored
    legacy = read_legacy_service_credentials("github")
    if legacy:
        return legacy
    raise HTTPException(
        status_code=500,
        detail=(
            "GitHub OAuth credentials not configured. Set them in "
            "Settings > Service Credentials (admin) or in "
            "server_credentials.json."
        ),
    )


def github_is_configured(config: dict) -> bool:
    """Admin ``is_configured`` predicate: both client fields present."""
    return bool(config.get("client_id") and config.get("client_secret"))


def get_user_github_oauth(user: dict) -> Optional[dict]:
    """The user's stored GitHub OAuth blob, or None when not connected.

    Reads the ``user_service_credentials`` row attached to the user dict
    by db/user_store.py (service key ``github``).
    """
    rows = user.get("service_credentials") or {}
    return (rows.get("github") or {}).get("oauth_blob")


def github_connected(row: dict) -> bool:
    """``UserConnectionSpec.connected`` hook over the stored row."""
    return bool((row.get("oauth_blob") or {}).get("access_token"))


def github_needs_reauth(row: dict) -> bool:
    """``UserConnectionSpec.needs_reauth`` hook: granted-scope check.

    The callback stores the scopes GitHub actually GRANTED (not the ones
    requested), so widening ``GITHUB_SCOPES`` later flags existing
    connections with the "Update Available" re-authorize badge instead of
    failing at call time. Blobs migrated from the pre-plugin
    ``users.github_oauth`` column carry the raw comma-separated ``scope``
    string; both shapes are read here.

    Scopes only exist for classic OAuth Apps (``gho_`` tokens). When the
    client credentials belong to a **GitHub App** instead, the token
    exchange returns a ``ghu_`` user access token with an empty ``scope``
    -- permissions come from the app installation, so the subset check
    would flag re-auth forever. Skip it for those tokens.
    """
    blob = row.get("oauth_blob") or {}
    token = blob.get("access_token") or ""
    if token.startswith("ghu_"):
        return False
    granted = blob.get("scopes")
    if granted is None:
        granted = [s.strip() for s in (blob.get("scope") or "").split(",")]
    granted_set = {s for s in granted if s}
    return not set(GITHUB_SCOPES).issubset(granted_set)


def token_expires_within(blob: dict, margin_seconds: int = _REFRESH_MARGIN_SECONDS) -> bool:
    """Whether the blob's access token expires within ``margin_seconds``.

    A blob without ``expires_at`` never expires (classic OAuth App tokens,
    and GitHub App tokens with expiration opted out). An unparseable
    timestamp reads as expired so a refresh is attempted.
    """
    expires_at_str = blob.get("expires_at")
    if not expires_at_str:
        return False
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except (TypeError, ValueError):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (expires_at - now).total_seconds() < margin_seconds


def build_token_blob(token_data: dict, *, previous: Optional[dict] = None) -> dict:
    """Build the ``oauth_blob`` JSON from a token-endpoint response.

    Used for both the initial code exchange and refreshes. ``previous``
    carries the original ``authorized_at`` (and the granted scope, which a
    refresh response does not repeat) across refreshes. The expiry and
    refresh-token fields are present only when GitHub issued an expiring
    token; the refresh token is never carried over because GitHub
    invalidates it on use.
    """
    previous = previous or {}
    now = datetime.now(timezone.utc)
    # GitHub reports the GRANTED scopes as a comma-separated string;
    # store them as a list too so the needs_reauth hook can compare
    # against GITHUB_SCOPES without re-parsing.
    scope_str = token_data.get("scope") or previous.get("scope") or ""
    blob = {
        "access_token": token_data.get("access_token"),
        "token_type": token_data.get("token_type", "bearer"),
        "scope": scope_str,
        "scopes": [s.strip() for s in scope_str.split(",") if s.strip()],
        "authorized_at": previous.get("authorized_at") or now.isoformat(),
    }
    if token_data.get("refresh_token"):
        blob["refresh_token"] = token_data["refresh_token"]
    for field, lifetime_field in (
        ("expires_at", "expires_in"),
        ("refresh_token_expires_at", "refresh_token_expires_in"),
    ):
        try:
            blob[field] = (now + timedelta(seconds=int(token_data[lifetime_field]))).isoformat()
        except (KeyError, TypeError, ValueError):
            pass
    return blob


def _set_user_blob(user: dict, blob: dict) -> None:
    """Point the in-memory user dict at ``blob`` so later calls in the
    same turn see it without a DB read."""
    rows = user.setdefault("service_credentials", {})
    rows.setdefault("github", {})["oauth_blob"] = blob


async def _load_stored_blob(user: dict) -> Optional[dict]:
    """Re-read the user's stored GitHub blob (None once disconnected)."""
    from db.user_service_credential_store import get_credential

    row = await get_credential(user["id"], "github")
    blob = (row or {}).get("oauth_blob")
    if blob:
        _set_user_blob(user, blob)
    return blob


async def _refresh_github_token(user: dict, blob: dict) -> Optional[str]:
    """Refresh the user's access token and persist the rotated blob.

    Returns the new access token. When the refresh fails (server
    unconfigured, refresh token expired or revoked, transport error) it
    falls back to the current access token while that has not actually
    expired yet, and otherwise returns None so callers surface the
    reconnect message.
    """
    fallback = None if token_expires_within(blob, 0) else blob.get("access_token")
    email = user.get("email", "unknown")
    if not blob.get("refresh_token"):
        return fallback

    try:
        config = load_github_client_config()
    except Exception:
        logger.warning("[GitHub] Token refresh skipped: server credentials not configured")
        return fallback

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": config.get("client_id"),
                    "client_secret": config.get("client_secret"),
                    "grant_type": "refresh_token",
                    "refresh_token": blob["refresh_token"],
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("[GitHub] Token refresh transport error for %s: %s", email, exc)
        return fallback

    # GitHub reports refresh errors (e.g. bad_refresh_token once the
    # 6-month refresh token has aged out) as HTTP 200 with an error body.
    try:
        token_data = response.json()
    except ValueError:
        token_data = {}
    if response.status_code != 200 or token_data.get("error") or not token_data.get("access_token"):
        logger.warning(
            "[GitHub] Token refresh failed for %s (HTTP %s): %s",
            email, response.status_code,
            token_data.get("error_description") or token_data.get("error") or response.text[:300],
        )
        return fallback

    new_blob = build_token_blob(token_data, previous=blob)

    from db.user_service_credential_store import upsert_credential
    await upsert_credential(user["id"], "github", oauth_blob=new_blob)
    _set_user_blob(user, new_blob)

    logger.info("[GitHub] Refreshed access token for %s", email)
    return new_blob["access_token"]


async def get_github_token(user: dict) -> Optional[str]:
    """Get a valid GitHub access token, refreshing when needed.

    Returns None when GitHub is not connected or an expired token could
    not be refreshed, so callers surface the reconnect message.
    """
    blob = get_user_github_oauth(user)
    if not blob or not blob.get("access_token"):
        return None
    if not blob.get("refresh_token"):
        # Non-expiring token -- nothing to refresh.
        return blob["access_token"]

    # Expiring token: start from the stored row, since another turn or
    # routine may have refreshed (and thereby invalidated our copy).
    blob = await _load_stored_blob(user)
    if not blob or not blob.get("access_token"):
        return None
    if not token_expires_within(blob):
        return blob["access_token"]

    lock = _refresh_locks.setdefault(user["id"], asyncio.Lock())
    async with lock:
        # Another coroutine may have refreshed while we waited on the lock.
        blob = await _load_stored_blob(user)
        if not blob or not blob.get("access_token"):
            return None
        if not token_expires_within(blob):
            return blob["access_token"]
        return await _refresh_github_token(user, blob)


async def load_github_credentials(user: dict):
    """authed_get credential loader: a valid access token, or None."""
    return await get_github_token(user)


def inject_github_bearer_auth(token: str, headers: dict) -> None:
    """authed_get auth injector: Bearer token header."""
    headers["Authorization"] = f"Bearer {token}"


# ---------------------------------------------------------------------------
# Direct REST access for the action-request handlers
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"

# Same defaults the authed_get service entry sends (GitHub rejects requests
# without a User-Agent).
GITHUB_DEFAULT_HEADERS = {
    "User-Agent": "Quest/1.0",
    "Accept": "application/vnd.github+json",
}


class GitHubAuthError(Exception):
    """The user has no usable GitHub connection (not connected, or an
    expired token that could not be refreshed)."""


async def github_request(
    user: dict,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> httpx.Response:
    """Make an authenticated GitHub REST request for the action-request
    handlers (plugins/github/handlers.py).

    ``path`` is the absolute API path (``/repos/...``); callers quote the
    segments. The writes deliberately do NOT ride on the authed_get
    allow-list, which stays GET-only -- they are reachable only through
    the approval-gated handlers. Injects a valid (proactively refreshed)
    Bearer token and retries once on 401 after re-running the loader,
    which re-reads the stored row and so picks up a token another turn
    refreshed in the meantime. Raises :class:`GitHubAuthError` when the
    user is not connected; upstream HTTP errors are returned as the
    response for the caller to classify.
    """
    token = await get_github_token(user)
    if not token:
        raise GitHubAuthError(MISSING_CREDENTIALS_ERROR["message"])

    url = f"{GITHUB_API_BASE}{path}"
    request_headers = {**GITHUB_DEFAULT_HEADERS, **(headers or {})}

    async with httpx.AsyncClient(timeout=timeout) as client:
        request_headers["Authorization"] = f"Bearer {token}"
        response = await client.request(
            method, url, params=params, json=json_body, headers=request_headers,
        )
        if response.status_code == 401:
            retry_token = await get_github_token(user)
            if retry_token and retry_token != token:
                request_headers["Authorization"] = f"Bearer {retry_token}"
                response = await client.request(
                    method, url, params=params, json=json_body, headers=request_headers,
                )
    return response


def github_error_message(response: httpx.Response) -> str:
    """A short human-readable error from a GitHub error response.

    GitHub error bodies are ``{"message": ..., "errors": [...]}`` where
    each entry of ``errors`` is a string or a ``{resource, field, code,
    message}`` dict; falls back to the raw text for anything else.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}: {response.text[:300]}"

    parts = [str(payload.get("message") or "").strip()]
    for err in payload.get("errors") or []:
        if isinstance(err, dict):
            detail = err.get("message") or ", ".join(
                str(err[k]) for k in ("resource", "field", "code") if err.get(k)
            )
        else:
            detail = str(err)
        if detail:
            parts.append(detail)
    message = "; ".join(p for p in parts if p)
    return f"HTTP {response.status_code}: {message[:500] or response.text[:300]}"
