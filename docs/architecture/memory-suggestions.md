# Memory Suggestions

## Overview

The model can propose a memory about the user **without interrupting the conversation**: the `suggest_memory` dynamic tool registers a pending row and returns at once, the turn keeps going, and the web UI shows a "N suggested memories" bubble above the composer. Clicking the bubble opens a panel over the conversation where the user edits, accepts (creating a `memories` row) or dismisses each suggestion. Nothing is saved until the user accepts.

This sits beside the blocking `create_memory` action request (see [Action Requests](action-requests.md)): an explicit "remember that ..." still opens the Approve / Revise / Stop card so the user confirms at once; unprompted observations go through the suggestion path. The system prompt tells the model which to use and what is worth suggesting.

## Key Files

- `db/models.py` -- `MemorySuggestion` / `MemorySuggestionStatus`: the `memory_suggestions` table (content, the model's `reasoning`, an optional `replaces_memory_id`, `status` pending / accepted / dismissed, the `memory_id` created on accept). Rows cascade with their user and conversation; both memory references are `SET NULL` on memory delete. Created by `alembic/versions/c7e2a9b4d1f8_add_memory_suggestions.py`.
- `db/memory_suggestion_store.py` -- create / list / count / accept / dismiss, all user-scoped. `list_pending()` joins the replaced memory so each row also carries `replaces_content` (its current text, or null). `accept_suggestion()` claims the row (`pending` -> `accepted`), inserts the memory (optionally with the user's edited text) and archives the replaced memory in one transaction; a second accept or dismiss raises `SuggestionAlreadyResolvedError`. The per-conversation pending cap is `MAX_PENDING_PER_CONVERSATION`.
- `chat/memory_suggestions.py` -- the tool body `register_suggestion()` with its duplicate guards (`already_saved` when an active memory holds exactly the text, `already_suggested` when the conversation already has it pending, a structured error at the cap), plus `accept_suggestion()` / `dismiss_suggestion()` wrappers; every change publishes `memory_suggestions_changed` via `publish_changed()`.
- `chat/llm/tool_schemas.py` -- the `suggest_memory` spec in `TOOL_CALL_REGISTRY` (`content`, optional `reasoning` and `replaces_memory_id`). Not `mutating`, not public-allowlisted.
- `chat/gemini_api/tool_dispatch.py` -- `_tool_suggest_memory` refuses sub-agent, cross-user subagent, inference-API and sandbox-script runs with a structured error (the backstop behind the prompt exclusions); the handler itself is `_handle_suggest_memory` in `chat/gemini_api/tool_handlers/memory.py`.
- `chat/gemini_api/system_prompt.py` -- the "Memories" rule (which path to use, what to suggest, search first) and the example call; the tool is excluded from routine, sub-agent, cross-user subagent and inference prompts. `chat/system_skills/catalog.py` (`_memory_content`) carries the full guidance in `system:memory`.
- `chat/memory_routes.py` -- `GET /app/api/conversations/{id}/memory-suggestions` (pending, oldest first; 404 for a conversation the user does not own), `POST /app/api/memory-suggestions/{id}/accept` (optional `content` override; 400 `invalid_memory`, 409 `suggestion_resolved`, 404 `suggestion_not_found`) and `POST .../dismiss`.
- `chat/routes/conversations.py` -- `GET /conversations/{id}` carries `pending_memory_suggestions` so the bubble is right on load.
- `chat/realtime/events.py` -- `make_memory_suggestions_changed(conversation_id, pending_count)`, a per-user global (listed in the FE's per-user-global allow-list in `frontend/src/services/PersistentWebSocket.ts`).
- `frontend/src/components/MemorySuggestions.tsx` -- the bubble and the review panel (per-row editable text, Accept / Dismiss, Accept all / Dismiss all, Escape and backdrop close; a 409 from another tab just drops the row). A row with `replaces_content` shows a word diff from the replaced memory to the draft (removed words struck through on red, added on green, via `frontend/src/utils/wordDiff.ts`) that follows the user's edits; without the text (memory deleted meanwhile) it falls back to a "Replaces an existing memory" pill. Rendered by `ChatPanel.tsx` above the expensive-resume card and the composer.
- `frontend/src/store/conversationStore.ts` / `frontend/src/hooks/useConversation.ts` -- `pendingMemorySuggestions`, seeded from the GET payload and replaced by every event's count; the open panel re-fetches its list when the count changes.

## Flow

1. The model calls `tool_call(tool_name="suggest_memory", arguments={content, reasoning?, replaces_memory_id?})`; `register_suggestion()` runs the guards, inserts the row and publishes the new pending count. The tool result (`status: "suggested"`, `suggestion_id`, `pending_count`) comes back immediately and the turn continues.
2. Every tab showing the conversation updates the bubble from the event; a fresh load reads `pending_memory_suggestions`.
3. The user opens the panel (`GET .../memory-suggestions`), optionally edits a row, and accepts or dismisses it. Accept creates the memory (and archives the replaced one); both publish the event, so other tabs and the bubble follow.

## Guidance the model gets

The prompt and the `system:memory` skill combine the memory rules of other harnesses: suggest only facts that are stable over time, likely to matter again and clearly about the user (role, expertise, preferences, working and writing style; corrections and confirmed approaches with the reason; ongoing work, goals and constraints with absolute dates; URLs, dashboards and tickets), never one-off task details, temporary states, anything derivable from the workspace or docs, or sensitive information unless the user asked. The model must `memory_search` the topic first, skip facts a memory already covers, and pass `replaces_memory_id` when a saved memory is outdated. One self-contained fact per call, a handful per conversation, and the reply must not restate the suggestion or claim it was saved.

## Design Decisions

- **Side channel, not a card.** The blocking card interrupts the user for every proposal; a review panel they open when they like keeps proactive suggestions cheap to ignore. The explicit-request path keeps the card because the user expects immediate confirmation there.
- **Exact-text duplicate guards only.** Semantic de-duplication is the model's job (search before suggesting); the server guards are cheap exact matches that prevent the obvious repeats without an extra model call.
- **Replace, not edit.** Memories have no agent-side edit tool. A suggestion with `replaces_memory_id` archives the old memory on accept, so corrections never leave two conflicting facts active.
- **Top-level only.** Sub-agents, cross-user subagents, inference runs, scripts and public-project conversations never reach the tool; routine runs do not see it (a canned task is not a conversation with the user).
