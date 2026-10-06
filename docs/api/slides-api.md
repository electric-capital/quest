# Slides API Documentation

This document describes how Quest accesses Google Slides content for reading presentations and exports decks into the conversation workspace via `google_export_slides`.

## Overview

Slides read operations use the `authed_get` tool to make authenticated GET requests directly to the Google Slides API v1 at `https://slides.googleapis.com/v1/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, loads the user's Google Services OAuth credentials, and injects a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Slides API endpoints.

Listing Google Slides presentations uses the Google Drive API via `authed_get` with a `mimeType='application/vnd.google-apps.presentation'` filter (Drive is already registered in `_SERVICE_REGISTRY`). Access is read-only.

## Authentication

Slides reads require the user to have connected Google Services. The `authed_get` handler loads credentials via `get_valid_service_credentials()` in `auth/google_credentials.py` and injects a Bearer token. If the user has not connected Google Services, the handler returns an error.

**OAuth scope required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `presentations.readonly` - Read access to Google Slides content

The scope is added to `GOOGLE_SERVICE_SCOPES` in `auth/config.py`. Because it is a new scope, **existing users must re-consent** to the Google Services OAuth grant before Slides requests will succeed; until re-consent, requests fail at the credential/scope check.

The Google Slides entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True` (automatic token refresh and retry on 401 responses), sharing the `_load_google_services_credentials` loader and `_inject_google_bearer_auth` injector with the other Google services.

## Presentation Read Access (via `authed_get`)

Slides reads use `authed_get` with the Google Slides API v1 at `slides.googleapis.com`. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines two read-only allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected: fetching a presentation by ID, and fetching a single page (slide/master/layout) by `pageObjectId`. The regexes use `[^/:]+` for the id segments to exclude the `:` character, which keeps write verbs such as `:batchUpdate` out of the allow-list. See that file for the exact patterns and `api/slides.py` (`get_instructions()`) for query parameter documentation provided to the LLM.

## Listing Google Slides (via Google Drive API)

The Google Slides API does not provide a list endpoint. Listing and searching for Google Slides presentations uses the Google Drive API with a mimeType filter (`mimeType='application/vnd.google-apps.presentation'`), also via `authed_get`. The Drive API base URL is `https://www.googleapis.com/drive/v3`. See `api/slides.py` (`get_instructions()`) for query parameter details.

## Exporting a Presentation to the Workspace (via `google_export_slides`)

Native Google Slides carry no downloadable bytes, so `download_drive_file` cannot fetch them. The `google_export_slides` tool call converts a deck through the Drive v3 `files.export` endpoint and writes the result to the conversation (or project) workspace, where the model reads it back (`get_workspace_file` for txt/pdf/images, `run_python` with python-pptx for pptx) or the user downloads it from the file browser. For the model's own reading, `txt` (the plain text of every slide) is far cheaper than paging the Slides API JSON.

**Implementation:** `_handle_google_export_slides()` in `chat/gemini_api/tool_handlers/drive.py`, a thin wrapper over the `_export_workspace_file()` routine shared with `google_export_doc` / `google_export_sheet`. Dispatched via `tool_call` from `TOOL_CALL_HANDLERS`; schema in `TOOL_CALL_REGISTRY`. Parameters: `presentation_id` (required), `format` (required), optional `filename`. Flow, receipt and error handling as documented for the Docs tool in [Docs API](docs-api.md#exporting-a-doc-to-the-workspace-via-google_export_doc); the kind check rejects anything other than `application/vnd.google-apps.presentation`, cross-pointing to `google_export_doc` / `google_export_sheet` for the other Workspace kinds and to `download_drive_file` for regular files.

**Supported formats** (`GOOGLE_SLIDES_EXPORT_FORMATS`, the complete Google Slides export set): `pptx`, `odp`, `pdf`, `txt`, `png`, `jpeg`, `svg`. The three image formats render the **first slide only** (Drive's export takes no page selector); the skill text points at `pdf` plus sandbox rasterization for every-slide images.

**Allow-list interaction / scope:** same as the Docs tool -- the Drive `files/{id}/export` GET entry, blocked in the bare `authed_get` tool path, works under `drive.readonly` (no new scope beyond the existing `presentations.readonly` for API reads). Not in `PUBLIC_TOOL_CALL_ALLOWLIST` or `SCRIPT_TOOL_CALL_ALLOWLIST`.

## System Skill

Slides usage instructions are surfaced through the gated `system:slides` skill (`requires="google_services"`), registered in `chat/system_skills/catalog.py` with its content built from `api/slides.py` (`get_instructions()`). The skill is loadable only when the user has connected Google Services. See [Skill Library](../architecture/skill-library.md).

## Response Size

Full presentation payloads are large and commonly exceed the `authed_get` response size gate. The skill instructions direct the model to trim payloads with the Slides API `fields` parameter (e.g. `fields=title,slides.objectId`), and for very large decks to pass `output_file` so the response body is written under the hidden `.responses/` workspace subdirectory (and read back via the receipt's `.responses/...` path) and bypasses the size gate. See [Large Response Protection](../architecture/gemini-api.md#large-response-protection).

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Slides reads are standard Google Slides API GET requests. Using `authed_get` with per-user OAuth support eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar, Google Drive, Google Docs, Google Sheets, and other external API services. The `authed_get` handler already provides credential injection, hostname-based service matching, and 401 retry logic.

**Why read-only?**
The allowed-endpoint regexes match only GET-shaped paths and exclude the `:` segment used by the Slides API for write verbs (`:batchUpdate`), so the registry entry cannot reach any mutation endpoint. This matches the read-only posture of the Docs integration (Sheets has a single approval-gated write). Export is a read too: it writes only into the workspace.

**Why use the Drive API for listing presentations?**
The Google Slides API does not provide a list endpoint. The Drive API is used with a mimeType filter to return only Google Slides presentations. This follows Google's recommended pattern for discovering presentations.

**Why a separate hostname (`slides.googleapis.com`) instead of path-prefix on `www.googleapis.com`?**
Unlike Google Calendar and Drive (which live at `www.googleapis.com/calendar/v3` and `www.googleapis.com/drive/v3`), the Google Slides API uses its own hostname (`slides.googleapis.com`). The `_SERVICE_REGISTRY` entry uses plain hostname matching without a `path_prefix` field.
