# Doc Notes Tools

## Overview

The two `tool_call` tools through which a model reaches [doc notes](doc-notes.md): `list_doc_notes` returns a doc's notes block, and `suggest_doc_note` files a proposed change for the user to review. This doc covers their registry specs and gate, which run kinds and prompts get each, the `list_doc_notes` result (the block, `can_suggest`, what it records), the `suggest_doc_note` dispatch order and backstop, and public conversations. The notes block, the seen record and the registration checks the tools rest on are in [Doc Notes](doc-notes.md).

## Key Files

| File | Role |
|------|------|
| `chat/llm/tool_schemas.py` | The two `TOOL_CALL_REGISTRY` specs and their `PUBLIC_TOOL_CALL_ALLOWLIST` and `CORE_TOOL_CALL_TOOLS` entries; the comment above the specs gives their gating |
| `chat/gemini_api/tool_dispatch.py` | `_tool_list_doc_notes()`, the `_tool_suggest_doc_note()` backstop, `_may_suggest_doc_notes()` (the one run-kind predicate of both) and `ToolContext.is_routine` |
| `chat/gemini_api/tool_handlers/docs.py` | The two handlers behind the gate check; the module docstring lists every error shape of the doc and notes tools |
| `chat/docs/service.py` | `list_doc_notes()`, the `list_doc_notes` body, and `DocNotesDisabled` |
| `chat/docs/notes.py` | `suggestion_allowed()` with `SLACK_DENY_CAUSES`, the taint rule `can_suggest` shares with registration, and `DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE` |
| `chat/docs/note_suggestions.py` | `register_suggestion()`, the `suggest_doc_note` body ([Doc Notes -- Registration](doc-notes.md#registration)) |
| `chat/gemini_api/script_tool_call.py` | `SCRIPT_TOOL_CALL_ALLOWLIST`, which holds `list_doc_notes` and not `suggest_doc_note` |
| `chat/gemini_api/system_prompt.py` | `_doc_note_tool_names()` and the per-prompt exclusions of `suggest_doc_note` |
| `chat/gemini_api/conversation.py` | `run_conversation_turn()`, which passes `doc_notes_enabled` to the public prompt and `is_routine` to dispatch |

## Registry and Gate

Both specs carry `requires_service: "doc_notes"`, the pseudo-key that mirrors the per-user gate, and both are core (`CORE_TOOL_CALL_TOOLS`), so every prompt that offers them documents them in full in its Dynamic Tools section. Neither is `mutating`: `list_doc_notes` reads, and `suggest_doc_note` only files a pending row for a person's review, so the mutating-tool refusals of inference-API and cross-user-subagent runs let both through and the [run-kind rules](#run-kinds-and-prompts) decide.

While the gate is closed for the caller (also when Quest Docs is closed, since the gate requires it), no prompt lists either tool, and every dispatched call answers `{"error": "doc_notes_disabled", "message"}` before anything else (arguments, the run-kind backstop, visibility), whatever the run kind. The prompt layers are in [Feature Gates -- Enforcement Layers (Quest Doc notes)](feature-gates.md#enforcement-layers-quest-doc-notes).

## Run Kinds and Prompts

| Tool | Dispatched for | Listed in (gate open) |
|------|----------------|-----------------------|
| `list_doc_notes` | every run kind: conversations (private, public, routine, Slack), sub-agents, cross-user subagents, inference-API runs, scripts | every prompt |
| `suggest_doc_note` | top-level conversations (private or public) and Slack conversations, routine runs excluded | the top-level, Slack and public prompts, not their routine variants |

- **Prompts**: the regular prompts drop both tools through the pseudo-key; the public prompt, which has no connected-services map, excludes `_doc_note_tool_names()` while its `doc_notes_enabled` is false. The sub-agent, cross-user subagent and inference-API prompts and both routine prompts exclude `suggest_doc_note` by name, beside the memory suggest tools.
- **Routine runs** keep run kind `top_level`, so their reads record deliveries like any conversation's. `ToolContext.is_routine`, which `run_conversation_turn()` sets from the routine id, keeps `suggest_doc_note` out at dispatch.
- **Public conversations**: both tools are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private doc there, notes included, so a public conversation reads and suggests the notes of its own project's public docs only.

## list_doc_notes

`list_doc_notes()` in `chat/docs/service.py` is the body; its docstring gives the order of the checks and the result keys.

- **The block**: `doc_notes` is the [notes block](doc-notes.md#notes-block) `read_doc` attaches, the whole instructions and a page of memories from `memory_offset`, whatever the seen record says. A script gets the whole block too, although its `read_doc` carries the counts only.
- **Hidden is missing**: a hidden doc answers with the missing doc's text, byte for byte the `read_doc` result ([Quest Docs -- Concepts and Invariants](quest-docs.md#concepts-and-invariants), invariant 2).
- **Recording**: in a recording run only ([Doc Notes -- Delivery Gate](doc-notes.md#delivery-gate)), through `record_notes_delivery()` as the last step. Page 0 records both versions, like a `read_doc` delivery. A later page records the instructions version with memories version 0, which the seen record's per-field maximum turns into an instructions-only record: a memories entry already there stays, so a write is not refused for instructions the model saw, while the next `read_doc` still delivers the memories.
- **`can_suggest`**: true when the run kind may suggest (`_may_suggest_doc_notes()`, the backstop's own predicate) and `suggestion_allowed()` admits the doc's write verdict (free, approval, or denied by a Slack cause), the taint check of `register_suggestion()`. It answers for a memory suggestion: instructions ownership, the seen record and the caps are not part of it.

## suggest_doc_note

`_tool_suggest_doc_note()` checks the gate, then the run kind, then hands over to `register_suggestion()`, whose checks follow ([Doc Notes -- Registration](doc-notes.md#registration)).

- **Backstop**: `_may_suggest_doc_notes()` admits a top-level or Slack conversation with a conversation id that is not a routine run. Sub-agent, cross-user subagent, inference-API, script and routine runs, and a call without a conversation, get `{"error": "suggest_not_available", "message"}` before any argument or visibility check, and nothing is registered. The script bridge does not list the tool, so a script never reaches the backstop.
- **Results**: the `register_suggestion()` dict unchanged; a `NoteSuggestionError` as `{"error": code, "message"}`; notes the conversation has not seen as `doc_notes_unseen` with the block, the shape of the [write refusal](doc-notes.md#write-refusal) with its own message; a hidden or missing doc, and a write verdict `suggestion_allowed()` refuses, as `{"error": "<text>"}`, exactly as the doc tools answer.
- **Slack and public conversations** may suggest: the access rule decides which docs, and every suggestion waits for the user's accept.

## Design Decisions

**Why does a later page record the instructions only?** Every page carries the whole instructions but not the newest memories. Recording both versions was rejected because it would credit memories the model never saw, so the next `read_doc` would not deliver them; recording nothing was rejected because a write would then be refused for instructions the model has just read.

**Why does `list_doc_notes` give a script the whole block when `read_doc` gives it the counts?** `read_doc` attaches the block as a delivery, which vouches for nothing when a script reads ([Doc Notes -- Design Decisions](doc-notes.md#design-decisions)); a script that calls `list_doc_notes` asked for the notes themselves, and counts would leave the tool empty for it. Either way a script records nothing, so the model's own write is still refused until the notes reach the model.

**Why does the backstop refuse routine runs, which the routine prompts already leave out?** A tool missing from a prompt can still be called, and `can_suggest` comes from the backstop's predicate: with the prompt alone, a routine's `list_doc_notes` would answer `can_suggest: true` for a tool its prompt does not offer, and the call would register. Nobody reviews a routine conversation's suggestions as it runs, so one predicate keeps routine runs out of both the tool and `can_suggest`.

**Why is `can_suggest` one bool, for a memory suggestion?** It answers what the model cannot settle from the rest of what it sees: whether its run kind and its write verdict on the doc, Slack causes included, admit a suggestion at all. A verdict over every check, or one per kind, was rejected: page 0 of the listing itself records the delivery the seen check needs, every suggestion moves the caps, and the tool descriptions state that instructions need the doc's owner, so those are left to their own refusals.

**Why is the gate checked before the run-kind refusal?** A closed gate then reads the same for every run kind, one `doc_notes_disabled` answer, as for `list_doc_notes`. Refusing the run kind first was rejected because `suggest_not_available` tells the model to put the note in its response, as if the feature were on for the user, so the answer to a closed gate would depend on where the call came from.
