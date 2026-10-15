"""Email/password sign-in (the ``login_method: "password"`` alternative to Google).

Every route here 404s with ``password_login_disabled`` unless the deployment's
sign-in method (``login_method()`` in auth/config.py) is "password"; the
Google login routes refuse in the opposite case, so exactly one method is
ever active. Accounts are the same ``users`` rows either way, keyed by
email, so switching to Google sign-in later keeps every account.

Passwords are only ever set through a one-time link (``password_tokens``):

- an admin issues an *invite* link (new account or reset) from Settings >
  Sign-in, optionally emailed;
- with outgoing email (SMTP) configured, anyone whose address passes the
  admission policy can request a *reset* link from the sign-in screen --
  for an address without an account this is self-service sign-up, and
  the emailed link proves they own the address;
- the prod bootstrap wizard hashes the first admin's password itself and
  hands it over through the pending-passwords file (applied at startup by
  ``apply_pending_admin_passwords``).

Logged-in users can change their password (current password required when
one is set). Password-issued session cookies carry the password-hash
fingerprint, so a password change signs out every other session.
"""

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth.config import (
    COOKIE_NAME,
    generate_api_key,
    is_password_login,
    oauth_base_url,
)
from auth.mailer import MailerError, send_email, smtp_configured
from auth.session import forbid_impersonation, get_user_from_cookie, set_session_cookie
from config.password_hashing import (
    burn_verify_time,
    hash_password,
    password_fingerprint,
    password_problem,
    read_pending_admin_passwords,
    pending_admin_passwords_path,
    verify_password,
)
from db.password_store import (
    consume_password_token,
    create_password_token,
    get_password_hash,
    get_valid_password_token,
    set_password_hash,
)
from db.user_store import create_user, get_user_by_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/password")

# Self-service links (sign-up / forgot password) are short-lived; admin
# invites have to survive being forwarded and read later.
RESET_LINK_TTL = timedelta(hours=1)
INVITE_LINK_TTL = timedelta(days=7)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, per process)
# ---------------------------------------------------------------------------

class _SlidingWindow:
    """Allow at most ``limit`` events per key within ``window`` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._events: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque:
        events = self._events[key]
        while events and events[0] <= now - self.window:
            events.popleft()
        if not events:
            self._events.pop(key, None)
            return deque()
        return events

    def blocked(self, key: str) -> bool:
        return len(self._trim(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        self._trim(key, now)
        self._events[key].append(now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


# Failed password checks: per account and per client address.
_failed_by_email = _SlidingWindow(limit=10, window=15 * 60)
_failed_by_ip = _SlidingWindow(limit=50, window=15 * 60)
# Link requests: one email per address per minute, and a per-client cap.
_links_by_email = _SlidingWindow(limit=1, window=60)
_links_by_ip = _SlidingWindow(limit=10, window=15 * 60)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many_attempts() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "rate_limited",
            "message": "Too many attempts. Wait a few minutes and try again.",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_password_mode() -> None:
    if not is_password_login():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "password_login_disabled",
                "message": "Password sign-in is not enabled on this deployment.",
            },
        )


def normalize_email(raw) -> str:
    return str(raw or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def _may_have_account(email: str) -> bool:
    """The admission policy (allowed domain / email whitelist; open in local mode)."""
    from chat.auth import check_user_allowed
    return check_user_allowed(email)


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return body


def _bad_request(error: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": error, "message": message})


def set_password_url(request: Request, raw_token: str) -> str:
    """Absolute link to the set-password page for *raw_token*.

    The token rides in the URL fragment, which browsers never send to the
    server, so it stays out of access logs and Referer headers.
    """
    return f"{oauth_base_url(request)}/set-password#token={raw_token}"


def _default_name(email: str) -> str:
    local = email.split("@", 1)[0]
    words = [w for w in re.split(r"[._+-]+", local) if w]
    return " ".join(w.capitalize() for w in words) or local


async def _ensure_account(email: str, name: str = "") -> dict:
    """The user row for *email*, created (without a password) if missing."""
    user = await get_user_by_email(email)
    if user:
        return user
    user = await create_user(
        email=email,
        name=name.strip()[:255] or _default_name(email),
        api_key=generate_api_key(),
        google_oauth=None,
        settings={},
    )
    logger.info("[PasswordLogin] Account created: %s", email)
    return user


async def issue_password_link(
    request: Request, email: str, purpose: str
) -> tuple[str, str]:
    """Mint a set-password link for *email*; returns ``(url, raw_token)``."""
    ttl = INVITE_LINK_TTL if purpose == "invite" else RESET_LINK_TTL
    raw = await create_password_token(email, purpose, ttl)
    return set_password_url(request, raw), raw


def invite_email_body(url: str, account_exists: bool) -> tuple[str, str]:
    """Subject and body of an admin-issued link email."""
    if account_exists:
        return (
            "Set a new Quest password",
            "An administrator created a link for you to set a new password "
            f"for your Quest account:\n\n{url}\n\n"
            "The link works once and expires in 7 days.\n",
        )
    return (
        "You're invited to Quest",
        "An administrator invited you to Quest. Choose a password to "
        f"finish creating your account:\n\n{url}\n\n"
        "The link works once and expires in 7 days.\n",
    )


def _self_service_email_body(url: str, account_exists: bool) -> tuple[str, str]:
    if account_exists:
        return (
            "Reset your Quest password",
            "Someone (hopefully you) asked to reset the password for your "
            f"Quest account. To choose a new password, open:\n\n{url}\n\n"
            "The link works once and expires in 1 hour. If you did not ask "
            "for this, ignore this email -- your password is unchanged.\n",
        )
    return (
        "Finish creating your Quest account",
        "Someone (hopefully you) asked to create a Quest account for this "
        f"address. To choose a password and sign in, open:\n\n{url}\n\n"
        "The link works once and expires in 1 hour. If you did not ask for "
        "this, ignore this email.\n",
    )


async def _send_quietly(to: str, subject: str, body: str) -> None:
    try:
        await send_email(to, subject, body)
    except MailerError:
        logger.exception("[PasswordLogin] Could not email a password link to %s", to)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/login")
async def password_login(request: Request):
    """Sign in with email + password; sets the session cookie."""
    _require_password_mode()
    body = await _read_json(request)
    email = normalize_email(body.get("email"))
    password = body.get("password")
    if not email or not isinstance(password, str) or not password:
        raise _bad_request("missing_fields", "Enter your email and password.")

    ip = _client_ip(request)
    if _failed_by_email.blocked(email) or _failed_by_ip.blocked(ip):
        raise _too_many_attempts()

    stored = await get_password_hash(email)
    if stored:
        ok = await asyncio.to_thread(verify_password, password, stored)
    else:
        await asyncio.to_thread(burn_verify_time, password)
        ok = False

    if not ok:
        _failed_by_email.hit(email)
        _failed_by_ip.hit(ip)
        logger.info("[PasswordLogin] Failed sign-in for %s from %s", email, ip)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_credentials", "message": "Incorrect email or password."},
        )

    if not _may_have_account(email):
        raise HTTPException(
            status_code=403,
            detail={"error": "access_denied", "message": "This account is not allowed to sign in."},
        )

    _failed_by_email.reset(email)
    user = await get_user_by_email(email)
    logger.info("[PasswordLogin] User signed in: %s", email)
    response = JSONResponse({"success": True, "email": email})
    return set_session_cookie(response, user["id"], password_fingerprint(stored))


@router.post("/request-link")
async def request_password_link(request: Request, background: BackgroundTasks):
    """Email a set-password link (forgot password / self-service sign-up).

    Requires outgoing email. The response never says whether the address
    has an account or may sign up.
    """
    _require_password_mode()
    if not smtp_configured():
        raise _bad_request(
            "email_not_configured",
            "This deployment cannot send email. Ask an administrator for a sign-in link.",
        )
    body = await _read_json(request)
    email = normalize_email(body.get("email"))
    if not is_valid_email(email):
        raise _bad_request("invalid_email", "Enter a valid email address.")

    ip = _client_ip(request)
    if _links_by_ip.blocked(ip):
        raise _too_many_attempts()
    _links_by_ip.hit(ip)

    generic = {
        "success": True,
        "message": "If this address can use this deployment, a link is on its way. "
                   "Check your inbox.",
    }
    if _links_by_email.blocked(email) or not _may_have_account(email):
        return generic
    _links_by_email.hit(email)

    account_exists = await get_user_by_email(email) is not None
    url, _ = await issue_password_link(request, email, "reset")
    subject, text = _self_service_email_body(url, account_exists)
    # Sent after the response so its timing does not reveal whether an
    # email went out.
    background.add_task(_send_quietly, email, subject, text)
    logger.info("[PasswordLogin] %s link requested for %s",
                "Reset" if account_exists else "Sign-up", email)
    return generic


@router.post("/link-info")
async def password_link_info(request: Request):
    """Describe a set-password link for the set-password page.

    POST (not GET) so the token never appears in a URL the server logs.
    """
    _require_password_mode()
    body = await _read_json(request)
    info = await get_valid_password_token(str(body.get("token") or ""))
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "invalid_link", "message": "This link is invalid or has expired."},
        )
    user = await get_user_by_email(info["email"])
    return {
        "email": info["email"],
        "purpose": info["purpose"],
        "account_exists": user is not None,
        "name": user.get("name", "") if user else "",
    }


@router.post("/set")
async def set_password_with_link(request: Request):
    """Use a set-password link: set the password (creating the account if
    needed) and sign in."""
    _require_password_mode()
    body = await _read_json(request)
    token = str(body.get("token") or "")
    password = body.get("password")
    problem = password_problem(password)
    if problem:
        raise _bad_request("weak_password", problem)

    info = await get_valid_password_token(token)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "invalid_link", "message": "This link is invalid or has expired."},
        )
    email = info["email"]
    if not _may_have_account(email):
        raise HTTPException(
            status_code=403,
            detail={"error": "access_denied", "message": "This account is not allowed to sign in."},
        )
    if await consume_password_token(token) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "invalid_link", "message": "This link is invalid or has expired."},
        )

    user = await _ensure_account(email, str(body.get("name") or ""))
    password_hash = await asyncio.to_thread(hash_password, password)
    await set_password_hash(email, password_hash)
    _failed_by_email.reset(email)
    logger.info("[PasswordLogin] Password set via %s link: %s", info["purpose"], email)
    response = JSONResponse({"success": True, "email": email})
    return set_session_cookie(response, user["id"], password_fingerprint(password_hash))


@router.post("/change", dependencies=[Depends(forbid_impersonation)])
async def change_password(request: Request):
    """Change the signed-in user's password.

    The current password is required when the account has one. Other
    sessions are signed out (their cookies carry the old fingerprint); this
    one gets a fresh cookie. Refused (403) to an admin impersonation session.
    """
    _require_password_mode()
    signed_cookie = request.cookies.get(COOKIE_NAME)
    user = await get_user_from_cookie(signed_cookie) if signed_cookie else None
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"error": "not_authenticated", "message": "Authentication required."},
        )
    from chat.auth import require_user_allowed
    require_user_allowed(user)

    body = await _read_json(request)
    new_password = body.get("new_password")
    problem = password_problem(new_password)
    if problem:
        raise _bad_request("weak_password", problem)

    email = user["email"]
    stored = await get_password_hash(email)
    if stored:
        if _failed_by_email.blocked(email):
            raise _too_many_attempts()
        current = body.get("current_password")
        ok = isinstance(current, str) and await asyncio.to_thread(verify_password, current, stored)
        if not ok:
            _failed_by_email.hit(email)
            raise _bad_request("invalid_credentials", "Current password is incorrect.")

    password_hash = await asyncio.to_thread(hash_password, new_password)
    await set_password_hash(email, password_hash)
    logger.info("[PasswordLogin] Password changed: %s", email)
    response = JSONResponse({"success": True})
    return set_session_cookie(response, user["id"], password_fingerprint(password_hash))


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------

async def apply_pending_admin_passwords(data_dir) -> list[str]:
    """Apply passwords the prod bootstrap wizard left for the server.

    The wizard runs before the database exists, so it writes
    ``{email: password_hash}`` to ``<data_dir>/pending_admin_passwords.json``;
    this creates missing accounts, stores the hashes and deletes the file.
    Returns the emails applied.
    """
    pending = read_pending_admin_passwords(data_dir)
    path = pending_admin_passwords_path(data_dir)
    if not pending:
        if path.exists():
            path.unlink()
        return []
    applied = []
    for email, password_hash in pending.items():
        await _ensure_account(email)
        await set_password_hash(email, password_hash)
        applied.append(email)
        logger.info("[PasswordLogin] Applied bootstrap password for %s", email)
    path.unlink()
    return applied

