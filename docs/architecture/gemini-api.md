# Conversation Loop and Tool Integration

This document describes the conversation loop package in `chat/gemini_api/`, which provides the provider-agnostic conversation loop with streaming, tool calling, sub-agent spawning (single and parallel), and in-memory session management. The conversation loop uses the `LLMProvider` interface from the [LLM Provider Abstraction Layer](llm-providers.md) to interact with models from different providers (Gemini, Anthropic) through a unified API.

## Overview

The conversation loop module replaced the original Docker-based gemini-cli approach (since removed entirely) with a direct SDK integration. The module creates persistent chat sessions via the `LLMProvider` interface (see [LLM Provider Abstraction](llm-providers.md)), streams model responses over the persistent multiplexed WebSocket (see [Realtime Architecture](realtime.md)), and dispatches tool calls through a shared `_dispatch_tool_call()` function that handles local tools inline and routes HTTP tools through the internal route dispatch pipeline (see [Route Dispatch Architecture](route-dispatch.md)).

Many tools are dispatched through the `tool_call` meta tool pattern (see [Meta Tool Pattern](#meta-tool-pattern-tool_call)) via `TOOL_CALL_REGISTRY`:

- workspace tools (`list_workspace_files`, `get_workspace_file`, `write_workspace_file`, `edit_workspace_file`)
- memory read tools (`memory_search`, `memory_list`)
- `get_current_time`
- `find_slack_channel`
- `download_drive_file` (Drive binary content downloads to workspace)
- `google_export_doc` / `google_export_sheet` / `google_export_slides` (native Google Doc / Sheet / Slides -> workspace file in any of that kind's export formats via Drive `files.export`, one shared `_export_workspace_file()` routine; see [Docs API](../api/docs-api.md), [Sheets API](../api/sheets-api.md), [Slides API](../api/slides-api.md))
- `archive_gmail_message` (Gmail message archiving with `[Quest]/archived` label)
- the Gmail Simple tools (`get_gmail_messages`, `list_gmail_labels`, `get_gmail_message_urls`, `create_gmail_draft`, `send_gmail_to_self` -- wrapping the `/api/gmail-simple/*` endpoint handlers, which stay registered as HTTP routes for sandboxed scripts; see [Gmail API Documentation](../api/gmail-api.md))
- the Slack read tools (`list_slack_teams`, `list_slack_conversations`, `get_slack_conversation_history`, `get_slack_conversation_replies`, `search_slack_messages`, `get_slack_user_info`, `list_slack_users` -- plugin-registered by the in-tree Slack plugin, wrapping the endpoint functions in `plugins/slack/upstream.py`):
  - unlike Gmail Simple these have **no per-method HTTP routes** (sandbox scripts invoke them through the script tool-call bridge `POST /api/tool-call` -- see [Script Tool-Call Bridge](#script-tool-call-bridge-post-apitool-call)), and the `/api/slack` prefix is in `_BLOCKED_PROXY_PATHS` so cached sessions still emitting `curl_proxy_get` get a use-the-dedicated-tool error
  - server-side team_id resolution, limit capping, and 429 retries stay in the plugin's upstream module, and `ToolResultWithNotices` limit-cap advisories are merged into the tool's JSON result under a `notices` key
- `set_conversation_name` (auto-naming conversations in the sidebar)
- `authed_get` (authenticated external API requests with automatic credential injection, large-response protection, and an `output_file` option that writes the response body into the hidden `.responses/` workspace subdirectory, non-destructively -- see [Authenticated External API Requests](#authenticated-external-api-requests-authed_get))
- `get_response_content` (chunked reading of stored large API responses -- see [Large Response Protection](#large-response-protection))

Memory writes go through `create_action_request(request_type="create_memory", ...)`; see [Action Requests](action-requests.md).

The module also provides skill discovery and loading tools (`list_skills`, `search_skills`, `load_skills`) that let agents browse and retrieve skill definitions from the skill library via `db/skill_store.py`, a `run_script` tool that executes workspace scripts in ephemeral Podman containers and a `run_python` tool that runs inline Python code in the same container without creating workspace files (see [Script Runner Architecture](script-runner.md)).

It also supports sub-agent spawning via the `agent_task` tool (single), the `agent_task_parallel` tool (batched concurrent execution), and the `agent_task_parallel_template` tool (template-based batched concurrent execution from a shared prompt template with per-agent variables, restricted to cheaper models), allowing the top-level model to delegate independent tasks to ephemeral sub-agents that share the same workspace and API access.

The module also implements a generic durable wait-handle mechanism (`tool_wait_handles` table + `wait_for_handles` tool) that lets the model block on tools awaiting human input across server restarts. It currently backs `send_slack_reply_and_get_response` (Slack thread suspend; always blocking via `SuspendForSlackReply`) and `create_action_request` (inline Approve / Revise / Deny card; always blocking via `SuspendForActionRequest`). See [Wait Handles Architecture](wait-handles.md).

The module wraps every user message in an XML envelope containing the current UTC time and the user's local time (derived from the IANA timezone string sent by the frontend) before passing it to the LLM, giving the model accurate time context on every turn. The module also tracks cached token usage via the provider's `get_usage()` method, recording how many input tokens were served from cache.

The persistent-WS event protocol includes `text` (streamed as `text_delta` envelopes on the wire), `tool_use`, `tool_result`, `sub_agent_tool_use`, `sub_agent_tool_result`, `sub_agent_finished`, `stats`, `action_request`, and `conversation_updated` event types. See [Realtime Architecture](realtime.md).

The chat route wiring connects this module to the persistent multiplexed WebSocket handler in `chat/realtime/socket.py` (specifically `_handle_send_message` and the `_run_send_message` task body).

`_run_send_message` wraps `run_conversation_turn()` in a cancellable asyncio task tracked in the per-conversation send registry (`_active_send_runs`), provides disconnect resilience (the run continues if the client disconnects -- only an explicit `stop` op cancels), supports partial message recovery via the same `_save_interrupted_sdk_history()` tail edit the deleted per-turn handler used, and preserves interrupted messages in SDK history so the model can see what it generated before being stopped. See [Realtime Architecture](realtime.md).

## Tool Declarations

Tool declarations are defined in a provider-agnostic JSON Schema format in `chat/llm/tool_schemas.py` and organized into three tiers. See that file directly for parameter schemas. See [LLM Provider Abstraction](llm-providers.md) for converter functions that translate these to each provider's native format.

**Tier 1: `BASE_TOOLS`** -- 12 tools shared by all tiers (top-level, sub-agent, nested sub-agent, Slack): `curl_proxy_get`, `curl_proxy_post`, `tool_call` (meta tool for dynamic dispatch), `load_gmail_attachment`, `run_script`, `run_python`, `list_skills`, `search_skills`, `load_skills`, `list_my_skills`, `get_skill`, `list_routines`.

`list_my_skills` / `get_skill` are read-only inspection of the user's own DB-backed skill setup (categorized listing + single-skill detail with autoload status); see [Skill Library -- Skill Inspection Tools](skill-library.md#skill-inspection-tools). The matching skill **write** path (create / edit, incl. visibility, sharing, and project auto-load) rides on `create_action_request` via the `create_skill` / `edit_skill` request types, not a dedicated tool; see [Skill Library -- Creating and Editing Skills via Action Requests](skill-library.md#creating-and-editing-skills-via-action-requests).

`list_routines` is the analogous read-only inspection of the current project's routines (name, prompt, model, schedule, auto-loaded skills; structured error outside project conversations), whose write path rides on `create_action_request(request_type="edit_routine")`; see [Routines Architecture -- Agent Tools](routines.md#agent-tools).

**Dynamic tools (dispatched via `tool_call`):** 32 core tools defined in `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py`, invoked by the LLM through `tool_call(tool_name="...", arguments={...})`: `get_current_time`, `list_workspace_files`, `get_workspace_file`, `write_workspace_file`, `edit_workspace_file`, `memory_search`, `memory_list`, `wait_for_handles`, `authed_get`, `authed_post`, `get_response_content`, `download_drive_file`, `google_export_doc`, `archive_gmail_message`, `list_gmail_quest_labels`, `modify_gmail_labels`, `get_gmail_messages`, `list_gmail_labels`, `get_gmail_message_urls`, `create_gmail_draft`, `send_gmail_to_self`, `set_conversation_name`, `project_db_query`, and the seven Quest Docs tools (`list_docs`, `search_docs`, `read_doc`, `create_doc`, `edit_doc`, `append_to_doc`, `add_doc_image`). The doc tools are gated on the `docs` connected-services pseudo-key and hidden from the read-only tiers' prompts; see [Quest Docs](quest-docs.md#model-facing-tools).

See [Gmail API Documentation](../api/gmail-api.md) for the Gmail Simple tools (which wrap the `/api/gmail-simple/*` endpoint handlers kept registered for sandboxed scripts) and the Quest-managed label tools.

Loaded plugins register additional dynamic tools at startup:

- the Telegram plugin's four `requires_service: "telegram"` read tools (`telegram_get_me`, `telegram_list_dialogs`, `telegram_get_messages`, `telegram_list_contacts` -- see [Telegram plugin doc](../../plugins/telegram/docs/telegram-api.md); they replaced the `/api/telegram/*` proxy routes and the prefix is blocked in route dispatch)
- the Slack plugin's nine grandfathered-name tools (`find_slack_channel`, `list_slack_teams`, ..., `send_slack_dm_to_self` -- see [Slack plugin doc](../../plugins/slack/docs/slack-api.md); no per-method HTTP routes, sandbox scripts reach them through the script tool-call bridge `POST /api/tool-call` below, and the `/api/slack` prefix is blocked in route dispatch so cached sessions get a pointer to the dedicated tools)
- the GitHub plugin's `github_get_job_log`
- and more -- see [Plugins](plugins.md)

See [Project Database Architecture](project-db.md) for `project_db_query` details. See [Large Response Protection](#large-response-protection) for `get_response_content` details. See [Wait Handles Architecture](wait-handles.md) for `wait_for_handles` details. Memory writes use `create_action_request(request_type="create_memory", ...)` rather than a dynamic tool.

**Tier 2: `TOP_LEVEL_TOOLS`** -- 15 tools for top-level agents: `BASE_TOOLS` (11) + `agent_task` (single sub-agent), `agent_task_parallel` (batched concurrent sub-agents, max 10 tasks), `agent_task_parallel_template` (template-based batched concurrent sub-agents, max 20 tasks, restricted to cheaper models), `create_action_request` (propose write operations requiring user approval). The `agent_task` tool description directs the model to prefer `agent_task_parallel` for multiple independent tasks.

**Tier 3: `SUB_AGENT_TOOLS`** -- 12 tools for sub-agents: `BASE_TOOLS` (11) + `agent_task_response` (return results to parent). Sub-agents cannot spawn sub-agents, create action requests, set conversation names, or call `wait_for_handles`. A sub-agent that needs an approval-gated write returns the proposed `request_type` + `params` + `reasoning` to the parent via `agent_task_response`; the parent issues `create_action_request` on its behalf. See [Action Requests Architecture](action-requests.md) for the escape-hatch flow.

**Nested tier: `SUB_AGENT_TOOLS_NESTED`** -- `SUB_AGENT_TOOLS` (12) + `agent_task_nested` (13 tools). Given only to a **1st-level** sub-agent when the conversation has the `nested_subagents` flag set (see [Conversation Flags](conversation-flags.md)). `agent_task_nested` spawns a single 2nd-level sub-agent restricted to `claude-haiku-4.5` / `gemini-3.5-flash-lite` and with no further spawner. A 2nd-level sub-agent always gets plain `SUB_AGENT_TOOLS` -- there is no 3rd level. See [Nested Sub-Agents](#nested-sub-agents).

**Slack tier: `SLACK_TOP_LEVEL_TOOLS`** -- `TOP_LEVEL_TOOLS` plus `send_slack_reply_and_get_response`, used only when `run_conversation_turn()` is called with `origin="slack"`. The extra tool posts a threaded reply to Slack and blocks on an `asyncio.Future` until the user replies in the same thread (debounced). Sub-agents do not see it. See [Slack Socket Mode](slack-socket-mode.md) for the full flow.

**Routine tiers: `ROUTINE_TOP_LEVEL_TOOLS` / `PUBLIC_ROUTINE_TOOLS`** -- `TOP_LEVEL_TOOLS` (or `PUBLIC_TOOLS` in a public project) plus `routine_completed`, used whenever `run_conversation_turn()` is called with a `routine_id` (every turn of a routine conversation carries it). `routine_completed` is a pure completion signal handled by `_handle_routine_completed()` in `chat/gemini_api/turn_tools.py`: it acknowledges the call and the run drivers in `chat/routine_runs.py` look for the persisted tool_use to decide whether the routine run finished or needs a follow-up nudge turn. It is the one loop-handled arm not in `_PUBLIC_BLOCKED_LOOP_TOOLS`. See [Routines -- Run Completion](routines.md#run-completion).

The `set_conversation_name` tool is in `TOOL_CALL_REGISTRY` but excluded from sub-agent visibility via the `exclude` parameter on `_build_dynamic_tools_section()` in `chat/gemini_api/system_prompt.py`.

The `create_action_request` tool proposes write operations to external services that require user approval. The dispatch arm first calls `handler.validate_params(params)`; the per-handler validator rejects any unknown top-level or nested-row key up front via the shared helper in `chat/action_request_types/_param_validation.py`, raising `ValueError`. The `ValueError` arm in `_handle_create_action_request()` (`chat/gemini_api/turn_tools.py`) returns `{"error": "Invalid parameters: ..."}` as a synchronous tool result -- no `action_requests` row, no wait handle, no card, no `SuspendForActionRequest` -- so the model self-corrects on the same turn.

On valid params, the dispatch arm persists an `action_requests` row, inserts a linked `kind="action_request"` row in `tool_wait_handles`, emits the `action_request` event (with `preview_fields`, `approve_label`, and `wait_handle_id` generated by `get_preview_for_request()` from `chat/action_request_types/registry.py`), and publishes `request_count_changed` on the user channel before raising `SuspendForActionRequest` to unwind the agent loop.

The call blocks until the user clicks Approve / Revise / Stop on the inline card. On Approve or Revise, the resume bucket closes the dangling `create_action_request` tool_use with the wait-handle row's `response` so the model reads the verdict directly off its tool result; on a Revise the response carries `verdict: "denied"` plus a top-level `feedback` field (mirrored inside `result.feedback`) so the model can apologise, propose an alternative, or fix and re-issue. On Stop nothing resumes: the request is discarded, the conversation halts, and the tool_use is closed with `verdict: "stopped"` only when the user's next message arrives (see [Action Requests -- Stop](action-requests.md#stop)).

The tool is hard-disabled in Slack-driven runs (the dispatch arm short-circuits when `is_slack_origin` is true; the prompt-level `top_level_exclude` set in `chat/gemini_api/system_prompt.py` also drops it from the dynamic-tools enumeration). See [Action Requests Architecture](action-requests.md) for details.

Limit constants (`MAX_PARALLEL_TASKS`, `MAX_PARALLEL_TEMPLATE_TASKS`, `MAX_AGENT_TASKS_PER_TURN`, `MAX_SUB_AGENT_TURNS`, `SUB_AGENT_TURN_WARNING_THRESHOLD`) and the `TEMPLATE_BATCH_ALLOWED_MODELS` set are defined in `chat/gemini_api/constants.py`. The `get_sub_agent_turn_limits(model)` function in the same file returns model-specific limits.

The two `curl_proxy_*` tools enforce that URLs must target `localhost/api/*` on an allowed port (`8000` in production; both `8000` and `9000` in dev mode, since the dev server runs on port 9000) and that the `Authorization` header must NOT be set by the model (it is injected automatically by route dispatch). The allowed port set is built dynamically using `get_proxy_port()` from `chat/gemini_api/constants.py`. These tool names map directly to the `_TOOL_METHOD_MAP` in `chat/route_dispatch.py`, which translates them to HTTP methods for internal dispatch.

The `get_current_time` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_get_current_time()` in `chat/gemini_api/tool_handlers/misc.py` without HTTP routing.

It returns JSON containing `unix_timestamp`, `utc` (iso8601 + readable), and `user_timezone` (iso8601 + readable + utc_offset). The user's timezone string is already available as a parameter to `run_conversation_turn()` (passed from the persistent-WS `send_message` payload). If the timezone is invalid or unrecognized, the response falls back to UTC-only with an error note. No external dependencies are needed -- it uses stdlib `datetime`, `time`, and `zoneinfo`.

The `list_workspace_files` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_list_workspace_files()` in `chat/gemini_api/tool_handlers/workspace.py`. It returns a JSON object containing `file_count` and a `files` array. Each entry has `path` (relative to workspace root), `size_bytes`, and `modified` (ISO 8601 UTC).

The workspace directory is resolved via `_get_workspace_dir()`, which returns `ChatStorage.get_conversation_workspace_root(conversation_id)` in `chat/storage.py` (`data/chats/{conversation_id}/workspace/`) and creates it if it does not exist. This is the conversation's own workspace for standalone and project conversations alike; the `project_id` argument is ignored, and the shared project workspace is not reachable through the workspace tools. Files are enumerated with `Path.rglob("*")`, so nested directories are included.

The `get_workspace_file` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_get_workspace_file()` in `chat/gemini_api/tool_handlers/workspace.py`. It retrieves a single file from the workspace with two delivery modes:

- **Small text files** (recognized text extension or `text/*` MIME type, and size <= 100KB as defined by `_TEXT_INLINE_LIMIT`): file contents are returned inline in the tool response JSON (`path`, `size_bytes`, `content`). Text file detection uses `_is_text_file()`, which checks the extension against `_TEXT_EXTENSIONS` (a set of ~50 common text/code extensions in `chat/gemini_api/constants.py`) and falls back to `mimetypes.guess_type()` for `text/*` MIME types.
- **Binary or large files**: before uploading, the handler pre-checks the file's MIME type against `_UNSUPPORTED_GEMINI_MIME_TYPES` (a deny-list in `chat/gemini_api/constants.py` covering Office/OpenXML, OpenDocument, archives, and executables). If the MIME type is on the deny-list, the handler returns a structured error JSON with a `suggestion` field directing the model to use `run_script` or `run_python` with python-docx or openpyxl instead -- this avoids a wasted upload and a conversation-killing 400 error.
  - The handler then runs a pre-flight size check before any upload or inline attempt: the file size is compared against the per-model attachment cap resolved by `get_attach_limit_for_model()` in `chat/llm/file_limits.py`, keyed off the model actually being called (conversation or sub-agent model).
  - Oversized files return a structured error JSON (`error`, `path`, `size_bytes`, `limit_bytes`, `mime_type`, `model`, `backend`, `suggestion`) with a MIME-type-specific suggestion built by `_oversize_suggestion()` (split PDFs into page chunks with the preinstalled `pypdf` via `run_python`/`run_script`, downscale images with Pillow, slice/aggregate text files) instead of letting the provider API request fail.
  - The check covers both the binary upload path and the large-text inline fallback, since both attach the bytes to the request. The `get_workspace_file` tool schema description (`chat/llm/tool_schemas.py`), the system prompt (`chat/gemini_api/system_prompt.py`), and the `system:workspace` skill (`chat/system_skills/catalog.py`) all note the limit so the model plans ahead for big files.
  - For supported, within-limit files, the file is uploaded via the provider's `upload_file()` method (Gemini inlines images and PDFs as `types.Part.from_bytes()` parts on Vertex; Anthropic base64-encodes images and PDFs as inline content blocks -- per-backend size caps are centralized in `chat/llm/file_limits.py`, see [LLM Provider Abstraction -- Attachment Size Limits](llm-providers.md#attachment-size-limits)). The tool response includes the file reference in `extra_parts` so the model can analyze the file natively.

The `get_workspace_file` handler validates paths to prevent directory traversal (rejects `..` components, verifies resolved path is within the workspace directory). It returns `(result_json, extra_parts)` where `extra_parts` is a list of provider-specific content parts appended after the function response. The `get_workspace_file` entry in `TOOL_CALL_REGISTRY` uses a generic description; the file delivery mechanism differs by provider but is handled transparently by the `upload_file()` method.

The `load_gmail_attachment` tool is a local tool -- it executes inline via `_handle_load_gmail_attachment()` in `chat/gemini_api/tool_handlers/workspace.py`. It fetches a Gmail message attachment and makes it available for the model to analyze directly in context (e.g., read a PDF, inspect an image). The model obtains the `message_id` and `attachment_id` from the `## Attachments` section of a `get_gmail_messages` response (the `attachmentId` value).

The handler calls `_resolve_gmail_attachment()` from `api/gmail/draft_endpoints.py` to fetch the raw attachment bytes using the user's Google Services OAuth credentials, writes the bytes to a temporary file, and uploads via the provider's `upload_file()` method (Gemini inlines images and PDFs as `types.Part.from_bytes()` parts on Vertex; Anthropic base64-encodes supported images and PDFs as inline content blocks).

Before uploading, the handler pre-checks the attachment's MIME type against `_UNSUPPORTED_GEMINI_MIME_TYPES` (the same deny-list used by `get_workspace_file`). If the MIME type is on the deny-list, the handler returns a structured error JSON with a `suggestion` field directing the model to use `run_script` or `run_python` with python-docx or openpyxl instead.

It also runs the same pre-flight size check as `get_workspace_file` against the per-model attachment cap from `get_attach_limit_for_model()` in `chat/llm/file_limits.py` -- Gmail attachments can reach ~25MB raw, above e.g. the Anthropic-on-Vertex effective cap -- returning a structured error JSON (`error`, `filename`, `size_bytes`, `limit_bytes`, `mime_type`, `model`, `backend`, `suggestion`) instead of letting the provider request fail.

On success, the file reference is included in `extra_parts` alongside the function response so the model can analyze the file natively. The temporary file is always deleted in the `finally` block regardless of success or failure. The `filename` and `mime_type` parameters default to `"attachment"` and `"application/octet-stream"` respectively if not provided. The `account` parameter is accepted for forward-compatibility but is currently ignored -- the authenticated user's Google Services credentials are always used.

If the user has not connected Google Services, a descriptive error is returned in the JSON result. If the upload fails, the error response includes `api_error_type`, `api_error_detail`, and a `suggestion` field. This tool follows the same upload and MIME type pre-check pattern as `get_workspace_file` and carries `provider_descriptions` in its `ToolSpec` for provider-accurate tool descriptions. It requires Google Services to be connected (uses `_resolve_gmail_attachment()` in `api/gmail/draft_endpoints.py` which requires `google_services_oauth`). See [Gmail API documentation](../api/gmail-api.md) for the `_resolve_gmail_attachment()` helper and attachment metadata fields.

The `write_workspace_file` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_write_workspace_file()` in `chat/gemini_api/tool_handlers/workspace.py`. It writes or creates a text file in the conversation workspace.

The handler validates paths to prevent directory traversal (rejects `..` components, verifies the resolved path is within the workspace directory), enforces a 1MB file size limit on the `content` parameter, creates parent directories as needed, and writes the content as UTF-8 text. If a file already exists at the given path, it is silently overwritten. The workspace directory is resolved via `_get_workspace_dir()`.

On a successful write the handler publishes a per-user `file_list_changed` global on the realtime bus (via `_publish_file_list_changed`) so any open file browsers silent-refresh mid-turn; failed writes do not emit. See [Realtime -- Backend publish sites](realtime.md#backend-publish-sites-per-user-globals).

The `edit_workspace_file` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_edit_workspace_file()` in `chat/gemini_api/tool_handlers/workspace.py`. It performs an exact string replacement in an existing workspace text file: `old_string` must match the file contents exactly and appear exactly once, unless `replace_all` is set, in which case every occurrence is replaced. Unlike `write_workspace_file`, it never creates files or parent directories.

The handler applies the same path-traversal validation and 1MB size cap as `write_workspace_file` (both the existing file and the post-replacement content), and rejects files that are not valid UTF-8 (a strict decode -- editing with `errors="replace"` would corrupt binary files on write-back; the error suggests `run_python` instead).

Edits are gated by a read-before-edit check: the conversation must have previously seen the file's contents via `get_workspace_file`, `write_workspace_file`, or an earlier successful edit. Seen paths are recorded in a per-conversation `workspace_reads.json` sidecar in the conversation directory (`data/chats/{conversation_id}/workspace_reads.json`, even for project conversations whose workspace is project-shared) via `ChatStorage.add_workspace_read_paths()` / `get_workspace_read_paths()` in `chat/storage.py` (recording is best-effort via `_mark_workspace_file_read()` and never breaks the read/write that triggered it).

Because the sidecar is on disk and keyed by conversation, the gate survives server restarts and covers sub-agents, which share the conversation's workspace. The sidecar's read-modify-write needs no lock: parallel sub-agents are coroutines on the single event loop, so the synchronous helper cannot be interleaved (see the concurrency note on `add_workspace_read_paths()`).

Mismatched or ambiguous `old_string` values return structured errors directing the model to re-read the file or add context / pass `replace_all`. On a successful edit the handler publishes `file_list_changed` like `write_workspace_file`. There are no frontend changes -- dynamic tools render generically in the chat transcript.

The `memory_search` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_memory_search()` in `chat/gemini_api/tool_handlers/memory.py`. It wraps `search_memories()` from `db/memory_store.py` to perform FTS5 full-text search against the user's saved memories.

The search query supports simple keywords, phrases (`"exact phrase"`), prefix matching (`meet*`), and boolean operators (`AND`, `OR`, `NOT`). It returns a JSON object containing `match_count` and a `memories` array. Each entry has `id`, `content`, `created_at`, and `updated_at` (nullable). Search errors are caught and returned as JSON error strings.

The `memory_list` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below). It executes inline via `_handle_memory_list()` in `chat/gemini_api/tool_handlers/memory.py`. It wraps `list_memories()` from `db/memory_store.py` to return all active (non-archived) memories for the user, ordered by creation time (newest first). It returns a JSON object containing `memory_count` and a `memories` array with the same fields as `memory_search`.

Both memory tools give agents read-only access to the user's memories. Memories are personal notes and facts saved by the user through the Settings panel or from previous conversations. The system prompt instructs agents to search memories when the user refers to something they should already know, and to list memories when the user asks what the agent knows about them.

The `find_slack_channel` tool is a dynamic tool dispatched via the `tool_call` meta tool (see [Meta Tool Pattern](#meta-tool-pattern-tool_call) below), registered by the Slack plugin. It executes inline via `_tool_find_slack_channel()` in `plugins/slack/tools.py`. It searches for Slack channels by name using a case-insensitive substring match. The tool takes a `search` string parameter and returns a JSON object containing `match_count` and a `channels` array. Each channel entry has `channel_id`, `channel_name`, `team_id`, and `team_name`.

The tool uses `auth.teams.list` (shared bot token via the loader in `auth/config.py`) to resolve Enterprise org IDs (E-prefix) to workspace team IDs (T-prefix), since org-level Slack installs store an Enterprise ID in `default_team_id`. It then calls `conversations.list` (user token) with the resolved workspace team IDs, filtering to `public_channel` and `private_channel` types (excluding archived channels). If `auth.teams.list` fails or no bot token is configured, the tool falls back to the user's `default_team_id`.

The tool paginates through channels (up to 10 pages of 200 channels per workspace) and searches across all workspaces. No new OAuth scopes are needed -- it uses the existing `channels:read` and `groups:read` user token scopes. The system prompt instructs the model to use `find_slack_channel(search)` to look up channel IDs before sending Slack messages via `create_action_request`.

The `run_script` tool executes workspace scripts (Python, Bash) in ephemeral Podman containers. It is dispatched via `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` and handled by `_handle_run_script()` in `chat/gemini_api/tool_handlers/sandbox.py`. The tool returns a JSON object containing `stdout`, `stderr`, and `return_code`. See [Script Runner Architecture](script-runner.md) for the container image, pre-installed tools, parameters, networking, security model, and design decisions.

The `run_python` tool runs inline Python code in the same Podman container as `run_script`, but pipes the script via stdin (`python3 -u -`) instead of reading from a workspace file. It is dispatched via `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` and handled by `_handle_run_python()` in `chat/gemini_api/tool_handlers/sandbox.py`.

The tool takes a required `script` parameter (the inline Python code), optional `args` (appended to the command line), and optional `timeout` (default 120s, max 300s). The container, networking, mounts, and security are identical to `run_script`. The guidance in the system prompt directs the model to use `run_python` for one-off tasks (quick calculations, data transformations) and `write_workspace_file` + `run_script` for reusable scripts. See [Script Runner Architecture](script-runner.md) for details.

## Meta Tool Pattern (`tool_call`)

The meta tool pattern consolidates multiple tool declarations into a single `tool_call` tool registered with the LLM. Instead of each tool consuming a slot in the LLM's function declaration list (which counts against context and per-request limits), tools can be defined in `TOOL_CALL_REGISTRY` and documented in the system prompt. The LLM invokes them via `tool_call(tool_name="<name>", arguments={...})`.

### Motivation

Each tool registered with the LLM as a `FunctionDeclaration` consumes context tokens for its schema (name, description, parameter definitions). As the tool count grows, this overhead becomes significant. The meta tool pattern moves tool documentation from the function declaration schema into the system prompt, where it is more compact and can be cached by prompt caching. This reduces the per-tool context overhead while keeping the same dispatch and validation guarantees.

### How It Works

1. **Registration**: `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py` maps tool names to their full `ToolSpec` definitions. This dict is the single source of truth -- it drives the system prompt documentation, the dispatch validation, and the available tool list
2. **System prompt**: `_build_dynamic_tools_section()` in `chat/gemini_api/system_prompt.py` iterates `TOOL_CALL_REGISTRY` and formats each tool's name, description, and parameters into a human-readable "Dynamic Tools" section. This section is included in both the top-level and sub-agent system prompts
3. **LLM invocation**: The LLM calls `tool_call(tool_name="get_current_time", arguments={})` instead of calling `get_current_time()` directly
4. **Dispatch**: The `tool_call` branch in `_dispatch_tool_call()` extracts `tool_name` and `arguments`, validates `tool_name` against `TOOL_CALL_REGISTRY`, pops `intent_message` from inner args, and delegates to the appropriate handler. Invalid tool names or malformed arguments return structured JSON error messages
5. **Frontend display**: `ToolUseMessage.tsx` checks if `tool_name === 'tool_call'` and resolves the inner tool name from `tool_input.tool_name` for display, so the user sees "get_current_time" instead of "tool_call"

### Backward Compatibility

The direct-name dispatch paths for the converted tools (`get_current_time`, `list_workspace_files`, `get_workspace_file`, `write_workspace_file`, `memory_search`, `memory_list`) are preserved in `_dispatch_tool_call()` (as elif branches after the `tool_call` branch). This ensures that cached LLM sessions -- which still have the old individual tool declarations in their SDK history -- continue to work without errors.

New sessions use `tool_call` because the current `BASE_TOOL_DECLARATIONS` no longer includes the individual declarations. Retired tools have no fallback arm: a cached session that still emits one falls through to the `tool_call` "unknown tool" branch, and the model self-corrects on the next turn from the structured error.

### Currently Converted Tools

All dynamic tools in `TOOL_CALL_REGISTRY` have been converted to the meta tool pattern. Backward compatibility dispatch paths for all converted tools are preserved in `_dispatch_tool_call()` for cached LLM sessions. The system prompt documentation for dynamic tools is auto-generated from `TOOL_CALL_REGISTRY` -- no manual prompt editing needed.

## Message Metadata Wrapping

Every user message sent to the LLM is wrapped in an XML envelope before being passed to `chat.send_message_stream()`. The raw original message is stored in chat history unchanged; only the LLM sees the wrapped form.

**Implementation:** `_wrap_message_with_metadata(message, user_timezone, loaded_skills_content="", attached_filenames=None)` in `chat/gemini_api/conversation.py`, called at the top of the main conversation loop in `run_conversation_turn()` before each turn's first `send_message_stream()` call.

**Envelope format:**

```
<message_metadata>
UTC Time: <weekday, Month DD, YYYY HH:MM:SS UTC>
User Local Time: <weekday, Month DD, YYYY HH:MM:SS AM/PM TZ> (<IANA timezone>)
Files attached to this message ...: <name1>, <name2>   (only when filenames are present)
</message_metadata>

<conversation_skills_loaded>        (only present when skills are loaded)
The user has loaded the following skills into this conversation. These skills
apply to the ENTIRE conversation from this point forward, not just this message.
Treat them as persistent instructions for all subsequent messages.

### Skill Name
Skill content...
</conversation_skills_loaded>

<message>
<the user's actual message text>
</message>
```

**Conversation skills injection:** When the user loads skills into a conversation via the Skill Selector Modal, the `loaded_skills_content` parameter is populated with the resolved skill content (built as `### {name}\n{content}` blocks). The `<conversation_skills_loaded>` XML section is injected between `<message_metadata>` and `<message>` in the envelope. This section only appears on messages where skills are first loaded; however, the system prompt instructs the LLM to treat the loaded skills as persistent instructions that apply to all subsequent messages in the conversation. See [Skill Library Architecture](skill-library.md) for the full conversation skill loader feature.

**Attached-files line:** When the composer attaches generic workspace files to a message (via the "Attach" button -- see [Frontend -- Composer Component](frontend.md#composer-component)), the just-uploaded workspace-relative filenames arrive on `_wrap_message_with_metadata` via `attached_filenames` (threaded FE -> `send_message` WS frame's `attached_filenames` -> `run_conversation_turn` -> here).

A single "Files attached to this message ..." line is appended inside the same `<message_metadata>` block, pointing the model at the workspace files it can read with its file tools. It is emitted ONLY when filenames are present (non-empty strings, after filtering) and ONLY on that one triggering turn -- there is no per-turn workspace enumeration; later turns rely on the model's normal file tools. The files themselves are uploaded out-of-band to the workspace before the send (no inline file parts in the turn).

**Timezone resolution:** The IANA timezone string comes from the `timezone` parameter passed through `run_conversation_turn()` (ultimately sourced from the persistent-WS `send_message` payload). If the timezone string is absent or not recognized by `zoneinfo.available_timezones()`, the local time line reads `User Local Time: (unknown timezone)` and UTC is still included.

**System prompt integration:** Both `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py` include a "Message format" section that explains the XML envelope to the LLM, including the optional `<conversation_skills_loaded>` section. The system prompt instructs the model to use the embedded time information for time-sensitive questions without calling `get_current_time()` for simple cases, to treat conversation skills as persistent instructions, and not to repeat or quote the XML tags back to the user.

**Design decision:** Injecting time context at the message level means the model always has an accurate timestamp for every message without consuming a tool call. This is especially useful for queries like "what time is it?" or date-relative calculations. The `get_current_time()` tool remains available for cases requiring precise time information within a multi-step task.

## Wait-Handle Suspend / Resume

Tools that block on the user (`wait_for_handles`, the Slack-only `send_slack_reply_and_get_response`, and `create_action_request`'s inline Approve / Revise / Stop card) share one durable mechanism: insert a `tool_wait_handles` row whose `tool_id` is the registering call's LLM tool_use id, then raise a sentinel exception that unwinds the model loop without emitting a `tool_result`. The dangling `tool_use` left on disk is closed on the next run by the resume bucket below. See [Wait Handles Architecture](wait-handles.md) for the full mechanism (data model, REST endpoints, headless resume, design rationale). The notes below cover the conversation-loop integration only.

### Suspend Sentinels

Three exception classes in `chat/gemini_api/turn_tools.py` (the per-tool handler module for the loop-handled arms; `conversation.py` re-imports them for its catch clauses) mark a clean suspend:

- `SuspendForWaitHandles` -- raised by `_await_wait_for_handles()` when at least one of the requested handles is still pending. Validates `handle_ids` (1--8 strings), `reason` (required, truncated at 200 chars), and `timeout_seconds` (defaults to `_WAIT_FOR_HANDLES_MAX_TIMEOUT` -- currently 1,209,600 s / ~14 days -- and is clamped to [1, that maximum] when set). Before raising, the arm schedules a per-handle expiry task via `chat/wait_handles/wait_timer.py` so a still-pending row gets flipped to `timed_out` on deadline.
- `SuspendForSlackReply` -- raised by the `send_slack_reply_and_get_response` arm after posting the reply via `slack_driven_runtime.post_thread_reply()` and inserting a `kind="slack_reply"` row keyed implicitly by `(channel, thread_ts)` via the row payload. See [Slack Socket Mode](slack-socket-mode.md).
- `SuspendForActionRequest` -- raised by the `create_action_request` arm after persisting the `action_requests` row, inserting the linked `kind="action_request"` wait-handle row, and emitting the `action_request` event. The resume bucket closes the dangling `create_action_request` tool_use with the wait-handle row's `response` (`{verdict, request_id, feedback?, result}`) on resolve. See [Action Requests](action-requests.md).

All three inherit from `Exception` (not `BaseException`) so they never mask `CancelledError`. The top-level `run_conversation_turn()` catches each sentinel, logs an info line, and returns the `structured_messages` list cleanly -- this is a successful suspend, not a turn-end, so no `error` or `stats` event is emitted. By the time the sentinel is raised, the boundary save in `_capturing_on_event` has already persisted `sdk_history.json` ending on the dangling tool_use (see [SDK History Persistence](#sdk-history-persistence)).

`wait_for_handles` is excluded from sub-agents (no UI channel) and short-circuits with a structured error in Slack-driven runs (Slack uses `send_slack_reply_and_get_response` instead -- see [Slack Socket Mode](slack-socket-mode.md)).

### Resume Bucket Logic

At the top of `run_conversation_turn()`, `provider.get_pending_tool_use_args()` returns the `(tool_id, tool_name, args)` tuples on the last assistant turn of the loaded session. For each tool_id, the loop looks up the matching `tool_wait_handles` row via `get_handle_by_tool_id()` and closes the dangling tool_use accordingly:

- `kind="slack_reply"` row present -- closed with `{"user_reply": ..., "posted_ts": ...}` from the row's `response` and `payload`. A row still in `pending` (resume kicked before the debounce flush completed) is closed with a `{"status": "still_waiting"}` marker so the model can re-issue the tool.
- `kind="action_request"` row present -- closed with the row's `response` dict verbatim (`{"verdict": "executed", "request_id": N, "result": {...}}` for Approve, `{"verdict": "denied", "request_id": N, "feedback": "...", "result": {"denied": true, "feedback": "..."}}` for Revise, or `{"verdict": "stopped", ...}` plus an explanatory `note` for a Stop that the user's next message is now closing). Matched before the `wait_for_handles` catch-all so the row never funnels through `_build_wait_for_handles_result`. A row still in `pending` (resume kicked before the resolve endpoint finished) is closed with a `{"status": "still_waiting"}` marker.
- Any `wait_for_handles` / `tool_call(tool_name="wait_for_handles")` tool_use, with or without a row -- closed with `_build_wait_for_handles_result(user_id, handle_ids)`, a fresh DB read of every handle id the original call was waiting on. The resume path applies the same id cap as the live dispatch arm.
- Any other dangling tool_use (cancellation / restart mid-tool) -- closed with the synthetic `{"status": "interrupted", ...}` marker.

When no dangling tool_use is present (e.g., the suspended arm completed before the process died, or the user pressed Accept on a card whose model loop already moved past it), the user's wrapped message becomes the next turn as usual. When the resume runs with an empty `message` (i.e., headless continuation kicked by `chat/wait_handles/resume.py`) and there is no dangling tool_use, the loop bails before invoking the model so it does not run a turn against empty user input.

### Frontend Card

`ActionRequestMessage` in `frontend/src/components/ActionRequestMessage.tsx` renders the Approve / Revise / Stop UI for every `create_action_request` invocation, including the `create_memory` request type that replaced the old confirm-action card (devplan 00065). The component reads the linked `tool_wait_handles` row's status to render the collapsed state across reloads even before the (possibly headless-resumed) model loop emits a `tool_result`. See [Action Requests](action-requests.md).

`ToolUseMessage.tsx` renders `Waiting for: <reason>` in place of the raw tool name when the underlying tool is `wait_for_handles` (or `tool_call(tool_name="wait_for_handles", ...)`), so the chat reads naturally to the end user.

### Stop on the Persistent WS Control Plane

The `{op: "stop", conversation_id}` op on the persistent WS (`chat/realtime/socket.py:_handle_stop`) cancels the in-flight task for that conversation (if any) and **unconditionally** calls `cancel_pending_wait_handles_for_conversation()`. Without the unconditional cancel, a stop click on a sentinel-suspended run (where the model task already returned cleanly via `SuspendForWaitHandles` / `SuspendForSlackReply` / `SuspendForActionRequest`) would leave rows pending with nobody to resolve them. The server replies `{type: "stop_acknowledged", conversation_id}` so the UI can clear its "thinking" indicator immediately.

`_run_send_message` catches `CancelledError`, calls `_save_interrupted_sdk_history()` for the on-disk tail edit, calls `remove_chat_session()`, and calls `cancel_pending_wait_handles_for_conversation()` -- mirroring the cancel-time cleanup the deleted `_handle_api_mode()` did. The legacy `confirm_action_result` WebSocket bridge in `chat/routes/_helpers.py` was removed; clients resolve wait handles via REST (`POST /app/api/wait-handles/{id}/resolve`, `POST /app/api/action-requests/{id}/resolve`).

All base tools support the `intent_message` parameter, which captures a brief, user-friendly summary of the model's intent (max 50 characters, e.g. "Fetch unread emails", "Check current time"). It is **required** on `tool_call`, `curl_proxy_get`, and `curl_proxy_post`; optional on the remaining base tools. It is extracted from the tool call args via `args.pop("intent_message", "")` before tool execution so it never reaches the handler. The backend truncates it to 50 characters as a safety measure.

The intent message flows through the persistent-WS `tool_use` events and is displayed in the frontend as the primary label of the tool message, with the raw tool name shown as the secondary label. When no intent message is provided, the tool name is shown as the primary label instead. For `tool_call` invocations, `frontend/src/components/ToolUseMessage.tsx` resolves the inner tool name from `tool_input.tool_name` for display instead of showing "tool_call".

The canonical tool declarations use the `ToolSpec` TypedDict from `chat/llm/base.py` in JSON Schema format. At session creation time, these are converted to provider-native formats via `to_gemini_declarations()` or `to_anthropic_tools()` in `chat/llm/tool_schemas.py`.

## Error Surfacing

A run-fatal exception must reach the user durably, not just as a transient WS event -- otherwise a conversation that dies mid-turn looks like it simply stopped. Three layers cooperate:

1. **Durable error message.** The top-level `except Exception` in `run_conversation_turn()` appends a `{"type": "error", "error": "<ExceptionType>: <message>", "stacktrace": ..., "timestamp": ...}` structured message to `structured_messages` *before* emitting the `error` event. `"error"` is in `FLUSH_EVENT_TYPES` (`chat/_flush_helper.py`), so the on_event flush writes it to `chat_history.json` and fans out `message_appended` -- the error bubble (rendered by `frontend/src/components/Message.tsx`) survives reloads, expired WS subscriptions, and disconnected tabs.

   Any partial text streamed before the failure is flushed first via `_flush_partial_text()` (the same helper the `CancelledError` arms use), so truncated output is preserved alongside the error. All `run_conversation_turn` callers (persistent WS, Slack-driven runs, headless wait-handle resume via `make_flush_callback`; the scheduler persists `messages_out` on both the success and failure paths) get the durable error for free.

2. **Abnormal Gemini stream termination.** `GeminiProvider.send_message_stream()` (`chat/llm/gemini_provider.py`) tracks `finish_reason` / `prompt_feedback.block_reason` and raises `GeminiStreamAbnormalTermination` when the stream ends on anything other than `STOP` (e.g. `MALFORMED_FUNCTION_CALL`, `SAFETY`, a blocked prompt). Without the check, such a stream yields no events and the loop treats the empty turn as a normal completion -- the run ends silently with no error anywhere.

   `MAX_TOKENS` is special-cased: if text or a function call was already streamed the truncated turn is delivered (with a logged warning); it raises only when the truncation produced no output at all (e.g. cut off mid-function-call). The Gemini client performs no automatic retries today (unlike the Anthropic SDK's default two retries on 429/5xx) -- see the TODO in `_get_client()` about enabling `HttpRetryOptions`.

3. **Contained tool failures.** `_dispatch_tool_call()` wraps the dispatch body (`_dispatch_tool_call_inner()`) in a catch-all that converts unexpected handler exceptions into a structured `{"error": "Tool '<name>' failed: ..."}` result, so a single failing tool call is surfaced to the model (which can retry or adapt) instead of aborting the entire run.

The `send_message_finished` envelope also carries `error: true` when the run raised, so live clients can distinguish "died" from "finished cleanly" (the frontend uses it to skip the success desktop notification). See [Realtime Architecture](realtime.md).

## Authenticated External API Requests (`authed_get`)

The `authed_get` tool makes authenticated GET requests to supported external APIs with automatic server-side credential injection. The LLM provides only a URL (and optional headers); the handler inspects the hostname (and optionally path prefix), matches it against a registry of known services, loads the appropriate credential (API key or per-user OAuth token), and injects it into the outgoing request. The LLM never sees or handles credentials. Services that require per-user OAuth set `requires_user: True` in their registry entry and receive the user dict for credential loading. Services with `retry_on_401: True` automatically refresh credentials and retry on 401 responses.

### How It Works

1. LLM invokes `tool_call(tool_name="authed_get", arguments={"url": "https://pro-api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"})`
2. `_dispatch_tool_call()` routes to `handle_authed_get()` in `chat/gemini_api/authed_get.py`
3. Handler validates the URL (must be HTTPS), extracts the hostname and path, and calls `_find_service()` to look up a matching entry in `_SERVICE_REGISTRY` (checks path-prefix-scoped entries first, then plain hostname)
4. If the service declares `allowed_endpoints`, the handler validates the URL path against the compiled regex patterns before proceeding. If no pattern matches, the request is rejected with an error listing the allowed patterns (currently only Airtable uses this mechanism)
5. If a match is found, the handler calls the service's `load_credentials` function. For server-key services (e.g., CoinGecko), this loads from the per-service credential store (`data/service_credentials/<service>.json`, admin-managed in Settings > Service Credentials, with a legacy `server_credentials.json` fallback -- see [Service Credentials](service-credentials.md)). For per-user OAuth services (`requires_user: True`, e.g., Gmail, Google Calendar, Google Drive, Google Docs, Google Sheets, Google Slides, Google Tasks), this calls an async loader with the user dict. For per-user PAT services (e.g., Airtable), this reads the token from the user dict
6. The service's `inject_auth` function injects the credential into the request headers (e.g., `x-cg-pro-api-key` for CoinGecko, `Authorization: Bearer` for Google services and Airtable PAT)
7. Handler makes the HTTP GET request via `httpx.AsyncClient` with a 30-second timeout
8. If the response is 401 and the service has `retry_on_401: True`, the handler refreshes credentials and retries once
9. Successful responses pass through the size gate (see [Large Response Protection](#large-response-protection)): responses within `AUTHED_GET_SIZE_LIMIT` are returned directly; larger responses are either rejected with a `response_too_large` error or stored as blob files for chunked reading depending on the `force_large_response` flag. If the caller passed `output_file`, the size gate is bypassed entirely and the body is written under the hidden `.responses/` subdirectory of the conversation/project workspace via `_handle_authed_get_to_file()`

### Supported Services

The `_SERVICE_REGISTRY` dict in `chat/gemini_api/authed_get.py` maps API hostnames (with optional path prefixes) to their auth configuration. Currently registered services:

- CoinGecko Pro (server API key)
- Google Calendar, Google Drive, Google Docs, Gmail, Google Sheets, Google Slides (read-only), Google Tasks, the four Google Cloud (GCP) hosts (Resource Manager, Compute Engine, GKE/Container, Cloud Logging -- all read-only; see [GCP API Documentation](../api/gcp-api.md)) (all per-user OAuth with 401 retry)
- Airtable (per-user PAT with `allowed_endpoints` validation)
- GitHub (per-user OAuth token without 401 retry, with `allowed_endpoints` validation and service `default_headers`)
- the no-auth Federal Register / SEC EDGAR hosts

See the registry entries in `chat/gemini_api/authed_get.py` for exact hostnames, credential loaders, and auth injection functions.

### Allowed Endpoints Validation

Services can optionally declare an `allowed_endpoints` field in their `_SERVICE_REGISTRY` entry: a list of regex patterns defining which URL paths are permitted. Before making the HTTP request, `_make_authed_request()` checks the request path against these compiled regexes (compiled at module load time for performance). If no pattern matches, the request is rejected with an error listing the allowed patterns.

The mechanism is used by Airtable (five patterns for the read-only API paths), the GitHub plugin's entry (regex patterns covering user profile, repos, branches, issues, PRs, commits, contents, org repos, and search), and the Google services (Calendar, Drive, Docs, Sheets, Gmail, Tasks, and the four GCP hosts). See the individual service's registry entry for the exact pattern list. For service-specific notes see [Airtable API Documentation](../api/airtable-api.md), [GitHub API Documentation](../../plugins/github/docs/github-api.md), and [GCP API Documentation](../api/gcp-api.md).

Allow-listing is **verb-scoped**: `_make_authed_request()` validates GET requests against `allowed_endpoints` (compiled into `_allowed_endpoints`) and POST requests against the independent `allowed_post_endpoints` (compiled into `_allowed_post_endpoints`). The two dimensions are strictly separate -- a GET can never reach a POST-only path and vice-versa, and a host with no POST allow-list rejects all POSTs. Today only the GCP Resource Manager (`organizations:search`) and Cloud Logging (`entries:list`) hosts declare `allowed_post_endpoints`; both are reachable only via [authed_post](#post-capable-sibling-authed_post).

### Service Default Headers

Services can optionally declare a `default_headers` dict in their `_SERVICE_REGISTRY` entry containing headers that must be attached to every outgoing request to that service. `_make_authed_request()` merges these into the request before caller-supplied headers and auth injection, with the precedence:

1. Service `default_headers` (lowest priority)
2. Caller-supplied `headers` argument -- can override service defaults, restricted to the allow-list below
3. `inject_auth` result -- always wins so auth headers cannot be clobbered

The same merge runs on both the initial request and the 401 retry branch so retries honor the service defaults.

### Caller Header Allow-list

Caller-supplied headers (the model's `headers` argument on the `authed_get` / `authed_post` tools, or the `headers` field of the sandbox `/api/authed-get` / `/api/authed-post` proxy bodies) are restricted to `_ALLOWED_CALLER_HEADERS` in `chat/gemini_api/authed_get.py`: plain content-negotiation headers only (`Accept`, `Accept-Language`; compared case-insensitively). `_make_authed_request()` rejects a request carrying any other header name with a JSON error (`Header(s) not allowed: ...`) **before** credential loading and network I/O, on every service -- the rejection is not host-specific.

The reason is that caller headers ride on requests carrying the user's upstream credential (for Google services, the broad `cloud-platform` OAuth bearer), so a model-supplied header is an untrusted instruction to the provider under the user's identity, and the path allow-lists cannot see the header dimension.

Providers interpret several headers as operation metadata or method controls: `X-HTTP-Method-Override` can turn a nominally read-only verb into a write, `X-Goog-User-Project` re-bills quota to another project, and `X-Goog-Request-Reason` is persisted verbatim into the target project's Cloud Audit Logs (an exfiltration channel to an attacker-owned project). Rejecting rather than silently stripping gives the model an actionable error.

The only legitimate caller header in use today is the GitHub media-type `Accept` override (e.g. `application/vnd.github.raw+json`), which remains allowed. Currently used by the GitHub plugin's entry to inject `User-Agent: Quest/1.0` (GitHub's REST API rejects requests without a User-Agent) and `Accept: application/vnd.github+json` (GitHub's recommended versioned response format).

### Design Decisions

**Why inject credentials server-side instead of having the LLM include them?**
API keys must never appear in LLM context (tool descriptions, history, or responses) because they could be leaked in logs, cached sessions, or user-visible output. Server-side injection keeps credentials out of the LLM's view entirely.

**Why a hostname-based registry with optional path-prefix scoping?**
Hostname matching is simple, unambiguous, and sufficient for most external APIs where all endpoints share a single hostname. Path-prefix scoping (e.g., `www.googleapis.com/calendar/v3`) extends this to shared hostnames like `www.googleapis.com` where multiple Google APIs coexist. `_find_service()` checks path-prefix entries first for specificity, then falls back to plain hostname.

**Why a separate handler file (`authed_get.py`) instead of adding to the `tool_handlers/` package?**
The `authed_get` handler has its own service registry, auth injection pattern, and HTTP client lifecycle, making it a distinct concern from the local tool handlers in the `tool_handlers/` package. A separate file keeps each handler module focused.

### Proxy Endpoint (`POST /api/authed-get`)

A REST proxy endpoint exposes the same `authed_get` functionality to sandbox code running inside `run_script` and `run_python` containers. Sandbox scripts cannot invoke LLM tool calls directly, so this endpoint lets them make authenticated external API requests through the same credential-injection pipeline.

**Endpoint:** `POST /api/authed-get` (registered in `quest.py`)

**Auth:** Bearer token via the `get_current_user_cookie_or_apikey` dependency in `chat/gemini_api/authed_get.py` on the main app (which, like every auth dependency, applies the `check_user_allowed()` admission policy -- see [auth.md](auth.md)); on the sandbox tool API port that dependency is overridden so ONLY the container's ephemeral `QUEST_API_KEY` sandbox token is accepted (see [Script Runner Architecture](script-runner.md#sandbox-tokens))

**Request body:** `AuthedGetRequest` Pydantic model in `chat/gemini_api/authed_get.py` -- accepts `url` (string, required) and `headers` (object, optional), matching the `authed_get` tool call args minus `intent_message`

**Handler:** `authed_get_endpoint()` in `chat/gemini_api/authed_get.py` delegates to the same `handle_authed_get()` function used by the tool call path, then parses the result back to JSON for a properly formatted `JSONResponse`

**System prompt:** The sandbox instructions section in `chat/gemini_api/system_prompt.py` informs the LLM about this endpoint so it can include the appropriate `requests.post()` call when writing sandbox scripts that need authenticated external API data

**Blocklist:** The `curl_proxy_get` and `curl_proxy_post` tools are blocked from reaching `/api/authed-get` via `_BLOCKED_PROXY_PATHS` in `chat/route_dispatch.py`. This prevents the LLM from bypassing the tool call dispatch path by constructing a `curl_proxy_post` URL targeting the proxy endpoint directly. The endpoint is only accessible as a direct HTTP call from sandbox scripts (with `QUEST_API_KEY` auth) or as the `authed_get` tool call via `_dispatch_tool_call()`. See [Route Dispatch - Blocked Proxy Paths](route-dispatch.md#blocked-proxy-paths).

### POST-capable Sibling (`authed_post`)

`authed_post` is the POST-capable sibling of `authed_get` for the narrow set of reads Google exposes only as POST verbs (initially the two GCP reads `organizations:search` and `entries:list` -- see [GCP API Documentation](../api/gcp-api.md)). It is **not** a general POST proxy: the target host must be in `_SERVICE_REGISTRY` and the path must match that host's `allowed_post_endpoints` allow-list, which is independent of the GET `allowed_endpoints` (see [Allowed Endpoints Validation](#allowed-endpoints-validation)). Write verbs are absent from every POST allow-list and are therefore unreachable.

It shares all of `authed_get`'s machinery: `_make_authed_request()` was generalized with `method` + `json_body` parameters, so registry lookup, credential loading, 401 retry, header precedence, the size gate, and the `output_file` `.responses/` write path are identical across both verbs. `handle_authed_post()` (the tool path) and `authed_post_endpoint()` (the sandbox proxy) both live in `chat/gemini_api/authed_get.py`; the tool-call dispatch branch is in `chat/gemini_api/tool_dispatch.py` and the `TOOL_CALL_REGISTRY` schema (with a `body` parameter) is in `chat/llm/tool_schemas.py`.

**Proxy endpoint:** `POST /api/authed-post` (registered in `quest.py`, `AuthedPostRequest` body model) exposes `authed_post` to sandbox code, mirroring `/api/authed-get`. It is likewise added to `_BLOCKED_PROXY_PATHS` in `chat/route_dispatch.py` so `curl_proxy_*` cannot reach it. The sandbox-snippet instructions in the `system:run_script`-adjacent catalog note (`chat/system_skills/catalog.py`) document the `requests.post(.../api/authed-post)` shape.

### Script Tool-Call Bridge (`POST /api/tool-call`)

`POST /api/tool-call` (handler `script_tool_call_endpoint()` in `chat/gemini_api/script_tool_call.py`, registered in `quest.py`) generalizes the `/api/authed-get` pattern: it lets sandbox code invoke an **allow-listed subset** of the dynamic tools with the same `{tool_name, arguments}` body shape the LLM's `tool_call` meta tool uses.

The handler validates `tool_name` against `SCRIPT_TOOL_CALL_ALLOWLIST`, then delegates to the shared `_dispatch_tool_call()` chain with `conversation_id=None` (and no provider), so argument coercion, error conversion, and notices behave identically to agent-initiated calls. JSON tool results return as JSON; non-JSON results (the `get_gmail_messages` markdown document) return as `text/plain`. A non-allow-listed `tool_name` returns 400 with the available list.

**Allow-list** (`SCRIPT_TOOL_CALL_ALLOWLIST`, explicit so new registry tools stay script-invisible until deliberately added):

- the Gmail Simple and Quest-label tools (parity with the `/api/gmail-simple/*` routes that already serve scripts)
- the memory reads (`memory_search`, `memory_list`)
- the Quest Docs reads (`list_docs`, `search_docs`, `read_doc`). The bridge dispatches with `is_script=True`, so the doc access rule sees `run_kind="script"`: no conversation or project (project docs stay hidden), never public, and every doc write denied. See [Quest Docs -- Script Bridge](quest-docs.md#script-bridge)
- plus plugin-declared entries unioned in via `extend_script_allowlist()` -- the Telegram plugin opts in its four reads (the bridge is the script path to Telegram, which has no HTTP routes) and the Slack plugin opts in all nine of its tools (the bridge is the script path to Slack, which has no per-method HTTP routes; `send_slack_dm_to_self`'s `files` attachments need conversation context and error over the bridge -- the old `/api/slack-simple/dm-self` route is retired)

Deliberately excluded:

- suspend/blocking tools (`wait_for_handles` raises agent-loop sentinels)
- conversation/project-scoped tools (`set_conversation_name`, `project_db_query`, `get_response_content`, the workspace-download tools)
- workspace file tools (scripts have `/workspace` mounted directly; `get_workspace_file` also returns provider parts with no HTTP representation)
- `authed_get`/`authed_post` (their dedicated routes handle binary responses, which the bridge does not)

**Auth:** Bearer token (`get_current_user_cookie_or_apikey`, overridden to sandbox-token-only on the sandbox port), same as `/api/authed-get`. **Blocklist:** `/api/tool-call` is in `_BLOCKED_PROXY_PATHS` so the LLM cannot reach the bridge via `curl_proxy_post` -- it invokes dynamic tools natively via `tool_call`. **System prompt:** the sandbox instructions in the `system:workspace` skill (`chat/system_skills/catalog.py`) document the endpoint and its allow-list, and the `system:slack` skill shows the Slack-read usage shape.

### Large Response Protection

The `authed_get` tool enforces a size gate on API responses to prevent large payloads from consuming excessive LLM context. Responses exceeding the size limit are rejected by default, with the LLM instructed to either narrow the request, explicitly opt in to blob storage with `force_large_response`, or save the body to the hidden `.responses/` workspace subdirectory with `output_file`.

**Key files:**
- `chat/gemini_api/constants.py` -- `AUTHED_GET_SIZE_LIMIT` (3KB default), `GET_RESPONSE_CONTENT_MAX_CHUNK` (5KB), `_RESPONSE_BLOB_DIR`
- `chat/gemini_api/authed_get.py` -- size gate logic in `handle_authed_get()`, plus `_handle_authed_get_to_file()` for the `output_file` branch
- `chat/gemini_api/tool_handlers/response_blobs.py` -- `_handle_get_response_content()` for chunked reading
- `chat/gemini_api/tool_dispatch.py` -- dispatch wiring for `force_large_response`, `output_file`, and `get_response_content`
- `chat/llm/tool_schemas.py` -- `authed_get` schema (includes `force_large_response` and `output_file` parameters) and `get_response_content` schema in `TOOL_CALL_REGISTRY`

**How it works:**

1. After `_make_authed_request()` returns a successful response, `handle_authed_get()` checks the UTF-8 byte length against `AUTHED_GET_SIZE_LIMIT`. Error responses always pass through regardless of size
2. If the response is within the limit, it is returned directly to the LLM
3. If the response exceeds the limit and `force_large_response` is not set, the handler returns a structured error (`response_too_large`) with the response size, the limit, and instructions to narrow the request or retry with `force_large_response=true`
4. If `force_large_response=true`, the response body is written to `data/chats/{conversation_id}/responses/response-{hash}.blob` (where `{hash}` is a truncated SHA-256 of the content). The LLM receives the hash, total size, and instructions to read chunks via `get_response_content`
5. The LLM calls `get_response_content(hash, offset, length)` to read the stored blob in chunks. The handler in `_handle_get_response_content()` validates the hash (hex-only), clamps `length` to `GET_RESPONSE_CONTENT_MAX_CHUNK`, reads the file, and returns a JSON object with `content`, `offset`, `length`, `total_size`, and `has_more`

**Direct-to-workspace branch (`output_file`):**

When the LLM passes `output_file` (a workspace-relative path), `handle_authed_get()` skips the size gate entirely and dispatches to `_handle_authed_get_to_file()` in `chat/gemini_api/authed_get.py`. The candidate path is re-rooted under a hidden `.responses/` subdirectory of the conversation/project workspace (auto-created), so an `output_file` of `foo.json` lands at `.responses/foo.json`; the receipt's `path` reports the full `.responses/...` path, which the model must pass verbatim to read the file back.

The workspace root comes from `_get_workspace_dir()` (the same root used by `download_drive_file` / the GitHub plugin's `github_get_job_log`), `_publish_file_list_changed()` notifies the file browser, and the tool returns a small JSON receipt (path, filename, bytes_written, content_type, status_code) instead of the body. A trailing `/` or an existing-directory destination triggers a synthesized filename (`authed-get-{sha256[:16]}{ext}`).

Unlike `write_workspace_file` (which overwrites), the write is non-destructive: if a file already exists at the resolved path, the call returns an error ("A file already exists at ... Refusing to overwrite it; choose a different output_file name.") rather than clobbering a previously saved body. Absolute paths and `..` traversal are rejected before the upstream call. Upstream HTTP errors short-circuit without writing -- the JSON error from `_make_authed_request()` is returned unchanged.

`output_file` also unlocks `alt=media` (the standard `authed_get` block on `alt=media` only applies when the body would land in LLM context). When both are set, `output_file` wins and `force_large_response` is ignored. `output_file` is a tool-only argument: internal Python callers of `_make_authed_request` (`download_drive_file`, `github_get_job_log`, etc.) and the `POST /api/authed-get` proxy endpoint are unchanged.

**Design decisions:**

**Why a size gate instead of always returning the full response?** Large API responses (e.g., full Gmail thread listings, Drive file lists, Airtable record dumps) consume significant LLM context tokens. The size gate forces the LLM to prefer targeted queries (using `fields`, `maxResults`, filters) before falling back to full responses, resulting in better context utilization.

**Why file-based storage with chunked reading?** Storing the response as a blob file and reading it in chunks keeps each tool result within a manageable size. The LLM can read only the portions it needs rather than receiving the entire payload at once.

**Why character-based offsets instead of byte-based?** Character-based slicing avoids splitting multi-byte UTF-8 characters, which would produce invalid text chunks. The handler reads the entire file into memory for character-based slicing. See the NOTE comment in `_handle_get_response_content()` for future considerations if blobs grow very large.

**Why a separate `output_file` branch alongside `force_large_response`?** The blob/`get_response_content` path is for cases where the model still wants to read the body itself, just paged. `output_file` is for the common case where the model knows up front it will hand the body to `run_python` / `run_script` / `get_workspace_file` rather than read it inline -- writing straight to the workspace skips the blob staging area and avoids the chunked-read round trips.

Bodies land under the hidden `.responses/` subdirectory (hidden by default in the file browser, since machine-written API bodies are noise the user rarely wants to see) and collide-rather-than-overwrite so a later turn can still rely on an earlier saved body. Keeping both means the model can pick the right one per call instead of being forced through chunked reading.

## Shared Tool Dispatch

`_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` is a shared function that consolidates tool dispatch logic for all tools except the agent-specific ones (`agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, `agent_task_response`). Both the main conversation loop (`run_conversation_turn()`) and the sub-agent loop (`_run_sub_agent()`) call this function to execute shared tools, eliminating the duplicated if/elif chains that previously existed in both loops.

`_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` is an async function that consolidates tool dispatch logic. See `chat/gemini_api/tool_dispatch.py` for the full function signature. It returns a tuple of `(result_string, extra_parts)` where `extra_parts` is a list of provider-specific content parts (empty for most tools). Extra parts are non-empty when `get_workspace_file` or `load_gmail_attachment` upload binary/large files, or when HTTP tools return a `ToolResultWithNotices` wrapper (see [Route Dispatch - ToolResultWithNotices Pattern](route-dispatch.md#toolresultwithnotices-pattern)).

The public function is a thin guard around `_dispatch_tool_call_inner()`: any unexpected handler exception is logged and converted into a structured `{"error": "Tool '<name>' failed: ..."}` result so one failing tool never aborts the whole run (see [Error Surfacing](#error-surfacing)).

### Dispatch Order

1. `tool_call` (meta tool) -- extracts `tool_name` and `arguments` from args; validates `tool_name` against `TOOL_CALL_REGISTRY`; pops `intent_message` from inner args; dispatches to the appropriate handler based on `tool_name` (every tool in `TOOL_CALL_REGISTRY` -- see [Tool Declarations](#tool-declarations) for the roster). Returns JSON error for unknown tool names, missing `tool_name`, or non-dict `arguments`. Logs as `tool_call:<inner_tool_name>` for observability.
   - The `get_workspace_file` dispatch branch returns `(result, extra_parts)` for the tuple unpacking pattern
   - The `download_drive_file` branch calls `_handle_download_drive_file()` from `chat/gemini_api/tool_handlers/drive.py` to download Drive file content to the workspace
   - The `google_export_doc` / `google_export_sheet` / `google_export_slides` branches call `_handle_google_export_doc()` / `_handle_google_export_sheet()` / `_handle_google_export_slides()` from the same module to export a native Google Doc / Sheet / Slides file to the workspace in a chosen format
   - The `archive_gmail_message` branch calls `_handle_archive_gmail_message()` from `chat/gemini_api/tool_handlers/gmail_labels.py` to archive a Gmail message
   - The `set_conversation_name` branch calls `_handle_set_conversation_name()` from `chat/gemini_api/tool_handlers/misc.py` to set a custom display name on the conversation
   - The `authed_get` branch calls `handle_authed_get()` from `chat/gemini_api/authed_get.py` for authenticated external API requests, passing `force_large_response`, `output_file`, and `conversation_id` for large response protection (see [Large Response Protection](#large-response-protection))
   - The `get_response_content` branch calls `_handle_get_response_content()` from `chat/gemini_api/tool_handlers/response_blobs.py` for chunked reading of stored large responses
2. `get_current_time` -- calls `_handle_get_current_time(timezone)` locally (backward compatibility path for cached sessions; new sessions use `tool_call`)
3. `list_workspace_files` -- calls `_handle_list_workspace_files(user_id, conversation_id)` locally (backward compatibility path for cached sessions; new sessions use `tool_call`)
4. `get_workspace_file` -- calls `_handle_get_workspace_file(client, user_id, conversation_id, path)` locally, returns `(result, extra_parts)` (backward compatibility path for cached sessions; new sessions use `tool_call`). Pre-checks MIME type against `_UNSUPPORTED_GEMINI_MIME_TYPES` and file size against the per-model attachment cap (`get_attach_limit_for_model()` in `chat/llm/file_limits.py`) before uploading; uploads via `provider.upload_file()` (Gemini: inline `Part.from_bytes` for images/PDFs on Vertex; Anthropic: base64-encoded inline content for images/PDFs)
5. `load_gmail_attachment` -- calls `_handle_load_gmail_attachment(client, user, message_id, attachment_id, filename, mime_type)` locally, returns `(result, extra_parts)`. Same MIME-type and per-model size pre-checks and provider-agnostic upload as `get_workspace_file`
6. `write_workspace_file` -- calls `_handle_write_workspace_file(user_id, conversation_id, path, content)` locally (backward compatibility path for cached sessions; new sessions use `tool_call`)
7. `memory_search` -- calls `_handle_memory_search(user_id, query)` locally (backward compatibility path for cached sessions; new sessions use `tool_call`)
8. `memory_list` -- calls `_handle_memory_list(user_id)` locally (backward compatibility path for cached sessions; new sessions use `tool_call`)
9. `list_skills` -- calls `_handle_list_skills(user_id)` locally; returns skill IDs, names, descriptions, and visibility (no content)
10. `search_skills` -- calls `_handle_search_skills(user_id, keyword)` locally; case-insensitive substring match on name/description via `search_accessible_skills()` in `db/skill_store.py`
11. `load_skills` -- calls `_handle_load_skills(user_id, skill_ids)` locally; batch-fetches full skill content via `get_accessible_skills_by_ids()` in `db/skill_store.py` and returns a plain markdown document (not JSON) assembled by `_render_loaded_skills_markdown()`. See [Skill Library -- load_skills Response Format](skill-library.md#load_skills-response-format)
12. `list_my_skills` -- calls `_handle_list_my_skills(user_id, project_id, exclude_*)` locally; categorized read-only listing of the user's DB skills with per-skill autoload status (no content, no `system:*`). See [Skill Library -- Skill Inspection Tools](skill-library.md#skill-inspection-tools)
13. `get_skill` -- calls `_handle_get_skill(user_id, skill_id, project_id)` locally; full read-only detail for one DB skill, with the owner-only share roster. See [Skill Library -- Skill Inspection Tools](skill-library.md#skill-inspection-tools)
14. `list_routines` -- calls `_handle_list_routines(user_id, project_id)` locally; read-only listing of the current project's routines with schedule and auto-loaded-skill data (structured error when the conversation is not in a project). See [Routines Architecture -- Agent Tools](routines.md#agent-tools)
16. `run_script` -- executes a workspace script in an ephemeral Podman container; see [Script Runner Architecture](script-runner.md)
17. `run_python` -- executes inline Python code in an ephemeral Podman container (same container as `run_script`, script piped via stdin); see [Script Runner Architecture](script-runner.md)
18. Fallback -- calls `execute_tool_call(app, user, tool_name, args)` from `chat/route_dispatch.py` for HTTP tools (`curl_proxy_get`, `curl_proxy_post`). Returns `(result, notices)` where `notices` is a list of advisory strings extracted from `ToolResultWithNotices` wrappers; each notice is converted to a provider-specific text `extra_part` via `provider.make_text_part()`

Agent-specific tools (`agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, `create_action_request`, `agent_task_response`) are NOT handled by `_dispatch_tool_call()` -- they are handled directly by the calling loop since they require loop-specific context (e.g., the main loop handles `agent_task`/`agent_task_parallel`/`agent_task_parallel_template` with spawning and logging, `create_action_request` with database persistence via `db/action_request_store.py`, while the sub-agent loop handles `agent_task_response` for termination).

The `wait_for_handles` tool is in `TOOL_CALL_REGISTRY` but the top-level conversation loop intercepts its `tool_call` invocations before normal `_dispatch_tool_call()` to block on outstanding rows; see [Wait Handles Architecture](wait-handles.md).

## System Prompt

`get_system_prompt(user_api_key, base_url, custom_system_prompt, connected_services, user_name, user_email, project_guide, skills_content, has_project)` in `chat/gemini_api/system_prompt.py` builds the top-level system prompt. Per-backend API documentation (Gmail, Slack, Telegram, Twitter, GitHub, Airtable, Google Calendar/Drive/Docs/Sheets/Tasks) is **not** inlined here -- it lives in loadable **system skills** and is pulled in on demand via the `load_skills` tool. See [Skill Library Architecture -- System Skills](skill-library.md#system-skills) for the catalog, gating, and loading flow.

The prompt is assembled from, in order:

1. Identity line derived from `user_name` / `user_email`
2. Optional `custom_system_prompt` from the resolved guide
3. Optional `skills_content` (user + project auto-loaded DB skills) as an "Enabled Skills" section
4. Optional `project_guide` as a "Project-Specific Instructions" section; when the conversation is in a project, a short pointer to `system:project_db` is appended
5. Message-envelope explainer (XML metadata wrapping, UTC/local time hints, `<conversation_skills_loaded>` semantics)
6. Sixteen directly-registered tools: `curl_proxy_get`, `curl_proxy_post`, `tool_call`, `load_gmail_attachment`, `agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, `create_action_request`, `run_script`, `run_python`, `list_skills`, `search_skills`, `load_skills`, `list_my_skills`, `get_skill`, `list_routines`. Descriptions are short and point at the relevant `system:<backend>` skill for backend-specific details
7. Auto-generated "Dynamic Tools" section produced by `_build_dynamic_tools_section()` over `TOOL_CALL_REGISTRY` (tools dispatched via the `tool_call` meta tool)
8. System-skills enumeration block produced by `build_system_skills_enumeration(connected_services, has_project)` from `chat.system_skills` -- a compact bullet list (id, description, when-to-load) of every catalog entry visible under the current gates. This is the only always-on signal that a given backend's docs exist; the model loads them as needed
9. Short backend-agnostic "Important rules" bullets:
   - the `{base_url}/api/` prefix constraint for `curl_proxy_*`
   - the no-`Authorization`-header rule
   - the directive to load the matching `system:<backend>` skill before calling a backend's APIs
   - the `send_slack_dm_to_self` self-messaging exception to `create_action_request`
   - sub-agent guidance (parallel vs template, retry, parallelism caps)
   - the inline-image convention: the web chat renders `![alt](workspace/relative/path.png)` markdown images from the conversation workspace (PNG/JPEG/GIF/WebP, workspace-root-relative paths; not available in Slack replies). The same guidance appears in the `system:workspace` skill's charts section and in `get_public_project_system_prompt()`; the FE resolution mechanism is in [Frontend Architecture](frontend.md) (Message component section)
10. "Conversation naming" block instructing the model to call `set_conversation_name` as its first tool call on the opening turn. When `is_routine=True` (routine runs -- `run_conversation_turn()` passes `bool(routine_id)`) the block is a one-line note that the conversation is already named after its routine, and `set_conversation_name` is also dropped from the Dynamic Tools section; routine conversations get their `custom_name` at creation time instead (see [Routines Architecture](routines.md))
11. Strategy for large data processing tasks (Scout / Batch / Aggregate pattern), model selection guidance, and generic short-form examples (workspace files, memory, `get_current_time`, `agent_task`, `load_skills`)
12. Proxy preamble (`api.instructions._preamble_instructions(base_url, user_api_key, connected_services)`) -- the "Quest API Proxy" intro plus auth-header rule. This is the only piece of the previously-massive `get_instructions_content()` tail that is still inlined; it's small, universal, and anchors the rest of the prompt.

Backend docs (former `{instructions}` tail) are no longer expanded here. Each `api/*.get_instructions(base_url)` function is reachable only through the matching `system:<backend>` skill's `content_builder` in `chat/system_skills/catalog.py`.

The custom system prompt is resolved from the Guides system in `run_conversation_turn()` before calling `get_system_prompt()`. On the first message in a conversation, the guide is resolved from the `guide_id` parameter (or the user's default guide) via `db/guide_store.py`, and its content is snapshotted into `chat_history.json`. On subsequent messages, the snapshotted content is used directly (preserving the prompt even if the guide is later edited or deleted).

Auto-loaded skills are also resolved in `run_conversation_turn()` at two levels: user-level via `get_user_autoloaded_skills()` and (for project conversations) project-level via `get_project_autoloaded_skills()` from `db/skill_store.py`. The two sets are merged with deduplication by skill ID, so a skill auto-loaded at both levels is only injected once. Unlike guides, skills are NOT snapshotted -- each message uses the current auto-loaded skills and current content. The pre-resolved skills content string is passed as `skills_content` to both `get_system_prompt()` and all sub-agent calls. See [Skill Library Architecture](skill-library.md) for details on skill injection and design decisions.

The connected services dict is obtained via `get_user_connected_services(user)` from `api.instructions` (auth dependencies imported from the `auth/` submodule). The user identity (`user_name`, `user_email`) is extracted from the `user` dict via `user.get("name", "")` and `user["email"]`. Users configure guides via the Settings panel > Guides section (see [Guides Architecture](guides.md)) or via the legacy Custom Instructions textarea (which syncs to the default guide).

`get_sub_agent_system_prompt(agent_name, user_api_key, base_url, custom_system_prompt, connected_services, user_name, user_email, project_guide, skills_content, has_project, can_nest=False)` in `chat/gemini_api/system_prompt.py` builds the system prompt for sub-agents. It follows the same structure as the top-level prompt but with key differences:

- Identifies the model as a sub-agent with the given `agent_name`
- Injects the same user identity line as the top-level prompt (if `user_email` is present)
- Includes the same "Message format" XML envelope explanation (sub-agents also receive wrapped messages)
- Lists the same nine base tools plus `agent_task_response` instead of `agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, and `create_action_request` (ten tools total). The dynamic tools section excludes `set_conversation_name` via the `exclude` parameter on `_build_dynamic_tools_section()`. When `can_nest=True` (a 1st-level sub-agent in a `nested_subagents` conversation), the enumeration becomes eleven tools, adding `agent_task_nested`
- Carries a "What you cannot do" rule that explicitly enumerates the absent tools (`create_action_request`, `wait_for_handles`, `agent_task*`, `set_conversation_name`) and instructs the model to return any approval-gated write proposal to the parent via `agent_task_response` rather than attempt the write directly. See [Action Requests Architecture](action-requests.md) for the escape-hatch flow.
  - When `can_nest=True`, this rule is relaxed to permit exactly one tier of 2nd-level sub-agents via `agent_task_nested` (restricted to `claude-haiku-4.5` / `gemini-3.5-flash-lite`, leaves that cannot spawn further); the other top-level tools stay forbidden. `can_nest` is always `False` for 2nd-level sub-agents. See [Nested Sub-Agents](#nested-sub-agents)
- Receives the same `connected_services` / `has_project` gating, so the system-skills enumeration is identical to the parent's
- Receives the same pre-resolved `skills_content` from the parent, so sub-agents see the same auto-loaded DB skills
- Gets access to `list_skills` / `search_skills` / `load_skills` so it can pull in backend skills independently of the parent; loaded skill content lives only for the sub-agent's turn
- Instructs the model to call `agent_task_response(response="...")` when the task is complete and not to ask follow-up questions

Both system prompts are passed as `system_instruction` in their respective chat session configs, so the SDK includes them in every request automatically.

### Conditional Service Filtering

Both system prompt functions accept an optional `connected_services` parameter (dict mapping service group names to booleans). The dict is forwarded to `build_system_skills_enumeration()` so only skills whose `requires` gate is satisfied are enumerated -- backend skills for services the user has not connected are hidden from the model's view. When `connected_services` is `None`, all backend skills are enumerated (used by tests and admin tooling).

`get_user_connected_services(user)` in `api/instructions.py` builds the dict from non-null OAuth token / API key fields on the user record (`google_services_oauth`, `slack_oauth.access_token`, `telegram_session`, plus `airtable`, `ramp`), extended with one key per loaded plugin (e.g. `github`, `twitter` -- true only when the plugin's server config AND the user's stored credential row are both present).

### Sub-Agent Retry Rule

The system prompt instructs the model to retry failed sub-agents (up to 2 times) instead of doing the work itself. This preserves the parent agent's context window -- if a sub-agent fails due to an infrastructure error (API timeout, rate limit, transient error), spawning a new sub-agent with the same task is cheaper than the parent reading and processing the data directly. This rule is defined in `get_system_prompt()` in `chat/gemini_api/system_prompt.py` as a bullet in the "Important rules" section.

### Large Data Processing Strategy

The system prompt includes a "Strategy for large data processing tasks" section in `get_system_prompt()` in `chat/gemini_api/system_prompt.py` that teaches the model a three-phase pattern for handling large volumes of data (e.g., many emails, Slack messages, or Telegram histories). The goal is to preserve the parent agent's context window by delegating raw data reading to sub-agents and only aggregating condensed results.

**Three-phase pattern (Scout / Batch / Aggregate):**

1. **Scout** -- A single sub-agent estimates the data volume and proposes time-range-based batch boundaries. The scout queries list/search APIs with date filters and reports approximate counts per time range, without reading full message contents
2. **Batch processing** -- Parallel sub-agents (via `agent_task_parallel`) each process one batch. Each sub-agent reads the full data for its assigned time range, distills it into a concise summary, and returns the summary via `agent_task_response`
3. **Aggregation** -- The parent agent merges the condensed summaries from all batch sub-agents into a coherent response for the user

**Model selection guidance** (included in the system prompt):

- `gemini-3.5-flash-lite` (Vertex-backed Flash-Lite) for the simplest, fastest, cheapest tasks -- straightforward data retrieval, counting, basic lookups
- `gemini-3.6-flash`, `gemini-3.7-flash`, and `gemini-3.8-flash` (Vertex-backed Flash) or `claude-haiku-4.5` (Claude Haiku) for search, retrieval, counting, and summarization tasks -- scout sub-agents, batch readers, and any task primarily reading and condensing data; `gemini-3.8-flash` is the newest Flash-class model
- `claude-sonnet-4-6` (Claude Sonnet) for tasks requiring moderate reasoning capability -- analysis, summarization, and multi-step tasks that benefit from stronger reasoning than Haiku but do not need Opus-level capability
- `claude-opus-4-6` (Claude Opus 4.6) for demanding reasoning, coding, and analysis tasks; remains available alongside the newer Opus models
- `claude-opus-4-7` (Claude Opus 4.7) for demanding reasoning, coding, and analysis tasks -- highly capable, superseded by Opus 4.8
- `claude-opus-4-8` (Claude Opus 4.8) for the most demanding reasoning, coding, and analysis tasks -- a highly capable Anthropic model with a 1M-token context window
- `claude-sonnet-5` (Claude Sonnet 5) for demanding reasoning, coding, and analysis tasks -- a highly capable Anthropic model with a 1M-token context window
- `claude-sonnet-5-5` (Claude Sonnet 5.5) for demanding reasoning, coding, and analysis tasks -- the newest Sonnet, the best balance of speed and intelligence in the Claude line-up at Sonnet cost, with a 1M-token context window
- `claude-opus-5` (Claude Opus 5) for the most demanding reasoning, coding, and analysis tasks -- a highly capable Anthropic model with a 1M-token context window
- `claude-opus-5-5` (Claude Opus 5.5) for the most demanding reasoning, coding, and analysis tasks -- the newest and most capable Anthropic model, built for long-running agentic work and cheaper per token than Opus 5, with a 1M-token context window (Opus 4.8, Sonnet 5, Sonnet 5.5, Opus 5, and Opus 5.5 are the only Claude models not capped at 200K)
- Default to Flash-Lite, Flash, or Haiku when in doubt; most batch-processing sub-agents should use a faster/cheaper model

**Batch size guidelines** (included in the system prompt):

- Email: 20-50 messages per batch
- Slack: 100-200 messages per batch
- Time ranges as batch boundaries to prevent overlap
- Skip the scout phase for small volumes (fewer than ~20 emails or ~50 Slack messages)

**Error handling guidelines** (included in the system prompt):

- Retry failed sub-agents by spawning new ones (up to 2 times) instead of doing the work directly
- Re-dispatch only failed tasks from an `agent_task_parallel` batch, not successful ones
- When a sub-agent hits its input token limit, split the batch into smaller sub-ranges and re-dispatch

## Deprecated Model Remapping

The `_DEPRECATED_MODELS` dict in `chat/gemini_api/constants.py` maps retired model IDs to their replacements. Currently it maps `gemini-3-pro-preview` to `gemini-3.1-pro-preview`. This remapping is applied in three places within the backend:

1. `run_conversation_turn()` -- remaps the model after resolving the default from `server_config.json`
2. Single sub-agent model resolution -- remaps the `model` parameter from `agent_task` tool calls
3. Parallel sub-agent model resolution -- remaps per-task `model` parameters in `agent_task_parallel` calls

The frontend performs equivalent remapping via `DEPRECATED_MODEL_MAP` in `frontend/src/constants/models.ts`:

- `ConversationModelsContext.tsx` -- remaps the per-user default model (fetched from `GET /app/api/me`, no longer localStorage) and per-conversation models
- `Sidebar.tsx` -- remaps routine model when executing a routine via `handleRunRoutine()`
- `RoutineSettingsModal.tsx` -- remaps routine model when loading routine settings for editing

The `GEMINI_3_PRO` enum value is preserved in `db/models.py` for historical analysis of past API calls recorded in the `llm_calls_gemini` analytics table.

## Client Singletons

Each LLM provider maintains its own SDK client cache. The `GeminiProvider` caches one `genai.Client` created with `vertexai=True` plus the project ID and region from the `gemini_vertex` section of `server_config.json` (falling back to `anthropic.vertex_project_id`). The `AnthropicProvider` holds an `AsyncAnthropicVertex` singleton created lazily with the project ID and region from `server_config.json`. Provider instances themselves are lazily-created singletons managed by `get_provider_instance()` in `chat/llm/config.py`. See [LLM Provider Abstraction](llm-providers.md) for details.

The legacy `_get_client()` in `chat/gemini_api/session.py` may still exist for backward compatibility but the canonical client management is in the provider implementations.

## In-Memory Chat Session Store

### Session Lifecycle

Chat sessions are stored in `_active_chats` (module-level dict in `chat/gemini_api/session.py`), keyed by `(user_id, conversation_id)` tuples (where `user_id` is the integer primary key).

`get_or_create_chat(provider, user_id, conversation_id, model, system_prompt, history=None, disk_history=None, disk_provider=None)` returns an existing session or creates a new one via `provider.create_session()`. The optional `history` parameter accepts a list of history dicts (loaded from `sdk_history.json` on restart) and is passed to the provider so the session is initialized with full prior context. The optional `disk_history` and `disk_provider` parameters provide a fallback history source when an in-memory session is discarded due to a model change (see [Model Change History Preservation](#model-change-history-preservation) below). The session config includes:

- `system_instruction` -- The system prompt (tool reference, system-skills enumeration, and the proxy preamble; backend docs are loaded on demand via `load_skills` -- see [System Prompt](#system-prompt))
- `tools` -- A `types.Tool` wrapping `TOOL_DECLARATIONS` (base tools + `agent_task` + `agent_task_parallel` + `agent_task_parallel_template`)
- `automatic_function_calling` -- Explicitly disabled (set to `disable=True`) so the module controls the tool execution loop

### Session Removal

`remove_chat_session(user_id, conversation_id)` in `chat/gemini_api/session.py` removes a session from `_active_chats`. This is called by `_run_send_message` in `chat/realtime/socket.py` when a conversation turn is cancelled (user sends `{op:"stop"}`). The interrupted session's internal history is now inconsistent (the SDK saw a partial exchange), so discarding it ensures the next message starts a fresh session.

`invalidate_user_sessions(user_id)` in `chat/gemini_api/session.py` removes all cached chat sessions for a user from `_active_chats`. This is called in two scenarios:
1. By `update_settings()` in `chat/routes/user.py` when user settings change (e.g., custom system prompt)
2. By all OAuth callbacks in the `auth/` submodule and the plugin routers (`auth/google_services.py`, `plugins/slack/oauth.py`, `plugins/telegram/auth.py`, `auth/service_key.py` for plugin keys) when a user connects or disconnects a service, so the system prompt refreshes to include or exclude the newly connected/disconnected service's documentation

Discarding all sessions ensures the next message in any conversation creates a fresh session with the updated system prompt. Returns the number of sessions removed.

### History Management

The SDK chat session automatically tracks conversation history internally. Each call to `chat.send_message_stream()` appends the user message, model response, function calls, and function results to the session's history. No manual history reconstruction is needed between turns within the same session.

#### SDK History Persistence

`_save_sdk_history()` in `chat/gemini_api/history.py` serializes the full session history from `chat.get_history()` to `sdk_history.json` in the conversation directory (alongside `chat_history.json`). Each `Content` object is serialized via Pydantic's `model_dump_json(exclude_none=True)` (which handles proto wrapper types and SDK-internal objects correctly), then parsed back to plain dicts that are both `json.dump`-safe and `Content.model_validate()`-safe on reload. The file is written with compact JSON separators for minimal disk usage.

`_load_sdk_history()` reads the saved dicts from `sdk_history.json` and returns a `(history, provider_name)` tuple, where `provider_name` is `'gemini'` or `'anthropic'` (legacy files without a provider envelope are assumed Gemini). The provider name enables format compatibility checking -- Gemini-format history (role `"model"`, `parts` arrays) is incompatible with Anthropic sessions (role `"assistant"`, `content` arrays) and vice versa.

The caller in `run_conversation_turn()` always loads disk history eagerly and passes it to `get_or_create_chat()` both as the primary `history` (when no in-memory session exists and providers match) and as `disk_history`/`disk_provider` fallback (for model-change scenarios).

Both save and load operations are wrapped in `try/except` blocks -- failures log warnings but never crash the conversation loop. The save path uses `ChatStorage.get_conversation_dir()` (the validated central resolver, see [Data Paths](data-paths.md)) to locate the conversation directory.

The save fires at flush-event boundaries: `_capturing_on_event` in `chat/gemini_api/conversation.py` wraps the caller's `on_event` and calls `_save_sdk_history()` after returning whenever the event type is in `FLUSH_EVENT_TYPES` (`tool_use`, `tool_result`, `action_request`, `stats` -- the constant lives in `chat/_flush_helper.py`, alongside the `make_flush_callback()` factory the call sites use to flush `chat_history.json`). Because the same event-set drives both files, `chat_history.json` and `sdk_history.json` advance together.

Suspension paths -- the `wait_for_handles` arm (`SuspendForWaitHandles`), the `send_slack_reply_and_get_response` arm (`SuspendForSlackReply`), and the `create_action_request` arm (`SuspendForActionRequest`) -- rely on the boundary save: by the time the sentinel is raised, the on-disk envelope already ends on the dangling `tool_use` shape the resume bucket closes. See [Dangling Tool_use Recovery on Resume](#dangling-tool_use-recovery-on-resume) below and [Slack Socket Mode -- Restart Resilience](slack-socket-mode.md#restart-resilience). Save failures are logged via `logger.warning(..., exc_info=True)` and swallowed, matching the policy in `_save_sdk_history()` itself.

#### Dangling Tool_use Recovery on Resume

When disk history is loaded for a conversation whose last assistant turn has unanswered tool_use blocks (sentinel-suspended `wait_for_handles` or `send_slack_reply_and_get_response`, or any cancellation / restart mid-tool), the top of `run_conversation_turn()` calls `provider.get_pending_tool_use_args(chat)` and -- for each dangling tool_id -- looks up the matching `tool_wait_handles` row to decide how to close it. See [Resume Bucket Logic](#resume-bucket-logic) for the bucketing.

`_save_interrupted_sdk_history()` in `chat/routes/_helpers.py` reads the existing on-disk envelope via `_load_sdk_history()` and walks the last entry's parts/blocks (Gemini `parts` or Anthropic `content`) to detect a dangling tool_use. When dangling, the helper leaves the envelope untouched so the resume bucket can close it; otherwise it appends partial assistant text plus the interruption marker as a tail edit.

Because the boundary save keeps the on-disk file current, the helper does not call `provider.save_history()` and contains no provider isinstance branches. `provider.get_pending_tool_use_args()` and `append_user_text()` (defined on `LLMProvider`; see [LLM Provider Abstraction](llm-providers.md#llmprovider-interface)) are used by the resume bucket logic at the top of `run_conversation_turn()`.

### Session Recovery on Restart

On server restart, in-memory sessions in `_active_chats` are lost. However, the SDK session history persisted in `sdk_history.json` is loaded automatically when the next message arrives for a conversation.

The `run_conversation_turn()` function always loads disk history eagerly via `_load_sdk_history()` (which returns a `(history, provider_name)` tuple). If no in-memory session exists for the `(user_id, conversation_id)` key and the disk history's provider matches the target model's provider, the history is passed to `get_or_create_chat(history=...)`. If the providers do not match (e.g., a conversation was last used with Gemini but is now being opened with a Claude model), the incompatible history is discarded with a log message.

The disk history is also passed as `disk_history`/`disk_provider` fallback parameters so that `get_or_create_chat()` can use it when an existing in-memory session is discarded due to a model change. This means the model retains full context from prior turns across server restarts and model switches, including all tool calls and results, as long as the history format is compatible.

The `chat_history.json` file continues to serve as the persistence layer for frontend display, written by `ChatStorage.append_message` and `ChatStorage.append_structured_messages` in `chat/storage.py`. Both append paths stamp a per-conversation monotonic `seq` and call the shared `_publish_appended_to_bus` helper so the persistent-WS subscribers (other tabs, the live web UI for a Slack-driven run) receive `message_appended` events. The `sdk_history.json` file is a separate persistence layer specifically for reconstructing SDK sessions. See [Realtime Architecture](realtime.md) for the seq protocol and replay buffer.

### Model Change History Preservation

When a user switches models mid-conversation (via the model dropdown), `get_or_create_chat()` in `chat/gemini_api/session.py` detects the mismatch between the stored model and the requested model, and applies provider-aware history preservation logic:

- **Same-provider switch** (e.g., Claude Haiku 4.5 to Claude Sonnet 4.6): The in-memory session's history is extracted via `provider.save_history()` and used to initialize the new session. History format is compatible because both models use the same provider, so full conversation context is preserved.
- **Cross-provider switch** (e.g., Gemini to Claude): In-memory history is incompatible (Gemini uses role `"model"` with `parts` arrays; Anthropic uses role `"assistant"` with `content` arrays). The function falls back to `disk_history` if the disk history's provider matches the new model's provider. If no compatible fallback exists, the session starts fresh.
- **Fallback chain**: If `provider.save_history()` fails during a same-provider switch, the function falls back to `disk_history` (if provider-compatible), then to no history.

Provider resolution uses `get_provider_for_model()` from `chat/llm/config.py`. If the model is not recognized, the provider is treated as `None` and the switch is handled as cross-provider.

The frontend enforces provider locking after the first message (filtering the model dropdown to same-provider models), so cross-provider switches are not expected during normal interactive use. However, the backend handles them gracefully for robustness, particularly for scheduled routine conversations where the routine's configured model might differ from the default.

## Sub-Agent Execution

The `agent_task` tool allows the top-level model to spawn ephemeral sub-agents that work independently on delegated tasks. Sub-agent execution is implemented in `_run_sub_agent()` in `chat/gemini_api/sub_agent.py`.

### Sub-Agent Lifecycle

1. The top-level model calls `agent_task(name, prompt, description, model?)` during the conversation loop
2. `_run_sub_agent()` creates an ephemeral chat session via `client.aio.chats.create()` with `SUB_AGENT_TOOL_DECLARATIONS` (9 base tools + `agent_task_response`, no `agent_task` -- 10 tools total, plus dynamic tools via `tool_call` from `TOOL_CALL_REGISTRY` excluding `set_conversation_name`). The session uses the `model` specified in the `agent_task` call, or inherits the parent's model if omitted.
   - The chosen model must not be in `SUB_AGENT_DISALLOWED_MODELS` (see [Sub-Agent Model Restriction](#sub-agent-model-restriction) below) -- the calling arms reject disallowed models before spawning, and `_run_sub_agent()` carries a defense-in-depth guard at its top that returns the same error string if reached
3. The sub-agent receives its own system prompt via `get_sub_agent_system_prompt()`, which includes the same system-skills enumeration and baseline tools as the parent and instructs it to return results via `agent_task_response()`. Backend docs are loaded on demand via `load_skills` (see [System Prompt](#system-prompt))
4. The sub-agent runs a streaming conversation loop similar to the main loop, executing shared tool calls via `_dispatch_tool_call()` and handling `agent_task_response` directly for termination
5. The sub-agent terminates when it calls `agent_task_response(response="...")`. The system sends an acknowledgment function response back to the model and drains the model's final response to ensure the conversation stream is fully completed and HTTP connections are properly released. The response string is then returned to the parent
6. If the sub-agent finishes without calling `agent_task_response` (no more function calls), accumulated text output is returned as a fallback
7. The sub-agent's response is returned to the top-level conversation loop as the `tool_result` for the `agent_task` call
8. Every terminal path in `_run_sub_agent()` emits a `sub_agent_finished` event (via the inner `_emit_finished()` helper) with `status="success"` before returning. If the sub-agent raises before it can emit its own event, the parent catch sites in `run_conversation_turn()` (for `agent_task`) and `_run_parallel_sub_agents()` (for `agent_task_parallel` / `agent_task_parallel_template`) emit a fallback `sub_agent_finished` event with `status="error"`. This event is the canonical "this sub-agent is done" signal used by the UI -- the per-row and group `COMPLETED` / `ERRORED` badges are driven from it, not from whether the last inner tool call has a result

### Sub-Agent Constraints

- **No recursive spawning**: Sub-agents receive `SUB_AGENT_TOOL_DECLARATIONS` which excludes `agent_task`, so they cannot spawn their own sub-agents
- **No approval-gated writes**: `create_action_request` is also absent from `SUB_AGENT_TOOL_DECLARATIONS`. Approval cards live in the top-level run only, and the suspend / resume machinery (`SuspendForActionRequest`, dangling-tool_use closure) is exclusively top-level. A sub-agent that needs a write should return the proposal via `agent_task_response` for the parent to issue. See [Action Requests Architecture](action-requests.md)
- **Ephemeral sessions**: Sub-agent chat sessions are not stored in `_active_chats` -- they are created and discarded within a single `_run_sub_agent()` call. The conversation is fully completed (acknowledgment sent and final response drained) before the session is discarded, ensuring proper HTTP connection cleanup
- **Sub-agent tool call visibility**: Sub-agent tool calls are emitted as transient `sub_agent_tool_use` and `sub_agent_tool_result` events on the persistent WS for display in an expandable tree structure nested under the parent `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` tool call. The sub-agent's intermediate text output and final response are not streamed -- only its tool calls are visible. Events carry `parent_tool_id` and `agent_name` so the frontend can group them correctly.
  - The events are NOT durable on the wire (no `seq`, no replay buffer entry); persistence happens at the parent `tool_result` flush boundary, which attaches the accumulated sub-agent events to the parent message's `sub_agent_tool_calls` array via the boundary save in `_capturing_on_event`. A late subscriber misses the live transient events but hydrates the full sub-agent tree on the next `message_appended` for the parent `tool_result`.
  - Emission failures are caught and logged at DEBUG level to avoid disrupting the sub-agent loop. See [Realtime Architecture](realtime.md) and [Chat API](../api/chat-api.md) for event format details
- **Sub-agent finished signal**: In addition to per-tool-call events, every terminal path in `_run_sub_agent()` emits a single `sub_agent_finished` event with `status="success"` or `status="error"` (fallback error events are also emitted from the parent catch sites in `run_conversation_turn()` and `_run_parallel_sub_agents()` when the sub-agent raises before emitting its own). This is the canonical "done" signal the frontend uses to flip the sub-agent's `COMPLETED` / `ERRORED` badge -- it is deliberately decoupled from the last inner `tool_result` so sub-agents that still have post-tool-call work to do (e.g. summarizing before calling `agent_task_response`) continue to render as running
- **Shared workspace**: Sub-agents have access to the same conversation workspace and API endpoints as the parent agent
- **Turn limit**: Model-specific, determined by `get_sub_agent_turn_limits(model)` in `chat/gemini_api/constants.py`. Models with 200K-token context windows (Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7) get a max of 20 turns because they fill their context faster; 1M-token models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5) and unknown models get the default max of 60 turns (`MAX_SUB_AGENT_TURNS`). If exceeded, accumulated text is returned or a timeout message is generated
- **Turn warning**: Model-specific, also from `get_sub_agent_turn_limits(model)`. The warning threshold is 15 for 200K-token models and 50 (`SUB_AGENT_TURN_WARNING_THRESHOLD`) for 1M-token/unknown models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5). When the turn count reaches the threshold, a system warning is injected into the tool results via `provider.inject_turn_warning()`, instructing the model to call `agent_task_response` immediately with whatever findings it has gathered. The warning is injected on every turn from the threshold onward until the sub-agent terminates or hits the hard limit
- **Context window warning**: When a sub-agent's context window usage reaches `SUB_AGENT_CONTEXT_WARNING_THRESHOLD = 0.75` (75%, constant in `chat/gemini_api/constants.py`), a system warning is injected into the tool results via the same `provider.inject_turn_warning()` mechanism.
  - The check uses `compute_total_context_tokens()` from `chat/llm/base.py` to compute current context usage and `max_input_tokens` from `MODEL_REGISTRY` in `chat/llm/config.py` to determine the model's context limit. The warning tells the sub-agent its context is X% full and instructs it to call `agent_task_response` immediately. Both the turn warning and context warning can fire on the same turn if both thresholds are crossed simultaneously
- **Logging**: Every tool call within a sub-agent is logged at INFO level with the prefix `[sub-agent:<name>]`, including the tool name, turn number, conversation ID, user email, duration in milliseconds, and result length. When a sub-agent terminates via `agent_task_response`, the response length and user email are also logged. All sub-agent log entries include `user=<email>` for per-user filtering
- **Model selection**: The optional `model` parameter allows the parent to choose a different model for the sub-agent, including models from a different provider (e.g., `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, or `gemini-3.8-flash` for simpler tasks, `claude-haiku-4.5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, or `claude-opus-5-5` for Anthropic). When omitted, the sub-agent inherits the parent's model. Gemini 3.1 Pro (`gemini-3.1-pro-preview`) may NOT be used by sub-agents -- see [Sub-Agent Model Restriction](#sub-agent-model-restriction) below
- **Error isolation**: Exceptions in `_run_sub_agent()` are caught by the parent loop and returned as JSON error strings in the tool result, so a failing sub-agent does not crash the parent conversation
- **Usage tracking**: Per-turn token usage for each sub-agent API call is recorded to the per-provider raw analytics tables (`llm_calls_gemini` / `llm_calls_anthropic`) via `record_api_call()` with `call_type=ApiCallType.SUB_AGENT`. Usage is also accumulated into the `usage_accumulator` dict for the parent to include in the stats event breakdown. New input tokens are computed via `compute_new_input_tokens()` using the sub-agent's own provider name, supporting mixed-provider sub-agents

### Sub-Agent Model Restriction

Sub-agents may not run on models in the `SUB_AGENT_DISALLOWED_MODELS` set in `chat/gemini_api/constants.py` (currently `{"gemini-3.1-pro-preview"}` -- Gemini 3.1 Pro, reserved for top-level user-driven conversations only). The restriction applies whether the model was chosen explicitly via the optional `model` parameter or inherited from the parent's model. Top-level conversations are unaffected.

The check is applied after the `_DEPRECATED_MODELS` alias remap (so the deprecated `gemini-3-pro-preview` alias, which remaps to `gemini-3.1-pro-preview`, is also blocked) and before any sub-agent is spawned (so no tokens are spent). It lives in three places:

- The `agent_task` handler (`_handle_agent_task()` in `chat/gemini_api/turn_tools.py`)
- `_run_parallel_sub_agents()` in `chat/gemini_api/sub_agent.py` (covers both `agent_task_parallel` and the template-rendered `agent_task_parallel_template` path)
- A defense-in-depth guard at the top of `_run_sub_agent()` in `chat/gemini_api/sub_agent.py`

On a disallowed model the call returns a clean, model-readable error tool result (no exception) suggesting an allowed alternative, so the model can re-spawn on the same turn. `agent_task_parallel_template` already excludes `gemini-3.1-pro-preview` structurally because it is absent from `TEMPLATE_BATCH_ALLOWED_MODELS`. The `agent_task` / `agent_task_parallel` `model` parameter descriptions in `chat/llm/tool_schemas.py` list the valid values (excluding Gemini 3.1 Pro) and note that it is not available to sub-agents.

### Nested Sub-Agents

When a conversation has the `nested_subagents` flag set (see [Conversation Flags](conversation-flags.md)), a **1st-level** sub-agent may spawn one tier of **2nd-level** (nested) sub-agents. The flag is threaded as `nested_subagents` into `run_conversation_turn()`, which passes `nested_enabled=True` into the `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` spawn arms; those forward it into `_run_sub_agent()`. There is no 3rd level.

`_run_sub_agent()` in `chat/gemini_api/sub_agent.py` carries `nested_enabled`, `level` (1 = spawned by the top-level agent, 2 = spawned by a 1st-level sub-agent), and `nested_parent_id` parameters:

- **Tool tier**: a 1st-level sub-agent with the flag on (`can_nest = nested_enabled and level == 1`) gets `SUB_AGENT_TOOLS_NESTED` (adds `agent_task_nested`); otherwise it gets plain `SUB_AGENT_TOOLS`. A 2nd-level sub-agent always gets `SUB_AGENT_TOOLS` -- it is a leaf with no spawner.
- **Nested-spawn dispatch**: the `agent_task_nested` branch in `_run_sub_agent()`'s function-call loop (analogous to the `agent_task_response` branch) resolves the required `model` (after the `_DEPRECATED_MODELS` remap), enforces the 2nd-level allow-list, and recursively calls `_run_sub_agent(..., level=2, nested_parent_id=<node id>)`. Nested usage is rolled into the 1st-level agent's accumulator so top-level totals capture it.
- **2nd-level model restriction**: nested sub-agents are restricted to `NESTED_SUB_AGENT_ALLOWED_MODELS = {"claude-haiku-4.5", "gemini-3.5-flash-lite"}` in `chat/gemini_api/constants.py` (the two cheapest/fastest models, for cheap leaf work). The same set is duplicated in `chat/llm/tool_schemas.py` (to avoid a circular import) for the `agent_task_nested` `model` enum; keep the two in sync.
  - Enforcement lives both in the nested-spawn dispatch branch (clean error tool result on a disallowed model, so the 1st-level agent self-corrects) and as a defense-in-depth guard at the top of `_run_sub_agent()` when `level == 2`. `gemini-3.1-pro-preview` stays excluded, consistent with `SUB_AGENT_DISALLOWED_MODELS`.
- **Per-layer system prompt**: `get_system_prompt(nested_subagents=...)` appends a short note to the top-level sub-agent guidance when on; `get_sub_agent_system_prompt(can_nest=...)` enumerates `agent_task_nested` and relaxes the "no spawning" rule for a can-nest 1st-level sub-agent, and keeps the original leaf-agent copy otherwise. See [System Prompt](#system-prompt).
- **UI surfacing**: the `agent_task_nested` call is emitted as a `sub_agent_tool_use` / `sub_agent_tool_result` NODE event carrying `nested_agent_id` / `nested_agent_name` / `nested_agent_model` (use) and `nested_agent_status` (result); the 2nd-level agent's own `sub_agent_*` events carry `nested_parent_id == nested_agent_id` so the FE nests the grandchild's tool calls one extra indent level under that node. See [Realtime Architecture](realtime.md#sub-agent-events).

### `_run_sub_agent()` Function Signature

`async _run_sub_agent(app, provider, user, conversation_id, timezone, model, agent_name, prompt, custom_prompt=None, project_id=None, project_guide="", skills_content="", usage_accumulator=None, on_event=None, parent_tool_id=None, nested_enabled=False, level=1, nested_parent_id=None) -> str`

| Parameter | Type | Description |
|---|---|---|
| `app` | FastAPI | App instance (for route dispatch) |
| `provider` | `LLMProvider` | The provider instance |
| `user` | `dict[str, Any]` | Authenticated user dict |
| `conversation_id` | `str` | Conversation UUID (for workspace access) |
| `timezone` | `str` | User's IANA timezone string |
| `model` | `str` | Model ID (any valid ID from the model registry, e.g., `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `claude-haiku-4.5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-opus-5-5`) |
| `agent_name` | `str` | Display name for this sub-agent |
| `prompt` | `str` | The task instructions |
| `custom_prompt` | `str \| None` | Optional user custom system prompt |
| `project_id` | `str \| None` | Optional project UUID for project-aware workspace resolution |
| `project_guide` | `str` | Optional project-specific instructions |
| `skills_content` | `str` | Pre-resolved auto-loaded skills content string from the parent (passed through to `get_sub_agent_system_prompt()`) |
| `usage_accumulator` | `dict \| None` | Optional dict for aggregating token usage (`input_tokens`, `output_tokens`, `cached_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `new_input_tokens`, `call_count`). When provided, per-turn usage from this sub-agent is accumulated into it for the parent to read. `new_input_tokens` is computed via `compute_new_input_tokens()` using the sub-agent's own provider |
| `on_event` | `Callable[[dict], Awaitable[None]] \| None` | Optional async callback for emitting `sub_agent_tool_use`, `sub_agent_tool_result`, and `sub_agent_finished` events. Passed from the parent conversation loop; in the persistent-WS run path it both fires the durable flush and mirrors transients to the per-conversation channel |
| `parent_tool_id` | `str \| None` | Tool ID of the parent `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` call. Included in emitted events so the frontend can nest sub-agent tool calls under the correct parent |
| `nested_enabled` | `bool` | Whether the conversation's `nested_subagents` flag is on. Only meaningful at `level == 1` (`can_nest = nested_enabled and level == 1`): a 1st-level sub-agent then gets `SUB_AGENT_TOOLS_NESTED` and the can-nest prompt copy. See [Nested Sub-Agents](#nested-sub-agents) |
| `level` | `int` | Nesting depth. `1` = spawned by the top-level agent; `2` = spawned by a 1st-level sub-agent (a leaf restricted to `NESTED_SUB_AGENT_ALLOWED_MODELS`, defense-in-depth guarded at the top of the function) |
| `nested_parent_id` | `str \| None` | Set only for a 2nd-level sub-agent: the id of the `agent_task_nested` NODE under which this grandchild's `sub_agent_*` events are grouped one extra indent level in the UI |

Returns the sub-agent's response as a string.

## Parallel Sub-Agent Execution

The `agent_task_parallel` tool allows the top-level model to spawn multiple sub-agents that run concurrently. Parallel execution is implemented in `_run_parallel_sub_agents()` in `chat/gemini_api/sub_agent.py`.

A template-based variant, `agent_task_parallel_template`, spawns sub-agents from a single `prompt_template` with `{var}`-style placeholders (Python `str.format()`). Each entry in the `agents` array provides a `name` and template variable values. The `model` parameter is required and restricted to cheaper models defined in `TEMPLATE_BATCH_ALLOWED_MODELS` in `chat/gemini_api/constants.py`. The maximum number of agents per call is `MAX_PARALLEL_TEMPLATE_TASKS` (20). Template rendering and validation are handled by `_run_parallel_sub_agents_template()` in `chat/gemini_api/sub_agent.py`, which renders prompts and delegates to `_run_parallel_sub_agents()` for execution.

### Parallel Execution Flow

1. The top-level model calls `agent_task_parallel(tasks=[...])` during the conversation loop
2. `_run_parallel_sub_agents()` validates the task list: rejects empty arrays, enforces `MAX_PARALLEL_TASKS = 10` (constant in `chat/gemini_api/constants.py`), checks for duplicate or empty task IDs
3. Each task is wrapped in a `run_one_task()` coroutine that calls `_run_sub_agent()` with the task's parameters
4. All coroutines are launched concurrently via `asyncio.gather()`
5. Individual task failures are caught within `run_one_task()` and returned with `status: "error"` instead of propagating -- a failing sub-agent does not affect other tasks in the batch
6. Results are assembled into a dict keyed by the caller-specified task `id`, with each value containing `status` ("success" or "error"), `name`, and either `response` (on success) or `error` (on failure)
7. The combined results are returned as a JSON string to the parent conversation loop as the `tool_result` for the `agent_task_parallel` call

### Validation Rules

- The `tasks` array must be non-empty
- Maximum of `MAX_PARALLEL_TASKS = 10` tasks per invocation
- Each task must have a non-empty `id` field
- Task IDs must be unique within the batch (duplicates are rejected)

### `_run_parallel_sub_agents()` Function Signature

`async _run_parallel_sub_agents(app, client, user, conversation_id, timezone, parent_model, tasks, custom_prompt=None, project_id=None, project_guide="", skills_content="", usage_accumulator=None, on_event=None, parent_tool_id=None) -> str`

| Parameter | Type | Description |
|---|---|---|
| `app` | FastAPI | App instance (for route dispatch) |
| `client` | `LLMProvider` | The provider instance |
| `user` | `dict[str, Any]` | Authenticated user dict |
| `conversation_id` | `str` | Conversation UUID (for workspace access) |
| `timezone` | `str` | User's IANA timezone string |
| `parent_model` | `str` | Parent agent's model ID (used as fallback when a task omits `model`) |
| `tasks` | `list[dict[str, Any]]` | List of task specification dicts |
| `custom_prompt` | `str \| None` | Optional user custom system prompt |
| `project_id` | `str \| None` | Optional project UUID for project-aware workspace resolution |
| `project_guide` | `str` | Optional project-specific instructions |
| `skills_content` | `str` | Pre-resolved auto-loaded skills content string from the parent (passed through to each sub-agent) |
| `usage_accumulator` | `dict \| None` | Optional dict for aggregating token usage (including `cached_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `new_input_tokens`) across all parallel tasks. Each task's `_usage` dict is merged into this accumulator after completion |

Returns a JSON string with a `results` object keyed by task ID.

### Frontend Display

The frontend renders both `agent_task_parallel` and `agent_task_parallel_template` calls in `frontend/src/components/ToolUseMessage.tsx` with a person icon (same as `agent_task`). The primary label shows "{N} parallel sub-agent(s)" (e.g., "3 parallel sub-agents") and the secondary label lists the sub-agent names joined by commas (e.g., "Email Researcher, Calendar Checker, Data Analyst"). The names come from `tool_input.tasks[].name` for the parallel variant and `tool_input.agents[].name` for the template variant.

### Sub-Agent Tool Call Tree Display

Sub-agent tool calls are displayed as an expandable tree structure nested under their parent `agent_task`, `agent_task_parallel`, or `agent_task_parallel_template` tool call in the UI. This gives users visibility into what sub-agents are doing without cluttering the main conversation stream.

**Display behavior:**
- For `agent_task` (single sub-agent): tool calls are listed directly under the parent, each expandable to show intent, input parameters, and output
- For `agent_task_parallel` and `agent_task_parallel_template` (multiple sub-agents): tool calls are grouped by agent name into expandable `SubAgentSection` entries, each showing a `RUNNING` / `COMPLETED` / `ERRORED` status indicator and containing that agent's tool calls. Both spawners share the same grouped-children code path in `ToolUseMessage.tsx` (the `isAnyParallel` branch), differing only in whether the expected-agent list is read from `tasks[]` or `agents[]`
- Sub-agent tool calls appear below the parent tool call whether the parent is collapsed or expanded
- During streaming, tool calls appear in real-time as `sub_agent_tool_use` and `sub_agent_tool_result` events arrive
- The per-row and outer-group `COMPLETED` / `ERRORED` badges are driven by `sub_agent_finished` events (tracked in `subAgentReturned` in `frontend/src/store/conversationStore.ts`), not by whether the last inner `tool_result` has arrived. See [Frontend Architecture](frontend.md) for the rendering logic
- The `subAgentToolCalls` and `subAgentReturned` maps are both cleared when streaming ends (`clearSubAgentToolCalls()`)

**Persistence and hydration:**
- Sub-agent tool call and finished events are captured by `_capturing_on_event()` in `chat/gemini_api/conversation.py` during execution, keyed by `parent_tool_id` (the captured types are `sub_agent_tool_use`, `sub_agent_tool_result`, and `sub_agent_finished`)
- When a `tool_result` is saved for an `agent_task`, `agent_task_parallel`, or `agent_task_parallel_template` call, the captured events are attached as `sub_agent_tool_calls` metadata on the `tool_result` structured message in `chat_history.json`
- The sub-agent data is NOT included in the LLM message history (SDK history) -- it is display-only metadata
- On conversation reload, `hydrateSubAgentToolCalls()` in `frontend/src/hooks/useConversation.ts` reconstructs both the `subAgentToolCalls` Map and the `subAgentReturned` Map from persisted `tool_result` messages that carry the `sub_agent_tool_calls` array, so the terminal badge state survives a page reload. Conversations persisted before `sub_agent_finished` was introduced fall back to treating a present parent `tool_result` as `COMPLETED` (logic in `ToolUseMessage.tsx`)

## Conversation Loop

`run_conversation_turn()` in `chat/gemini_api/conversation.py` is the main entry point called from `chat/realtime/socket.py:_run_send_message`, the headless wait-handle resume in `chat/wait_handles/resume.py:_run_resume`, and the Slack-driven runtime in `chat/slack_socket_mode.py:_dispatch_slack_model_run`. It runs a single conversation turn (user message in, assistant response out, with any number of tool call rounds).

### Function Signature

`async run_conversation_turn(app, user, message, conversation_id, timezone, model, on_event, messages_out=None, guide_id=None, project_id=None, skill_ids=None, origin="web", slack_context=None, attachments=None, flags=None) -> list[dict]`

| Parameter | Type | Description |
|---|---|---|
| `app` | FastAPI | App instance (passed to `execute_tool_call()` for route matching) |
| `user` | `dict[str, Any]` | Authenticated user dict with `id`, `email`, and `api_key` keys |
| `message` | `str` | User's message text |
| `conversation_id` | `str` | Conversation UUID |
| `timezone` | `str` | User's IANA timezone string |
| `model` | `str \| None` | Model ID (any valid ID from the model registry), or `None` to use `server_config.json` default |
| `on_event` | `Callable[[dict], Awaitable[None]]` | Async callback. In the persistent-WS path it fires the durable flush (which advances seq, writes disk, and publishes `message_appended`) on `FLUSH_EVENT_TYPES` and mirrors transient events (`text`, `sub_agent_*`, `tool_started`) to the per-conversation channel via `_publish_transient_event` |
| `messages_out` | `list[dict] \| None` | Optional shared list for partial-result and suspend recovery (see below) |
| `guide_id` | `str \| None` | Guide UUID from the frontend, or `None` to use the default guide. On first message, the resolved guide is snapshotted into `chat_history.json`. On subsequent messages, the snapshot is used instead |
| `project_id` | `str \| None` | Optional project UUID. When present, the project guide is loaded from `db/project_store.py` and injected into the system prompt, and workspace file tools resolve to the shared project workspace |
| `skill_ids` | `list[str] \| None` | Optional list of skill UUIDs to load into the conversation. When present, skill contents are resolved via `get_accessible_skills_by_ids()` and injected as a `<conversation_skills_loaded>` XML section in the user message envelope. IDs are persisted to `loaded_skills.json` via `ChatStorage.add_loaded_skill_ids()` |
| `origin` | `str` | Where this turn is being driven from. `"web"` (default) runs with `TOP_LEVEL_TOOLS` and the normal system prompt. `"slack"` runs with `SLACK_TOP_LEVEL_TOOLS` (adds `send_slack_reply_and_get_response`) and appends the Slack Reply Mode section to the system prompt. See [Slack Socket Mode](slack-socket-mode.md) |
| `slack_context` | `dict \| None` | Required when `origin="slack"`: `{"channel_id": str, "thread_ts": str}`. Consumed by the `send_slack_reply_and_get_response` tool dispatch arm to post the reply and register the slack_reply wait handle |
| `flags` | `list[str] \| None` | Per-conversation opt-in flags read off the conversation row by the caller (NULL/empty == no flags). Resolved via `is_flag_enabled()`; the `nested_subagents` flag is threaded as `nested_subagents=...` into `get_system_prompt()` and into the `agent_task` / `agent_task_parallel` / `agent_task_parallel_template` spawn arms (as `nested_enabled`). See [Conversation Flags](conversation-flags.md) and [Nested Sub-Agents](#nested-sub-agents) |

Returns a list of structured message dicts for persistence in `chat_history.json`.

**`messages_out` parameter**: When provided, every structured message is appended to this list *in addition to* the internal list. This lets the caller (`_run_send_message` in `chat/realtime/socket.py`, the Slack dispatch task in `chat/slack_socket_mode.py`, and the headless resume in `chat/wait_handles/resume.py`) recover partial results when the coroutine is cancelled via `asyncio.CancelledError` *or* when one of the suspend sentinels (`SuspendForWaitHandles`, `SuspendForSlackReply`, `SuspendForActionRequest`) unwinds the function without a normal return.

If `None`, the function creates its own internal list. The shared list is the same object used for the return value, so appending to it during execution gives the caller a live view of progress.

### Loop Flow

The main conversation loop has no turn limit -- it continues until the model finishes responding (no more function calls). A `turn_count` variable tracks the number of loop iterations for observability and is included in the `stats` event emitted at the end of each conversation turn.

Sub-agents have model-specific turn limits determined by `get_sub_agent_turn_limits(model)` in `chat/gemini_api/constants.py`: 200K-token models (Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7) get 20 turns max with a warning at 15, while 1M-token models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5) and unknown models get the default 60 turns max with a warning at 50. The main user-facing conversation is not artificially capped.

```
1. Load config and create/retrieve chat session
   - load_server_config() from config/server_config.py
   - Resolve guide content:
     a. Check if conversation already has a snapshotted guide via ChatStorage.get_guide_snapshot()
     b. If snapshot exists: use snapshotted content (preserves guide across edits/deletions)
     c. If no snapshot (first message): resolve from guide_id parameter, fall back to default guide via
        get_guide() / get_default_guide() / ensure_default_guide() from db/guide_store.py
     d. Snapshot the resolved guide into chat_history.json via ChatStorage.set_guide_snapshot()
   - If project_id is present, load project data via get_project() from db/project_store.py
     and extract the project guide text
   - Resolve auto-loaded skills: call get_user_autoloaded_skills() from db/skill_store.py;
     if project_id is present, also call get_project_autoloaded_skills() and merge with
     deduplication by skill ID; build skills content string (NOT snapshotted -- resolved fresh
     on every message)
   - Resolve conversation-loaded skills (if skill_ids provided): call
     get_accessible_skills_by_ids(user_id, skill_ids) from db/skill_store.py,
     build loaded_skills_content string for injection into user message envelope
   - get_user_connected_services(user) from api.instructions to determine connected services
   - get_system_prompt(..., custom_system_prompt=resolved_guide_content, connected_services=...,
     project_guide=..., skills_content=resolved_skills_content) to build filtered system prompt
   - Always load saved SDK history from disk via _load_sdk_history() (returns
     (history, provider_name) tuple or None); this is needed both for session
     creation and as a fallback when an in-memory session is discarded on model change
   - If no in-memory session exists and disk history provider matches target provider:
     use disk history as primary history
   - get_or_create_chat(..., history=history, disk_history=disk_history,
     disk_provider=disk_provider) for the (user_id, conversation_id) key
   - Persist the effective model to the conversation record via
     update_conversation_model(conversation_id, model) from db/conversation_store.py
     (no-op if the conversation already has a model set; best-effort, non-fatal on error)

2. Set current_message = user's text message

3. LOOP (no turn limit; turn_count tracks iterations for stats):
   a. Stream response via chat.send_message_stream(current_message)
      - For each chunk, extract text parts and function call parts
      - Text parts: emit {"type": "text", "content": "..."} via on_event
      - Function calls: collected for batch execution after streaming completes
      - Track usage_metadata from the last chunk for stats
      - On CancelledError: flush accumulated text_parts into structured_messages
        so the caller can persist the partial response, then re-raise
      - If a ClientError (INVALID_ARGUMENT) is raised and the message contains
        file_data parts, strip the file URI parts, replace with error text parts,
        and retry the turn (see MIME Type Rejection Recovery below)

   a2. Record per-turn usage to the database via record_api_call() from
       db/llm_call_store.py with call_type=ApiCallType.TOP_LEVEL, the model
       name, the provider-native raw_usage fields, and turn duration in
       milliseconds. Failures are caught and logged as warnings

   b. Save accumulated text as structured message (if non-empty)

   c. If no function calls -> BREAK (turn complete)

   d. Execute each function call:
      - Assign tool_id: use the SDK-provided fc.id if present, otherwise generate
        "{tool_name}_{uuid_hex8}" via uuid.uuid4() for global uniqueness across turns
      - Extract intent_message from args (popped before execution, truncated to 50 chars)
      - Emit {"type": "tool_use", ..., "tool_id": "...", "intent_message": "..."} via on_event
      - Save tool_use structured message (includes intent_message)
      - If tool is "agent_task": count how many agent_task calls have occurred so far in this
        model response; if the count exceeds MAX_AGENT_TASKS_PER_TURN (10), return a JSON error
        string directing the model to use agent_task_parallel instead. Otherwise, log [agent_task]
        spawn with agent name, model, conversation ID, and user;
        extract agent_model = args.get("model") or model (falls back to parent's model),
        then call _run_sub_agent(app, client, user, conversation_id, timezone, agent_model, name, prompt, on_event=on_event, parent_tool_id=tool_id)
        wrapped in try/except -- on success, log [agent_task] completion with duration and response length;
        on failure, log [agent_task] failure with duration; errors are returned as JSON error strings, not raised
      - If tool is "wait_for_handles" (top-level only, web origin only): call
        _await_wait_for_handles(), which validates inputs and either short-circuits
        with the DB-derived payload (when all handles are already resolved) or
        raises SuspendForWaitHandles. SDK history is already on disk via the
        boundary save fired by the wait_for_handles tool_use event (see SDK
        History Persistence). The top-level loop catches the sentinel and
        returns; the dangling tool_use is closed by the resume bucket on the
        next run. See [Wait Handles Architecture](wait-handles.md)
      - If tool is "agent_task_parallel": log [agent_task_parallel] spawn with task count, task names, conversation ID, and user;
        extract tasks list from args,
        then call _run_parallel_sub_agents(app, client, user, conversation_id, timezone, model, tasks)
        wrapped in try/except -- on success, log [agent_task_parallel] completion with task count and duration;
        on failure, log [agent_task_parallel] failure with duration; errors are returned as JSON error strings, not raised
      - If tool is "agent_task_parallel_template": log [agent_task_parallel_template] spawn with agent count, agent names, model, conversation ID, and user;
        extract prompt_template, model, and agents from args,
        then call _run_parallel_sub_agents_template(app, provider, user, conversation_id, timezone, prompt_template, model, agents)
        wrapped in try/except -- on success, log [agent_task_parallel_template] completion with agent count and duration;
        on failure, log [agent_task_parallel_template] failure with duration; errors are returned as JSON error strings, not raised
      - If tool is "tool_call" with tool_name="wait_for_handles": intercept before normal
        tool_call dispatch and run _await_wait_for_handles() (same select-style block as the
        direct wait_for_handles tool call above)
      - Otherwise: call _dispatch_tool_call(app, client, user, conversation_id, timezone, fc.name, args)
        which handles tool_call (dispatching to handlers for all TOOL_CALL_REGISTRY tools), backward-compatibility
        direct-name paths, load_gmail_attachment, skill tools, run_script, run_python, and falls back to
        execute_tool_call() from route_dispatch for HTTP tools (curl_proxy_get, curl_proxy_post).
        Returns (result, extra_parts). For HTTP tools, notices from ToolResultWithNotices are converted
        to text extra_parts via provider.make_text_part()
      - Emit {"type": "tool_result", ...} via on_event
      - Save tool_result structured message
      - Build types.Part.from_function_response() for the SDK
      - Append any extra_parts after the function response part

   e. Set current_message = list of function response parts (including any extra_parts)
   f. CONTINUE LOOP (model processes tool results and may issue more calls)

4. Emit {"type": "stats", "stats": {...}} with timing, token counts accumulated across ALL tool-call turns (combined and broken down by top-level vs sub-agent, including cached token counts, cache creation/read breakdown, and new input tokens), provider name, tool call count, turn count, and sub-agent call count. The stats event is in FLUSH_EVENT_TYPES, so the boundary save in _capturing_on_event persists the final SDK session history to sdk_history.json -- there is no separate end-of-loop _save_sdk_history() call

5. Return structured_messages list
```

### Streaming Behavior

The response is streamed chunk-by-chunk via `chat.send_message_stream()`. Each chunk may contain text parts (streamed to the frontend immediately via `on_event`) or function call parts (collected and executed after the stream completes). This means text appears in real-time on the frontend, while tool calls are batched per streaming response.

The module checks for `chunk.candidates[0].content.parts` defensively -- chunks without candidates or content are skipped. This handles edge cases where the SDK emits metadata-only chunks.

## Logging

The `chat` logger is configured at INFO level by the unified logging configuration in `chat/logging_config.py`. All `chat.*` submodule loggers inherit this level. The log format includes colored level labels, timestamps, process IDs, and module names. See [Logging Architecture](logging.md) for the full configuration details.

Each submodule uses `logger = logging.getLogger(__name__)` (which resolves to names like `chat.gemini_api.conversation`, `chat.gemini_api.sub_agent`, etc.) and logs the following events. All user-driven log entries include `user=<email>` for per-user filtering (see [Logging Architecture](logging.md) for the convention).

- **`[agent_task]` spawn**: Logged when a single sub-agent is spawned, including agent name, model, conversation ID, and user email
- **`[agent_task]` completion/failure**: Logged when a single sub-agent finishes, including agent name, duration in milliseconds, conversation ID, user email, and response length (on success) or exception (on failure)
- **`[agent_task_parallel]` spawn**: Logged when parallel sub-agents are spawned, including task count, task names, conversation ID, and user email
- **`[agent_task_parallel]` completion/failure**: Logged when parallel sub-agents finish, including task count, duration in milliseconds, conversation ID, and user email
- **`[agent_task_parallel_template]` spawn**: Logged when template-based parallel sub-agents are spawned, including agent count, agent names, model, conversation ID, and user email
- **`[agent_task_parallel_template]` completion/failure**: Logged when template-based parallel sub-agents finish, including agent count, duration in milliseconds, conversation ID, and user email
- **`[sub-agent:<name>]` tool call**: Logged for every internal tool call within a sub-agent, including the tool name, turn number, conversation ID, and user email
- **`[sub-agent:<name>]` tool result**: Logged when a sub-agent tool call completes, including the tool name, duration in milliseconds, conversation ID, user email, and result length
- **`[sub-agent:<name>]` agent_task_response**: Logged when a sub-agent terminates via `agent_task_response`, including conversation ID, user email, and response length
- **Sub-agent turn warning injected**: Logged at INFO level when the turn warning is injected (at the model-specific warning threshold and each turn thereafter), including agent name, current turn, max turns, conversation ID, and user email
- **Sub-agent context window warning injected**: Logged at INFO level when the context window warning is injected (at `SUB_AGENT_CONTEXT_WARNING_THRESHOLD = 0.75` and each turn thereafter while above the threshold), including agent name, current context tokens, max context tokens, usage percentage, conversation ID, and user email
- **Sub-agent turn limit exceeded**: Logged at WARNING level when a sub-agent exceeds its model-specific turn limit, including agent name, conversation ID, and user email
- **Top-level error**: Logged via `logger.exception()` when `run_conversation_turn()` fails, including user email and conversation ID

## Structured Message Format

The function returns a list of message dicts matching the format used by `chat_history.json`:

| Type | Fields |
|---|---|
| `text` | `type`, `role` ("assistant"), `content`, `timestamp` |
| `tool_use` | `type`, `role` ("assistant"), `tool_name`, `tool_input`, `tool_id`, `intent_message`, `timestamp` |
| `tool_result` | `type`, `role` ("assistant"), `tool_id`, `tool_output`, `timestamp`, optional `sub_agent_tool_calls` (array of persisted sub-agent events, present on `agent_task`/`agent_task_parallel`/`agent_task_parallel_template` results) |
| `stats` | `type`, `stats` (dict with `input_tokens`, `output_tokens`, `cached_tokens`, `new_input_tokens`, `provider`, `cache_creation_tokens`, `cache_read_tokens`, `duration_ms`, `tool_calls`, `turns`, `top_level_input_tokens`, `top_level_output_tokens`, `top_level_cached_tokens`, `top_level_new_input_tokens`, `top_level_cache_creation_tokens`, `top_level_cache_read_tokens`, `sub_agent_input_tokens`, `sub_agent_output_tokens`, `sub_agent_cached_tokens`, `sub_agent_new_input_tokens`, `sub_agent_cache_creation_tokens`, `sub_agent_cache_read_tokens`, `sub_agent_call_count`), `timestamp` |

All timestamps are generated via `utc_timestamp()` from `chat/storage.py`.

## Error Handling

The entire conversation loop is wrapped in a `try/except Exception` block in `run_conversation_turn()`. On error:

1. The full traceback is logged via `logger.exception()`
2. An `{"type": "error", "error": traceback_string}` event is emitted to the frontend via `on_event`
3. The exception is logged by the persistent-WS run task body (`chat/realtime/socket.py:_run_send_message`); the task body's `finally` runs the final flush and publishes `send_message_finished` regardless

Tool call execution errors are handled separately by `execute_tool_call()` in `chat/route_dispatch.py`, which **never raises** -- it returns error strings as tool results so the model can see and react to failures.

### MIME Type Rejection Recovery

The streaming call `chat.send_message_stream()` in the conversation loop is wrapped in a `ClientError` catch (`google.genai.errors.ClientError`) that detects MIME type rejections from the Gemini content generation API. When a file is uploaded via the File Upload API and referenced via `Part.from_uri`, the content generation API may reject it with a 400 `INVALID_ARGUMENT` error for unsupported MIME types. The recovery logic in `chat/gemini_api/conversation.py`:

1. Checks if the error is an `INVALID_ARGUMENT` and the current message contains `file_data` parts
2. If so, strips all `Part.from_uri` file parts from the message and replaces each with a `Part.from_text` error note advising the model to use `run_script` or `run_python` instead
3. Retries the turn with the cleaned message -- the SDK did not record the failed message in chat history (history recording runs only after full iteration), so retrying with modified parts is safe
4. If the error is not a file-part MIME rejection, it re-raises to the outer handler

This recovery mechanism is a safety net for cases that slip past the pre-upload deny-list check in `_handle_get_workspace_file()` and `_handle_load_gmail_attachment()`. The pre-check in tool handlers (via `_UNSUPPORTED_GEMINI_MIME_TYPES`) prevents most unsupported uploads, but the `ClientError` catch in the conversation loop handles edge cases where the MIME type was not on the deny-list but the API still rejects it.

## Chat Route Wiring

The Gemini API module is invoked from `chat/realtime/socket.py:_run_send_message` (the persistent-WS run task body). The legacy per-turn endpoint `chat/routes/websocket.py` and the `_handle_api_mode` dispatcher were removed in devplan 00062.

### `_run_send_message` Behavior

The persistent-WS run task (`chat/realtime/socket.py`):

1. Acquires the per-conversation slot in `_active_send_runs` (rejects with `send_message_rejected` on contention).
2. Appends the user message via `ChatStorage.append_message` (which stamps `seq`, advances `last_message_seq`, and publishes `message_appended` via `_publish_appended_to_bus`) so onlooker tabs see the bubble immediately.
3. Creates a `shared_messages` list and passes it as `messages_out` to `run_conversation_turn()`.
4. Builds the flush callback via `chat/_flush_helper.py:make_flush_callback` -- which both writes to disk AND publishes `message_appended` for each newly stamped `(seq, message)` pair.
5. Wraps `on_event` so durable events fire the flush and transient events (`text`, `sub_agent_*`, `conversation_updated`, `tool_started`, errors) are mirrored to the per-conversation channel via `_publish_transient_event`.
6. Runs `run_conversation_turn()`. On `CancelledError` (from `_handle_stop`), calls `_save_interrupted_sdk_history()` (tail edit on the on-disk SDK history envelope, skipped when the last entry already ends on a dangling tool_use), `remove_chat_session()`, and `cancel_pending_wait_handles_for_conversation()`.
7. In the `finally` block, appends an `"interrupted"` marker if stop was requested, runs the final flush, and publishes `send_message_finished` (with `interrupted: bool`) so subscribers can drain text buffers and clear spinners.

The same flush callback from `_flush_helper.py` is also used by the headless wait-handle resume (`chat/wait_handles/resume.py:_run_resume`) and the Slack-driven runtime (`chat/slack_socket_mode.py:_dispatch_slack_model_run`), so durable events fan out automatically from those paths too -- this is what lets a resumed run push its continuation to a viewing tab and a Slack-driven conversation stream live in the web UI without a reload.

### Workspace Creation

The `create_conversation` endpoint in `chat/routes/conversations.py` does not create a workspace directory up front: the SDK manages sessions in memory, and the `workspace/` subdirectory is created lazily by the workspace file tools when first needed.

## Design Decisions

**Why disable automatic function calling (AFC)?**
The SDK supports AFC where it executes tool functions automatically. The module disables this (`automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)`) to: (a) stream `tool_use` and `tool_result` events to the frontend for real-time display, (b) enforce URL validation via route dispatch before execution, (c) control error handling so tool failures don't crash the conversation.

**Why in-memory sessions with disk-backed history?**
In-memory sessions provide fast access during normal operation since the server runs continuously. On restart, the SDK session history is loaded from `sdk_history.json` and passed to `chats.create(history=...)` to reconstruct the session with full prior context. This combines the simplicity of in-memory caching with the durability of disk persistence.

**Why serialize SDK history via `model_dump_json(exclude_none=True)` instead of `json.dumps()`?**
The SDK's `Content` objects contain proto wrapper types and internal objects that `json.dumps()` cannot handle directly. Pydantic's `model_dump_json(exclude_none=True)` correctly serializes all fields (including nested `Part` objects with function calls, function responses, and file references) and produces compact JSON. The `exclude_none=True` flag avoids persisting null fields, keeping the file small and ensuring `Content.model_validate()` can round-trip the data cleanly on reload.

**Why save SDK history at every flush-event boundary instead of only at end-of-turn?**
A single end-of-turn save is too late for the suspend paths: a Slack-driven run that suspends inside `send_slack_reply_and_get_response` for hours, a `wait_for_handles` block that suspends until the user resolves a handle, and a `create_action_request` call that suspends until the user resolves the inline card. All three end the in-memory turn at a `tool_use` rather than a clean stop, so a server restart between the suspension and the resume would lose the dangling-tool_use shape the resume bucket logic depends on.

Tying the SDK save to the existing `chat_history.json` flush events (`tool_use`, `tool_result`, `action_request`, `stats`) yields a single invariant -- both files advance together at every meaningful boundary -- and lets the cancel-time helper (`_save_interrupted_sdk_history`) be a small tail edit on the on-disk envelope rather than a provider-branched rebuild. The cost is a compact JSON write per flush event, negligible compared to API call latency.

**Why a separate `sdk_history.json` instead of reusing `chat_history.json`?**
The two files serve different purposes and have different formats. `chat_history.json` stores structured messages for frontend display (with types like `text`, `tool_use`, `tool_result`, `stats`). `sdk_history.json` stores the SDK's internal `Content` objects (with `parts`, `role`, and proto-level fields) needed to reconstruct the chat session. Merging them would require a lossy translation layer in both directions.

**Why `(user_id, conversation_id)` as the session key?**
This ensures user isolation -- one user cannot access another user's chat session even if they know the conversation ID. It also allows the same user to have multiple concurrent conversations with independent session state. The integer `user_id` (auto-incrementing primary key) is used instead of the email string for compactness and consistency with the database schema.

**Why batch tool calls per streaming response?**
The Gemini SDK may return multiple function calls in a single response. The module collects all function calls from one streaming response, executes them sequentially, and sends all function results back to the model as a single message. This matches the SDK's expected protocol for multi-tool responses.

**Why a shared `messages_out` list instead of returning from the function?**
The function unwinds without returning normally on cancellation (`CancelledError`) and on the suspend sentinels (`SuspendForWaitHandles`, `SuspendForSlackReply`, `SuspendForActionRequest`). The shared list lets the caller (`_run_send_message` on the persistent WS, the Slack dispatch task, the headless resume) inspect whatever messages were accumulated before unwind and persist them to `chat_history.json`. Without this, partial responses would be lost on interruption and the suspend sentinels could not flush their final state.

**Why silently ignore publish failures during the model loop?**
The conversation loop runs in-process on the persistent WS task and should complete so that (a) the model's response is persisted and (b) the SDK session history stays consistent. The `on_event` wrapper that mirrors transient events to the bus and the `_publish_appended_to_bus` helper that fans out `message_appended` both swallow exceptions -- a publish failure does not roll back the disk write, and a closed socket on the way out does not interrupt the loop.

**Why discard the SDK session on cancellation?**
When a conversation turn is cancelled mid-stream, the SDK session's internal history contains a partial exchange (the user message was sent but the model response is incomplete). Continuing from this state on the next message would confuse the model. Discarding the session via `remove_chat_session()` forces a fresh start from `sdk_history.json`, which now includes the partial response with an interruption marker (see below).

**Why distinguish same-provider and cross-provider model switches?**
When a user changes models mid-conversation, the old session must be replaced. Previously, all model switches discarded history unconditionally, starting the new session with no context. This caused context loss when following up on scheduled routine conversations (where the routine's configured model might differ from the user's selected model).

The fix distinguishes two cases: same-provider switches (e.g., Haiku to Sonnet) preserve the in-memory history because both models use the same message format; cross-provider switches (e.g., Gemini to Claude) cannot reuse in-memory history due to format incompatibility, but fall back to disk history (`sdk_history.json`) if the persisted history's provider matches the new model's provider. Disk history is always loaded eagerly in `run_conversation_turn()` and passed to `get_or_create_chat()` as `disk_history`/`disk_provider` so it is available for this fallback.

**Why preserve interrupted messages in SDK history?**
Before this feature, cancelling a streaming response discarded both the in-memory session and any record of what the model had generated. On the next message, the model had no awareness that it had previously started a response.

Now, the boundary save keeps `sdk_history.json` current at every flush event, and `_save_interrupted_sdk_history()` in `chat/routes/_helpers.py` performs a tail edit on the on-disk envelope -- appending partial assistant text from `shared_messages` plus an interruption marker (`"[Response interrupted by user before completion]"`) to the last assistant entry's last text part/block (or as a fresh text part if none exists). The marker is appended to the assistant's own message rather than added as a separate user message to maintain strict user/assistant role alternation required by the Anthropic API.

When the conversation resumes, the next session is rebuilt from this augmented `sdk_history.json`, giving the model full context of what it previously generated and explicit knowledge that the response was stopped.

**Why flush partial text in CancelledError handlers at multiple layers?**
Cancellation can arrive at any point during streaming. The Gemini conversation loop in `chat/gemini_api/conversation.py` catches `CancelledError` to flush accumulated `text_parts` into `structured_messages` before re-raising. The Anthropic provider in `chat/llm/anthropic_provider.py` catches `CancelledError` to flush any in-progress text block and append partial `assistant_content_blocks` to `session.messages` before re-raising.

These complementary handlers ensure that partial content is captured regardless of which layer the cancellation interrupts -- the conversation loop level captures text for `chat_history.json` persistence, while the provider level captures content for SDK history serialization.

**Why a per-backend client cache?**
The `genai.Client` holds an httpx connection pool that chat sessions reference. Creating a new client per request would orphan existing sessions. `GeminiProvider._get_client()` caches one client so all sessions share a connection pool and remain usable across messages. See [LLM Provider Abstraction -- GeminiProvider](llm-providers.md#geminiprovider).

**Why is `get_current_time` a local tool instead of an HTTP endpoint?**
The tool only needs `datetime`, `time`, and the user's timezone string (already passed as a parameter to `run_conversation_turn()`). Routing it through HTTP dispatch would add unnecessary complexity -- there is no existing endpoint to call, and creating one would require auth plumbing for a simple clock read. Executing it inline in the conversation loop (`_handle_get_current_time()` in `chat/gemini_api/tool_handlers/misc.py`) keeps it zero-dependency (stdlib only: `datetime`, `time`, `zoneinfo`) and avoids adding new routes or dependencies.

**Why are the workspace file tools local instead of using the File Browser API endpoints?**
The File Browser API endpoints in `chat/file_routes.py` are designed for the frontend (list, upload, download with HTTP responses). The workspace file tools serve a different purpose: giving the LLM direct access to workspace files.

The `get_workspace_file` tool uploads binary/large files via the provider's `upload_file()` method -- Gemini inlines images and PDFs as `types.Part.from_bytes()` parts on Vertex, and Anthropic base64-encodes supported images and PDFs as inline content blocks. For unsupported MIME types, `upload_file()` returns `None` and the calling code handles the fallback.

The `write_workspace_file` tool writes text files directly to the workspace directory with path traversal protection and a 1MB size limit; it silently overwrites existing files, and on success it publishes a `file_list_changed` per-user global so open file browsers silent-refresh mid-turn (see [Realtime -- Backend publish sites](realtime.md#backend-publish-sites-per-user-globals)). The `edit_workspace_file` tool modifies existing text files in place via exact string replacement with the same protections plus a read-before-edit gate (see the tool description above).

Implementing these as local tools avoids adding provider-specific logic to the REST endpoints and keeps the file upload integration co-located with the other LLM code in the `chat/` package.

**Why the 100KB inline threshold for text files?**
Small text files (under `_TEXT_INLINE_LIMIT = 100KB`) are returned directly in the tool response JSON, avoiding the overhead of a Gemini File Upload API round trip. The 100KB threshold is a pragmatic trade-off: large enough to cover most source files and config files, small enough to avoid bloating the context window with very large text blobs. Files above this size are uploaded via the File API regardless of type.

**Why a MIME type deny-list instead of an allow-list for file uploads?**
The Gemini File Upload API accepts a wide range of file types, but the content generation API (`send_message_stream`) rejects certain MIME types with a 400 `INVALID_ARGUMENT` error when they are referenced via `Part.from_uri`. This mismatch is dangerous because the upload succeeds but the subsequent generation call fails, killing the conversation thread.

The deny-list (`_UNSUPPORTED_GEMINI_MIME_TYPES` in `chat/gemini_api/constants.py`) targets specific categories known to fail: Office/OpenXML (.docx, .xlsx, .pptx, .doc, .xls, .ppt), OpenDocument (.odt, .ods, .odp), archives (.zip, .rar, .7z, .gz, .tar), and executables. An allow-list would be fragile because Gemini supports many MIME types and the supported set may expand. The deny-list approach lets new supported types work automatically while blocking known-bad ones. The `ClientError` recovery in `conversation.py` serves as a safety net for MIME types not yet on the deny-list.

**Why pre-check MIME types before uploading instead of only catching errors after?**
Uploading a file to the Gemini File Upload API and then having `send_message_stream` reject it wastes time (upload latency + API call latency) and produces a `ClientError` that requires recovery logic (stripping file parts and retrying). Pre-checking against the deny-list avoids both the wasted upload and the error recovery path. The tool handler returns a structured error JSON with a `suggestion` field that guides the model toward `run_script` or `run_python` with python-docx/openpyxl, which is a more reliable path for Office documents.

**Why pre-flight file size checks in the tool handlers instead of relying on provider-level caps?**
A file attached to the request is persisted in session history and replayed on every subsequent turn, so an oversized part that blows the provider's request-payload limit (e.g. the Anthropic-on-Vertex 30MB ceiling) does not just fail one call -- it permanently breaks the conversation. The pre-flight checks in `_handle_get_workspace_file()` and `_handle_load_gmail_attachment()` reject the file before any bytes attach, turning the failure into a normal tool error with an actionable `suggestion` the model can act on in the same turn.

The caps live in `chat/llm/file_limits.py` as the single source of truth and are resolved per the model actually being called (sub-agent models included); unknown or empty model strings fall back to the smallest cap so a model-threading bug can never reproduce the oversized-payload blow-up. The provider-level caps (`AnthropicProvider.upload_file()` document cap, the Gemini-Vertex inline cap) remain as defense-in-depth for callers that bypass the tool handlers, e.g. composer attachments -- see [LLM Provider Abstraction -- Attachment Size Limits](llm-providers.md#attachment-size-limits). Behavior is covered by `tests/test_workspace_file_size_limits.py`.

**Why use `extra_parts` alongside function responses?**
The Gemini SDK expects function results as `Part.from_function_response()` objects. However, uploaded file references (`Part.from_uri`) cannot be embedded inside a function response -- they must be separate parts in the same message. The `extra_parts` mechanism allows `_handle_get_workspace_file()` and `_handle_load_gmail_attachment()` to return both the JSON result (wrapped in a function response) and the file reference part.

For Gemini, these extra parts are appended after the function response in the `function_response_parts` list. For Anthropic, `format_tool_results()` converts the `tool_result` content to an array format (text block + inline content block dicts) when `extra_parts` are present. The mechanism is provider-agnostic at the tool handler level -- each provider's `format_tool_results()` handles the integration.

**Why are the memory tools local instead of using the memory REST endpoints?**
The memory REST endpoints in `chat/memory_routes.py` are designed for the frontend (CRUD operations with HTTP request/response lifecycle). The agent memory tools serve a different purpose: giving the LLM read-only access to the user's memories during a conversation. The handlers `_handle_memory_search()` and `_handle_memory_list()` call `search_memories()` and `list_memories()` directly from `db/memory_store.py`, avoiding the overhead of HTTP dispatch and keeping the interface minimal (search and list only, no create/update/delete). This follows the same pattern used for workspace file tools.

**Why extract `_dispatch_tool_call()` as a shared function?**
Both `run_conversation_turn()` and `_run_sub_agent()` execute the same set of shared tools (local tools and HTTP tools via route dispatch). Previously, each function had its own if/elif chain for these tools, duplicating the dispatch logic. Extracting `_dispatch_tool_call()` eliminates this duplication and ensures that adding a new shared tool (e.g., `memory_search`, `memory_list`) requires changes in only one place. Agent-specific tools (`agent_task`, `agent_task_parallel`, `agent_task_response`) remain in their respective loops because they require loop-specific context (spawning, logging, termination).

**Why a meta `tool_call` tool instead of registering every tool individually?**
Each `FunctionDeclaration` registered with the LLM consumes context tokens for its schema (name, description, parameters). As the tool count grows, this schema overhead becomes a meaningful fraction of the context budget. The meta tool pattern moves tool documentation from function declaration schemas into the system prompt, where it is more compact (plain text vs. structured schema) and benefits from prompt caching (Anthropic caches the system prompt; Gemini caches stable context).

A single `tool_call` declaration replaces N individual declarations, saving context space proportional to the number of converted tools. The LLM learns about available dynamic tools from the auto-generated "Dynamic Tools" section in the system prompt. Backward compatibility is maintained by keeping the old direct-name dispatch branches in `_dispatch_tool_call()` so cached sessions that still reference the old tool names continue to work.

**Why split tool declarations into three tiers?**
The top-level agent needs `agent_task` to spawn sub-agents, but sub-agents must not have `agent_task` (to prevent recursive spawning). Sub-agents instead need `agent_task_response` to return results to the parent. The three-tier structure (`BASE_TOOL_DECLARATIONS`, `TOOL_DECLARATIONS`, `SUB_AGENT_TOOL_DECLARATIONS`) keeps the base tools in one place and composes the tier-specific lists by appending the appropriate declaration. This avoids duplicating the nine base tool definitions.

**Why are sub-agent messages silent (not streamed to the frontend)?**
Sub-agents work on delegated tasks that may involve many intermediate tool calls. Streaming all of these to the frontend would create noise in the conversation thread and confuse the user, since the sub-agent's intermediate steps are not part of the main conversation flow. Only the parent's `tool_use` (showing the sub-agent name and description) and `tool_result` (the sub-agent's final response) are emitted, giving the user visibility into what was delegated and what was returned.

**Why ephemeral sessions for sub-agents instead of storing them in `_active_chats`?**
Sub-agents complete their work within a single `_run_sub_agent()` call and are not expected to be resumed later. Storing them in the session store would create orphaned sessions that never get cleaned up. Ephemeral sessions are created and garbage-collected automatically when the function returns.

**Why a turn limit for sub-agents?**
Without a safety limit, a sub-agent could enter an infinite tool-calling loop (e.g., repeatedly calling an API that always returns results prompting another call). The turn limit prevents unbounded execution. When exceeded, accumulated text output is returned as a best-effort result.

**Why are turn limits model-specific?**
Models with smaller context windows (200K tokens, e.g., Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, and Claude Opus 4.7) fill their context much faster than 1M-token models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5), so they need a lower turn limit (20 turns, warning at 15) to avoid context overflow. Larger-context models get the default limits (60 turns, warning at 50). The `get_sub_agent_turn_limits(model)` function in `chat/gemini_api/constants.py` looks up the model's `max_input_tokens` in `MODEL_REGISTRY` from `chat/llm/config.py` to determine the appropriate limits; unknown models fall back to the large-model defaults.

**Why a turn warning threshold before the hard limit?**
The hard turn limit cuts off the sub-agent abruptly, potentially losing work in progress. The warning threshold (5 turns before the limit for 200K-token models, 10 turns for 1M-token models) gives the model advance notice by injecting a system warning into the tool results. This warning instructs the model to call `agent_task_response` immediately with its findings so far, producing a graceful wind-down rather than an abrupt cutoff.

The warning is injected via `provider.inject_turn_warning()` on the `LLMProvider` interface (see [LLM Provider Abstraction](llm-providers.md)), which appends a text part to the formatted tool results in a provider-specific way (Gemini: `Part.from_text`, Anthropic: text content block).

**Why a context window warning (`SUB_AGENT_CONTEXT_WARNING_THRESHOLD = 0.75`) in addition to the turn warning?**
The turn warning catches runaway loops (too many iterations), but a sub-agent can also exhaust its context window well before hitting the turn limit -- for example, when processing large tool results that consume many tokens per turn. The context warning at 75% capacity detects this orthogonal failure mode by checking `compute_total_context_tokens()` against the model's `max_input_tokens` from `MODEL_REGISTRY` after each turn.

It follows the exact same `inject_turn_warning()` mechanism and logging format as the turn warning, keeping the implementation consistent. Both warnings can fire on the same turn if both thresholds are crossed, which is harmless since redundant urgency reinforces the instruction to return immediately.

**Why no turn limit for the main conversation loop?**
The main conversation loop intentionally has no turn limit. An earlier version applied `MAX_TURNS = 40` to the main loop, but this was a mistake -- the limit was meant only for sub-agents. In the main user-facing conversation, hitting the turn limit would silently cut off the response mid-way through a multi-step task, leaving the user with incomplete results and no indication of what happened. Sub-agents have a turn limit (`MAX_SUB_AGENT_TURNS = 60`) because they run silently without user oversight and could enter infinite loops. The main conversation, by contrast, is user-supervised -- the user can see the model's progress and click Stop at any time.

**Why log sub-agent tool calls and spawn/completion events?**
Sub-agent execution is silent (not streamed to the frontend), making it difficult to debug issues or understand performance characteristics. The logging at INFO level with structured prefixes (`[agent_task]`, `[agent_task_parallel]`, `[sub-agent:<name>]`) makes sub-agent behavior observable via server logs without affecting the user-facing experience. Duration tracking in milliseconds helps identify slow tool calls and overall sub-agent execution time.

**Why does `agent_task` support an optional `model` parameter?**
The parent model may want to delegate simple tasks (data retrieval, formatting) to a faster/cheaper model like `gemini-3.5-flash-lite` while keeping a more capable model for itself. When `model` is omitted, the sub-agent inherits the parent's model via `agent_model = args.get("model") or ctx.model` in the `agent_task` handler (see `chat/gemini_api/turn_tools.py`). This keeps the common case simple (no parameter needed) while allowing cost/latency optimization for multi-agent workflows.

**Why does `agent_task` require `description` in addition to `name` and `prompt`?**
The `name` is a short display label (e.g., "Email Researcher") and the `description` is a brief UI-facing summary of what the sub-agent is doing (e.g., "Searching for unread emails from Alice"). The `prompt` contains the full detailed instructions but is too long for UI display. The frontend uses `name` as the primary label and `description` as the secondary label, providing a better user experience than showing the raw tool name `agent_task`.

**Why a separate `agent_task_parallel` tool instead of allowing multiple concurrent `agent_task` calls?**
The Gemini SDK may return multiple function calls in a single response, but they are executed sequentially in the conversation loop. Even if the model emitted multiple `agent_task` calls in one response, they would run one after another. The `agent_task_parallel` tool explicitly uses `asyncio.gather()` to run all tasks concurrently, providing genuine parallelism. It also gives the model a single result containing all responses tagged by ID, which is easier to reason about than receiving sequential results from separate tool calls.

**Why require unique IDs in `agent_task_parallel` tasks?**
The caller-specified `id` field tags each result so the model can deterministically identify which response came from which task. Without IDs, the model would need to infer the mapping from result contents, which is fragile. Requiring uniqueness prevents ambiguous results.

**Why cap parallel tasks at `MAX_PARALLEL_TASKS = 10`?**
Each sub-agent creates its own Gemini API chat session and may make multiple tool calls. Running too many concurrently could exhaust API rate limits or overwhelm the server. The limit of 10 provides enough parallelism for practical multi-source research tasks while bounding resource consumption.

**Why enforce `MAX_AGENT_TASKS_PER_TURN = 10` for sequential `agent_task` calls?**
Without backend enforcement, the model could emit an unbounded number of sequential `agent_task` calls in a single response, each running one at a time. This wastes time compared to `agent_task_parallel`, which runs tasks concurrently. The cap of 10 matches the `MAX_PARALLEL_TASKS` limit and returns a JSON error directing the model to use `agent_task_parallel` instead. This is both a performance guardrail (preventing runaway sequential spawning) and a nudge toward the more efficient parallel tool.

**Why isolate individual task failures in `agent_task_parallel`?**
When running multiple independent tasks, one failure should not invalidate the results of the others. The `run_one_task()` wrapper in `_run_parallel_sub_agents()` catches exceptions per-task and returns them with `status: "error"`, so the model receives partial results and can decide how to handle the failure. This is different from `agent_task`, where a single failure is returned directly as the tool result.

**Why drain the final response after `agent_task_response`?**
When a sub-agent calls `agent_task_response`, the code sends an acknowledgment function response back to the model and consumes its final streaming response before returning. This ensures: (a) the SDK's `record_history()` runs for the final turn, keeping the chat object's history consistent, (b) the underlying HTTP streaming connection (httpx) is properly closed via the `_aiter_response_stream` finally block, preventing connection pool leaks, and (c) the conversation lifecycle is fully completed from the API server's perspective.

The drain is wrapped in a try/except so that a failure during draining does not lose the already-captured sub-agent response. Usage metadata from the final response is captured for token stats logging.

**Why filter system instructions by connected services?**
Previously, all API endpoint documentation was inlined in every system prompt -- even disconnected services were often included in full. Today two filters cut prompt size: (a) backend docs are not inlined at all, they live in loadable system skills (see [Skill Library Architecture -- System Skills](skill-library.md#system-skills)); (b) the system-skills enumeration itself is filtered by `connected_services`, so a user without Slack never sees `system:slack` in the enumeration. The `connected_services` parameter defaults to `None` (include all backend skills) and is used by tests and admin tooling.

**Why invalidate sessions on OAuth callback?**
When a user connects or disconnects a service, the enumerated skill list in the system prompt needs to change. Since system prompts are baked into the SDK chat session at creation time, existing sessions with stale prompts must be discarded. Calling `invalidate_user_sessions()` in all OAuth callbacks (Google Services, Slack, Telegram, GitHub, Twitter, Airtable) ensures the next message creates a fresh session with the updated enumeration.

**Why co-locate instruction functions with their API submodules?**
Each API submodule (e.g., `api/gmail/`, `api/drive.py`) defines a `get_instructions()` function containing the per-backend tool usage documentation. `chat/system_skills/catalog.py` binds each backend's system skill id to the corresponding `get_instructions()` callable via a `content_builder` (plugin skills bind theirs to a data file, e.g. a plugin's `instructions.md`). Keeping per-backend docs co-located with the endpoint code (rather than in a separate skills directory) means updating an API endpoint and its tool-usage description happens in the same file.

**Why a generic durable wait-handle mechanism?**
The wait-handle mechanism (`tool_wait_handles` DB table + `wait_for_handles` tool + `chat/wait_handles/` runtime and resume helpers) absorbs every tool that blocks on user input. Centralising the lifecycle (register / wait / resolve / cancel / timeout / resume) avoids duplicating synchronization, persistence, and resume logic across each new blocking tool, and makes "the suspended run survives a restart" a property of the mechanism rather than a per-tool concern. See [Wait Handles Architecture](wait-handles.md).

**Why does `wait_for_handles` accept a timeout instead of blocking indefinitely?**
A bounded wait keeps abandoned model loops from sitting on futures forever, and a finite timeout means the live arm always returns so the next user-driven path can pick up state from the DB (the headless-resume path depends on this). The cap is set to ~14 days (`_WAIT_FOR_HANDLES_MAX_TIMEOUT` in `chat/gemini_api/turn_tools.py`) -- long enough that a routed action request the user only sees the next morning still wakes the original arm, but bounded enough that a forgotten handle eventually times out.

The default is the same value because the realistic blocking horizon for human-in-the-loop work is days, not minutes; the model is steered to omit `timeout_seconds` for the normal case and only pass a smaller cap when it deliberately wants to give up sooner.

**Why are memory writes routed through `create_action_request` instead of a dedicated tool?**
Action requests already carry Approve / Revise / Stop semantics, server-rendered preview fields, an always-blocking suspend, and a cross-conversation Requests pane -- everything the memory case needs. Folding memory writes into `create_action_request(request_type="create_memory", ...)` collapses tool, wait-handle kind, and structured message onto the generic action-request path; the `CreateMemoryHandler` in `chat/action_request_types/create_memory.py` runs `db.memory_store.create_memory` on Approve and returns the new `memory_id` on `result`. See [Action Requests](action-requests.md) for the per-type catalog.

**Why UUID-based tool IDs instead of counter-based?**
Tool IDs are generated as `"{tool_name}_{uuid_hex8}"` using `uuid.uuid4().hex[:8]` in `chat/gemini_api/conversation.py`. An earlier implementation used a counter-based scheme (`"{tool_name}_{tool_call_count}"`), but the counter reset to zero at the start of each conversation turn, causing collisions across turns. The wait-handle resume bucket looks up rows by `tool_id`, and the frontend reconciles confirm-action UI by `tool_id` -- both rely on the global uniqueness UUID-based IDs provide.

**Why record API call usage per-turn to the database instead of only emitting in the stats event?**
The stats event is ephemeral on the wire but durable on disk -- it is emitted over the persistent WS as a transient and is also a `FLUSH_EVENT_TYPES` member, so each stats emit is appended to `chat_history.json` with a stamped `seq`, but querying aggregate usage across conversations requires parsing JSON files. Recording to the per-provider raw analytics tables (`llm_calls_gemini` / `llm_calls_anthropic`) enables efficient SQL queries for per-conversation, per-user, and per-model usage analytics. The per-turn granularity allows breaking down usage by call type (top-level vs sub-agent) and agent name.

**Why use `usage_accumulator` dicts to bubble sub-agent usage to the parent?**
Sub-agents run in separate functions (`_run_sub_agent()`, `_run_parallel_sub_agents()`) that do not have access to the parent's token counters. Passing a mutable dict allows per-turn sub-agent usage to be accumulated without introducing global state or return-value complexity. The parent reads the accumulated totals after the sub-agent returns and adds them to the stats event's breakdown fields.

**Why do action-request cards resolve outcome via the wait-handle row instead of via a matching `tool_result`?**
The `wait_handle_id` carried on the persisted `action_request` message points at the authoritative `tool_wait_handles` row. Looking up the row's `status` directly means the collapsed state is correct even when the model loop is not yet alive to have emitted a corresponding `tool_result` (e.g., the user clicked Approve after a server restart and the headless resume has not yet run, or is mid-stream). It also dodges the false-match problem a `tool_result`-scanning approach would have when the same tool is invoked multiple times in a conversation.

## Token Usage Tracking

Every LLM API call (both top-level and sub-agent, across all providers) is recorded to the per-provider raw analytics tables (`llm_calls_gemini` / `llm_calls_anthropic`) via `record_api_call()` in `db/llm_call_store.py`, which dispatches on `provider`.

The recording is done per-turn (per `send_message_stream` call) and captures model name, call type, agent name, `backend` (from `get_backend_for_model()`), `level` (1 for top-level/1st-level, 2 for nested sub-agents), duration, and the provider-NATIVE token fields copied verbatim from `raw_usage` into that table's columns (the coalesced input/output/cached parameters serve only as a degraded-stream fallback when the provider returned no usage object).

The DB recording does not feed the in-UI stats; the coalesced `stats` event / display path described below is a separate, unchanged in-memory accumulation. See [Database -- LlmCallGemini and LlmCallAnthropic](database.md#llmcallgemini-and-llmcallanthropic-models) for the column contract.

For the stats event, token usage is accumulated across ALL tool-call turns in the conversation loop (not just the last turn), ensuring it reflects the full conversation cost; its `cached_tokens` field tracks provider-specific caching: `cached_content_token_count` from the Gemini API response `usage_metadata`, or the sum of `cache_read_tokens` and `cache_creation_tokens` from Anthropic's prompt caching response.

The `new_input_tokens` metric provides a uniform measure of uncached work across providers, computed by `compute_new_input_tokens()` in `chat/llm/base.py`. The `provider` field in the stats event identifies which provider was used, enabling the frontend to display provider-specific cache breakdowns. The `model` field identifies the specific model ID that generated the response, used by the frontend to display model names in Quest message tooltips.

### Recording Flow

1. **Top-level turns**: After each streaming response in `run_conversation_turn()`, `record_api_call()` is called with `call_type=ApiCallType.TOP_LEVEL`, the model name, the `raw_usage` native token fields from the provider's `get_usage()` (plus the coalesced counts as degraded-stream fallback), and the turn duration in milliseconds. The per-turn `new_input_tokens` is computed via `compute_new_input_tokens()` and accumulated into running counters along with `cache_creation_tokens` and `cache_read_tokens`. All counters accumulate across all turns in the conversation loop, not just the last turn
2. **Sub-agent turns**: After each streaming response in `_run_sub_agent()`, `record_api_call()` is called with `call_type=ApiCallType.SUB_AGENT`, the model name, the sub-agent's `agent_name`, and per-turn token counts (including cached tokens, cache creation/read breakdown, and new input tokens) and duration. New input tokens are computed via `compute_new_input_tokens()` using the sub-agent's own provider name (supporting mixed-provider sub-agents). Additionally, per-turn usage is accumulated into a `usage_accumulator` dict that the parent reads after the sub-agent completes
3. **Sub-agent drain turns**: When a sub-agent terminates via `agent_task_response`, an acknowledgment is sent back to the model and its final streaming response is drained. The drain turn's token usage (including cached tokens, cache creation/read, and new input tokens) is also recorded via `record_api_call()` and accumulated

### Usage Accumulation

Sub-agent usage flows up to the parent via `usage_accumulator` dicts:

1. `run_conversation_turn()` creates a `sub_agent_usage` dict `{}` before calling `_run_sub_agent()` or `_run_parallel_sub_agents()`
2. `_run_sub_agent()` accumulates its per-turn `input_tokens`, `output_tokens`, `cached_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `new_input_tokens`, and `call_count` into the `usage_accumulator`. `new_input_tokens` is computed via `compute_new_input_tokens()` using the sub-agent's own provider name
3. `_run_parallel_sub_agents()` passes a per-task `usage_accumulator` to each `_run_sub_agent()` call, then merges all task accumulators (including `cache_creation_tokens`, `cache_read_tokens`, `new_input_tokens`) into the parent accumulator
4. After the sub-agent call returns, `run_conversation_turn()` adds the accumulated totals to the running `sub_agent_input_tokens_total`, `sub_agent_output_tokens_total`, `sub_agent_cached_tokens_total`, `sub_agent_cache_creation_tokens_total`, `sub_agent_cache_read_tokens_total`, `sub_agent_new_input_tokens_total`, and `sub_agent_call_count` counters

### Stats Event

The `stats` event emitted at the end of each conversation turn (step 5 of the conversation loop) includes all fields accumulated across ALL tool-call turns in the conversation loop:

| Field | Description |
|-------|-------------|
| `input_tokens` | Total input tokens (top-level + all sub-agents), accumulated across all turns |
| `output_tokens` | Total output tokens (top-level + all sub-agents), accumulated across all turns |
| `cached_tokens` | Total cached input tokens (top-level + all sub-agents) |
| `new_input_tokens` | Total new (uncached) input tokens (top-level + all sub-agents), computed via `compute_new_input_tokens()` |
| `provider` | Provider name string (`"gemini"` or `"anthropic"`) for the top-level agent |
| `cache_creation_tokens` | Total cache creation tokens (top-level + all sub-agents; Anthropic-specific, always 0 for Gemini) |
| `cache_read_tokens` | Total cache read tokens (top-level + all sub-agents; Anthropic-specific, always 0 for Gemini) |
| `duration_ms` | Total wall-clock time for the conversation turn |
| `tool_calls` | Number of tool calls made by the top-level agent |
| `turns` | Number of model turns in the conversation loop |
| `top_level_input_tokens` | Input tokens from top-level agent turns only, accumulated across all turns |
| `top_level_output_tokens` | Output tokens from top-level agent turns only, accumulated across all turns |
| `top_level_cached_tokens` | Cached input tokens from top-level agent turns only |
| `top_level_new_input_tokens` | New input tokens from top-level agent turns only |
| `top_level_cache_creation_tokens` | Cache creation tokens from top-level agent turns only |
| `top_level_cache_read_tokens` | Cache read tokens from top-level agent turns only |
| `sub_agent_input_tokens` | Input tokens from all sub-agent turns combined |
| `sub_agent_output_tokens` | Output tokens from all sub-agent turns combined |
| `sub_agent_cached_tokens` | Cached input tokens from all sub-agent turns combined |
| `sub_agent_new_input_tokens` | New input tokens from all sub-agent turns combined |
| `sub_agent_cache_creation_tokens` | Cache creation tokens from all sub-agent turns combined |
| `sub_agent_cache_read_tokens` | Cache read tokens from all sub-agent turns combined |
| `sub_agent_call_count` | Total number of sub-agent API calls |
| `context_tokens` | Total tokens occupying the context window after the last turn, computed via `compute_total_context_tokens()` from the last turn's raw usage (provider-specific: Gemini uses `input_tokens` directly; Anthropic sums `input_tokens + cache_read_tokens + cache_creation_tokens`) |
| `max_context_tokens` | Maximum input tokens for the model, from `MODEL_REGISTRY` (e.g., 1,000,000 for Gemini, Claude Opus 4.8, and Claude Sonnet 5, 200,000 for the other Anthropic models) |

All token counts are accumulated across all turns in the conversation loop, not just the last streaming response. The `sub_agent_*` fields aggregate across all sub-agent calls (single and parallel) that occurred during the turn. The `context_tokens` and `max_context_tokens` fields are used by the frontend `ContextIndicator` component to display context window utilization.

### Frontend Display

The stats bar in `frontend/src/components/Message.tsx` uses a unified token display that adapts based on available data:

**New unified display** (when `new_input_tokens` is present in the stats event):

- Primary line shows `NEW INPUT {n}` (top-level agent new input when sub-agents are involved, or total new input otherwise), `OUTPUT {n}`, `TIME {n}s`, and `TOOLS {n}`. Each `NEW INPUT` and `SUBAGENTS` label is wrapped in a tooltip container (`.stats-tooltip-container` in `frontend/src/components/Message.css`) that shows a provider-specific breakdown on hover
- Hover tooltip for `NEW INPUT` shows a provider-aware breakdown:
  - Gemini: Total input, Cached, New
  - Anthropic: Non-cached input, Cache creation, Cache read, New
- When sub-agents were used (`sub_agent_call_count > 0`), a second row shows `SUBAGENTS {new_input} / {output} ({N} calls)` with its own hover tooltip (new input, cached if > 0, output, call count)

**Legacy fallback** (for backward compatibility with older stats messages lacking `new_input_tokens`):

- Shows `INPUT {n} (cached)` format, same as before

All token count values are formatted with locale-aware thousand separators via `formatNumber()` from `frontend/src/utils/formatters.ts`. The `new_input_tokens`, `provider`, `model`, `cache_creation_tokens`, `cache_read_tokens`, and per-call-type breakdown fields in `UsageStats` (defined in `frontend/src/api/types.ts`) are optional -- older stats messages without them render normally with the legacy display.

## Constraints

- In-memory sessions are lost on server restart, but SDK history is automatically restored from `sdk_history.json` on the next message (provider-compatibility checked), so the model retains full prior context. On model changes, same-provider switches preserve in-memory history; cross-provider switches fall back to disk history if compatible
- When a streaming response is interrupted by the user, partial text and completed tool call content blocks are preserved in `sdk_history.json` with an interruption marker (`"[Response interrupted by user before completion]"`) appended to the assistant's message. Both Gemini and Anthropic provider paths handle cancellation by flushing in-progress content before re-raising `CancelledError`.
  - Edge cases handled include empty partial text (a minimal marker-only message is added), tool-use-only content blocks (marker appended as a new text part/block), and cancellation during the MIME retry path
  - When the flushed session ends with an unanswered tool_use (e.g., a Slack run cancelled mid-suspend), the interruption-marker text append is skipped and the dangling-tool_use state is saved as-is for the resume path to close -- see [Dangling Tool_use Recovery on Resume](#dangling-tool_use-recovery-on-resume)
- The `google-genai>=1.0.0` dependency was added to `pyproject.toml`; the `websockets` version constraint was relaxed from `>=16.0` to `>=13.0` to resolve a dependency conflict
- Explicit context caching for the system prompt is not yet implemented (planned as a future optimization). However, Gemini's implicit caching is tracked via `cached_content_token_count` from `usage_metadata` and reported in the stats event and database
- Top-level agents have sixteen registered tools:
  - `curl_proxy_get`, `curl_proxy_post` (dispatched via route dispatch)
  - `tool_call` (meta tool for dynamic dispatch of the `TOOL_CALL_REGISTRY` tools -- see [Tool Declarations](#tool-declarations) for the roster)
  - `load_gmail_attachment`
  - `list_skills`, `search_skills`, `load_skills` (skill discovery and loading via `db/skill_store.py`)
  - `list_my_skills`, `get_skill` (read-only inspection of the user's own DB skills, see [Skill Library -- Skill Inspection Tools](skill-library.md#skill-inspection-tools))
  - `list_routines` (read-only listing of the current project's routines, see [Routines Architecture -- Agent Tools](routines.md#agent-tools))
  - `run_script`, `run_python` (both executed in ephemeral Podman containers)
  - `agent_task` (spawns a single sub-agent, with optional `model` parameter)
  - `agent_task_parallel` (spawns multiple sub-agents concurrently, max `MAX_PARALLEL_TASKS = 10` tasks per invocation)
  - `agent_task_parallel_template` (spawns template-based sub-agents concurrently from a shared prompt template, max `MAX_PARALLEL_TEMPLATE_TASKS = 20` per invocation, restricted to `TEMPLATE_BATCH_ALLOWED_MODELS`)
  - `create_action_request` (proposes a write operation to an external service with user approval; blocks the agent loop until the user resolves the inline card via `SuspendForActionRequest`)
- Memory writes are issued as `create_action_request(request_type="create_memory", ...)`. See [Wait Handles Architecture](wait-handles.md)
- Sequential `agent_task` calls within a single model response are capped at `MAX_AGENT_TASKS_PER_TURN = 10`; calls beyond this limit receive a JSON error directing the model to use `agent_task_parallel` instead
- Sub-agents have ten tools: the nine base tools plus `agent_task_response` (terminates the sub-agent and returns results), plus dynamic tools via `tool_call` (all `TOOL_CALL_REGISTRY` entries except `set_conversation_name`)
- Sub-agents cannot spawn their own sub-agents (no recursive spawning) -- neither `agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, nor `create_action_request` are available to sub-agents. The `set_conversation_name` dynamic tool is excluded from sub-agent visibility via the `exclude` parameter on `_build_dynamic_tools_section()`
- `wait_for_handles` blocks the conversation loop on one or more durable wait handles in the `tool_wait_handles` table. `timeout_seconds` is optional; when omitted it defaults to `_WAIT_FOR_HANDLES_MAX_TIMEOUT` in `chat/gemini_api/turn_tools.py` (currently ~14 days), and when set it is clamped to [1, that maximum]. On timeout, still-pending rows are best-effort transitioned to `timed_out`. The DB row is the source of truth, so the suspended state survives server restarts via the resume bucket logic at the top of `run_conversation_turn()` and the headless resume path in `chat/wait_handles/resume.py`. See [Wait Handles Architecture](wait-handles.md)
- Pending confirmations are cleaned up on session removal (`remove_chat_session()`) with an auto-rejection (feedback: "Session interrupted") to prevent dangling coroutines
- The main conversation loop has no turn limit -- it runs until the model finishes responding. A `turn_count` variable tracks iterations and is included in the `stats` event for observability
- Sub-agents have model-specific turn limits determined by `get_sub_agent_turn_limits(model)` in `chat/gemini_api/constants.py`: 200K-token models (Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7) get 20 turns max with warning at 15; 1M-token models (Gemini variants, Claude Opus 4.8, and Claude Sonnet 5) and unknown models get the default 60/50. The warning is injected via `provider.inject_turn_warning()` to prompt a graceful wind-down
- All limit constants (`MAX_PARALLEL_TASKS`, `MAX_PARALLEL_TEMPLATE_TASKS`, `MAX_AGENT_TASKS_PER_TURN`, `MAX_SUB_AGENT_TURNS`, `SUB_AGENT_TURN_WARNING_THRESHOLD`) and the `TEMPLATE_BATCH_ALLOWED_MODELS` set and `get_sub_agent_turn_limits(model)` function are defined in `chat/gemini_api/constants.py`
- Sub-agent messages are silent -- they are not streamed to the frontend
- Usage metadata (token counts) is extracted from each streaming response via `provider.get_usage()` and accumulated across all turns in the conversation loop. If the provider does not return usage, token counts default to 0
- Per-turn provider-native token usage is recorded to the per-provider raw analytics tables (`llm_calls_gemini` / `llm_calls_anthropic`) via `record_api_call()` from `db/llm_call_store.py` for both top-level and sub-agent calls. Recording failures are caught and logged as warnings -- they never interrupt the conversation
- Sub-agent token usage is tracked separately and reported in the `stats` event with breakdown fields (`top_level_input_tokens`, `top_level_output_tokens`, `top_level_cached_tokens`, `top_level_new_input_tokens`, `top_level_cache_creation_tokens`, `top_level_cache_read_tokens`, `sub_agent_input_tokens`, `sub_agent_output_tokens`, `sub_agent_cached_tokens`, `sub_agent_new_input_tokens`, `sub_agent_cache_creation_tokens`, `sub_agent_cache_read_tokens`, `sub_agent_call_count`).
  - The aggregate `input_tokens`, `output_tokens`, `cached_tokens`, `new_input_tokens`, `cache_creation_tokens`, and `cache_read_tokens` fields include both top-level and sub-agent totals. The `provider` field identifies the top-level provider name
- System instructions are filtered by connected services: only system skills whose `requires` gate is satisfied are enumerated in the system prompt. Backend docs themselves are not inlined; the model loads them via `load_skills`. The `/api/instructions` endpoint is an exception -- it renders all `get_instructions()` output regardless of connection status for reference
- Session invalidation occurs on all OAuth callbacks in the `auth/` submodule (Google Services, Slack, Telegram, plugin keys) in addition to settings changes, ensuring system prompts reflect current connection status
- Unsupported MIME types (Office/OpenXML, OpenDocument, archives, executables) are pre-checked against `_UNSUPPORTED_GEMINI_MIME_TYPES` in `chat/gemini_api/constants.py` before uploading to the Gemini File API. Files with denied MIME types return a structured error with a suggestion to use `run_script` or `run_python` with python-docx/openpyxl. As a safety net, `ClientError` from the content generation API is caught in the conversation loop -- file URI parts are stripped and replaced with error text, and the turn is retried
- File uploads to the Gemini File API use `asyncio.wait_for(timeout=60)` to prevent indefinite hangs on upload

