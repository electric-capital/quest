# Drive API Documentation

This document describes how Quest accesses Google Drive data for reads (file metadata, permissions, revisions, comments, changes, quota, shared drives), file downloads and Google Workspace exports, and writes (Save to Drive plus the agent-driven `upload_to_drive` and `create_drive_folder` action requests).

## Overview

Drive read operations use the `authed_get` tool to make authenticated GET requests directly to the Google Drive API v3 at `https://www.googleapis.com/drive/v3/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname and path, loads the user's Google Services OAuth credentials, and injects a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Drive API endpoints. Binary file content downloads use the separate `download_drive_file` tool, which downloads the file to the conversation workspace so the LLM can then read it with `get_workspace_file`.

## Authentication

Drive reads require the user to have connected Google Services. The `authed_get` handler loads credentials via `get_valid_service_credentials()` in `auth/google_credentials.py` and injects a Bearer token. If the user has not connected Google Services, the handler returns an error.

**OAuth scopes required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `drive.readonly` - Read access to Drive files and metadata
- `drive.file` - Write access to files created by the app (used by Save to Drive)

The Google Drive entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True` (automatic token refresh and retry on 401 responses).

## Drive Read Access (via `authed_get`)

Drive reads use `authed_get` with the Google Drive API v3. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected. See that file for the allowed endpoint list and `api/drive.py` (`get_instructions()`) for query parameter documentation provided to the LLM.

The allow-list covers these read-only resources (all GET, all under the existing `drive.readonly` scope, so no re-consent is needed):

| Resource | Paths | Notes |
|----------|-------|-------|
| Files | `files`, `files/{id}` | List/search and single-file metadata (`alt=media` blocked in the tool path, see below) |
| Export | `files/{id}/export` | Reachable only through the three export tools / `output_file` (see below) |
| Permissions | `files/{id}/permissions`, `.../permissions/{id}` | Who has access; a `type=anyone` entry means link-shared |
| Revisions | `files/{id}/revisions`, `.../revisions/{id}` | Version history; `alt=media` on a revision is blocked like any binary download |
| Comments | `files/{id}/comments`, `.../comments/{id}`, `.../comments/{id}/replies`, `.../replies/{id}` | Google requires `fields` on these calls; the skill text says so |
| Changes | `changes/startPageToken`, `changes` | Change-feed cursor + "what changed since"; `pageToken` required on `changes`, the model is told to persist `newStartPageToken` |
| About | `about` | Storage quota + connected account; `fields` required |
| Shared drives | `drives`, `drives/{id}` | List and single shared drive |

Write-shaped paths (`files/{id}/copy`, `files/{id}/watch`, `changes/watch`, `drives/{id}/hide|unhide`, `modifyLabels`) and the legacy `teamdrives` / `apps` resources stay unmatched. `tests/test_google_export_sheet_slides_handlers.py` pins the admitted and refused sets.

**Note:** The `alt=media` parameter is blocked in the `authed_get` tool call path. Binary file content must be downloaded using `download_drive_file` instead (see below). The `POST /api/authed-get` proxy endpoint still supports `alt=media` for sandbox scripts that need direct binary access.

## File Content Downloads (via `download_drive_file`)

Binary file content downloads use the `download_drive_file` tool call, which downloads the file to the conversation workspace (also in a project conversation). The LLM can then read or analyze the file using `get_workspace_file` (`read_file` with a `chat://` path in a project conversation).

**Implementation:** `_handle_download_drive_file()` in `chat/gemini_api/tool_handlers/drive.py`. Fetches file metadata first (to determine filename if not provided), then downloads binary content via `_make_authed_request()` with `alt=media` and `raw_response=True`, and saves to the conversation workspace directory. See that function for tool parameters.

Native Google Workspace files have no `alt=media` bytes. They are exported to the workspace instead via three sibling tools sharing one implementation (`_export_workspace_file()` in `chat/gemini_api/tool_handlers/drive.py`, Drive `files/{id}/export`): `google_export_doc` (Docs, nine formats -- see [Docs API](docs-api.md#exporting-a-doc-to-the-workspace-via-google_export_doc)), `google_export_sheet` (Sheets: xlsx, ods, pdf, csv, tsv, zip -- csv/tsv first tab only; see [Sheets API](sheets-api.md#exporting-a-spreadsheet-to-the-workspace-via-google_export_sheet)) and `google_export_slides` (Slides: pptx, odp, pdf, txt, png, jpeg, svg -- images first slide only; see [Slides API](slides-api.md#exporting-a-presentation-to-the-workspace-via-google_export_slides)). Each tool pre-checks the file's `mimeType` and, when it is the wrong Workspace kind, returns a cross-pointer naming the right sibling tool and its id parameter; regular files are pointed at `download_drive_file`. The `^/drive/v3/files/[^/]+/export$` GET allow-list entry exists for these tools and `handle_authed_get()` blocks the path without `output_file` the same way it blocks `alt=media`, naming all three tools.

## Save to Drive (via File Browser)

The FileViewerModal provides a "Save to Drive" button for markdown files viewed in the file browser. This uploads the raw markdown content to Google Drive, where Google natively converts it to a Google Document.

**Endpoint:** POST `/app/api/conversations/{conversation_id}/files/save-to-drive`

**Implementation:** `save_to_drive()` in `chat/file_routes.py`. See that function for request/response fields.

**Auth:** Session cookie or API key Bearer token (same as other file browser endpoints)

Creates a new Google Document in the user's Drive root via multipart upload to the Google Drive API v3 with `application/vnd.google-apps.document` MIME type. Authentication handled via `make_authenticated_request()` from `auth/google_credentials.py`.

**OAuth scope required:** `drive.file` (per-app file creation scope, part of `GOOGLE_SERVICE_SCOPES` in `auth/config.py`). Users who connected Google Services before this scope was added must reconnect via Settings > Data Connections to grant the updated permissions.

**Frontend:** `saveToDrive()` in `frontend/src/api/fileApi.ts`. "Save to Drive" button appears in the FileViewerModal when viewing `.md` files.

## Drive Upload (via `upload_to_drive` Action Request)

The agent uploads workspace files to the user's Drive by proposing an `upload_to_drive` action request (`create_action_request(request_type="upload_to_drive", params={...})`), which appends an inline approval card. One request carries up to 10 files bound for a single destination folder (a `files` list of `{path, filename?}` entries), so a batch costs the user one approval instead of one per file. On Approve, the handler reads each workspace file's raw bytes and uploads them to Drive sequentially.

Unlike "Save to Drive" (markdown-only, converted to a native Google Doc), this uploads the files **as-is** with no conversion, so the results are generic Drive file links (`https://drive.google.com/file/d/<id>/view`).

**Handler:** `UploadToDriveHandler` in `chat/action_request_types/upload_to_drive.py`. The following are all documented in [Action Requests Architecture](../architecture/action-requests.md):

- Params (`files` required -- 1..10 `{path, filename?}` entries, no duplicate paths; the legacy single-file top-level `path` / `filename` shape is still accepted for pending pre-deploy rows; `folder_id` optional; `new_folder_name` / `new_folder_parent_id` optional, creating the destination folder at approval time -- `new_folder_name` is mutually exclusive with `folder_id`, and the parent id is only valid alongside the name),
- the per-file 50 MB cap,
- the `drive.file` scope pre-check,
- shared-drive support (`supportsAllDrives=true`),
- the server-injected `folder_name` / `new_folder_parent_name` preview enrichment,
- the `folder_id` / `folder_name` / `folder_url` result extension when a folder was created, and
- the partial-batch / duplicate-folder recovery error shape on mid-batch failure.

Paths are conversation-workspace paths only; in a project conversation a project file must first be copied in (`copy_file` from `proj://` to `chat://`), and a not-found error says so (`PROJECT_COPY_FIRST_SUFFIX`, `chat/workspace_hints.py`). Every file is pre-flight resolved (traversal guards + size cap) via `resolve_workspace_file()` in `chat/action_request_types/_io_attachments.py` before any Drive mutation; bytes are then read one file at a time via `read_resolved_file_bytes()`.

## Drive Folder Creation (via `create_drive_folder` Action Request)

The agent creates a Drive folder by proposing a `create_drive_folder` action request (required `name`, optional `parent_folder_id`; shared drives supported). On Approve, the handler creates the folder via the Drive v3 `files.create` metadata endpoint and the result carries the new `folder_id` plus a `https://drive.google.com/drive/folders/<id>` URL that follow-up `upload_to_drive` requests can target.

**Handler:** `CreateDriveFolderHandler` in `chat/action_request_types/create_drive_folder.py`. Validation rules, the Folder / Parent preview rows, and the server-injected `parent_folder_name` enrichment are documented in [Action Requests Architecture](../architecture/action-requests.md). Both Drive handlers share constants, scope/error helpers, folder-name resolution, and the `create_folder()` call via `chat/action_request_types/_drive.py`.

## Shared Notes for Drive Write Action Requests

**Auth:** Both request types require connected Google Services with the `drive.file` scope (same scope as Save to Drive). Legacy connections that predate the write scope must reconnect via Settings > Data Connections.

**Scope caveat:** Under `drive.file` the app can only see files and folders it created or that were opened with it, so targeting an arbitrary pre-existing folder id (`folder_id` / `new_folder_parent_id` / `parent_folder_id`) can fail with a Drive 404 if that folder is not app-accessible. Folders created through `create_drive_folder` or the `new_folder_name` path are app-created and always usable as parents.

**Model-facing instructions:** `api/drive.py` (`get_instructions()`, "Google Drive Write Operations" section), surfaced via the `system:drive` skill. The `system:action_requests` routing map in `chat/system_skills/catalog.py` points the model to `system:drive` for Drive uploads and folder creation, and its generic "Minimize approval round trips" section (plus the drive skill's "Batch your uploads" note) instructs the model to batch same-destination uploads into one request's `files` list, fold folder creation into the upload via `new_folder_name` rather than a separate `create_drive_folder` request, and only split batches across requests for >10 files or multiple destination folders.

The instructions also direct the model to search for an existing folder via `authed_get` before proposing `create_drive_folder` (Drive allows same-name sibling folders).

Both types slot into the existing action-request architecture: they block via an `action_request` wait handle plus `SuspendForActionRequest`, require no DB migration (only the `ActionRequestType` enum addition in `db/models.py`, from which the tool schemas derive), no new wait-handle kind, and no new REST endpoint (the existing resolve endpoint passes `conversation_id` to `execute()` for workspace resolution). They are disabled in Slack-driven runs, and sub-agents must hand the proposed request back to the parent via `agent_task_response` (`create_action_request` is top-level only).

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Drive reads are standard Google Drive API GET requests. Using `authed_get` with per-user OAuth support eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar and other external API services. The `authed_get` handler already provides credential injection, hostname-based service matching, and 401 retry logic.

**Why a separate `download_drive_file` tool instead of `alt=media` via `authed_get`?**
Binary file content is useless to the LLM directly. The `download_drive_file` tool downloads the file to the workspace, where `get_workspace_file` can properly handle it (inline for text, uploaded via the provider's file API for binary/large files). The `alt=media` parameter is blocked in the `authed_get` tool call path to enforce this pattern. The `POST /api/authed-get` proxy endpoint still supports `alt=media` for sandbox scripts.

**Why `drive.file` scope instead of `drive` (full access)?**
The `drive.file` scope limits write access to files created by the application, following the principle of least privilege. Users' existing Drive files remain read-only (via `drive.readonly`). This scope requires users to re-authorize Google Services after the upgrade.

**Why support shared drives parameters?**
Many organizations use Google Workspace with shared drives. The `supportsAllDrives`, `includeItemsFromAllDrives`, `corpora`, and `driveId` parameters enable access to both personal and shared drive content.
