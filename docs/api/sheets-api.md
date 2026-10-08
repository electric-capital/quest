# Sheets API Documentation

This document describes how Quest accesses Google Sheets content for reading spreadsheets, exports whole spreadsheets into the conversation workspace via `google_export_sheet`, and how the agent edits spreadsheet cells via the `edit_google_spreadsheet` action request.

## Overview

Sheets read operations use the `authed_get` tool to make authenticated GET requests directly to the Google Sheets API v4 at `https://sheets.googleapis.com/v4/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, loads the user's Google Services OAuth credentials, and injects a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Sheets API endpoints. Listing Google Spreadsheets uses the Google Drive API via `authed_get` with a `mimeType='application/vnd.google-apps.spreadsheet'` filter (Drive is already registered in `_SERVICE_REGISTRY`).

Sheets **writes** go exclusively through the approval-gated `edit_google_spreadsheet` action request (see [Cell Writes via Action Request](#cell-writes-via-action-request-edit_google_spreadsheet) below); there is no direct write path.

## Key Files

| File | Description |
|------|-------------|
| `api/sheets.py` | `get_instructions()` for system prompt documentation (no endpoint functions; reads use `authed_get`), including the "Google Sheets Write Operations" `edit_google_spreadsheet` spec |
| `chat/action_request_types/edit_google_spreadsheet.py` | `EditGoogleSpreadsheetHandler` -- approval-gated cell-range writes with read-before-write verification |
| `chat/gemini_api/tool_handlers/drive.py` | `_handle_google_export_sheet()` + `GOOGLE_SHEET_EXPORT_FORMATS` -- the `google_export_sheet` tool over the shared `_export_workspace_file()` routine |
| `chat/gemini_api/authed_get.py` | `handle_authed_get()` handler with `sheets.googleapis.com` entry in `_SERVICE_REGISTRY` for authenticated Sheets API GET requests, including `allowed_endpoints` regex validation |
| `auth/google_credentials.py` | `get_valid_service_credentials()` (service credential resolution using `google_services_oauth` exclusively) |
| `analysis/analyze_large_tool_results.py` | Analytics for detecting both legacy proxy and new `authed_get` patterns for Sheets usage |

## Authentication

Sheets reads require the user to have connected Google Services. The `authed_get` handler loads credentials via `get_valid_service_credentials()` in `auth/google_credentials.py` and injects a Bearer token. If the user has not connected Google Services, the handler returns an error.

**OAuth scopes required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `spreadsheets.readonly` - Read access to Google Sheets content
- `spreadsheets` - Cell writes via the `edit_google_spreadsheet` action request (added later, so legacy connections may lack it; the handler runs an execute-time scope pre-check and raises a "reconnect Google Services" error instead of an opaque 403)

The Google Sheets entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True` (automatic token refresh and retry on 401 responses).

## Spreadsheet Read Access (via `authed_get`)

Sheets reads use `authed_get` with the Google Sheets API v4 at `sheets.googleapis.com`. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected. See that file for the allowed endpoint list and `api/sheets.py` (`get_instructions()`) for query parameter documentation provided to the LLM.

## Listing Google Spreadsheets (via Google Drive API)

The Google Sheets API does not provide a list endpoint. Listing and searching for Google Spreadsheets uses the Google Drive API with a mimeType filter (`mimeType='application/vnd.google-apps.spreadsheet'`), also via `authed_get`. The Drive API base URL is `https://www.googleapis.com/drive/v3`. See `api/sheets.py` (`get_instructions()`) for query parameter details.

## Exporting a Spreadsheet to the Workspace (via `google_export_sheet`)

Native Google Sheets carry no downloadable bytes, so `download_drive_file` cannot fetch them. The `google_export_sheet` tool call converts a spreadsheet through the Drive v3 `files.export` endpoint and writes the result to the conversation workspace (also in a project conversation, never the project workspace), where the model reads it back (`get_workspace_file` (`read_file` with a `chat://` path in a project conversation) for csv/tsv, `run_python` with openpyxl/pandas for xlsx/ods) or the user downloads it from the file browser.

**Implementation:** `_handle_google_export_sheet()` in `chat/gemini_api/tool_handlers/drive.py`, a thin wrapper over the `_export_workspace_file()` routine shared with `google_export_doc` / `google_export_slides` (format resolution, mimeType pre-check, export call, sanitized `<title><ext>` filename, `_publish_file_list_changed`). Dispatched via `tool_call` from `TOOL_CALL_HANDLERS`; schema in `TOOL_CALL_REGISTRY`. Parameters: `spreadsheet_id` (required), `format` (required), optional `filename`. The flow, receipt shape and error handling are those documented for the Docs tool in [Docs API](docs-api.md#exporting-a-doc-to-the-workspace-via-google_export_doc); only the kind check differs: anything other than `application/vnd.google-apps.spreadsheet` is rejected, with a cross-pointer to `google_export_doc` / `google_export_slides` for the other Workspace kinds and to `download_drive_file` for regular files.

**Supported formats** (`GOOGLE_SHEET_EXPORT_FORMATS`, the complete Google Sheets export set): `xlsx`, `ods`, `pdf`, `csv`, `tsv`, `zip` (zipped HTML, one page per tab). `csv` and `tsv` export the **first tab only** -- Drive's export endpoint takes no tab selector -- so the skill text steers per-tab reads to the Sheets API `values` endpoints and whole-workbook needs to `xlsx`. The `system:sheets` skill (`api/sheets.py`) shows the model each format's MIME type.

**Allow-list interaction / scope:** same as the Docs tool -- the Drive `files/{id}/export` GET entry, blocked in the bare `authed_get` tool path, works under `drive.readonly`. Not in `PUBLIC_TOOL_CALL_ALLOWLIST` or `SCRIPT_TOOL_CALL_ALLOWLIST`.

## Cell Writes via Action Request (`edit_google_spreadsheet`)

The agent edits spreadsheet cells by proposing an `edit_google_spreadsheet` action request (`EditGoogleSpreadsheetHandler` in `chat/action_request_types/edit_google_spreadsheet.py`), which the user approves from a card showing a spreadsheet-style diff table (row numbers, column letters, up to 2 rows/cols of surrounding context, changed cells highlighted).

Params are:

- `spreadsheet_id`,
- `tab`,
- `range` (bounded A1 rectangle, no sheet prefix; max 200 rows x 52 cols, 1000 cells),
- `values` (2d replacement array matching the range dimensions exactly), and
- `current_values` (the values the model just read from the range; the skill directs a `valueRenderOption=FORMULA` read because **formula cells must be claimed by their formula text, not their computed value**).

The handler verifies `current_values` against the live sheet at proposal time (same-turn rejection with a per-cell diff, before any card/row is written) and again at Approve time, so the model can never overwrite cells -- or formulas -- it has not read, and a sheet that changed while the card sat open is not clobbered. The diff table shows formula cells by their old/new formulas and drops context rows/cols that are entirely blank. Approved writes go to `PUT /v4/spreadsheets/{id}/values/{range}?valueInputOption=USER_ENTERED`. See [Action Requests Architecture](../architecture/action-requests.md) for the full parameter, validation, and preview specification.

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Sheets reads are standard Google Sheets API GET requests. Using `authed_get` with per-user OAuth support eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar, Google Drive, Google Docs, and other external API services. The `authed_get` handler already provides credential injection, hostname-based service matching, and 401 retry logic.

**Why use the Drive API for listing spreadsheets?**
The Google Sheets API does not provide a list endpoint. The Drive API is used with a mimeType filter to return only Google Spreadsheets. This follows Google's recommended pattern for discovering spreadsheets.

**Why no Simple API for reads?**
The Sheets API response structure is already well-organized for programmatic consumption. Direct access via `authed_get` provides the full spreadsheet structure including metadata, cell values, and formatting information without needing a simplified wrapper.

**Why does `edit_google_spreadsheet` require `current_values`?**
A cell write is destructive in a way most action requests are not: the approval card can only show what the model *claims* is there unless the server independently knows the current contents. Requiring the model to echo back what it read -- and verifying that claim against the live sheet both when the card is created and when it is approved -- turns "the model overwrote a column it never looked at" from a silent data-loss bug into a same-turn validation error, and doubles as the data source for the old-value side of the diff table.

**Why a separate hostname (`sheets.googleapis.com`) instead of path-prefix on `www.googleapis.com`?**
Unlike Google Calendar and Drive (which live at `www.googleapis.com/calendar/v3` and `www.googleapis.com/drive/v3`), the Google Sheets API uses its own hostname (`sheets.googleapis.com`). The `_SERVICE_REGISTRY` entry uses plain hostname matching without a `path_prefix` field.

