# Analytics Store

The DuckDB file `analytics.duckdb` beside `quest.db` holds the append-only **event ledgers**: one row per time something happened, written from the conversation loop and read only by grouped admin reports. This doc owns the store itself (opening, migrations, the write path, backfills, the operational rules) and the ledgers in it; the reports built on them are in [Admin System Reports](admin-system-monitor.md).

## Overview

`quest.db` ([Database](database.md)) is the transactional store: rows that are read back to decide behaviour, updated in place, and cascade-deleted with their owners. Ledgers are different: nothing is ever updated, rows outlive the conversation, user or skill they name, and the only readers aggregate millions of rows by date. Keeping them in their own file means their writes never contend for the SQLite writer lock, `quest.db` and its backups stay small, and date-range aggregations stay fast as the ledgers grow. DuckDB was chosen over a second SQLite file because it is a columnar engine built for exactly those reads, and because its JSON reader turns a transcript backfill into a query (see [Design Decisions](#design-decisions)).

The ledgers are `skill_loads` (issue #172) and `tool_calls` (issue #171). The per-call token analytics (`llm_calls_*`, today in `quest.db`) are meant to follow the same pattern.

## Key Files

| File | Description |
|------|-------------|
| `analytics/store.py` | `AnalyticsStore` (one open database, per-thread cursors), the migration runner `apply_migrations()` / `list_migrations()`, the process-wide `open_store()` / `get_store()` / `close_store()` |
| `analytics/migrations/NNNN_name.sql` | Numbered, forward-only schema migrations; each file's header comment says what it adds and why |
| `analytics/ledger.py` | `Ledger` (the queue and writer thread), the discriminator tuples and column tuples of each ledger (`SKILL_LOAD_SOURCES` / `SKILL_LOADS_COLUMNS`, `TOOL_CALL_TYPES` / `TOOL_CALL_SOURCES` / `TOOL_CALLS_COLUMNS`), `effective_tool_name()`, the best-effort module-level `record_skill_load()` / `record_tool_call()` every recording site calls |
| `analytics/reports.py` | `query_skill_usage()` / `query_tool_calls()` (the grouped ledger queries), `query_backfill()`, `build_skills_report()` / `build_tool_calls_report()` (the pure folds; row contracts in their docstrings) |
| `analytics/backfill.py` | `backfill_skill_reads()` and `backfill_tool_calls()`, their lifespan tasks behind `run_backfills()`, the `_backfills` marker helpers |
| `config/paths.py` | `ANALYTICS_DB_PATH` |
| `quest.py` | `lifespan`: open the store, start the ledger, launch the backfill task; stop and close on shutdown |
| `chat/gemini_api/conversation.py`, `chat/gemini_api/tool_handlers/skills.py`, `chat/action_request_types/create_skill.py` | The `skill_loads` recording sites (see [skill_loads](#skill_loads)) |
| `chat/gemini_api/conversation.py`, `chat/gemini_api/sub_agent.py`, `chat/gemini_api/script_tool_call.py` | The `tool_calls` recording sites (see [tool_calls](#tool_calls)); `chat/storage.py` `ChatStorage.list_transcript_tool_calls()` feeds its backfill |
| `tests/test_analytics_store.py`, `tests/test_skill_loads_ledger.py`, `tests/test_tool_calls_ledger.py`, `tests/test_admin_skills_report.py`, `tests/test_admin_tool_calls_report.py` | Migration runner and schema contracts, write paths and backfills, queries and reports |

## Lifecycle and Concurrency

A DuckDB file has at most one read-write opener, so the server process owns it for its whole life: `lifespan` opens it (applying pending migrations before anything writes), starts the ledger writer thread and launches the backfill task; shutdown cancels the backfill, stops the writer after a final flush and closes the file, which folds the write-ahead log back into it. Nothing else may open the file while the server runs -- a shell, a script or a second checkout pointed at the same data directory fails with a lock error, and so would the server if one of those held it first.

The `duckdb` module is a blocking C++ library and its connections are not thread-safe, so no store call runs on the event loop: report handlers take a cursor inside `asyncio.to_thread`, the writer thread keeps one cursor for its lifetime, and each cursor is a duplicate connection on the same database (`AnalyticsStore.cursor()`), closed when done.

A failed open is logged and the server boots without the store: every `record_*` call drops its row, and the Skills report answers 503 `analytics_unavailable`. Nothing that gates behaviour depends on the store being open.

## Write Path

The `record_*` helpers in `analytics/ledger.py` (`record_skill_load()`, `record_tool_call()`) are synchronous and never block: it validates, stamps naive-UTC time and appends rows to an in-memory queue, so the system-prompt build and the tool handlers can call it inline. The writer thread drains the queue on an interval (sooner once a batch's worth is waiting) and inserts each table's rows in one transaction; a batch that fails to insert is dropped and logged rather than retried, so a poison row cannot wedge the writer, and the queue is capped so a dead writer cannot grow memory without bound.

The cost of that design is a loss window: a hard crash loses whatever was queued since the last flush. That is acceptable for analytics and is the reason the ledger must never be the source of truth for anything the application decides -- the `edit_skill` read gate stays on the per-conversation `skill_reads.json` sidecar ([Skill Library](skill-library.md#creating-and-editing-skills-via-action-requests)), even though the ledger records the same events.

Without an installed ledger (tests, out-of-process scripts, the degraded boot above) the module-level helper is inert.

## Ledgers

Every ledger follows the same shape: a `created_at TIMESTAMP` in UTC, the ids of what the event concerns as plain values with no foreign key (rows outlive what they name), string discriminators, and **no primary key or index** -- rows arrive in time order, and DuckDB answers the reports' date-range filters from its per-block min/max statistics. Columns the recording site may not know are nullable, and NULL means "not captured", the convention the `llm_calls_*` tables already use.

### skill_loads

One row per skill per time its body was put in front of the model (migration `0002_skill_loads.sql` has the columns). `source` says how it got there -- the values and their meaning are `SKILL_LOAD_SOURCES` in `analytics/ledger.py` -- and the recording sites are every path that also writes the read-gate sidecar:

- The system-prompt build in `chat/gemini_api/conversation.py`: one row per skill per tier (user, project and routine auto-loads, or the caller-specified skills of a cross-user subagent run) on **every** build, so a conversation with an auto-loaded skill contributes a row per turn; plus the composer-loaded `skill_ids` of the send. These rows carry `project_id` and `routine_id`.
- `_mark_skills_read()` in `chat/gemini_api/tool_handlers/skills.py` for `load_skills` (DB and `system:*` skills alike; failed system-skill gates are not loads) and `get_skill`.
- `create_skill` on approval: the model authored the content.

Granularity is per event, not per conversation: the report derives distinct conversations and users itself, and the per-turn rows are what make "how often is this skill actually in context" answerable later.

### tool_calls

One row per tool call, recorded **when the call is dispatched**, not when its result lands (migration `0003_tool_calls.sql` has the columns): a call that suspends the conversation on an action request or a connect request still counts, and the ledger stays append-only. Only names and ids are stored -- never arguments or outputs. `tool_name` is the *effective* tool: a call through the `tool_call` meta-tool is recorded under the inner tool it routed to with `dynamic = true` (`effective_tool_name()`), a direct tool under its own name. `call_type` says who issued it (`TOOL_CALL_TYPES`), `source` whether the row was recorded live or imported (`TOOL_CALL_SOURCES`); `tool_id` is the provider tool-use id, so a row joins back to its `tool_result` message or wait handle. The recording sites:

- The function-call loop in `chat/gemini_api/conversation.py`, right after the durable `tool_use` message: `top_level`, with `project_id` and `routine_id`.
- The per-call loop in `chat/gemini_api/sub_agent.py`, before it branches: `sub_agent`, with `agent_name`, `sub_agent_level` (1 or 2) and the `parent_tool_id` of the `agent_task*` call that spawned the agent, so malformed, finishing (`agent_task_response`), nested-spawn and dispatched calls all count. No `routine_id` (the report attributes routines from the conversation row).
- The sandbox tool API bridge in `chat/gemini_api/script_tool_call.py`: `script`, always `dynamic`, attributed to the launching conversation and project the lease carries (so `conversation_id` can be NULL), no `tool_id`.

Status and duration are deliberately absent: filling them in when the result lands would mean updating rows, and a suspended call may have no result for hours. Join `tool_id` to the transcript when one call's outcome matters.

## Schema Changes

Migrations are the SQL files in `analytics/migrations/`, named `NNNN_name.sql` with contiguous versions from 1. `apply_migrations()` runs at every open: it creates the `_migrations` table if needed, applies each unrecorded file in version order inside its own transaction (DuckDB DDL is transactional, so a failing file leaves nothing half-applied and stops the run) and records it. There are no down migrations; recovery from a bad one is restoring the file. Alembic is not involved (see [Design Decisions](#design-decisions)).

Rules for writing one:

- **Additive change**: `ALTER TABLE ... ADD COLUMN` a nullable column. Older rows read NULL ("not captured"). A rename is `ALTER TABLE ... RENAME COLUMN`.
- **Anything else** (type change, dropped column, constraint): rebuild -- `CREATE TABLE x__new AS SELECT ... FROM x`, drop, rename, in the one transaction the runner already provides. Ledgers are small enough that this takes well under a second, and it sidesteps every DuckDB `ALTER` restriction.
- **New discriminator value**: no migration; add it to the tuple in `analytics/ledger.py`. Never a DuckDB `ENUM`.
- **Keep the ledger module in step**: `tests/test_analytics_store.py` applies the migrations to a fresh database and asserts the table's columns equal `SKILL_LOADS_COLUMNS`, so a column change without the matching code change (or the reverse) fails CI rather than the first insert.
- **Data imports are not migrations**: anything that reads the filesystem or takes minutes is a [backfill](#backfills).

## Backfills

A backfill imports history that predates a ledger. It runs as a lifespan background task after the store is open, in a worker thread, and writes its rows and its `_backfills` marker row in a single transaction, so a crash mid-way leaves nothing and the next boot runs it again; a present marker means never again. Reports show partial data until it finishes and carry the marker's status.

`run_backfills()` is the lifespan task; it runs each import in turn, each guarded so one failure does not block the next:

- `run_skill_reads_backfill()` imports every surviving conversation's `skill_reads.json` (`ChatStorage.get_skill_read_ids`). The sidecar is a set with no timestamps or provenance, so each id becomes one row with `source = 'backfill'` stamped at the conversation's `created_at`, with its `project_id` and `routine_id` from the `conversations` row; conversations deleted before the import are not recoverable.
- `run_tool_calls_backfill()` imports every surviving conversation's transcript tool calls (`ChatStorage.list_transcript_tool_calls`): each top-level `tool_use` message, stamped at its own timestamp, and each `sub_agent_tool_use` event the loop persisted on an `agent_task*` `tool_result` under `sub_agent_tool_calls`, stamped at that result's timestamp (the events carry none of their own). Calls stamped at or after the moment the live ledger started are skipped, so a conversation active across the deploy is not counted twice. Script calls were never persisted and are not recoverable.

## Reports

A report is a grouped query over one ledger (`query_*` in `analytics/reports.py`, run on a cursor in a worker thread) plus a pure fold (`build_*_report`) that joins the labels the handler resolved from `quest.db` -- skill names and kinds via `list_skill_labels()` in `db/skill_store.py` and the system catalog, routine conversations via `list_conversation_activity_rows()` -- in Python. The Skills and Tool Calls sections of [Admin System Reports](admin-system-monitor.md) are the two today. `quest.db` is not attached from DuckDB: the sqlite extension is a runtime download, encrypted columns are ciphertext anyway, and it would couple two engines on the request path. No report returns content, only ids, names and counts.

## Operational Constraints

- **No out-of-process access while the server runs.** Ad hoc queries wait for a stop, or go through a report. A scheduled Parquet export is the intended way to open that up; it does not exist yet.
- **Backups copy the file (and its `.wal`) with the server stopped**, like `quest.db`.
- **The `duckdb` version is pinned** in `pyproject.toml`. A newer library reads an older file; the reverse is not guaranteed, so a version bump is a one-way deploy step. Because the server is the only opener, two versions never meet on one file.
- **Not everything is rebuildable.** `skill_loads` rows can be re-derived from transcripts and sidecars and most `tool_calls` rows from transcripts, but script calls and anything a future ledger records at a point no file captures exist nowhere else. Migrations stay non-destructive.

## Design Decisions

**Why DuckDB rather than tables in `quest.db`?**
Ledger inserts happen on every turn; in `quest.db` they would queue behind the single SQLite writer with every user-facing write, and the admin reports' full scans would run against the production file. A separate file fixes both whichever engine it uses; DuckDB adds columnar scans and aggregation that stay fast at tens of millions of rows, and a JSON reader that makes transcript backfills a query. The price is a second schema mechanism and the single-opener rule above.

**Why a sixty-line migration runner instead of Alembic?**
Alembic's DuckDB support is a third-party dialect, autogenerate does not know DuckDB's `ALTER` limits, and ledgers change rarely and additively. Numbered SQL files applied in transactions are what DuckDB projects conventionally do, and the schema-contract test replaces autogenerate's drift detection.

**Why no primary keys or indexes?**
DuckDB's ART indexes make inserts dearer and restrict later `ALTER TABLE`s, and nothing looks a ledger row up by key. Time-ordered inserts plus block statistics already prune the date-range scans the reports run.

**Why batch writes on a thread instead of awaiting each insert?**
DuckDB has no asyncio driver, and the recording sites include the synchronous system-prompt build. A queue plus one writer thread keeps every caller non-blocking, gives natural batching, and isolates the event loop from the C++ library entirely. The loss window it introduces is why the ledger never gates behaviour.

**Why keep `skill_reads.json` now that the ledger records the same events?**
The sidecar is the `edit_skill` read gate: it must be durable the instant it is written and readable without the store. The ledger is neither by design.
