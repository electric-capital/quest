# Project Memories

## Overview

Every project, public ones included, keeps its own memories beside each user's personal ones: short facts about the project (conventions, decisions and their reasons, stakeholders, data sources, recurring pitfalls, the state of long-running workstreams) that every conversation of the project can read. They share the personal memories' shape -- a 4KB text, an archive flag, FTS5 search, read tools, the non-blocking suggestion path -- but are scoped to the project and deleted with it. They build up mostly automatically: the model suggests one whenever it learns a durable project fact, and `suggest_project_memory` is also how it handles an explicit "remember this for the project"; there is no approval card. Personal memories and their suggestions are described in [Memory Suggestions](memory-suggestions.md).

## Key Files

- `db/models.py` -- `ProjectMemory` (`project_memories`, cascading with the project) and `ProjectMemorySuggestion` (`project_memory_suggestions`, the same lifecycle as `MemorySuggestion` plus `project_id`). Created by `alembic/versions/d4f8b2a6c1e9_add_project_memories.py`, which also builds the `project_memories_fts` table and its sync triggers.
- `db/project_memory_store.py` -- project-scoped CRUD and FTS search. Functions take a project id, not a user: ownership is checked at the edges.
- `db/project_memory_suggestion_store.py` -- the suggestion rows; reuses the content normalisation, cap and `SuggestionAlreadyResolvedError` of `db/memory_suggestion_store.py`. Accept creates the project memory and archives `replaces_memory_id` in one transaction.
- `chat/project_memories.py` -- the `suggest_project_memory` tool body with the same duplicate guards as `suggest_memory`, accept / dismiss wrappers, and the `project_memory_suggestions_changed` per-user global.
- `chat/llm/tool_schemas.py` -- the three tool specs, members of `PROJECT_ONLY_TOOL_CALL_TOOLS` (hidden outside projects) and of `PUBLIC_TOOL_CALL_ALLOWLIST`, but not of `SCRIPT_TOOL_CALL_ALLOWLIST`.
- `chat/gemini_api/tool_dispatch.py` / `chat/gemini_api/tool_handlers/memory.py` -- handlers; every tool works on `ToolContext.project_id` only, and the suggest handler refuses every non-top-level run.
- `chat/gemini_api/system_prompt.py` -- `_project_memories_section()`: the "Project Memories" paragraph of every project prompt, telling the model to suggest proactively and, outside public projects, how a project memory differs from a personal one. `chat/system_skills/catalog.py` (`_memory_content`) carries the full guidance in `system:memory`.
- `chat/project_memory_routes.py` -- owner CRUD under `/app/api/projects/{id}/memories` and the suggestion review routes, mirroring `chat/memory_routes.py`.
- `chat/routes/conversations.py` -- `pending_project_memory_suggestions` on `GET /conversations/{id}`.
- `frontend/src/components/MemorySuggestions.tsx` -- `kind="project"` renders the project bubble and panel; `ChatPanel.tsx` shows both kinds side by side in `.memory-suggestions-bar`. The project kind has its own icon, wording and a fixed amber colour so it never reads as a personal suggestion under any colour theme.
- `frontend/src/components/settings/MemoriesSection.tsx` -- the memories table; with a `projectId` it manages the project's memories and is the Memories section of `ProjectSettingsModal.tsx`.

## Flow

1. The model reads with `project_memory_search` / `project_memory_list`, then proposes with `suggest_project_memory`, unprompted or because the user asked.
2. `register_suggestion()` in `chat/project_memories.py` stores the row and publishes the pending count; `useConversation.ts` feeds it into `pendingProjectMemorySuggestions` in `conversationStore.ts`.
3. The user accepts or dismisses from the project panel or the inbox's Memories tab (`/app/api/project-memory-suggestions/{id}/accept|dismiss`; the inbox lists project suggestions beside personal ones, see [Memory Suggestions -- The Inbox's Memories Tab](memory-suggestions.md#the-inboxs-memories-tab)), or edits memories directly in Project Settings.

## Who Sees Them

| Run | Read tools | Suggest |
|-----|-----------|---------|
| Project conversation (private or public), top level | yes | yes |
| Project routine run | yes | no (prompt + tier) |
| Sub-agent in a project | yes | no (dispatch refuses) |
| Standalone, cross-user subagent, inference API, script | no | no |

## Design Decisions

- **Separate tables, not a `project_id` column on `memories`.** Every personal-memory query stays untouched, so a project fact can never leak into `memory_search` of another project or a standalone chat.
- **Suggestions only, no card.** Project memories are meant to accumulate without interrupting the conversation; the review bubble is the single consent point, also for explicit requests.
- **Available in public projects.** A public conversation sees only its own project's memories, which nothing outside the project can read, and every write waits for the user's accept -- so no internal data reaches the internet-facing sandbox and nothing leaves the project. The user's personal memories stay unreachable there.
- **A second bubble, not a mixed panel.** The two stores answer different questions (who the user is vs. what the project knows); separate bubbles make the destination of each accept obvious.
