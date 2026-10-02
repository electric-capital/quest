# Settings Data Connections UI Pattern

This document describes the Data Connections section of the Settings modal: how connector rows are rendered, what states they expose, and the rules that all connectors must follow.

## Overview

The Data Connections section of the Settings modal (Settings > Data Connections) lists **only the connections the user has added** (i.e. rows whose `connected` is true) -- the list builds up one connection at a time as the user adds them. New connections are added through an explicit flow: an "+ Add Connection" button opens a picker of the supported services not yet connected, and picking one triggers that service's flow (an OAuth popup, or a key-entry step).

Everything is **fully data-driven**: `GET /connectors` returns a LIST of row objects covering every supported service (connected or not) and `DataConnectionsSection.tsx` renders both the connected list and the add picker generically from it -- there is no per-service JSX, so adding a connector (core or plugin-provided) is a backend-only change. Each connected row shows the connector's label (plus an optional description line), a status badge, and one or two action controls. The shape of a row's behavior depends on its `kind`: `"oauth"` (popup-based) or `"api_key"` (key form).

This connected-list + add-flow shape is also the seam for a planned evolution: allowing multiple connections of the same service under different auth (e.g. two Google accounts). The list-of-added-connections UI already reads like a list of connection instances; only the backend row identity (today one row per service) will need to change.

## The Row Contract

Each `/connectors` row carries:

- `service`, `label`, optional `description` -- identity and display text
- `kind`: `"oauth"` or `"api_key"`
- `connected`, and for oauth rows optionally `needs_reauth`
- `available` -- optional; `false` hides the row entirely (from both the connected list and the add picker). Every service needing admin-configured server-side integration credentials computes it:
  - the core oauth rows (Google Services, Ramp) probe their canonical credential loader (`load_google_oauth_config` / the ramp store read) via the `_server_configured` helper in `get_connectors()`, and
  - plugin rows use `plugin_server_available()` (e.g. a plugin without its server-side base URL, the Slack plugin without OAuth client credentials, or the Telegram plugin without its MTProto app credentials).

  Airtable carries no flag -- the user supplies their own PAT, so there is nothing server-side to configure.
- oauth rows: `connect_url` (the popup URL, `?popup=1` included) -- the frontend never builds auth URLs itself, which also removed the old popup-blocked-link URL special cases
- api_key rows: `key_url` + `key_field` (the save form POSTs `{[key_field]: key}` to `key_url`), `key_placeholder`, optional `key_hint` (last characters of the stored key), and `disconnect_url`; optional `key_help` (a short plain-text sentence or two) and `key_help_links` (`{label, url}` list, https only) rendered above the key form in the add-connection key-entry step to tell the user which credential to create and where (the Tailscale plugin links the admin console's OAuth-client and API-token pages plus the two KB articles)

Types are defined in `frontend/src/api/types.ts` (`ConnectorRow`, `ConnectorsResponse`).

## The Connected List

The section body is the list of connected rows (`connected && available !== false`). When it is empty, a short empty-state line replaces it ("No connections yet. ..."). Not-connected services never render as rows here -- they only appear in the add picker.

### OAuth Connected Rows

OAuth rows (core: Google Services, Ramp; plugin: e.g. Slack, GitHub, Twitter/X, Telegram) show:

- Badge: green "Connected" label (CSS class `connector-badge-ok`)
- Button: single "Reconnect" button (CSS class `connector-btn-reconnect`)
- No disconnect button

Clicking "Reconnect" opens the OAuth popup at the row's `connect_url` -- the same URL the add flow uses to connect. This re-authenticates the user, replacing the stored token with a fresh one. There is intentionally no Disconnect button for OAuth connectors; reconnecting is the correct way to refresh or replace a connection.

Any oauth row may report a needs-reauth state when `connected && needs_reauth` (today only Google Services computes it, from missing OAuth scopes):

- Badge: yellow "Update Available" label (CSS class `connector-badge-warn`)
- Button: "Re-authorize" (CSS class `connector-btn-connect`), which opens the same OAuth popup

### API Key Connected Rows

API key rows (Airtable, plus api_key-kind plugin rows) show:

- Badge: green "Connected" label
- Key hint: the row's `key_hint` (`...hint`)
- A "Disconnect" control (CSS class `connector-btn-disconnect`) that POSTs to the row's `disconnect_url`

Unlike OAuth connectors, API key connectors do show a Disconnect control because there is no "Reconnect" concept -- a new key must be entered explicitly. Disconnecting removes the row from the connected list; the service reappears in the add picker.

## The Add Flow

Below the connected list, an "+ Add Connection" button (hidden when every supported service is already connected) opens an inline picker panel showing the addable services -- rows with `available !== false && !connected` -- as a **grid of icon tiles**, each with the service's brand icon, label, and a kind hint ("Sign in" for oauth, "API key" for api_key; the description, when present, becomes the tile's hover title).

Icons come from `serviceIcons.tsx` (`ServiceIcon`, keyed by the row's `service` id): inline brand SVGs for the known services plus a generic plug fallback for unknown ones, so the picker stays data-driven -- an icon entry is optional polish, not a requirement for a new connector. Connected rows reuse the same icon at a smaller size. Picking a tile triggers that service's flow:

- **oauth**: the picker closes and the OAuth popup opens at the row's `connect_url` (same `handleOAuthConnect` path as Reconnect). When the flow completes, the status refresh makes the new connection appear in the list.
- **api_key**: the panel advances to a key-entry step ("Connect <label>", with a Back control) rendering an optional help paragraph (`key_help` text + `key_help_links` opened in a new tab) above the `ApiKeyForm` (placeholder from `key_placeholder`); Save posts the key to `key_url` as `{[key_field]: key}` via `saveConnectorKey()` in `frontend/src/api/client.ts`, then closes the panel and refreshes. The key-entry step re-resolves the picked service against the freshest connector list, so a background refresh (or the service getting connected in another tab) drops it back to the picker instead of acting on stale row data.

The add flow is state-machine simple: `closed` -> `pick` -> (`enter_key` for api_key picks). There is no persisted "added but not connected" state -- a connection exists exactly when its credential does, so an abandoned OAuth popup or key form leaves nothing behind.

## Connector Status Backend

`get_connectors()` in `chat/routes/user.py` assembles the row list. Each core row's `connected` boolean is derived from the presence of a non-null credential on the `User` model, and each row's URLs point at the existing per-service auth endpoints (`/auth/<service>...`). See `get_connectors()` for the specific checks per connector.

## Plugin Rows

After the core rows, `get_connectors()` appends one `api_key` row per loaded plugin that declares an `api_key`-kind `UserConnectionSpec` (see `config/plugin_types.py`):

- `label` from the manifest,
- `key_url`/`disconnect_url` pointing at the generic key routes (`POST /auth/service-key/{plugin id}` and `.../remove` in `auth/service_key.py`, backed by the `user_service_credentials` table -- see [Database](database.md)),
- `connected` from the spec's `connected` predicate over the user's stored row,
- `key_hint` (last 4 of the key) unless the spec sets `key_hint: False`, and
- `available` from the plugin's server-level configuration (`plugin_server_available()` in `config/plugins.py`) so an unconfigured plugin's row is hidden exactly like Ramp's.

A plugin can supply a custom key placeholder via `UserConnectionSpec.key_placeholder`, and key-entry guidance via `UserConnectionSpec.key_help` / `key_help_links` (`HelpLink(label, url)` tuples; the loader requires `https://` URLs and rejects both fields on oauth-kind specs).

Plugins with an `oauth`-kind connection get an `oauth` row instead:

- `connect_url` follows the `/auth/<plugin id>?popup=1` convention (served by the plugin's own router, mounted at startup),
- `connected` from the spec's predicate over the stored row's `oauth_blob`,
- `needs_reauth` from the spec's optional hook (e.g. the GitHub plugin's granted-scope check driving the "Update Available" badge), and
- the same `available` server-config gating -- the GitHub row arrives this way from `plugins/github`.

The same server-AND-user combination gates every plugin's skills and tools via `get_user_connected_services()`.

## Connector-Gated Settings Sections

The same `GET /connectors` rows also gate the per-connector settings sections in the Settings nav (`CONNECTOR_SECTIONS` in `SettingsModal.tsx`). These are listed after the main sections (Data Connections, Memories, Guides, Skills, Inference API) and only while their backing service is usable for the current user:

| Section | Connector row | Shown when |
|---------|---------------|------------|
| Slack | `slack` | the row is available AND `connected` |
| Gmail | `google_services` | the row is available AND `connected` |
| SMS Messages | `twilio` | the row is available (plugin loaded + admin-configured) |

Slack's default-model picker and Gmail's Quest-label list are meaningless without the user's own connection, so those two sections require `connected`; the Twilio SMS Messages editor only needs the plugin to be available since its phone verification happens in the section itself. The modal loads the rows on open and reloads them through the same `refreshConnectionStatus` callback Data Connections invokes after every connect/disconnect, so a section appears the moment its connection is added and disappears when it is removed; if the currently selected section disappears, the modal falls back to Data Connections instead of showing an empty pane.

## handleOAuthConnect Flow

When an oauth service is picked in the add flow, or a Reconnect / Re-authorize button is clicked:

1. `handleOAuthConnect(url)` in `DataConnectionsSection.tsx` is called with the row's `connect_url`
2. `openOAuthPopup(url)` in `frontend/src/utils/oauthPopup.ts` opens a centered 600×700 popup window
3. A `window.addEventListener('message', ...)` handler waits for `oauth_callback_success` or `oauth_callback_error` from the popup
4. A `setInterval` polls `popup.closed` every 500ms as a fallback
5. On success (either via `postMessage` or popup close detection): `fetchConnectors()` re-fetches connector status and updates the UI
6. If the popup is blocked by the browser, an inline notice links directly to the same `connect_url` in a new tab

See [OAuth Popup Flow](oauth-popup.md) for the full postMessage protocol.

## Design Decisions

**Why a list of rows instead of the old fixed-key `connectors` dict?**
The dict shape required a hand-written JSX block (plus types and client functions) per connector, and availability/popup-URL special cases accumulated per service. The list shape moves every per-service fact to the backend row, which is also the seam plugin-provided connectors will use (see the plugin architecture plan, Phase 3).

**Why show only connected rows instead of every supported service?**
As core and plugin connectors accumulate, a fixed roster of mostly-"Not Connected" rows buries the user's actual connections in noise. Listing only added connections keeps the section proportional to what the user uses, and the add picker remains the single discovery surface for what's supported. It also sets up the planned multiple-connections-per-service evolution (e.g. two Google accounts): the list already reads as connection instances rather than a service catalog.

**Why is there no persisted "added but not connected" state?**
`connected` (credential presence) is the sole source of truth for list membership. A separate added-services list would need its own storage and could drift from reality (added-but-broken rows, connected-but-not-added rows from older flows). An abandoned add flow simply leaves nothing behind, which is the intuitive outcome.

**Why no Disconnect button for OAuth connectors?**
Reconnecting is the primary use case -- users reconnect when their token has expired, when they want to re-authorize, or when they've changed their account. A separate Disconnect button adds friction and rarely serves a legitimate need. Removing the token is a destructive action that leaves the user with degraded functionality; there is no value in "connecting to nothing." If a user wants to stop sharing data, they can revoke access from the external service's settings page instead.

**Why Reconnect instead of a separate "Refresh Token" flow?**
Reconnecting through the full OAuth flow is simpler and more robust than a dedicated refresh endpoint. It handles all token refresh scenarios (expired token, revoked access, scope changes) with a single code path.

**Why are API key connectors different?**
API key connectors have no external OAuth provider to redirect to, so "Reconnect" has no meaning. The user enters a key directly; they need an explicit Disconnect action to clear it before entering a new one.
