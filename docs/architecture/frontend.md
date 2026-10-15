# Frontend Architecture

The React single-page app in `frontend/`: build and serving, the state layer that keeps per-conversation state alive across navigation, URL routing, and the main surfaces (sidebar, composer and home screen, chat panel, message and tool-call rendering, the right-hand workspace panel). The REST client is in [Frontend API Client](../api/api-client-usage.md), the realtime protocol in [Realtime](realtime.md), and the Quest Docs and Quest Tables views in [Quest Docs -- Frontend](quest-docs.md#frontend) and [Quest Tables Frontend](quest-tables-frontend.md).

## Overview

React 19 and TypeScript (versions in `frontend/package.json`), built by Vite into static files that FastAPI serves from the same origin, so REST calls and the WebSocket need no proxy. There is no Vite dev server: `run.py` installs the packages and runs `npm run build` (`tsc -b` over both tsconfig projects, then `vite build` into `frontend/dist/`) before starting the server, in every run mode. The CI checks are in [Development Workflows -- Frontend Checks](../setup/development-workflows.md#frontend-checks).

## Key Files

| File | Role |
|------|------|
| `frontend/src/App.tsx` | Routes; `AppContent` syncs URL params into context and picks the layout |
| `frontend/src/contexts/` | One context per app-wide responsibility, composed by `AppProviders.tsx` |
| `frontend/src/store/conversationStore.ts` | Per-conversation store |
| `frontend/src/services/PersistentWebSocket.ts`, `frontend/src/services/WebSocketManager.ts` | The one socket; the shim feeding its events into the store |
| `frontend/src/hooks/useConversation.ts` | One conversation's state, loading, tail fetches, send |
| `frontend/src/components/` | Surfaces, each in its section below; `MobileShell.tsx` is the phone layout |
| `frontend/src/components/LazyBoundary.tsx` | Code splitting: `lazyNamed()` and the Suspense plus chunk-error boundary |

## Build and Serving

`quest.py` mounts `frontend/dist/assets` at `/assets` and answers client-side URLs with `index.html` (see [Backend SPA Support](#backend-spa-support)). Outside that mount the server serves only single-segment root files (the `/{filename}` fallback), so any runtime asset in a subdirectory must land under `dist/assets/`.

The deployment may have no network egress, so nothing the app needs at runtime comes from a CDN: `vite-plugin-static-copy` (`frontend/vite.config.ts`) copies the pdf.js cmaps, fonts, wasm and ICC profiles into `dist/assets/pdfjs/`, `PdfViewer.tsx` imports the pdf.js worker with a Vite `?url` import, and the KaTeX stylesheet imported in `codeAndMath.ts` makes Vite bundle its fonts. The one third-party load is the JetBrains Mono stylesheet in `frontend/index.html` (the first family of `--font-mono`); offline, the stack's later families apply.

**Production build.** `vite build` minifies JavaScript and CSS and writes a source map beside every chunk (`build` in `frontend/vite.config.ts`), so browser devtools and stack traces map back to the sources. CSS minification runs after the `themeOverridePlugin.ts` rewrite and keeps its `:where()` scoping.

**Code splitting.** The main chunk holds the app shell. Views most page loads never open are lazy chunks, fetched on first render through `lazyNamed()` inside a `<LazyBoundary>` (`frontend/src/components/LazyBoundary.tsx`; grep `lazyNamed(` for the set): the file viewer, which every host renders as `LazyFileViewerModal.tsx`, and pdf.js behind a second split inside it; the Quest Docs editor and History in `DocViewer.tsx`; the System Reports page; and the admin-only Settings sections, which `components/settings/index.ts` therefore does not re-export. highlight.js and KaTeX form a chunk requested by content ([Lazy code and math](#message-component-with-markdown-rendering)). Rollup places a module in one chunk only, so a static import of a split module from the main graph silently undoes the split.

Chunk names are content-hashed, so after a redeploy a long-open tab can request a chunk that no longer exists; `/assets` answers 404 and the boundary shows a reload notice. **Why a notice instead of an automatic reload?** Composer text is not persisted, so a reload the user did not ask for could drop an unsent message.

## API Client

REST calls go through `frontend/src/api/` (cookie-authenticated, `ApiClientError` on failure, types in `types.ts`); see [Frontend API Client](../api/api-client-usage.md). Everything realtime -- subscribe and catchup, `send_message` / `stop`, streamed deltas, per-user globals -- rides the one socket in [Realtime](realtime.md).

## State Management Architecture

Per-conversation state lives outside React so it survives navigation: a run keeps streaming into its own conversation's store entry while the user looks at another one, and events always land in the conversation they belong to.

### Message Send Flow

1. The composer calls `sendMessage` from `useConversation`, which adds an optimistic user bubble to `conversationStore` and calls `webSocketManager.sendMessage()`.
2. `WebSocketManager` sends `{op: "send_message"}` on `persistentWebSocket` and tracks it until the server's receipt (see [Realtime -- Delivery Receipt and Failed Sends](realtime.md#delivery-receipt-and-failed-sends)).
3. The server runs the turn (`_run_send_message` in `chat/realtime/socket.py`); each durable flush publishes `message_appended`, with transient `text_delta` and sub-agent events in between.
4. `WebSocketManager` accumulates `text_delta` into the store's `partialResponse` and sub-agent events into its sub-agent maps; on `message_appended` it swaps the streaming text for a synthetic persisted entry at the event's seq.
5. `useConversation` answers each `message_appended` with `fetchConversationTail()` and merges the rows by seq, reconciling the optimistic bubble and replacing the synthetic entry in place.
6. `send_message_finished` ends the streaming state and fires `onStreamComplete`; the sidebar re-sorts on `conversation_list_changed`.

If the user switches away mid-run, the server-side subscription stays live and `WebSocketManager` keeps writing deltas into the original conversation's entry. On return the remounted hook subscribes again, and the server's catchup (or resync) supplies whatever was appended meanwhile; see [Realtime -- Subscription Protocol](realtime.md#subscription-protocol).

### Conversation Store (`src/store/conversationStore.ts`)

A Map of `ConversationState` by conversation id, created on demand so readers never null-check. `getConversationSnapshot(id)` returns that conversation's own state object, which changes only when that conversation does; `useConversation` hands it to `useSyncExternalStore`, so a component watching one conversation skips the re-renders other conversations' deltas would cause. The merge rules (`insertMessagesBySeq`, `finalizeStreamingTextInPlace`) are in [Realtime -- Optimistic Reconciliation](realtime.md#optimistic-reconciliation) and [Realtime -- Streaming-to-Finalized Swap](realtime.md#streaming-to-finalized-swap).

Live run state is `partialResponse` plus the sub-agent maps (`subAgentToolCalls`, and `subAgentReturned` for `sub_agent_finished`). Durable rows -- tool calls, action requests, stats, finalized text -- enter `messages` only through the tail fetch or a catchup; `streamingMessages` is only ever cleared, never filled.

### WebSocket Manager (`src/services/WebSocketManager.ts`)

A shim over `persistentWebSocket` that owns no socket (see its header comment). It forwards `sendMessage` (adding the browser's timezone) and `stopStreaming`; tracks each send as a `PendingSend` keyed by the bubble's `client_send_id`, failing the bubble into Retry / Discard when the socket is closed or `SEND_ACK_TIMEOUT_MS` passes without a receipt; turns `text_delta`, sub-agent events and `message_appended` into store updates; clears the streaming buffer on `subscription_expired` so the next boundary rebuilds from disk; and calls `onStreamComplete` listeners on `send_message_finished` and `onConversationRenamed` listeners on `conversation_updated`.

Closing or reloading a tab never cancels a run; only an explicit stop does (server side: [Realtime -- Send Lock and Stop Semantics](realtime.md#send-lock-and-stop-semantics)).

`sendMessage()` calls `requestNotificationPermission()` (`frontend/src/services/desktopNotifications.ts`) so the browser's prompt appears on the first send, not at page load. The only `sendDesktopNotification()` call, the "Response complete" alert in `finishStream()`, shows only with permission granted and the tab hidden.

### Application Contexts (`src/contexts/`)

App-wide state is split into one context per responsibility, composed by `AppProviders.tsx`, whose header comment lists each context's job and the provider order. Each provider memoizes its `value`, so a consumer re-renders only when the slice it reads changes; there is no umbrella hook.

**Boot order.** `AppConfigProvider` fetches `GET /app/api/config` and the version hash on mount and exposes `whenConfigLoaded`. `AuthProvider` runs `checkSession()` concurrently but commits nothing until the config has loaded, so `isCheckingAuth` turns false only once `loginMethod` is known (the sign-in screen never flashes the wrong form) and everything hydrated from the session sees the credentialed-model list. `ConversationModelsProvider` hydrates its defaults from `sessionSnapshot` (the raw `GET /me` payload) during render, not in an effect, so the first authenticated render already shows the stored default.

**AppConfigContext** sets the model catalog with `setModelCatalog()` before publishing `availableModelIds`, because consumers read the module-level catalog during that re-render. It polls `GET /app/api/version` on `VERSION_POLL_INTERVAL_MS` (not in hidden tabs) and sets `updateAvailable` when the hash changes; `AboutSection.tsx` reads the release fields of the same response (see [Development Workflows -- Releases](../setup/development-workflows.md#releases)).

**NavigationContext** carries two one-shot hand-offs to ChatPanel's auto-send effects: `pendingRoutineMessage` (a Sidebar routine run) and `pendingFirstMessage` (the home composer's first send, see [Home Screen](#home-screen-homecomposer)).

**ConversationModelsContext** keeps the user's default pick for new conversations per `ModelVisibility` (private, or public project, whose admin allow-list differs -- see [Model Selection](model-selection.md)) in `users.settings`, with no localStorage copy. A pick is a `ModelPick` (`frontend/src/constants/models.ts`): the model plus its thinking level (null = default), tracked beside the model everywhere -- per-visibility default, per conversation, `pendingFirstMessage`, every send:

- It is read only from a fresh `GET /app/api/me`: `refreshDefaultModel(visibility)` runs on every fresh new-chat composer and whenever a mounted composer's visibility flips (drilling into or out of a public project on the home screen, or an empty conversation's project fetch revealing a public project), so another tab's pick is never stale. An unusable stored pick falls through `resolveDefaultPick()`, whose header comment gives the chain; a stored level the model no longer lists reverts to the default (`resolveThinkingEffortFor()`).
- It is written only by `persistDefaultModel()` from ChatPanel's first-send paths, never on a picker change; `pendingFirstMessage` carries the public flag so the right key is written before ChatPanel's project fetch resolves.
- The per-conversation pick is separate: `setModelForConversation` PATCHes `/conversations/{id}/model` (model + level), `hydrateModelForConversation` sets only the in-memory value, and `setDraftModelForConversation` (home screen) touches neither the server nor the default. Deprecated ids are remapped through `DEPRECATED_MODEL_MAP`.

Provider locks also live here; see [Provider Locking](#provider-locking).

### useConversation Hook (`src/hooks/useConversation.ts`)

The per-conversation entry point (`UseConversationResult`). It loads the conversation once, records `last_message_seq` with `persistentWebSocket.setLastSeq()` and subscribes, so the first subscribe can be answered with up-to-date or catchup; answers `message_appended` with a tail fetch, applies catchup rows directly and refetches on resync; and hydrates context usage from the newest stats row, loaded skills, `pending_wait_handles` and the header's title and archive state. On unmount it detaches its handler but sends no `unsubscribe`, so an away conversation keeps receiving events until the server-side TTL lapses. An entry seeded by `seedNewConversation()` (loaded, zero messages) skips the loading state, so a just-created conversation shows its empty composer at once.

### Design Decisions

**Why a store outside React instead of component state?** Component state dies on unmount, so switching conversations mid-run would drop that run's events. `useSyncExternalStore` is React's own API for subscribing to such a store, correct under concurrent rendering, without a context-plus-reducer that re-renders every consumer.

**Why one socket singleton plus a shim?** The socket carries every conversation's events and the per-user globals, and its reconnect, heartbeat and subscription bookkeeping cannot be redone per component mount. `WebSocketManager` gives consumers one callback API and the text bundling instead of each filtering raw socket events.

## URL-Based Routing

Every conversation has its own URL, so deep links, reloads and back / forward work. `frontend/src/main.tsx` wraps the app in `BrowserRouter`; the `<Routes>` in `App.tsx` send `/`, `/chats/<id>` (standalone), `/projects/<pid>/<id>` (project conversation), `/inbox`, `/inbox/memories` and the Quest Docs and Quest Tables routes to `AppContent`, and render `/set-password` and the admin reports page outside it.

The URL is the source of truth: a `useEffect` in `AppContent` copies the params into `activeConversationId` and `activeProjectId`, and selection goes through `navigate()` in `handleConversationSelect` / `handleNewConversation`, both `useCallback`-stable for the memoized Sidebar. `/inbox*` is mirrored into `showRequestsView`; docs and tables routes are parsed on every render by `parseDocsRoute()` and `parseTablesRoute()` (`frontend/src/utils/docsRoute.ts`, `frontend/src/utils/tablesRoute.ts`), with no context field. Keeping the selection only in React state was rejected: it would be lost on reload and conversations could not be linked.

### Project Redirect

A `/chats/<id>` URL for a project conversation is rewritten to `/projects/<pid>/<id>`: the detail response (`chat/routes/conversations.py`) carries `project_id`, `useConversation` fires `onProjectIdLoaded`, and `handleProjectIdLoaded` in `App.tsx` navigates with `replace: true` -- a push would leave the pre-redirect URL in history, so Back would redirect again.

### Backend SPA Support

`quest.py` registers one `serve_spa_*` catch-all per client-side URL family, before the `/{filename}` static fallback, returning `frontend/dist/index.html` so a direct load or reload of a client-side URL reaches React Router instead of a 404. The `/admin` catch-all cannot shadow the admin API, which lives under `/app/api/admin/...`. FastAPI registers its interactive docs ahead of every app route, so they live off `/docs`, at the `docs_url` / `redoc_url` arguments of `FastAPI(...)` in `quest.py` (pinned by `tests/test_spa_docs_routes.py`).

## App Layout

`AppContent` (`frontend/src/App.tsx`) renders, in order of precedence: a loading screen during the session check, `SignInScreen` when unauthenticated, `MobileShell` below `MOBILE_BREAKPOINT_PX`, else the desktop row of `Sidebar`, a main pane and `SettingsModal`. The main pane is `RequestsView` while `showRequestsView` is set, else a Quest Docs or Quest Tables view on a `/docs` or `/tables` route (no right panel), else `ChatPanel` (or `HomeComposer` with no conversation) beside `RightPanel`. `MobileShell` (see its header comment) arranges the same components in a top bar and drawers. `document.title` is `appName`, which differs in local mode.

**Why components subscribe to their own realtime events.** The sidebar list hooks follow `onStreamComplete` and `conversation_list_changed`, `FileBrowser` follows `file_list_changed`, `ProjectTables` follows `onStreamComplete`. A refresh counter in a shared context would re-render every consumer whenever any run finished and flicker the UI; direct subscriptions re-render only the subscriber, which refreshes stale-while-revalidate.

**Admin menu.** `AdminOpsMenu.tsx` shows for admins and during impersonation, inline in the sidebar's `UserInfoBar.tsx` or floating on the System Reports page (no sidebar there). It offers a two-step server shutdown (`POST /app/api/admin/shutdown`) and the impersonation picker; see [Admin Impersonation](admin-impersonation.md).

## Authentication

The browser authenticates only with the session cookie (`COOKIE_NAME` in `auth/config.py`): REST calls send it with `credentials: 'include'` and the WebSocket upgrade carries it, so no API key is held in browser storage or URLs. `AuthContext` runs `checkSession()` (`frontend/src/utils/auth.ts`, `GET /app/api/me`) once on mount. `SignInScreen.tsx` renders the form for the configured sign-in method and, in local mode, the canned-account picker; `SetPasswordScreen.tsx` serves `/set-password` outside the auth gate. Methods and routes: [Auth -- Sign-in Methods](auth.md#sign-in-methods). A user with no connected service gets Settings opened on Data Connections, on desktop only, since the full-screen phone settings would take over the first view.

## Sidebar Component (`src/components/Sidebar.tsx`)

`Sidebar.tsx` is a shell; its header comment lists what it owns and delegates. The data hooks each own their realtime subscriptions, so the Sidebar has none: `hooks/useTopLevelConversations.ts` (paged standalone list, filters, background refresh), `hooks/useProjectConversations.ts` (per-project lists, the drilled list's poll), `hooks/useProjectRoutines.ts` and two instances each of `hooks/useDocs.ts` and `hooks/useTables.ts`. `hooks/useConversationListActions.ts` (one per list) owns menus, inline rename and optimistic archive; `utils/sidebarItems.ts` and `utils/sidebarDocs.ts` hold the pure list derivation, unit-tested without React; `components/sidebar/` renders, with one `ConversationRow` for every kind of row. Section components return fragments where `Sidebar.css` uses child selectors, so the DOM shape matches the stylesheet.

- **New Chat never creates a conversation.** It navigates to `/` (see [Home Screen](#home-screen-homecomposer)), keeping a project drill so the first send lands in that project. Only Duplicate Workspace and routine runs create a conversation up front; they seed the store with `seedNewConversation()` (`utils/newConversation.ts`) so `ChatPanel` paints at once, and leave the list refresh to the server's `conversation_list_changed`.
- **Drilling into a project** remembers the selected top-level conversation, then selects the URL's conversation if it belongs to the project, else the latest, else none; drilling back restores it. A URL naming a project auto-drills. `drilledProjectId` lives in `ProjectsContext` because `HomeComposer` and `RightPanel` read it too.
- **Filters** reset on every page load. For the top-level list they are query params of `GET /conversations` (see [Chat API](../api/chat-api.md)), also applied client-side by `applyConversationFilters()` so unchecking hides rows before the refetch returns. The project list filters archived rows server-side and Slack rows client-side.
- **Paging.** The top-level list fetches `CONVERSATIONS_PAGE_SIZE` rows at a time with `exclude_projects=true` and the keyset `next_cursor`; an `IntersectionObserver` on the Load more row pages on scroll, and background refreshes re-fetch the whole loaded window.
- **Drilled list.** `useProjectConversations` polls on `PROJECT_CONVERSATIONS_POLL_INTERVAL_MS` for scheduled routine runs (paused in hidden tabs) and refreshes silently on realtime events; the non-silent loader, which swaps in a loading placeholder, runs only on the first drill-in and the archive toggle.
- **Re-sort animation.** `hooks/useFlipListAnimation.ts` glides re-ordered rows to their new slot. It measures on every commit, deliberately without a dependency array, because the rendered order can change without any prop identity changing.
- **Inbox badge.** `RequestsBadge` is its own component so a count change re-renders only the badge. It sums open requests and pending memory suggestions from the `request_count_changed` and `*memory_suggestions_changed` globals, plus the same-tab bus in `services/requestEvents.ts`.
- **Elsewhere:** `updateAvailable` shows a reload prompt; custom names (`custom_name` on `Conversation`, `db/models.py`) override derived titles; Create Project from Chat opens `ConvertToProjectModal.tsx` ([Projects -- Converting a Conversation into a Project](projects.md#converting-a-conversation-into-a-project)); both routine modals show `ROUTINE_PROMPT_FILES_HINT` ([Routines -- File Spaces in Routine Prompts](routines.md#file-spaces-in-routine-prompts)).

## Composer Component

`frontend/src/components/Composer.tsx` is the one message composer, rendered by `ChatPanel` for a live conversation and by `HomeComposer` before one exists. Its header comment covers what it owns and the desktop / phone split; `ComposerProps` is how the host controls sending. Two props are cross-file contracts: `HomeComposer` passes `setDraftModelForConversation` as `onModelChange`, so a home-screen pick never PATCHes a conversation that does not exist, and sets `skipSendLocks` (see [Provider Locking](#provider-locking)).

The composer is styled by `ChatPanel.css`, its phone rules in the one `@media (max-width: ...)` block at the end of that file. The model trigger is the only shrinkable item in the controls row, so a long model name ellipsizes and Send never leaves the bubble; a new control must stay non-shrinking.

**Two attachment paths.** Clipboard images (`ALLOWED_PASTE_MIME_TYPES`, capped at `MAX_COMPOSER_ATTACHMENTS`) upload in two phases: `uploadComposerAttachments()` posts them to `POST /conversations/{id}/composer-attachments` and the returned refs ride `WebSocketSendMessage.attachments`; a failed upload keeps thumbnails and text for a retry (see [Realtime -- Message Attachments](realtime.md#message-attachments)). Generic files queue client-side under `maxUploadBytes` (`AppConfigContext.tsx`, the admin upload cap; see [File Browser API](../api/file-browser-api.md)) and `MAX_PENDING_FILES`, and go to the host as raw `File[]` through `onSend`, because the home screen has no workspace until it creates the conversation. The host uploads them through `POST /files/upload` and sends the names as `attached_filenames` ([Gemini API -- Message Metadata Wrapping](gemini-api.md#message-metadata-wrapping)).

The Flags popover is described in [Conversation Flags -- Composer UI](conversation-flags.md#composer-ui-out-of-band-selection), the skill picker in [Skill Library](skill-library.md).

**Context indicator.** `ContextIndicator.tsx` (header comment: the fixed-footprint ring) shows how much of the context window the conversation uses, from the `context_tokens` / `max_context_tokens` of the newest `stats` row: `compute_total_context_tokens()` in `chat/llm/base.py` counts occupancy per provider, and the maximum is the model's registry `max_input_tokens`. `useConversation` feeds them to `conversationStore.setContextUsage()` whenever messages load or a tail fetch lands; without a maximum the ring falls back to `maxInputTokens` in `frontend/src/constants/models.ts`. Clicking it opens `SystemPromptModal.tsx`, which fetches the conversation's saved system prompt (`GET /app/api/conversations/{id}/system-prompt`).

### Model Selector

`ModelSelector.tsx` (its header comment covers the two-level menu, unoffered selections and the phone sheet) renders a list `Composer.tsx` has already narrowed: `getSelectableModels(visibility)` minus uncredentialed models and anything outside the conversation's provider lock. The top level is `getTopLevelModels(visibility)`, the admin's slotted picks ([Model Selection](model-selection.md); `DEFAULT_MODEL_SELECTION` in `config/model_selection.py` when nothing is saved).

The catalog is runtime data: `GET /app/api/config` returns `models` (the Vertex registry plus every provider instance's models under `<instance>:<wire_id>` ids, deprecated and disabled ones included so old conversations still label correctly), and `AppConfigContext` passes it to `setModelCatalog()` in `frontend/src/constants/models.ts`, whose helpers do every lookup; a hand-mirrored Vertex-only list is the pre-fetch fallback. A conversation's model is stored on its row (`setModelForConversation`) and sent with every `send_message`.

### Provider Locking

After its first message a conversation's provider family is locked and the picker offers only same-family models, so a conversation never switches history formats midway (provider instances share the `openrouter` family, so switching instances stays allowed). Locks live in `ConversationModelsContext.tsx`, persisted in localStorage, written by the composer's first send (unless `skipSendLocks`) and by `ChatPanel.tsx` when a conversation with messages loads, on the routine auto-send and in the `pendingFirstMessage` auto-send.

The home composer must never write a lock: it is keyed by `HOME_DRAFT_KEY` (`frontend/src/constants/drafts.ts`), lock entries are never removed and the map is persisted, so a draft-key lock would restrict the home model menu to one provider for good. `HomeComposer` therefore passes `skipSendLocks`, and the lock lands on the real conversation id in ChatPanel's `pendingFirstMessage` effect. The context's lazy initializer strips any stored `HOME_DRAFT_KEY` entry, so the key's value must never change.

## Home Screen (HomeComposer)

`frontend/src/components/HomeComposer.tsx` renders at `/` when no conversation is selected. Its header comment covers the deferred-create flow, project awareness, the private / public default-model switch and the first-send file upload. Across files:

- Every New Chat control lands here, so a conversation exists only once its first message is sent. While the Sidebar is drilled into a project, the first send creates the chat in that project (`createProjectConversation(drilledProjectId)`), and the right panel meanwhile shows that project's cards. `drilledProjectId` is distinct from the URL-mirrored `activeProjectId`, which is null at `/`.
- The first send creates the conversation, seeds the store with `seedNewConversation()`, re-keys the queued skills, stashes the send in `pendingFirstMessage` (`PendingFirstMessage` in `NavigationContext.tsx`) and navigates. ChatPanel's auto-send effect performs the real send, persists the default model and locks the provider -- the only lock point for conversations started here.
- Queued files upload after the conversation exists and before navigation; a hard failure aborts the send, a partial one proceeds with a notice.

The footer glyph is `HeartBoltMorph.tsx` (shared with Settings > About); its header comment explains why the morph uses SMIL rather than the CSS `d` property, which Safari lacks.

## Quest Docs and Quest Tables UI

The `/docs` views are documented in [Quest Docs -- Frontend](quest-docs.md#frontend), the `/tables` views in [Quest Tables Frontend](quest-tables-frontend.md). In `AppContent` either route takes over the main pane with no right panel, and `showRequestsView` still wins over it, so the Sidebar clears that flag before navigating to a doc or table.

## ChatPanel Component

`frontend/src/components/ChatPanel.tsx` shows one conversation and hosts its `<Composer>`. Its data comes from `useConversation`; only scroll tracking and viewer flags are local, so remounting on a conversation switch loses nothing.

- **Title.** `ConversationHeader.tsx` offers Rename and Archive / Unarchive, deliberately no Delete: conversations are only archived. State is hydrated from `GET /conversations/{id}` and updated optimistically; the sidebar follows via `conversation_list_changed`, and model-chosen names arrive live via `webSocketManager.onConversationRenamed`.
- **Rendering.** Persisted rows are grouped by `groupConsecutiveToolCalls()` and rendered by `Message` and `ToolCallGroup`. During a run `partialResponse` renders as a live markdown bubble with the same plugin stack, and a typing placeholder shows until the first delta or row arrives.
- **Auto-scroll.** To the bottom on load and on send; during a run only while the user was near the bottom (`isNearBottom()`), so reading history is never interrupted. It re-anchors when tail fetches grow the list or the code-and-math chunk re-renders messages, and force-scrolls on the streaming-to-finalized swap, because the near-bottom flag can go stale during a smooth scroll.
- **Search hits.** When the search modal sets `scrollToMessageIndex` (`NavigationContext`), the panel scrolls to and highlights that `data-message-index`, then clears it.
- **Pending wait handles** lock the composer (`hasPendingWait` / `pendingWaitKind` from `useConversation`); see [Wait Handles -- Composer Sync](wait-handles.md#composer-sync).
- **Read-only conversations** (`isReadOnly`: Slack, cross-user subagent and inference API origins) disable input and hide send and attach, keeping the disabled model trigger and the context indicator; see [Slack Socket Mode](slack-socket-mode.md).

## Message Component with Markdown Rendering

`frontend/src/components/Message.tsx` renders persisted rows. `MessageContentRenderer`, memoized so already-rendered rows skip list re-renders, dispatches on `message.type` to per-type renderers in the same file (`tool_use` to `ToolUseMessage`, `action_request` to `ActionRequestMessage`, see [Action Requests](action-requests.md)). User text renders as plain pre-wrapped text; only assistant text is markdown. A failed send renders dimmed with Retry / Discard ([Realtime -- Delivery Receipt and Failed Sends](realtime.md#delivery-receipt-and-failed-sends)).

**Markdown stack.** Assistant messages, the streaming bubble, the compaction summary, the `.md` preview in `FileViewerModal.tsx` and the Quest Docs viewer, editor preview and History render through `RichMarkdown.tsx` (the one plugin stack) with the `markdownComponents` object exported from `Message.tsx` (new-tab links, `CopyableTable` copying as TSV plus HTML, `MarkdownImage`). `remark-breaks` is there because CommonMark's soft breaks depend on CSS `white-space`, so newlines visible in the pre-wrap streaming bubble collapsed once the message was persisted. `remarkMathCurrencyGuard` (`frontend/src/utils/remarkMathCurrencyGuard.ts`) applies Pandoc's rule so "$5 and $10" stays text. There is no `rehype-raw`: raw HTML in model output, `<img>` included, is dropped.

**Lazy code and math.** rehype-highlight and rehype-katex, with highlight.js, KaTeX and its stylesheet, are the code-and-math chunk (`components/codeAndMath.ts`, loaded by `hooks/useCodeAndMath.ts`). Only markdown with a code fence or a `$` (`needsCodeAndMath()`) and `ToolUseMessage`'s run_python script highlighting request it; such content renders plain until it lands, then re-renders. **Why load by content instead of with the app?** They are most of the markdown weight, and markdown without fences or `$` renders identically without them, so sign-in, the home screen and plain replies never wait for them.

**Inline workspace images.** The model can embed a workspace image with plain markdown and the chat renders it inline. `MarkdownImage` never auto-fetches an external image: an `<img>` fires its request on render, so a prompt-injected reply embedding an attacker URL with a secret in its query would leak data with no user interaction. Absolute srcs (any scheme, or protocol-relative) degrade to a click-through link instead. Only workspace-relative srcs render, from `GET /app/api/conversations/{id}/files/download` -- the conversation's own Chat Files, never the project workspace (the model is told to `copy_file` a `proj://` image first; old inline images in legacy project chats break, see [Projects -- Migration Notes](projects.md#migration-notes)).

`MarkdownWorkspaceContext` supplies the base: a context rather than a components factory, so `markdownComponents` stays a stable module constant and the memoization holds. `ChatPanel` provides the conversation id and a click-to-enlarge handler, `FileViewerModal` the file's own space, and the Quest Docs viewer an `assetBase` that resolves only `assets/<name>` ([Quest Docs -- Viewer](quest-docs.md#viewer)). A src that fails to load becomes a missing-image chip, tracked per URL so a src truncated mid-stream recovers.

**Usage stats.** Each turn ends with one `stats` row, emitted by `_emit_durable(ctx, {"type": "stats", ...})` at the end of `run_conversation_turn()` in `chat/gemini_api/conversation.py` from `UsageAccumulator.build_stats()` (`chat/gemini_api/usage.py`; fields in [Gemini API -- Token Usage Tracking](gemini-api.md#token-usage-tracking)). The stats branch of `MessageContentRenderer` shows new input, output, time and tool calls, with a provider-aware breakdown on hover (Gemini and OpenRouter count input inclusive of the cached subset; Anthropic splits cache creation from cache read), plus a sub-agent row when `sub_agent_call_count` is non-zero. Stats rows without `new_input_tokens` render input plus cached.

**Code colours.** `Message.css` overrides highlight.js's `atom-one-dark` theme per token class, dark on `:root` and light in the light-scheme block. Every colour passes WCAG AA against its code-block background and carries its measured ratio in a comment on the rule; a palette change must keep those annotations true.

## Tool Use Message Display

`ToolUseMessage.tsx` renders one tool call, collapsed until clicked. Labels are description-first: the model's `intent_message` is primary and the tool name secondary, because names like `curl_proxy_get` tell the user nothing. The sub-agent spawners (`agent_task`, `agent_task_parallel`, `agent_task_parallel_template`) derive their header from `tool_input` instead: the sub-agent's name and task, or the count and names of parallel agents.

**Sub-agent tree.** Sub-agent tool calls (`subAgentToolCalls`, keyed by parent tool id) render as a tree under the spawner: flat for `agent_task`, grouped by agent into `SubAgentSection`s for the parallel spawners. In a `nested_subagents` conversation a 1st-level agent's calls split into leaf calls, `agent_task_nested` node calls (`nested_agent_id`) and grandchild calls (`nested_parent_id`), each node rendering as a `NestedAgentNode` ([Realtime -- Nested Sub-Agent Events](realtime.md#nested-2nd-level-sub-agent-events)).

**Sub-agent status.** A sub-agent stays `RUNNING` until its `sub_agent_finished` arrives (`subAgentReturned`), not when its last inner `tool_result` lands: an agent that has not called `agent_task_response` is still running. A parallel group waits for every expected agent and shows `ERRORED` if any failed. A nested node's status comes from its node result's `nested_agent_status`, with the grandchild's `sub_agent_finished` as the reload fallback. Rows persisted without any `sub_agent_finished` count as completed once the parent `tool_result` exists.

**Grouping.** `groupConsecutiveToolCalls()` in `ChatPanel.tsx` folds adjacent tool calls into one `ToolCallGroup` showing the latest call and a count of the rest, instead of one row per call pushing the reply down the page. Results are matched by id (`findToolResultById()`) because in a group a result is not necessarily next to its call; `tool_result` rows never render at top level.

## Right Panel (File Browser and Project Tables)

The right panel shows the file spaces of what is on screen as floating cards, plus the project's database tables. `RightPanel.tsx` picks the card set:

| On screen | Cards |
|-----------|-------|
| Project conversation | **Chat Files** (`FileBrowser` over the conversation's own workspace, `/conversations/{cid}/files...`), **Project Files** (`FileBrowser` over the project's shared workspace, `/projects/{pid}/files...`), **Tables** (`ProjectTables`) |
| Standalone conversation | **Chat Files** alone |
| Home composer while the Sidebar is drilled into a project (URL `/`) | **Project Files** and **Tables** |
| Nothing | The `FileBrowser` empty state |

Each `FileBrowser` takes a `source` (`conversationSource(cid)` / `projectSource(pid)` from `api/fileApi.ts`) that selects its routes (`chat/file_routes.py`, see [File Browser API](../api/file-browser-api.md)), its title and its event filter. `RightPanel` uses `drilledProjectId` only when there is no conversation, so a standalone chat viewed while drilled never grows project cards. `MobileShell` renders the same `RightPanel` in its workspace drawer.

**Copy / Move.** In a project conversation `useWorkspaceCopy` (`frontend/src/hooks/useWorkspaceCopy.ts`) adds copy and move row actions through the `FileBrowser` `rowActions` prop, calling `copyFileToProject` / `copyFileFromProject` with the same relative path on the other side. Rows in the conversation scratch roots get no to-project action (`SCRATCH_ROOTS` in the hook hand-mirrors `SCRATCH_SOURCE_ROOTS` in `chat/file_storage.py`, the route's `forbidden_source` rule). A `409 destination_exists` opens an overwrite confirm that retries with `overwrite: true`; other failures, a move whose source survived (`moved: false`) and copies with skipped entries become a notice on the originating card; results arriving after a conversation switch are dropped.

Each card keeps its own path, history and show-hidden toggle (`FileBrowserStateContext`, keyed by `fileSourceKey()`), and downloads from either card pass the [Workspace Download Warning](#workspace-download-warning), since a project file may have been written by any sibling conversation.

### FileBrowser and useFileBrowser

- **Hidden entries.** Dot-prefixed entries are filtered client-side unless the show-hidden toggle is on; the list route returns everything.
- **Viewable types.** `TEXT_EXTENSIONS` and `CSV_EXTENSIONS` in `FileBrowser.tsx` hand-mirror `VIEWABLE_EXTENSIONS` in `chat/file_storage.py`, because the text-content route refuses anything else; images and PDFs come through the download route.
- **Uploads.** Folder drops are walked by `extractFilesFromDataTransfer()` (`utils/directoryTraversal.ts`) and sent as parallel `files` / `paths` form arrays so the server recreates the tree; uploads use `xhrUpload()` because only XMLHttpRequest reports upload progress.
- **Refresh.** Each card follows `file_list_changed` through `persistentWebSocket.onGlobalEvent`, for its own space only (`fileListEventMatchesSource()`), debounced by `FILE_LIST_REFRESH_DEBOUNCE_MS`. Tools and REST routes publish as each write lands, so the list updates mid-turn (publish sites: [Realtime -- Backend publish sites](realtime.md#backend-publish-sites-per-user-globals)). These refreshes are `silentRefresh()` (stale-while-revalidate); buttons key their disabled state on `isInitialLoading` so they do not flicker.

`useFileBrowser` drives source switches and folder navigation from one effect keyed on the source key and path: as two effects they could fire in the same commit and the in-flight guard swallowed the second fetch, leaving the panel on the previous conversation's files. The guard (`inFlightRef`) is keyed by source and path, a switch clears the old listing first, and a response for a source or path the user has left is discarded, so rapid A-to-B-to-A switching never shows a stale list.

### Workspace Download Warning

A prompt-injected agent can write workspace files that carry data the user never sees -- a script in an HTML page, EXIF bytes in an image, a macro in an Office document -- and a download moves such a file out of the deployment. So every UI download first calls `confirmDownload({name, kind})` from `useDownloadWarning()` (`frontend/src/contexts/DownloadWarningContext.tsx`), which resolves a `DownloadDecision`: cancel, the original, or the server's sanitized copy. The caller then fetches with `fileApi.downloadFile(target, path, variant)`, which returns a blob URL, and hands it to the browser with `saveBlobToDisk()`, the only place a workspace download reaches the disk. `useFileBrowser` calls `confirmDownload` itself; other components use `useWorkspaceDownload()`.

Which names warn, and how severely, is `getDownloadWarning()` in `frontend/src/utils/downloadWarnings.ts`, whose header comment states the allow-list rule. For the formats in `SANITIZABLE_IMAGE_EXTENSIONS` (hand-mirroring `chat/image_sanitizer.py`) the dialog leads with a sanitized copy from `/files/download-sanitized` and offers the original only behind a second, checkbox-gated confirm. A failed sanitized fetch (400 `unsanitizable_image`, when the bytes are not what the name claims) surfaces as an error, never as a silent fallback to the original.

- No remember-me: a file can be planted at any point of a conversation, so the dialog shows on every download.
- The dialog usually opens over another `ModalShell`, and both close on a document-level Escape listener, so the provider claims Escape in the capture phase and cancels only the warning.
- It is a frontend gate only: `/files/download` is unchanged and a direct URL fetch is not warned.
- Quest Docs downloads skip it, because a doc's markdown and sniffed raster assets leave little room for hidden data.

### FileViewerModal and PdfViewer

`FileViewerModal.tsx` (a lazy chunk, see [Build and Serving](#build-and-serving)) previews a file of either space (its `source` prop selects the routes). Text, JSON, markdown and CSV come from the text-content route (`fetchFileContent()`, bounded by `MAX_VIEW_SIZE`): JSON renders in `JsonTreeViewer`, `.md` through the chat's markdown stack, CSV through `parseCsv()` (`utils/csv.ts`) as a client-sorted table capped by `CSV_MAX_RENDER_ROWS` and `CSV_MAX_CELL_LENGTH`; any parse failure falls back to raw text. Images and PDFs are fetched as bytes from the cookie-authenticated download route, and PDFs over `PDF_MAX_PREVIEW_BYTES` are refused. Markdown files can be saved to Google Drive ([Drive API -- Save to Drive](../api/drive-api.md#save-to-drive-via-file-browser)).

`PdfViewer.tsx` (header comment: lazy rendering, self-hosted assets) routes every `getDocument` option through `PDF_DOCUMENT_OPTIONS` so asset URLs cannot drift, caps canvas resolution with `getDpr()`, and copies the incoming buffer before pdf.js transfers it to its worker, because StrictMode mounts effects twice in dev and the second mount would get a detached buffer.

**Why pdfjs-dist directly instead of a wrapper such as react-pdf?** The viewer needs control over lazy rendering, canvas eviction and asset URLs, which a wrapper would not remove. The exact version pin keeps the bundled worker matched to the main-thread library. Rendering only near-viewport pages and evicting far ones, rather than rendering every page at open, keeps memory bounded for any document length.

**Why parse and sort CSV in the browser instead of a backend query layer?** Unlike project tables, which SQLite sorts server-side, a CSV is just bytes that already arrive whole through the text-content route; client-side parsing needs no new endpoint, and `CSV_MAX_RENDER_ROWS` bounds the DOM. The hand-written parser, rather than a library, is RFC-4180-aware, never throws (malformed input falls back to raw text), and keeps the bundle small.

### RightPanel and Project Tables

`RightPanel.tsx` (header comment: card sets, split shares and their per-project localStorage keys) separates cards with pointer-captured drag handles, so touch drags work in the phone drawer. Its width is resizable from the left edge, persisted browser-wide under `WIDTH_STORAGE_KEY`, read on mount only and re-clamped when the window shrinks; `MIN_PANEL_WIDTH` is both default and floor.

`ProjectTables.tsx` lists the project database's tables and `TableViewerModal.tsx` pages through one (`api/projectDbApi.ts`, see [Project DB](project-db.md)). The list refreshes at the end of each turn on `webSocketManager.onStreamComplete`, because there is no project-database mutation event to refresh on mid-turn.

## Styling and Theming

Each component imports its own stylesheet; class names are plain strings, not CSS modules. Colours, grounds and the monospace stack are design tokens on `:root` in `frontend/src/index.css` (dark on `:root`, light in its `prefers-color-scheme: light` block), and stylesheets use them rather than a literal accent. The remaining literal colours are the semantic status colours, the syntax palette in `Message.css` and the action-request card in `ActionRequestMessage.css`.

**Light / dark / auto.** A media query cannot be overridden from JavaScript, so rather than rewrite every stylesheet, the PostCSS plugin `frontend/themeOverridePlugin.ts` (header comment: the rewrite) turns each `prefers-color-scheme` block into `:where()`-scoped copies keyed on `<html data-theme>`, keeping specificity and source order. Stylesheets keep plain media queries and never reference `data-theme`.

**Colour themes** swap the accent family and the two grounds for both schemes (`frontend/src/utils/colorTheme.ts`). The `:root` defaults are `DEFAULT_COLOR_THEME`; other themes are blocks in `frontend/src/themes.css`. A new theme touches four hand-mirrored places: `themes.css`, `COLOR_THEMES`, `COLOR_THEME_CHOICES` in `chat/routes/user.py`, and the id list in the `frontend/index.html` boot script.

**Persistence.** `AppearanceContext.tsx` owns both settings. The server values (`users.settings` `theme` / `color_theme`, via `PUT /app/api/settings` and `GET /app/api/me`) are the source of truth across devices; a localStorage copy (`THEME_STORAGE_KEY`, `COLOR_THEME_STORAGE_KEY`) lets the `index.html` script set both attributes before first paint, so a forced scheme never flashes the OS one.

**Breakpoint.** `MOBILE_BREAKPOINT_PX` in `frontend/src/hooks/useIsMobile.ts` picks the desktop layout or `MobileShell`; the phone `@media (max-width: ...)` block at the end of `ChatPanel.css` hand-mirrors it.

## Render Optimization

Wasted renders are cut at three sources: cross-conversation store updates (per-conversation snapshots), context churn (one memoized provider per responsibility), and unstable props (`React.memo` on `Sidebar`, `ChatPanel`, `MessageContentRenderer` and `ActionRequestMessage`, with `useCallback`-stable handlers from `App.tsx`). `RequestsBadge`'s same-tab event bus lives outside React context for the same reason.
