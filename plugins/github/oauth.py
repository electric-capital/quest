"""GitHub OAuth flow endpoints (plugin-provided router).

The oauth-kind user connection's router, mounted by quest.py under the
plugin's ``/auth/github`` namespace after plugin load. Same URLs as the
pre-plugin core flow (``/auth/github``, ``/auth/github/callback``,
``/auth/github/disconnect``) so existing GitHub OAuth app registrations
keep working; tokens now land in the ``user_service_credentials`` table
(``oauth_blob``) instead of a dedicated ``users`` column. Expiring GitHub
App tokens are stored with their refresh token and refreshed by
``plugins/github/upstream.py``.
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

from plugins.github.upstream import (
    GITHUB_SCOPES,
    GITHUB_TOKEN_URL,
    build_token_blob,
    load_github_client_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

_STATE_COOKIE = "github_oauth_state"


def _error_page(title: str, message: str, retry_link: bool = False) -> HTMLResponse:
    retry = (
        '<p><a href="/auth/github">Retry GitHub authentication</a></p>'
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


@router.get("/github")
async def auth_github(request: Request, popup: str = None):
    """Initiate GitHub OAuth flow."""
    # Check if user is authenticated with Google
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
    redirect_uri = f"{base_url}/auth/github/callback"

    try:
        github_config = load_github_client_config()
        client_id = github_config.get("client_id")

        # Signed, session-bound state cookie (CSRF nonce + popup flag).
        issued = mint_oauth_state(
            _STATE_COOKIE, user_id=user["id"], popup=popup == "1",
        )

        auth_url = "https://github.com/login/oauth/authorize?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(GITHUB_SCOPES),
            "state": issued.state,
        })
        return issued.attach(RedirectResponse(auth_url))

    except HTTPException as e:
        return _error_page("Configuration Error", str(e.detail))
    except Exception as e:
        return _error_page("Configuration Error", str(e))


@router.get("/github/callback")
async def auth_github_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """GitHub OAuth callback handler."""
    if error:
        return _error_page(
            "GitHub Authentication Error",
            f"GitHub authentication failed: {error}",
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
        github_config = load_github_client_config()
        client_id = github_config.get("client_id")
        client_secret = github_config.get("client_secret")

        # Exchange code for token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"}
            )

            if response.status_code != 200:
                raise Exception("Failed to exchange code for token")

            token_data = response.json()

            if token_data.get("error"):
                raise Exception(f"GitHub OAuth error: {token_data.get('error_description', token_data.get('error'))}")

            access_token = token_data.get("access_token")
            if not access_token:
                raise Exception("No access token in response")

            # Optionally fetch username for logging
            user_response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "Quest/1.0",
                }
            )
            github_username = None
            if user_response.status_code == 200:
                github_username = user_response.json().get("login")

        # Keeps the refresh token + expiry when GitHub issued an expiring
        # (GitHub App) token, so the credential loader can refresh it.
        blob = build_token_blob(token_data)
        await upsert_credential(user["id"], "github", oauth_blob=blob)

        logger.info(
            "[GitHub OAuth] User authenticated: %s (github_user=%s, expiring=%s)",
            user["email"], github_username or "unknown",
            "refresh_token" in blob,
        )

        # Invalidate cached chat sessions so the next message picks up the new system prompt
        from chat.gemini_api import invalidate_user_sessions
        invalidate_user_sessions(user["id"])

        # Redirect back to app (or close popup)
        if is_popup:
            response = HTMLResponse(generate_oauth_popup_success_page("GitHub"))
        else:
            response = RedirectResponse("/", status_code=303)
        response.delete_cookie(_STATE_COOKIE)
        return response

    except Exception as e:
        if is_popup:
            response = HTMLResponse(generate_oauth_popup_error_page("GitHub", str(e)))
        else:
            response = _error_page(
                "GitHub Authentication Error", str(e), retry_link=True,
            )
        response.delete_cookie(_STATE_COOKIE)
        return response


@router.post("/github/disconnect")
async def disconnect_github(request: Request):
    """Remove GitHub OAuth connection.

    POST /auth/github/disconnect
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await delete_credential(user["id"], "github")

    logger.info("[Auth] GitHub OAuth disconnected (user=%s)", user["email"])

    # Invalidate session so system prompt refreshes without GitHub docs
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}
