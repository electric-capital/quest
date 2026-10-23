"""Opening the DuckDB analytics file and keeping its schema current.

One process may hold a DuckDB file read-write, so the server owns
``analytics.duckdb`` for its whole lifetime: ``open_store()`` at lifespan
start (which applies pending migrations before anything writes),
``close_store()`` at shutdown. Nothing else opens the file while the
server runs -- not a shell, not a script -- or the open fails with a lock
error on one side.

Migrations are numbered SQL files in ``analytics/migrations/``
(``NNNN_name.sql``), applied in order by :func:`apply_migrations`, each in
its own transaction (DuckDB DDL is transactional, so a failing file leaves
nothing half-applied) and recorded in the ``_migrations`` table. They are
forward-only; there is no downgrade. The rules for writing one -- add a
nullable column for an additive change, rebuild the table for anything
else, never a DuckDB enum -- are in docs/architecture/analytics.md.

Connections are not thread-safe. Every thread that touches the store takes
its own cursor from :meth:`AnalyticsStore.cursor` (a duplicate connection
on the same database) and closes it when done; the ledger's writer thread
keeps one for its lifetime, report queries take one per call inside
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def utcnow() -> datetime:
    """Naive UTC now -- the form every ``TIMESTAMP`` column stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` for every migration file, in order.

    Raises ``ValueError`` on a duplicate version or a file that does not
    match ``NNNN_name.sql`` -- both are authoring mistakes that must fail
    loudly (in the tests) rather than silently skip a migration.
    """
    found: dict[int, tuple[str, Path]] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if not match:
            raise ValueError(f"analytics migration has an invalid name: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise ValueError(
                f"analytics migrations {found[version][1].name} and {path.name} "
                f"share version {version}"
            )
        found[version] = (match.group(2), path)
    return [(v, name, path) for v, (name, path) in sorted(found.items())]


def apply_migrations(
    conn: duckdb.DuckDBPyConnection, directory: Path = MIGRATIONS_DIR,
) -> list[int]:
    """Apply every migration not yet recorded in ``_migrations``.

    Returns the versions applied by this call (empty when up to date).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        "version INTEGER NOT NULL, name VARCHAR NOT NULL, applied_at TIMESTAMP NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM _migrations").fetchall()}
    done: list[int] = []
    for version, name, path in list_migrations(directory):
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) VALUES (?, ?, ?)",
                [version, name, utcnow()],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            logger.exception("analytics migration %s failed", path.name)
            raise
        logger.info("Applied analytics migration %s", path.name)
        done.append(version)
    return done


def table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    """``(column_name, data_type)`` of ``table`` in definition order."""
    return conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()


class AnalyticsStore:
    """One open DuckDB database; hand out per-thread cursors."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._lock = threading.Lock()

    def open(self) -> "AnalyticsStore":
        """Open (creating the file if needed) and apply pending migrations."""
        with self._lock:
            if self._conn is not None:
                return self
            conn = duckdb.connect(self.path)
            try:
                apply_migrations(conn)
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A duplicate connection for the calling thread; close it when done."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("analytics store is not open")
        return conn.cursor()

    def close(self) -> None:
        """Close the database (a checkpoint folds the WAL into the file)."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


_current: Optional[AnalyticsStore] = None


def get_store() -> Optional[AnalyticsStore]:
    """The process-wide store, or None before ``open_store`` / after ``close_store``."""
    return _current


def set_store(store: Optional[AnalyticsStore]) -> None:
    """Install (or clear) the process-wide store; tests use it with an
    in-memory store."""
    global _current
    _current = store


def open_store(path: str | Path | None = None) -> AnalyticsStore:
    """Open the configured analytics file and make it the process-wide store."""
    if path is None:
        from config.paths import ANALYTICS_DB_PATH
        path = ANALYTICS_DB_PATH
    store = AnalyticsStore(path).open()
    set_store(store)
    return store


def close_store() -> None:
    store = get_store()
    set_store(None)
    if store is not None:
        store.close()
