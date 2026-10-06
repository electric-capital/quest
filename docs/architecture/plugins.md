# Plugin Architecture

## Overview

Quest packages upstream API integrations as self-contained plugins: directories under `plugins/` (repo root) discovered from the filesystem at startup. A fork adds a proprietary integration by committing a directory into `plugins/` — no packaging, no entry points, no registration edits in core files, and no frontend changes (all plugin config UI is schema-driven from the backend).

Reference plugins prove the surface: the in-tree GitHub (`plugins/github/`, plugin id `github`) — the first **`oauth`-kind** user connection, packaging the OAuth flow router, an `authed_get` service entry, the `system:github` skill, the `github_get_job_log` tool, and three approval-gated action requests (run a workflow, comment on an issue / PR, close or reopen an issue). An out-of-tree plugin (loaded via `QUEST_PLUGIN_PATH`) can also demonstrate the **no-user-connection** shape: server credentials only, with per-user identity resolved server-side from the existing Google login.

Another, Microsoft 365 (`plugins/m365/`, plugin id `m365` — see [m365-mail-api.md](../../plugins/m365/docs/m365-mail-api.md)), is the oauth-kind shape with **expiring tokens**: the credential loader proactively refreshes near-expiry Graph tokens and persists the rotated refresh token (the Ramp pattern on the GitHub plugin shape).

Twitter/X (`plugins/twitter/`, plugin id `twitter` — see [twitter-api.md](../../plugins/twitter/docs/twitter-api.md)), follows the M365 expiring/rotating-token shape and adds the repo's only **PKCE** OAuth flow plus a grandfathered unprefixed action-request type (`send_twitter_dm`, persisted in pre-plugin `action_requests` rows).

Twilio SMS (`plugins/twilio/`, plugin id `twilio` — see [twilio-api.md](../../plugins/twilio/docs/twilio-api.md)) shows that the oauth-kind connection is really a **plugin-owned router** shape: there is no OAuth provider, the `/auth/twilio` router runs a phone-number verification flow (type number → texted code → type code) from a plugin-served popup page, stores the verified number in `oauth_blob`, and adds `GET/PUT /auth/twilio/templates` for the user's pre-written messages; its admin card carries a `bool` **trusted channel** field that gates what the no-approval self-SMS tool may send.

UniFi (`plugins/unifi/`, plugin id `unifi` — see [unifi-api.md](../../plugins/unifi/docs/unifi-api.md)) reuses that router shape for a **multi-key paste flow**: UniFi issues one API key per application (Network, Protect) and the generic `api_key` kind stores a single secret, so the `/auth/unifi` popup collects one or both keys, verifies each against the admin-configured controller host, and stores them together in `oauth_blob`; it is also the first plugin whose upstream host is admin-configured at runtime (a static-hostname `services` entry cannot express it, so everything is `tools`).

Iru (`plugins/iru/`, plugin id `iru` — see [iru-api.md](../../plugins/iru/docs/iru-api.md)) is the first in-tree **`api_key`-kind** user connection: the admin card holds only the tenant API origin (runtime-configured like UniFi, so again all `tools`), each user pastes their own permission-scoped Iru API token through the generic `/auth/service-key/iru` routes, and the fifteen read-only tools resolve free-form device / blueprint / user references to UUIDs and return compact rows; the write side (device actions as action requests) is not built yet.

Telegram (`plugins/telegram/`, plugin id `telegram` — see [telegram-api.md](../../plugins/telegram/docs/telegram-api.md)) hosts a third kind of non-OAuth login on the plugin-owned router shape: Telegram's own protocol (phone → code sent to the app → optional cloud password) run by Telethon, with the in-flight login parked server-side in `oauth_blob.pending` and the authorized `StringSession` stored as `oauth_blob.session` (migration `d7a1f3c9e2b4` moved it out of the dropped `users.telegram_session` column, re-encrypting under the new column label).

It is also the first plugin with an **`on_shutdown`** hook, because its `TelegramClientManager` keeps one Telethon client connected per user for the life of the process, and it carries the grandfathered `send_telegram_message` action type.

Google Workspace Admin (`plugins/google_admin/`, plugin id `google_admin` — see [google-admin-api.md](../../plugins/google_admin/docs/google-admin-api.md)) is the first plugin to **borrow a core credential**: it has no OAuth client of its own, its oauth-kind router runs a second, separately scoped Google grant against the core `google_oauth` client (read-only Admin SDK scopes that only Workspace administrators can use, so they stay out of the Google Services scope list), its admin card is a single `enabled` switch whose `is_configured` also requires the borrowed client, and its agent surface is two GET-only `authed_get` service entries on one host (the Directory API, and the Reports API told apart by `path_prefix`), four read-only Google Meet tools that page and aggregate Reports records too large for the inline size gate, and a skill -- no action requests. Its id is also the first with an underscore, which is why oauth routers live under `plugin_auth_prefix()` (`/auth/google-admin`): the id's underscores are written as hyphens in the URL namespace and the connector row's connect URL.

Tailscale (`plugins/tailscale/`, plugin id `tailscale` — see [tailscale-api.md](../../plugins/tailscale/docs/tailscale-api.md)) is the **services-only** shape (GET-only `authed_get` entries plus a skill -- no tools, no action requests) on the generic `api_key` connection, and is the first plugin with **no admin card**: `api.tailscale.com` is a fixed public host and every credential is the user's own, so it declares no `credential_schema` and `plugin_server_available()` treats it as always available. Its one pasted secret may be an API access token (used verbatim) or an OAuth client secret, which the credential loader exchanges for a short-lived access token via the client-credentials grant and caches per user -- the one place the api_key kind carries a derived, expiring credential.

Finally, Slack (`plugins/slack/`, plugin id `slack` — see [slack-api.md](../../plugins/slack/docs/slack-api.md)) is the deepest core migration: the GitHub oauth-kind shape plus **both** grandfather lists (`unprefixed_action_types` for `send_slack_message` / `send_slack_dm`, and `unprefixed_tools` for the nine infix-named tools like `list_slack_teams` baked into transcripts, sandbox scripts, and skill prose) and the sole `_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` exemption (`send_slack_dm_to_self` stays in the core public-project allowlist).

The Slack-*driven* conversation machinery (Socket Mode worker, `slack_conversations` table, `slack_reply` wait handles — see [slack-socket-mode.md](slack-socket-mode.md)) deliberately stays core: the plugin contract has no lifespan/suspend/tool-tier extension points.

## Key Files

- `config/plugin_types.py` — the manifest dataclasses (`QuestPlugin`, `CredentialField`, `UserConnectionSpec`, `PluginTool`); stdlib-only so manifests import without FastAPI
- `config/plugins.py` — filesystem discovery (`plugins/*/plugin.py`), validation, registry fan-out (`load_plugins()`, `get_loaded_plugins()`, `plugin_server_available()`)
- `plugins/README.md` — the hands-on authoring guide (layout, rules, gitignore note)
- `plugins/github/` — reference plugin, oauth kind (see [github-api.md](../../plugins/github/docs/github-api.md))
- `plugins/slack/` — in-tree plugin, oauth kind with both grandfather lists and the public-allowlist exemption (see [slack-api.md](../../plugins/slack/docs/slack-api.md))
- `plugins/twitter/` — in-tree plugin, oauth kind with expiring/rotating tokens + PKCE (see [twitter-api.md](../../plugins/twitter/docs/twitter-api.md))
- `plugins/twilio/` — in-tree plugin, oauth-kind router hosting a non-OAuth phone-verification flow + trusted-channel policy (see [twilio-api.md](../../plugins/twilio/docs/twilio-api.md))
- `plugins/unifi/` — in-tree plugin, oauth-kind router hosting a two-key (Network + Protect) API-key paste flow against an admin-configured controller host; read-only tools incl. live camera snapshots (see [unifi-api.md](../../plugins/unifi/docs/unifi-api.md))
- `plugins/iru/` — in-tree plugin, api_key kind against an admin-configured tenant API URL; fifteen read-only Iru (Kandji) MDM tools, no action requests yet (see [iru-api.md](../../plugins/iru/docs/iru-api.md))
- `plugins/telegram/` — in-tree plugin, oauth-kind router hosting Telegram's own (Telethon) login with server-side pending state, four read tools, the grandfathered `send_telegram_message` action request, and the `on_shutdown` hook closing its long-lived clients (see [telegram-api.md](../../plugins/telegram/docs/telegram-api.md))
- `plugins/google_admin/` — in-tree plugin, oauth kind that borrows the core Google OAuth client (admin card is a single `enabled` switch) for a separate read-only Workspace admin grant; two GET-only `authed_get` service entries (Admin SDK Directory API, and the Reports API scoped to the Meet audit logs + Meet usage), four read-only `google_admin_meet_*` tools (calls, call quality, Meet hardware, usage) and a skill (see [google-admin-api.md](../../plugins/google_admin/docs/google-admin-api.md))
- `plugins/tailscale/` — in-tree plugin, api_key kind with no admin card; a personal API access token or an exchanged-and-cached OAuth client secret feeding one GET-only `authed_get` service entry (Tailscale API v2) and a skill, no tools (see [tailscale-api.md](../../plugins/tailscale/docs/tailscale-api.md))
- `plugins/_example/` — single-file smoke-test fixture exercising every manifest field (loaded only by tests)
- `quest.py` — calls `load_plugins()` after core registries are populated, then `mount_plugin_oauth_routers(app)` (oauth-kind connection routers, before the SPA catch-all); the lifespan's shutdown phase awaits `shutdown_plugins()`

## Discovery and loading

At startup `load_plugins()` scans `plugins/*/plugin.py` (skipping `_`/`.`-prefixed directories), imports each under a synthetic module name, calls its exported `get_plugin() -> QuestPlugin`, validates the manifest, and fans it out into the core registries. A plugin that raises on import, fails validation, or errors during registration is logged and skipped — a broken plugin never prevents boot.

Registration is atomic. `register_plugin()` first preflights the manifest against the live registries (`_preflight_registration`: credential service id, `authed_get` keys, skill ids, action-request types, tool names) and rejects a collision in one error naming every conflict, with nothing mutated. Each fan-out step that then succeeds records its inverse in a per-plugin undo journal (`_REGISTRATIONS`), and a failure at any later step replays the journal before re-raising, so a skipped plugin leaves no partial surface — no orphan admin credential card, store-roster entry, service, skill, or tool — and a corrected manifest registers without stale collisions. Every undo removes only what its own step added (the add functions refuse duplicates, so a step that succeeded owns its entry), which is what keeps a rollback or `unregister_plugin()` from ever touching a core entry or another plugin's. Plugin tools go through `register_dynamic_tool(spec, handler)` in `chat/gemini_api/tool_dispatch.py`, the one registration surface for a tool_call spec/handler pair: the handler step failing removes the just-added spec, so `TOOL_CALL_REGISTRY` and `TOOL_CALL_HANDLERS` cannot diverge for plugin tools (core tools stay in their two literal tables, pinned by the parity test in `tests/test_tool_dispatch_table.py`).

Extra search roots can be supplied via the **`QUEST_PLUGIN_PATH`** environment variable: an `os.pathsep`-separated (`:` on Linux) list of directories, each traversed exactly like the in-tree `plugins/` dir (`<root>/*/plugin.py`, same hidden-dir skip and validation). The in-tree dir scans first, then the listed roots in order, so on a duplicate plugin id the in-tree plugin wins and the later duplicate is skipped like any broken plugin; entries that aren't directories log a warning and are skipped; blank/repeated entries are dropped.

External plugins are imported as standalone modules (a directory name colliding with one from another root gets a path-hash-disambiguated module name) — unlike in-tree plugins they can't use `plugins.<name>.*` package imports, so a multi-file external plugin manages its own imports (e.g. extends `sys.path` from its `plugin.py`).

Plugin-specific tests ship inside the plugin (`plugins/<id>/tests/`; out of tree, `<root>/<id>/tests/`): pytest's `testpaths` collects the in-tree suites, and on a bare `uv run pytest` the repo-root `conftest.py` also collects test directories under the `QUEST_PLUGIN_PATH` roots. Suites register/unregister their plugin per test via the `tests/plugin_support.py` fixture factory and `unregister_plugin()` (the maintained inverse of `register_plugin()`) — see [Development Workflows](../setup/development-workflows.md).

Validation enforces:

- unique id (not colliding with a core credential-store service name, nor with a connected-services pseudo-key in `PSEUDO_SERVICE_KEYS` from `api/instructions.py` -- today `docs`, the [Quest Docs](quest-docs.md) feature gate, which would otherwise overwrite the plugin's connected state),
- `<id>_`-prefixed action-request type and tool names (except the `unprefixed_action_types` / `unprefixed_tools` grandfather lists — see below),
- `system:<id>` skill ids plus the system-skill catalog's static checks (`validate_system_skill_definition` — e.g. the ≤120-char description limit — run at validation time so a bad skill fails manifest validation, not just startup registration; only the duplicate-skill-id check stays registration-only),
- `script_tool_allowlist` ⊆ own tools,
- tool names absent from the public-project allowlist (except the core-owned `_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` per-plugin exemptions in `config/plugins.py`), and
- well-formed `user_connection`/`services` entries.

For an oauth-kind `user_connection` it additionally requires an `oauth_router` whose every route path lives under the plugin's `/auth/<id>` namespace (`plugin_auth_prefix()`: the id's underscores are written as hyphens, so `google_admin` owns `/auth/google-admin`), rejects the api_key-only fields (`validate_key`, `key_placeholder`, `key_help`, `key_help_links`), and checks `scopes`/`needs_reauth` shapes; an api_key-kind spec must not set `oauth_router`.

## Manifest fan-out

| Manifest field | Registered into |
|---|---|
| `credential_schema` + `is_configured` + `credential_validate` | admin Service Credentials card + store roster (`config/service_specs.py: register_plugin_credentials`) |
| `user_connection` | `/connectors` row + `connected_services` gate + `user_service_credentials` storage; `api_key` kind: generic `/auth/service-key/<id>` key routes (key in `secret`); `oauth` kind: the plugin's `oauth_router` mounted under `/auth/<id>` (token JSON in `oauth_blob`, connect URL convention `/auth/<id>?popup=1`, optional `needs_reauth` hook for the re-authorize badge) |
| `services` | `authed_get`/`authed_post` `_SERVICE_REGISTRY` |
| `system_skills` | system-skill catalog (`register_system_skill`) |
| `action_request_handlers` | action-request handler registry + `create_action_request` type enum |
| `tools` | `TOOL_CALL_REGISTRY` + dispatch table + gated system-prompt enumeration (`requires_service`); `PluginTool.mutating=True` marks an approval-free tool that changes external state (self-DM / self-SMS sends, mail drafts, archiving) -- stamped as `spec["mutating"]`, such tools are refused in one-shot inference API runs (see [inference-api.md](../api/inference-api.md)) |
| `script_tool_allowlist` | sandbox-script bridge (`POST /api/tool-call`) |
| `post_load` | optional zero-arg startup hook run by `load_plugins()` right after the plugin registers (plugin-owned one-time work, e.g. a legacy credential-store migration); a failing hook is logged, the plugin stays loaded; test fixtures that register a manifest directly do not invoke it |
| `on_shutdown` | optional zero-arg shutdown hook (a plain function or a coroutine function) run by `shutdown_plugins()` from the quest.py lifespan's shutdown phase, in reverse load order (plugin-owned teardown, e.g. the Telegram plugin closing its long-lived Telethon clients); a failing hook is logged and the remaining hooks still run; test fixtures do not invoke it |

## Design constraints

- **No plugin `/api/*` HTTP routes.** A mounted `/api/*` route would be LLM-reachable via `curl_proxy_get` with no gating, size gate, or allow-listing. Bespoke upstream shapes (GraphQL proxies, runtime-configured hosts, non-HTTP protocols) live in `tools` handlers instead. The one routing exception is the oauth-kind connection's `oauth_router`: browser-facing, session-cookie-authed routes that loader validation confines to the plugin's `/auth/<id>` namespace, mounted by `mount_plugin_oauth_routers()` — matching the core OAuth connector flows, not an LLM surface.
- **No plugin frontend code.** The FE is one eagerly-built bundle; plugin config UI renders from the `CredentialField` schema and the generic `/connectors` row shape. The one in-tree exception is the Twilio plugin's Settings > SMS Messages editor (`SmsMessagesSection.tsx`, gated on the `twilio` connector row being available): a per-user *authoring* surface with no generic schema equivalent, kept in core the way Settings > Gmail's label list is.
- **No plugin DB columns.** Per-user credentials live in the generic `user_service_credentials` table (one row per user+service).
- **Public projects always block plugin tools and services**; not configurable by plugins. The one carve-out is core-owned: `_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` in `config/plugins.py` maps a tool name already in the core `PUBLIC_TOOL_CALL_ALLOWLIST` to the single plugin id allowed to serve it (today only `send_slack_dm_to_self` → `slack`) — the allowlist itself stays a core decision.
- **Combined availability gate**: a plugin's `connected_services` key is true only when the server side is configured (`is_configured` over the stored admin config) AND the user's stored credential row satisfies `user_connection.connected`. Gate skills via `SystemSkill.requires=<id>` and tool advertisement via `PluginTool.requires_service=<id>`.
- **Grandfathered type names**: `QuestPlugin.unprefixed_action_types` exempts listed action-request type names from the `<id>_` prefix rule. It exists only for core integrations migrated into plugins whose type names are persisted in old `action_requests` rows (the Twitter/X plugin's `send_twitter_dm`; the Slack plugin's `send_slack_message` / `send_slack_dm`); new plugins must use prefixed names. `QuestPlugin.unprefixed_tools` is the same escape hatch for tool names baked into persisted transcripts, sandbox scripts calling `POST /api/tool-call`, and skill prose (the Slack plugin's `list_slack_teams`, `send_slack_dm_to_self`, ...).

## Adding a proprietary integration in a fork

1. Create `plugins/<your_id>/plugin.py` exporting `get_plugin()`; follow the layout in `plugins/README.md` (data files like `instructions.md` next to the code, handlers in a subpackage imported via the normal `plugins.<your_id>.*` package path).
2. Remove/override the `plugins/*` gitignore lines in the fork and commit the directory.
3. Declare a `credential_schema` for the admin card and/or a `user_connection` for the per-user Data Connections row (`api_key` for pasted keys, `oauth` with an `/auth/<your_id>`-prefixed router for an OAuth flow — copy `plugins/github/oauth.py`, which reuses the shared popup pages in `auth/popup_helpers.py` and stores the granted token via `upsert_credential(..., oauth_blob=...)`); gate your skill/tools on the plugin id.
4. Restart: the loader logs `Loaded plugin '<id>' ...` and every surface (admin card, connector row, skill, tools, action-request types) appears with no core edits.

## Design Decisions

**Filesystem scan over Python entry points.** Entry points require an installed distribution, which fights "fork the repo and drop a directory in".

**Skip-and-log on failure, with rollback.** A broken plugin must never take down the server. Registration used to be a plain sequence of registry mutations, leaving a failed plugin's earlier entries in place (an admin credential card and `authed_get` service for a plugin absent from the loaded list, a skill describing tools that never registered); the undo journal in `register_plugin()` now rolls those back, so "skipped" means "absent", and the same journal is what `unregister_plugin()` replays for test fixtures.

**Core integrations migrate onto the same registration APIs.** There are no "two classes of integration"; the GitHub migration proved the oauth-kind user connection, the Twitter/X migration packaged the first grandfathered action-request type plus the rotating-refresh-token OAuth shape, the Slack migration packaged a whole tool family + action requests + public-allowlist tool while leaving the Slack-driven runtime core, and the Telegram migration added the `on_shutdown` lifecycle hook so a plugin can own a long-lived upstream connection end to end. Remaining core services (ramp, airtable, …) can follow the same templates.
