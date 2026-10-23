"""The analytics store: append-only event ledgers in a DuckDB file.

``quest.db`` (SQLite, SQLAlchemy, Alembic) holds the application's
transactional state. Event ledgers -- one row per time something happened,
read only by grouped admin reports -- live apart from it in
``analytics.duckdb`` so that their writes never contend for the SQLite
writer lock, ``quest.db`` and its backups stay small, and aggregations stay
fast as the ledgers grow. The modules:

- ``store``: opening the file, the numbered SQL migrations and the
  per-thread cursors (DuckDB is a blocking C++ library; nothing here is
  awaited directly from the event loop).
- ``ledger``: the write path -- synchronous, non-blocking ``record_*``
  calls that enqueue rows, and the single writer thread that inserts
  them in batches.
- ``reports``: the read queries behind the admin System Reports sections,
  run in a worker thread.
- ``backfill``: one-shot imports of history that predates a ledger, run
  as lifespan background tasks behind the ``_backfills`` marker table.

The design, the schema-change rules and the operational constraints are
in docs/architecture/analytics.md.
"""
