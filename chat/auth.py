"""Authentication middleware for chat endpoints."""

from fastapi import HTTPException, Request

from config import environment
from db.user_store import get_user_by_api_key
from auth.config import COOKIE_NAME, is_login_allowed, login_restriction_description
from auth.session import get_user_from_cookie


def check_user_allowed(email: str) -> bool:
    """Check if user's email is allowed to access the app.

    In local mode the restriction is disabled to support canned-account
    login with arbitrary email addresses. Otherwise access requires the
    allowed email domain OR membership in the allowed_login_emails
    whitelist (see is_login_allowed in auth/config.py), matching the
    Google login callback.

    Args:
        email: User's email address

    Returns:
        True if user is allowed, False otherwise
    """
    if not environment.enforce_domain():
        return True
    return is_login_allowed(email)


def require_user_allowed(user: dict) -> dict:
    """Raise 403 ``access_denied`` unless *user* passes the admission policy.

    Session cookies stay valid for weeks and ``users.api_key`` never
    expires, so login-time enforcement is not enough: a user removed from
    the allowed domain / ``allowed_login_emails`` whitelist keeps valid
    credentials. Every request-authenticating dependency (here and in
    ``auth/session.py``) funnels through this check so no route can
    resolve an offboarded user.
    """
    if not check_user_allowed(user.get("email", "")):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "access_denied",
                "message": f"Access restricted to {login_restriction_description()}."
            }
        )
    return user


async def get_current_user_cookie_or_apikey(request: Request) -> dict:
    """Authenticate via session cookie OR API key Bearer token.

    Cookie is tried first (for browser requests), then API key
    (for script/LLM requests). Raises 401 if neither is valid and 403
    when the resolved user no longer passes the admission policy.
    """
    # 1. Try session cookie
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if signed_cookie:
        user = await get_user_from_cookie(signed_cookie)
        if user:
            return require_user_allowed(user)

    # 2. Try API key
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        user = await get_user_by_api_key(api_key)
        if user:
            return require_user_allowed(user)

    raise HTTPException(
        status_code=401,
        detail={
            "error": "not_authenticated",
            "message": "Authentication required. Provide a session cookie or Authorization: Bearer <key> header."
        }
    )


async def get_current_user_cookie_or_apikey_checked(request: Request) -> dict:
    """Dual-auth with domain check.

    Kept as the name most routes declare; the admission check now lives in
    every base dependency (``require_user_allowed``), so this is identical
    to ``get_current_user_cookie_or_apikey``.
    """
    return await get_current_user_cookie_or_apikey(request)


def is_admin(email: str) -> bool:
    """Check if the given email is in the admin_emails list from server config.

    Comparison is case-insensitive since email addresses are case-insensitive
    by convention.

    Args:
        email: User's email address

    Returns:
        True if the email is in the admin_emails list, False otherwise
    """
    from config.server_config import load_server_config
    config = load_server_config()
    admin_emails = [e.lower() for e in config.get("admin_emails", [])]
    return email.lower() in admin_emails
