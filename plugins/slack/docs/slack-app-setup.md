# Slack App Setup

## Overview

Quest integrates with Slack using a two-tier token architecture: a shared bot token (installed once by an org admin, stored server-side) for bot-sent DMs and org-wide queries, and per-user OAuth tokens for read operations and user-initiated writes.

## Key Files

- Settings > Service Credentials (admin) -- Slack card managing `client_id`, `client_secret`, and the optional `bot_token` / `socket_mode_token` in the per-service credential store; see [Service Credentials](../../../docs/architecture/service-credentials.md)
- `server_credentials.json` -- legacy `slack` section, used as a fallback when the store has nothing
- `server_credentials.example.json` -- Template showing the legacy file structure
- `auth/config.py` -- `load_slack_client_config()`, `load_slack_bot_token()`, `load_slack_socket_mode_token()`, Slack OAuth scope constants
- `plugins/slack/oauth.py` -- OAuth callback handling, token storage
- `plugins/slack/upstream.py` -- Slack Web API helpers: `_get_slack_client()` shared connection pool, `resolve_team_id()` for E-prefix to T-prefix resolution, `_slack_request_with_retry()` retry helper
- `db/user_service_credential_store.py` -- Manages the per-user `slack` row (`oauth_blob`) in `user_service_credentials`
- `api/instructions.py` -- `get_user_connected_services()` checks connector status
- `chat/routes/user.py` -- `get_connectors()` for Data Connections UI
- `plugins/slack/tools.py` -- `_tool_find_slack_channel()` and the other Slack LLM tools
- `plugins/slack/handlers.py` -- `_build_slack_blocks()`, `_quest_attribution_text()` for user-sent message formatting

## Credential Configuration

A Slack app must be created at api.slack.com/apps. The app needs:
- **User token scopes** for read/write operations (defined in `auth/config.py`)
- **Bot token scopes**: `chat:write` (bot-sent DMs), plus `files:write` and `im:write` (file attachments on `send_slack_dm_to_self` self-DMs: the upload flow and its `conversations.open` DM-channel lookup). Add `im:history` and `im:read` when enabling Socket Mode (see below)
- **Redirect URL**: `http://localhost:8000/auth/slack/callback` (dev) or `https://your-domain.com/auth/slack/callback` (production)

An org admin installs the app once to obtain the shared bot token (`xoxb-...`). Individual users connect via the Data Connections section in Settings. See [OAuth Popup Flow](../../../docs/architecture/oauth-popup.md).

An admin enters the `client_id`, `client_secret`, and `bot_token` in the Slack card under Settings > Service Credentials (or, legacy, in the `slack` section of `server_credentials.json`). When Socket Mode is enabled, the App-Level Token goes in the same place as `socket_mode_token`.

## Socket Mode Configuration

Socket Mode lets Quest receive incoming user DMs to the bot over a WebSocket. Runtime behaviour and graceful-degradation semantics are in [Slack Socket Mode Architecture](../../../docs/architecture/slack-socket-mode.md). For the Slack app itself, the following UI settings must all be configured -- events will not flow without every one of them:

- **Socket Mode**: enabled. Generate an App-Level Token with the `connections:write` scope; save it as the Slack `socket_mode_token` (format: `xapp-...`). Loaded by `load_slack_socket_mode_token()` in `auth/config.py`
- **Bot Token Scopes**: add `im:history` and `im:read` (in addition to `chat:write`). Required for the bot to see DM events
- **Event Subscriptions**: enable events and subscribe the **bot** to the `message.im` event
- **App Home -> Show Tabs**: turn **Messages Tab** on, and check "Allow users to send Slash commands and messages from the messages tab". Without this, users see no DM input for the bot in the Slack UI even when all backend plumbing is correct
- **Reinstall** the app to the workspace after any scope change so the new bot token includes the updated scopes

Only one process can hold a Socket Mode connection per App-Level Token. When multiple Quest instances share credentials, set `socket_mode_token` on only the instance that should receive DMs; omit it everywhere else. `load_slack_socket_mode_token()` returns `None` when absent, and `chat/slack_socket_mode.py` disables itself in that case.

## Token Architecture

### Shared Bot Token

- Stored in the Slack service credentials (`bot_token`), loaded by `load_slack_bot_token()` in `auth/config.py`
- Used for: self-DMs via the `send_slack_dm_to_self` tool (`chat.postMessage` with link/media unfurling disabled for text-only; file sends use `conversations.open` + the `files.getUploadURLExternal`/`files.completeUploadExternal` upload flow so text and files land as one message), `auth.teams.list` for workspace listing and E-prefix team ID resolution
- Installed once by an org admin

### Per-User Token

- Stored as `slack_oauth` JSON column in `users` table (`data/quest.db`), managed via `db/user_store.py`
- Fields: `access_token`, `default_team_id`, `user_id`, `authorized_at`
- Used for: read operations (search, channels, history, users), channel search via `find_slack_channel` LLM tool, user-initiated writes via `send_slack_message` action requests

### Cross-Workspace Queries

Several read tools (`list_slack_conversations`, `search_slack_messages`, `list_slack_users`) accept an optional `team_id` parameter. When omitted, `resolve_team_id()` in `plugins/slack/upstream.py` falls back to the user's `default_team_id` from OAuth data.

For org-level installs, `auth.test` returns an Enterprise ID (E-prefix) instead of a workspace ID (T-prefix). `resolve_team_id()` detects E-prefix IDs and resolves them to T-prefix workspace IDs via `auth.teams.list` using the shared bot token.

## Connector Status

Connected when `slack_oauth` contains an `access_token` field. Checked in `get_user_connected_services()` in `api/instructions.py`, `get_current_user_info()` in `chat/routes/user.py`, and `get_connectors()` in `chat/routes/user.py`.

## Message Attribution

Bot-sent DMs (via the `send_slack_dm_to_self` tool) are identified as coming from the Quest bot. User-token messages (via `send_slack_message` action requests) include a Quest attribution suffix rendered as a Block Kit context block. See `_build_slack_blocks()` and `_quest_attribution_text()` in `plugins/slack/handlers.py`.

## Design Decisions

**Why a shared bot token instead of per-user bot tokens?**
The bot token represents the Slack app installation, not an individual user. Installing once at the org level provides a single token for write operations and org-wide queries, avoiding redundant installations.

**Why store default_team_id per user?**
In org-level installs, API methods require a `team_id` parameter. The `default_team_id` (from `auth.test` during OAuth) serves as the default workspace so users don't specify `team_id` on every request.

**Why check `access_token` instead of just `slack_oauth` presence?**
Ensures only users who completed the OAuth flow (with a valid token) are considered connected.

**Why does `find_slack_channel` call `auth.teams.list` directly instead of `resolve_team_id()`?**
`resolve_team_id()` returns a single workspace ID (first match). `find_slack_channel` needs all workspace IDs to search channels across every workspace in the organization.

**Why a shared connection pool?**
Per-request `httpx.AsyncClient` creation causes TCP/TLS churn and `ConnectTimeout` errors under concurrent load. A module-level shared client (`_get_slack_client()` in `plugins/slack/upstream.py`) with `max_connections=20` and `max_keepalive_connections=10` reuses established connections.

**Why retry with backoff?**
`_slack_request_with_retry()` in `plugins/slack/upstream.py` retries up to 3 times on `httpx.TimeoutException` (1s wait) and HTTP 429 (respects `Retry-After`, capped at 60s), making the proxy resilient to transient failures.
