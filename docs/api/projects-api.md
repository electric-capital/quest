# Projects API Documentation

This document describes the REST API endpoints for managing projects. Projects group related conversations with a shared workspace and optional project guide (custom instructions).

## Overview

The Projects API provides CRUD operations for projects and endpoints for managing project conversations. All project endpoints are defined in `chat/project_routes.py` and use the data access layer in `db/project_store.py`. The `Project` model is defined in `db/models.py`.

Projects allow users to organize conversations around a shared context. All conversations in a project share a single workspace directory (`data/projects/{project_id}/workspace/`) instead of each having its own. An optional project guide provides custom instructions injected into the system prompt alongside the user's selected per-conversation guide.

## Key Files

| File | Description |
|------|-------------|
| `chat/project_routes.py` | Project CRUD and project-conversation REST endpoints |
| `chat/project_skill_routes.py` | Project skill CRUD and project auto-load REST endpoints (`/app/api/projects/{project_id}/skills`) |
| `db/project_store.py` | Project data access layer (CRUD operations); `delete_project()` and `delete_all_user_projects()` explicitly delete project skills (non-cascading FK); the doc-source helpers `list_doc_source_projects()` / `set_doc_source_projects()` (`ProjectDocSourceError`) over the `project_doc_sources` link table |
| `db/skill_store.py` | Project skill data access layer (CRUD, auto-load management) |
| `db/models.py` | `Project`, `ProjectDocSource` (`project_doc_sources`, migration `b4d7e2a9c6f1`), `Skill` (with `project_id`), `ProjectSkillAutoload` ORM models, `Conversation.project_id` FK |
| `db/conversation_store.py` | `create_conversation()` accepts `project_id` and optional `routine_id`, `list_project_conversations_meta()` with `include_archived` filtering, `get_project_for_conversation()`, `set_conversation_project()` (attach a standalone conversation to a project), `archive_conversation()`, `unarchive_conversation()` |
| `chat/storage.py` | `ChatStorage.create_project_conversation()`, `ChatStorage.create_project_workspace()`, `ChatStorage.delete_project_workspace()`, `ChatStorage.list_project_conversations()`, `ChatStorage.get_project_workspace_root()`, `ChatStorage.set_conversation_flag()` |
| `quest.py` | Registers `project_router` |

## Authentication

All project endpoints support dual authentication: session cookie OR API key Bearer token (same as other `/app/api/*` endpoints). See [Chat API Authentication](chat-api.md) for details.

## Project Endpoints

All project endpoints are defined in `chat/project_routes.py`. Request/response models (Pydantic) are in the same file. Data access is in `db/project_store.py`.

- **GET `/app/api/projects`** -- List projects, ordered by most recently updated (`list_user_projects()`). Includes a subquery count of conversations per project. Archived projects are left out unless `include_archived=true` (mirroring GET `/app/api/conversations`); every project response carries the `archived` flag.
- **POST `/app/api/projects`** -- Create a project (`create_user_project()`). Creates a workspace directory at `data/projects/{project_id}/workspace/` via `ChatStorage.create_project_workspace()`.
  - Body accepts optional `public: bool` (default false) selecting the immutable public-project mode (internet sandbox, no internal data access; see [Public Projects Architecture](../architecture/public-projects.md)); the flag is returned on every project response and cannot be changed via PUT.
  - Public projects reject routines unless the `public_project_routines` feature gate is open for the user (400 `public_project_routines_disabled` on the routine and schedule endpoints, see [feature-gates.md](../architecture/feature-gates.md)) and always reject project-skill writes (400 `public_project_no_skills` in `chat/project_skill_routes.py`; skill list/read endpoints return empty).
- **POST `/app/api/projects/from-conversation`** -- Create a project seeded from an existing standalone conversation (`create_project_from_conversation()`). Body: `{name, conversation_id}`.
  - Validates the conversation exists, is owned by the caller, is not already in a project (400 `already_in_project`), and is not Slack-originated (400 `slack_conversation`).
  - Creates the project row and (empty) project workspace, sets `conversations.project_id` via `set_conversation_project()`, leaves the conversation's files in its own conversation workspace (`data/chats/{id}/workspace/`, created if missing) and sets the `converted_from_standalone` and `own_workspace` notice flags in its `chat_history.json` (best effort: a flag write failure is logged, never a 500), invalidates cached SDK sessions (project membership changes the system prompt), and publishes a `conversation_list_changed` event (action `moved_to_project`). Returns the project with `conversation_count: 1`.
  - Name validation and duplicate handling (409 `duplicate_name`) match POST `/app/api/projects` (shared `_create_project_checked()` helper).
  - The request body deliberately has no `public` field: converting attaches an existing private conversation (its history and its conversation-workspace files, which stay where they are) to the project, so public projects can only be created empty via plain POST `/app/api/projects`.
- **GET `/app/api/projects/{project_id}`** -- Get a project (`get_user_project()`). Scoped to the authenticated user.
- **PUT `/app/api/projects/{project_id}`** -- Update project name and/or guide (`update_user_project()`). When `guide` is updated, calls `invalidate_user_sessions()` from `chat/gemini_api/session.py` to discard cached SDK sessions.
- **PUT `/app/api/projects/{project_id}/archive`** / **PUT `/app/api/projects/{project_id}/unarchive`** -- Flip the project's `archived` flag (`archive_user_project()` / `unarchive_user_project()` via `set_project_archived()`), the project-level twins of the conversation archive endpoints. Archiving hides the project from the default list and pauses its scheduled routines (see [Scheduling Architecture](../architecture/scheduling.md)); nothing is deleted and by-id endpoints keep working, so a drilled-into archived project still resolves. `updated_at` is left untouched so unarchiving restores the project to its old position in the recency-ordered list. 404 for unknown projects and projects the caller does not own, like the other by-id endpoints. Returns the bare project row (no `conversation_count`).
- **GET `/app/api/projects/{project_id}/doc-sources`** -- The public projects whose Quest Docs this project's conversations may read (`get_project_doc_sources()`, see [Projects Architecture -- Docs Access](../architecture/projects.md#docs-access-doc-sources)). Response `{sources: [{id, name, public, archived}]}`, by name. 404 for unknown projects and projects of other users, like the other by-id endpoints.
- **PUT `/app/api/projects/{project_id}/doc-sources`** -- Replace the list (`update_project_doc_sources()` via `set_doc_source_projects()`). Body `{source_project_ids: [...]}` (full replacement, duplicates collapse, `[]` clears). 400 `public_project_no_doc_sources` when the project itself is public, 400 `invalid_doc_source` when an id is not one of the caller's public projects or is the project itself (nothing changes). Returns the new list in the GET shape. `updated_at` is left untouched.
- **DELETE `/app/api/projects/{project_id}`** -- Delete project and all conversations (`delete_user_project()`). Explicitly deletes project skills first (non-cascading FK on `skills.project_id`), then relies on `ON DELETE CASCADE` for conversation rows. Deletes workspace directory and all conversation chat directories.

## Project File Endpoints

`/app/api/projects/{project_id}/files...` (list, upload, content, download, download-folder, info, delete, create-folder, save-to-drive) browse the shared project workspace `data/projects/{id}/workspace/workspace/`; each conversation's own files stay under `/app/api/conversations/{id}/files...`, and `/conversations/{id}/files/copy-to-project` / `copy-from-project` move entries between the two. All in `chat/file_routes.py`; see [File Browser API](file-browser-api.md#project-workspace-routes).

## Project Conversation Endpoints

- **POST `/app/api/projects/{project_id}/conversations`** -- Create a conversation within a project (`create_project_conversation()`). Optionally accepts `routine_id` (a one-click routine run; in a public project this 400s `public_project_routines_disabled` unless the `public_project_routines` feature gate is open for the user). Creates chat directory and links to project workspace. Delegates to `ChatStorage.create_project_conversation()` in `chat/storage.py`, then publishes a per-user `conversation_list_changed` event (action `created`) like the standalone create endpoint, so a sidebar drilled into the project picks the new row up immediately instead of waiting for the first reply to finish or the 30s project poll.
- **GET `/app/api/projects/{project_id}/conversations`** -- List project conversations (`list_project_conversations_endpoint()`). Supports `include_archived` query param. Delegates to `list_project_conversations_meta()` in `db/conversation_store.py`.

---