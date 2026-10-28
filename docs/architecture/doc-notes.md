# Doc Notes

## Overview

Doc notes are texts that stick to a [Quest Doc](quest-docs.md) outside its body and its revisions. The **instructions** are one plain-text field on the `docs` row: the rules a model must follow when it works on the doc. The **memories** are short plain-text facts about the doc, one `doc_memories` row each, newest first. Proposed changes wait in `doc_note_suggestions` for a person to accept or dismiss them, with the lifecycle of [project memory suggestions](project-memories.md) (`MemorySuggestionStatus`); an instructions suggestion carries the complete proposed text, a memory suggestion one fact, optionally replacing a memory. This doc covers the gate, the data model, the version counters, concurrency and caps.

## Key Files

| File | Role |
|------|------|
| `config/feature_gates.py` | `FEATURE_DOC_NOTES`, its `FEATURE_REQUIRES` entry and `doc_notes_enabled_for()` |
| `chat/docs/constants.py` | The doc-notes section: caps, `DOC_NOTE_SUGGESTION_KINDS`, `DOC_NOTES_SERVICE_KEY`, `doc_notes_disabled_message()` |
| `api/instructions.py` | The `doc_notes` pseudo-key in both connected-services maps and in `PSEUDO_SERVICE_KEYS` |
| `db/models.py` | The notes columns on `Doc` (their comment says how to read the deferred text), `DocMemory`, `DocNoteSuggestion` |
| `alembic/versions/7103d39f4efb_add_doc_notes.py` | Adds the `docs` columns in place and creates both tables |
| `db/doc_store.py` | `NOTES_STATE_KEYS`, `get_notes_state()`, the instructions writers, and `delete_doc()`, which removes the notes with the row |
| `db/doc_memory_store.py` | Every `doc_memories` write; the module docstring gives the transaction shape |
| `chat/docs/routes.py` | `_doc_row()`, which leaves `NOTES_STATE_KEYS` out of UI rows |

## Feature Gate

`doc_notes` is off by default, supports per-user access and requires `docs` (`FEATURE_REQUIRES`): `doc_notes_enabled_for(email)` is true only while Quest Docs is also open for the same user. Enforcement points pass the caller's email, never the doc owner's, so notes are a per-user experience. Closing the gate deletes nothing. Layers: [Feature Gates -- Enforcement Layers (Quest Doc notes)](feature-gates.md#enforcement-layers-quest-doc-notes).

## Data Model

Columns and foreign-key actions are on the classes in `db/models.py`; the schema summary is [Database -- Doc and DocShare Models](database.md#doc-and-docshare-models).

- **Instructions** sit on the `docs` row with their version counter, last editor and time. Stored text is stripped with inner newlines kept; the empty string means none (`normalize_instructions()`).
- **Memories** cascade with the doc, and `memory_count` on the row caches their number. `source` tells a person's memory from an accepted suggestion's (`validate_source()`), and exact-duplicate text on one doc is refused.
- **Suggestions** cascade with their reviewer (the proposing conversation's user), the doc and the conversation; their memory references are nulled when that memory goes, and an instructions suggestion records the `instructions_version` it was written against.
- **Doc dicts never carry the instructions text.** The column is deferred with raiseload, and the `has_instructions` column property computes `length(instructions) > 0` in SQL. `_doc_to_dict()` adds the `NOTES_STATE_KEYS` (has instructions, memory count, both versions), enough to decide about notes without a second query; `get_notes_state()` is the one read that loads the text. UI rows drop those keys.
- **Delete.** `delete_doc()` removes the doc's suggestions and memories before the row, so they go even without `PRAGMA foreign_keys`; project and account deletes reach them through the `docs` cascades.

## Versions and Counters

- `instructions_version` grows by one per instructions change, clearing included; identical text changes nothing. It is also the instructions save's optimistic-concurrency token.
- `memories_version` grows by one per landed memory create, update or delete, and `memory_count` is recounted in the same transaction. Refusals, misses and identical-text updates bump nothing.
- Each counter moves only with its own kind of change, and no notes write touches `updated_at` (the body's concurrency token) or `last_write_source`. `update_doc_metadata()` never changes a note.

## Concurrency

- **Instructions**: `write_instructions_in_session()` is one compare-and-swap `UPDATE` on the expected `instructions_version`, read back in the same transaction, so two saves holding one token never both land. A mismatch raises `StaleInstructionsError` carrying the current state. `update_doc_instructions()` wraps it in its own session; the in-session form makes the save one step of a caller's transaction.
- **Memories**: last write wins, with no token. Every write takes the doc row's SQLite write lock first and follows the four steps of the `db/doc_memory_store.py` module docstring, so concurrent writers to one doc can neither lose a bump nor both pass the cap; the `*_in_session` helpers do the same inside a caller's transaction.
- Each store exception carries the `code` a route answers with.

## Caps

Every doc-notes cap and budget is a constant in the doc-notes section of `chat/docs/constants.py`, explained beside it. Length caps count characters after stripping, not bytes. The stores enforce both lengths and the memories-per-doc count; the pending-suggestion caps count per reviewing user, for the reason given beside `DOC_NOTE_MAX_PENDING_PER_DOC`.

## Access

The stores make no access decisions. Callers resolve `resolve_doc_access()` in `chat/docs/access.py` and decide who may write; the docstrings of `update_doc_instructions()` and `DocMemory` record the rule: the owner edits the instructions, and every editor of the doc (owner or write share) manages its memories.

## Constraints

- `docs` is altered in place, never rebuilt with Alembic batch mode ([Database -- Doc and DocShare Models](database.md#doc-and-docshare-models)).
- Notes are plaintext in `quest.db`, like doc metadata ([Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why are the instructions a column while memories get a table?** One field per doc, read with the doc, gains nothing from a join. Memories are a list whose items carry their own id, author and edits, which a column could hold only as a blob rewritten whole on every change.

**Why is the instructions text deferred?** List and search rows come from `select(Doc)` and need only whether instructions exist. Loading up to `DOC_INSTRUCTIONS_MAX_CHARS` per row for them was rejected; `has_instructions` answers in SQL, and raiseload turns an accidental load into an error instead of a silent per-row query.

**Why do notes changes never bump `updated_at`?** It is the body's token for editor saves, rename and restore, so a notes change would turn an open editor's save into a false conflict, the same reason share changes leave it alone.

**Why version counters rather than timestamps?** A counter moves by exactly one per landed change and compares by equality, so the compare-and-swap is immune to two writes in one clock tick or clock skew. Two counters let a consumer tell an instructions change from a memory change; `memory_count` is recounted rather than incremented so it cannot drift.
