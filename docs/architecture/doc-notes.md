# Doc Notes

## Overview

Doc notes are texts that stick to a [Quest Doc](quest-docs.md) outside its body and its revisions. The **instructions** are one plain-text field on the `docs` row, holding the rules a model should follow when it works on the doc. The **memories** are short plain-text facts about the doc, one `doc_memories` row each. `doc_note_suggestions` holds proposed changes to either, each reviewed by the user whose conversation proposed it, with the `MemorySuggestionStatus` lifecycle of [project memory suggestions](project-memories.md): an instructions suggestion carries the complete proposed text, a memory suggestion one fact, optionally replacing a memory.

The two model tools, `list_doc_notes` and `suggest_doc_note`, and the prompting (the `system:doc_notes` skill) are in [Doc Notes Tools](doc-notes-tools.md).

## Key Files

| File | Role |
|------|------|
| `config/feature_gates.py` | `FEATURE_DOC_NOTES`, its `FEATURE_REQUIRES` entry and `doc_notes_enabled_for()` |
| `chat/docs/constants.py` | The doc-notes section: caps, `DOC_NOTE_SUGGESTION_KINDS`, `DOC_NOTES_SERVICE_KEY`, `doc_notes_disabled_message()` |
| `api/instructions.py` | The `doc_notes` pseudo-key in both connected-services maps and in `PSEUDO_SERVICE_KEYS` |
| `db/models.py` | The notes columns on `Doc` (their comment says how to read the deferred text), `DocMemory`, `DocNoteSuggestion` |
| `alembic/versions/7103d39f4efb_add_doc_notes.py` | Adds the `docs` columns in place and creates both tables |
| `db/doc_store.py` | `NOTES_STATE_KEYS`, the notes-state reads, the instructions writers, and `delete_doc()`, which removes the notes with the row |
| `db/doc_memory_store.py` | Every `doc_memories` write (the module docstring gives the transaction shape) and the admin report's `list_all_doc_memory_rows()` |
| `db/doc_note_suggestion_store.py` | The `doc_note_suggestions` rows; the module docstring gives the kinds, the supersede rule and the one-transaction accept |
| `chat/docs/routes.py` | `_doc_row()`, which leaves `NOTES_STATE_KEYS` out of UI rows |
| `chat/docs/notes.py` | The delivery gate, the suggestion taint rule (`suggestion_allowed()`), the notes block and its text copy, and the copy strings: the refusal messages and the `suggest_doc_note` copy (`DOC_NOTE_*`); no I/O |
| `chat/docs/service.py` | The "Doc notes" helper section, the delivery in the read tools and `create_doc`, the notes step of `_resolve_write()`, and the suggestion events of `delete_doc_from_ui()`; the module docstring states the notes invariants |
| `chat/docs/note_suggestions.py` | The `suggest_doc_note` body, accept, dismiss, the review rows and the `doc_note_suggestions_changed` publisher; the module docstring states who may suggest and review |
| `chat/docs/access.py` | `DocAccess.deny_cause`, the machine cause of a denied verdict, which the suggestion gate reads |
| `chat/realtime/events.py`, `chat/docs/events.py` | The two notes envelopes; the audience publishers of `doc_notes_changed` |
| `chat/gemini_api/tool_handlers/docs.py` | Maps `DocNotesUnseen` to the `doc_notes_unseen` tool result |
| `chat/action_request_types/doc_precard.py` | The `write_doc` pre-card's text refusal and its `instructions_version` pin |
| `chat/action_request_types/write_doc.py` | `SERVER_INJECTED_KEYS`, which holds the pin, and `WriteDocHandler.execute`, which passes it to the service |
| `chat/storage.py` | The "Doc notes seen" section: the per-conversation `doc_notes_seen.json` sidecar |
| `chat/compaction.py`, `chat/gemini_api/session.py` | The seen record's deleters, `compact_conversation()` and `get_or_create_chat()` |
| `chat/project_routes.py`, `chat/routes/user.py` | The project and account deletes, which capture the pending suggestions on the docs they delete and publish for them afterwards |
| `chat/docs/note_routes.py`, `chat/docs/note_suggestion_routes.py` | The notes routes and the suggestion review routes ([Quest Docs API -- Notes](../api/quest-docs-api.md#notes)); each module docstring gives the check order and the errors |
| `chat/docs/note_payloads.py` | The JSON shapes the routes and the inbox share; the module docstring holds the `source_label` table |
| `chat/memory_inbox.py`, `chat/routes/conversations.py`, `chat/feature_usage_report.py` | Where doc note suggestions join the other memory suggestions: the inbox's `doc` kind, `pending_doc_note_suggestions`, the admin Memories report |

## Feature Gate

`FEATURE_DOC_NOTES` is an off-by-default, per-user gate that requires `docs`; its layers are in [Feature Gates -- Enforcement Layers (Quest Doc notes)](feature-gates.md#enforcement-layers-quest-doc-notes). Closing it deletes nothing and leaves suggestion review open ([Review](#review)).

## Data Model

The columns, foreign-key actions and cascades are on the classes in `db/models.py`, summarised in [Database -- Doc and DocShare Models](database.md#doc-and-docshare-models). What the schema does not say:

- Instructions are stored stripped, and the empty string means none (`normalize_instructions()`).
- A memory's `source` names the person who wrote it or the suggestion it came from (`validate_source()`), and exact-duplicate text on one doc is refused.
- A suggestion's `user_id` is its reviewer, and an instructions suggestion records the `instructions_version` it was written against.
- **Doc dicts never carry the instructions text.** The column is deferred with raiseload, and the `has_instructions` column property answers in SQL. `_doc_to_dict()` adds the `NOTES_STATE_KEYS` (counts and versions, never the text), enough to decide about notes without a second query, and UI rows drop them. The notes-state reads in `db/doc_store.py` are the only reads of the text.

## Versions and Counters

Each version counter moves only with its own kind of change: `instructions_version` with an instructions change (it is also the save's optimistic-concurrency token), `memories_version` with a memory create, update or delete, which also recounts `memory_count`. Refused and identical-text writes move nothing. No notes write touches `updated_at` (the body's concurrency token) or `last_write_source`, and `update_doc_metadata()` never changes a note.

## Concurrency

- **Instructions**: the save is one compare-and-swap `UPDATE` on the expected `instructions_version`, so two saves holding one token never both land; the loser gets `StaleInstructionsError` with the current state (`write_instructions_in_session()`).
- **Memories**: last write wins, with no token. Every write first takes SQLite's write lock with a no-op `UPDATE` of the doc row, then follows the steps in the `db/doc_memory_store.py` module docstring, so concurrent writers to one doc can neither lose a version bump nor both pass the cap.
- The `*_in_session` writers of both stores run inside a caller's transaction and never commit, so a caller can combine a notes write with its own writes; it reads the resulting versions with `load_notes_state_in_session()`, never from a loaded `Doc`.

## Caps

Every doc-notes cap and budget is a constant in the doc-notes section of `chat/docs/constants.py`, explained beside it. The stores enforce the instructions and memory lengths and the memories-per-doc count; `chat/docs/notes.py` keeps the notes block and its text copy within their budgets ([Notes Block](#notes-block)); `register_suggestion()` enforces the pending-suggestion caps ([Registration](#registration)).

## Access

The stores, the suggestion store included, make no access decisions; callers resolve `resolve_doc_access()` in `chat/docs/access.py` first, as for every `db/doc_store.py` function. Who may suggest and who may review is in [Suggestions](#suggestions).

## Seen Record

`CHATS_DIR/<conversation_id>/doc_notes_seen.json` holds, per doc, the `instructions_version` and `memories_version` this conversation's model was last shown, read back as a `NotesSeen` (`chat/docs/notes.py`). `ChatStorage` manages it in its "Doc notes seen" section (`chat/storage.py`), whose docstrings give the file shape and the validation.

- **Never backwards**: `record_doc_notes_seen()` stores the per-field maximum of the stored entry and the given versions, and skips a write that changes nothing.
- **Failures fail closed**: a missing or corrupt file reads as no record for any doc, a malformed entry as no record for that doc. A failed write is logged and leaves the record as it was, so the notes are delivered (or a write refused) again until a write lands. A failed delete empties the file in place instead, because a surviving record would let writes skip the notes; only when that fails too is the record left standing, logged as an error.
- **Event loop only**: it is written synchronously on the event loop, under the single-event-loop guarantee of the [read sidecar](quest-docs.md#read-sidecar), with no lock.

The record vouches only for blocks still in the model's context, so `clear_doc_notes_seen()` deletes it whenever the model-facing history is rebuilt from less than the full `sdk_history.json`:

- `compact_conversation()` (`chat/compaction.py`), just before it writes the compacted history, because the summary may drop a delivered block. Clearing first means a failure in between leaves the full history with no record, never the compacted one with a stale record.
- `get_or_create_chat()` (`chat/gemini_api/session.py`), whenever it creates a session with no history: a cross-provider switch with no compatible disk copy (also after a restart, when the caller discarded another provider's disk history) or a failed `save_history()` with none. On a conversation's first session this is a no-op.

A thinking-effort or same-provider rebuild and a session seeded from compatible disk history carry the history over and keep the record. `doc_reads.json` survives both deletions, so after a rebuild a write still passes the read-before-edit gate, and `write_refused()`, finding no record, stops it while the doc has notes.

## Delivery Gate

The gate functions in `chat/docs/notes.py` do no I/O: a caller loads the doc's notes state and the conversation's `NotesSeen`, decides with them, and writes the record back. `has_notes()`, `needs_delivery()` and `write_refused()` take either a doc dict (`has_instructions`, never the text) or the `get_notes_state()` dict; their docstrings give the exact rules. What the rules encode:

- **Delivery** (`needs_delivery()`): a read carries the notes block when the conversation has no record and the doc has notes, or when either version moved past the record, also when the change emptied the notes, so the model learns that a rule it saw is gone.
- **Write refusal** (`write_refused()`): a model write is refused when there is no record and the doc has notes, or when the instructions moved past the record and are non-empty. A memory change or cleared instructions are delivered on the next read but never block a write: memories are background facts, while the instructions are the rules a change must follow.
- A record ahead of the doc counts as current.

`records_delivery()` decides whether a run's deliveries update its conversation's record, and so whether its writes can be refused for unseen notes: only the run kinds in `RECORDING_RUN_KINDS`, with a conversation id. Its docstring gives the reason for each other run kind. The one that spans files is `sub_agent`: it dispatches with the parent's `conversation_id`, so its reads count for the parent in `doc_reads.json`, but the parent model never sees the block the sub-agent was shown, so the seen record never credits it. What each run kind gets on a read is in [Delivery on Reads](#delivery-on-reads).

## Notes Block

`build_notes_block()` builds the `doc_notes` object a doc tool result carries; its docstring gives the keys. It takes the `get_notes_state()` dict, since doc dicts never carry the instructions text, and `list_doc_memories()`, newest first. Callers read `get_notes_state()` before `list_doc_memories()`, so a memory change between the two reads shows the model memories at least as new as the `memories_version` it records, never older.

- **Budget**: the block's JSON text, written with `ensure_ascii=False`, stays within `DOC_NOTES_DELIVERY_MAX_CHARS`. The instructions always come whole; the memories are a prefix, newest first, ending before the first one that does not fit, and `memories_omitted` counts the rest.
- **Paging**: `memory_offset` skips that many of the newest memories. When memories remain past the offset but none fits, the block still carries the newest remaining one, over the budget, so advancing the offset by the memories returned always moves forward.
- **Current state**: the block shows the notes as they are, whatever the record says; cleared instructions come as the empty string and no memories as an empty list.

### Text Copy

`render_notes_text()` is the block's plain-text form for a `write_doc` pre-card refusal, an `Invalid parameters` tool error with no cap of its own, so it stays within `DOC_NOTES_PRECARD_MAX_CHARS`; its docstring gives the layout and what is cut. The budget is sized so instructions within `DOC_INSTRUCTIONS_MAX_CHARS` always come whole beside the widest trailer, because a refusal records the delivery it carried and a cut copy could not be recorded. Should the budget be lowered below that, a cut copy returns `NotesText` with `instructions_whole` false, and a caller must not record that instructions version as seen.

The strings the model reads in the block and in its text copy are module constants at the top of `chat/docs/notes.py`, kept together for review under the [stand-alone rule](doc-notes-tools.md#prompting).

## Delivery on Reads

The doc tools check the gate through `notes_enabled()` in `chat/docs/service.py`, for the caller and never for the doc owner. While it is closed nothing below runs, and every doc tool returns the payload it returns without notes. While it is open:

- **`list_docs` / `search_docs`**: every row ends with `notes`, the counts of `notes_counts()` read from the row. Never content; notes are never searched.
- **`read_doc`**: the result ends with the `notes` counts and, when the notes are delivered, the `doc_notes` [block](#notes-block) from `load_notes()`; the counts are then the block's, so they agree with it even when a memory changed between the two notes reads.
- **`create_doc`**: records the new doc as seen at versions (0, 0), the creator exception. No notes can predate the creating conversation, and a note added later moves a version past the record, which re-arms delivery and, for instructions, the refusal.

Whether a `read_doc` carries the block depends on the run kind:

- **Recording runs** (`records_delivery()`): when `needs_delivery()` holds against the seen record, and the delivered versions are then recorded.
- **Sub-agent and inference-API runs**: whenever the doc has notes, never recorded. `notes_seen()` returns no record outside recording runs, so a sub-agent, which shares its parent's conversation id, never has its block suppressed by the parent's record.
- **Scripts** (`_READ_DOC_COUNTS_ONLY_RUN_KINDS`): the counts only; `list_doc_notes` gives them the block ([Doc Notes Tools](doc-notes-tools.md#list_doc_notes)).

`record_notes_delivery()` writes the record for the doc tools. It is a no-op outside recording runs and runs as the last step, with no `await` before the return or raise; its docstring gives the reason.

## Write Refusal

The notes step is the last check of `_resolve_write()` in `chat/docs/service.py`: after the denied verdict, `edit`'s read-before-edit and `add_image`'s conversation requirement, before the caller's approval verdict. The three write tools and the `write_doc` pre-card therefore share it, and read-only runs never reach it, because the access rule denies their writes first. For a tool write it runs only in the first pass, outside the per-doc lock; the re-resolve under the lock skips it (approve-time execute repeats only its [pin check](#instructions-pin-on-write_doc-cards)).

In a recording run where `write_refused()` holds, `_refuse_unseen_notes()` records the delivery and raises `DocNotesUnseen`, which carries the block and its text copy. It decides on the row, then again on the freshly loaded state, so notes emptied in between never refuse. The identical retry passes. The model sees the refusal as:

- **Write tools**: `{"error": "doc_notes_unseen", "message", "doc_notes": {...}}`, mapped in `chat/gemini_api/tool_handlers/docs.py`. The message is `DOC_NOTES_UNSEEN_MESSAGE` (`chat/docs/notes.py`) with the doc's title, and `doc_notes` is the block.
- **`write_doc` pre-card** (`doc_precard_check()` -> `preview_write_operation()`, which asks for the text form): the same message, the [text copy](#text-copy) and the block's `note`, as the same-turn `Invalid parameters` text. When the copy had to cut the instructions, the seen instructions version is left where it was, so the retry is refused again.

## Instructions Pin on write_doc Cards

While the gate is open for the proposer, the pre-card injects the live row's `instructions_version` into the card's params. It is one of the `SERVER_INJECTED_KEYS` in `chat/action_request_types/write_doc.py`, so a model-supplied copy is dropped. `WriteDocHandler.execute` passes it to the service as `expected_instructions_version`, a server-only pin of `_normalize_write_params()` (`_SERVER_PINS`, never part of a `suggested_request`).

At approve time (`bypass_approval`) the notes step never consults the seen record. `_check_pinned_instructions()` refuses with `DOC_INSTRUCTIONS_CHANGED_MESSAGE` when the live `instructions_version` differs from the pin and the live instructions are non-empty; being a pure comparison, it runs in the first pass and again on the row re-read under the per-doc lock. The refusal fails the Approve and leaves the request open, like a stale `old_string` ([Quest Docs -- The write_doc Action Request](quest-docs.md#the-write_doc-action-request)); [its text](doc-notes-tools.md#prompting) is for the approver. A card without a pin (proposed while the gate was closed for the proposer) or a gate closed at execute is not compared, and memory changes never refuse. The card shows no notes.

## Suggestions

A model never writes the notes: it proposes a change with `suggest_doc_note` ([Doc Notes Tools](doc-notes-tools.md#suggest_doc_note)), and a person accepts or dismisses it. `db/doc_note_suggestion_store.py` is the doc-notes twin of `db/project_memory_suggestion_store.py`, sharing its status lifecycle, `SuggestionAlreadyResolvedError` and resolved-list keyset cursor, and `chat/docs/note_suggestions.py` is the twin of `chat/project_memories.py`. The reviewer is always the proposing conversation's user (`user_id`); every store function but the cross-user reports is scoped to that user.

- **Supersede**: a user has at most one pending instructions suggestion per doc, so they never review two competing full texts. `create_suggestion()` dismisses the older one, from any of the user's conversations, in the insert's own transaction, under SQLite's write lock taken as the [memory writers](#concurrency) take it; the store's module docstring gives what that serializes.
- **Content** is normalized by the notes stores' own functions, so the suggestion caps count characters exactly as the notes caps do.
- **`SuggestionTargetGoneError`**: the doc or the conversation went before the insert; the service reports it as a missing doc.

### Registration

`register_suggestion()` is the `suggest_doc_note` body; its docstring lists the checks in order and the result statuses. What the order encodes:

- **Taint** (`suggestion_allowed()` in `chat/docs/notes.py`): only a conversation whose write verdict on the doc is `free` or `approval`, or `denied` with one of the `SLACK_DENY_CAUSES` (matched on `DocAccess.deny_cause`), may suggest, so a private conversation can never put text into a public doc's notes. Any other denied verdict answers with its `deny_reason`, and a hidden doc with the missing doc's text, exactly as the doc tools do.
- **Owner-only instructions**: an instructions suggestion also needs the conversation's user to own the doc (`instructions_owner_only`).
- **Seen first**: the conversation must have seen every current note, memories included (`needs_delivery()`, stricter than the write refusal's `write_refused()`), so a model never proposes a change to notes it has not read. It is checked on the doc row and again, under the lock, on the notes state the guards and `base_instructions_version` come from, so an instructions save landing in between reaches the model before a suggestion can claim it as its base. The refusal is a `DocNotesUnseen` with its own message, and it records the delivery it carries, so the identical retry passes.
- **Guards**: `already_saved` (a memory, or the instructions, already holds exactly this text) and `already_suggested` (the same text pending from any of the user's conversations) answer with a status, not an error. The pending caps count this user's rows only (the reasons are beside `DOC_NOTE_MAX_PENDING_PER_DOC` in `chat/docs/constants.py`), and an instructions suggestion is never refused by a cap that the row it supersedes would free.
- **Empty instructions** are refused as `invalid_instructions` ([Design Decisions](#design-decisions)).
- **Serialized per user**: from the replaced-memory check to the insert, a registration holds `_suggest_lock()`, one `asyncio.Lock` per suggesting user.

A registration publishes `doc_note_suggestions_changed` for its conversation and for every other conversation whose instructions suggestion it superseded. Its own refusals are `NoteSuggestionError`s, whose codes are listed on the class.

### Review

`accept_suggestion()`, `dismiss_suggestion()` and `review_rows()` in `chat/docs/note_suggestions.py` serve only the suggestion's own user, and none is gated, by `doc_notes` or by `docs`, nor are their routes.

- **Accept re-checks access** on a freshly loaded doc row: a memory needs the UI verdict `free` (`can_edit`), instructions need ownership; anything else is `forbidden`. **Dismiss** needs nothing but ownership of the suggestion, so a recipient whose share was revoked or downgraded can still clear their rows.
- **One transaction, claim first**: the store's `accept_suggestion()` claims the row (`pending` -> `accepted`), which takes the write lock, then writes the notes through the `*_in_session` writers ([Concurrency](#concurrency)). Any refusal rolls the claim back with the write, so the row stays pending and the notes unchanged; an accept or dismiss that lost the claim gets `suggestion_resolved`.
- **Instructions** are written behind `expected_instructions_version`: the version the reviewer's diff was rendered against or, without one, the version the proposal was written against. A moved field answers `stale_instructions` with the current text and version, so the reviewer can compare again and re-accept. A reviewer's override of `""` clears the field.
- **Memories**: a replacing suggestion overwrites that memory in place, or inserts when it was deleted meanwhile; the row records the memory in `memory_id`.
- **Seen advance**: an unedited accept advances the proposing conversation's seen record to the versions after the accept, but only from a record that was current just before it.
- After the commit, `doc_note_suggestions_changed` goes to the user and, when a version moved, `doc_notes_changed` to the doc's audience ([Realtime](#realtime)).

Review refusals are `NoteSuggestionError` codes too; `HTTP_STATUS` maps them to HTTP statuses, and the class docstring says which carry `current` or `memory_id`.

`review_rows()` adds what a review list shows beside a row: the doc's title and, for a pending row, the replaced memory's current text and, for instructions, the current field and whether it moved past the proposal's version (a resolved row gets none of these, since an accepted instructions row would always read as moved). For a doc the user can no longer see, or one that is gone, it blanks those doc-derived fields, so hidden notes never leak.

## Realtime

Two per-user globals, built by `make_doc_notes_changed()` and `make_doc_note_suggestions_changed()` in `chat/realtime/events.py`. Both publishers are best-effort and never fail the write.

- **`doc_notes_changed {doc_id, instructions_version, memories_version}`** goes after every change to the notes to the doc's audience, the same as `doc_changed` ([Quest Docs -- Realtime](quest-docs.md#realtime)), so an open view of the notes re-fetches. It carries the counters, never text, and comes alone: notes never bump `updated_at` and are not part of list rows, so no `doc_changed` or `doc_list_changed` accompanies it. `publish_notes_changed()` in `chat/docs/service.py` is its one publisher (through `publish_doc_notes_changed_for()` in `chat/docs/events.py`); every writer of the instructions or the memories must call it after its store write, with the `get_notes_state()` dict after the change.
- **`doc_note_suggestions_changed {conversation_id, pending_count, total_pending}`** is the twin of `project_memory_suggestions_changed`. It goes to the reviewing user only, from `publish_changed()` in `chat/docs/note_suggestions.py`, whenever one conversation's pending set changes; the module docstring lists the causes. `pending_count` is that conversation's count, `total_pending` the user's across all conversations.

## UI Routes, Inbox and Report

The routes, their shapes and their errors are in [Quest Docs API -- Notes](../api/quest-docs-api.md#notes) and [Note Suggestions](../api/quest-docs-api.md#note-suggestions). What they encode:

- **Roles**: any viewer reads the notes; only the owner saves the instructions, any share refused ([Design Decisions](#design-decisions)); an editor (owner or write share) writes memories, last write wins.
- **Gates**: the notes routes answer `doc_notes_disabled` first; the review routes check no gate ([Review](#review)).
- **Identities**: a memory goes out with a `source_label` instead of its `source` and editor ids, naming a person to the doc owner only (`source_label()` in `chat/docs/note_payloads.py`), and `instructions_updated_by` is the owner's alone.

`GET /conversations/{id}` carries `pending_doc_note_suggestions`. The inbox routes list the user's suggestions as the `doc` kind, which the Memories tab leaves out ([Memory Suggestions -- The Inbox's Memories Tab](memory-suggestions.md#the-inboxs-memories-tab)), and the [Slack reminder](slack-pending-request-notifier.md) counts them with the rest. The admin Memories report counts doc memories per doc owner and doc note suggestions per reviewer ([Admin System Reports -- Docs, Tables and Memories Sections](admin-system-monitor.md#docs-tables-and-memories-sections)).

## Frontend

`frontend/src/api/docsApi.ts` holds the notes and review calls, the gate name `DOC_NOTES_FEATURE` and the helpers for the `stale_instructions` and `duplicate_memory` conflicts. `frontend/src/hooks/useDocNotes.ts` serves one doc's notes and the viewer's own pending suggestions for it, gated inside the hook; its header comment gives the events it follows and the stale-payload guard. The doc viewer's Notes panel shows and edits the notes through it ([Quest Docs -- Viewer](quest-docs.md#viewer)). `frontend/src/store/conversationStore.ts` keeps `pendingDocNoteSuggestions`, fed by `pending_doc_note_suggestions` and `doc_note_suggestions_changed`.

## Lifecycle

- **Doc delete** (`delete_doc_from_ui()`, `delete_user_project()` in `chat/project_routes.py`, `delete_account()` in `chat/routes/user.py`): memories and suggestions cascade with the doc row, and the instructions go with it. Each path calls `capture_pending_pairs()` before the delete, for the `(user_id, conversation_id)` pairs of the docs' pending suggestions, and `publish_changed_for_pairs()` after it, so every affected reviewer's counts drop the rows. The account delete skips the deleted user's own pairs, whose tabs go with the account. Both steps are best-effort.
- **Conversation delete**: its suggestions cascade without an event ([Known Limitations](#known-limitations)); an accepted memory stays, its `source` naming a suggestion that no longer exists.
- **Share revoked or downgraded**: the recipient's pending suggestions stay. Accept answers `forbidden`, dismiss works, and once the doc is hidden `review_rows()` blanks its fields in the conversation list and the inbox, while the per-doc list answers 404.

## Constraints

- `docs` is altered in place, never rebuilt with Alembic batch mode ([Database -- Doc and DocShare Models](database.md#doc-and-docshare-models)).
- Notes are plaintext in `quest.db`, like doc metadata ([Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why are the instructions a column while memories get a table?** One field per doc, read with the doc, gains nothing from a join. Memories are a list whose items carry their own id, author and edits, which a column could hold only as a blob rewritten whole on every change.

**Why is the instructions text deferred?** List and search rows come from `select(Doc)` and need only whether instructions exist. Loading up to `DOC_INSTRUCTIONS_MAX_CHARS` per row for them was rejected; `has_instructions` answers in SQL, and raiseload turns an accidental load into an error instead of a silent per-row query.

**Why do notes changes never bump `updated_at`?** It is the body's token for editor saves, rename and restore, so a notes change would turn an open editor's save into a false conflict, the same reason share changes leave it alone.

**Why version counters rather than timestamps?** A counter moves by exactly one per landed change and compares by equality, so the compare-and-swap is immune to two writes in one clock tick or clock skew. Two counters let a consumer tell an instructions change from a memory change; `memory_count` is recounted rather than incremented so it cannot drift.

**Why does the seen record keep the maximum rather than the latest write?** Two deliveries of one doc can interleave (parallel tool calls awaiting database reads in between), and an overwrite would let the older one land last, moving the record backwards and costing an extra delivery or refusal. Versions only grow and every recorded one was delivered into the current context, so the maximum is a version the model has seen.

**Why do compaction and a history-less session delete the seen record but keep `doc_reads.json`?** The record vouches that delivered rules are still in the model's context, which a summary or an empty history may have dropped; keeping it was rejected because a write that ignores a rule still lands, with nothing to catch it. A stale view of the body is caught by the exact-match edit instead ([Quest Docs -- Design Decisions](quest-docs.md#design-decisions)), so the read sidecar keeps its credits and the notes gate alone stops the first write after a rebuild.

**Why is the block budget measured on its JSON text with `ensure_ascii=False`?** The block reaches the model as JSON, so the measure includes what serialization adds (keys, the note, escaped quotes and control characters), which a sum of the raw texts would miss. `ensure_ascii=False` counts a non-ASCII character once, as the instructions and memory caps do, so notes in a non-Latin script get as many memories as English ones instead of a fraction under `\uXXXX` escaping.

**Why are the memories a newest-first prefix with a progress rule?** A prefix leaves one gap, the older end, which `memories_omitted` counts and a page at `memory_offset` resumes from; packing shorter older memories into the room a long one left would show a set with holes the model could neither name nor page to. The progress rule keeps a memory too large for the room beside the instructions from stalling paging at one offset.

**Why does `records_delivery()` raise for an unknown run kind?** Not recording also means never refusing a write for unseen notes, so a run kind added to `RUN_KINDS` (`chat/artifact_access.py`) and silently treated as non-recording would let its model write past instructions it was never shown. Raising makes whoever adds a kind place it; a conversation id that is neither `None` nor a string raises `TypeError` for the same reason instead of reading as no conversation.

**Why does a refusal record the delivery it carried?** The refusal result puts the block in the model's context, so the identical retry can pass. Requiring a `read_doc` before the retry was rejected: it costs a call and returns a body the write may not need, such as for an append, to deliver notes the model already holds.

**Why does the notes step sit before the approval verdict?** The model meets the notes on its first write attempt, before an `approval_required` result hands it a `suggested_request` to forward, so the card it proposes can already follow the instructions. Placing the step after the verdict was rejected: the model would forward a card drafted without the rules and meet them only at the pre-card.

**Why does approve-time execute skip the seen record but pin the instructions version?** The approver is a person, often a write-share recipient rather than the owner who wrote the rules, and the card shows no instructions, so they cannot weigh an instructions change themselves; the pin refuses a card whose instructions moved while it sat open. Re-checking the seen record at Approve was rejected because compaction can delete the record while the card sits open, refusing a proposal that had seen the notes.

**Why do sub-agent and inference-API reads get the block without recording it?** The parent model never sees a sub-agent's block, so crediting it would let the parent write past notes it was never shown, while the sub-agent still needs the notes for its own work. An inference run is a fresh conversation per call, so there is nothing to remember and every read is a first touch.

**Why does a doc without notes give those runs no block?** Attaching an empty block to every read was rejected as noise for a run that has nothing to remember. A recording run gets an emptied block only to learn that a rule it was shown is gone.

**Why does a script's `read_doc` carry the counts only?** A script is code, not the model, and records nothing, so a block delivered to it would vouch for nothing; the model's own write is still refused until the notes reach the model. The counts cost nothing and let a script see which docs have notes.

**Why does a closed gate turn everything off, including the creator's (0, 0) record?** A closed gate must leave every doc tool and sidecar exactly as without notes, decided by one `notes_enabled()` check per tool. Writing the creator record regardless was rejected so that no part of the feature runs for a user who does not have it; a doc created while the gate was closed is treated like any other once it opens.

**Why are the two Slack deny causes admitted?** Slack cannot open an approval card, so access rule 6 turns every approval verdict into a denial there, and the owner's require-approval switch does the same. Refusing those causes would keep Slack conversations from suggesting on any shared doc or any doc with the switch on, although the person could make the change from the web UI and a suggestion already waits for a person. Matching on the `deny_reason` text was rejected because wording is not a key; hence `DocAccess.deny_cause`.

**Why may only the owner change the instructions, in the UI or by suggestion?** The instructions are directives the model follows in every conversation that touches the doc, so only the owner authors them, even where a write share may edit the body and the memories. A suggestion is reviewed by its proposer, who could never accept it on a doc they do not own, so the refusal tells the model to put the rule in its reply for the person to pass on.

**Why is an empty instructions suggestion refused?** A model proposing to wipe every rule is far more likely a mistake than an intent, and an accept is one click. Registering it, as the store's normalization would allow, was rejected: the person clears the field themselves, and a reviewer's accept override of `""` still clears it.

**Why does an accept without a version default to the proposal's version?** The notes keep no history, so an accept landing over an instructions change made after the proposal would destroy that change for good. Defaulting to the live version was rejected because it overwrites such a change unseen; against the proposal's version the accept answers `stale_instructions` instead.

**Why a per-user lock around the registration checks?** A model can issue several suggest calls in one response, and they run concurrently: without the lock each would read the counts and the pending texts before either inserts, so both could pass a cap or the duplicate guard. One `asyncio.Lock` per user closes that within the one process and event loop ([Known Limitations](#known-limitations)); the supersede rule needs no lock, being enforced under the database write lock.

**Why is suggestion review never gated, when the notes routes are?** A person accepting is a person acting, which the gate's promise that model tools behave as without notes does not cover, and a pending row filed while the gate was open must stay answerable when it closes. Hiding the rows was rejected because closing the gate deletes nothing, so they would linger unanswerable while the inbox and the Slack reminder still counted them. Reading and editing the notes is the feature itself, so those routes stay gated.

**Why does an accept advance the seen record only when unedited, and regardless of the gate?** An unedited accept puts into the notes exactly what the model proposed, so re-arming its conversation would refuse its next write for its own rule; an edited text is not what the model saw, so it must meet it on its next read. Advancing only from a record that was current just before the accept never credits a change the model has not seen. The record is read only while the gate is open, so writing it while closed is harmless and keeps it right if the gate reopens.

**Why does a suggestion row send `note_kind` rather than `kind`?** The inbox tags every row's `kind` with its family (`user`, `project`, `doc`), which a doc row's own kind would collide with. `public_suggestion()` renames it on every route, so the per-doc list, the conversation list and the inbox share one row shape; renaming it in the inbox alone was rejected as one row in two shapes.

**Why does a memory carry a label instead of its writer?** The raw `source` names the person who wrote it or the suggestion it came from, identities a share recipient is shown nowhere else (doc rows blank `last_write_source` and `last_write_user` for them too). Sending the ids for the client to word was rejected because they would leave the server; `source_label()` words them per viewer, naming a person to the owner only.

**Why does `review_rows()` blank the doc but keep the row?** A doc the user can no longer see must not leak its title or notes through a review list. Dropping the row was rejected because it is still the user's, written by their own conversation, and they must be able to dismiss it.

## Known Limitations

- **A second write in the same model response passes.** When one response issues several writes to a doc, the first refusal records the delivery, so the others are not refused; the refusal message (`DOC_NOTES_UNSEEN_MESSAGE`) asks the model to check them against the notes. Tracking delivery per model step was judged out of proportion.
- **A tool write's notes step is not re-checked under the per-doc lock.** An instructions change landing between that check and the write applies from the next write on.
- **A process crash can leave a record for a block the model never received**, when it lands after a read recorded its delivery and before the tool result was flushed to the chat history. A Stop is safe: the tool result joins the run's messages with no `await` after the service returns (`_emit_durable()` in `chat/gemini_api/turn_tools.py`), and the next turn replays it.
- **A conversation delete drops its suggestions without an event.** The rows cascade with the conversation, as do those of a project delete's own conversations, and, as for project memory suggestions, no `doc_note_suggestions_changed` goes out; the user's other tabs catch up on their next event or a reload.
- **A registration landing between a delete's capture and the delete is missed.** Registration does not take the per-doc lock, so a suggestion committed after a delete path captured the pending pairs leaves that conversation's count one too high until its next event.
- **The registration guards are exact within one process only.** `_suggest_lock()` is an `asyncio.Lock`, so registrations in two server processes could both pass a cap or the duplicate guard. The supersede rule holds regardless.
