# GitHub App Setup

## Overview

Quest integrates with GitHub using the OAuth web flow. Each user connects individually via OAuth to obtain an access token for read-only API operations (repos, issues, pull requests, commits, search, file contents, stargazers).

The admin may register either a classic **OAuth App** or a **GitHub App**; both use the same authorize/callback URLs. OAuth App tokens never expire. GitHub App user tokens expire after 8 hours (unless "Expire user authorization tokens" is turned off in the app's settings), and Quest refreshes them automatically with the accompanying refresh token -- see Token Architecture.

## Key Files

The integration is packaged as the in-tree `plugins/github` plugin (see [Plugins](../../../docs/architecture/plugins.md)):

- Settings > Service Credentials (admin) -- GitHub card (schema-declared by the plugin) managing `client_id` and `client_secret` in the per-service credential store (`github.json`); see [Service Credentials](../../../docs/architecture/service-credentials.md)
- `server_credentials.json` -- legacy `github` section, migrated into the store at startup and used as a runtime fallback by `load_github_client_config()` in `plugins/github/upstream.py`
- `server_credentials.example.json` -- Template showing the legacy file structure
- `plugins/github/upstream.py` -- `GITHUB_SCOPES` (`repo`, `read:org`), client-config loader, credential/connected/needs_reauth hooks, token-blob construction (`build_token_blob`) and the refreshing token loader (`get_github_token`)
- `plugins/github/oauth.py` -- OAuth flow router (start, callback, disconnect), token storage
- `plugins/github/manifest.py` -- the plugin manifest incl. the `api.github.com` `authed_get` service entry (credential loader, Bearer auth injector, allowed-endpoints regex list, service `default_headers`)
- `plugins/github/instructions.md` -- `system:github` skill content
- `db/user_service_credential_store.py` -- token JSON in the github row's `oauth_blob`
- `chat/gemini_api/session.py` -- `invalidate_user_sessions()` called on connect to refresh system prompts
- `api/instructions.py` -- `get_user_connected_services()` checks connector status (plugin loop) for skill/tool gating
- `chat/routes/user.py` -- `get_connectors()` auto-appends the plugin's oauth row for the Data Connections UI

## Credential Configuration

Create either an OAuth App (GitHub Developer Settings > OAuth Apps) or a GitHub App (Developer Settings > GitHub Apps; with a GitHub App, repo/org access comes from the app's permissions and installations rather than OAuth scopes). The app requires:
- A callback URL: `http://localhost:8000/auth/github/callback` (dev) or `https://your-domain.com/auth/github/callback` (production)
- An admin enters the resulting `client_id` and `client_secret` in Settings > Service Credentials (or, legacy, in the `github` section of `server_credentials.json`)

Users connect individually via the Data Connections section in Settings, which opens an OAuth popup flow. See [OAuth Popup Flow](../../../docs/architecture/oauth-popup.md).

## OAuth Scopes

Scopes are defined in `GITHUB_SCOPES` in `plugins/github/upstream.py`: `repo` (full repo access -- GitHub has no read-only repo scope) and `read:org` (read-only organization membership). The callback stores the scopes GitHub actually granted; when the granted set no longer covers `GITHUB_SCOPES`, the connector row shows the "Update Available" re-authorize badge (the plugin's `needs_reauth` hook).

Despite `repo` granting write permissions at the OAuth level, Quest exposes only read-only GitHub API paths through the `allowed_endpoints` regex list on the plugin's `api.github.com` service entry (`plugins/github/manifest.py`). No write operations are reachable.

## Token Architecture

- **Per-user tokens** stored in the generic `user_service_credentials` table (service `github`, token JSON in `oauth_blob`), managed via `db/user_service_credential_store.py`; the pre-plugin `users.github_oauth` column was migrated into rows by Alembic revision `a7c3e91b52d8`
- Fields stored (`build_token_blob()` in `plugins/github/upstream.py`): `access_token`, `token_type`, `scope` (raw comma-separated grant), `scopes` (granted list), `authorized_at`, plus -- only for expiring GitHub App tokens -- `refresh_token`, `expires_at` and `refresh_token_expires_at`
- OAuth App tokens (`gho_`) do not expire -- they remain valid until the user revokes them, and the loader passes them through untouched
- GitHub App user tokens (`ghu_`) expire after 8 hours; the refresh token (`ghr_`) lasts 6 months and rotates on every use, and each refresh also invalidates the previous access token. `get_github_token()` refreshes within 5 minutes of expiry under a per-user lock and persists the new pair, so routines keep working unattended as long as the connection is used at least once every 6 months
- Because a refresh kills the old tokens, the loader reads the stored row (not the possibly outdated user dict of the current turn) whenever the blob carries a refresh token; the `api.github.com` service entry sets `retry_on_401` so a 401 re-runs the loader as a backstop
- A failed refresh (refresh token expired or revoked) falls back to the current access token while it is still valid, then surfaces the `github_oauth_required` reconnect message
- Connections made before refresh support were stored without the refresh token; those users reconnect once
- No shared bot token (unlike Slack) -- all API access is per-user

On connect, `invalidate_user_sessions()` from `chat/gemini_api/session.py` is called so the next conversation picks up the GitHub API documentation in system instructions.

## Connector Status

The connector is "connected" when the user's stored github row has an `oauth_blob.access_token` (the plugin's `connected` hook) AND the admin side is configured -- the standard combined plugin gate applied by `get_user_connected_services()` and the `/connectors` row. While the admin has not entered client credentials, the row is hidden entirely (`available: false`), mirroring Ramp.

## Design Decisions

**OAuth App or GitHub App?**
OAuth Apps are simpler -- tokens do not expire and any user can authorize without an org-level installation. GitHub Apps offer finer-grained permissions but need installation on each org and issue short-lived user tokens. Quest supports both: the callback stores whatever GitHub returns, and refresh logic only engages when a refresh token is present.

**Why refresh on use instead of a background job?**
Routines and chats reach GitHub only through the credential loader, so refreshing there covers every caller without a daemon. Each refresh issues a new 6-month refresh token, so any connection used at least twice a year stays alive.

**Why read-only endpoints only?**
Write operations carry higher risk and require more careful permission management. Read-only access provides immediate value for querying repository data while minimizing risk.

**Why `repo` scope?**
GitHub's OAuth scope model does not offer a read-only scope for repository access. `repo` is the only way to access private repository data.
