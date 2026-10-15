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
- ``revert`` -- restore to a point by forward replay of the change log.
"""
