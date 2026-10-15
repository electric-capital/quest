# Quest Tables API

## Overview

The caller-facing contract of [Quest Tables](../architecture/quest-tables.md): the query language a caller passes to read or select rows (filter, sort, column list, paging), the result it gets back, and the refusals it can receive, each a machine `code` plus a message written for a model or a person. Storage, schema operations, the change log, restore and the access matrix are architecture and live in the architecture doc.

## Key Files

| File | Description |
|------|-------------|
| `chat/tables/filters.py` | The query language: grammar, ops per column type and the SQL each op compiles to (module docstring), `FilterError`, `run_query()` |
| `chat/tables/constants.py` | The filter, sort and paging caps (`TABLE_FILTER_*`, `TABLE_SORT_MAX_KEYS`, `TABLE_QUERY_*`, `TABLE_GRID_MAX_LIMIT`) |
| `chat/tables/storage.py` | `TableStorageError` and its subclasses, the table file refusals |
| `chat/tables/schema.py` / `chat/tables/schema_ops.py` | `ColumnSpecError`, `TableValueError`, `SchemaOperationError` |
| `chat/tables/access.py` | Deny reasons, notes, `approval_required` and not-found messages |
| `db/table_store.py` | The metadata refusals (title, cap, concurrency tokens, shares) |

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

## Design Decisions

**Why does `ne` match empty cells, and `not` take the complement?**
SQL's three-valued logic would drop NULL rows from both, so "status is not done" would silently skip rows with no status and `not` of a filter would not return the rest of the table. Matching what a person means keeps a filter and its negation partitioning the rows.

**Why `CAST AS REAL` for ranges and sorting but strings for equality?**
Numbers are stored as canonical decimal strings, whose text order is not numeric order; a REAL cast orders them correctly up to about 15 significant digits without a shadow column, while equality keeps the exact string comparison that uniqueness uses.
