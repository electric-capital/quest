# Frontend API Client

This document describes the frontend API client for Quest.

## Overview

The API client provides a type-safe interface for interacting with the chat backend via REST and WebSocket APIs. The frontend authenticates via the session cookie (named `COOKIE_NAME` from `auth/config.py`, environment-dependent) -- no API key is stored or transmitted by the browser.

## Key Files

| File | Description |
|------|-------------|
| `frontend/src/api/client.ts` | REST API functions for all backend endpoints (conversations, settings, memories, guides, skills, projects, routines, schedules, auth) |
| `frontend/src/api/request.ts` | Shared JSON request wrapper (`apiGet`/`apiPost`/`apiPut`/`apiDelete`, `withQuery`, `handleErrorResponse`) and the `ApiClientError` class every `client.ts` function is built on |
| `frontend/src/api/types.ts` | TypeScript type definitions for all API request/response shapes |
| `frontend/src/api/config.ts` | API endpoint URL builders |
| `frontend/src/services/WebSocketManager.ts` | Singleton WebSocket client for streaming chat responses and routing structured events into the conversation store |
| `frontend/src/services/desktopNotifications.ts` | Browser Notification API wrapper used by `WebSocketManager` for background completion and action-request alerts |
| `frontend/src/utils/auth.ts` | Session check utility (`checkSession()`) |
| `frontend/src/utils/oauthPopup.ts` | OAuth popup window utility (`openOAuthPopup()`) for opening centered popup windows during OAuth connector flows |
| `frontend/src/hooks/useConversation.ts` | Conversation state hook with WebSocket integration |

## REST API Client

**Location:** `frontend/src/api/client.ts`

Every function is one call to the shared wrapper in `frontend/src/api/request.ts`: `apiGet`/`apiPost`/`apiPut`/`apiDelete<T>(url, { query?, body?, nullOn? })` sends the session cookie (`credentials: 'include'`), JSON-serializes `body` (setting `Content-Type` only when a body is present), appends `query` params while dropping `undefined`/`null` entries, and returns the parsed JSON body. `nullOn: [404]` turns a listed status into a `null` result for optional rows such as a routine's schedule.

Non-2xx responses throw `ApiClientError` (defined in `request.ts`, re-exported from `client.ts`) with `statusCode`, `errorCode`, `message`, and an optional `details` field populated with the full parsed error body; the message and code are read from either the flat `{error, message}` body or FastAPI's `{detail: {...}}` / `{detail: "text"}` envelope.

Callers reading structured error payloads (for example the `current` row inside a `409 stale_update` response from the routine and schedule update endpoints) read it from `err.details`.

## WebSocket Streaming

**Location:** `frontend/src/services/WebSocketManager.ts` (singleton: `webSocketManager`)

The WebSocket connection sends the session cookie automatically. WebSocket message types are documented in [Chat API - Server Stream Event Types](chat-api.md). `sendMessage()` also requests notification permission via `frontend/src/services/desktopNotifications.ts` on user-initiated sends.

## Session Authentication

**Location:** `frontend/src/utils/auth.ts`

**Function:** `checkSession()`

Checks if the user has a valid session by calling `GET /app/api/me` with `credentials: 'include'`. Returns `{ email, name, google_services_connected, has_any_service_connected }` if authenticated, `null` otherwise.

Used by `AuthContext` on mount to determine authentication state. The `has_any_service_connected` flag is used to auto-open the settings panel to the Data Connections section for users with zero connected services (truly new users).

## UI Components Using the Client

Components that consume the API client are primarily in `frontend/src/components/` and `frontend/src/components/settings/`. The function names in `frontend/src/api/client.ts` correspond directly to the component actions (e.g., `Sidebar.tsx` calls `fetchConversations`, `createConversation`, etc.).

## Authentication Flow

On mount, `AuthContext.tsx` calls `checkSession()` (in `frontend/src/utils/auth.ts`) which hits `GET /app/api/me` with the session cookie. If unauthenticated, `SignInScreen.tsx` is shown. If `has_any_service_connected` is false (new user), settings auto-opens to Data Connections. Post-OAuth-popup updates use `refreshConnectionStatus()` in `AuthContext.tsx`.

## Type Definitions

All TypeScript types are defined in `frontend/src/api/types.ts`. See that file for the full type definitions. Key types include `ConversationDetail` (with optional `project_id` for URL routing), `StreamEvent`, `MessageContent`, and `WebSocketSendMessage`.

## Design Decisions

**Why session cookie instead of API key in the browser?**
The session cookie is already present after OAuth login. Using it directly eliminates the need to extract the API key, store it in localStorage, and re-transmit it on every request. This reduces the attack surface (no API key in localStorage, dev tools, or WebSocket URLs).

**Why keep API key auth alongside cookie auth?**
Scripts and LLM agents need to authenticate without a browser session. The dual-auth approach (`get_current_user_cookie_or_apikey`) supports both consumers without breaking backward compatibility.

**Why separate REST and WebSocket APIs?**
REST is used for CRUD operations (list, create, get). WebSocket provides real-time streaming for message responses from Gemini.

**Why optimistic UI updates?**
User messages appear immediately in the UI before backend confirmation. Improves perceived responsiveness.