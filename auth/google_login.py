"""Google app login OAuth flow endpoints."""

import logging
from html import escape
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from db.user_store import get_user_by_email, create_user, update_user_field
from auth.config import COOKIE_NAME, COOKIE_SECURE, COOKIE_VERSION, generate_api_key, is_login_allowed, is_password_login, login_restriction_description, oauth_base_url
from auth.oauth_state import clear_oauth_state, mint_oauth_state, verify_oauth_state
from auth.session import forbid_impersonation, get_cookie_serializer
from auth.google_credentials import get_login_oauth_flow, get_valid_credentials

logger = logging.getLogger(__name__)

_STATE_COOKIE = "login_oauth_state"

router = APIRouter(prefix="/auth")


@router.get("/logout")
async def auth_logout():
    """Clear session cookie and redirect to root (sign-in screen)."""
    response = RedirectResponse("/")
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/reset-api-key", dependencies=[Depends(forbid_impersonation)])
async def reset_api_key(request: Request):
    """Reset the user's API key and generate a new one."""
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        return RedirectResponse("/auth/", status_code=303)

    from auth.session import get_user_from_cookie
    user = await get_user_from_cookie(signed_cookie)
    if not user:
        return RedirectResponse("/auth/", status_code=303)

    result = await update_user_field(user["email"], api_key=generate_api_key())
    if not result:
        return RedirectResponse("/auth/", status_code=303)

    logger.info("[Auth] API key reset (user=%s)", user["email"])

    return RedirectResponse("/auth/", status_code=303)


def _google_login_disabled() -> HTTPException:
    """Google sign-in is off while the deployment uses password sign-in."""
    return HTTPException(
        status_code=404,
        detail={
            "error": "google_login_disabled",
            "message": "This deployment uses email and password sign-in.",
        },
    )


@router.get("/login-url")
async def get_login_url(request: Request):
    """Return the Google OAuth login URL for the frontend sign-in screen."""
    if is_password_login():
        raise _google_login_disabled()
    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/callback"

    try:
        flow = get_login_oauth_flow(redirect_uri)
        # No session exists yet, so the login state is signed but unbound.
        issued = mint_oauth_state(_STATE_COOKIE)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
            state=issued.state,
        )
        return issued.attach(JSONResponse({"auth_url": auth_url}))
    except HTTPException as e:
        raise HTTPException(status_code=500, detail={"error": "oauth_config_error", "message": str(e.detail)})


@router.get("/", response_class=HTMLResponse)
async def auth_page(request: Request, force: Optional[str] = None):
    """Redirect to / if authenticated, or show sign-in page."""
    if is_password_login():
        # The SPA's sign-in screen hosts the password form.
        return RedirectResponse("/")
    base_url = oauth_base_url(request)

    # Check for existing session (unless force re-auth)
    if not force:
        signed_cookie = request.cookies.get(COOKIE_NAME)
        if signed_cookie:
            from auth.session import get_user_from_cookie
            user = await get_user_from_cookie(signed_cookie)
            if user:
                creds = await get_valid_credentials(user)
                if creds:
                    return RedirectResponse("/")

    redirect_uri = f"{base_url}/auth/callback"

    try:
        flow = get_login_oauth_flow(redirect_uri)
        issued = mint_oauth_state(_STATE_COOKIE)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
            state=issued.state,
        )
    except HTTPException as e:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Configuration Error</h1>
            <p style="color: red;">{escape(str(e.detail))}</p>
        </body>
        </html>
        """)

    return issued.attach(HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Quest - Sign In</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 600px;
                margin: 50px auto;
                padding: 20px;
                text-align: center;
            }}
            h1 {{ color: #333; }}
            .btn {{
                display: inline-block;
                padding: 12px 24px;
                background-color: #4285f4;
                color: white;
                text-decoration: none;
                border-radius: 4px;
                font-size: 16px;
                margin-top: 20px;
            }}
            .btn:hover {{ background-color: #357abd; }}
            .note {{
                margin-top: 30px;
                color: #666;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <h1>Quest</h1>
        <p>API Proxy with OAuth & Access Control</p>
        <a href="{auth_url}" class="btn">Sign in with Google</a>
        <p class="note">Access restricted to {login_restriction_description()}.</p>
    </body>
    </html>
    """))


@router.get("/callback")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    """OAuth callback handler for app login."""
    if is_password_login():
        # Exactly one sign-in method is active: a callback minted before an
        # admin switch (or crafted by hand) must not open a session.
        resp = HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Google sign-in is disabled</h1>
            <p>This deployment uses email and password sign-in.</p>
            <p><a href="/">Go to the sign-in page</a></p>
        </body>
        </html>
        """, status_code=404)
        return clear_oauth_state(resp, _STATE_COOKIE)
    if error:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Authentication Error</h1>
            <p style="color: red;">Google authentication failed: {escape(error)}</p>
            <p><a href="/">Try again</a></p>
        </body>
        </html>
        """)

    # CSRF check: the callback must carry the nonce minted by this browser's
    # sign-in page (login CSRF would otherwise sign the victim into an
    # attacker-chosen account).
    if verify_oauth_state(request, _STATE_COOKIE, state) is None:
        resp = HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Security Error</h1>
            <p style="color: red;">Invalid state parameter. Please try again.</p>
            <p><a href="/auth/">Try again</a></p>
        </body>
        </html>
        """)
        return clear_oauth_state(resp, _STATE_COOKIE)

    if not code:
        return clear_oauth_state(RedirectResponse("/auth/"), _STATE_COOKIE)

    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/callback"

    try:
        flow = get_login_oauth_flow(redirect_uri)
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Get user info to verify email
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"}
            )

            if response.status_code != 200:
                raise Exception("Failed to get user info")

            user_info = response.json()
            email = user_info.get("email", "")
            # The v2 userinfo `id` field is the Google account's stable
            # OAuth `sub` claim; persisted for integrations that resolve
            # users by Google identity.
            google_sub = user_info.get("id") or None

        # Validate email (always enforced; local mode can only swap the
        # expected domain/whitelist via server config, never skip the check)
        if not is_login_allowed(email):
            return HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head><title>Quest - Access Denied</title></head>
            <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
                <h1>Access Denied</h1>
                <p style="color: red;">Access restricted to {login_restriction_description()}.</p>
                <p>You signed in as: {escape(email)}</p>
                <p><a href="/">Try again with a different account</a></p>
            </body>
            </html>
            """)

        # Check if user already exists in the database
        existing_user = await get_user_by_email(email)
        new_google_oauth = {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None
        }

        if existing_user:
            update_fields = {
                "name": user_info.get("name", ""),
                "google_oauth": new_google_oauth,
            }
            if google_sub:
                update_fields["google_sub"] = google_sub

            # NOTE: Migration logic for combined google_oauth -> google_services_oauth was
            # removed. All existing users have been migrated. The migration was incorrectly
            # triggering after "Logout & Disconnect" + re-login, causing Google Services
            # to appear connected when it wasn't.

            await update_user_field(email, **update_fields)
        else:
            await create_user(
                email=email,
                name=user_info.get("name", ""),
                api_key=generate_api_key(),
                google_oauth=new_google_oauth,
                settings={},
                google_sub=google_sub,
            )

        # Log token information for debugging
        logger.info("[OAuth] User authenticated: %s", email)
        logger.info("[OAuth] Has refresh token: %s (user=%s)", credentials.refresh_token is not None, email)
        logger.info("[OAuth] Token expiry: %s (user=%s)", credentials.expiry, email)

        # Set signed session cookie with versioned payload and redirect to app
        user_record = await get_user_by_email(email)
        signed_payload = get_cookie_serializer().dumps({"v": COOKIE_VERSION, "uid": user_record["id"]})
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME,
            value=signed_payload,
            httponly=True,
            max_age=60 * 60 * 24 * 30,  # 30 days
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        return clear_oauth_state(response, _STATE_COOKIE)

    except Exception as e:
        resp = HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Authentication Error</h1>
            <p style="color: red;">An error occurred: {escape(str(e))}</p>
            <p><a href="/">Try again</a></p>
        </body>
        </html>
        """)
        return clear_oauth_state(resp, _STATE_COOKIE)
