# Slack Integration (plugin)

## Overview

Slack access is packaged as the in-tree `plugins/slack` plugin (plugin id
`slack` -- see [Plugins Architecture](../../../docs/architecture/plugins.md)).
The plugin owns the Slack *integration surface*: the admin credential card,
the per-user OAuth connection, the nine dynamic tools, the two send action
requests, the `system:slack` skill, and the sandbox-script bridge entries.

The Slack-*driven* conversation machinery -- the Socket Mode worker, the
`slack_conversations` table, `slack_reply` wait handles,
`SLACK_TOP_LEVEL_TOOLS` / `send_slack_reply_and_get_response`, and the
Slack Reply Mode prompt -- stays core; see
[Slack Socket Mode](../../../docs/architecture/slack-socket-mode.md). It
shares the same admin credential store entry and resolves users from the
same `user_service_credentials` rows this plugin's OAuth callback writes.

## Plugin layout

| File | Role |
|------|------|
| `manifest.py` | `get_plugin()` -- oauth-kind manifest with both grandfather lists (`unprefixed_action_types`, `unprefixed_tools`) |
| `upstream.py` | Slack Web API client (shared `httpx.AsyncClient` pool, 429/timeout retry, `MAX_LIMIT = 50` cap with `ToolResultWithNotices` pagination advisories), the read/send endpoint functions, `resolve_team_id()` E->T Enterprise-id resolution, and the credential helpers (`get_user_slack_oauth`, `slack_connected`, `slack_is_configured`) |
| `tools.py` | The nine `PluginTool`s (specs + `(ctx, args)` handlers), incl. `find_slack_channel` and `send_slack_dm_to_self` |
| `handlers.py` | `SendSlackMessageHandler` / `SendSlackDmHandler` action requests, Block Kit attribution blocks, and the preview resolvers (channel name, thread context) via the `enrich_params_for_preview` hook |
| `oauth.py` | `/auth/slack` OAuth router (same URLs as pre-plugin, plus `POST /auth/slack/disconnect`); tokens land in `user_service_credentials.oauth_blob` |
| `instructions.md` | The `system:slack` skill body |
| `docs/slack-app-setup.md` | Creating and configuring the Slack app |

## Credentials

- **Admin (server) credentials** -- the `slack` service credential store
  entry (`data/service_credentials/slack.json`, legacy
  `server_credentials.json` `slack` section still migrating post-plugin-load):
  OAuth app `client_id`/`client_secret` (required for the per-user
  connection) plus the optional shared `bot_token` (`xoxb-...`, used for
  `send_slack_dm_to_self`, `list_slack_teams`, and Enterprise-id
  resolution) and `socket_mode_token` (`xapp-...`, consumed by the core
  Socket Mode worker). The loaders (`load_slack_client_config`,
  `load_slack_bot_token`, `load_slack_socket_mode_token`) stay in core
  `auth/config.py` because the core Socket Mode worker needs them too.
- **Per-user connection** -- oauth kind. The callback stores
  `{access_token, default_team_id, user_id, authorized_at}` in the user's
  `user_service_credentials` row (service `slack`, `oauth_blob`); migration
  `f3a9c5d81b42` moved the pre-plugin `users.slack_oauth` column there.
  The core Socket Mode worker's reverse lookup
  (`db/user_store.py get_user_by_slack_user_id`) json_extracts
  `$.user_id` from the same rows.

## Tools (all tool_call-routed, `requires_service="slack"`)

Reads (user token unless noted): `list_slack_teams` (bot token),
`list_slack_conversations`, `get_slack_conversation_history`,
`get_slack_conversation_replies`, `search_slack_messages`,
`get_slack_user_info`, `list_slack_users`, `find_slack_channel`
(cross-workspace channel-name substring search). List reads cap at 50
per call with a paginate-via-`next_cursor` notice merged into the JSON
result.

`send_slack_dm_to_self` sends a bot-delivered DM to the user themselves
(no approval; optional conversation-workspace file attachments via
Slack's external upload flow, max 10 files / 50 MB each; in a project
conversation project files must be copied in first with `copy_file`). It is the one connector tool
allowed in public-project conversations, via the core-owned
`_PUBLIC_ALLOWLIST_MIGRATED_TOOLS` exemption in `config/plugins.py`.
Because the text is model-written and never shown on an approval card,
the text-only `chat.postMessage` send sets `unfurl_links` and
`unfurl_media` to false: otherwise a prompt-injected URL would make
Slack's link crawler fetch it, leaking whatever the model put in the
query string (e.g. its prompt-visible API key) to an arbitrary host.
The recipient stays the user alone; only Slack's server-side URL fetch
is suppressed.

All nine tools are script-bridge invocable (`POST /api/tool-call`); the
tool names predate the plugin and ride on the manifest's
`unprefixed_tools` grandfather list.

## Action requests

`send_slack_message` (preferred: any channel/group/DM by `channel_id`,
optional `thread_ts` threaded reply; channel-id and thread-ts formats
validated at proposal time; channel name + thread context resolved into
the approval card by the handler's `enrich_params_for_preview` hook) and
the legacy `send_slack_dm` (`user_id` + `message`). Both send with the
user's own OAuth token and append a Block Kit "created using Quest"
attribution context block (linking to `app_base_url` when configured).
Both type names are persisted in old `action_requests` rows and ride on
`unprefixed_action_types`.
