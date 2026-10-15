"""Ramp OAuth 2.0 authorization-code flow endpoints.

Ramp is a confidential client (client_id + client_secret, HTTP Basic on the
token endpoint), so unlike Twitter/X no PKCE is needed. Ramp access tokens
expire and refresh tokens ROTATE on every use, so the flow stores
refresh_token + expires_at and ``refresh_ramp_token`` must persist the
rotated refresh token or the user's connection is permanently broken.
"""

import logging
from html import escape
import urllib.parse
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.user_store import update_user_field
from auth.config import COOKIE_NAME, RAMP_SCOPES, load_ramp_client_config, oauth_base_url
from auth.oauth_state import clear_oauth_state, mint_oauth_state, verify_oauth_state
from auth.session import forbid_impersonation, get_user_from_cookie
from auth.popup_helpers import generate_oauth_popup_success_page, generate_oauth_popup_error_page

logger = logging.getLogger(__name__)

_STATE_COOKIE = "ramp_oauth_state"

# Storing, replacing or removing a connection's credentials is the account
# owner's call: an admin impersonation session gets 403 on every route here.
router = APIRouter(prefix="/auth", dependencies=[Depends(forbid_impersonation)])

RAMP_AUTHORIZE_URL = "https://app.ramp.com/v1/authorize"
RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"

# Shown whenever Ramp turns a user away from the consent screen. Ramp only
# lets Admins and Business Owners authorize a developer app; everyone else
# gets "Business not authorized to use this application".
ADMIN_ONLY_NOTE = (
    "Only Admins and Business Owners of the Ramp business can connect Ramp. "
    "If you are a regular Ramp employee, ask a Ramp admin to connect instead."
)


@router.get("/ramp")
async def auth_ramp(request: Request, popup: str = None):
    """Initiate Ramp OAuth 2.0 authorization-code flow."""
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        return HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Authentication Required</h1>
            <p>Please authenticate with Google first.</p>
            <p><a href="/">Sign in with Google</a></p>
        </body>
        </html>
        """)

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        return RedirectResponse("/auth/")

    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/ramp/callback"

    try:
        ramp_config = load_ramp_client_config()
        client_id = ramp_config.get("client_id")

        # Signed, session-bound state cookie (CSRF nonce + popup flag).
        issued = mint_oauth_state(
            _STATE_COOKIE, user_id=user["id"], popup=popup == "1",
        )
        state = issued.state

        auth_url = RAMP_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(RAMP_SCOPES),
            "state": state,
        })

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


@router.get("/ramp/callback")
async def auth_ramp_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    """Handle the Ramp OAuth 2.0 callback."""
    if error:
        # Ramp refuses the consent screen to anyone who is not an Admin or
        # Business Owner of the Ramp business ("Business not authorized to
        # use this application"), which is the usual reason to land here.
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Quest - Error</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h1>Ramp Authentication Error</h1>
            <p style="color: red;">Ramp authentication failed: {escape(error)}</p>
            <p>{ADMIN_ONLY_NOTE}</p>
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
            <p><a href="/auth/ramp">Retry Ramp authentication</a></p>
        </body>
        </html>
        """)
        return clear_oauth_state(resp, _STATE_COOKIE)
    is_popup = bool(state_payload.get("popup"))

    if not code:
        return clear_oauth_state(RedirectResponse("/auth/"), _STATE_COOKIE)

    base_url = oauth_base_url(request)
    redirect_uri = f"{base_url}/auth/ramp/callback"

    try:
        ramp_config = load_ramp_client_config()
        client_id = ramp_config.get("client_id")
        client_secret = ramp_config.get("client_secret")

        # Exchange the authorization code for tokens
        async with httpx.AsyncClient() as client:
            response = await client.post(
                RAMP_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                raise Exception(f"Failed to exchange code for token: {response.text}")

            token_data = response.json()

            if token_data.get("error"):
                raise Exception(
                    f"Ramp OAuth error: {token_data.get('error_description', token_data.get('error'))}"
                )

            access_token = token_data.get("access_token")
            if not access_token:
                raise Exception("No access token in response")

            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()

        # Store per-user Ramp OAuth data
        await update_user_field(user["email"], ramp_oauth={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": token_data.get("scope", ""),
            "token_type": token_data.get("token_type", "bearer"),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        })

        logger.info("[Ramp OAuth] User authenticated: %s", user["email"])

        # Invalidate cached chat sessions so the next message picks up Ramp docs
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user["id"])

        if is_popup:
            response = HTMLResponse(generate_oauth_popup_success_page("Ramp"))
        else:
            response = RedirectResponse("/", status_code=303)
        response.delete_cookie(_STATE_COOKIE)
        return response

    except Exception as e:
        if is_popup:
            response = HTMLResponse(generate_oauth_popup_error_page("Ramp", str(e)))
            response.delete_cookie(_STATE_COOKIE)
            return response
        else:
            resp = HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head><title>Quest - Error</title></head>
            <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
                <h1>Ramp Authentication Error</h1>
                <p style="color: red;">{escape(str(e))}</p>
                <p><a href="/auth/ramp">Retry Ramp authentication</a></p>
            </body>
            </html>
            """)
            return clear_oauth_state(resp, _STATE_COOKIE)


@router.post("/ramp/disconnect")
async def disconnect_ramp(request: Request):
    """Remove Ramp OAuth connection.

    POST /auth/ramp/disconnect
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = user["email"]

    result = await update_user_field(email, ramp_oauth=None)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "message": "User not found"},
        )

    logger.info("[Auth] Ramp OAuth disconnected (user=%s)", email)

    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}


async def refresh_ramp_token(user: dict) -> str:
    """Refresh an expired Ramp access token using the stored refresh token.

    Ramp rotates refresh tokens on every use, so the new refresh_token from
    the response must be stored or the user's connection will be permanently
    broken.

    Args:
        user: User dict (mutated in-place to reflect new token data).

    Returns:
        The new access_token string.

    Raises:
        HTTPException: If the refresh fails (revoked token, network error, etc.).
    """
    ramp_oauth = user.get("ramp_oauth") or {}
    refresh_token = ramp_oauth.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "ramp_token_expired",
                "message": (
                    "Ramp connection expired. Please reconnect in Settings > Data Connections."
                ),
            },
        )

    try:
        ramp_config = load_ramp_client_config()
        client_id = ramp_config.get("client_id")
        client_secret = ramp_config.get("client_secret")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                RAMP_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                raise Exception(f"Token refresh failed: {response.text}")

            token_data = response.json()

            if token_data.get("error"):
                raise Exception(
                    f"Ramp token refresh error: {token_data.get('error_description', token_data.get('error'))}"
                )

        new_access_token = token_data["access_token"]
        new_refresh_token = token_data.get("refresh_token", refresh_token)
        expires_in = token_data.get("expires_in", 3600)
        new_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()

        updated_oauth = {
            **ramp_oauth,
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "expires_at": new_expires_at,
        }

        # Persist to DB
        await update_user_field(user["email"], ramp_oauth=updated_oauth)
        # Mutate in-place so the current request uses the fresh token
        user["ramp_oauth"] = updated_oauth

        logger.info("[Ramp OAuth] Token refreshed for user %s", user.get("email"))
        return new_access_token

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Ramp OAuth] Token refresh failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "ramp_token_expired",
                "message": (
                    "Ramp connection expired. Please reconnect in Settings > Data Connections."
                ),
            },
        ) from exc
