# Frontend API Client

The frontend's REST client and the session check. The realtime socket is documented in [Realtime](../architecture/realtime.md); how the client fits the rest of the app is in [Frontend Architecture](../architecture/frontend.md).

## Overview

The browser authenticates only with the session cookie (`COOKIE_NAME` in `auth/config.py`, named per run mode): every REST call sends it with `credentials: 'include'` and the WebSocket upgrade carries it, so no API key is ever stored in or transmitted by the browser.

## Key Files

| File | Description |
|------|-------------|
| `frontend/src/api/client.ts` | One function per backend REST endpoint |
| `frontend/src/api/request.ts` | Shared JSON request wrapper (`apiGet` / `apiPost` / `apiPut` / `apiDelete`, `withQuery`, `handleErrorResponse`) and the `ApiClientError` class every `client.ts` function is built on |
| `frontend/src/api/types.ts` | Request and response types, including the socket envelopes (`StreamEvent`, `WebSocketSendMessage`) and message content (`MessageContent`) |
| `frontend/src/api/config.ts` | The `endpoints` URL builders, including the `ws://` / `wss://` socket URL |
| `frontend/src/api/fileApi.ts`, `frontend/src/api/docsApi.ts`, `frontend/src/api/projectDbApi.ts` | Clients for the workspace file routes, Quest Docs and project tables (see [Frontend Architecture -- Right Panel](../architecture/frontend.md#right-panel-file-browser-and-project-tables)) |
| `frontend/src/services/PersistentWebSocket.ts`, `frontend/src/services/WebSocketManager.ts` | The single multiplexed socket and the shim that routes its events into the conversation store |
| `frontend/src/services/desktopNotifications.ts` | Browser Notification API wrapper; `WebSocketManager` asks for permission on the first send and raises the response-complete alert |
| `frontend/src/utils/auth.ts` | `checkSession()` |
| `frontend/src/utils/oauthPopup.ts` | `openOAuthPopup()`, the centered popup window for OAuth connector flows |

## REST API Client

Every function in `client.ts` is one call to the shared wrapper in `request.ts`: `apiGet` / `apiPost` / `apiPut` / `apiDelete<T>(url, { query?, body?, nullOn? })` sends the session cookie, JSON-serializes `body` (setting `Content-Type` only when a body is present), appends `query` params while dropping `undefined` / `null` entries, and returns the parsed JSON body. `nullOn: [404]` turns a listed status into a `null` result for optional rows such as a routine's schedule.

Non-2xx responses throw `ApiClientError` (defined in `request.ts`, re-exported from `client.ts`) with `statusCode`, `errorCode`, `message` and an optional `details` holding the full parsed error body; the message and code are read from either the flat `{error, message}` body or FastAPI's `{detail: ...}` envelope. Callers that need a structured payload -- for example the `current` row of a `409 stale_update` from the routine and schedule update endpoints -- read it from `err.details`.

Consumers are mostly components, hooks and contexts under `frontend/src/`; function names match the actions (e.g. `hooks/useTopLevelConversations.ts` calls `fetchConversations`).

## Session Check

`checkSession()` in `frontend/src/utils/auth.ts` calls `GET /app/api/me` with the cookie and returns the payload (its return type in the same file), or `null` when there is no valid session. `AuthContext.tsx` calls it once on mount and keeps the raw payload as `sessionSnapshot` for the other providers; `refreshConnectionStatus()` re-reads the connection flags after an OAuth popup closes. The boot order and the sign-in screen are in [Frontend Architecture -- Authentication](../architecture/frontend.md#authentication).

## Design Decisions

**Why the session cookie instead of an API key in the browser?** The cookie already exists after sign-in. Holding the API key in the browser instead would mean storing it in localStorage and sending it on every request and in WebSocket URLs, exposing it to scripts, dev tools and logs.

**Why keep API-key auth alongside the cookie?** Scripts and agents authenticate without a browser session, so the server's request dependencies (e.g. `get_current_user_cookie_or_apikey` in `chat/auth.py`) accept either.

**Why REST plus one WebSocket rather than everything over the socket?** CRUD calls are plain request / response with JSON errors, which REST fits; the socket carries only what must be pushed (streamed deltas, durable-append signals, per-user globals).
