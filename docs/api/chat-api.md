# Chat API Documentation

This document describes the REST and WebSocket endpoints for Quest. The WebSocket endpoint is the persistent multiplexed `/app/api/stream` socket; see [Realtime Architecture](../architecture/realtime.md) for the protocol, lifecycle, and live-update fan-out.

## Authentication

### Dual Authentication (Cookie + API Key)

All frontend-facing endpoints (`/app/api/*`) support **dual authentication**: session cookie OR API key Bearer token. The backend tries cookie auth first, then falls back to API key auth.

- **Browser frontend**: Uses the session cookie automatically (set during OAuth login; cookie name is environment-dependent via `COOKIE_NAME` in `auth/config.py`). No API key is stored or transmitted by the frontend.
- **Scripts and LLM agents**: Continue using `Authorization: Bearer <api_key>` header as before.
- **Upstream service endpoints** (`/api/gmail-*`, `/api/docs-*`, `/api/sheets-*`, plus the sandbox bridges `/api/authed-get`, `/api/authed-post`, and `/api/tool-call`): API key auth only (not called by the browser frontend).
  - Note: Calendar and Drive reads use `authed_get` tool calls instead of proxy endpoints, and Slack is dedicated dynamic tools with no HTTP routes at all (reads and the `send_slack_dm_to_self` self-DM send alike; the old `/api/slack-simple/dm-self` route is retired -- scripts use the `/api/tool-call` bridge), as are the Telegram reads (`telegram_*` tools; the old `/api/telegram/*` routes are retired the same way).
  - Plugin service access is via plugin tools only -- plugins cannot mount proxy routes.

### Chat App HTML

The chat application is served at `/` regardless of authentication state. The React frontend checks the session via `GET /app/api/me`. Unauthenticated users see the `SignInScreen` component which fetches the OAuth URL from `GET /auth/login-url`.

### Authentication Methods

- **Session cookie (browser):** Cookie name from `COOKIE_NAME` in `auth/config.py`. All frontend `fetch()` calls use `credentials: 'include'`. WebSocket connections send the cookie automatically.
- **API key (scripts/LLM agents):** `Authorization: Bearer <api_key>` header. WebSocket also accepts `?api_key=` query parameter.
- **API key reset:** `POST /api/reset-api-key` in `quest.py` (`api_reset_api_key()`).
- **Service connections:** OAuth popup flows for Google Services, Slack, GitHub, Telegram; API key forms for Airtable and api_key-kind plugin connections. See [OAuth Popup Flow](../architecture/oauth-popup.md) and [Settings Data Connections](../architecture/settings-data-connections.md).

### Authentication Errors

Error codes and responses are defined in the auth dependencies in `auth/session.py` and `chat/auth.py`. Domain restriction logic is in `check_user_allowed()` (relaxed in dev mode) and is enforced by every one of those dependencies via `require_user_allowed()` (403 `access_denied`), not only by the `_checked` variant.

### Dev Login Endpoint

`POST /auth/dev-login` -- Only available when `QUEST_ENV=dev`. Creates or looks up a user by email and sets a session cookie. See `auth/dev_login.py` for parameters and error handling. See [Auth Submodule Architecture](../architecture/auth.md) for the full dev login flow.

### General API Error Responses

Upstream service endpoints (`/api/gmail-*`, `/api/docs-*`, etc.) return JSON error objects with `error` and `message` fields. Error generation is in `get_valid_service_credentials()` in `auth/google_credentials.py` and `get_current_user` in `auth/session.py`.

### Frontend Authentication Flow

The frontend authenticates via session cookie without storing any API key. On mount, `AuthContext.tsx` calls `GET /app/api/me` with `credentials: 'include'`. If unauthenticated, the inline `SignInScreen` component is shown. The flow is implemented in `frontend/src/utils/auth.ts` (`checkSession`), `frontend/src/contexts/AuthContext.tsx`, and `frontend/src/components/SignInScreen.tsx`.

## REST Endpoints

All conversation REST endpoints are defined in `chat/routes/conversations.py`. See that file and `db/conversation_store.py` for parameters, request/response shapes, and error codes. Types are in `frontend/src/api/types.ts`.

- **GET `/app/api/conversations`** -- List conversations for the authenticated user, sorted by most recent activity (`list_conversations_endpoint()`). With no query params it returns the full list (legacy behavior).
  - Supports `include_archived`, plus server-side sidebar filters: `exclude_projects=true` drops project-linked rows, `include_slack=false` / `include_inference=false` drop `origin="slack"` / `origin="inference_api"` rows (NULL-origin legacy rows always survive origin filters).
  - Optional paging via `limit` (1..200) + `cursor`: responses always carry `has_more` and `next_cursor` (an opaque keyset cursor encoding the last row's `(last_message_at, id)`; paging is keyset -- `(last_message_at, id) < cursor` under the `last_message_at DESC, id DESC` sort -- so newly prepended conversations can't shift page windows; malformed cursors 400 with `invalid_cursor`).
  - Each conversation object includes `project_id`, `routine_id`, `archived`, `custom_name`, and `model` fields. The `title` field resolves via `ChatStorage._resolve_list_title()`: `custom_name` if set, otherwise the cached `auto_title` column, otherwise a one-shot `chat_history.json` read for legacy rows (see [Database -- Conversation Model](../architecture/database.md#conversation-model)).
- **POST `/app/api/conversations`** -- Create a new conversation and workspace (`create_conversation_endpoint()`). Optionally accepts `routine_id` to link to a routine.
  - The response (`CreateConversationResponse` in `frontend/src/api/types.ts`) carries `id`, `created_at`, `last_message_seq` (always `0`), `origin` (`"web"` for this endpoint), `project_id` (always `null` here), and `model` (always `null` until the first message) so the FE can seed its conversation store and persistent-WS subscribe state without a follow-up `GET /conversations/{id}`.
  - The project-conversation create endpoint (`POST /app/api/projects/{project_id}/conversations`) returns the same shape with `project_id` set to the path argument.
- **POST `/app/api/conversations/{id}/duplicate-workspace`** -- Create a new empty standalone conversation seeded with a copy of the source conversation's workspace files (`duplicate_conversation_workspace()`).
  - Copies the `workspace/` subdir via `ChatStorage.copy_workspace_files()` (off-thread `shutil.copytree`; a project-source conversation copies the shared project workspace); conversation metadata sidecars (`chat_history.json`, `workspace_reads.json`, ...) are not copied, nor is the hidden workspace-root `.responses/` dir (authed_get response bodies tied to the source conversation's tool calls, meaningless without that context).
  - Returns the same seed-friendly shape as `POST /conversations`. Surfaced as the "Duplicate Workspace" item in the sidebar conversation meatball menu (`Sidebar.tsx`), which navigates to the new conversation.
- **GET `/app/api/conversations/{id}`** -- Get conversation with full message history (`get_conversation_endpoint()`). Response includes `last_message_seq` so the persistent-WS client can subscribe with a usable `last_seq` right after hydration. Also carries the sidebar title fields -- `title` (resolved via `ChatStorage._resolve_list_title()`), `custom_name`, and `archived` -- so the chat header title unit shows the same title as the sidebar row. See [Realtime Architecture](../architecture/realtime.md).
- **GET `/app/api/conversations/{id}/tail?after_seq=N`** -- Return only messages with `seq > after_seq` (`get_conversation_tail()`). Used by the persistent-WS client to fetch the new tail after a `message_appended` event without re-downloading the full conversation; also used as the `resync` fallback when the in-memory replay buffer can't fulfil a catchup. Response shape: `{messages: [...], last_message_seq: N}`.
- **PUT `/app/api/conversations/{id}/archive`** -- Soft-delete a conversation (`archive_conversation_endpoint()`). Delegates to `archive_conversation()` in `db/conversation_store.py`.
- **PUT `/app/api/conversations/{id}/unarchive`** -- Restore an archived conversation (`unarchive_conversation_endpoint()`). Delegates to `unarchive_conversation()` in `db/conversation_store.py`.
- **PUT `/app/api/conversations/{id}/rename`** -- Set or clear a custom name (`rename_conversation_endpoint()`). Max length: `MAX_CONVERSATION_NAME_LENGTH` in `db/conversation_store.py`. Empty strings normalized to NULL.
- **PATCH `/app/api/conversations/{id}/model`** -- Update the LLM model for a conversation (`update_conversation_model_endpoint()`). Uses `set_conversation_model()` in `db/conversation_store.py` (always overwrites).
- **GET `/app/api/conversations/{id}/loaded-skills`** -- Get manually loaded skill IDs (`get_conversation_loaded_skills()`). Reads from `data/chats/{id}/loaded_skills.json`.
- **GET `/app/api/conversations/{id}/system-prompt`** -- Get the saved system prompt (`get_conversation_system_prompt()`). Reads from `data/chats/{id}/system_prompt.txt`.
- **GET `/app/api/search`** -- Search across all conversation messages (`search_endpoint()`). File-based scanning via `ChatStorage.search_conversations()` in `chat/storage.py`.
- **POST `/app/api/conversations/{id}/composer-attachments`** -- Persist images pasted into the chat composer ahead of the `send_message` WS frame (`upload_composer_attachments()` in `chat/file_routes.py`). See [Composer Attachments](#composer-attachments).

### Conversation Creation Side Effects

Creating a conversation:
- Creates a row in `conversations` table via `db/conversation_store.py`
- Creates directory at `data/chats/{id}/` with `chat_history.json`
- No workspace directory is created up front; the SDK manages sessions in memory, and the `workspace/` subdirectory is created lazily by the workspace file tools when first needed

---

### Get Conversation

**Endpoint**: `GET /app/api/conversations/{conversation_id}` -- retrieves a conversation with its full message history. Implementation: `get_conversation_endpoint()` in `chat/routes/conversations.py`. Response shape: `ConversationDetail` in `frontend/src/api/types.ts`.

The response merges server-side metadata (`model`, `routine_id`, `project_id`, `origin`, `flags`) onto the dict returned by `ChatStorage.get_conversation()`. The `flags` array is the persisted per-conversation flag set (NULL == `[]`), surfaced so the composer can render its read-only "N flag(s) enabled" label after the first message and across reloads (see [Conversation Flags](../architecture/conversation-flags.md)).

For Slack-driven conversations (`origin === "slack"`) it additionally surfaces `slack_channel_id`, `slack_thread_ts` (looked up via `list_slack_conversations_for_user()`), and `slack_team_id` (from `user.slack_oauth.default_team_id`). These three identifiers let the web UI build the native Slack deep link described in [Slack Socket Mode](../architecture/slack-socket-mode.md).

The response also carries `pending_wait_handles` -- a list projected from `tool_wait_handle_store.list_pending_for_conversation` with one `{id, kind, correlation_id, created_at}` entry per still-`pending` row -- so a fresh load can lock the composer immediately when the suspended turn is waiting on an action request or `slack_reply`. See [Wait Handles -- Composer Sync](../architecture/wait-handles.md#composer-sync).

It additionally carries `expensive_resume` -- the verdict from `check_expensive_resume()` in `chat/expensive_resume.py` (`null` == no warning) -- so the FE can block the composer behind the expensive-resume warning card on load. See [Expensive-Resume Warning](../architecture/expensive-resume.md).

Returns `404 Not Found` with error code `conversation_not_found` when the conversation does not exist or does not belong to the authenticated user.

---

### Archive Conversation

Archive a conversation (soft-delete). Archived conversations are excluded from list results by default and hidden from the sidebar.

**Endpoint**: `PUT /app/api/conversations/{conversation_id}/archive`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Response**: `200 OK`

- Conversation metadata object with `id`, `user_id`, `project_id`, `routine_id`, `model`, `created_at`, `last_message_at`, `archived` (true)

**Side effects**:
- Sets `archived=True` on the conversation row in the `conversations` table via `archive_conversation()` in `db/conversation_store.py`

**Error Response**: `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `archive_conversation_endpoint()` in `chat/routes/conversations.py`. Delegates to `archive_conversation()` in `db/conversation_store.py`.

**Frontend Usage**: The meatball menu on each conversation entry in `Sidebar.tsx` calls `archiveConversation(conversationId)` in `frontend/src/api/client.ts`. The UI optimistically removes the conversation from the sidebar list.

---

### Unarchive Conversation

Restore a previously archived conversation.

**Endpoint**: `PUT /app/api/conversations/{conversation_id}/unarchive`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Response**: `200 OK`

- Conversation metadata object with `id`, `user_id`, `project_id`, `routine_id`, `model`, `created_at`, `last_message_at`, `archived` (false)

**Side effects**:
- Sets `archived=False` on the conversation row in the `conversations` table via `unarchive_conversation()` in `db/conversation_store.py`

**Error Response**: `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `unarchive_conversation_endpoint()` in `chat/routes/conversations.py`. Delegates to `unarchive_conversation()` in `db/conversation_store.py`.

**Frontend Usage**: The meatball menu on archived conversation entries in `Sidebar.tsx` shows "Unarchive" instead of "Archive". Calls `unarchiveConversation(conversationId)` in `frontend/src/api/client.ts`. The UI optimistically updates the conversation's archived state.

---

### Rename Conversation

Set or clear a custom name for a conversation. Custom names take priority over auto-generated titles (derived from the first user message).

**Endpoint**: `PUT /app/api/conversations/{conversation_id}/rename`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Request body** (JSON):
- `custom_name` (string or null): The custom name to assign. Max 100 characters (`MAX_CONVERSATION_NAME_LENGTH` in `db/conversation_store.py`). Whitespace is stripped. Pass an empty string or `null` to clear the custom name and revert to the auto-generated title.

**Response**: `200 OK`

- Conversation metadata object with `id`, `user_id`, `project_id`, `routine_id`, `model`, `custom_name`, `created_at`, `last_message_at`, `archived`

**Side effects**:
- Sets `custom_name` on the conversation row in the `conversations` table via `rename_conversation()` in `db/conversation_store.py`
- Empty strings and whitespace-only strings are normalized to NULL (reverts to auto-generated title)

**Error Responses**:
- `400 Bad Request` if name exceeds 100 characters (error code: `name_too_long`)
- `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `rename_conversation_endpoint()` in `chat/routes/conversations.py`. Delegates to `rename_conversation()` in `db/conversation_store.py`.

**Frontend Usage**: The meatball menu (three-dot icon) on each conversation entry in `Sidebar.tsx` includes a "Rename" option. Clicking it replaces the conversation title with an inline text input, pre-selected for immediate typing. Press Enter to save (calls `renameConversation(conversationId, customName)` in `frontend/src/api/client.ts`), Escape to cancel. The UI optimistically updates the conversation title. For routine sub-item conversations, the custom name displays with the timestamp in parentheses (e.g., "Custom Name (2:30 PM)").

---

### Update Conversation Model

Update the LLM model for a conversation. Used by the frontend to persist model changes when the user switches models via the model selector dropdown.

**Endpoint**: `PATCH /app/api/conversations/{conversation_id}/model`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Request body** (JSON):
- `model` (required, string): LLM model ID to set (e.g., `"claude-sonnet-4-6"`, `"gemini-3.5-flash-lite"`)

**Response**: `200 OK` with `{"ok": true}`

**Side effects**:
- Unconditionally sets the `model` column on the conversation row via `set_conversation_model()` in `db/conversation_store.py` (always overwrites, unlike `update_conversation_model()` which is no-op if already set)

**Error Response**: `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `update_conversation_model_endpoint()` in `chat/routes/conversations.py`. Delegates to `set_conversation_model()` in `db/conversation_store.py`.

**Frontend Usage**: The model selector dropdown in `ChatPanel.tsx` calls this endpoint when the user changes the model. The frontend hydrates the model selector from the `model` field in the conversation metadata returned by the server, replacing the previous localStorage-based persistence.

---

### Get Conversation Loaded Skills

Retrieve the list of manually loaded skill IDs for a conversation. Used by the frontend to hydrate the loaded skills state when a conversation is loaded from the server.

**Endpoint**: `GET /app/api/conversations/{conversation_id}/loaded-skills`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Response**: `200 OK`

- `{"skill_ids": [...]}` where each entry is a skill UUID string. Returns an empty list if no skills have been loaded.

**Error Response**: `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `get_conversation_loaded_skills()` in `chat/routes/conversations.py`. Reads from `ChatStorage.get_loaded_skill_ids()` in `chat/storage.py`, which reads `data/chats/{conversation_id}/loaded_skills.json`.

**Frontend Usage**: `useConversation` hook in `frontend/src/hooks/useConversation.ts` calls `fetchConversationLoadedSkills(conversationId)` from `frontend/src/api/client.ts` after loading conversation history, hydrating the loaded skills set in `ConversationSkillsContext` so the Skill Selector Modal can show already-loaded skills as greyed out. See [Skill Library Architecture](../architecture/skill-library.md) for the full conversation skill loader feature.

---

### Get Conversation System Prompt

Retrieve the saved system prompt for a conversation. The system prompt is persisted to `system_prompt.txt` in the conversation directory on each message send, so it always reflects the prompt used for the most recent message.

**Endpoint**: `GET /app/api/conversations/{conversation_id}/system-prompt`

**Authentication**: Required (session cookie or Bearer token)

**URL Parameters**:
- `conversation_id`: UUID of the conversation

**Response**: `200 OK`

- `{"system_prompt": "..."}` where the value is the full system prompt text, or `null` if no messages have been sent yet (the prompt file is created on first message send).

**Error Response**: `404 Not Found` if conversation does not exist or does not belong to the authenticated user (error code: `conversation_not_found`)

**Implementation**: `get_conversation_system_prompt()` in `chat/routes/conversations.py`. Reads from `ChatStorage.get_system_prompt_text()` in `chat/storage.py`, which reads `data/chats/{conversation_id}/system_prompt.txt`.

**Frontend Usage**: `SystemPromptModal` component in `frontend/src/components/SystemPromptModal.tsx` calls `fetchSystemPrompt(conversationId)` from `frontend/src/api/client.ts` when the modal opens (triggered by the info icon next to the context usage indicator in `ContextIndicator.tsx`).

---

### Search Conversations

Search across all conversation messages for the authenticated user, returning highlighted snippets.

**Endpoint**: `GET /app/api/search`

**Authentication**: Required (session cookie or Bearer token)

**Query Parameters**:
- `q` (required, string): Search query. Case-insensitive matching against message content.

**Response**: `200 OK`

**Response type**: `SearchResponse` in `frontend/src/api/types.ts`

**Response fields**:
- `results`: Array of `SearchResult` objects, each containing:
  - `conversation_id`: UUID of the matching conversation
  - `conversation_title`: Display title of the conversation
  - `matches`: Array of matching snippets (max 3 per conversation), each with highlighted query terms

**Limits**:
- Maximum 3 matching snippets per conversation
- Maximum 50 total results across all conversations

**Implementation**: `search_endpoint()` in `chat/routes/conversations.py`. Delegates to `ChatStorage.search_conversations()` in `chat/storage.py`, which performs file-based scanning of `chat_history.json` files with case-insensitive matching.

**Frontend Usage**: The `SearchModal` component (`frontend/src/components/SearchModal.tsx`) calls `searchConversations(query)` in `frontend/src/api/client.ts` with debounced typeahead input. Results display with highlighted snippets and keyboard navigation. Clicking a result navigates to the conversation and scrolls to the matching message via `scrollToMessageIndex` in `NavigationContext`.
---

### Composer Attachments

**Endpoint**: `POST /app/api/conversations/{conversation_id}/composer-attachments` -- persist clipboard-pasted images for the next `send_message`. Implementation: `upload_composer_attachments()` in `chat/file_routes.py`.

The endpoint is intentionally stricter than `POST /files/upload`:

- Accepts only `image/png` and `image/jpeg` (`_COMPOSER_ATTACHMENT_ALLOWED_MIMES`); other clipboard image types (gif/webp/svg) are rejected up front.
- Validates magic bytes (`_sniff_image_mime`) and rejects on `mime_mismatch` if the declared content_type contradicts the sniffed bytes.
- Caps per-file size at `_COMPOSER_ATTACHMENT_MAX_SIZE = 10 MiB`. Anthropic's 5 MiB image limit is enforced later in the provider upload path (oversized images still land on disk; `_build_user_message_with_attachments` in `chat/gemini_api/conversation.py` substitutes a text note).
- Stores each file under `workspace/pasted/<attachment_id>.<ext>` (UUID hex; `pasted` subfolder via `save_uploaded_file`) so the on-disk layout is visibly distinct from the user-managed workspace root.

Response shape: `ComposerAttachmentUploadResponse` in `frontend/src/api/types.ts` -- a `{attachments, errors}` pair where each `attachments` entry is a `ComposerAttachmentRef` (`attachment_id`, `filename`, `workspace_path`, `mime_type`, `size_bytes`) and each `errors` entry carries `{filename, error, message}`. The composer forwards `attachments` on the next `send_message` envelope (see [Send Message](#send-message)).

The endpoint publishes `file_list_changed` on the per-user channel like the other workspace-mutating routes (see [File Browser API](file-browser-api.md)), so a paste-and-send refreshes the file browser in any other tab the same user has open.

The frontend caller is `uploadComposerAttachments()` in `frontend/src/api/fileApi.ts`, driven by `handleSendMessage` in `frontend/src/components/ChatPanel.tsx`. See [Realtime -- Message Attachments](../architecture/realtime.md#message-attachments) for the end-to-end flow including provider-multimodal hand-off and the `attachments_unsupported_during_resume` rejection.

---

## WebSocket Endpoint

**Endpoint**: `WS /app/api/stream` (persistent, multiplexed -- one socket per browser session)

**Authentication**: Session cookie (browser, with periodic re-validation on the heartbeat) or `?api_key=` query parameter (scripts). Impersonation `imp` is honoured automatically.

The endpoint is implemented in `chat/realtime/socket.py`; the frontend client is `frontend/src/services/PersistentWebSocket.ts`.

The socket carries subscribe / unsubscribe / catchup / resync (with a 5-min server-side TTL per `(connection, conversation)` refreshed every 3 min by the foreground client; the React lifecycle does not eagerly unsubscribe on tab switch), the `send_message` and `stop` control plane, durable per-conversation events (`message_appended` with monotonic `seq`), transient streaming events, and per-user globals. See [Realtime Architecture](../architecture/realtime.md) for the full protocol, lifecycle (heartbeat, watchdog, reconnect, subscription TTL), seq allocation, replay buffer, send-lock, and stop semantics.

The historical per-turn `WS /app/api/conversations/{id}/message` endpoint and the legacy `confirm_action_result` bridge were removed in devplan 00062; clients use the persistent socket and resolve wait handles via REST (`POST /app/api/wait-handles/{id}/resolve`, `POST /app/api/action-requests/{id}/resolve`).

### Send Message

`{op: "send_message", conversation_id, message, model?, guide_id?, skill_ids?, timezone?, attachments?, flags?, attached_filenames?, client_send_id?}` kicks off a model run on this conversation (`guide_id` is only sent by the routine auto-send path -- the composer no longer offers guides).

The user message is appended to `chat_history.json` (which fires `message_appended` to every subscriber), the sending connection alone receives the delivery receipt `{type: "send_message_accepted", conversation_id, seq, client_send_id?}` (the optional opaque `client_send_id` from the request is echoed verbatim so the sender can match the receipt to its optimistic bubble; no receipt is sent when the append fails -- see [Realtime -- Delivery Receipt and Failed Sends](../architecture/realtime.md#delivery-receipt-and-failed-sends)), then `run_conversation_turn()` runs as a cancellable task.

A second concurrent `send_message` for the same conversation is rejected with `{type: "send_message_rejected", reason: "already_running"}` (per-conversation send lock; see [Realtime -- Send Lock](../architecture/realtime.md#send-lock-and-stop-semantics)).

Before the run starts, the user is re-read fresh from the DB (the persistent WS is never re-handshaked on an OAuth connect, so the connect-time snapshot can go stale); a deleted user row rejects the send with `{type: "send_message_rejected", reason: "user_not_found"}`. See [Realtime -- Fresh User Per Turn](../architecture/realtime.md#fresh-user-per-turn).

`attachments` is the array of composer-pasted image refs returned by [Composer Attachments](#composer-attachments). When set, an empty `message` string is accepted (text-or-attachments-required, not text-required), and `_handle_send_message` re-validates each ref (PNG/JPEG `mime_type`, non-empty `workspace_path` with no `..` segments, max 10 entries) before persisting the user message via `ChatStorage.append_message(..., attachments=...)` and forwarding into `run_conversation_turn`.

A composer-paste send into a conversation whose saved SDK history still has a dangling tool_use is rejected with `{type: "send_message_rejected", reason: "attachments_unsupported_during_resume"}` so the FE can prompt the user to send images in a new turn after the in-flight run finishes. See [Realtime -- Message Attachments](../architecture/realtime.md#message-attachments) for the end-to-end flow.

`attached_filenames` is a separate, optional list of workspace-relative names for generic files the composer just uploaded for this turn via the existing `POST /files/upload` route (the composer "Attach" button; NOT the `attachments` image path and NOT a new endpoint). `_handle_send_message` validates it is a list of non-empty strings and forwards it into `run_conversation_turn`, which surfaces the names only in that one turn's `<message_metadata>` block (no per-turn workspace enumeration). See [Gemini API -- Message Metadata Wrapping](../architecture/gemini-api.md#message-metadata-wrapping) and [Frontend -- Composer Component](../architecture/frontend.md#composer-component).

On the **first** message of a conversation only (`last_message_seq == 0`), a leading `%%flags[name,...]` magic line activates per-conversation flags: the recognized flags are persisted onto the conversation row and the line is stripped from the text sent to the model and from the cached sidebar title, while the displayed/persisted bubble keeps the line intact. If stripping the line leaves no prompt and there are no attachments, the send is rejected with `{type: "error", code: 4400, error: "invalid_send_message"}` and no flags are persisted. A `%%flags` line in any later (non-first) message is treated as literal text. See [Conversation Flags](../architecture/conversation-flags.md).

### Stop

`{op: "stop", conversation_id}` cancels any in-flight run for the conversation and unconditionally calls `cancel_pending_wait_handles_for_conversation()` so a stop click on a sentinel-suspended run (`SuspendForWaitHandles` / `SuspendForSlackReply` / `SuspendForActionRequest`) still flips dangling rows to `cancelled`. The cancel-time tail edit (`_save_interrupted_sdk_history`), session removal, and final flush mirror the historical per-turn handler verbatim.

The server replies `{type: "stop_acknowledged", conversation_id}` so the UI can clear its "thinking" indicator immediately. Regular socket disconnects (page refresh, network blips) do NOT cancel in-progress work; only explicit `stop` ops do.

### Server Event Types

Defined in the model loop in `chat/gemini_api/conversation.py`, the realtime publish helpers in `chat/realtime/events.py`, and the socket handler's transient mirror in `chat/realtime/socket.py:_publish_transient_event`. Parsed by `PersistentWebSocket.ts` (envelope routing) and `WebSocketManager.ts` (UI wiring). The set:

- Subscribe lifecycle: **`subscribed`** (with `mode: "up_to_date"` | `"catchup"` | `"resync"`, plus `current_seq`, optional `messages` for catchup, optional `reason` for resync), **`unsubscribed`**, **`subscription_expired`** (`conversation_id`; emitted by the per-connection sweep loop when the 5-min server-side TTL on a `(connection, conversation)` subscription lapses without a refresh -- the client clears any streaming buffer and rebuilds canonical state on the next `message_appended` boundary, or re-subscribes to keep observing). See [Realtime -- Subscription TTL](../architecture/realtime.md#subscription-ttl-and-refresh).
- Run lifecycle (per-conversation):
  - **`send_message_accepted`** (`seq`, optional echoed `client_send_id`; sender-only delivery receipt sent after the user row is appended),
  - **`send_message_finished`** (`interrupted: bool`),
  - **`send_message_rejected`** (`reason: "already_running" | "attachments_unsupported_during_resume" | "user_not_found" | "expensive_resume_unacknowledged"`):
    - `attachments_unsupported_during_resume` when a composer-paste send lands on a conversation with a dangling tool_use,
    - `user_not_found` when the fresh per-turn DB re-read finds the user row deleted,
    - `expensive_resume_unacknowledged` when a send into a flagged long-idle/long-context conversation lacks the `expensive_resume_acknowledged: true` envelope field -- see [Expensive-Resume Warning](../architecture/expensive-resume.md),
  - **`stop_acknowledged`**.
- Durable per-conversation: **`message_appended`** (`seq`, `conversation_id`). The client REST-fetches the new tail via `GET /conversations/{id}/tail` and merges the rows by `seq` (so overlapping tail-fetches stay in disk order). Bodies are NOT pushed on the live channel.
- Transient per-conversation: **`text_delta`** (LLM text chunk, `content`), **`tool_started`** (boundary "tool started" hint, `tool_id`, `tool_name`), **`sub_agent_tool_use`** / **`sub_agent_tool_result`** (sub-agent tool calls during `agent_task` / `agent_task_parallel` / `agent_task_parallel_template`, keyed by `parent_tool_id`; persisted on the parent `tool_result`'s `sub_agent_tool_calls` array via the boundary save), **`sub_agent_finished`** (canonical sub-agent done signal, `parent_tool_id`, `agent_name`, `status`, optional `error`), **`conversation_updated`** (out-of-band when the model sets a conversation name via `set_conversation_name`).
- Per-user globals:
  - **`request_count_changed`** (`counts: {all, open, executed, denied, stopped}`),
  - **`conversation_list_changed`** (`conversation_id`, `action: "created" | "archived" | "unarchived" | "renamed" | "model_changed"`),
  - **`file_list_changed`** (`conversation_id`, `project_id`, `scope: "project" | "conversation"`; emitted from each workspace-mutating tool handler and REST route on success so file browsers can silent-refresh mid-turn; project-scoped writes fan out across sibling-conversation tabs),
  - **`wait_handle_resolved`** (`conversation_id`, `handle_id`, `kind`, `status`, optional `request_id`, optional `response`),
  - **`routine_list_changed`** (`project_id`; emitted when a `create_routine`/`edit_routine` action request executes so the sidebar refreshes its cached per-project routine list).
- Liveness / errors: **`ping`** / **`pong`**, **`error`** (carries `code` and `error`/`message`; codes include 4400 invalid op, 4401 auth, 4404 not found, 4503 queue overflow, 4500 internal).

The durable-event set persisted to `chat_history.json` is `FLUSH_EVENT_TYPES` in `chat/_flush_helper.py` (`tool_use`, `tool_result`, `action_request`, `stats`); these events advance `seq` and trigger `message_appended`. Streaming text is bundled into a synthetic `text` message at the next flush boundary by both the backend (the model loop) and the frontend (`WebSocketManager`'s text-delta buffer).

### Message Persistence

Durable messages are saved to `chat_history.json` at the flush-event boundary by `chat/_flush_helper.py:make_flush_callback` -- the single fan-out point that advances `conversations.last_message_seq` and publishes `message_appended` to every persistent-WS subscriber. The user message at turn-start uses the single-message path (`ChatStorage.append_message`) which also stamps a `seq` and publishes via the shared `_publish_appended_to_bus` helper. Timestamps use ISO 8601 UTC with "Z" suffix via `utc_timestamp()` in `chat/storage.py`.

### WebSocket Close Codes

Custom 4xxx codes from `chat/realtime/socket.py`: 4401 (auth failed / revoked), 4408 (heartbeat timeout), 4503 (per-user queue overflow), 4500 (internal). The 4401 close triggers a `GET /app/api/me` probe on the client; success retries the socket, failure routes to the existing unauth flow.

---

### Get Current User (Session-Based) -- DEPRECATED

`GET /app/api/user` -- Deprecated. Returns user email and API key from session cookie. Kept for backward compatibility with external tools/scripts. The frontend uses `GET /app/api/me` instead.

---

### Get Current User Info

`GET /app/api/me` -- Returns user email, display name, connection status, admin flag, `default_model` (the per-user "last-used" default web/composer model for private conversations, raw from `users.settings.default_model`) and `public_default_model` (the same for public-project conversations, raw from `users.settings.public_default_model`); either may be `null` -- the FE owns the fallback per visibility: Opus 4.8 when allowed + credentialed, else the admin's first top-level pick, else the first offerable model per `available_models` on `GET /app/api/config`. See `get_current_user_info()` in `chat/routes/user.py`. Response type: `UserInfo` in `frontend/src/api/types.ts`. The FE re-reads this endpoint on every fresh new-chat composer mount and on every private<->public context switch for cross-tab default-model correctness (see [Frontend](../architecture/frontend.md), ConversationModelsContext `refreshDefaultModel`).

The `has_any_service_connected` flag drives auto-opening the settings panel to Data Connections for new users. The `is_admin` flag (from `admin_emails` in `server_config.json`) controls visibility of the `AdminOpsMenu` component.

---

### Admin, Settings, and User Endpoints

These endpoints are defined in `chat/routes/user.py` and `chat/routes/admin.py`. See those files for parameters, request/response shapes, and error codes.

- **POST `/app/api/admin/shutdown`** -- Graceful server shutdown (`admin_shutdown()` in `chat/routes/admin.py`). Admin-only (checked via `is_admin()` in `chat/auth.py`). Sets `app.state.shutdown_event`; lifespan handler sends SIGTERM after 1-second delay.
- **GET `/app/api/settings`** -- Get user settings (`get_settings()` in `chat/routes/user.py`). Reads from `settings` column in user record.
- **PUT `/app/api/settings`** -- Update user settings (`update_settings()`). Calls `invalidate_user_sessions()` to discard cached SDK sessions when system prompt changes.
  - Accepts `default_model` (per-user web/composer default for private conversations) and `public_default_model` (the same for public-project conversations), each validated via `resolve_model()` (Vertex registry ids and `<instance>:<wire_id>` instance-model ids) with empty string normalised to NULL (clear) and unknown ids rejected with 400 `invalid_model` -- mirrors the `slack_default_model` handling. The FE writes one of them best-effort only on the first send of a new chat, under the key matching the chat's visibility.
  - Also accepts `theme` (Settings > Appearance colour scheme): `"light"` / `"dark"` stored as-is, `"auto"` or empty string normalised to NULL (follow the OS), anything else rejected with 400 `invalid_theme`. `GET /app/api/me` surfaces the stored value as `theme` (null = auto) so the FE can apply it on hydration.
- **GET `/api/instructions`** -- Get full API documentation with embedded API key (`get_instructions()` in `quest.py`). Includes all services regardless of connection status (unlike system prompts which are filtered). Source of truth: `get_instructions_content()` in `api/instructions.py`.
- **POST `/api/reset-api-key`** -- Generate a new API key (`api_reset_api_key()` in `quest.py`). Old key stops working immediately. Key generation via `generate_api_key()` in `auth/config.py`.
- **GET `/app/api/connectors`** -- Get the user's Data Connections rows (`get_connectors()`). Returns a LIST of generically renderable rows (`kind: "oauth"` with `connect_url`, or `"api_key"` with `key_url`/`key_field`/`disconnect_url`; `available: false` hides a row; scope-mismatch detection for Google Services via `needs_reauth`). Re-fetched after OAuth popup completion. See [Settings Data Connections](../architecture/settings-data-connections.md).
- **POST `/app/api/logout`** -- Log out, preserving data (`logout()`). Clears session cookie.
- **POST `/app/api/logout-and-disconnect`** -- Log out and revoke OAuth tokens (`logout_and_disconnect()`).
- **POST `/app/api/delete-account`** -- Delete all account data (`delete_account()`). Cascades to memories, action requests, and user record.

---

## Memory Endpoints

All memory endpoints are defined in `chat/memory_routes.py` with data access in `db/memory_store.py`. Memories are markdown text blobs (max `MAX_MEMORY_SIZE` = 4KB). Full-text search is powered by SQLite FTS5 with sync triggers (see [Database Architecture](../architecture/database.md)).

- **GET `/app/api/memories`** -- List or search memories (`list_user_memories()`). Supports `q` param for FTS5 search and `include_archived` param. Without `q`, returns all memories newest first.
- **POST `/app/api/memories`** -- Create memory (`create_user_memory()`). FTS5 index updated via `memories_ai` trigger. The Settings UI is the only direct client; the LLM writes memories indirectly via `create_action_request(request_type="create_memory", ...)`, which routes through `CreateMemoryHandler` (see [Action Requests](../architecture/action-requests.md)).
- **GET `/app/api/memories/{memory_id}`** -- Get memory (`get_user_memory()`).
- **PUT `/app/api/memories/{memory_id}`** -- Update memory content (`update_user_memory()`). FTS5 index updated via `memories_au` trigger.
- **DELETE `/app/api/memories/{memory_id}`** -- Delete memory (`delete_user_memory()`). FTS5 index updated via `memories_ad` trigger.
- **PUT `/app/api/memories/{memory_id}/archive`** -- Archive memory (`archive_user_memory()`).
- **PUT `/app/api/memories/{memory_id}/unarchive`** -- Unarchive memory (`unarchive_user_memory()`).

---

## Wait Handle Endpoints

All wait-handle endpoints are defined in `chat/wait_handle_routes.py` with data access in `db/tool_wait_handle_store.py`. See [Wait Handles Architecture](../architecture/wait-handles.md) for the full mechanism, including the suspend sentinels, headless resume, and the atomic accept-and-resolve path on `POST /app/api/memories`.

- **GET `/app/api/wait-handles/{handle_id}`** -- Fetch a wait-handle row scoped to the authenticated user (`get_wait_handle()`). Returns 404 if not found or owned by someone else.
- **POST `/app/api/wait-handles/{handle_id}/resolve`** -- Resolve a pending wait handle (`resolve_wait_handle()`). Body shape is `ResolveWaitHandleRequest` in `chat/wait_handle_routes.py` (`status` in `{accepted, rejected, cancelled}`, optional `response`, `correlation_kind`, `correlation_id`, `feedback`). First-writer wins via the `status == PENDING` precondition in `tool_wait_handle_store.resolve_handle()`.
  - After the DB write, always calls `wait_resume.maybe_kick_resume()` to close the dangling `wait_for_handles` tool_use and stream the model's continuation into `chat_history.json`. The kick is dedupe-safe via `_active_resumes` and gated on the on-disk transcript still ending on a dangling wait, so it is a no-op when there is nothing to resume.
  - Errors: 404 (missing), 400 (already resolved), 409 (race lost).

---

## Action Request Endpoints

All action request endpoints are defined in `chat/action_request_routes.py` with data access in `db/action_request_store.py`. See [Action Requests Architecture](../architecture/action-requests.md) for the full architecture.

Action requests are created by the LLM agent during conversations and persist with status `open` until approved, revised, or stopped. Built-in request types include Slack messages/DMs, Telegram messages, calendar invites, and Twitter/X DMs. All responses are enriched with server-rendered preview data via `_enrich_with_preview()` using `get_preview_for_request()` from `chat/action_request_types/registry.py`.

- **GET `/app/api/action-requests`** -- List action requests, newest first. Supports `include_context` for enriched results with routine/project metadata (delegates to `list_action_requests_enriched()`) and `status` (`open` / `executed` / `denied` / `stopped`) to filter at the DB level via the `ix_action_requests_user_status_created` composite index; omitting `status` returns all statuses. Per-row preview enrichment runs concurrently via `asyncio.gather()`.
- **GET `/app/api/action-requests/count`** -- Count action requests, optionally filtered by `status` param.
- **GET `/app/api/action-requests/counts`** -- Count by all statuses in one response (`open`, `executed`, `denied`, `stopped`, `all`) via `count_action_requests_by_status()`.
- **GET `/app/api/action-requests/{id}`** -- Get a single action request.
- **POST `/app/api/action-requests/{id}/resolve`** -- Approve (`"execute"`), revise (`"deny"` + `feedback`), or stop (`"stop"`) a request. Body is `ResolveRequestBody` in `chat/action_request_routes.py` (`action`, optional `feedback`). A bare `"deny"` without feedback is the legacy one-click Deny, still accepted but no longer offered by the web UI except on `subagent_return` cards.
  - On execute, looks up the `ActionRequestHandler` for the `request_type` in the handler registry (`chat/action_request_types/registry.py`) and calls `handler.execute()`. If execution fails, status remains `open` for retry.
  - On deny (Revise), the optional `feedback` string (whitespace stripped, capped at 4000 chars, empty becomes `None`) is persisted on the `action_requests.result` row and on the linked wait-handle `response` so the resumed model reads it off the closed `create_action_request` tool_use.
  - On stop, the row becomes `stopped` (`result: {"stopped": true}`), the linked wait handle is resolved `stopped` so the composer unlocks, every other open request of the same conversation is stopped too, and NO resume is kicked: the conversation halts until the user's next message, which closes the dangling tool_use with `verdict: "stopped"`. `feedback` is ignored. See [Action Requests Architecture](../architecture/action-requests.md#stop).

---

## Guide Endpoints

All guide endpoints are defined in `chat/guide_routes.py` with data access in `db/guide_store.py`. See [Guides Architecture](../architecture/guides.md) for the full architecture.

Guides are named system prompt presets, **deprecated in favor of skills**: new guides cannot be created, the composer no longer offers guide selection (only routine guide overrides still send a `guide_id`), new conversations no longer fall back to the default guide, and a missing default guide is never recreated. Existing guides remain manageable in Settings so users can convert them.

- **GET `/app/api/guides`** -- List guides. Default guide first, then alphabetically. Does not auto-create a default.
- **POST `/app/api/guides`** -- Returns 410 `guides_deprecated`.
- **POST `/app/api/guides/{guide_id}/convert-to-skill`** -- Convert the guide into a private skill and delete the guide (see [Guides Architecture -- Migration to Skills](../architecture/guides.md#migration-to-skills)).
- **GET `/app/api/guides/{guide_id}`** -- Get guide.
- **PUT `/app/api/guides/{guide_id}`** -- Update guide. Updating `content` calls `invalidate_user_sessions()` to discard cached SDK sessions.
- **DELETE `/app/api/guides/{guide_id}`** -- Delete any guide, the default included. Existing conversations using the deleted guide are unaffected (they use snapshotted content).

---

### Unauthenticated Endpoints

- **GET `/app/api/config`** -- Get environment config (`get_app_config()` in `chat/routes/user.py`). Returns `quest_env` (`"dev"` or `"prod"`). Frontend derives app name ("DevQuest" vs "Quest") from this.
- **GET `/app/api/version`** -- Release info of the running instance (`get_app_version()` in `chat/routes/user.py`): `git_hash` (HEAD commit, polled by the frontend to detect redeploys) plus `version`, `tag`, `released` (ISO date-time) and `commits_since_tag` from the nearest `v<semver>` release tag (`config/version.py`, all null/0 on an untagged checkout; see [Development Workflows -- Releases](../setup/development-workflows.md#releases)). Captured once at process startup; shown in Settings > About.
- **GET `/auth/login-url`** -- Get Google OAuth login URL (`get_login_url()` in `auth/google_login.py`). Used by `SignInScreen.tsx`.

---

## Model and Mode Configuration

Available models are defined in the model registry in `chat/llm/config.py`. Deprecated model IDs are silently remapped. The model is passed via the WebSocket message payload and persisted on the `conversations.model` column. After the first message, the provider (Gemini or Anthropic) is locked per-conversation. Provider locking is in `frontend/src/contexts/ConversationModelsContext.tsx`. API keys are from `server_credentials.json` (Gemini) or ADC credentials (Anthropic Vertex AI).

All runs use direct Python SDK integration via `run_conversation_turn()` in `chat/gemini_api/conversation.py`, run by `chat/realtime/socket.py:_run_send_message` from the persistent WS. (The former Docker-based Gemini CLI chat mode and its `gemini.chat_mode` config knob were removed when gemini-cli was deprecated.)

## Data Storage

### Chat History

Location: `data/chats/{conversation_id}/chat_history.json`. Message types (`text`, `tool_use`, `tool_result`, `interrupted`, plus marker rows like `compaction` and `model_fallback` -- the latter recording a mid-turn Anthropic refusal-fallback model switch, see [LLM Providers -- Refusal fallbacks](../architecture/llm-providers.md)) and their fields are defined in `chat/storage.py`. Timestamps use ISO 8601 UTC with "Z" suffix via `utc_timestamp()`. The frontend parser (`parseUTCTimestamp()` in `frontend/src/utils/formatters.ts`) is backwards compatible with legacy timestamps.

### Workspace Directory Structure

Standalone conversations store files in `data/chats/{conversation_id}/workspace/`. Project conversations share a workspace at `data/projects/{project_id}/workspace/`. See [Projects Architecture](../architecture/projects.md) for workspace resolution details.

