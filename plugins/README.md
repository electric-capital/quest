# Plugins

This directory holds self-contained upstream-API integration plugins that are
discovered from the filesystem at server startup — no packaging, no entry
points, no registration edits in core files.

## What a plugin is

A plugin is a directory containing a `plugin.py` that exports a single
function:

```python
from config.plugin_types import QuestPlugin

def get_plugin() -> QuestPlugin:
    return QuestPlugin(id="acme_tracker", label="Acme Tracker", ...)
```

The manifest dataclasses live in `config/plugin_types.py`
(`QuestPlugin`, `CredentialField`, `UserConnectionSpec`, `PluginTool`).
The loader (`config/plugins.py`) scans `plugins/*/plugin.py` at startup
— plus any extra roots listed in the `QUEST_PLUGIN_PATH` environment
variable (`os.pathsep`-separated directories, traversed the same way;
the in-tree dir scans first so its plugins win duplicate-id conflicts;
note an out-of-tree plugin can't use `plugins.<name>.*` package imports,
so a multi-file one manages its own imports from its `plugin.py`) —
validates each manifest, and fans it out into the core registries:

| Manifest field | Registered into |
|---|---|
| `system_skills` | system-skill catalog (loadable via `load_skills`) |
| `action_request_handlers` | action-request handler registry + `create_action_request` type enum |
| `tools` | `TOOL_CALL_REGISTRY` + the tool_call dispatch table |
| `services` | `authed_get`/`authed_post` service registry |
| `script_tool_allowlist` | sandbox-script tool bridge (`POST /api/tool-call`) |
| `credential_schema` + `is_configured` (+ optional `credential_validate` normalization hook) | admin Settings > Service Credentials card (schema-rendered) + `data/service_credentials/<id>.json` store file + the generic `PUT /admin/service-credentials/<id>` endpoint |
| `user_connection` | per-user Data Connections row + a `connected_services` gate keyed by the plugin id; `api_key` kind: generic `POST /auth/service-key/<id>` key routes (placeholder overridable via `key_placeholder`); `oauth` kind: the plugin's `oauth_router` mounted under `/auth/<id>` at startup |
| `post_load` | optional zero-arg startup hook, run by `load_plugins()` right after registration — plugin-owned one-time work such as migrating a legacy credential location; failures are logged, the plugin stays loaded |
| `on_shutdown` | optional zero-arg shutdown hook (sync or `async`), run by `shutdown_plugins()` from the app lifespan's shutdown phase in reverse load order — plugin-owned teardown such as closing long-lived upstream client connections; failures are logged, the remaining hooks still run |

Plugins deliberately cannot mount `/api/*` HTTP routes: a mounted
`/api/*` route would be LLM-reachable via `curl_proxy_get` with no
service gating, size gate, or allow-listing. Anything a pass-through
`services` entry can't express (GraphQL proxies, runtime-configured
hosts, non-HTTP protocols, response shaping) belongs in a `tools`
handler — arbitrary Python behind the gated tool registry — with
sandbox-script access opted in via `script_tool_allowlist`. The one
routing exception is the oauth-kind user connection's `oauth_router`
(below): browser-facing, session-cookie-authed routes confined by
validation to the plugin's `/auth/<id>` namespace.

A plugin that declares a `credential_schema` gets a generic admin
Settings card automatically: the fields render from the schema
(`text` / `secret` / `bool` / `textarea`, with `required` / `required_if` /
`visible_if` honored), secrets are masked on read and keep-on-empty on
save, and `is_configured` drives the card's Configured badge. See
`config/service_specs.py`.

A plugin that declares a `user_connection` of kind `api_key` gets a
per-user surface automatically: a generic Data Connections row (key entry
+ disconnect), the `POST /auth/service-key/<id>` / `.../remove` routes
(key format checked by the optional `validate_key` hook), storage in the
`user_service_credentials` table, and a `<plugin id>` entry in
`get_user_connected_services()` that is true only when the server side is
configured (`is_configured` over the stored admin config) AND the
`connected` predicate accepts the user's stored credential row. Gate a
skill on it via `SystemSkill.requires=<plugin id>` and a tool's
advertisement via `PluginTool.requires_service=<plugin id>`. The row is
hidden from Data Connections while the server side is unconfigured.

A `user_connection` of kind `oauth` supplies an `oauth_router`
(FastAPI `APIRouter`) whose every route lives under `/auth/<plugin id>`
(enforced at load; an underscore in the id is written as a hyphen in the
URL, so plugin `google_admin` owns `/auth/google-admin`); quest.py mounts
it after plugin load. The Data
Connections row renders a Connect popup pointing at the
`/auth/<id>?popup=1` convention URL, the callback stores the granted
token JSON via `upsert_credential(user_id, "<id>", oauth_blob={...})`,
and the shared success/error popup pages live in
`auth/popup_helpers.py`. The optional `needs_reauth` hook (called with
the stored row) drives the row's "Update Available" re-authorize badge —
e.g. the github plugin compares the GRANTED scopes recorded at callback
time against its current `GITHUB_SCOPES`. The api_key-only fields
(`validate_key`, `key_placeholder`) are rejected on an oauth spec. See
`plugins/github/` for the reference implementation.

## Layout

```
plugins/
  acme_tracker/
    plugin.py            # must export get_plugin() -> QuestPlugin
    instructions.md      # LLM-facing skill content (data file)
    handlers/            # ActionRequestHandler modules
    docs/                # plugin-specific documentation (API reference,
                         # upstream app setup guide); indexed in the
                         # "Plugins" section of docs/index.md
    ...anything else the plugin needs
```

## Reference plugin

Nine in-tree implementations (proprietary integrations can live in
external repos and load via `QUEST_PLUGIN_PATH`):

- `plugins/github/` — the GitHub integration (plugin id `github`) with a
  credential schema (OAuth app client id/secret), an **oauth-kind**
  per-user connection (`oauth.py` router + granted-scope `needs_reauth`
  hook), the `api.github.com` `authed_get` service entry, the
  `system:github` skill, the `github_get_job_log` tool, and three
  approval-gated action requests (`github_trigger_workflow`,
  `github_comment_on_issue`, `github_set_issue_state`) whose
  `validate_against_upstream` hooks read the target from GitHub to name
  it on the card.
- `plugins/m365/` — the Microsoft 365 (Outlook Mail) integration (plugin
  id `m365`): an oauth-kind connection with **expiring tokens** — the
  credential loader proactively refreshes near-expiry Microsoft Graph
  tokens and persists the rotated refresh token — plus the
  `graph.microsoft.com` read-only `authed_get` entry, the `system:m365`
  skill, and seven `m365_*` mail tools mirroring the Gmail surface
  (two script-bridge enabled).
- `plugins/slack/` — the Slack integration (plugin id `slack`): the
  GitHub oauth-kind shape (non-expiring user tokens) plus a credential
  schema carrying the shared bot / Socket Mode tokens the core
  Slack-driven runtime consumes, the `system:slack` skill, two
  grandfathered action-request handlers (`send_slack_message` /
  `send_slack_dm`), and nine grandfathered-name tools
  (`unprefixed_tools`: `list_slack_teams`, ...,
  `send_slack_dm_to_self` — the latter also public-project allowlisted
  via the core-owned exemption in `config/plugins.py`). The Socket Mode
  worker, `slack_conversations` table, and `slack_reply` wait handles
  stay core.
- `plugins/twitter/` — the Twitter/X integration (plugin id `twitter`):
  the M365 expiring/rotating-token oauth shape plus the repo's only PKCE
  OAuth flow, the `api.twitter.com` read-only `authed_get` entry, the
  `system:twitter` skill, and the grandfathered `send_twitter_dm`
  action-request handler (`unprefixed_action_types`).
- `plugins/twilio/` — the Twilio SMS integration (plugin id `twilio`):
  an oauth-kind connection with NO OAuth provider -- the `/auth/twilio`
  router is a phone-number verification flow (type number, get a texted
  code, type it back; plugin-served popup page) plus `templates` routes
  for the user's pre-written messages -- an admin card with a `bool`
  **trusted channel** field, the no-approval `twilio_send_self_sms` tool
  whose allowed content depends on that switch, `twilio_list_sms_templates`,
  the `system:twilio` skill, and the approval-gated `twilio_send_sms`
  action request.
- `plugins/unifi/` — the UniFi Network + Protect integration (plugin id
  `unifi`): an admin card holding just the controller host (+ a TLS
  verification switch), an oauth-kind connection whose `/auth/unifi`
  popup collects the user's per-application API keys (Network and/or
  Protect -- UniFi issues one per app, so the single-secret `api_key`
  kind does not fit) and verifies each against the controller before
  storing them, the `system:unifi` skill, and eleven read-only
  `unifi_*` tools (network status, devices, clients, cameras, live
  camera snapshots saved to the workspace and attached as images).
- `plugins/iru/` — the Iru (formerly Kandji) Apple device management
  integration (plugin id `iru`): an admin card holding just the tenant
  API URL (runtime-configured host, so everything is a `tools` handler),
  the generic **api_key-kind** per-user connection (each user's own Iru
  API token, permission-scoped on the Iru side), the `system:iru` skill,
  and fifteen read-only `iru_*` tools (devices, blueprints, library item
  status, users, tags, Prism reports, threats, vulnerabilities, audit
  log, ADE devices). Read side only for now: no action requests, and the
  device secrets endpoints are deliberately not wrapped.
- `plugins/telegram/` — the Telegram integration (plugin id `telegram`):
  the Twilio router shape again, this time hosting **Telegram's own
  login protocol** (phone number -> code sent to the Telegram app ->
  optional cloud password) with the in-flight login parked server-side
  in `oauth_blob.pending` and the authorized Telethon `StringSession`
  stored as `oauth_blob.session`; an admin card for the MTProto app
  `api_id`/`api_hash` (legacy `telegram_app_info` section of
  server_config.json migrated by `post_load`); the four `telegram_*`
  read tools (script-bridge enabled), the `system:telegram` skill, the
  grandfathered `send_telegram_message` action request
  (`unprefixed_action_types`), and the first **`on_shutdown`** hook
  (closing the per-user Telethon clients its `TelegramClientManager`
  keeps connected for the life of the process).

- `plugins/google_admin/` — the Google Workspace Admin integration
  (plugin id `google_admin`): read-only directory (users, groups, org
  units) and device inventory access for Workspace administrators. The
  **services-only** shape — one GET-only `authed_get` service entry
  (`admin.googleapis.com`) and the `system:google_admin` skill, no tools
  or action requests — and the
  first plugin that **borrows a core credential**: its oauth-kind router
  runs a separate, read-only-scoped Google grant against the core
  `google_oauth` client, so the admin card is a single `enabled` switch
  and `is_configured` also requires that client. Its router lives under
  `/auth/google-admin` (see the namespace rule below).

Each `plugin.py` is a thin re-export of `manifest.py` so tests import
the real modules through the normal `plugins.<id>.*` package path.

## Tests

A plugin's tests live in its own `tests/` directory
(`plugins/<id>/tests/`), collected by the repo's pytest `testpaths`
alongside the core `tests/` suite. Both the plugin directory and its
`tests/` directory carry an `__init__.py` (keeps test module names
unique across plugins), and `tests/conftest.py` builds the plugin's
registration fixture from the shared factory:

```python
from pathlib import Path

from tests.plugin_support import plugin_fixture

my_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)
```

Out-of-tree plugins under a `QUEST_PLUGIN_PATH` root use the identical
layout; a bare `uv run pytest` from the quest repo root auto-collects
their `tests/` directories too (repo-root `conftest.py`). See
[Development Workflows](../docs/setup/development-workflows.md).

## Rules (enforced at load time)

- `id` matches `^[a-z][a-z0-9_]*$`, is unique, and does not collide with a
  core credential-store service name.
- Action-request type names and tool names are prefixed `<id>_`. The only
  exceptions are the grandfather lists for core integrations migrated
  into plugins: `unprefixed_action_types` for type names persisted in
  old `action_requests` rows (the Twitter/X plugin's `send_twitter_dm`,
  the Slack plugin's `send_slack_message` / `send_slack_dm`) and
  `unprefixed_tools` for tool names baked into transcripts, sandbox
  scripts, and skill prose (the Slack plugin's nine tools). New plugins
  must not use them.
- System skill ids are `system:<id>` (or `system:<id>_...`).
- `script_tool_allowlist` may only name the plugin's own tools.
- Public-project conversations always block plugin tools and services;
  plugins cannot opt out (the sole carve-out is the core-owned
  `_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` map in `config/plugins.py`).
- A plugin that raises on import, fails validation, or errors during
  registration is logged and skipped — it never prevents the server from
  booting. Registration is atomic: name clashes with live registry
  entries are rejected before anything is mutated, and a failure partway
  through rolls back the steps already done, so a skipped plugin leaves
  no partial surface and a corrected one loads cleanly on the next start.
- Directories starting with `_` or `.` are ignored by discovery
  (`plugins/_example/` is the in-repo smoke-test fixture, loaded only by
  the test suite).

## Git ignore note

Upstream gitignores `plugins/*` except this README, `.gitkeep`,
`_example/`, and the in-tree `github/`,
`m365/`, `slack/`, `twitter/`, `twilio/`, `unifi/`, `iru/`, `telegram/`,
and `google_admin/` plugins. A fork
that ships proprietary plugins should remove or override those ignore
lines in the fork and commit its plugin directories as ordinary files —
there is no upstream merge surface either way.
