# Routines Architecture

This document describes the Routines feature, which provides canned prompts attached to projects that can be run in one click or on an automatic schedule.

## Overview

A Routine is a reusable prompt template scoped to a project. Each routine combines:

- a prompt (the text sent as the first message),
- an optional guide override (a deprecated [guide](guides.md) applied to conversations the routine creates -- the last remaining way a new conversation gets a guide, and only while the admin `guides` feature gate is open for the user; see [feature-gates.md](feature-gates.md)), and
- an optional model specification (which AI model to use).

When a user runs a routine, the frontend creates a new conversation in the project with a `routine_id` link, optionally sets the model, and auto-sends the prompt as the first message (carrying the guide override's `guide_id` in the send envelope).

Routines can also have an automatic schedule attached, causing the system to execute the routine on a timer without user interaction. See [Scheduling Architecture](scheduling.md) for the full scheduling documentation.

Each routine also carries its own auto-load skill list, persisted in the `routine_skill_autoloads` junction table. Routine auto-loads are merged with the user's and project's auto-load skills (deduped by skill ID, with user auto-loads first, then project, then routine) every time the routine is invoked -- manual play from the sidebar, scheduler-driven runs, and wait-handle resumes. See [Skill Library Architecture](skill-library.md) for the full auto-load tier model and [Database Architecture](database.md) for the `routine_skill_autoloads` schema.

Conversations created by a routine (either manually or via the scheduler) are linked back to the routine via the `routine_id` column on the `Conversation` model. In the sidebar, conversations with a `routine_id` are grouped under collapsible entries per routine, providing a clear visual association between routines and their output conversations.

Routines are managed through dedicated creation and settings modals (`NewRoutineModal` and `RoutineSettingsModal`) and displayed in their own always-visible section in the sidebar when drilled into a project. They are a sub-feature of Projects -- every routine belongs to exactly one project.

## Routine Model

The `Routine` model in `db/models.py` maps to the `routines` table. See [Database Architecture](database.md) for the column-level schema. Key constraints: composite unique index on `(project_id, name)` prevents duplicate routine names within a project, `ON DELETE CASCADE` on `project_id` and `user_id` for cleanup, and `ON DELETE SET NULL` on `guide_id` so deleting a guide does not break routines that reference it.

## Data Access Layer

`db/routine_store.py` provides CRUD operations for routines. All functions are `async` and use `AsyncSessionLocal`. Callers must `await` every store call. The `update_routine()` function uses an ellipsis sentinel (`...`) for both `guide_id` and `model` to distinguish "don't change" (default) from "clear" (`None`). The API layer translates `clear_guide`/`clear_model` boolean flags into the sentinel pattern (see `chat/routine_routes.py`).

## Optimistic Concurrency

Routine and schedule edits are guarded against concurrent overwrites with an `updated_at` token. `create_routine()` stamps `updated_at` on insert so every row has a non-null baseline. `update_routine()` and `update_schedule()` accept an optional `expected_updated_at` argument: when supplied, the store compares it against the row's current `updated_at` (ISO string) and raises [`StaleRoutineError`](../../db/routine_store.py) or [`StaleScheduleError`](../../db/schedule_store.py) on mismatch. The exception carries the fresh server-side row as `.current` so the route handler can return it without a second DB read.

When the supplied field values match the existing row, the update short-circuits without bumping `updated_at`, so a true no-op save does not invalidate other open clients' tokens. Legacy rows with NULL `updated_at` are backfilled from `created_at` by the Alembic migration [`a9e2be6b9caa`](../../alembic/versions/a9e2be6b9caa_backfill_routine_updated_at.py).

The `PUT` endpoints for both routines and schedules accept `expected_updated_at` in the request body and respond with `409 stale_update` (flat JSON body with `error`, `message`, and `current` keys) when the token mismatches. See [Routines API](../api/routines-api.md) and [Schedules API](../api/schedules-api.md) for the wire shape.

## Agent Tools

The model can inspect and (with user approval) create or edit a project's routines from inside any of the project's conversations. Both surfaces are spec'd for the model in the `system:routines` system skill (`chat/system_skills/catalog.py`, `requires_project=True`).

- **`list_routines`** -- a read-only `BASE_TOOLS` tool (declared in `chat/llm/tool_schemas.py`, handled by `_handle_list_routines()` in `chat/gemini_api/tool_handlers/routines.py`). Returns the current project's routines with `id`, `name`, the full `prompt`, `model`, `guide_id`, the inline `schedule` summary (via `list_routines_with_schedules()`), and `autoloaded_skills` (`[{id, name}]` resolved from `routine_skill_autoloads`). Conversations outside a project get a structured error. Not in the public-project allowlist (public projects have no routines anyway).
- **`create_routine`** -- an action request that proposes a new routine in the current project: required `name` + `prompt`, optional `model`, `schedule` spec, and `skill_ids` to auto-load. When the proposal omits `model`, the pre-card check picks one server-side -- Claude Sonnet 5, falling back to Gemini 3.7 Flash, first available per `get_available_models()`; unset when neither is -- and the approval card shows it marked "(default)". Public projects, name collisions, and inaccessible skills are rejected same-turn before any card.
- **`edit_routine`** -- an action request (`create_action_request(request_type="edit_routine", ...)`) that proposes changes to a routine's name, prompt (full replacement -- the approval card shows a line diff of current vs proposed prompt via the shared `skill_content_diff` preview field / `SkillContentDiffPreview.tsx`), model (or `clear_model`), schedule (full-replacement spec, or `clear_schedule`), and auto-loaded skills (`add_skill_ids` / `remove_skill_ids`), rendered as the standard Approve / Revise / Deny card.

  Project scoping, routine existence, name collisions, and skill access are rejected same-turn before any card (shared `routine_precard_check` in `chat/action_request_types/routine_precard.py`); routine-row updates carry the proposal-time `updated_at` token so a concurrent edit fails cleanly at Approve time. The deprecated guide override is not settable or editable, and there is no agent-side delete. See [Action Requests Architecture](action-requests.md) for the full handler behavior.

## Run Routine Flow

Running a routine is entirely frontend-driven. The flow involves the Sidebar, the NavigationContext / ConversationModelsContext hand-off state, and ChatPanel working together:

1. User clicks the play button next to a routine in the project drill-down view in `frontend/src/components/Sidebar.tsx` (`handleRunRoutine()`)
2. Sidebar creates a new conversation in the project via `createProjectConversation(projectId, routineId)`, passing the routine's ID so the backend links the conversation to the routine
3. If the routine has a `model`, Sidebar calls `setModelForConversation()` on `ConversationModelsContext` to set the model for the new conversation
4. Sidebar sets `pendingRoutineMessage` on `NavigationContext` with the conversation ID, prompt text, and the routine's guide ID (the guide override)
5. `ChatPanel` mounts for the new conversation, detects `pendingRoutineMessage` matching its `conversationId`, and auto-sends the prompt via `webSocketManager.sendMessage()`, forwarding the guide ID in the `send_message` envelope
6. ChatPanel clears `pendingRoutineMessage` after sending
7. On the server, `_handle_send_message` in `chat/realtime/socket.py` recognizes the first message of a routine conversation (`routine_id` set and `last_message_seq == 0`) as the routine run and passes `routine_run=True` to `_run_send_message`, which drives the turn through `drive_routine_run()` (see Run Completion below) instead of a single `run_conversation_turn()` call. `send_message_finished` is published only after the whole run, so the composer stays locked through any nudge turn. Later messages in the conversation are the user's own follow-ups and run as ordinary turns.

Because `ChatStorage.create_project_conversation()` receives a `routine_id`, it also copies the routine's name into the conversation's `custom_name` at creation (see Conversation Naming below).

The `routine_id` is persisted on the conversation record in the database. This link enables the sidebar to group conversations by their originating routine (see Sidebar Conversation Grouping below). The backend `POST /app/api/projects/{project_id}/conversations` endpoint accepts an optional `routine_id` in the request body for this purpose. The id must name one of the caller's own routines in that project (`get_routine(user_id, routine_id)` + project match, 404 `Routine not found` otherwise, the same shape as the routine routes): the stored id drives the routine's auto-loaded skills into every turn's system prompt, so a foreign routine id would leak another user's private skill bodies.

## Scheduled Run Flow

Routines with an attached schedule are executed automatically by the background scheduler daemon in `chat/scheduler.py`. Unlike the manual run flow (which is frontend-driven), scheduled runs are entirely backend-driven:

1. The scheduler daemon polls every 30 seconds and identifies due schedules via `_is_due()` in `chat/scheduler.py`
2. For each due schedule, spawns `_execute_scheduled_run()` as an independent asyncio task
3. The task looks up the user record via `get_user_by_id()` from `db/user_store.py`
4. Creates a new conversation in the project via `ChatStorage.create_project_conversation()`, passing the `routine_id` so the conversation is linked to the routine (and named after it -- see Conversation Naming below)
5. Saves the routine prompt as a user message via `ChatStorage.append_message()`
6. Calls `drive_routine_run()` from `chat/routine_runs.py`, which runs `run_conversation_turn()` with a no-op `on_event` callback (no WebSocket to stream to), passing the routine's `model` if specified and the `routine_id` so `run_conversation_turn()` can resolve and merge the routine's auto-load skills (see [Skill Library Architecture](skill-library.md)), and re-runs one follow-up turn if the model ends without calling `routine_completed` (see Run Completion below)
7. Persists the response messages (all turns plus any `routine_nudge` notice, in order) via `ChatStorage.append_structured_messages()`
8. The resulting conversation appears in the project's sidebar grouped under the routine entry, picked up automatically by the sidebar's 30-second polling mechanism if the user is drilled into the project (see [Frontend Architecture](frontend.md) for polling details)

See [Scheduling Architecture](scheduling.md) for the full scheduler daemon documentation, timezone handling, skip-if-running mechanism, and design decisions.

## Conversation Naming

Routine conversations do not go through the model-driven first-reply naming that regular conversations use (the "Conversation naming" block in `get_system_prompt()` asking for a `set_conversation_name` tool call -- see [Gemini API Architecture](gemini-api.md)). Instead:

- `ChatStorage.create_project_conversation()` in `chat/storage.py` looks the routine up (`get_routine(user_id, routine_id)`) whenever a `routine_id` is passed and stores its name as the conversation's `custom_name` (via the `custom_name` parameter on `db/conversation_store.py:create_conversation()`, stripped and truncated to `MAX_CONVERSATION_NAME_LENGTH`). Both the one-click route and the scheduler create routine conversations through this method, so every routine run is titled after its routine from the moment the row exists -- no first reply needed.
- `run_conversation_turn()` passes `is_routine=bool(routine_id)` to `get_system_prompt()`. For routine runs the first-reply naming block is replaced with a "Routine run" note that the conversation is already named (plus the Routine completion instruction described below), and `set_conversation_name` is left out of the Dynamic Tools enumeration. A cached session that still calls the tool hits the handler's existing `already_set` short-circuit, so the routine name is never overwritten by the model. The user can still rename the conversation from the header dropdown.

## Run Completion

Routine runs are unattended, and weaker models sometimes end their stream before the routine's work is actually done -- a manual "please continue" would get them to finish. The completion protocol makes that automatic:

- **The `routine_completed` tool.** Every turn of a routine conversation (a `routine_id` on the row) runs with the `ROUTINE_TOP_LEVEL_TOOLS` tier -- `TOP_LEVEL_TOOLS` plus `routine_completed` -- or `PUBLIC_ROUTINE_TOOLS` in a public project (both in `chat/llm/tool_schemas.py`; the tier is picked in `run_conversation_turn()`). The tool takes an optional `summary` and does nothing but acknowledge the call (`_handle_routine_completed()` in `chat/gemini_api/turn_tools.py`, `{"status": "completed"}`; an error outside routine conversations). Its persisted `tool_use` is the completion signal. The arm is registered under two keys: the top-level name and `ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY` (`tool_call:routine_completed`, resolved by `registry_key_for()` like the `wait_for_handles` arm), because live runs showed Gemini Flash-Lite calling `tool_call(tool_name="routine_completed", arguments={...})` even though the tool has its own declaration; `routine_completed_called()` counts both spellings, and the prompt asks for the direct call. It is the one loop-handled arm not in `_PUBLIC_BLOCKED_LOOP_TOOLS`, because it reads and writes nothing and public-project routine runs need it.
- **The instruction.** The routine variant of the system prompt (`_ROUTINE_NAMING_SECTION` in `chat/gemini_api/system_prompt.py`, used by both `get_system_prompt(is_routine=True)` and `get_public_project_system_prompt(is_routine=True)`) tells the model to call `routine_completed` exactly once as its last tool call after all requested work is done, that a run ending without it is treated as unfinished, and to call it anyway with an explanation when part of the task could not be completed. Routine prompts themselves do not need to mention the tool.
- **The driver.** `drive_routine_run()` in `chat/routine_runs.py` is shared by the scheduler (`_execute_scheduled_run()`) and the one-click run (`_run_send_message(routine_run=True)` in `chat/realtime/socket.py`). It runs the prompt turn, then inspects the structured messages that turn produced: a `routine_completed` tool_use means the run is done; a dangling tool_use (a `tool_use` with no `tool_result` -- the trace of a `create_action_request` / `wait_for_handles` / Slack-reply suspend) means the run is paused on a wait handle and is left alone, since the wait-handle resume will continue it; anything else is an unfinished run. For an unfinished run the driver appends a durable `routine_nudge` message (`role: "system"`, `content` = the nudge text, `attempt`; `routine_nudge` is in `FLUSH_EVENT_TYPES` and is also emitted through the caller's `on_event` so the WebSocket flush persists and fans it out immediately) and runs one more `run_conversation_turn()` whose message is `ROUTINE_COMPLETION_NUDGE` -- a `SYSTEM:`-prefixed note that the run ended without calling `routine_completed`, that the model should make sure every piece of requested work was actually done, and that it should finish by calling the tool. `MAX_ROUTINE_COMPLETION_NUDGES` (1) caps the follow-ups; a run that still ends without the call is logged at warning level and the run is otherwise treated as finished (the scheduler ledger row is `completed`, the composer unlocks). Exceptions from a turn propagate unchanged, so a failed run is still recorded as failed with its durable error bubble.
- **Transcript.** The frontend renders `routine_nudge` as a system notice (teal `.message-routine-nudge` row in `Message.tsx`, exact nudge text in the tooltip), not as a user bubble; the model sees the nudge as an ordinary user-turn message inside the usual `message_metadata` envelope.

Not covered: a routine run that suspends on an action request and is later resumed by the wait-handle resume (`chat/wait_handles/resume.py`) runs its continuation without the nudge check -- the resume path cannot tell the routine run from a manual follow-up, and the user who approved the card is watching anyway.

## File Spaces in Routine Prompts

Every run is a fresh project conversation, so by default everything it writes lands in that run's own conversation workspace (`chat://`, `/workspace`; the run's Chat Files card) and is not visible to the next run. A routine whose runs build on each other -- a state file read back next run, a report appended every week -- must read and write the project workspace explicitly: `proj://` paths with the scheme-qualified file tools, or `/project/...` from a script (see [Projects -- Workspace Resolution](projects.md#workspace-resolution)). The guidance reaches both sides:

- **Model**: the `system:routines` skill (`chat/system_skills/catalog.py`, "Runs that build on each other") tells the agent to use `proj://` paths (`read_file("proj://state.json")`, `write_file(...)`, `copy_file` from `chat://` to `proj://`) and to say so in any routine prompt it proposes via `create_routine` / `edit_routine`; every project prompt's "Two file spaces" paragraph defaults output to `chat://` (see [Gemini API Integration -- System Prompt](gemini-api.md#system-prompt)).
- **User**: the Prompt field of New Routine and Routine Settings carries a help line, `ROUTINE_PROMPT_FILES_HINT` in `frontend/src/constants/routines.ts` ("Each run is a fresh chat, and its files stay in that run's chat. If runs build on each other, have the prompt read and write the project space explicitly ...").

The routine completion tool and the scheduler are unaffected.

**Migration notes:** routines written when every project conversation shared the project workspace and that relied on it (a run reading what an earlier run wrote) must be updated to address the project space with `proj://` paths in their prompts -- the help text above says this in the app. Legacy project chats (created before the change, including earlier routine runs) show their earlier files under Project Files only; their Chat Files card starts empty, old inline images pointing at those files show the broken-reference chip, and the model gets the legacy note pointing it at `proj://` (see [Projects -- Migration Notes](projects.md#migration-notes)).

## Frontend

### NewRoutineModal

`frontend/src/components/NewRoutineModal.tsx` is a dedicated modal for creating a new routine within a project. It is rendered as a React portal and provides:

- Name input (max 100 characters)
- Prompt textarea, with the `ROUTINE_PROMPT_FILES_HINT` help line (see [File Spaces in Routine Prompts](#file-spaces-in-routine-prompts))
- Guide override dropdown: shows all non-default user guides (from `GuidesContext.guides`) plus a "None" option that maps to `guide_id: null` (guides are deprecated; the hint says so). Rendered only while the `guides` feature gate is on for the user (`enabled_features` from `GET /me`); otherwise the modal sends `guide_id: null`
- Model selector dropdown: shows the non-deprecated models from the shared `SELECTABLE_MODELS` view in `frontend/src/constants/models.ts` (Gemini 3.1 Flash-Lite, Gemini 3 Flash, Gemini 3.5 Flash, Gemini 3.5 Flash-Lite, Gemini 3.6 Flash, and the Claude models); default is Gemini 3 Flash. The routine-settings modal additionally keeps a routine's stored deprecated model (e.g. Gemini 3.1 Pro) as an extra "(deprecated)" option so saving unrelated edits doesn't silently switch the model

The form resets when the modal opens. On submit, it calls `createRoutine()` and fires the `onRoutineCreated` callback, which triggers a reload of the routines list in the sidebar. Styles are in `frontend/src/components/NewRoutineModal.css`.

### RoutineSettingsModal

`frontend/src/components/RoutineSettingsModal.tsx` is a dedicated modal (720px wide) for editing an existing routine's settings and deleting it. It is rendered as a React portal and uses a left navigation sidebar with five sections: Prompt, Schedule, Skills, Costs, and Delete (with Delete pinned to the bottom of the nav as a danger item). The modal resets to the Prompt section each time it opens.

**Prompt section:**
- Name input (max 100 characters)
- Auto-growing prompt textarea that expands to fit content up to approximately 20 lines (~420px), following the same pattern as the ChatPanel textarea. The textarea auto-sizes on initial load, on input, and when navigating back to the Prompt section. The label carries the `ROUTINE_PROMPT_FILES_HINT` help line
- Guide override (deprecated, read-only leftover): there is no guide selection widget anymore. The field renders only when the routine still has a persisted `guide_id`, showing the guide's name read-only with a "Clear" button; clearing swaps the display for a "Guide will be removed when you save" hint and the save sends `clear_guide: true`. Routines without a guide show nothing
- Model selector dropdown: shows all available models from the shared `AVAILABLE_MODELS` constant; deprecated models stored on routines are silently remapped on load via `DEPRECATED_MODEL_MAP` from `frontend/src/constants/models.ts`
- Save button and error display

**Schedule section:**
- A single "Run on a schedule" checkbox that controls whether the schedule is active. When unchecked, schedule configuration inputs remain visible but are grayed out and disabled, preserving the configuration for when the user re-enables the schedule. Schedule state is loaded when the modal opens via `fetchRoutineSchedule()`. See [Scheduling Architecture](scheduling.md) for schedule type details
- Save button and error display

**Skills section:**
- Lists all skills the user has access to (own + shared + public, fetched via `fetchSkills()`) with a per-skill auto-load toggle for the routine. Auto-loaded skill IDs for the routine are fetched on section activation via `fetchRoutineAutoloadedSkillIds(projectId, routineId)` and toggled via `setRoutineSkillAutoload(projectId, routineId, skillId, enabled)`. Toggles persist immediately to the `routine_skill_autoloads` table -- there is no separate Save button. See [Skill Library Architecture](skill-library.md) for how routine auto-loads merge with user and project auto-loads at conversation time

**Costs section** (`frontend/src/components/RoutineCostsSection.tsx`, read-only):
- Fetched on section activation via `fetchRoutineCosts(projectId, routineId)` (`GET .../routines/{id}/costs`, see [Routines API](../api/routines-api.md)); the component is keyed on the routine id so each routine gets a fresh fetch
- Two headline cards, "Last 7 days" and "Last 28 days": the window's cost, its run count, and a period-over-period delta against the preceding period of the same length (signed dollar change plus percentage, "no spend before" when the base period had none, "n/a" when either period contains an unpriced model); rising spend is tinted amber, falling spend green
- A "Total since <routine created date>" line with the lifetime cost, run, call and token counts
- A "Recent runs" table (newest 10): start time, model(s) used, tokens, cost. Rows are clickable and open the run's conversation (the Sidebar passes `onOpenRunConversation`, which closes the modal and selects the conversation inside the project)
- Every cost figure carries the `~` estimate marker / provenance tooltip conventions of `ConversationUsageCell` (`formatCost` / `describeCostSource`), and an unknown (null) figure renders as an em dash with an explanatory tooltip
- The report itself is assembled by `chat/routine_costs.py`: `list_routine_conversation_rows()` (db/conversation_store.py) supplies the runs, `get_usage_by_model_for_conversation_query()` (db/llm_call_store.py) aggregates their raw call rows through an `IN (subquery)` on `routine_conversation_ids_query()` so a long-running routine's id set never round-trips as a bind list, and the pure `build_cost_report()` buckets runs by start time (7d/28d current + previous, lifetime, recent runs). Design choice: attributing a run's whole cost to its start instant keeps the cards, the lifetime line and the table mutually consistent; the admin Users report slices by call time instead, so the two are different views rather than the same number

**Delete section:**
- Danger zone with delete confirmation dialog

On save, it calls `updateRoutine()` and handles schedule create/update/delete as needed, then fires the `onRoutineUpdated` callback. On delete, it calls `deleteRoutine()` and fires the `onRoutineDeleted` callback. Both callbacks trigger a reload of the routines list in the sidebar. Styles are in `frontend/src/components/RoutineSettingsModal.css`.

**Concurrent-edit handling:** The modal snapshots the routine and schedule `updated_at` tokens when it opens and sends them as `expected_updated_at` on every save, refreshing the snapshots on success. When the server returns `409 stale_update`, the modal renders a stacked conflict modal on top of itself (z-index 1100 over the settings modal's 1000) offering "Discard my changes and reload" (drops local edits and reloads from the server's `current` row) and "Overwrite anyway" (retries the save without the token so the server skips the check). The settings modal stays mounted under the conflict modal so the user's unsaved field values are preserved across the decision.

### Sidebar Routines Section

When the user drills into a project in `frontend/src/components/Sidebar.tsx`, routines are loaded alongside conversations via `loadProjectRoutines()`. The "Routines" section is always visible in the project drill-down view (similar to how the "Projects" section is always visible in the main sidebar). When the project has no routines, a dotted-border "Create Routine" button is shown. When routines exist, a small "+" button appears in the section header for creating new routines (both open the `NewRoutineModal`).

Each routine entry displays the routine name, a settings gear icon that opens the `RoutineSettingsModal`, and a play button (triangle icon) that triggers `handleRunRoutine()`. If a routine has an active schedule (`schedule.is_enabled`), a small clock icon appears next to the routine name as a visual indicator.

The routine listing API includes schedule data (via a left-join in `db/routine_store.py`), so the sidebar does not need separate API calls to determine schedule presence.

Routines are reloaded from the server when routine creation, update, or deletion callbacks fire (`handleRoutineCreated`, `handleRoutineUpdated`, `handleRoutineDeleted`), as well as when `handleProjectUpdated()` fires (triggered by project settings changes).

### Sidebar Conversation Grouping

In the project drill-down's Conversations section, conversations that have a `routine_id` are grouped under a single collapsible entry per routine rather than listed individually. This provides a clear visual hierarchy showing which conversations were created by which routine.

**Group entry appearance:**
- A chevron toggle (right-pointing when collapsed, down-pointing when expanded)
- A refresh-cycle icon indicating the conversation is routine-generated
- The routine name as the group label
- A badge showing the count of conversations in the group

**Sub-item appearance:**
- When a group is expanded, each conversation shows a relative timestamp (e.g., "2h ago") for recent conversations or an absolute timestamp (e.g., "Jan 15") for older ones, instead of repeating the routine's prompt text as the title

**Sort order:**
- Routine groups and standalone conversations are interleaved and sorted by recency (based on the most recent conversation in each group, or `last_message_at` for standalone conversations)
- Within a routine group, conversations are sorted by recency (newest first)

**Styling:**
- Full dark/light mode CSS support for group entries, chevrons, icons, badges, and sub-items
- Styles are in `frontend/src/components/Sidebar.css`

**Data flow:**
- The conversation listing API returns `routine_id` on each conversation object
- The sidebar groups conversations client-side by matching `routine_id` values to loaded routine data
- Conversations whose `routine_id` does not match any currently loaded routine (e.g., if the routine was deleted) fall through to the ungrouped conversation list since `routine_id` is set to NULL via `ON DELETE SET NULL`

## Cascade Deletion

Routines and their schedules are cleaned up in multiple scenarios:

- **Project deletion**: The `ON DELETE CASCADE` FK on `routines.project_id` automatically removes all routines when a project is deleted at the database level. Routine schedules are then removed via the `ON DELETE CASCADE` FK on `routine_schedules.routine_id`
- **User account deletion**: The `delete_account()` handler in `chat/routes/user.py` explicitly calls `delete_all_user_routines(user_id)` from `db/routine_store.py` before deleting the user record. The `ON DELETE CASCADE` FK on `routines.user_id` and `routine_schedules.user_id` also provides database-level cleanup
- **Guide deletion**: The `ON DELETE SET NULL` FK on `routines.guide_id` sets the guide reference to NULL when a guide is deleted, rather than deleting the routine
- **Routine deletion**: The `ON DELETE CASCADE` FK on `routine_schedules.routine_id` automatically removes the schedule when a routine is deleted. The `ON DELETE SET NULL` FK on `conversations.routine_id` sets the routine reference to NULL on conversations created by the routine, preserving the conversations

## Design Decisions

**Why scope routines to projects instead of making them global?**
Routines are designed for repetitive tasks within a specific project context. Scoping them to projects keeps the UI organized (routines appear in the project drill-down) and ensures the routine's prompt makes sense in the context of the project's workspace, database and guide.

**Why do runs not share files by default?**
Runs are ordinary project conversations, and keeping every conversation's scratch out of the shared space is the point of the conversation workspace (see [Projects -- Design Decisions](projects.md#design-decisions)). Most runs are self-contained; the ones that accumulate state opt in by naming `proj://` paths in their prompt (see [File Spaces in Routine Prompts](#file-spaces-in-routine-prompts)), which keeps the dependency visible in the routine itself.

**Why denormalize `user_id` on the routines table?**
The `user_id` column is technically derivable from `project_id` (via the project's `user_id`), but storing it directly on the routine enables fast per-user queries (e.g., `delete_all_user_routines()` during account deletion) without joining through the projects table.

**Why use an ellipsis sentinel for `guide_id` and `model` updates?**
The `update_routine()` function needs to distinguish three states for both `guide_id` and `model`: "don't change" (default), "set to a specific value" (string), and "clear the value" (`None`). Python's ellipsis (`...`) serves as an unambiguous sentinel that cannot be confused with `None`. The API layer translates `clear_guide`/`clear_model` boolean flags into the sentinel pattern (see `chat/routine_routes.py`).

**Why is running a routine purely frontend-driven?**
The "run" action is just creating a conversation and sending a message -- two operations the frontend already handles. Keeping the logic in the frontend avoids adding a dedicated backend endpoint for an operation that composes existing primitives. The `pendingRoutineMessage` pattern in `NavigationContext` coordinates the creation and auto-send across components.

**Why allow routines to specify a model?**
Different routines may have different complexity levels. A simple data retrieval routine might work well with Gemini 3.1 Flash-Lite (fastest, cheapest), a daily summary routine with Gemini 3 Flash (faster, cheaper), while a complex analysis routine might need a more capable model like Claude Opus. Storing the model on the routine lets users configure this once and have it apply consistently across both manual and scheduled runs.

When no model is specified, manual runs use the user's current model selection and scheduled runs use the server config default. Deprecated model IDs stored on routines are silently remapped at runtime (see [Gemini API Integration](gemini-api.md) for the full remapping details).

**Why `ON DELETE SET NULL` for `guide_id` instead of `CASCADE`?**
Deleting a guide should not delete routines that happened to reference it. Setting `guide_id` to NULL gracefully degrades the routine to run without a guide, preserving the routine's prompt and other settings.

**Why is scheduling in a separate table and module instead of extending the routine model?**
A separate `routine_schedules` table keeps the scheduling concern isolated from existing routine CRUD, avoids widening the routines table with many nullable columns, and makes it easy to query "all schedules that are due" across all users. The scheduler daemon (`chat/scheduler.py`) and schedule API (`chat/schedule_routes.py`) are separate modules from routine CRUD. See [Scheduling Architecture](scheduling.md) for the full set of scheduling design decisions.

**Why link conversations to routines via a `routine_id` FK instead of inferring from the prompt text?**
A direct FK provides a reliable, indexed link that survives prompt edits and avoids false matches. Using `ON DELETE SET NULL` means deleting a routine gracefully degrades conversations to ungrouped entries in the sidebar rather than orphaning them. The FK also enables efficient grouping queries without scanning conversation content.

**Why group routine conversations in the sidebar instead of listing them individually?**
Routines (especially scheduled ones) can produce many conversations over time. Listing them all individually would overwhelm the sidebar. Collapsible grouping keeps the sidebar manageable while still providing access to every conversation. The group shows the conversation count and uses timestamps instead of the repeated routine prompt text for sub-items.

**Why show timestamps instead of titles for routine sub-items?**
All conversations created by the same routine share the same initial prompt and are all titled with the routine's name, so the title would be identical across all sub-items. Showing relative or absolute timestamps provides more useful differentiation.

**Why name routine conversations after the routine instead of letting the model name them?**
Every run of a routine starts from the same prompt, so the model-chosen name was the same each run and cost a tool call (plus prompt text) per run for no information. Setting `custom_name` at creation gives the same result for free, works identically for scheduled runs (no UI in the loop) and one-click runs, and lets the routine-run system prompt skip the naming instruction entirely.

**Why a composite unique index on `(project_id, name)` instead of `(user_id, name)`?**
Routine names should be unique within a project, not globally per user. Different projects may have routines with the same name (e.g., "Daily Report") since they serve different contexts.

## Constraints

- Each project's routine names must be unique (enforced by the `ix_routines_project_id_name` composite unique index)
- Routine name maximum length: 100 characters (enforced in `db/routine_store.py` via `MAX_ROUTINE_NAME_LENGTH`)
- Routine prompt maximum size: 16KB (enforced in `db/routine_store.py` via `MAX_ROUTINE_PROMPT_SIZE`)
- Routine prompt cannot be empty (enforced at both the data access layer and the API validation layer)
- Routines are deleted when the parent project is deleted (via `ON DELETE CASCADE` FK on `routines.project_id`)
- Routines are deleted when the owner's account is deleted (via explicit `delete_all_user_routines()` call and `ON DELETE CASCADE` FK on `routines.user_id`)
- Deleting a guide sets `routines.guide_id` to NULL (via `ON DELETE SET NULL` FK)
- Public projects have routines only while the admin `public_project_routines` feature gate is open for the user (API, one-click runs, scheduler and UI all gated -- see [feature-gates.md](feature-gates.md)); a routine there has no skill auto-loads or guide override, and its model list is the public-allowed one
- The `routines` table and its indexes are created by the Alembic migration `6766d7c126ba`; the `model` column is added by migration `ae661b3ecf4c`
- Running a routine manually does not use a dedicated backend endpoint; it composes existing conversation creation (with `routine_id`) and WebSocket message sending
- Conversations created by a routine (manual or scheduled) have `routine_id` set on the `conversations` table; the `routine_id` FK uses `ON DELETE SET NULL` so deleting a routine preserves its conversations
- The `conversations.routine_id` FK, index, and `ON DELETE SET NULL` behavior are created by the Alembic migration `c7e3f1a2b4d6`
- Sidebar conversation grouping is performed client-side by matching conversation `routine_id` values to loaded routine data
- Scheduled routine execution is handled by the background scheduler daemon in `chat/scheduler.py`; see [Scheduling Architecture](scheduling.md) for scheduling-specific constraints
- The `routine_skill_autoloads` junction table is created by the Alembic migration `41e59b874c9d`; both FKs use `ON DELETE CASCADE` so routine auto-load rows are cleaned up when the routine or skill is deleted
