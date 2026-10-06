# Realtime (Persistent Multiplexed WebSocket)

## Overview

A single persistent WebSocket per browser session carries every realtime signal from the backend to the web UI: subscribe / unsubscribe / catchup / resync against any conversation the user owns, the `send_message` and `stop` control plane, durable per-conversation events (`message_appended` with monotonic `seq`), transient streaming events (text deltas, sub-agent updates), and per-user globals (open-request count badge, conversation list mutations, wait-handle resolutions). The per-turn `WS /app/api/conversations/{id}/message` endpoint that previously serviced one socket per turn was deleted; it lives on only as the historical name for what is now `WS /app/api/stream`.

This is the channel that lets resumed runs (memory accept, action-request approve, slack debounce flush) push their continuation to a viewing tab without a reload, lets a Slack-driven conversation stream live in the web UI, lets a second tab observe a turn driven from the first, and lets the open-request badge update without polling.

## Key Files

### Backend (`chat/realtime/`)

- `chat/realtime/socket.py` -- the `WS /app/api/stream` endpoint (`stream_endpoint`). Spawns a reader / writer / heartbeat / subscription-sweep coroutine quartet per connection (`_reader_loop`, `_writer_loop`, `_heartbeat_loop`, `_subscription_sweep_loop`).
  - Owns `_handle_subscribe`, `_handle_unsubscribe`, `_handle_send_message`, `_handle_stop`, plus the per-connection subscription deadline map (used for TTL-based eviction; see [Subscription Protocol](#subscription-protocol)), the per-conversation send registry (`_active_send_runs`), and the run task body (`_run_send_message`).
  - Constants `SUBSCRIPTION_TTL_SECONDS = 300` and `SUBSCRIPTION_SWEEP_INTERVAL_SECONDS = 30` and the custom close codes (4401 auth, 4408 heartbeat, 4503 queue overflow, 4500 internal) are at the top of the file.
- `chat/realtime/bus.py` -- `Bus` singleton with two channel namespaces (per-conversation and per-user). `SubscriberQueue` is a bounded `asyncio.Queue` (`DEFAULT_QUEUE_MAXSIZE = 256`) with two sticky overflow flags consumed by the writer loop. `connected_user_ids()` snapshots the users with at least one live connection, for install-wide fan-out of a per-user event (Quest Docs everyone shares).
- `chat/realtime/replay_buffer.py` -- per-conversation deque ring buffer (`DEFAULT_BUFFER_MAXLEN = 200`). `slice(after_seq)` returns `[]` (up to date), a non-empty list (catchup window), or `None` (gap too large -- caller responds `resync`). `eviction_loop()` is a background coroutine spawned from FastAPI lifespan; it sweeps every `EVICTION_SWEEP_INTERVAL_SECONDS = 300` and drops conversations with no subscribers and no appends in the last `IDLE_EVICTION_SECONDS = 1800`.
- `chat/realtime/events.py` -- typed envelope helpers (`make_request_count_changed`, `make_conversation_list_changed`, `make_file_list_changed`, `make_routine_list_changed`, `make_doc_list_changed`, `make_doc_changed`, `make_wait_handle_resolved`, `make_message_appended`, `make_text_delta`, `make_tool_started`, `make_subscription_expired`). Centralising envelope construction keeps the wire format consistent across publish sites.
- `chat/_flush_helper.py` -- `make_flush_callback()` is the single fan-out point for durable events. After `ChatStorage.append_structured_messages()` succeeds, the closure calls `chat.storage._publish_appended_to_bus()` for every newly-stamped `(seq, message)` pair.
- `chat/storage.py` -- `_publish_appended_to_bus()` is the shared helper called from both `append_message()` (single-message path used by user-message-at-turn-start) and `make_flush_callback()` (structured-message batch path). It pushes into `replay_buffer` and publishes `message_appended` to the bus. Failures are best-effort; they never roll back the disk write.
- `chat/routes/conversations.py` -- `GET /conversations/{id}` returns `last_message_seq` so the client can subscribe with a usable `last_seq` right after hydration; `GET /conversations/{id}/tail?after_seq=N` returns the message tail for catchup / resync REST fallback.
- `db/models.py:Conversation` -- `last_message_seq` integer column (server default `"0"`, application default `0`). Cached high-water mark; the JSON file is the source of truth for the actual seq stamped on each message.
- `db/conversation_store.py` -- `update_last_message_seq()` mirrors the high-water seq into the DB. `_conversation_to_dict` includes `last_message_seq`. `get_conversation_meta()` is what `_handle_subscribe` reads to compute `current_seq` without re-reading the file.
- `alembic/versions/2a4c7e9b1d3f_add_last_message_seq_to_conversations.py` -- adds the column. Backfill happens in `quest.py:lifespan` rather than in the Alembic op (the migration cannot portably read JSON files); the lifespan iterates rows with `last_message_seq = 0` and a non-empty file, counts messages, and writes the count back. Idempotent on every boot until clean.
- `quest.py` -- registers `chat.realtime.socket.router` and spawns `chat.realtime.replay_buffer.eviction_loop()` from the lifespan.

### Backend publish sites (per-user globals)

Every publisher is a best-effort `bus.publish_to_user(...)` wrapped in try/except so a publish failure never breaks the underlying write:

- `chat/action_request_routes.py` -- on resolve, publishes `wait_handle_resolved` (linked handle) and `request_count_changed` (recomputed open count); executing a `create_routine`/`edit_routine` request additionally publishes `routine_list_changed {project_id}` so the sidebar's cached per-project routine list refreshes without a page reload (no `conversation_id` on the envelope, so it routes to global handlers without an FE allow-list entry). On create, the dispatch handler in `chat/gemini_api/turn_tools.py` publishes `request_count_changed` after the `action_requests` row is inserted.
- `chat/wait_handle_routes.py` -- on `POST /app/api/wait-handles/{id}/resolve`, publishes `wait_handle_resolved`.
- `chat/slack_driven_runtime.py` -- `_flush_after()` publishes `wait_handle_resolved` on slack_reply resolve.
- `chat/wait_handles/wait_timer.py` -- on timeout flip, publishes `wait_handle_resolved` with `status="timed_out"`.
- `chat/gemini_api/session.py` -- `cancel_pending_wait_handles_for_conversation` publishes a `wait_handle_resolved` envelope with `status="cancelled"` per row it flips, so the persistent-WS `{op:"stop"}` handler unlocks the FE composer across tabs without waiting for a tail refetch. See [Wait Handles -- Composer Sync](wait-handles.md#composer-sync).
- `chat/routes/conversations.py` -- the `_publish_list_changed()` helper publishes `conversation_list_changed` after each create / archive / unarchive / rename / model PATCH.
- `chat/slack_socket_mode.py` -- `_handle_top_level_dm` publishes `conversation_list_changed` after the `slack_conversations` row is created.
- `chat/gemini_api/tool_handlers/_common.py` -- the shared `_publish_file_list_changed` helper is called from each workspace-mutating tool handler on the success path: `_handle_write_workspace_file`, `_handle_edit_workspace_file`, `_handle_download_drive_file`, `_handle_google_export_doc`, `_handle_run_script`, `_handle_run_python` (plugin file-download handlers like the GitHub plugin's `github_get_job_log` call the same helper). Failed writes do not emit.
  - The two container tools (`run_script`, `run_python`) emit unconditionally on a clean container exit because the `:Z` workspace mount means the model code could have written zero or many files and a cheap diff is not available.
  - Carries `scope="project"` when the conversation belongs to a project, `scope="conversation"` otherwise.
- `chat/file_routes.py` -- the local `_publish_file_list_changed` helper is called from the three mutating REST routes (`upload_files`, `delete_file`, `create_folder`) so a write driven from one tab refreshes file browsers in any other tab the same user has open. Same `scope` rule as the tool handler emission sites.
- `chat/docs/events.py` -- the Quest Docs globals `doc_list_changed` / `doc_changed`, sent to the doc's **audience**: the owner plus every direct share recipient (`doc_audience()`), and, when the doc has an "everyone" share row, every user connected right now (`bus.connected_user_ids()` in `chat/realtime/bus.py`, an in-process snapshot of the per-user channels; ids deduped). That broadcast is not filtered by the recipients' gates, and an account deletion notifies no one (see [Quest Docs -- Known Limitations](quest-docs.md#known-limitations)). They are called by:

  - `chat/docs/service.py`, after every body/asset write (`publish_doc_write()`: model tools, the `write_doc` action request, and the UI's content save, image upload / delete and History restore in `chat/docs/ui_writes.py`) and for the UI create and delete (the delete captures the audience before the share rows cascade away; no doc's mode can be switched);
  - `chat/docs/routes.py`, only for a rename that changed something;
  - `chat/docs/share_routes.py`, after a share add / change / remove: `doc_list_changed` to the owner and the affected recipient, or to every connected user for the everyone row (`publish_share_changed()`);
  - `chat/docs/history.py`, after a History copy: `doc_list_changed` to the caller only;
  - the project-delete route in `chat/project_routes.py`, once after its directory sweep, to the combined audience of the project's docs collected before the delete (`docs_audience()`).

  Event-loop thread only: never from inside `asyncio.to_thread`. See [Quest Docs](quest-docs.md#realtime).

### Frontend

- `frontend/src/services/PersistentWebSocket.ts` -- `PersistentWebSocketClient` singleton (`persistentWebSocket`). Owns:
  - the socket lifecycle, exponential-backoff reconnect (`RECONNECT_BASE_MS = 1000`, `RECONNECT_CAP_MS = 30000`), client-originated 25s ping,
  - 60s inbound-frame watchdog (ticked every `WATCHDOG_TICK_MS = 5000` so the deadline still fires under background-tab `setTimeout` throttling),
  - per-conversation `lastSeqByConversation` map persisted to `sessionStorage` under the `quest_persistent_ws_last_seq` key,
  - and a per-conversation `subscriptionRefreshTimers` map of `setInterval` handles that re-issue `subscribe` every `SUBSCRIPTION_REFRESH_INTERVAL_MS = 180000` (3 minutes) while the conversation stays in the `subscribed` set so the server-side TTL never lapses.
  - Public API: `connect()`, `disconnect()`, `subscribe(id)`, `unsubscribe(id)`, `send(payload)`, `onGlobalEvent(handler)`, `onConversationEvent(id, handler)`, `setLastSeq(id, seq)`, `getLastSeq(id)`.
- `frontend/src/services/WebSocketManager.ts` -- thin compatibility shim over `persistentWebSocket`. Translates per-conversation transient events (`text_delta`, `sub_agent_*`, `tool_started`) into the existing `streamingMessages` UI contract, bundles streaming text deltas into a synthetic `text` message at the next `message_appended` flush boundary, handles `subscription_expired` by clearing the streaming buffer for that conversation (so the next durable `message_appended` boundary triggers a tail fetch and rebuilds canonical state), and forwards `sendMessage` / `stopStreaming` to the persistent socket.
- `frontend/src/api/config.ts` -- `endpoints.persistentStream()` builds the `WS /app/api/stream` URL (with `ws://` / `wss://` protocol picked from `window.location.protocol`).
- `frontend/src/api/client.ts` -- `fetchConversationTail(conversationId, afterSeq)` hits `GET /conversations/{id}/tail?after_seq=N`.
- `frontend/src/contexts/AuthContext.tsx` -- calls `persistentWebSocket.connect()` once when `isAuthenticated` becomes true and `disconnect()` on logout / account-delete.
- `frontend/src/hooks/useConversation.ts` -- subscribes to `onConversationEvent(conversationId, ...)` on mount, records `response.last_message_seq` from `fetchConversation` so the first subscribe carries a usable `last_seq`, fetches the tail on each `message_appended` event, hands the fetched rows to `conversationStore.insertMessagesBySeq` so racing tail-fetches stay in disk order regardless of resolve order, and reconciles optimistic user bubbles in-place.
  - The effect cleanup detaches the per-conversation event handler but does NOT send a wire `unsubscribe`; the conversation just stays subscribed on the server until the TTL expires (or the WS closes). This ensures transient `text_delta` events emitted while the user is on a different conversation still land in `conversationStore[id]` instead of being dropped, so coming back mid-stream produces a complete in-progress bubble.
  - The same seq-ordered insert runs on `subscribed` envelopes with `mode: "catchup"`.
- `frontend/src/components/Sidebar.tsx` -- `RequestsBadge` subscribes to `onGlobalEvent` filtering for `request_count_changed`. Sidebar list refresh subscribes for `conversation_list_changed`; a second subscription re-fetches a project's routines on `routine_list_changed`.
- `frontend/src/components/ActionRequestMessage.tsx` -- subscribes for `wait_handle_resolved` whose `request_id` matches `message.request_id` and updates status / feedback in place.
- `frontend/src/components/RequestsView.tsx` -- subscribes for `request_count_changed`.
- `frontend/src/hooks/useDocs.ts` (sidebar Docs blocks, All Docs incl. "Shared with you") and `frontend/src/hooks/useProjectDocsIndex.ts` (All Docs per-project fan-out) -- silent re-fetch on `doc_list_changed`, trailing-debounced (300 ms / 600 ms) so a burst, e.g. writes to a doc shared with everyone, costs one re-fetch per list. `frontend/src/hooks/useDoc.ts` (doc viewer) -- re-fetches on `doc_changed` for its `doc_id` when `updated_at` differs from the shown one, and on every `doc_list_changed` (a delete or a revoked share sends only that, so the re-fetch 404s into the not-found state). Share recipients receive both events like the owner, so their views update live. See [Quest Docs -- Frontend](quest-docs.md#realtime-consumers).
- `frontend/src/components/FileBrowser.tsx` -- subscribes for `file_list_changed` and filters by the active conversation: an envelope with `scope === "project"` matches when `project_id` equals the active `activeProjectId` (so a project-scoped write under project P refreshes every open tab viewing any conversation under P), an envelope with `scope === "conversation"` matches only when `conversation_id` equals the active `conversationId`.
  - A 200ms debounce (`FILE_LIST_REFRESH_DEBOUNCE_MS`) coalesces bursts of writes within a single turn into one `silentRefresh()` call.
  - This replaced the prior `WebSocketManager.onStreamComplete` / `onToolResult` subscriptions; the file browser is now event-driven against workspace mutations rather than coupled to the streaming lifecycle, so it refreshes mid-turn after each successful write.

## Subscription Protocol

Client ops (sent as JSON):

- `{op: "subscribe", conversation_id, last_seq}` -- subscribe to a conversation channel and reconcile against the client's known seq. The client persists `last_seq` per conversation in `sessionStorage`. Each `subscribe` op records (or refreshes) a per-`(connection, conversation)` deadline of `now + SUBSCRIPTION_TTL_SECONDS = 300` (5 min) on the server. Re-subscribing is idempotent and just pushes the deadline forward, which is exactly how the FE keeps inactive subs alive (see below).
- `{op: "unsubscribe", conversation_id}` -- explicit teardown. Used only when the client knows a conversation is no longer interesting (rare); React lifecycle / tab switching does NOT fire this op.
- `{op: "send_message", conversation_id, message, model?, guide_id?, skill_ids?, timezone?, attachments?, flags?, attached_filenames?}` -- start a model run on this conversation. Mirrors the historical per-turn WS message envelope.
  - `guide_id` is only sent by the routine auto-send path (routine guide override); the composer no longer offers guides (deprecated in favor of skills).
  - `attachments` is the array of composer-pasted image refs returned by `POST /conversations/{id}/composer-attachments` (see [Chat API -- Composer Attachments](../api/chat-api.md#composer-attachments)).
  - `flags` is the optional out-of-band per-conversation flag selection from the composer Flags popover (a list of strings, honored first-message-only, validated against `KNOWN_FLAGS` and merged with any `%%flags` magic line; see [Conversation Flags -- Composer UI](conversation-flags.md#composer-ui-out-of-band-selection)).
  - `attached_filenames` is the optional list of workspace-relative names of generic files the composer just uploaded for this turn (via the existing `POST /files/upload` route -- distinct from the `attachments` image path).
  - `_handle_send_message` validates it is a list of non-empty strings, forwards it through `_run_send_message` into `run_conversation_turn`, and the names surface only in that turn's `<message_metadata>` block (no per-turn workspace enumeration; see [Gemini API -- Message Metadata Wrapping](gemini-api.md#message-metadata-wrapping)).
  - Server-side validation in `_handle_send_message` allows empty `message` text when at least one attachment is present, caps `attachments` at 10 entries, and re-validates each ref's `mime_type` (PNG/JPEG only) and `workspace_path` (no `..` segments).
  - The user is also re-read fresh from the DB before the run starts; a deleted user row rejects the send with `reason: "user_not_found"` (see [Fresh User Per Turn](#fresh-user-per-turn)).
  - The optional `expensive_resume_acknowledged: true` field marks that the user clicked through the expensive-resume warning card; without it, a non-first-message send into a flagged long-idle/long-context conversation is rejected with `reason: "expensive_resume_unacknowledged"` (see [Expensive-Resume Warning](expensive-resume.md)). See [Message Attachments](#message-attachments).
- `{op: "stop", conversation_id}` -- cancel any in-flight run for the conversation and cancel pending wait handles.
- `{op: "ping"}` / `{op: "pong"}` -- liveness. The server pings every 25s; any inbound frame on the client side bumps the watchdog deadline.

Server reply to `subscribe` (single envelope, three modes):

- `mode: "up_to_date"` -- client's `last_seq >= server.last_seq`; start streaming live.
- `mode: "catchup"` -- gap fits in the replay buffer; envelope carries `messages: [...]` with each message body and its `seq` stamp.
- `mode: "resync"` (with a `reason` string -- `"buffer_evicted"`, `"buffer_unavailable"`, `"queue_overflow"`) -- gap exceeds the buffer or queue overflowed. Client REST-fetches `GET /conversations/{id}/tail?after_seq=client_last_seq` and updates `last_seq` from the response.

Every mode also carries `run_active: bool` -- whether a model run is streaming on the conversation right now (`is_run_active()` in `chat/realtime/socket.py`: a live `_active_send_runs` task, or a headless wait-handle resume that has passed its hold gates, tracked by `_streaming_resumes` in `chat/wait_handles/resume.py`). See [Run-State Reconcile on Subscribe](#run-state-reconcile-on-subscribe).

Live envelopes the server emits on a subscribed channel:

- Durable: `message_appended {conversation_id, seq}`. The client's `lastSeqByConversation` map is updated on receipt and the new tail is fetched via REST.
- Transient: `text_delta {conversation_id, content}`, `sub_agent_tool_use`, `sub_agent_tool_result`, `sub_agent_finished`, `tool_started`, plus run-lifecycle envelopes:
  - `send_message_accepted {conversation_id, seq, client_send_id?}` (sent ONLY to the sending connection right after the user row is appended and before the run task starts; the sender's delivery receipt -- see [Delivery Receipt and Failed Sends](#delivery-receipt-and-failed-sends)),
  - `resume_started {conversation_id}` (a headless wait-handle resume began streaming on this conversation; the FE enters the same streaming/stop composer state as a local send until the paired `send_message_finished` arrives -- see [Wait Handles](wait-handles.md#suspend--resume)),
  - `send_message_finished {interrupted, error}` (published by both the WS send path and the headless resume; `error: true` when the run raised; the durable error bubble arrives separately as a persisted `{"type": "error"}` message via `message_appended` -- see [Conversation Loop -- Error Surfacing](gemini-api.md#error-surfacing) -- and the FE uses the flag to skip the success desktop notification),
  - `send_message_rejected {reason}`, where `reason` is
    - `"already_running"` for the per-conversation send-lock,
    - `"attachments_unsupported_during_resume"` when a composer-paste send lands on a conversation whose saved SDK history still has a dangling tool_use (see [Message Attachments](#message-attachments)),
    - `"user_not_found"` when the user row no longer exists at the fresh per-turn re-read (see [Fresh User Per Turn](#fresh-user-per-turn)),
    - or `"expensive_resume_unacknowledged"` when a non-first-message send into a flagged long-idle/long-context conversation lacks the `expensive_resume_acknowledged: true` envelope field (see [Expensive-Resume Warning](expensive-resume.md)),
  - `stop_acknowledged`,
  - the TTL-eviction envelope `subscription_expired {conversation_id}` (see [Subscription TTL](#subscription-ttl-and-refresh)),
  - and pass-through `conversation_updated` and `error`.
  - None of these carry a `seq`; a late subscriber misses any in-flight ones but folds the next `message_appended` flush into history.
- Per-user globals: `request_count_changed {counts}`, `conversation_list_changed {conversation_id, action}`, `file_list_changed {conversation_id, project_id, scope}`, `routine_list_changed {project_id}`, `doc_list_changed` (no payload), `doc_changed {doc_id, updated_at}`, `wait_handle_resolved {conversation_id, handle_id, kind, status, request_id?, response?}`. The two Quest Docs envelopes carry no `conversation_id`, so they reach the global handlers without an allow-list entry (consumers listed under [Frontend](#frontend)). The `file_list_changed` envelope carries a `conversation_id` but is per-user-scoped: project-scoped writes must reach sibling-conversation tabs that don't subscribe to the triggering conversation. The FE allow-list in `PersistentWebSocket.handleMessage` includes its `type` so the global-handler path runs even though a `conversation_id` is present (same pattern as `conversation_list_changed`).

The frontend distinguishes per-conversation envelopes (any envelope carrying a `conversation_id` other than the listed globals) from per-user globals via the explicit allow-list in `PersistentWebSocket.handleMessage` so that envelopes like `conversation_list_changed` and `file_list_changed` (which mention a `conversation_id` but are user-scoped) reach the right handlers.

## Run-State Reconcile on Subscribe

The run-lifecycle envelopes `resume_started` / `send_message_finished` are transient: one published while a tab's socket was down (network blip, watchdog `forceReconnect`, a server heartbeat close after an event-loop stall) is gone for good. The durable rows still arrive through the reconnect `catchup`, so before this reconcile a run that ended during the gap left the tab with its final answer on screen but the "Generating response..." spinner and the locked composer in place until a page reload -- the client's only signal that a run had ended was the transient envelope it never received.

`WebSocketManager.syncRunState(conversationId, run_active)` runs on every `subscribed` ack, from both `useConversation`'s handler (the viewed conversation) and the streaming buffer's own handler (a conversation whose `ChatPanel` is unmounted while the user is on another screen, drained by the 3-min subscribe refresh):

- `run_active: false` while the tab is streaming -- the run is over: drain the buffer like an abandoned send (no synthetic "interrupted" marker; a stopped run persists its own durable marker, which the catchup delivered) and fire `onStreamComplete`. Skipped while a tracked send is still awaiting its `send_message_accepted` receipt: the hook subscribes and sends back-to-back on a fresh conversation, so the ack for that subscribe is computed before the server has registered the run.
- `run_active: true` while the tab is idle -- a run this tab did not start, or lost track of across a reconnect / page reload, is streaming: enter the same streaming/stop composer state as a `resume_started` (`attachToResumeStream`) so the composer shows the stop button instead of offering a send the server would reject with `already_running`. This is also what makes a page reload mid-run, or a second tab, show the spinner.
- Field absent (older server) -- no-op.

Both server registries drop a run in the same event-loop step that publishes its `send_message_finished` (no await between the publish and the task returning; `_streaming_resumes` is discarded right after the publish), so an ack computed after that publish never reports a finished run as live. A held resume (sibling cards pending, stopped card) sits in `_active_resumes` without ever publishing the paired envelopes, which is why the resume side reports the `_streaming_resumes` marker rather than task presence.

---

## Subscription TTL and Refresh

A subscription is a per-`(connection, conversation)` server-side deadline rather than a lifecycle-bound resource. Each `subscribe` op writes `now + SUBSCRIPTION_TTL_SECONDS = 300` into the connection's deadline map (replacing any prior value, so re-subscribing simply pushes the deadline forward). `_subscription_sweep_loop` runs every `SUBSCRIPTION_SWEEP_INTERVAL_SECONDS = 30`; for each entry whose deadline has passed it calls `bus.unsubscribe_conversation(...)` and emits a transient `{op: "subscription_expired", conversation_id}` envelope to that connection.

The frontend keeps an interested subscription alive by re-issuing `subscribe` every `SUBSCRIPTION_REFRESH_INTERVAL_MS = 180000` (3 min) for every conversation in `PersistentWebSocket`'s `subscribed` set, regardless of which conversation the user is currently viewing. The 3-min refresh against a 5-min server TTL gives one full miss of headroom before eviction. The React lifecycle does NOT fire `unsubscribe` on tab switch / unmount: the cleanup only detaches the per-conversation event handler. This is the bug fix that prevents transient `text_delta` events on the away-conversation from being silently dropped while the user is briefly elsewhere.

When the client receives `subscription_expired` for a conversation that is still in its `subscribed` set, `WebSocketManager` clears the streaming buffer for that conversation. Canonical state is rebuilt the next time a `message_appended` flush boundary lands and the existing tail-fetch / `insertMessagesBySeq` path runs. If the client wants to keep observing the conversation past the eviction it just re-subscribes (the refresh `setInterval` does this automatically while the conv stays in `subscribed`); the next `subscribe` op is reconciled like any other, replying `up_to_date` / `catchup` / `resync` against `last_seq`.

The explicit `{op: "unsubscribe"}` op still exists for genuine teardown -- it removes the subscription from the bus and from the connection's deadline map -- but no current FE call site fires it.

## Seq Allocation

`chat_history.json` is the source of truth: every appended message is stamped with a per-conversation monotonic `seq` inside the same write that appends it. `conversations.last_message_seq` is a denormalised cache so the subscribe handler can answer `up_to_date` / `catchup` / `resync` without re-reading the file.

The seq is computed by `ChatStorage._read_seq_high_water()` in `chat/storage.py`: it returns the max `seq` already on disk, or for a pre-migration file with no stamps it falls back to `len(messages)` so the first stamped message gets `len + 1`. Both `append_message()` (single-message path) and `append_structured_messages()` (batch path) read the high-water, stamp, write the file, and call `update_last_message_seq()` to advance the DB cache. Both paths return `(seq, message)` pairs (or a list of them) to their callers, so the bus publish can advertise a seq the disk already records.

The bus publish happens AFTER both writes succeed. A DB-write failure after a successful file write is recovered on the next append: `_read_seq_high_water` re-reads from the file and the DB column is overwritten back to truth.

## Replay Buffer

In-memory only. Per-conversation `collections.deque(maxlen=200)` of `(seq, message)` pairs, plus a per-conversation last-append timestamp for idle eviction. Append path is `_publish_appended_to_bus` -> `replay_buffer.append`. Slice path is `_handle_subscribe` -> `replay_buffer.slice(after_seq)`:

- empty deque or `after_seq >= latest` -- return `[]` (caller answers `up_to_date` if the DB seq matches, or downgrades to `resync` with reason `buffer_unavailable` if the DB seq is ahead but the buffer was evicted).
- `after_seq + 1 < earliest_seq` -- return `None` (caller answers `resync` with reason `buffer_evicted`).
- otherwise -- return the messages with `seq > after_seq`.

After a server restart the buffer is empty, the DB still has `last_message_seq`, and the chat history file has the durable bodies, so every reconnecting client gets `resync` and recovers via REST. The `eviction_loop` background task drops buffers with no subscribers and no appends for 30+ minutes so the memory footprint stays bounded across many long-lived but inactive conversations.

## Send Lock and Stop Semantics

`_active_send_runs: dict[str, asyncio.Task]` at module scope in `chat/realtime/socket.py` is a per-conversation registry of in-flight runs. A second `send_message` for a conversation that already has a running task is rejected with `{type: "send_message_rejected", reason: "already_running"}` rather than letting both runs race on `chat_history.json` and `sdk_history.json`. Cleanup is via `task.add_done_callback(_cleanup)` so the slot opens on success, error, or cancel.

The same `already_running` rejection also fires while a headless wait-handle resume is in flight for the conversation (`wait_resume.is_resuming`): a user send racing that resume would interleave appends on the same shared in-memory session and orphan tool_use blocks (every later Anthropic call then 400s). The reverse guard lives in `maybe_kick_resume`, which defers the resume while `get_active_send_run(conversation_id)` reports an in-flight send.

In practice this rejection is a backstop rather than the primary UX: `_run_resume` publishes the run-lifecycle envelopes `resume_started` / `send_message_finished` on the conversation channel (and mirrors transient events like `text_delta` via `_publish_transient_event`), so viewing tabs enter the same streaming/stop composer state as a local send for the resume's whole lifetime instead of offering a doomed send. See [Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume).

`_handle_stop`:

- cancels the entry in `_active_send_runs` (if any),
- cancels an in-flight headless resume for the conversation (`wait_resume.get_active_resume` -- the FE shows the same stop button during a resume, so stop must reach it too; the resume's `CancelledError` handler saves the interrupted SDK history and its `finally` appends the durable "interrupted" marker plus `send_message_finished {interrupted: true}`),
- and unconditionally calls `cancel_pending_wait_handles_for_conversation()` so a stop click on a sentinel-suspended run (where the model task already returned cleanly via `SuspendForWaitHandles` / `SuspendForSlackReply` / `SuspendForActionRequest`) still flips dangling rows to `cancelled`.

It replies a `stop_acknowledged` envelope so the client UI can clear its "thinking" spinner immediately. The cancel-time tail edit (`_save_interrupted_sdk_history`), session removal (`remove_chat_session`), and final flush all happen inside `_run_send_message`'s `CancelledError` handler, mirroring the per-turn `_handle_api_mode` semantics that were deleted from `chat/routes/_helpers.py`.

## Fresh User Per Turn

The persistent WS is held open for the whole browser session and is never re-handshaked when the user connects/disconnects a data source in Settings. Because of this, `_handle_send_message` in `chat/realtime/socket.py` re-reads the user fresh from the DB via `get_user_by_id(conn.user_id)` at the start of every `send_message` turn and threads that fresh dict into `_run_send_message` instead of the connect-time `conn.user` snapshot. It also refreshes `conn.user` so any later snapshot consumer benefits from the fresh read.

This is a correctness invariant, not an optimization: credential/connection-gated decisions made inside a run -- `system:*` skill gates, the connected-services enumeration in the system prompt, `authed_get` token loaders, and sub-agent spawns -- must never depend on connection-cached user state. The DB is authoritative and is read fresh each turn.

Without it, connecting a service (e.g. GitHub) after the tab's WebSocket had already opened had no effect until a full page reload -- `system:github` would fail to load with "GitHub is not connected" even though Settings > Data Connections showed it connected (that endpoint reads the user fresh from the DB on every request). The deleted per-turn WS endpoint re-authenticated (and thus re-read the row) on every turn, so this restores parity.

Failure handling: a missing user row (deleted account) rejects the send with `{type: "send_message_rejected", reason: "user_not_found"}`. A transient DB error falls back to the cached `conn.user` snapshot with a logged warning rather than dropping the send.

## Heartbeat and Auth

The server runs a 25s ping / 60s pong-deadline loop in `_heartbeat_loop`. Every `AUTH_REVALIDATE_INTERVAL_SECONDS = 300` the heartbeat also calls `_revalidate_auth`, which re-reads the session cookie via `get_user_from_cookie`. A revoked / rotated / expired cookie or a `user_id` change (impersonation rotated underneath us) closes the socket with code 4401; the client probes `GET /app/api/me` and either reconnects (still authenticated) or lets the existing `AuthContext` session-check flow take over.

The client runs its own watchdog independent of the OS TCP layer. `WATCHDOG_DEADLINE_MS = 60000` is the deadline; `lastFrameAt` is bumped on every inbound message; the watchdog ticks every 5s so background-tab `setTimeout` throttling still leaves room before the deadline. On deadline, `forceReconnect()` tears down the (probably half-open) socket and the existing `onclose` handler triggers reconnect. The client also sends its own `{op: "ping"}` every 25s so any reverse proxy / NAT idle timeout sees traffic in both directions; the server replies with `{type: "pong"}` and bumps its own `last_pong_at` so client-driven liveness counts.

Auth on the persistent WS is the same model as the deleted per-turn endpoint: signed session cookie first (honouring impersonation `imp` via `auth/session.get_user_from_cookie`), then `?api_key=` query param fallback, then `chat.auth.check_user_allowed` against the user's email. See [Admin Impersonation](admin-impersonation.md) and [Auth](auth.md).

## Backpressure

`SubscriberQueue.put_nowait` is non-blocking. On `QueueFull` the queue's `overflowed` (per-user channel) and `overflowed_conversations` (per-conversation channel) sticky flags are set. The writer loop checks them on the next drain:

- per-conversation overflow -- emits `{type: "resync", conversation_id, reason: "queue_overflow"}` and clears the marker so the client recovers via REST.
- per-user overflow -- emits an `{type: "error", code: 4503, error: "queue_overflow"}` and closes the socket; the client reconnects from scratch.

This bounds memory per slow consumer and keeps a stuck tab from holding up everyone else.

## Delivery Receipt and Failed Sends

`persistentWebSocket.send()` returns `false` when the socket is closed, and a half-open socket (a backgrounded phone with the radio off) accepts `ws.send` into the browser buffer for up to the 60s watchdog deadline without the frame ever reaching the server. Neither case produces a server reply, so before this mechanism an unsent message left the optimistic bubble and the stop button hanging until a reload silently wiped them, and the conversation stayed empty ("New Chat") on every other device.

Two pieces close that gap:

- **Server receipt.** `_handle_send_message` sends the SENDING connection `send_message_accepted {conversation_id, seq, client_send_id?}` immediately after `ChatStorage.append_message` returns and before the run task is created. `client_send_id` is an opaque string from the request envelope echoed verbatim (omitted when absent or not a non-empty string); `seq` is the user row's seq. No receipt is ever sent when the append fails (the existing `append_failed` error envelope goes out instead), so a receipt always means the row is on disk. `message_appended` still fans out to every subscriber as before; the receipt is the sender-only signal that its own frame arrived.
- **Client tracking.** `useConversation.sendMessage` stamps each optimistic bubble with a `client_send_id` and `WebSocketManager.sendMessage` keeps a `PendingSend` (the exact envelope) keyed by it.
  - A `send()` that returns `false` fails the send at once (`send_failed: "not_connected"`). Otherwise a 15s timer (`SEND_ACK_TIMEOUT_MS`) waits for the receipt; on expiry, if the bubble is still unstamped (no receipt AND no reconciling row via catchup / tail-fetch), the send is failed with `send_failed: "unconfirmed"`.
  - Failing a send ends the streaming state without the synthetic "interrupted" marker (same teardown as `send_message_rejected`) and leaves the bubble in place, dimmed, with an inline notice and **Retry** / **Discard** buttons (`Message.tsx`). Retry re-dispatches the identical envelope under the same `client_send_id` (bubble text preserved, failed state cleared while in flight); Discard drops the bubble and the tracking.
  - A receipt that lands after the failure (slow rather than lost) clears the failed state and re-enters the streaming state via `attachToResumeStream` so the run's events render. `send_message_rejected` removes the in-flight tracked bubble by id and leaves parked failed sends alone.

The `client_send_id` and `send_failed` fields are client-only and never persisted (the server ignores `client_send_id` beyond echoing it).

## Optimistic Reconciliation

The user-message bubble is rendered optimistically by `useConversation.sendMessage` with `optimistic: true` so the UI is responsive before the server's `message_appended` round-trip arrives. When the server-side `message_appended` event lands, the conversation hook fetches the new tail (the user message has now been stamped with a `seq`) and reconciles the optimistic placeholder in-place: same content, now with `seq` and timestamp, no duplicate. If the optimistic flag is missing on a `message_appended`-driven append (e.g. the tab is observing a turn driven by a different tab), the new bubble is folded into the messages list via `insertMessagesBySeq`.

`insertMessagesBySeq` (in `frontend/src/store/conversationStore.ts`) merges newly-fetched rows at their seq position, dedupes against existing seqs, and keeps non-seq'd optimistic placeholders pinned to the tail. This is what guarantees disk-order rendering when separate `message_appended` events trigger overlapping tail-fetches that resolve out-of-order -- previously the smaller-payload fetch could land first and the per-turn `stats` row would render above the assistant text until reload.

## Streaming-to-Finalized Swap

On the primary tab (the one driving the run), `WebSocketManager.handleMessageAppended` calls `conversationStore.finalizeStreamingTextInPlace(conversationId, seq)` whenever a `message_appended` event carries a `seq`. If `partialResponse` is non-empty -- i.e. an assistant text bubble was streaming -- this synthesises a persisted text row at the event's `seq`, marks it `synthetic`, inserts it at the seq-ordered position, and clears `partialResponse` in a single store update.

When the tail-fetch lands, `insertMessagesBySeq` replaces the synthetic entry in place at the same `seq` (preserving array index, React key, and DOM node) so React reconciles the canonical row's timestamp and metadata into the existing bubble instead of unmount + remount. Tool boundaries (where `partialResponse` is empty) and subscribed (non-primary) tabs (which never accumulate `partialResponse`) take the plain canonical-insert path with no synthetic.

## Sub-Agent Events

Sub-agent events (`sub_agent_tool_use`, `sub_agent_tool_result`, `sub_agent_finished`) are transient on the wire -- they ride the per-conversation channel during a live run. Persistence happens at the parent `tool_result` flush boundary: the boundary save in `_capturing_on_event` (see [Gemini API -- SDK History Persistence](gemini-api.md#sdk-history-persistence)) attaches the accumulated sub-agent events to the parent message's `sub_agent_tool_calls` array.

So a viewer who joins mid-`agent_task_parallel` misses the live sub-agent stream, but on the next `message_appended` for the parent `tool_result`, the REST tail-fetch hydrates the full sub-agent tree from the persisted message. Every event carries `parent_tool_id` + `agent_name` so the FE groups them under the right 1st-level agent row.

### Nested (2nd-level) Sub-Agent Events

When a 1st-level sub-agent spawns a 2nd-level sub-agent via `agent_task_nested` (only in a `nested_subagents` conversation -- see [Conversation Flags](conversation-flags.md) and [Gemini API -- Nested Sub-Agents](gemini-api.md#nested-sub-agents)), the same three event types carry extra fields so the FE can render the grandchild one extra indent level deep:

- The `agent_task_nested` call is emitted as a **NODE** `sub_agent_tool_use` (under the 1st-level agent's `parent_tool_id`) carrying `nested_agent_id` (stable id for the node + its grandchild events), `nested_agent_name`, and `nested_agent_model`. A matching NODE `sub_agent_tool_result` carries `nested_agent_id` + `nested_agent_status` (`success` / `error`) to flip the node from RUNNING to its terminal badge -- this resolves even when the grandchild never spawned (e.g. a rejected model).
- The 2nd-level agent's own `sub_agent_tool_use` / `sub_agent_tool_result` / `sub_agent_finished` events carry `nested_parent_id == nested_agent_id` so the FE nests its tool calls under the node rather than flat under the 1st-level row.

The field shapes are in `frontend/src/api/types.ts` (`SubAgentToolUseMessage`, `SubAgentToolResultMessage`, `SubAgentFinishedMessage`, `PersistedSubAgentEvent`). The live path (`frontend/src/services/WebSocketManager.ts`) and the reload-hydration path (`frontend/src/hooks/useConversation.ts`) both key a 2nd-level `sub_agent_finished` under `nested_parent_id` (falling back to `parent_tool_id` for normal 1st-level agents) so the nested node's badge resolves on reload. Rendering is in `frontend/src/components/ToolUseMessage.tsx` (see [Frontend Architecture](frontend.md)).

## Live Updates Replace Polling

Two former poll loops are now event-driven:

- The 30s `setInterval` on `GET /app/api/action-requests/counts` in `Sidebar.tsx`'s `RequestsBadge` (and the matching one in `RequestsView`) is removed. The badge mounts with a single `loadCount()` and then updates from `request_count_changed` events.
- The action-request card no longer needs to poll for resolved state from another tab; `wait_handle_resolved` events scoped by `request_id` flip the local state directly.

The conversation list still does an initial fetch on sidebar mount; the auto-refresh on stream complete now keys off `conversation_list_changed` events instead of `WebSocketManager.onStreamComplete`. Project-conversation polling in the project drill-down is unchanged (it still polls every 30s) because scheduled-routine creates do not currently round-trip through the bus from the scheduler context -- a future refinement.

The file browser auto-refresh similarly decouples from the streaming lifecycle. `WebSocketManager.onStreamComplete` (`send_message_finished`) and the dead `onToolResult` callback API previously fired the silent reload at end-of-turn boundaries; the FileBrowser now subscribes to `file_list_changed` directly and refreshes mid-turn after each workspace mutation. `send_message_finished` still fires for other end-of-turn UI (sidebar fallback refresh, ProjectTables silent reload) but does not drive the file browser.

## Message Attachments

The chat composer accepts PNG/JPEG images pasted from the clipboard and ships them on the next `send_message` as lightweight refs. The end-to-end is:

1. The FE paste handler in `frontend/src/components/ChatPanel.tsx` (`handlePaste`) filters `event.clipboardData.items` by `image/png` / `image/jpeg`, queues each as a `PastedImage` with a local `URL.createObjectURL` preview, enforces `MAX_COMPOSER_ATTACHMENTS = 10`, and surfaces inline rejection notices for unsupported MIME types.
2. On send, `handleSendMessage` two-phases: first `uploadComposerAttachments(conversationId, files)` in `frontend/src/api/fileApi.ts` POSTs the bytes to `POST /app/api/conversations/{id}/composer-attachments` (see [Chat API -- Composer Attachments](../api/chat-api.md#composer-attachments)); then the returned `ComposerAttachmentRef[]` rides on `WebSocketSendMessage.attachments` via `webSocketManager.sendMessage` and `persistentWebSocket.send`. Upload errors leave the queued thumbnails intact so the user can retry without re-pasting.
3. `_handle_send_message` in `chat/realtime/socket.py` re-validates each ref (PNG/JPEG only, no `..` in `workspace_path`, max 10), allows empty-text-with-attachments, then refuses the send with `send_message_rejected {reason: "attachments_unsupported_during_resume"}` when the conversation's saved SDK history still has a dangling tool_use -- folding pasted images into synthesised tool_results is out of scope. The rejection is detected by `provider.get_pending_tool_use_args_from_history` against `_load_sdk_history(conv_id)` and is wrapped in try/except (any detection failure falls through to the normal path).
4. The user-message append goes through `ChatStorage.append_message(..., attachments=...)` in `chat/storage.py`, which adds an `attachments` field to the message row on `chat_history.json` so a transcript reload re-renders the thumbnails.
5. `run_conversation_turn(..., attachments=...)` in `chat/gemini_api/conversation.py` builds a multimodal user turn via `_build_user_message_with_attachments`: resolve each `workspace_path` under the conversation workspace, call `provider.upload_file` (Anthropic returns the content-block dict directly; Gemini awaits the File API ACTIVE state), and wrap with `provider.make_file_part`. Upload failures (Anthropic's 5 MB image cap, Gemini File API errors) degrade to a text note that points the model at the workspace path so the file is still discoverable.
6. The transcript renderer in `frontend/src/components/Message.tsx` renders a `message-attachments` row of `<img>` thumbnails under the user bubble (skipped for assistant messages). Each thumbnail sources from `GET /files/download?path=...` and clicks into the existing `FileViewerModal` popover.

The bytes always land under `workspace/pasted/<attachment_id>.<ext>` so the on-disk layout is visibly distinct from the user-managed workspace root. The upload endpoint sniffs magic bytes and rejects on `mime_mismatch`; see [File Browser API](../api/file-browser-api.md) for how the file browser auto-refreshes after the upload (the endpoint publishes `file_list_changed` like the other workspace-mutating routes).

## Constraints

- **Single-process FastAPI deploy.** The bus is a plain `asyncio` singleton with no Redis. Multi-process scaling would require a cross-process fan-out layer; the Slack Socket Mode constraint already pins this deployment to a single process. See [Slack Socket Mode -- Constraints](slack-socket-mode.md).
- **Replay buffer is in-memory only.** Server restart drops every buffer. The DB `last_message_seq` survives, the chat history file survives, so reconnecting clients get `resync` and recover via REST.
- **Backpressure is per-subscriber.** A slow consumer is dropped (per-conversation: `resync`; per-user: socket close) rather than backpressured against the publisher; publishers never block on the bus.
- **`chat_history.json` writes are not multi-process safe.** Single-process deploy plus the per-conversation send lock plus the per-conversation flush lock make this a non-issue in practice; documented for completeness.

## Design Decisions

**Why a persistent multiplexed WS instead of per-turn sockets?**
Three problems forced the rewrite. First, the per-turn WS closed when the model suspended on a wait handle, so the headless resume's continuation could not be pushed -- the user had to reload. Second, Slack-driven conversations had no live channel to the web UI; a tab navigated to one saw zero progress while the bot ran. Third, the open-request badge polled every 30s. One persistent socket fixes all three with a single fan-out point and removes a polling loop.

**Why JSON file as seq truth instead of the DB?**
Two writes (file + DB) cannot be made atomic across processes, but the file write is the one a client ultimately reads bodies from. Stamping seq inside the same write that appends the body means seq and message ordering are atomic on disk. The DB cache is denormalised; if it falls behind on a write failure, the next append's `_read_seq_high_water` recovers it. Treating the DB as truth would require either an Alembic-backed migration of message bodies (awkward) or a window where seq is on disk but not in the DB (a subscribing client could be told `current_seq=N` while the file already shows `N+1`).

**Why durable + transient instead of pushing every event with a seq?**
Stamping every text delta with a seq would multiply append-points by orders of magnitude (every chunk versus every flush event), and a late subscriber needing to replay 5,000 deltas through the buffer would be useless anyway. The existing `FLUSH_EVENT_TYPES` boundary set already gives the natural durable-event cadence -- one seq per appended `chat_history.json` message -- and transients fold themselves into the next flushed text/tool message at the same boundary.

**Why a server-side per-conversation send lock instead of optimistic concurrency?**
Two tabs racing `send_message` for the same conversation would both append to `chat_history.json` and `sdk_history.json` and corrupt the dangling-tool_use shape the resume bucket depends on. The lock is the simplest correct primitive: the second tab gets `send_message_rejected` immediately and can disable its composer. A FIFO queue would complicate UX (long unexplained delays), and per-file optimistic concurrency would require restructuring the storage layer.

**Why a client-side watchdog on top of the server's heartbeat?**
A half-open WebSocket (NAT timeout, mobile sleep, mid-tunnel drop) can leave the OS TCP layer unaware that the path is dead -- outbound writes keep buffering. Without a client-side deadline on inbound frames the user sees a stale UI for minutes. The watchdog adds a 60s deadline that fires `forceReconnect` and triggers the existing reconnect / resubscribe / catchup flow.

**Why keep `requestEvents.ts` after the persistent WS arrived?**
The per-user `request_count_changed` round-trip is fast but not instant; emitting the local `emitRequestCountChange()` after the same-tab approve/revise/stop REST call still updates the badge immediately (idempotent with the server event arriving milliseconds later). Cross-tab events go through the persistent WS exclusively. A future refactor could drop the local bus once round-trip latency is measured.

**Why TTL-based subscription eviction instead of unsubscribing on tab switch?**
The original FE called `unsubscribe` from a React effect cleanup on every conversation switch. That dropped subs for the away-conversation, so transient `text_delta` events emitted while the user was on a different tab were silently discarded; coming back mid-stream produced an in-progress assistant message with missing middle chunks.

A TTL-with-refresh model keeps the sub alive across UI churn (the FE re-subscribes every 3 min while the conv is in its `subscribed` set, ahead of the 5-min server deadline) while still bounding server state -- any client that goes away without explicit teardown is GC'd within 5 min by the per-connection sweep. The explicit `unsubscribe` op survives for genuine teardown but is not on the lifecycle path.

**Why does the resolve endpoint still call `maybe_kick_resume` instead of relying on the bus?**
The bus is only a fan-out layer; it does not run the model. Resolving a wait handle just flips the DB row -- closing the dangling tool_use still requires running `run_conversation_turn(message="")` against the conversation. The persistent WS makes the resumed continuation visible to viewing tabs, but the kick itself is unchanged from the wait-handle architecture (see [Wait Handles](wait-handles.md)).
