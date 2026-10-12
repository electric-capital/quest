# SEC EDGAR API

## Overview

Read-only access to US Securities and Exchange Commission EDGAR data -- company submission/filing history and XBRL financial facts -- packaged as the in-tree `plugins/sec_edgar` plugin (plugin id `sec_edgar`; see [Plugins](../../../docs/architecture/plugins.md)).

The plugin contributes no tools and no action requests. Every read is a plain GET the model composes through `authed_get` against two plugin-registered service entries, guided by the `system:sec_edgar` skill: the public SEC EDGAR data API at `https://data.sec.gov/...`, plus the two static ticker→CIK map files on `https://www.sec.gov/files/...`.

The SEC EDGAR data API is free, public, and requires **no authentication**, so the plugin is the **credential-free** shape: nothing to configure server-side (no `credential_schema`, so no admin card) and nothing to connect per user (no `user_connection`, so no Data Connections row). It is modelled closely on the core [Federal Register API](../../../docs/api/federal-register-api.md) no-auth entry, moved behind the plugin contract.

## Key Files

| File | Description |
|------|-------------|
| `plugins/sec_edgar/manifest.py` | The manifest: the `data.sec.gov` and `www.sec.gov` service entries (plain hostname keys, `allowed_endpoints` regexes, SEC `User-Agent` `default_headers`), the no-auth `load_sec_edgar_credentials()` / `inject_no_auth()` pair, and the ungated `system:sec_edgar` skill |
| `plugins/sec_edgar/instructions.md` | `system:sec_edgar` skill content: base URL, CIK 10-digit zero-padding rule, ticker→CIK resolution workflow, key-paths table, parallel-arrays note, size-gate / `output_file` guidance, rate limit, example tool calls |
| `plugins/sec_edgar/tests/` | allow-list and request tests (`test_sec_edgar_services.py`), manifest shape, skill visibility and the `/me` aggregate (`test_sec_edgar_manifest.py`) |
| `config/plugins.py` | `plugin_is_connectionless()` / `connectionless_plugin_ids()`: the loader-side notion of a plugin with nothing to connect, consumed by `GET /me` (see below) |

## No Authentication

The SEC EDGAR data API needs no credentials, so both hosts are modelled as no-auth entries, sharing the plugin's loader and injector:

- **No-arg `None` loader:** `load_sec_edgar_credentials()` is a synchronous, no-argument function that returns `None` (matching the call shape of the CoinGecko key loader rather than the per-user `async (user)` loaders).
- **No-op injector:** `inject_no_auth()` is used as `inject_auth`. `_make_authed_request()` always invokes `inject_auth` even when the loaded credential is `None`, so the no-op injector makes that call harmless rather than special-casing the code path.
- **`requires_user` deliberately unset:** neither entry sets `requires_user`. The missing-credentials guard in `_make_authed_request()` only fires when `credentials is None and requires_user`, so leaving `requires_user` unset lets the `None` credential flow past the guard instead of being treated as a missing-credentials error.
- **No `retry_on_401`:** there is no token to refresh, so the 401-retry branch is not enabled.
- **`default_headers` -- SEC `User-Agent` requirement:** SEC's fair-access policy returns HTTP 403 to any request lacking a descriptive `User-Agent` (name + contact). Both entries set `default_headers` with `User-Agent: Quest/1.0 (admin@quest.example)` so the header is injected on every request below caller-supplied headers in precedence -- the same mechanism GitHub and Federal Register use. This is **not** a custom injector; it rides on the existing `default_headers` merge. The model does not (and should not) set the header itself.

See [Authenticated External API Requests](../../../docs/architecture/gemini-api.md#authenticated-external-api-requests-authed_get) for the service registry schema, the credential-loading flow, and the `default_headers` merge precedence that the no-auth path reuses.

## Connected-Services Gate

A plugin normally contributes a `connected_services` key that gates its skills (`SystemSkill.requires`) and tools (`PluginTool.requires_service`) on the server being configured AND the user being connected. With neither a credential schema nor a user connection, `get_user_connected_services()` reports `sec_edgar` as **always true**, and the skill does not rely on it anyway: it is registered with `requires=None`, so it is visible and loadable for every user regardless of connection state, exactly as the core `system:federal_register` skill is.

The one place an always-true key would mislead is `GET /me`'s `has_any_service_connected`, which the frontend uses for the first-run "nothing connected yet, open Settings" nudge. `chat/routes/user.py` therefore skips the ids returned by `config.plugins.connectionless_plugin_ids()` (loaded plugins with neither a `credential_schema` nor a `user_connection`) alongside the capability pseudo-keys, so a fresh user still sees the nudge with the plugin loaded.

## Service Entries (via `authed_get`)

SEC EDGAR reads use `authed_get` with the full API URL. The two entries in `plugins/sec_edgar/manifest.py` gate access via `allowed_endpoints` regex validation -- requests to non-matching paths are rejected. See that file for the exact pattern list and `plugins/sec_edgar/instructions.md` for the LLM-facing documentation (base URL, key paths, the parallel-arrays layout of `filings.recent`, and example invocations). Neither entry defines `allowed_post_endpoints`, so `authed_post` rejects every POST to both hosts.

### Hosts and Path Gating

Both entries use a **plain hostname key with no `path_prefix`**. Unlike Federal Register (whose entire JSON surface lives under `/api/v1`), `data.sec.gov` serves multiple distinct path roots (`/submissions/`, `/api/xbrl/...`), so path scoping is done entirely by the `allowed_endpoints` regexes rather than a prefix.

**`data.sec.gov`** (submission + XBRL data) allow-lists, with CIK as a 10-digit zero-padded value (`CIK0000320193`):

- `/submissions/CIK##########.json` -- primary entity filing history.
- `/submissions/CIK##########-submissions-NNN.json` -- older-filings spillover for large filers.
- `/api/xbrl/companyconcept/CIK##########/<taxonomy>/<tag>.json` -- one concept's time series for one entity.
- `/api/xbrl/companyfacts/CIK##########.json` -- every XBRL fact for one entity (large).
- `/api/xbrl/frames/<taxonomy>/<tag>/<unit>/CY####[Q#][I].json` -- one concept+unit across all entities for a period (`I` = instantaneous/point-in-time).

**`www.sec.gov`** is restricted to **exactly the two static ticker→CIK map files** and nothing else:

- `/files/company_tickers.json`
- `/files/company_tickers_exchange.json`

The rest of `www.sec.gov` (the human-facing site, `/cgi-bin/browse-edgar`, other `/files/...`) is **not proxied** -- the `allowed_endpoints` regexes match only those two paths. The `efts.sec.gov` full-text search host is **not** integrated at all (see Design Decisions).

### Ticker → CIK Resolution Workflow

Every `data.sec.gov` path requires a CIK (Central Index Key), not a ticker. To let the agent resolve a stock ticker to a CIK without leaving `authed_get`, the two SEC ticker→CIK map files on `www.sec.gov` are narrowly allow-listed.

The map files are large (thousands of entries), so the skill instructions direct the model to fetch them with `output_file` (written under the hidden `.responses/` workspace subdirectory), then read/search the file via the receipt's `.responses/...` path and **zero-pad** the resulting integer `cik_str` to 10 digits to build the `data.sec.gov` URL.

See `plugins/sec_edgar/instructions.md` for the file shapes and the workflow detail. If the CIK is already known, the map fetch is skipped.

### CIK Zero-Padding

The `allowed_endpoints` regexes for `data.sec.gov` require the CIK to be exactly 10 digits (`CIK\d{10}`), so a non-padded value like `CIK320193` is rejected at the allow-list. The `cik_str` field in the map files is an unpadded integer; the model must left-pad it with zeros to 10 digits itself.

### Large Responses

The `companyfacts` payload and busy-filer `submissions` payloads vastly exceed the ~3 KB `authed_get` size gate and return `response_too_large` if fetched inline. EDGAR endpoints do **not** support a `fields[]`-style trimmer, so `output_file` (which writes the body under the hidden `.responses/` workspace subdirectory and bypasses the size gate) is the primary tool for large payloads; the skill also directs the model to prefer `companyconcept` (one metric) over `companyfacts` (everything) when only one value is needed. See [Large Response Protection](../../../docs/architecture/gemini-api.md#large-response-protection).

## Skill

`system:sec_edgar` (`plugins/sec_edgar/instructions.md`), ungated: registered through the plugin's `system_skills` with `requires=None`, so it is enumerated in the always-on system-skill bullet list for every user and loaded on demand via `load_skills(skill_ids=["system:sec_edgar"])`. See [Skill Library Architecture -- System Skills](../../../docs/architecture/skill-library.md#system-skills). The core `authed_get` tool description and the system prompt name SEC EDGAR only as an example of a plugin-registered host; the endpoint detail lives in the skill.

## Constraints

- **Read-only:** only the GET paths on the `allowed_endpoints` lists are reachable; there is no POST allow-list and there are no SEC EDGAR write operations.
- **Path-gated by regex:** only the allow-listed `data.sec.gov` paths and the two `www.sec.gov` map files are proxied; all other paths on both hosts are rejected. `efts.sec.gov` is not registered.
- **No authentication:** no credentials, connection, admin card, or Settings entry; the skill is always available.
- **`User-Agent` required:** SEC returns HTTP 403 without a descriptive `User-Agent`; the server injects one via `default_headers` on both entries.
- **CIK format:** the CIK must be 10-digit zero-padded in `data.sec.gov` paths (`CIK0000320193`, not `CIK320193`).
- **Size gate:** responses over the ~3 KB `authed_get` size gate (notably `companyfacts` and busy-filer `submissions`) must be written to the hidden `.responses/` workspace subdirectory with `output_file`; EDGAR has no `fields[]`-style trimmer.
- **Rate limit:** SEC's fair-access policy caps usage at 10 requests/second. This is documented in the skill but **not enforced** in code.
- **Not in public projects:** plugin services are blocked there like every other plugin surface (`authed_get` is not in the public tool tier at all).
- **Removable:** deleting (or not shipping) the plugin directory removes both hosts and the skill; nothing in core references them.

## Design Decisions

**Why a plugin for a credential-free API?**
The integration is two registry entries and a skill -- exactly what the plugin manifest's `services` + `system_skills` fields carry. Packaging it keeps the core `_SERVICE_REGISTRY` and skill catalog free of integration-specific entries, lets a deployment drop the integration by removing a directory, and gives the suite an in-tree example of the credential-free shape (the Tailscale plugin already proved the services-only shape with a per-user key).

**Why no `connected_services` gate on the skill?**
There is no connection state to gate on, so `requires="sec_edgar"` would merely pin the skill to a key that is always true. `requires=None` states the intent directly and matches the core Federal Register skill; the key still exists (always true) so `PluginTool.requires_service`-style gating would also work if the plugin ever grew tools.

**Why does `GET /me` skip connectionless plugins?**
`has_any_service_connected` drives the frontend's first-run nudge to open Settings. Counting an always-true key would silence it for every user of every deployment. The skip is keyed on the manifest shape (`plugin_is_connectionless()`: no credential schema and no user connection) rather than on the plugin id, so any future credential-free plugin behaves the same way.

**Why `authed_get` instead of proxy endpoints or tools?**
SEC EDGAR reads are standard public API GET requests. Using `authed_get` avoids dedicated proxy endpoints, reuses the shared `allowed_endpoints` path gating and large-response protection, and aligns SEC EDGAR with the same pattern used by Google services, Airtable, GitHub, Tailscale, and Federal Register.

**Why a no-auth service instead of skipping credential handling?**
`_make_authed_request()` always loads credentials and invokes `inject_auth`. Rather than branch the shared code path for an unauthenticated host, the service supplies a `None`-returning loader and a no-op injector and leaves `requires_user` unset, so the generic flow runs unchanged and the `None` credential never trips the missing-credentials guard.

**Why a plain hostname key (no `path_prefix`)?**
Unlike Federal Register's single `/api/v1` JSON surface, `data.sec.gov` serves multiple distinct path roots (`/submissions/`, `/api/xbrl/...`). A single prefix would not cover them, so the entry uses a bare hostname key and relies entirely on the `allowed_endpoints` regexes for path gating.

**Why allow-list `www.sec.gov` to only two files?**
The submission/XBRL calls require a 10-digit CIK, but the only public ticker→CIK mapping lives in two static JSON files on `www.sec.gov`. Narrowly allow-listing exactly those two files lets the agent resolve a ticker to a CIK itself, while keeping the rest of `www.sec.gov` (the human site and other paths) unreachable.

**Why inject `User-Agent` via `default_headers` instead of a custom injector?**
SEC's `User-Agent` requirement is a static header, not a per-request credential. The existing `default_headers` merge already injects static headers below caller headers in precedence (as GitHub and Federal Register do), so reusing it avoids adding a bespoke injector.

**Why is `efts.sec.gov` (full-text search) not integrated?**
The EDGAR full-text search host was out of scope for this integration, which covers structured submission and XBRL data. It is a possible future extension: a third no-auth service entry in the plugin's manifest following the same pattern, with its own `allowed_endpoints` and the SEC `User-Agent` default header.
