"""Airtable Personal Access Token configuration endpoint.

Airtable supports both OAuth and Personal Access Tokens (PATs) for API auth.
We use PATs because:
- No token refresh needed: PATs don't expire (unlike OAuth access tokens)
- No coordination between conversations: a single static token works everywhere
- Simpler storage: one string column vs. a JSON blob with access/refresh/expiry
- The Google Services OAuth refresh logic (auth/google_credentials.py) already
  demonstrated the complexity of in-memory token coordination across sessions

See docs/api/airtable-api.md "Why Personal Access Tokens instead of OAuth?" for
the full rationale.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db.user_store import update_user_field
from auth.config import COOKIE_NAME
from auth.session import forbid_impersonation, get_user_from_cookie

logger = logging.getLogger(__name__)

# Storing, replacing or removing a connection's credentials is the account
# owner's call: an admin impersonation session gets 403 on every route here.
router = APIRouter(prefix="/auth", dependencies=[Depends(forbid_impersonation)])


class AirtableTokenRequest(BaseModel):
    """Request body for saving Airtable Personal Access Token."""
    token: str


@router.post("/airtable/save-token")
async def save_airtable_token(
    request: Request,
    body: AirtableTokenRequest,
):
    """Save Airtable Personal Access Token to user profile.

    POST /auth/airtable/save-token

    Request body:
    {
        "token": "patXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"
    }
    """
    # Get user from cookie
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = user["email"]
    token = body.token.strip()

    # Basic validation - Airtable Personal Access Tokens start with "pat" prefix
    if not token.startswith("pat"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_token_format",
                "message": "Invalid Airtable Personal Access Token format. Token should start with 'pat'."
            }
        )

    # Update user's Airtable token
    result = await update_user_field(email, airtable_token=token)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "message": "User not found"}
        )

    logger.info("[Auth] Airtable Personal Access Token saved (user=%s)", email)

    # Invalidate session so system prompt refreshes with Airtable docs
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}


@router.post("/airtable/remove-token")
async def remove_airtable_token(request: Request):
    """Remove Airtable Personal Access Token from user profile.

    POST /auth/airtable/remove-token
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = user["email"]

    # Remove token by setting it to None
    result = await update_user_field(email, airtable_token=None)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "message": "User not found"}
        )

    logger.info("[Auth] Airtable Personal Access Token removed (user=%s)", email)

    # Invalidate session so system prompt refreshes without Airtable docs
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}
