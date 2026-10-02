# Google Workspace Admin API

## Overview

Read-only access to a Google Workspace account's directory (users, groups, org units, domains, custom user schemas, rooms and buildings), device inventory (ChromeOS and mobile devices), and Google Meet activity (calls, per-participant call quality, Meet hardware room devices, usage statistics), packaged as the in-tree `plugins/google_admin` plugin (plugin id `google_admin`; see [Plugins](../../../docs/architecture/plugins.md)). Setup in [Google Workspace Admin Setup](google-admin-setup.md).

Two surfaces, both guided by the `system:google_admin` skill, and no action requests:

- **Directory and devices:** plain Google API GETs the model composes through `authed_get` against the plugin's Directory service entry. The user-centred use case drives the surface: a person's profile and security state, the groups they belong to, their org unit, and the devices they use.
- **Google Meet:** four read-only `google_admin_meet_*` tools over the Admin SDK Reports API (see Meet Tools), plus a second, path-scoped `authed_get` entry for raw Reports reads.

## Connection

A per-user OAuth connection that is **separate from Google Services** (the core Gmail/Drive/Calendar connector), although it borrows the same Google OAuth client:

- The admin scopes are useless to anyone who is not a Workspace administrator, so they are not in `GOOGLE_SERVICE_SCOPES` (`auth/config.py`), where adding them would flag every user for re-consent. Only the people who connect this row are asked for them.
- The authorizing Google account does not have to be the Quest login. Administrators commonly hold a dedicated admin account, so the flow shows the account picker and records the connected account in `oauth_blob.account` rather than failing closed on a mismatch the way `auth/google_services.py` does.
- The authorize request omits `include_granted_scopes`, so the stored token carries only the read-only admin scopes and never inherits the write-capable scopes of the same user's Google Services grant.

Router: `plugins/google_admin/oauth.py`. Its URL namespace is `/auth/google-admin` -- the plugin id with the underscore written as a hyphen, per `plugin_auth_prefix()` in `config/plugins.py`, matching the core `/auth/google-services` -- so the connect URL is `/auth/google-admin?popup=1` and the callback `/auth/google-admin/callback`. It uses the signed session-bound state cookie from `auth/oauth_state.py` and the shared popup pages. Token JSON lives in `user_service_credentials.oauth_blob` (service `google_admin`): access token, refresh token, `expires_at`, the granted `scope`/`scopes`, `account`, `authorized_at`.

`get_google_admin_token()` in `plugins/google_admin/upstream.py` refreshes within 5 minutes of expiry under a per-user asyncio lock and persists the new access token. Google does not rotate refresh tokens, so the stored one is carried over. A failed refresh returns `None`, which surfaces as the `google_admin_oauth_required` reconnect error.

The `needs_reauth` hook compares the granted scopes against `GOOGLE_ADMIN_SCOPES`, so a scope unticked on Google's consent screen, or a later widening of the scope list, shows the "Update Available" badge on the connector row.

Disconnecting deletes the stored row but does not call Google's revoke endpoint: a revoke removes the user's whole grant to the OAuth client, which would also end the Google Services connection of the same account.

## Scopes

Defined in `GOOGLE_ADMIN_SCOPES` (`plugins/google_admin/upstream.py`); every one is a `.readonly` variant, asserted by `test_scopes_are_all_read_only`. Besides the Directory scopes, the Meet support adds `admin.directory.resource.calendar.readonly` (rooms, buildings, features), `admin.reports.audit.readonly` (the Meet and Meet hardware audit logs) and `admin.reports.usage.readonly` (Meet usage). The two Reports scopes cover every application's audit log and usage report at the token level; the service allow-list and the tools confine reads to Meet. Widening the list flags connections made before it with the `needs_reauth` "Update Available" badge, and the Meet tools check the recorded grant first (`has_granted_scope()`), answering `google_admin_reconnect_required` without a round trip.

Two scopes are deliberately not requested:

- `admin.directory.user.security` (third-party app grants, app passwords, backup verification codes): Google offers no read-only variant of it.
- `cloud-identity.devices.readonly` (the Cloud Identity Devices API): Google refuses to put it on a user consent screen and rejects the whole authorize request with `Error 400: invalid_scope` ("Some requested scopes cannot be shown"). See Design Decisions.

What an account can actually read is still decided by Google: the calls succeed only for the resources the connected account's admin role covers.

## Service Entries (via authed_get)

Two entries on `admin.googleapis.com`, declared in `plugins/google_admin/manifest.py`, each with the plugin's own credential loader and injector, `requires_user`, and the `google_admin_oauth_required` missing-credentials error. Neither defines `allowed_post_endpoints`, so `authed_post` rejects every POST to the host.

**Directory** (key `admin.googleapis.com`, the Admin SDK Directory API under `/admin/directory/v1`). Reachable (GET only): users and their aliases; groups, aliases, members and membership checks; org units; domains and domain aliases; custom user schemas; the customer record; ChromeOS devices (list, get, count); mobile devices (list, get); calendar resources (rooms), buildings and room features (list, get).

Id segments use `[^/:]+`, which excludes Google's `:`-encoded custom methods; the one custom method allowed, the read-only `chromeos:countChromeOsDevices`, is spelled out. Org unit paths are the exception: they are multi-segment and the id form contains a colon (`id:...`), so that pattern accepts any number of segments but refuses dot segments, literal or percent-encoded.

Not allow-listed although Google serves them as GETs: `users/{key}/tokens`, `/asps` and `/verificationCodes` (the last returns live backup codes), user photos, admin roles and role assignments, printers, and ChromeOS command status.

**Reports** (key `admin.googleapis.com/admin/reports/v1` with `path_prefix` `/admin/reports/v1`, so `_find_service()` routes Reports paths to it before the host-level Directory entry). Reachable: `activity/users/{all|user}/applications/meet` and `.../meet_hardware` (audit records) and `usage/dates/{YYYY-MM-DD}` (customer usage report; the skill tells the model to always pass `parameters=meet:...`). Not reachable: every other application's audit log (login, admin, token, Drive, SAML, ...), user and entity usage reports, and the `watch` push channel. A raw `call_ended` record is about the size of the 3 KB inline gate on its own, so the skill sends raw reads to `output_file`.

## Meet Tools

`plugins/google_admin/tools.py` (handlers + specs, `requires_service="google_admin"`, all four in the plugin's `script_tool_allowlist`), `plugins/google_admin/reports.py` (Reports API client: `require_token()` scope pre-check, newest-first paging with a page cap and a `truncated` flag, partial-response `fields`, error mapping to `GoogleAdminError` codes `google_admin_oauth_required` / `google_admin_reconnect_required` / `google_admin_forbidden` (missing Reports privilege) / `google_admin_api_disabled` / `google_admin_rate_limited` / `google_admin_bad_request` / `google_admin_upstream_error`), and `plugins/google_admin/meet.py` (pure shaping):

| Tool | Source | Output |
|------|--------|--------|
| `google_admin_meet_calls` | `meet` / `call_ended`, filtered upstream | conferences newest first: code, organizer, start (earliest join = leave time minus `duration_seconds`) / end, participant and external counts, device mix, Meet hardware present, verdict counts, worst endpoints |
| `google_admin_meet_call_quality` | same | one row per participant session, worst first, with a curated metric subset (`CORE_METRICS`, or all metrics + IP / calendar event / encryption with `include_all_metrics`), verdict and reasons; a summary over every scanned row (verdicts, by device type / transport, median / p90 / max of `SUMMARY_METRICS`, most common issues); the `thresholds` used |
| `google_admin_meet_hardware` | `meet_hardware` (all events) + `meet` / `call_ended` with `identifier_type==device_id` | per-device roster from `summarize_hardware()`: newest peripheral attach/detach state, missing/found, `in_call`, calls joined per platform, restarts, software updates, app load errors, feedback, `concerns`; call quality per device merged by device id, then display name (`merge_hardware_call_quality()`); optional event timeline |
| `google_admin_meet_usage` | `usage/dates/{date}` per Pacific-time day, 5 concurrent | daily `meet:` values, totals of the additive ones; a "not yet available" day is noted, a privilege / connection error aborts |

Call filters map to the Reports `filters` parameter (`param==value`, ANDed, only honoured with `eventName`): `conference_id`, `organizer_email`, `participant` -> `identifier`, `device_type`, and the pseudo type `meet_hardware` -> `identifier_type==device_id`. A meeting code (or meet.google.com link) is tried in each spelling from `meeting_code_variants()` -- the documented lowercase hyphenated form, then the compact upper and lower case forms -- until one matches, because the filter is an exact match and Google documents the format only by example.

The quality verdict (`assess_quality()`, `QUALITY_THRESHOLDS`) is a Quest heuristic, not a Google figure: fair / poor at audio packet loss 1 / 5 %, video packet loss 2 / 8 %, jitter 30 / 50 ms, round-trip time 150 / 300 ms, congestion 5 / 20 %; a `network_error` / `system_error` end reason makes an endpoint poor, a 1-2 rating fair, and no measured metric (dial-in, very short joins) `unknown`. Every quality result carries the thresholds so the model can explain a verdict.

Scans collect at most `CALL_SCAN_PAGES` (5) / `HARDWARE_SCAN_PAGES` (10) pages of 1000 records, newest first, then report `truncated` with `covered_from`. Windows are `days` (default 7) or `start_time` / `end_time`, at most 180 days back (Google keeps Meet and Meet hardware audit records for 6 months); usage ranges are at most 92 days, ending yesterday at the latest.

## Skill

`system:google_admin` (`plugins/google_admin/instructions.md`), gated on `google_admin` (server enabled AND user connected): the Directory path table with its query parameters (incl. rooms), the user / ChromeOS / mobile search syntaxes, a Google Meet section (which tool answers what, how to read verdicts, send vs receive metrics, truncation, masking of external participants, the Meet hardware coverage gap, matching devices to rooms by name, usage-report semantics, typical call sequences, and the raw Reports paths with the `filters` syntax), an "everything about one user" call sequence (profile, groups, mobile and ChromeOS devices), the `fields=` and `output_file` guidance for large pages, and how to read Google's 403 variants (missing admin privilege, API not enabled, insufficient scope).

## Key Files

- `plugins/google_admin/manifest.py` — manifest: the `enabled` admin switch, oauth user connection, the two service entries with their allow-lists, the Meet tools + script allowlist, the skill
- `plugins/google_admin/upstream.py` — scopes, server-config predicates, token blob and refresh, `has_granted_scope()`, `authed_get` loader and injector
- `plugins/google_admin/oauth.py` — OAuth router (start, callback, disconnect)
- `plugins/google_admin/reports.py` — Reports API client for the Meet tools (paging, error mapping)
- `plugins/google_admin/meet.py` — Meet record shaping: parameter flattening, quality verdict, conference / quality / hardware summaries, usage totals
- `plugins/google_admin/tools.py` — the four `google_admin_meet_*` tools: argument handling (windows, meeting-code spellings, filters) and specs
- `plugins/google_admin/instructions.md` — `system:google_admin` skill content
- `plugins/google_admin/tests/` — allow-list and request tests (`test_google_admin_services.py`), token and config tests (`test_google_admin_upstream.py`), OAuth flow tests (`test_google_admin_oauth_flow.py`), Meet shaping and tool tests against a faked Reports API (`test_google_admin_meet.py`)

## Constraints

- **Read-only, twice over:** the grant holds only `.readonly` scopes and the service entries allow GET paths only; the tools only issue GETs. No directory, device or Meet change is possible through this plugin.
- **Workspace administrators only:** a non-admin can complete the connection, but Google answers the calls with 403. The Meet tools need the Admin console **Reports** privilege; rooms need a privilege that can read calendar resources.
- **API must be enabled:** the Admin SDK API must be enabled in the GCP project that owns the Google OAuth client, or Google returns 403 `SERVICE_DISABLED`.
- **No laptops or desktops:** the Directory API only knows ChromeOS and mobile devices. Endpoint Verification devices (macOS, Windows, Linux) live in the Cloud Identity Devices API, which this connection cannot reach.
- **No Meet hardware inventory:** Google has no API listing Meet hardware or its live status, so `google_admin_meet_hardware` only knows devices that logged an event in the window, and peripheral / call states are the last ones logged. Meet hardware device ids changed in March-April 2026; older records carry the previous id.
- **Meet data lag:** a `call_ended` record lands a few minutes after the endpoint leaves, so calls in progress are invisible; usage reports lag 1-3 days and count only meetings organized inside the organization.
- **Size gate:** user and device pages exceed the `authed_get` response size gate unless trimmed with `fields=` or written to the workspace with `output_file` (see [Large Response Protection](../../../docs/architecture/gemini-api.md#large-response-protection)).
- **Not in public projects:** plugin services are blocked there like every other plugin surface.

## Design Decisions

**Why a plugin with its own connection instead of more Google Services scopes?**
Slides and GCP were added by extending `GOOGLE_SERVICE_SCOPES`, which makes every user re-consent. That is the wrong trade for scopes only administrators can use, and it would put directory-wide access on every user's consent screen. A separate connection keeps the grant opt-in and lets an administrator connect a dedicated admin account.

**Why reuse the core Google OAuth client instead of a second client?**
The deployment already has a Google OAuth client for sign-in and Google Services. A second client would mean a second consent-screen configuration to maintain for no isolation benefit: tokens are scoped per authorization, not per client. The cost is one more redirect URI on the existing client, which the admin `enabled` switch acknowledges.

**Why an `authed_get` service entry instead of dedicated tools for the directory?**
The Directory API is a fixed-host, GET-only REST API with server-side search, field trimming and paging, which is exactly what `authed_get` proxies. Dedicated tools would re-implement that surface. Tools become worthwhile for auto-paginated whole-directory exports, which are not built.

**Why dedicated tools for Meet, then?**
The Reports API cannot trim a record's `parameters` array with `fields=`, and one `call_ended` record carries ~70 parameters -- about the whole 3 KB `authed_get` inline limit -- so even "how was this one meeting" would need `output_file` plus `run_python`. The useful answers (a verdict per participant, a conference rollup, a device roster) need paging, flattening and aggregation, which is the response shaping the plugin guidelines put in tools. The raw Reports entry stays for the long tail (other Meet events, metrics the tools drop).

**Why derive Meet hardware from the audit log?**
There is no Meet hardware API: the Directory `chromeosdevices` resource, the Chrome Management telemetry API and the Cloud Identity Devices API do not cover Meet hardware (it carries its own license and Admin console list), and the Meet REST API has no device resources. The `meet_hardware` audit log (peripherals, presence, restarts, app load errors, calls joined on Meet / Zoom / Teams / Webex / SIP) plus the `call_ended` records of `device_id` endpoints is the only programmatic signal, so the tool reconstructs a roster from them and says that idle devices are invisible.

**Why not the Meet REST API (`meet.googleapis.com`)?**
It only returns conferences the authorizing user organized or joined (no admin-wide mode without domain-wide delegation), has no quality metrics or device types, and deletes records after 30 days; the Reports API's Meet audit log covers the whole organization for 6 months with the quality metrics.

**Why heuristic verdicts?**
Google publishes no per-call thresholds (the Admin console's Meet quality tool has no API). Common VoIP rules of thumb give the model a consistent first read; the raw metrics and the thresholds ride along in every result so the verdict can be explained or second-guessed.

**Why no Cloud Identity Devices API (laptops and desktops)?**
Its scopes cannot be granted through a user consent screen: with `cloud-identity.devices.readonly` in the request, Google answers `Error 400: invalid_scope` ("Some requested scopes cannot be shown") after the account is chosen, and a single unshowable scope fails the whole connection. The scope string itself is valid, so the failure only appears once a real account signs in. That API is reachable only through a service account with domain-wide delegation, which is a different trust model: a server-held key that can read the whole tenant for every Quest user, instead of each administrator's own grant. Adding it would be a separate, admin-configured credential rather than an extension of this connection.

**Why does the URL namespace use a hyphen when the plugin id has an underscore?**
Plugin ids are Python-identifier-shaped (`^[a-z][a-z0-9_]*$`) because they double as tool prefixes, skill ids and credential-store file names. URLs follow the core `/auth/google-services` style instead, so `plugin_auth_prefix()` writes the id's underscores as hyphens for the router namespace and the connector row's connect URL. Ids cannot contain hyphens, so no two ids share a namespace.
