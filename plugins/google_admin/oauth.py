"""Google Workspace Admin OAuth flow endpoints (plugin-provided router).

The oauth-kind user connection's router, mounted by quest.py under the
plugin's ``/auth/google-admin`` namespace after plugin load. A standard
Google authorization-code flow against the deployment's core Google OAuth
client, requesting ONLY the read-only admin scopes (plus the email
identity scope) with offline access so a refresh token lands in the
stored blob; token JSON lives in the ``user_service_credentials`` table
(``oauth_blob``), same as the GitHub and Microsoft 365 plugins.

Unlike the core Google Services connector, the authorizing Google account
does NOT have to be the Quest login: Workspace administrators commonly
hold a separate admin account. The account that was connected is recorded
in the blob.
"""

import logging
from html import escape
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth.config import COOKIE_NAME, oauth_base_url
from auth.oauth_state import clear_oauth_state, mint_oauth_state, verify_oauth_state
from auth.session import get_user_from_cookie
from auth.popup_helpers import (
    generate_oauth_popup_error_page,
    generate_oauth_popup_success_page,
)
from db.user_service_credential_store import delete_credential, upsert_credential

from plugins.google_admin.upstream import (
    GOOGLE_ADMIN_SCOPE_REQUEST,
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    SERVICE_ID,
    build_token_blob,
    load_google_admin_client_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# The plugin's URL namespace: the id with its underscore as a hyphen (see
# config.plugins.plugin_auth_prefix), matching /auth/google-services.
_AUTH_PATH = "/auth/google-admin"

_STATE_COOKIE = "google_admin_oauth_state"

_LABEL = "Google Workspace Admin"

_HTTP_TIMEOUT = 30.0


def _error_page(title: str, message: str, retry_link: bool = False) -> HTMLResponse:
    retry = (
        f'<p><a href="{_AUTH_PATH}">Retry {_LABEL} authentication</a></p>'
        if retry_link else '<p><a href="/">Back to home</a></p>'
    )
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><title>Quest - Error</title></head>
    <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
        <h1>{escape(title)}</h1>
        <p style="color: red;">{escape(message)}</p>
        {retry}
    </body>
    </html>
    """)


@router.get("/google-admin")
async def auth_google_admin(request: Request, popup: str = None):
    """Initiate the Google Workspace Admin OAuth flow."""
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        return _error_page("Authentication Required", "Please sign in first.")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        return RedirectResponse("/auth/")

    redirect_uri = f"{oauth_base_url(request)}{_AUTH_PATH}/callback"

    try:
        config = load_google_admin_client_config()

        # Signed, session-bound state cookie (CSRF nonce + popup flag).
        issued = mint_oauth_state(
            _STATE_COOKIE, user_id=user["id"], popup=popup == "1",
        )

        auth_url = f"{GOOGLE_AUTH_URL}?" + urlencode({
            "client_id": config["client_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": GOOGLE_ADMIN_SCOPE_REQUEST,
            "state": issued.state,
            # Offline access + forced consent so Google returns a refresh
            # token on every connect; the account picker lets the user
            # choose a dedicated admin account. include_granted_scopes is
            # left off so this token never accumulates the (write-capable)
            # scopes of the same user's Google Services connection.
            "access_type": "offline",
            "prompt": "consent select_account",
        })

        return issued.attach(RedirectResponse(auth_url))

    except HTTPException as e:
        return _error_page("Configuration Error", str(e.detail))
    except Exception as e:
        return _error_page("Configuration Error", str(e))


@router.get("/google-admin/callback")
async def auth_google_admin_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    """Google Workspace Admin OAuth callback handler."""
    if error:
        return _error_page(
            f"{_LABEL} Authentication Error",
            f"Google authentication failed: {error}",
        )

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
        resp = _error_page(
            "Security Error",
            "Invalid state parameter. Please try again.",
            retry_link=True,
        )
        return clear_oauth_state(resp, _STATE_COOKIE)
    is_popup = bool(state_payload.get("popup"))

    if not code:
        return clear_oauth_state(RedirectResponse("/auth/"), _STATE_COOKIE)

    try:
        config = load_google_admin_client_config()
        redirect_uri = f"{oauth_base_url(request)}{_AUTH_PATH}/callback"

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            token_response = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            })

            if token_response.status_code != 200:
                try:
                    body = token_response.json()
                    detail = body.get("error_description") or body.get("error", "")
                except Exception:
                    detail = token_response.text[:300]
                raise Exception(f"Failed to exchange code for token: {detail}")

            token_data = token_response.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise Exception("No access token in response")

            # Record which Google account was connected. It may be a
            # dedicated admin account rather than the Quest login, so a
            # mismatch is not an error; a failed lookup just leaves the
            # account unknown.
            account = None
            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_response.status_code == 200:
                userinfo = userinfo_response.json()
                account = {
                    "email": userinfo.get("email"),
                    "domain": userinfo.get("hd"),
                }

        # build_token_blob keeps the scopes Google actually GRANTED (the
        # token response's ``scope``), which is what needs_reauth compares.
        blob = build_token_blob(token_data, account=account)
        await upsert_credential(user["id"], SERVICE_ID, oauth_blob=blob)

        logger.info(
            "[Google Admin OAuth] User authenticated: %s (admin_account=%s)",
            user["email"], (account or {}).get("email", "unknown"),
        )

        # Invalidate cached chat sessions so the next message picks up the
        # new system prompt (Google Workspace Admin skill now advertised).
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user["id"])

        if is_popup:
            response = HTMLResponse(generate_oauth_popup_success_page(_LABEL))
        else:
            response = RedirectResponse("/", status_code=303)
        return clear_oauth_state(response, _STATE_COOKIE)

    except Exception as e:
        if is_popup:
            response = HTMLResponse(generate_oauth_popup_error_page(_LABEL, str(e)))
        else:
            response = _error_page(
                f"{_LABEL} Authentication Error", str(e), retry_link=True,
            )
        return clear_oauth_state(response, _STATE_COOKIE)


@router.post("/google-admin/disconnect")
async def disconnect_google_admin(request: Request):
    """Remove the Google Workspace Admin OAuth connection.

    POST /auth/google-admin/disconnect
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await delete_credential(user["id"], SERVICE_ID)

    logger.info("[Auth] Google Workspace Admin disconnected (user=%s)", user["email"])

    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}
