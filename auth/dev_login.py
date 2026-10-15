"""Local-only login endpoints (no OAuth).

Provides POST /auth/dev-login, which creates and authenticates users by
simply providing an email address, and GET /auth/dev-accounts, which
returns the roster of existing accounts so the sign-in screen can offer
one-click canned-account login. Both are only functional in local mode
(QUEST_ENV=local; the legacy alias "dev" also works) and return 404
otherwise.
"""

import hashlib
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from config import environment
from db.user_store import get_user_by_email, create_user, list_all_users
from auth.config import generate_api_key
from auth.session import set_session_cookie

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# Canned name lists for deterministic name generation
FIRST_NAMES = [
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi",
    "Ivan", "Judy", "Karl", "Laura", "Mallory", "Niaj", "Oscar", "Peggy",
    "Quinn", "Rupert", "Sybil", "Trent",
]

LAST_NAMES = [
    "Anderson", "Brown", "Chen", "Davis", "Evans", "Fischer", "Garcia",
    "Huang", "Ibrahim", "Jones", "Kim", "Lopez", "Miller", "Nakamura",
    "Olsen", "Park", "Quinn", "Rivera", "Smith", "Tanaka",
]

# Basic email pattern for validation
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _generate_dev_google_sub(email: str) -> str:
    """Generate a deterministic fake Google sub for a local dev account.

    Real Google subs are numeric strings; the ``dev-`` prefix makes the
    fakes recognizable and guarantees they never collide with a real sub.
    Seeding them keeps local flows exercising the ``users.google_sub``
    column without real Google logins.
    """
    digest = hashlib.md5(("sub:" + email).encode()).hexdigest()
    return f"dev-{digest}"


def _generate_name(email: str) -> str:
    """Generate a deterministic name from an email address.

    Uses MD5 hashing (not for security, just for determinism) to pick
    a first name and last name from the canned lists. The same email
    always produces the same name.
    """
    first_hash = hashlib.md5(email.encode()).hexdigest()
    last_hash = hashlib.md5((email + "last").encode()).hexdigest()
    first_idx = int(first_hash, 16) % len(FIRST_NAMES)
    last_idx = int(last_hash, 16) % len(LAST_NAMES)
    return f"{FIRST_NAMES[first_idx]} {LAST_NAMES[last_idx]}"


@router.get("/dev-accounts")
async def dev_accounts():
    """Return the roster of accounts for the local sign-in account picker.

    Only available in local mode. Returns 404 otherwise to avoid
    revealing the endpoint exists in production.
    """
    if not environment.allow_dev_login():
        raise HTTPException(status_code=404, detail="Not found")

    from chat.auth import is_admin

    users = await list_all_users()
    accounts = [
        {
            "email": u["email"],
            "name": u.get("name", ""),
            "is_admin": is_admin(u["email"]),
        }
        for u in users
    ]
    # Admins first, then alphabetically by email, so the seeded canned
    # admin is always the top suggestion.
    accounts.sort(key=lambda a: (not a["is_admin"], a["email"]))
    return {"accounts": accounts}


@router.post("/dev-login")
async def dev_login(request: Request):
    """Local-only email login: create or authenticate a user by email.

    Only available in local mode. Returns 404 otherwise to avoid
    revealing the endpoint exists in production.
    """
    if not environment.allow_dev_login():
        raise HTTPException(status_code=404, detail="Not found")

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    email = (body.get("email") or "").strip().lower()

    # Validate email
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email format")

    # Look up existing user or create new one
    existing_user = await get_user_by_email(email)
    created = False

    if existing_user:
        # Block dev login for users who have Google OAuth credentials.
        # They should use the real Google sign-in flow instead.
        if existing_user.get("google_oauth"):
            raise HTTPException(
                status_code=403,
                detail="User has Google OAuth credentials — use Google sign-in instead",
            )
        user_record = existing_user
        name = existing_user["name"]
        logger.info("[DevLogin] Existing user logged in: %s", email)
    else:
        name = _generate_name(email)
        user_record = await create_user(
            email=email,
            name=name,
            api_key=generate_api_key(),
            google_oauth=None,
            settings={},
            google_sub=_generate_dev_google_sub(email),
        )
        created = True
        logger.info("[DevLogin] New user created: %s (name=%s)", email, name)

    # Same session cookie as the Google OAuth login
    response = JSONResponse(
        content={"success": True, "email": email, "name": name, "created": created}
    )
    return set_session_cookie(response, user_record)
