"""Session/cookie management and FastAPI authentication dependencies.

Session cookies are signed with a timed serializer and verified against a
server-side lifetime (``session_max_age()``) and the account's session
generation (``users.session_epoch``), so a cookie stops verifying once its
lifetime is over or the account's sessions are revoked
(``revoke_sessions()``), whatever the browser does with it.

Also home of the admin-impersonation guards: the ``forbid_impersonation``
dependency for routes an impersonation session may not use, and the
per-request impersonation audit line (``ImpersonationAuditMiddleware`` plus
the hook in ``get_user_from_cookie``).
"""

import contextvars
import logging
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from db.user_store import bump_session_epoch, get_user_by_api_key, get_user_by_id
from auth.config import COOKIE_NAME, COOKIE_SECURE, COOKIE_VERSION, get_secret_key

logger = logging.getLogger(__name__)


# Session lifetimes, enforced server-side through the signature timestamp
# and mirrored in the browser's max_age. session_max_age() picks one.
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
IMPERSONATION_MAX_AGE_SECONDS = 60 * 60  # 1 hour

_SESSION_SALT = "quest-session-timed"


def get_session_serializer() -> URLSafeTimedSerializer:
    """The timed serializer that signs and verifies session cookies."""
    return URLSafeTimedSerializer(get_secret_key(), salt=_SESSION_SALT)


def session_max_age(payload: dict) -> int:
    """Lifetime in seconds of a session cookie with this payload.

    Impersonation cookies (``imp`` present) live for an hour, every other
    session for 30 days. Issuing (the browser ``max_age``) and verifying
    (the signature age) both go through here.
    """
    if payload.get("imp") is not None:
        return IMPERSONATION_MAX_AGE_SECONDS
    return SESSION_MAX_AGE_SECONDS


def _is_int(value, minimum: int) -> bool:
    """A real int (bool is a subclass of int in Python) of at least *minimum*."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _load_session_payload(signed_cookie: str) -> Optional[dict]:
    """Verify a session cookie's signature and age; return its payload.

    The age limit depends on the payload (``session_max_age()``), which is
    only readable after a first verification under the longest lifetime;
    a shorter-lived cookie is then re-checked under its own limit.
    """
    serializer = get_session_serializer()
    try:
        payload = serializer.loads(signed_cookie, max_age=SESSION_MAX_AGE_SECONDS)
        if not isinstance(payload, dict):
            return None
        max_age = session_max_age(payload)
        if max_age < SESSION_MAX_AGE_SECONDS:
            serializer.loads(signed_cookie, max_age=max_age)
    except BadSignature:  # includes SignatureExpired and BadTimeSignature
        return None
    return payload


async def get_user_from_cookie(signed_cookie: str) -> Optional[dict]:
    """Deserialize a session cookie and return the user dict.

    Only accepts cookies that verify under the timed session serializer
    within their lifetime (``session_max_age()``), match the current
    COOKIE_VERSION, carry a valid positive integer user ID and carry the
    account's current ``session_epoch`` (``e``). Rejects version
    mismatches, boolean uid / epoch values (bool is a subclass of int in
    Python), and non-positive IDs. Cookies signed without a timestamp
    (the pre-timed format) fail verification.
    """
    if not signed_cookie:
        return None
    cookie_value = _load_session_payload(signed_cookie)
    if cookie_value is None:
        return None

    # Strict version check: must be exactly COOKIE_VERSION (int),
    # not a float, bool, or other numeric type
    version = cookie_value.get("v")
    if not _is_int(version, 0) or version != COOKIE_VERSION:
        return None

    # Strict user ID check: must be a positive int, NOT a bool
    user_id = cookie_value.get("uid")
    if not _is_int(user_id, 1):
        return None

    epoch = cookie_value.get("e")
    if not _is_int(epoch, 0):
        return None

    user = await get_user_by_id(user_id)
    if user is None:
        return None

    # Server-side revocation: a cookie belongs to the session generation it
    # was issued under. Sign-out and password changes bump the stored epoch
    # (revoke_sessions(), set_password_hash() in db/password_store.py),
    # ending every older cookie of the account. The user row is loaded
    # here anyway, so the check costs no extra query.
    if epoch != user.get("session_epoch"):
        return None

    # Password-issued sessions carry the fingerprint of the password hash
    # they were issued under ("pw"). They die when the password changes
    # (new salt -> new fingerprint) and whenever the deployment is not in
    # password sign-in mode (after the switch to Google sign-in, users must
    # sign in again with Google).
    pw = cookie_value.get("pw")
    if pw is not None:
        from auth.config import is_password_login
        if not is_password_login():
            return None
        if not isinstance(pw, str) or pw != user.get("password_fp"):
            return None

    # Check for impersonation: if "imp" (impersonator user ID) is present,
    # validate the impersonator is still a valid admin whose own session
    # generation ("ie") is current. If so, annotate the returned user dict
    # with impersonator info; otherwise treat the cookie as invalid.
    imp_uid = cookie_value.get("imp")
    if imp_uid is not None:
        if not _is_int(imp_uid, 1):
            return None
        imp_epoch = cookie_value.get("ie")
        if not _is_int(imp_epoch, 0):
            return None
        impersonator = await get_user_by_id(imp_uid)
        if impersonator is None:
            return None
        if imp_epoch != impersonator.get("session_epoch"):
            return None
        from chat.auth import is_admin as check_is_admin
        if not check_is_admin(impersonator["email"]):
            # Admin access revoked -- invalidate impersonation session
            return None
        user["_impersonator_uid"] = imp_uid
        user["_impersonator_email"] = impersonator["email"]
        user["_impersonator_name"] = impersonator.get("name", "")
        _audit_impersonated_request(user)

    return user


# ---------------------------------------------------------------------------
# Admin impersonation: audit line and forbidden routes
# ---------------------------------------------------------------------------

# The request being served, for the impersonation audit line: method, path,
# and whether the line was already written. ImpersonationAuditMiddleware sets
# a fresh record per HTTP request / WebSocket connection; the record is a
# mutable dict so a write from a dependency (or a task the handler spawned,
# which runs on a copy of the context) is seen by the whole request.
_audit_request: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "quest_impersonation_audit_request", default=None,
)


def _audit_impersonated_request(user: dict) -> None:
    """Log that the current request is served under impersonation.

    Called by ``get_user_from_cookie`` once it has validated an impersonation
    cookie, so every request that resolves an identity through an
    impersonation session is attributed to the admin behind it. One line per
    request: an identity can be resolved more than once in a request (route
    dependency plus handler, the WebSocket heartbeat's re-validation).
    """
    record = _audit_request.get()
    if record is None:
        # Outside the middleware (no request context): still attribute it.
        method, path = "-", "-"
    else:
        if record["logged"]:
            return
        record["logged"] = True
        method, path = record["method"], record["path"]
    logger.info(
        "[impersonation] admin=%s (id=%s) as user=%s (id=%s): %s %s",
        user.get("_impersonator_email"), user.get("_impersonator_uid"),
        user.get("email"), user.get("id"), method, path,
    )


class ImpersonationAuditMiddleware:
    """ASGI middleware giving the impersonation audit line its request.

    Pure ASGI (not ``BaseHTTPMiddleware``) so it covers WebSocket
    connections too and leaves streaming responses untouched. It only scopes
    the ``_audit_request`` record; the line itself is written where the
    impersonation is validated (``get_user_from_cookie``) or refused
    (``forbid_impersonation``). The query string is left out: OAuth
    callbacks carry codes in it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET") if kind == "http" else "WEBSOCKET"
        token = _audit_request.set(
            {"method": method, "path": scope.get("path", ""), "logged": False}
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _audit_request.reset(token)


def session_impersonation_claim(request: Request) -> Optional[tuple]:
    """Return ``(user_id, impersonator_id)`` when the session cookie is an
    impersonation cookie, else ``None``.

    Checks the signature only (no database lookup, no age or epoch check):
    any correctly signed session cookie that carries an ``imp`` field
    counts, even a malformed, expired or revoked one, so the guard fails
    closed. A cookie with a bad signature carries no trusted claim and does
    not count (it resolves no identity either).
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        return None
    try:
        payload = get_session_serializer().loads(signed_cookie)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("imp") is None:
        return None
    return payload.get("uid"), payload.get("imp")


IMPERSONATION_FORBIDDEN_MESSAGE = (
    "This action is not available while impersonating another user. "
    "Stop impersonating first."
)


async def forbid_impersonation(request: Request) -> None:
    """FastAPI dependency: refuse the route to an impersonation session.

    Raises 403 ``impersonation_forbidden`` when the request's session cookie
    is an impersonation cookie (``session_impersonation_claim``). Declared
    via ``dependencies=[Depends(forbid_impersonation)]`` on routes that
    issue, reveal, rotate or revoke credentials or destroy the account, and
    on the credential-connection routers. Route-level dependencies run
    before the route's user dependency, so this check never touches the
    database and logs the refused request itself (by user id; the
    "started impersonating" line maps ids to emails).
    """
    claim = session_impersonation_claim(request)
    if claim is None:
        return
    target_uid, admin_uid = claim
    logger.warning(
        "[impersonation] refused admin id=%s as user id=%s: %s %s",
        admin_uid, target_uid, request.method, request.url.path,
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "impersonation_forbidden",
            "message": IMPERSONATION_FORBIDDEN_MESSAGE,
        },
    )


def _set_cookie(response, payload: dict):
    response.set_cookie(
        key=COOKIE_NAME,
        value=get_session_serializer().dumps(payload),
        httponly=True,
        max_age=session_max_age(payload),
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


def _epoch(user: dict) -> int:
    return user.get("session_epoch") or 0


def set_session_cookie(response, user: dict, password_fp: Optional[str] = None):
    """Attach a signed session cookie for *user* to *response*.

    *user* is the account's user dict; its ``session_epoch`` goes into the
    cookie, so pass a dict loaded after any epoch bump (e.g. after
    set_password_hash()) or the new cookie is stale on arrival.
    ``password_fp`` binds a password-issued session to the current
    password hash (see get_user_from_cookie).
    """
    payload = {"v": COOKIE_VERSION, "uid": user["id"], "e": _epoch(user)}
    if password_fp:
        payload["pw"] = password_fp
    return _set_cookie(response, payload)


def set_impersonation_cookie(response, target: dict, admin: dict):
    """Attach a one-hour session cookie in which *admin* acts as *target*.

    Carries both accounts' session epochs (``e`` for the target, ``ie`` for
    the admin), so revoking either account's sessions ends it.
    """
    payload = {
        "v": COOKIE_VERSION,
        "uid": target["id"],
        "e": _epoch(target),
        "imp": admin["id"],
        "ie": _epoch(admin),
    }
    return _set_cookie(response, payload)


def session_owner_id(user: dict) -> int:
    """Id of the account a resolved session belongs to: the admin while
    impersonating, otherwise the user."""
    return user.get("_impersonator_uid") or user["id"]


async def revoke_sessions(user: dict) -> None:
    """End every session of the account behind *user*'s session.

    Bumps the session owner's epoch (``session_owner_id()``): an admin
    signing out of an impersonation ends the admin's sessions, not the
    impersonated user's.
    """
    await bump_session_epoch(session_owner_id(user))


async def revoke_cookie_session(request: Request) -> None:
    """Sign-out: revoke the sessions of the account behind the request's
    session cookie. A missing or invalid cookie revokes nothing."""
    signed_cookie = request.cookies.get(COOKIE_NAME)
    user = await get_user_from_cookie(signed_cookie) if signed_cookie else None
    if user is not None:
        await revoke_sessions(user)


def _require_allowed(user: dict) -> dict:
    """Apply the admission policy (allowed domain / email whitelist).

    Lazy import: ``chat.auth`` imports this module (same pattern as the
    ``is_admin`` check in ``get_user_from_cookie``).
    """
    from chat.auth import require_user_allowed
    return require_user_allowed(user)


async def get_current_user(request: Request) -> dict:
    """Extract and validate API key from Authorization header.

    Raises 401 on a missing/unknown key and 403 when the key's owner no
    longer passes the admission policy.
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "Invalid or missing API key. Include 'Authorization: Bearer <key>' header."
            }
        )

    api_key = auth_header[7:]  # Remove "Bearer " prefix
    user = await get_user_by_api_key(api_key)

    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "Invalid or missing API key. Include 'Authorization: Bearer <key>' header."
            }
        )

    return _require_allowed(user)


async def get_current_user_cookie_or_apikey(request: Request) -> dict:
    """Authenticate via session cookie or API key. Used by endpoints
    that serve both the browser frontend and script/LLM clients.

    Raises 401 when neither credential resolves a user and 403 when the
    resolved user no longer passes the admission policy.
    """
    # Try cookie
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if signed_cookie:
        user = await get_user_from_cookie(signed_cookie)
        if user:
            return _require_allowed(user)

    # Try API key
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        user = await get_user_by_api_key(api_key)
        if user:
            return _require_allowed(user)

    raise HTTPException(
        status_code=401,
        detail={
            "error": "not_authenticated",
            "message": "Authentication required."
        }
    )
