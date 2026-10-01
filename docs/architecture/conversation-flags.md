# Conversation Flags

## Overview

Per-conversation flags are a small, extensible registry of opt-in behaviors set at the *start* of a conversation (on the first user message) and persisted onto the `Conversation` row as a JSON array of enabled flag names.

There are two activation paths, both honored only on the first web message and merged into one set: a **composer Flags popover** (checkboxes that ride out-of-band on the `send_message` payload's `flags` field) and a magic first line of the form `%%flags[name,...]` (a comma-separated list inside the brackets).

The magic line is parsed, the recognized flags are persisted, and the line is stripped from the text sent to the model and from the cached sidebar title -- but kept verbatim in the persisted/displayed message bubble so it survives copy-paste into a new conversation. The out-of-band field carries no text and so needs no stripping. See [Composer UI](#composer-ui-out-of-band-selection) below.

Two flags exist today: `nested_subagents`, which lets 1st-level sub-agents spawn one tier of 2nd-level sub-agents (see [Gemini API](gemini-api.md)), and `user_subagents`, which lets the conversation propose cross-user subagent runs via `create_action_request(request_type="run_user_subagent")` -- without it the dispatch arm rejects that request type same-turn (see [Cross-User Subagents](user-subagents.md); the flag gates only the caller side, never the launched subagent conversation).

A flag can additionally be **feature-gated**: `user_subagents` also requires the matching server-global admin feature gate to be on (see [Feature Gates](feature-gates.md)). While the gate is closed the composer popover hides the flag, `_handle_send_message` drops it from both activation paths via `filter_gated_flags()` before persistence, and the conversation loop rejects the proposal even on rows that persisted the flag while the gate was open.

## Key Files

| File | Description |
|------|-------------|
| `chat/conversation_flags.py` | The flags registry. `FLAG_NESTED_SUBAGENTS`, `KNOWN_FLAGS` (frozenset of recognized names), `FLAG_LABELS` (canonical human label + description per flag, the source the FE constant mirrors), `parse_flags_line()` (first-line magic-line parser), `is_flag_enabled()` (NULL-safe membership check). Import-light (regex + stdlib only) so both the WS layer and the conversation loop can reuse it without pulling in the LLM stack |
| `db/models.py` | The nullable `flags` JSON column on `Conversation` (NULL == no flags). See [Database Architecture](database.md) |
| `db/conversation_store.py` | `set_conversation_flags()` (idempotent set-only-if-not-already-set), `get_conversation_flags()` (NULL-safe read), `_conversation_to_dict()` emits `flags` |
| `chat/realtime/socket.py` | `_handle_send_message` validates the out-of-band `flags` field, does the first-message magic-line parse/persist/strip, unions the two into `merged_flags`, and persists first-message-only; `_run_send_message` threads `flags` into `run_conversation_turn`. See [Realtime Architecture](realtime.md) |
| `chat/routes/conversations.py` | `get_conversation_endpoint()` surfaces the persisted `flags` array on `GET /conversations/{id}` (NULL == `[]`) so the FE can render the read-only label after the first message and across reloads. See [Chat API](../api/chat-api.md) |
| `frontend/src/constants/flags.ts` | Hand-mirrored FE copy of the registry: `AVAILABLE_FLAGS` (id + label + description, mirroring `KNOWN_FLAGS` + `FLAG_LABELS`) and `getFlagLabel()`. Static like `constants/models.ts` (not fetched); adding a flag requires touching both this file and `chat/conversation_flags.py` |
| `frontend/src/components/Composer.tsx` | The shared composer (used by both `ChatPanel` and the root `HomeComposer`) owns the Flags button + checkbox popover (fresh conversation only) and the post-send read-only "N flag(s) enabled" label with hover tooltip; passes selected flags into the host's `onSend` first-message-only. On the home screen the flags are carried into the first send via `NavigationContext.pendingFirstMessage` -> `ChatPanel`'s auto-send effect (see [Frontend -- Home Screen](frontend.md#home-screen-homecomposer)) |
| `frontend/src/hooks/useConversation.ts` | Hydrates `conversationFlags` from the `GET` response and forwards the `flags` arg through `sendMessage` to the WS manager |
| `chat/storage.py` | `ChatStorage.append_message` / `append_structured_messages` derive the sidebar `auto_title` from a flags-stripped copy of the first user message |
| `chat/gemini_api/conversation.py` | `run_conversation_turn` takes `flags` and resolves `nested_subagents` + `user_subagents` via `is_flag_enabled()` |
| `chat/wait_handles/resume.py` | The resume path loads flags via `get_conversation_flags()` so a suspended turn honors the same behaviors. See [Wait Handles Architecture](wait-handles.md) |

## Flags Registry

`KNOWN_FLAGS` in `chat/conversation_flags.py` is the authoritative set of recognized flag names. Tokens in a `%%flags[...]` line (or in the out-of-band `flags` field) that are not in this set are silently dropped, so both activation paths stay forgiving and forward-compatible. Only recognized flags are persisted.

`FLAG_LABELS` in the same module is the canonical human display registry (label + description per flag) used by the composer popover; the frontend hand-mirrors it in `frontend/src/constants/flags.ts` (`AVAILABLE_FLAGS`) -- there is no endpoint, so adding a future flag requires touching **both** files plus `KNOWN_FLAGS` and a consumer.

Aside from that FE/BE drift caveat, adding a flag is a small change: define a constant, add it to `KNOWN_FLAGS` and `FLAG_LABELS`, mirror it in `AVAILABLE_FLAGS`, and wire up a consumer; the storage column and the parser need no change.

## First-Message Flag Parsing

The magic line is parsed only on the first web message, at the single send entry point. A `%%flags[...]` line in any later message (or in any non-web first message) is treated as literal text.

1. The user sends a message → `chat/realtime/socket.py` (`_handle_send_message`). `is_first_message` is computed as `meta["last_message_seq"] == 0` (a reliable signal because the user message has not been appended yet).
2. On the first message only, `parse_flags_line(user_message)` is run. The parser looks ONLY at the first line; if it matches `%%flags[<tokens>]` (regex `_FLAGS_LINE_RE` in `chat/conversation_flags.py`), it returns `(recognized_flags, stripped_message)` where `recognized_flags` is the deduped, lowercased subset present in `KNOWN_FLAGS` and `stripped_message` is the body with the magic line (and its single trailing newline) removed. Non-matching first lines return `([], message)` unchanged.
3. **Empty-after-strip guard**: if stripping leaves no real prompt and there are no attachments, the send is rejected (error code `4400`) and flags are NOT persisted. The empty-check runs before persisting so a rejected send never leaves stray flags on the row.
4. Recognized flags are persisted via `set_conversation_flags()` (after the send-lock / resume gates pass). The setter is idempotent: a no-op for an empty list, a missing row, or a row that already has flags.
5. Three distinct copies of the message diverge:
   - **Persisted/displayed bubble** keeps the ORIGINAL text (magic line intact) so it copy-pastes cleanly into a new conversation -- `ChatStorage.append_message(content=original_message)`.
   - **Model input** uses the STRIPPED text so the LLM is never instructed by the `%%flags` syntax -- `_run_send_message(user_message=model_message)`.
   - **Sidebar title** is derived from a flags-stripped copy inside `ChatStorage.append_message` / `append_structured_messages`, so the sidebar never shows the magic syntax. `parse_flags_line` is a no-op for any normal message, so this stripping is safe to apply unconditionally.

## Composer UI (Out-of-Band Selection)

The composer toolbar carries a **Flags** button (flag icon) next to the `+ Skill` button. On a fresh conversation (`messages.length === 0`) clicking it opens a popover of human-readable checkboxes -- one per entry in `AVAILABLE_FLAGS` (`frontend/src/constants/flags.ts`), each showing the flag's label and description. The selection is local UI state in the shared `Composer.tsx` (`selectedFlags`), reset on conversation switch and closed on outside click.

On the first send (and only the first -- `messages.length === 0`), the selected flag ids ride **out of band** on the `send_message` WebSocket payload as the optional `flags` field (a list of strings), threaded `Composer` onSend -> `ChatPanel.handleSendMessage` -> `useConversation.sendMessage` -> `WebSocketManager.sendMessage` (which omits the field when empty).

On the root home screen the same `Composer` lives in `HomeComposer`, which has no live conversation yet, so the selected flags are stashed on `NavigationContext.pendingFirstMessage` and replayed through `ChatPanel`'s auto-send effect on the first send (see [Frontend -- Home Screen](frontend.md#home-screen-homecomposer)).

This is distinct from, and parallel to, the `%%flags` magic line, which remains a fallback (e.g. for copy-pasted prompts). See [Realtime -- send_message](realtime.md).

Backend handling is in `_handle_send_message` (`chat/realtime/socket.py`): the `flags` field is validated to be a list of strings (lowercased/trimmed, intersected with `KNOWN_FLAGS`, deduped, unknowns and malformed shapes dropped -- a non-list is treated as no flags), then unioned on the first message only with the `%%flags` parse result into `merged_flags`. That merged set is persisted via the existing idempotent `set_conversation_flags()` and threaded into the run (see below). The out-of-band field carries no prompt text, so it bypasses the strip / empty-after-strip machinery entirely.

After the first message the Flags button is replaced by a read-only `"N flag(s) enabled"` text label with a native hover tooltip (`title=`) listing the enabled flags' labels (`getFlagLabel`); it renders nothing when zero flags are enabled. The label prefers `conversationFlags` (hydrated from `GET /conversations/{id}`, which now returns the persisted `flags` array -- see [Chat API](../api/chat-api.md)) and falls back to the `Composer`-local `selectedFlags` while the first send round-trips, so it survives reload.

## Flag Threading Through the Run

Flags are read from the DB on every turn (not just the first), so the effective flag set is threaded into the run rather than re-parsed.

`_handle_send_message` passes `meta["flags"] or merged_flags` (existing row flags win over the flags just merged off this message -- magic line plus out-of-band selection) into `_run_send_message`, which forwards them to `run_conversation_turn(flags=...)`.

The conversation loop resolves `nested_subagents` from the array and threads it into the system-prompt builder and the sub-agent spawn arms (see [Gemini API](gemini-api.md)). The wait-handle resume path has no `meta` dict, so it queries `get_conversation_flags()` directly to preserve behavior across suspend/resume.

## Scope and Constraints

- **Web-only**: both activation paths live at the web send entry point (`_handle_send_message`) -- the out-of-band `flags` field originates only from the web composer, and the magic line is parsed only here. Slack-driven conversations (Socket Mode worker) and routine/scheduler-created first messages set no flags; supporting them would be a follow-up.
- **Start-of-conversation only**: flags are set once on the first message and never changed thereafter (the setter is set-only-if-not-already-set). This makes per-conversation system-prompt divergence (e.g. the nested sub-agent prompt note) stable for the conversation's lifetime.
- **Failure handling**: `set_conversation_flags` failures are non-fatal -- the turn still runs with the in-memory merged flags so the requested behavior is honored.
- **FE/BE registry drift**: the composer popover's label/description text is a static hand-mirror (`frontend/src/constants/flags.ts`) of `FLAG_LABELS` in `chat/conversation_flags.py`, not fetched from an endpoint (mirroring the `constants/models.ts` pattern). The two can drift; adding a flag requires editing both.
