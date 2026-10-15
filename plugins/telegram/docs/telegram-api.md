# Telegram Integration

This document describes how Quest exposes Telegram to the LLM agent and to sandbox scripts, and how a user connects their Telegram account. It is packaged as the in-tree `plugins/telegram` plugin (plugin id `telegram`; see [Plugin Architecture](../../../docs/architecture/plugins.md)).

## Overview

Quest reads Telegram dialogs, messages, and contacts through four dedicated dynamic tools dispatched via `tool_call` (`telegram_get_me`, `telegram_list_dialogs`, `telegram_get_messages`, `telegram_list_contacts`).

There are no `/api/telegram/*` HTTP routes: the `/api/telegram` prefix stays blocked in route dispatch so cached sessions that still emit `curl_proxy_get` get a "use the dedicated tool" pointer, and sandbox scripts (`run_script` / `run_python`) reach the same four tools through the `POST /api/tool-call` bridge.

Sending goes through the `send_telegram_message` action request. Every read and write runs on a persistent Telethon client connection kept per user by the `TelegramClientManager` singleton.

Three things distinguish the plugin from the OAuth plugins:

1. **No OAuth provider.** The per-user connection is a Telethon `StringSession` obtained through Telegram's own login: the user types their phone number, Telegram sends a code to their Telegram app, and (for accounts with two-step verification) the user types their cloud password. The manifest still declares the `oauth` connection kind because that is the shape that mounts a plugin-owned, session-cookie-authed router under `/auth/telegram` and renders the generic Connect-popup row; the popup page is the plugin's own login form.
2. **Server-side login state.** The in-flight login (phone, `phone_code_hash`, the not-yet-authorized Telethon session string, the stage, expiry, attempt counter) lives in the user's `user_service_credentials` row under `oauth_blob.pending`, encrypted at rest -- never in a cookie. A pending login never counts as connected, and an existing authorized session survives a failed re-login because `pending` is written beside it and only replaces it on success.
3. **A long-lived upstream connection.** `TelegramClientManager` keeps one connected Telethon client per user for the life of the process, so the manifest supplies the `on_shutdown` hook that closes them all when the server stops.

## Key Files

| File | Description |
|------|-------------|
| `plugins/telegram/manifest.py` | The plugin manifest: credential schema (`api_id`, `api_hash`), the oauth-kind user connection wired to the login router, the `system:telegram` skill, the four tools (all script-bridge enabled), the `send_telegram_message` handler (grandfathered via `unprefixed_action_types`), `post_load` (legacy credential migration) and `on_shutdown` (close clients) |
| `plugins/telegram/upstream.py` | Admin credential loader (`load_telegram_credentials`: store, then the legacy `telegram_app_info` section of server_config.json; `normalize_app_info`, `telegram_is_configured`, `validate_telegram_credentials`, `migrate_legacy_credentials`), `create_telegram_client`, the per-user session helpers (`get_telegram_session`, `telegram_connected`), `TelegramClientManager` (`get_client` / `drop_client` / `close_all`), the plain async read functions (`get_me`, `list_dialogs`, `get_messages`, `list_contacts`) and `resolve_dialog_name` |
| `plugins/telegram/auth.py` | The `/auth/telegram` router: popup page, `send-code`, `verify`, `2fa`, `disconnect` |
| `plugins/telegram/tools.py` | The `telegram_*` `PluginTool`s: argument coercion (`dialog_id` required and numeric, `limit`/`offset_id` ints, `reverse` bool) and `_run_telegram_call()` rendering the read functions' `HTTPException` details as `{"error": ...}` JSON |
| `plugins/telegram/handlers.py` | `SendTelegramMessageHandler` (`send_telegram_message` action request) with its `dialog_id` format validation and the `enrich_params_for_preview` hook that injects `dialog_name` |
| `plugins/telegram/instructions.md` | The `system:telegram` skill body (LLM-facing) |
| `alembic/versions/d7a1f3c9e2b4_*.py` | Migration moving `users.telegram_session` into `user_service_credentials` rows |
| `chat/route_dispatch.py` | `/api/telegram` in `_BLOCKED_PROXY_PATHS` (core) |

## Configuration

- Admin: Settings > Service Credentials > Telegram (store file `data/service_credentials/telegram.json`, written by the generic `PUT /admin/service-credentials/telegram`). Fields: `api_id` (digits, validated by `validate_telegram_credentials`) and `api_hash` (secret), both from my.telegram.org/apps. `is_configured` requires both; the connector row is hidden until then. See [telegram-setup.md](telegram-setup.md).
- Legacy location: the `telegram_app_info` section of `server_config.json`. The plugin's `post_load` hook (`migrate_legacy_credentials`) copies it into the store at load time (normalizing the JSON-integer `api_id` to the string form the admin form uses, and rewriting a store file an older release copied verbatim), never touching the legacy file; `load_telegram_credentials` still falls back to the section if the copy could not be written.

## Login Flow (`/auth/telegram`)

Every route is session-cookie authed like the core OAuth flows. The page served by `GET /auth/telegram?popup=1` drives the JSON endpoints with `fetch` and posts the shared `oauth_callback_success` message to the opener on success (see [OAuth Popup Flow](../../../docs/architecture/oauth-popup.md)).

1. `POST /auth/telegram/send-code` `{"phone"}` -- normalizes the number (country code required), asks Telegram for a login code with a fresh Telethon client, and parks `{phone, phone_code_hash, session, stage: "code", expires_at, attempts}` under `oauth_blob.pending`. Telegram's `FloodWaitError` surfaces as 429 `flood_wait`.
2. `POST /auth/telegram/verify` `{"code"}` -- resumes the pending session and calls `sign_in`. A wrong code counts an attempt (`incorrect_code`; the login is voided after `MAX_ATTEMPTS`), an expired pending entry or Telegram's `PhoneCodeExpiredError` voids it (`code_expired`), and `SessionPasswordNeededError` moves the pending entry to `stage: "password"` and answers `needs_2fa: true`. Otherwise the login completes.
3. `POST /auth/telegram/2fa` `{"password"}` -- finishes a two-step-verification sign-in (`incorrect_password` counts an attempt).
4. Completion stores `{"session": <StringSession>, "phone", "connected_at"}` as the whole blob, drops any cached client for the user, and calls `invalidate_user_sessions()` so the next message advertises the Telegram skill and tools.
5. `POST /auth/telegram/disconnect` -- drops the cached client, best-effort `log_out()`s the session at Telegram, deletes the row, and invalidates chat sessions.

`telegram_connected` (the manifest's `connected` predicate) looks only at `oauth_blob.session`.

## Read Tools

All four tools are gated by `requires_service: "telegram"` and documented by the `system:telegram` skill, so the system prompt names them on that skill's enumeration line only for users whose `connected_services["telegram"]` is true (server configured AND session stored -- the combined plugin gate in `get_user_connected_services()`); loading the skill appends their parameter reference. Result shapes and the paging semantics are documented for the model in `instructions.md`.

| Tool | Arguments | Wraps |
|------|-----------|-------|
| `telegram_get_me` | -- | `get_me()` -- id, username, first/last name, phone, is_premium |
| `telegram_list_dialogs` | `limit` (default 20) | `list_dialogs()` -- `{dialogs: [...]}`, the numeric `id` is the `dialog_id` for messages and sends |
| `telegram_get_messages` | `dialog_id` (required), `limit` (50), `offset_id` (0), `offset_date` (ISO8601), `reverse` (false) | `get_messages()` -- `{messages: [...]}`, newest first unless `reverse` |
| `telegram_list_contacts` | -- | `list_contacts()` -- `{contacts: [...]}` |

Errors come back as `{"error": "<code>", "message": "..."}` with the codes the read functions raise (`telegram_not_connected`, `telegram_session_expired`, `telegram_api_error`, `invalid_date`); a non-numeric `dialog_id` is rejected in the handler before the client lookup. The tools are deliberately NOT in `PUBLIC_TOOL_CALL_ALLOWLIST` (public projects see no internal connectors).

### From sandbox scripts

Code running in the script sandbox cannot use `tool_call`; it POSTs `{"tool_name": "telegram_list_dialogs", "arguments": {"limit": 50}}` to `http://localhost:$QUEST_PORT/api/tool-call` with the `QUEST_API_KEY` bearer token (see [Gemini API -- Script Tool-Call Bridge](../../../docs/architecture/gemini-api.md#script-tool-call-bridge-post-apitool-call)). None of the Telegram reads depend on conversation context, so they behave identically over the bridge.

## Sending Messages (Action Request)

Sending is handled through the Action Requests system rather than a tool. The agent proposes a `send_telegram_message` action request, the user approves it via the chat UI, and the backend executes it via `SendTelegramMessageHandler`.

`validate_params()` format-checks `dialog_id` before persisting an `action_requests` row: the value must be a Python `int` or a digit string with an optional leading `-` (the regex lives at module scope in `handlers.py`), which rejects `@username`, `t.me/...` URLs, `+E.164` phone numbers, Slack-style `C.../U...` ids, whitespace-padded strings, `bool` (which subclasses `int`), `float`, `list`, and `dict`.

Out-of-band integers are also rejected: `0`, the bare `-100` supergroup/channel marker prefix, and `|dialog_id| > 10**15` (see `_DIALOG_ID_MAX_ABS`).

Format errors name the field and echo the offending value truncated to ~100 chars so the model self-corrects on the same turn instead of failing inside `execute()` with an opaque Telethon `PeerIdInvalid`. The 4096-character `message` cap matches Telegram's limit.

**Dialog name resolution:** the handler's `enrich_params_for_preview()` hook resolves the numeric id to a display name (first/last name for users, title for groups/channels) via `resolve_dialog_name()` and stores it as `dialog_name` for the approval card; the label is always re-derived from `dialog_id`, so a model-supplied `dialog_name` can never survive (it is also not in the handler's allow-list).

See [Action Requests Architecture](../../../docs/architecture/action-requests.md) for the full lifecycle.

## Connection Management

`TelegramClientManager` (in `upstream.py`) keeps one connected Telethon client per user id, created lazily on first use and reused while connected. `get_client` raises the structured `telegram_not_connected` (no stored session) / `telegram_session_expired` (Telegram rejects the session) / `telegram_api_error` (connection failure) errors before any read runs. A login or disconnect calls `drop_client` so a stale client never outlives its session, and the plugin's `on_shutdown` hook (`close_all_clients`) disconnects every client when the server stops.

## Testing

`plugins/telegram/tests/` pins the registry/dispatch wiring and prompt gating (`test_telegram_tools.py`, incl. that `quest.app` mounts no `/api/telegram/*` route and exactly the five `/auth/telegram` routes), the send handler's validation and hooks (`test_telegram_send_validation.py`), the login flow with a fake Telethon client (`test_telegram_auth_flow.py`), and the credential loader / legacy migration / client manager (`test_telegram_upstream.py`). The session migration is covered end to end by `tests/test_telegram_session_migration.py` (subprocess alembic against a temp deployment).

## Design Decisions

**Why a plugin?**
Telegram had the same shape as the integrations already packaged as plugins (admin credential card, per-user connection, gated skill, tools, one action request) plus two things the contract lacked: a non-OAuth login and a long-lived upstream connection. The Twilio plugin had already shown that the oauth-kind router hosts any browser-facing login; the missing piece was a shutdown hook, which the port added to the manifest (`on_shutdown`).

**Why keep the login state server-side?**
The pre-plugin flow carried the phone, code hash, and the pre-auth Telethon session between form pages in a signed cookie. Signing prevents tampering, not reading, and a Telethon session string is a credential. `oauth_blob.pending` is encrypted at rest, restart-safe, and lets the server enforce expiry and an attempt cap.

**Why re-encrypt in the migration instead of copying the column?**
Every encrypted column binds its own `<table>.<column>` label into the AES-GCM tag, so a ciphertext moved between columns fails authentication. Migration `d7a1f3c9e2b4` decrypts each session under `users.telegram_session` and re-encrypts it under `user_service_credentials.oauth_blob` in Python (accepting legacy plaintext rows too), then drops the column with a native `DROP COLUMN` (a batch recreate would cascade-delete the rows it just wrote).

**Why persistent client connections?**
Creating a new Telethon connection per request would be slow (TCP + TLS + Telegram auth handshake). The manager keeps connections warm; the `on_shutdown` hook makes sure they are closed cleanly.

**Why are writes an action request instead of a tool?**
Sending messages as the user is sensitive; the approval card guarantees a human sees the recipient and the exact text first, mirroring the Slack and Twitter/X sends.
