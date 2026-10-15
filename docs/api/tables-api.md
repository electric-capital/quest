# Quest Tables API

## Overview

The caller-facing contract of [Quest Tables](../architecture/quest-tables.md): the query language a caller passes to read or select rows (filter, sort, column list, paging), the result it gets back, the refusals it can receive, each a machine `code` plus a message written for a model or a person, and the model tools built on them. Storage, schema operations, the change log, restore and the access matrix are architecture and live in the architecture doc.

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

## Queries

A query is JSON validated against the table's schema before any I/O: an optional filter tree, sort keys, a column list and a limit and offset. `parse_filter()`, `parse_sort()` and `check_page()` validate; `compile_filter()` and `run_query()` turn the result into SQL. The grammar, the ops each column type allows (`allowed_ops()`, with `OP_OPERANDS` naming the operand each op takes) and the SQL of every op are in the `chat/tables/filters.py` module docstring. A refusal is a `FilterError` whose `code` names the rule (the codes are on the class) and whose `path` (`and[1].not.value`, `sort[0].direction`) points at the offending node, so a tool error or a filter bar can show where.

- **Columns**: a filter, sort or column list names columns by name, exactly or else case-insensitively (names are unique case-insensitively). Names are lookups only: the SQL holds nothing but validated keys and the fixed row fields, and every value is a bound parameter. The row fields `_id`, `_created_at` and `_updated_at` sort but do not filter.
- **Operands** are canonicalized like writes ([Canonical Values](../architecture/quest-tables.md#canonical-values)), so `"10"` and `10` select the same rows, booleans are JSON `true`/`false` only and enum operands must be options. Number operands ignore the column `precision`: precision rounds writes only and never rewrites stored cells, so a rounded operand would miss older values and shift range bounds. Substring operands are plain text, not URLs; for url columns they are not NFC-normalized, matching cells stored as given. A multi enum's `eq` compares the whole canonical set.
- **Empty cells**: `eq`, `in`, the range ops and the substring ops never match an empty cell; `ne` does; `not` is the exact complement of its child, so `not` of any node returns precisely the rows the node does not. An empty multi enum is empty, so `is_empty` is `is_null`.
- **Numbers** compare as numbers for ranges and ordering (approximate past about 15 significant digits) and as canonical strings for equality; past that precision a range op and an equality op can disagree on the same value.
- **Case**: the substring ops fold case for ASCII letters only; `É` does not match `é`.
- **Sort**: nulls last in both directions, binary string order for text, url and enum, and always `_id` as the final tiebreaker, so offset paging is stable.
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
- `approval_required` adds `suggested_request: {"request_type": "write_table_rows", "params"}`, which the model forwards unchanged through `create_action_request`. The params are the tool's own arguments plus `operation` (shape in the `chat/tables/write_request.py` module docstring), so the round trip to `apply_row_write()` at Approve is lossless. Arguments are shape-checked before the verdict (`invalid_args`, or `too_many_rows` with `count` and `cap` past `TABLE_WRITE_MAX_ROWS`), so an approval result always carries a well-formed card.
- A `FilterError` adds `path`; storage and schema refusals add their fields (`_ERROR_FIELDS`), such as the `problems` list of `invalid_rows` ([Refusals](#refusals)).
- Every other service refusal carries its code, such as `denied` with the verdict's deny reason or `invalid_args`.

### Tiers

- **Prompt**: every spec has `requires_service: "tables"`, so the tools leave the prompt while the [feature gate](../architecture/feature-gates.md#enforcement-layers-quest-tables) is closed. All seven are documented by the `system:tables` skill (`_SKILL_DOCUMENTED_TOOLS`), whose content is a short placeholder ahead of the tool reference the loader appends.
- **Public conversations**: all seven are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private table there and gives a public table only `free` or `denied`, so no card is needed.
- **Script bridge**: the three reads only (`SCRIPT_TOOL_CALL_ALLOWLIST`), with a caller that has no project, so scripts see user tables only ([Service Layer](../architecture/quest-tables.md#service-layer)).
- **Read-only tiers**: sub-agent and cross-user subagent prompts leave out `_table_write_tool_names()` (`chat/gemini_api/system_prompt.py`); the access rule denies the writes there anyway.
- **Inference API runs**: the `mutating` row writes are refused at dispatch and on the run's sandbox lease ([Inference API](inference-api.md)).

## Design Decisions

**Why does `ne` match empty cells, and `not` take the complement?**
SQL's three-valued logic would drop NULL rows from both, so "status is not done" would silently skip rows with no status and `not` of a filter would not return the rest of the table. Matching what a person means keeps a filter and its negation partitioning the rows.

**Why `CAST AS REAL` for ranges and sorting but strings for equality?**
Numbers are stored as canonical decimal strings, whose text order is not numeric order; a REAL cast orders them correctly up to about 15 significant digits without a shadow column, while equality keeps the exact string comparison that uniqueness uses.
