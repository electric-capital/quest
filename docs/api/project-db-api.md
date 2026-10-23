# Project DB API Documentation

This document describes the REST API endpoints for browsing and managing project SQLite database tables from the UI.

## Overview

The Project DB API provides endpoints for listing tables, fetching table data, and deleting tables from a project's SQLite database. These endpoints power the ProjectTables browser in the frontend right panel. The project database itself is managed by the LLM via the `project_db_query` tool -- see [Project Database Architecture](../architecture/project-db.md) for details on the tool, the database lifecycle and the deprecation gate. The endpoints are not gated: they only list, read and drop tables, and the frontend uses the list to decide whether a project still shows its Tables card.

**Endpoint prefix:** `/app/api/projects/{project_id}/tables`

## Key Files

| File | Description |
|------|-------------|
| `chat/project_db_routes.py` | Endpoint implementations (`list_project_tables`, `get_project_table_data`, `delete_project_table`) |
| `quest.py` | Route registration (`project_db_router`) |
| `frontend/src/api/projectDbApi.ts` | Frontend API client (`fetchProjectTables()`, `fetchTableData()`, `deleteProjectTable()`) |
| `frontend/src/api/config.ts` | Endpoint URL builders (`projectTables`, `projectTableData`) |
| `frontend/src/api/types.ts` | TypeScript types (`ProjectTable`, `ProjectTablesResponse`, `TableDataResponse`) |

## Authentication

All endpoints support dual authentication: session cookie OR API key Bearer token (same as other `/app/api/*` endpoints). See `chat/auth.py` (`get_current_user_cookie_or_apikey_checked`).

Project ownership is verified on every request -- the project must exist and belong to the authenticated user.

## Endpoints

All endpoints are defined in `chat/project_db_routes.py`. See that file for parameters, response shapes, and error handling.

- **GET `/projects/{project_id}/tables`** -- List all user-created tables in the project database (`list_project_tables()`). Queries `sqlite_master` filtering out internal `sqlite_*` tables. Returns an empty list if the database file does not yet exist (lazy creation).

- **GET `/projects/{project_id}/tables/{table_name}`** -- Fetch columns and rows from a specific table (`get_project_table_data()`). Validates the table name against `sqlite_master` to prevent SQL injection. Supports pagination and optional server-side sorting via query parameters (see the function signature for exact names and defaults). When a sort column is supplied, it is whitelisted against the live table schema via `PRAGMA table_info`; unknown columns return HTTP 400 with error code `invalid_sort`. The response echoes back the applied sort so the client can render an active-column indicator. BLOB values are serialized as `[BLOB, N bytes]` strings.

- **DELETE `/projects/{project_id}/tables/{table_name}`** -- Drop a table from the project database (`delete_project_table()`). Validates the table name against `sqlite_master` before executing `DROP TABLE`. Returns `status` and `table_name` on success.

## Design Decisions

**Why validate table names against sqlite_master?**
The table name is interpolated into a SQL query (`SELECT * FROM "{table_name}"`). Validating that the name exists in `sqlite_master` via a parameterized query prevents SQL injection through crafted table names.

**Why dedicated endpoints instead of reusing the `project_db_query` tool?**
The tool executes arbitrary SQL and is designed for LLM use with context-window-friendly result limits. The table browser endpoints serve a different purpose: paginated, structured table display and safe table management for the UI. They also avoid exposing arbitrary SQL execution to the frontend.

**Why serialize BLOBs as descriptive strings?**
Binary data cannot be represented in JSON. The `[BLOB, N bytes]` format gives users a meaningful indication of BLOB content without attempting to encode or display raw bytes.

**Why whitelist sort columns via `PRAGMA table_info`?**
SQLite does not accept parameter placeholders in `ORDER BY`, so the column name must be interpolated into the SQL. Validating the supplied name against the live table schema prevents SQL injection while still allowing sorting on any real column, including ones added after the table was created.
