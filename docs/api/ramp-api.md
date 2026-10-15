# Ramp API Documentation

This document describes how Quest reads Ramp spend-management data (card transactions, cards, bills, reimbursements, vendors, statements, accounting data) via the `authed_get` tool.

## Overview

Ramp reads use the `authed_get` tool to make GET requests directly to the Ramp developer API at `https://api.ramp.com/developer/v1/...`. Ramp is a per-user OAuth 2.0 connector (like GitHub and Twitter/X): an admin configures a Ramp OAuth client (client id/secret) in Settings > Service Credentials, then each user connects individually via the Data Connections popup flow.

**Ramp admins only.** Ramp lets only Admins and Business Owners of the Ramp business authorize a developer app (anyone else is turned away with "Business not authorized to use this application"), and the resulting token reads the whole business's data rather than the connected user's own. Every user-visible label says so: the Data Connections row and the admin Service Credentials card are titled "Ramp (Ramp admins only)", the row's description reads "Business-wide spend data; only Ramp Admins and Business Owners can connect", the callback error page carries `ADMIN_ONLY_NOTE` (`auth/ramp.py`), the `system:ramp` skill is named "Ramp (Ramp admins only)", and the not-connected error returned to the model says only a Ramp Admin or Business Owner can connect. Ramp's employee-level authorization (`auth_level`, PKCE, the `agent-tools` endpoints used by Ramp MCP and the Ramp CLI) is not used: it requires Ramp to allowlist each client's exact redirect URI, which does not fit self-hosted instances.

Unlike GitHub, Ramp access tokens **expire** and refresh tokens **rotate on every use**, so the credential loader proactively refreshes near-expiry tokens (the Twitter pattern) and the registry entry additionally sets `retry_on_401` as a backstop.

The backend documentation is carried in the `system:ramp` system skill, gated on the `ramp` connection.

## Key Files

| File | Description |
|------|-------------|
| `auth/ramp.py` | OAuth flow: `GET /auth/ramp` (authorize redirect with all read scopes), `GET /auth/ramp/callback` (code exchange via HTTP Basic client auth, stores `ramp_oauth`), `POST /auth/ramp/disconnect`, and `refresh_ramp_token()` (rotating-refresh-token aware; persists the new refresh token) |
| `auth/config.py` | `RAMP_SCOPES` (every read scope the API offers plus `offline_access`; `cards:read_vault` excluded) and `load_ramp_client_config()` (per-service store only, no legacy fallback) |
| `api/ramp.py` | `get_ramp_token(user)` (proactive refresh inside a 5-minute expiry margin) and `get_instructions(base_url)` for the `system:ramp` skill content |
| `chat/gemini_api/authed_get.py` | `api.ramp.com` entry in `_SERVICE_REGISTRY` (`requires_user`, `retry_on_401`, `missing_credentials_error`, allow-list generated from `_RAMP_READ_RESOURCES` plus explicit cards patterns); `_load_ramp_credentials()` (delegates to `get_ramp_token`, returns `None` on failure) and `_inject_ramp_bearer_auth()` |
| `chat/system_skills/catalog.py` | Registers `system:ramp` gated on `requires="ramp"`; `content_builder` delegates to `api.ramp.get_instructions` |
| `db/models.py` | `ramp_oauth` JSON column on `users` (`access_token`, `refresh_token`, `expires_at`, `scope`, `token_type`, `authorized_at`); migration `b7d4f21a8c3e` |
| `chat/routes/user.py` | `ramp` entry in `GET /connectors` with `available` (admin credentials present) and `connected`; `ramp_connected` folded into `/me`'s `has_any_service_connected` |
| `chat/routes/admin.py` | `ramp` in `SERVICE_CREDENTIAL_LABELS` / `_SERVICE_FORM_VIEWS` (flat client id/secret form) and `PUT /admin/service-credentials/ramp` |
| `api/instructions.py` | `"ramp"` key in `get_user_connected_services()`; Ramp section in the instructions document |

## OAuth Flow

- **Authorize URL:** `https://app.ramp.com/v1/authorize` (standard authorization-code; Ramp is a confidential client, so no PKCE — HTTP Basic `client_id:client_secret` authenticates the token exchange).
- **Token URL:** `https://api.ramp.com/developer/v1/token` (also used for `grant_type=refresh_token`).
- **Scopes:** `RAMP_SCOPES` in `auth/config.py` requests every `:read` scope grantable to a Ramp developer app plus `offline_access` (required for refresh tokens). `cards:read_vault` (full card numbers/CVVs) is deliberately excluded, and `applications:read` / `incorporation:read` / `spend_requests:read` (present in Ramp's OpenAPI spec but not enableable on apps) are omitted. The admin-created Ramp app must have all requested scopes enabled or the authorize page rejects the request.
- **Token expiry:** Ramp access tokens expire (`expires_at` stored) and refresh tokens rotate on every refresh; `refresh_ramp_token()` persists the rotated refresh token immediately — losing it permanently breaks the connection.

## Ramp Read Access (via `authed_get`)

The `api.ramp.com` registry entry gates paths with an `allowed_endpoints` regex list built from `_RAMP_READ_RESOURCES` — one pattern per resource root (`transactions`, `bills`, `reimbursements`, `users`, `vendors`, `statements`, `accounting`, `audit-logs`, …) matching the root and any sub-path, so list, detail, and nested reads all work.

Cards are the exception: `/cards/physical` and `/cards/virtual` (list + detail) are allow-listed explicitly so that the card-vault endpoints (`/cards/vault/...`, `/vault/...` — full card numbers behind the `cards:read_vault` scope) stay unreachable. The token endpoints (`/developer/v1/token`, `/token/revoke`) and webhook config are also not proxied.

No `allowed_post_endpoints` list exists, so every POST is rejected — the surface is read-only by construction.

Pagination is cursor-based (`page_size`, `start`; responses carry a `page.next` URL). Amounts are integers in the currency's minor unit. Large responses should use `authed_get`'s `output_file` argument. See `api/ramp.py` (`get_instructions()`) for the LLM-facing path table and examples.

## Connector Availability Gating

`GET /connectors` reports `available: false` for Ramp when no admin has configured Ramp client credentials in the per-service store, and the frontend hides the Data Connections card entirely. Once credentials exist the card appears and users can connect.

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
Ramp reads are standard REST GETs; reusing `authed_get` gets path gating, credential injection, 401 retry, and large-response protection for free, consistent with GitHub/Airtable/SEC EDGAR.

**Why proactive refresh in the loader AND `retry_on_401`?**
The loader refresh (5-minute margin, `get_ramp_token`) handles the common case without burning a failed request. `retry_on_401` covers clock skew or early revocation-of-access-token races. On refresh failure the loader returns `None` so the model sees the actionable `ramp_oauth_required` reconnect message instead of a raw exception.

**Why resource-root allow-list patterns instead of one regex per endpoint?**
The Ramp read surface is ~80 GET endpoints across ~30 resources and all mutations are POST/PATCH/DELETE (rejected wholesale by the verb-scoped allow-list design), so per-resource patterns keep the list maintainable without loosening the read-only guarantee. The only sensitive GETs — card-vault PANs — live under path roots (`cards/vault`, `vault`) that are excluded from the resource list and covered by explicit cards patterns instead.

**Why exclude `cards:read_vault`?**
Full card numbers are PCI-sensitive and unnecessary for spend analytics. Excluding the scope at authorize time *and* the paths at the allow-list keeps PANs unreachable even if Ramp broadens a token's grants.
