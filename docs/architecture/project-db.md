# Project Database Architecture

This document describes the per-project SQLite database feature, which gives the LLM a dedicated database for storing and querying structured data within a project.

## Overview

Each project can have a dedicated SQLite database that persists across all conversations in the project. The LLM accesses it via the `project_db_query` dynamic tool, which accepts arbitrary SQL queries. The database is created lazily on first use and lives at `data/projects/{project_id}/project.db`, alongside (but separate from) the project workspace directory. The tool is only available in project conversations -- standalone conversations do not have access.

## Key Files

| File | Role |
|------|------|
| `chat/llm/tool_schemas.py` | `project_db_query` entry in `TOOL_CALL_REGISTRY` (tool name, description, parameter schema) |
| `chat/gemini_api/tool_handlers/project_db.py` | `_handle_project_db_query()` entry point and `_execute_project_db_query()` implementation using `aiosqlite` |
| `chat/gemini_api/tool_dispatch.py` | Dispatch routing for `project_db_query` in both direct and `tool_call` meta tool paths |
| `chat/gemini_api/system_prompt.py` | Conditional injection of "Project Database" section and `project_db_query` tool visibility based on `has_project` flag |
| `chat/gemini_api/conversation.py` | Passes `has_project=bool(project_id)` to the system prompt builder |
| `chat/gemini_api/sub_agent.py` | Passes `has_project=bool(project_id)` to the sub-agent system prompt builder |
| `config/paths.py` | Defines `PROJECTS_DIR` (defaults to `data/projects/`); re-exported by `chat/storage.py` for backward compatibility |
| `chat/project_db_routes.py` | REST API endpoints for browsing and managing project database tables (`list_project_tables`, `get_project_table_data`, `delete_project_table`) |
| `frontend/src/components/RightPanel.tsx` | Splits the right panel between FileBrowser (top) and ProjectTables (bottom) with a draggable divider |
| `frontend/src/components/ProjectTables.tsx` | Lists tables in the project database, auto-refreshes on WebSocket events |
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

The `has_project` flag is set to `bool(project_id)` in both `conversation.py` (for top-level agents) and `sub_agent.py` (for sub-agents). When `has_project` is `True`, the system prompt also includes a "Project Database" paragraph instructing the LLM on how to use the tool.

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
- **Lazy creation**: The database file and parent directory are created on first tool invocation, not when the project is created
- **Cleanup**: The database is deleted when the project is deleted, via the existing `shutil.rmtree` on the project directory

## Table Browser

The project database has a browser UI that lets users inspect and manage table contents from the frontend. When a conversation belongs to a project, the right panel splits into FileBrowser (top) and ProjectTables (bottom) via `frontend/src/components/RightPanel.tsx`. The split position is persisted to `localStorage` per project (key: `quest_project_tables_split_{projectId}`).

The ProjectTables component (`frontend/src/components/ProjectTables.tsx`) lists all user-created tables and auto-refreshes on the `WebSocketManager.onStreamComplete` callback (`send_message_finished`) at end-of-turn. The companion `onToolResult` subscription was always dead -- no backend caller drove it -- and was dropped when the file browser migrated to `file_list_changed`. A dedicated project-DB mutation event is a future refinement; for now the project DB table list refreshes once per turn rather than mid-turn.

Clicking a table opens a `TableViewerModal` (`frontend/src/components/TableViewerModal.tsx`) rendered via `createPortal` to `document.body`, showing columns, rows with pagination, NULL styling, and cell truncation with tooltips for long values. Column headers are clickable and cycle through none -> asc -> desc -> none; sort changes reset the pagination offset and re-query the server with `sort_by`/`sort_dir`, with the active column marked by an arrow indicator.

Sort and pagination refetches keep the existing rows mounted under an `is-refetching` dim class instead of unmounting the table, avoiding flicker. If the server rejects the sort column with `invalid_sort` (e.g. the schema changed underneath), the client clears the sort state.

Each table entry has a meatball menu (three-dot icon) that appears on hover and offers a "Delete table" action. Deletion requires confirmation via `window.confirm()` and calls the `DELETE /app/api/projects/{project_id}/tables/{table_name}` endpoint (see `delete_project_table()` in `chat/project_db_routes.py`).

The backend endpoints are in `chat/project_db_routes.py` -- see [Project DB API](../api/project-db-api.md) for endpoint details. The API client is in `frontend/src/api/projectDbApi.ts` (`fetchProjectTables()`, `fetchTableData()`, `deleteProjectTable()`) with endpoint URL builders in `frontend/src/api/config.ts` (`projectTables`, `projectTableData`). TypeScript types (`ProjectTable`, `ProjectTablesResponse`, `TableDataResponse`) are in `frontend/src/api/types.ts`.

When no project is associated with the conversation, `RightPanel` renders only the FileBrowser at full height. The `App.tsx` component passes both `conversationId` and `projectId` to `RightPanel`, which replaced the previous direct `<FileBrowser>` usage.

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
