"""Ephemeral, in-memory bearer tokens for script-sandbox containers.

Every ``run_script`` / ``run_python`` container used to receive the user's
long-lived ``users.api_key`` as ``QUEST_API_KEY``. That key authenticates
the whole main-app HTTP surface, so any path that carried it out of the
container (echoed tool output, prompt injection, a provider-side leak)
yielded a full-account bearer (issue #201).

This module replaces it with a per-run token:

* :func:`issue_sandbox_token` mints a random ``qsb_``-prefixed token bound
  to one user and the launching conversation's context (its id, project and
  public flag, which the ``POST /api/tool-call`` bridge hands to tool
  dispatch so a script sees the docs and tables its conversation sees) with
  a hard expiry equal to the container's maximum lifetime plus a small
  grace.
* :func:`revoke_sandbox_token` drops it the moment the container exits;
  :func:`sandbox_token_lease` wraps both around the container run.
* :func:`resolve_sandbox_token` is what the sandbox tool API server's auth
  dependency (:func:`get_current_sandbox_user`) consults. The main app
  never consults it, so a sandbox token cannot authenticate anything but
  the loopback-only sandbox routes -- and those routes accept nothing
  else (``users.api_key`` and session cookies are rejected there).

The mapping is deliberately process-local: containers die with the server
process, so there is nothing worth persisting, and a restart invalidates
every outstanding token by construction.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "qsb_"

# Slack added on top of the container's clamped timeout so a token cannot
# expire underneath a script that is still inside its allowed window
# (podman start-up latency is inside the timeout, but wall-clock skew
# between issue time and the timer start is not).
TOKEN_GRACE_SECONDS = 15


@dataclass(frozen=True)
class SandboxLease:
    """One outstanding sandbox token."""

    user_id: int
    conversation_id: str | None
    expires_at: float  # time.monotonic() deadline
    # The launching conversation's project and mode. The script tool-call
    # bridge passes them to _dispatch_tool_call, so the Quest Docs / Tables
    # access rule scopes a script exactly like the conversation that ran it
    # (read-only, see chat/artifact_access.py). Public containers never get
    # a token, so is_public is False on every lease a script can present.
    project_id: str | None = None
    is_public: bool = False
    # Set for containers launched from a one-shot inference API run: the
    # sandbox tool API then refuses the mutating dynamic tools on the
    # POST /api/tool-call bridge and the mutating HTTP routes (both from
    # chat.llm.tool_schemas), so a script cannot do what the run's own
    # tool dispatch refuses.
    block_mutating_tools: bool = False


_leases: dict[str, SandboxLease] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _purge_expired_locked(now: float) -> None:
    expired = [tok for tok, lease in _leases.items() if lease.expires_at <= now]
    for tok in expired:
        del _leases[tok]


def issue_sandbox_token(
    user_id: int,
    *,
    ttl_seconds: float,
    conversation_id: str | None = None,
    project_id: str | None = None,
    is_public: bool = False,
    block_mutating_tools: bool = False,
) -> str:
    """Mint a token for *user_id* valid for ``ttl_seconds`` from now."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    with _lock:
        _purge_expired_locked(now)
        _leases[token] = SandboxLease(
            user_id=int(user_id),
            conversation_id=conversation_id,
            expires_at=now + ttl_seconds,
            project_id=project_id,
            is_public=is_public,
            block_mutating_tools=block_mutating_tools,
        )
    return token


def revoke_sandbox_token(token: str) -> None:
    """Invalidate *token* immediately (no-op if unknown or already gone)."""
    with _lock:
        _leases.pop(token, None)


def resolve_sandbox_token(token: str) -> SandboxLease | None:
    """Return the live lease for *token*, or ``None`` if unknown/expired."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    now = _now()
    with _lock:
        _purge_expired_locked(now)
        return _leases.get(token)


def active_lease_count() -> int:
    """Number of unexpired tokens (diagnostics / tests)."""
    now = _now()
    with _lock:
        _purge_expired_locked(now)
        return len(_leases)


@contextlib.contextmanager
def sandbox_token_lease(
    user_id: int,
    *,
    ttl_seconds: float,
    conversation_id: str | None = None,
    project_id: str | None = None,
    is_public: bool = False,
    block_mutating_tools: bool = False,
) -> Iterator[str]:
    """Issue a token for the duration of a container run and revoke it after.

    The revoke runs on every exit path (normal completion, timeout kill,
    exception), so a container that outlives its ``podman run`` client --
    e.g. after the timeout SIGKILLs the client but conmon keeps the
    container alive briefly -- is left holding a dead credential. The
    expiry is only the backstop for the server task itself dying mid-run.
    """
    token = issue_sandbox_token(
        user_id, ttl_seconds=ttl_seconds, conversation_id=conversation_id,
        project_id=project_id, is_public=is_public,
        block_mutating_tools=block_mutating_tools,
    )
    try:
        yield token
    finally:
        revoke_sandbox_token(token)


async def get_current_sandbox_user(request: Request) -> dict:
    """FastAPI dependency: authenticate a sandbox-token bearer ONLY.

    Installed on the sandbox tool API app via ``dependency_overrides`` in
    place of every cookie/``users.api_key`` auth dependency the shared
    script-facing routes declare (see ``create_sandbox_app``). The user is
    re-read from the DB on every call so credential/connection changes
    made during a run are honoured.
    """
    from db.user_store import get_user_by_id

    auth_header = request.headers.get("Authorization", "")
    lease = None
    if auth_header.startswith("Bearer "):
        lease = resolve_sandbox_token(auth_header[7:].strip())
    user = await get_user_by_id(lease.user_id) if lease is not None else None
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_sandbox_token",
                "message": (
                    "Authentication required. Sandbox scripts must send "
                    "'Authorization: Bearer <QUEST_API_KEY>' using the token "
                    "injected into the container; it is only valid while the "
                    "container is running."
                ),
            },
        )
    # Expose the lease to the route body (the script tool-call bridge reads
    # block_mutating_tools through get_sandbox_lease) and refuse the
    # mutating HTTP routes outright for restricted leases.
    request.state.sandbox_lease = lease
    if lease.block_mutating_tools:
        from chat.llm.tool_schemas import MUTATING_PROXY_PATHS
        if request.url.path.rstrip("/") in MUTATING_PROXY_PATHS:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "mutating_route_blocked",
                    "message": (
                        f"'{request.url.path}' is not available from this "
                        "container: it changes external state and the "
                        "launching inference API run is read-only."
                    ),
                },
            )
    return user


def get_sandbox_lease(request: Request) -> SandboxLease | None:
    """FastAPI dependency: the lease behind this request, if any.

    Populated by :func:`get_current_sandbox_user` on the sandbox tool API;
    ``None`` on the main app (cookie / ``users.api_key`` callers hold no
    lease and no sandbox restrictions).
    """
    return getattr(request.state, "sandbox_lease", None)
