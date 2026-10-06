"""Shared data-directory path constants.

All modules that need to reference files inside the data directory should
import the constants defined here instead of computing their own paths.

The data directory defaults to ``PROJECT_ROOT / "data"`` but can be
overridden (highest precedence first):

1. the ``QUEST_DATA_DIR`` environment variable -- set by ``run.py`` in
   local mode to point at the per-run throwaway data directory;
2. the ``data_dir`` key in ``server_config.json`` (located at the
   project root).

Relative values are resolved against ``PROJECT_ROOT``; absolute values
are used as-is.

This module intentionally uses **only** the Python standard library
(``pathlib``, ``json``) so it can be imported very early in the
application lifecycle -- before heavyweight packages like SQLAlchemy
or FastAPI are loaded.

This module does NOT create any directories.  Directory creation is
the responsibility of startup code (``quest.py`` lifespan, ``run.py``).
"""

import json
import os
from pathlib import Path

# Project root -- the repository checkout directory.
PROJECT_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Read optional data_dir override from server_config.json
# ---------------------------------------------------------------------------

def _resolve_data_dir() -> Path:
    """Determine the data directory from env var, server_config.json, or default."""
    env_override = os.environ.get("QUEST_DATA_DIR")
    if env_override:
        p = Path(env_override)
        if p.is_absolute():
            return p
        return (PROJECT_ROOT / p).resolve()
    config_file = PROJECT_ROOT / "server_config.json"
    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                config = json.load(f)
            raw = config.get("data_dir")
            if raw:
                p = Path(raw)
                if p.is_absolute():
                    return p
                return (PROJECT_ROOT / p).resolve()
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return PROJECT_ROOT / "data"


DATA_DIR: Path = _resolve_data_dir()

DATABASE_PATH: Path = DATA_DIR / "quest.db"
CHATS_DIR: Path = DATA_DIR / "chats"
PROJECTS_DIR: Path = DATA_DIR / "projects"
DOCS_DIR: Path = DATA_DIR / "docs"
SECRET_KEY_FILE: Path = DATA_DIR / "secret_key"
LOG_DIR: Path = DATA_DIR / "logs"
SERVICE_CREDENTIALS_DIR: Path = DATA_DIR / "service_credentials"
INFERENCE_CREDENTIALS_DIR: Path = DATA_DIR / "inference_credentials"
INFERENCE_PROVIDERS_FILE: Path = DATA_DIR / "inference_providers.json"
OPENROUTER_CATALOG_FILE: Path = DATA_DIR / "openrouter_catalog.json"
MODEL_HEALTH_FILE: Path = DATA_DIR / "model_health.json"
FEATURE_GATES_FILE: Path = DATA_DIR / "feature_gates.json"
MODEL_SELECTION_FILE: Path = DATA_DIR / "model_selection.json"


def migrate_legacy_database_file() -> None:
    """Rename a pre-rename ``praixy.db`` database (and its WAL/SHM sidecars)
    to ``quest.db`` so deployments created before the project was renamed
    keep their data.

    Idempotent: does nothing when ``quest.db`` already exists or no legacy
    file is present. Called from every startup path that touches the
    database (``run.py``, ``quest.py`` lifespan, ``alembic/env.py``).
    """
    legacy = DATA_DIR / "praixy.db"
    if DATABASE_PATH.exists() or not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        src = DATA_DIR / ("praixy.db" + suffix)
        if src.exists():
            src.rename(DATA_DIR / ("quest.db" + suffix))
