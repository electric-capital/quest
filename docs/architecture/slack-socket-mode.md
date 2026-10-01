# Slack Socket Mode

## Overview

Quest runs a Slack Socket Mode client as a background worker tied to the FastAPI lifespan.

The worker receives Events API envelopes over a WebSocket authenticated by an App-Level Token and turns user DMs to the bot into fully-driven Quest conversations: a top-level DM starts a new conversation (`origin="slack"`), threaded replies continue it, and the model answers by calling a dedicated tool that posts a Slack reply and suspends the run via a `kind="slack_reply"` wait-handle row.

When the user replies in that thread, the Socket Mode worker resolves the row and kicks a headless resume so the model gets the next turn. See [Wait Handles Architecture](wait-handles.md) for the unified suspend / resume mechanism.

## Key Files

- [`chat/slack_socket_mode.py`](../../chat/slack_socket_mode.py) -- Socket Mode client, lifespan hooks, DM filter, top-level/threaded dispatch, and the headless model-run coroutine (`_dispatch_slack_model_run`) that calls `run_conversation_turn` with `origin="slack"`
- [`chat/slack_driven_runtime.py`](../../chat/slack_driven_runtime.py) -- Shared runtime state for in-flight Slack runs: `AsyncWebClient` reference, app reference for resume kicks, debounce buffer per `(channel, thread_ts)`, active-run registry, typing-indicator helper, and `shutdown()` drain. The debounce flush also publishes `wait_handle_resolved` on the user channel so any viewing web tab updates in place. Pending `slack_reply` wait-handle rows are NOT touched on shutdown -- they survive restart and resume from DB on next boot
- [`chat/slack_conversation_store.py`](../../chat/slack_conversation_store.py) -- `slack_conversations` table access (`create_slack_conversation`, `get_slack_conversation`, `list_slack_conversations_for_user`)
- [`chat/storage.py`](../../chat/storage.py) -- `ChatStorage.create_slack_conversation()` creates the Quest conversation row (with `origin="slack"`) plus its `slack_conversations` mapping and on-disk directory
- [`chat/gemini_api/conversation.py`](../../chat/gemini_api/conversation.py) -- `run_conversation_turn(origin, slack_context, ...)` wires the Slack-only tool schema and the Slack system prompt branch; the `send_slack_reply_and_get_response` dispatch handler lives in `chat/gemini_api/turn_tools.py`
- [`chat/gemini_api/system_prompt.py`](../../chat/gemini_api/system_prompt.py) -- `get_system_prompt(is_slack=True)` appends the Slack Reply Mode section (`_SLACK_REPLY_MODE_SECTION`)
- [`chat/gemini_api/session.py`](../../chat/gemini_api/session.py) -- `get_or_create_chat(tools=...)` lets the Slack caller swap `TOP_LEVEL_TOOLS` for `SLACK_TOP_LEVEL_TOOLS`
- [`chat/llm/tool_schemas.py`](../../chat/llm/tool_schemas.py) -- `_SEND_SLACK_REPLY` tool spec and the `SLACK_TOP_LEVEL_TOOLS` tier (top-level only; not exposed to sub-agents)
- [`chat/routes/conversations.py`](../../chat/routes/conversations.py) -- `GET /conversations/{id}` surfaces `origin`, `slack_channel_id`, `slack_thread_ts`, and `slack_team_id` (`default_team_id` from the user's `user_service_credentials` slack row's `oauth_blob`) so the web UI can gate the composer and build a native Slack deep link
- [`chat/routes/user.py`](../../chat/routes/user.py) -- `PUT /settings` accepts `slack_default_model`; validated against `MODEL_REGISTRY`, empty string normalised to NULL
- [`frontend/src/utils/slackLinks.ts`](../../frontend/src/utils/slackLinks.ts) -- `buildSlackThreadUrl()` constructs the `slack://channel?team=...&id=...&message=...` native deep link
- [`frontend/src/components/ChatPanel.tsx`](../../frontend/src/components/ChatPanel.tsx) -- renders the Slack-thread banner above the locked composer and the disabled model selector for Slack-driven conversations
- [`frontend/src/components/Sidebar.tsx`](../../frontend/src/components/Sidebar.tsx) -- `SlackConversationIcon` rendered next to conversation titles when `conversation.origin === 'slack'`
- [`frontend/src/components/settings/SlackSection.tsx`](../../frontend/src/components/settings/SlackSection.tsx) -- Settings > Slack section with the default-model dropdown (listed only while the user's Slack connector row is connected; see [Settings Data Connections](settings-data-connections.md))
- [`db/models.py`](../../db/models.py) -- `Conversation.origin` column and the `SlackConversation` ORM model
- [`db/conversation_store.py`](../../db/conversation_store.py) -- `create_conversation(origin=...)` and `_conversation_to_dict` including `origin`
- [`db/user_store.py`](../../db/user_store.py) -- `get_user_by_slack_user_id()` resolves a Quest user by json_extracting `$.user_id` from the `user_service_credentials` slack row's `oauth_blob` (written by the Slack plugin's OAuth callback)
- [`auth/config.py`](../../auth/config.py) -- `load_slack_socket_mode_token()` (App-Level Token) and `load_slack_bot_token()`; the loaders stay core (the worker needs them) even though the `slack` credential store entry is registered by the Slack plugin (see [Plugins](plugins.md))
- [`quest.py`](../../quest.py) -- `lifespan()` calls `slack_socket_mode.start(app)` and `slack_socket_mode.shutdown()` wrapped so Slack-side failures cannot block boot or shutdown
- Migration [`alembic/versions/e58a2bf14c01_add_slack_conversations_table_and_origin.py`](../../alembic/versions/e58a2bf14c01_add_slack_conversations_table_and_origin.py) -- adds `conversations.origin` and creates `slack_conversations`

## Lifecycle

1. Startup: `lifespan()` in `quest.py` imports `chat.slack_socket_mode` and calls `start(app)` inside a try/except
2. `start()` loads the App-Level Token; returns early with an INFO log when absent (graceful degradation -- the rest of the server still boots)
3. `start()` loads the bot token (catches the `HTTPException` raised when missing and treats it as "Socket Mode disabled"), constructs an `AsyncWebClient`, installs it on `slack_driven_runtime` via `set_web_client()`, calls `auth_test` to cache the bot's own `user_id`, then opens the Socket Mode connection
4. Shutdown: `lifespan()` calls `slack_socket_mode.shutdown()`, which first invokes `slack_driven_runtime.shutdown()` (drains pending futures, cancels debounces, cancels active runs) and then closes the Socket Mode client and resets module state. No-ops when `start()` never ran or returned early

## DM Filtering

`_handle_socket_mode_request()` acks every envelope first (Slack redelivers unacked envelopes and eventually drops the connection), then filters down to plain user-sent DMs before dispatching:

- `req.type == "events_api"`
- `event.type == "message"`
- `event.channel_type == "im"`
- No `subtype` (drops edits, deletions, file_share, bot_message, channel_join, etc.)
- No `bot_id`
- `event.user` is not the bot's own cached `user_id`

Message text is truncated to `_MAX_TEXT_LOG_CHARS` (500) when logged. All handler exceptions are caught so they cannot leak back into the SDK.

## User Resolution

For every accepted DM, `_handle_socket_mode_request()` resolves the Quest user via `get_user_by_slack_user_id(slack_user_id)` in `db/user_store.py`, which joins `user_service_credentials` (service `slack`) and matches `json_extract(oauth_blob, '$.user_id')`. The same per-user Slack OAuth record the Slack plugin's `/auth/slack` callback stores (see the [Slack plugin doc](../../plugins/slack/docs/slack-api.md)) is reused -- no additional onboarding step is required. DMs from Slack users with no linked Quest account are dropped with a warning.

## Dispatch: Top-Level DM vs Threaded Reply

The handler classifies an accepted DM by whether its `thread_ts` is unset or equal to its own `ts`:

**Top-level DM** -- `_handle_top_level_dm()`:
1. `ChatStorage.create_slack_conversation()` creates a new conversation with `origin="slack"`, writes the initial `chat_history.json`, inserts the `conversations` row, and inserts the `slack_conversations` row keyed by `(slack_channel_id, slack_thread_ts=ts)`
2. The user's message is appended to `chat_history.json`
3. Best-effort typing indicator via `slack_driven_runtime.set_typing()`
4. Kicks off `_dispatch_slack_model_run()` as an asyncio task registered with `slack_driven_runtime.register_run()`

**Threaded reply** -- `_handle_threaded_reply()`:
1. Looks up the thread in `slack_conversations`; drops silently when the thread root is not tracked (e.g., a reply to a bot-authored message in an unrelated thread)
2. Enforces ownership: the replying Slack user must map to the same Quest user that owns the conversation
3. `slack_driven_runtime.enqueue_user_reply()`:
    - Queries `tool_wait_handle_store.find_pending_slack_reply(channel, thread_ts)`. If a pending row exists (the model is suspended on `send_slack_reply_and_get_response` for this thread), buffers the reply and schedules a fresh debounce timer. Returns `True`. The socket handler then only persists the user's message to `chat_history.json` -- the debounce flush resolves the row and kicks `wait_resume.maybe_kick_resume()`
    - If no pending row exists, returns `False`
4. If no pending row but `slack_driven_runtime.is_run_active(conversation_id)` is true, the reply is dropped with a log line (avoids launching a concurrent second run against the same conversation)
5. Otherwise, the reply becomes the next turn: persist the user's message and start a new `_dispatch_slack_model_run()` task

## Headless Model Run

`_dispatch_slack_model_run()` mirrors the pattern used by the scheduler (see [Scheduling Architecture](scheduling.md)) but with three Slack-specific knobs:

- `origin="slack"` and `slack_context={"channel_id", "thread_ts"}` are passed to `run_conversation_turn` so the system prompt switches to Slack Reply Mode and the blocking tool knows where to post
- Incremental flushing: the caller passes a shared `messages_out` list plus an `on_event` callback built from `make_flush_callback()` in `chat/_flush_helper.py`, which writes newly-appended structured messages to `chat_history.json` on the events in `FLUSH_EVENT_TYPES` (`tool_use`, `tool_result`, `action_request`, `stats`). The boundary save hook in `run_conversation_turn()` keys off the same set so `sdk_history.json` advances together. This is critical because the model suspends inside `send_slack_reply_and_get_response` for potentially hours -- without incremental flushing the web UI would show only the initial user message on refresh
- Cancellation: the task is registered in `slack_driven_runtime._active_runs`; `shutdown()` cancels it, the coroutine catches `CancelledError`, flushes what it has, and re-raises

The conversation's model is read from `db/conversation_store.get_conversation_meta()` so threaded replies reuse the same model the conversation was first run with. The model for a newly-created Slack conversation is chosen by `_handle_top_level_dm()`; see [Per-User Default Model](#per-user-default-model) below.

## Per-User Default Model

Users can pick a preferred model for Slack-driven conversations via Settings > Slack (the section is listed only while the user has connected Slack). The value is stored as `slack_default_model` inside the existing `users.settings` JSON column -- no schema migration. See [Database Architecture](database.md) for the `User.settings` column. This is fully separate from the per-user web/composer default `default_model` (also in `users.settings`; see [Frontend](frontend.md) ConversationModelsContext): different settings key, different UI, different read path, and they are never coupled.

- **Write path**: `PUT /app/api/settings` (`chat/routes/user.py`) validates the candidate against `chat/llm/config.MODEL_REGISTRY` and returns HTTP 400 on unknown IDs. An empty string is normalised to `NULL` (clears the preference so the server default applies again).
- **Read path**: `_handle_top_level_dm()` in `chat/slack_socket_mode.py` reads `settings.slack_default_model` when creating a new Slack-driven conversation. Unknown/stale IDs (e.g., after a model is retired from the registry) are silently dropped to `None`, and `ChatStorage.create_slack_conversation()` falls back to the server-default model. Threaded replies are unaffected -- they reuse the conversation's already-persisted `model`.
- **UI**: `frontend/src/components/settings/SlackSection.tsx` renders the dropdown (options derived from `AVAILABLE_MODELS` in `frontend/src/constants/models.ts`, plus a "Server default" empty-value option). The disabled selector on the read-only `ChatPanel` (see [Disabled Model Selector](#disabled-model-selector) above) uses the same "Server default" label for consistency.

## Blocking Reply Tool

Defined as `_SEND_SLACK_REPLY` in `chat/llm/tool_schemas.py` and exposed only via `SLACK_TOP_LEVEL_TOOLS` (top-level session only; sub-agents do not see it):

The dispatch handler (`_handle_send_slack_reply()` in `chat/gemini_api/turn_tools.py`) validates the text, posts a threaded reply via `slack_driven_runtime.post_thread_reply()`, inserts a `kind="slack_reply"` `tool_wait_handles` row whose payload carries `{channel, thread_ts, posted_text, posted_ts}`, and raises `SuspendForSlackReply`.

The top-level loop catches the sentinel, returns cleanly, and leaves a dangling `send_slack_reply_and_get_response` tool_use on disk (the boundary save in `_capturing_on_event` already persisted `sdk_history.json` at the `tool_use` event for this call).

When the user replies, the debounce flush in `slack_driven_runtime._flush_after()` resolves the row with the collated `user_reply` and calls `wait_resume.maybe_kick_resume()`; the resume bucket reads the row and closes the dangling tool_use with `{"user_reply": ..., "posted_ts": ...}`.

`MAX_REPLY_CHARS = 3000` caps outgoing text (Slack's Assistant thread limit). Oversize text is rejected before posting with a structured error so the model can retry with a shorter message. There is no in-tool timeout: the wait-handle row stays pending until the user replies, the deadline-equivalent timer fires (currently not configured for slack_reply), or `cancel_pending_for_conversation()` flips it on conversation stop / cancellation. See [Wait Handles Architecture](wait-handles.md).

## Restart Resilience

A Slack-driven run can stay suspended inside `send_slack_reply_and_get_response` for hours or days waiting for the user's next reply. The Slack Reply Mode prompt tells the model to end every turn by calling the blocking tool, so the in-memory session never reaches a clean turn-end -- the suspension always lands on a dangling `send_slack_reply_and_get_response` tool_use, with the matching `slack_reply` wait-handle row holding the durable state.

**Save invariant: the flush-event boundary** (see [SDK History Persistence](gemini-api.md#sdk-history-persistence)). The `tool_use` event for `send_slack_reply_and_get_response` fires before the dispatch arm posts the Slack reply or inserts the wait-handle row, so by the time `SuspendForSlackReply` is raised, `sdk_history.json` already ends on the dangling-tool_use shape the resume bucket closes.

**Cancellation tail edit.** `_dispatch_slack_model_run()` in `chat/slack_socket_mode.py` catches `CancelledError`, calls `_save_interrupted_sdk_history()` for any partial-text tail edit, and then calls `remove_chat_session()` so any follow-up turn in the same process rebuilds cleanly from disk. `_save_interrupted_sdk_history()` skips the tail edit when the on-disk last entry already ends on a dangling tool_use; appending text there would leave an unmatched tool_use followed by user text, which Anthropic rejects with `"tool_use ids were found without tool_result blocks immediately after"`.

**Resume.** When the user replies in the thread, `enqueue_user_reply()` finds the pending `slack_reply` row, schedules a debounce timer, and on flush resolves the row with the collated `user_reply` and calls `wait_resume.maybe_kick_resume()`.

The resume runs a fresh `run_conversation_turn()` with `message=""`, `origin="slack"`, and the `slack_context` reconstructed from the `slack_conversations` row; the resume bucket at the top of `run_conversation_turn()` looks up each dangling tool_use's `tool_id` in `tool_wait_handles` and closes the `send_slack_reply_and_get_response` tool_use with the resolved row's `{user_reply, posted_ts}`. See [Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume) for the unified bucket logic.

`_handle_threaded_reply()` logs a warning when `sdk_history.json` is missing at resume time. This should only affect Slack conversations created before the persistence fixes landed.

## Debounced User Replies

`slack_driven_runtime.enqueue_user_reply()` coalesces rapid successive user replies in the same thread. Each accepted reply appends to a per-`(channel, thread_ts)` buffer and schedules a fresh `_flush_after(channel, thread_ts, DEBOUNCE_SECONDS=1.5)` task, cancelling the prior one.

When the timer fires, the buffered parts are joined with `\n\n` and `_flush_after()` resolves the matching `slack_reply` wait-handle row with `{user_reply: <collated>, posted_ts}` and calls `wait_resume.maybe_kick_resume()` so the suspended run wakes up. This avoids spamming the model with tiny back-to-back turns when the user sends "send-send-send"-style messages.

The debounce buffer is per-process; if it is lost across a restart, the next reply just opens a fresh debounce window against the still-pending wait-handle row.

## Slack Reply Mode System Prompt

`get_system_prompt(is_slack=True)` appends `_SLACK_REPLY_MODE_SECTION` (defined at module top of `chat/gemini_api/system_prompt.py`). The section instructs the model to:

- Produce no direct text output (only the tool reaches the user)
- Call `send_slack_reply_and_get_response` exactly once per turn after all tool calls and thinking are done
- Format `text` using Slack mrkdwn (`*bold*`, `_italic_`, `` `code` ``, ` ```blocks``` `, `<url|label>`), not GitHub-flavored markdown
- Treat `create_action_request` as unavailable in Slack mode (its approval UI lives in the web app, and the call would otherwise block on a card the Slack-only user cannot see). Deliver write-action proposals -- including memory writes, which now go through `create_action_request(request_type="create_memory", ...)` -- in the reply text instead

See the constant for the full text. The web UI composer is locked read-only (see Frontend below), so these rules are the only thing keeping the model from emitting dead plain-text turns.

## Web-UI-Bound Tools Disabled

Slack-origin runs cannot use `wait_for_handles` or `create_action_request` because their confirmation / approval UIs live in the (read-only) web composer. Both layers of the gate live in the same file-set as the rest of Slack Reply Mode:

- **Prompt hygiene**: `_build_dynamic_tools_section()` in `chat/gemini_api/system_prompt.py` omits both tools from the Dynamic Tools enumeration when `is_slack=True` (via the `top_level_exclude` set built in `get_system_prompt`). `_SLACK_REPLY_MODE_SECTION` tells the model not to call them, to acknowledge memory requests in the Slack reply (noting that persisting memories requires the web UI), and to deliver write-action proposals in the reply text instead
- **Behavioural gate (defense in depth)**: the top-level dispatch handlers in `chat/gemini_api/turn_tools.py` short-circuit with a structured JSON error when `is_slack_origin` is true, before any wait-handle row is inserted. This applies to both the `wait_for_handles` arm and the `create_action_request` arm.
  - Without the `create_action_request` gate, a Slack run could suspend on `SuspendForActionRequest` waiting for an Approve / Revise / Stop click on a card the user cannot see, deadlocking the conversation. With it, the model gets a structured error and can fall back to delivering the proposal in the Slack reply text.
  - Memory writes -- which now ride on `create_action_request(request_type="create_memory", ...)` -- inherit this gate automatically

See [Wait Handles Architecture](wait-handles.md) for the wait-handle mechanism used in web conversations.

## Web UI Behavior

`GET /conversations/{id}` returns `origin`, `slack_channel_id`, `slack_thread_ts`, and `slack_team_id` so the frontend can render the conversation read-only. See [Frontend Architecture](frontend.md) for the `ChatPanel` gating details. Tool calls and user replies are flushed to `chat_history.json` incrementally, and the same `make_flush_callback` wiring publishes `message_appended` on the persistent-WS conversation channel after each append. A web tab subscribed to a Slack-driven conversation streams the bot's progress live, including resumed turns after a `slack_reply` resolution -- no reload required. See [Realtime Architecture](realtime.md).

### Sidebar Marker

`ChatStorage.list_conversations()` and `list_project_conversations()` include each row's `origin` in the returned dicts. `Sidebar.tsx` renders a small Slack icon (`SlackConversationIcon`) next to the title for every entry where `origin === 'slack'` in all three listing surfaces: the top-level conversations list, routine sub-items, and the project-drilled ungrouped list. Slack-origin conversations are hidden from the sidebar by default and only appear when "Show Slack Conversations" is checked in the per-section filter popover; see [Conversation List Filter](frontend.md#sidebar-component-srccomponentssidebartsx) in the Sidebar Features section of Frontend Architecture.

### Native Slack Thread Link

When the conversation is Slack-driven and the composer is locked, `ChatPanel.tsx` renders a banner above the input reading "Replying is disabled -- use Slack: Open Slack thread". The link is built by `buildSlackThreadUrl()` in `frontend/src/utils/slackLinks.ts` using the `slack://channel?team=<TEAM_ID>&id=<CHANNEL_ID>&message=<THREAD_TS>` URI scheme so clicking hands off to the installed desktop app. When `slack_team_id` is absent (e.g., the user's Slack OAuth record predates the `default_team_id` field), the banner still renders the disabled-text but without the clickable link.

### Disabled Model Selector

Slack-driven conversations show a **disabled** model selector that displays the conversation's persisted `model` (see [Database Architecture](database.md) for `conversations.model`). The selector uses a "Server default" sentinel option when the stored value is NULL, matching the label used in the Slack settings section. Model IDs that are no longer in `AVAILABLE_MODELS` (retired or unknown) are still rendered by ID so the user can see exactly what the conversation is running on. The normal (editable) model/guide/skill row is hidden for these conversations.

## Constraints

- **Single connection per App-Level Token**: Slack permits only one active Socket Mode WebSocket per App-Level Token. When multiple Quest instances share credentials (e.g., a developer instance and a production instance), only set `slack.socket_mode_token` on the instance that should receive DMs
- **Slack app configuration**: Socket Mode requires specific Slack app scopes and settings (Messages Tab on, `im:history` + `im:read`, `message.im` event subscription, App-Level Token with `connections:write`, `chat:write` for `chat.postMessage`). See [Slack App Setup](../../plugins/slack/docs/slack-app-setup.md)
- **`assistant.threads.setStatus` scope optional**: Absent or unauthorized, the typing indicator self-disables; everything else keeps working
- **Graceful degradation**: Missing App-Level Token or bot token causes `start()` to return early; the rest of the server boots normally
- **Concurrent replies while a run is executing**: Threaded replies that arrive while a run is in-flight but no `slack_reply` wait-handle row is pending are dropped with a log line, to avoid concurrent runs against the same conversation

## Typing Indicator Timing

The Slack typing indicator (`assistant.threads.setStatus`) is set whenever the bot is actively processing and cleared whenever it isn't. Slack also auto-clears the indicator on every `chat.postMessage`. The timing rules:

- Top-level DM dispatch and threaded-reply dispatch set "Quest is thinking..." / "Quest is working..." right before launching the model run. The dispatch task clears it in `finally` when the run truly ends -- but skips the clear when the run suspended again on another `slack_reply` (detected via `_conversation_has_dangling_slack_reply()`), because Slack already cleared it on the chat.postMessage.
- The `send_slack_reply_and_get_response` dispatch arm does **not** re-set "Quest is working..." before raising `SuspendForSlackReply`. Re-setting there would race the Slack auto-clear-on-postMessage and strand a stale indicator throughout the user-wait period.
- The headless resume in `chat/wait_handles/resume.py` re-asserts "Quest is working..." on resume start (Slack cleared it on the previous reply's postMessage) and clears it in `finally` -- again skipping the clear when the run suspended again.

`slack_driven_runtime.set_typing()` self-disables on the first error so apps without the Assistant feature / scope see no errors, just no indicator.

## Design Decisions

**Why a blocking tool instead of one-shot "fire a reply and exit"?**
Slack DMs are inherently multi-turn. By suspending the run on a `slack_reply` wait-handle row, the model keeps its entire tool state (memory of prior tool calls, working set, sub-agent results) across Slack turns for as long as the user stays engaged. A one-shot reply would force the model to re-acquire context on every user message.

**Why a wait-handle row + sentinel instead of an in-process future?**
The same rationale as the rest of [Wait Handles Architecture](wait-handles.md): a slack_reply suspend can outlast the process. Storing the suspend state in the DB and resuming via a fresh `run_conversation_turn()` after the row resolves makes restart-resilience indistinguishable from same-process resolution and unifies the bookkeeping with `wait_for_handles`.

**Why ack before filtering?**
Slack requires a response within ~3 seconds. Acking first avoids redelivery storms and connection drops when the handler takes time or the envelope is uninteresting. Identical rationale to the original log-and-drop worker.

**Why a separate `SLACK_TOP_LEVEL_TOOLS` tier rather than always exposing the tool?**
Exposing `send_slack_reply_and_get_response` in web conversations would give the model a tool it cannot meaningfully use (no `slack_context`) and that conflicts with normal text output. Building a Slack-only tier keeps the web-mode tool set unchanged. Sub-agents never see the tool either; only the top-level agent speaks to Slack.

**Why debounce user replies in the resolver rather than resolving the row on the first reply?**
Users frequently send a thought in multiple rapid Slack messages. Without debouncing, each would start its own turn, each of which would produce its own reply, thrashing the thread. 1.5s strikes a balance between coalescing and responsiveness. The debounce buffer lives in `slack_driven_runtime` and is purely a per-process accelerator; losing it on restart is fine because the next reply just opens a fresh debounce window against the still-pending wait-handle row.

**Why drop threaded replies that arrive with an active run and no pending wait-handle row?**
The model may have returned (turn ended) without calling the blocking tool, or may be mid-tool-call. Starting a second run would race with the first against the same conversation file, SDK history, and `(channel, thread_ts)` mapping. Logging-and-dropping is a pragmatic safety valve; in practice the reply typically arrives after the run fully completes, which follows the "no active run" path and starts cleanly.

**Why incrementally flush `chat_history.json` on tool events?**
A Slack-driven run can stay suspended inside the blocking tool for hours. Flushing only at the end would mean refreshing the web UI shows a blank conversation until the run eventually exits. Flushing on each tool event gives the user a live transcript on reload, and -- since the same flush callback now publishes `message_appended` to the persistent WS -- streams the conversation live without a reload to any tab subscribed to the conversation. See [Realtime Architecture](realtime.md).

**Why look up the bot's own `user_id` and filter on it?**
Slack Events API does not reliably set `bot_id` on messages posted via `chat.postMessage` with a user-level token in every path, so ignoring only `bot_id` messages can loop the bot on its own replies. Caching `auth_test().user_id` and dropping events where `event.user == _bot_user_id` is a belt-and-suspenders check on top of the `bot_id`/`subtype` filter.
