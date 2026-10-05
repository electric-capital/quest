# Skill Library Architecture

This document describes the Skill Library feature, which provides a system for creating, sharing, and discovering reusable skill definitions (instructions/prompts) across users.

## Overview

The Skill Library allows users to create named skill definitions containing instructions or prompts that can be shared with other users. Unlike guides (which are per-user with CASCADE delete on user removal), skills use SET NULL on creator deletion so they persist for other users who have access. Skills support four visibility levels: private (creator only), shared (creator + explicitly shared users via a junction table), public (all users), and project (scoped to a specific project). Project-level skills are managed through the Project Settings modal and can be auto-loaded for all conversations in a project.

In addition to DB-backed user skills, the codebase also ships a fixed catalog of **system skills** (hardcoded, non-DB, loadable through the same `list_skills`/`search_skills`/`load_skills` tools). See [System Skills](#system-skills) below.

Skills replace the deprecated [guides](guides.md) feature: guide creation is disabled, and existing guides can be converted one-click into private skills (the default guide's skill is auto-loaded). See [Guides -- Migration to Skills](guides.md#migration-to-skills).

## Skill Model

The `Skill` model in `db/models.py` maps to the `skills` table. See [Database Architecture](database.md) for the column-level schema.

Key characteristics:
- UUID string primary key (generated at application layer)
- `creator_id` uses `ON DELETE SET NULL` (not CASCADE) so skills persist when the creator is deleted
- `project_id` nullable column with non-cascading FK to `projects.id`; NULL for user-level skills, non-NULL for project-level skills
- Composite unique index `ix_skills_creator_id_name` on `(creator_id, name)` prevents duplicate skill names per creator
- Index `ix_skills_visibility` on `visibility` supports efficient filtering by visibility level
- Index `ix_skills_project_id` on `project_id` supports efficient project-scoped queries

## SkillShare Model

The `SkillShare` model in `db/models.py` maps to the `skill_shares` table. This is a junction table that tracks which users have access to skills with `visibility="shared"`.

Key characteristics:
- UUID string primary key
- Composite unique index `ix_skill_shares_skill_id_user_id` on `(skill_id, user_id)` prevents duplicate shares
- `skill_id` uses `ON DELETE CASCADE` (shares are removed when the skill is deleted)
- `user_id` uses `ON DELETE CASCADE` (shares are removed when the user is deleted)

## UserSkillAutoload Model

The `UserSkillAutoload` model in `db/models.py` maps to the `user_skill_autoloads` table. This junction table tracks which skills a user has auto-loaded (automatically included in every conversation).

Key characteristics:
- UUID string primary key
- Composite unique index `ix_user_skill_autoloads_user_id_skill_id` on `(user_id, skill_id)` prevents duplicate auto-load entries
- `user_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the user is deleted)
- `skill_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the skill is deleted)
- Users can auto-load any skill they have access to (own, shared, or public), not just their own skills

## ProjectSkillAutoload Model

The `ProjectSkillAutoload` model in `db/models.py` maps to the `project_skill_autoloads` table. This junction table tracks which skills a project has auto-loaded (automatically included in every conversation within that project, in addition to user-level auto-loads).

Key characteristics:
- UUID string primary key
- Composite unique index `ix_project_skill_autoloads_project_id_skill_id` on `(project_id, skill_id)` prevents duplicate auto-load entries
- `project_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the project is deleted)
- `skill_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the skill is deleted)
- Any skill accessible to the project can be auto-loaded: project-specific skills, user-level skills, and shared/public skills. Enabling is gated in the PUT autoload endpoint (the skill must belong to this project or pass `user_can_access_skill()` for the caller), and `get_project_autoloaded_skills()` re-checks access against the project owner at conversation time, so a stale row stops resolving once a share is revoked or the skill goes private

## RoutineSkillAutoload Model

The `RoutineSkillAutoload` model in `db/models.py` maps to the `routine_skill_autoloads` table. This junction table tracks which skills a routine has auto-loaded (automatically included in every conversation created by that routine, in addition to the user's and project's auto-load skills). See [Routines Architecture](routines.md) for the routine feature itself and the Skills nav section of `RoutineSettingsModal` that drives the toggles.

Key characteristics:
- UUID string primary key
- Composite unique index `ix_routine_skill_autoloads_routine_id_skill_id` on `(routine_id, skill_id)` prevents duplicate auto-load entries
- `routine_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the routine is deleted)
- `skill_id` uses `ON DELETE CASCADE` (auto-load entries are removed when the skill is deleted)
- Any skill the user has access to can be auto-loaded on a routine (gated by `user_can_access_skill()` in the PUT autoload endpoint)
- Table is created by the Alembic migration `41e59b874c9d`

## Visibility Levels

The `SkillVisibility` enum in `db/models.py` defines four levels:

| Level | Access |
|-------|--------|
| `private` | Creator only (default) |
| `shared` | Creator + users with a `skill_shares` entry |
| `public` | All users |
| `project` | Scoped to a specific project; cannot be shared |

## Data Access Layer

`db/skill_store.py` provides async CRUD operations, sharing management, and access checks. It follows the same pattern as `db/guide_store.py` (each function opens a fresh `AsyncSessionLocal()` session). See [Database Architecture](database.md) for the function summary table.

### Access Check Logic

The `user_can_access_skill()` function in `db/skill_store.py` determines access:

1. Creator always has access
2. Public skills are accessible to all
3. Shared skills require a `skill_shares` entry for the requesting user
4. Private skills are only accessible to the creator

### Listing Logic

The `list_accessible_skills()` function returns the union of:
- Skills created by the user
- Public skills
- Shared skills where the user has a share entry

Results are ordered by `created_at` descending. Optional filters: `owned_only` (only the user's own skills) and `visibility_filter` (filter by visibility level).

### Search Logic

The `search_accessible_skills(user_id, keyword)` function performs a case-insensitive substring match (`ILIKE`) against skill `name` and `description` fields. It applies the same access control as `list_accessible_skills()` (own + public + shared skills), returning only matching skills. Results are ordered by `created_at` descending. This function is used by the `search_skills` LLM tool (see [LLM Skill Tools](#llm-skill-tools) below).

### Validation Limits

Validation constants in `db/skill_store.py`:
- `MAX_SKILL_NAME_LENGTH`: 100 characters
- `MAX_SKILL_DESCRIPTION_LENGTH`: 500 characters
- `MAX_SKILL_CONTENT_SIZE`: 64KB (enforced on UTF-8 byte length)

## API Endpoints

User-level skill endpoints (CRUD, sharing, auto-load, search, shared-with-me) are in `chat/skill_routes.py` at `/app/api/skills`. Project skill endpoints (CRUD and project-level auto-load) are in `chat/project_skill_routes.py` at `/app/api/projects/{project_id}/skills`. All project endpoints verify project ownership before performing any operation. The conversation loaded-skills endpoint is in `chat/routes/conversations.py`. See [Skills API](../api/skills-api.md) for the full endpoint reference.

## LLM Skill Tools

Five LLM tools allow agents to discover, load, and inspect skills at runtime, complementing the user-managed enabled skills that are injected into the system prompt. These tools are defined in `BASE_TOOLS` in `chat/llm/tool_schemas.py` and are available to all tool tiers (top-level, sub-agent, nested sub-agent, Slack) -- they auto-convert to both Gemini and Anthropic tool formats.

The first three tools are the discover-and-load path (they include the `system:*` catalog); the last two are read-only inspection of the user's own DB-backed skill setup (they exclude `system:*` skills, see [Skill Inspection Tools](#skill-inspection-tools) below).

| Tool | Purpose | Handler | DB Function | Output format |
|------|---------|---------|-------------|---------------|
| `list_skills` | List all accessible skills (IDs, names, descriptions, visibility -- no content) | `_handle_list_skills()` in `chat/gemini_api/tool_handlers/skills.py` | `list_accessible_skills()` in `db/skill_store.py` | JSON |
| `search_skills` | Search skills by keyword against name and description (IDs, names, descriptions, visibility -- no content) | `_handle_search_skills()` in `chat/gemini_api/tool_handlers/skills.py` | `search_accessible_skills()` in `db/skill_store.py` | JSON |
| `load_skills` | Fetch full content of one or more skills by ID | `_handle_load_skills()` in `chat/gemini_api/tool_handlers/skills.py` | `get_accessible_skills_by_ids()` in `db/skill_store.py` | Markdown (see [load_skills Response Format](#load_skills-response-format)) |
| `list_my_skills` | Categorized listing of the user's DB skills with per-skill autoload status (no content, no `system:*`) | `_handle_list_my_skills()` in `chat/gemini_api/tool_handlers/skills.py` | `list_accessible_skills()` + `list_project_skills()` in `db/skill_store.py` | JSON |
| `get_skill` | Full detail for one DB skill by id (content, settings, owner-only share roster) | `_handle_get_skill()` in `chat/gemini_api/tool_handlers/skills.py` | `get_skill()` / `get_project_skill()` / `list_skill_shares()` in `db/skill_store.py` | JSON |

**Discovery-then-load pattern:** `list_skills` and `search_skills` intentionally omit skill content to keep responses compact. The agent uses these to identify relevant skills, then calls `load_skills` with specific IDs to fetch the full content. This avoids transferring potentially large skill content (up to 64KB each) when only browsing.

**Access control:** All five tools respect the same visibility rules as the REST API -- agents can only see skills they have access to (own, public, explicitly shared, or scoped to the conversation's project). Inaccessible or nonexistent skill IDs passed to `load_skills` are silently skipped; `get_skill` returns an error envelope for an inaccessible id.

**Dispatch:** The tools are routed via `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py`, alongside the other base tools. Each handler imports its DB function lazily to avoid circular imports.

For `list_skills` / `search_skills` / `load_skills`, the dispatch also passes `connected_services` (via `api.instructions.get_user_connected_services`), `has_project`, `base_url` (via `chat.gemini_api.constants.get_proxy_base_url`), and `api_key` so the handlers can merge the system-skill catalog with the DB result (see [System Skills](#system-skills) below).

For `list_my_skills` / `get_skill`, the dispatch passes the conversation's `project_id` so the handlers can include project-scoped skills and resolve the project/routine autoload tiers.

### Skill Inspection Tools

`list_my_skills` and `get_skill` (in `chat/gemini_api/tool_handlers/skills.py`) are read-only inspection tools over the user's DB-backed skills. Unlike the discover-and-load trio, they exclude `system:*` skills and exist so the agent can report on and reason about how the user's skill setup is configured. They are the read half of the "help users create and manage skills" capability; the write half is the `create_skill` / `edit_skill` action requests (see [Creating and Editing Skills via Action Requests](#creating-and-editing-skills-via-action-requests) below). The model is steered to read with `get_skill` before proposing an `edit_skill`.

**`list_my_skills`** returns a categorized listing. By default it includes own (user-authored) + directly-shared + public + (when the conversation is in a project) current-project skills; four optional boolean filters (`exclude_own`, `exclude_shared`, `exclude_public`, `exclude_project`, all default false) narrow the categories.

The handler merges `list_accessible_skills(user_id)` with `list_project_skills(project_id)` (project skills are excluded from `list_accessible_skills`) and dedupes by id. Each entry is compact metadata -- `id`, `name`, `description`, `visibility`, derived `category` (own/shared/public/project), `creator_name`, `creator_email`, and an inline `autoload` block (see below). No skill content bodies are returned.

**`get_skill`** returns full detail for one skill by `skill_id`: the fields above plus `content`, `creator_id`, `created_at`, `updated_at`, `is_owner`, `shares`, and `autoload`. Access is gated on `user_can_access_skill(user_id, skill_id)`, with a fallback that grants access when the skill belongs to the conversation's current project (via `get_project_skill(project_id, skill_id)`) -- necessary because `user_can_access_skill` only covers own/public/shared visibility, not `project`.

The owner-only `shares` roster is fetched via `list_skill_shares` **only when the caller is the owner** (non-owners and project skills get `shares: null`); this gating matters because `list_skill_shares` itself has no built-in access check. Project skills (no creator) report `is_owner: false`, `shares: null`.

Both `load_skills` (its DB results) and `get_skill` record the fetched skill ids into the per-conversation `skill_reads.json` sidecar (`ChatStorage.add_skill_read_ids`), which licenses `edit_skill` content edits -- see [Creating and Editing Skills via Action Requests](#creating-and-editing-skills-via-action-requests) below.

**Autoload metadata:** Both tools attach an inline `autoload` block per skill -- `{user: bool, project: bool, routines: [routine_name, ...]}` -- indicating which autoload tiers load the skill. It is computed by `_build_autoload_resolver(user_id, project_id)` in `chat/gemini_api/tool_handlers/skills.py`, which precomputes the three membership sets once per call (via `list_user_autoloaded_skill_ids` / `list_project_autoloaded_skill_ids` / `list_routine_autoloaded_skill_ids` in `db/skill_store.py`).

Because `routine_id` is not threaded through tool dispatch, the routine tier is enumerated across *all* of the project's routines by name (via `list_routines` in `db/routine_store.py`). See [Auto-load tiers](#routineskillautoload-model) above and [Routines Architecture](routines.md).

### load_skills Response Format

`load_skills` returns a plain markdown document (not JSON) on success. The renderer lives next to the handler as `_render_loaded_skills_markdown()` in `chat/gemini_api/tool_handlers/skills.py`. `list_skills` and `search_skills` continue to return JSON -- they carry only compact metadata with no long bodies, so the JSON escape tax does not apply.

The document opens with `# Loaded skills` and a counts line (`Loaded K skill(s). E error(s).`). When any requested system-skill's gate failed (e.g., the backend is disconnected or a project-only skill was requested outside a project), a `## Load errors` section is emitted **before** the successful skill blocks so the model sees failures first.

Each successful entry is rendered as its own block separated by `===` lines (surrounded by blank lines) with a `# <skill name>` heading, a short metadata header (`**ID:**`, `**Visibility:**`, `**Description:**`), and a `## Skill Content` section carrying the raw skill body verbatim (not fenced, since skill content is itself markdown prose and may contain its own fences or headings).

Unknown `system:*` ids and inaccessible DB UUIDs are silently skipped and do not contribute to either count.

When no entries resolve at all (empty input handling aside -- see below), the document surfaces the sentinel `_No skills matched the requested ids. They may not exist or you may not have access._` under the counts line.

**Error envelopes still use JSON.** Two paths return a single-line JSON error envelope (`{"error": "..."}`) rather than markdown: an empty `skill_ids` list (API-contract error), and any outer exception while loading. This matches the Gmail Simple Fetch convention (see [Gmail API -- Message Fetch Response Format](../api/gmail-api.md#message-fetch-response-format)) where per-entry failures live inside the markdown document while API-contract errors surface as JSON.

**Why markdown?** The tool output is consumed only by the LLM. A JSON envelope wrapping skill bodies forces the model to read past escaped `\n`, `\"`, and `\\` sequences for every line of skill content (up to 64KB per skill), which wastes tokens and obscures nested markdown fences inside the skill. Emitting the bodies verbatim inside a single markdown document eliminates that escape tax. This mirrors the `render_message_markdown()` / `render_batch_markdown()` approach introduced for Gmail Simple Fetch (commit `9f23b54`).

## Creating and Editing Skills via Action Requests

The agent proposes skill writes (create / edit) through the generic `create_action_request` tool, blocking on an inline Approve / Revise / Stop card -- it has no dedicated skill-write tool, mirroring the memory-write path. The two request types are `create_skill` (`CreateSkillHandler` in `chat/action_request_types/create_skill.py`) and `edit_skill` (`EditSkillHandler` in `chat/action_request_types/edit_skill.py`); both are web-only (action requests are disabled in Slack-driven runs).

The full per-type parameter semantics, ownership / visibility / sharing rules, the auto-clear-on-leave-`shared` behavior, the project-only `project_autoload` toggle, and the dual name-collision guards are documented in [Action Requests -- request types](action-requests.md) and the [Skill Pre-card Checks](action-requests.md#skill-pre-card-checks) section. Highlights specific to the skill model:

- **Targets**: `create_skill` targets a user-level skill (default) or a project skill (`target="project"`, gated on owning the conversation's project). `edit_skill` infers the target from the looked-up skill's `project_id`.
- **Content edits are a search-and-replace**: `edit_skill` takes an `old_string` / `new_string` pair (+ optional `replace_all`) mirroring `edit_workspace_file`, never a full-body `content` overwrite.
  - They are gated on the conversation having loaded or read the skill -- tracked in the per-conversation `skill_reads.json` sidecar (`ChatStorage.get_skill_read_ids` / `add_skill_read_ids` in `chat/storage.py`), populated by system-prompt auto-loads, composer-loaded skills, `load_skills`, `get_skill`, and `create_skill`.
  - The replacement is applied against the current content at both proposal time (which also injects the `content_diff` the approval card renders as a collapsible line diff) and Approve time (TOCTOU close).
  - Shared logic in `chat/action_request_types/_skill_content_edit.py`; details in [Action Requests](action-requests.md).
- **Sharing mutation**: user skills accept `share_emails` on create and `add_share_emails` / `remove_share_emails` on edit (only while `shared`); the route-layer email->id resolution pattern (unknown / self silently skipped) is reused at execute time. Moving visibility away from `shared` wipes the roster via `clear_skill_shares()` in `db/skill_store.py`.
- **Project auto-load**: `edit_skill` exposes a project-only `project_autoload: bool` that toggles the `project_skill_autoloads` flag via `set_project_skill_autoload()`, reusing the same single-owner access gate as the REST `PUT .../skills/{skill_id}/autoload` endpoint. There is no user/routine auto-load toggle on the write path.
- **`system:*` skills are not editable** (rejected by `is_system_skill_id()` in `validate_params`).
- **`_skill_to_dict()`** in `db/skill_store.py` now includes `project_id` so `edit_skill` and the pre-card check can branch user-vs-project from the returned dict.

The model-facing parameter spec lives in the `system:skill_management` system skill (see the catalog below), which co-documents the read inspection tools and these write request types.

## System Skills

System skills are hardcoded (non-DB) skills that carry per-backend API documentation. They used to live inline in the system prompt on every turn; moving them into loadable skills shrinks the always-present prompt and lets the model pull in only the backend(s) it needs.

### Identifier Scheme

- System skill ids use the reserved prefix `system:` (e.g. `system:slack`, `system:gmail`). `SYSTEM_SKILL_PREFIX` is defined in `chat/system_skills/catalog.py`.
- DB skill ids are UUID strings and never collide with `system:` ids.
- `is_system_skill_id(skill_id)` in `chat/system_skills/loader.py` is the predicate tool handlers use to route an incoming id to the catalog vs the DB store.

### Visibility

System skills expose the string literal `"visibility": "system"` in tool results. This is distinct from the `SkillVisibility` enum in `db/models.py` (which is DB-only and still has exactly four values: `private`, `shared`, `public`, `project`). The `"system"` literal is attached by the tool handlers; no database row exists for a system skill.

### Catalog

The catalog lives in `chat/system_skills/catalog.py`. Each entry is a `SystemSkill` dataclass with an `id`, `name`, `description` (≤120 chars, enforced by `_register()`), a `when_to_load` hint, an optional `requires` gate (`connected_services` key), an optional `requires_project` flag, and a `content_builder: (base_url, api_key) -> str` callable. Backend skills delegate `content_builder` to the existing `api/*.get_instructions(base_url)` function for that backend; cross-cutting skills (memory / workspace / action_requests / project_db) build their content from strings in `catalog.py` itself.

Current catalog (registered in order):

| ID | Gate | Summary |
|----|------|---------|
| `system:gmail` | `google_services` | Gmail Simple tools + Raw API read/draft/send-self/archive (delegates to `api.gmail.get_instructions`) |
| `system:calendar` | `google_services` | Google Calendar reads via `authed_get` plus the `create_calendar_invite` / `edit_calendar_event` action-request specs (delegates to `api.calendar.get_instructions`) |
| `system:drive` | `google_services` | Google Drive reads, `download_drive_file`, Save-to-Drive, plus `upload_to_drive` / `create_drive_folder` action-request specs (delegates to `api.drive.get_instructions`) |
| `system:docs` | `google_services` | Google Docs reads (delegates to `api.docs.get_instructions`) |
| `system:sheets` | `google_services` | Google Sheets reads (delegates to `api.sheets.get_instructions`) |
| `system:slides` | `google_services` | Read-only Google Slides reads via `authed_get` (get presentation + single page; write verbs blocked) plus Drive mimeType listing (delegates to `api.slides.get_instructions`). See [Slides API](../api/slides-api.md) |
| `system:tasks` | `google_services` | Google Tasks reads (delegates to `api.tasks.get_instructions`) |
| `system:slack` | `slack` | Slack reads, `find_slack_channel`, `send_slack_dm_to_self` (self-DMs incl. workspace-file attachments), plus `send_slack_message` / `send_slack_dm` action-request spec (delegates to `api.slack.get_instructions`) |
| `system:telegram` | `telegram` | Telegram reads plus `send_telegram_message` action-request spec (plugin-registered from `plugins/telegram`, content from its `instructions.md`) |
| `system:twitter` | `twitter` | Twitter/X reads via `authed_get` plus `send_twitter_dm` action-request spec (plugin-registered from `plugins/twitter`, content from its `instructions.md`) |
| `system:airtable` | `airtable` | Airtable reads (delegates to `api.airtable.get_instructions`) |
| `system:github` | `github` | GitHub reads plus `github_get_job_log` tool (plugin-registered from `plugins/github`, content from its `instructions.md`) |
| `system:federal_register` | — (none) | Federal Register reads via `authed_get` -- documents, agencies, public-inspection docs (delegates to `api.federal_register.get_instructions`). Ungated: the API is free/public and needs no connection, so it is always visible/loadable. See [Federal Register API](../api/federal-register-api.md) |
| `system:memory` | — | Full semantics of `memory_search` / `memory_list` plus the `create_action_request(request_type="create_memory", ...)` write path |
| `system:workspace` | — | Workspace file tools, `run_python` vs `run_script` patterns, sandbox details (incl. when to use the pre-installed pypdf/PyPDFForm libraries vs `get_workspace_file` for PDFs), plus guidance to put non-user-facing scratch in hidden dot-prefixed dirs (`.temp/`, cross-linked to `authed_get`'s `.responses/`) since the file browser hides them by default |
| `system:action_requests` | — | Generic approval / cancel / retry mechanics for `create_action_request`; points at per-backend skills for request-type specs; includes an "Inside a sub-agent" subsection covering the `agent_task_response` escape hatch |
| `system:skill_management` | — | Skill inspection (`list_my_skills` / `get_skill`) plus the `create_skill` / `edit_skill` action-request specs (user-vs-project targeting, visibility/share rules, project auto-load toggle, collision behavior); content built from a string in `catalog.py` |
| `system:quest_docs` | `docs` (pseudo-key: the per-user Quest Docs feature gate) | Quest Docs tools (list / search / read / create / edit / append / add image), modes and write verdicts, read-before-edit, the `approval_required` -> `write_doc` action-request handoff, images, paging, the routine append pattern, and the script bridge. Not `system:docs` (that is Google Docs). See [Quest Docs](quest-docs.md#prompting) |
| `system:routines` | — (requires project) | Project-routine inspection (`list_routines`) plus the `create_routine` / `edit_routine` action-request specs (name / full-replacement prompt / model / schedule / skill auto-loads); content built from a string in `catalog.py`. See [Routines Architecture -- Agent Tools](routines.md#agent-tools) |
| `system:project_db` | — (requires project) | `project_db_query` usage, schema/limits, when to create tables |

### Visibility Gating

`list_system_skills(connected_services, has_project)` in `chat/system_skills/loader.py` filters the catalog:
- A skill with a `requires` gate is visible only when `connected_services[requires]` is truthy. Passing `connected_services=None` is treated as "include all backend skills" (used by tests and admin tooling).
- A skill with `requires_project=True` is visible only when `has_project` is true.
- Cross-cutting skills without gates are always visible (except `system:routines` and `system:project_db`, which use `requires_project`).

### Enumeration in the System Prompt

`build_system_skills_enumeration(connected_services, has_project)` in `chat/system_skills/loader.py` renders a compact Markdown bullet list (one line per visible skill: id, description, when-to-load hint) prefixed by a short header directing the model to `load_skills(skill_ids=[...])` before calling that backend's APIs. The block is inserted by `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py`, immediately after the baseline tools list and before the `**Important rules:**` section. When no skills are visible the builder returns an empty string.

### Tool Integration

The three existing skill tools handle `system:*` ids uniformly:

- `_handle_list_skills()` and `_handle_search_skills()` in `chat/gemini_api/tool_handlers/skills.py` prepend system-skill entries (with `visibility: "system"` and a `when_to_load` field) to the DB-resolved results. Search performs a case-insensitive substring match against the skill's id, name, description, and `when_to_load`.
- `_handle_load_skills()` partitions incoming `skill_ids` via `is_system_skill_id()`: `system:*` ids go to `load_system_skills()`, other ids go to `db.skill_store.get_accessible_skills_by_ids()`. Results are merged preserving call order.
  - System ids whose gate fails yield an internal entry with an `error` key ("not connected — connect in Settings > Data Connections" or "project-only") instead of `content`; for `system:quest_docs`, whose gate is the `docs` feature-gate pseudo-key rather than a connector, the error is `docs_disabled_message()` (points at Settings > Features, the same text the doc tools return). Unknown `system:*` ids are silently skipped.
  - The merged entry list is then handed to `_render_loaded_skills_markdown()` (see [load_skills Response Format](#load_skills-response-format)) -- gate-failed entries become bullets under `## Load errors`, successful entries become `===`-separated blocks.

The `list_skills` / `search_skills` / `load_skills` tool descriptions in `chat/llm/tool_schemas.py` document the system-id scheme and call out that system and DB ids can be mixed in a single `load_skills` call.

### Auto-load

System skills are not eligible for auto-load. The enumeration block is the only always-on signal; the model decides per-turn which skills to pull in. Auto-load remains a DB-only concept (`user_skill_autoloads` / `project_skill_autoloads`).

## Design Decisions

**Why SET NULL on creator deletion instead of CASCADE?**
Skills can be shared with or visible to other users. If the creator deletes their account, those users should retain access to the skill. SET NULL preserves the skill with a NULL `creator_id`, indicating the original creator no longer exists. This differs from guides, which are personal to each user and should be cleaned up on deletion.

**Why a separate skill_shares junction table instead of a JSON column?**
A junction table allows efficient querying from both directions (which users can access a skill, and which skills a user can access), supports database-level cascade deletion, and enables proper unique constraints to prevent duplicate shares. JSON columns would require full-table scans for access checks.

**Why resolve emails to user IDs in the route layer?**
The share endpoint accepts email addresses (user-friendly) and resolves them to integer user IDs before passing to the store layer. This keeps the store layer clean (works with IDs only) while providing a convenient API. Unknown emails are silently skipped, and sharing with oneself is prevented.

**Why 64KB max content instead of 16KB like guides?**
Skills are intended to be comprehensive, reusable instruction sets that may include detailed examples and multi-step workflows. The larger limit accommodates this use case while still preventing abuse.

**Why are skills NOT snapshotted like guides?**
Guides are snapshotted into `chat_history.json` on first message so that a conversation preserves the guide content even if the guide is later edited or deleted. Skills are intentionally not snapshotted -- each message resolves the current set of enabled skills and their current content. This means edits to a skill are immediately reflected in all conversations that have it enabled, and disabling a skill removes it from future messages without affecting the conversation history. This design treats skills as living, cross-conversation resources rather than per-conversation configuration.

**Why silently skip inaccessible auto-loaded skills?**
A user's auto-loaded skills may include skills that have been deleted, made private by another user, or had their share revoked. The `get_user_autoloaded_skills()` function applies access control filtering, so inaccessible skills are automatically excluded without raising errors. The `ON DELETE CASCADE` foreign keys on `user_skill_autoloads` also clean up rows when skills are deleted.

**Why provide LLM tools for skill discovery in addition to enabled skill injection?**
Enabled skills are injected into every system prompt, consuming context window on every turn. The LLM skill tools (`list_skills`, `search_skills`, `load_skills`) let the agent discover and load skills on demand -- useful when the user asks about available skills, when the agent needs a skill that is not currently enabled, or when browsing the skill library programmatically. The two mechanisms are complementary: enabled skills provide always-available context, while the tools provide on-demand access.

**Why a non-cascading FK on `skills.project_id` instead of CASCADE?**
The skills table is shared between user-level and project-level skills. Using a cascading FK would delete project skills automatically when a project is deleted, but the FK also needs to support the case where project_id is NULL (user-level skills). The non-cascading FK requires explicit cleanup in `delete_project()` and `delete_all_user_projects()` in `db/project_store.py`, which delete project skills before deleting the project row. This explicit approach makes the cleanup visible and avoids unexpected behavior.

**Why force `visibility='project'` for project-scoped skills?**
Project skills are inherently scoped to a single project and should not participate in the user-level sharing model (private/shared/public). The dedicated `project` visibility level makes this distinction explicit at the data layer and prevents project skills from appearing in user-level skill listings.

**Why merge user and project auto-loads with deduplication?**
A user might auto-load a skill at the user level and the same skill might be auto-loaded at the project level. Deduplicating by skill ID ensures the skill is only injected once into the system prompt, avoiding redundant content. User-level auto-loads are processed first so they take precedence.

**Why split skill discovery into list/search and load as separate tools?**
Skills can be up to 64KB each. Listing or searching all accessible skills with full content would produce very large tool results. By returning only metadata (ID, name, description, visibility) from `list_skills` and `search_skills`, the agent can browse efficiently and then selectively load only the skills it needs via `load_skills`.

**Why pass pre-resolved skills content to sub-agents?**
Rather than having each sub-agent independently resolve enabled skills from user settings, the parent's `run_conversation_turn()` resolves skills once and passes the resulting content string through. This avoids redundant database queries and ensures all sub-agents in a conversation turn see the same skill content as the parent.

## Constraints

- Skill names must be unique per creator (enforced by the `ix_skills_creator_id_name` composite unique index)
- Skill name maximum length: 100 characters (enforced in `db/skill_store.py` via `MAX_SKILL_NAME_LENGTH`)
- Skill description maximum length: 500 characters (enforced in `db/skill_store.py` via `MAX_SKILL_DESCRIPTION_LENGTH`)
- Skill content maximum size: 64KB (enforced in `db/skill_store.py` via `MAX_SKILL_CONTENT_SIZE`)
- Only the creator can update, delete, or manage shares for a skill
- Skills persist when the creator is deleted (via `ON DELETE SET NULL` on `creator_id`)
- Skill shares are deleted when the skill or the shared-with user is deleted (via `ON DELETE CASCADE` on both FKs)
- Auto-load entries are deleted when the skill or the user is deleted (via `ON DELETE CASCADE` on both FKs in `user_skill_autoloads`)
- Users can auto-load any skill they have access to (own, shared, or public), enforced by `user_can_access_skill()` check in the `PUT /app/api/skills/{skill_id}/autoload` endpoint
- Project skills have `visibility='project'` and cannot be shared via the skill_shares mechanism
- Project skills are explicitly deleted before project deletion (non-cascading FK on `skills.project_id`)
- Project auto-load entries are deleted when the project or skill is deleted (via `ON DELETE CASCADE` on both FKs in `project_skill_autoloads`)
- Routine auto-load entries are deleted when the routine or skill is deleted (via `ON DELETE CASCADE` on both FKs in `routine_skill_autoloads`)
- Users can auto-load any skill they have access to on a routine; the `PUT .../routines/{rid}/skills/{sid}/autoload` endpoint enforces `user_can_access_skill()` before persisting
- The `skills` and `skill_shares` tables are created by Alembic migration `92ea0f869803`; the `user_skill_autoloads` table is created by migration `503d2eb4046f`; the `skills.project_id` column and `project_skill_autoloads` table are created by migration `cc3c9d86f70f`; the `routine_skill_autoloads` table is created by migration `41e59b874c9d`

## Frontend Architecture

### User Settings UI

The "Skills" section in `settings/SkillsSection.tsx` is a category nav item in the user SettingsModal alongside Data Connections, Memories, Guides, LLM Instructions, and Sign Out. It provides full skill management with a tabbed interface:

**Tab layout:** "My Skills" / "Shared with Me" tabs at the top of the section.

**"My Skills" tab:**
- List all skills owned by the current user (fetched with `owned=true` filter)
- Create new skills with name, description (optional), content, and visibility selector (private/shared/public)
- Edit existing skills inline (name, description, content, visibility)
- Delete skills with confirmation dialog
- Visibility badges color-coded by level (private: neutral, shared: purple, public: green)
- "Shares" button appears on shared-visibility skills, opening an inline share management panel
- Share management: list current shares (email + name), add shares by email, remove shares
- Auto-load toggle on each skill card (with tooltip: "Auto-loaded skills are loaded in every conversation")
- Save status feedback ("Skill saved" / error message with auto-dismiss)
- Skills loaded on-demand when the Skills section is activated (not on modal open)

**"Shared with Me" tab:**
- Lists skills accessible to the user but not created by them (public + explicitly shared skills), fetched via `fetchSharedWithMeSkills()`
- Each skill card shows creator info, description, and an auto-load toggle
- Shared skills are loaded lazily on first tab activation
- Auto-load toggle works the same as on the "My Skills" tab

### Project Settings Skills UI

The "Skills" section in `ProjectSettingsModal.tsx` renders the `ProjectSkillsSection` component (`frontend/src/components/ProjectSkillsSection.tsx`). The ProjectSettingsModal has been restructured with a sidebar navigation containing three sections: General (project name and guide), Skills, and Danger Zone (delete project). The modal is widened to accommodate the sidebar layout.

The `ProjectSkillsSection` component provides a three-tab interface:

**"Project Skills" tab:**
- Lists all skills scoped to this project (fetched via `fetchProjectSkills(projectId)`)
- Full CRUD: create new project skills with name, description, content
- Edit and delete existing project skills
- Project skills always have `visibility='project'`
- Auto-load toggle on each skill card (persisted via `setProjectSkillAutoload()`)

**"My Skills" tab:**
- Read-only view of the current user's own skills (fetched via `fetchSkills(owned=true)`)
- Auto-load toggle on each skill card for project-level auto-loading (persisted via `setProjectSkillAutoload()`)
- Skills are not editable from this tab

**"Shared with Me" tab:**
- Read-only view of skills shared with or public to the current user (fetched via `fetchSharedWithMeSkills()`)
- Auto-load toggle on each skill card for project-level auto-loading (persisted via `setProjectSkillAutoload()`)
- Skills are not editable from this tab

The component reuses styles from `settings/SkillsSection.css` for consistent appearance. Auto-loaded skill IDs for the project are fetched on mount via `fetchProjectAutoloadedSkillIds(projectId)`.

### Auto-load State and Persistence

Auto-loaded skills are tracked at two levels:

**User-level auto-loads** are tracked via a `Set<string>` of skill IDs, persisted server-side in the `user_skill_autoloads` junction table:

- **Loading:** On mount, `fetchAutoloadedSkillIds()` calls `GET /app/api/skills/autoloaded` to get the current auto-loaded IDs
- **Toggling:** `setSkillAutoload(skillId, enabled)` calls `PUT /app/api/skills/{skill_id}/autoload` to toggle auto-load for a skill
- **Access control:** Users can auto-load any skill they have access to, including shared and public skills (not just their own)

**Project-level auto-loads** are tracked via a `Set<string>` of skill IDs, persisted server-side in the `project_skill_autoloads` junction table:

- **Loading:** On mount, `fetchProjectAutoloadedSkillIds(projectId)` calls `GET /app/api/projects/{project_id}/skills/autoloaded` to get the current project auto-loaded IDs
- **Toggling:** `setProjectSkillAutoload(projectId, skillId, enabled)` calls `PUT /app/api/projects/{project_id}/skills/{skill_id}/autoload` to toggle auto-load for a skill in the project
- **Scope:** Project auto-loads can include project-specific skills, user-level skills, and shared/public skills

### System Prompt Injection

When a user sends a message, auto-loaded skills are resolved and injected into the system prompt. This happens on every message -- skills are NOT snapshotted (unlike guides). Each message uses the current set of auto-loaded skills and their current content.

**Data flow (API mode):**

1. `run_conversation_turn()` in `chat/gemini_api/conversation.py` calls `get_user_autoloaded_skills(user_id)` from `db/skill_store.py`
2. If the conversation belongs to a project, also calls `get_project_autoloaded_skills(project_id)` from `db/skill_store.py`
3. If the conversation was created by a routine (`routine_id` is passed through by the realtime socket handler, scheduler, and wait-handle resume path), also calls `get_routine_autoloaded_skills(routine_id, user_id, project_id)` from `db/skill_store.py`
4. Merges all three sets of auto-loaded skills, deduplicating by skill ID. Precedence order is user -> project -> routine (the first occurrence wins)
5. If non-empty, builds a skills content string by joining `### {skill_name}\n{skill_content}` blocks separated by double newlines
6. Passes `skills_content` to `get_system_prompt()` in `chat/gemini_api/system_prompt.py`
7. The system prompt builder inserts it as an "Enabled Skills" section between Custom Instructions and Project-Specific Instructions
8. For sub-agents, the same pre-resolved `skills_content` string is passed through to `_run_sub_agent()` and `_run_parallel_sub_agents()` in `chat/gemini_api/sub_agent.py`

**System prompt section ordering:**

1. Identity line (who the user is)
2. User's Custom Instructions (from guide)
3. Enabled Skills
4. Project-Specific Instructions (from project guide)
5. Tool documentation and API docs

**User auto-load resolution:** `get_user_autoloaded_skills(user_id)` in `db/skill_store.py` joins `user_skill_autoloads` with `skills` and applies access control filtering (creator, public, or shared-with-user). Results are ordered by name for deterministic prompt ordering. Skills that the user can no longer access (e.g., share revoked, skill deleted) are automatically excluded.

**Project auto-load resolution:** `get_project_autoloaded_skills(project_id)` in `db/skill_store.py` joins `project_skill_autoloads` with `skills`. Results are ordered by name for deterministic prompt ordering. The conversation loop merges project auto-loads with user auto-loads, deduplicating by skill ID so the same skill is not injected twice.

**Routine auto-load resolution:** `get_routine_autoloaded_skills(routine_id, user_id, project_id)` in `db/skill_store.py` joins `routine_skill_autoloads` with `skills`, scoped through the `routines` row to the conversation owner and project (a routine id that isn't one of the owner's routines in that project resolves to no skills -- defense in depth behind the create-endpoint check in [routines.md](routines.md)). Results are ordered by name for deterministic prompt ordering.

The conversation loop merges routine auto-loads after user and project auto-loads, deduplicating by skill ID. The `routine_id` is sourced from the conversation metadata in the persistent-WS send handler (`chat/realtime/socket.py:_handle_send_message`), the scheduler (`chat/scheduler.py:_execute_scheduled_run`), and the wait-handle resume path (`chat/wait_handles/resume.py:_run_resume`) so routine auto-loads apply uniformly to manual sends, scheduler-driven runs, and resumes after Slack-reply / action-request suspensions.

### Sharing UI Flow

When the user clicks "Shares" on a shared-visibility skill:

1. The skill card switches to a share management view (replaces the normal card content)
2. Existing shares are loaded via `fetchSkillShares(skillId)` and displayed as a list of email/name rows with "Remove" buttons
3. A type-ahead autocomplete input replaces the previous plain email input. As the user types (debounced), `searchUsers(query)` calls `GET /app/api/users/search?q=` to find matching users by name or email. Results appear in a dropdown showing both name and email; clicking a result adds the share immediately via `addSkillShares()`
4. "Done" button closes the share management view and returns to the normal card display
5. Unknown emails are silently skipped by the backend; sharing with oneself is prevented (the search endpoint also excludes the current user from results)

## Conversation Skill Loader

In addition to auto-loaded skills (which apply to every conversation), users can manually load skills into a specific conversation via the Skill Selector Modal. Conversation-loaded skills persist for the entire conversation from the point they are loaded and are injected as a `<conversation_skills_loaded>` XML section in the user message envelope.

### Frontend UI

A "+ Skill" button appears next to the guide selector dropdown under the message input box in `ChatPanel.tsx`. Clicking it opens the `SkillSelectorModal`, which is modeled after `SearchModal.tsx` and uses `createPortal` for rendering.

**SkillSelectorModal behavior:**

- On open, fetches all accessible skills via `fetchSkills()` and auto-loaded skill IDs via `fetchAutoloadedSkillIds()` in parallel
- Client-side keyword filtering against skill name and description (no server-side search call needed)
- Multi-select checkboxes: users check the skills they want loaded
- Already-loaded skills (from earlier in the conversation) appear greyed out with a "loaded" badge and disabled checkbox
- Auto-loaded skills appear greyed out with an "auto-loaded" badge and disabled checkbox
- Non-private skills show a visibility badge ("shared" or "public")
- Footer shows confirm and cancel buttons; confirm is disabled when no skills are checked
- Escape key or overlay click closes the modal

**State management:**

- `ConversationSkillsContext` maintains two per-conversation maps: `conversationQueuedSkills` (skills selected but not yet sent) and `conversationLoadedSkills` (skills already sent in a message)
- When the user confirms skills in the modal, they are queued via `setQueuedSkillsForConversation()`
- On the next message send, `ChatPanel` reads the queued skills, includes them as `skill_ids` in the persistent-WS `send_message` payload, then moves them from queued to loaded state
- On conversation load, `useConversation` hydrates the loaded skills set from the server via `fetchConversationLoadedSkills()`

### Backend Flow

1. Frontend sends a `send_message` op on the persistent WS with a `skill_ids` array (see `WebSocketSendMessage` in `frontend/src/api/types.ts`)
2. `chat/realtime/socket.py:_handle_send_message` extracts `skill_ids` from the payload and forwards them to `_run_send_message`, which passes them through to `run_conversation_turn()`
3. `run_conversation_turn()` in `chat/gemini_api/conversation.py` resolves the skill contents via `get_accessible_skills_by_ids(user_id, skill_ids)` from `db/skill_store.py`
4. Resolved skill content is built as `### {name}\n{content}` blocks joined by double newlines
5. The skill content is passed to `_wrap_message_with_metadata()`, which injects it as a `<conversation_skills_loaded>` XML section between `<message_metadata>` and `<message>` in the user message envelope
6. `ChatStorage.add_loaded_skill_ids(conversation_id, skill_ids)` persists the loaded skill IDs to `data/chats/{id}/loaded_skills.json` (merges with any previously loaded skills)
7. The `<conversation_skills_loaded>` section includes a preamble instructing the LLM that these skills are persistent instructions for the entire conversation

### REST Endpoint

**Endpoint:** `GET /app/api/conversations/{conversation_id}/loaded-skills` (see `chat/routes/conversations.py`)

Returns the list of manually loaded skill IDs for a conversation. Used by the frontend to hydrate the loaded skills state when a conversation is loaded.

**Auth:** Required (ownership-checked via `conversation_store.get_conversation_meta()`)

**Response:** `{"skill_ids": [...]}` where each entry is a skill UUID string. Returns an empty list if no skills have been loaded.

### Persistence

Loaded skill IDs are stored in `data/chats/{conversation_id}/loaded_skills.json` as `{"skill_ids": [...]}`. The file is created on first skill load and updated (merged) on subsequent loads. `ChatStorage._get_loaded_skills_file()`, `ChatStorage.get_loaded_skill_ids()`, and `ChatStorage.add_loaded_skill_ids()` in `chat/storage.py` manage this file.

### Difference from Auto-loaded Skills

| Aspect | User Auto-loaded Skills | Project Auto-loaded Skills | Routine Auto-loaded Skills | Conversation-loaded Skills |
|--------|------------------------|---------------------------|---------------------------|---------------------------|
| Scope | All conversations for the user | All conversations in the project | All conversations created by the routine (manual play, scheduler-driven, resume) | Single conversation |
| Injection point | System prompt ("Enabled Skills" section) | System prompt ("Enabled Skills" section, merged with user auto-loads) | System prompt ("Enabled Skills" section, merged after user and project auto-loads) | User message envelope (`<conversation_skills_loaded>` XML section) |
| Configured via | Settings UI auto-load toggle | Project Settings Skills section auto-load toggle | RoutineSettingsModal Skills section auto-load toggle | "+ Skill" button in ChatPanel |
| Persistence | `user_skill_autoloads` database table | `project_skill_autoloads` database table | `routine_skill_autoloads` database table | `loaded_skills.json` file per conversation |
| Resolution | Every message resolves current auto-loads | Every message resolves current project auto-loads, deduplicated against user auto-loads | Every message resolves current routine auto-loads, deduplicated against user and project auto-loads | Only included in the message where skills are first loaded; LLM instructed to treat as persistent |

### Design Decisions

**Why inject conversation-loaded skills in the user message instead of the system prompt?**
Auto-loaded skills are part of the system prompt because they apply universally. Conversation-loaded skills are per-conversation and loaded mid-conversation, so they are injected in the user message envelope via an XML section. This avoids modifying the system prompt mid-session (which would require session invalidation) and makes the loading point explicit in the conversation history.

**Why persist loaded skill IDs to a JSON file instead of the database?**
Conversation chat history is already file-based (`chat_history.json`), and loaded skills are per-conversation state. Using a JSON file (`loaded_skills.json`) alongside the chat history keeps the persistence model consistent without adding a new database table.

**Why use client-side filtering instead of the server search endpoint?**
The modal fetches all accessible skills upfront (the same call used by the settings UI) and filters client-side by name and description. This provides instant filtering without network round-trips and avoids the need for a separate debounced search endpoint. The total number of accessible skills per user is small enough to load in a single request.
