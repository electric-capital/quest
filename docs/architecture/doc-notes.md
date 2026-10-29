# Doc Notes

## Overview

Doc notes are texts that stick to a [Quest Doc](quest-docs.md) outside its body and its revisions. The **instructions** are one plain-text field on the `docs` row, holding the rules a model should follow when it works on the doc. The **memories** are short plain-text facts about the doc, one `doc_memories` row each. `doc_note_suggestions` holds proposed changes to either, each reviewed by the user whose conversation proposed it, with the `MemorySuggestionStatus` lifecycle of [project memory suggestions](project-memories.md): an instructions suggestion carries the complete proposed text, a memory suggestion one fact, optionally replacing a memory. This doc covers the feature gate, the data model, the version counters, concurrency, caps, the per-conversation seen record, the delivery gate, the notes block, delivery on the doc reads, the write refusal, the instructions pin on `write_doc` cards and the known limitations.

## Key Files

| File | Role |
|------|------|
| `config/feature_gates.py` | `FEATURE_DOC_NOTES`, its `FEATURE_REQUIRES` entry and `doc_notes_enabled_for()` |
| `chat/docs/constants.py` | The doc-notes section: caps, `DOC_NOTE_SUGGESTION_KINDS`, `DOC_NOTES_SERVICE_KEY`, `doc_notes_disabled_message()` |
| `api/instructions.py` | The `doc_notes` pseudo-key in both connected-services maps and in `PSEUDO_SERVICE_KEYS` |
| `db/models.py` | The notes columns on `Doc` (their comment says how to read the deferred text), `DocMemory`, `DocNoteSuggestion` |
| `alembic/versions/7103d39f4efb_add_doc_notes.py` | Adds the `docs` columns in place and creates both tables |
| `db/doc_store.py` | `NOTES_STATE_KEYS`, the notes-state reads, the instructions writers, and `delete_doc()`, which removes the notes with the row |
| `db/doc_memory_store.py` | Every `doc_memories` write; the module docstring gives the transaction shape |
| `chat/docs/routes.py` | `_doc_row()`, which leaves `NOTES_STATE_KEYS` out of UI rows |
| `chat/docs/notes.py` | The delivery gate, the notes block and its text copy, and the copy strings the model reads, incl. both refusal messages; no I/O |
| `chat/docs/service.py` | The "Doc notes" helper section (gate check, counts, loading, seen record), the delivery in the read tools and `create_doc`, and the notes step of `_resolve_write()`; the module docstring states the notes invariants |
| `chat/gemini_api/tool_handlers/docs.py` | Maps `DocNotesUnseen` to the `doc_notes_unseen` tool result |
| `chat/action_request_types/doc_precard.py` | The `write_doc` pre-card's text refusal and its `instructions_version` pin |
| `chat/action_request_types/write_doc.py` | `SERVER_INJECTED_KEYS`, which holds the pin, and `WriteDocHandler.execute`, which passes it to the service |
| `chat/storage.py` | The "Doc notes seen" section: the per-conversation `doc_notes_seen.json` sidecar |
| `chat/compaction.py`, `chat/gemini_api/session.py` | The seen record's deleters, `compact_conversation()` and `get_or_create_chat()` |

## Feature Gate

`FEATURE_DOC_NOTES` is an off-by-default, per-user gate that requires `docs`; its layers are in [Feature Gates -- Enforcement Layers (Quest Doc notes)](feature-gates.md#enforcement-layers-quest-doc-notes). Closing it deletes nothing.

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

Every doc-notes cap and budget is a constant in the doc-notes section of `chat/docs/constants.py`, explained beside it. The stores enforce the instructions and memory lengths and the memories-per-doc count; `chat/docs/notes.py` keeps the notes block and its text copy within their budgets ([Notes Block](#notes-block)).

## Access

The stores make no access decisions; callers resolve `resolve_doc_access()` in `chat/docs/access.py` first, as for every `db/doc_store.py` function.

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

`render_notes_text()` is the block's plain-text form for a `write_doc` pre-card refusal, an `Invalid parameters` tool error with no cap of its own, so it stays within `DOC_NOTES_PRECARD_MAX_CHARS` and leaves out the `note`. The instructions come whole, followed by the longest prefix of whole memories that fits, and a trailer counts every memory left out. The budget is sized so instructions within `DOC_INSTRUCTIONS_MAX_CHARS` always come whole beside the widest trailer, because a refusal records the delivery it carried and a cut copy could not be recorded. Only instructions longer than the budget allows (if the budget is lowered) are cut, ending in a marker line counting the characters cut, with no memory shown; the returned `NotesText` then has `instructions_whole` false, and a caller must not record that instructions version as seen.

The strings the model reads in the block and in its text copy are module constants at the top of `chat/docs/notes.py`, kept together for review.

## Delivery on Reads

The doc tools check the gate through `notes_enabled()` in `chat/docs/service.py`, for the caller and never for the doc owner. While it is closed nothing below runs, and every doc tool returns the payload it returns without notes. While it is open:

- **`list_docs` / `search_docs`**: every row ends with `notes`, the counts of `notes_counts()` read from the row. Never content; notes are never searched.
- **`read_doc`**: the result ends with the `notes` counts and, when the notes are delivered, the `doc_notes` [block](#notes-block) from `load_notes()`; the counts are then the block's, so they agree with it even when a memory changed between the two notes reads.
- **`create_doc`**: records the new doc as seen at versions (0, 0), the creator exception. No notes can predate the creating conversation, and a note added later moves a version past the record, which re-arms delivery and, for instructions, the refusal.

Whether a `read_doc` carries the block depends on the run kind:

- **Recording runs** (`records_delivery()`): when `needs_delivery()` holds against the seen record, and the delivered versions are then recorded.
- **Sub-agent and inference-API runs**: whenever the doc has notes, never recorded. `notes_seen()` returns no record outside recording runs, so a sub-agent, which shares its parent's conversation id, never has its block suppressed by the parent's record.
- **Scripts** (`_READ_DOC_COUNTS_ONLY_RUN_KINDS`): the counts only.

`record_notes_delivery()` writes the record for the doc tools. It is a no-op outside recording runs and runs as the last step, with no `await` before the return or raise; its docstring gives the reason.

## Write Refusal

The notes step is the last check of `_resolve_write()` in `chat/docs/service.py`: after the denied verdict, `edit`'s read-before-edit and `add_image`'s conversation requirement, before the caller's approval verdict. The three write tools and the `write_doc` pre-card therefore share it, and read-only runs never reach it, because the access rule denies their writes first. It runs only in the first pass, outside the per-doc lock; the re-resolve under the lock skips it.

In a recording run where `write_refused()` holds, `_refuse_unseen_notes()` records the delivery and raises `DocNotesUnseen`, which carries the block and its text copy. It decides on the row, then again on the freshly loaded state, so notes emptied in between never refuse. The identical retry passes. The model sees the refusal as:

- **Write tools**: `{"error": "doc_notes_unseen", "message", "doc_notes": {...}}`, mapped in `chat/gemini_api/tool_handlers/docs.py`. The message is `DOC_NOTES_UNSEEN_MESSAGE` (`chat/docs/notes.py`) with the doc's title, and `doc_notes` is the block.
- **`write_doc` pre-card** (`doc_precard_check()` -> `preview_write_operation()`, which asks for the text form): the same message, a blank line and the [text copy](#text-copy), as the same-turn `Invalid parameters` text. When the copy had to cut the instructions, the seen instructions version is left where it was, so the retry is refused again.

## Instructions Pin on write_doc Cards

While the gate is open for the proposer, the pre-card injects the live row's `instructions_version` into the card's params. It is one of the `SERVER_INJECTED_KEYS` in `chat/action_request_types/write_doc.py`, so a model-supplied copy is dropped. `WriteDocHandler.execute` passes it to the service as `expected_instructions_version`, a server-only pin of `_normalize_write_params()` (`_SERVER_PINS`, never part of a `suggested_request`).

At approve time (`bypass_approval`) the notes step never consults the seen record. `_check_pinned_instructions()` refuses with `DOC_INSTRUCTIONS_CHANGED_MESSAGE` when the live `instructions_version` differs from the pin and the live instructions are non-empty. The refusal fails the Approve and leaves the request open, like a stale `old_string` ([Quest Docs -- The write_doc Action Request](quest-docs.md#the-write_doc-action-request)). A card without a pin (proposed while the gate was closed for the proposer) or a gate closed at execute is not compared, and memory changes never refuse. The card shows no notes.

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

**Why do scripts get the counts only?** A script is code, not the model, and records nothing, so a block delivered to it would vouch for nothing; the model's own write is still refused until the notes reach the model. The counts cost nothing and let a script see which docs have notes.

**Why does a closed gate turn everything off, including the creator's (0, 0) record?** A closed gate must leave every doc tool and sidecar exactly as without notes, decided by one `notes_enabled()` check per tool. Writing the creator record regardless was rejected so that no part of the feature runs for a user who does not have it; a doc created while the gate was closed is treated like any other once it opens.

## Known Limitations

- **A second write in the same model response passes.** When one response issues several writes to a doc, the first refusal records the delivery, so the others are not refused. Tracking delivery per model step was judged out of proportion.
- **The write gate is not re-checked under the per-doc lock.** It reads the versions in the first pass of `apply_write_operation()`, so an instructions change landing between that check and the write applies from the next write on.
- **A process crash can leave a record for a block the model never received**, when it lands after a read recorded its delivery and before the tool result was flushed to the chat history. A Stop is safe: the tool result joins the run's messages with no `await` after the service returns (`_emit_durable()` in `chat/gemini_api/turn_tools.py`), and the next turn replays it.
