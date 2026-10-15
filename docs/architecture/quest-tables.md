# Quest Tables Architecture

## Overview

A **Quest Table** is a typed table that lives inside Quest, owned by a user (a **user table**) or by a project (a **project table**, owned by the project owner). Each table is a `data_tables` row in `quest.db` holding its metadata and ordered column schema, plus a directory `<data_dir>/tables/<table_id>/` for its own `table.db` (rows and change log); the rows never live in `quest.db`. This doc covers the metadata store, the column and value model, schema operations, the table file (row writes, schema changes, change log, restore), the access rule, the service layer every caller goes through, the table action requests, the realtime events, the delete sweeps and the `tables` [feature gate](feature-gates.md). The query language, the model tools, results and refusals as callers see them are in the [Quest Tables API](../api/tables-api.md). Ownership, scope, modes and shares follow [Quest Docs](quest-docs.md#concepts-and-invariants), with the same terms.

## Key Files

| File | Description |
|------|-------------|
| `chat/tables/constants.py` | Caps and value sets shared across modules, the prompt pseudo-key, the not-found and gate-closed texts |
| `chat/tables/schema.py` | The column model (`Column`), column spec parsing, the stored schema JSON and cell value canonicalization; pure, no I/O |
| `chat/tables/schema_ops.py` | Schema operations (`apply_schema_operations()`), the side effects they emit, and `schema_diff()`; pure, no I/O |
| `chat/tables/storage.py` | The table file layer: `table.db` and its guarded open, row writes, schema changes, the change log, reads, the download copy; synchronous `sqlite3` |
| `chat/tables/revert.py` | Restore to a point by forward replay and the in-effect helpers; its module docstring holds the replay rules |
| `chat/tables/filters.py` | The query layer: filter, sort and paging compiled to parameterized SQL, and the `run_query()` executor ([Quest Tables API](../api/tables-api.md#queries)) |
| `chat/tables/access.py` | `resolve_table_access()` and the table wording of its verdicts; the module docstring holds the matrix |
| `chat/tables/service.py` | The one read/write path for table files: gate, access, the per-table lock, quest.db bumps, events; its module docstring holds the pipeline and the error classes ([Service Layer](#service-layer)) |
| `chat/tables/events.py` / `chat/realtime/events.py` | The two realtime globals: audience and publishers (module docstring); the `make_table_*` envelopes ([Realtime Events](#realtime-events)) |
| `chat/gemini_api/tool_handlers/tables.py` / `chat/tables/write_request.py` | The model tools over the service and the `write_table_rows` params shape ([Quest Tables API -- Model Tools](../api/tables-api.md#model-tools)) |
| `chat/action_request_types/table_precard.py` | The table cards' shared pre-card dispatch and helpers; each type has a handler module and, for the schema and rows cards, a pre-card module beside it ([Action Requests](#action-requests)) |
| `chat/artifact_access.py` | `resolve_core()`, the visibility and write procedure shared with Quest Docs ([Quest Docs -- Access Rule](quest-docs.md#access-rule)) |
| `db/table_store.py` | Async metadata CRUD, title uniqueness, the per-owner cap, candidate lists, the concurrency tokens, shares |
| `db/models.py` | `DataTable`, `DataTableShare` |
| `alembic/versions/e5c97b362821_create_data_tables_and_shares.py` | Creates `data_tables` + `data_table_shares` and their indexes |
| `config/paths.py` / `chat/storage.py` | `TABLES_DIR`; `ChatStorage.get_table_dir()`, the sole id-to-path resolver |
| `config/feature_gates.py` / `api/instructions.py` | `FEATURE_TABLES`, `tables_enabled_for()`; the `tables` pseudo-key in `get_user_connected_services()` and `PSEUDO_SERVICE_KEYS` |

## Modes

Tables have the two modes of Quest Docs, `private` and `public`, with the same purpose: a public table may only ever hold content a public conversation could see. The mode is stored on every row and fixed at creation. A user table (`project_id` NULL) is always private (`create_table()` refuses a public one). A project table takes its project's immutable `projects.public` flag; the caller passes it as `mode`, since the store does not read the project. No store function changes a table's mode.

## Storage

### On-disk layout

A table's files live under `TABLES_DIR/<table_id>/`, a path produced only by `ChatStorage.get_table_dir()`, which applies the canonical-id and containment checks of every resolver and does not create the directory ([Data Paths](data-paths.md#conversation-project-doc-and-table-path-resolution)). The store never touches the directory: `delete_table()` removes the row and its shares, and the caller removes the directory afterwards. Only `create_table_db()` creates it ([The Table File](#the-table-file)). `list_table_ids_for_project()` and `list_table_ids_for_user()` let a project or account delete collect the ids before the row cascade ([Lifecycle](#lifecycle)).

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
- **Keyset paging**: `list_accessible_tables()` (owned or shared, filtered by scope and optionally by mode) and `list_tables_shared_with()` order by `(updated_at DESC, id DESC)` and take a `(updated_at, id)` cursor plus a limit. The mode filter exists so the SQL `LIMIT` applies to the set a public caller can see.
- **Metadata token**: `update_table_metadata()` (title, description, `require_approval`) takes `expected_updated_at` and raises `StaleTableError`, carrying the current row, on a mismatch; the UPDATE is conditional on the `updated_at` it read, so a writer landing in between also makes it stale. It bumps `updated_at` only when a field changes.
- **Schema token**: `update_schema()` takes `expected_schema_version`, raises `StaleTableSchemaError` (not a `StaleTableError` subclass, since it maps to its own outcome) on a mismatch, and bumps `schema_version` by one in a single UPDATE conditional on the expected version, so of two writers holding the same version exactly one wins.
- **Row writes**: `update_after_write()` records a landed write's `row_count` and source and always bumps `updated_at`; it never touches `schema_version`.
- **Shares**: `add_share()` upserts, so one recipient (or the everyone row) has at most one share row, refuses the owner as a recipient, and raises `ShareTargetGoneError` when the table or recipient was deleted mid-insert. Share changes never bump `updated_at`.

## Schema Model

`chat/tables/schema.py` holds every rule about columns and values, so every caller checks a value by one set of rules.

A `Column` is frozen (tuples inside, fresh lists handed out). Its `key` is immutable and is also the SQL column name in the table's `rows`; its `name` is what people and models address. `Column.to_dict()` emits every field in the stored order. Stored JSON is not trusted: `Column.from_dict()` requires every field, refuses unknown ones and re-validates all of them, and `load_schema()` re-checks name and key uniqueness.

**Keys** are `TABLE_COLUMN_KEY_PREFIX` plus `TABLE_COLUMN_KEY_HEX_LEN` random lowercase hex digits (`new_column_key()`), assigned by the server and never shown to models. `parse_column_spec()` refuses a `key` field as `unknown_field` unless the caller passes `allow_key` (server code that assigns keys itself), and then requires it well formed and unused.

**Column specs** go through `parse_column_spec()` (one column joining existing ones) or `parse_columns()` (a new table's full list). Each rule has its own `ColumnSpecError` code:

- Names: `validate_column_name()` checks the shape (the same rule covers enum options and table titles); uniqueness is case-insensitive (`name_taken()`), while operations address a column by its exact name (`find_column()`). The names of the server-maintained row columns (`RESERVED_COLUMN_NAMES`) are refused in any letter case (`reserved_name`).
- Enum options are unique case-insensitively; `options` and `multiple` are refused on other types.
- `unique` is allowed only on the types in `TABLE_UNIQUE_COLUMN_TYPES`; `precision` only on numbers, bounded by `TABLE_NUMBER_MAX_PRECISION`.
- A `default` is canonicalized with the column's own value rules, so a stored default is always a canonical value.

## Canonical Values

Every cell value has one canonical logical form, which defaults hold, models see, and equality and uniqueness compare. The rules are in the `chat/tables/schema.py` module docstring, the caps in `chat/tables/constants.py`; in short:

- **text**: NFC-normalized, line endings turned into LF, capped after normalization.
- **number**: a decimal string with no exponent, no leading `+`, no trailing fractional zeros, every zero as `"0"`; a float goes through its `repr`, so `0.1` stays `0.1`. A column `precision` rounds half-even on write.
- **url**: the stripped string as given, `http` or `https` with a host; a backslash is refused, since parsers and browsers disagree on the host.
- **boolean**: JSON `true` or `false` only.
- **enum**: one option by exact match. A `multiple` enum takes a list, deduplicated and sorted by option order; an empty list is no value.

`coerce_value()` canonicalizes and lets None through; `canonicalize()` adds the `required` check. `apply_default()` is the insert path: an omitted key takes the column default, an explicit null does not. `coerce_option()` checks one option of an enum, the operand of a multi-enum `contains`.

The `table.db` cell form differs from the logical form only for booleans and multi enums; `to_storage()` and `from_storage()` are the only conversions. A refused value raises `TableValueError` ([Refusals](../api/tables-api.md#refusals)).

## Schema Operations

`apply_schema_operations()` in `chat/tables/schema_ops.py` is pure. It takes the current columns, an operation list (the operations and their params are `_OP_PARAMS`) and `table_empty`, applies the operations in order to a copy, each seeing the result of the ones before, and returns a `SchemaChangePlan`. The first refusal raises `SchemaOperationError` (with `op_index`) and nothing applies. The plan carries:

- `columns`: the new schema.
- `effects`: the side effects, in execution order.
- `new_title`: the last `rename_table` title, stripped.
- `operations`: one normalized dict per input operation, with canonical params and the immutable key of every column it addresses (`add_column` carries the full column with its assigned key). It is the change log's audit record, not a replay input: it carries keys, which operations may not name.

`table_empty` only decides whether a required `add_column` needs a default (`required_needs_default`) and whether a `Backfill` is emitted; row checks are emitted either way.

**Effects** are frozen dataclasses (their docstrings say what each does) that the table storage layer executes in order within one change ([Schema Changes](#schema-changes)): column DDL, the capture of a dropped column's values (always ahead of its drop), unique index creation and removal, backfills, enum rewrites and wraps, and violation checks. A violation check refuses the whole change with `rows_violate`, naming at most `TABLE_ROWS_VIOLATE_MAX_IDS` rows, when existing rows break a `required`, `unique` or removed-option rule; a unique check always precedes its index creation.

After the last operation, checks and backfills that the rest of the batch made moot are dropped, judged against the final columns: a `required` or `unique` check whose column is gone or no longer has the flag, and an option check or backfill whose column is gone. A dropped unique check takes with it the index creation its own operation emitted and the first later index removal for that column, since that transient index built unchecked could fail on duplicates. Column DDL, rewrites, wraps and captures are always kept.

**Refusals** carry a code (see the handlers and [Refusals](../api/tables-api.md#refusals)). The structural ones: a column's type never changes (`type_change_not_supported`, for the reserved type-change operation names and for any operation with a `type` field), the last column and the last enum option cannot be removed, an option the default uses cannot be removed, and a multi enum never becomes single.

A `rename_table` title is only validated; title collision is the store's (`_title_taken()`).

**`schema_diff()`** compares two column lists by key for previews, so a rename is a rename rather than a drop plus an add. Each entry has a status, its field changes, a `moved` flag (moving one column flags only that column) and the enum options added and removed; `order_changed` covers the whole list. It sees columns only, so an option rename appears as one removed and one added option, and the caller labels renames from the plan's `EnumRewrite` effects, taken in sequence.

## The Table File

`chat/tables/storage.py` owns everything inside a table's directory; its module docstring describes the layout. Every public function takes the table directory from `ChatStorage.get_table_dir()`, never a database path. `table.db` holds three tables:

- `rows`: `_id`, `_created_at` and `_updated_at` (fixed-width UTC ISO strings, so text order is time order), then one column per schema column named by its key, with a unique index per `unique` column. Only keys that pass `is_valid_column_key()` are interpolated into SQL (`_ident()`).
- `changes`: the [change log](#change-log); its columns are the DDL in `create_table_db()`.
- `meta`: the schema JSON, `schema_version`, `table_id` and `title` (`write_meta()`, `read_meta()`), so a downloaded copy is self-describing. `quest.db` stays the source of truth.

`create_table_db()` is the only function that creates the directory, and creates it exclusively (`storage_exists` for an existing entry of any kind), so a new table never inherits an orphan file; on any failure it removes what it made, so a retry starts clean. The DDL, the meta rows and change #1 (kind `schema`, shape in the docstring) commit in one transaction, so the file has a change log entry from its first commit. Every other function refuses a missing directory with `storage_missing`.

### Opening

Every connection comes from `connect()`: synchronous `sqlite3`, one connection per call, called through `asyncio.to_thread` under the [per-table lock](#service-layer). Each connection runs in WAL mode with the busy timeout `TABLE_DB_BUSY_TIMEOUT_S` (a lock held past it refuses with `busy`, for reads and writes alike) and with SQLite's double-quoted-string fallback disabled, so a quoted key that names no column fails as `schema_mismatch` instead of reading as a string literal. An authorizer denies ATTACH, as for the [project database](project-db.md). `VACUUM INTO` compiles to an ATTACH of its target, so the download copy allows exactly that filename for that one statement (`_attach_allowed()`).

The path guards (listed in the module docstring) refuse with `unsafe_path` anything in the table directory that is not the regular file or directory it should be, including a symlink, a planted FIFO, a side file of the wrong kind and a `table.db` swapped between the check and the connect; side files are checked before and after the pragmas, since enabling WAL creates them.

## Row Writes

`insert_rows()`, `update_rows()`, `upsert_rows()` and `delete_rows()` each run one `BEGIN IMMEDIATE` transaction whose steps the module docstring lists; any refusal rolls back, so nothing is written. With `dry_run=True` the same transaction runs to its end and then rolls back, so it raises exactly what the write would and otherwise returns the would-be result with `change_id` None and the change's before and after row images (`RowWriteResult`), which a real write leaves empty. Payload rows address columns by name; stored rows, images and `dump_rows()` output are keyed by key.

- **insert**: an omitted column takes its default, an explicit null does not.
- **update**: each entry names an `_id` and a partial patch; only the named columns change, an explicit null clears a cell, `_updated_at` moves and `_created_at` stays. An unknown `_id` fails the whole call. Unique values may swap between rows in one call.
- **upsert**: `key_column` must name a unique column, checked before any row work. Each row needs a key value, at most once per call. A row whose key is stored is a partial patch as in update; any other row is an insert with defaults, and only inserts count against `TABLE_MAX_ROWS`.
- **delete**: exactly one of a list of ids or a prepared `(where_sql, params)` pair, which must be trusted SQL built from validated keys with every value bound, never model text. A filter matching more than `TABLE_WRITE_MAX_ROWS` rows refuses with the matching count; one matching nothing writes no change and returns `change_id` None.

## Schema Changes

`apply_schema_change()` executes a `SchemaChangePlan` from [Schema Operations](#schema-operations) on the live table in one `BEGIN IMMEDIATE` transaction that writes one `schema` change entry; its docstring is the full contract. The caller, `service.apply_schema_change()`, loads the columns, `schema_version` and title from `quest.db`, builds the plan with `table_empty` computed under the table lock, and after the call records the result in `quest.db` with `table_store.update_schema()` (schema, version, title, row count) under the schema token.

- **Plan order**: the transaction first checks the file against the passed schema and version (`schema_mismatch`), then runs `plan.effects` exactly in order; operations are never re-derived. A violation check runs against the rows as the earlier effects left them, so a check on a column the batch just added sees that column.
- **Cell rewrites**: a backfill writes the default's stored form to every row; an option rename in a multi enum rewrites each cell, deduplicated and re-sorted by the new column's option order. No rewrite touches `_updated_at`, so a [restore](#restore) that replays the change reproduces rows byte-equal.
- **Stale-plan guard**: after the effects, every column that is newly required (added required or made required) is checked for empty cells, so a plan built with a stale `table_empty=True` (no backfill emitted) cannot leave a required column empty.
- **Meta**: `meta` gets the new schema, `schema_version + 1` and the title, in the same transaction as the change entry.

A refusal leaves the file unchanged. `rows_violate` reports the first failing check in effect order, then the stale-plan guard; a unique index creation that still hits a duplicate re-runs the unique check to report it. `change_too_large` carries an operation-aware hint on how to split the change.

`check_schema_violations()` is the dry run: the same execution inside a transaction that always rolls back, so it raises exactly what the real call would and writes nothing. It takes the write lock for its duration. When the change would apply it returns what it would touch (counts per column key and the rows backfilled; shape in the docstring). `service.preview_schema_change()` runs it, so the `change_table_schema` pre-card refuses a change before the card opens rather than on Approve.

## Queries

`chat/tables/filters.py` compiles a query into SQL over `rows`: validated keys quoted by `_ident()`, every value bound. Its contract (grammar, operand rules, empty-cell semantics, sort, caps, the result) is owned by [Quest Tables API -- Queries](../api/tables-api.md#queries). `run_query()` runs the count and the page in one read transaction; a filter delete passes `compile_filter()`'s `(where_sql, params)` pair to `delete_rows()`.

## Change Log

Every write appends one `changes` entry inside its own transaction (`append_change()`). A `rows` change's `before` and `after` are lists of full stored rows sorted by `_id`: `_id`, both timestamps and every schema column, keyed by key, cells in stored form. An insert carries only `after`, a delete only `before`, an update both, and an upsert's `before` holds only the rows that existed before the call. A [restore](#restore) replays a change from its `after` image and `row_ids` alone; the `before` image serves the History detail view.

A `schema` change (one per [schema change](#schema-changes); change #1 has its own shape, in `create_table_db()`) has dict images defined in the `apply_schema_change()` docstring: both sides' columns, title and physical column order, the dropped columns' values, the ordered cell rewrites a restore replays forward, the backfills and the normalized operations. Rewrite steps list row ids, not cell copies, wherever the old value is derivable; the change's `row_ids` name every row whose stored cells it touched.

A `restore` entry has `before` `{}` and `after` `{"restore_to": <change_id>}` ([Restore](#restore)).

`size_bytes` is the UTF-8 length of both encoded images. Above `TABLE_CHANGE_MAX_BYTES` the write is refused (`ChangeTooLargeError`, with a caller-supplied hint on how to split it), so each replay step of a restore reads one bounded entry. The log is kept in full from table creation. `get_change()` and `list_changes()` read the log.

## Restore

`restore_to()` in `chat/tables/revert.py`, the only revert operation, rebuilds the table as it was right after a chosen change; its docstrings hold the rules.

- **Replay path.** Defined in the module docstring; `replay_path()` walks it in linear time.
- **In effect.** An entry is in effect when the path to the newest entry contains it. Nothing is stored for it and a page of the log cannot decide it alone, so `list_changes_in_effect()` reads the page and the light `(id, kind, restore_to)` records in one read transaction.
- **Replay.** Into a temporary `table.db.restore-<random>` beside the live file, applying each entry on the path as the module docstring describes (rows verbatim under their original `_id`s; schema entries reproducing column and index order exactly). Only states the table passed through are rebuilt, so nothing is re-validated; memory is bounded by one entry. Images that do not replay are `corrupt_storage`.
- **Finish.** The log is copied unchanged plus one `restore` entry (`schema_after` one past `schema_before`, so open views reload); the `rows` sequence goes past every `_id` the log names and the live sequence; `meta` gets the target's columns and title.
- **Swap.** The temp file is built in rollback-journal mode, switched to WAL, closed and fsynced. The live file is checkpointed with `wal_checkpoint(TRUNCATE)` and its connection closed so its `-wal` and `-shm` disappear; a side file left behind means another connection is open (`busy`). Only then does `os.replace()` swap the file in, since SQLite would replay a stale WAL into whatever file holds the name. The final re-check that the log did not move is best effort; only the [per-table lock](#service-layer) prevents a write landing before the rename.
- **Failure.** A refusal or error before the rename leaves the live file untouched and removes the temp file; temp files a crashed restore left are removed at the start of the next one. After the rename a failing directory fsync is only logged, since the table is already restored.

Updating `quest.db` from the `RestoreResult` is the caller's job (`service.apply_restore()`). Restoring to the newest entry is allowed and records a restore entry over the same state.

## Download Copy

`copy_for_download()` writes a single-file SQLite copy with `VACUUM INTO`: `rows` and `meta`, plus `changes` only with `include_history`. The copy is left in rollback-journal mode, so no side file belongs to it. The destination must not exist (`copy_exists`), and a partial copy is removed on failure.

## Access

`resolve_table_access()` in `chat/tables/access.py` is the one access decision for a table: the tools, the HTTP routes (`run_kind="ui"`), the [table cards](#action-requests) (at pre-card and at Approve), `list_tables` and the prompts consume its `TableAccess` and re-derive nothing. The module docstring holds the matrix and the ordered schema rules; `tests/test_table_access.py` covers every cell.

**Rows** follow the Quest Docs rule word for word, with "table" for "doc" and `write_table_rows` for `write_doc`: the procedure is `resolve_core()` in `chat/artifact_access.py` ([Quest Docs -- Access Rule](quest-docs.md#access-rule)), and `_DENY_TEXT` maps its deny causes to table wording. So a project table is reachable only from its project's conversations (and the UI), plus read-only from a private project's conversations when its project is a doc source; public conversations never see private tables; scripts see user tables only; and the owner's `require_approval` switch turns a free rows verdict into an approval tagged `required_by_owner`, denied in public conversations and Slack runs, where no card can open.

**Schema** is a second verdict, never `free`: no model-facing tool changes a schema, only the `change_table_schema` card executor and the UI routes do. It is denied with the rows reason wherever the rows verdict is read-only; otherwise it is an approval (a card) where the rows path could reach one, and denied in Slack runs and public conversations, with a reason that tells the model to ask the user to make the change in the Quest UI. In the UI the owner gets approval (their own save in the schema editor is the approval) and a write-share recipient is denied: schema edits are owner-only, though the recipient edits rows and may restore the table, schema changes included, because the restore route gates on rows `free` with `run_kind="ui"`, not on `schema`. Since the two verdicts can differ, `TableAccess` carries `schema_deny_reason` beside `deny_reason`.

The module's helpers word a verdict for callers ([Refusals](../api/tables-api.md#refusals)); `create_table_deny_reason()` limits `create_table` cards to top-level private conversations. A hidden table is indistinguishable from a missing one: `not_found_message()` returns `table_not_found_message()` for both, and callers make the same lookups on both paths. Ownership-only actions (rename, `require_approval`, delete, shares) are checked by the routes, outside the matrix.

## Service Layer

`chat/tables/service.py` is the one read/write path for table files: the model tools, the table action requests and the HTTP routes call it, and nothing else opens a `table.db`. Its module docstring gives the pipeline (gate, lookup, verdict, lock, file call in a thread, `quest.db` bump, events) and the error classes; each function's docstring gives its result and refusals. Refusals the service decides are `TableError` subclasses with a `code`; those of the storage, filter and schema layers propagate unchanged, so tools and routes map every refusal by its `code` ([Refusals](../api/tables-api.md#refusals)).

**Caller.** A `Caller` carries the user, conversation, project, `is_public` and `run_kind`, which the service passes to `resolve_table_access()` together with the doc-source projects (only a private project's conversations have them). Tool dispatch builds it in `_table_caller()` (`chat/gemini_api/tool_dispatch.py`): a script caller has no conversation and no project and is never public, so scripts see user tables only. Routes use `caller_for_ui()` (`run_kind="ui"`).

**The per-table lock.** Each table id has one `asyncio.Lock`, held by every reader and every writer of `table.db` for the whole file call, and by writers also through the `quest.db` bump and the events, so `row_count`, `schema_version` and `updated_at` land in write order. Readers take it too, because a restore swaps the file in and needs no other connection open ([Restore](#restore)). The lock is process-local (one event loop per process).

- **Resolve twice.** Access is resolved before the lock, so a hidden or denied table never takes or creates one, then the row is re-fetched and re-resolved under it, so a share revoked or a schema changed while the call queued decides the outcome. Without `bypass_approval`, an approval verdict dry-runs the write under the lock first, so invalid rows get the free path's refusal and only a write that would succeed raises `TableApprovalRequired`. With `bypass_approval=True` (the card executor) an approval verdict proceeds, while hidden and denied still refuse.
- **Previews and pins.** `preview_row_write()` and `preview_schema_change()` dry-run a row write and a schema change under the lock, with nothing written, bumped or published. The first adds the change's row images by column name and `rows_digest()` of its before images; the second returns the plan, `schema_diff()` and the `check_schema_violations()` counts. The executors pin what their card showed through `apply_row_write()` and `apply_schema_change()` ([Action Requests](#action-requests)).
- **Shielding.** Row writes, schema changes, restores, creates and deletes run shielded from cancellation (`_shielded()`), so the Stop button mid-turn cannot land between the file commit and the `quest.db` bump; the caller still sees the cancellation.
- **Lost schema bump.** If the `quest.db` bump of a schema change or restore is lost anyway (the file committed, then `update_schema()` failed or the process died), every later call would fail the file's schema check. On that `schema_mismatch`, `_heal_lost_schema_bump()` records the file's schema, row count and newest source in `quest.db` when the file is exactly one version ahead and tells the audience; the call still refuses, so its retry works.
- **Schema changes and restores** both record through `update_schema()`, since a restore always bumps `schema_version`. A `rename_table` title goes to `quest.db` first, so a collision refuses before the file changes, and is rolled back if the file change fails.
- **Create** lays out the directory under a fresh id, then inserts the row; a failed insert removes the directory, so a crash in between leaves an invisible orphan directory, never a row without a file. Creates are serialized per owner (an `owner:<id>` lock), so the store's check-then-insert title rule and per-owner cap hold. Authorizing a create is the caller's job (the routes, the `create_table` card); the service checks the gate, the column specs, the project's owner and the mode (`mode_mismatch`: a conversation creates only tables of its own mode).
- **Delete** (owner only) runs under the table lock: it captures the audience, deletes the row (the shares cascade), removes the directory best effort and then publishes to the captured audience.

## Action Requests

`create_table`, `change_table_schema` and `write_table_rows` are the approval forms of a create, a schema change and a row write; the card contract is in [Quest Tables API -- Action Requests](../api/tables-api.md#action-requests). The pipeline is the one of [the write_doc card](quest-docs.md#the-write_doc-action-request): `validate_params` checks shape only, a pre-card dry-runs the proposal against the live table and injects the preview, and `execute` re-checks against the table at Approve.

- **Pre-card.** `table_precard_check()` (called from the `create_action_request` arm after the `write_doc` pre-card) dispatches to one arm per type. Each arm checks the gate before any DB work and decides with the proposal's conversation as caller (`run_kind` `top_level`, or `slack` for a Slack run; `is_public` from the conversation's project). A refusal is a same-turn `Invalid parameters` with no card; tables-layer errors keep their code and text, so a hidden table stays byte-equal to a missing one. A row write that needs no card is refused (`use_tool_directly`).
- **Approve-time caller.** `execute_caller()` rebuilds the caller from the proposal's conversation and project, never a UI caller, so a card from a private conversation can never write a public table and a share revoked meanwhile hides the table. A refusal raises `RuntimeError` and leaves the request open ([Execution Failure Behavior](action-requests.md#execution-failure-behavior)).
- **TOCTOU.** `create_table` refuses `stale_request` unless the columns still parse to the card's `column_specs`, and the store re-checks the title collision under the per-owner lock (`duplicate_title`). `change_table_schema` passes the card's `expected_schema_version` (`stale_schema`), and the change re-runs the `rows_violate` checks under the lock. `write_table_rows` pins the proposal's `schema_version` and `rows_digest`: after its re-fetch, in the same lock hold as the write, `apply_row_write()` checks the version (`stale_schema`), then dry-runs the write and compares the digest of the rows it would touch (`rows_changed`, naming their current values). The digest covers row ids and stored cells, not the schema, hence the version pin beside it.
- **Attribution.** The resolve route passes `request_id` to these types (`REQUEST_ID_TYPES` in `chat/action_request_routes.py`). The change's source and the table's `last_write_source` are `action_request:<id>` (`write_source()`), the actor the approver; a card is approved in its own conversation, so that is the proposer, whom `get_action_request_owners()` returns for the id.

## Realtime Events

`chat/tables/events.py` publishes two transient per-user globals: `table_list_changed` (list views re-fetch) and `table_changed {table_id, updated_at, schema_version}` (an open viewer re-fetches its grid, and its schema when `schema_version` moved). A null `updated_at` (with a null `schema_version`) means the table's existence or the receiver's access changed. The envelopes are `make_table_list_changed()` and `make_table_changed()` in `chat/realtime/events.py`; the protocol is in [Realtime](realtime.md#backend-publish-sites-per-user-globals).

The audience is the owner plus every direct share recipient, and every user connected at publish time when the table has an everyone share. It is not filtered by gate or access: such a user can receive a table id, a timestamp and a version, never a title or content, and any fetch goes through the access rule. A delete captures the audience before the share rows cascade away. The module docstring lists who publishes what. An account deletion notifies no one.

## Lifecycle

A project delete (`delete_user_project()` in `chat/project_routes.py`) collects the project's tables with their shares before the cascade, then removes their directories (`service.delete_table_dirs()`) and publishes a null `table_changed` per table and one `table_list_changed` to the combined audience. An account delete (`delete_account()` in `chat/routes/user.py`) collects `list_table_ids_for_user()` before the cascade and removes the directories after it. Both sweeps run after the rows are gone and without the table lock, so a call still queued on the lock fails its re-fetch as not found; a sweep failure is logged, never raised.

## Feature Gate

The off-by-default, per-user `tables` gate (`FEATURE_TABLES`, `tables_enabled_for()`) is mirrored into the connected-services map as the pseudo-key `tables` (`TABLES_SERVICE_KEY`), so tools and skills that declare `requires_service: "tables"` drop out of the prompt while the gate is closed for the user. The gate deletes nothing. See [Feature Gates -- Enforcement Layers (Quest Tables)](feature-gates.md#enforcement-layers-quest-tables).

## Constraints

- Every cap shared across modules lives in `chat/tables/constants.py`, and consumers read it from there rather than repeating it; listing limits live with the tools and routes that enforce them.
- `TABLE_TITLE_MAX_LEN` matches the `String(200)` title column; raising it needs a migration.

## Design Decisions

**Why `data_tables` instead of `tables`?**
`tables` reads badly next to SQLAlchemy metadata (`Base.metadata.tables`); the feature, gate and constants still say tables.

**Why scope title uniqueness per mode?**
A per-owner or per-project scope that ignored mode would let a public conversation learn from a collision error that a private table of that title exists.

**Why two concurrency tokens instead of one?**
Row writes bump `updated_at` constantly; a single token would make every schema edit in flight go stale on the next row write or rename. `schema_version` changes only with the schema, so it guards exactly the thing a schema edit depends on.

**Why keep rows out of `quest.db`?**
Each table's rows live in its own `table.db`, so deleting its data means removing one directory.

**Why store numbers as canonical decimal strings instead of SQLite REAL?**
A REAL rounds past about 15 significant digits and makes equality depend on binary representation; one canonical string per value makes equality and uniqueness exact string comparisons.

**Why does the digit cap count rendered digits rather than significant digits?**
The rendered string is what is stored and shown; a significant-digit cap would let a value like 1e300 render as hundreds of digits.

**Why server-assigned, immutable column keys?**
Cells are stored under the key, so a rename edits only the schema JSON and never touches DDL, and the change log and diff follow a column across renames; keeping keys out of model input means a model cannot forge or collide them.

**Why no column type changes?**
Converting stored values in place needs a rule per type pair and loses values; adding a column of the new type and copying with row writes keeps every step an ordinary change in the log.

**Why synchronous `sqlite3` through `asyncio.to_thread` instead of `aiosqlite`?**
The guards need the raw connection before the first statement (authorizer, double-quoted-string setting, inode recheck) and an authorizer swap around `VACUUM INTO`; and since writes are serialized by the per-table lock, wrapping one whole write function costs one thread hop per write where `aiosqlite` hops once per statement.

**Why AUTOINCREMENT on `rows._id`?**
A plain `INTEGER PRIMARY KEY` hands a deleted top id out again. A restore replays rows under their original `_id`s, so reuse would make one id name two different rows across the log.

**Why change images of full stored rows keyed by key?**
Keys survive renames, so an old image still applies after a column rename; full rows in stored form make a replay a plain INSERT, with no re-canonicalization and no merge against the current row.

**Why no NOT NULL on user columns?**
`required` is enforced in Python on every write and by violation checks on schema changes; in the DDL, toggling it would need a table rebuild, since SQLite cannot alter a column constraint.

**Why restore by forward replay instead of reversing changes one by one?**
Replaying `after` images from change #1 only rebuilds states the table passed through, so a restore can never clash with a later change, needs no re-validation and is never refused for size, and restoring past a restore is the same operation. It costs time linear in the log, fine for a rare, human-initiated action.

**Why pin a row write card with a digest of the touched rows rather than `updated_at`?**
Every row write bumps `updated_at`, so that token would make every open card stale on any unrelated write; the digest changes only when a row the write touches, or the set it matches, changes.

**Why keep the whole log?**
Forward replay starts at change #1, so a shortened log would make every point unreachable; the per-entry size cap bounds each replay step.
