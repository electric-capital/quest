# Doc Notes

## Overview

Doc notes are texts that stick to a [Quest Doc](quest-docs.md) outside its body and its revisions. The **instructions** are one plain-text field on the `docs` row, holding the rules a model should follow when it works on the doc. The **memories** are short plain-text facts about the doc, one `doc_memories` row each. `doc_note_suggestions` holds proposed changes to either, each reviewed by the user whose conversation proposed it, with the `MemorySuggestionStatus` lifecycle of [project memory suggestions](project-memories.md): an instructions suggestion carries the complete proposed text, a memory suggestion one fact, optionally replacing a memory. This doc covers the gate, the data model, the version counters, concurrency and caps.

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

Every doc-notes cap and budget is a constant in the doc-notes section of `chat/docs/constants.py`, explained beside it. The stores enforce the instructions and memory lengths and the memories-per-doc count.

## Access

The stores make no access decisions; callers resolve `resolve_doc_access()` in `chat/docs/access.py` first, as for every `db/doc_store.py` function.

## Constraints

- `docs` is altered in place, never rebuilt with Alembic batch mode ([Database -- Doc and DocShare Models](database.md#doc-and-docshare-models)).
- Notes are plaintext in `quest.db`, like doc metadata ([Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why are the instructions a column while memories get a table?** One field per doc, read with the doc, gains nothing from a join. Memories are a list whose items carry their own id, author and edits, which a column could hold only as a blob rewritten whole on every change.

**Why is the instructions text deferred?** List and search rows come from `select(Doc)` and need only whether instructions exist. Loading up to `DOC_INSTRUCTIONS_MAX_CHARS` per row for them was rejected; `has_instructions` answers in SQL, and raiseload turns an accidental load into an error instead of a silent per-row query.

**Why do notes changes never bump `updated_at`?** It is the body's token for editor saves, rename and restore, so a notes change would turn an open editor's save into a false conflict, the same reason share changes leave it alone.

**Why version counters rather than timestamps?** A counter moves by exactly one per landed change and compares by equality, so the compare-and-swap is immune to two writes in one clock tick or clock skew. Two counters let a consumer tell an instructions change from a memory change; `memory_count` is recounted rather than incremented so it cannot drift.
