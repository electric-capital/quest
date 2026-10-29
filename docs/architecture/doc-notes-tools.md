# Doc Notes Tools

## Overview

The two `tool_call` tools through which a model reaches [doc notes](doc-notes.md): `list_doc_notes` returns a doc's notes block, and `suggest_doc_note` files a proposed change for the user to review. This doc covers their registry specs and gate, which run kinds and prompts get each, the `list_doc_notes` result (the block, `can_suggest`, what it records), the `suggest_doc_note` dispatch order and backstop, public conversations, and the prompting: the `system:doc_notes` skill, the public prompt's notes line and the rule for the copy that tool results carry. The notes block, the seen record and the registration checks the tools rest on are in [Doc Notes](doc-notes.md).

## Key Files

| File | Role |
|------|------|
| `chat/llm/tool_schemas.py` | The two `TOOL_CALL_REGISTRY` specs and their `PUBLIC_TOOL_CALL_ALLOWLIST` and `_SKILL_DOCUMENTED_TOOLS` entries; the comment above the specs gives their gating |
| `chat/gemini_api/tool_dispatch.py` | `_tool_list_doc_notes()`, the `_tool_suggest_doc_note()` backstop, `_may_suggest_doc_notes()` (the one run-kind predicate of both) and `ToolContext.is_routine` |
| `chat/gemini_api/tool_handlers/docs.py` | The two handlers behind the gate check; the module docstring lists every error shape of the doc and notes tools |
| `chat/docs/service.py` | `list_doc_notes()`, the `list_doc_notes` body, and `DocNotesDisabled` |
| `chat/docs/notes.py` | `suggestion_allowed()` with `SLACK_DENY_CAUSES`, the taint rule `can_suggest` shares with registration, `DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE`, and the copy constants, whose rules are in the comment above them |
| `chat/docs/note_suggestions.py` | `register_suggestion()`, the `suggest_doc_note` body ([Doc Notes -- Registration](doc-notes.md#registration)) |
| `chat/gemini_api/script_tool_call.py` | `SCRIPT_TOOL_CALL_ALLOWLIST`, which holds `list_doc_notes` and not `suggest_doc_note` |
| `chat/system_skills/catalog.py`, `chat/system_skills/loader.py` | The `system:doc_notes` skill (`_doc_notes_content` and its registration), the notes pointer in `_quest_docs_content`, and the skill's gate-closed load in `load_system_skills()` |
| `chat/gemini_api/system_prompt.py` | `_doc_note_tool_names()`, the per-prompt exclusions of `suggest_doc_note`, and the public notes line (`_public_doc_notes_line()` over the `_PUBLIC_DOC_NOTES_*` constants) |
| `chat/gemini_api/conversation.py` | `run_conversation_turn()`, which passes `doc_notes_enabled` to the public prompt and `is_routine` to dispatch |

## Registry and Gate

Both specs carry `requires_service: "doc_notes"`, the pseudo-key that mirrors the per-user gate, and both are documented by the `system:doc_notes` skill (`_SKILL_DOCUMENTED_TOOLS`): a regular prompt names them on that skill's enumeration line, and loading the skill appends their full reference ([Conversation Loop -- Prompt Tiers](gemini-api.md#prompt-tiers)). The public-project prompt has no regular skill tier: it inlines every tool it offers that no public system skill (`PUBLIC_SYSTEM_SKILLS`) documents, both of these included, in its Dynamic Tools section. Neither is `mutating`: `list_doc_notes` reads, and `suggest_doc_note` only files a pending row for a person's review, so the mutating-tool refusals of inference-API and cross-user-subagent runs let both through and the [run-kind rules](#run-kinds-and-prompts) decide.

While the gate is closed for the caller (also when Quest Docs is closed), the tools and the skill are off as [Feature Gates -- Enforcement Layers (Quest Doc notes)](feature-gates.md#enforcement-layers-quest-doc-notes) describes, and a dispatched call answers `doc_notes_disabled` before its arguments, the run-kind backstop or visibility are checked ([Design Decisions](#design-decisions)).

## Run Kinds and Prompts

| Tool | Dispatched for | Listed in (gate open) |
|------|----------------|-----------------------|
| `list_doc_notes` | every run kind: conversations (private, public, routine, Slack), sub-agents, cross-user subagents, inference-API runs, scripts | every prompt: on the `system:doc_notes` line, or inline in the public prompts |
| `suggest_doc_note` | top-level conversations (private or public) and Slack conversations, routine runs excluded | the top-level and Slack prompts' `system:doc_notes` line and the public prompt, not their routine variants |

- **Prompts**: the regular prompts drop both tools, and the skill with them, through the pseudo-key; the public prompt, which has no connected-services map, excludes `_doc_note_tool_names()` unless both its `docs_enabled` and its `doc_notes_enabled` are true, so the notes tools never appear without the doc tools. The sub-agent, cross-user subagent and inference-API prompts and both routine prompts exclude `suggest_doc_note` by name, beside the memory suggest tools; the exclude set reaches the enumeration too, so their `system:doc_notes` line names `list_doc_notes` alone.
- **Loading the skill** appends the reference of both tools in every run kind (`render_tool_reference()` takes no exclude set); the skill names the runs that are not offered `suggest_doc_note`, and the backstop refuses them.
- **Routine runs** keep run kind `top_level`, so their reads record deliveries like any conversation's. `ToolContext.is_routine`, which `run_conversation_turn()` sets from the routine id, keeps `suggest_doc_note` out at dispatch.
- **Public conversations**: both tools are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private doc there, notes included, so a public conversation reads and suggests the notes of its own project's public docs only.

## list_doc_notes

`list_doc_notes()` in `chat/docs/service.py` is the body; its docstring gives the order of the checks and the result keys.

- **The block**: `doc_notes` is the [notes block](doc-notes.md#notes-block) `read_doc` attaches, the whole instructions and a page of memories from `memory_offset`, whatever the seen record says. A script gets the whole block too, although its `read_doc` carries the counts only.
- **Hidden is missing**: a hidden doc answers with the missing doc's text, byte for byte the `read_doc` result ([Quest Docs -- Concepts and Invariants](quest-docs.md#concepts-and-invariants), invariant 2).
- **Recording**: in a recording run only ([Doc Notes -- Delivery Gate](doc-notes.md#delivery-gate)), through `record_notes_delivery()` as the last step. Page 0 records both versions, like a `read_doc` delivery. A later page advances the instructions of an existing entry only: it records memories version 0, which the seen record's per-field maximum turns into an instructions-only update, so a write is not refused for instructions the model saw while the next `read_doc` still delivers the newer memories. A later page in a conversation with no entry for the doc records nothing, so its first write is still refused with the whole notes.
- **`can_suggest`**: true when the run kind may suggest (`_may_suggest_doc_notes()`, the backstop's own predicate) and `suggestion_allowed()` admits the doc's write verdict (free, approval, or denied by a Slack cause), the taint check of `register_suggestion()`. It answers for a memory suggestion: instructions ownership, the seen record and the caps are not part of it.

## suggest_doc_note

`_tool_suggest_doc_note()` checks the gate, then the run kind, then hands over to `register_suggestion()`, whose checks follow ([Doc Notes -- Registration](doc-notes.md#registration)).

- **Backstop**: `_may_suggest_doc_notes()` admits a top-level or Slack conversation with a conversation id that is not a routine run. Sub-agent, cross-user subagent, inference-API, script and routine runs, and a call without a conversation, get `{"error": "suggest_not_available", "message"}` before any argument or visibility check, and nothing is registered. The script bridge does not list the tool, so a script never reaches the backstop.
- **Results**: the `register_suggestion()` dict unchanged; a `NoteSuggestionError` as `{"error": code, "message"}`; notes the conversation has not seen as `doc_notes_unseen` with the block, the shape of the [write refusal](doc-notes.md#write-refusal) with its own message; a hidden or missing doc, and a write verdict `suggestion_allowed()` refuses, as `{"error": "<text>"}`, exactly as the doc tools answer.
- **Slack and public conversations** may suggest: the access rule decides which docs, and every suggestion waits for the user's accept.

## Prompting

The model learns about notes from the tool descriptions, the `system:doc_notes` skill and the `system:quest_docs` pointer to it, the public prompt's notes line, and the copy that tool results carry, which must work without the skill.

- **`system:doc_notes`** (`_doc_notes_content` in `chat/system_skills/catalog.py`, registered after `system:quest_docs` with `requires=DOC_NOTES_SERVICE_KEY`) is the model's guide to the notes: what the instructions and the memories are and how to treat each (the instructions govern only how the doc is changed, and a user request that one forbids is confirmed with the user first), how notes reach the model, the write refusal in both its forms and its retry (other writes of the same response included), the approve-time pin, when to suggest which kind (instructions as the complete rewritten text of an explicit standing rule, a memory as one durable fact), which store a fact belongs in (the doc's notes, a personal or a project memory), who may suggest from where, and the runs without `suggest_doc_note`. Its suggestion guidance mirrors `system:memory`'s.
- **Quoted texts**: the refusal texts the skill quotes are interpolated from their constants in `chat/docs/notes.py` and `chat/docs/access.py`, and its caps from `chat/docs/constants.py`, so the skill cannot drift from what the tools return.
- **Line text**: the skill's `description` and `when_to_load` never name `suggest_doc_note`; the `Tools:` tail of each prompt's line says which notes tools that prompt offers.
- **`system:quest_docs` pointer**: a short paragraph closing "Read before edit" in `_quest_docs_content` points at `system:doc_notes`. `system:quest_docs` is listed whenever Quest Docs is on and its content builder cannot see the notes gate, so the pointer is conditioned on what a doc result carries and reads true with the notes gate closed.
- **Public notes line**: `_public_doc_notes_line()` in `chat/gemini_api/system_prompt.py` stands in for the skill, which a public conversation cannot load. It is a separate line appended to `_PUBLIC_DOCS_SECTION` only while `docs_enabled` and `doc_notes_enabled`, so with either gate closed the public prompt carries no notes text. It covers what notes are and, like the block note, that they ask for nothing beyond changing the doc, then their delivery, the refusal and retry and `list_doc_notes`, then either how to suggest or, in a routine run, that a proposed note goes in the response, a variant that never names `suggest_doc_note`. Like the rest of the public prompt it names no internal-only tool or skill (`tests/test_public_projects.py::test_no_internal_tool_docs`).
- **Stand-alone copy**: the block's `note` (`DOC_NOTES_BLOCK_NOTE`, which the `write_doc` pre-card refusal also carries after the text copy) and the two unseen-notes messages (`DOC_NOTES_UNSEEN_MESSAGE`, `DOC_NOTE_SUGGEST_UNSEEN_MESSAGE`) reach models that never loaded the skill and runs that are not offered `suggest_doc_note`. They name no skill and name `suggest_doc_note` only conditionally. The block note limits the notes to changing the doc, as the skill does ([Design Decisions](#design-decisions)). The unseen messages say that the identical retry goes through, and the write refusal's asks for the doc's other writes in the same response to be checked against the notes, since only the first is refused ([Doc Notes -- Known Limitations](doc-notes.md#known-limitations)). The rules are in the comment above the copy constants in `chat/docs/notes.py`.
- **The approver's text**: `DOC_INSTRUCTIONS_CHANGED_MESSAGE` is the one notes string a person reads rather than the model. It is the Approve-time refusal of the [instructions pin](doc-notes.md#instructions-pin-on-write_doc-cards), which the approve route's `execution_failed` error (`chat/action_request_routes.py`) shows on the card and in the inbox, so it points the approver at the card's Revise, whose feedback has the model propose the change again.

The golden prompt snapshots (`tests/snapshots/`) are taken with every gate closed, so they carry none of this text.

## Design Decisions

**Why does a later page update only the instructions, and only an existing entry?** Every page carries the whole instructions but not the newest memories. Recording both versions was rejected because it would credit memories the model never saw, so the next `read_doc` would not deliver them. Over an existing entry (the model was shown page 0 at some point), advancing the instructions spares a refusal for instructions the model has just read. Creating an entry, even `(instructions, 0)`, was rejected: once any entry exists `write_refused()` looks at the instructions alone, so a model that paged in first would write without ever being shown the newest memories, breaking notes before writes.

**Why does `list_doc_notes` give a script the whole block when `read_doc` gives it the counts?** `read_doc` attaches the block as a delivery, which vouches for nothing when a script reads ([Doc Notes -- Design Decisions](doc-notes.md#design-decisions)); a script that calls `list_doc_notes` asked for the notes themselves, and counts would leave the tool empty for it. Either way a script records nothing, so the model's own write is still refused until the notes reach the model.

**Why does the backstop refuse routine runs, which the routine prompts already leave out?** A tool missing from a prompt can still be called, and `can_suggest` comes from the backstop's predicate: with the prompt alone, a routine's `list_doc_notes` would answer `can_suggest: true` for a tool its prompt does not offer, and the call would register. Nobody reviews a routine conversation's suggestions as it runs, so one predicate keeps routine runs out of both the tool and `can_suggest`.

**Why is `can_suggest` one bool, for a memory suggestion?** It answers what the model cannot settle from the rest of what it sees: whether its run kind and its write verdict on the doc, Slack causes included, admit a suggestion at all. A verdict over every check, or one per kind, was rejected: page 0 of the listing itself records the delivery the seen check needs, every suggestion moves the caps, and the tool descriptions state that instructions need the doc's owner, so those are left to their own refusals.

**Why is the gate checked before the run-kind refusal?** A closed gate then reads the same for every run kind, one `doc_notes_disabled` answer, as for `list_doc_notes`. Refusing the run kind first was rejected because `suggest_not_available` tells the model to put the note in its response, as if the feature were on for the user, so the answer to a closed gate would depend on where the call came from.

**Why a skill of its own rather than a section of `system:quest_docs`?** The notes have their own gate, while `system:quest_docs` is listed whenever Quest Docs is on and its content builder cannot see the notes gate, so a notes section there would teach tools the model may not have. `system:doc_notes` drops out with the gate and is the one skill documenting both tools, so loading it brings their reference with the guidance; `system:quest_docs` keeps only a pointer worded to read true either way.

**Why do the instructions govern only how the doc is changed?** Notes are written by people other than the user (a shared doc's owner writes its instructions, any editor its memories) and reach every conversation that reads the doc, public ones with internet access among them. Treating them as general directives was rejected because a doc could then steer a reader's conversation into contacting someone or revealing data. The skill states the limit, and so does the stand-alone copy (the block `note`, carried by the `write_doc` pre-card refusal too, and the public notes line): that copy is all a conversation that cannot load the skill reads, so its sentence on the limit is not redundant with the skill.

**Why do the block note and the unseen-notes messages stand alone?** They reach models that never loaded the skill, every public conversation among them, and runs that are not offered `suggest_doc_note`. Leaning on the skill was rejected: a public model cannot load it, a routine would be told to call a tool it lacks, and a refusal that did not say the retry passes could leave a model re-reading in a loop or giving up.

**Why does the skill's line never name `suggest_doc_note`?** The description and `when_to_load` read the same in every prompt that lists the skill, routine and read-only prompts included, where the tool is not offered; the `Tools:` tail, filtered by each builder's exclude set, is the per-prompt list. Naming the tool in the fixed text was rejected because it would invite those runs to make a call the backstop refuses.

**Why does the public prompt get a line instead of the skill?** A public conversation loads only `PUBLIC_SYSTEM_SKILLS`, whose content may name public tools alone, while `system:doc_notes` names internal-only skills and run kinds (`system:memory`, Slack, standalone chats). Adding it to that roster was rejected for that reason; the public prompt already stands in for `system:quest_docs` and `system:tables` with paragraphs of its own, and the notes line extends the docs one.

**Why does the instructions-changed text address the approver?** Only the person who clicked Approve reads it. Wording it for the model, as the other refusals are, was rejected because that reader cannot propose anything; Revise is how they send the model back to propose the change under the current instructions.
