# Quest Tables Architecture

## Overview

A **Quest Table** is a typed table that lives inside Quest, owned by a user (a **user table**) or by a project (a **project table**, owned by the project owner). Each table is a `data_tables` row in `quest.db` holding its metadata and ordered column schema, plus a directory `<data_dir>/tables/<table_id>/` for its own `table.db` (rows and change log); the rows never live in `quest.db`. This doc covers the metadata layer (the two `quest.db` tables, the store module and its rules, the on-disk path, the constants and the per-user `tables` [feature gate](feature-gates.md)) the column layer (the column schema model, canonical cell values and schema operations) and the table file layer (the `table.db` layout, its guarded open, row writes, the change log and the download copy). Ownership, scope, modes and shares follow [Quest Docs](quest-docs.md#concepts-and-invariants), with the same terms.

## Key Files

| File | Description |
|------|-------------|
| `chat/tables/constants.py` | The caps and retention numbers shared across modules (`TABLE_*`), the mode, share-permission and column-type sets, the prompt pseudo-key `TABLES_SERVICE_KEY`, `table_not_found_message()` (the one not-found text) and `tables_disabled_message()` |
| `chat/tables/schema.py` | The column model (`Column`), column spec parsing, the stored schema JSON and cell value canonicalization; pure, no I/O |
| `chat/tables/schema_ops.py` | Schema operations (`apply_schema_operations()`), the side effects they emit, and `schema_diff()`; pure, no I/O |
| `chat/tables/storage.py` | The table file layer: `table.db` DDL and its guarded open, the row writes, the change log and its pruning, row and change reads, the download copy; synchronous `sqlite3` |
| `db/table_store.py` | Async metadata CRUD, title uniqueness (`_title_taken()`), the per-owner cap, the keyset candidate lists, the two concurrency tokens, the id collectors for directory sweeps, the share helpers |
| `db/models.py` | `DataTable`, `DataTableShare` |
| `alembic/versions/e5c97b362821_create_data_tables_and_shares.py` | Creates `data_tables` + `data_table_shares` and their indexes |
| `config/paths.py` / `chat/storage.py` | `TABLES_DIR`; `ChatStorage.get_table_dir()`, the sole id-to-path resolver |
| `config/feature_gates.py` / `api/instructions.py` | `FEATURE_TABLES`, `tables_enabled_for()`; the `tables` pseudo-key in `get_user_connected_services()` and `PSEUDO_SERVICE_KEYS` |

## Modes

Tables have the two modes of Quest Docs, `private` and `public`, with the same purpose: a public table may only ever hold content a public conversation could see. The mode is stored on every row and fixed at creation. A user table (`project_id` NULL) is always private, and `create_table()` refuses a public one (`TableValidationError`). A project table takes its project's immutable `projects.public` flag; the caller passes it as `mode`, since the store does not read the project. No store function changes a table's mode.

## Storage

### On-disk layout

A table's files live under `TABLES_DIR/<table_id>/`, a path produced only by `ChatStorage.get_table_dir()`, which applies the canonical-id and containment checks of every resolver and does not create the directory ([Data Paths](data-paths.md#conversation-project-doc-and-table-path-resolution)). The store never touches the directory: `delete_table()` removes the row and its shares, and the caller removes the directory afterwards. Only `create_table_db()` creates it ([The Table File](#the-table-file)). `list_table_ids_for_project()` and `list_table_ids_for_user()` exist so a project or account delete can collect the ids before the row cascade and sweep the directories after it.

### Tables

`data_tables` and `data_table_shares` are modeled in `db/models.py` and stored through `db/table_store.py`; the columns, indexes and FK actions are on the classes. What the columns do not say:

- `owner_id` cascades with the user and `project_id` with the project. `schema` (the ORM attribute is `table_schema`) is the ordered column list in the stored form of the [Schema Model](#schema-model); the store checks only its shape, and callers convert it with `load_schema()` and `dump_schema()`. `row_count` is cached for list views. `last_write_source` names the writer (the forms are listed on the column).
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

## Schema Model

`chat/tables/schema.py` holds every rule about columns and values, so every caller checks a value by one set of rules.

A `Column` is frozen (tuples inside, fresh lists handed out). Its `key` is immutable and is also the SQL column name in the table's `rows`; its `name` is what people and models address. `Column.to_dict()` emits every field in the stored order. Stored JSON is not trusted: `Column.from_dict()` requires every field, refuses unknown ones and re-validates all of them, and `load_schema()` re-checks name and key uniqueness.

**Keys** are `TABLE_COLUMN_KEY_PREFIX` plus `TABLE_COLUMN_KEY_HEX_LEN` random lowercase hex digits (`new_column_key()`), assigned by the server and never shown to models. `parse_column_spec()` refuses a `key` field as `unknown_field` unless the caller passes `allow_key` (server code that assigns keys itself), and then requires it well formed and unused.

**Column specs** go through `parse_column_spec()` (one column joining existing ones) or `parse_columns()` (a new table's full list). Each rule has its own `ColumnSpecError` code:

- Names: `validate_column_name()` checks the shape (no control characters, format characters such as zero-width or bidi controls, or line and paragraph separators; the same rule covers enum options and table titles); uniqueness is case-insensitive (`name_taken()`), while operations address a column by its exact name (`find_column()`). The names of the server-maintained row columns (`RESERVED_COLUMN_NAMES`) are refused in any letter case (`reserved_name`).
- Enum options: a bounded, non-empty list of bounded strings with no surrounding whitespace or control characters, unique case-insensitively. `options` and `multiple` are refused on other types.
- `unique` is allowed only on the types in `TABLE_UNIQUE_COLUMN_TYPES`; `precision` only on numbers, bounded by `TABLE_NUMBER_MAX_PRECISION`.
- A `default` is canonicalized with the column's own value rules, so a stored default is always a canonical value.

## Canonical Values

Every cell value has one canonical logical form, which defaults hold, models see, and equality and uniqueness compare. The rules are in the `chat/tables/schema.py` module docstring, the caps in `chat/tables/constants.py`; in short:

- **text**: NFC-normalized, CRLF and lone CR turned into LF, capped after normalization. NUL and unpaired surrogates are refused, since neither can be stored as UTF-8 text.
- **number**: a decimal string with no exponent, no leading `+`, no trailing fractional zeros, every zero as `"0"`. Input is a JSON number or a plain decimal string (an exponent is allowed; separators, units, NaN and Infinity are not); a float goes through its `repr`, so `0.1` stays `0.1`. `TABLE_NUMBER_MAX_DIGITS` counts the digits of the rendered string. A column `precision` rounds half-even, and a value below half a unit of the last place becomes `"0"`.
- **url**: the stripped string as given, `http` or `https` with a host and a valid port. Internal whitespace, control characters (C1 included), format characters, line and paragraph separators and backslashes are refused; with a backslash the parser and browsers disagree on the host.
- **boolean**: JSON `true` or `false` only.
- **enum**: one option by exact match. A `multiple` enum takes a list, deduplicated and sorted by option order; an empty list is no value.

`coerce_value()` canonicalizes and lets None through (its docstring names the filter ops that do not use it). `canonicalize()` adds the `required` check. `apply_default()` is the insert path: an omitted key takes the column default, an explicit null does not. `coerce_option()` checks one option of an enum, the operand of a multi-enum `contains`.

The `table.db` cell form differs from the logical form only for booleans (1 / 0) and multi enums (compact JSON); `to_storage()` and `from_storage()` are the only conversions. A refused value raises `TableValueError`, whose codes are listed in the module docstring and whose `detail` is the reason without the column prefix, for an `invalid_rows` problem that reports the column separately.

## Schema Operations

`apply_schema_operations()` in `chat/tables/schema_ops.py` is pure. It takes the current columns, an operation list (the operations and their params are `_OP_PARAMS`) and `table_empty`, applies the operations in order to a copy, each seeing the result of the ones before, and returns a `SchemaChangePlan`. The first refusal raises `SchemaOperationError` (with `op_index`) and nothing applies. The plan carries:

- `columns`: the new schema.
- `effects`: the side effects, in execution order.
- `new_title`: the last `rename_table` title, stripped.
- `operations`: one normalized dict per input operation, with canonical params and the immutable key of every column it addresses (`add_column` carries the full column with its assigned key). It is the change log's audit record, not a replay input: it carries keys, which operations may not name.

`table_empty` only decides whether a required `add_column` needs a default (`required_needs_default`) and whether a `Backfill` is emitted; row checks are emitted either way.

**Effects** are frozen dataclasses with a `kind` and `to_dict()`; the table storage layer executes them in order within one change: column DDL (`AddColumnDDL`, `DropColumnDDL`), `CaptureDroppedValues` (every value of a dropped column saved into the change's before-image, always ahead of its drop), unique index creation and removal, `Backfill` (a new required column's default written to every row), `EnumRewrite` (an option rename applied to every cell, inside the JSON list for a multi enum), `WrapMulti` (single-enum cells wrapped into lists), and `ViolationCheck`. A violation check refuses the whole change with `rows_violate`, naming at most `TABLE_ROWS_VIOLATE_MAX_IDS` rows, when existing rows break a `required`, `unique` or removed-option rule; a unique check always precedes its index creation.

After the last operation, checks and backfills that the rest of the batch made moot are dropped, judged against the final columns: a `required` or `unique` check whose column is gone or no longer has the flag, and an option check or backfill whose column is gone. A dropped unique check takes with it the index creation its own operation emitted and the first later index removal for that column, since building that transient index unchecked over duplicate values would fail; removing an index that existed before the batch is kept. Column DDL, rewrites, wraps and captures are always kept.

**Refusals** carry a code (see the handlers). The structural ones: a column's type never changes (`type_change_not_supported`, for the reserved type-change operation names and for any operation with a `type` field), the last column and the last enum option cannot be removed, an option the default uses cannot be removed, and a multi enum never becomes single.

A `rename_table` title is only validated; title collision is the store's (`_title_taken()`).

**`schema_diff()`** compares two column lists by key for previews, so a rename is a rename rather than a drop plus an add. Each entry has a status, its field changes, a `moved` flag (moving one column flags only that column) and the enum options added and removed; `order_changed` covers the whole list. It sees columns only, so an option rename appears as one removed and one added option, and the caller labels renames from the plan's `EnumRewrite` effects, taken in sequence.

## The Table File

`chat/tables/storage.py` owns everything inside a table's directory; its module docstring describes the layout. Every public function takes the table directory from `ChatStorage.get_table_dir()`, never a database path. `table.db` holds three tables:

- `rows`: `_id INTEGER PRIMARY KEY AUTOINCREMENT`, `_created_at` and `_updated_at` (fixed-width UTC ISO strings, so text order is time order), then one column per schema column named by its key, typed by `sql_type()`. User columns carry no NOT NULL. Each `unique` column has a unique index named by `unique_index_name()`. Only keys that pass `is_valid_column_key()` are interpolated into SQL (`_ident()`).
- `changes`: the [change log](#change-log); its columns are the DDL in `create_table_db()`.
- `meta`: the schema JSON, `schema_version`, `table_id` and `title` (`write_meta()`, `read_meta()`), so a downloaded copy is self-describing. `quest.db` stays the source of truth.

`create_table_db()` is the only function that creates the directory, and creates it exclusively (`storage_exists` for an existing entry of any kind), so a new table never inherits an orphan file; on any failure it removes what it made, so a retry starts clean. The DDL, the meta rows and change #1 (kind `schema`, shape in the docstring) commit in one transaction, so the file has a change log entry from its first commit. Every other function refuses a missing directory with `storage_missing`.

### Opening

Every connection comes from `connect()`: synchronous `sqlite3`, one connection per call, which the service calls through `asyncio.to_thread` under its per-table lock. Each connection runs in WAL mode with the busy timeout `TABLE_DB_BUSY_TIMEOUT_S` (a lock held past it refuses with `busy`) and with SQLite's double-quoted-string fallback disabled, so a quoted key that names no column fails as `schema_mismatch` instead of reading as a string literal. An authorizer denies ATTACH, as for the [project database](project-db.md), although no model SQL reaches the file. `VACUUM INTO` compiles to an ATTACH of its target, so the download copy allows exactly that filename for that one statement (`_attach_allowed()`).

The path guards refuse with `unsafe_path`: a table directory that is not a real directory (`lstat`, so a symlink fails); a `table.db` that is a symlink or not a regular file (`O_NOFOLLOW` plus `fstat`, opened non-blocking so a planted FIFO cannot hang the open); a `-wal`, `-shm` or `-journal` side file that is not a regular file, checked before and after the pragmas since enabling WAL creates them; and a `table.db` whose device and inode changed between the check and the connect.

## Row Writes

`insert_rows()`, `update_rows()`, `upsert_rows()` and `delete_rows()` each run one `BEGIN IMMEDIATE` transaction (`_write_txn()`): check the live file against the schema and `schema_version` the caller passed, validate every row collecting every problem, apply the caps, check unique columns against the state after the write, apply it, append the change entry and prune. Any refusal rolls back, so nothing is written. Payload rows address columns by name; stored rows, images and `dump_rows()` output are keyed by key. Values go through `apply_default()` (inserts) or `canonicalize()` (patches), then `to_storage()`. `RowWriteResult` describes the outcome.

- **insert**: an omitted column takes its default, an explicit null does not.
- **update**: each entry names an `_id` and a partial patch; only the named columns change, an explicit null clears a cell, `_updated_at` moves and `_created_at` stays. An unknown `_id` fails the whole call. Unique values may swap between rows in one call.
- **upsert**: `key_column` must name a unique column, checked before any row work. Each row needs a key value, at most once per call. A row whose key is stored is a partial patch as in update; any other row is an insert with defaults, and only inserts count against `TABLE_MAX_ROWS`.
- **delete**: exactly one of a list of ids or a prepared `(where_sql, params)` pair, which must be trusted SQL built from validated keys with every value bound, never model text. A filter matching more than `TABLE_WRITE_MAX_ROWS` rows refuses with the matching count; one matching nothing writes no change and returns `change_id` None.

## Change Log

Every write appends one `changes` entry inside its own transaction (`append_change()`). A `rows` change's `before` and `after` are lists of full stored rows sorted by `_id`: `_id`, both timestamps and every schema column, keyed by key, cells in stored form. An insert carries only `after`, a delete only `before`, an update both, and an upsert's `before` holds only the rows that existed before the call. The images carry what a revert needs to replay a change as a plain INSERT, UPDATE or DELETE.

`size_bytes` is the UTF-8 length of both encoded images. Above `TABLE_CHANGE_MAX_BYTES` the write is refused (`ChangeTooLargeError`, with a caller-supplied hint on how to split it), so every recorded change stays undoable. `prune_changes()` runs in the same transaction, dropping entries older than `TABLE_CHANGE_RETENTION_DAYS` and then all but the newest `TABLE_CHANGE_MAX_COUNT`. `get_change()` and `list_changes()` read the log.

## Refusals

Every refusal is a `TableStorageError` with a machine `code` and a model-facing `message`; the class docstring lists the plain codes, and each subclass carries its own. Each write's docstring names the refusals and problem codes it can raise. What the docstrings do not say:

- `invalid_rows` (`InvalidRowsError`) carries every problem of the call as `{row, column, code, reason}`, ordered by row, then schema column order.
- `too_many_rows`, `table_full` and `change_too_large` are the caps; `unknown_key_column` and `key_not_unique` (`UpsertKeyError`) refuse an upsert key.
- `schema_mismatch` means `quest.db` and `table.db` disagree on the schema; it is the file-level check, distinct from the store's `StaleTableSchemaError` token.
- `constraint_failed` is a residual SQLite integrity error that the Python checks should have caught; its message is fixed, so no SQLite text reaches a model.
- `busy`, `unsafe_path`, `storage_missing`, `storage_exists` and `corrupt_storage` come from the file itself; `no_rows`, `invalid_selector` and `copy_exists` from malformed calls.

## Download Copy

`copy_for_download()` writes a single-file SQLite copy with `VACUUM INTO`: `rows` and `meta`, plus `changes` only with `include_history`. The copy is left in rollback-journal mode, so no side file belongs to it. The destination must not exist (`copy_exists`), and a partial copy is removed on failure.

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

**Why store numbers as canonical decimal strings instead of SQLite REAL?**
A REAL rounds past about 15 significant digits and makes equality depend on binary representation; one canonical string per value makes equality and uniqueness exact string comparisons.

**Why does the digit cap count rendered digits rather than significant digits?**
The rendered string is what is stored and shown; a significant-digit cap would let a value like 1e300 render as hundreds of digits.

**Why server-assigned, immutable column keys?**
Cells are stored under the key, so a rename edits only the schema JSON and never touches DDL, and the change log and diff follow a column across renames; keeping keys out of model input means a model cannot forge or collide them.

**Why no column type changes?**
Converting stored values in place needs a rule per type pair and a lossy undo; adding a column of the new type and copying with row writes keeps every step an ordinary, undoable change.

**Why synchronous `sqlite3` through `asyncio.to_thread` instead of `aiosqlite`?**
The guards need the raw connection before the first statement (authorizer, double-quoted-string setting, inode recheck) and an authorizer swap around `VACUUM INTO`; and since writes are serialized by the per-table lock, wrapping one whole write function costs one thread hop per write where `aiosqlite` hops once per statement.

**Why AUTOINCREMENT on `rows._id`?**
A plain `INTEGER PRIMARY KEY` hands a deleted top id out again. Undo re-inserts a deleted row under its original `_id`, so reuse would let a later insert collide with a pending undo and make a change's `row_ids` ambiguous.

**Why change images of full stored rows keyed by key?**
Keys survive renames, so an old image still applies after a column rename; full rows in stored form make a replay a plain INSERT or UPDATE, with no re-canonicalization and no merge against the current row.

**Why no NOT NULL on user columns?**
`required` is enforced in Python on every write and by violation checks on schema changes; in the DDL, toggling it would need a table rebuild, since SQLite cannot alter a column constraint.
