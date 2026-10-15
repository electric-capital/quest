# Wait Handles

## Overview

Wait handles are a unified durable-suspend mechanism for tools that need human input. A tool that asks the user a question writes a row to the `tool_wait_handles` table; the dispatch arm then raises a sentinel exception that unwinds the model loop without emitting a `tool_result`. The dangling `tool_use` left on disk is closed on the next run by the resume bucket, which reads the (then-resolved) DB row.

The DB row is the source of truth. There is no in-process future registry, no live `notify_resolved` fast path, and no shutdown drain -- everything blocking on the user goes through the same suspend / resume cycle, so a server restart between registration and resolution is indistinguishable from a same-process resolution.

The mechanism currently backs four call sites:

- `wait_for_handles` -- generic tool surface for blocking on one or more outstanding handle ids. The `run_user_subagent` execute path returns a `user_subagent`-kind handle id that the caller's model blocks on this way (see [Cross-User Subagents](user-subagents.md)); otherwise the tool is kept for resume-bucket compatibility and future opt-in waits.
- `send_slack_reply_and_get_response` -- Slack-driven thread suspend (see [Slack Socket Mode](slack-socket-mode.md)). The reply is keyed implicitly by `(channel, thread_ts)` via the row payload, and the Socket Mode debounce flush resolves the row directly. Always blocking via `SuspendForSlackReply`.
- `create_action_request` -- inline Approve / Revise / Stop card. Always blocking via `SuspendForActionRequest`: the dispatch arm raises the sentinel after persisting the rows and emitting the websocket event, and the resume bucket closes the dangling `create_action_request` tool_use with the wait-handle row's `response` on resolve.
  - The resolve endpoint handles all three buttons and updates both the `action_requests` row and the linked wait handle; on Revise, the user's feedback rides on the wait-handle response so the resumed model can react to it.
  - Stop is the odd one out: the handle becomes terminal (`stopped`, so the composer unlocks and the inbox drops the row) but NO resume is kicked and `_run_resume()` holds on stopped rows -- the tool_use is closed only by the user's next send, with a `stopped` verdict and that message in the same turn.
  - Memory writes use `request_type="create_memory"` and ride on this path. See [Action Requests](action-requests.md).
- `return_to_caller` -- a cross-user subagent's return proposal. Rides on an `action_request`-kind handle and `SuspendForActionRequest` like the generic card, but its deny branch has bespoke semantics (Revise re-drives the subagent under `chat/user_subagent.py`'s control with the generic resume kick suppressed; hard Deny ends the run and never resumes the subagent conversation). See [Cross-User Subagents](user-subagents.md).

## Key Files

- `db/models.py` -- `ToolWaitHandle` ORM model; `ToolWaitHandleKind` (`slack_reply`, `action_request`, `user_subagent`); `ToolWaitHandleStatus`. The schema column is `String(50)` so adding kinds requires no migration. See [Database Architecture](database.md)
- `db/tool_wait_handle_store.py` -- async CRUD plus `find_pending_slack_reply(channel, thread_ts)`, `find_pending_for_action_request(user_id, request_id)`, `bulk_get_handles_by_ids`, `mark_timed_out`, `cancel_pending_for_conversation`, `get_handle_by_tool_id`, and the expiry helpers `set_expires_at` / `list_expired_pending`. `create_handle()` accepts optional `correlation_kind` / `correlation_id` for kinds where the linkage is known up front (used by `action_request`) and an optional `expires_at`
- `chat/gemini_api/turn_tools.py` -- `SuspendForWaitHandles`, `SuspendForSlackReply`, and `SuspendForActionRequest` sentinel classes; `_await_wait_for_handles`, `_build_wait_for_handles_result`; the per-tool dispatch handlers that register handles and raise the sentinels
- `chat/gemini_api/conversation.py` -- the top-level `run_conversation_turn()` catches all three sentinels and returns cleanly, and the resume bucket logic at the top of the same function closes the dangling tool_use on the next run
- `chat/wait_handles/wait_timer.py` -- deadline enforcement: the per-handle `asyncio.Task` timers, the wait-site overdue check, and the `expires_at` sweep (`expiry_sweep_loop`, `sweep_expired_handles`). See [Expiry](#expiry)
- `chat/wait_handles/resume.py` -- `maybe_kick_resume`, `_run_resume`, `_conversation_has_dangling_wait`, `_conversation_has_dangling_slack_reply`. Drives a fresh headless `run_conversation_turn()` with `message=""` and preserves `origin` + `slack_context` so Slack-origin resumes boot with the right tools
- `chat/wait_handle_routes.py` -- REST endpoints (`GET /app/api/wait-handles/{id}`, `POST /app/api/wait-handles/{id}/resolve`); always calls `maybe_kick_resume` on success
- `chat/action_request_routes.py` -- `POST /app/api/action-requests/{id}/resolve` resolves the linked `kind="action_request"` handle (via `find_pending_for_action_request`) and kicks `maybe_kick_resume` after the action_request row is flipped. See [Action Requests](action-requests.md)
- `chat/realtime/socket.py` -- the persistent-WS `{op:"stop"}` handler unconditionally calls `cancel_pending_wait_handles_for_conversation` (the `_run_send_message` task body's `CancelledError` arm does the same on its way out). Clients resolve handles only through the REST endpoints
- `chat/llm/tool_schemas.py` -- `wait_for_handles` tool spec (required `reason`, optional `timeout_seconds`, a capped `handle_ids` list)
- `alembic/versions/c1f2a3d4e5b6_create_tool_wait_handles.py` -- creates the table and its indexes
- `alembic/versions/3b9d4f7c2e10_cancel_pending_memory_suggestions.py` -- one-shot data migration that cancels any historical `pending` rows of retired kinds so they cannot strand resumes
- `frontend/src/components/ToolUseMessage.tsx` -- renders `Waiting for: <reason>` in place of `wait_for_handles`
- `frontend/src/api/types.ts` -- `WaitHandle` type (the endpoint URLs are in `frontend/src/api/config.ts`; no frontend code resolves handles through them)
- `quest.py` -- registers `wait_handle_router` and starts `expiry_sweep_loop` in the lifespan (cancelled on shutdown). No lifespan shutdown drain: pending rows survive restart and resume from DB on next boot

## Data Model

`tool_wait_handles` rows are owned by a user, scoped to a conversation, and identified by a UUID. The `kind` column discriminates between `slack_reply` (Slack-driven thread suspend) and `action_request` (inline Approve / Revise / Stop card); both share the same lifecycle.

`tool_id` is the LLM tool_use id of the registering call and is indexed so the resume bucket can look it up by tool id. `payload` carries kind-specific content (`{channel, thread_ts, posted_text, posted_ts}` for slack replies; `{request_id, request_type, params}` for action requests).

`response` is populated on resolve and carries kind-specific resolution data (the user's Slack reply text, or `{verdict, request_id, feedback?, result}` for action requests -- `feedback` is present at the top level on a Revise denial so the resumed model can read it directly without descending into `result`).

`correlation_kind` / `correlation_id` are populated **at create time** for `action_request` (the `action_requests.id` is in hand when the wait handle is inserted, so `find_pending_for_action_request()` can index the lookup by it). The `String(50)` `kind` column also accommodates legacy historical rows from now-retired kinds without a schema change.

`expires_at` is the row's deadline, enforced as described in [Expiry](#expiry). It is set at create time only for `user_subagent` (the 14-day `_CALLER_HANDLE_TTL` backstop in `chat/action_request_types/run_user_subagent.py`); a `wait_for_handles` call overwrites it with that wait's own deadline. `slack_reply` and `action_request` rows are created without one.

`ToolWaitHandleStatus` defines the lifecycle: `pending` -> `accepted` | `rejected` | `cancelled` | `timed_out` | `stopped`. Every transition out of `pending` (`resolve_handle`, `cancel_pending_for_conversation`, `mark_timed_out`) is a conditional `UPDATE ... WHERE status = 'pending'` per row, so when a resolve races a timeout the row's state in the DB picks the single winner and the loser no-ops. Indexes (`user_id`, `conversation_id`, `(user_id, status)`, `tool_id`) cover the access patterns of the resume path, the per-conversation cancellation, and the lookup-by-tool-id used by the resume bucket. See [Database Architecture](database.md) for the column-by-column schema and [Constraints](#constraints).

## Tool Surface

`wait_for_handles` is exposed via the standard `tool_call` meta-tool path; its declaration is in `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py`. The model passes `handle_ids` (non-empty, at most `_WAIT_FOR_HANDLES_MAX_IDS`), a required human-readable `reason` (rendered verbatim by the UI as `Waiting for: <reason>`; truncated at `_WAIT_FOR_HANDLES_MAX_REASON_LEN`), and an optional `timeout_seconds`. The caps and timeout bounds are constants at the top of `chat/gemini_api/turn_tools.py`.

The tool description steers the model to omit `timeout_seconds` for the normal human-in-the-loop case; omitted, it defaults to `_WAIT_FOR_HANDLES_DEFAULT_TIMEOUT`, which equals the maximum, so the call blocks until a handle resolves or the maximum elapses. When set, values are clamped to [`_WAIT_FOR_HANDLES_MIN_TIMEOUT`, `_WAIT_FOR_HANDLES_MAX_TIMEOUT`]. The tool returns `{"resolved": [...], "still_pending": [...], "unknown": [...]}` from a fresh DB read on every wake.

Tools that block on user input do **not** themselves use a model-facing tool to register a handle. They call `tool_wait_handle_store.create_handle()` from the dispatch arm (the `send_slack_reply_and_get_response` handler and the `create_action_request` handler in `chat/gemini_api/turn_tools.py`), append a corresponding structured message (`action_request` for action requests, or none for Slack replies -- the tool_use itself is the marker), emit any associated event, and then raise `SuspendForSlackReply` (Slack reply -- always blocking) or `SuspendForActionRequest` (action request -- always blocking; the model gets the verdict directly from the tool's return value on resume).

`wait_for_handles` is top-level only:

- **Sub-agents** -- excluded from the dynamic tools section because there is no UI channel to a sub-agent's user; the resume bucket logic also lives only in the top-level `run_conversation_turn()` loop.
- **Slack-driven runs** -- the `tool_call` arm short-circuits with a structured JSON error when `is_slack_origin` is true and the inner tool name is `wait_for_handles`. Slack runs use the implicit `slack_reply` wait handle registered by `send_slack_reply_and_get_response`; see [Slack Socket Mode](slack-socket-mode.md).

## Suspend / Resume

The dispatch arm of any blocking tool follows the same shape:

1. Insert a `pending` row via `create_handle()`.
2. Save `sdk_history.json` -- already done by `run_conversation_turn()` right after the model response that made the call, before dispatch; see [SDK History Persistence](gemini-api.md#sdk-history-persistence).
3. For `wait_for_handles`: time out any requested row whose `expires_at` has already passed (returning at once if nothing is left pending), persist the wait's deadline on the still-pending rows via `set_expires_at()`, schedule a per-handle expiry timer via `wait_timer.schedule_timeout_for_handles()`, then raise `SuspendForWaitHandles`. See [Expiry](#expiry).
   For `send_slack_reply_and_get_response`: post the Slack reply via `slack_driven_runtime.post_thread_reply()`, then raise `SuspendForSlackReply`.
   For `create_action_request`: emit the `action_request` websocket event and append the structured chat-history message, then raise `SuspendForActionRequest`. This sentinel alone is caught **inside** the dispatch loop's per-call arm: the loop records it, keeps dispatching the remaining calls of the same parallel batch (each further `create_action_request` opens another card + row; regular tools run and persist their `tool_result`), then re-raises the first sentinel after the batch with the other cards' handle ids on `sibling_handle_ids`.
4. The top-level `run_conversation_turn()` catches the sentinel, logs an info line, and returns the structured-messages list cleanly. No error event is emitted; this is a successful suspend, not a turn-end.

On disk, `sdk_history.json` ends on an assistant turn whose last content block is the dangling tool_use (`wait_for_handles`, `send_slack_reply_and_get_response`, or `create_action_request`). The `chat_history.json` flush events (`FLUSH_EVENT_TYPES` in `chat/_flush_helper.py`) persist the `tool_use` row and any card before the sentinel is raised.

Resolution writes the row first (first writer wins via the conditional update in `resolve_handle()`), then calls `wait_resume.maybe_kick_resume(app, user, conversation_id)`. Callers:

- `POST /app/api/wait-handles/{id}/resolve` (`chat/wait_handle_routes.py`) -- explicit accept/decline/cancel; no frontend code calls it, because the action-request resolve endpoint resolves its own handle.
- `POST /app/api/action-requests/{id}/resolve` (`chat/action_request_routes.py`) -- the Approve / Revise / Stop path; resolves the linked `kind="action_request"` handle and kicks the resume (Approve / Revise only -- Stop resolves the handle as `stopped` without a kick, and stops the conversation's other open cards too). `create_memory` action requests insert the new memory row inside the handler's `execute()` so the row id is on `result.memory_id`.
- `slack_driven_runtime._flush_after()` -- Slack debounce flush that resolves a `slack_reply` row with the collated user text.
- `wait_timer._expire_after()` and `wait_timer.sweep_expired_handles()` -- timeouts that flip `pending` to `timed_out` and kick resume so the model wakes with a final status (see [Expiry](#expiry)).

All resolution paths additionally publish `wait_handle_resolved` on the user channel so a viewing tab updates in place; the action-request path also publishes `request_count_changed` / `conversation_list_changed` as appropriate. See [Realtime Architecture](realtime.md).

`maybe_kick_resume` is dedupe-safe via a per-conversation `_active_resumes` map and is gated by `_conversation_has_dangling_wait`, which delegates to the provider (via the shared `_pending_tool_uses()` reader) to inspect `sdk_history.json` for a dangling `wait_for_handles`, `send_slack_reply_and_get_response`, or `create_action_request` tool_use. If the saved transcript has already moved past the wait, the kick is a no-op.

**Multi-card gate.** A parallel tool-call batch can leave several dangling `create_action_request` tool_uses, one pending row each. `_run_resume()` therefore starts with `_unresolved_blocking_handle_ids(user_id, conversation_id)`: the still-`pending` rows of kind `action_request` / `slack_reply` (the one-row-per-tool_use kinds; `wait_for_handles` rows belong to other tools and keep their any-resolves contract) whose `tool_id` is one of the dangling tool_uses.

While that list is non-empty the resume returns before publishing `resume_started` -- nothing streams, the composer stays locked on the remaining `pending_wait_handles` -- and the resolve of the last card kicks the resume that actually runs, so the model receives every verdict in one go.

Scoping by dangling `tool_id` means a stale pending row from an older turn cannot wedge the conversation; a failing check falls through to the pre-gate behavior.

Web-side runs are serialized per conversation: a headless resume and a persistent-WS send must never drive `run_conversation_turn` concurrently, because both append to the same shared in-memory session and interleaved appends orphan `tool_use` blocks mid-history (Anthropic then rejects every later call with a 400). Two cross-guards enforce this.

The WS `send_message` handler rejects with reason `already_running` while `wait_resume.is_resuming(conversation_id)` is true (mirroring the existing second-tab send lock), and `maybe_kick_resume` defers -- via a done-callback on the blocking task -- when `chat.realtime.socket.get_active_send_run(conversation_id)` reports an in-flight send.

The same done-callback mechanism re-kicks after an in-flight *resume* completes, so a resolution that lands while the previous resume task is still unwinding is re-checked rather than silently dropped (the re-entry no-ops via `_conversation_has_dangling_wait` when nothing is left to drive).

As defense in depth, `run_conversation_turn` calls `provider.repair_session_history()` right after obtaining the session: the Anthropic override closes orphaned mid-history `tool_use` blocks with a synthetic `interrupted` result and drops stray/duplicate `tool_result` blocks in place (a dangling tool_use on the *final* message is left for the resume bucket), so a conversation whose history was corrupted before the guards shipped self-heals on its next run. Slack-driven runs keep their own registry-based mutual exclusion in `slack_driven_runtime._active_runs`.

`_run_resume()` reads the conversation's stored `origin` and -- for `origin="slack"` -- the matching `slack_conversations` row, so the headless `run_conversation_turn(message="", origin=..., slack_context=...)` boots with the right tool set. Slack-origin resumes register the task in `slack_driven_runtime._active_runs` so `is_run_active()` reflects the in-flight resume, and the typing indicator is set on resume start and cleared in `finally` (or left alone if the run suspends again on another `slack_reply`).

`_run_resume()` also publishes the same run-lifecycle envelopes as a WS send on the conversation channel: `resume_started` on entry and `send_message_finished {interrupted, error}` in its `finally` (the pair is emitted inside the coroutine, so a task cancelled before its first tick emits neither and a client can never latch a streaming state with no terminal event). Transient events (`text_delta`, sub-agent updates) are mirrored via `chat.realtime.socket._publish_transient_event`, exactly like `_run_send_message`.

The FE reacts to `resume_started` by entering the normal streaming/stop composer state (`webSocketManager.attachToResumeStream`), so after an action-request Approve / Revise the composer shows the stop button (a card Stop kicks no resume, so the composer simply unlocks) -- not an enabled input whose send would bounce off the `already_running` guard -- until the resume finishes.

The WS `stop` handler cancels an active resume via `wait_resume.get_active_resume(conversation_id)`; the cancelled resume saves the interrupted SDK history, appends the durable `interrupted` marker, and reports `send_message_finished {interrupted: true}`.

The resume bucket at the top of `run_conversation_turn()` walks the dangling tool_uses, looks up each one's `tool_id` in `tool_wait_handles`, and closes them:

- `kind="slack_reply"` -- closed with `{"user_reply": ..., "posted_ts": ...}` from the row's `response` and `payload`. If the row is somehow still `pending` when the resume runs, a `{"status": "still_waiting"}` marker is used instead.
- `kind="action_request"` -- closed with the row's `response` dict verbatim: `{"verdict": "executed", "request_id": N, "result": {...}}` for Approve, `{"verdict": "denied", "request_id": N, "result": {"denied": true}}` plus a top-level `feedback` field (mirrored inside `result.feedback`) for Revise.
  - A `stopped` row (Stop) is closed with its `{"verdict": "stopped", ...}` response plus a `note` built by `_stopped_action_request_result()` saying the request was not executed and that the user's new message follows in this turn (a different note when the run carries no message).
  - If the row is still `pending` when the resume runs (race with the resolve endpoint), a `{"status": "still_waiting"}` marker is used instead.
  - This branch matches before the `wait_for_handles` catch-all so an action_request row never funnels through `_build_wait_for_handles_result`.
- Any `wait_for_handles` / `tool_call(tool_name="wait_for_handles")` tool_use, with or without a row -- closed with `_build_wait_for_handles_result(user_id, handle_ids)` from a fresh DB read. A model that emits a follow-up `wait_for_handles(handle_ids=[<id>])` after a blocking `create_action_request` short-circuits here: the row is already terminal, so the live arm returns the resolved-row projection immediately without registering a new suspend.
- Any other dangling tool_use with no wait-handle row -- closed with its own persisted `tool_output` replayed from `chat_history.json` (`_load_persisted_tool_results()` in `chat/gemini_api/conversation.py`) when the call did run but the batch unwound on a sibling `create_action_request` suspend before `format_tool_results`; otherwise (cancellation / restart mid-tool) with the synthetic `{"status": "interrupted", ...}` marker. Provider-side extra parts (e.g. a Gemini file part) are not replayed, only the text output.

When the resume runs with `message=""` and there is no dangling tool_use, the loop bails before invoking the model so it does not run a turn against empty user input.

`cancel_pending_for_conversation()` flips every still-pending row in the conversation to `cancelled`. The persistent-WS `{op:"stop"}` handler in `chat/realtime/socket.py` calls it unconditionally so a stop click on a sentinel-suspended run (where the API task already returned successfully) does not leave rows pending with nobody to resolve them.

## Expiry

A pending row times out once its `expires_at` passes. Three paths in `chat/wait_handles/wait_timer.py` enforce this. Each flips rows through `mark_timed_out()`, then publishes `wait_handle_resolved` (`status="timed_out"`) on the owner's channel and kicks one `maybe_kick_resume` per affected conversation:

- **Timers** -- `schedule_timeout_for_handles()` starts one in-memory `asyncio.Task` per handle when a `wait_for_handles` call suspends; a later wait on the same handle replaces it. A timer that fires after the row was resolved no-ops.
- **Wait-site check** -- before suspending, the `wait_for_handles` arm passes the requested pending rows to `expire_overdue_handles()`. Overdue rows time out on the spot, so the model reads `timed_out` instead of suspending on them; the calling conversation gets no kick because its live run reads the status directly.
- **Sweep** -- `expiry_sweep_loop()`, started in the `quest.py` lifespan, runs `sweep_expired_handles()` at startup and then every `EXPIRY_SWEEP_INTERVAL_SECONDS`. It lists every pending row whose deadline has passed (`list_expired_pending()`), times them out per owner, loads the owner and kicks the resume. The sweep enforces a deadline whose timer died with the process, and is the only enforcement for a deadline set at create time (the `user_subagent` backstop). An overdue row stays pending for at most one interval.

The sweep and the wait-site check pass `expired_by=now` to `mark_timed_out()`, so a row times out only if the DB still holds it as pending with a passed deadline: a handle resolved, or given a later deadline, after the sweep read it is left alone, and overlapping passes cannot announce one row twice. `set_expires_at()` never moves a deadline that has already passed, so a new wait cannot revive an overdue row.

`_build_wait_for_handles_result()` is a pure read. A row the sweep has not reached yet reads as `still_pending`; once the sweep flips it, its kick wakes the conversation if it is still suspended on a wait.

## Composer Sync

The web composer is locked on any conversation whose suspended turn still has a `pending` wait-handle row, regardless of which tab loaded the conversation or whether a stream is in flight. The gate is driven by the DB row, not by per-tab streaming state, so a fresh conversation load (Requests-pane navigation, page reload, second tab) lands with the composer already disabled.

Two channels keep the FE in sync:

- `GET /conversations/{id}` returns a `pending_wait_handles` array projected from `tool_wait_handle_store.list_pending_for_conversation`, with `{id, kind, correlation_id, created_at}` per row. `chat/routes/conversations.py` populates the field; `chat/storage.py` carries it on `ConversationDetail`. `useConversation` seeds `conversationStore.pendingWaitHandles[conversationId]` from this field on load and on every resync refetch, and also derives entries from newly-arrived open `action_request` messages in tail / catchup paths so a same-tab `create_action_request` locks the composer at the flush boundary without a round-trip through the detail endpoint.
- `wait_handle_resolved` events on the persistent WebSocket -- now also emitted by `cancel_pending_wait_handles_for_conversation` in `chat/gemini_api/session.py` with `status="cancelled"` for each row it flips, so the persistent-WS `{op:"stop"}` handler unlocks the composer across tabs without waiting for a tail refetch -- call `removePendingWaitHandle` in the FE on every termination (accept / reject / cancel / timeout). See [Realtime Architecture](realtime.md).

`conversationStore` exposes `pendingWaitHandles: PendingWaitInfo[]` per conversation with mutators `setPendingWaitHandles` / `removePendingWaitHandle` and the selector `hasPendingWaitHandle(conversationId)`. `useConversation` re-exports `hasPendingWait` and `pendingWaitKind`; `ChatPanel` ORs `hasPendingWait` into `inputDisabled` and uses `pendingWaitKind === "action_request"` to swap the textarea placeholder to "Approve, revise, or stop the pending request to continue.". The same gate also blocks paste. Slack-origin conversations were already read-only via the `origin === "slack"` gate; the new OR makes no observable difference there but covers `slack_reply` waits if `origin` gating is ever relaxed.

## REST Endpoints

`chat/wait_handle_routes.py` exposes:

- `GET /app/api/wait-handles/{handle_id}` -- returns the row scoped to the authenticated user (404 if not found or owned by someone else).
- `POST /app/api/wait-handles/{handle_id}/resolve` -- body fields are defined by `ResolveWaitHandleRequest` in the same file: `status` (must be `accepted`, `rejected`, or `cancelled`), optional `response` dict, optional `correlation_kind` / `correlation_id`, optional `feedback` (merged into `response`). Returns 422 (request validation) on any other status, 404 if missing, 400 if already resolved, 409 if a concurrent resolver (or a timeout) won the row first. On success, always calls `maybe_kick_resume`. See [Chat API](../api/chat-api.md) for the full endpoint catalog.

The `POST /app/api/action-requests/{id}/resolve` endpoint in `chat/action_request_routes.py` is the primary client of the wait-handle resolve path: it bridges to `tool_wait_handle_store.resolve_handle()` and `maybe_kick_resume()` after flipping the `action_requests` row. See [Action Requests](action-requests.md).

## Constraints

- The `tool_wait_handles` table is created by `alembic/versions/c1f2a3d4e5b6_create_tool_wait_handles.py`. The `kind` column is `String(50)`, so adding a kind is a code-only change.
- `wait_for_handles` limits are the `_WAIT_FOR_HANDLES_*` constants in `chat/gemini_api/turn_tools.py` (see [Tool Surface](#tool-surface)); the resume path applies the same id cap as the live arm.
- Per-handle expiry timers in `wait_timer.py` are in-memory `asyncio.Task`s and are lost on process restart; the persisted `expires_at` and the sweep enforce those deadlines after a restart, up to one sweep interval late. See [Expiry](#expiry).
- Headless resume is durable across restarts and pushes the streamed continuation to viewing tabs via the persistent multiplexed WebSocket. The same `make_flush_callback` that drove the originating run wires through `_run_resume`, so each `chat_history.json` append publishes `message_appended` to every subscriber on the conversation channel; a tab open on the conversation REST-fetches the new tail and renders without a reload. See [Realtime Architecture](realtime.md).
- `wait_for_handles` is excluded from sub-agent tool sets and from Slack-driven runs; the dispatch arms short-circuit with a structured error before any wait-handle row is inserted. See [Route Dispatch](route-dispatch.md) and [Slack Socket Mode](slack-socket-mode.md).

## Design Decisions

**Why a single sentinel-and-resume mechanism instead of three separate paths?**
Each blocking site uses one sentinel-exception-plus-DB-row pattern: one suspend protocol, one resume bucket, one cancellation primitive, one set of restart-resilience invariants. `create_action_request` and `send_slack_reply_and_get_response` raise their own sentinels (`SuspendForActionRequest`, `SuspendForSlackReply`) so the resume bucket can match by row `kind`. Every register site that wakes the model blocks directly -- there is no synchronous-pending opt-in case where the model has to remember to call `wait_for_handles`.

**Why no in-process future registry?**
Tying wait state to the lifecycle of an in-memory future couples resolution to the lifecycle of the process. A restart, an OOM, or a coroutine cancellation between registration and resolution silently drops the wait. With the DB row as truth and resume always re-spawning a fresh `run_conversation_turn`, same-process and cross-restart resolutions take exactly the same code path -- there is no fast path to keep correct.

**Why a sentinel exception instead of `await`-ing inside the dispatch arm?**
The dispatch arm runs deep inside the streaming-response loop. To suspend cleanly it must unwind the streaming generator, the function-call collection, and the per-turn flush plumbing without emitting a `tool_result` (which would close the dangling tool_use the resume bucket depends on). Raising an exception that the top-level loop catches is the cleanest way to express "stop this turn, leave a dangling tool_use, return cleanly." Inheriting from `Exception` rather than `BaseException` keeps the sentinels out of `CancelledError`'s lane.

**Why does every register path block directly instead of returning a handle id?**
The Slack Reply Mode prompt ends every turn by calling the reply tool and waiting for the user's next message, and an action request always carries a Revise button whose feedback would have nowhere to land if the model didn't wait. Returning a handle id synchronously would make the model responsible for remembering to call `wait_for_handles`; blocking lets it read the verdict directly off the tool's return value.

**Why per-handle `asyncio.Task` timers instead of an `asyncio.wait(timeout=...)` arm?**
Without an in-process future to wait on, there is nothing to attach a timeout to inside the dispatch arm. A small background task per handle gives the same behaviour (mark `timed_out` and wake the conversation on expiry) without coupling the timer to the lifetime of any particular suspend. Resolution before the deadline makes the task a no-op when it eventually fires, which keeps the contract narrow -- timers do not need to be eagerly cancelled on resolve.

**Why a DB sweep as well as the timers?**
A timer dies with the process, so on its own a restart would leave a timed wait pending forever, and a deadline set at create time (the `user_subagent` backstop) has no timer at all. Persisting the deadline in `expires_at` and sweeping it at startup and on an interval makes the deadline as durable as the row. The timers stay because they fire on time, while the sweep can be up to one interval late.

**Why does `wait_for_handles` take a bounded timeout instead of blocking indefinitely?**
An unbounded wait would let a forgotten handle pin a suspended conversation forever. The maximum (`_WAIT_FOR_HANDLES_MAX_TIMEOUT`) is on the order of weeks, long enough that a request the user only sees the next morning still wakes the original arm, and it is also the default because the realistic horizon for human-in-the-loop work is days; the model passes a smaller value only when it deliberately wants to give up sooner.

**Why does the REST resolver always kick a headless resume?**
There is no live model loop to notify. The dispatch arm raised the sentinel and returned; closing the dangling tool_use requires running the model again. `maybe_kick_resume` is dedupe-safe via `_active_resumes` and is gated on the on-disk transcript actually still ending on a dangling wait, so it is a cheap no-op when there is nothing to drive (e.g., the model already moved past the wait, or two near-simultaneous Accept clicks race).

**Why does the headless-resume continuation reach the open tab?**
The resolve endpoint runs from any HTTP context (another tab, another device, a Slack callback) and has no handle on a viewer's socket, so delivery cannot come from it. Instead `_run_resume` uses the same `make_flush_callback` as the originating run, which publishes `message_appended` on the per-conversation channel after every disk append. A viewing tab subscribed via the persistent socket fetches the new tail via REST and renders the resumed continuation in place. See [Realtime Architecture](realtime.md) and [Slack Socket Mode -- Web UI Behavior](slack-socket-mode.md#web-ui-behavior).

**Why require `reason` on `wait_for_handles`?**
The chat UI shows `Waiting for: <reason>` in place of the raw tool name, so the value is user-facing. Making it required forces the model to articulate what the user is being asked to do. Empty strings are rejected at validation; oversize strings are silently truncated rather than rejected because the wait should never fail to register over a cosmetic length issue.
