"""SQLAlchemy engine and session configuration.

One async engine (``sqlite+aiosqlite``) serves every store module and its
FastAPI endpoint callers. Alembic builds its own sync engine in
``alembic/env.py``.
"""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config.paths import DATABASE_PATH

ASYNC_DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# For a file-based aiosqlite URL, create_async_engine uses
# AsyncAdaptedQueuePool (verified at runtime on SQLAlchemy 2.0.46), so
# pool_size / max_overflow / pool_timeout ARE honored. The pool helps
# concurrent READS proceed under WAL (see the PRAGMA listener below); WRITES
# still serialize at the SQLite single-writer lock regardless of pool size.
# busy_timeout (set per-connection in the PRAGMA listener) is what actually
# prevents "database is locked" errors under write contention -- the pool only
# governs how many concurrent read sessions can be open before callers queue.
# Sized modestly above the default (5) to accommodate the FastAPI async
# endpoints + background scheduler + Slack worker sharing this engine.
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)


@event.listens_for(async_engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Set SQLite per-connection PRAGMAs for FK enforcement and concurrency.

    SQLite does not enforce foreign keys by default; PRAGMA foreign_keys
    must be set to ON for every new connection.  This ensures all
    ON DELETE CASCADE / SET NULL constraints defined in the schema are
    respected at the database engine level.

    WAL journal mode lets readers proceed concurrently with a single
    writer (the default rollback journal blocks readers during a write).
    busy_timeout makes a connection wait up to 5s for the writer lock
    instead of immediately raising "database is locked" under contention.
    synchronous=NORMAL is the standard safe companion to WAL -- it trades a
    vanishingly small durability window on power-loss for materially fewer
    fsyncs. (journal_mode persists in the DB file; busy_timeout and
    synchronous are per-connection and must be re-asserted on every connect.)
    Matches the WAL + busy_timeout precedent already used for project DBs.

    The async engine delegates connect events to its underlying
    sync_engine, so every pooled connection gets these PRAGMAs.
    """
    dbapi_connection.execute("PRAGMA foreign_keys=ON")
    dbapi_connection.execute("PRAGMA journal_mode=WAL")
    dbapi_connection.execute("PRAGMA busy_timeout=5000")
    dbapi_connection.execute("PRAGMA synchronous=NORMAL")


AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)
