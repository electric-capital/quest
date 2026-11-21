"""Shared lifecycle helpers for per-user OAuth tokens that expire.

A plugin whose upstream issues expiring access tokens keeps the token JSON
in the ``oauth_blob`` of the user's ``user_service_credentials`` row and
refreshes it shortly before expiry. The provider-specific parts -- the
token endpoint request and the blob layout -- stay in the plugin's
``upstream.py``; the parts that are the same for every provider live here:

- :func:`token_expires_within` -- the near-expiry check over ``expires_at``;
- :func:`get_user_oauth_blob` / :func:`set_user_oauth_blob` -- the blob on
  the in-memory user dict;
- :func:`store_user_oauth_blob` -- persist a refreshed blob and update the
  user dict;
- :func:`current_or_refreshed_token` -- return the stored access token, or
  refresh it under a per-user lock when it is about to expire.

Like the rest of ``config/``, this module is stdlib-only at import time;
the credential store is imported lazily inside the function that needs it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

# Safety margin: refresh a token that expires within 5 minutes.
REFRESH_MARGIN_SECONDS = 300


def token_expires_within(
    blob: dict,
    margin_seconds: int = REFRESH_MARGIN_SECONDS,
    *,
    missing_means_expired: bool = True,
) -> bool:
    """Whether the blob's access token expires within ``margin_seconds``.

    ``expires_at`` is an ISO timestamp; a naive one is read as UTC. An
    unparseable value reads as expired, so a refresh is attempted rather
    than a possibly-dead token sent. A missing value reads as expired too
    unless ``missing_means_expired`` is False, which is for providers that
    also issue tokens with no expiry at all.
    """
    expires_at_str = blob.get("expires_at")
    if not expires_at_str:
        return missing_means_expired
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except (TypeError, ValueError):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (expires_at - now).total_seconds() < margin_seconds


def get_user_oauth_blob(user: dict, service: str) -> Optional[dict]:
    """The user's stored OAuth blob for ``service``, or None when not connected.

    Reads the default ``user_service_credentials`` row that
    db/user_store.py attaches to the user dict.
    """
    rows = user.get("service_credentials") or {}
    return (rows.get(service) or {}).get("oauth_blob")


def set_user_oauth_blob(user: dict, service: str, blob: dict) -> None:
    """Point the in-memory user dict at ``blob``, so later calls in the
    same turn see it without a DB read."""
    rows = user.setdefault("service_credentials", {})
    rows.setdefault(service, {})["oauth_blob"] = blob


async def store_user_oauth_blob(user: dict, service: str, blob: dict) -> None:
    """Persist ``blob`` as the user's credential for ``service`` and update
    the in-memory user dict to match."""
    from db.user_service_credential_store import upsert_credential

    await upsert_credential(user["id"], service, oauth_blob=blob)
    set_user_oauth_blob(user, service, blob)


async def current_or_refreshed_token(
    user: dict,
    service: str,
    refresh: Callable[[dict, dict], Awaitable[Optional[str]]],
    locks: dict[Any, asyncio.Lock],
    *,
    force_refresh: bool = False,
) -> Optional[str]:
    """The user's access token for ``service``, refreshed when near expiry.

    Returns None when the user is not connected. A token that is not about
    to expire is returned as stored. Otherwise ``refresh(user, blob)`` runs
    under the user's lock from ``locks`` (so concurrent tool calls in one
    turn do not race duplicate refresh requests, which would lose a rotated
    refresh token) and its result is returned: the new access token, or
    None when the refresh failed. ``force_refresh`` skips the expiry check,
    for a retry after the upstream answered 401.
    """
    blob = get_user_oauth_blob(user, service)
    if not blob or not blob.get("access_token"):
        return None

    if not force_refresh and not token_expires_within(blob):
        return blob["access_token"]

    lock = locks.setdefault(user.get("id"), asyncio.Lock())
    async with lock:
        # Another coroutine may have refreshed while we waited on the lock.
        blob = get_user_oauth_blob(user, service) or blob
        if not force_refresh and not token_expires_within(blob):
            return blob.get("access_token")
        return await refresh(user, blob)
