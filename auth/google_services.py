"""Google Services OAuth flow endpoints."""

import logging
from html import escape
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.user_store import update_user_field
from auth.config import COOKIE_NAME, GOOGLE_SERVICE_SCOPES, oauth_base_url
from auth.oauth_state import clear_oauth_state, mint_oauth_state, verify_oauth_state
from auth.session import forbid_impersonation, get_user_from_cookie
from auth.google_credentials import get_google_services_oauth_flow
from auth.popup_helpers import generate_oauth_popup_success_page, generate_oauth_popup_error_page

logger = logging.getLogger(__name__)

_STATE_COOKIE = "google_services_oauth_state"

# Storing, replacing or removing a connection's credentials is the account
# owner's call: an admin impersonation session gets 403 on every route here.
router = APIRouter(prefix="/auth", dependencies=[Depends(forbid_impersonation)])


@router.get("/google-services")
async def auth_google_services(request: Request, popup: str = None):
    """Initiate Google Services OAuth flow (separate from app login)."""
    # Require existing session (user must be logged in first)
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        return RedirectResponse("/auth/")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        return RedirectResponse("/auth/")

    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/google-services/callback"

    try:
        flow = get_google_services_oauth_flow(redirect_uri)

        # Signed, session-bound state cookie (CSRF nonce + popup flag).
        issued = mint_oauth_state(
            _STATE_COOKIE, user_id=user["id"], popup=popup == "1",
        )

        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
            login_hint=user["email"],  # Pre-fill the email to avoid account picker confusion
            state=issued.state,
        )

        return issued.attach(RedirectResponse(auth_url))
    except Exception as e:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Configuration Error</h1>
            <p style="color: red;">{escape(str(e))}</p>
        </body>
        </html>
        """)


@router.get("/google-services/callback")
async def auth_google_services_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    """Callback for Google Services OAuth flow."""
    if error:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Google Services Authentication Error</h1>
            <p style="color: red;">Google services authentication failed: {escape(error)}</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """)

    # The state cookie is bound to the session that started the flow:
    # resolve the session first, then verify the signed nonce against it.
    signed_cookie = request.cookies.get(COOKIE_NAME)
    user = await get_user_from_cookie(signed_cookie) if signed_cookie else None
    if not user:
        return clear_oauth_state(RedirectResponse("/auth/"), _STATE_COOKIE)

    state_payload = verify_oauth_state(
        request, _STATE_COOKIE, state, user_id=user["id"],
    )
    if state_payload is None:
        resp = HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Security Error</h1>
            <p style="color: red;">Invalid state parameter. Please try again.</p>
            <p><a href="/auth/google-services">Retry Google Services authorization</a></p>
        </body>
        </html>
        """)
        return clear_oauth_state(resp, _STATE_COOKIE)
    is_popup = bool(state_payload.get("popup"))

    if not code:
        return clear_oauth_state(RedirectResponse("/auth/"), _STATE_COOKIE)

    session_email = user["email"]

    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/google-services/callback"

    try:
        flow = get_google_services_oauth_flow(redirect_uri)
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Verify the token belongs to the same user who is logged in.
        # Fail CLOSED: if the identity lookup does not succeed we cannot
        # prove which Google account the token belongs to, so refuse to
        # persist it rather than silently binding an unverified account.
        async with httpx.AsyncClient() as client:
            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"}
            )
            if userinfo_resp.status_code != 200:
                raise Exception(
                    "Failed to verify the Google account that authorized "
                    f"services access (userinfo HTTP {userinfo_resp.status_code})"
                )
            services_email = userinfo_resp.json().get("email", "")
            if not services_email or services_email.casefold() != session_email.casefold():
                return clear_oauth_state(HTMLResponse("""
                <!DOCTYPE html>
                <html>
                <head><title>Quest - Error</title></head>
                <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
                    <h1>Account Mismatch</h1>
                    <p style="color: red;">Google Services must be authorized with the same account you logged in with.</p>
                    <p><a href="/">Back to home</a></p>
                </body>
                </html>
                """), _STATE_COOKIE)

        # Store Google services tokens separately.
        #
        # Persist the scopes Google ACTUALLY GRANTED (credentials.granted_scopes,
        # parsed from the token response's `scope` field), NOT the scopes we
        # requested (GOOGLE_SERVICE_SCOPES). These can differ when the user
        # de-selects a scope on the consent screen, or when a previously-granted
        # token is being upgraded. Storing the requested set verbatim would mask
        # a partial grant: the /connectors needs_reauth check compares the stored
        # scopes against GOOGLE_SERVICE_SCOPES, so recording requested-not-granted
        # could leave a connector showing "Connected" while API calls 403 with
        # ACCESS_TOKEN_SCOPE_INSUFFICIENT (exactly the GCP cloud-platform
        # symptom). Fall back to the requested set only if Google returned no
        # explicit scope field.
        granted_scopes = list(credentials.granted_scopes) if getattr(
            credentials, "granted_scopes", None
        ) else list(GOOGLE_SERVICE_SCOPES)

        await update_user_field(session_email, google_services_oauth={
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            "scopes": granted_scopes,
            "authorized_at": datetime.utcnow().isoformat()
        })

        logger.info("[OAuth] Google services authorized for: %s", session_email)

        # Invalidate cached chat sessions so the next message picks up the new system prompt
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user["id"])

        if is_popup:
            resp = HTMLResponse(generate_oauth_popup_success_page("Google Services"))
        else:
            resp = RedirectResponse("/", status_code=303)
        return clear_oauth_state(resp, _STATE_COOKIE)

    except Exception as e:
        if is_popup:
            resp = HTMLResponse(generate_oauth_popup_error_page("Google Services", str(e)))
        else:
            resp = HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head><title>Quest - Error</title></head>
            <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
                <h1>Google Services Authentication Error</h1>
                <p style="color: red;">An error occurred: {escape(str(e))}</p>
                <p><a href="/">Try again</a></p>
            </body>
            </html>
            """)
        return clear_oauth_state(resp, _STATE_COOKIE)
