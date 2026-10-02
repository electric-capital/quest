"""User info, settings, connectors, and account management endpoints."""

import logging
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from chat.auth import check_user_allowed, get_current_user_cookie_or_apikey_checked, is_admin

from auth.config import COOKIE_NAME
from auth.session import get_user_from_cookie

from config.version import RELEASE_INFO
from chat.routes import router

logger = logging.getLogger(__name__)


class UserSettingsUpdate(BaseModel):
    custom_system_prompt: Optional[str] = None
    slack_default_model: Optional[str] = None
    default_model: Optional[str] = None
    # Last-used composer model for PUBLIC-project conversations, tracked
    # separately from ``default_model`` (private) because the admin's Model
    # Selection allow-lists differ per visibility. Same validation/clearing.
    public_default_model: Optional[str] = None
    gmail_labels: Optional[List[str]] = None
    # Settings > Appearance colour-scheme preference: "light", "dark" or
    # "auto" (follow the OS). Empty string clears the stored value (= auto).
    theme: Optional[str] = None
    # Settings > Appearance colour theme (palette: accent + grounds), one of
    # COLOR_THEME_CHOICES. Empty string clears the stored value (= default).
    color_theme: Optional[str] = None
    # Settings > Slack: Slack bot DM reminders about open ("unanswered")
    # action requests. Absent/None means enabled -- False is the only
    # stored off state (a bool survives the exclude_none filter below).
    slack_pending_notifications_enabled: Optional[bool] = None
    # Reminder cadence in minutes, clamped to
    # [MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES] from chat/slack_notifier.py.
    # 0 clears the stored value (= server default of 60).
    slack_pending_notification_interval_minutes: Optional[int] = None


# Accepted ``theme`` values. "auto" is the default and is stored as None.
THEME_CHOICES = ("light", "dark", "auto")

# Accepted ``color_theme`` values. Mirrors the FE registry in
# frontend/src/utils/colorTheme.ts (COLOR_THEMES) and the palettes in
# frontend/src/themes.css -- keep all three in step. The first entry is the
# default the FE applies when nothing is stored.
COLOR_THEME_CHOICES = ("prototype", "electric-blue", "alloy", "recall")


@router.get("/user")
async def get_current_user_from_cookie(request: Request):
    """DEPRECATED: Get current user info from session cookie.

    This endpoint is deprecated. The frontend now authenticates directly
    via the session cookie on all endpoints (no API key needed).
    Kept for backward compatibility with external tools/scripts.

    Previously used by the frontend to auto-populate the API key when
    the user was already logged in via OAuth.

    Args:
        request: FastAPI Request object with session cookie

    Returns:
        Dictionary with email and api_key

    Raises:
        HTTPException: 401 if not authenticated or cookie invalid
    """
    signed_cookie = request.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "not_authenticated",
                "message": "No session cookie found. Please log in at /auth/"
            }
        )

    user = get_user_from_cookie(signed_cookie)
    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "user_not_found",
                "message": "User not found. Please log in at /auth/"
            }
        )

    if not check_user_allowed(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "access_denied",
                "message": "Access restricted."
            }
        )

    return {
        "email": user["email"],
        "name": user.get("name", ""),
        "api_key": user.get("api_key", "")
    }


# ---------------------------------------------------------------------------
# App config (unauthenticated)
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_app_config():
    """Return public app configuration (no auth required).

    Used by the frontend to detect the run mode (page title shows
    "DevQuest" in local mode and the sign-in screen offers canned-account
    login) and to filter the model picker to models that are actually
    working: backend credentials configured and no failing verdict in the
    model-health store (see chat/llm/health.py).
    """
    from config import environment
    from chat.llm.config import get_available_models, public_model_catalog
    from auth.config import allowed_login_domain, login_method, login_restriction_description
    from auth.mailer import smtp_configured

    return {
        "quest_env": environment.get_quest_env(),
        "available_models": get_available_models(),
        # Metadata for every known model (incl. deprecated / admin-disabled
        # ones so old conversations still label correctly); the frontend
        # model catalog is built from this instead of a hand-mirrored list.
        "models": public_model_catalog(),
        "allowed_login_domain": allowed_login_domain(),
        # Human-readable sign-in restriction for the sign-in screen note.
        # Never enumerates the allowed_login_emails whitelist (this
        # endpoint is unauthenticated).
        "login_restriction": login_restriction_description(),
        # Active sign-in method ("google" or "password") and, for password
        # sign-in, whether the sign-in screen can offer self-service
        # sign-up / forgot-password (requires outgoing email).
        "login_method": login_method(),
        "password_self_service": smtp_configured(),
    }


@router.get("/version")
async def get_app_version():
    """Return the running instance's release info (no auth required).

    ``git_hash`` is the HEAD commit; ``version``/``tag``/``released``/
    ``commits_since_tag`` come from the nearest ``v<semver>`` release tag
    (see config/version.py -- all None/0 on an untagged checkout). Captured
    once at process startup so the values stay accurate even if the working
    tree changes while the process is still running. The frontend polls
    ``git_hash`` to detect redeploys and Settings > About shows the rest.
    """
    return RELEASE_INFO.to_dict()


# ---------------------------------------------------------------------------
# User info & settings endpoints
# ---------------------------------------------------------------------------

@router.get("/me")
async def get_current_user_info(user: dict = Depends(get_current_user_cookie_or_apikey_checked)):
    """Get current user info via API key auth.

    Args:
        user: Authenticated user dictionary from get_current_user_cookie_or_apikey_checked

    Returns:
        Dictionary with email, name, google_services_connected, and has_any_service_connected flags
    """
    from config.feature_gates import enabled_features
    from api.instructions import get_user_connected_services

    # One roster for every service -- core and plugin alike -- so the
    # aggregate can't drift out of sync with new services (the old
    # hand-written OR silently missed twitter and airtable).
    connected_services = get_user_connected_services(user)

    is_impersonating = "_impersonator_uid" in user
    impersonator_email = user.get("_impersonator_email")
    impersonator_name = user.get("_impersonator_name")

    return {
        "email": user["email"],
        "name": user.get("name", ""),
        "google_services_connected": connected_services["google_services"],
        "has_any_service_connected": any(connected_services.values()),
        "is_admin": is_admin(user["email"]),
        "is_impersonating": is_impersonating,
        "impersonator_email": impersonator_email,
        "impersonator_name": impersonator_name,
        # Per-user "last-used" default conversation model (web/composer). May be
        # None when never set; the FE applies its Opus-4.8 fallback. Stored in
        # the users.settings JSON blob (same pattern as slack_default_model),
        # written only on the first send of a new chat.
        "default_model": user.get("settings", {}).get("default_model"),
        # Same for public-project conversations: the model last sent from a
        # public-project composer. The FE keeps the two apart so drilling
        # into / out of a public project restores that context's own pick.
        "public_default_model": user.get("settings", {}).get("public_default_model"),
        # Settings > Appearance colour-scheme preference ("light" / "dark");
        # None means auto (follow the OS). The FE applies it to <html
        # data-theme> on hydration and caches it in localStorage so the next
        # page load paints the right scheme before /me answers.
        "theme": user.get("settings", {}).get("theme"),
        # Settings > Appearance colour theme id; None means the default
        # ("prototype"). Applied to <html data-color-theme> the same way.
        "color_theme": user.get("settings", {}).get("color_theme"),
        # Server-global admin feature gates that are currently on FOR THIS
        # USER (see config/feature_gates.py; a gate can be restricted to
        # specific users). The FE uses this to hide feature-gated entries
        # from the composer Flags popover and the New Project modal's
        # public checkbox when the gate is closed for this user.
        "enabled_features": enabled_features(user["email"]),
        # Whether the account has an email/password sign-in password (the
        # Settings > Password section then asks for the current one).
        "has_password": bool(user.get("password_fp")),
    }


@router.get("/settings")
async def get_settings(user: dict = Depends(get_current_user_cookie_or_apikey_checked)):
    """Get current user's settings.

    Args:
        user: Authenticated user dictionary from get_current_user_cookie_or_apikey_checked

    Returns:
        Dictionary with user settings
    """
    return {
        "settings": user.get("settings", {})
    }


@router.put("/settings")
async def update_settings(
    settings_update: UserSettingsUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Update current user's settings.

    Args:
        settings_update: Settings fields to update
        user: Authenticated user

    Returns:
        Updated settings dictionary
    """
    user_email = user["email"]
    user_id = user["id"]

    from db.user_store import update_user_settings, get_user_by_email as db_get_user

    if not await db_get_user(user_email):
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "message": "User not found"})

    # Update only provided fields
    update_data = settings_update.model_dump(exclude_none=True)

    # slack_default_model: validate against MODEL_REGISTRY; treat empty
    # string as "clear" so the stored value becomes None (server default).
    if "slack_default_model" in update_data:
        from chat.llm.config import resolve_model
        candidate = update_data["slack_default_model"]
        if candidate == "":
            update_data["slack_default_model"] = None
        elif resolve_model(candidate) is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_model",
                    "message": f"Unknown model id: {candidate}",
                },
            )

    # default_model: per-user "last-used" default for new web conversations.
    # Validate against MODEL_REGISTRY; treat empty string as "clear" so the
    # stored value becomes None (FE falls back to Opus 4.8). Mirrors the
    # slack_default_model handling above.
    # public_default_model is the public-project twin (written on the first
    # send of a new chat inside a public project).
    for key in ("default_model", "public_default_model"):
        if key not in update_data:
            continue
        from chat.llm.config import resolve_model
        candidate = update_data[key]
        if candidate == "":
            update_data[key] = None
        elif resolve_model(candidate) is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_model",
                    "message": f"Unknown model id: {candidate}",
                },
            )

    # theme: Settings > Appearance colour scheme. "auto" and "" both clear
    # the stored value (None = follow the OS), anything else must be one of
    # THEME_CHOICES.
    if "theme" in update_data:
        candidate = update_data["theme"]
        if candidate in ("", "auto"):
            update_data["theme"] = None
        elif candidate not in THEME_CHOICES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_theme",
                    "message": f"Unknown theme: {candidate}",
                },
            )

    # color_theme: Settings > Appearance colour theme. "" clears the stored
    # value (None = default palette); anything else must be one of
    # COLOR_THEME_CHOICES.
    if "color_theme" in update_data:
        candidate = update_data["color_theme"]
        if candidate == "":
            update_data["color_theme"] = None
        elif candidate not in COLOR_THEME_CHOICES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_color_theme",
                    "message": f"Unknown colour theme: {candidate}",
                },
            )

    # slack_pending_notification_interval_minutes: reminder cadence for the
    # Slack pending-request notifier. 0 clears the stored value (None =
    # server default); anything else must fall inside the notifier's bounds.
    if "slack_pending_notification_interval_minutes" in update_data:
        from chat.slack_notifier import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES
        candidate = update_data["slack_pending_notification_interval_minutes"]
        if candidate == 0:
            update_data["slack_pending_notification_interval_minutes"] = None
        elif not MIN_INTERVAL_MINUTES <= candidate <= MAX_INTERVAL_MINUTES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_interval",
                    "message": (
                        "Reminder interval must be between "
                        f"{MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} "
                        "minutes."
                    ),
                },
            )

    # gmail_labels: list of Quest-manageable Gmail label names (each stored
    # name maps to a "[Quest]/<name>" label in Gmail). Normalized and
    # validated; an empty list clears the configuration.
    if "gmail_labels" in update_data:
        from api.gmail.quest_labels import validate_gmail_label_names
        try:
            update_data["gmail_labels"] = validate_gmail_label_names(update_data["gmail_labels"])
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_gmail_labels",
                    "message": str(exc),
                },
            )

    updated_user = await update_user_settings(user_email, update_data)

    # Backward compatibility: when custom_system_prompt is updated via
    # the settings API, also update the default guide's content so they
    # stay in sync. A missing default guide is not recreated -- guides
    # are deprecated and a converted (deleted) default must stay gone.
    # Skipped entirely while the guides feature gate is closed for this
    # user (guides are inert then, and must not be written to either).
    from config.feature_gates import guides_enabled_for
    if settings_update.custom_system_prompt is not None and guides_enabled_for(user_email):
        from db.guide_store import get_default_guide, update_guide
        default_guide = await get_default_guide(user_id)
        if default_guide:
            await update_guide(user_id, default_guide["id"], content=settings_update.custom_system_prompt)

    # Invalidate any cached chat sessions for this user so the new
    # system prompt takes effect on the next message
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user_id)

    return {
        "settings": updated_user["settings"]
    }


@router.get("/connectors")
async def get_connectors(user: dict = Depends(get_current_user_cookie_or_apikey_checked)):
    """List the current user's Data Connections rows.

    Returns a LIST of row objects the frontend renders generically (no
    per-service JSX): ``kind`` is "oauth" (Connect/Reconnect popup via
    ``connect_url``) or "api_key" (key entry POSTing ``{key_field: <key>}``
    to ``key_url``, disconnect via ``disconnect_url``). ``available: false``
    hides a row entirely (e.g. a plugin without its server-side base
    URL, Ramp without admin OAuth client credentials).
    """
    from auth.config import (
        GOOGLE_SERVICE_SCOPES,
        load_google_oauth_config,
    )

    def _server_configured(loader) -> bool:
        """True when the service's server-side integration credentials load.

        Each core connector needs admin-configured app credentials (an
        OAuth client, ...) before a user connection can work;
        rows whose loader raises are reported unavailable so the FE hides
        them from both the connected list and the add picker.
        """
        try:
            loader()
        except Exception:
            return False
        return True

    google_services_oauth = user.get("google_services_oauth")
    google_services_connected = google_services_oauth is not None
    google_services_needs_reauth = False
    if google_services_connected and google_services_oauth:
        authorized_scopes = set(google_services_oauth.get("scopes", []))
        required_scopes = set(GOOGLE_SERVICE_SCOPES)
        if not required_scopes.issubset(authorized_scopes):
            google_services_needs_reauth = True

    airtable_token = user.get("airtable_token")

    # Ramp needs admin-configured OAuth client credentials; when unset the
    # connector is reported unavailable so the FE hides it.
    from config.service_credentials import read_service_credentials
    ramp_available = read_service_credentials("ramp") is not None

    connectors = [
        {
            "service": "google_services",
            "label": "Google Services",
            "description": "Gmail, Calendar, Drive, Docs, Sheets",
            "kind": "oauth",
            "connect_url": "/auth/google-services?popup=1",
            "connected": google_services_connected,
            "needs_reauth": google_services_needs_reauth,
            "available": _server_configured(load_google_oauth_config),
        },
        # The Slack and Telegram rows are plugin-provided (plugins/slack and
        # plugins/telegram declare oauth-kind user_connections) via the
        # generic plugin loop below.
        {
            "service": "airtable",
            "label": "Airtable",
            "kind": "api_key",
            "connected": airtable_token is not None,
            "key_hint": airtable_token[-4:] if airtable_token else None,
            "key_url": "/auth/airtable/save-token",
            "key_field": "token",
            "key_placeholder": "Paste Personal Access Token (pat...)",
            "disconnect_url": "/auth/airtable/remove-token",
        },
        {
            "service": "ramp",
            "label": "Ramp",
            "kind": "oauth",
            "connect_url": "/auth/ramp?popup=1",
            "connected": user.get("ramp_oauth") is not None,
            "available": ramp_available,
        },
    ]

    # Plugin rows: every loaded plugin with a user connection gets a
    # generically rendered row backed by the user_service_credentials
    # table. api_key connections use the generic key routes
    # (auth/service_key.py); oauth connections use the plugin's mounted
    # /auth/<id> router via the connect-URL convention. The row is hidden
    # (available: false) while the plugin's server-level config is
    # missing/disabled, mirroring the Ramp behavior above.
    from config.plugins import (
        get_loaded_plugins,
        plugin_auth_prefix,
        plugin_server_available,
    )

    stored_rows = user.get("service_credentials") or {}
    for plugin in get_loaded_plugins():
        spec = plugin.user_connection
        if spec is None:
            continue
        row = stored_rows.get(plugin.id)
        try:
            connected = bool(row) and bool(spec.connected(row))
        except Exception:
            connected = False
        plugin_row = {
            "service": plugin.id,
            "label": plugin.label,
            "kind": spec.kind,
            "connected": connected,
            "available": plugin_server_available(plugin),
        }
        if spec.kind == "api_key":
            secret = (row or {}).get("secret")
            plugin_row.update({
                "key_hint": secret[-4:] if (spec.key_hint and secret) else None,
                "key_url": f"/auth/service-key/{plugin.id}",
                "key_field": "api_key",
                "key_placeholder": (
                    spec.key_placeholder or f"Paste {plugin.label} API key"
                ),
                "disconnect_url": f"/auth/service-key/{plugin.id}/remove",
            })
        else:  # oauth
            needs_reauth = False
            if row and spec.needs_reauth is not None:
                try:
                    needs_reauth = bool(spec.needs_reauth(row))
                except Exception:
                    needs_reauth = False
            plugin_row.update({
                "connect_url": f"{plugin_auth_prefix(plugin.id)}?popup=1",
                "needs_reauth": needs_reauth,
            })
        connectors.append(plugin_row)

    return {"connectors": connectors}


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookie. Preserves all user data and OAuth tokens."""
    from fastapi.responses import JSONResponse
    response = JSONResponse({"success": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/logout-and-disconnect")
async def logout_and_disconnect(
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Logout and revoke all OAuth connector tokens. Preserves conversations."""
    from db.user_store import remove_user_fields, get_user_by_email as db_get_user
    from fastapi.responses import JSONResponse
    import httpx

    email = user["email"]
    user_id = user["id"]
    user_record = await db_get_user(email)
    if not user_record:
        raise HTTPException(status_code=404, detail={"error": "user_not_found"})

    # Revoke Google Services OAuth token
    google_services_oauth = user_record.get("google_services_oauth")
    if google_services_oauth and google_services_oauth.get("access_token"):
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": google_services_oauth["access_token"]}
                )
        except Exception:
            pass  # Best-effort revocation

    # Remove all connector tokens
    await remove_user_fields(email, ["google_services_oauth"])

    # Remove per-user plugin service credentials (user_service_credentials
    # rows, incl. the Slack and Telegram plugins' oauth rows -- the plugin
    # analogue of the users columns cleared above).
    from db.user_service_credential_store import delete_all_credentials
    await delete_all_credentials(user_id)

    # Invalidate cached chat sessions
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user_id)

    response = JSONResponse({"success": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/delete-account")
async def delete_account(
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked)
):
    """Delete all account data: user record, chat history, OAuth tokens."""
    from db.user_store import delete_user, get_user_by_email as db_get_user
    from fastapi.responses import JSONResponse
    import shutil
    import httpx

    email = user["email"]
    user_id = user["id"]
    user_record = await db_get_user(email)

    if not user_record:
        raise HTTPException(status_code=404, detail={"error": "user_not_found"})

    # Revoke Google Services OAuth token (best-effort)
    google_services_oauth = user_record.get("google_services_oauth")
    if google_services_oauth and google_services_oauth.get("access_token"):
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": google_services_oauth["access_token"]}
                )
        except Exception:
            pass

    # Revoke Google login OAuth token (best-effort)
    google_oauth = user_record.get("google_oauth")
    if google_oauth and google_oauth.get("access_token"):
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": google_oauth["access_token"]}
                )
        except Exception:
            pass

    # Delete all user memories, guides, routines, and action requests before deleting the user record
    from db.memory_store import delete_all_user_memories
    from db.guide_store import delete_all_user_guides
    from db.routine_store import delete_all_user_routines
    from db.action_request_store import delete_all_user_action_requests
    await delete_all_user_memories(user_id)
    await delete_all_user_guides(user_id)
    await delete_all_user_routines(user_id)
    await delete_all_user_action_requests(user_id)

    # Collect project and conversation directories BEFORE deleting the user
    # (ON DELETE CASCADE will remove project and conversation rows when delete_user() runs).
    from db.project_store import list_projects, delete_all_user_projects
    from db.conversation_store import list_conversations_meta
    from chat.storage import ChatStorage
    conversations_meta = await list_conversations_meta(user_id)
    user_projects = await list_projects(user_id)

    # Delete projects (DB rows -- CASCADE deletes linked conversation rows)
    await delete_all_user_projects(user_id)

    # Delete user record from database (cascades to remaining conversations rows)
    await delete_user(email)

    # Delete each conversation's filesystem directory individually
    for conv in conversations_meta:
        conv_dir = ChatStorage.get_conversation_dir(conv["id"])
        if conv_dir.exists():
            shutil.rmtree(conv_dir)

    # Delete each project's filesystem directory (workspace)
    for proj in user_projects:
        proj_dir = ChatStorage.get_project_dir(proj["id"])
        if proj_dir.exists():
            shutil.rmtree(proj_dir)

    # Invalidate cached chat sessions
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user_id)

    response = JSONResponse({"success": True})
    response.delete_cookie(COOKIE_NAME)
    return response
