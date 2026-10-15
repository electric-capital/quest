# Conversation Loop and Tool Integration

This document covers the `chat/gemini_api/` package, which runs every model conversation: the top-level turn loop, tool declarations and dispatch, the system prompts, the in-memory session store with disk-backed history, and sub-agents. The package name predates the multi-provider design (see the package docstring). It reaches models only through the `LLMProvider` interface ([LLM Provider Abstraction](llm-providers.md)), streams over the persistent WebSocket ([Realtime](realtime.md)), sends HTTP tools through [Route Dispatch](route-dispatch.md), and blocks on the user through [Wait Handles](wait-handles.md).

## Key Files

- `chat/gemini_api/conversation.py` -- `run_conversation_turn()`, the top-level loop; message envelope, turn-start resolution, MIME retry
- `chat/gemini_api/turn_tools.py` -- `TURN_TOOL_HANDLERS`, the tool arms the loop handles itself (sub-agent spawners, `create_action_request`, `wait_for_handles`, the Slack reply, the finish tools), and the suspend sentinels
- `chat/gemini_api/tool_dispatch.py` -- `_dispatch_tool_call()` with the `TOOL_CALL_HANDLERS` / `DIRECT_TOOL_HANDLERS` tables and `ToolContext`
- `chat/gemini_api/tool_handlers/` -- per-tool handler functions (workspace, memory, Drive, Gmail, skills, sandbox, ...)
- `chat/gemini_api/authed_get.py` -- `authed_get` / `authed_post`, `_SERVICE_REGISTRY`, the sandbox proxy endpoints
- `chat/gemini_api/script_tool_call.py` -- the sandbox `POST /api/tool-call` bridge
- `chat/gemini_api/system_prompt.py` -- every system prompt variant
- `chat/gemini_api/session.py` -- the in-memory session store and session invalidation
- `chat/gemini_api/history.py` -- `sdk_history.json` save / load
- `chat/gemini_api/sub_agent.py` -- `_run_sub_agent()` and the parallel runners
- `chat/gemini_api/usage.py` -- `UsageAccumulator`, the per-run token totals behind the `stats` event
- `chat/gemini_api/run_context.py` -- `RunContext`, the per-run state threaded through the loop and its tool arms
- `chat/gemini_api/constants.py` -- limits, model sets and the MIME deny-list
- `chat/llm/tool_schemas.py` -- tool declarations, tiers and `TOOL_CALL_REGISTRY`

## Conversation Loop

`run_conversation_turn()` in `chat/gemini_api/conversation.py` runs one turn: a message in, any number of model / tool rounds, and out the structured messages persisted to `chat_history.json` (types in `chat/storage.py`, see [Chat API](../api/chat-api.md)). Callers: the persistent-WS run task (`_run_send_message` in `chat/realtime/socket.py`), the headless wait-handle resume (`_run_resume` in `chat/wait_handles/resume.py`), the Slack runtime (`_dispatch_slack_model_run` in `chat/slack_socket_mode.py`), the scheduler (`chat/scheduler.py`) and the cross-user-subagent and inference-API drivers (`chat/user_subagent.py`, `chat/inference_api.py`).

A turn across files:

1. Turn start resolves the model (`_DEPRECATED_MODELS`, `is_model_allowed()`), the guide snapshot and notice flags, project instructions, skills, flags and connected services, builds the prompt ([System Prompt](#system-prompt)), gets the session ([In-Memory Chat Session Store](#in-memory-chat-session-store)) and repairs its history (`provider.repair_session_history()`); per-run state goes into `RunContext`.
2. The resume bucket closes dangling tool_uses from their wait-handle rows ([Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume)).
3. Each round streams through `_stream_turn()` and records usage ([Token Usage Tracking](#token-usage-tracking)). Each tool call in the round, in order, emits a durable `tool_use` and is answered by `invalid_tool_arguments_result()` for unparseable arguments, `public_blocked_result()` for a loop arm in a public project (`_PUBLIC_BLOCKED_LOOP_TOOLS`), its `TURN_TOOL_HANDLERS` arm, or `_dispatch_tool_call()` ([Shared Tool Dispatch](#shared-tool-dispatch)).
4. The round's results go back as one message; a round without tool calls ends the turn with the `stats` event.

Several `create_action_request` calls in one round each open their own card before the loop suspends ([Action Requests](action-requests.md)). Tool ids are the provider's, else `<tool_name>_<8 hex>`; they must be unique across turns because the resume bucket finds wait-handle rows by tool id.

`messages_out` is a caller-owned list that receives every structured message as it is produced, so callers can persist partial output when the coroutine unwinds through `CancelledError` or a suspend sentinel instead of returning. `origin` selects the Slack tier and prompt section ([Slack Socket Mode](slack-socket-mode.md)), and `flags` carries the conversation's opt-in flags ([Conversation Flags](conversation-flags.md)).

The top-level loop has no turn cap (`turn_count` is reported in `stats`); sub-agents have one.

## Chat Route Wiring

`_run_send_message` in `chat/realtime/socket.py` drives a web turn: it holds the per-conversation send slot, persists the user message, and passes a shared `messages_out` list and an `on_event` running the `make_flush_callback()` flush (`chat/_flush_helper.py`) for durable events and `_publish_transient_event` for transient ones; on `CancelledError` it saves the interrupted SDK history, drops the session and cancels pending wait handles. The headless resume and the Slack runtime share the flush callback, so their durable events reach viewing tabs too. Send lock and stop semantics: [Realtime -- Send Lock and Stop Semantics](realtime.md#send-lock-and-stop-semantics).

## Tool Declarations

Declarations are provider-agnostic JSON Schema (`ToolSpec` in `chat/llm/base.py`) in `chat/llm/tool_schemas.py`, converted per provider at session creation by `to_gemini_declarations()` / `to_anthropic_tools()` (see [LLM Provider Abstraction](llm-providers.md)). Tiers are composed from one base list so the shared tools are defined once:

- `BASE_TOOLS` -- shared by every tier: the two `curl_proxy_*` HTTP tools, the `tool_call` meta tool, `load_gmail_attachment`, the sandbox runners and the skill / routine read tools.
- `TOP_LEVEL_TOOLS` -- adds the sub-agent spawners (`agent_task`, `agent_task_parallel`, `agent_task_parallel_template`) and `create_action_request`.
- `SUB_AGENT_TOOLS` -- adds `agent_task_response` and nothing that spawns or blocks: a sub-agent that needs an approval-gated write returns the proposal to the parent ([Action Requests](action-requests.md)). `SUB_AGENT_TOOLS_NESTED` adds `agent_task_nested` (see [Nested Sub-Agents](#nested-sub-agents)).
- `SLACK_TOP_LEVEL_TOOLS` -- adds `send_slack_reply_and_get_response` for `origin="slack"` runs ([Slack Socket Mode](slack-socket-mode.md)).
- `ROUTINE_TOP_LEVEL_TOOLS` / `PUBLIC_ROUTINE_TOOLS` -- add `routine_completed` for routine runs ([Routines -- Run Completion](routines.md#run-completion)).
- `PUBLIC_TOOLS`, `USER_SUBAGENT_TOOLS`, `INFERENCE_API_TOOLS` -- the public-project, cross-user-subagent and inference-API tiers ([Public Projects](public-projects.md), [User Subagents](user-subagents.md), [Inference API](../api/inference-api.md)).

The dynamic tools reached through `tool_call` are the keys of `TOOL_CALL_REGISTRY` plus what plugins register at load ([Plugins](plugins.md)); see [Meta Tool Pattern](#meta-tool-pattern-tool_call) and its [Prompt Tiers](#prompt-tiers). Visibility rules: `set_conversation_name` is hidden from sub-agents (`_build_dynamic_tools_section()`'s `exclude` argument, `chat/gemini_api/system_prompt.py`); `wait_for_handles` is top-level web only ([Wait Handles](wait-handles.md)); the Quest Docs and Quest Tables tools are gated on the `docs` and `tables` pseudo-services ([Quest Docs](quest-docs.md#model-facing-tools), [Quest Tables API](../api/tables-api.md#tiers)); `create_action_request` is dropped from Slack runs by both the prompt (`top_level_exclude`) and its dispatch arm ([Action Requests](action-requests.md)). The sub-agent caps and model sets live in `chat/gemini_api/constants.py`.

Every base tool accepts an `intent_message`, a short user-facing label shown instead of the raw tool name (required where the raw name means nothing, per each schema's `required` list). The loop pops it before dispatch and truncates it to the schema's stated cap; rendering is in `ToolUseMessage.tsx` (see [Frontend](frontend.md)). The `curl_proxy_*` URLs are checked by `validate_url()` ([Route Dispatch](route-dispatch.md)); the handler runs in-process as the dispatching user, so the prompt tells the model never to set `Authorization`.

### Conversation Workspace File Tools

`list_workspace_files`, `get_workspace_file`, `write_workspace_file` and `edit_workspace_file` (handlers in `chat/gemini_api/tool_handlers/workspace.py`) work on the conversation's own workspace, resolved by `conversation_workspace_dir()` in `chat/gemini_api/tool_handlers/_common.py` (`ChatStorage.get_conversation_workspace_root()`, created on first use) in every conversation. Paths resolving outside the root are rejected; writes are UTF-8 text capped by `_WRITE_FILE_MAX_SIZE`, and `edit_workspace_file` is an exact-string replacement (unique unless `replace_all`) that never creates files. Successful writes publish `file_list_changed` ([Realtime -- Backend publish sites](realtime.md#backend-publish-sites-per-user-globals)).

`get_workspace_file` returns small text files (`_is_text_file()`, under `_TEXT_INLINE_LIMIT`) inline; anything else is attached as a provider part via `provider.upload_file()` and returned in `extra_parts`. Before attaching, the MIME type is checked against `_UNSUPPORTED_GEMINI_MIME_TYPES` and the size against `get_attach_limit_for_model()` (`chat/llm/file_limits.py`) for the model actually being called; both failures return a structured error with a `suggestion` (`_oversize_suggestion()`) instead of a failing provider call. `load_gmail_attachment` (`_handle_load_gmail_attachment()`, bytes from `_resolve_gmail_attachment()` in `api/gmail/draft_endpoints.py`) attaches the same way behind the same two checks.

An edit is allowed only after the conversation has seen the file (via `get_workspace_file`, `write_workspace_file` or an earlier edit). Seen paths are kept in the `workspace_reads.json` sidecar in the conversation directory (`ChatStorage.add_workspace_read_paths()` / `get_workspace_read_paths()`, recorded best-effort by `_mark_workspace_file_read()`), so the gate survives restarts and covers sub-agents. The sidecar's read-modify-write needs no lock; see the concurrency note on `add_workspace_read_paths()`.

The other inline tools are documented with their features: memory reads `memory_search` / `memory_list` (`chat/gemini_api/tool_handlers/memory.py`; writes go through `create_action_request(request_type="create_memory")`), `get_current_time` (`chat/gemini_api/tool_handlers/misc.py`), `run_script` / `run_python` ([Script Runner](script-runner.md)), the skill tools ([Skill Library](skill-library.md)), `list_routines` ([Routines -- Agent Tools](routines.md#agent-tools)), `project_db_query` ([Project Database](project-db.md)), the Quest Tables tools ([Quest Tables API](../api/tables-api.md#model-tools)), the Gmail tools ([Gmail API](../api/gmail-api.md)), the Drive download and Workspace export tools ([Drive API](../api/drive-api.md)) and the Slack tools ([Slack plugin](../../plugins/slack/docs/slack-api.md)).

### Scheme-Qualified File Tools

A project conversation has two file spaces: its own conversation workspace and the project workspace shared by every conversation of the project (`ChatStorage.get_project_workspace_root()`, resolved by `project_workspace_dir()` in `chat/gemini_api/tool_handlers/_common.py`). Its file tools name the space in every path -- `chat://<rel>` or `proj://<rel>`, parsed only by `parse_space_path()` in `chat/gemini_api/tool_handlers/file_paths.py`:

- `list_files` / `read_file` / `write_file` / `edit_file` (`chat/gemini_api/tool_handlers/workspace.py`) behave like the four workspace tools over either root. Reads are recorded in `workspace_reads.json` under the full scheme path; the workspace tools also record the `chat://` key and `read_file` on `chat://` also records the bare key, so either family licenses an edit of the same conversation file, while a `proj://` edit needs a `proj://` read or write. Sub-agents share the parent's sidecar.
- `copy_file` (`chat/gemini_api/tool_handlers/workspace_copy.py`, over `file_storage.copy_entry` in a thread) copies between or within the spaces: `destination_exists` without `overwrite`, `invalid_destination` for a destination equal to, inside or containing the source, `forbidden_source` for a `chat://` scratch root (`SCRATCH_SOURCE_ROOTS` in `chat/file_storage.py`) copied toward `proj://`, and `file_list_changed` for the destination scope.

A bare path, unknown scheme, `..` or absolute remainder returns `invalid_path`; `proj://` without a project returns `no_project`. The five tools are in `PROJECT_ONLY_TOOL_CALL_TOOLS` (`chat/llm/tool_schemas.py`, with `project_db_query`), so they appear wherever `project_db_query` does -- top-level, routine, sub-agent (incl. nested) and public-project conversations -- and never in standalone, cross-user-subagent or inference-API runs. They are in `PUBLIC_TOOL_CALL_ALLOWLIST` and not in `SCRIPT_TOOL_CALL_ALLOWLIST`, because scripts have `/workspace` and `/project` mounted.

Project prompts leave the four `*_workspace_file` tools (`PROJECT_HIDDEN_WORKSPACE_TOOLS`) out of the Dynamic Tools section and name the scheme tools in their file rules, so a prompt never offers two overlapping file tool sets; the four stay dispatchable for replayed transcripts. Every other file-taking tool (attachments, inline images, `run_script`, `add_doc_image`, `return_to_caller.files`, plugin attachment params) takes conversation-workspace paths only. The core schemas carry `PROJECT_COPY_FIRST_SENTENCE` and the attachment resolvers' not-found errors in a project conversation append `PROJECT_COPY_FIRST_SUFFIX`, both in `chat/workspace_hints.py`; `run_script` has its own not-found wording.

## Meta Tool Pattern (`tool_call`)

The tools in `TOOL_CALL_REGISTRY` are reached through one declared tool, `tool_call(tool_name, arguments)`. The registry drives the system prompt's "Dynamic Tools" section (`_build_dynamic_tools_section()`), dispatch validation and the handler table, and `ToolUseMessage.tsx` displays the inner tool name. Declaring each tool individually was rejected because every function declaration's schema costs context on every request, while the prompt text is compact and cacheable.

### Prompt Tiers

Every registry spec is **core** (`CORE_TOOL_CALL_TOOLS` in `chat/llm/tool_schemas.py`, stamped `spec["core"]`: documented in full in every Dynamic Tools section) or **skill-documented** (`spec["skill"]`, from `_SKILL_DOCUMENTED_TOOLS` or `PluginTool.skill`, see [Plugins](plugins.md)): the prompt only names the tool on its skill's enumeration line ([Skill Library -- Enumeration in the System Prompt](skill-library.md#enumeration-in-the-system-prompt)), and loading the skill appends a "Tool reference" rendered from the registry (`render_tool_reference()`), so parameter docs cannot drift from the specs. `_stamp_prompt_tiers()` refuses a tool in neither tier. A builder's `exclude` set reaches both renderings (`exclude_tools` on `build_system_skills_enumeration()`). The public-project prompt has no skill tools and renders with `core_only=False`.

`DIRECT_TOOL_HANDLERS` (`chat/gemini_api/tool_dispatch.py`) also accepts some registry tools by their bare name, beside the always-direct base tools, so a model that calls them that way reaches the same handler. Any other bare name reaches route dispatch, which answers with a structured unknown-tool error the model corrects from.

## Shared Tool Dispatch

The loop routes each tool call one of two ways. Arms that need the loop itself -- spawning sub-agents, suspending on the user, finishing a routine / user-subagent / inference run -- are `TURN_TOOL_HANDLERS` in `chat/gemini_api/turn_tools.py`, looked up through `registry_key_for()`, which also routes the `tool_call` forms of `wait_for_handles` and `routine_completed` to their arms. Everything else goes through `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py`, which the sub-agent loop uses too and which returns `(result, extra_parts)`.

`_dispatch_tool_call_inner()` tries, in order:

1. `tool_call` -- validates the inner name against `TOOL_CALL_REGISTRY` and dispatches through `TOOL_CALL_HANDLERS` (plugins add entries with `register_dynamic_tool()`). The unknown-tool error lists only the tools the prompt offers.
2. `DIRECT_TOOL_HANDLERS` -- the tools called by their own name.
3. `execute_tool_call()` in `chat/route_dispatch.py` for `curl_proxy_get` / `curl_proxy_post`; its `ToolResultWithNotices` advisories become text `extra_parts` via `provider.make_text_part()` ([Route Dispatch](route-dispatch.md#toolresultwithnotices-pattern)).

Every handler takes `(ctx: ToolContext, args)`; see the module docstring. The public `_dispatch_tool_call()` converts any handler exception into a `{"error": "Tool '<name>' failed: ..."}` result (see [Error Surfacing](#error-surfacing)).

## Message Metadata Wrapping

`_wrap_message_with_metadata()` in `chat/gemini_api/conversation.py` wraps each user message before it reaches the model; `chat_history.json` keeps the raw text. The envelope is a `<message_metadata>` block (UTC and user-local time from the IANA `timezone` the frontend sends, `User Local Time: (unknown timezone)` when it is missing or unknown), an optional `<conversation_skills_loaded>` block, and the `<message>` block.

- `<conversation_skills_loaded>` carries the skills the user loaded through the composer's skill selector (`loaded_skills_content`, resolved from `skill_ids`), on the message where they are loaded; the prompt tells the model to treat them as standing instructions ([Skill Library](skill-library.md)).
- When the composer attached files ([Frontend -- Composer Component](frontend.md#composer-component)), their workspace-relative names arrive as `attached_filenames` and one "Files attached to this message" line is added to the metadata block on that turn only; the files themselves were uploaded to the workspace before the send.

Every prompt variant explains the envelope, tells the model to use the embedded time for simple time questions, and not to echo the tags. Per-message time was chosen over relying on `get_current_time` because it costs no tool call; the tool remains for precise times mid-task.

## Error Surfacing

A run-fatal exception must reach the user durably, not just as a transient WS event -- otherwise a conversation that dies mid-turn looks like it simply stopped. Three layers cooperate:

1. **Durable error message.** The top-level `except Exception` in `run_conversation_turn()` appends a `{"type": "error", "error": "<ExceptionType>: <message>", "stacktrace": ..., "timestamp": ...}` structured message before emitting the `error` event and re-raising. `"error"` is in `FLUSH_EVENT_TYPES` (`chat/_flush_helper.py`), so the flush writes it to `chat_history.json` and fans out `message_appended`; the bubble (`frontend/src/components/Message.tsx`) survives reloads and disconnected tabs. Partial streamed text is flushed first by `_flush_partial_text()`. Every `run_conversation_turn` caller gets this; the scheduler persists `messages_out` on both outcomes.
2. **Abnormal Gemini stream termination.** `GeminiProvider.send_message_stream()` (`chat/llm/gemini_provider.py`) raises `GeminiStreamAbnormalTermination` when the stream ends on anything other than `STOP` (e.g. `MALFORMED_FUNCTION_CALL`, `SAFETY`, a blocked prompt); otherwise such a stream yields nothing and the loop would treat the empty turn as success. `MAX_TOKENS` delivers already-streamed output and raises only when nothing was produced. The Gemini client does not retry (see the TODO in `GeminiProvider._get_client()`), unlike the Anthropic SDK.
3. **Contained tool failures.** `_dispatch_tool_call()` turns handler exceptions into tool error results, so one failing tool never aborts the run.

`send_message_finished` carries `error: true` when the run raised, so live clients can tell "died" from "finished" ([Realtime](realtime.md)).

`run_conversation_turn()` retries a turn once, with an error note in place of each `file_data` part, when Gemini rejects such a part with `INVALID_ARGUMENT`.

## Authenticated External API Requests (`authed_get`)

`authed_get` (`handle_authed_get()` in `chat/gemini_api/authed_get.py`) makes an HTTPS GET to an external API with the user's credential injected server-side; the model supplies only the URL and optional allow-listed headers. `_find_service()` matches the host -- path-prefix-scoped entries first, then the plain host -- against `_SERVICE_REGISTRY`; the entry's `load_credentials` and `inject_auth` supply and attach the credential (entries with `requires_user: True` get the user dict), and `retry_on_401` entries refresh and retry once. `_make_authed_request()` does this shared work for both verbs, followed by the size gate ([Large Response Protection](#large-response-protection)).

The optional `connection` argument selects one of the user's connections to a multi-connection service; its rules and error codes are in [User Connections -- Tool Dispatch](user-connections.md#tool-dispatch).

### Supported Services

The core `_SERVICE_REGISTRY` entries are the roster. They come in four credential shapes: a server-side API key from the credential store (CoinGecko, see [Service Credentials](service-credentials.md)), per-user Google OAuth with 401 retry (the Google Workspace and Google Cloud hosts, see [GCP API](../api/gcp-api.md)), a per-user token off the user dict (Airtable PAT, Ramp OAuth), and no auth (Federal Register). Plugins add entries through `register_service()` (e.g. GitHub, SEC EDGAR).

### Allowed Endpoints Validation

`_parse_request_url()` parses the URL once with `httpx.URL`; the registry lookup, the allow-list match (on the percent-encoded path as sent, without query or fragment), the tools' `alt=media` / `files.export` checks and the request itself all use that parse. Before any lookup it refuses a non-`https` scheme, a `user@` part, a non-default port, and a path with a `.` or `..` segment, an empty segment (`//`), a backslash or a control character, literal or percent-encoded (`_path_problem()`): upstream servers decode those forms on their own terms.

An entry's `allowed_endpoints` regexes (compiled at registration) gate the request path; a miss is rejected with the allowed patterns. Every entry with per-user credentials declares one, as do no-auth hosts path-scoped to an API under a human-facing site: an allow-list pins the entry to read-only paths so a user credential can never reach a write verb. Service notes: [Airtable](../api/airtable-api.md), [GitHub](../../plugins/github/docs/github-api.md), [GCP](../api/gcp-api.md).

Allow-listing is verb-scoped: GETs are checked against `allowed_endpoints`, POSTs against the independent `allowed_post_endpoints`, and a host without a POST list rejects every POST. Only GCP entries whose reads Google exposes as POST verbs declare one ([authed_post](#post-capable-sibling-authed_post)).

### Service Default Headers

An entry's `default_headers` go on every request to that service, merged in `_make_authed_request()` with this precedence: service defaults, then the caller's allow-listed headers, then `inject_auth`, which always wins so auth cannot be clobbered. The 401 retry merges the same way. The GitHub plugin's entry sends a User-Agent (GitHub rejects requests without one) and its versioned `Accept`; Federal Register sends a User-Agent.

### Caller Header Allow-list

Caller headers (the tools' `headers` argument or the sandbox proxy bodies' `headers` field) are limited to the content-negotiation names in `_ALLOWED_CALLER_HEADERS`, compared case-insensitively; any other name is rejected with a JSON error before credential loading or network I/O. Caller headers ride on requests carrying the user's credential (for Google, the broad `cloud-platform` bearer) and the path allow-lists cannot see them: `X-HTTP-Method-Override` can turn a read into a write, `X-Goog-User-Project` re-bills quota, and `X-Goog-Request-Reason` lands verbatim in the target project's audit logs, an exfiltration channel. Rejecting rather than stripping gives the model an actionable error.

### Design Decisions

**Why inject credentials server-side?** Putting keys in the model's context (tool descriptions, history, responses) would leak them into logs, cached sessions and user-visible output.

**Why a hostname registry with optional path prefixes?** Hostname matching is unambiguous for most APIs; path prefixes (e.g. `www.googleapis.com/calendar/v3`) split shared hosts. A URL-pattern registry was unnecessary for the APIs in use.

### Proxy Endpoint (`POST /api/authed-get`)

Sandbox scripts reach the same handler through `POST /api/authed-get` (`authed_get_endpoint()`, body `AuthedGetRequest`), which `register_sandbox_api_routes()` in `chat/sandbox_api.py` mounts on the main app and the sandbox tool API. On the sandbox port only the run's sandbox token authenticates ([Script Runner](script-runner.md#sandbox-tokens)); the main app uses `get_current_user_cookie_or_apikey` (`auth/session.py`), which enforces the admission policy ([Auth](auth.md)). Unlike the tool, it returns binary bodies (Drive `alt=media`). `_BLOCKED_PROXY_PATHS` in `chat/route_dispatch.py` keeps `curl_proxy_*` off it ([Route Dispatch -- Blocked Proxy Paths](route-dispatch.md#blocked-proxy-paths)). The `system:workspace` skill documents it for scripts.

### POST-capable Sibling (`authed_post`)

`authed_post` (`handle_authed_post()`; sandbox route `POST /api/authed-post`, `authed_post_endpoint()`, body `AuthedPostRequest`) serves the reads Google exposes only as POST verbs ([GCP API](../api/gcp-api.md)). It is not a general POST proxy: the path must match the host's `allowed_post_endpoints`, and write verbs appear on no POST list. It shares registry lookup, credential loading, 401 retry, header precedence, the size gate and `output_file` with `authed_get` through `_make_authed_request()`'s `method` / `json_body` parameters. The route is in `_BLOCKED_PROXY_PATHS` too.

### Script Tool-Call Bridge (`POST /api/tool-call`)

`POST /api/tool-call` (`script_tool_call_endpoint()` in `chat/gemini_api/script_tool_call.py`) lets sandbox code call an allow-listed subset of the dynamic tools with the meta tool's `{tool_name, arguments}` shape. It validates `tool_name` against `SCRIPT_TOOL_CALL_ALLOWLIST` (400 with the list otherwise) and runs the shared `_dispatch_tool_call()` with `conversation_id=None` and `is_script=True`, so coercion, errors and notices match agent calls; non-JSON results return as `text/plain`.

The list is explicit, so registry tools stay script-invisible until added: the Gmail Simple and Quest-label tools, the memory reads, the Quest Docs and Quest Tables reads (`run_kind="script"`: no project docs or tables, read-only -- [Quest Docs -- Script Bridge](quest-docs.md#script-bridge)) and each plugin manifest's `script_tool_allowlist` via `extend_script_allowlist()` (the only script path to the Slack and Telegram tools, which have no HTTP routes). Arguments naming workspace files (e.g. `send_slack_dm_to_self`'s `files`) fail over the bridge, which has no conversation. Excluded on purpose: `wait_for_handles` (raises loop sentinels), conversation- or project-scoped tools (`set_conversation_name`, `project_db_query`, `get_response_content`, the workspace-download tools), file tools (scripts have the mounts; `get_workspace_file` returns provider parts), and `authed_get` / `authed_post` (their routes handle binary bodies).

Auth matches `/api/authed-get`, and the route is in `_BLOCKED_PROXY_PATHS` so the model uses `tool_call` instead. The `system:workspace` skill documents it for scripts.

### Large Response Protection

`authed_get` / `authed_post` bodies over `AUTHED_GET_SIZE_LIMIT` (`chat/gemini_api/constants.py`) are refused by `_apply_size_gate()` with a `response_too_large` error telling the model to narrow the request, opt into blob storage with `force_large_response`, or save the body with `output_file`; error responses always pass. With `force_large_response`, the body is stored under the conversation directory's `_RESPONSE_BLOB_DIR`, named by a content hash, and `get_response_content(hash, offset, length)` (`_handle_get_response_content()` in `chat/gemini_api/tool_handlers/response_blobs.py`) reads it back in chunks of at most `GET_RESPONSE_CONTENT_MAX_CHUNK` characters.

`output_file` skips the gate: `_handle_authed_get_to_file()` writes the body under the hidden `.responses/` directory of the conversation workspace (also in project conversations) and returns a receipt whose `path` the model must reuse. It never overwrites (an existing file is an error), synthesizes a hash-based name for a trailing `/` or existing directory, rejects absolute and `..` paths before the upstream call, and returns upstream errors unchanged without writing. It also unlocks `alt=media` and wins over `force_large_response`. It is tool-only; internal callers of `_make_authed_request()` and the sandbox routes do not take it.

**Why a size gate?** Full listings (Gmail threads, Drive files, Airtable dumps) burn context; refusing by default pushes the model toward `fields`, filters and paging before it opts in.

**Why chunked blobs and `output_file` both?** Blobs serve a model that still wants to read the body itself, a page at a time; `output_file` serves the common case where the body goes to `run_python` / `run_script` / `get_workspace_file`, skipping staging and chunk round trips. Bodies land under hidden `.responses/` because users rarely want them in the file browser, and collide rather than overwrite so a later turn can rely on an earlier saved body.

**Why character offsets?** Byte offsets could split multi-byte UTF-8 characters; the cost is reading the whole blob per chunk (see the NOTE in `_handle_get_response_content()`).

## System Prompt

`chat/gemini_api/system_prompt.py` builds every prompt: `get_system_prompt()` (top-level, incl. Slack and routine runs), `get_sub_agent_system_prompt()`, `get_public_project_system_prompt()`, `get_user_subagent_system_prompt()` and `get_inference_api_system_prompt()`. Section order is in each builder. A prompt becomes the session's system instruction, so it is fixed for the session's life; that is why connecting a service or changing settings invalidates sessions ([In-Memory Chat Session Store](#in-memory-chat-session-store)). What spans files:

- **Backend docs are skills.** The prompt carries only the system-skills enumeration from `build_system_skills_enumeration()` (`chat/system_skills/loader.py`), filtered by the `connected_services` dict from `get_user_connected_services()` (`api/instructions.py`: core credentials plus one key per loaded plugin), so unconnected services stay invisible; `None` enumerates everything. Each skill line names the `tool_call` tools it documents ([Prompt Tiers](#prompt-tiers)). The model loads backend docs with `load_skills` ([Skill Library -- System Skills](skill-library.md#system-skills)). The only inlined API text is the proxy preamble (`_build_proxy_preamble()`).
- **Instructions and skills.** Custom instructions come from a guide snapshot or an explicit guide, only while the `guides` gate is open ([Guides](guides.md)). Auto-loaded skills (user and project tiers, deduped by id, resolved fresh each turn) arrive as `skills_content` and are passed to sub-agents too ([Skill Library](skill-library.md)).
- **Saved memories.** The top-level prompt carries the user's active memories as a "Saved Memories" section (`memories_content`, rendered by `chat/memory_prompt.py`: newest first as `<memory id created>` entries, capped by `MAX_INJECTED_MEMORIES` and `MAX_INJECTED_MEMORY_CHARS`, with a closing line counting the older memories left out, pointing the model at `memory_list` / `memory_search`). The section is as of the session's start (a memory saved later is not re-injected until the session is rebuilt, and the prompt says so). A store failure drops the section rather than the turn. Public-project, cross-user subagent, inference-API and sub-agent prompts never carry memories.
- **Project text.** Project conversations get the project instructions, a `system:project_db` pointer, the "Two file spaces" paragraph (`_TWO_FILE_SPACES_SECTION`, via `_project_files_section()`) and the notice notes below; a standalone prompt carries none of it.
- **Connected accounts.** See [User Connections -- Connected Accounts Prompt Paragraph](user-connections.md#connected-accounts-prompt-paragraph).
- **Routine runs** (`is_routine`) replace the conversation-naming instruction with a note and drop `set_conversation_name` ([Routines](routines.md)).
- **Guidance prose** -- the rules, sub-agent guidance (parallel vs template, retry a failed sub-agent instead of doing its work, the scout / batch / aggregate pattern for large data, per-model choice and batch sizes), the inline-image convention and the examples -- is written in `get_system_prompt()`; edit it there.

The sub-agent prompt mirrors the top-level one (identity, envelope, enumeration, skills, connected accounts, project text), lists its own tier, tells it to finish with `agent_task_response` without follow-up questions, and forbids the top-level-only tools, telling it to return approval-gated writes to the parent; with `can_nest` it permits `agent_task_nested` ([Nested Sub-Agents](#nested-sub-agents)).

### Workspace Notice Flags

The `legacy_shared_workspace` and `converted_from_standalone` flags in `chat_history.json` (`CONVERSATION_NOTICE_FLAGS` in `chat/storage.py`) add notes to the project prompts of the top-level, sub-agent and public-project builders; how they are set is in [Projects](projects.md). `run_conversation_turn()` reads them with the guide snapshot from one `chat_history.json` read (`_read_chat_history()`, `_resolve_workspace_notice_flags()`), and sub-agents receive them through `RunContext.workspace_notice_flags` without re-reading.

## Deprecated Model Remapping

`_DEPRECATED_MODELS` in `chat/gemini_api/constants.py` maps retired model ids to their replacements wherever a model id enters the loop (the turn's model and every sub-agent spawn arm; grep its uses). The frontend mirror is `DEPRECATED_MODEL_MAP` in `frontend/src/constants/models.ts`. Retired ids stay in the `ModelId` enum (`db/models.py`) for historical analytics rows.

## In-Memory Chat Session Store

`_active_chats` in `chat/gemini_api/session.py` holds one live provider session per `(user_id, conversation_id)`, so users are isolated and each conversation keeps its own state. `get_or_create_chat()` returns it or builds one via `provider.create_session()` with the system prompt, the run's tool tier and, on Gemini, automatic function calling disabled. Sub-agent sessions never enter the store.

`remove_chat_session()` drops one session (e.g. after a cancelled turn or a compaction) and `invalidate_user_sessions()` drops all of a user's (settings changes, every connect / disconnect path); grep their callers. The next message rebuilds the session from disk with a fresh prompt.

### SDK History Persistence

`sdk_history.json`, beside `chat_history.json`, holds the provider-native session history: `_save_sdk_history()` in `chat/gemini_api/history.py` serializes through `provider.save_history()` into a `{provider, history}` envelope, and `_load_sdk_history()` returns `(history, provider_name)`. Both log and swallow failures.

`run_conversation_turn()` in `chat/gemini_api/conversation.py` saves once per tool-requesting model response, before dispatching (`_persist_sdk_history()`, in a worker thread, completing even if the run is cancelled). Dispatch never touches the session, so the file ends on the dangling tool_use(s) a suspend, Stop or restart mid-tool resumes from ([Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume); [Slack Socket Mode -- Restart Resilience](slack-socket-mode.md#restart-resilience)). Turn end and errors save once more, synchronously after the final flush; a cancelled turn is not saved (see Interrupted Turns).

### Model Change History Preservation

`run_conversation_turn()` loads disk history every turn and, with no live session (e.g. after a restart), seeds one from it when the providers match (`get_provider_for_model()` in `chat/llm/config.py`). On a model switch, `get_or_create_chat()` carries the old session's history over within a provider family, falls back to a disk copy that matches the target provider across families, and otherwise starts empty. The frontend locks the provider after the first message, so cross-provider switches come mainly from routines whose model differs from the conversation's.

### Interrupted Turns

On cancellation, `_run_send_message` calls `_save_interrupted_sdk_history()` (`chat/routes/_helpers.py`), which appends the partial assistant text and an interruption marker to the on-disk envelope's last assistant entry (an envelope ending on a dangling tool_use is left for the resume bucket), then `remove_chat_session()`, so the next turn rebuilds from disk and the model knows it was stopped. Partial text reaches `chat_history.json` through `_flush_partial_text()` in `_stream_turn()`; the Anthropic and OpenRouter providers flush their in-progress block on `CancelledError`.

## Sub-Agent Execution

The spawn arms in `chat/gemini_api/turn_tools.py` call `_run_sub_agent()` / `_run_parallel_sub_agents()` in `chat/gemini_api/sub_agent.py`:

- `agent_task` runs one sub-agent. More than `MAX_AGENT_TASKS_PER_TURN` calls in one response get an error steering the model to the parallel tool.
- `agent_task_parallel` runs a batch concurrently (`asyncio.gather()`, at most `MAX_PARALLEL_TASKS`, non-empty unique task ids). `run_one_task()` isolates failures, and the result maps each task id to `status`, `name` and `response` or `error`.
- `agent_task_parallel_template` renders one `prompt_template` per entry of `agents` (`_run_parallel_sub_agents_template()`, at most `MAX_PARALLEL_TEMPLATE_TASKS`), with a required `model` from `TEMPLATE_BATCH_ALLOWED_MODELS`.

A sub-agent runs its own streaming loop on an ephemeral session with `get_sub_agent_system_prompt()` and the `SUB_AGENT_TOOLS` tier, shares the conversation workspace (and `proj://` in a project) and the parent's user, and dispatches shared tools through `_dispatch_tool_call()`, on the `model` argument or the parent's (any provider). It ends on `agent_task_response`, after which the loop acknowledges the call and drains the final response so the stream closes and usage is recorded; without that call its accumulated text is returned. An exception becomes a JSON error tool result. Beyond the parent's context it takes the nesting parameters below and `parent_tool_id`, grouping events under the spawning call.

Bounds: `get_sub_agent_turn_limits(model)` (`chat/gemini_api/constants.py`) sets a hard turn cap and an earlier warning turn, lower for models with a small registry `max_input_tokens`; a context warning fires at `SUB_AGENT_CONTEXT_WARNING_THRESHOLD` of `max_input_tokens` (via `compute_total_context_tokens()`). Both warnings ride `provider.inject_turn_warning()` and tell the sub-agent to return its findings immediately.

Events: the sub-agent's tool calls stream as transient `sub_agent_tool_use` / `sub_agent_tool_result`, and `sub_agent_finished` (every terminal path, with parent-side fallbacks when the sub-agent raises first) is the frontend's done signal. `_capturing_on_event` attaches the captured events to the parent `tool_result` as `sub_agent_tool_calls`, display-only and never in SDK history ([Realtime -- Sub-Agent Events](realtime.md#sub-agent-events); rendering in `ToolUseMessage.tsx`, see [Frontend](frontend.md)). Usage: see [Token Usage Tracking](#token-usage-tracking).

### Sub-Agent Model Restriction

Sub-agents may not run on the models in `SUB_AGENT_DISALLOWED_MODELS` (`chat/gemini_api/constants.py`, whose comment says why), whether chosen or inherited. The check runs after the `_DEPRECATED_MODELS` remap, so a retired alias is blocked too, and before anything spawns: `_handle_agent_task()` (`chat/gemini_api/turn_tools.py`), `_run_parallel_sub_agents()` for both parallel tools, and a guard at the top of `_run_sub_agent()`. A disallowed model returns a model-readable error suggesting an alternative, so the model re-spawns on the same turn. `TEMPLATE_BATCH_ALLOWED_MODELS` excludes these models structurally.

### Nested Sub-Agents

With the conversation's `nested_subagents` flag ([Conversation Flags](conversation-flags.md)), a 1st-level sub-agent may spawn one tier of 2nd-level sub-agents; there is no 3rd level. The flag reaches the spawn arms as `nested_enabled`, and `_run_sub_agent()` takes `level` (1 or 2) and `nested_parent_id`:

- **Tier**: a 1st-level sub-agent with the flag gets `SUB_AGENT_TOOLS_NESTED` and the can-nest prompt; a 2nd-level one always gets `SUB_AGENT_TOOLS`. `get_system_prompt(nested_subagents=...)` adds a note to the top-level sub-agent guidance.
- **Dispatch**: the `agent_task_nested` branch of `_run_sub_agent()`'s loop remaps the required `model`, checks it and recursively calls `_run_sub_agent(level=2)`; nested usage is rolled into the 1st-level agent's accumulator.
- **Models**: `NESTED_SUB_AGENT_ALLOWED_MODELS` (`chat/gemini_api/constants.py`), the cheapest models, never a disallowed one. A copy in `chat/llm/tool_schemas.py` (avoiding a circular import) feeds the `model` enum; keep the two in sync. The check runs in the dispatch branch (an error result the 1st-level agent corrects from) and at the top of `_run_sub_agent()` for `level == 2`.
- **UI**: the `agent_task_nested` call is a NODE `sub_agent_tool_use` / `sub_agent_tool_result` pair carrying `nested_agent_*` fields, and the grandchild's events carry `nested_parent_id` ([Realtime](realtime.md#sub-agent-events)).

## Token Usage Tracking

Every model call -- top-level turns, sub-agent turns and the sub-agent drain turn -- is recorded by `record_api_call()` (`db/llm_call_store.py`) into its provider's raw analytics table with call type (`ApiCallType`), agent name, `backend` (`get_backend_for_model()`), nesting `level`, duration and the provider-native `raw_usage` fields; the coalesced counts serve only when a stream returned no usage object. Recording failures are logged, never raised. Columns: [Database](database.md#llmcallgemini-and-llmcallanthropic-models).

Separately, the run's `UsageAccumulator` (`chat/gemini_api/usage.py`) sums the top-level rounds (`add_turn()`) and the `usage_accumulator` dicts the sub-agent runners fill (`add_sub_agent()`, called from the spawn arms in `chat/gemini_api/turn_tools.py`), and `build_stats()` produces the `stats` event. Each sub-agent computes its `new_input_tokens` with its own provider, so mixed-provider batches add up correctly, and the parallel runner merges its per-task dicts before handing them up. Passing a mutable dict down was chosen over shared counters so concurrent sub-agents need no global state.

### Stats Event

The `stats` event emitted at the end of each turn is the dict built by `UsageAccumulator.build_stats()`; its frontend type is `UsageStats` in `frontend/src/api/types.ts`. The shape, which the code cannot explain on its own:

- the unprefixed token fields are totals across the top-level agent and every sub-agent, accumulated across ALL tool-call rounds of the turn; the `top_level_*` and `sub_agent_*` prefixed twins are the same metrics split by call type, and `sub_agent_call_count` counts sub-agent API calls
- `new_input_tokens` (and its prefixed twins) is the provider-uniform uncached-work measure from `compute_new_input_tokens()`; `cached_tokens` is the provider's own cached count; the cache creation / read split is Anthropic-specific and stays 0 on providers without it
- `provider` and `model` identify the top-level agent's provider family and model id (the frontend uses them for the provider-specific tooltip breakdown and the model name in message tooltips)
- `duration_ms`, `tool_calls` and `turns` describe the whole turn (wall-clock, top-level tool calls, model rounds)
- `context_tokens` is the context-window occupancy after the last top-level round via `compute_total_context_tokens()` (provider-specific counting rules live there); `max_context_tokens` is the model's registry `max_input_tokens`; the composer's `ContextIndicator` displays the pair

The stats bar is described in [Frontend -- Message Component with Markdown Rendering](frontend.md#message-component-with-markdown-rendering).

## Logging

Sub-agent activity logs under `[agent_task]` / `[agent_task_parallel]` / `[agent_task_parallel_template]` (spawn, completion, failure, from `turn_tools.py`) and `[sub-agent:<name>]` (each tool call, termination, turn and context warnings, from `sub_agent.py`); top-level failures log via `logger.exception()` in `run_conversation_turn()`. All carry `user=<email>` ([Logging -- User Identification Convention](logging.md#user-identification-convention)).

## Design Decisions

**Why disable automatic function calling?** The loop must stream `tool_use` / `tool_result` events, apply its own dispatch checks, and turn tool failures into results rather than exceptions.

**Why a separate `sdk_history.json`?** `chat_history.json` holds display messages and the provider history holds native content; one file would need a lossy translation in both directions.

**Why save SDK history per tool-requesting response rather than only at turn end?** A suspended turn (a Slack reply or approval card can wait for days) ends at a dangling tool_use that a restart or Stop must find on disk. The turn-end save is synchronous so a Stop cannot land between it and the return and double-append the turn's text.

**Why put the interruption marker on the assistant message?** A separate user message would break the strict user / assistant alternation Anthropic requires.

**Why swallow publish failures during a run?** A closed socket or bus error must not abort the run or roll back the disk write; subscribers recover through catchup.

**Why pre-flight attachment size checks instead of provider-level caps alone?** An attached part is replayed on every later turn, so an oversized one would break the conversation permanently; the handlers turn it into a normal tool error. Unknown models get the smallest cap, and the provider caps stay as defense in depth ([LLM Provider Abstraction -- Attachment Size Limits](llm-providers.md#attachment-size-limits)).

**Why a MIME deny-list instead of an allow-list?** Gemini accepts a wide and growing set of types; the deny-list blocks the known-bad groups (commented on `_UNSUPPORTED_GEMINI_MIME_TYPES`) and admits the rest.

**Why `extra_parts` beside the function response?** Attached files cannot sit inside a function-response part; each provider's `format_tool_results()` places them next to it.

**Why scheme-qualified paths instead of a tool family per space?** A mandatory `chat://` / `proj://` scheme puts the space in every call and transcript and makes a bare path fail loudly instead of landing in the wrong space; one five-tool set covers both spaces and copies.

**Why tool tiers?** Sub-agents must not spawn (unbounded recursion) or block on the user (no UI channel), and composing tiers from `BASE_TOOLS` avoids duplicating the shared declarations.

**Why turn caps for sub-agents but not the top-level loop?** A sub-agent runs unsupervised and could loop forever; the top-level user watches and can Stop, and a cap there would silently cut off multi-step work. The two warnings let a sub-agent wind down with partial findings.

**Why `agent_task_parallel` instead of several `agent_task` calls?** Tool calls in one response run sequentially, so only an explicit batch runs concurrently, and one result keyed by task id is easier to map than separate results. The per-turn `agent_task` cap pushes the model to the batch; `MAX_PARALLEL_TASKS` bounds a batch because every sub-agent opens its own model session and may call tools.

**Why drain the response after `agent_task_response`?** Returning without consuming the final stream would leave its HTTP connection open and the session history incomplete.

**Why co-locate `get_instructions()` with each API module?** The endpoint code and its model-facing usage notes change together; the system-skill catalog (`chat/system_skills/catalog.py`) only binds skill ids to them.

**Why record usage per call in the database as well as in `stats`?** The `stats` message lives in each conversation's JSON, so cross-conversation analytics would mean parsing every file; per-call rows make per-user and per-model SQL cheap.

## Constraints

- Sessions are per-process memory; after a restart a conversation continues from `sdk_history.json` only when its provider family matches the model in use.
- On Vertex there is no file-upload round trip: `GeminiProvider.upload_file()` inlines bytes as a `Part`, subject to the caps in `chat/llm/file_limits.py`.
- `GET /api/instructions` (`quest.py`) renders every backend's `get_instructions()` regardless of connection status.
