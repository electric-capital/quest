# OAuth Popup Flow Architecture

This document describes the OAuth popup flow used by connector buttons (Google Services, Slack, GitHub, Telegram, Twitter/X) in the Settings panel. Instead of navigating the user away from the app, OAuth flows open in a centered popup window that communicates completion back to the opener via `postMessage`.

## Popup Flow Overview

1. User clicks a Connect/Reconnect button in the Data Connections section of the Settings panel
2. `DataConnectionsSection` calls `handleOAuthConnect(url, service)` with the `?popup=1` query parameter appended
3. `openOAuthPopup()` in `frontend/src/utils/oauthPopup.ts` opens a centered 600x700 popup window
4. The popup navigates through the OAuth provider's consent flow (Google, Slack, or Telegram's multi-step auth)
5. On completion, the backend callback returns an HTML page (instead of redirecting to `/`) that calls `window.opener.postMessage()` and closes itself
6. `DataConnectionsSection` receives the `postMessage` event, re-fetches connector status, and calls `refreshConnectionStatus()` on `AuthContext`
7. The settings panel updates to reflect the new connection status without a page reload

## Popup Flag Propagation

Each connector passes the `popup=1` flag through the OAuth flow differently, because each has different state management requirements:

### Google Services

The popup flag rides in the signed, session-bound `google_services_oauth_state` cookie minted by `auth/oauth_state.py` (see [Auth](auth.md#oauth-state-cookies)); the `state` query parameter itself is just the CSRF nonce.

**Flow**: `?popup=1` on `/auth/google-services` --> `mint_oauth_state(..., popup=True)` sets the signed cookie, nonce goes in `state` --> Google OAuth redirect --> callback `verify_oauth_state()` returns the payload, checks `popup` field

**Key code**: `auth_google_services()` and `auth_google_services_callback()` in `auth/google_services.py`

### Slack

The popup flag is stored alongside the CSRF nonce in the signed, session-bound `slack_oauth_state` cookie (payload `{"csrf": "<nonce>", "uid": <user id>, "popup": true}`, minted and verified by `auth/oauth_state.py` -- the same pattern every other oauth-kind connector uses). The OAuth flow only requests user token scopes (for read operations); no bot token scopes are included in the authorization URL because the bot token is a shared credential installed by an admin (see [Slack App Setup](../../plugins/slack/docs/slack-app-setup.md)).

**Flow**: `?popup=1` on `/auth/slack` --> signed state cookie with `popup: true` --> Slack OAuth redirect (user scopes only) --> callback `verify_oauth_state()` returns the payload, checks `popup` field

**Key code**: `auth_slack()` and `auth_slack_callback()` in `plugins/slack/oauth.py`

### Telegram

Telegram (the `plugins/telegram` plugin) has no OAuth provider: the popup is a single plugin-served page that walks the user through Telegram's own login (phone number, the code sent to their Telegram app, optional cloud password) by calling the plugin's JSON endpoints with `fetch`.

Nothing about the flow lives in a cookie: the in-flight login (phone, `phone_code_hash`, the not-yet-authorized Telethon session string, stage, expiry, attempts) is parked server-side in the user's `user_service_credentials` row under `oauth_blob.pending`, encrypted at rest, and only replaced by the authorized `session` on success.

There is no popup flag to carry through -- the page always posts `oauth_callback_success` to `window.opener` when one exists and redirects home otherwise (the Twilio plugin's pattern).

**Flow**: `?popup=1` on `/auth/telegram` --> page `POST`s `/auth/telegram/send-code` --> `POST /auth/telegram/verify` (answers `needs_2fa: true` for accounts with a cloud password) --> optional `POST /auth/telegram/2fa` --> `postMessage` + `window.close()`

**Key code**: `telegram_connect_page()`, `telegram_send_code()`, `telegram_verify_code()`, and `telegram_verify_password()` in `plugins/telegram/auth.py`

### Twitter/X

The popup flag is stored alongside the CSRF token and PKCE code verifier in the `twitter_oauth_state` cookie as JSON: `{"csrf": "<token>", "code_verifier": "<verifier>", "popup": true}`. This cookie is also used for PKCE security in the OAuth 2.0 PKCE flow.

**Flow**: `?popup=1` on `/auth/twitter` --> JSON cookie `{"csrf": "...", "code_verifier": "...", "popup": true}` --> Twitter OAuth redirect (PKCE) --> callback reads cookie, parses JSON, checks `popup` field

**Key code**: `auth_twitter()` and `auth_twitter_callback()` in `plugins/twitter/oauth.py` (the Twitter/X plugin's `oauth_router`, mounted under `/auth/twitter` by `mount_plugin_oauth_routers()`)

## postMessage Protocol

When popup mode is active, the backend callback returns an HTML page instead of redirecting to `/`. Two HTML page generators handle the success and error cases:

### Success: `generate_oauth_popup_success_page(service)`

Returns an HTML page that:
1. Calls `window.opener.postMessage()` with `{ type: 'oauth_callback_success', service: '<service>' }` scoped to `window.location.origin`
2. Calls `window.close()` to close the popup
3. Falls back to `window.location.href = '/'` if `window.opener` is null (popup was opened as a new tab)

### Error: `generate_oauth_popup_error_page(service, error_message)`

Returns an HTML page that:
1. Displays the error message
2. Provides a "Close this window" button that calls `window.opener.postMessage()` with `{ type: 'oauth_callback_error', service: '<service>', error: '<message>' }` and closes the popup
3. Provides a "Return to app" link as a fallback

### Message Types

| Message Type | Fields | Sent When |
|---|---|---|
| `oauth_callback_success` | `type`, `service` | OAuth flow completed successfully |
| `oauth_callback_error` | `type`, `service`, `error` | OAuth flow failed |

## Frontend Event Handling

The `DataConnectionsSection` component in `frontend/src/components/settings/DataConnectionsSection.tsx` uses three mechanisms to detect OAuth completion:

### 1. postMessage Listener

A `window.addEventListener('message', ...)` handler listens for `oauth_callback_success` and `oauth_callback_error` messages. On success, it re-fetches connector status via `fetchConnectors()` and calls `refreshConnectionStatus()`. The listener verifies `event.origin` matches `window.location.origin` for security.

### 2. Popup Close Polling

A `setInterval` polls `pendingOAuthPopup.closed` every 500ms as a fallback. If the popup closes without sending a `postMessage` (e.g., user closes it manually, or the popup page fails to load the script), the polling detects the closure and re-fetches connector status. This ensures the settings panel updates even if the `postMessage` mechanism fails.

### 3. Popup-Blocked Fallback

If `window.open()` returns `null` (browser blocked the popup), `DataConnectionsSection` displays an inline notice with a direct `<a href>` link that opens the OAuth URL in a new tab via `target="_blank"`. The link includes the `?popup=1` parameter so the completion page still attempts `postMessage`.

## refreshConnectionStatus()

`refreshConnectionStatus()` in `frontend/src/contexts/AuthContext.tsx` calls `checkSession()` (which hits `GET /app/api/me`) to refresh the `googleServicesConnected` and `hasAnyServiceConnected` flags. This ensures the app-level state reflects the latest connection status after an OAuth popup completes. It is called both on `postMessage` receipt and on popup close detection.

## Non-Popup Fallback

When `?popup=1` is not present on the auth endpoints, the original behavior is preserved: callbacks redirect to `/` via `RedirectResponse("/", status_code=303)`. This maintains backward compatibility for direct navigation to auth URLs.

## Design Decisions

**Why popup windows instead of in-page redirects?**
OAuth flows navigate to external providers (Google, Slack) or multi-step internal pages (Telegram). Navigating the main window away from the app disrupts the user's context -- the settings modal closes, unsaved state is lost, and the user must re-navigate after returning. Popup windows keep the main app intact while the OAuth flow happens in a separate window.

**Why `postMessage` instead of polling an API?**
`postMessage` provides immediate, event-driven notification when the OAuth flow completes. The alternative -- polling the connector status API until it changes -- would introduce latency, unnecessary network traffic, and race conditions. The popup page knows exactly when the flow finishes and can notify the opener instantly.

**Why encode the popup flag in the OAuth state parameter for Google Services?**
Google Services uses a standard OAuth redirect flow where the only way to pass data from the initiation to the callback is the `state` parameter. The state is signed to prevent tampering. This avoids adding a new cookie just for the popup flag.

**Why use the existing state cookie for Slack?**
Slack already uses a `slack_oauth_state` cookie for CSRF protection. Adding the popup flag to the same JSON structure avoids creating an additional cookie and keeps the state management centralized.

**Why does Telegram keep its login state server-side instead of in a cookie?**
The pre-plugin flow carried the phone, code hash, and the pre-auth Telethon session between form pages in a signed (but client-readable) cookie. Signing prevents tampering, not reading, and a session string is a credential; parking the in-flight login in the encrypted `oauth_blob.pending` field keeps it off the client, makes it restart-safe, and lets the server enforce an expiry and an attempt cap. The single-page `fetch` design also removes the need to thread a popup flag through the steps at all.

**Why poll for popup closure as a fallback?**
The `postMessage` protocol is reliable when the popup loads successfully, but there are edge cases where it might not fire: the user closes the popup manually during the OAuth provider's consent screen, the popup navigates to a page that does not call `postMessage`, or an unhandled error occurs. Polling `popup.closed` every 500ms catches these cases and triggers a connector status refresh.

**Why show a popup-blocked fallback instead of silently failing?**
Some browsers block popup windows by default. Rather than failing silently (user clicks Connect and nothing visible happens), the settings panel detects the blocked popup and shows a direct link the user can click to open the OAuth URL in a new tab. The `?popup=1` parameter is preserved so the completion page still attempts `postMessage`.

**Why not close the popup when the settings modal closes?**
If the user opens a popup and then closes the settings modal, the popup is left open so the OAuth flow can complete. Forcibly closing the popup would abort the flow and confuse the user who is mid-consent on the external provider's page. The popup reference is cleared from state, and polling stops, but the popup itself continues.

## Constraints

- Popup windows may be blocked by browser settings; the fallback direct link mitigates this
- The `postMessage` origin check (`event.origin !== window.location.origin`) ensures messages are only accepted from the same origin
- The popup flag does not affect session invalidation or connector behavior -- it only controls whether the callback returns HTML (popup) or a redirect (non-popup)
- Airtable and api_key-kind plugin connections do not use the popup flow because they use an inline `ApiKeyForm` component instead of an external OAuth redirect

