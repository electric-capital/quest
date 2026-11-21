# Federal Register API Documentation

This document describes how Quest reads US Federal Register data (documents, agencies, and public-inspection documents) via the `authed_get` tool.

## Overview

Federal Register reads use the `authed_get` tool to make GET requests directly to the public Federal Register API v1 at `https://www.federalregister.gov/api/v1/...`. The Federal Register API is a free, public US government API that requires **no authentication**, so unlike every other `authed_get` backend it has no credentials, no connection step, and no Settings entry.

The `authed_get` handler in `chat/gemini_api/authed_get.py` matches the hostname, applies a `path_prefix` scope so the proxy is confined to `/api/v1`, runs the request path through an `allowed_endpoints` regex allow-list, and injects nothing (a no-op auth injector).

The backend documentation is carried in the ungated `system:federal_register` system skill rather than always-on prompt text.

## Key Files

| File | Description |
|------|-------------|
| `api/federal_register.py` | `get_instructions(base_url)` for the `system:federal_register` skill content (base URL constant `FEDERAL_REGISTER_API_BASE`, key-paths table, documents-search query params, the title-case `conditions[type][]` gotcha, size-gate / `fields[]` / `output_file` guidance, example tool calls); no endpoint functions -- reads use `authed_get` |
| `chat/gemini_api/authed_get.py` | `handle_authed_get()` handler with the `www.federalregister.gov/api/v1` entry in `_SERVICE_REGISTRY` (`path_prefix`, `allowed_endpoints` regexes, `default_headers`); `_load_federal_register_credentials()` (sync, no-arg, returns `None`) and `_inject_no_auth()` (no-op injector) model the no-auth service |
| `chat/system_skills/catalog.py` | Registers `system:federal_register` as an ungated (`requires=None`) `SystemSkill`; delegates `content_builder` to `api.federal_register.get_instructions` |
| `chat/llm/tool_schemas.py` | The `authed_get` tool description lists "Federal Register" among supported hosts and `system:federal_register` in the `system:*` skill id list (endpoints not inlined -- the skill carries the detail) |
| `chat/gemini_api/system_prompt.py` | "Federal Register" added to the prose lines enumerating `authed_get` backends |

## No Authentication

The Federal Register API needs no credentials, so the service is modelled as a no-auth entry in `_SERVICE_REGISTRY`:

- **No-arg `None` loader:** `_load_federal_register_credentials()` in `chat/gemini_api/authed_get.py` is a synchronous, no-argument function that returns `None` (matching the call shape of the CoinGecko key loader rather than the per-user `async (user)` loaders).
- **No-op injector:** `_inject_no_auth()` is used as `inject_auth`. `_make_authed_request()` always invokes `inject_auth` even when the loaded credential is `None`, so the no-op injector makes that call harmless rather than special-casing the code path.
- **`requires_user` deliberately unset:** The registry entry does not set `requires_user`. The missing-credentials guard in `_make_authed_request()` only fires when `credentials is None and requires_user`, so leaving `requires_user` unset lets the `None` credential flow past the guard instead of being treated as a missing-credentials error.
- **No `retry_on_401`:** There is no token to refresh, so the 401-retry branch is not enabled.
- **`default_headers`:** A `User-Agent: Quest/1.0` header is set (polite and de-risks any future bot filtering on the host).

See [Authenticated External API Requests](../architecture/gemini-api.md#authenticated-external-api-requests-authed_get) for the service registry schema, the credential-loading flow, and the header merge precedence that the no-auth path reuses.

## Federal Register Read Access (via `authed_get`)

Federal Register reads use `authed_get` with the full API URL. The `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py` defines the allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected.

The allow-list covers read-only paths only: documents search (`.json`/`.csv`), single and comma-joined multi-document fetches, document facets, public-inspection documents (search, current, single), agencies (list and single by slug or numeric id), and suggested searches (list and single). The `/images/{identifier}` path is intentionally not allow-listed.

See that file for the full pattern list and `api/federal_register.py` (`get_instructions()`) for the LLM-facing documentation (base URL, key paths, query parameters, and example invocations).

Documents search results and the full `/agencies` list frequently exceed the 3 KB `authed_get` size gate. The skill instructions direct the model to trim payloads with `fields[]` (URL-encoded as `fields%5B%5D`) plus a small `per_page`, and to pass `output_file` for the large agencies list or CSV exports so the body is written under the hidden `.responses/` workspace subdirectory (read back via the receipt's `.responses/...` path) instead of returned inline. See [Large Response Protection](../architecture/gemini-api.md#large-response-protection).

### Path Scoping

The registry key is `www.federalregister.gov/api/v1` (host plus path prefix) and the entry sets `path_prefix: "/api/v1"`. This scopes `authed_get` to the JSON API surface only -- it cannot be used to proxy arbitrary `www.federalregister.gov` human-website URLs, even though they share a hostname. The `allowed_endpoints` regexes provide a second, finer layer of read-only path gating on top of the prefix scope.

## System Instructions Integration

Federal Register documentation is exposed through the `system:federal_register` system skill rather than being inlined in every system prompt:

- `api/federal_register.py` (`get_instructions()`) builds the skill content.
- `chat/system_skills/catalog.py` registers `system:federal_register` with `requires=None` (ungated). Because it has no `requires` gate and no `requires_project`, it is always visible and loadable for every user regardless of connection state -- there is no `connected_services` key to check.
- The model loads it on demand via `load_skills(skill_ids=["system:federal_register"])`; it is enumerated in the always-on system-skill bullet list. See [Skill Library Architecture -- System Skills](../architecture/skill-library.md#system-skills).
- The `authed_get` tool schema in `chat/llm/tool_schemas.py` lists "Federal Register" among supported hosts and `system:federal_register` among the `system:*` skill ids, and the `authed_get` backend prose in `chat/gemini_api/system_prompt.py` names Federal Register.

## Constraints

- **Read-only:** Only the GET paths on the `allowed_endpoints` list are reachable; the allow-list is GET-only and there are no Federal Register write operations.
- **Path-scoped:** Only `/api/v1/...` paths are proxied; arbitrary `www.federalregister.gov` website URLs are rejected by the `path_prefix` scope.
- **No authentication:** No credentials, connection, or Settings entry; the skill is always available.
- **Size gate:** Responses over the ~3 KB `authed_get` size gate must be trimmed with `fields[]` + small `per_page` or written to the hidden `.responses/` workspace subdirectory with `output_file`.
- **Allowed endpoints:** Only the read-only path patterns defined in the Federal Register `_SERVICE_REGISTRY` entry are permitted; other paths (including `/images/{identifier}`) are rejected by the `allowed_endpoints` validation.

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Federal Register reads are standard public API GET requests. Using `authed_get` avoids dedicated proxy endpoints, reuses the shared `allowed_endpoints` path gating and large-response protection, and aligns Federal Register with the same pattern used by Google Calendar, Drive, Docs, Sheets, Tasks, Gmail Raw, Airtable, and GitHub.

**Why a no-auth service instead of skipping credential handling?**
`_make_authed_request()` always loads credentials and invokes `inject_auth`. Rather than branch the shared code path for an unauthenticated host, the service supplies a `None`-returning loader and a no-op injector and leaves `requires_user` unset, so the generic flow runs unchanged and the `None` credential never trips the missing-credentials guard.

**Why path-scope the registry key to `/api/v1`?**
The hostname `www.federalregister.gov` also serves the public human-facing website. Scoping the registry entry to the `/api/v1` prefix prevents `authed_get` from being used to proxy arbitrary website pages, confining it to the documented JSON API surface.

**Why an ungated (`requires=None`) system skill?**
The API needs no connection or credentials, so there is no connection state to gate on. Making the skill ungated keeps it visible and loadable for every user, while still keeping the backend detail out of the always-on prompt (the model loads it only when working with federal regulations).
