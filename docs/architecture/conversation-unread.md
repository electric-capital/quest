# Conversation Unread State

## Overview

A conversation is unread from the moment a run on it ends until a client shows it in its main view in a focused window. The sidebar marks unread rows with a dot. The state is server-side, so it is the same on every device and survives reloads; one client reading a conversation clears the dot on all of them.

## Key Files

- `db/models.py` (`Conversation.unread_seq`, `Conversation.read_seq`) -- the state. The conversation is unread while `unread_seq > read_seq`.
- `db/conversation_store.py` -- `mark_conversation_unread()` and `mark_conversation_read()`, the only writers; `UNREAD_ORIGINS` decides which conversations take unread state.
- `chat/unread.py` -- `mark_unread_after_run()`, the hook the run paths call, and `publish_changed()` for the realtime event.
- `chat/routes/conversations.py` (`mark_conversation_read_endpoint`) -- the client's acknowledgement.
- `frontend/src/store/unreadStore.ts` -- client-side merge of the seqs and the acknowledgement of the conversation on screen (header comment).
- `frontend/src/hooks/useConversationUnread.ts` -- the React bindings: event and focus wiring, the main view reporting its conversation, the row's dot state.
- `frontend/src/components/sidebar/ConversationRow.tsx` -- renders the dot.

Tests: `tests/test_conversation_unread.py`, and `unreadStore.test.ts`, `useConversationUnread.test.tsx` and `ConversationRow.test.tsx` beside the frontend files.

## When a Conversation Becomes Unread

`mark_unread_after_run()` sets `unread_seq` to the row's `last_message_seq`. It is called at the end of a run, after the final flush:

- the WS send task (`_run_send_message` in `chat/realtime/socket.py`),
- the headless wait-handle resume (`_run_resume` in `chat/wait_handles/resume.py`),
- a scheduled routine run (`_execute_scheduled_run` in `chat/scheduler.py`).

A run ends when the model finishes its output, when the turn fails (the error row is something to read), and when the model suspends on an action request or another wait handle, since a suspend returns from `run_conversation_turn` like a finished turn. Nothing marks a conversation unread mid-run.

Two cases are excluded:

- **A run the user stopped.** They were looking at it, and the interrupted marker is not news.
- **Conversations read elsewhere** (`UNREAD_ORIGINS`): Slack, inference API and cross-user subagent conversations are read-only in the web UI and consumed in another place, so a dot on them would never clear in normal use.

In both send paths the call sits before the `send_message_finished` publish, because nothing may await between that publish and the run leaving its registry ([Realtime -- Run-State Reconcile on Subscribe](realtime.md#run-state-reconcile-on-subscribe)).

## When a Conversation Becomes Read

Only a client can say the user saw something, so reading is an explicit acknowledgement: `POST /conversations/{id}/read` with the seq the client is acknowledging. `mark_conversation_read()` clamps it to `last_message_seq` and never lowers `read_seq`.

The client sends it when all three hold (`acknowledgeViewed()` in `unreadStore.ts`):

1. the conversation is the one `ChatPanel` shows and it has loaded (`useMarkConversationViewed`),
2. the document is visible and has focus,
3. the store knows the conversation as unread.

An admin impersonating the owner never acknowledges: `useUnreadTracking` switches the store's acknowledging off and the endpoint changes nothing for an impersonated session ([Admin Impersonation](admin-impersonation.md)), because the admin looking is not the owner reading.

The store re-checks whenever one of them changes: a conversation comes on screen, the window gains focus or visibility, or new seqs arrive. A conversation open in an unfocused window therefore stays unread until the window is focused.

## Realtime

Both writers publish the per-user global `conversation_unread_changed {conversation_id, unread_seq, read_seq}` when a seq moved (`make_conversation_unread_changed()` in `chat/realtime/events.py`). It carries a `conversation_id`, so it is on the frontend's global allow-list ([Realtime -- Subscription Protocol](realtime.md#subscription-protocol)).

The seqs reach a client from four sources: list rows, the conversation detail, the event, and every `subscribed` ack (`_handle_subscribe`), which covers an event published while the socket was down. `unreadStore.merge()` folds each in with `max`, and a row's dot is computed from the row and the store together.

When the event names the conversation on screen in a focused window, the store acknowledges inside the same `merge()` call, before notifying listeners, so that client never renders the dot. Other clients of the same user show it for the round trip of the acknowledgement.

## Constraints

- Existing conversations start read: both columns default to zero (Alembic migration `d4b8f1a6c3e7`).
- Only conversation rows show unread state. A project row and a collapsed routine-run group give no sign of unread conversations inside them.
- A dot that appears from the event does not re-sort the list; the row moves when the list next refreshes ([Frontend -- Sidebar](frontend.md#sidebar-component-srccomponentssidebartsx)).

## Design Decisions

**Why two monotonic seqs instead of an `unread` boolean?**
A boolean is order-dependent on both sides. On the server, an acknowledgement sent before a later run ended would clear the newer output. On the client, a list fetch that started before a read and resolved after it would bring the dot back. With seqs that only move forward, every writer and every merge is a `max`, so late, repeated and reordered reports are harmless.

**Why an explicit acknowledgement instead of marking read on `GET /conversations/{id}`?**
Loading is not seeing: a background tab and an unfocused window load and stay subscribed, and the tail and resync paths fetch without the user present. Only the client knows whether its window has focus.

**Why not have clients report what they are viewing, so the server skips marking unread?**
That would avoid the brief dot on a user's other clients, but a half-open socket (a sleeping laptop, a phone that lost signal) keeps its last report until the heartbeat times out, and the server would silently drop an unread mark for output nobody saw. Acknowledging after the fact fails the other way: the worst case is a dot that should have been cleared.
