# Projects Architecture

This document describes the Projects feature, which allows users to group related conversations under a shared workspace and optional project guide (custom instructions; labeled **"Project Instructions"** in the UI). The project guide is a plain text field on the project (`projects.guide`) -- it is unrelated to the deprecated per-conversation [Guides](guides.md) feature and is not deprecated with it; "guide" survives only as the backend field name.

## Overview

A Project is a container that groups one or more related conversations. All conversations in a project share a single workspace directory, enabling agents to build on prior work across conversations. Each project can have an optional guide -- custom instructions injected into the system prompt for all conversations in the project, alongside the user's per-conversation guide.

Projects can also have Skills -- reusable instruction definitions scoped to the project, with auto-load capability so they are injected into the system prompt for all project conversations (see [Skill Library Architecture](skill-library.md) for the full skill library documentation).

Projects can also have Routines -- canned prompts that create a new conversation and auto-send a prompt in one click. Routines can optionally have automatic schedules attached, allowing them to run on a timer without user interaction. See [Routines Architecture](routines.md) for the full routines documentation and [Scheduling Architecture](scheduling.md) for the scheduling system.

A project can be created as a **public project**: an immutable creation-time mode (`projects.public`) that gives its conversations an internet-enabled script sandbox while cutting them off from every internal resource (skills, memories, connectors, action requests, sub-agents). Public projects cannot have project skills, cannot be created from an existing conversation, and have routines only while the admin `public_project_routines` feature gate is open for the user. See [Public Projects Architecture](public-projects.md).

Standalone conversations (not in a project) continue to work exactly as before: each has its own workspace at `data/chats/{conversation_id}/workspace/`.

Projects also have a dedicated per-project SQLite database that the LLM can use to store and query structured data across conversations. See [Project Database Architecture](project-db.md) for the full documentation.

## Project Model

The `Project` model in `db/models.py` maps to the `projects` table. See [Database Architecture](database.md) for the column-level schema. Key constraints: composite unique index on `(user_id, name)` prevents duplicate project names per user, and `ON DELETE CASCADE` on `user_id` automatically removes projects when the user is deleted.

## Conversation-Project Association

The `Conversation` model in `db/models.py` has optional `project_id` (FK to `projects.id`, CASCADE) and `routine_id` (FK to `routines.id`, SET NULL) columns. Deleting a project cascades to its conversation metadata rows. Deleting a routine preserves conversations it created (sets `routine_id` to NULL). The `routine_id` link enables the sidebar to group conversations by their originating routine. See [Routines Architecture](routines.md) for conversation grouping details and [Database Architecture](database.md) for the full column schema.

## Data Access Layer

`db/project_store.py` provides CRUD operations for projects. All functions are `async` and use `AsyncSessionLocal`. `delete_project()` explicitly deletes project skills first (non-cascading FK on `skills.project_id`), then deletes the project row (CASCADE removes conversation rows). `delete_all_user_projects()` follows the same pattern for bulk deletion during account deletion.

`db/conversation_store.py` provides project-aware conversation functions including `create_conversation()` (accepts optional `project_id` and `routine_id`), `list_project_conversations_meta()`, `get_project_for_conversation()`, and `set_conversation_project()` (attaches a standalone conversation to a project; refuses conversations that already belong to one).

## Workspace Resolution

Two workspace roots exist, each with one resolver in `chat/storage.py`:

- **Conversation workspace**: `data/chats/{conversation_id}/workspace/` (`ChatStorage.get_conversation_workspace_root()`), for every conversation, standalone or in a project. `create_project_conversation()` creates it up front.
  The project workspace is reached through the project file routes (`/app/api/projects/{project_id}/files...`) and the copy-to-project / copy-from-project routes (see [File Browser API](../api/file-browser-api.md)).
- **Project workspace**: `data/projects/{project_id}/workspace/workspace/` (`ChatStorage.get_project_workspace_root()`), shared by all conversations in the project.

The conversation file routes (`resolve_owned_workspace`) and the tool handlers' `_get_workspace_dir()` helper (`chat/gemini_api/tool_handlers/_common.py`, which creates the dir) both resolve to the conversation workspace, project conversations included. `resolve_owned_project_workspace()` in `chat/conversation_access.py` resolves an owned project's workspace root.

## Converting a Conversation into a Project

A standalone (non-project, non-Slack) conversation can be turned into a new project via **POST `/app/api/projects/from-conversation`** (see [Projects API](../api/projects-api.md)). The endpoint creates the project and its (empty) project workspace, flips `conversations.project_id` via `set_conversation_project()`, and sets the `converted_from_standalone` and `own_workspace` flags at the top level of the conversation's `chat_history.json` (`ChatStorage.set_conversation_flag()`, best effort -- a failed flag write is logged and the conversion still succeeds). No files move: they stay in the conversation workspace `data/chats/{conversation_id}/workspace/` (created if the conversation never had one).

Cached SDK sessions are invalidated because project membership changes the system prompt (project guide and skill auto-loads).

In the frontend, the option appears as "Create Project from Chat" in the top-level conversation entry's dropdown menu in `Sidebar.tsx` (hidden for Slack conversations). It opens `ConvertToProjectModal` (`frontend/src/components/ConvertToProjectModal.tsx`, reusing the `NewProjectModal` styles), which prefills the project name from the conversation title and explains that the chat becomes the project's first conversation while its files stay with the chat (the project starts with an empty Project Files space; the user can ask the chat, or use Move to project, to share files with later chats). On success the sidebar refetches projects, drops the conversation from the top-level list, and drills into the new project, auto-selecting the moved conversation.

## Archiving a Project

A project can be archived (`projects.archived`, migration `c9e1f4a7b2d8`) -- the project-level twin of archiving a conversation. Archiving is a soft hide, not a delete:

- **GET `/app/api/projects`** leaves archived projects out unless `include_archived=true`; by-id endpoints keep working, so a drilled-into archived project still resolves and its conversations stay usable.
- The scheduler **pauses every schedule of the project's routines**: `list_enabled_schedules()` / `list_interrupted_runs()` in `db/schedule_store.py` carry `project_archived` on the routine summary, and `_schedule_paused_reason()` in `chat/scheduler.py` routes archived-project schedules (and gated public-project ones) through the shared `_skip_paused_schedule()` path -- no run, no ledger row, an anchored `next_due_at` moved past the skipped occurrence, interrupted runs not retried. Unarchiving resumes at the next regular occurrence (see [Scheduling Architecture](scheduling.md)).
- Rows, workspace, conversations, skills and routines are all kept; `set_project_archived()` in `db/project_store.py` leaves `updated_at` alone so unarchiving restores the old list position. The default of `list_projects()` includes archived projects so account deletion still collects every workspace directory.

The flag is flipped via **PUT `/app/api/projects/{project_id}/archive`** and **`/unarchive`** (see [Projects API](../api/projects-api.md)). In the UI the controls live in the Project Settings modal's Danger Zone (an Archive / Unarchive block above Delete), the sidebar Projects header has an options menu with "Show Archived" (reusing `ConversationFilterMenu`), archived rows render dimmed, and the drill-down header shows an "Archived" chip.

## Docs Access (doc sources)

A **private** project can be given read access to the [Quest Docs](quest-docs.md) of zero or more of its owner's **public** projects. The setting is per project (nothing is blanket): the owner picks the public projects in Project Settings > Docs Access, and from then on every conversation of the private project -- top-level turns, routines, Slack-driven runs, sub-agents and the other read-only run kinds -- can list, search and read those projects' docs (`list_docs` scope `public`, included in `all`), never change them. Standalone conversations have no setting and no access; public projects cannot be given sources (their conversations stay confined to their own project's docs).

- **Storage**: the `project_doc_sources` link table (`ProjectDocSource` in `db/models.py`, migration `b4d7e2a9c6f1`): one `(project_id, source_project_id)` row per grant, both FKs cascading with their project. `set_doc_source_projects()` in `db/project_store.py` replaces the list under validation (owner's private project, owner's public sources, no self-reference; `ProjectDocSourceError` with codes `public_project_no_doc_sources` / `invalid_doc_source`) and leaves `updated_at` alone; `list_doc_source_project_ids()` / `list_doc_source_projects()` read it.
- **Enforcement**: the doc access rule's project rule has one exception for public docs of a listed source (`doc_source_project_ids` on `resolve_doc_access()` in `chat/docs/access.py`); the doc service loads the project's sources once per call (`_doc_sources()` in `chat/docs/service.py`) and the store query includes the sources' public docs (`source_project_ids` on `list_accessible_docs()`). The taint rule is untouched: a private conversation's verdict on a public doc is always read-only.
- **API**: GET / PUT `/app/api/projects/{id}/doc-sources` (see [Projects API](../api/projects-api.md)).
- **UI**: the Docs Access section of `ProjectSettingsModal` (`ProjectDocSourcesSection.tsx`): a checkbox per public project of the user (archived ones flagged) from `ProjectsContext`, saved as a full replacement; hidden for public projects.

## Storage Layout

```
data/
├── projects/
│   └── {project_id}/
│       ├── project.db              # Per-project SQLite database (created lazily, see project-db.md)
│       └── workspace/
│           └── workspace/          # Project workspace shared by all project conversations
├── chats/
│   └── {conversation_id}/          # Per-conversation directory (both standalone and project)
│       ├── chat_history.json       # Messages + notice flags (includes project_id field for project conversations)
│       ├── sdk_history.json        # SDK session history
│       └── workspace/              # Conversation workspace (files created by the agent or uploaded by the user)
└── quest.db                       # SQLite database (projects table, conversations.project_id FK)
```

Chat history files (`chat_history.json`) for project conversations include a `project_id` field at the top level, plus the server-set notice flags of `CONVERSATION_NOTICE_FLAGS` in `chat/storage.py` (read with `ChatStorage.get_conversation_flags()`, written with `set_conversation_flag()`):

- `own_workspace` -- the explicit post-cutover marker: set by `create_project_conversation()` and by POST `/projects/from-conversation`, meaning the conversation has always had its own conversation workspace. A project conversation WITHOUT it that has at least one assistant message is a legacy conversation whose earlier files live in the shared project workspace. The presence of `data/chats/{id}/workspace/` is deliberately not the marker: opening a conversation's files or running any tool creates that dir as a side effect.
- `converted_from_standalone` -- set by POST `/projects/from-conversation` (files left in place).
- `legacy_shared_workspace` -- reserved for legacy project conversations.

## Project Guide Injection

The project guide is injected into the system prompt as a "Project-Specific Instructions" section, separate from the user's per-conversation guide. Both `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py` accept a `project_guide` parameter.

**Guide resolution flow in `run_conversation_turn()`:**

1. If the conversation has a `project_id`, look up the project via `get_project()` from `db/project_store.py`
2. Extract the project's `guide` field
3. Pass it as `project_guide` to `get_system_prompt()` and to sub-agent prompt builders
4. The project guide appears after the user's custom instructions section and before the tool documentation

**System prompt structure:**
```
Role description
User identity line
---
User's Custom Instructions (from per-conversation guide)
---
Project-Specific Instructions (from project guide)
---
Tool documentation and instructions
```

Both the per-conversation guide (resolved via the Guides system and snapshotted) and the project guide (resolved live from the database on each turn) can be active simultaneously. The project guide is always read live from the database (not snapshotted), so editing a project's guide takes effect on the next message in any of its conversations.

## WebSocket Integration

The persistent-WS `send_message` handler in `chat/realtime/socket.py` detects project membership automatically:

1. After validating conversation ownership, the handler reads `project_id` from the conversation metadata (`meta.get("project_id")`)
2. The `project_id` is passed through to `run_conversation_turn()` via `_run_send_message`
3. All workspace file tool calls (`list_workspace_files`, `get_workspace_file`, `write_workspace_file`, `edit_workspace_file`) receive the `project_id` via `_dispatch_tool_call()` for correct workspace resolution. These tools are dispatched through the `tool_call` meta tool pattern (see [Gemini API Integration](gemini-api.md#meta-tool-pattern-tool_call))

No project-specific protocol changes are needed -- conversations in a project use the same `WS /app/api/stream` endpoint as standalone conversations.

## Session Invalidation

When a project's guide is updated via `PUT /app/api/projects/{project_id}`, the endpoint calls `invalidate_user_sessions(user_id)` from `chat/gemini_api/session.py` to discard all cached SDK chat sessions. This ensures the next message in any conversation creates a fresh session with the updated project guide in the system prompt.

## Account Deletion

The `delete_account()` handler in `chat/routes/user.py` cleans up projects, routines, schedules, and project skills during account deletion:

1. Collects the list of user projects and conversations before deletion
2. Calls `delete_all_user_routines(user_id)` from `db/routine_store.py` (CASCADE deletes linked schedules)
3. Calls `delete_all_user_projects(user_id)` from `db/project_store.py` (explicitly deletes project skills first due to non-cascading FK, then CASCADE deletes linked conversation rows and routines)
4. Deletes each project's filesystem directory (`data/projects/{project_id}/`)
5. Deletes each conversation's chat directory (`data/chats/{conversation_id}/`)

## Frontend

The frontend provides UI components for project management:

- `NewProjectModal` (`frontend/src/components/NewProjectModal.tsx`) -- Modal for creating a new project (name input)
- `ProjectSettingsModal` (`frontend/src/components/ProjectSettingsModal.tsx`) -- Modal for editing project settings with sidebar navigation containing four sections: General (project name, project instructions -- the `guide` field's UI label), Skills, Docs Access (`ProjectDocSourcesSection.tsx`, see [Docs Access](#docs-access-doc-sources); hidden for public projects like Skills) and Danger Zone (an Archive / Unarchive Project block -- one click, reversible, neutral styling -- above the two-step Delete Project). The modal is widened with a row layout to accommodate the sidebar.

  On mobile (keyed on the `useIsMobile` hook, mirroring `SettingsModal`) it renders as a full-screen two-tier takeover: the first screen is the section list, tapping a section slides its content over the list, and a header back button (or Escape) returns to the list; mobile styling is scoped by a `project-settings-modal-mobile` class.

  The Skills section renders the `ProjectSkillsSection` component. Styles are in `frontend/src/components/ProjectSettingsModal.css`. Routines are managed separately via dedicated `NewRoutineModal` and `RoutineSettingsModal` components (see [Routines Architecture](routines.md))
- `ProjectSkillsSection` (`frontend/src/components/ProjectSkillsSection.tsx`) -- Project skills management component with three tabs: "Project Skills" (full CRUD for project-scoped skills with auto-load toggle), "My Skills" (read-only view of user's own skills with project auto-load toggle), and "Shared with Me" (read-only view of shared/public skills with project auto-load toggle). Reuses styles from `settings/SkillsSection.css`. See [Skill Library Architecture](skill-library.md) for details

**Sidebar Project Navigation** (in `frontend/src/components/Sidebar.tsx`):

The Projects section (`components/sidebar/ProjectsSection.tsx`) is always visible in the sidebar. When no projects exist, a dotted-border "Create Project" button is shown. When projects exist, a "+" button appears in the Projects section header for creating new projects, next to an options (vertical-dots) menu -- the same `ConversationFilterMenu` the conversation sections use -- with a "Show Archived" checkbox. `ProjectsContext` always fetches the full list (`fetchProjects({ includeArchived: true })`) so by-id lookups (the drilled project, `HomeComposer`, `MobileShell`) resolve archived projects too; the section filters archived rows client-side behind the toggle (state in `Sidebar.tsx`, reset on page load like the conversation filters) and renders them dimmed via the `archived` class. Each project entry displays a folder icon and a right-pointing navigation chevron. The Requests and Search nav items are positioned above the sliding panels container (`.sidebar-panels`) inside `.sidebar-content`, so they remain visible when the user drills into a project.

Clicking a project triggers a slide-left animation that reveals the project's conversations as a drill-down view. The drill-down saves the currently selected top-level conversation ID to a ref (`previousTopLevelConversationId`) before entering the project. `handleProjectDrillDown` is async: it calls `loadProjectConversations()` (which returns `Promise<Conversation[]>`) and then auto-selects the latest conversation in the project. If the project has no conversations, the main panel shows an empty state. When `activeProjectId` is set from URL params (e.g., navigating to `/projects/<pid>/<id>`), a `useEffect` auto-drills into the corresponding project.

A back button at the top of the drill-down view calls `handleDrillDownBack()`, which restores the conversation that was selected before drilling in (read from the `previousTopLevelConversationId` ref). If no conversation was selected before, the main panel returns to the empty state. The ref is cleared after restoration.

**Sidebar Routines Section** (in `frontend/src/components/Sidebar.tsx`):

When drilled into a project, the "Routines" section is always visible above the "Conversations" section (similar to how the "Projects" section is always visible in the main sidebar). When no routines exist, a "Create Routine" button is shown. When routines exist, a "+" button in the section header opens the `NewRoutineModal`. Each routine entry shows the routine name, a settings gear icon (opens `RoutineSettingsModal`), and a play button. If a routine has an active schedule, a clock icon indicator appears next to the routine name.

Clicking the play button creates a new conversation in the project (linked to the routine via `routine_id`), optionally sets the guide override, and auto-sends the routine's prompt as the first message via the `pendingRoutineMessage` mechanism in `NavigationContext`.

In the Conversations section, conversations created by routines are grouped under collapsible entries per routine (showing the routine name, conversation count badge, and chevron toggle), while standalone conversations are listed individually. Groups and standalone conversations are interleaved by recency. See [Routines Architecture](routines.md) for the full run flow, conversation grouping details, and [Scheduling Architecture](scheduling.md) for the automatic scheduling system.

## Design Decisions

**Why a shared workspace instead of per-conversation workspaces?**
Projects are designed for related work that builds on itself across conversations. A shared workspace means files created in one conversation are immediately available in the next, eliminating the need to re-upload or recreate artifacts. This mirrors how a developer would work in a single directory across multiple terminal sessions.

**Why is the project guide read live instead of snapshotted like per-conversation guides?**
Per-conversation guides are snapshotted on first message to preserve the exact instructions the conversation started with. Project guides serve a different purpose -- they provide shared context that the user may update as the project evolves (e.g., adding new requirements or API documentation). Reading the guide live from the database ensures all conversations in the project always use the latest project-level instructions.

**Why `ON DELETE CASCADE` on `conversations.project_id`?**
When a project is deleted, its conversations should be removed as well since they reference a shared workspace that will be deleted. The CASCADE constraint handles this at the database level, preventing orphaned conversation rows.

**Why store project workspace at `data/projects/{project_id}/workspace/` instead of alongside chats?**
Separating project workspaces from conversation directories makes the ownership model clear: conversation directories in `data/chats/` contain per-conversation data (chat history, SDK history), while project directories in `data/projects/` contain shared data (workspace files). This prevents confusion about which files are shared vs. conversation-specific.

**Why a composite unique index on `(user_id, name)` for projects?**
Project names should be unique per user to avoid confusion in the sidebar and project selectors. The database-level constraint prevents race conditions that application-level checks alone could miss.

## Constraints

- Each user's project names must be unique (enforced by the `ix_projects_user_id_name` composite unique index)
- Project name maximum length: 100 characters (enforced in `db/project_store.py` via `MAX_PROJECT_NAME_LENGTH`)
- Project guide maximum size: 16KB (enforced in `db/project_store.py` via `MAX_PROJECT_GUIDE_SIZE`)
- Projects are deleted when the user account is deleted (via `ON DELETE CASCADE` foreign key and explicit `delete_all_user_projects()` in the account deletion flow)
- The `projects` table and its indexes are created by the Alembic migration `f09302acbdf1`
- The `conversations.project_id` FK and index are also created by the same migration
- Deleting a project explicitly deletes project skills first (non-cascading FK on `skills.project_id`), then cascades to conversation metadata rows and routines. The caller must explicitly delete filesystem directories (project workspace and conversation chat directories)
- The project guide is injected into the system prompt alongside (not replacing) the per-conversation guide
- Project skills are managed via the Project Skills API (`/app/api/projects/{project_id}/skills`); see [Skill Library Architecture](skill-library.md) for skill constraints
- Project routines are managed via the Routines API (`/app/api/projects/{project_id}/routines`); routine schedules via the Schedules API (`/app/api/projects/{project_id}/routines/{routine_id}/schedule`); see [Routines Architecture](routines.md) and [Scheduling Architecture](scheduling.md) for constraints
- Workspace roots come only from `ChatStorage.get_conversation_workspace_root()` / `get_project_workspace_root()`; no caller appends `"workspace"` to a resolver result
