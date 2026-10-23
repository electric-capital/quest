-- Foundation: the backfill marker table.
--
-- One row per completed one-shot history import (analytics/backfill.py).
-- Backfills are not migrations: they read the filesystem and take minutes,
-- so they run in the background after startup and record themselves here
-- when done. (The migration runner's own ledger is the _migrations table,
-- created by analytics/store.py before any migration runs.)
CREATE TABLE _backfills (
    name         VARCHAR   NOT NULL,
    completed_at TIMESTAMP NOT NULL,   -- UTC
    row_count    BIGINT    NOT NULL
);
