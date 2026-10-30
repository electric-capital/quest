"""Admin endpoints for dreaming (chat/dreaming.py): the model choice,
the recent run log and a run-now for the requesting admin.

Settings > Dreaming (frontend/src/components/settings/DreamingSection.tsx)
is the only client. The feature gate itself is flipped under Settings >
Features like every other gate; these routes stay usable while it is
closed so an admin can pick the model before opening it.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.routes import router
from chat.routes.admin import _require_admin

logger = logging.getLogger(__name__)

RECENT_RUNS_LIMIT = 30


def _unavailable_reason(spec, configured: set[str], available: set[str]) -> Optional[str]:
    from config.model_selection import is_model_allowed

    if not is_model_allowed(spec.id, public=False):
        return "not_allowed_private"
    if spec.id in available:
        return None
    return "failing" if spec.id in configured else "not_configured"


async def _dreaming_view(user: dict) -> dict:
    from chat.dreaming import DreamingUnavailable, resolve_dream_model
    from chat.llm.config import get_available_models, get_configured_models, list_model_specs
    from config.dreaming import read_dreaming_settings
    from config.feature_gates import FEATURE_DREAMING, dreaming_enabled_for, is_feature_enabled
    from db.dream_run_store import list_recent_runs
    from db.user_store import get_users_by_ids

    settings = read_dreaming_settings()
    configured = set(get_configured_models())
    available = set(get_available_models())
    models = []
    for spec in list_model_specs():
        if spec.deprecated or not spec.enabled:
            continue
        reason = _unavailable_reason(spec, configured, available)
        models.append({
            "id": spec.id,
            "display_name": spec.display_name,
            "provider_label": spec.provider_label,
            "thinking_efforts": list(spec.thinking_efforts),
            "default_thinking_effort": spec.thinking_effort,
            "dreamable": reason is None,
            "unavailable_reason": reason,
        })
    resolved = None
    unavailable_reason = None
    try:
        model, effort, automatic = resolve_dream_model(settings)
        resolved = {"model": model, "thinking_effort": effort, "automatic": automatic}
    except DreamingUnavailable as exc:
        unavailable_reason = str(exc)

    runs = await list_recent_runs(RECENT_RUNS_LIMIT)
    users = await get_users_by_ids({run["user_id"] for run in runs})
    for run in runs:
        run["user_email"] = (users.get(run["user_id"]) or {}).get("email")
    return {
        "settings": settings,
        "resolved": resolved,
        "unavailable_reason": unavailable_reason,
        "enabled": is_feature_enabled(FEATURE_DREAMING),
        "enabled_for_me": dreaming_enabled_for(user["email"]),
        "models": models,
        "recent_runs": runs,
    }


@router.get("/admin/dreaming")
async def admin_get_dreaming(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The dreaming settings, the model the next dream would run on, every
    enabled model with whether a dream may use it, and the recent runs."""
    _require_admin(user)
    return await _dreaming_view(user)


class DreamingSettingsUpdate(BaseModel):
    # None = automatic (the first dreamable model of the preference list).
    model: Optional[str] = None
    # None = the model's default level.
    thinking_effort: Optional[str] = None


@router.put("/admin/dreaming")
async def admin_update_dreaming(
    body: DreamingSettingsUpdate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Replace the dreaming settings (persisted in data/dreaming.json).

    400 ``unknown_model`` for a model id the registry cannot resolve, 400
    ``invalid_params`` for a model not allowed in private conversations
    (a dream reads private memories) or a thinking level the model does
    not list. A pinned model that is merely unavailable right now is
    accepted: the dream falls back to the automatic choice until it is
    back. Returns the GET shape.
    """
    _require_admin(user)
    from chat.llm.config import resolve_model, resolve_thinking_effort
    from config.dreaming import normalize_settings, save_dreaming_settings
    from config.model_selection import is_model_allowed

    settings = normalize_settings(body.model_dump())
    spec = None
    if settings["model"] is not None:
        spec = resolve_model(settings["model"])
        if spec is None or spec.deprecated:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown_model", "message": f"Unknown model: {settings['model']}"},
            )
        if not is_model_allowed(spec.id, public=False):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_params",
                    "message": (
                        f"{spec.display_name} is not allowed in private conversations "
                        "(Settings > Model Selection); a dream reads private memories."
                    ),
                },
            )
        settings["model"] = spec.id
    # With a pinned model the level must be one it lists; with the automatic
    # choice it is kept as a preference and applied when the chosen model
    # lists it (resolve_dream_model drops it otherwise).
    if settings["thinking_effort"] is not None and spec is not None:
        if resolve_thinking_effort(spec, settings["thinking_effort"]) is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_params",
                    "message": (
                        f"{spec.display_name} has no thinking level "
                        f"{settings['thinking_effort']!r}."
                    ),
                },
            )
    save_dreaming_settings(settings)
    logger.info(
        "[dreaming] admin %s set model=%s thinking_effort=%s",
        user["email"], settings["model"], settings["thinking_effort"],
    )
    return await _dreaming_view(user)


@router.post("/admin/dreaming/run")
async def admin_run_dream(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Dream for the requesting admin now, skipping the scheduler's pacing.

    403 ``feature_disabled`` while the gate is closed for them. Returns
    ``{"status": "skipped", "reason"}`` when the admin has too few
    memories, a dream running or dream suggestions still unreviewed, else
    ``{"status": "completed" | "failed", "run": {...}}``.
    """
    _require_admin(user)
    from chat.dreaming import TRIGGER_MANUAL, run_dream, skip_reason
    from config.feature_gates import dreaming_enabled_for

    if not dreaming_enabled_for(user["email"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_disabled",
                "message": "Dreaming is turned off for you (Settings > Features).",
            },
        )
    reason = await skip_reason(user["id"], scheduled=False)
    if reason is not None:
        return {"status": "skipped", "reason": reason}
    run = await run_dream({"id": user["id"], "email": user["email"]}, TRIGGER_MANUAL)
    run["user_email"] = user["email"]
    return {"status": run["status"], "run": run}
