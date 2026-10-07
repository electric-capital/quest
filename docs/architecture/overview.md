# System Architecture Overview

This document provides a high-level overview of the Quest architecture, explaining how the different components work together to deliver a ChatGPT-style interface for multiple LLM providers (Google Gemini and Anthropic Claude on Vertex AI).

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         User's Browser                          │
│                     (http://localhost:8000)                     │
└────────────┬────────────────────────────────────────────────────┘
             │
             │ HTTP/WebSocket
             │
┌────────────▼────────────────────────────────────────────────────┐
│                FastAPI Backend (Port 8000)                      │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              React + TypeScript Frontend                 │   │
│  │  (served as static files from frontend/dist/)           │   │
│  │  - react-router-dom (URL-based conversation routing)    │   │
│  │  - Sidebar component (projects + conversations + search) │   │
│  │  - SearchModal component (conversation search)          │   │
│  │  - ChatPanel component (message display & input)        │   │
│  │  - FileBrowser component (workspace file management)    │   │
│  │  - UserInfoBar component (user profile in sidebar)      │   │
│  │  - SettingsModal + settings/ sub-components             │   │
│  │  - Message component (markdown rendering)               │   │
│  │  - conversationStore (per-conversation state)           │   │
│  │  - WebSocketManager (routes messages to store)          │   │
│  │  - useConversation hook (subscribe to conversation)     │   │
│  │  - API client (REST endpoints)                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Main App (quest.py)                                   │   │
│  │  - Instructions + Reset API Key endpoints               │   │
│  │  - Static file serving + SPA catch-all routes           │   │
│  │  - Lifespan: scheduler, shutdown event, cleanup         │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Auth Module (auth/)                                    │   │
│  │  ├── config.py - Constants, scopes, credential loaders  │   │
│  │  ├── session.py - Cookie serializers, auth dependencies │   │
│  │  ├── google_login.py - Google app login (5 endpoints)   │   │
│  │  ├── google_services.py - Google Services OAuth         │   │
│  │  ├── slack.py - Slack OAuth flow                        │   │
│  │  ├── airtable.py - Airtable token management           │   │
│  │  ├── dev_login.py - Dev-only email login (QUEST_ENV=dev) │  │
│  │  ├── google_credentials.py - Credential management     │   │
│  │  └── popup_helpers.py - OAuth popup HTML generators     │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Chat Module (chat/)                                    │   │
│  │  ├── __init__.py - Package init                        │   │
│  │  ├── logging_config.py - Unified log config + large_tool_results │   │
│  │  ├── routes.py - REST + WS + search + admin endpoints   │   │
│  │  ├── memory_routes.py - Memory CRUD + archive endpoints │   │
│  │  ├── guide_routes.py - Guide CRUD endpoints            │   │
│  │  ├── project_routes.py - Project CRUD + project conversations │   │
│  │  ├── file_routes.py - File browser endpoints (list, upload, download, download-folder, read, info, delete, create-folder, save to Drive) │   │
│  │  ├── file_storage.py - File ops, sanitization, dir conflict detection, deletion, folder zipping, folder creation │   │
│  │  ├── schedule_routes.py - Schedule CRUD endpoints      │   │
│  │  ├── action_request_routes.py - Action request endpoints │   │
│  │  ├── skill_routes.py - Skill Library CRUD + sharing + user search │   │
│  │  ├── project_skill_routes.py - Project skill CRUD + auto-load │   │
│  │  ├── action_request_types/ - Action request handler package  │   │
│  │  ├── scheduler.py - Background routine scheduler       │   │
│  │  ├── auth.py - Auth deps + is_admin() check             │   │
│  │  ├── storage.py - Chat persistence + guide snapshots + search + loaded skills │   │
│  │  ├── gemini_api/ - Conversation loop package              │   │
│  │  ├── llm/ - LLM provider abstraction layer               │   │
│  │  │   ├── base.py - LLMProvider interface                  │   │
│  │  │   ├── config.py - Model registry + provider singletons │   │
│  │  │   ├── gemini_provider.py - Gemini SDK provider         │   │
│  │  │   ├── anthropic_provider.py - Anthropic Vertex AI      │   │
│  │  │   └── tool_schemas.py - Provider-agnostic tool defs    │   │
│  │  └── route_dispatch.py - Internal route dispatch       │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  API Module (api/)                                      │   │
│  │  ├── instructions.py - Instruction aggregation layer    │   │
│  │  ├── gmail/, calendar.py, drive.py, docs.py, sheets.py │   │
│  │  ├── tasks.py, airtable.py                            │   │
│  │  └── Each has get_instructions() for system prompt docs │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────┬────────────────────────────────────────────────────┘
             │
             │ API calls (provider-specific)
             │
┌────────────▼────────────────────────────────────────────────────┐
│                         LLM APIs                                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Google Gemini API (google-genai SDK)                    │   │
│  │  - gemini-3.5-flash-lite (Vertex)                        │   │
│  │  - gemini-3.6-flash, 3.7-flash, 3.8-flash (Vertex)       │   │
│  │  - deprecated (still runnable): gemini-3.1-pro-preview,  │   │
│  │    gemini-3-flash-preview, 3.1-flash-lite, 3.5-flash     │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Anthropic Claude on Vertex AI (anthropic[vertex] SDK)  │   │
│  │  - claude-haiku-4.5 (claude-haiku-4-5 on Vertex)        │   │
│  │  - claude-sonnet-4-6 (claude-sonnet-4-6 on Vertex)      │   │
│  │  - claude-opus-4-6 (claude-opus-4-6 on Vertex)          │   │
│  │  - claude-opus-4-7 (claude-opus-4-7 on Vertex)          │   │
│  │  - claude-opus-4-8 (claude-opus-4-8 on Vertex, 1M ctx)  │   │
│  │  - claude-sonnet-5 (claude-sonnet-5 on Vertex, 1M ctx)  │   │
│  │  - claude-sonnet-5-5 (claude-sonnet-5-5 on Vertex, 1M)  │   │
│  │  - claude-opus-5 (claude-opus-5 on Vertex, 1M ctx)      │   │
│  │  - claude-opus-5-5 (claude-opus-5-5 on Vertex, 1M ctx)  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Component Overview

### Frontend (React + Vite)

The frontend is a modern single-page application built with:

- **React 19**: UI framework with hooks-based component architecture
- **Vite 7**: Fast build tool with hot module replacement
- **TypeScript**: Type-safe development with strict typing
- **Native WebSocket API**: For real-time streaming from LLM providers

**Main Components**:
- **App**: Main application component with URL-based routing and three-column layout
  - Wrapped in `AppProviders` and `BrowserRouter` for global state and client-side routing
  - Three route patterns: `/`, `/chats/:id`, `/projects/:pid/:id` -- URL params are the source of truth for active conversation
  - Sidebar (with Requests, Search, Projects, and Conversations sections) + ChatPanel + FileBrowser layout spanning full browser window
  - Session cookie auth with inline `SignInScreen` if not authenticated (no redirect to `/auth/`)
- **Sidebar**: Multi-section navigation with Requests, Search, Projects, and Conversations
  - Requests section with badge counter for open action requests
  - Search section with magnifier icon that opens `SearchModal` for cross-conversation message search (also triggered by Cmd/Ctrl+K)
  - Projects section is always visible: shows dotted-border "Create Project" button when empty, "+" button in header when projects exist. Project entries display folder icons and right-pointing chevrons. Clicking a project triggers a slide-left drill-down into that project's routines and conversations, with a back button to return. Routines section is always visible in the drill-down (shows "Create Routine" button when empty, "+" button in header when routines exist); each routine entry has a play button and a settings gear icon
  - Conversations list with inline "New Chat" button at top. Compact conversation entries (title only, no message count or timestamp)
  - Subscribes directly to `webSocketManager.onStreamComplete` and uses stale-while-revalidate refresh (keeps existing list visible while fetching)
  - Includes `UserInfoBar` at the bottom showing user name, email, and settings gear icon
- **SearchModal**: Conversation search modal (`frontend/src/components/SearchModal.tsx`)
  - Debounced typeahead input calling `searchConversations()` in `frontend/src/api/client.ts`
  - Keyboard navigation through results, highlighted query matches in snippets
  - Selecting a result navigates to the conversation and scrolls to the matching message via `scrollToMessageIndex` in `NavigationContext`
- **ChatPanel**: Main chat interface with message display and input
  - Uses `useConversation` hook for per-conversation state
  - `data-message-index` attributes on message elements for scroll targeting from search results
  - Scroll-to-message with highlight animation when `scrollToMessageIndex` is set
  - Guide selector dropdown next to model selector for picking a guide per conversation. Includes a pencil icon button that opens Settings to the Guides section. Once the first message is sent, the guide selector becomes read-only (locked). Old conversations without a guide hide the selector entirely. See [Guides Architecture](guides.md)
  - "+ Skill" button next to the guide selector for loading skills into the current conversation. Opens `SkillSelectorModal` for multi-select skill browsing with keyword filter. See [Skill Library Architecture](skill-library.md)
  - Model selector dropdown with provider locking: after the first message, the dropdown filters to only show models from the same provider (Gemini or Anthropic), preventing cross-provider switching mid-conversation. See [Frontend Architecture](frontend.md) for provider locking details
  - Real-time markdown rendering during streaming
  - Pre-stream typing indicator with animated dots
  - Blinking cursor on streaming text
  - Structured message rendering for tool use display
  - Consecutive tool calls grouped into collapsible units via `ToolCallGroup` component; uses `groupConsecutiveToolCalls()` and `findToolResultById()` utilities
- **Message**: Message display component with markdown rendering and syntax highlighting
  - MessageContentRenderer for routing to appropriate component based on message type (text, tool_use, tool_result, action_request, stats)
- **ActionRequestMessage**: Inline rendering of agent-proposed action requests (e.g., `send_slack_message`, `send_telegram_message`, `create_calendar_invite`, plus plugin-registered types) with Approve/Revise/Stop buttons (labeled per the handler's verb -- "Send" for messaging requests, "Create"/"Save" for create/edit requests).
  - Non-blocking -- does not disable the chat input. State persisted in the database, so survives page reloads.
  - Displays resolved channel names (e.g., #general, @alice) for Slack message requests, resolved dialog names for Telegram message requests, and calendar invite details for invite requests.
  - Resolved requests collapse to a one-line summary rendered from each handler's `resolved_label` + `summary_snippet`. See [Action Requests Architecture](action-requests.md)
- **ToolCallGroup**: Groups consecutive tool calls into a two-level collapsible hierarchy
  - Collapsed: shows the latest tool call + "and N more tools" summary
  - Expanded: shows all tool calls in the group, each individually expandable via `ToolUseMessage`
  - See `frontend/src/components/ToolCallGroup.tsx` and `ToolCallGroup.css`
- **ToolUseMessage**: Collapsible component for displaying individual tool invocations
  - Shows the agent-provided `intent_message` (description) as the primary label and the tool name as the secondary label; falls back to tool name as primary if no description is available
  - Human-readable descriptions use proportional sans-serif font; raw tool names use monospace font
  - Special rendering for `agent_task` calls: displays the sub-agent's `name` as the title and `description` as the intent, with a person icon instead of the default wrench icon
  - Special rendering for `agent_task_parallel` calls: displays "{N} parallel sub-agent(s)" as the title and lists sub-agent names as the intent, with a person icon
  - Running/completed status indicator
  - Expandable input/output sections
  - Animated transitions and dark/light mode support
- **UserInfoBar**: User profile bar at bottom of sidebar
  - Displays user name (or email prefix as fallback), email, and avatar initial
  - Gear icon opens the SettingsModal
  - User info fetched via `GET /app/api/me` (stored in `AuthContext`)
- **SettingsModal**: Full-screen settings panel
  - 80% viewport modal rendered via `createPortal` to `document.body`
  - `SettingsModal.tsx` is a thin shell providing modal chrome, left nav sidebar, and section routing
  - `SettingsModal.css` retains shell/layout styles and shared utility classes
  - 6 per-section sub-components in `frontend/src/components/settings/`, each managing its own state and data fetching, mounting/unmounting as the user navigates tabs:
    - `DataConnectionsSection` -- OAuth connectors (Google Services, Slack, Telegram, plugin rows like GitHub and Twitter/X) with Connect/Reconnect buttons and popup handling; API key connectors (Airtable, api_key-kind plugin rows) with shared `ApiKeyForm`. See [OAuth Popup Flow](oauth-popup.md)
    - `MemoriesSection` -- Memory CRUD with archive toggle, inline editing, "Show archived" toggle
    - `GuidesSection` -- Guide CRUD with default guide handling, inline editing, save status feedback. After mutations, `refreshContextGuides()` updates `GuidesContext`
    - `SkillsSection` -- Tabbed interface ("My Skills" / "Shared with Me"), skill CRUD, visibility badges, sharing with type-ahead user search, auto-load toggle with server-backed persistence via `user_skill_autoloads` table
- **SkillSelectorModal**: Multi-select modal for loading skills into the current conversation (see `frontend/src/components/SkillSelectorModal.tsx`)
  - Fetches all accessible skills and auto-loaded skill IDs on open
  - Client-side keyword filtering by name and description
  - Already-loaded skills greyed out with "loaded" badge; auto-loaded skills greyed out with "auto-loaded" badge
  - Confirm loads selected skills into the conversation; skills persist for the entire conversation from that point
    - `SignOutSection` -- Three tiers: Logout, Logout & Disconnect, Delete Account
  - Barrel re-export via `frontend/src/components/settings/index.ts`
  - Loads settings via `GET /app/api/settings`, saves via `PUT /app/api/settings`
  - Auto-opens to Data Connections when authenticated but no services are connected (truly new users only)
  - Popup-blocked fallback: shows direct link to open OAuth URL in a new tab if browser blocks the popup
  - Escape key closes modal; overlay click closes modal
- **SignInScreen**: Inline sign-in component displayed for unauthenticated users
  - Heading uses `appName` from `AppConfigContext` ("DevQuest" in dev mode, "Quest" in production)
  - Fetches Google OAuth URL from `GET /auth/login-url` and displays a "Sign in with Google" button
  - When `isDevMode` is true (from AppConfigContext), shows a dev login section below the Google button: email input field + "Dev Login" button, separated by an "or" divider. Submits `POST /auth/dev-login` with `{"email": "..."}` and reloads the page on success
  - Replaces the previous redirect-to-`/auth/` behavior for fresh/unauthenticated users
- **FileBrowser**: Right-side panel for workspace file management
  - List view of files/folders with icons and metadata
  - Upload files and folders via drag-and-drop (folders recursively traversed via `directoryTraversal.ts`), upload files via Upload Files button, create new folders in the current path via New Folder button (opens `NewFolderModal`), download files, download folders as zip
  - Inline viewing of text files (`.md`, `.py`, `.txt`), images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.bmp`, `.ico`, `.avif`), and PDFs (`.pdf`, via the pdf.js-based `PdfViewer` with thumbnail rail) via FileViewerModal, with Save to Drive for `.md` files
  - Folder navigation with back/forward/up buttons
  - Per-conversation path state via `FileBrowserStateContext`
  - Auto-refresh on `file_list_changed` per-user globals (emitted by workspace-mutating tools and REST routes), filtered by active conversation / project, with a 200ms debounce
  - Structured error display for partial upload failures
- **API Client**: Type-safe REST API functions

**State Management Architecture**:
- **conversationStore**: Observable store holding per-conversation state (messages, streaming, errors)
- **WebSocketManager**: Singleton shim over `persistentWebSocket` that routes WebSocket messages to the correct conversation in the store; exposes `onStreamComplete` and `onConversationRenamed` callback subscriptions for component-level refresh (Sidebar subscribes for conversation list refresh; ProjectTables subscribes for the project DB table list refresh). The FileBrowser instead subscribes to `persistentWebSocket.onGlobalEvent` for `file_list_changed` per-user globals so it refreshes mid-turn after each workspace mutation
- **Application contexts** (`frontend/src/contexts/*Context.tsx`, one per responsibility, composed by `AppProviders` -- see [Frontend](frontend.md#application-contexts-srccontexts)): React contexts providing:
  - activeConversationId, auth state, per-conversation browser path state, user info (`userEmail`, `userName`), `googleServicesConnected` flag, `hasAnyServiceConnected` flag
  - settings modal state (`isSettingsOpen`, `setSettingsOpen`)
  - `refreshConnectionStatus()` for updating connection state after OAuth popup completion
  - guide state (`guides`, `guidesLoaded`, `loadGuides()`, `getGuideForConversation()`, `setGuideForConversation()`, `isConversationGuideLocked()`, `lockConversationGuide()`, `conversationHasGuide()`)
  - provider locking state (`getLockedProvider()`, `lockConversationProvider()`, `isProviderLocked()`)
  - search navigation state (`scrollToMessageIndex`, `setScrollToMessageIndex`)
- **useConversation**: Hook using `useSyncExternalStore` to subscribe to specific conversation's state
- **useFileBrowser**: Hook managing file list state, path navigation, and silent refresh

This architecture enables seamless conversation switching during streaming - messages route to the correct conversation regardless of which conversation is currently visible. See [Frontend Architecture](frontend.md) for details.

**Streaming Features**:
- Streaming messages render markdown in real-time (headings, code blocks, tables, etc.)
- Visual feedback before first chunk arrives ("Generating response...")
- Blinking cursor indicator at end of streaming text
- Consistent rendering between streaming and completed messages

**Tool Use Display**:
- Tool invocations displayed as collapsible UI components during streaming
- Description-first display: the agent-provided `intent_message` is shown as the primary label (in proportional sans-serif font) and the tool name as the secondary label; falls back to tool name as primary (in monospace font) if no description is available
- Collapsed state uses 85% opacity (hover: 100%) with boosted alpha on text, icons, badges, and chevrons for improved legibility in both dark and light modes
- Status indicator: "running..." (animated pulse) or "completed" (green)
- Special rendering for `agent_task` tool calls: sub-agent `name` shown as title, `description` shown as intent, person icon instead of wrench icon (see `frontend/src/components/ToolUseMessage.tsx`)
  - Special rendering for `agent_task_parallel` tool calls: "{N} parallel sub-agent(s)" shown as title, sub-agent names listed as intent, person icon
- Expandable sections reveal full tool input (JSON) and output
- Tool results paired with their corresponding tool_use messages
- Smooth animations for expand/collapse and status transitions
- Full dark/light mode support with contrast improvements in both modes

**Usage Stats Display**:
- After each Gemini CLI response, usage statistics are displayed as small, centered, low-contrast metadata
- Stats shown: input tokens, output tokens, duration (seconds), and tool calls
- Format: `INPUT 268104   OUTPUT 4942   TIME 119.2s   TOOLS 12`
- Implementation: Backend parses "result" type messages from Gemini CLI (`chat/routes/_helpers.py`), frontend renders via `MessageContentRenderer` in `frontend/src/components/Message.tsx`
- TypeScript types: `UsageStats` and `StatsMessage` in `frontend/src/api/types.ts`
- Styling: `frontend/src/components/Message.css` (`.stats-message` class)

**Context Usage Indicator**:
- Displays context window utilization as a small circular meter (Claude Code-style ring) on the composer controls row, right-aligned beside the Send button; the ring fills clockwise from 12 o'clock as the context is consumed and has a FIXED footprint whatever the percentage (a variable-width "NN% context" label used to push the Send button out of narrow phone rows)
- Hover tooltip shows the figures (e.g., "14% of context used · 28K / 200K max"); the ring carries an `aria-label` with the same percentage
- Color-coded thresholds on the ring fill: accent colour (under 70%), amber warning (70-90%), red danger (90%+)
- Clicking the ring (it renders as a button when `onInfoClick` is given) opens a `SystemPromptModal` showing the full system prompt used for the current conversation; there is no separate info icon anymore
- Backend emits `context_tokens` and `max_context_tokens` in the stats event via `compute_total_context_tokens()` in `chat/llm/base.py` (provider-specific: Anthropic sums input + cache tokens; Gemini uses raw input tokens)
- Frontend: `ContextIndicator` component in `frontend/src/components/ContextIndicator.tsx` (with `onInfoClick` callback for system prompt viewer), hydrated from stats events during streaming (via `WebSocketManager`) and from historical messages on conversation load (via `useConversation` hook)
- State: `contextTokens` and `maxContextTokens` fields in `conversationStore`, updated via `setContextUsage()`
- Fallback max context from `maxInputTokens` in `frontend/src/constants/models.ts` if not provided by stats event

**System Prompt Viewer**:
- Full system prompt persisted to `data/chats/{conversation_id}/system_prompt.txt` on each message send
- API endpoint `GET /app/api/conversations/{conversation_id}/system-prompt` returns the saved system prompt text (see `chat/routes/conversations.py`)
- Frontend: `SystemPromptModal` component in `frontend/src/components/SystemPromptModal.tsx` (styled like `FileViewerModal`, fetches system prompt on open, Escape to close)
- Persistence: `ChatStorage.set_system_prompt()` and `ChatStorage.get_system_prompt_text()` in `chat/storage.py`

The frontend is built as static files and served by FastAPI on the same port as the backend API.

### Backend (FastAPI)

The backend provides:

1. **Authentication Layer**: Two-tier Google OAuth architecture with dual auth
   - **App Login**: Minimal scopes (`openid`, `userinfo.email`, `userinfo.profile`) for user identity. Establishes session cookie and API key. Unauthenticated users see an inline `SignInScreen` on `/` that fetches the OAuth URL from `GET /auth/login-url`.
   - **Google Services** (`/auth/google-services`): Separate OAuth flow for Google API access (Gmail, Calendar, Drive, Docs, Sheets, Tasks). Can be re-authorized independently without affecting login session or other integrations. Connect/Reconnect buttons available in the Data Connections section of the Settings panel. OAuth flows open in popup windows (see [OAuth Popup Flow](oauth-popup.md)). Users who connected before the `tasks.readonly` scope was added must re-authorize to grant the new scope.
   - OAuth callback preserves all existing user data (Slack, Telegram, Airtable, GitHub, plugin-key connections) on re-auth
   - All OAuth callbacks either close the popup (when `?popup=1` is active) or redirect to `/` (non-popup fallback)
   - Login OAuth and Google Services OAuth are fully separated with no cross-pollination; after logout+disconnect and re-login, Google Services correctly shows "Not Connected"
   - **Dual auth on frontend endpoints**: All `/app/api/*` endpoints accept session cookie OR API key Bearer token. Cookie tried first (browser), then API key (scripts/LLM).
   - Frontend authenticates via session cookie directly (no API key stored in browser)
   - Frontend retrieves user info via `GET /app/api/me` with session cookie (includes `google_services_connected`, `has_any_service_connected`, and `is_admin` booleans)
   - Users can reset their API key via `POST /api/reset-api-key` (cookie or Bearer token auth, see `quest.py`; auth dependencies in `auth/session.py`)
   - Domain restriction (the allowed login domain, see `auth/config.py`); relaxed in dev mode (`QUEST_ENV=dev`) via `check_user_allowed()` in `chat/auth.py` to allow any email domain
   - **Dev Login** (`QUEST_ENV=dev` only): `POST /auth/dev-login` accepts `{"email": "..."}`, auto-creates user with deterministic name, sets session cookie. Returns 404 in non-dev mode. Users with existing Google OAuth credentials are blocked (403). See [Auth Submodule](auth.md) for details

2. **Chat API**: RESTful endpoints and WebSocket streaming
   - Conversation CRUD operations (including rename)
   - Real-time message streaming via WebSocket
   - Chat history persistence
   - User info and settings endpoints (`GET /app/api/me`, `GET /app/api/settings`, `PUT /app/api/settings`)
   - Unauthenticated app config endpoint (`GET /app/api/config`) for frontend branding (returns `quest_env`)
   - Unauthenticated version endpoint (`GET /app/api/version`) returning the git commit hash and the release version derived from the nearest `v<semver>` git tag, captured at process startup (see `config/version.py`; shown in Settings > About)
   - Memory CRUD, update, archive/unarchive, and search endpoints (`GET/POST /app/api/memories`, `GET/PUT/DELETE /app/api/memories/{id}`, `PUT /app/api/memories/{id}/archive`, `PUT /app/api/memories/{id}/unarchive`) -- see `chat/memory_routes.py`
   - Guide CRUD endpoints (`GET/POST /app/api/guides`, `GET/PUT/DELETE /app/api/guides/{id}`) -- see `chat/guide_routes.py`
   - Project CRUD and project-conversation endpoints (`GET/POST /app/api/projects`, `GET/PUT/DELETE /app/api/projects/{id}`, `GET/POST /app/api/projects/{id}/conversations`) -- see `chat/project_routes.py` and [Projects API](../api/projects-api.md)
   - Routine CRUD endpoints (`GET/POST /app/api/projects/{id}/routines`, `GET/PUT/DELETE /app/api/projects/{id}/routines/{routine_id}`) -- see `chat/routine_routes.py` and [Routines API](../api/routines-api.md)
   - Schedule CRUD endpoints (`GET/POST/PUT/DELETE /app/api/projects/{id}/routines/{routine_id}/schedule`) -- see `chat/schedule_routes.py` and [Schedules API](../api/schedules-api.md)
   - Action request endpoints (`GET /app/api/action-requests`, `GET /app/api/action-requests/{id}`, `POST /app/api/action-requests/{id}/resolve`) -- see `chat/action_request_routes.py` and [Action Requests Architecture](action-requests.md)
   - Conversation search endpoint (`GET /app/api/search?q={query}`) -- file-based scanning with case-insensitive matching and highlighted snippets; see `chat/routes/conversations.py` and `chat/storage.py`
   - Connector status endpoint (`GET /app/api/connectors`)
   - Account management endpoints (`POST /app/api/logout`, `POST /app/api/logout-and-disconnect`, `POST /app/api/delete-account`)
   - Admin operations: `POST /app/api/admin/shutdown` (requires admin auth via `is_admin()` in `chat/auth.py`; admin emails configured in `server_config.json` `admin_emails` array)

3. **Background Scheduler**: Asyncio background task for automatic routine execution
   - Started via FastAPI's `lifespan` context manager in `quest.py`
   - Polls every 30 seconds for due schedules across all users
   - Executes scheduled routines headlessly (creates conversations, calls `run_conversation_turn()` with no-op callback)
   - Supports daily, hourly, and interval-based schedules with timezone-aware DST handling
   - Skip-if-running logic prevents overlapping interval-based runs; stale flag cleanup after 2 hours
   - Spawned execution tasks tracked in `_active_execution_tasks` set in `chat/scheduler.py`; `shutdown()` cancels all tracked tasks on server shutdown; `_execute_scheduled_run()` handles `CancelledError` gracefully
   - See [Scheduling Architecture](scheduling.md) for full details

4. **LLM Provider Abstraction**: Multi-provider support via `chat/llm/` package
   - `LLMProvider` abstract interface with implementations for Gemini and Anthropic (Vertex AI)
   - Model registry maps model IDs to providers; provider singletons created lazily
   - Provider-agnostic tool definitions in JSON Schema format with per-provider converters
   - See [LLM Provider Abstraction](llm-providers.md) for full details

5. **Workspace Management**: Isolated environments per conversation, plus a shared one per project
   - Every conversation (standalone or in a project) gets its own conversation workspace at `data/chats/{conversation_id}/workspace/` (`ChatStorage.get_conversation_workspace_root()`)
   - Each project additionally has a shared project workspace at `data/projects/{project_id}/workspace/workspace/` (`ChatStorage.get_project_workspace_root()`)
   - LLM session state persists across sessions via `sdk_history.json`

### Storage Layer

User data, memories, guides, projects, routines, routine schedules, conversation metadata, and action requests are stored in a SQLite database; chat message history uses JSON files. All data-directory paths are defined in `config/paths.py` and can be relocated by setting the `data_dir` key in `server_config.json` (see [Data Paths](data-paths.md)):

```
data/
├── quest.db                    # SQLite database (user accounts, API keys, OAuth tokens, settings, memories, guides, projects, routines, routine schedules, conversation metadata including custom names, action requests)
├── logs/                        # Application log files (created automatically at startup)
│   └── large_tool_results.jsonl  # Tool call results exceeding 2048 bytes (JSONL format, rotating, 50MB max, 3 backups)
├── projects/
│   └── {project_id}/
│       └── workspace/           # Shared workspace for all conversations in a project
└── chats/
    └── {conversation_id}/       # Per-conversation directory (flat layout, one folder per UUID)
        ├── chat_history.json    # Messages and metadata (includes project_id for project conversations)
        └── workspace/           # Standalone conversation workspace

config/
└── paths.py                     # Centralized data-directory path constants (DATA_DIR, DATABASE_PATH, CHATS_DIR, etc.)

server_config.json                # Server configuration (admin_emails, model, Vertex AI settings, optional data_dir override)
alembic.ini                       # Alembic migration configuration
```

**Database layer** (`db/` package):
- `db/engine.py` -- Dual sync/async SQLAlchemy engines for the SQLite database (path from `DATABASE_PATH` in `config/paths.py`, defaults to `data/quest.db`): sync `engine` + `SessionLocal` (Alembic migrations only) and async `async_engine` + `AsyncSessionLocal` (all store modules and their FastAPI endpoint callers)
- `db/models.py` -- `User`, `Memory`, `Guide`, `Project`, `Routine`, `RoutineSchedule`, `Conversation`, `ActionRequest`, `LlmCallGemini`, and `LlmCallAnthropic` ORM models:
  - User has id (auto-increment integer PK), email (unique index), api_key (unique index), OAuth tokens as JSON columns, settings as JSON column
  - Memory has id (UUID PK), user_id (FK to users.id, indexed), content (Text), created_at
  - Guide has id (UUID PK), user_id (FK to users.id, indexed), name, content, is_default, created_at, updated_at
  - Project has id (UUID PK), user_id (FK to users.id, CASCADE, indexed), name, guide, created_at, updated_at
  - Routine has id (UUID PK), project_id (FK to projects.id, CASCADE), user_id (FK to users.id, CASCADE), name, prompt, guide_id (FK to guides.id, SET NULL)
  - RoutineSchedule has id (UUID PK), routine_id (FK to routines.id, CASCADE, unique), user_id (FK to users.id, CASCADE), schedule_type, type-specific columns, scheduling state
  - Conversation has id (Text PK, UUID), user_id (FK to users, indexed), project_id (nullable FK to projects.id, CASCADE, indexed), custom_name (nullable String(100)), created_at, last_message_at
  - ActionRequest has id (auto-increment int PK), user_id (FK to users.id, CASCADE, indexed), conversation_id, request_type, params (JSON), reasoning, status, result (JSON), created_at, resolved_at
- `db/user_store.py` -- User data access (async)
- `db/memory_store.py` -- Memory CRUD and search (async)
- `db/guide_store.py` -- Guide CRUD with default guide management (async)
- `db/conversation_store.py` -- Conversation metadata including project membership and archiving (async)
- `db/project_store.py` -- Project CRUD (async)
- `db/routine_store.py` -- Routine CRUD (async)
- `db/schedule_store.py` -- Schedule CRUD and scheduler helpers for run lifecycle tracking (async)
- `db/action_request_store.py` -- Action request CRUD and enriched listing (async)

**Migrations** (`alembic/`):
- Schema changes managed by Alembic with autogenerate support
- `run.py` runs `alembic upgrade head` before starting the server
- See [Database Architecture](database.md) for details

**Key Design Decisions**:
- SQLite for user data, memories, guides, projects, routines, routine schedules, conversation metadata, and action requests (transactional writes, indexed API key lookups, FTS5 for memory search, no separate server)
- JSON files for chat message history (append-heavy, per-conversation isolation)
- Flat `data/chats/{conversation_id}/` layout: ownership and timing tracked in SQLite `conversations` table instead of being derived from the directory hierarchy; `ChatStorage` path helpers no longer accept `user_id`
- Workspace isolation for standalone conversations; shared workspace for project conversations (see [Projects Architecture](projects.md))
- Preserved Gemini state (session files, settings)
- UTC timestamps with explicit "Z" suffix for consistent timezone handling (see `utc_timestamp()` in `chat/storage.py`)

### LLM Integration

Conversations run through the direct SDK integration in `chat/gemini_api/` (see [Gemini API Integration](gemini-api.md)) on top of the multi-provider abstraction in `chat/llm/` (see [LLM Provider Abstraction](llm-providers.md)):

- **Model**: User-selectable per conversation via UI dropdown (`gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `claude-haiku-4.5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, or `claude-opus-5-5`); after the first message, switching is restricted to models from the same provider. `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`, and `gemini-3.5-flash` are deprecated: hidden from the dropdown for new picks but still runnable by conversations/routines that already use them
- **Vertex AI**: All models (Anthropic and Gemini) run on Vertex AI and use Google Cloud Application Default Credentials

**Configuration System**:

The default model is configured via `server_config.json` (key: `gemini.model`, default: `gemini-3.1-pro-preview` -- deprecated but still runnable; deployments should override this to a current model), loaded by `load_server_config()` in `config/server_config.py` along with the `anthropic.*` and `gemini_vertex.*` Vertex AI settings and their env-var overrides.

(The former Docker-based Gemini CLI chat mode -- `gemini.chat_mode`, `gemini.docker_image`, `Dockerfile.gemini`, and `chat/gemini_docker.py` -- was removed when gemini-cli was deprecated.)

**Gmail Integration Capabilities**:

Quest provides both read and write access to Gmail:
- **Read access**: Search messages, fetch message content (decoded/markdown), list labels (uses `gmail.readonly` scope)
- **Write access**: Create draft emails via the `create_gmail_draft` dynamic tool / `POST /api/gmail-simple/drafts` (uses `gmail.compose` scope, does NOT send); send emails to self via the `send_gmail_to_self` dynamic tool / `POST /api/gmail-simple/send-self` (subject auto-prefixed with `[Quest]`, markdown body rendered to HTML, sent as multipart/alternative, same drive/workspace/gmail attachments + inline `cid:` images as drafts)
- **Reply threading**: Simplified messages include `message_id_header` for constructing reply drafts with proper threading

OAuth scopes are configured in the `SCOPES` and `GOOGLE_SERVICE_SCOPES` lists in `auth/config.py`. The `gmail.compose` scope allows sending, but direct sending is restricted to the authenticated user only (via `send-self`); emails to other recipients must go through draft creation. The `drive.file` scope enables Save to Drive functionality for workspace files. For details, see [Gmail API Documentation](../api/gmail-api.md) and [Drive API Documentation](../api/drive-api.md).

**Slack Integration Capabilities**:

Quest provides both read and write access to Slack using a two-tier token architecture:
- **Read access**: Dedicated dynamic tools dispatched via `tool_call` (`search_slack_messages`, `list_slack_conversations`, `get_slack_conversation_history`, `get_slack_conversation_replies`, `get_slack_user_info`, `list_slack_users`, `list_slack_teams`) wrapping the endpoint functions in `plugins/slack/upstream.py` directly (uses per-user token obtained via OAuth).
  - No per-method HTTP routes: the read routes were removed from quest.py -- sandbox scripts invoke the same tools through the script tool-call bridge (`POST /api/tool-call` with a `{tool_name, arguments}` body, see [Gemini API -- Script Tool-Call Bridge](gemini-api.md#script-tool-call-bridge-post-apitool-call)) -- and the `/api/slack` prefix is blocked in route dispatch so cached sessions still emitting `curl_proxy_get` get a use-the-dedicated-tool error.
  - All list reads cap results to `MAX_LIMIT = 50` per request; when a higher limit is requested, a `ToolResultWithNotices` advisory directs the LLM to paginate via `next_cursor` (merged into the tool's JSON result under a `notices` key).
  - The workspace-scoped reads accept an optional `team_id` parameter for cross-workspace queries; defaults to the user's home workspace
- **Channel search**: `find_slack_channel` dynamic tool (dispatched via `tool_call` meta tool) in `plugins/slack/tools.py` searches channels by name substring (case-insensitive) across all workspaces. Resolves E-prefix Enterprise IDs to T-prefix workspace IDs via `auth.teams.list` (bot token), then queries `conversations.list` (user token). Uses existing `channels:read` and `groups:read` scopes
- **Write access (bot, self-messaging)**: Send DMs to the authenticated user via the `send_slack_dm_to_self` dynamic tool (uses shared bot token; messages appear from the Quest app), with optional workspace-file attachments (`files`: workspace-relative paths, max 10, 50 MB each).
  - Attachments are uploaded through Slack's external upload flow (`conversations.open` for the DM channel, then `files.getUploadURLExternal` + `files.completeUploadExternal` with the message as initial comment, so text and files land as ONE Slack message; file sends need the `files:write` and `im:write` bot scopes on top of `chat:write`).
  - This is the preferred path when the user wants to message themselves (e.g., "send me a message", "DM me", "remind me") -- it requires no approval and works in automated/scheduled routines where there is no human to approve action requests. The system prompt, tool declaration, and API instructions all direct the model to prefer this tool for self-messaging over `send_slack_message` action requests.
  - It is also the sole connector tool available in [public-project](public-projects.md) conversations (outbound-only, fixed recipient). There is no HTTP route (the old `POST /api/slack-simple/dm-self` is retired); sandbox scripts send text-only self-DMs through the `/api/tool-call` bridge
- **Write access (user, others/channels)**: Send messages to other people or Slack channels via `send_slack_message` action requests (uses per-user token, requires `chat:write` and `im:write` scopes, Block Kit formatted with dynamic Quest attribution that includes the sender's first name). Channel names resolved at creation time for preview display. Legacy `send_slack_dm` handler preserved for backward compatibility
- **Org-wide queries**: List workspaces via the `list_slack_teams` tool (uses shared bot token)

The bot token is a shared credential installed once by an org admin and stored in the `slack` section of `server_credentials.json` alongside `client_id` and `client_secret`. Per-user OAuth grants user tokens for both read operations and user-initiated write operations (sending Slack messages via action requests). For Slack setup and OAuth configuration, see [Slack App Setup Guide](../../plugins/slack/docs/slack-app-setup.md).

**Telegram Integration** (the `plugins/telegram` plugin -- see [Plugin Architecture](plugins.md)):

Quest supports Telegram via Telethon:
- **Authentication**: Telegram's own login (phone number, code sent to the Telegram app, optional cloud password) run by the plugin's `/auth/telegram` router (`plugins/telegram/auth.py`); the in-flight login is parked server-side in the credential row's `oauth_blob.pending`
- **Session storage**: Telethon's StringSession in the user's `user_service_credentials` row (service `telegram`, `oauth_blob.session`; migration `d7a1f3c9e2b4` moved it out of the old `users.telegram_session` column)
- **Configuration**: API credentials (`api_id`/`api_hash`) in the per-service credential store (admin Settings > Service Credentials; legacy `telegram_app_info` section of `server_config.json` migrated by the plugin's `post_load` hook)
- **Client helpers**: `plugins/telegram/upstream.py` provides `create_telegram_client()`, credential loading, and the `TelegramClientManager` whose per-user connections the plugin's `on_shutdown` hook closes
- **Read access**: List dialogs, fetch messages, list contacts via the dedicated `telegram_get_me` / `telegram_list_dialogs` / `telegram_get_messages` / `telegram_list_contacts` dynamic tools (`tool_call`; no `/api/telegram/*` HTTP routes -- the prefix is blocked in route dispatch; sandbox scripts use the `/api/tool-call` bridge)
- **Write access**: Send messages to any Telegram dialog (user, group, or channel) via `send_telegram_message` action requests (uses Telethon client, HTML-escaped messages with attribution footer, dialog names resolved at creation time for preview display)

For Telegram setup and configuration, see [Telegram Setup Guide](../../plugins/telegram/docs/telegram-setup.md).

**Airtable Integration**:

Quest provides read-only access to Airtable via Personal Access Tokens and the `authed_get` tool:
- **Authentication**: Personal Access Token (PAT) stored in `airtable_token` column in `db/models.py`. PATs are used instead of OAuth to avoid token refresh complexity, conversation coordination issues, and background refresh requirements -- see [Airtable API Documentation](../api/airtable-api.md) for the full rationale
- **Token management**: Endpoints in `auth/airtable.py` at `/auth/airtable/save-token` and `/auth/airtable/remove-token`
- **API access**: Read-only access via `authed_get` tool with `https://api.airtable.com/v0/...` URLs; instruction text only in `api/airtable.py` (no proxy endpoints)
- **Allowed endpoints**: Server-side URL path validation via `allowed_endpoints` regex patterns in `_SERVICE_REGISTRY` restricts access to five read-only API paths
- **Configuration**: Users paste their PAT in Settings > Data Connections via the shared `ApiKeyForm` component; when connected, shows "Connected" badge with key preview (...XXXX via `key_hint`) and Disconnect button
- **Read access**: List bases, get base schemas, list/filter records, get individual records, list comments
- **Token validation**: Tokens must start with "pat" prefix
- **Session invalidation**: Token save/remove triggers system prompt refresh to include/exclude Airtable documentation

For Airtable API details, see [Airtable API Documentation](../api/airtable-api.md).

**GitHub Integration**:

Quest provides read-only access to GitHub via per-user OAuth tokens and the `authed_get` tool, packaged as the in-tree `plugins/github` plugin (the oauth-kind reference plugin -- see [Plugins](plugins.md)):
- **Authentication**: Standard GitHub OAuth App flow. Tokens do not expire (no refresh logic needed). Admin client credentials in the `github` per-service credential store entry (legacy `server_credentials.json` `github` section migrates at startup)
- **OAuth flow**: the plugin's router in `plugins/github/oauth.py` (`/auth/github`, `/auth/github/callback`, `/auth/github/disconnect`) with popup window support, mounted by `mount_plugin_oauth_routers()`
- **Token storage**: `oauth_blob` of the user's `user_service_credentials` row for service `github` (`access_token`, `token_type`, `scope`, granted `scopes` list, `authorized_at`)
- **Scopes**: `repo` (private repo access -- no read-only scope exists in GitHub) and `read:org` (organization membership). Defined in `GITHUB_SCOPES` in `plugins/github/upstream.py`; a stale grant (granted set no longer covering `GITHUB_SCOPES`) flags `needs_reauth` on the connector row
- **API access**: Read-only access via `authed_get` tool with `https://api.github.com/...` URLs; instruction text in `plugins/github/instructions.md` (the `system:github` skill). The plugin-registered `api.github.com` service entry provides the credential loader, Bearer auth injector, and allowed-endpoints list
- **Allowed endpoints**: Server-side URL path validation via `allowed_endpoints` regex patterns in the service entry enforces the read-only allow-list (user profile, repos, branches, issues, PRs, commits, contents, org repos, search, Actions reads) regardless of what the `repo` OAuth scope grants at the token level
- **Default headers**: The service entry's `default_headers` field injects `User-Agent: Quest/1.0` and `Accept: application/vnd.github+json` on every request (GitHub's REST API rejects requests without a `User-Agent`). See [Authenticated External API Requests](gemini-api.md#authenticated-external-api-requests-authed_get)
- **Configuration**: Users connect via Settings > Data Connections using the OAuth popup flow
- **Session invalidation**: Connect triggers system prompt refresh to include/exclude GitHub API documentation

For GitHub setup, see [GitHub App Setup Guide](../../plugins/github/docs/github-app-setup.md). For API details, see [GitHub API Documentation](../../plugins/github/docs/github-api.md).

### Gemini API Migration

The project migrated from the original Docker-based Gemini CLI integration to a direct Python Gemini API integration using the `google-genai` SDK (the Docker mode has since been removed entirely). This migration has three layers:

**Route Dispatch** (`chat/route_dispatch.py`) -- Internal tool call dispatch that lets the model's `curl_proxy_get` and `curl_proxy_post` tool calls invoke FastAPI endpoint handlers directly without HTTP. The dispatch pipeline validates URLs (must be `localhost/api/*` on an allowed port -- `8000` in production, both `8000` and `9000` in dev mode), resolves routes via Starlette's `route.matches()`, introspects handler signatures to build keyword arguments, and calls endpoints directly.

Every successful dispatch is logged at INFO level with HTTP method, path, endpoint name, duration, user email, and result length; errors are logged with tool name, URL, and user email. This module established the `user=<email>` logging convention. See [Route Dispatch Architecture](route-dispatch.md).

**LLM Provider Abstraction** (`chat/llm/` package) -- Multi-provider LLM abstraction layer with `LLMProvider` interface, model registry, and provider-agnostic tool definitions. Supports Google Gemini (via `google-genai` SDK) and Anthropic Claude (via `anthropic[vertex]` SDK on Vertex AI). See [LLM Provider Abstraction](llm-providers.md).

**Conversation Loop** (`chat/gemini_api/` package) -- Provider-agnostic conversation loop using the `LLMProvider` interface for streaming and tool calling. Replaces the Docker container with in-process SDK chat sessions. The package defines tool declarations in three tiers in `chat/llm/tool_schemas.py` (`BASE_TOOLS` with 9 shared tools including the `tool_call` meta tool, `TOP_LEVEL_TOOLS` with 13 tools for top-level agents, `SUB_AGENT_TOOLS` with 10 tools for sub-agents), plus `TOOL_CALL_REGISTRY` with the dynamic tools dispatched via the `tool_call` meta tool (see [Conversation Loop and Tool Integration -- Tool Declarations](gemini-api.md#tool-declarations) for the roster).

The package manages in-memory chat sessions keyed by `(user_id, conversation_id)` in `chat/gemini_api/session.py`, and runs a streaming conversation loop in `chat/gemini_api/conversation.py` with UUID-based tool ID generation (`uuid.uuid4()`) for global uniqueness across turns that dispatches tool calls through a shared `_dispatch_tool_call()` function in `chat/gemini_api/tool_dispatch.py`. This function handles:

- the `tool_call` meta tool (validates `tool_name` against `TOOL_CALL_REGISTRY`, delegates to the appropriate handler)
- remaining direct local tools (`load_gmail_attachment`, `run_script`, `run_python`, `list_skills`, `search_skills`, `load_skills`) via handlers in the `chat/gemini_api/tool_handlers/` package (one module per tool domain)
- HTTP tools, by falling back to the route dispatch pipeline (`curl_proxy_get`, `curl_proxy_post`), which enforces a blocklist (`_BLOCKED_PROXY_PATHS` in `chat/route_dispatch.py`) preventing access to internal-only endpoints like `/api/authed-get`

Backward compatibility dispatch paths for all converted tools are retained for cached sessions. Agent-specific tools (`agent_task`, `agent_task_parallel`, `create_action_request`, and the other loop-handled arms) are dispatched through the per-tool handler registry `TURN_TOOL_HANDLERS` in `chat/gemini_api/turn_tools.py` (`agent_task_response` is handled in the sub-agent loop).

The `wait_for_handles` tool is in `TOOL_CALL_REGISTRY` but the top-level conversation loop routes its `tool_call` invocation to the registry handler in `turn_tools.py` before normal dispatch to block on outstanding wait-handle rows; see [Wait Handles Architecture](wait-handles.md). Both the main loop and sub-agent loop use `_dispatch_tool_call()` for shared tools, eliminating duplicated dispatch logic.

System prompts (built in `chat/gemini_api/system_prompt.py`) include a user identity line ("You are an AI agent helping {name} ({email}).") and are conditionally built based on the user's connected services via `get_user_connected_services()` from `api.instructions` (auth dependencies imported from `auth/` submodule) -- only documentation and examples for connected services are included.

The main conversation loop has no turn limit (it runs until the model finishes responding; `turn_count` is tracked and included in the `stats` event for observability). Sub-agents (implemented in `chat/gemini_api/sub_agent.py`) have model-specific turn limits determined by `get_sub_agent_turn_limits(model)` in `chat/gemini_api/constants.py`:

- 200K-token models (Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7) get 20 turns max with warning at 15, while 1M-token models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5) and unknown models get the default 60/50
- the turn warning is injected via `provider.inject_turn_warning()` to prompt graceful wind-down before the hard limit, and a context window warning is injected at `SUB_AGENT_CONTEXT_WARNING_THRESHOLD = 0.75` (75% of the model's `max_input_tokens`) via the same mechanism

Top-level agents have access to:

- `agent_task` for spawning a single sub-agent (with an optional `model` parameter to select the sub-agent's model, which can be from any provider; tool description directs the model to prefer `agent_task_parallel` for multiple independent tasks)
- `agent_task_parallel` for spawning multiple sub-agents concurrently (up to `MAX_PARALLEL_TASKS = 10`, with per-task error isolation)
- `create_action_request` for proposing write operations (including memory writes via `request_type="create_memory"`) with user approval

Sequential `agent_task` calls within a single model response are capped at `MAX_AGENT_TASKS_PER_TURN = 10` (returns error directing use of `agent_task_parallel`); sub-agents have `agent_task_response` for returning results. All limit constants are defined in `chat/gemini_api/constants.py`. Sub-agent spawn/completion, internal tool calls, and termination are all logged at INFO level with structured prefixes (`[agent_task]`, `[agent_task_parallel]`, `[sub-agent:<name>]`) and include `user=<email>` for per-user filtering.

The workspace file tools allow the model to list, read, write, and edit files in the conversation workspace; binary/large files are uploaded via the provider's `upload_file()` method for native analysis (Gemini uses the File Upload API; Anthropic base64-encodes supported images and PDFs inline).

`write_workspace_file` creates/overwrites text files with path traversal protection and a 1MB size limit; `edit_workspace_file` performs exact string replacement on existing text files with the same protections plus a read-before-edit gate tracked in a per-conversation `workspace_reads.json` sidecar (see [Conversation Loop and Tool Integration](gemini-api.md)).

The memory tools (`memory_search`, `memory_list`) give agents read-only access to the user's saved memories via `db/memory_store.py`. Memory writes go through `create_action_request(request_type="create_memory", ...)`, which inserts an `action_requests` row plus a linked `kind="action_request"` wait-handle row and blocks the agent loop via `SuspendForActionRequest` until the user clicks Approve / Revise / Deny on the inline card.

On Approve, `CreateMemoryHandler` calls `db.memory_store.create_memory` and returns the new `memory_id` on `result`. The DB rows are the source of truth, so the suspend/resume survives server restarts -- see [Wait Handles Architecture](wait-handles.md) and [Action Requests](action-requests.md).

SDK history persistence is handled by `chat/gemini_api/history.py`. The `chat/gemini_api/__init__.py` re-exports `run_conversation_turn`, `remove_chat_session`, `invalidate_user_sessions`, and `cancel_pending_wait_handles_for_conversation`.

All application logging uses a unified configuration defined in `chat/logging_config.py`, and all user-driven log entries follow the `user=<email>` convention for consistent user identification across modules. See [Logging Architecture](logging.md) for details and [Gemini API Integration](gemini-api.md) for the Gemini-specific logging.

**Chat Route Wiring** (`chat/realtime/` package) -- Chat traffic rides on the persistent multiplexed WebSocket `WS /app/api/stream` (`chat/realtime/socket.py`). The `_handle_send_message` op runs the model via `_run_send_message`, which calls `run_conversation_turn()` from `chat/gemini_api/`. The `{op:"stop"}` handler cancels the in-flight task and unconditionally calls `cancel_pending_wait_handles_for_conversation()` so a sentinel-suspended run cannot leave wait-handle rows pending.

Disconnect resilience: `_run_send_message` continues to completion if the client disconnects -- only an explicit `stop` op cancels. Partial message recovery: the shared `messages_out` list is also flushed on the suspend sentinels (`SuspendForWaitHandles` / `SuspendForSlackReply` / `SuspendForActionRequest`) and on `CancelledError`. On cancellation, `remove_chat_session()` discards the interrupted SDK session and `_save_interrupted_sdk_history()` performs the on-disk tail edit.

The legacy per-turn endpoint `chat/routes/websocket.py` was removed in devplan 00062; clients resolve wait handles via REST (`POST /app/api/wait-handles/{id}/resolve`, `POST /app/api/action-requests/{id}/resolve`). See [Realtime Architecture](realtime.md), [Wait Handles Architecture](wait-handles.md), and [Gemini API Integration](gemini-api.md).

This eliminates the Docker overhead, removes the data exfiltration vector (no shell access, no arbitrary network calls), and preserves the URL-based mental model.

**Design Decision: Embedded API Documentation**

The system prompt embeds API documentation directly rather than pointing to an endpoint. This saves the model one tool call round trip at the start of each conversation. The `get_instructions_content()` function in `api/instructions.py` serves as the single source of truth for API documentation, used by:
1. The `/api/instructions` endpoint (for manual API key users -- shows all services regardless of connection status)
2. The `get_system_prompt()` and `get_sub_agent_system_prompt()` functions in `chat/gemini_api/system_prompt.py` (for SDK system prompt -- filtered by connected services, includes user identity line)

**Design Decision: Conditional System Instructions**

System instructions sent to the LLM are conditionally built based on which services the user has connected. Instead of including all API endpoint documentation in every system prompt, only the documentation for connected services is included. This reduces context window usage and prevents the model from attempting to use APIs the user has not authorized.

The implementation uses a composable architecture:
- `get_user_connected_services(user)` in `api/instructions.py` extracts connection status from the user dict, returning a dict with boolean keys: `google_services`, `airtable`, `ramp`, plus one key per loaded plugin (e.g. `slack`, `telegram`, `github`, `twitter`)
- Each API submodule (e.g., `api/gmail/`, `api/drive.py`, `api/airtable.py`) defines a `get_instructions()` function containing the system prompt documentation for that service; `get_instructions_content()` in `api/instructions.py` aggregates them
- When `connected_services` is `None` (the default), all services are included for backward compatibility (used by the `/api/instructions` endpoint)
- The tool usage examples in the system prompt are also filtered by connection status via `_build_service_examples()` in `chat/gemini_api/system_prompt.py`
- Session invalidation is called in all OAuth callbacks and token save/remove endpoints in the `auth/` submodule (`auth/google_services.py`, `plugins/slack/oauth.py`, `plugins/telegram/auth.py`, `auth/service_key.py`, `auth/airtable.py`) so that system prompts refresh immediately when a user connects or disconnects a service

**Script Runner (Podman)**:

The `run_script` and `run_python` tools execute scripts in ephemeral Podman containers. `run_script` runs workspace files; `run_python` runs inline Python code piped via stdin (`python3 -u -`), avoiding throwaway `.py` files for one-off tasks. Both tools share the same container, networking, mounts, and security. The container image is defined in `Dockerfile.script-runner` and auto-built at startup by `run.py`. See [Script Runner Architecture](script-runner.md) for the pre-installed tool list, container environment, security model, and networking details.

## Data Flow

### Creating a New Conversation

```
1. User clicks "New Chat" (inline button at top of Conversations list in sidebar)
2. Frontend → POST /app/api/conversations (cookie sent automatically)
3. Backend authenticates user via session cookie (or API key)
4. Backend creates conversation ID (UUID)
5. Backend inserts a row in the conversations table via db/conversation_store.py
   (stores user_id, conversation_id, created_at, last_message_at)
6. Backend creates directory: data/chats/{id}/
7. Backend initializes chat_history.json
   (no workspace directory yet -- the SDK manages sessions in memory and the
   workspace/ subdirectory is created lazily by the workspace file tools)
8. Backend returns conversation ID and timestamp
9. Frontend navigates to new chat view
```

### Sending a Message

```
1. User types message in ChatPanel textarea and presses Enter
2. ChatPanel calls useConversation.sendMessage(text)
3. useConversation adds user message optimistically to conversationStore
4. WebSocketManager opens WebSocket: /app/api/conversations/{id}/message (cookie sent automatically)
5. Backend validates session cookie (or API key query param) and conversation ownership
6. WebSocketManager sends JSON: {"message": "user's text", "timezone": "America/Los_Angeles", "model": "gemini-3.1-pro-preview", "guide_id": "uuid"}
   - Timezone captured from browser via Intl.DateTimeFormat().resolvedOptions().timeZone
   - Model selected by user via dropdown in ChatPanel (persisted server-side on conversation record; hydrated from server on load; filtered to same-provider models after first message)
   - Guide ID selected by user via dropdown in ChatPanel (persisted per-conversation in localStorage; omitted for old conversations without guides)
7. Backend saves user message to chat_history.json
8. Backend runs the turn:
   9a. Backend calls _handle_send_message() in chat/realtime/socket.py
   9b. _run_send_message creates a cancellable asyncio task running run_conversation_turn(); the task is registered in the per-conversation send lock _active_send_runs
   9c. run_conversation_turn() resolves the provider via chat/llm/config.py (model registry)
       and gets or creates an in-memory session via the LLMProvider interface
   9d. Model response is streamed via provider.send_message_stream(); the on_event hook fires the durable flush callback (which advances seq, writes chat_history.json, publishes message_appended) and mirrors transient text_delta / sub_agent_* / tool_started events to the conversation channel
   9e. Tool calls are dispatched through route_dispatch.execute_tool_call()

10. Backend forwards events to the persistent WebSocket on the per-conversation channel:
    - Transient: text_delta, tool_started, sub_agent_tool_use / sub_agent_tool_result / sub_agent_finished, conversation_updated
    - Durable (via flush callback): message_appended {seq} -- client REST-fetches the new tail
    - Run lifecycle: send_message_finished {interrupted}, stop_acknowledged, send_message_rejected
    - Per-user globals: file_list_changed {conversation_id, project_id, scope}, conversation_list_changed, request_count_changed, wait_handle_resolved
    - Tool / approval payloads (tool_use, tool_result, action_request, stats) are persisted to chat_history.json by the flush callback and surface in the next message_appended round trip
11. WebSocketManager receives messages and updates conversationStore:
    - Text chunks accumulated in partialResponse
    - Tool use/result messages added to streamingMessages
    - (Messages route to correct conversation even if user switches conversations)
12. Backend saves all structured messages (text, tool_use, tool_result, action_request) to chat_history.json
13. On stream complete, WebSocketManager transfers streamingMessages to messages in store
14. WebSocketManager notifies stream complete → the Sidebar's list hooks (`useTopLevelConversations` / `useProjectConversations`) refresh their lists
15. WebSocket closes with code 1000 (normal closure)
```

### Tool Use Message Flow

When the LLM uses tools during response generation, the system handles them specially:

```
1. Gemini decides to use a tool (e.g., read a file, make an API call, spawn a sub-agent)
2. Backend receives tool_use event from Gemini CLI stream
3. Backend saves any accumulated text as a separate message
4. Backend sends structured tool_use message to frontend:
   {"type": "tool_use", "tool_name": "...", "tool_input": {...}, "tool_id": "...", "intent_message": "..."}
5. Frontend renders ToolUseMessage component with "running..." status
   - For regular tools: displays intent_message (description) as primary label, tool name as secondary label
   - Falls back to tool name as primary label if no intent_message is available
   - For agent_task: displays sub-agent name as title, description as intent, person icon
   - For agent_task_parallel: displays "{N} parallel sub-agent(s)" as title, sub-agent names as intent, person icon
6. Backend executes the tool:
   - For agent_task: spawns sub-agent via _run_sub_agent() in chat/gemini_api/sub_agent.py
     (sub-agent runs silently -- its intermediate tool calls are not streamed)
   - For agent_task_parallel: spawns all sub-agents concurrently via _run_parallel_sub_agents()
     in chat/gemini_api/sub_agent.py (all run silently; individual failures are isolated)
   - For other tools: executes via route dispatch or local handler
7. Backend receives tool_result event
8. Backend sends tool_result to frontend:
   {"type": "tool_result", "tool_id": "...", "tool_output": "..."}
9. Frontend updates ToolUseMessage to show "completed" status
10. User can expand the component to see full input/output
11. Process repeats if Gemini uses more tools
12. Final text response is displayed after all tool uses complete
```

### File Browser Operations

```
1. User selects a conversation
2. FileBrowser component mounts with conversationId
3. useFileBrowser hook loads file list: GET /app/api/conversations/{id}/files?path=/
4. Backend validates path stays within workspace directory
5. Backend reads workspace directory and returns file metadata
6. FileBrowser displays files with icons, sizes, and dates

File Upload (flat):
1. User drops file(s) or clicks upload button
2. Frontend sends POST /app/api/conversations/{id}/files/upload?path=/current/path
3. Backend validates path and saves file to workspace
4. useFileBrowser hook refreshes file list

Folder Upload (drag-and-drop):
1. User drops folder on FileBrowser
2. extractFilesFromDataTransfer() (directoryTraversal.ts) recursively traverses via webkitGetAsEntry()
3. .DS_Store files filtered out; FileWithPath[] returned with relative paths
4. Frontend sends POST .../files/upload with files + paths form data arrays
5. Backend uses save_uploaded_file_with_path() to recreate directory structure
6. If a file exists where a directory needs to be created, _check_dir_conflicts() raises clear error
7. Response contains uploadedFiles + errors; partial successes are kept
8. useFileBrowser hook refreshes file list

Folder Download:
1. User clicks meatball menu on a folder, selects "Download as Zip"
2. Frontend sends GET /app/api/conversations/{id}/files/download-folder?path=/folder
3. Backend creates temporary zip via create_folder_zip() in file_storage.py
4. Returns zip as FileResponse; BackgroundTask cleans up temp file
5. Browser downloads {folder-name}.zip

Viewing Files:
1. User clicks a viewable file (text, image, or PDF) in the file list
2. FileViewerModal opens -- text files fetched via content endpoint, images and PDFs fetched via download endpoint
3. Text rendered in scrollable <pre>; images rendered with checkerboard background, responsive scaling, and dimension/size metadata in title bar; PDFs rendered by PdfViewer (pdf.js, thumbnail rail + lazy fit-to-width pages, 100 MB preview cap)

Auto-Refresh on Workspace Mutation:
1. Backend tool handler (e.g., write_workspace_file) or mutating REST route
   (upload_files, delete_file, create_folder) completes successfully
2. Backend publishes file_list_changed via bus.publish_to_user, carrying
   conversation_id, project_id, and scope ("project" or "conversation")
3. PersistentWebSocket forwards the per-user global to onGlobalEvent listeners
4. FileBrowser filters by active conversation (scope="conversation") or
   active project (scope="project"), debounces 200ms, then calls silentRefresh()
5. useFileBrowser hook fetches updated file list (stale-while-revalidate)
```

### Stopping/Interrupting a Response

Users can stop a streaming response by clicking the Stop button:

```
1. User clicks Stop button (red button that replaces Send during streaming)
2. ChatPanel.tsx calls WebSocketManager.stopStreaming(conversationId)
3. WebSocketManager sends {"type": "stop"} via WebSocket
4. Backend receives the stop signal
   5a. Backend cancels the asyncio task running run_conversation_turn()
   5b. On CancelledError, remove_chat_session() discards the interrupted SDK session
       (the session's history is now inconsistent; next message starts fresh)
   5c. Partial messages are recovered from the shared messages_out list

6. Backend appends an "interrupted" marker to the structured messages
7. Backend saves all collected messages to chat_history.json
8. WebSocket closes with code 1000
9. Frontend displays orange indicator showing "Response interrupted by user"
10. Message component renders interrupted message with distinct styling
```

**Key Implementation Files**:
- Frontend stop button: `frontend/src/components/ChatPanel.tsx`, `ChatPanel.css`
- Stop signal forwarded to persistent WS: `frontend/src/services/WebSocketManager.ts` -> `frontend/src/services/PersistentWebSocket.ts`
- Backend stop handling: `chat/realtime/socket.py` (`_handle_stop`, `_run_send_message` `CancelledError` arm)
- Task cancellation and session cleanup: `chat/gemini_api/session.py` (`remove_chat_session()`)
- Interrupted message type: `frontend/src/api/types.ts`
- Interrupted message display: `frontend/src/components/Message.tsx`, `Message.css`

**Design Decision: Regular Disconnects vs Stop**

Regular WebSocket disconnects (browser refresh, network issues, navigation) do NOT terminate the in-progress work. Only explicit stop requests terminate it. This allows:
- Users to disconnect and reconnect without losing in-progress work
- Background processing to continue if user navigates away
- Partial responses to be recovered on reconnection

Disconnect resilience is implemented in `_run_send_message` (`chat/realtime/socket.py`): the run task is decoupled from any single connection, so a closed socket does not interrupt it. Publish failures to the bus are silently swallowed; the durable flush callback writes the message to disk regardless, and the next subscribe / catchup recovers via REST. All messages are persisted via the shared `messages_out` list.

### Loading Chat History

```
1. User clicks on a conversation in the sidebar (either in the main Conversations list or within a project drill-down)
2. Sidebar calls onConversationSelect → App.tsx navigates to /chats/<id> or /projects/<pid>/<id> → URL params synced to context
3. ChatPanel component mounts/updates with new conversationId
4. ChatPanel calls fetchConversation(conversationId)
5. Frontend → GET /app/api/conversations/{id} (cookie sent automatically)
6. Backend validates session cookie (or API key) and ownership
7. Backend reads data/chats/{id}/chat_history.json
8. Backend returns full conversation with all messages
9. ChatPanel renders message history with user/assistant styling
10. ChatPanel auto-scrolls to bottom (instant)
```

### Searching Conversations

```
1. User presses Cmd/Ctrl+K or clicks Search in sidebar
2. SearchModal opens (`frontend/src/components/SearchModal.tsx`)
3. User types query → debounced input triggers searchConversations(query) in `frontend/src/api/client.ts`
4. Frontend → GET /app/api/search?q={query} (cookie sent automatically)
5. Backend validates session, calls ChatStorage.search_conversations() in `chat/storage.py`
6. search_conversations() scans chat_history.json files with case-insensitive matching
7. Backend returns highlighted snippets (max 3 per conversation, 50 total)
8. SearchModal displays results with keyboard navigation
9. User clicks/selects a result → navigates to conversation URL and sets scrollToMessageIndex in NavigationContext
10. ChatPanel loads conversation and scrolls to the target message with highlight animation
```

## Authentication Flow

The chat app uses an inline sign-in screen on `/` instead of redirecting to a separate `/auth/` page:

### Google OAuth Login (All Environments)

```
1. User visits / in browser
2. If not authenticated: inline SignInScreen component is displayed
3. SignInScreen fetches Google OAuth URL from GET /auth/login-url
4. User clicks "Sign in with Google"
5. Google OAuth flow (authorize → callback → exchange code for tokens)
6. Quest validates the email against the allowed login domain
7. Quest generates API key and stores user in the database via `db/user_store.py` (including name from Google user info)
8. Quest sets session cookie (name determined by `COOKIE_NAME` in `auth/config.py`, environment-dependent; see [Auth Submodule](auth.md)) with signed, versioned payload (`{"v": COOKIE_VERSION, "uid": user_id}`)
9. OAuth callback redirects to /
10. Frontend detects valid session and renders the chat interface
```

### Dev Email Login (QUEST_ENV=dev Only)

```
1. User visits / in browser
2. AppConfigContext fetches GET /app/api/config, detects quest_env === "dev", sets isDevMode = true
3. SignInScreen shows dev login section: email input + "Dev Login" button below the Google sign-in button
4. User enters any email address and clicks "Dev Login"
5. Frontend POSTs {"email": "..."} to /auth/dev-login
6. Backend validates email, looks up or creates user with deterministic name from canned lists
7. If user has existing google_oauth credentials → 403 (must use Google sign-in)
8. Backend sets session cookie (same format as Google OAuth: {"v": COOKIE_VERSION, "uid": user_id})
9. Frontend reloads the page
10. Session check succeeds, chat interface loads
```

The domain check (`check_user_allowed()` in `chat/auth.py`) is relaxed in dev mode to allow any email domain, not just the allowed login domain.

### Cookie-Based Frontend Auth

When the user visits the chat app after logging in, the frontend authenticates directly via the session cookie:

```
1. User visits / (session cookie already set from OAuth login)
2. AppConfigContext fetches GET /app/api/config to determine appName ("DevQuest" or "Quest")
3. App.tsx sets document.title to appName; frontend shows loading state with appName heading
4. AuthContext calls checkSession() which hits GET /app/api/me with cookie
4. Backend validates session cookie (named `COOKIE_NAME` from `auth/config.py`, environment-dependent) via `get_user_from_cookie()` in `auth/session.py` (strict type checks on `uid` and `v` fields: must be int, not bool, and uid must be positive), returns { email, name, google_services_connected, has_any_service_connected }
5. Frontend sets isAuthenticated = true, stores userEmail, userName, googleServicesConnected, and hasAnyServiceConnected
6. If has_any_service_connected is false (no services connected at all), settings auto-opens to Data Connections section
7. Frontend renders chat interface with Sidebar (Projects section + Conversations list), ChatPanel, and FileBrowser
8. All subsequent API calls use credentials: 'include' (cookie sent automatically)
9. WebSocket connects without ?api_key= (cookie sent automatically by browser)
```

If the session check fails (not logged in, expired session, etc.):
- Frontend displays the inline `SignInScreen` component

### User Profile & Settings

```
User Info Flow:
1. AuthContext calls checkSession() on mount
2. checkSession() calls GET /app/api/me with credentials: 'include' (cookie auth)
3. Backend returns { email, name, google_services_connected, has_any_service_connected } from the database
4. AuthContext stores userEmail, userName, googleServicesConnected, hasAnyServiceConnected, sets isAuthenticated = true
5. If hasAnyServiceConnected is false (no services connected at all), settings auto-opens to Data Connections
6. Sidebar renders UserInfoBar at bottom with name, email, avatar initial, and gear icon

Settings Flow (Custom Instructions):
1. User clicks gear icon in UserInfoBar
2. App.tsx sets isSettingsOpen (NavigationContext) = true
3. SettingsModal opens (rendered via createPortal to document.body)
4. Modal loads current settings: GET /app/api/settings
5. User edits custom system prompt and clicks Save
6. Frontend calls PUT /app/api/settings with { custom_system_prompt: "..." }
7. Backend updates user's settings in the database via `db/user_store.py`
8. Backend calls invalidate_user_sessions(user_id) in chat/gemini_api/session.py
   to discard all cached SDK sessions for the user
9. Next message creates a fresh session with the updated system prompt

Settings Flow (Data Connections):
1. User clicks "Data Connections" in the Settings nav sidebar
2. DataConnectionsSection fetches connector status: GET /app/api/connectors (cookie auth)
3. Backend returns connection status for all connectors (Google Services, Slack, GitHub,
   Telegram, Airtable, plugin rows) with connect/reconnect URLs; api_key rows include `key_hint`
   (last 4 characters of the stored key) when connected
4. OAuth connectors (Google Services, Slack, GitHub, Telegram) show Connect/Reconnect button
   and optional "needs new scopes" badge
5. API key connectors (Airtable, api_key-kind plugin rows) use a shared ApiKeyForm component: text field to paste
   key when disconnected, "Connected" badge + key preview (...XXXX) + Disconnect button when connected
6. Clicking an OAuth connector button opens the OAuth flow in a popup window (via openOAuthPopup()
   from frontend/src/utils/oauthPopup.ts) with ?popup=1 appended to the auth URL
7. OAuth flow completes in the popup; backend callback returns an HTML page that
   calls window.opener.postMessage() and closes the popup (see generate_oauth_popup_success_page()
   in auth/popup_helpers.py)
8. DataConnectionsSection listens for postMessage events and re-fetches connector status on completion
9. AuthContext.refreshConnectionStatus() is called to update app-level connection state
10. Fallback: if browser blocks the popup, an inline direct link is shown
11. This section auto-opens when authenticated but no services are connected (truly new users only)
See [OAuth Popup Flow Architecture](oauth-popup.md) for full details

Settings Flow (Sign Out):
1. User clicks "Sign Out" in the Settings nav sidebar
2. Three options are presented:
   - Logout: POST /app/api/logout (preserves conversations + OAuth tokens)
   - Logout & Disconnect: POST /app/api/logout-and-disconnect (preserves conversations,
     revokes OAuth tokens)
   - Delete Account: POST /app/api/delete-account (removes all account data)
3. Each action clears the session cookie and returns the user to the sign-in screen
```

**User Identity in System Prompts**:

Both `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py` accept optional `user_name` and `user_email` parameters. When `user_email` is present, an identity line is injected after the role description so the LLM knows who it is assisting. The identity line format is:
- With name and email: "You are an AI agent helping {name} ({email})."
- With email only: "You are an AI agent helping {email}."
- No email: no identity line added

User data comes from the `user` dict at call sites (`user.get("name", "")` and `user["email"]`; the user's integer `id` is used for storage paths and session keys).

**Custom System Prompt Injection (Guides)**:

Custom system instructions are injected via the Guides system. Each user has named guides (system prompt presets) stored in the `guides` database table. A default guide always exists (migrated from the old `custom_system_prompt` setting). See [Guides Architecture](guides.md).

- `run_conversation_turn()` resolves the guide content for the conversation (from a snapshot if the conversation already has one, or from the selected/default guide on first message) and passes it as `custom_system_prompt` to `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py`. It appears as a "User's Custom Instructions" section in the system prompt. On first message, the guide content is snapshotted into `chat_history.json` via `ChatStorage.set_guide_snapshot()`.
- **Backward compatibility**: Updating the custom system prompt via `PUT /app/api/settings` also updates the default guide's content (in `chat/routes/user.py`), keeping the old settings path in sync with the new guide system.

**Auto-loaded Skills Injection**:

Skills can be auto-loaded at two levels: user-level (applies to all conversations) and project-level (applies to all conversations in a project). User-level auto-loaded skill IDs are stored in the `user_skill_autoloads` junction table; project-level auto-loaded skill IDs are stored in the `project_skill_autoloads` junction table. Users can auto-load any skill they have access to, including shared and public skills.

On each message, the backend resolves both user and project auto-loaded skills, merges them with deduplication by skill ID, and injects them as an "Enabled Skills" section in the system prompt. Skills are NOT snapshotted (unlike guides) -- each message uses current auto-loaded skills and current content, so edits to a skill are immediately reflected.

Inaccessible skills are automatically excluded. Sub-agents receive the same pre-resolved skills content as the parent. See [Skill Library Architecture](skill-library.md) for details.

- **System prompt ordering**: Identity line, Custom Instructions (guide), Enabled Skills, Project-Specific Instructions, Tool documentation
- `run_conversation_turn()` in `chat/gemini_api/conversation.py` calls `get_user_autoloaded_skills()` and (for project conversations) `get_project_autoloaded_skills()`, merges with deduplication, and passes `skills_content` to `get_system_prompt()`, `_run_sub_agent()`, and `_run_parallel_sub_agents()`
- **Key files**: `db/skill_store.py` (`get_user_autoloaded_skills()`, `get_project_autoloaded_skills()`), `chat/gemini_api/system_prompt.py` (`skills_content` parameter), `chat/gemini_api/sub_agent.py` (`skills_content` passthrough)

**Conversation Skill Loader**:

Users can also load skills into a specific conversation via the "+ Skill" button next to the guide selector in `ChatPanel.tsx`. This opens a `SkillSelectorModal` where users can browse all accessible skills, filter by keyword, and multi-select skills to load. Already-loaded and auto-loaded skills appear greyed out with badges. Selected skills are sent as `skill_ids` in the WebSocket message payload.

On the backend, `run_conversation_turn()` resolves the skill contents via `get_accessible_skills_by_ids()` from `db/skill_store.py` and injects them as a `<conversation_skills_loaded>` XML section in the user message envelope (via `_wrap_message_with_metadata()` in `chat/gemini_api/conversation.py`). The system prompt instructs the LLM to treat these as persistent instructions for the entire conversation.

Loaded skill IDs are persisted to `data/chats/{id}/loaded_skills.json` via `ChatStorage.add_loaded_skill_ids()` in `chat/storage.py`, and retrieved via `GET /app/api/conversations/{id}/loaded-skills` for hydration on page reload. See [Skill Library Architecture](skill-library.md) for details.

- **Frontend files**: `frontend/src/components/SkillSelectorModal.tsx` (modal), `frontend/src/components/ChatPanel.tsx` ("+ Skill" button), `frontend/src/contexts/ConversationSkillsContext.tsx` (queued/loaded skill state), `frontend/src/hooks/useConversation.ts` (hydration from server)
- **Backend files**: `chat/routes/conversations.py` (`GET /conversations/{id}/loaded-skills`), `chat/realtime/socket.py` (`skill_ids` extraction from the persistent-WS `send_message` payload), `chat/storage.py` (loaded skills file persistence), `chat/gemini_api/conversation.py` (skill resolution, `_wrap_message_with_metadata()` injection, `ChatStorage.add_loaded_skill_ids()`), `chat/gemini_api/system_prompt.py` (conversation skills guidance in system prompt)

**Conditional System Instructions**:

System instructions are filtered based on the user's connected services. Only documentation for services the user has actually connected is included in the system prompt:

- `run_conversation_turn()` calls `get_user_connected_services(user)` from `api.instructions` and passes the result to `get_system_prompt()` and `get_sub_agent_system_prompt()`, along with the user's name and email for identity injection. Both functions forward `connected_services` to `get_instructions_content()` in `api.instructions` for documentation filtering, and to `_build_service_examples()` in `chat/gemini_api/system_prompt.py` for example filtering. Auth dependencies are imported from the `auth/` submodule.
- **`/api/instructions` endpoint**: Always shows all services regardless of connection status (passes `connected_services=None`).
- **Service groups**: `google_services` (Gmail, Calendar, Drive, Docs, Sheets, Tasks), `airtable`, `ramp`, plus loaded plugins (e.g. `slack`, `telegram`, `github`, `twitter`).
- **Key files**:
  - `api/instructions.py` (`get_user_connected_services()`, `get_instructions_content()`)
  - per-service `get_instructions()` functions in each API submodule (`api/gmail/instructions.py`, `api/calendar.py` (instruction text only; reads use `authed_get`), `api/drive.py` (instruction text only; reads use `authed_get`, binary downloads use `download_drive_file`), `api/docs.py`, `api/sheets.py`, `api/tasks.py`, `api/airtable.py` (instruction text only; reads use `authed_get`))
  - `auth/` submodule (`auth/config.py` for scope constants, `auth/session.py` for auth dependencies, `auth/google_credentials.py` for credential management)
  - `chat/gemini_api/system_prompt.py` (`get_system_prompt()`, `get_sub_agent_system_prompt()`, `_build_service_examples()`).


**Session Invalidation**: `invalidate_user_sessions()` in `chat/gemini_api/session.py` removes all cached chat sessions for the user from `_active_chats`. This is called when settings change (custom system prompt update) and when the user connects or disconnects a service (all OAuth callbacks and plugin key routes: Google Services, Slack, Telegram, plugin service keys). This ensures the next message creates a fresh session with the updated system prompt that reflects the user's current service connections. Note that auto-loaded skills are resolved fresh on every message (not from the cached session), so session invalidation is not required for skill auto-load changes.

**Note**: In development, the frontend proxies auth requests to the backend, so the full OAuth flow works seamlessly. The `/auth/` page still exists for the OAuth flow but now redirects authenticated users to `/`. The sign-in UI is rendered inline on `/` via the `SignInScreen` component. Legacy `/chat*` URLs are 301-redirected to `/*` for backward compatibility.

## Security Considerations

1. **Domain Restriction**: Only emails on the allowed login domain allowed
   - Implemented in `chat/auth.py` as `check_user_allowed()`
   - Configurable for future domain changes

2. **Dual Authentication**: Frontend endpoints (`/app/api/*`) accept session cookie OR API key Bearer token
   - Browser frontend uses session cookie (name determined by `COOKIE_NAME` in `auth/config.py`: `quest_session` in prod, `quest_session_dev` in dev; no API key stored in browser)
   - Scripts and LLM agents use `Authorization: Bearer <api_key>` header
   - Upstream service endpoints (`/api/gmail-*`, `/api/docs-*`, etc.) require API key only
   - Keys are randomly generated (32+ bytes), stored in `data/quest.db` (indexed for fast lookup)
   - Keys can be reset via `POST /api/reset-api-key` (cookie auth; no Settings UI for this anymore). Preserves all other user data.
   - Users can manage connected services via the Settings panel ("Data Connections" section)
   - Users can log out or delete their account via the Settings panel ("Sign Out" section)

3. **Workspace Isolation**: Each conversation has isolated filesystem
   - Prevents cross-conversation data leakage
   - User-level directory segregation
   - Script-runner container volume mounts enforce boundaries

4. **Token Storage**: Google OAuth tokens stored as JSON columns in `data/quest.db`
   - Not exposed to frontend
   - Used for backend operations (Gmail read/draft creation, Calendar, Drive, Docs, Sheets, Tasks)
   - On token refresh, both the database and the in-memory `user` dict are updated so subsequent API calls within the same WebSocket session use the fresh token (see `auth/google_credentials.py`)

5. **Cookie Validation**: `get_user_from_cookie()` in `auth/session.py` applies strict type checks to prevent type-confusion attacks
   - Both `uid` and `v` (version) fields must be `int` but not `bool` (`isinstance(x, int) and not isinstance(x, bool)`)
   - This prevents Python's `bool` subclass of `int` from allowing `uid: true` to resolve to user ID 1
   - `uid` must be a positive integer (`uid > 0`)
   - Version must exactly match `COOKIE_VERSION`
   - Non-dict payloads and legacy formats are rejected

6. **Container Security**: Script-runner Podman containers run with minimal privileges
   - No persistent containers (--rm flag)
   - Rootless Podman with sandboxed networking (see [Script Runner](script-runner.md))

## Scalability Considerations

Current limitations (designed for internal use):

- **Single Server**: No horizontal scaling
- **SQLite**: Single-writer model; sufficient for current user base but would need PostgreSQL for high-concurrency writes
- **File-based Chat Storage**: Chat history in JSON files, not suitable for high volume

Future improvements for scale:
- Migrate SQLite to PostgreSQL (change `DATABASE_URL` and `ASYNC_DATABASE_URL` in `db/engine.py` and `alembic.ini`)
- Move chat history to PostgreSQL
- Implement connection pooling for Gemini (Slack proxy already uses a shared `httpx.AsyncClient` with connection pooling in `plugins/slack/upstream.py`)
- Add message queue for async processing
- Add Redis for session management

## Development vs Production

### Development Mode
- Frontend built to static assets and served by FastAPI (port 9000 via `run.py --dev`)
- Backend runs on Uvicorn with --reload
- `QUEST_ENV=dev` set by `run.py --dev`, resulting in cookie name `quest_session_dev` and "DevQuest" UI branding (page title, headings)
- Logging configured via `--log-config logging_config.json` in `run.py`; `chat`, `auth`, and `quest` loggers set to DEBUG level
- Source maps enabled

### Production Mode
- Frontend builds to static assets (`frontend/dist/`)
- Backend serves static files at `/` path
- Single port deployment (8000)
- `QUEST_ENV=prod` set by `run.py --prod` (or unset), resulting in cookie name `quest_session` and "Quest" UI branding
- Logging configured via `--log-config logging_config.json` in `run.py`
- Minified and optimized bundles
- Source maps enabled for debugging

**Static File Serving Architecture**:
1. **Asset Files** (JS, CSS): Served from `/assets/` via FastAPI StaticFiles mount
   - Mount location: `frontend/dist/assets/`
   - Vite generates hashed filenames for cache busting (e.g., `index-BXQtnFIp.js`)
   - Mount is placed BEFORE route handlers to prevent conflicts

2. **HTML Entry Point**: Served at `/` root route
   - File: `frontend/dist/index.html`
   - Requires authentication via session cookie (`COOKIE_NAME` from `auth/config.py`)
   - Unauthenticated users see the inline `SignInScreen` component (the frontend handles auth state)

3. **SPA Catch-All Routes**: `/chats/{rest:path}`, `/projects/{rest:path}`, and `/admin/{rest:path}` serve `index.html`
   - Enables deep linking for URL-based routing (e.g., `/chats/<uuid>`, `/projects/<pid>/<uuid>`, `/admin/system-monitor`)
   - React Router handles client-side route matching after the HTML loads
   - Registered before the `/{filename}` fallback to prevent static file matching

4. **Root Static Files** (e.g., vite.svg): Served via `/{filename}` route
   - Catches files in `frontend/dist/` root
   - No authentication required for static assets
   - Returns 404 if file doesn't exist

5. **Backward Compatibility**: `/chat*` URLs are 301-redirected to `/*`

**URL Routing Priority** (from highest to lowest):
1. `/assets/*` - Static file mount (highest priority)
2. `/app/api/*` - API endpoints (chat routes)
3. `/` - HTML entry point with auth
4. `/chats/*`, `/projects/*`, `/admin/*` - SPA catch-all routes (serve `index.html` for deep links)
5. `/{filename}` - Root-level static files
6. `/chat*` - 301 redirect to `/*` (backward compatibility)

**Build Output Structure**:
```
frontend/dist/
├── index.html              # Entry point (served at /)
├── vite.svg                # Root static file (served at /vite.svg)
└── assets/                 # Hashed assets (served at /assets/*)
    ├── index-BXQtnFIp.js
    ├── index-BXQtnFIp.js.map
    └── index-D_IOohIK.css
```

**Deployment Process**:
```bash
# Option 1: Use startup script (recommended)
python3 run.py --prod   # Production mode (port 8000, QUEST_ENV=prod)
python3 run.py --dev    # Development mode (port 9000, QUEST_ENV=dev)
python3 run.py          # Defaults to --dev

# Option 2: Manual steps
cd frontend && npm install && npm run build && cd ..
uv sync
QUEST_ENV=prod uv run uvicorn quest:app --host 0.0.0.0 --port 8000 --log-config logging_config.json
```

The startup script (`run.py`, Python 3.8+ standard library only) automates:
1. Setting `QUEST_ENV` (`prod` or `dev`) for environment-based cookie names and UI branding
2. Frontend dependency installation and build (`npm install` + `npm run build`)
3. Backend dependency installation (`uv sync`)
4. Database migrations (`uv run alembic upgrade head`)
5. Script-runner Podman image validation
6. Server startup (port 8000 for `--prod`, port 9000 for `--dev`)

## Technology Choices Rationale

| Choice | Rationale |
|--------|-----------|
| FastAPI | Async support, WebSocket support, auto-generated docs, type hints |
| React | Component-based UI, large ecosystem, excellent dev tools |
| Vite | Fast builds, modern ESM-based architecture |
| TypeScript | Type safety, better IDE support, catches errors early |
| Podman | Rootless container isolation for the script runner, reproducible environments |
| SQLite + SQLAlchemy | Transactional user data, indexed lookups, no separate server, Alembic migrations |
| JSON storage (chats) | Simple per-conversation files, easy to inspect and debug |
| WebSocket | Real-time streaming, efficient for chat, native browser support |
| uv | Fast Python package management, lock files, reproducible builds |

