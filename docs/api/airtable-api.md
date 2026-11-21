# Airtable API Documentation

This document describes how Quest accesses Airtable data for reading bases, tables, records, and comments.

## Overview

Airtable read operations use the `authed_get` tool to make authenticated GET requests directly to the Airtable API v0 at `https://api.airtable.com/v0/...`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, loads the user's Airtable Personal Access Token, and injects it as a Bearer token into each request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to write or admin endpoints.

## Key Files

| File | Description |
|------|-------------|
| `api/airtable.py` | `get_instructions()` for system prompt documentation (no endpoint functions; reads use `authed_get`) |
| `chat/gemini_api/authed_get.py` | `handle_authed_get()` handler with `api.airtable.com` entry in `_SERVICE_REGISTRY` for authenticated Airtable API GET requests, including `allowed_endpoints` regex validation |
| `auth/airtable.py` | Token management endpoints (`/auth/airtable/save-token`, `/auth/airtable/remove-token`) |
| `db/models.py` | `airtable_token` column in `users` table |

## Authentication

Airtable reads require the user to have configured a Personal Access Token via Settings > Data Connections.

**Token type:** Personal Access Token (PAT), starting with "pat" prefix. PATs are used instead of OAuth -- see [Design Decisions](#design-decisions) for the rationale.

**Token storage:** `airtable_token` column in the `users` table (`db/models.py`), updated via `auth/airtable.py` endpoints.

**Credential loading:** The `_load_airtable_credentials()` function in `chat/gemini_api/authed_get.py` reads the token from the user dict. If the user has not configured a token, the handler returns a `missing_credentials_error` directing the user to Settings > Data Connections.

The Airtable entry in `_SERVICE_REGISTRY` sets `requires_user: True` (per-user credentials) and does not set `retry_on_401` because PATs do not expire or require refresh.

## Airtable Read Access (via `authed_get`)

Airtable reads are invoked as tool calls: `tool_call(tool_name="authed_get", arguments={"url": "https://api.airtable.com/v0/..."})`. The LLM constructs the full Airtable API URL with query parameters; `authed_get` handles authentication automatically.

### Allowed Endpoints Validation

The Airtable `_SERVICE_REGISTRY` entry includes an `allowed_endpoints` field containing regex patterns that define which URL paths are permitted. Before making the HTTP request, `_make_authed_request()` checks the request path against these patterns. If no pattern matches, the request is rejected. See `chat/gemini_api/authed_get.py` for the actual list of allowed path patterns.

## Token Configuration Endpoints

Defined in `auth/airtable.py`:

- `POST /auth/airtable/save-token` -- validates PAT prefix, stores in user record, invalidates sessions to refresh system prompts
- `POST /auth/airtable/remove-token` -- clears token from user record, invalidates sessions

## System Instructions Integration

When a user has configured their Airtable token, the model can reach the Airtable API documentation:

- `get_instructions()` in `api/airtable.py` generates Airtable API documentation section
- `get_user_connected_services()` in `api/instructions.py` checks for `airtable_token` in user dict
- The system prompt carries a one-line Airtable entry in its service list (`_preamble_instructions()` in `api/instructions.py`) only when connected
- The full documentation is the `system:airtable` skill in `chat/system_skills/catalog.py` (`requires="airtable"`), which the model loads on demand

## Constraints

- **Read-only:** No write operations (create, update, delete) are supported
- **Rate limiting:** Airtable enforces 5 requests per second per base
- **Pagination:** Maximum 100 records per page (use offset for continuation)
- **Token scopes:** Personal Access Token must have appropriate scopes and base access
- **Allowed endpoints:** Only the read-only path patterns defined in `_SERVICE_REGISTRY` are permitted; other Airtable API paths are rejected by the `allowed_endpoints` validation

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Airtable reads are standard Airtable API GET requests. Using `authed_get` eliminates the need for dedicated proxy endpoints in `quest.py`, reduces backend code, and follows the same pattern used for Google Calendar, Google Drive, Google Docs, Google Sheets, Google Tasks, and Gmail Raw API.

**Why read-only access?**
Write operations (create, update, delete records) require more complex permissions and validation. Starting with read-only access provides immediate value for querying data while minimizing risk.

**Why Personal Access Tokens instead of OAuth?**
Airtable offers both OAuth and PATs. Quest uses PATs because: (1) OAuth tokens expire and require refresh coordination across concurrent conversations, (2) no background refresh daemon is needed since PATs don't expire, (3) simpler storage model (single string vs. multiple OAuth fields), (4) PATs allow granular scope control in Airtable's UI, and (5) Quest is a single-user proxy where OAuth's third-party authorization model adds complexity without benefit. PATs are Airtable's recommended approach for server-side integrations.

**Why `allowed_endpoints` validation?**
Airtable PATs grant broad access -- a read-scoped PAT allows GET requests to any API path the user has base access for. The `allowed_endpoints` mechanism restricts which URL paths the `authed_get` handler will proxy, adding a server-side guardrail independent of the token's own permissions. The mechanism is generic and used by all services with `authed_get` support.

**Why invalidate sessions on token save/remove?**
Session invalidation ensures the system prompt refreshes immediately to include or exclude the Airtable entry and skill based on the user's connection status.
