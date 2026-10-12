"""Generic per-user API key endpoints for plugin services.

Plugins declaring a ``UserConnectionSpec`` of kind ``api_key`` get these
routes automatically (no dedicated per-service endpoint pair needed):

- ``POST /auth/service-key/{service}``: save the caller's key for the
  plugin service (body ``{"api_key": "..."}``), validated by the plugin's
  optional ``validate_key`` hook.
- ``POST /auth/service-key/{service}/remove``: disconnect (delete the row).

Keys live in the ``user_service_credentials`` table (see
``db/user_service_credential_store.py``). Both routes invalidate the
user's cached chat sessions so the next message rebuilds the system prompt
with the changed connection state.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from auth.config import COOKIE_NAME
from auth.session import get_user_from_cookie

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


def _resolve_api_key_plugin(service: str):
    """Return the loaded plugin backing an api_key connection, or 404."""
    from config.plugins import get_loaded_plugin

    plugin = get_loaded_plugin(service)
    spec = plugin.user_connection if plugin else None
    # A multi-connection service manages its keys per connection through
    # /auth/connections/{service}: saving here would overwrite the default
    # row's secret without the account binding, and removing would delete
    # every connection at once (no service-wide Disconnect, spec 11.6).
    if spec is None or spec.kind != "api_key" or spec.multi_connection:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_service",
                "message": f"No API-key service named {service!r}.",
            },
        )
    return plugin


async def _require_user(request: Request) -> dict:
    """Resolve the session-cookie user from the session cookie."""
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.post("/service-key/{service}")
async def save_service_key(service: str, request: Request):
    """Save the current user's API key for a plugin service."""
    plugin = _resolve_api_key_plugin(service)
    user = await _require_user(request)

    try:
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    validate_key = plugin.user_connection.validate_key
    if validate_key is not None:
        try:
            error = validate_key(api_key)
        except Exception:
            logger.exception(
                "Plugin %r validate_key raised; rejecting key", plugin.id,
            )
            error = "Key validation failed."
        if error:
            raise HTTPException(status_code=400, detail=error)

    from db.user_service_credential_store import upsert_credential
    await upsert_credential(user["id"], plugin.id, secret=api_key)
    logger.info("[%s] API key saved (user=%s)", plugin.id, user["email"])

    # Invalidate cached chat sessions so the next message picks up the new
    # connection state in the system prompt.
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True, "message": "API key saved successfully"}


@router.post("/service-key/{service}/remove")
async def remove_service_key(service: str, request: Request):
    """Remove the current user's API key for a plugin service."""
    plugin = _resolve_api_key_plugin(service)
    user = await _require_user(request)

    from db.user_service_credential_store import delete_credential
    removed = await delete_credential(user["id"], plugin.id)
    if removed:
        logger.info("[%s] API key removed (user=%s)", plugin.id, user["email"])

    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])

    return {"success": True}
