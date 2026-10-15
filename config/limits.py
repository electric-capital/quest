"""Server-global admin-configurable limits.

Admins set these from the Settings > Limits section (GET/PUT
``/admin/limits`` in ``chat/routes/admin.py``); the values are persisted as
a JSON object in ``DATA_DIR / "limits.json"``. A missing or malformed file,
or a missing / out-of-range key, means the default for that limit.

Limits:

* ``max_upload_mb`` -- the per-file cap for workspace uploads
  (``POST /files/upload`` and the project file routes) and for the
  composer's generic-file attachments, which go through the same upload.
  Uploads are buffered in memory while they are saved, which is why the
  ceiling is bounded.

This module intentionally uses only the Python standard library
(mirroring ``config/feature_gates.py``) so it is cheap to import from the
file routes.
"""

import json
import logging
import os
import tempfile

from config.paths import LIMITS_FILE

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_MB = 200
MIN_MAX_UPLOAD_MB = 1
MAX_MAX_UPLOAD_MB = 2048

_MB = 1024 * 1024


def _valid_upload_mb(value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_MAX_UPLOAD_MB <= value <= MAX_MAX_UPLOAD_MB
    )


def read_limits() -> dict:
    """The normalized limits: ``{"max_upload_mb": int}``.

    Unreadable files and invalid values fall back to the defaults (logged),
    so a hand-edited file can never disable uploads.
    """
    stored: dict = {}
    try:
        with open(LIMITS_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            stored = raw
        else:
            logger.warning("%s is not a JSON object; using default limits", LIMITS_FILE)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s (%s); using default limits", LIMITS_FILE, exc)

    upload_mb = stored.get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB)
    if not _valid_upload_mb(upload_mb):
        logger.warning(
            "Invalid max_upload_mb %r in %s; using %d",
            upload_mb, LIMITS_FILE, DEFAULT_MAX_UPLOAD_MB,
        )
        upload_mb = DEFAULT_MAX_UPLOAD_MB
    return {"max_upload_mb": upload_mb}


def max_upload_bytes() -> int:
    """The current per-file workspace upload cap in bytes."""
    return read_limits()["max_upload_mb"] * _MB


def _write_limits(limits: dict) -> None:
    """Persist the limits atomically (temp file + ``os.replace``, like
    ``config/feature_gates.py``)."""
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=LIMITS_FILE.parent, prefix=".limits.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(limits, f, indent=2)
        os.replace(tmp_path, LIMITS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_max_upload_mb(value: int) -> dict:
    """Persist a new upload cap and return the full updated limits.

    Raises ``ValueError`` unless ``value`` is an int in
    ``MIN_MAX_UPLOAD_MB..MAX_MAX_UPLOAD_MB``.
    """
    if not _valid_upload_mb(value):
        raise ValueError(
            f"max_upload_mb must be a whole number from {MIN_MAX_UPLOAD_MB} "
            f"to {MAX_MAX_UPLOAD_MB}"
        )
    limits = read_limits()
    limits["max_upload_mb"] = value
    _write_limits(limits)
    return limits
