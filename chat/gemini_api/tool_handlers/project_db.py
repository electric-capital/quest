"""Per-project SQLite database query handler.
"""

import json
import sqlite3
from pathlib import Path

import aiosqlite

from chat.project_db import project_db_retired_message, project_has_legacy_db
from chat.storage import ChatStorage


# ---------------------------------------------------------------------------
# Project SQLite database handler
# ---------------------------------------------------------------------------

# Maximum number of rows returned for SELECT queries to prevent enormous
# results from consuming the LLM's context window.
_PROJECT_DB_MAX_ROWS = 1000
# Maximum JSON result size in bytes before truncation.
_PROJECT_DB_MAX_RESULT_BYTES = 100_000

_ATTACH_BLOCKED_ERROR = "ATTACH DATABASE is not allowed. This database is isolated to the project."


def _deny_attach_authorizer(action: int, _arg1, _arg2, _db_name, _trigger) -> int:
    """SQLite authorizer callback that refuses ATTACH at statement-compile time.

    The textual prefix check in ``_execute_project_db_query`` is only a
    fast path: a leading SQL comment (``/* x */ ATTACH ...``) or other
    formatting slips past it, and ATTACH would then open or *create* a
    SQLite file at any path the service process can reach.  The authorizer
    runs inside sqlite3_prepare, after tokenisation, so it cannot be fooled
    by comments, whitespace, or case.
    """
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _is_read_query(query: str) -> bool:
    """Determine if a SQL query is a read (SELECT/PRAGMA/EXPLAIN) query.

    Used to decide whether to fetch results or return the affected row count.
    """
    stripped = query.strip().upper()
    return stripped.startswith(("SELECT", "PRAGMA", "EXPLAIN"))


async def _execute_project_db_query(db_path: Path, query: str) -> str:
    """Execute a SQL query against a project SQLite database.

    Uses aiosqlite for native async I/O without thread delegation.
    Opens a new connection, executes the query, and closes it.

    Args:
        db_path: Path to the project's SQLite database file.
        query: The SQL query to execute.

    Returns:
        JSON string with the query results or affected row count.
    """
    # Block ATTACH DATABASE to prevent access to other SQLite files
    # (fast path only -- the authorizer below is the real guard).
    stripped_upper = query.strip().upper()
    if stripped_upper.startswith("ATTACH"):
        return json.dumps({"error": _ATTACH_BLOCKED_ERROR})

    try:
        async with aiosqlite.connect(str(db_path), timeout=10) as conn:
            await conn.set_authorizer(_deny_attach_authorizer)
            # Enable WAL mode for better concurrent read performance
            await conn.execute("PRAGMA journal_mode=WAL")
            # Set busy timeout for brief lock contention
            await conn.execute("PRAGMA busy_timeout=5000")

            cursor = await conn.execute(query)

            if _is_read_query(query):
                # Read query: fetch rows with column names
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = await cursor.fetchmany(_PROJECT_DB_MAX_ROWS + 1)

                truncated = len(rows) > _PROJECT_DB_MAX_ROWS
                if truncated:
                    rows = rows[:_PROJECT_DB_MAX_ROWS]

                result_data = {
                    "columns": columns,
                    "rows": [list(row) for row in rows],
                    "row_count": len(rows),
                }
                if truncated:
                    result_data["truncated"] = True
                    result_data["note"] = (
                        f"Results truncated to {_PROJECT_DB_MAX_ROWS} rows. "
                        "Use LIMIT/OFFSET or WHERE clauses to narrow results."
                    )

                result_json = json.dumps(result_data, default=str)

                # Truncate if the JSON is too large
                if len(result_json.encode("utf-8")) > _PROJECT_DB_MAX_RESULT_BYTES:
                    # Re-serialize with fewer rows until it fits
                    while rows and len(json.dumps({
                        "columns": columns,
                        "rows": [list(r) for r in rows],
                        "row_count": len(rows),
                        "truncated": True,
                        "note": "Results truncated due to size limit.",
                    }, default=str).encode("utf-8")) > _PROJECT_DB_MAX_RESULT_BYTES:
                        rows = rows[:len(rows) // 2]

                    result_data = {
                        "columns": columns,
                        "rows": [list(r) for r in rows],
                        "row_count": len(rows),
                        "truncated": True,
                        "note": (
                            "Results truncated due to size limit. "
                            "Use LIMIT/OFFSET or WHERE clauses to narrow results."
                        ),
                    }
                    result_json = json.dumps(result_data, default=str)

                return result_json
            else:
                # Write query: commit and return affected row count
                await conn.commit()
                return json.dumps({
                    "status": "ok",
                    "rows_affected": cursor.rowcount,
                })
    except aiosqlite.Error as e:
        if str(e) == "not authorized":
            # Authorizer denial: only ATTACH is denied, so surface the same
            # message as the fast path instead of SQLite's terse wording.
            return json.dumps({"error": _ATTACH_BLOCKED_ERROR})
        return json.dumps({
            "error": f"SQL error: {e}",
        })


async def _handle_project_db_query(project_id: str | None, query: str) -> str:
    """Execute a SQL query against a project's dedicated SQLite database.

    The database file lives at data/projects/{project_id}/project.db. The
    feature is retired: only a project whose database already holds a table
    (``project_has_legacy_db``) may query it; every other project gets the
    retirement error, so no project can start a new database.

    Args:
        project_id: The project UUID. None for standalone conversations.
        query: The SQL query to execute.

    Returns:
        JSON string with the query results or an error message.
    """
    if not project_id:
        return json.dumps({
            "error": (
                "This tool is only available in project conversations. "
                "Standalone conversations do not have a project database."
            )
        })

    if not project_has_legacy_db(project_id):
        return json.dumps({"error": project_db_retired_message()})

    if not query or not query.strip():
        return json.dumps({"error": "Query cannot be empty."})

    db_path = ChatStorage.get_project_db_path(project_id)

    # Execute the query using native async SQLite
    result = await _execute_project_db_query(db_path, query)
    return result

