# Ramp App Setup

## Overview

Quest integrates with Ramp (spend management: corporate cards, bills, reimbursements) using a standard OAuth 2.0 authorization-code flow. Each user connects individually via OAuth to obtain an access token for read-only API operations. Ramp access tokens expire and refresh tokens rotate on every use, so Quest stores the refresh token and refreshes automatically.

The connector is for **Ramp admins only**: Ramp lets only Admins and Business Owners of the Ramp business authorize a developer app, and the token they obtain reads business-wide data. Regular Ramp employees cannot connect, which is why the connector is labelled "Ramp (Ramp admins only)" in Settings > Data Connections and Settings > Service Credentials (see [Ramp API](../api/ramp-api.md)).

## Key Files

- Settings > Service Credentials (admin) -- Ramp card managing `client_id` and `client_secret` in the per-service credential store; see [Service Credentials](../architecture/service-credentials.md). Ramp has no legacy `server_credentials.json` fallback.
- `auth/config.py` -- `RAMP_SCOPES` constant (all read scopes + `offline_access`) and `load_ramp_client_config()`
- `auth/ramp.py` -- OAuth authorize/callback/disconnect endpoints and `refresh_ramp_token()`
- `api/ramp.py` -- `get_ramp_token()` (proactive refresh) and the `system:ramp` skill content
- `chat/gemini_api/authed_get.py` -- `api.ramp.com` entry in `_SERVICE_REGISTRY` (read-only allow-list)
- `db/models.py` -- `ramp_oauth` JSON column on the `users` table

## Credential Configuration

Create a Ramp developer app at Ramp > Settings > Developer API (an owner/admin of the Ramp business must do this):

1. Create a new app and enable **all read scopes** (Quest requests every `:read` scope plus `offline_access`; it does not request `cards:read_vault` or any write scope). Enabling a scope Quest requests but the app lacks causes the authorize page to reject the request.
2. Add the redirect URI: `https://your-domain.com/auth/ramp/callback` (or the raw-IP / `oauth_hostname` dev equivalent -- the callback is built via `oauth_base_url()`).
3. An admin enters the resulting `client_id` and `client_secret` in Settings > Service Credentials > Ramp.

Until an admin configures these credentials, the Ramp card in Settings > Data Connections is hidden for all users (`available: false` from `GET /connectors`).

Ramp Admins and Business Owners then connect individually via the Data Connections section in Settings, which opens an OAuth popup flow. See [OAuth Popup Flow](../architecture/oauth-popup.md). Anyone else is refused by Ramp's consent screen; the callback error page explains that only a Ramp admin can connect.

## OAuth Scopes

Scopes are defined in `RAMP_SCOPES` in `auth/config.py`: every read scope grantable to a Ramp developer app (transactions, cards, bills, reimbursements, accounting, audit logs, ...) plus `offline_access` for refresh tokens. `cards:read_vault` (full card numbers) is deliberately excluded, and the `applications:read` / `incorporation:read` / `spend_requests:read` scopes that appear in Ramp's OpenAPI spec are absent because Ramp apps cannot enable them -- requesting any scope the app lacks makes the authorize page reject the whole request.

Read-only access is additionally enforced app-side by the `allowed_endpoints` regex list on the `api.ramp.com` entry in `_SERVICE_REGISTRY` (`chat/gemini_api/authed_get.py`): only GET paths are allow-listed, no POST allow-list exists, and the card-vault paths are excluded.

## Token Architecture

- **Per-user tokens** stored as the `ramp_oauth` JSON column in the `users` table, managed via `db/user_store.py`
- Fields stored: `access_token`, `refresh_token`, `expires_at`, `scope`, `token_type`, `authorized_at`
- Access tokens expire; `get_ramp_token()` in `api/ramp.py` refreshes when the token is within 5 minutes of expiry, and the `authed_get` entry sets `retry_on_401` as a backstop
- Ramp **rotates refresh tokens on every use**: `refresh_ramp_token()` persists the new refresh token immediately; a lost rotation permanently breaks the connection (the user must reconnect)
- No shared bot token -- all API access is per-user

On connect/disconnect, `invalidate_user_sessions()` is called so the next conversation picks up the changed skill availability.

## Connector Status

The connector is "connected" when the user's `ramp_oauth` data is not None. Checked in `get_user_connected_services()` in `api/instructions.py`, `get_current_user_info()` in `chat/routes/user.py`, and `get_connectors()` in `chat/routes/user.py` (which also reports `available` from the admin credential presence).

## Design Decisions

**Why no PKCE (unlike Twitter/X)?**
Ramp is a confidential client: the token endpoint authenticates with HTTP Basic `client_id:client_secret`. PKCE adds nothing when a client secret is already required, so the flow mirrors the GitHub flow's simpler shape (now `plugins/github/oauth.py`) with Twitter's refresh handling grafted on.

**Why request all read scopes up front?**
The integration surface is the whole read API via `authed_get`; per-feature incremental consent would force repeated re-auth popups. Write scopes and the card-vault scope are excluded, so the token cannot mutate anything or read card numbers even before the app-side path allow-list.

**Why is the connector hidden until an admin configures credentials?**
Without a client id/secret the authorize redirect can only fail. The `available: false` pattern hides the card so users on instances without a Ramp app never see a dead Connect button.
