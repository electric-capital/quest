# Project Database Architecture

This document describes the per-project SQLite database, a deprecated feature that gave the LLM a dedicated database for storing and querying structured data within a project. Its successor is [Quest Tables](quest-tables.md).

## Overview

A project's database persists across all conversations in the project. The LLM accesses it via the `project_db_query` dynamic tool, which accepts arbitrary SQL queries. The database lives at `data/projects/{project_id}/project.db`, alongside (but separate from) the project workspace directory. The tool is only available in project conversations -- standalone conversations do not have access.

The feature is retired for every project that does not already hold data in it (see [Deprecation Gate](#deprecation-gate)): such a project has no `project_db_query` tool, no `system:project_db` skill, no "Project Database" prompt paragraph and no Tables card, and the handler refuses, so no project can start a new database. A project whose database already holds a table keeps the whole feature, new tables in that database included.

## Key Files

| File | Role |
|------|------|
| `chat/project_db.py` | The deprecation gate: `project_has_legacy_db()`, the `project_db` connected-services pseudo-key (`PROJECT_DB_SERVICE_KEY`), `with_project_db_service()` and the retirement message |
| `chat/llm/tool_schemas.py` | `project_db_query` entry in `TOOL_CALL_REGISTRY` (tool name, description, parameter schema), gated with `requires_service: PROJECT_DB_SERVICE_KEY` |
| `chat/gemini_api/tool_handlers/project_db.py` | `_handle_project_db_query()` entry point and `_execute_project_db_query()` implementation using `aiosqlite` |
| `chat/gemini_api/tool_dispatch.py` | Dispatch routing for `project_db_query` in both direct and `tool_call` meta tool paths |
| `chat/gemini_api/system_prompt.py` | Conditional injection of the "Project Database" paragraph (`_PROJECT_DB_PARAGRAPH`, `_project_db_open()`) and `project_db_query` tool visibility based on `has_project` and the `project_db` pseudo-key; the public prompt's `project_db_enabled` flag |
| `chat/gemini_api/conversation.py` | Passes `has_project=bool(project_id)` and a connected-services map carrying the `project_db` key (`with_project_db_service`) to the system prompt builder; `project_db_enabled` to the public one |
| `chat/gemini_api/sub_agent.py` | The same for the sub-agent system prompt builder |
| `chat/gemini_api/tool_dispatch.py` | `_get_connected_services(ctx)` adds the `project_db` key for the skill tools (`list_skills`, `search_skills`, `load_skills`) |
| `chat/system_skills/catalog.py` | The `system:project_db` skill, gated on the project (`requires_project`) and on the `project_db` key (`requires`) |
| `config/paths.py` | Defines `PROJECTS_DIR` (defaults to `data/projects/`); re-exported by `chat/storage.py` for backward compatibility |
| `chat/project_db_routes.py` | REST API endpoints for browsing and managing project database tables (`list_project_tables`, `get_project_table_data`, `delete_project_table`) |
| `frontend/src/hooks/useLegacyProjectTables.ts` | Fetches the project's table list for the right panel (on project change and at the end of each turn); the panel shows the Tables card only while the list is non-empty |
| `frontend/src/components/RightPanel.tsx` | Adds the Tables card (`ProjectTables`) below the file cards, with draggable dividers, for a project with legacy tables |
| `frontend/src/components/ProjectTables.tsx` | The Tables card: renders the list from `useLegacyProjectTables`, opens the viewer, deletes tables |
| `frontend/src/components/TableViewerModal.tsx` | Modal (via `createPortal`) for viewing table data with sticky headers, pagination, server-side column sorting, NULL styling, and cell truncation |
| `frontend/src/api/projectDbApi.ts` | API client with `fetchProjectTables()`, `fetchTableData()`, and `deleteProjectTable()` |

## Storage Layout

The project database file sits inside the project's data directory, alongside the project workspace:

```
data/projects/{project_id}/
├── project.db        # Per-project SQLite database (created lazily)
└── workspace/
    └── workspace/    # Project workspace shared by all project conversations (proj://, /project)
```

The database is separate from the workspace so that workspace file operations (listing, reading, writing) do not interact with it. Since the database lives under `data/projects/{project_id}/`, it is automatically cleaned up when a project is deleted via the existing `shutil.rmtree` in `ChatStorage.delete_project_dir()` in `chat/storage.py`.

## Tool Registration

The `project_db_query` tool is registered in `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py`. It is a dynamic tool invoked via the `tool_call` meta tool pattern (see [Gemini API Integration](gemini-api.md#meta-tool-pattern-tool_call)).

Parameters:
- `query` (string, required) -- The SQL query to execute. Supports any valid SQLite SQL (CREATE TABLE, INSERT, UPDATE, DELETE, SELECT, etc.). One statement per call.
- `intent_message` (string, optional) -- Brief user-friendly summary of intent.

## Conditional Availability

The tool is only visible and available in project conversations. This is controlled at two levels:

1. **System prompt exclusion**: `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py` accept a `has_project` parameter. When `has_project` is `False`, `project_db_query` is added to the `exclude` set passed to `_build_dynamic_tools_section()`, hiding it from the LLM's tool documentation.

2. **Handler guard**: `_handle_project_db_query()` in `chat/gemini_api/tool_handlers/project_db.py` returns a JSON error if `project_id` is `None`, preventing execution even if the LLM somehow invokes the tool in a standalone conversation.

The `has_project` flag is set to `bool(project_id)` in both `conversation.py` (for top-level agents) and `sub_agent.py` (for sub-agents). When `has_project` is `True` and the project passes the [deprecation gate](#deprecation-gate), the system prompt also includes a "Project Database" paragraph (`_PROJECT_DB_PARAGRAPH`) telling the LLM the database already holds tables and how to query them; while Quest Tables is offered, it adds a note that marks the database as legacy and points new records at tables ([Quest Tables -- Prompting](quest-tables.md#prompting)).

## Deprecation Gate

The project database is deprecated in favour of Quest Tables, and the first step of retiring it is that no project can start using it. The rule, in `chat/project_db.py`:

- `project_has_legacy_db(project_id)` is true when the project's `project.db` exists and holds at least one user table (a `sqlite_master` lookup over a read-only connection). A missing file, an empty database (a file created lazily by a query that never made a table) or a database whose tables were all dropped is false; a file that exists but cannot be read counts as legacy data, so a transient SQLite error never hides a project's existing tables. The check never creates the file.
- `PROJECT_DB_SERVICE_KEY` (`"project_db"`) is a connected-services pseudo-key (`PSEUDO_SERVICE_KEYS` in `api/instructions.py`, like the `docs`, `tables` and `browser` keys). The user-level maps (`get_user_connected_services`, `get_public_connected_services`) set it to false; the conversation-aware callers -- `run_conversation_turn`, `_run_sub_agent` and `_get_connected_services()` in `chat/gemini_api/tool_dispatch.py` -- overwrite it per project with `with_project_db_service(services, project_id)`.

Every surface follows the key:

| Surface | Gate |
|---------|------|
| `project_db_query` tool | `requires_service: PROJECT_DB_SERVICE_KEY` on its registry spec, so `tool_is_advertised()` leaves it out of the Dynamic Tools section; `_handle_project_db_query()` also refuses with `project_db_retired_message()` before touching the file, so a hallucinated or replayed call cannot create a database |
| `system:project_db` skill | `requires=PROJECT_DB_SERVICE_KEY` besides `requires_project`; `load_skills` returns the retirement message for a project without legacy tables |
| Prompt paragraph | `_project_db_open(connected_services)` in the standard and sub-agent prompts (a `None` map, the unknown caller context, shows everything, as `_tables_open()` does); the public prompt takes the gate as the explicit `project_db_enabled` flag and also drops the tool from its inlined list |
| Tables card | The right panel fetches the table list (`useLegacyProjectTables`) and renders the `ProjectTables` card only while the list is non-empty, or when the list failed to load so the error shows |

The REST endpoints in `chat/project_db_routes.py` stay ungated: they only list, read and drop tables, and a project without a database already answers an empty list or 404.

**Why gate on the data rather than on a per-project flag or a feature gate?** The data is the truth: a project with tables must keep working, a project without them must not start, and both follow automatically from the file -- there is nothing to migrate, back-fill or administer, and dropping the last table retires the feature for that project on its own. Checking a project's database is one read-only `sqlite_master` lookup per prompt build.

## Query Execution Flow

1. LLM calls `tool_call(tool_name="project_db_query", arguments={"query": "..."})`
2. `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` routes to `_handle_project_db_query(project_id, query)`
3. Handler validates `project_id` is not `None` and `query` is not empty
4. Builds the database path: `PROJECTS_DIR / project_id / "project.db"` (creates parent directory if needed)
5. Calls `_execute_project_db_query(db_path, query)` which uses `aiosqlite` for async I/O

## Query Handling Details

`_execute_project_db_query()` in `chat/gemini_api/tool_handlers/project_db.py`:

- **Connection**: Opens a new `aiosqlite` connection per call with a 10-second connection timeout
- **Pragmas**: Enables WAL journal mode and sets a 5-second busy timeout on each connection
- **Read vs. write detection**: `_is_read_query()` checks if the query starts with `SELECT`, `PRAGMA`, or `EXPLAIN`
- **Read queries**: Fetches rows with column names, returns JSON with `columns`, `rows`, and `row_count` fields
- **Write queries**: Commits the transaction and returns `status` and `rows_affected`
- **Errors**: SQLite errors are caught and returned as JSON with an `error` field

## Constraints

- **Row limit**: SELECT results are capped at 1000 rows (`_PROJECT_DB_MAX_ROWS`). If exceeded, the result includes `truncated: true` and a note suggesting LIMIT/OFFSET or WHERE clauses
- **Size limit**: JSON result payloads are capped at 100KB (`_PROJECT_DB_MAX_RESULT_BYTES`). If exceeded, rows are halved iteratively until the payload fits
- **Single statement**: Only one SQL statement per call is supported (enforced by `aiosqlite.execute()` behavior)
- **ATTACH blocked**: A SQLite authorizer on every connection denies `SQLITE_ATTACH` at statement-compile time, so `ATTACH` is rejected however it is written (leading comments, whitespace, case); a `startswith("ATTACH")` check remains only as a fast path with the same error message
- **Project-only**: Returns an error JSON if invoked without a `project_id` (standalone conversations)
- **Legacy projects only**: Returns the retirement error JSON when the project holds no tables in its database ([Deprecation Gate](#deprecation-gate)), so the database file is never created
- **Cleanup**: The database is deleted when the project is deleted, via the existing `shutil.rmtree` on the project directory

## Table Browser

The project database has a browser UI that lets users inspect and manage table contents from the frontend. For a project with legacy tables, the right panel (`frontend/src/components/RightPanel.tsx`) adds a Tables card (`ProjectTables`) below the file cards, with draggable dividers; the shares are persisted to `localStorage` per project (`quest_project_tables_split_{pid}` for Project Files + Tables, `quest_project_panel_split3_{pid}` for all three cards). A project without legacy tables has no card, and its two file cards persist their split under `quest_project_files_split_{pid}`, derived from the three-card shares until dragged.

The table list is fetched by `useLegacyProjectTables` (`frontend/src/hooks/useLegacyProjectTables.ts`), which the panel uses both to decide whether the card exists and to feed it: it loads when the project changes and refreshes on the `WebSocketManager.onStreamComplete` callback (`send_message_finished`) at end-of-turn, so a card whose last table was dropped during a turn disappears when the turn ends. The companion `onToolResult` subscription was always dead -- no backend caller drove it -- and was dropped when the file browser migrated to `file_list_changed`. A dedicated project-DB mutation event is a future refinement; for now the project DB table list refreshes once per turn rather than mid-turn.

Clicking a table opens a `TableViewerModal` (`frontend/src/components/TableViewerModal.tsx`) rendered via `createPortal` to `document.body`, showing columns, rows with pagination, NULL styling, and cell truncation with tooltips for long values. Column headers are clickable and cycle through none -> asc -> desc -> none; sort changes reset the pagination offset and re-query the server with `sort_by`/`sort_dir`, with the active column marked by an arrow indicator.

Sort and pagination refetches keep the existing rows mounted under an `is-refetching` dim class instead of unmounting the table, avoiding flicker. If the server rejects the sort column with `invalid_sort` (e.g. the schema changed underneath), the client clears the sort state.

Each table entry has a meatball menu (three-dot icon) that appears on hover and offers a "Delete table" action. Deletion requires confirmation via `window.confirm()` and calls the `DELETE /app/api/projects/{project_id}/tables/{table_name}` endpoint (see `delete_project_table()` in `chat/project_db_routes.py`).

The backend endpoints are in `chat/project_db_routes.py` -- see [Project DB API](../api/project-db-api.md) for endpoint details. The API client is in `frontend/src/api/projectDbApi.ts` (`fetchProjectTables()`, `fetchTableData()`, `deleteProjectTable()`) with endpoint URL builders in `frontend/src/api/config.ts` (`projectTables`, `projectTableData`). TypeScript types (`ProjectTable`, `ProjectTablesResponse`, `TableDataResponse`) are in `frontend/src/api/types.ts`.

When no project is associated with the conversation, `RightPanel` renders only the FileBrowser at full height. The `App.tsx` component passes both `conversationId` and `projectId` to `RightPanel`.

## Design Decisions

**Why a per-project database instead of per-conversation?**
Projects are designed for related work that builds on itself across conversations. A shared database follows the same philosophy as the project workspace -- data stored in one conversation is immediately queryable in the next. Like the project workspace (and unlike each conversation's own workspace), it is reached only on purpose, through `project_db_query`. This enables use cases like tracking tasks, logging entries, or building datasets incrementally across multiple conversations.

**Why `aiosqlite` instead of the existing SQLAlchemy async sessions?**
The project database is user-controlled (arbitrary schema, arbitrary queries) rather than application-managed. SQLAlchemy's ORM and migration tooling add no value here. `aiosqlite` provides native async SQLite I/O without thread pool overhead, matching the handler's async execution model.

**Why open a new connection per call instead of pooling?**
Project databases are accessed infrequently (only when the LLM decides to query), and many projects may never use the feature. Connection pooling would hold file handles open for databases that may not be accessed again for hours or days. Opening and closing per call keeps resource usage proportional to actual usage. WAL mode ensures good read performance despite the lack of persistent connections.

**Why block ATTACH DATABASE?**
Without this restriction, the LLM could potentially read or modify other SQLite files on the host filesystem, or *create* a SQLite file at any path the service process can write (e.g. under `frontend/dist`, where the unauthenticated `/{filename}` static fallback would then reveal which files exist -- a covert channel for prompt-visible secrets). The guard is enforced with `sqlite3.Connection.set_authorizer` rather than by inspecting the query text: a textual prefix check was bypassed by a leading SQL comment (`/* x */ ATTACH ...`), whereas the authorizer runs after tokenisation and cannot be fooled by formatting.

**Why row and size limits?**
Large query results would consume the LLM's context window and degrade response quality. The 1000-row and 100KB limits keep results manageable while still supporting substantial datasets. The LLM is guided to use LIMIT/OFFSET or WHERE clauses for larger result sets.
