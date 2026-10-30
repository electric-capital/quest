# Doc Note Suggestions

## Overview

A model never writes a Quest Doc's [notes](doc-notes.md): it proposes a change with `suggest_doc_note` ([Doc Notes Tools](doc-notes-tools.md#suggest_doc_note)), and a person accepts or dismisses it. `doc_note_suggestions` holds proposed changes to either part of the notes, each reviewed by the user whose conversation proposed it, with the `MemorySuggestionStatus` lifecycle of [project memory suggestions](project-memories.md): an instructions suggestion carries the complete proposed text, a memory suggestion one fact, optionally replacing a memory. This doc covers the suggestion store, registration, review, the suggestion event, where the rows are counted and listed, the review surfaces in the frontend, and what happens to pending rows when their doc, conversation or share goes.

## Key Files

| File | Role |
|------|------|
| `db/models.py` | `DocNoteSuggestion` |
| `db/doc_note_suggestion_store.py` | The `doc_note_suggestions` rows; the module docstring gives the kinds, the supersede rule and the one-transaction accept |
| `chat/docs/note_suggestions.py` | The `suggest_doc_note` body, accept, dismiss, the review rows and the `doc_note_suggestions_changed` publisher; the module docstring states who may suggest and review |
| `chat/docs/access.py` | `DocAccess.deny_cause`, the machine cause of a denied verdict, which the suggestion gate reads |
| `chat/docs/note_suggestion_routes.py` | The review routes ([Quest Docs API -- Note Suggestions](../api/quest-docs-api.md#note-suggestions)); the module docstring gives the check order and the errors |
| `chat/project_routes.py`, `chat/routes/user.py` | The project and account deletes, which capture the pending suggestions on the docs they delete and publish for them afterwards |
| `chat/memory_inbox.py`, `chat/routes/conversations.py`, `chat/feature_usage_report.py` | Where doc note suggestions join the other memory suggestions: the inbox's `doc` kind, `pending_doc_note_suggestions`, the admin Memories report |

## Suggestion Store

`db/doc_note_suggestion_store.py` is the doc-notes twin of `db/project_memory_suggestion_store.py`, sharing its status lifecycle, `SuggestionAlreadyResolvedError` and resolved-list keyset cursor, and `chat/docs/note_suggestions.py` is the twin of `chat/project_memories.py`. The reviewer is always the proposing conversation's user (`user_id`); every store function but the cross-user reports is scoped to that user. An instructions suggestion records the `instructions_version` it was written against (`base_instructions_version`).

- **Supersede**: a user has at most one pending instructions suggestion per doc, so they never review two competing full texts. `create_suggestion()` dismisses the older one, from any of the user's conversations, in the insert's own transaction, under SQLite's write lock taken as the [memory writers](doc-notes.md#concurrency) take it; the store's module docstring gives what that serializes.
- **Content** is normalized by the notes stores' own functions, so the suggestion caps count characters exactly as the notes caps do.
- **`SuggestionTargetGoneError`**: the doc or the conversation went before the insert; the service reports it as a missing doc.

## Registration

`register_suggestion()` is the `suggest_doc_note` body; its docstring lists the checks in order and the result statuses. What the order encodes:

- **Taint** (`suggestion_allowed()` in `chat/docs/notes.py`): only a conversation whose write verdict on the doc is `free` or `approval`, or `denied` with one of the `SLACK_DENY_CAUSES` (matched on `DocAccess.deny_cause`), may suggest, so a private conversation can never put text into a public doc's notes. Any other denied verdict answers with its `deny_reason`, and a hidden doc with the missing doc's text, exactly as the doc tools do.
- **Owner-only instructions**: an instructions suggestion also needs the conversation's user to own the doc (`instructions_owner_only`; [Doc Notes -- Design Decisions](doc-notes.md#design-decisions)).
- **Seen first**: the conversation must have seen every current note, memories included (`needs_delivery()`, stricter than the write refusal's `write_refused()`; [Doc Notes -- Delivery Gate](doc-notes.md#delivery-gate)), so a model never proposes a change to notes it has not read. It is checked on the doc row and again, under the lock, on the notes state the guards and `base_instructions_version` come from, so an instructions save landing in between reaches the model before a suggestion can claim it as its base. The refusal is a `DocNotesUnseen` with its own message, and it records the delivery it carries, so the identical retry passes.
- **Guards**: `already_saved` (a memory, or the instructions, already holds exactly this text) and `already_suggested` (the same text pending from any of the user's conversations) answer with a status, not an error. The pending caps count this user's rows only (the reasons are beside `DOC_NOTE_MAX_PENDING_PER_DOC` in `chat/docs/constants.py`), and an instructions suggestion is never refused by a cap that the row it supersedes would free.
- **Empty instructions** are refused as `invalid_instructions` ([Design Decisions](#design-decisions)).
- **Serialized per user**: from the replaced-memory check to the insert, a registration holds `_suggest_lock()`, one `asyncio.Lock` per suggesting user.

A registration publishes `doc_note_suggestions_changed` for its conversation and for every other conversation whose instructions suggestion it superseded. Its own refusals are `NoteSuggestionError`s, whose codes are listed on the class.

## Review

`accept_suggestion()`, `dismiss_suggestion()` and `review_rows()` in `chat/docs/note_suggestions.py` serve only the suggestion's own user; neither they nor their routes are gated, by `doc_notes` or `docs`.

- **Accept re-checks access** on a freshly loaded doc row: a memory needs the UI verdict `free` (`can_edit`), instructions need ownership; anything else is `forbidden`. **Dismiss** needs nothing but ownership of the suggestion, so a recipient whose share was revoked or downgraded can still clear their rows.
- **One transaction, claim first**: the store's `accept_suggestion()` claims the row (`pending` -> `accepted`), which takes the write lock, then writes the notes through the `*_in_session` writers ([Doc Notes -- Concurrency](doc-notes.md#concurrency)). Any refusal rolls the claim back with the write, so the row stays pending and the notes unchanged; an accept or dismiss that lost the claim gets `suggestion_resolved`.
- **Instructions** are written behind `expected_instructions_version`: the version the reviewer's diff was rendered against or, without one, the version the proposal was written against. A moved field answers `stale_instructions` with the current text and version, so the reviewer can compare and re-accept. A reviewer's override of `""` clears the field.
- **Memories**: a replacing suggestion overwrites that memory in place, or inserts when it was deleted meanwhile; the row records the memory in `memory_id`.
- **Seen advance**: an unedited accept advances the proposing conversation's [seen record](doc-notes.md#seen-record) to the versions after the accept, but only from a record that was current just before it.
- After the commit, `doc_note_suggestions_changed` goes to the user and, when a version moved, `doc_notes_changed` to the doc's audience ([Realtime](#realtime), [Doc Notes -- Realtime](doc-notes.md#realtime)).

Review refusals are `NoteSuggestionError` codes too; `HTTP_STATUS` maps them to statuses, and the class docstring says which carry `current` or `memory_id`.

`review_rows()` adds what a review list shows beside a row: the doc's title and, for a pending row, the replaced memory's current text and, for instructions, the current field, its version (`current_instructions_version`, which an accept sends as `expected_instructions_version`) and whether it moved past the proposal's version (a resolved row gets none of these, since an accepted instructions row would always read as moved). For a doc hidden from the user or gone, it blanks those doc-derived fields so its notes never leak.

## Realtime

`doc_note_suggestions_changed {conversation_id, pending_count, total_pending}`, a per-user global built by `make_doc_note_suggestions_changed()` in `chat/realtime/events.py`, is the twin of `project_memory_suggestions_changed`. It goes to the reviewing user only, from `publish_changed()` in `chat/docs/note_suggestions.py`, whenever one conversation's pending set changes; the module docstring lists the causes. `pending_count` is that conversation's count, `total_pending` the user's across all conversations. The publisher is best-effort and never fails the write.

## Counts, Inbox and Report

`GET /conversations/{id}` carries `pending_doc_note_suggestions`. The inbox routes (`chat/memory_inbox.py`) list the user's suggestions as the `doc` kind beside the memory suggestions, each as its [review row](#review), for the [Memories tab](memory-suggestions.md#the-inboxs-memories-tab), and the [Slack reminder](slack-pending-request-notifier.md) counts them with the rest, whatever the user's gates ([Design Decisions](#design-decisions)). The admin Memories report counts doc memories per doc owner and doc note suggestions per reviewer ([Admin System Reports -- Docs, Tables and Memories Sections](admin-system-monitor.md#docs-tables-and-memories-sections)).

## Frontend

Doc note suggestions are reviewed on three surfaces: the conversation's doc bubble above the composer (`MemorySuggestions.tsx`, `kind="doc"`), the inbox's Memories tab (`MemoryInbox.tsx`) and the doc viewer's Notes panel (`frontend/src/components/docs/DocNotesPanel.tsx`, through `useDocNotes`; [Doc Notes -- Frontend](doc-notes.md#frontend)). The row, bubble, inbox and count machinery they share with the memory suggestions is in [Memory Suggestions](memory-suggestions.md). What is particular to doc notes:

- **One row, one set of helpers.** Every surface renders `MemorySuggestionRow` with kind `doc` ([Memory Suggestions -- Design Decisions](memory-suggestions.md#design-decisions)), whose header comment gives what a doc row adds, and accepts through the helpers of `frontend/src/components/docNoteReview.ts`. Accept all, in the bubble and the inbox, skips a doc row its own Accept refuses. The calls are in `frontend/src/api/docsApi.ts`.
- **Stale instructions.** An instructions accept sends the `current_instructions_version` its diff was rendered against; a 409 `stale_instructions` re-bases the row on the current text the error carries, keeping the draft, so the next Accept sends that version ([Review](#review)).
- **Gate.** The Notes panel's block shows only while the `doc_notes` gate is open, since `useDocNotes` is gated; the bubble and the inbox review the rows whatever the gate, as the routes do ([Design Decisions](#design-decisions)).
- **Doc links.** A row links its doc's viewer through `DocTitleLink`, with no link while the doc is gone or hidden. Following it from the inbox closes the inbox view first (`RequestsView.tsx`), since `showRequestsView` wins over a docs route.

## Lifecycle

- **Doc delete** (`delete_doc_from_ui()`, `delete_user_project()` in `chat/project_routes.py`, `delete_account()` in `chat/routes/user.py`): the suggestions cascade with the doc row ([Doc Notes -- Lifecycle](doc-notes.md#lifecycle)). Each path calls `capture_pending_pairs()` before the delete, for the `(user_id, conversation_id)` pairs of the docs' pending suggestions, and `publish_changed_for_pairs()` after it, so every affected reviewer's counts drop the rows. The account delete skips the deleted user's own pairs, whose tabs go with the account. Both steps are best-effort.
- **Conversation delete**: its suggestions cascade without an event ([Known Limitations](#known-limitations)); an accepted memory stays, its `source` naming a suggestion that no longer exists.
- **Share revoked or downgraded**: the recipient's pending suggestions stay. Accept answers `forbidden`, dismiss works, and once the doc is hidden `review_rows()` blanks its fields in the conversation list and the inbox, while the per-doc list answers 404.

## Design Decisions

**Why are the two Slack deny causes admitted?** Slack cannot open an approval card, so access rule 6 turns every approval verdict into a denial there, and the owner's require-approval switch does the same. Refusing those causes would keep Slack conversations from suggesting on any shared doc or any doc with the switch on, although the person could make the change from the web UI and a suggestion already waits for a person. Matching on the `deny_reason` text was rejected because wording is not a key; hence `DocAccess.deny_cause`.

**Why is an empty instructions suggestion refused?** A model proposing to wipe every rule is far more likely a mistake than an intent, and an accept is one click. Registering it, as the store's normalization would allow, was rejected: the person clears the field themselves, and a reviewer's accept override of `""` still clears it.

**Why does an accept without a version default to the proposal's version?** The notes keep no history, so an accept landing over an instructions change made after the proposal would destroy that change for good. Defaulting to the live version was rejected because it overwrites such a change unseen; against the proposal's version the accept answers `stale_instructions` instead.

**Why a per-user lock around the registration checks?** A model can issue several suggest calls in one response, and they run concurrently: without the lock each would read the counts and the pending texts before either inserts, so both could pass a cap or the duplicate guard. One `asyncio.Lock` per user closes that within the one process and event loop ([Known Limitations](#known-limitations)); the supersede rule needs no lock, being enforced under the database write lock.

**Why is suggestion review never gated, when the notes routes are?** A person accepting is a person acting, which the gate's promise that model tools behave as without notes does not cover, and a pending row filed while the gate was open must stay answerable when it closes. Hiding the rows was rejected because closing the gate deletes nothing, so they would linger unanswerable while the inbox and the Slack reminder still counted them. Reading and editing the notes is the feature itself, so those routes stay gated.

**Why does an accept advance the seen record only when unedited, and regardless of the gate?** An unedited accept puts into the notes exactly what the model proposed, so re-arming its conversation would refuse its next write for its own rule; an edited text is not what the model saw, so it must meet it on its next read. Advancing only from a record that was current just before the accept never credits a change the model has not seen. The record is read only while the gate is open, so writing it while closed is harmless and keeps it right if the gate reopens.

**Why does a suggestion row send `note_kind` rather than `kind`?** The inbox tags every row's `kind` with its family (`user`, `project`, `doc`), which a doc row's own kind would collide with. `public_suggestion()` renames it on every route, so the per-doc list, the conversation list and the inbox share one row shape; renaming it in the inbox alone was rejected as one row in two shapes.

**Why does `review_rows()` blank the doc but keep the row?** A doc the user can no longer see must not leak its title or notes through a review list. Dropping the row was rejected because it is still the user's, written by their own conversation, and they must be able to dismiss it.

## Known Limitations

- **A conversation delete drops its suggestions without an event.** The rows cascade with the conversation, as do those of a project delete's own conversations, and, as for project memory suggestions, no `doc_note_suggestions_changed` goes out; the user's other tabs catch up on their next event or a reload.
- **A registration landing between a delete's capture and the delete is missed.** Registration does not take the per-doc lock, so a suggestion committed after a delete path captured the pending pairs leaves that conversation's count one too high until its next event.
- **The registration guards are exact within one process only.** `_suggest_lock()` is an `asyncio.Lock`, so registrations in two server processes could both pass a cap or the duplicate guard. The supersede rule holds regardless.
