# Service Credentials

## Overview

Server-level credentials for upstream API integrations (OAuth client configs, API keys) are stored in a per-service credential store: one `<service>.json` file per service under `DATA_DIR / "service_credentials"` (file mode 0600, directory mode 0700, contents encrypted at rest as `{"encrypted": "qenc1:..."}` -- see [Encryption at Rest](encryption-at-rest.md); reads accept a plaintext JSON file, which is what the stdlib-only bootstrap wizard and local pre-baking write before the key is available, and `encrypt_plaintext_credential_files()` converts those at startup). Admins manage them from a Settings section instead of editing files on disk.

The core roster (`CORE_SERVICES`) covers:

- the Google OAuth client (`google_oauth`, used for login and all Google services),
- Ramp (`ramp`),
- CoinGecko Pro (`coingecko`: the single server-level API key the `authed_get` service registry injects as `x-cg-pro-api-key` on `pro-api.coingecko.com` requests, read by `load_coingecko_api_key()` in `auth/config.py` with the usual store-first/legacy-section fallback), and
- outgoing email (`smtp`: host, port, implicit-TLS toggle, optional username/password, from address; configured = host + from address; store-only, no legacy location). Read by `auth/mailer.py` for the email/password sign-in flow's self-service reset/sign-up links and emailed admin invites -- see [Auth](auth.md#sign-in-methods).

Plugins that declare a `credential_schema` are appended to the roster at load time (`KNOWN_SERVICES` = core + loaded plugins), getting a store file and an admin card with zero additional wiring:

- Slack (plugin id `slack`: OAuth app client id/secret plus the optional shared bot and Socket Mode tokens the core Socket Mode worker consumes) arrives this way from `plugins/slack` (its legacy server_credentials.json `slack` section still migrates because the startup migration runs in the app lifespan, after plugin load),
- GitHub (plugin id `github`: OAuth app client id/secret, same legacy-section migration) from `plugins/github`,
- Microsoft 365 (plugin id `m365`: Entra ID tenant id + app client id/secret, no legacy location) from `plugins/m365`,
- Twitter/X (plugin id `twitter`: OAuth 2.0 app client id/secret, legacy standalone `twitter_credentials.json` still migrating the same post-plugin-load way) from `plugins/twitter`, and
- Telegram (plugin id `telegram`: the MTProto app `api_id`/`api_hash` from my.telegram.org; its legacy location is the `telegram_app_info` section of server_config.json -- the general server CONFIG file -- which the plugin's own `post_load` hook migrates, normalizing the JSON-integer `api_id` to the string form the admin form uses) from `plugins/telegram`.

No remaining server-level credential reads the legacy consolidated `server_credentials.json` at the project root except as a migration source / read fallback. LLM backend credentials for future API-key inference providers live in a separate but structurally identical per-provider store -- see [Inference Providers](inference-providers.md).

The admin surface is fully schema-driven: every service -- core or plugin -- is described by a `ServiceSpec` in `config/service_specs.py` (label, a tuple of `CredentialField` rows, an `is_configured` predicate, and optional `flatten`/`unflatten`/`validate` hooks for stored shapes the flat schema can't express, e.g. Google's nested `web` wrapper; plugins supply a `validate` hook via `QuestPlugin.credential_validate`, e.g. base-URL normalization).

One generic list GET returns each service's field schema plus masked form values, one generic PUT validates saves against the schema, and the frontend renders one generic card component per entry -- adding a service requires no new endpoint or frontend change.

Each service's legacy location is its section in `server_credentials.json`, except Twitter/X whose legacy location is the standalone `twitter_credentials.json` at the project root. Ramp has no legacy location at all -- the store is its only source. A plugin with legacy credential shapes of its own migrates them in its `post_load` startup hook, which runs right after registration.

A plugin whose upstream base URL lives in its store entry reads it back through its own helper as the single source of that service's availability; when the entry is disabled or empty, the plugin's tools return a structured not-configured error and its row disappears from every user's Data Connections screen (`available: false` on `GET /connectors`). Absent any configuration, such integrations are off by default.

## Key Files

| File | Description |
|------|-------------|
| `config/service_credentials.py` | The store: `CORE_SERVICES` roster + `KNOWN_SERVICES` (core + plugin services, extended by `register_plugin_service()` at plugin load), `read_service_credentials` / `write_service_credentials` (atomic temp-file + `os.replace`, 0600, encrypted `{"encrypted": ...}` wrapper with plaintext-tolerant reads), `encrypt_plaintext_credential_files()` (startup sweep), legacy reads (`server_credentials.json` sections; `twitter_credentials.json` for twitter), and `migrate_legacy_credentials()` |
| `config/service_specs.py` | `ServiceSpec` per service (label, `CredentialField` schema, `is_configured`, optional flatten/unflatten/validate hooks), the core spec definitions, `register_plugin_credentials()` (called by the plugin loader for plugins with a `credential_schema`), and the generic `form_view()` / `resolve_update()` machinery the admin endpoints run on |
| `config/paths.py` | `SERVICE_CREDENTIALS_DIR` constant |
| `auth/config.py` | `load_google_oauth_config()` and `load_slack_client_config()` (and the bot/Socket Mode token loaders on top of it -- these stay core because the core Slack Socket Mode worker needs them, treating an unregistered `slack` store service as unconfigured when the plugin failed to load) read the store first, then fall back to their legacy file; `load_ramp_client_config()` reads the store only (the GitHub and Twitter/X loaders moved to `plugins/github/upstream.py` / `plugins/twitter/upstream.py` with the same precedence) |
| `quest.py` | Lifespan startup calls `migrate_legacy_credentials()` (best-effort, non-fatal) |
| `chat/routes/admin.py` | Admin-gated endpoints: list GET (full schema + form view per service), single-service GET (secrets masked), and ONE generic PUT `/admin/service-credentials/{service}` validating against the service's spec |
| `frontend/src/components/settings/ServiceCredentialsSection.tsx` | Admin-only Settings section: configured services as read-only summary cards (`ConfiguredServiceCard`, Edit swaps in the form), unconfigured ones as an icon tile grid that expands into the form; the form (`CredentialForm`) is rendered entirely from the list endpoint's `fields` schema (text/secret/bool/textarea, `visible_if`/`required_if` honored client-side) |
| `frontend/src/components/settings/serviceIcons.tsx` | Brand icons keyed by service id, shared with the Data Connections picker (generic plug fallback for unknown services) |
| `frontend/src/components/SettingsModal.tsx` | Shows the "Service Credentials" nav item only when `isAdmin` |
| `frontend/src/api/client.ts` | `fetchServiceCredentials` (list), `fetchServiceCredential`, and the generic `updateServiceCredentials(service, values)`; schema types (`CredentialFieldSchema`, `ServiceCredentialDetail`) in `frontend/src/api/types.ts` |
| `run.py` | Local-mode service summary checks the store in addition to the legacy file; also pre-bakes store files from the parent directory's shared `dev-config.json` (see below) |

## Precedence and Migration

- Loaders always prefer the per-service store; the legacy file is only consulted when the store has nothing for that service.
- At startup, `migrate_legacy_credentials()` copies each `KNOWN_SERVICES` entry from its legacy location into the store. Services that already have a per-service file are skipped, so credentials saved through the admin UI are never overwritten. The legacy files themselves are never modified or deleted.
- Store reads are fresh on every call (no caching), so credentials saved in the admin UI take effect without a server restart.
- In local mode, `run.py` pre-bakes store files from the `service_credentials` mapping in the checkout parent directory's shared `dev-config.json` before the server starts, so those services (e.g. `google_oauth`) show up ready-configured in the admin UI on a fresh throwaway data directory. An existing store file always wins, same as the legacy migration. See [Shared Dev Config in Local Mode](../setup/development.md#shared-dev-config-dev-configjson-in-local-mode).

## Admin API and UI

Endpoints live in `chat/routes/admin.py` under `/app/api/admin/service-credentials`, gated by `is_admin()` like the other admin endpoints. `GET /admin/service-credentials` returns every service (core + plugins) with its `fields` schema and masked form view; `GET .../{service}` returns one. Reads never return secret values -- for each `secret` field the form only learns `<key>_set: bool`; a PUT with an empty secret value keeps the stored one, which lets the forms round-trip without ever transporting secrets back to the browser.

Writes go through the single generic `PUT /admin/service-credentials/{service}` with a flat `{field key: value}` body. `config/service_specs.py`'s `resolve_update()` enforces the schema: unknown keys are rejected, strings are stripped, `required` / `required_if` (required iff a named bool toggle is on) produce 400 `invalid_params`, and the resolved values start from the currently effective config so unrecognized stored keys (e.g. inside Google's `web` wrapper) survive a save.

Per-service quirks live in spec hooks, not endpoints: `google_oauth` flattens/unflattens its nested `web` wrapper (redirect URIs edit as a newline-separated textarea; standard Google endpoint URLs are filled in on save), and plugins normalize their values in their `credential_validate` hooks (e.g. base-URL trailing-slash stripping and scheme checks). A plugin's `is_configured` can report configured only when an enablement toggle is on, so a stored-but-disabled entry reads "Not configured".

The Settings UI section (`ServiceCredentialsSection.tsx`) fetches the list once and splits it by `configured`. Configured services are listed at the top as summary cards showing the stored parameters read-only (text/textarea values verbatim, bools as On/Off, secrets masked as dots or "Not set" from the `<key>_set` flags, `visible_if`-hidden fields omitted) with an Edit button that swaps the summary for the form in place. Unconfigured services sit below in an "Add a service" grid of brand-icon tiles (`serviceIcons.tsx`); picking one replaces the grid with a "Configure <label>" panel holding the same form. The form renders `bool` fields as toggles, `secret` fields as write-only password inputs with keep-on-empty placeholders, `textarea` fields multi-line, and `visible_if` fields hidden until their toggle is on; a successful save collapses the form and moves the service into the configured list, Cancel discards. Only one form is open at a time. It is reachable only for admins; the endpoints enforce the same check server-side. Plugin services get the identical treatment with no frontend change.

## Design Decisions

**Why per-service files instead of one consolidated JSON?**
Independent writes: saving one service's credentials from the UI can never corrupt or race with another service's data, and file permissions apply per credential set.

**Why keep the legacy file as a read fallback instead of deleting it after migration?**
The legacy file may be provisioned by deployment tooling and shared with services not yet moved to the store. Leaving it untouched makes the migration risk-free and reversible; the store simply wins when both exist.

**Why is the client secret write-only in the API?**
Admin sessions don't need the secret back to manage it -- masking it keeps the secret out of browser memory, devtools, and any response logging, while the empty-means-keep convention still allows editing every other field.
