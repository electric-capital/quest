# User Connections (Multi-Account)

## Overview

A **connection** is one `user_service_credentials` row: one credential a user holds for one plugin service. Every plugin service may hold one connection per user; a plugin that opts in with `UserConnectionSpec(multi_connection=True)` lets a user hold several (up to the spec's `connection_limit`), each identified by its **account** (for IMAP the mailbox address; for a service without a natural address a short user-chosen name; a Google email once the core Google follow-up lands) and carrying an optional user-written **description**. Exactly one connection per (user, service) is the **default**. Tool calls name a connection through an optional `connection` argument holding the account; omitting it means the default. The model sees accounts only, never row ids.

Single-connection plugins (every plugin except IMAP today) and the core Google / Airtable / Ramp slots (their own `users` columns) are unaffected: same user-dict key, same loaders, same `/connectors` rows, same UI. The IMAP plugin ([imap-api.md](../../plugins/imap/docs/imap-api.md)) is the first and so far only multi-connection plugin; it uses the `form` connection kind, which exists for multi-connection plugins only.

## Concepts and Invariants

| Term | Meaning |
|------|---------|
| Service | A plugin id (`imap`, `m365`, ...). Core connectors (`google_services`, `airtable`, `ramp`) are not rows in this table. |
| Connection | One `user_service_credentials` row for (user, service). |
| Account | The connection's model-facing identifier, stored lower-cased (`normalize_account()` in `db/user_service_credential_store.py`): non-empty, at most 320 characters, no whitespace, control/format characters, `"` or backtick -- accounts are rendered unquoted into the prompt and must round-trip through the `connection` argument as one token, so a user-chosen name is a token like `home-lab`, not a phrase. Legacy rows (created before multi-connection) have `''`. |
| Description | Optional user-written note, normalized by `normalize_description()`: whitespace runs (newlines included) collapse to one space, empty = `None`, at most 200 characters. Shown in Settings and the prompt; never used for lookup. |
| Default | The one row per (user, service) with `is_default`. The target of every call that omits `connection`; for single-connection services the only row. |
| Multi-connection service | A loaded plugin whose `UserConnectionSpec.multi_connection` is true. Only these get more than one row, the `connection` argument on their tools and `authed_get` entries, prompt enumeration and per-connection Settings cards. |
| Connection kind | `api_key` (one secret in `secret`), `oauth` (plugin router writes token JSON into `oauth_blob`), `form` (several schema-described values, verified on save, stored in `oauth_blob`). |

Invariants:

1. **Compat shim.** `user["service_credentials"][service]` is always the default row. Code that was never converted cannot see a non-default row.
2. **Exactly one default** while any row exists: the first row created is the default; `set_default_connection()` is the only way to move it; deleting the default promotes the oldest remaining row. Kept by the store in single transactions and backstopped by a partial unique index.
3. **Account uniqueness.** `(user_id, service, account)` is unique; accounts are stored lower-cased, so the index alone makes it case-insensitive. A duplicate is refused (`ConnectionExists` / HTTP 409 `connection_exists`); re-entering credentials goes through PUT on the existing row.
4. **Row ids never reach the model.** Tools, prompts and errors speak in accounts; ids are an HTTP detail of the Settings UI.
5. **Single-connection plugins never act on a `connection` argument.** Their tools do not declare it and their `authed_get` entries may not set `accepts_connection` (loader-enforced); `authed_get` refuses a named connection for them with `connection_not_supported` before loading credentials. Tool dispatch does not reject undeclared arguments, so a stray `connection` passed to a single-connection plugin tool is simply ignored and the handler uses the default row, as before.

## Data Model

Table `user_service_credentials` (`UserServiceCredential` in `db/models.py`) gains three columns via migration `e3c9b7a1d5f2` (`alembic/versions/e3c9b7a1d5f2_multi_connection_service_credentials.py`, down-revision `b4d7e2a9c6f1`):

- `account` -- `String(320) NOT NULL DEFAULT ''`
- `description` -- `Text NULL`
- `is_default` -- `Boolean NOT NULL DEFAULT 1` (legacy rows were the only row, hence default)

Index changes: the old unique `ix_user_service_credentials_user_id_service` on `(user_id, service)` is dropped; `ix_user_service_credentials_user_service_account` (unique on `(user_id, service, account)`) and `ix_user_service_credentials_user_service_default` (unique on `(user_id, service)` with `sqlite_where=is_default`, a partial index so two defaults cannot coexist) are added; `ix_user_service_credentials_user_id` stays. The ORM `__table_args__` match. The upgrade runs in `batch_alter_table` with the default `recreate="auto"` and never rewrites the encrypted columns. The **downgrade refuses** with a `RuntimeError` naming the offending (user, service) pairs while any (user, service) has more than one row; otherwise it drops the new indexes and columns and restores the old unique index.

`secret` (`EncryptedText`) and `oauth_blob` (`EncryptedJSON`) are unchanged. The `form` kind reuses `oauth_blob` as its credential blob: `{**extras, "values": {...}}` (the verify hook's extras next to the submitted values; the `values` key always wins over an extras key of the same name), so form secrets such as an IMAP password are encrypted at rest with the rest of the blob ([encryption-at-rest.md](encryption-at-rest.md)). `to_dict()` gains `id`, `account`, `description`, `is_default`; it never reaches the frontend (the connectors route builds its own view).

## Store

`db/user_service_credential_store.py` has two layers. Lists are ordered default first, then `created_at`, then `id`.

**Single-connection API** (historical signatures, act on the default row): `get_credential` (default row), `list_credentials` (service -> default row), `upsert_credential` (replaces `secret` and `oauth_blob` of the default row, creating it as default with account `''` when none exists; leaves `account`/`description` alone; retries once on a first-row race), `delete_credential` (deletes **every** row of the service), `delete_all_credentials` (every row of the user; used by logout-and-disconnect).

**Connection API** (individual rows):

- `list_connections(user_id, service)`, `list_all_connections(user_id)` and `fetch_all_connections(db, user_id)` (the same single query inside a caller-owned session, used by `_user_dict`).
- `get_connection(user_id, service, *, account=None, row_id=None)` -- by case-insensitive account and/or id (both must match when both given).
- `create_connection(..., account, description, secret, oauth_blob, make_default, limit)` -- normalizes account/description (ValueError), raises `ConnectionExists` on a duplicate account and `ConnectionLimit` when the (user, service) already holds `limit` rows (the caller passes the spec's `connection_limit`; best-effort under concurrency, a race can overshoot by the number of concurrent writers). The first row is always the default; a later one only with `make_default=True`. A lost unique-index race re-reports as `ConnectionExists` or retries once.
- `update_connection(user_id, service, row_id, *, description, secret, oauth_blob)` -- sentinel-based partial update; never touches `account` or `is_default`.
- `set_default_connection(user_id, service, row_id)` -- clears the old default and sets the new one in one transaction.
- `delete_connection(user_id, service, row_id)` -- deletes the row and, if it was the default, promotes the oldest remaining row (`created_at`, then `id`) in the same transaction.
- `upsert_connection_by_account(...)` -- update in place when the account exists, else create; the helper for future multi-account OAuth callbacks (no caller yet).

No store function sets `account` on an existing row: a conversion that backfills legacy `''` accounts needs a new helper (see Conversion Requirements).

## User Dict

`_user_dict` in `db/user_store.py` runs `fetch_all_connections` once (skipped while no loaded plugin declares a `user_connection`) and, when the user has any row, attaches:

- `service_credentials` -- `{service: default row}`, the compat shim every single-connection plugin and `get_user_connected_services()` read.
- `service_connections` -- `{service: [rows, default first]}`.

`service_credentials[svc]` **is the same dict object** as `service_connections[svc][0]`, so in-place token refreshes on the default row (GitHub, Twitter loaders) stay visible through the resolver.

`get_user_connected_services()` (`api/instructions.py`) treats a multi-connection plugin as connected when **any** row satisfies `spec.connected` and the plugin's server side is available; single-connection plugins still test only the default row.

## Resolver

`config/user_connections.py` (stdlib-only at import, dict-based) is the only place a `connection` string maps to a row:

- `service_rows(user, service)` -- rows from `service_connections`, default reordered first, falling back to the single `service_credentials` row for older/hand-built user dicts.
- `resolve_connection(user, service, connection=None)` -- blank/omitted -> the default row (or `None` when the user has no row: the caller's "not connected" handling applies, also for a named connection with no rows). Named -> the row whose account matches after strip + lower-case. For a multi-connection service, no match raises `ConnectionNotFound` (`.service`, `.requested`, `.available` = named accounts, default first; unnamed legacy rows are not offered). For a single-connection service a name is accepted only when it equals the sole row's account, else `ConnectionNotSupported`.
- `is_multi_connection(service)`, `connection_label(row)` (`acct` / `acct (default)`; a legacy default row reads `default`), `connection_summary(user, service)` (`{account, description, is_default, label}` per row, never ids or credentials; also fed to skill builders).
- `build_connected_accounts_paragraph(user)` -- the prompt paragraph (below).

Structured errors produced from these exceptions (by `authed_get` and by plugin tool handlers):

| Code | When |
|------|------|
| `connection_not_found` | A named connection matches no row of a multi-connection service. Carries `service`, `requested`, `available` so a stale prompt (connection added or removed mid-conversation) self-corrects on the next call. There is deliberately no `list_connections` tool. |
| `connection_not_supported` | A connection was named for a service that does not take one: in `authed_get` whenever the matched entry lacks `accepts_connection` (returned before any credential load or HTTP request), or `ConnectionNotSupported` raised by a loader. |
| `invalid_connection` | `authed_get` / `authed_post` got a non-string `connection` (IMAP tools return their own `invalid_arguments` for the same case). |

## Manifest Fields

`config/plugin_types.py` (see [plugins.md](plugins.md) for the rest of the manifest):

- **`UserConnectionSpec`** gains `kind="form"`; `multi_connection` (default False); `connection_limit` (default 10, 1..50); form-only `fields` (a tuple of `CredentialField`, the admin-card type), `field_options()` (runtime `select` choices per field key, e.g. an admin-configured server list; select fields it covers may leave `options` empty) and `verify(values) -> Awaitable[VerifyResult]` (logs in before the row is saved); and for all kinds `account_from(row) -> account` (required for multi-connection `oauth` / `api_key`; called synchronously) and `row_warning(row) -> str | None` (a short per-connection warning in Settings). `needs_reauth` is evaluated per connection for multi-connection rows.
- **`VerifyResult`** -- `account` (required on success), `error` (human-readable, shown under the form), `extras` (merged into the stored blob, e.g. server capabilities).
- **`CredentialField`** gains `type="select"` with static `options`, and an optional plain-text `help` rendered under the label by the shared frontend `SchemaForm` (admin cards and connection forms alike). Admin cards still reject `select` server-side (`allow_select` in `config/service_specs.py`).
- **`authed_get` service entries** (`QuestPlugin.services`) gain `accepts_connection`. `register_service` in `chat/gemini_api/authed_get.py` requires it to come with `requires_user` and a loader that accepts a `connection` keyword (checked with `inspect.signature`, `_loader_accepts_connection`).
- **Skill content builders** (`build_skill_content` in `chat/system_skills/loader.py`) receive `connections=connection_summary(user, skill.requires)` only when their signature accepts a `connections` keyword (`builder_accepts_connections`); `(base_url, api_key)` builders are untouched.

## Loader Validation

`validate_plugin` in `config/plugins.py` (`_validate_connection_extras`, `_validate_form_connection`, plus the tool and service checks) refuses a plugin when:

- `multi_connection` is not a bool, or `connection_limit` is not an int in 1..50; any of `account_from` / `row_warning` / `field_options` / `verify` is set but not callable.
- a multi-connection `api_key` or `oauth` spec lacks `account_from`; an `api_key` / `oauth` spec sets `fields`, `field_options` or `verify`.
- a `form` spec sets `oauth_router` / `scopes` or any of `validate_key`, `key_placeholder`, `key_help`, `key_help_links`; is not `multi_connection` (the generic routes only serve multi-connection plugins, so a single-connection form could never connect); lacks `fields` or `verify`; has a field that is not a `CredentialField`, a key not matching `^[a-z][a-z0-9_]{0,31}$`, or a reserved key (`description`, `account`, `connection`); or fails the admin-card field rules (`_validate_fields`, with `select` allowed and runtime-option keys exempt from needing static `options` when `field_options` is set).
- a multi-connection plugin has a tool whose `parameters.properties` lacks `connection`, declares it with a type other than `string`, or lists it in `required` -- so a plugin cannot half-convert.
- a multi-connection plugin has an `authed_get` service entry without `accepts_connection: True`, or a single-connection plugin sets it.

Plugin ids `connections` and `service_key` are reserved (`_RESERVED_PLUGIN_IDS`) because an oauth router under `/auth/<id>` would collide with the core routes.

## Tool Dispatch

- **`authed_get` / `authed_post`** declare an optional string `connection` in `chat/llm/tool_schemas.py` (in every prompt that lists the tools, so the snapshots carry the argument for every user). `_tool_authed_get` / `_tool_authed_post` in `chat/gemini_api/tool_dispatch.py` pass `args.get("connection")` to `handle_authed_get` / `handle_authed_post`, and `_make_authed_request` validates it (non-string -> `invalid_connection`, blank = omitted, named for a non-opted-in entry -> `connection_not_supported`) before `_call_loader` calls `loader(user, connection=...)` for `accepts_connection` entries (`loader(user)` otherwise). The 401-retry path re-calls the loader with the same connection. `ConnectionNotFound` / `ConnectionNotSupported` raised by the loader become the structured errors above.
- **Plugin tools** of a multi-connection plugin declare `connection` themselves and resolve it with `resolve_connection(ctx.user, "<id>", args.get("connection"))`, mapping the exceptions to the same error codes (reference: `_resolve` in `plugins/imap/tools.py`, which adds a `message` and its own `imap_not_connected`).
- **Scripts.** Sandbox scripts pass `connection` inside the `arguments` object of the `/api/tool-call` bridge for allow-listed plugin tools, and in the body of the sandbox API's `/api/authed-get` / `/api/authed-post` (`AuthedGetRequest` / `AuthedPostRequest.connection`). `authed_get` itself is not on `SCRIPT_TOOL_CALL_ALLOWLIST`, so the tool-call bridge needed no change.
- **Run kinds.** Connections ride on the user dict, so every run kind resolves them identically. Sub-agents inherit the parent's user and may name any of the user's connections. A cross-user subagent runs as the target user with the target's connections; the caller cannot name one. Inference API runs refuse `mutating` tools as before (IMAP has none). Slack-driven turns use the standard prompt and behave like web turns. Public-project conversations do have `tool_call`, but `authed_get` / `authed_post` (`TOOL_CALL_REGISTRY` entries reached through it) are not in `PUBLIC_TOOL_CALL_ALLOWLIST` and are rejected at dispatch, and no plugin tool is reachable either: the loader refuses plugin tool names in that allowlist, the only exception being the core-owned `_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` entry (`send_slack_dm_to_self`, a single-connection Slack tool).

## Connected Accounts Prompt Paragraph

`build_connected_accounts_paragraph(user)` builds a "**Connected accounts**" paragraph that `_connected_accounts_block()` in `chat/gemini_api/system_prompt.py` appends right after the system-skills enumeration. It is computed in `run_conversation_turn` (`chat/gemini_api/conversation.py`) for the top-level, cross-user-subagent and inference-API prompts (forced empty for public projects) and in `chat/gemini_api/sub_agent.py` for sub-agent prompts; the public-project prompt never takes it.

A service is listed only when it is a loaded multi-connection plugin, the user counts as connected to it (`get_user_connected_services()`), and it has **two or more listed entries**: named connections plus an unnamed legacy row when it is the default (rendered `(unnamed connection) (default)`, reachable by omitting the argument); unnamed non-default rows are dropped before counting. The paragraph is omitted entirely when no service qualifies, so users without multiple connections get byte-identical prompts. Each line is `- <label> (<id>): <acct> (default) -- user-written description: "<...>"; <acct> ...`. Descriptions are collapsed to one line, capped at 200 characters and JSON-quoted (`_quote_description`), and the paragraph tells the model the quoted descriptions are user-written data, not instructions.

## HTTP API

### `GET /connectors`

`get_connectors` in `chat/routes/user.py`. Single-connection rows are unchanged. A multi-connection plugin's row adds, via `multi_connection_fields()` in `auth/connections.py`: `connected` (any row satisfies `spec.connected`), `multi_connection: true`, `connection_limit`, `connections` (one `connection_view()` per row, default first: `id`, `account`, `description`, `is_default`, per-row `needs_reauth` and `warning` from the spec hooks -- hook exceptions are logged and read as False / no warning -- and `created_at`; never a secret or form value) and `connections_url` (`/auth/connections/<id>`). `form` rows carry only `service` / `label` / `kind` / `available` plus these fields and `form_fields` (`form_fields_view()`: key, label, type, required, placeholder, help, required_if, visible_if and the resolved select `options` from `resolve_field_options()`, which falls back to static options when `field_options()` raises or returns a non-dict). Multi-connection `oauth` rows keep `connect_url` / `needs_reauth`; multi-connection `api_key` rows omit the service-wide `key_hint` / `key_url` / `key_field` / `key_placeholder` / `disconnect_url` (keeping `key_help` / `key_help_links`), because `/auth/service-key/{service}` and its `/remove` return 404 `unknown_service` for them (`_resolve_api_key_plugin` in `auth/service_key.py`): their keys are added with `POST` and rotated with `PUT {api_key}` on the connection.

### `/auth/connections` routes

`auth/connections.py`, mounted in `quest.py`. Cookie-or-API-key auth (`get_current_user_cookie_or_apikey`); not reachable by the model (`curl_proxy_get` only dispatches `/api/*`, and the sandbox port serves only its allow-listed routes). Every error body is `{"error", "message", ...}` under `detail`. Every successful write calls `invalidate_user_sessions` so the next message rebuilds the prompt.

- `POST /auth/connections/{service}` -> 201 `{connection}`. `form`: body `{values, description?}`; `validate_form_values()` checks unknown keys, required / `required_if`, types (bool vs string), a 4096-character cap per value and select membership; then `verify` runs. `api_key`: body `{api_key, description?}`; `validate_key`, then `account_from({"service", "secret"})`. Stored with the spec's `connection_limit`.
- `PUT /auth/connections/{service}/{id}` -> 200 `{connection}`. Body keys `description`, `is_default` (only `true` is accepted), `values` (form) or `api_key` (api_key kind), validated before any write. `values` are **merged over the stored values** -- a blank secret keeps the stored one -- then re-validated and re-verified, and must yield the row's own account; `api_key` re-runs `validate_key` + `account_from` with the same account check. `{}` is a no-op 200 that writes and invalidates nothing.
- `DELETE /auth/connections/{service}/{id}` -> 204; the store promotes a new default when needed.

Verification (`_run_verify`): per-user rate limit of 10 verify / api_key attempts (add or re-entry) per rolling 60 s (`VERIFY_RATE_LIMIT`, in-process memory; an api_key attempt is charged only after it passes the local `validate_key` check) and a 20 s timeout (`VERIFY_TIMEOUT_SECONDS`); the returned account goes through `normalize_account()`.

| Status | Code | When |
|--------|------|------|
| 404 | `unknown_service` | The service is not a loaded multi-connection (or form) plugin. |
| 404 | `connection_not_found` | PUT/DELETE on an id that is not this user's row of this service. |
| 400 | `invalid_request` | Body not a JSON object, unknown body keys, missing/blank `api_key`, non-bool `is_default`, both `values` and `api_key`, or a credential key that does not match the kind. |
| 400 | `use_connect_url` | POST for an `oauth`-kind service (rows come from the plugin's own callback). |
| 400 | `invalid_values` | Form values fail the schema; `fields` maps field key -> message. |
| 400 | `invalid_description` | Description longer than 200 characters after normalization, or not a string. |
| 400 | `verify_failed` | The hook returned an error (its message), raised, timed out, or returned an unusable account. |
| 400 | `invalid_key` | api_key kind: `validate_key` rejected the key. |
| 400 | `account_unresolved` | api_key kind: `account_from` raised or yielded an unusable account. |
| 400 | `account_mismatch` | Re-entered credentials resolve to another account, or the row is a legacy row without an account (delete and re-add instead). |
| 400 | `cannot_unset_default` | `is_default: false`; make another connection the default instead. |
| 400 | `connection_limit` | The service already holds `connection_limit` rows; carries `limit`. |
| 409 | `connection_exists` | The account is already connected; carries `account` and the existing `connection_id`. |
| 409 | `conflict` | A unique-index race survived the store's own retry. |
| 429 | `rate_limited` | Verify rate limit hit; `retry_after` in the body and a `Retry-After` header. |

## Settings UI

`frontend/src/components/settings/DataConnectionsSection.tsx` renders a multi-connection row (`isMultiConnection()` in `connectionHelpers.ts`: `multi_connection` or `kind: 'form'`) as one `ConnectionCard.tsx` per connection, default first: brand icon, account (`(unnamed connection)` for legacy rows), an inline description editor (saved on blur/Enter via `PUT {description}`, no request when unchanged, empty = `null`, Escape reverts), a **Default** chip or **Make default** action, a `needs_reauth` badge ("Update Available" for the oauth kind, "Needs re-entry" otherwise), the `warning` line, a hint on legacy non-oauth rows without an account ("To re-enter its credentials, disconnect this connection and add it again."), Reconnect, and a per-card Disconnect (confirm, then DELETE). There is no service-wide Disconnect for these services.

The add picker offers a multi-connection service while it has fewer than `connection_limit` connections (`isAddable()`). Picking a form tile opens `ConnectionForm.tsx`: `SchemaForm.tsx` over `form_fields` (the same field components the admin Service Credentials cards use) plus an optional Description field; errors render inline and the form keeps its values -- per-field `invalid_values`, the plugin's `verify_failed` message, `connection_limit`, the 429 retry delay, and `connection_exists` as "This account is already connected." with a **Show it** link that scrolls to and highlights the existing card. Reconnect reopens the form with **only the secret fields** (`connectionFormFields(..., 'reconnect')`) and sends what was typed, relying on the server-side merge; a form without secret fields re-enters with every non-bool field shown as optional (blanks omitted), and an all-bool form offers no Reconnect (`canReconnect()`). Reconnect is hidden for non-oauth legacy rows without an account; an oauth default card keeps it regardless of account. An `api_key` multi-connection service gets a single API key field for add and re-entry. **OAuth multi-connection is guarded off**: until callbacks create rows per account, only the default card offers Reconnect and the service leaves the picker once it has a connection, so a second "add" cannot overwrite the default account. Typed client calls are `createConnection` / `updateConnection` / `deleteConnection` with `ConnectionApiError` in `frontend/src/api/client.ts`; types `ConnectorRow`, `ConnectionRow`, `ConnectionFormField` in `frontend/src/api/types.ts`. See [settings-data-connections.md](settings-data-connections.md) and [frontend.md](frontend.md) for the surrounding section.

## Lifecycle and Edge Cases

- **Legacy rows.** The migration gives every existing row `account = ''`, `is_default = 1`. Single-connection plugins never read `account`. On a multi-connection service an unnamed row works as the default (omitted `connection`), cannot be named, is left out of `available` lists, is listed in the prompt only as the default, and cannot be re-entered (`account_mismatch`; the UI hides Reconnect).
- **Defaults.** First row = default; Make default (`PUT {is_default: true}`) is the only way to move it; deleting the default promotes the oldest remaining row.
- **Duplicates.** 409 `connection_exists`; re-entering a password for an existing account goes through PUT on that row.
- **Deletion.** Per-connection DELETE only for multi-connection services. Logout-and-disconnect (`delete_all_credentials`) removes every row; account deletion cascades through the FK.
- **Admin removes a server** (IMAP). Rows stay; `row_warning` shows "Server no longer allowed"; the tools return `imap_host_not_allowed` for that connection because the stored `server` is resolved against the **current** admin list on every connect (and on verify). The user deletes the card or the admin re-adds the line.
- **Session freshness.** The user is re-read from the DB each turn and the prompt is rebuilt from it, so a connection added in Settings is usable on the next message; within a turn a stale list self-corrects through `connection_not_found`.
- **Known gap (generic layer).** A `PUT {values}` that changes a non-secret field (e.g. the host) while leaving the password blank re-verifies the **stored** password against the new value, because the merge keeps blank secrets. The new value must still pass select membership; for IMAP the server list is admin-owned, so this cannot reach an unlisted host.

## Security Notes

- The compat shim means an unconverted plugin can never read a non-default row; converted code reads rows only through `resolve_connection`.
- No route response carries a secret or a stored form value: `connection_view()` is the only serialization, shared by `/connectors` and the connection routes. Form secrets live in the encrypted `oauth_blob`.
- Form values are validated server-side against the schema (unknown keys refused, select membership enforced against the runtime options, 4096 characters per value), so a client cannot smuggle a value outside an admin-controlled choice list.
- Descriptions are user-authored text that reaches the system prompt: normalized and capped on write, JSON-quoted and labelled user-written on render. Accounts are restricted to one token without quotes or backticks.
- `verify` is rate-limited per user (10 / 60 s) so the form cannot brute-force an upstream login through Quest, and time-boxed at 20 s.
- The connection routes are browser/API-key routes outside `/api/*`, so neither the model (`curl_proxy_get`) nor sandbox scripts can create, change or delete connections.

## Conversion Requirements

What a multi-connection plugin must satisfy; the loader enforces items 1, 3 and 4 (and the form-kind rules), so a partial conversion fails at startup. IMAP (`plugins/imap`, [imap-api.md](../../plugins/imap/docs/imap-api.md)) is the worked reference for the form kind.

1. **Spec.** `UserConnectionSpec(multi_connection=True, connection_limit=...)`. `oauth` / `api_key` kinds must supply `account_from(row)`, returning an account that passes `normalize_account()` (one lower-case token); for `api_key` it is called synchronously with `{"service", "secret"}`, so it must derive the account from the key alone. `form` kinds get the account from `verify` (IMAP: `verify_imap_connection` in `plugins/imap/upstream.py` returns the username when it contains `@`, else `username@host`, plus server capabilities as extras). Optional `row_warning` (IMAP: `imap_row_warning`).
2. **Legacy rows.** Existing single rows keep `account = ''` and keep working as the default. A backfill from `account_from` in the plugin's `post_load` hook needs a new store helper that sets `account` (the store has none today); rows it cannot resolve stay unnamed.
3. **Loaders and services.** Credential loaders become `(user, connection=None)` and call `resolve_connection`; every `authed_get` service entry of the plugin sets `accepts_connection: True` (with `requires_user`).
4. **Tools.** Every tool spec declares an optional string `connection` parameter, and handlers resolve it with `resolve_connection`, mapping `ConnectionNotFound` / `ConnectionNotSupported` to `connection_not_found` / `connection_not_supported` (IMAP: `_resolve` in `plugins/imap/tools.py`). Per-connection caches and on-disk paths should key on the account (IMAP: the URL cache key and the `imap-attachments/<account>/` folder).
5. **OAuth kind** (not built yet): the callback must create rows with `upsert_connection_by_account` (the account from `account_from`) instead of `upsert_credential`, a `connection=new` connect-URL parameter must stop it from overwriting the default, and the frontend guards in `canReconnect()` / `isAddable()` must be lifted. POST `/auth/connections/{service}` stays `use_connect_url`; PUT/DELETE already work.
6. **api_key kind:** nothing extra -- the service-wide key routes 404 automatically and keys move to POST / `PUT {api_key}` on the connection.
7. **Skill.** The system skill's content builder accepts `connections=` and documents the `connection` argument once, pointing at the Connected accounts list (IMAP: `imap_skill_content` in `plugins/imap/manifest.py` fills `{{CONNECTIONS_LIST}}` / `{{CONNECTION_EXAMPLE}}` in `plugins/imap/instructions.md`).
8. **Docs.** Update the plugin's docs and its `docs/index.md` row.

**Core Google multi-account (follow-up).** The designed next target moves `users.google_services_oauth` into rows with `service="google_services"` and account = the Google email the callback already verifies through UserInfo; the fail-closed identity match becomes "the default connection must be the session account, additional connections may be other Google identities"; every Google loader gains `connection`; the `google_admin` plugin's separate connection stays separate; Airtable and Ramp follow the same move. Tracked in [#94](https://github.com/electric-capital/quest/issues/94).

## Design Decisions

**Why evolve the table in place behind a compat shim?** Every plugin, loader and route already reads `service_credentials[service]`; keeping that as the default row makes the change invisible to unconverted code and lets plugins convert one PR at a time.

**Why accounts, not row ids, in tool calls?** The model must be able to name a connection the user described in words, and ids would leak a Settings-UI detail into transcripts. Account uniqueness per (user, service) makes the account a stable key.

**Why no tool listing a service's accounts?** The prompt paragraph enumerates the choices and the `connection_not_found` error lists the current accounts, which covers connections added mid-conversation without another tool. `list_service_connections` ([Connect Requests](connect-requests.md)) lists services, not the accounts within one.

**Why only list services with two or more connections?** With one connection there is nothing to choose; omitting the paragraph keeps existing users' prompts byte-identical.

**Why does `form` require `multi_connection`?** The generic routes that create form rows serve only multi-connection plugins; a single-connection form plugin would have no way to connect.

## Key Files

| File | Description |
|------|-------------|
| `config/user_connections.py` | Resolver (`resolve_connection`, `service_rows`, `connection_summary`, `connection_label`, `is_multi_connection`), `ConnectionNotFound` / `ConnectionNotSupported`, `build_connected_accounts_paragraph` |
| `config/plugin_types.py` | `UserConnectionSpec` multi-connection and form fields, `VerifyResult`, `CredentialField` (`select`, `help`) |
| `config/plugins.py` | Loader validation (`_validate_connection_extras`, `_validate_form_connection`, tool `connection` and service `accepts_connection` checks), `get_user_connection_spec`, `_RESERVED_PLUGIN_IDS` |
| `db/models.py` | `UserServiceCredential` columns and indexes |
| `alembic/versions/e3c9b7a1d5f2_multi_connection_service_credentials.py` | Migration: three columns, index swap, guarded downgrade |
| `db/user_service_credential_store.py` | Single-connection and connection store APIs, `normalize_account` / `normalize_description`, `ConnectionExists` / `ConnectionLimit` |
| `db/user_store.py` | `_user_dict`: `service_credentials` (default rows) + `service_connections` |
| `api/instructions.py` | `get_user_connected_services`: any connected row counts for multi-connection plugins |
| `auth/connections.py` | `/auth/connections` routes, form validation, verify rate limit/timeout, `connection_view` / `multi_connection_fields` shared with `/connectors` |
| `auth/service_key.py` | Generic key routes; 404 for multi-connection api_key plugins |
| `chat/routes/user.py` | `get_connectors`: multi-connection row shape |
| `chat/gemini_api/authed_get.py` | `accepts_connection` registration check, `connection` handling, structured errors, sandbox proxy bodies |
| `chat/llm/tool_schemas.py` / `chat/gemini_api/tool_dispatch.py` | `connection` parameter on `authed_get` / `authed_post` and its pass-through |
| `chat/gemini_api/system_prompt.py`, `conversation.py`, `sub_agent.py` | Connected accounts paragraph placement per prompt |
| `chat/system_skills/loader.py` | `connections` kwarg for skill content builders |
| `frontend/src/components/settings/DataConnectionsSection.tsx`, `ConnectionCard.tsx`, `ConnectionForm.tsx`, `connectionHelpers.ts`, `SchemaForm.tsx` | Per-connection cards, add/re-entry form, picker and reconnect rules, shared schema form |
| `frontend/src/api/types.ts`, `frontend/src/api/client.ts` | Connector/connection types, connection route client with `ConnectionApiError` |
| `plugins/imap/` | Reference multi-connection `form` plugin (`manifest.py`, `settings.py`, `upstream.py`, `tools.py`) |
| `tests/test_user_connections.py`, `tests/test_user_service_connections_store.py`, `tests/test_multi_connection_migration.py`, `tests/test_connections_routes.py`, `tests/test_connectors_multi.py`, `tests/test_multiconn_dispatch.py`, `tests/test_multiconn_fixture_plugin.py`, `tests/fixture_plugins/multiconn/` | Resolver, store, migration, routes, connectors, dispatch and prompt tests over a test-only multi-connection plugin |
| `frontend/src/components/settings/DataConnectionsSection.test.tsx`, `frontend/src/components/settings/SchemaForm.test.tsx` | Per-connection cards, add/re-entry form, picker rules, schema form |
| `plugins/imap/tests/` | IMAP plugin suite over a fake `imaplib` |
