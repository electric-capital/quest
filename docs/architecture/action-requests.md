# Action Requests Architecture

This document describes the Action Requests system, which allows the LLM agent to propose write operations to external services (e.g., sending a Slack message, Telegram message, creating a calendar invite, uploading a workspace file to Google Drive, creating a Google Drive folder, or editing a Google Spreadsheet cell range) -- plus internal-store writes (saving a memory, and creating or editing a DB-backed skill) -- that require explicit user approval before execution.

Plugins register additional request types (loaded via `plugins/` or `QUEST_PLUGIN_PATH` -- see [Plugins](plugins.md)); the mechanics below apply to core and plugin handlers alike.

## Overview

Action Requests extend the agent's capabilities beyond read-only operations by letting it propose actions that modify external state. The agent proposes an action via the `create_action_request` tool; the request is persisted in its own `action_requests` table with status `open`, and a linked `tool_wait_handles` row of `kind="action_request"` is created. The dispatch arm raises `SuspendForActionRequest` so the agent loop unwinds; the call blocks until the user resolves the request (see [Wait Handles Architecture](wait-handles.md)).

The user resolves a pending request from inline chat buttons or from the cross-conversation Requests pane via three actions: **Approve** (execute the action), **Revise** (reject with a free-text feedback message that flows back to the model on resume), or **Stop** (discard the request and halt the conversation until the user's next message -- see [Stop](#stop) below). There is no one-click Deny button on action cards; the only card that still shows Deny is the cross-user `subagent_return` card (see [Cross-User Subagents](user-subagents.md)).

Approved requests are executed server-side by a registry of `ActionRequestHandler` subclasses, one per request type, and the same resolve endpoint also resolves the linked wait handle so the resumed run closes the dangling `create_action_request` tool_use with the verdict payload -- including the user's revise feedback (when present) so the model can apologise, propose an alternative, or fix and re-issue.

When a new request arrives while the browser tab is hidden and notification permission has already been granted, the frontend also surfaces a desktop notification via `frontend/src/services/desktopNotifications.ts`.

**Documentation split.** The `create_action_request` tool description in `chat/llm/tool_schemas.py` is deliberately generic -- it covers the approval mechanics but not the per-backend `request_type` catalog. Per-backend request-type specifications (parameter names, whitelists, example payloads) live in each backend's system skill:

- Slack writes in `system:slack`
- Telegram writes in `system:telegram`
- Twitter DMs in `system:twitter`
- calendar invites in `system:calendar`
- Drive uploads and folder creation (`upload_to_drive`, `create_drive_folder`) in `system:drive`
- Sheets cell edits (`edit_google_spreadsheet`) in `system:sheets`
- the GCP VM hard reset (`reset_gcp_instance`) in `system:gcp`
- memory writes (`create_memory`) in `system:memory`
- skill create/edit (`create_skill` / `edit_skill`) in `system:skill_management`
- routine create/edit (`create_routine` / `edit_routine`) in `system:routines`
- cross-user subagent runs (`run_user_subagent`) in `system:user_subagents` (the companion `subagent_return` type is excluded from the tool enum and mintable only by the `return_to_caller` dispatch arm inside subagent conversations, with bespoke Revise/Deny semantics -- see [Cross-User Subagents](user-subagents.md))

Cross-cutting approval / cancel / retry behavior lives in `system:action_requests`. The model loads the relevant skill before calling `create_action_request` for a given backend. See [Skill Library Architecture -- System Skills](skill-library.md#system-skills) for the catalog.

The supported request types are:

- **`send_slack_message`** (Slack plugin -- handlers in `plugins/slack/handlers.py`, grandfathered unprefixed type names): Sends a Slack message to any channel, group, or DM on behalf of the authenticated user.
  - Intended for messages to **other people or channels** -- when the user wants to message themselves, the system prompt directs the model to use the `send_slack_dm_to_self` dynamic tool instead (no approval needed, works in automated/scheduled routines, supports workspace-file attachments).
  - It accepts a `channel_id` parameter that can be a channel ID (C...), group ID (G...), or user ID (U.../W... for DMs), and an optional `thread_ts` parameter (a Slack message timestamp) to reply in a thread.
  - `channel_id` and `thread_ts` are format-validated at `validate_params()` time (channel id must be an uppercase Slack object id with a recognised `C`/`G`/`D`/`U`/`W` prefix; thread ts, when non-empty, must be a `<unix_seconds>.<microseconds>` Slack message timestamp -- not a permalink-style `p...` id) so malformed values are rejected up front with the offending value echoed (truncated) in the error rather than failing inside `execute()` with an opaque Slack `channel_not_found`.
  - When a user ID is provided, a DM conversation is opened automatically via `conversations.open`. Channel names and thread context are resolved server-side via the handler's `enrich_params_for_preview()` hook for human-readable preview display. The legacy `send_slack_dm` handler is preserved for backward compatibility.
- **`send_telegram_message`**: Sends a Telegram message to any dialog (user, group, or channel) using the authenticated user's Telethon session. It accepts a `dialog_id` (numeric Telegram dialog ID) and `message` (plain text, max 4096 characters).
  - `dialog_id` is format-validated at `validate_params()` time (must be a Python `int` or a digit string with optional leading `-`; `bool`/`float`/`list`/`dict` and shapes like `@username`, `t.me/...`, `+E.164`, or Slack-style `C.../U...` ids are rejected; `0`, the bare `-100` supergroup marker, and `|id| > 10**15` are also rejected) so malformed values are short-circuited up front with the offending value echoed (truncated) in the error rather than failing inside `execute()` with an opaque Telethon `PeerIdInvalid`.
  - The regex and magnitude bound live at module scope in `plugins/telegram/handlers.py` (the handler is plugin-registered, with the pre-plugin type name grandfathered via `unprefixed_action_types`).
  - Dialog names are resolved server-side by the handler's `enrich_params_for_preview()` hook for preview display. Messages are HTML-escaped for safety and include an italic attribution footer.
- **`create_calendar_invite`**: Creates a Google Calendar event on behalf of the authenticated user.
  - It accepts `summary`, `start`, and `end` as required parameters, plus optional `calendar_id`, `attendees`, `location`, `description`, and `time_zone` fields. The handler renders a preview with the event title, calendar target, start/end time, and any optional attendees or notes, then creates the event only after approval.
  - This flow requires Google Services with writable Calendar scope (`calendar.events`) in addition to the existing Google Services connection. Created requests collapse to a one-line `Created` summary in the chat UI, and the event description is passed through unchanged.
- **`edit_calendar_event`**: Updates an existing Google Calendar event (`chat/action_request_types/edit_calendar_event.py`).
  - Required `event_id` and `expected_updated` (the event's `updated` timestamp the model read), optional `calendar_id` (default `primary`), and at least one of `summary`, `start`, `end` (RFC3339 or all-day `YYYY-MM-DD`; either alone, both when switching kinds), `time_zone`, `attendees` (COMPLETE replacement list; kept attendees retain their live `responseStatus`/`optional` records), `location`, `description` (`""` clears).
  - Read-before-write is enforced like `edit_google_spreadsheet`: `validate_against_upstream()` GETs the live event and rejects same-turn (no card) on 404, cancelled status, an `expected_updated` mismatch, a no-op (every supplied field already matches), or a merged start/end out of order, then injects the server-only `current_event` snapshot.
  - `execute()` re-reads and re-compares `expected_updated` at Approve time (TOCTOU close) before a Calendar v3 `events.patch` carrying only the supplied fields (start/end sent as complete objects nulling the unused `date`/`dateTime` key, `sendUpdates=all` whenever the resulting event has attendees).
  - The card renders before → after values per changed field, a `skill_content_diff` line diff for description changes, an attendee add/remove line, a recurrence Scope line (master id = entire series, instance id = this occurrence), and a notification note; collapsed label `Updated`. `calendar_name` is resolved via the handler's `enrich_params_for_preview` hook (not the legacy ladder).
- **`upload_to_drive`**: Uploads one or more files from the conversation workspace to the user's Google Drive after a single approval.
  - Required parameter is `files` -- a 1..10-entry list of `{path, filename?}` objects (workspace-relative paths; absolute paths, `..` traversal, and duplicate paths within one request are rejected at `validate_params()` time; per-entry `filename` overrides the Drive name, sanitized via `_sanitize_workspace_filename`, defaulting to the file's basename).
  - All files in one request land in the same destination folder; the model-facing docs instruct batching same-destination uploads into one request so the user approves once, not per file. The legacy single-file top-level `path` / `filename` shape is still accepted (mutually exclusive with `files`) so pending pre-deploy rows execute unchanged.
  - Optional `folder_id` targets a Drive folder (defaults to My Drive root). Alternatively, optional `new_folder_name` (mutually exclusive with `folder_id`) creates the destination folder at approval time -- after every workspace file is pre-flight resolved but before any upload -- with optional `new_folder_parent_id` (only valid alongside `new_folder_name`) placing the new folder under an existing parent; the result then also carries `folder_id` / `folder_name` / `folder_url` so follow-up upload batches can target the new folder with plain `folder_id`.
  - If an upload fails mid-batch, the raised error lists the files that already uploaded (name + file id) and, when a folder was created, the created folder's id with a pointer to retry with `folder_id` -- a plain re-approve would re-upload the successes and create a duplicate folder, since Drive allows same-name siblings.
  - Unlike "Save to Drive" (which is markdown-only and sets `application/vnd.google-apps.document` so Drive *converts* the file into a native Google Doc), this handler uploads the **raw bytes as-is** -- the content-type is guessed from each filename and the results are generic Drive file links `https://drive.google.com/file/d/<id>/view` (result shape `{success, count, files: [{file_id, name, url}]}`; legacy `path` requests keep the flat `{file_id, name, url}` shape). Uploads are capped at 50 MB per file.
  - The handler pre-flight-validates every file through `resolve_workspace_file()` in `chat/action_request_types/_io_attachments.py` (shared traversal guards + size cap) before any Drive mutation, then reads bytes one file at a time via `read_resolved_file_bytes()` and uploads via a `multipart/related` body to the Drive v3 upload API with `supportsAllDrives=true` (shared-drive support).
  - On approval the handler also performs an explicit `drive.file` scope pre-check and raises a "reconnect Google Services" error on legacy connections that predate the write scope.
  - `render_preview()` shows File / Source / Destination rows for a single file, or a `Files` count row plus per-file `File N` rows (with `path → name` renames inline) for a batch, resolving `folder_id` (or `new_folder_parent_id`, rendered as `New folder "..." in <parent>`) to a human folder name (server-injected `folder_name` / `new_folder_parent_name`, like `calendar_name`). The `approve_label` is `"Upload"`.
- **`create_drive_folder`**: Creates a Google Drive folder after approval.
  - Required parameter is `name` (trimmed, non-empty; folder names are Drive metadata, not filesystem paths, so no filename sanitization applies). Optional `parent_folder_id` targets an existing parent folder (shared drives supported); when omitted the folder lands in the Drive root ("My Drive").
  - On approval the handler POSTs folder metadata (`mimeType` `application/vnd.google-apps.folder`) to the Drive v3 `files.create` endpoint with `supportsAllDrives=true`, returning `folder_id`, `name`, and a `https://drive.google.com/drive/folders/<id>` URL the model can chain into follow-up `upload_to_drive` requests.
  - `render_preview()` shows Folder / Parent rows, resolving `parent_folder_id` to a human folder name (server-injected `parent_folder_name`). Requires the same `drive.file` scope pre-check as `upload_to_drive`. The `approve_label` is `"Create"` and `resolved_label` is `"Created"`, with the folder name as the collapsed-card `summary_snippet`.
- **`edit_google_spreadsheet`**: Overwrites a rectangular cell range on one tab of a Google Spreadsheet after approval.
  - Required parameters are `spreadsheet_id`, `tab` (sheet title, passed separately from the range), `range` (a bounded A1 rectangle like `B2:D5` or a single cell `B2` -- unbounded `A:A` / `1:3` ranges and sheet-qualified ranges are rejected at `validate_params()` time; capped at 200 rows x 52 columns and 1000 cells), `values` (2d replacement array exactly matching the range dimensions; cells are strings/numbers/booleans, `""` clears a cell, `null` is rejected because the Sheets API would skip the cell instead of clearing it), and `current_values` (2d array of the values currently in the range as the model just read them; `null`/`""` for empty cells).
  - **Read-before-write enforcement**: the handler overrides `validate_against_upstream()` to fetch the live range in three renderings (FORMATTED_VALUE, UNFORMATTED_VALUE, FORMULA); a plain cell's claim matches if it equals any rendering (with a numeric-equality fallback), while a **formula cell must be claimed by its formula text** -- claiming the computed value is rejected, so a model that never read the formulas (the skill directs it to read with `valueRenderOption=FORMULA`) cannot silently destroy them.
  - It rejects the proposal same-turn with a per-cell diff when `current_values` does not match, so the model cannot overwrite cells it never read.
  - `execute()` repeats the identical comparison at Approve time as the TOCTOU close (the sheet may have changed while the card sat open), raising `RuntimeError` so the request stays open.
  - The proposal-time hook also 404-checks the spreadsheet, verifies the tab exists (case-sensitive, with available tabs listed in the error), checks the range lies inside the sheet's grid, and server-injects `spreadsheet_title`, `sheet_gid`, and a `context_grid` (the target range expanded by up to 2 rows/cols on each side, clamped to the sheet bounds; formula cells display as their formula text, everything else as the formatted rendering); transient failures (network, 401/403, 5xx) log a WARNING and defer verification to `execute()`.
  - `render_preview()` trims all-blank context rows/cols from the window edges (never into the target rectangle) via `_trim_blank_context()`, then returns Spreadsheet / Tab / Range rows plus a structured `{"key": "Changes", "type": "spreadsheet_diff", "grid": {...}}` field carrying the context window, the replacement values, and a server-computed per-cell `changed` matrix; the frontend renders it as a spreadsheet-style diff table (see [Frontend Rendering](#frontend-rendering)).
  - On approval the handler runs a `spreadsheets` write-scope pre-check (reauth error on legacy connections; the scope was added to `GOOGLE_SERVICE_SCOPES` in `auth/config.py` alongside the existing `spreadsheets.readonly`), re-verifies, and PUTs to the Sheets v4 `values.update` endpoint with `valueInputOption=USER_ENTERED`, returning `{"success": True, "spreadsheet_id", "range" (the API's updatedRange), "updated_cells", "url"}` (the URL carries `#gid=` when the sheet id was captured).
  - The `approve_label` is `"Apply"`; `resolved_label` is `"Saved"` with a `title -- tab!range` `summary_snippet`. The model-facing parameter spec lives in `api/sheets.py` (`get_instructions()`, "Google Sheets Write Operations" section), surfaced via the `system:sheets` skill.
- **`create_memory`**: Saves a new user memory after explicit approval. The `params` object carries `content` (string, max `MAX_MEMORY_SIZE` = 4 KB; `validate_params()` strips whitespace and rejects empty content).
  - The handler's `display_name` is `"Save Memory"` and `approve_label` is `"Save"`. `render_preview()` returns a single `{"key": "Memory", "value": <content>}` row. On approval the handler calls `db.memory_store.create_memory(user_id, content)` and returns `{"success": True, "memory_id": <new_id>}`.
  - This is the only model-facing memory-write path; the model has no dedicated memory-write tool.
- **`create_skill`** (`CreateSkillHandler` in `chat/action_request_types/create_skill.py`): Creates a new DB-backed skill after approval. Skills are an internal Quest store (like memory), so -- like `create_memory` -- the handler has no `validate_against_upstream` and no external API key. An optional `target` (`"user"` default, or `"project"`) selects the destination.
  - **User target**: creates a skill the caller owns with `visibility` in `{private, shared, public}` (default `private`) and an optional `share_emails` roster accepted only when `visibility == "shared"` (emails resolved to user IDs at execute time, unknown / self silently skipped).
  - **Project target**: creates a `visibility="project"` skill in the conversation's project; `visibility` / `share_emails` are rejected, and execution requires the conversation to be in a project the caller owns (`db.project_store.get_project(user_id, project_id)` truthy).
  - `name` (<=100 chars) and `content` (<=64 KB) are required; the size limits reuse `MAX_SKILL_*` constants from `db/skill_store.py`. Name uniqueness is enforced per-creator (user) or per-project (project) at BOTH a pre-card check (see [Skill Pre-card Checks](#skill-pre-card-checks)) and an execute-time TOCTOU guard that raises `RuntimeError` so the request stays open.
  - `display_name` is `"Create Skill"`, `approve_label` is `"Create"`, `resolved_label` is `"Created"`. Returns `{"success": True, "skill_id", "target", "shared_with"}`.
- **`edit_skill`** (`EditSkillHandler` in `chat/action_request_types/edit_skill.py`): Partial-edits an existing DB-backed skill keyed by `skill_id` (the DB UUID the model gets from `list_my_skills` / `get_skill`). `system:*` ids are rejected at `validate_params()` time (they are not DB rows). The target (user vs project skill) is inferred at execute time from the looked-up skill's `project_id` -- there is no `target` param. Empty payloads (only `skill_id`, no editable field) are rejected; `project_autoload` counts as an editable field.
  - **Content edits are an exact search-and-replace** mirroring `edit_workspace_file` -- an `old_string` / `new_string` pair (empty `new_string` deletes the match) plus optional `replace_all`, never a full-body overwrite; a legacy `content` param is rejected by `validate_params()` with a message steering to the new interface (but `execute()` still honors it on rows proposed before the change).
  - The shared replacement/diff logic lives in `chat/action_request_types/_skill_content_edit.py`: `apply_content_edit()` enforces found-and-unique-unless-`replace_all` matching plus non-empty / max-size results, and runs at BOTH proposal time (pre-card, see below) and Approve time against the then-live content -- so a skill that changed while the card sat open fails cleanly (`old_string not found`) instead of being clobbered.
  - Content edits are additionally gated on the conversation having loaded or read the skill (the `skill_reads.json` sidecar via `ChatStorage.get_skill_read_ids`, populated by system-prompt auto-loads, composer-loaded skills, `load_skills` / `get_skill`, and `create_skill`), checked at pre-card and re-checked in `execute()`. The approval card renders a server-computed line diff (see Skill Pre-card Checks).
  - **User skills**: editable fields are `name` / `description` / content (`old_string`/`new_string`) / `visibility` plus `add_share_emails` / `remove_share_emails` (add/remove semantics); share mutations are valid only when the effective visibility is `shared`, and moving visibility AWAY from `shared` **auto-clears the entire share roster** via `db.skill_store.clear_skill_shares` (so stale shares cannot silently re-activate). Ownership is gated by `update_skill` returning `None` for non-owners; `project_autoload` is rejected.
  - **Project skills**: editable fields are `name` / `description` / content plus a project-only `project_autoload: bool` toggling the `project_skill_autoloads` auto-load flag via `set_project_skill_autoload`; `visibility` and the share lists are rejected, and edits require owning the skill's project AND a `project_id` match (double gate).
  - Name-change collisions are pre-checked and re-checked per the resolved target's scope. `display_name` is `"Edit Skill"`, `approve_label` is `"Save"`, `resolved_label` is `"Saved"`. Returns `{"success": True, "skill_id", "target"}`.
- **`create_routine`** (`CreateRoutineHandler` in `chat/action_request_types/create_routine.py`): Creates a new routine in the current conversation's project after approval. Required: `name` (<=100 chars, per-project unique) and `prompt` (<=16 KB).
  - Optional: `model` (registry-validated, deprecated ids rejected; when omitted, the pre-card check injects a default -- Claude Sonnet 5, then Gemini 3.7 Flash, first available per `pick_default_routine_model()` -- shown on the card as "(default)" via the injected `model_defaulted` flag, or stays unset when neither is available), a `schedule` spec (same full-replacement shape as `edit_routine`, applied after the routine row is created), and `skill_ids` to auto-load (gated on `user_can_access_skill`, re-verified up front at execute time before the routine is created). No `guide_id` (guides are deprecated) and no agent-side delete.
  - Same-turn pre-card checks (project existence, public-project rejection -- routines in public projects are managed from the UI only, behind the `public_project_routines` feature gate -- name collision, skill access) run in `routine_precard_check` (see [Routine Pre-card Checks](#routine-pre-card-checks)); execute re-verifies the project/public/name guards (a unique-index violation surfaces as a clean `RuntimeError`).
  - Validation shared with `edit_routine` (schedule spec, model check, skill-id lists) plus the execute-time `apply_schedule_spec()` helper live in `chat/action_request_types/_routine_validation.py`. `display_name` is `"Create Routine"`, `approve_label` is `"Create"`. Returns `{"success": True, "routine_id"}`. Spec'd in the `system:routines` system skill.
- **`edit_routine`** (`EditRoutineHandler` in `chat/action_request_types/edit_routine.py`): Partial-edits an existing project routine keyed by `routine_id` (the UUID the model gets from the `list_routines` read tool). Routines are an internal Quest store, so there is no `validate_against_upstream`. Editable pieces:
  - `name` (<=100 chars, per-project unique) / `prompt` (FULL replacement, <=16 KB -- routine prompts are small, so no search-and-replace; the approval card still shows a reviewable line diff: the pre-card check injects `content_diff` built by `build_content_diff()` from `chat/action_request_types/_skill_content_edit.py` comparing the current vs proposed prompt, and `render_preview()` emits it as the same `skill_content_diff`-typed field the skill edits use, rendered by `SkillContentDiffPreview.tsx`) / `model` (validated against `MODEL_REGISTRY`, deprecated ids rejected) with a `clear_model` reset flag;
  - a full-replacement `schedule` spec (`schedule_type` daily / hourly / every_n_minutes with the matching fields, optional `is_enabled` defaulting true -- created when the routine has no schedule, fully replacing it otherwise, with type-irrelevant fields cleared) or `clear_schedule` to delete it (`schedule`/`clear_schedule` and `model`/`clear_model` are mutually-exclusive pairs);
  - and `add_skill_ids` / `remove_skill_ids` toggling rows in `routine_skill_autoloads` (adds gated on `user_can_access_skill`, re-verified up front at execute time before any change is applied).
  - Empty payloads (only `routine_id`) are rejected. The deprecated per-routine guide override is deliberately not editable. Shared validation lives in `chat/action_request_types/_routine_validation.py`; same-turn pre-card checks plus an optimistic-concurrency token capture run in `routine_precard_check` (see [Routine Pre-card Checks](#routine-pre-card-checks)).
  - At execute time the routine must still belong to the conversation's project, and routine-row field updates go through `update_routine()` with the proposal-time `expected_updated_at` token so a routine edited while the card sat open fails with a clean re-propose message instead of clobbering (`StaleRoutineError` -> `RuntimeError`). Daily schedules convert `daily_time_local` + IANA `timezone` to `daily_time_utc` via `_local_time_to_utc` from `chat/schedule_routes.py` (inside the shared `apply_schedule_spec()`).
  - `display_name` is `"Edit Routine"`, `approve_label` is `"Save"`. Returns `{"success": True, "routine_id"}`. Spec'd in the `system:routines` system skill; see [Routines Architecture -- Agent Tools](routines.md#agent-tools).

## Tool Declaration

The `create_action_request` tool is declared in `chat/llm/tool_schemas.py` as a top-level-only tool (not available to sub-agents). It accepts `request_type`, `params`, and `reasoning` parameters. The tool description is generic; the per-backend request-type catalog (exact `params` shapes) lives in each backend's `api/*.get_instructions()` under a "Write Operations" section, which is surfaced to the model through the corresponding `system:<name>` skill.

`request_type` values are constrained at the schema layer by `ACTION_REQUEST_TYPE_ENUM` in `chat/llm/tool_schemas.py` -- seeded from the core `ActionRequestType` enum in `db/models.py` and extended in place by plugin registration (see [Plugins](plugins.md)).

**Sub-agent escape hatch.** Sub-agents (`agent_task` / `agent_task_parallel` / `agent_task_parallel_template`) cannot call `create_action_request` themselves -- the tool is absent from `SUB_AGENT_TOOLS`, only the top-level loop has the matching dispatch arm and `SuspendForActionRequest` machinery, and only the top-level run has the inline approval card UI to suspend on. A sub-agent that needs an approval-gated write returns the proposed `request_type` + `params` + `reasoning` to the parent via `agent_task_response`, and the parent issues `create_action_request` on its behalf.

The model-facing copy that carries this rule lives in:

- the sub-agent system prompt (`get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py`)
- the `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` / `create_action_request` / `agent_task_response` tool descriptions in `chat/llm/tool_schemas.py`
- the `system:action_requests` skill (`chat/system_skills/catalog.py:_action_requests_content`)
- a one-sentence reminder in each per-backend skill's "Write Operations" section (`api/calendar.py` and plugin skills' instruction content, e.g. `plugins/slack/instructions.md`, `plugins/telegram/instructions.md`)

Tests in `tests/test_create_action_request_schema.py` and `tests/test_system_skills.py` pin the wording so it cannot silently disappear.

Approval-gated writes therefore should not be delegated to a sub-agent in the first place -- the parent should hand the sub-agent the data-gathering / drafting work and reserve the write itself.

When the agent calls `create_action_request`, the backend (in the dispatch handler `_handle_create_action_request()` at `chat/gemini_api/turn_tools.py`):

1. In Slack-driven runs (`is_slack_origin` is true), short-circuits with a structured error before any DB writes -- the approval card lives in the web app and a blocking suspend on a card the Slack-only user cannot see would deadlock the conversation. The matching prompt-level gate adds `create_action_request` to the `top_level_exclude` set in `chat/gemini_api/system_prompt.py` so the tool is not even enumerated in Slack mode.
2. Validates the `request_type` against the handler registry in `chat/action_request_types/registry.py`.
3. Creates a row in the `action_requests` table via `db/action_request_store.py` with status `open`.
4. Creates a linked `tool_wait_handles` row of `kind="action_request"` via `tool_wait_handle_store.create_handle()`. `correlation_kind="action_request"` and `correlation_id=str(request_id)` are populated at create time (the request id is known up front). The wait handle's `tool_id` is the `create_action_request` LLM tool_use id, and the `payload` carries `{request_id, request_type, params}`.
5. Emits the `action_request` WebSocket event and appends the structured chat-history message (both carry the new `wait_handle_id`). The boundary save in `_capturing_on_event` flushes the `tool_use` event to `sdk_history.json` before the dispatch arm raises, so the dangling-tool_use shape is durable across restarts.
6. Raises `SuspendForActionRequest(handle_id, tool_id, request_id)`. The dispatch loop in `chat/gemini_api/conversation.py` catches it **per call** (only for the `create_action_request` registry key -- `return_to_caller` keeps its immediate unwind), records it, and keeps dispatching the rest of the same tool-call batch; after the batch it re-raises the first sentinel with the others' handle ids on `sibling_handle_ids` (logging only).
   - The top-level handler catches that and returns the accumulated `structured_messages` cleanly without emitting a `tool_result` for any suspended call -- this matches the `SuspendForSlackReply` pattern used by `send_slack_reply_and_get_response`. The dangling `create_action_request` tool_use(s) stay on disk until the resume bucket closes them.

**Parallel action requests.** Models (Opus in particular) often emit several `create_action_request` calls in one response. Because the loop no longer unwinds on the first suspend, every call in the batch opens its own card and `kind="action_request"` wait handle, and the non-suspending calls in the same batch (e.g. a workspace read) still run and persist their `tool_result` to `chat_history.json`.

The composer stays locked until **all** cards are resolved (`pending_wait_handles` lists one entry per card; the placeholder reads "Approve, revise, or stop the N pending requests to continue."), and the user may Approve / Revise them in any order (Stop on any one card stops its open siblings too -- see below). The headless resume kicked by each resolve is held by `_unresolved_blocking_handle_ids()` in `chat/wait_handles/resume.py` while any dangling `create_action_request` tool_use still has a pending row, so the model only continues once it can be handed every verdict at once -- one `tool_result` per call.

The model-facing guidance (system prompt tool list + `system:action_requests` "Approval lifecycle" item 5) says to batch independent actions this way and to stay sequential when one action needs another's result.

Step 2's `validate_params(...)` is strict about unknown keys -- if the model passes a key the handler does not recognise, steps 3-6 are skipped entirely and the model is told on the same turn. See [Param Validation](#param-validation) below for the full short-circuit shape.

When the user clicks Approve or Revise, the resolve endpoint (see [REST API Endpoints](#rest-api-endpoints) below) flips the `action_requests` row, resolves the linked wait handle with a `{verdict, request_id, feedback?, result}` response, and calls `wait_resume.maybe_kick_resume()`.

The headless resume runs `run_conversation_turn(message="")` against the conversation (once no sibling card from the same batch is still pending -- see the parallel note above); the resume bucket sees each dangling `create_action_request` tool_use, looks up the matching `kind="action_request"` row by `tool_id`, and closes the tool_use with the row's `response` dict verbatim (or with `{"status": "still_waiting"}` if the resume races ahead of the resolve endpoint).

### Stop

The card's third button is **Stop**, not Deny: it discards the request AND halts the conversation loop. The endpoint flips the row to `stopped` (`result: {"stopped": true}`), resolves the handle as `stopped` with `{"verdict": "stopped", ...}`, publishes `wait_handle_resolved` so the composer unlocks, stops every other open card of the same conversation (a parallel batch cannot be left half-open -- the composer would stay locked and approving a sibling later would resume as if nothing had been stopped), and deliberately kicks NO resume.

`_run_resume()` additionally holds whenever a dangling `create_action_request` tool_use has a `stopped` row (`_stopped_handle_ids()` in `chat/wait_handles/resume.py`), so a stray re-kick never wakes the model without input.

The dangling tool_use stays open on disk until the user's next message: that send enters the resume bucket, which closes it with the row's `stopped` response plus a `note` explaining that the request was not executed and the new message follows (`_stopped_action_request_result()` in `chat/gemini_api/conversation.py`), and appends the user's text to the same turn. From the model's point of view `create_action_request` simply returned late with `verdict: "stopped"`.

In short, Stop is what the user clicks when they do not want the action AND have nothing further to say right now: nothing runs, the model does not get another turn, and the next message the user sends is what the model sees next. To reject with an explanation and let the model react immediately, use Revise instead.

`subagent_return` cards keep a hard Deny instead (the target user cannot type into the read-only subagent conversation, so "wait for the next message" has no meaning there); `action: "stop"` on such a card is treated as that deny. A dangling sibling tool_use that has no row but did run (a regular tool dispatched in the same batch) is closed with its persisted `tool_output` replayed from `chat_history.json` via `_load_persisted_tool_results()`.

The model then continues on the same turn, reading the verdict directly off the tool's return value -- no follow-up `wait_for_handles` is needed. The multi-step accept-then-create pattern (e.g., create a Drive folder, then upload files bound to the new `folder_id`) collapses to two sequential `create_action_request` calls. The `system:action_requests` system skill in `chat/system_skills/catalog.py` documents the verdict shape and the multi-step pattern with worked examples.

## Param Validation

Same-turn rejection (no card, no row, no event) flows through two stages: a synchronous local `validate_params(...)` first, then an awaited upstream-service hook for handlers that have one (see [Upstream Validation Hook](#upstream-validation-hook) below). Both raise `ValueError`, both are caught by the same `except ValueError` block in the dispatch handler at `chat/gemini_api/turn_tools.py`, and both deliver the rejection to the model as `{"error": "Invalid parameters: ..."}` so the model self-corrects without bothering the user.

Every handler's `validate_params(params)` rejects unknown keys up front by calling the shared helper `reject_unknown_params(request_type, params, allowed, *, path=None)` in `chat/action_request_types/_param_validation.py` as its first step. The helper raises `ValueError` listing the unknown keys (sorted) and the allowed set (sorted) -- e.g. `Unknown parameter for send_slack_message: ['channel']. Allowed parameters: [...]`.

The motivating bug: a model proposing a request with a stray key not on that handler (the failure shape generalizes to any unknown key) used to silently drop the field and persist a card the user approved without seeing it; with strict validation the model sees the rejection and either drops the bogus key or reaches for the right handler.

Nested junction-row entries reuse the same helper with a `path` argument so the error message keeps the path-style format produced by the per-row validators (e.g. the `attachments[]` / `link_attachments[]` / `rename_attachments[]` / `rename_link_attachments[]` rows whose validators live in `chat/action_request_types/_io_attachments.py`, and plugin handlers' nested rows).

The server-injected post-validation keys (`calendar_name` for `create_calendar_invite` and `edit_calendar_event` (plus `current_event` for the latter), `folder_name` and `new_folder_parent_name` for `upload_to_drive`, and `parent_folder_name` for `create_drive_folder`) are added to `validated_params` by `_enrich_params_for_preview()` in `chat/gemini_api/turn_tools.py` after `validate_params()` returns, so they are intentionally not in any handler's allow-list.

Handlers can do the same via the `ActionRequestHandler.enrich_params_for_preview(params, user)` hook (default no-op), called right after the legacy ladder -- plugin handlers must use the hook since the ladder is core-only (the Slack plugin's `send_slack_message` injects `channel_name` and `thread_context` this way, and the Telegram plugin's `send_telegram_message` injects `dialog_name`).

## Upstream Validation Hook

After `handler.validate_params(...)` returns and **before** any `action_requests` row, wait handle, or `action_request` event is written, the dispatch arm awaits `handler.validate_against_upstream(validated_params, user)`. The default implementation on `ActionRequestHandler` (`chat/action_request_types/base.py`) is a no-op that returns `validated_params` unchanged; a handler whose upstream offers a dry-run endpoint overrides it to POST the same body that `execute()` would send, so FK / enum / cross-field rejections become same-turn errors instead of post-Approve failures.

An upstream `400 {"valid": false, "errors": {...}}` is reformatted by `_format_errors_payload` (sorted keys, deterministic JSON) and raised as `ValueError("Upstream validation failed: ...")`; the dispatch arm's existing `except ValueError` catch wraps it in the same `{"error": "Invalid parameters: ..."}` envelope local `validate_params` rejections take, so the model self-corrects on the same turn with no card.

A `404 {"valid": false, "error": "..."}` (missing `<entity>_id` on the edit endpoints; note the singular `error` key) raises `ValueError("Upstream validation: <error>")` through the same path.

Network errors, timeouts, unparseable bodies, 401/403 (bad upstream credentials), and any 4xx/5xx outside the documented 400/404 contract log a WARNING and fall through to local-only validation -- the proposal is created, the user clicks Approve, and the real upstream error surfaces from `execute()` if it is still relevant. Treating a transient outage as `Invalid parameters` would mislead the model. The timeout is 10s.

An upstream dry-run typically covers what the mutation-time validation covers: FK existence, enum / choice whitelists, decimal and date parsing, and cross-field rules. It cannot cover everything (e.g. uniqueness races or checks the dry-run endpoint skips), so some rejections still surface only at Approve time and keep the request open for retry. Local checks survive only where the upstream silently lets them through:

- **Unknown-key rejection** (`reject_unknown_params`). Django's `data.get(...)` pattern silently ignores unknown keys; without the local pre-check the model could propose a stray field, get `{"valid": true}` back, and have the mutation silently drop the field (the bug devplan 00075 fixed).
- **Empty-payload rejection on every edit handler** (no editable field beyond `<entity>_id`).
- **`attachments` workspace-path validation** in `_io_attachments.py`. The validate call deliberately omits the `attachments` list (the upstream validator does not read file bytes; workspace disk reads stay in `execute()` so a missing file does not invalidate an open proposal).
- **Boolean strict-`isinstance(bool)` checks** -- no truthy coercion.

## Skill Pre-card Checks

`create_skill` / `edit_skill` need async, DB-and-user-context checks that the synchronous `validate_params()` cannot perform (name-collision lookups, project-access gates, target resolution). These run in the dispatch arm right after `validate_against_upstream()`, before any `action_requests` row -- by calling `skill_precard_check()` in `chat/action_request_types/skill_precard.py` (the dispatch arm threads `conversation_id` in for the read gate).

The function raises `ValueError` on a failure, reusing the same `except ValueError` early-return that surfaces `Invalid parameters` with no card so the model can self-correct same-turn. It performs:

- **Name-collision pre-check**: for `create_skill` (when `name` is set) and `edit_skill` (only when `name` is changing), scoped per the resolved target -- per-creator for user skills, per-project for project skills, excluding the edited skill's own id.
- **Project-access pre-check**: when the resolved target is a project skill, verifies the turn's `project_id` is set, matches the skill's `project_id`, and that the user owns the project.
- **`project_autoload` mismatch pre-check**: rejects `project_autoload` on `edit_skill` when the target is a user skill.
- **Content-edit resolution**: for `edit_skill` `old_string` / `new_string` edits, enforces the read-before-edit gate (the conversation must have loaded or read the skill, per `ChatStorage.get_skill_read_ids`) and applies `apply_content_edit()` against the current content so a not-found / ambiguous match (or an empty / oversized result) is rejected same-turn with no card.
- **Preview enrichment**: injects `current_skill_name` / `skill_target` for `edit_skill` (server-injected, not in `_ALLOWED_PARAMS`, parallel to `calendar_name`) so the card header reads nicely, plus -- for content edits -- the `content_diff` line diff built by `build_content_diff()` in `chat/action_request_types/_skill_content_edit.py`.
  - `render_preview()` surfaces it as a `skill_content_diff`-typed preview field (`+A / -R line(s)` plus the full typed line list) rendered by `SkillContentDiffPreview.tsx`, which shows the changed hunks with 2 context lines by default and a "Show full skill content" toggle for the whole body; without the injected diff it falls back to plain `Replace` / `With` rows.

These are an agent-experience optimization; the execute-time guards in the handlers remain the authoritative TOCTOU enforcement (a user could create a colliding skill between the pre-check and Approve). This is the only handler family whose pre-card hook is a separate module rather than a `validate_against_upstream()` override, because skills have no external `/validate/` endpoint and the checks are internal-DB lookups.

## Routine Pre-card Checks

`create_routine` / `edit_routine` get the same treatment via `routine_precard_check()` in `chat/action_request_types/routine_precard.py`, called from the same dispatch-arm spot as `skill_precard_check()`. It rejects (same-turn `ValueError`, no card):

- conversations outside a project;
- for `create_routine` a missing or public project and a `name` colliding with an existing routine;
- for `edit_routine` a `routine_id` that does not exist or belongs to a different project and a `name` change colliding with another routine;
- and (both types) added skill ids the user cannot access.

It also injects preview enrichment:

- a `skill_names` id->name map for the skill rows,
- for `create_routine` the default `model` + `model_defaulted` flag when the proposal omitted a model (see [Routines Architecture](routines.md)),
- and -- for `edit_routine` -- `current_routine_name` for the card header, the routine row's `updated_at` as `expected_updated_at` (the execute-time optimistic-concurrency token described in the `edit_routine` bullet above), and `content_diff` (the `build_content_diff()` line diff of the current vs proposed prompt) whenever the edit changes the prompt, so the card renders the same collapsible diff snippet as skill content edits.

## Handler Registry

The `chat/action_request_types/` package implements an extensible registry pattern for action request handlers.

**`ActionRequestHandler` base class** (`chat/action_request_types/base.py`): Defines the interface for all handlers with an abstract `execute(params, user, *, conversation_id=None, project_id=None)` method, an async `render_preview(params, user)` method that returns a list of `PreviewField` dicts (`{key, value}`), an `approve_label` property (defaults to `"Approve"`), and the two collapsed-card members:

- a `resolved_label` property (the status word shown once the request is executed -- defaults to `"Sent"`, overridden per handler to `"Created"` / `"Saved"` / `"Uploaded"` etc.)
- a `summary_snippet(params)` method (a short params summary for the collapsed card; defaults to `""`, truncated to 80 chars by `get_summary_snippet()` in the registry, and exceptions degrade to no snippet so legacy params shapes can never break rendering).

Most handlers ignore the `conversation_id` / `project_id` kwargs; handlers with workspace file attachments use them to resolve the conversation workspace for file uploads (`project_id` is omitted at the call site so the storage layer infers project membership from `conversation_id`).

The `render_preview()` method is responsible for generating human-readable preview data for the frontend, including any external name resolution (e.g., looking up Slack channel names via API). The `get_preview_for_request()` async function in `chat/action_request_types/registry.py` looks up the handler by request type, calls `render_preview()`, and includes `display_name`, `approve_label`, `resolved_label`, and `summary_snippet` in the payload (with generic fallbacks for unknown types).

**Registry** (`chat/action_request_types/registry.py`): A module-level dict mapping `request_type` strings to `ActionRequestHandler` subclass instances. New request types are added by subclassing `ActionRequestHandler` and registering the instance.

**`send_slack_message` handler** (`SendSlackMessageHandler` in `plugins/slack/handlers.py`): Sends a Slack message to any channel, group, or DM using the user's Slack token. Requires the `chat:write` scope on the user token (and `im:write` when targeting a user ID for DMs).

The `params` object must contain `channel_id` (a Slack channel ID like C..., group ID like G..., or user ID like U.../W... for DMs) and `message` (text content in Slack mrkdwn format, max 3000 characters to fit Slack's section block text limit), and may optionally include `thread_ts` (a Slack message timestamp string to reply in a thread).

`validate_params()` rejects unknown keys, requires non-empty `channel_id` / `message`, and enforces the Slack object-id shape on `channel_id` and the `<unix_seconds>.<microseconds>` shape on `thread_ts` (when non-empty; whitespace-only is treated as absent and not carried forward). The two regexes (and the recognised-prefix tuple) live at module scope in `chat/action_request_types/send_slack_message.py`; format errors name the field and echo the offending value truncated to ~100 chars so the model self-corrects on the same turn (no `action_requests` row, no card, no wait handle).

Whitelisting the prefix set means malformed channel ids (`#channel`, `@user`, permalink URLs, lowercase ids, raw ints, unknown prefixes) and permalink-style `p...` thread ids are short-circuited before they would have surfaced as opaque Slack `channel_not_found` errors inside `execute()`. The handler's `approve_label` is `"Send"`. When a user ID is provided as `channel_id`, the handler opens a DM conversation automatically via `conversations.open`.

The handler's `render_preview()` method resolves channel names via `_resolve_slack_channel_name()` in `chat/action_request_types/slack_helpers.py`, which calls `conversations.info` for channel/group IDs and `users.info` for user IDs, producing human-readable names (e.g., #general, @alice).

When `thread_ts` is present, `render_preview()` fetches thread context via `_fetch_slack_thread_context()` in `chat/action_request_types/slack_helpers.py`, which calls Slack's `conversations.replies` API to retrieve the parent message and up to 3 latest replies (with sender names resolved via `_resolve_slack_user_name()`). The thread context is included in the preview fields.

When executing the approved request, the handler passes `thread_ts` through to Slack's `chat.postMessage` API to post the reply in the thread.

Messages are sent using Block Kit formatting via `_build_slack_blocks()` in `chat/action_request_types/slack_helpers.py`: a `section` block containing the message body, followed by a `context` block with dynamic Quest attribution text (where "Quest" links to the app URL).

The attribution includes the sender's first name when available (e.g., "_Alice created and sent this message using Quest_"), falling back to "_Created and sent using Quest_" when no name is available. The sender's first name is extracted from the user's Google OAuth name via `_extract_first_name()` in `chat/action_request_types/helpers.py`.

`_build_slack_blocks()` accepts an optional `first_name` parameter that both `SendSlackDmHandler.execute()` and `SendSlackMessageHandler.execute()` pass through after extracting it from `user["name"]`.

On success, the handler returns a result dict with the Slack API response. On failure, the request stays in `open` status so the user can retry.

**`send_telegram_message` handler** (`SendTelegramMessageHandler` in `plugins/telegram/handlers.py`, registered by the Telegram plugin): Sends a Telegram message to any dialog (user, group, or channel) using the user's existing Telethon session via `TelegramClientManager` in `plugins/telegram/upstream.py`. The `params` object must contain `dialog_id` (a numeric Telegram dialog ID) and `message` (text content, max 4096 characters to match Telegram's message length limit).

`validate_params()` rejects unknown keys, requires a non-empty `dialog_id` / `message`, and format-validates `dialog_id`: the value must be a Python `int` or a digit string with an optional leading `-` (the `_DIALOG_ID_STR_RE` regex at module scope), so `bool` (which subclasses `int` and would coerce to `0`/`1`), `float`, `list`, `dict`, whitespace-padded strings, `@username`, `t.me/...` URLs, `+E.164` phone numbers, and Slack-style `C.../U...` ids all fail up front.

The integer is then range-checked against three bad shapes: `0` (no valid peer is zero), the bare `-100` supergroup/channel marker prefix (the full form is `-100<digits>`), and `|dialog_id| > _DIALOG_ID_MAX_ABS` (10^15 -- catches 17+ digit ids misrouted from Twitter / Slack / a phone number while leaving legitimate ~13-digit supergroup ids intact).

Format errors name the field and echo the offending value truncated to ~100 chars via `_truncate_for_error` so the model self-corrects on the same turn (no `action_requests` row, no card, no wait handle) instead of failing inside `execute()` with an opaque Telethon `PeerIdInvalid`; coverage is in `plugins/telegram/tests/test_telegram_send_validation.py`. The handler's `approve_label` is `"Send"`. The message body is HTML-escaped via `html.escape()` for safety and sent with HTML parse mode. An italic attribution footer is appended to every message.

The handler's `enrich_params_for_preview()` hook resolves dialog names via `resolve_dialog_name()` in `plugins/telegram/upstream.py`, which obtains a Telethon client from `TelegramClientManager` and calls `client.get_entity(dialog_id)` to look up the entity's display name (first/last name for users, title for groups/channels); the label is always re-derived from `dialog_id`, so a model-supplied `dialog_name` can never survive.

On success, the handler returns a result dict with `success`, `dialog_id`, and `message_id`. If the user's Telegram session is not connected or has expired, the handler raises a `RuntimeError` directing the user to connect via Settings.

**Shared Drive plumbing** (`chat/action_request_types/_drive.py`): The two Drive-backed handlers share one module for the Drive API base constants, the `drive.file` scope check helpers, the reauth error message, Google API error extraction, the best-effort `_resolve_folder_name()` preview helper (`GET {DRIVE_API_BASE}/files/<id>?fields=name&supportsAllDrives=true`, mirroring `_resolve_calendar_name`; falls back to the raw id or "My Drive (root)"), and the `create_folder()` call (`files.create` with the folder mimeType, `supportsAllDrives=true`) used by both `create_drive_folder` and `upload_to_drive`'s on-the-fly folder path.

**`upload_to_drive` handler** (`UploadToDriveHandler` in `chat/action_request_types/upload_to_drive.py`): Uploads one or more workspace files to the user's Google Drive after a single approval, reusing the same multipart upload mechanism as the file-browser "Save to Drive" feature (`chat/file_routes.py:save_file_to_drive`) but uploading the **raw bytes** rather than converting markdown to a Google Doc.

`validate_params()` requires a `files` list of 1..10 `{path, filename?}` entries (rejecting absolute paths, `..` traversal, and duplicate paths up front; per-entry `filename` sanitized via `_sanitize_workspace_filename`, dropped if it sanitizes empty so `execute()` falls back to the basename; inner shape validated by the shared `validate_attachments_param()`).

It still accepts the legacy top-level `path` / `filename` single-file shape (mutually exclusive with `files`) for pending pre-deploy rows, accepts an optional `folder_id`, and accepts the optional create-destination-folder pair `new_folder_name` / `new_folder_parent_id` (`new_folder_name` is mutually exclusive with `folder_id`; `new_folder_parent_id` is only valid alongside `new_folder_name`).

File existence and the per-file 50 MB size cap are deferred to `execute()` (where `conversation_id` is available for workspace resolution).

On execution the handler:

- asserts a connected Google Services token,
- performs an explicit `drive.file` scope pre-check (raising a "reconnect Google Services" error on legacy connections that predate the write scope),
- pre-flight resolves **every** file via `resolve_workspace_file()` in `chat/action_request_types/_io_attachments.py` (traversal guards + 50 MB cap) so a missing/oversize file fails the batch before any Drive mutation,
- creates the destination folder via `_drive.create_folder()` when `new_folder_name` is present,
- then loops over the files -- reading each one's bytes via `read_resolved_file_bytes()` (one file in memory at a time) and POSTing a `multipart/related` body (metadata JSON + raw bytes) to `{DRIVE_UPLOAD_BASE}/files?uploadType=multipart&supportsAllDrives=true` via `make_authenticated_request()`.

Each MIME type is guessed from the filename (never `application/vnd.google-apps.document`, which would trigger conversion); the created folder's id (or `folder_id`, when present) is set as `parents`. The result returns `{"success": True, "count": N, "files": [{"file_id": ..., "name": ..., "url": "https://drive.google.com/file/d/<id>/view"}, ...]}` (legacy `path` requests keep the flat single-file shape), extended with `folder_id` / `folder_name` / `folder_url` when a folder was created so the model can chain follow-up upload batches into it.

If an upload fails mid-batch, the raised error lists the already-uploaded files (name + file id) and, after folder creation, the created folder's id, steering the model toward a fresh request with only the remaining files and `folder_id` -- re-approving the still-open request would re-upload the successes and create a duplicate folder (Drive permits same-name siblings).

`render_preview()` renders File / Source / Destination rows for one file, or a `Files` count row plus per-file `File N` rows (renames shown as `path → name`) plus Destination for a batch, resolving `folder_id` (or the new folder's parent) to a human folder name via `_resolve_folder_name()`. The `display_name` is `"Upload to Drive"` and `approve_label` is `"Upload"`.

The model-facing parameter spec lives in `api/drive.py` (`get_instructions()`, "Google Drive Write Operations" section), surfaced via the `system:drive` skill, which also carries the "Batch your uploads" guidance (batch same-destination files into one request; fold folder creation in via `new_folder_name`; split only for >10 files or multiple destinations) alongside the generic "Minimize approval round trips" section in `system:action_requests`.

**`create_drive_folder` handler** (`CreateDriveFolderHandler` in `chat/action_request_types/create_drive_folder.py`): Creates a Google Drive folder after approval via `_drive.create_folder()`. `validate_params()` requires a non-empty `name` (trimmed; no filename sanitization -- folder names are Drive metadata, not filesystem paths) and accepts an optional non-empty `parent_folder_id` (shared drives supported; defaults to the Drive root).

On execution the handler runs the same Google-connection and `drive.file` scope pre-checks as `upload_to_drive`, then returns `{"success": True, "folder_id": ..., "name": ..., "url": "https://drive.google.com/drive/folders/<id>"}`. `render_preview()` renders Folder / Parent rows, resolving `parent_folder_id` via `_resolve_folder_name()` (server-injected `parent_folder_name`) and showing "My Drive (root)" when no parent is given.

The `display_name` is `"Create Drive Folder"` and `approve_label` is `"Create"`. The model-facing parameter spec lives in the same `api/drive.py` `get_instructions()` section, which also directs the model to search for an existing folder via `authed_get` first because Drive allows same-name sibling folders.

**`edit_google_spreadsheet` handler** (`EditGoogleSpreadsheetHandler` in `chat/action_request_types/edit_google_spreadsheet.py`): Overwrites a cell range in a Google Spreadsheet after approval; the full behavior (read-before-write verification at proposal AND approve time, context-window capture, scope pre-check, `values.update` call) is described in the type list at the top of this document.

The following all live at module scope:

- Range parsing (`_parse_range` -- bounded A1 rectangles only, normalized so start <= end)
- A1 column-letter conversion
- cell normalization (`_normalize_cell`: `None` -> `""`, bools -> `TRUE`/`FALSE`, integral floats -> int repr)
- the formula-aware cell comparison (`_cell_matches_live`: formula cells -- detected via the FORMULA rendering -- match only on formula text; plain cells match any of the three renderings with a `float()` equality fallback so `"1,234.50"` formatted reads match `1234.5` unformatted reads)
- and the trailing-empty-cell padding the Sheets values API requires (`_pad_grid`)

It reuses `_extract_google_api_error` / `_get_authorized_google_scopes` from `_drive.py`.

**`send_slack_dm` handler** (legacy): Preserved for backward compatibility. Behaves identically to `send_slack_message` but accepts a `user_id` parameter instead of `channel_id`, targeting only DMs.

## REST API Endpoints

All endpoints are defined in `chat/action_request_routes.py` and require session cookie or Bearer token authentication. The endpoints cover listing (with optional enriched context), counting (by status), fetching individual requests, and resolving (approve / revise / stop). Each response object is enriched with server-rendered preview data via `_enrich_with_preview()`, which calls `get_preview_for_request()` from `chat/action_request_types/registry.py`. See `chat/action_request_routes.py` for the full endpoint definitions.

The `POST /app/api/action-requests/{id}/resolve` endpoint is the single entry point for both UI flows (inline `ActionRequestMessage` and the cross-conversation `RequestsView` pane). The body is a `ResolveRequestBody` (`chat/action_request_routes.py`) carrying an `action` discriminator (`"execute"`, `"deny"`, or `"stop"`) and an optional `feedback` string used by the **Revise** flow.

Revise posts `action: "deny"` with `feedback`; Stop posts `action: "stop"`. A bare `action: "deny"` without feedback is the legacy one-click Deny: the endpoint still accepts it (and `subagent_return` cards still send it), but no action card in the web UI offers it any more -- the UI's third button is Stop. The validator strips whitespace, treats empty strings as `None`, and truncates to `_MAX_FEEDBACK_LEN` (4000 chars) so an oversize message never fails the resolve.

On Approve and Revise the endpoint also resolves the linked wait-handle row and kicks a headless resume so the suspended `create_action_request` tool_use is closed; on Stop it resolves the handle but kicks nothing (see [Stop](#stop) above). The bridge helper `_resolve_linked_wait_handle()` in the same file looks up the pending handle via `tool_wait_handle_store.find_pending_for_action_request(user_id, request_id)`, calls `tool_wait_handle_store.resolve_handle()`, and then calls `wait_resume.maybe_kick_resume()`:

- **Stop** -- handle goes to `stopped` with `response={"verdict": "stopped", "request_id": N, "result": {"stopped": true}}`; the action_request `result` is `{"stopped": true}`. No resume is kicked; the resume bucket adds an explanatory `note` when the user's next message closes the tool_use.
- **Revise (and the legacy bare deny)** -- handle goes to `rejected`. The action_request `result` is `{"denied": true, "feedback": "<user text>"}` (Revise) or `{"denied": true}` (legacy bare `deny` / `subagent_return` Deny). The wait-handle `response` mirrors that as `{"verdict": "denied", "request_id": N, "feedback"?: "<user text>", "result": {...}}`; `feedback` is also surfaced at the top level of `response` so the resumed model reads `response.feedback` next to `response.verdict` directly off the `create_action_request` tool result without digging into nested `result`.
- **Execute success** -- handle goes to `accepted` with `response={"verdict": "executed", "request_id": N, "result": <handler.execute() return value>}`. The model reads the backend's return payload (e.g., the new `company_id` or `note_id`) directly out of `response.result`.

Both wait-handle resolves are best-effort and wrapped in a try/except, mirroring `chat/memory_routes.py` -- a missing or already-resolved handle is logged and ignored. In-flight action requests that were created before this feature shipped have no linked handle, so the lookup returns `None` and the resolve is a no-op (graceful rollout).

After resolving the DB rows on either branch, the endpoint also calls `ChatStorage.update_action_request_message()` (`chat/storage.py`) to rewrite the matching `{"type": "action_request", "request_id": N}` entry in `chat_history.json` with the new `status`, `result`, and (on Revise) `feedback` fields, so a page reload or replay shows the same outcome the live UI shows. Disk failures are swallowed; the persisted DB rows remain authoritative.

## Frontend Rendering

Action requests are rendered inline in the chat conversation via the `ActionRequestMessage` component in `frontend/src/components/ActionRequestMessage.tsx`. Key behaviours:

- **Composer locked while pending**: The chat input is disabled on the suspended conversation whenever a `kind="action_request"` wait handle is still `pending`. `useConversation` exposes `hasPendingWait` and `pendingWaitKind`, which `ChatPanel` ORs into `inputDisabled` (also gating paste); the textarea placeholder switches to "Approve, revise, or stop the pending request to continue."
  - This holds across reloads, tab switches, and the Requests-pane round trip because the gate is driven by the `pending_wait_handles` field on `GET /conversations/{id}` and by `wait_handle_resolved` events on the persistent WebSocket, not by per-tab streaming state. See [Wait Handles -- Composer Sync](wait-handles.md#composer-sync). The user can still start a new conversation or open the cross-conversation Requests pane while the suspended run waits.
- **Persistent state**: Action request status is stored in the database (not in-memory), so it survives page reloads and conversation switches.
- **Server-rendered previews**: Preview rendering is driven by `preview_fields` (a list of `{key, value}` dicts) provided by the server. The frontend generically loops over these fields to render key-value rows, rather than containing type-specific rendering logic.
  - A field may additionally carry a `type` discriminator for structured previews: `type: "spreadsheet_diff"` (from the `edit_google_spreadsheet` handler) carries a `grid` payload that `SpreadsheetDiffPreview` (`frontend/src/components/SpreadsheetDiffPreview.tsx` + `.css`, shared by `ActionRequestMessage` and `RequestsView`) renders as a spreadsheet-style table -- sticky row-number and column-letter headers, up to 2 rows/cols of muted context cells around the replaced range, changed cells showing the old value struck through next to the new value, unchanged-but-rewritten cells lightly tinted, plus a legend; the whole table scrolls horizontally inside the card.
  - Fields with an unknown `type` (or no `grid`) fall back to the plain key/value row using the field's text `value` (for `edit_google_spreadsheet`, a "N of M cell(s) changing" summary).
  - The `approve_label` from the server controls the approve button text (e.g., "Send" for messaging requests, "Approve" for others). For new messages that arrive via WebSocket, `preview_fields` and `approve_label` are included in the `action_request` event payload. For older messages that lack these fields, the frontend falls back to fetching preview data from the REST API (`GET /app/api/action-requests/{id}`).
- **Background notifications**: When an `action_request` event arrives on the persistent WebSocket while the tab is hidden, `WebSocketManager` calls `sendDesktopNotification()` from `frontend/src/services/desktopNotifications.ts`. The notification title is `Quest — New action request`, and the body prefers `display_name` with `request_type` as a fallback. Notification permission is requested lazily on the first user-initiated message, so receiving an action request alone does not trigger a browser prompt.
- **No content truncation**: Preview values are displayed in full (e.g., long message bodies are not truncated).
- **Approve / Revise / Stop buttons**: The component shows the agent's reasoning, the preview fields, and three buttons. The approve button label comes from `approve_label`; the Revise button (amber) opens the inline `ReviseFeedbackForm`; the Stop button (red) discards the request and halts the conversation until the user's next message.
  - On approve it calls `POST /app/api/action-requests/{id}/resolve` with `action: "execute"`. On stop it posts `action: "stop"`. On revise it posts `action: "deny"` plus `feedback: "<text>"`. `subagent_return` cards render the third button as **Deny** and post a bare `action: "deny"` (hard deny ends the subagent run).
- **Revise feedback form**: The shared `ReviseFeedbackForm` component (`frontend/src/components/ReviseFeedbackForm.tsx` + `.css`) is rendered inline in place of the button row by both `ActionRequestMessage` and `RequestsView`. It auto-focuses the textarea on mount, disables Send while the trimmed value is empty (a user who just wants out has the Stop button), submits on Enter, inserts a newline on Shift+Enter, and cancels on Esc.
- **Resolved state**: After resolution, the component collapses to a one-line summary showing the outcome. The status word and params summary are server-derived: executed cards show the handler's `resolved_label` (e.g. "Created" for creation types, "Saved" for edits, "Sent" otherwise) with its `summary_snippet` appended, revised (status `denied`) cards always show "Denied" and stopped cards "Stopped".
  - Both fields ride on the durable `action_request` message and on `GET /app/api/action-requests/{id}` responses; old messages that predate them are backfilled by the mount-time REST fallback (with a generic "Sent"/no-snippet fallback when the request row is gone). The frontend contains no per-request-type rendering.
  - When the row carries `result.feedback` (Revise path), the collapsed card also renders an italic "Feedback: ..." line under the status so the user's reasoning is visible after reload. The same line renders under revised cards in `RequestsView`.
  - A chevron toggle on the right edge of the collapsed card re-expands it in place, showing the reasoning and preview fields read-only (no buttons) so the proposed content stays accessible after resolution; the existing mount-time REST fallback backfills `preview_fields` for resolved cards that lack them.

## Execution Failure Behavior

When a handler's `execute()` method raises an exception, the action request remains in `open` status **and** the linked wait handle stays `pending` -- the resolve endpoint short-circuits with a 500 before reaching either resolve step, so neither row is touched. The resolve endpoint returns the error to the frontend, which displays it to the user, and the user can retry by clicking Approve again. On a successful retry both rows flip together, the resume bucket closes the dangling `create_action_request` tool_use, and the model wakes up to continue the turn.

This design avoids permanently losing the request on transient failures (e.g., Slack API timeout) and keeps the suspended run parked on the same handle until the action actually executes.

**Single-outcome resolution.** Because the handler's external write runs *before* the row flips to `executed`, the resolve endpoint serializes every resolution of one conversation's cards under a process-local `asyncio.Lock` keyed by `conversation_id` (`_conversation_resolution_lock()` in `chat/action_request_routes.py`; keyed per conversation rather than per request so Stop's sibling sweep never nests locks).

The row is re-read under the lock, so a double-click, a second tab, or an Approve racing a Revise runs the handler at most once and the loser gets the same `400 already_resolved` a click on an already-resolved card gets.

As the second line of defence, `resolve_action_request()` in `db/action_request_store.py` is a single conditional `UPDATE ... WHERE status = 'open'` (compare-and-set): it returns the updated row to exactly one caller and `None` to every other, and every branch of the endpoint treats `None` as `already_resolved` and skips its wait-handle / resume / subagent side effects. Regression coverage: `tests/test_action_request_resolve_race.py`.

## Account Deletion Cleanup

When a user account is deleted via `POST /app/api/delete-account`, all action requests for that user are cleaned up. The `action_requests.user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so action requests are automatically removed at the database level when the user record is deleted.

## Requests Single-Pane View

In addition to inline action request rendering within chat conversations, the frontend provides a dedicated full-pane Requests view for managing all action requests across conversations.

### Data Access Layer

`db/action_request_store.py` provides additional functions for the Requests view: efficient count queries (with optional status filter and grouped-by-status counts) and an enriched listing (`list_action_requests_enriched(user_id, status=None)`) that batch-loads conversation, routine, and project data, with an optional `status` filter applied at the DB level (`open`/`executed`/`denied`; `None` returns all statuses) via the composite `ix_action_requests_user_status_created` index. See `db/action_request_store.py` for the full function signatures.

### API Endpoints

The count and enriched listing endpoints are in `chat/action_request_routes.py`. See the route definitions there for query parameters and response shapes.

### Frontend Components

**RequestsView** (`frontend/src/components/RequestsView.tsx` + `RequestsView.css`): Full-pane component replacing the chat panel and file browser when active. Features include:
- Filter tabs: Open (default), Executed, Denied (Revise outcomes -- the status is still named `denied`), Stopped, All -- with server-provided counts in each tab badge via `fetchActionRequestCounts()`. Switching tabs re-fetches the matching slice via `fetchActionRequestsEnriched(include_context, status)`, passing the tab name as the `status` query param (omitted on the All tab). DB-side filtering is backed by the `ix_action_requests_user_status_created` composite index, so the frontend no longer over-fetches and filters client-side.
- Request cards displaying the agent's reasoning, server-rendered preview fields, routine/project context, and conversation navigation links ("Go to conversation" uses `useNavigate()` from `react-router-dom` to navigate to `/chats/<id>` or `/projects/<pid>/<id>`)
- Approve / Revise / Stop buttons for open requests (approve button label driven by `approve_label` from the server, e.g., "Send" for messaging requests; Revise opens the inline `ReviseFeedbackForm` to send a feedback message that resolves the request as denied with `result.feedback` populated; Stop resolves it as stopped and halts that conversation until its owner types again -- `subagent_return` rows show Deny instead).
  - After a row's status is mutated locally by Approve / Revise, a render-time `requests.filter((r) => r.status === filter)` re-applies the tab predicate so the row vanishes immediately from non-All tabs without a refetch (the server-filtered slice no longer matches it); Stop additionally re-fetches the list because it also stops the clicked row's open siblings in the same conversation.
- Empty state distinguishes "no requests at all" (`serverCounts.all === 0`) from "no requests for this filter" so a user on a quieter tab is not told they have zero requests when other tabs are populated. The banner-click reload also passes the current tab's `status` so the "N new requests" indicator respects the active filter.
- Generic preview field rendering via loop over `preview_fields` from the API response
- Routine and project context display (routine name, project name) from enriched data
- Live updates: subscribes to `request_count_changed` events on the persistent WebSocket via `persistentWebSocket.onGlobalEvent()` for cross-tab immediate count refresh. The local `onRequestCountChange()` bus from `frontend/src/services/requestEvents.ts` is kept as a same-tab optimisation so a click in this view updates the badge without a server round-trip. The previous 30-second polling fallback was removed; an initial `fetchActionRequestCounts()` runs once on mount.
- Non-disruptive "N new requests" banner: when the server-reported total exceeds the count at last full load, a banner appears above the request list. Clicking the banner reloads the current tab's slice and scrolls to the top. The banner avoids disrupting the user's current scroll position or in-progress approve/revise/stop actions

**Sidebar integration** (`frontend/src/components/Sidebar.tsx` + `Sidebar.css`): A "Requests" section at the top of the sidebar displays the open request count as a badge. The badge mounts with a single `fetchActionRequestCounts()` call and updates from `request_count_changed` events on the persistent WebSocket (`persistentWebSocket.onGlobalEvent()`) -- the previous 30-second poll plus `visibilitychange` re-fetch was removed in devplan 00062.

The local `onRequestCountChange()` bus is still emitted from `ActionRequestMessage` and `RequestsView` after a same-tab approve/revise/stop so the badge updates without waiting for the server publish to round-trip; the persistent-WS event arrives milliseconds later and is idempotent. Server publishers of `request_count_changed`: the action-request resolve endpoint and the `create_action_request` dispatch handler in `chat/gemini_api/turn_tools.py`.

**Context state** (`frontend/src/contexts/ConversationContext.tsx`): The `showRequestsView` boolean and `setShowRequestsView` setter control whether the Requests pane is displayed.

**App layout** (`frontend/src/App.tsx`): When `showRequestsView` is true, the main content area renders `RequestsView` instead of the chat panel and file browser.

## Design Decisions

**Why a separate `action_requests` table alongside `tool_wait_handles`?**
Action requests need richer per-row state than wait handles do: the `request_type` discriminator drives an executor registry, the `result` column captures handler return values, the `params` schema is per-type, and the cross-conversation Requests pane needs to query and enrich requests independently of any conversation's wait-handle state. Wait handles, in contrast, are a generic synchronization primitive whose payload is opaque to the runtime.

Each action request also creates a linked wait-handle row of `kind="action_request"` that drives the suspend/resume cycle, but the two rows serve different purposes: the `action_requests` row is the persisted business object (visible in the Requests pane, retried on failure, surveyed by status), and the wait handle is the suspend/resume primitive that lets the suspended turn close its dangling tool_use with the verdict on resume. Folding the two would conflate "blocked tool" with "deferred external write" and lose the per-row state.

**Why block by default instead of returning a synchronous `pending` result?**
Every action request carries a Revise button whose feedback would otherwise have nowhere to land -- if the model ended its turn after the call, the user's revise text would never be surfaced to the model unless it had explicitly chosen to call `wait_for_handles` on the returned id. Making the call block guarantees the verdict (and any revise feedback) reaches the model on the same turn it issued the request.

It also collapses the multi-step accept-then-create pattern (create a company, then create a contact bound to the new `company_id`) from `create_action_request` + `wait_for_handles` to a single `create_action_request` call.

The principal awkward case is scheduled routines: a routine that issues an action request now parks inside the call until the user approves it (up to ~14 days, the wait-handle cap). See [Routine Scheduling](scheduling.md) for the routine implication; the `system:action_requests` skill steers routines toward `send_slack_dm_to_self`-style write paths instead.

**Why a handler registry pattern?**
The registry pattern makes it straightforward to add new action request types. Each handler is a self-contained class that knows how to execute its specific action. Adding a new type requires only subclassing `ActionRequestHandler`, implementing `execute()`, and registering the instance -- no changes to the routing or resolution logic.

**Why keep requests open on execution failure?**
Transient failures (API timeouts, rate limits, temporary service outages) should not permanently mark a request as failed. Keeping it open lets the user retry without the agent needing to re-propose the action.

**Why a dedicated Requests pane in addition to inline chat rendering?**
Inline action request rendering works well when the user is already in the conversation where the request was created, but it requires navigating to each conversation individually to find pending requests. The Requests single-pane view aggregates all requests across conversations in one place, making it easy to review and act on pending requests without context switching. The enriched data (routine name, project name) provides additional context that is not available inline.

**Why a separate count endpoint instead of deriving count from the list?**
The sidebar badge fetches `GET /app/api/action-requests/counts` once on mount and then updates from `request_count_changed` events on the persistent WebSocket. A COUNT query is significantly cheaper than fetching and serializing full action request objects, and the dedicated `count_action_requests()` function runs an efficient `SELECT COUNT(*)` query, avoiding unnecessary data transfer and serialization overhead for a simple badge number.

**Why server-side preview rendering instead of client-side?**
Moving preview rendering (including external name resolution) from the frontend to server-side `render_preview()` methods on each `ActionRequestHandler` subclass consolidates type-specific rendering logic in one place. The frontend was simplified from ~160 lines of type-specific rendering to a generic loop over `{key, value}` preview fields.

Adding a new action request type no longer requires frontend changes -- only a new handler with a `render_preview()` method. Server-side rendering also enables richer previews (e.g., resolving upstream entity names) without exposing those APIs to the frontend.

**Why a standalone event emitter instead of React context for badge refresh?**
The `requestEvents.ts` module uses a simple pub/sub pattern outside of React's context system. Putting the refresh signal into React context would cause cascading re-renders across all context consumers (Sidebar, ChatPanel, etc.) whenever the count changes. The standalone event emitter lets the `RequestsBadge` component subscribe independently, keeping re-renders isolated to the badge itself.

The persistent-WS singleton (`persistentWebSocket`, also a non-React singleton) can publish events the same way without needing access to React state. After devplan 00062 the cross-tab path is `persistentWebSocket.onGlobalEvent('request_count_changed')`; the local emitter survives only as a same-tab latency optimisation.
