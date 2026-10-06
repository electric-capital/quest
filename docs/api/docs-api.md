# Docs API Documentation

This document describes how Quest accesses Google Docs content for reading documents and exports Docs into the conversation workspace as files.

## Overview

Docs read operations use the `authed_get` tool to make authenticated GET requests directly to the Google Docs API v1 at `https://docs.googleapis.com/v1/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, loads the user's Google Services OAuth credentials, and injects a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Docs API endpoints. Listing Google Docs uses the Google Drive API via `authed_get` with a `mimeType='application/vnd.google-apps.document'` filter (Drive is already registered in `_SERVICE_REGISTRY`).

## Authentication

Docs reads require the user to have connected Google Services. The `authed_get` handler loads credentials via `get_valid_service_credentials()` in `auth/google_credentials.py` and injects a Bearer token. If the user has not connected Google Services, the handler returns an error.

**OAuth scope required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `documents.readonly` - Read access to Google Docs content

The Google Docs entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True` (automatic token refresh and retry on 401 responses).

## Document Read Access (via `authed_get`)

Docs reads use `authed_get` with the Google Docs API v1 at `docs.googleapis.com`. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected. See that file for the allowed endpoint list and `api/docs.py` (`get_instructions()`) for query parameter documentation provided to the LLM.

## Listing Google Docs (via Google Drive API)

The Google Docs API does not provide a list endpoint. Listing and searching for Google Docs uses the Google Drive API with a mimeType filter (`mimeType='application/vnd.google-apps.document'`), also via `authed_get`. The Drive API base URL is `https://www.googleapis.com/drive/v3`. See `api/docs.py` (`get_instructions()`) for query parameter details.

## Exporting a Doc to the Workspace (via `google_export_doc`)

Native Google Docs carry no downloadable bytes -- `alt=media` on `files/{id}` fails for Google Workspace files, so `download_drive_file` cannot fetch them. The `google_export_doc` tool call converts a Doc through the Drive v3 `files.export` endpoint and writes the result to the conversation (or project) workspace, where the model reads it back with `get_workspace_file` or the user downloads it from the file browser.

**Implementation:** `_handle_google_export_doc()` in `chat/gemini_api/tool_handlers/drive.py`, a thin wrapper over the `_export_workspace_file()` routine shared with the sibling `google_export_sheet` / `google_export_slides` tools (per-kind entries in `_EXPORT_KINDS`: accepted source mimeType, format table, label, id parameter name), dispatched via `tool_call` from the `TOOL_CALL_HANDLERS` table in `chat/gemini_api/tool_dispatch.py`; schema in `TOOL_CALL_REGISTRY` (`chat/llm/tool_schemas.py`). Parameters: `document_id` (required), `format` (required), optional `filename`.

**Flow:**

1. Resolve `format` against `GOOGLE_DOC_EXPORT_FORMATS` (short name, case-insensitive, leading dot tolerated, or the exact export MIME type). Unknown formats return an `error` plus the `supported_formats` map without any upstream call.
2. Fetch `files/{id}?fields=name,mimeType&supportsAllDrives=true` via `_make_authed_request()`. Anything other than `application/vnd.google-apps.document` is rejected: regular files get a pointer to `download_drive_file`; a Sheet or Slides file gets a cross-pointer to `google_export_sheet` / `google_export_slides` with the right id parameter filled in; other Workspace types (Forms, ...) get a "not supported by this tool" error. The title supplies the default filename.
3. `GET files/{id}/export?mimeType=<export mime>` with `raw_response=True`. Upstream errors (incl. Google's 10 MB export cap) are returned as `error` with a hint to try a lighter format.
4. Write the bytes to `<workspace>/<filename>` (filename sanitized via `_sanitize_workspace_filename`, traversal-checked; default `<title><ext>`; overwrites like `download_drive_file`), then `_publish_file_list_changed()` so the file browser refreshes. The receipt carries `filename`, `size_bytes`, `format`, `export_mime_type`, `content_type`, `document_title`, and a `message` with the follow-up `get_workspace_file` call.

**Supported formats** (the complete Google Docs export set): `pdf`, `docx`, `odt`, `rtf`, `txt`, `md`, `html`, `epub`, `zip` (zipped HTML with images). The table in `api/docs.py` (`get_instructions()`, surfaced by the `system:docs` skill) shows the model each format's MIME type and steers it to `md`/`txt` when it needs to read the content itself.

**Allow-list interaction:** the Drive `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` gained the GET pattern `^/drive/v3/files/[^/]+/export$` so the handler can reuse `_make_authed_request()` (credential injection, 401 refresh). `handle_authed_get()` rejects that path when `output_file` is absent -- mirroring the `alt=media` block -- and points the model at the three export tools; with `output_file` set the export lands under `.responses/` like any other body. Export works under the existing `drive.readonly` scope, no re-consent needed.

**Not allow-listed for public projects or the script bridge:** like `download_drive_file`, the tool stays out of `PUBLIC_TOOL_CALL_ALLOWLIST` and `SCRIPT_TOOL_CALL_ALLOWLIST`.

## Design Decisions

**Why a dedicated `google_export_doc` tool instead of reusing `download_drive_file` with a format parameter?**
`download_drive_file` is a byte-for-byte fetch (`alt=media`) whose contract is "the file as stored"; export is a conversion with a format choice, a different endpoint, a Docs-only precondition, and different failure modes (the 10 MB cap). Keeping them separate keeps each tool's description short and unambiguous for the model, and the mimeType pre-check gives a precise cross-pointer in each direction.

**Why three export tools (Docs / Sheets / Slides) instead of one `google_export_file` with a union format enum?**
The three kinds have disjoint format sets with kind-specific caveats (csv/tsv = first tab, png/jpeg/svg = first slide), and a single enum would let the model ask for `docx` on a Sheet and only learn upstream that it is invalid. Per-kind tools keep each enum exact and each description short, while one shared `_export_workspace_file()` routine keeps the behaviour (filename rules, errors, receipts) identical, and the mimeType pre-check cross-points between the siblings so a wrong guess costs one cheap metadata call.


**Why `authed_get` instead of proxy endpoints?**
Docs reads are standard Google Docs API GET requests. Using `authed_get` with per-user OAuth support eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar, Google Drive, and other external API services. The `authed_get` handler already provides credential injection, hostname-based service matching, and 401 retry logic.

**Why use the Drive API for listing documents?**
The Google Docs API does not provide a list endpoint. The Drive API is used with a mimeType filter to return only Google Docs. This follows Google's recommended pattern for discovering documents.

**Why a separate hostname (`docs.googleapis.com`) instead of path-prefix on `www.googleapis.com`?**
Unlike Google Calendar and Drive (which live at `www.googleapis.com/calendar/v3` and `www.googleapis.com/drive/v3`), the Google Docs API uses its own hostname (`docs.googleapis.com`). The `_SERVICE_REGISTRY` entry uses plain hostname matching without a `path_prefix` field.
