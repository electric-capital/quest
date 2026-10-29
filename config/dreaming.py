"""Dreaming settings: the admin's model choice for the memory reviews.

Dreams (chat/dreaming.py) run with no user present and cost the deployment,
so which model they use is an admin decision, not the user's composer
pick. The settings live in ``DATA_DIR / "dreaming.json"``::

    {"version": 1, "model": "claude-opus-5-5" | null, "thinking_effort": "high" | null}

``model`` null means automatic: :func:`chat.dreaming.resolve_dream_model`
picks the first of a preference list that is available and allowed in
private conversations. ``thinking_effort`` null runs the model at its
default level. A missing or malformed file reads as both null.

Standard library only, like ``config/feature_gates.py``, so the daemon's
poll and the admin routes can read it cheaply.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from config.paths import DREAMING_SETTINGS_FILE

logger = logging.getLogger(__name__)

MAX_MODEL_ID_LENGTH = 100
MAX_THINKING_EFFORT_LENGTH = 20

DEFAULT_SETTINGS: dict = {"model": None, "thinking_effort": None}


def normalize_settings(value) -> dict:
    """Normalize a persisted or submitted object into the canonical shape.

    Non-string or empty ``model`` / ``thinking_effort`` become None; strings
    are trimmed (the effort lower-cased) and capped. Never raises; the
    admin endpoint validates against the model registry on top of this.
    """
    if not isinstance(value, dict):
        return dict(DEFAULT_SETTINGS)
    model = value.get("model")
    model = model.strip()[:MAX_MODEL_ID_LENGTH] if isinstance(model, str) else ""
    effort = value.get("thinking_effort")
    effort = (
        effort.strip().lower()[:MAX_THINKING_EFFORT_LENGTH]
        if isinstance(effort, str) else ""
    )
    return {"model": model or None, "thinking_effort": effort or None}


def read_dreaming_settings() -> dict:
    """The stored settings, normalized; the defaults when the file is
    missing or malformed (logged)."""
    if not DREAMING_SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(DREAMING_SETTINGS_FILE, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.warning(
            "Malformed dreaming settings file %s; using defaults", DREAMING_SETTINGS_FILE,
        )
        return dict(DEFAULT_SETTINGS)
    return normalize_settings(data)


def save_dreaming_settings(settings: dict) -> dict:
    """Persist ``settings`` atomically (temp file + ``os.replace``, like
    the other data-dir stores); returns what was stored."""
    normalized = normalize_settings(settings)
    DREAMING_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=DREAMING_SETTINGS_FILE.parent, prefix=".dreaming.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, **normalized}, f, indent=2)
        os.replace(tmp_path, DREAMING_SETTINGS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return normalized
