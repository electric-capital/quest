# Logging Architecture

This document describes the unified logging configuration used across the Quest backend. All application loggers and uvicorn loggers share a single format, producing consistent output with colored level labels, timestamps, process IDs, and module names.

## Loggers

Six named loggers are configured, all at INFO level by default (the `chat`, `auth`, and `quest` loggers are promoted to DEBUG level in dev mode -- see [Dev Mode Debug Logging](#dev-mode-debug-logging) below):

| Logger | Source modules | Description |
|--------|---------------|-------------|
| `uvicorn` / `uvicorn.error` | uvicorn internals | Server startup, shutdown, and error messages |
| `uvicorn.access` | uvicorn internals | HTTP access log (client address, request line, status code) |
| `chat` | `chat.gemini_api`, `chat.route_dispatch`, `chat.routes`, `chat.scheduler`, and other `chat.*` modules | Application-level logging for Gemini API calls, tool dispatch, sub-agent execution, WebSocket lifecycle, streaming events, and scheduler decision logging |
| `auth` | `auth.google_login`, `auth.google_services`, `auth.google_credentials`, and other `auth.*` modules (plugin login routers such as `plugins.slack.oauth` and `plugins.telegram.auth` log under their own module names) | OAuth flow events, token refresh, credential management, and authentication logging |
| `quest` | `quest.py` | API key management and proxy-level logging |
| `large_tool_results` | `chat.gemini_api.tool_dispatch` (`_log_large_tool_result()`), called from `tool_dispatch.py` and `conversation.py` | File-based logger for tool call results exceeding 2048 bytes; writes JSONL to `data/logs/large_tool_results.jsonl` via a `RotatingFileHandler` (50MB max, 3 backups) |

## Log Format

All loggers use uvicorn's `DefaultFormatter` (or `AccessFormatter` for access logs) to produce colored level labels via the `%(levelprefix)s` placeholder.

**Default format** (server, application, and OAuth logs):

`%(levelprefix)s %(asctime)s [%(process)d] %(name)-20s %(message)s`

**Access format** (HTTP request logs):

`%(levelprefix)s %(asctime)s [%(process)d] %(name)-20s %(client_addr)s - "%(request_line)s" %(status_code)s`

**Large tool results format** (file-based, see below):

`%(message)s` (each line is a complete JSON object that already includes a timestamp; no wrapper formatting needed)

**Date format**: `%Y-%m-%d %H:%M:%S`

Console loggers output to stderr. The `large_tool_results` logger outputs to `{DATA_DIR}/logs/large_tool_results.jsonl` (where `DATA_DIR` is from `config/paths.py`, defaulting to `data/`; see Large Tool Result Logging below and [Data Paths](data-paths.md)).

## User Identification Convention

All user-driven activity log entries include the user's email address using the pattern `user=<email>`. This convention was established in `chat/route_dispatch.py` and is followed across all modules that log user-initiated actions:

- `chat/route_dispatch.py` -- `user=<email>` on every successful tool dispatch and every error
- `chat/gemini_api/` submodules -- `user=<email>` on sub-agent spawn/completion/failure (in `sub_agent.py`), sub-agent tool calls, sub-agent turn limit warnings, sub-agent context window warnings, and top-level errors (in `conversation.py`)
- `chat/realtime/socket.py` -- `user_id=<id>` (the persistent WS authenticates by user_id) on heartbeat / queue-overflow / auth-revalidation events; `chat/realtime/socket.py:_run_send_message` logs cancellation, interrupted markers, and run failures with the conversation id
- `auth/*` modules and plugin login routers -- `user=<email>` on OAuth events (token refresh, login, service authorization), service key saves, Telegram login events (`plugins/telegram/auth.py`), and service token operations
- `quest.py` -- `user=<email>` on API key resets and instruction endpoint requests

This makes it straightforward to filter logs for a specific user by searching for `user=someone@example.com`.

## Quest Tables Diagnostics

The tables service (`chat/tables/service.py`) and routes (`chat/tables/routes.py`) log on the `chat` logger with a `[tables]` prefix, so an intermittent failure on a table file leaves evidence ([Quest Tables -- Service Layer](quest-tables.md#service-layer)):

- `[tables] created <id> (<user|project> table, <mode>, N columns) source=... user=<email> lock_wait=<ms> file=<ms> row=<ms> <dir report>` (INFO): every create, with the per-owner lock wait and the durations of the file layout and the `quest.db` insert. The stamp a later failed read is matched against.
- `[tables] read of <id> failed: <code> (<message>) lock_wait=<ms> call=<ms> <dir report> user=<email>` (WARNING): a read whose file call raised (`busy`, `storage_missing`, `schema_mismatch`, an unexpected error), with the time spent waiting for the per-table lock and in the call.
- `[tables] slow read of <id>: lock_wait=<ms> call=<ms> <dir report> user=<email>` (WARNING): a read that succeeded but waited for the lock or ran past `TABLE_SLOW_READ_LOG_S` (`chat/tables/constants.py`).
- `[tables] storage refusal on <id>: <code> -> <status> (<message>)` (WARNING, routes): every file-layer refusal a route turned into a non-500 response, such as a 503 `busy`; a 500 logs `[tables] storage error on <id>: <code>` with the traceback instead.

The dir report is `storage.describe_table_dir()`: `dir=missing`, or `dir=present` followed by `table.db=<size>B age=<seconds since its last change>` and `table.db-wal` / `-shm` / `-journal` as `<size>B`, `absent` or `not-a-file`, as they were at that moment.

In the browser, `useTable` and `useTableRows` (`frontend/src/hooks/`) log `[tables] detail fetch failed` and `[tables] rows query failed` to the console with the request as sent, the response status, code and body (or the network error's name and message, "Failed to fetch" for a dropped connection) and `navigator.onLine`.

## Scheduler Decision Logging

The background scheduler daemon (`chat/scheduler.py`) produces structured log output at INFO level for every poll cycle. All scheduler log lines use the `[scheduler]` prefix. See [Scheduling Architecture](scheduling.md) for full details on the scheduler daemon.

**Per-routine decisions**: Each enabled schedule gets an INFO log line showing whether it fired or was skipped, with a human-readable reason:

- `[scheduler] <routine_name>: FIRE -- <reason> (schedule=<id>, type=<type>)`
- `[scheduler] <routine_name>: skip -- <reason> (schedule=<id>, type=<type>)`

**Poll cycle summary**: After all schedules are checked, a summary line reports totals (only when enabled schedules exist):

- `[scheduler] Poll complete: checked <N> schedule(s), fired <N> (now=<HH:MM:SS> UTC)`

**Execution lifecycle**: Each scheduled run logs start, completion, and failure:

- `[scheduler] Executing scheduled run: routine=<name>, project=<id>, user=<id>, type=<type>`
- `[scheduler] Completed scheduled run: routine=<name>, conversation=<id>`
- `[scheduler] Failed scheduled run: routine=<name>, schedule=<id>, conversation=<id>` (with exception traceback)

**Other scheduler events**: Startup, shutdown, DST reconversion, and stale flag cleanup:

- `[scheduler] Routine scheduler started (poll interval: <N>s)`
- `[scheduler] Scheduler loop cancelled, shutting down`
- `[scheduler] DST reconversion: schedule <id> UTC time <old> -> <new>`
- `[scheduler] Cleared <N> stale running flags`

Note: Scheduler log lines do not include the `user=<email>` convention because scheduled runs are system-initiated, not user-driven. The routine name and schedule ID provide sufficient context for tracing.

## Large Tool Result Logging

Tool call results that exceed 2048 bytes are captured to a dedicated log file for analysis. This provides visibility into large data flowing through tool calls without cluttering the main application logs.

**Logger**: `large_tool_results` (defined in `chat/gemini_api/tool_dispatch.py` as `_large_tool_logger`)

**Log file**: `data/logs/large_tool_results.jsonl`

**Rotation**: `RotatingFileHandler` with 50MB max file size, 3 backup files, UTF-8 encoding (configured in `chat/logging_config.py` and `logging_config.json`)

**Threshold**: Tool results are only logged when the UTF-8 encoded result exceeds 2048 bytes (`_LARGE_RESULT_THRESHOLD` in `chat/gemini_api/tool_dispatch.py`)

**Captured data**: Each entry is a single-line JSON object containing:
- `timestamp` -- ISO 8601 UTC timestamp
- `tool_name` -- name of the tool that produced the result
- `tool_args` -- the parameters sent to the tool
- `result` -- the full result string
- `result_length_bytes` -- size of the result in bytes
- `model` -- the LLM model ID (e.g., `gemini-3.1-pro-preview`)
- `provider` -- derived from model via `get_provider_for_model()` in `chat/llm/config.py`
- `call_type` -- `top_level` or `sub_agent`
- `agent_name` -- sub-agent display name (only set when `call_type` is `sub_agent`)
- `user_email` -- the user's email address
- `conversation_id` -- the conversation UUID

**Call sites**: The `_log_large_tool_result()` function is called from two files:
- `chat/gemini_api/tool_dispatch.py` -- after every `_dispatch_tool_call()` completes (covers all dispatched tool calls including route dispatch, workspace files, memory, and script runner tools)
- `chat/gemini_api/conversation.py` -- once per loop-handled arm (the `TURN_TOOL_HANDLERS` registry in `chat/gemini_api/turn_tools.py`: sub-agent tasks, parallel sub-agents, action requests, wait handles) after the handler returns

**Directory creation**: The log directory (`LOG_DIR` from `config/paths.py`, defaults to `data/logs/`) is created automatically in two places to handle both startup paths:
- At import time in `chat/logging_config.py` (covers programmatic startup via `quest.py`)
- In `run.py` before launching the uvicorn subprocess (covers CLI-based startup where `logging_config.json` is applied)

## Dev Mode Debug Logging

When `run.py` starts in dev mode (the default, or with `--dev`), the `chat`, `auth`, and `quest` loggers are promoted to DEBUG level. This is done by loading `logging_config.json`, patching the logger levels in memory, writing the modified config to a temporary JSON file, and passing that file to uvicorn's `--log-config` flag. The original `logging_config.json` is not modified. In production mode (`--prod`), loggers remain at their default INFO level.

DEBUG-level output includes:
- Verbose Docker-mode streaming lines in `chat/routes/_helpers.py` (raw stream JSON, parsed JSON, tool_use/tool_result forwarding) when Docker chat mode is configured
- Persistent-WS publish failures in `chat/realtime/socket.py` and `chat/storage.py:_publish_appended_to_bus` (queue overflow, transient publish errors)
- Anthropic prompt cache statistics in `chat/llm/anthropic_provider.py` (input, cache_read, cache_creation, and output token counts after each API call)

## How Config Is Applied

There are two paths for applying the logging configuration, depending on how uvicorn is started:

1. **Programmatic** (`quest.py` main block) -- Calls `get_log_config()` from `chat/logging_config.py` and passes the dict directly to `uvicorn.run(log_config=...)`. Used when running the app via `uv run python quest.py`.

2. **CLI-based** (`run.py`) -- Passes `--log-config logging_config.json` to the uvicorn command. The JSON file contains the same configuration as the Python dict. Used by `run.py` for both development and production.

Both paths produce identical output. The JSON file exists because uvicorn's CLI `--log-config` flag requires a file path, not a Python dict.

## Design Decisions

**Why unified logging instead of per-module handler setup?**
Previously, `chat/__init__.py` configured its own `StreamHandler` and `Formatter`, which could conflict with uvicorn's default logging. The unified config ensures all log output -- uvicorn server messages, HTTP access logs, application logs (`chat`), auth logs (`auth`), and proxy logs (`quest`) -- shares the same format and handlers, eliminating duplicate or inconsistent output.

**Why use uvicorn's formatters?**
Uvicorn's `DefaultFormatter` and `AccessFormatter` provide colored level labels (`INFO:`, `WARNING:`, `ERROR:`) that work well in both TTY and non-TTY environments. Color auto-detection is built in (the `use_colors` parameter defaults to `None` for auto-detect).

**Why both a Python module and a JSON file?**
The Python dict config (`get_log_config()`) is used when starting uvicorn programmatically. The JSON file (`logging_config.json`) is the equivalent for CLI-based invocations where only a file path can be passed. Both are kept in sync manually.

**Why convert print() to logger calls in chat/routes/, auth/ modules, and quest.py?**
`print()` calls in the chat routes package (WebSocket streaming and stop handling) and auth modules (OAuth flow events, now in `auth/` submodule) were converted to `logger.*()` calls so they participate in the unified format with timestamps and level labels. This also allows verbose WebSocket streaming output to be placed at DEBUG level (see `chat/routes/_helpers.py`), keeping INFO-level logs focused on meaningful events rather than per-chunk streaming noise.

**Why include user=\<email\> in all user-driven log entries?**
When multiple users are active simultaneously, log entries without user identification make it difficult to trace a specific user's request through the system. The `user=<email>` convention, established in `chat/route_dispatch.py` and adopted across `chat/routes/` submodules, `chat/gemini_api/` submodules, `auth/*` modules, and `quest.py`, allows filtering all logs for a single user's session. The pattern uses a simple `user=value` format rather than structured logging fields, keeping log messages human-readable while remaining easy to `grep`.

**Why a separate file-based logger for large tool results?**
Large tool call results (e.g., full API responses, file contents) can be thousands of bytes and would overwhelm the main application log if included inline. Writing them to a dedicated rotating log file keeps the stderr logs clean while preserving the full result data for debugging and analysis. The 2048-byte threshold balances capturing meaningful large results without logging every small tool response.

**Why use DEBUG level for verbose WebSocket streaming output?**
The per-chunk streaming lines in `chat/routes/_helpers.py` (raw stream JSON, parsed JSON, tool_use/tool_result forwarding) are useful for debugging protocol issues but produce high volume during normal operation. Placing them at DEBUG level keeps the default INFO output clean while still making them available when needed via log level configuration.

**Why automatically enable DEBUG in dev mode?**
DEBUG-level output (WebSocket streaming details, Anthropic cache stats) is essential during development but too noisy for production. Rather than requiring developers to manually configure log levels, `run.py` patches the config automatically in dev mode via a temporary file, keeping the committed `logging_config.json` clean at INFO level for production use.

