# Tasks API Documentation

This document describes how Quest accesses Google Tasks data for reading task lists and tasks.

## Overview

Task read operations use the `authed_get` tool to make authenticated GET requests directly to the Google Tasks API v1 at `https://tasks.googleapis.com/tasks/v1/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, loads the user's Google Services OAuth credentials, and injects a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Tasks API endpoints.

## Key Files

| File | Description |
|------|-------------|
| `api/tasks.py` | `get_instructions()` for system prompt documentation (no endpoint functions; reads use `authed_get`) |
| `chat/gemini_api/authed_get.py` | `handle_authed_get()` handler with `tasks.googleapis.com` entry in `_SERVICE_REGISTRY` for authenticated Tasks API GET requests, including `allowed_endpoints` regex validation |
| `auth/google_credentials.py` | `get_valid_service_credentials()` (service credential resolution using `google_services_oauth` exclusively) |

## Authentication

Task reads require the user to have connected Google Services. The `authed_get` handler loads credentials via `get_valid_service_credentials()` in `auth/google_credentials.py` and injects a Bearer token. If the user has not connected Google Services, the handler returns an error.

**OAuth scope required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `tasks.readonly` - Read access to Google Tasks data

**Re-authorization note:** Users who connected Google Services before the `tasks.readonly` scope was added must re-authorize via Settings > Data Connections > Google Services to grant the new scope.

The Google Tasks entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True` (automatic token refresh and retry on 401 responses).

## Task Read Access (via `authed_get`)

Task reads use `authed_get` with the Google Tasks API v1 at `tasks.googleapis.com`. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected. See that file for the allowed endpoint list and `api/tasks.py` (`get_instructions()`) for query parameter documentation provided to the LLM.

## System Instructions Integration

When a user has connected Google Services, the model can reach the Tasks API documentation:

- `get_instructions()` function in `api/tasks.py` generates Tasks API documentation section
- `get_user_connected_services()` in `api/instructions.py` checks for `google_services_oauth` in user dict
- The system prompt carries a one-line Tasks entry in its service list (`_preamble_instructions()` in `api/instructions.py`) when `connected_services["google_services"]` is True
- The full documentation is the `system:tasks` skill in `chat/system_skills/catalog.py` (`requires="google_services"`), which the model loads on demand

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Task reads are standard Google Tasks API GET requests. Using `authed_get` with per-user OAuth support eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar, Google Drive, Google Docs, Google Sheets, and Gmail Raw API. The `authed_get` handler already provides credential injection, hostname-based service matching, and 401 retry logic.

**Why only read access (no Simple API)?**
Tasks API responses are already well-structured and don't require transformation for LLM consumption. Direct access via `authed_get` provides the full task structure without needing a simplified wrapper.

**Why a separate hostname (`tasks.googleapis.com`) instead of path-prefix on `www.googleapis.com`?**
Unlike Google Calendar and Drive (which live at `www.googleapis.com/calendar/v3` and `www.googleapis.com/drive/v3`), the Google Tasks API uses its own hostname (`tasks.googleapis.com`). The `_SERVICE_REGISTRY` entry uses plain hostname matching without a `path_prefix` field.

## Constraints

- **Read-only:** No write operations (create, update, delete) are supported
- **Scope re-authorization:** Users who connected Google Services before `tasks.readonly` was added must re-authorize to grant the new scope
- **Allowed endpoints:** Only the read-only path patterns defined in `_SERVICE_REGISTRY` are permitted; other Tasks API paths are rejected by the `allowed_endpoints` validation
