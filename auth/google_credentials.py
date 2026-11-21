"""Google credential management and authenticated API requests."""

import logging
from typing import Optional

import httpx
from dateutil import parser as dateutil_parser
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from fastapi import HTTPException

from db.user_store import update_user_field
from auth.config import (
    load_client_config,
    load_google_oauth_config,
    SCOPES,
    LOGIN_SCOPES,
    GOOGLE_SERVICE_SCOPES,
)

logger = logging.getLogger(__name__)


def get_login_oauth_flow(redirect_uri: str) -> Flow:
    """Create OAuth flow for app login (minimal scopes)."""
    client_config = load_google_oauth_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri
    )
    return flow


# Identity scope requested alongside the service scopes so the callback can
# read the authorizing account's email from the UserInfo endpoint and refuse
# a token that belongs to a different Google account than the Quest login.
# Deliberately NOT part of GOOGLE_SERVICE_SCOPES: the /connectors
# needs_reauth check compares stored scopes against that list, and adding
# it there would flag every existing connection for re-consent.
GOOGLE_SERVICES_IDENTITY_SCOPE = "https://www.googleapis.com/auth/userinfo.email"

# Whenever a userinfo.* scope is requested, Google adds "openid" to the
# granted scopes on the token response. google-auth-oauthlib compares the
# requested and granted sets on fetch_token and raises "Scope has changed"
# when they differ, which failed every services connect/re-auth. Requesting
# "openid" explicitly keeps the two sets equal.
GOOGLE_SERVICES_IDENTITY_SCOPES = ["openid", GOOGLE_SERVICES_IDENTITY_SCOPE]


def get_google_services_oauth_flow(redirect_uri: str) -> Flow:
    """Create OAuth flow for Google services access (full scopes + email identity)."""
    client_config = load_google_oauth_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=[*GOOGLE_SERVICE_SCOPES, *GOOGLE_SERVICES_IDENTITY_SCOPES],
        redirect_uri=redirect_uri
    )
    return flow


async def get_valid_credentials(user: dict, force_refresh: bool = False) -> Optional[Credentials]:
    """Get valid Google credentials for a user, refreshing if needed.

    Args:
        user: User dictionary with oauth data
        force_refresh: If True, force a token refresh even if not expired
    """
    oauth_data = user.get("google_oauth")
    if not oauth_data:
        return None

    # Load client credentials from credentials.json
    client_config = load_client_config()

    # Parse expiry from stored data
    expiry = None
    if oauth_data.get("expiry"):
        try:
            expiry = dateutil_parser.isoparse(oauth_data["expiry"])
        except Exception:
            pass

    creds = Credentials(
        token=oauth_data.get("access_token"),
        refresh_token=oauth_data.get("refresh_token"),
        token_uri=oauth_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_config.get("client_id"),
        client_secret=client_config.get("client_secret"),
        scopes=SCOPES,
        expiry=expiry
    )

    # Check if token is expired (or force refresh) and refresh if possible
    if (force_refresh or creds.expired) and creds.refresh_token:
        try:
            reason = "forced" if force_refresh else "expired"
            logger.info("[OAuth] Refreshing %s token for %s", reason, user["email"])
            creds.refresh(GoogleRequest())
            # Update stored tokens
            email = user["email"]
            updated_oauth = {
                **user.get("google_oauth", {}),
                "access_token": creds.token,
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            }
            await update_user_field(email, google_oauth=updated_oauth)

            # Update the in-memory user dict so subsequent calls in the
            # same session see the refreshed token without re-querying the DB.
            user["google_oauth"] = updated_oauth
            logger.info("[OAuth] Token refreshed successfully for %s", user["email"])
        except Exception as e:
            logger.warning("[OAuth] Token refresh failed for %s: %s", user["email"], e)
            return None
    elif creds.expired and not creds.refresh_token:
        logger.warning("[OAuth] Token expired but no refresh token available for %s", user["email"])

    if not creds.valid:
        return None

    return creds


async def get_valid_service_credentials(user: dict, force_refresh: bool = False) -> Optional[Credentials]:
    """Get valid Google service credentials for a user, refreshing if needed.

    Uses google_services_oauth tokens (separate from login tokens).

    Args:
        user: User dictionary with oauth data
        force_refresh: If True, force a token refresh even if not expired

    Returns:
        Valid Credentials object, or None if authentication required
    """
    # Use dedicated service tokens only
    oauth_data = user.get("google_services_oauth")

    if not oauth_data:
        return None

    client_config = load_client_config()

    expiry = None
    if oauth_data.get("expiry"):
        try:
            expiry = dateutil_parser.isoparse(oauth_data["expiry"])
        except Exception:
            pass

    # Determine which scopes to use
    scopes = oauth_data.get("scopes", SCOPES)

    creds = Credentials(
        token=oauth_data.get("access_token"),
        refresh_token=oauth_data.get("refresh_token"),
        token_uri=oauth_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_config.get("client_id"),
        client_secret=client_config.get("client_secret"),
        scopes=scopes,
        expiry=expiry
    )

    if (force_refresh or creds.expired) and creds.refresh_token:
        try:
            reason = "forced" if force_refresh else "expired"
            logger.info("[OAuth] Refreshing %s service token for %s", reason, user["email"])
            creds.refresh(GoogleRequest())

            # Update stored service tokens
            email = user["email"]
            field = "google_services_oauth"
            current_oauth = user.get(field, {})
            updated_oauth = {
                **current_oauth,
                "access_token": creds.token,
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            }
            await update_user_field(email, **{field: updated_oauth})

            # Update the in-memory user dict so subsequent calls in the
            # same session see the refreshed token without re-querying the DB.
            user[field] = updated_oauth
            logger.info("[OAuth] Service token refreshed successfully for %s", user["email"])
        except Exception as e:
            logger.warning("[OAuth] Service token refresh failed for %s: %s", user["email"], e)
            return None
    elif creds.expired and not creds.refresh_token:
        logger.warning("[OAuth] Service token expired but no refresh token for %s", user["email"])

    if not creds.valid:
        return None

    return creds


async def make_authenticated_request(
    client: httpx.AsyncClient,
    user: dict,
    method: str,
    url: str,
    **kwargs
) -> httpx.Response:
    """Make an authenticated request to Google API with automatic token refresh on 401.

    Uses service credentials (not login credentials) for API calls.

    Args:
        client: httpx AsyncClient instance
        user: User dict with oauth data
        method: HTTP method (GET, POST, etc)
        url: Full URL to request
        **kwargs: Additional arguments to pass to the request (params, content, headers, etc)

    Returns:
        httpx.Response object

    Raises:
        HTTPException: If authentication fails after refresh attempt
    """
    credentials = await get_valid_service_credentials(user)

    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_services_auth_required",
                "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
            }
        )

    # Merge authorization header with any provided headers
    headers = kwargs.get("headers", {})
    headers["Authorization"] = f"Bearer {credentials.token}"
    kwargs["headers"] = headers

    # Make initial request
    response = await client.request(method, url, **kwargs)

    # If 401, try refreshing token and retry once
    if response.status_code == 401:
        logger.info("[OAuth] Got 401 response, attempting service token refresh for %s", user["email"])
        # get_valid_service_credentials is a coroutine -- must be awaited, else
        # `credentials` would be a coroutine object and the retry path would be
        # broken (pre-existing bug fixed opportunistically).
        credentials = await get_valid_service_credentials(user, force_refresh=True)

        if not credentials:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "google_services_auth_required",
                    "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
                }
            )

        # Update authorization header with new token
        headers["Authorization"] = f"Bearer {credentials.token}"
        kwargs["headers"] = headers

        # Retry request
        response = await client.request(method, url, **kwargs)

        if response.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "google_services_auth_required",
                    "message": "Google services authorization required. Please visit /auth/ and connect Google Services."
                }
            )

    return response
