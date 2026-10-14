# Project Memories

## Overview

A private project keeps its own memories beside each user's personal ones: short facts about the project (conventions, decisions and their reasons, stakeholders, data sources, recurring pitfalls, the state of long-running workstreams) that every conversation of the project can read. They work like personal memories end to end -- a 4KB text, an archive flag, FTS5 search, read tools, a blocking action request for explicit "remember this for the project" requests and a non-blocking suggestion path -- but are scoped to the project and deleted with it. Personal memories and their suggestions are described in [Memory Suggestions](memory-suggestions.md).

## Key Files

- `db/models.py` -- `ProjectMemory` (`project_memories`, cascading with the project) and `ProjectMemorySuggestion` (`project_memory_suggestions`, the same lifecycle as `MemorySuggestion` plus `project_id`). Created by `alembic/versions/d4f8b2a6c1e9_add_project_memories.py`, which also builds the `project_memories_fts` table and its sync triggers.
- `db/project_memory_store.py` -- project-scoped CRUD and FTS search. Functions take a project id, not a user: ownership is checked at the edges.
- `db/project_memory_suggestion_store.py` -- the suggestion rows; reuses the content normalisation, cap and `SuggestionAlreadyResolvedError` of `db/memory_suggestion_store.py`. Accept creates the project memory and archives `replaces_memory_id` in one transaction.
- `chat/project_memories.py` -- the `suggest_project_memory` tool body with the same duplicate guards as `suggest_memory`, accept / dismiss wrappers, and the `project_memory_suggestions_changed` per-user global.
- `chat/llm/tool_schemas.py` -- the three tool specs and `PROJECT_MEMORY_TOOLS`. The tools are hidden outside projects like `PROJECT_ONLY_TOOL_CALL_TOOLS`, but are deliberately absent from `PUBLIC_TOOL_CALL_ALLOWLIST` and `SCRIPT_TOOL_CALL_ALLOWLIST`.
- `chat/gemini_api/tool_dispatch.py` / `chat/gemini_api/tool_handlers/memory.py` -- handlers; reads use `ToolContext.project_id`, the suggest handler refuses every non-top-level run and public conversations.
- `chat/action_request_types/create_project_memory.py` -- the `create_project_memory` card; its pre-card check (called from `chat/gemini_api/turn_tools.py`) requires a private project and injects the project name shown on the card.
- `chat/gemini_api/system_prompt.py` -- `_project_memories_section()`: the "Project Memories" paragraph of private-project prompts, telling the model how a project memory differs from a personal one and which path to use. `chat/system_skills/catalog.py` (`_memory_content`) carries the full guidance in `system:memory`.
- `chat/project_memory_routes.py` -- owner CRUD under `/app/api/projects/{id}/memories` (400 `public_project_no_memories` for public projects) and the suggestion review routes, mirroring `chat/memory_routes.py`.
- `chat/routes/conversations.py` -- `pending_project_memory_suggestions` on `GET /conversations/{id}`.
- `frontend/src/components/MemorySuggestions.tsx` -- `kind="project"` renders the project bubble and panel; `ChatPanel.tsx` shows both kinds side by side in `.memory-suggestions-bar`. The project kind has its own icon, wording and a fixed amber colour so it never reads as a personal suggestion under any colour theme.
- `frontend/src/components/settings/MemoriesSection.tsx` -- the memories table; with a `projectId` it manages the project's memories and is the Memories section of `ProjectSettingsModal.tsx` (hidden for public projects).

## Flow

1. The model reads with `project_memory_search` / `project_memory_list`, then proposes with `suggest_project_memory` (or the `create_project_memory` card when the user asked explicitly).
2. `register_suggestion()` in `chat/project_memories.py` stores the row and publishes the pending count; `useConversation.ts` feeds it into `pendingProjectMemorySuggestions` in `conversationStore.ts`.
3. The user accepts or dismisses from the project panel (`/app/api/project-memory-suggestions/{id}/accept|dismiss`), or edits memories directly in Project Settings.

## Who Sees Them

| Run | Read tools | Suggest |
|-----|-----------|---------|
| Private project, top level | yes | yes |
| Private project routine run | yes | no (prompt + tier) |
| Sub-agent in a project | yes | no (dispatch refuses) |
| Public project | no (allowlist) | no |
| Standalone, cross-user subagent, inference API, script | no | no |

## Design Decisions

- **Separate tables, not a `project_id` column on `memories`.** Every personal-memory query stays untouched, so a project fact can never leak into `memory_search` of another project or a standalone chat.
- **Not in public projects.** Public conversations are cut off from memories of either kind; a project memory written from an internet-facing conversation would be a persistent prompt-injection channel.
- **A second bubble, not a mixed panel.** The two stores answer different questions (who the user is vs. what the project knows); separate bubbles make the destination of each accept obvious.
