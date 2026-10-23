# Quest Tables API

## Overview

The caller-facing contract of [Quest Tables](../architecture/quest-tables.md): the query language a caller passes to read or select rows (filter, sort, column list, paging), the result it gets back, the refusals it can receive, each a machine `code` plus a message written for a model or a person, the model tools built on them, the three table action requests, and the HTTP routes of the Quest UI. Storage, schema operations, the change log, restore, the access matrix and prompting are architecture and live in the architecture doc.

## Key Files

| File | Description |
|------|-------------|
| `chat/tables/filters.py` | The query language: grammar, ops per column type and the SQL each op compiles to (module docstring), `FilterError`, `run_query()` |
| `chat/tables/constants.py` | The filter, sort and paging caps (`TABLE_FILTER_*`, `TABLE_SORT_MAX_KEYS`, `TABLE_QUERY_*`, `TABLE_GRID_MAX_LIMIT`) |
| `chat/tables/storage.py` | `TableStorageError` and its subclasses, the table file refusals |
| `chat/tables/schema.py` / `chat/tables/schema_ops.py` | `ColumnSpecError`, `TableValueError`, `SchemaOperationError` |
| `chat/tables/access.py` | Deny reasons, notes, `approval_required` and not-found messages |
| `db/table_store.py` | The metadata refusals (title, cap, concurrency tokens, shares) |
| `chat/tables/service.py` | `TableError` and its subclasses, the refusals the service layer decides |
| `chat/llm/tool_schemas.py` | The seven table tool specs (`requires_service: "tables"`, row writes `mutating`), their allowlist and skill entries |
| `chat/gemini_api/tool_handlers/tables.py` | The tool handlers: argument coercion, result shaping, the error payloads (module docstring) |
| `chat/tables/write_request.py` | The `write_table_rows` params shape shared by the row write tools and the action request (module docstring) |
| `chat/action_request_types/create_table.py` / `chat/action_request_types/change_table_schema.py` / `chat/action_request_types/write_table_rows.py` | The three table card handlers: params, preview fields, labels, execute results |
| `chat/action_request_types/table_precard.py` / `chat/action_request_types/table_schema_precard.py` / `chat/action_request_types/table_rows_precard.py` | Their pre-card arms, the injected preview keys and the refusal texts |
| `chat/tables/routes.py` | Tables, schema and rows routes; the module docstring holds the conventions, the row and detail shapes and the error table; the shared route helpers |
| `chat/tables/history_routes.py` / `chat/tables/download_routes.py` / `chat/tables/share_routes.py` | History and restore, download, sharing; each module docstring is its contract |
| `chat/tables/export.py` | The CSV file rules, formula-injection guard included (module docstring) |

## Queries

A query is JSON validated against the table's schema before any I/O: an optional filter tree, sort keys, a column list and a limit and offset. `parse_filter()`, `parse_sort()` and `check_page()` validate; `compile_filter()` and `run_query()` turn the result into SQL. The grammar, the ops each column type allows (`allowed_ops()`, with `OP_OPERANDS` naming the operand each op takes) and the SQL of every op are in the `chat/tables/filters.py` module docstring. A refusal is a `FilterError` whose `code` names the rule (the codes are on the class) and whose `path` (`and[1].not.value`, `sort[0].direction`) points at the offending node, so a tool error or a filter bar can show where.

- **Columns**: a filter, sort or column list names columns by name, exactly or else case-insensitively (names are unique case-insensitively). Names are lookups only: the SQL holds nothing but validated keys and the fixed row fields, and every value is a bound parameter. The row fields `_id`, `_created_at` and `_updated_at` sort but do not filter.
- **Operands** are canonicalized like writes ([Canonical Values](../architecture/quest-tables.md#canonical-values)), so `"10"` and `10` select the same rows, booleans are JSON `true`/`false` only and enum operands must be options. Number operands ignore the column `precision`: precision rounds writes only and never rewrites stored cells, so a rounded operand would miss older values and shift range bounds. Substring operands are plain text, not URLs; for url columns they are not NFC-normalized, matching cells stored as given. A multi enum's `eq` compares the whole canonical set.
- **Empty cells**: `eq`, `in`, the range ops and the substring ops never match an empty cell; `ne` does; `not` is the exact complement of its child, so `not` of any node returns precisely the rows the node does not. An empty multi enum is empty, so `is_empty` is `is_null`.
- **Numbers** compare as numbers for ranges and ordering (approximate past about 15 significant digits) and as canonical strings for equality; past that precision a range op and an equality op can disagree on the same value.
- **Dates** take the range ops of numbers, with every operand a `YYYY-MM-DD` string like a date cell; ranges, `between` and sorting compare the stored string, which is date order for the canonical form, so nothing is cast.
- **Case**: the substring ops fold case for ASCII letters only; `É` does not match `é`.
- **Sort**: nulls last in both directions, binary string order for text, url, enum and date, and always `_id` as the final tiebreaker, so offset paging is stable.
- **Caps**: filter depth and leaf count, values per list operand, sort keys, page size and offset are constants in `chat/tables/constants.py`; a caller may pass a lower page cap to `check_page()`, as the grid does with `TABLE_GRID_MAX_LIMIT`. Depth and leaves are checked while descending, so a hostile input is refused without being walked.
- **Filter deletes**: a filter that selects rows to delete goes through `compile_filter()`, which refuses a missing filter, so a dropped argument cannot match the whole table.

## Results

`run_query()` returns `total` (the filtered count) and a page of `rows`, read in one transaction so the two agree. Rows are keyed by column name, with `_id` and `_updated_at`, every cell in its canonical logical form. The result is not capped by size; a caller that must fit a byte budget (`TABLE_QUERY_MAX_BYTES`) shrinks the page itself. Given the schema version, the query refuses with `schema_mismatch` when the file disagrees, like a write.

## Refusals

Every refusal carries a machine `code` and a `message` safe to show a model; no SQLite text ever reaches one. The classes and their codes:

- **Table file** (`TableStorageError` in `chat/tables/storage.py`): the class docstring lists the plain codes and each subclass carries its own; each write's docstring names the refusals it can raise.
  - `invalid_rows` (`InvalidRowsError`) carries every problem of the call as `{row, column, code, reason}`, ordered by row; within a row, row-level problems come first, then `_id`, then schema columns in order, then unknown names. A `duplicate_value` lands on the row that writes the value. Each write's docstring names its problem codes.
  - `too_many_rows`, `table_full` and `change_too_large` are the caps; `change_too_large` ends with a hint on how to split the write. `unknown_key_column` and `key_not_unique` (`UpsertKeyError`) refuse an upsert key.
  - `rows_violate` (`RowsViolateError`) refuses a schema change that existing rows break, naming the column, the check, a capped list of offending `_id`s and the total ([Schema Changes](../architecture/quest-tables.md#schema-changes)).
  - `schema_mismatch` means `quest.db` and `table.db` disagree on the schema; it is the file-level check, distinct from the store's stale-schema token.
  - `constraint_failed` is a residual SQLite integrity error the Python checks should have caught, with a fixed message.
  - `busy`, `unsafe_path`, `storage_missing`, `storage_exists` and `corrupt_storage` come from the file itself; `no_rows`, `invalid_selector` and `copy_exists` from malformed calls; `unknown_change` (`UnknownChangeError`, a `RevertRefusal`) from a restore.
- **Columns and values**: `ColumnSpecError` refuses a column spec and `SchemaOperationError` (with `op_index`) a schema operation, each with a code per rule (see the handlers); `TableValueError` refuses a cell value, with a `detail` that omits the column so a row problem can report the column separately.
- **Queries**: `FilterError` ([Queries](#queries)).
- **Metadata** (`db/table_store.py`): `DuplicateTableTitleError`, `TableLimitError`, `TableValidationError`, `ShareTargetGoneError`, and the two concurrency tokens, `StaleTableError` (carrying the current row) and `StaleTableSchemaError`.
- **Access** (`chat/tables/access.py`): a denied verdict carries `deny_reason` for rows and `schema_deny_reason` for schema changes, worded to tell the model what to do instead; an approval verdict maps to the `approval_required` message of its request type. A hidden table and a missing one get the same `table_not_found_message()`, so a refusal never reveals that a table exists.

## Model Tools

Models reach tables through seven `TOOL_CALL_REGISTRY` specs, dispatched through `tool_call` to `chat/gemini_api/tool_handlers/tables.py`. The handlers coerce loose arguments (numbers and booleans sent as strings; a filter, sort, column list or row list sent as a JSON string), call `chat/tables/service.py` and shape the result; nothing else reaches a table from a model. The spec descriptions document the parameters and stand alone, since the public prompt carries no skills.

- **Reads**: `list_tables(scope?, limit?)`, `get_table(table_id)`, `query_rows(table_id, filter?, sort?, columns?, limit?, offset?, count_only?)`.
- **Row writes**: `insert_rows(table_id, rows)`, `update_rows(table_id, updates)` (each update an `_id` plus a partial patch), `upsert_rows(table_id, key_column, rows)`, `delete_rows(table_id, row_ids | filter)`, with the semantics of [Row Writes](../architecture/quest-tables.md#row-writes).

### Results

- `list_tables` returns `{tables, count}`, one summary per visible table (`_summary()`): metadata, scope, `row_count`, the column labels, `writable` (the rows verdict) with `write_note`, and `schema_change` (the schema verdict). Scopes are `TABLE_SCOPES` in the service; the limit bounds are `LIST_DEFAULT_LIMIT` / `LIST_MAX_LIMIT`. A table whose stored schema does not load is skipped and logged, so it cannot hide the rest.
- `get_table` returns the detail (`_detail()`): every column definition in order, `schema_version`, both verdicts with their notes, and `sample_rows`, the first `SAMPLE_ROWS` rows by `_id` in the query row shape.
- `query_rows` returns `{rows, total, truncated, offset, limit}` with the rows of [Results](#results), at most `TABLE_QUERY_MAX_ROWS` per page; `count_only` returns `{total}`. A payload over `TABLE_QUERY_MAX_BYTES` is halved until it fits (`_fit_rows()`, the `project_db_query` convention): `truncated` is true and `hint` names the offset to continue from, or asks for fewer `columns` when one row alone is too large.
- A row write returns its op's counts (an upsert adds `inserted_ids` and `updated_ids`), `row_ids`, `change_id` (null only for a filter delete that matched nothing), `row_count` and `updated_at` (`_write_result()`).

A column appears as `Name:type`, a multi enum as `Name:enum[]` (`_column_label()`). A column's key never appears in any tool output: `get_table` drops it, and refusal payloads leave out `column_key`.

### Tool Errors

Every error is `{"error": <code>, "message": <text>}` plus the refusal's structured fields:

- `tables_disabled` comes before anything else, with no DB work, while the gate is closed.
- `table_not_found` is the one answer for a missing and a hidden table, byte-equal, after the same lookups.
- `approval_required` adds `suggested_request: {"request_type": "write_table_rows", "params"}`, which the model forwards unchanged through `create_action_request` ([write_table_rows](#write_table_rows)). The params are the tool's own arguments plus `operation` (shape in the `chat/tables/write_request.py` module docstring), so the round trip to `apply_row_write()` at Approve is lossless. Arguments are shape-checked before the verdict (`invalid_args`, or `too_many_rows` with `count` and `cap` past `TABLE_WRITE_MAX_ROWS`), so an approval result always carries a well-formed card. The write is then dry-run against the live schema and rows, so rows it would refuse get the free path's error (such as `invalid_rows`) instead of a card.
- A `FilterError` adds `path`; storage and schema refusals add their fields (`_ERROR_FIELDS`), such as the `problems` list of `invalid_rows` ([Refusals](#refusals)).
- Every other service refusal carries its code, such as `denied` with the verdict's deny reason or `invalid_args`.

### Tiers

- **Prompt**: every spec has `requires_service: "tables"`, so the tools leave the prompt while the [feature gate](../architecture/feature-gates.md#enforcement-layers-quest-tables) is closed. All seven are documented by the `system:tables` skill (`_SKILL_DOCUMENTED_TOOLS`), whose guide precedes the tool reference the loader appends ([Prompting](../architecture/quest-tables.md#prompting)).
- **Public conversations**: all seven are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private table there; a public table's rows are `free` or `denied`, or `approval` under the owner's switch, and its schema is always `approval`. The three table cards are the action requests a public conversation can open ([Public Projects -- Action requests](../architecture/public-projects.md#action-requests)).
- **Script bridge**: the three reads only (`SCRIPT_TOOL_CALL_ALLOWLIST`), scoped by the sandbox lease to what the launching conversation reads, project tables included ([Service Layer](../architecture/quest-tables.md#service-layer)).
- **Read-only tiers**: sub-agent and cross-user subagent prompts leave out `_table_write_tool_names()` (`chat/gemini_api/system_prompt.py`); the access rule denies the writes there anyway.
- **Inference API runs**: the `mutating` row writes are refused at dispatch and on the run's sandbox lease ([Inference API](inference-api.md)).

## Action Requests

Three request types change a table on Approve: `create_table`, `change_table_schema` and `write_table_rows`. The pipeline is the one of [the write_doc card](../architecture/quest-docs.md#the-write_doc-action-request): `validate_params` checks shape only, a pre-card dry-runs the proposal against the live table and injects the preview, and `execute` re-checks against the table at Approve.

- **Pre-card.** `table_precard_check()` (called from the `create_action_request` arm after the `write_doc` pre-card) dispatches to one arm per type. Each arm checks the gate before any DB work and decides with the proposal's conversation as caller (`run_kind` `top_level`, or `slack` for a Slack run; `is_public` from the conversation's project row, never from the proposal, so a public conversation's card reaches only its own public tables). A refusal is a same-turn `Invalid parameters` with no card; tables-layer errors keep their code and text, so a hidden table stays byte-equal to a missing one. A row write that needs no card is refused (`use_tool_directly`).
- **Approve-time caller.** `execute_caller()` rebuilds the caller from the proposal's conversation and project, never a UI caller, so a card from a private conversation can never write a public table and a share revoked meanwhile hides the table. A refusal raises `RuntimeError` and leaves the request open ([Execution Failure Behavior](../architecture/action-requests.md#execution-failure-behavior)).
- **TOCTOU.** `create_table` refuses `stale_request` unless the columns still parse to the card's `column_specs`, and the store re-checks the title collision under the per-owner lock (`duplicate_title`). `change_table_schema` passes the card's `expected_schema_version` (`stale_schema`), and the change re-runs the `rows_violate` checks under the lock. `write_table_rows` pins the proposal's `schema_version` and `rows_digest`: after its re-fetch, in the same lock hold as the write, `apply_row_write()` checks the version (`stale_schema`), then dry-runs the write and compares the digest of the rows it would touch (`rows_changed`, naming their current values). The digest covers row ids and stored cells, not the schema, hence the version pin beside it.
- **Attribution.** The resolve route passes `request_id` to these types (`REQUEST_ID_TYPES` in `chat/action_request_routes.py`). The change's source and the table's `last_write_source` are `action_request:<id>` (`write_source()`), the actor the approver; a card is approved in its own conversation, so that is the proposer, whom `get_action_request_owners()` returns for the id.

Common to the three:

- **Params** are strict: an unknown key is refused, and a model-supplied copy of a server-injected key (each handler's `SERVER_INJECTED_KEYS`) is dropped by `validate_params` before the pre-card writes its own.
- **Refusal text** is `<code>: <message>`. At proposal the model gets `{"error": "Invalid parameters: <code>: <message>"}` and no card opens; at Approve the resolve route answers 500 `execution_failed` with `Failed to execute request: <code>: <message>` and the request stays open. Codes from the tables layers keep their own text ([Refusals](#refusals)), so a hidden table gets the byte-equal `table_not_found`. Every type refuses `tables_disabled` first (gate closed, no DB work), then `project_not_found` when the conversation's project row is gone or not the user's, at proposal and at Approve alike. The shape checks of `create_table` and `change_table_schema` `validate_params` are plain text without a code; those of `write_table_rows` are `invalid_args: ...`.
- **Table fields**: a card about an existing table gets the injected `table_title`, `table_mode`, `table_scope` (`"user"` / `"project"`), `share_summary` (counts only, such as `shared with 2 users`) and `require_approval`, shown as the Table, Mode, Scope, Shares and Approval rows, each only when present (`common_preview_fields()`).
- **Structured fields**: a preview field with a `type` carries its payload under a second key beside the plain `value`; a renderer that does not know the type shows `value`, and the card UI is in [Quest Tables Frontend -- Card Previews](../architecture/quest-tables-frontend.md#card-previews). No payload carries a column key.

### create_table

- **Params**: `title` (required, non-empty), `description` (default `""`), `target` (`"user"`, the default, or `"project"`, which needs a project conversation; a public conversation must pass `"project"`, since user tables are always private), `columns` (required, at least one and at most `TABLE_MAX_COLUMNS` column specs as `parse_column_spec()` takes them; a `key` is refused).
- **Injected**: `table_mode`, `project_title` (null for a user table) and `column_specs`, the parsed columns without keys. `title` and `description` are stored stripped.
- **Preview**: Title, Description (when set), Mode, Scope (the target), Project (project target only) and Columns, of type `table_columns`: `{"key": "Columns", "value": "2 columns: Name (text), Status (enum)", "type": "table_columns", "columns": [...]}`. Each entry is `Column.to_dict()` without `key`, every field present: `{"name": "Status", "type": "enum", "required": false, "unique": false, "default": "Open", "options": ["Open", "Done"], "multiple": false, "precision": null, "description": ""}`.
- **Labels**: display name `Create Table`, approve `Create`, resolved `Created`; the snippet is `<title> (<N> columns)`.
- **Refused at proposal**, in order: `tables_disabled`, `project_not_found`, `denied` (a Slack run), `invalid_args` (project target outside a project conversation, or user target from a public conversation), `project_not_found` (the project is not the user's), `mode_mismatch` (fail-closed: the table's mode, the project's flag or private for a user table, must be the conversation's), `invalid_request` (title or description), the `ColumnSpecError` code with the column named, `duplicate_title`, `table_limit`.
- **Execute** returns `{table_id, title, mode, scope, project_id, columns: [{name, type}], schema_version}`. Refusals: `invalid_request` (malformed params), `invalid_args`, `tables_disabled`, `project_not_found`, `denied`, `stale_request` (the columns no longer parse to the card's `column_specs`), and the create's own codes, such as `duplicate_title` for a same-title table created while the card was open, or `table_limit`.

### change_table_schema

- **Params**, all required: `table_id`, `operations` (at least one and at most `TABLE_SCHEMA_MAX_OPERATIONS` name-based operations of `chat/tables/schema_ops.py`, applied in order as one change) and `expected_schema_version` (a positive integer, the `schema_version` `get_table` reported).
- **Injected**: the table fields, `schema_diff`, `operations_count`, `rows_backfilled`, `dropped_values` (column name to the number of non-null cells dropped) and `operation_summaries` (one line per operation, such as `Renamed column State to Status`).
- **Preview**: the table rows; Changes, of type `table_schema_diff`, whose `value` is `<N> operations: <summary>; <summary>`; Cells rewritten (when `cells_rewritten_total` is above 0); Dropped values (`Notes: 3 values`, when any).
- **`schema_diff`** is `schema_ops.schema_diff()` (its docstring defines every field) with keys removed and counts added: each entry gets `cells_rewritten` (the distinct cells of that column whose final value differs, so a chained or round-trip option rename counts a cell once or not at all; 0 when none) and `options_renamed` (that column's `rename_enum_option` rewrites in order), and the top level gets `cells_rewritten_total`. A renamed column whose option `Done` became `Closed`: `{"columns": [{"status": "renamed", "moved": false, "before": {"name": "State", ...}, "after": {"name": "Status", ...}, "changes": {"name": {"before": "State", "after": "Status"}, "options": {"before": ["Open", "Done"], "after": ["Open", "Closed"]}}, "options_added": ["Closed"], "options_removed": ["Done"], "cells_rewritten": 4, "options_renamed": [{"option": "Done", "new_option": "Closed"}]}], "order_changed": false, "before_order": ["State"], "after_order": ["Status"], "changed": true, "cells_rewritten_total": 4}`. `before` and `after` are full key-less column dicts, null for an added or dropped column.
- **Labels**: `Change Table Schema`, approve `Apply`, resolved `Schema changed`; the snippet is the first operation summary, with ` and <N> more` when there are several, else the table title.
- **Refused at proposal**, in order: `tables_disabled`, `project_not_found`, `table_not_found`, `denied` (the schema deny reason), `stale_schema` (the message names the current `schema_version` and lists the current columns as compact key-less JSON specs, default-valued fields left out and descriptions clipped, so the model can re-propose against them), each operation's `SchemaOperationError` code (the message starts `Operation <n> (<op>):`), `invalid_request` or `duplicate_title` for a `rename_table`, then the dry run's `rows_violate`, `change_too_large`, `schema_mismatch` or `busy`.
- **Execute** returns `{table_id, title, schema_version, change_id, columns: [{name, type}]}`. Refusals: `invalid_request` (malformed params), `stale_schema` (the columns changed after the proposal; nothing was changed), and the codes above as the live table now gives them.

### write_table_rows

- **Params**: the shape in the `chat/tables/write_request.py` module docstring, `table_id`, `operation` (`insert`, `update`, `upsert` or `delete`) and that operation's keys, exactly what a row write tool returns as `suggested_request.params` ([Tool Errors](#tool-errors)).
- **Injected**: the table fields, `approval_reason` (`"shared"` / `"required_by_owner"`), the pins `schema_version` and `rows_digest`, `row_counts` `{inserted, updated, deleted}`, `upsert_split` `{insert, update}` (upsert only) and `rows_diff`.
- **Preview**: the table rows; Operation (`Insert`, `Update`, `Upsert on <key_column>`, `Delete`); Rows, of type `table_rows_diff`, whose `value` is `<Op> <N> rows`, plus ` (<M> inserted, <K> updated)` for an upsert; Filter, compact JSON clipped to `FILTER_TEXT_MAX_CHARS`, for a filter delete.
- **`rows_diff`** is `{columns, rows, total, truncated}`: `columns` the names in schema order, `rows` at most `ROWS_DIFF_MAX_ROWS` entries of `{_id, status, before, after, changed_columns}`, `total` the full count and `truncated` true past the cap. Values are logical, keyed by column name, with no timestamps. An insert gives `added` rows (`_id` and `before` null, `after` with defaults applied); an update `changed` rows with both sides and `changed_columns`, the names whose value differs; an upsert `changed` for matched rows and `added` for new ones; a delete `deleted` rows (`after` null). An upsert matching one row: `{"columns": ["Name", "Status"], "rows": [{"_id": 7, "status": "changed", "before": {"Name": "Ada", "Status": "Open"}, "after": {"Name": "Ada", "Status": "Done"}, "changed_columns": ["Status"]}, {"_id": null, "status": "added", "before": null, "after": {"Name": "Lin", "Status": "Open"}, "changed_columns": []}], "total": 2, "truncated": false}`.
- **Labels**: `Write Table Rows`, approve `Write`, resolved `Rows written`; the snippet is `<Op> <N> rows in <title>`.
- **Refused at proposal**, in order: `tables_disabled`, `project_not_found`, the shape's `invalid_args` or `too_many_rows`, then the dry run's own codes (`table_not_found`, `denied`, `invalid_rows` with every problem listed as row, column and reason, `too_many_rows`, `table_full`, `change_too_large`, a filter code, `unknown_key_column` / `key_not_unique`), then `use_tool_directly` (the table is directly writable; call the row tool) and `no_rows` (a filter delete matching nothing).
- **Execute** returns `{table_id, title, operation, inserted, updated, deleted, row_ids, change_id, row_count}`, `title` as the card showed it. Refusals: `stale_request` (the card has no pins), `stale_schema`, `rows_changed` (the message names the current values of the rows that changed, up to `ROWS_CHANGED_MESSAGE_MAX`, such as `Row 7 now: {"Status": "Done"}` or `Row 9 no longer exists`; nothing was written), and the live table's own refusals, such as an `invalid_rows` (every problem listed, as at proposal) for an option removed meanwhile.

## HTTP Routes

The Quest UI surface: four routers under `/app/api`, mounted in `quest.py`. The module docstrings of `chat/tables/routes.py`, `chat/tables/history_routes.py`, `chat/tables/download_routes.py` and `chat/tables/share_routes.py` are the full contract: bodies, query parameters, response fields and the order of checks. Every handler calls `chat/tables/service.py` with `service.caller_for_ui()`. Models never reach these routes: tool calls are limited to `/api/` paths ([Route Dispatch](../architecture/route-dispatch.md)).

### Conventions

- **Auth and gate**: `get_current_user_cookie_or_apikey_checked`; each handler's first check is 403 `tables_disabled`, before any DB work. FastAPI's 422 for an ill-typed body or query parameter, such as a non-boolean `require_approval`, comes before it.
- **Visibility**: every by-id route resolves the table with the `ui` verdict (`_get_table_for_ui()`); a missing, hidden or malformed id gets one byte-equal 404 `table_not_found`.
- **Owner only** (403 `forbidden`, before the body is read): the metadata edit, the schema edit, delete and both share routes.
- **Editors only** (the `ui` rows verdict `free`: the owner or a write share): the three history routes and a download with history. A reader gets 403 `forbidden` right after visibility, before the query string or body is read. Every reader may query rows and download without history.
- **Errors** are `{"detail": {"error", "message", ...}}`. `_ERROR_STATUS` in `chat/tables/routes.py` maps each code to its status (anything absent is 400), and the module docstring lists the structured extras per code, such as `problems` for `invalid_rows` or the current `schema` for `stale_schema`. The one flat body is the 409 `stale_update` of a metadata edit, `{"error", "message", "current": <list row>}` (`_stale_conflict()`), as in the [docs routes](quest-docs-api.md#response-shapes). A 409 `schema_mismatch` succeeds on reload and retry, since the service heals a lost schema bump.

### Shapes

- **List row** (`_ui_row()` / `_ui_rows()`, one batched user lookup per response; fields under "Row shape" in the `routes.py` docstring): the store's table dict without `schema` and `shares`, plus scope and sharing fields, the owner-only `shares` roster and `last_write_user` (`last_write_source` blanked for non-owners, as for docs), and `access`, the flags the UI may offer, derived from the two `ui` verdicts.
- **Detail** (`_ui_detail()`): the row plus the full stored `schema`, column keys included. The UI addresses columns by key, so a rename never breaks an open grid; the model tools never see a key ([Model Tools -- Results](#results-1)).
- **Change entry and detail**: defined in the `history_routes.py` docstring. `source` is raw for the owner only and `source_label` is phrased for the caller, so a share recipient never learns the owner's conversation titles. The detail turns image cells into logical values, bounds the images by `TABLE_CHANGE_DETAIL_MAX_ROWS` and `TABLE_CHANGE_DETAIL_MAX_BYTES` with `truncated` flags, and never sends the internal replay data.

### Endpoints

- **GET `/tables`** (`list_ui_tables`): the three keyset-paged streams of `GET /docs`: the caller's own user tables, one owned project's tables (`project_id`) and tables shared with the caller (`shared=true`).
- **POST `/tables`** (`create_ui_table`, 201): returns the detail; change #1 carries the source `ui:<user_id>`.
- **GET `/tables/{id}`** (`get_ui_table`): the detail plus `last_write_conversation`, the docs helper.
- **PUT `/tables/{id}`** (`update_ui_table`, owner): title, description and `require_approval`, with an optional `expected_updated_at` (`service.update_metadata()`). It writes no change-log entry, and `updated_at` and `table_changed` move only when a field changes.
- **PUT `/tables/{id}/schema`** (`change_ui_table_schema`, owner): name-based operations and `expected_schema_version`, as in [change_table_schema](#change_table_schema); returns the detail plus `change_id`.
- **GET `/tables/{id}/rows`** (`query_ui_table_rows`): [Queries](#queries) with `filter` and `sort` as JSON query parameters, so filters name columns by name, but the rows come back keyed by column **key**, with `_id` and `_updated_at`. A `limit` above `TABLE_GRID_MAX_LIMIT` is 400 `invalid_limit`, never clamped; rows are never cut for size, so `truncated` only means more rows match past the page. `schema_version` comes back so the grid can tell its schema went stale.
- **DELETE `/tables/{id}`** (`delete_ui_table`, owner, 204).
- **GET `/tables/{id}/changes`** (`list_table_changes`, editors): newest first, paged by `before_id`, each entry flagged `in_effect` ([Restore](../architecture/quest-tables.md#restore)).
- **GET `/tables/{id}/changes/{change_id}`** (`get_table_change`, editors): the entry plus the detail; a malformed or missing id is 404 `unknown_change`.
- **POST `/tables/{id}/restore`** (`restore_table`, editors): `service.apply_restore()` to `change_id`, source `revert:<change_id>`; a write share may restore schema changes too ([Access](../architecture/quest-tables.md#access)). Returns the new restore entry and the detail.
- **GET `/tables/{id}/download`** (`download_table`): `format` `csv` (default) or `sqlite`, else 400 `invalid_format`; `include_history=true` is editors only and SQLite only (400 `invalid_request` with CSV). An attachment named after the title (`_download_filename()`). SQLite is the [download copy](../architecture/quest-tables.md#download-copy). The CSV rules, including the leading `'` that defuses spreadsheet formulas in text, url and enum cells and header names, are in the `chat/tables/export.py` docstring. The temp directory is removed on every path (`_TempDirFileResponse`).
- **POST `/tables/{id}/shares`** and **DELETE `/tables/{id}/shares/{share_id}`** (`add_table_share`, `remove_table_share`, owner): the docs [Sharing](quest-docs-api.md#sharing) routes for tables, same codes, an upsert per recipient; both return the owner's list row. There is no GET: the roster is on the owner's detail.

### Events

The routes publish nothing themselves; the service sends the [realtime events](../architecture/quest-tables.md#realtime-events) after each landed change. A share change bumps no `updated_at`, so it sends `table_changed` with a null `updated_at` to the old and new audiences together, closing a removed recipient's viewer; re-sharing with the same permission publishes nothing.

### SPA Routes

`GET /tables` and `GET /tables/{rest:path}`, outside `/app/api`, are `serve_spa_tables` in `quest.py`: they return `index.html` so the deep links `/tables`, `/tables?project=<id>` and `/tables/<id>` survive a reload.

## Design Decisions

**Why does `ne` match empty cells, and `not` take the complement?**
SQL's three-valued logic would drop NULL rows from both, so "status is not done" would silently skip rows with no status and `not` of a filter would not return the rest of the table. Matching what a person means keeps a filter and its negation partitioning the rows.

**Why `CAST AS REAL` for ranges and sorting but strings for equality?**
Numbers are stored as canonical decimal strings, whose text order is not numeric order; a REAL cast orders them correctly up to about 15 significant digits without a shadow column, while equality keeps the exact string comparison that uniqueness uses.

**Why are rows keyed by column key in the HTTP routes but by name in the tools?**
The grid holds rows across edits; keys survive a rename, so an open page stays valid while the schema changes under it. Models address columns by name and never see keys, so they cannot forge or collide them.
