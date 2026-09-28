"""Shared validation + schedule-application helpers for the routine action
requests (``create_routine`` / ``edit_routine``).

Both handlers accept the same ``schedule`` full-replacement spec, the same
skill-id lists, and the same registry-checked ``model`` value; the sync
validators live here so the two ``validate_params`` implementations stay in
lockstep. :func:`apply_schedule_spec` is the execute-time counterpart shared
by both handlers (create-or-replace the routine's single schedule row,
including the daily local->UTC conversion).
"""

from zoneinfo import ZoneInfo

SCHEDULE_KEYS = frozenset(
    {
        "schedule_type",
        "daily_time_local",
        "timezone",
        "hourly_minute",
        "interval_minutes",
        "weekly_days",
        "is_enabled",
    }
)

SCHEDULE_TYPES = ("daily", "weekly", "hourly", "every_n_minutes")

# Preference order for the model a create_routine proposal gets when the
# agent omits ``model``: Sonnet 5 first, Gemini 3.7 Flash as the fallback.
DEFAULT_ROUTINE_MODELS = ("claude-sonnet-5", "gemini-3.7-flash")


def pick_default_routine_model() -> str | None:
    """Pick the model for a ``create_routine`` proposal that omitted ``model``.

    Returns the first entry of :data:`DEFAULT_ROUTINE_MODELS` that is
    currently available (credentials configured and no failing health
    verdict), or ``None`` when neither is -- e.g. an OpenRouter-only
    personal deployment -- so the routine is created without a pinned model
    and keeps the run-time default behavior.
    """
    from chat.llm.config import get_available_models

    available = set(get_available_models())
    for model_id in DEFAULT_ROUTINE_MODELS:
        if model_id in available:
            return model_id
    return None


def validate_skill_id_list(value, key: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of skill id strings.")
    normalized: list[str] = []
    for skill_id in value:
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise ValueError(f"{key} must be a list of non-empty skill id strings.")
        normalized.append(skill_id.strip())
    if not normalized:
        raise ValueError(f"{key} must not be an empty list.")
    return normalized


def validate_model_value(model) -> str:
    """Validate a routine ``model`` param against the registry.

    Deprecated ids are rejected (they stay runnable on existing routines but
    cannot be newly assigned). Returns the stripped model id.
    """
    from chat.llm.config import list_model_specs, resolve_model

    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string.")
    model = model.strip()
    spec = resolve_model(model)
    valid = [s.id for s in list_model_specs() if not s.deprecated]
    if spec is None:
        raise ValueError(f"Unknown model '{model}'. Valid models: {valid}")
    if spec.deprecated:
        raise ValueError(
            f"Model '{model}' is deprecated and cannot be newly assigned to "
            f"a routine. Valid models: {valid}"
        )
    return spec.id


def validate_schedule_spec(schedule) -> dict:
    """Validate the full-replacement ``schedule`` param and return it normalized."""
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be a JSON object.")
    unknown = set(schedule) - SCHEDULE_KEYS
    if unknown:
        raise ValueError(
            f"Unknown schedule key(s): {sorted(unknown)}. "
            f"Allowed: {sorted(SCHEDULE_KEYS)}."
        )

    schedule_type = schedule.get("schedule_type")
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError(
            f"schedule.schedule_type must be one of {list(SCHEDULE_TYPES)}."
        )

    out: dict = {"schedule_type": schedule_type}

    if schedule_type in ("daily", "weekly"):
        time_local = schedule.get("daily_time_local")
        tz_name = schedule.get("timezone")
        if not isinstance(time_local, str) or not isinstance(tz_name, str):
            raise ValueError(
                f"A {schedule_type} schedule requires daily_time_local (\"HH:MM\") and "
                "timezone (IANA name, e.g. \"America/New_York\")."
            )
        _validate_time_string(time_local)
        try:
            ZoneInfo(tz_name)
        except Exception:
            raise ValueError(f"Invalid timezone: {tz_name}")
        out["daily_time_local"] = time_local
        out["timezone"] = tz_name
        if schedule_type == "weekly":
            from db.schedule_timing import validate_weekly_days
            out["weekly_days"] = validate_weekly_days(schedule.get("weekly_days"))
    elif schedule_type == "hourly":
        minute = schedule.get("hourly_minute")
        if not isinstance(minute, int) or isinstance(minute, bool) or not (0 <= minute <= 59):
            raise ValueError("An hourly schedule requires hourly_minute (integer 0-59).")
        out["hourly_minute"] = minute
    else:  # every_n_minutes
        interval = schedule.get("interval_minutes")
        if not isinstance(interval, int) or isinstance(interval, bool) or not (1 <= interval <= 1440):
            raise ValueError(
                "An every_n_minutes schedule requires interval_minutes "
                "(integer 1-1440)."
            )
        out["interval_minutes"] = interval

    is_enabled = schedule.get("is_enabled", True)
    if not isinstance(is_enabled, bool):
        raise ValueError("schedule.is_enabled must be a boolean.")
    out["is_enabled"] = is_enabled
    return out


def _validate_time_string(time_str: str) -> None:
    if not time_str or len(time_str) != 5 or time_str[2] != ":":
        raise ValueError(f"Invalid time format: {time_str}. Must be HH:MM")
    try:
        h, m = int(time_str[:2]), int(time_str[3:])
    except ValueError:
        raise ValueError(f"Invalid time format: {time_str}. Must be HH:MM")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time format: {time_str}. Must be HH:MM")


def describe_schedule(schedule: dict) -> str:
    """One-line human summary for the approval card's Schedule row."""
    schedule_type = schedule.get("schedule_type")
    if schedule_type == "daily":
        desc = f"Daily at {schedule.get('daily_time_local')} ({schedule.get('timezone')})"
    elif schedule_type == "weekly":
        from db.schedule_timing import describe_weekly_days
        desc = (
            f"Weekly on {describe_weekly_days(schedule.get('weekly_days'))} at "
            f"{schedule.get('daily_time_local')} ({schedule.get('timezone')})"
        )
    elif schedule_type == "hourly":
        desc = f"Hourly at minute {schedule.get('hourly_minute')}"
    else:
        desc = f"Every {schedule.get('interval_minutes')} minute(s)"
    if not schedule.get("is_enabled", True):
        desc += " (disabled)"
    return desc


async def apply_schedule_spec(user_id: int, routine_id: str, spec: dict) -> None:
    """Create-or-replace the routine's schedule row from a validated spec.

    Full replacement: type-irrelevant fields are cleared so a daily->hourly
    switch doesn't leave stale daily config behind. Daily schedules convert
    the local time to UTC via the same helper the REST layer uses.
    """
    from db import schedule_store

    daily_time_utc = None
    if spec["schedule_type"] in ("daily", "weekly"):
        from chat.schedule_routes import _local_time_to_utc
        daily_time_utc = _local_time_to_utc(
            spec["daily_time_local"], spec["timezone"]
        )
    existing = await schedule_store.get_schedule_for_routine(routine_id)
    if existing:
        await schedule_store.update_schedule(
            schedule_id=existing["id"],
            user_id=user_id,
            schedule_type=spec["schedule_type"],
            daily_time_utc=daily_time_utc,
            daily_time_local=spec.get("daily_time_local"),
            timezone_str=spec.get("timezone"),
            hourly_minute=spec.get("hourly_minute"),
            interval_minutes=spec.get("interval_minutes"),
            weekly_days=spec.get("weekly_days"),
            is_enabled=spec["is_enabled"],
        )
    else:
        created = await schedule_store.create_schedule(
            user_id=user_id,
            routine_id=routine_id,
            schedule_type=spec["schedule_type"],
            daily_time_utc=daily_time_utc,
            daily_time_local=spec.get("daily_time_local"),
            timezone_str=spec.get("timezone"),
            hourly_minute=spec.get("hourly_minute"),
            interval_minutes=spec.get("interval_minutes"),
            weekly_days=spec.get("weekly_days"),
        )
        if not spec["is_enabled"]:
            await schedule_store.update_schedule(
                schedule_id=created["id"],
                user_id=user_id,
                is_enabled=False,
            )
