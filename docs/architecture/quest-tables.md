# Quest Tables Architecture

## Overview

A **Quest Table** is a typed table that lives inside Quest, owned by a user (a **user table**) or by a project (a **project table**, owned by the project owner). Each table is a `data_tables` row in `quest.db` holding its metadata and ordered column schema, plus a directory `<data_dir>/tables/<table_id>/` for its own `table.db` (rows and change log); the rows never live in `quest.db`. This doc covers the metadata layer: the two `quest.db` tables, the store module and its rules, the on-disk path, the constants and the per-user `tables` [feature gate](feature-gates.md). Ownership, scope, modes and shares follow [Quest Docs](quest-docs.md#concepts-and-invariants), with the same terms.

## Key Files

| File | Description |
|------|-------------|
| `chat/tables/constants.py` | The caps and retention numbers shared across modules (`TABLE_*`), the mode, share-permission and column-type sets, the prompt pseudo-key `TABLES_SERVICE_KEY`, `table_not_found_message()` (the one not-found text) and `tables_disabled_message()` |
| `db/table_store.py` | Async metadata CRUD, title uniqueness (`_title_taken()`), the per-owner cap, the keyset candidate lists, the two concurrency tokens, the id collectors for directory sweeps, the share helpers |
| `db/models.py` | `DataTable`, `DataTableShare` |
| `alembic/versions/e5c97b362821_create_data_tables_and_shares.py` | Creates `data_tables` + `data_table_shares` and their indexes |
| `config/paths.py` / `chat/storage.py` | `TABLES_DIR`; `ChatStorage.get_table_dir()`, the sole id-to-path resolver |
| `config/feature_gates.py` / `api/instructions.py` | `FEATURE_TABLES`, `tables_enabled_for()`; the `tables` pseudo-key in `get_user_connected_services()` and `PSEUDO_SERVICE_KEYS` |

## Modes

Tables have the two modes of Quest Docs, `private` and `public`, with the same purpose: a public table may only ever hold content a public conversation could see. The mode is stored on every row and fixed at creation. A user table (`project_id` NULL) is always private, and `create_table()` refuses a public one (`TableValidationError`). A project table takes its project's immutable `projects.public` flag; the caller passes it as `mode`, since the store does not read the project. No store function changes a table's mode.

## Storage

### On-disk layout

A table's files live under `TABLES_DIR/<table_id>/`, a path produced only by `ChatStorage.get_table_dir()`, which applies the canonical-id and containment checks of every resolver and does not create the directory ([Data Paths](data-paths.md#conversation-project-doc-and-table-path-resolution)). The store never touches the directory: `delete_table()` removes the row and its shares, and the caller removes the directory afterwards. `list_table_ids_for_project()` and `list_table_ids_for_user()` exist so a project or account delete can collect the ids before the row cascade and sweep the directories after it.

### Tables

`data_tables` and `data_table_shares` are modeled in `db/models.py` and stored through `db/table_store.py`; the columns, indexes and FK actions are on the classes. What the columns do not say:

- `owner_id` cascades with the user and `project_id` with the project. `schema` (the ORM attribute is `table_schema`) is the ordered column list; the store checks only its shape, and column-level validation belongs to the caller. `row_count` is cached for list views. `last_write_source` names the writer (the forms are listed on the column).
- `require_approval` is the owner's switch, as `docs.require_approval`.
- In `data_table_shares`, `user_id` NULL grants `read` or `write` to everyone on the install. SQLite treats NULLs as distinct in the unique `(table_id, user_id)` index, so the partial unique index `ix_data_table_shares_everyone` allows at most one everyone row per table.
- **Title uniqueness** is case-insensitive per (owner, project, mode), enforced by `_title_taken()` rather than an index (a collision raises `DuplicateTableTitleError`), with the rules of the docs store: a NULL `project_id` is its own scope, the comparison uses Python `casefold()`, and it is check-then-insert with no lock against a simultaneous create.

## Store Rules

`db/table_store.py` follows the async pattern of `db/doc_store.py` and makes no access decisions: its list functions return candidates, and the caller applies the access rule to each row.

- **Per-owner cap**: `create_table()` counts every table the owner has, user and project tables alike, and raises `TableLimitError` at `TABLE_MAX_PER_OWNER`. Like the title check it is check-then-insert, so simultaneous creates can pass it together.
- **Invisibility**: `get_table()` (with the default `with_shares=True`) runs its shares query for a missing id too, so a nonexistent table and an existing one cost the same two queries, and `table_not_found_message()` is the one not-found text for both a missing and a hidden table.
- **Keyset paging**: `list_accessible_tables()` (owned or shared with the user, filtered by scope -- user tables, one project's tables, the public tables of `source_project_ids` -- and optionally by mode) and `list_tables_shared_with()` order by `(updated_at DESC, id DESC)` and take a `(updated_at, id)` cursor plus a limit. The mode filter exists so the SQL `LIMIT` applies to the set a public caller can see.
- **Metadata token**: `update_table_metadata()` (title, description, `require_approval`) takes `expected_updated_at` and raises `StaleTableError`, carrying the current row, on a mismatch; the UPDATE is conditional on the `updated_at` it read, so a writer landing in between also makes it stale. It bumps `updated_at` only when a field changes.
- **Schema token**: `update_schema()` takes `expected_schema_version`, raises `StaleTableSchemaError` (not a `StaleTableError` subclass, since it maps to its own outcome) on a mismatch, and bumps `schema_version` by one in a single UPDATE conditional on the expected version, so of two writers holding the same version exactly one wins.
- **Row writes**: `update_after_write()` records a landed write's `row_count` and source and always bumps `updated_at`; it never touches `schema_version`.
- **Shares**: `add_share()` upserts, so one recipient (or the everyone row) has at most one share row, refuses the owner as a recipient, and raises `ShareTargetGoneError` when the table or recipient was deleted mid-insert. Share changes never bump `updated_at`.

## Feature Gate

The off-by-default, per-user `tables` gate (`FEATURE_TABLES`, `tables_enabled_for()`) is mirrored into the connected-services map as the pseudo-key `tables` (`TABLES_SERVICE_KEY`), so tools and skills that declare `requires_service: "tables"` drop out of the prompt while the gate is closed for the user. The gate deletes nothing. See [Feature Gates -- Enforcement Layers (Quest Tables)](feature-gates.md#enforcement-layers-quest-tables).

## Constraints

- Every cap and retention number shared across modules lives in `chat/tables/constants.py`, and consumers read it from there rather than repeating it; the remaining query, filter and listing limits live with the code that enforces them.
- `TABLE_TITLE_MAX_LEN` matches the `String(200)` title column; raising it needs a migration.

## Design Decisions

**Why `data_tables` instead of `tables`?**
`tables` reads badly next to SQLAlchemy metadata (`Base.metadata.tables`); the feature, gate and constants still say tables.

**Why scope title uniqueness per mode?**
A per-owner or per-project scope that ignored mode would let a public conversation learn from a collision error that a private table of that title exists.

**Why two concurrency tokens instead of one?**
Row writes bump `updated_at` constantly; a single token would make every schema edit in flight go stale on the next row write or rename. `schema_version` changes only with the schema, so it guards exactly the thing a schema edit depends on.

**Why keep rows out of `quest.db`?**
Rows live in a per-table `table.db`, so `quest.db` carries only metadata and deleting a table's data means removing one directory.
