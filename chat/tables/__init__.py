"""Quest Tables: user- and project-owned typed tables (see
docs/architecture/quest-tables.md).

Modules:

- ``constants`` -- size caps, modes, the
  connected-services pseudo-key, and the shared error texts.
- ``schema`` -- the column model, column-spec validation, stored schema
  load/dump, and cell value canonicalization.
- ``schema_ops`` -- schema operations, their side effects and the schema
  diff.
- ``storage`` -- the per-table ``table.db``: DDL and guarded open, row
  writes, the change log, reads and the download copy.
- ``filters`` -- the filter DSL, sort and paging compiled to SQL, and the
  query executor.
- ``revert`` -- restore to a point by forward replay of the change log.
- ``access`` -- the access rule (``resolve_table_access``).
- ``service`` -- the ONE read/write path for table files: gate, access,
  the per-table lock, quest.db bumps, realtime events.
- ``events`` -- the ``table_list_changed`` / ``table_changed`` realtime
  globals.
"""
