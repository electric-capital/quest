"""The legacy per-project SQLite database: the gate that retires it.

The project database (``project_db_query``, the table browser card) is
deprecated in favour of Quest Tables. It stays available only to projects
that already hold data in it: a project whose ``project.db`` has at least
one user table keeps the tool, the ``system:project_db`` skill, the prompt
paragraph and the Tables card; every other project -- new ones, ones that
never used the database, ones whose last table was dropped -- has none of
them, so no new project can start using it.

``PROJECT_DB_SERVICE_KEY`` is the connected-services pseudo-key (see
``api/instructions.py`` ``PSEUDO_SERVICE_KEYS``) that carries the gate into
the prompt builders and the system-skill loader, like the Quest Tables and
browser keys do. It is per project, not per user: the user-level maps set
it to False and the conversation-aware callers overwrite it with
``project_has_legacy_db()``.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Connected-services pseudo-key gating project_db_query and system:project_db.
PROJECT_DB_SERVICE_KEY = "project_db"


def project_has_legacy_db(project_id: str | None) -> bool:
    """Whether *project_id* keeps the legacy project database.

    True when the project's ``project.db`` exists and holds at least one
    user table. A missing file, an empty database (created lazily by a
    query that never made a table) or a database whose tables were all
    dropped is False. A file that exists but cannot be read is treated as
    legacy data, so a transient SQLite error never hides a project's
    existing tables.
    """
    if not project_id:
        return False
    from chat.storage import ChatStorage, InvalidStorageIdError
    try:
        db_path = ChatStorage.get_project_db_path(project_id)
    except InvalidStorageIdError:
        return False
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(
            "Could not inspect project database for project %s, assuming legacy data: %s",
            project_id, e,
        )
        return True
    return row is not None


def with_project_db_service(services: dict[str, bool], project_id: str | None) -> dict[str, bool]:
    """Set ``PROJECT_DB_SERVICE_KEY`` on a connected-services map for a
    conversation of *project_id* (in place, returned for chaining)."""
    services[PROJECT_DB_SERVICE_KEY] = project_has_legacy_db(project_id)
    return services


def project_db_retired_message() -> str:
    """The refusal every closed path returns to the model."""
    return (
        "The project database is retired for this project: it is only available "
        "to projects that already hold tables in it, and this project has none. "
        "Keep structured records in Quest Tables (load `system:tables`) instead."
    )
