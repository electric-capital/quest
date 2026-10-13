# Route Dispatch (Internal Tool Execution)

This document describes the internal route dispatch mechanism in `chat/route_dispatch.py`, which allows Gemini tool calls to invoke FastAPI endpoint handlers directly without making actual HTTP requests.

## Overview

Route dispatch is the foundation layer of the API mode integration. It replaces the Docker-based tool execution (where Gemini CLI runs shell commands like `curl`) with a direct Python dispatch pipeline. When the LLM issues a tool call like `curl_proxy_get(url="http://localhost:8000/api/gmail-simple/messages/123")`, the route dispatcher parses the URL, finds the matching FastAPI route, builds the handler's keyword arguments by introspecting its signature, and calls the handler function directly -- no HTTP roundtrip involved.

## Dispatch Pipeline

The full tool call flow is orchestrated by `execute_tool_call()` in `chat/route_dispatch.py`:

```
1. Tool call received (tool_name, tool_args)
2. Map tool_name to HTTP method via _TOOL_METHOD_MAP
   - "curl_proxy_get" -> GET
   - "curl_proxy_post" -> POST
3. validate_url(tool_args["url"])
   - Parse URL, enforce localhost with allowed port (8000 + proxy port from get_proxy_port()), enforce /api/* path prefix
   - Extract query parameters
4. resolve_route(app, method, path)
   - Iterate app.routes, use Starlette route.matches() for matching
   - Return (endpoint_callable, path_params)
5. build_handler_kwargs(endpoint, path_params, query_params, user, body_str)
   - Introspect handler signature with inspect.signature()
   - Unwrap FastAPI FieldInfo defaults (Query, Path, Header, etc.) to extract actual default values
   - Resolve each parameter from path params, query params, Depends, body, or Request
6. await endpoint(**kwargs) (timed)
7. Extract notices if result is a ToolResultWithNotices wrapper (unwrap inner result)
8. _serialize_result(result) -> string
   - Handle Response objects, dicts, lists, strings
9. Log [route_dispatch] success: HTTP method, path, endpoint name, duration (ms), user email, result length
10. Return (result_string, notices) tuple (or (JSON error string, []) on any exception, logged with tool name, URL, and user email)
```

## Key Functions

All functions are in `chat/route_dispatch.py`. See the source file for full signatures and parameter details.

**`validate_url(url)`** -- Parses and validates a tool call URL. Enforces that the host is `localhost`, the port is `8000`, the configured proxy port from `get_proxy_port()`, or the sandbox tool API port from `get_sandbox_port()` (the model sees the sandbox port in the script-sandbox docs and may echo it), and the path starts with `/api/`. Returns the URL path and parsed query parameters.

**`resolve_route(app, method, path)`** -- Matches a method + path against the FastAPI app's registered routes using Starlette's `route.matches()` mechanism. Returns the endpoint callable and path parameters.

**`build_handler_kwargs(endpoint, ...)`** -- Introspects the handler's signature using `inspect.signature()` and resolves each parameter from path params, query params, `Depends` injection, Pydantic body models, or defaults. Unwraps FastAPI `FieldInfo` metadata objects (e.g., `Query()`, `Path()`) to extract actual default values. Supports `conversation_id` auto-injection into Pydantic body models and scalar parameters.

**`execute_tool_call(app, user, tool_name, tool_args, conversation_id=None)`** -- The main entry point for the conversation loop. Orchestrates the full dispatch pipeline and always returns a tuple of `(result_string, notices)`. Never raises exceptions -- all errors are caught and returned as JSON error strings. When a route handler returns a `ToolResultWithNotices` wrapper (from `chat/tool_notices.py`), the notices are extracted and returned separately. See [ToolResultWithNotices Pattern](#toolresultwithnotices-pattern) below.

## Blocked Proxy Paths

The `_BLOCKED_PROXY_PATHS` list in `chat/route_dispatch.py` defines API paths that `curl_proxy_get` and `curl_proxy_post` tool calls are forbidden from reaching. Paths are matched as prefixes -- both exact matches and sub-paths are blocked. When a tool call targets a blocked path, `execute_tool_call()` returns a JSON error directing the LLM to use the appropriate direct tool call instead.

Currently blocked:

| Path | Reason |
|---|---|
| `/api/authed-get` | Must be accessed via the `authed_get` tool call (for LLM use) or direct HTTP from sandbox scripts (for `run_script`/`run_python` use). Blocking prevents the LLM from bypassing the `tool_call` dispatch path. See [Gemini API Integration - Proxy Endpoint](gemini-api.md#proxy-endpoint-post-apiauthed-get) |
| `/api/authed-post` | Same as `/api/authed-get`, for the POST-capable sibling `authed_post` tool |
| `/api/tool-call` | The script tool-call bridge is for sandbox code only (ephemeral `QUEST_API_KEY` sandbox-token auth); the LLM invokes dynamic tools natively via the `tool_call` meta tool. See [Gemini API -- Script Tool-Call Bridge](gemini-api.md#script-tool-call-bridge-post-apitool-call) |
| `/api/slack`, `/api/slack-simple` | Slack is dedicated dynamic tools only (`list_slack_conversations`, `send_slack_dm_to_self`, etc.) with no HTTP routes; the blocked prefixes give cached sessions that still emit `curl_proxy_*` a "use the dedicated tool" error instead of a bare no-route error (`/api/slack-simple` needs its own entry because prefix matching is segment-bounded) |
| `/api/twitter` | Twitter/X reads moved to the twitter plugin's `api.twitter.com` `authed_get` service entry; same cached-session pointer rationale |
| `/api/telegram` | Telegram reads are dedicated dynamic tools only (`telegram_get_me`, `telegram_list_dialogs`, `telegram_get_messages`, `telegram_list_contacts`) with no HTTP routes, registered by the `plugins/telegram` plugin; sandbox scripts use the `/api/tool-call` bridge. See [Telegram API](../../plugins/telegram/docs/telegram-api.md) |
| `/api/imap` | IMAP mailboxes are the dedicated `imap_*` dynamic tools of the `plugins/imap` plugin and never had HTTP routes; the prefix is reserved so a model guessing at `/api/imap/*` gets a "use the dedicated tool" pointer and no future route under it is reachable through `curl_proxy_*` |

## Edge Cases Handled

**Fake Request objects**: Some handlers accept a `request: Request` parameter (e.g., `batch_request`, `get_instructions`). The dispatcher builds a minimal Starlette `Request` with the correct method, path, headers, and body via `_build_fake_request()`. The fake request supports `.body()`, `.headers`, and `.base_url`.

**Pydantic body models**: POST handlers like `create_draft` accept typed Pydantic body parameters (e.g., `CreateDraftRequest` in `api/gmail/models.py`). The dispatcher detects `BaseModel` subclass annotations and parses the body string via `model_validate_json()`.

**conversation_id auto-injection (Pydantic body models)**: After parsing a Pydantic body model, `build_handler_kwargs()` checks whether the model instance has a `conversation_id` attribute that is `None`. If so, and if a `conversation_id` was passed into the function (originating from `execute_tool_call()` via `chat/gemini_api/tool_dispatch.py`), it injects the value directly onto the model instance.

This allows handlers like `create_draft()` in `api/gmail/draft_endpoints.py` to receive the conversation context without the LLM needing to know or provide its own conversation ID. Used by `CreateDraftRequest` for resolving workspace file attachments (see [Gmail API - Attachments](../api/gmail-api.md#attachments)).

**conversation_id auto-injection (scalar GET parameters)**: For any handler parameter named `conversation_id` that is not a Pydantic model, `build_handler_kwargs()` injects the value from the dispatch context and skips query parameter lookup entirely. This prevents the LLM from overriding the value via URL query params (which could cause cross-conversation data access).

When called outside dispatch (direct HTTP), `conversation_id` is `None` and the handler should gracefully degrade.

Used by Gmail Simple API GET handlers (`get_message_simple()`, `get_messages_batch()`, `get_urls_by_identifiers()`, `get_urls_for_message()` in `api/gmail/simple_endpoints.py`) for URL replacement caching and lookup -- though the primary LLM path to these handlers is now the dedicated Gmail Simple tools, whose handlers pass `conversation_id` directly from dispatch context without route dispatch (see [Gmail API](../api/gmail-api.md#gmail-simple-tools)).

**FastAPI FieldInfo defaults (Query, Path, Header, etc.)**: When a handler declares `param: Optional[int] = Query(None)`, `inspect.signature()` returns the `Query()` object (a Pydantic `FieldInfo` instance) as the parameter's default, not the actual value `None`.

Without unwrapping, these `FieldInfo` objects would be passed as literal default values -- producing garbage strings like `"annotation=nonetype required=false..."` when coerced to string. This caused `400: Invalid Value` errors on Google Docs endpoints (e.g., `get_document_raw()`) when users omitted optional query parameters like `includeTabsContent`.

The unwrapping step in `build_handler_kwargs()` uses `isinstance(default, FieldInfo)` to detect these objects and extracts the real default via `FieldInfo.default`, with a special case for `PydanticUndefined` (from `Query(...)`) which indicates a required parameter.

**Optional parameters with defaults**: Query parameters annotated as `Optional[X]` that are not present in the URL receive `None` (or their declared default value). This matches FastAPI's own behavior.

**Unknown Depends**: If a handler has a `Depends()` with an unrecognized dependency function, the dispatcher logs a warning and skips the parameter rather than crashing. The handler will fail naturally if it actually needs the value.

**Bool coercion from strings**: Query parameter values arrive as strings. The `_coerce()` helper handles boolean conversion from strings like `"true"`, `"1"`, `"yes"`.

## Logging

The module uses `logger = logging.getLogger(__name__)` in `chat/route_dispatch.py` (which resolves to `chat.route_dispatch`). The `chat` logger is configured at INFO level by the unified logging configuration in `chat/logging_config.py`. All log entries include `user=<email>` for per-user filtering -- this module established the `user=<email>` convention that is now followed across all backend modules. See [Logging Architecture](logging.md) for the full configuration details and user identification convention.

- **Successful tool calls**: Logged at INFO level with the prefix `[route_dispatch]`, including: HTTP method (GET/POST), URL path, endpoint function name (via `__name__`), call duration in milliseconds, user email, and serialized result length
- **Failed tool calls**: Logged at ERROR level via `logger.exception()` with the prefix `[route_dispatch]`, including: tool name, URL from the tool args, and user email. The full exception traceback is included

## ToolResultWithNotices Pattern

The `ToolResultWithNotices` dataclass in `chat/tool_notices.py` provides a mechanism for route handlers to attach advisory text notices to their responses. When a handler returns a `ToolResultWithNotices` instead of a plain value, the route dispatch layer extracts the notices and passes them back to the tool dispatch layer as a separate list.

The flow is:

1. A route handler detects a condition that warrants an advisory notice and returns `ToolResultWithNotices(result=data, notices=["..."])`
2. `execute_tool_call()` in `chat/route_dispatch.py` detects the wrapper via `isinstance()`, extracts the `notices` list, unwraps the inner `result`, and returns `(serialized_result, notices)`
3. `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` receives the notices and converts each one into a provider-specific text part via `provider.make_text_part(notice)`, appending them to the `extra_parts` list
4. The extra parts are included alongside the function response in the message sent back to the LLM

The `ToolResultWithNotices` module has no dependencies on the rest of the `chat` package, so it can be safely imported by `api.*` modules without circular imports. Currently used by the Slack plugin's endpoint functions (`plugins/slack/upstream.py`) to notify the LLM when a requested limit was capped. Note the Slack reads are now reached only through the dedicated Slack read tools (`list_slack_conversations` etc.), whose handlers call the endpoint functions directly and merge the notices into the tool's JSON result under a `notices` key -- the route-dispatch flow above currently has no producer, but the mechanism stays for future route handlers.

## Design Decisions

**Why internal dispatch instead of HTTP loopback?**
Eliminates network overhead, avoids port conflicts, removes the need for the Docker container's `--network=host`, and prevents data exfiltration through arbitrary network access. The model can only reach registered `/api/*` endpoints.

**Why URL-based tool names?**
The model thinks in URLs (`http://localhost:8000/api/gmail-simple/messages/...`). This is intentional: when the future `run_command` tool ships (Docker with isolated networking), scripts inside the container will use the same URLs via real HTTP, and the model already knows how to construct them.

**Why only `/api/*` paths?**
URL allowlisting prevents the model from accessing auth endpoints (`/auth/*`), app endpoints (`/app/*`), or external URLs. Only the proxy API surface is exposed.

**Why catch all exceptions in `execute_tool_call()`?**
A crashed tool call would break the Gemini conversation loop. By catching everything and returning error strings, the model can see the error and adjust (e.g., fix a malformed URL, try a different endpoint).

**Why unwrap FieldInfo instead of using FastAPI's own dependency injection?**
The route dispatcher bypasses FastAPI's request handling and calls endpoint functions directly. FastAPI's dependency injection system normally resolves `Query()`, `Path()`, and other `FieldInfo` defaults during request processing, but since the dispatcher invokes handlers without a real HTTP request, it must replicate this resolution. Unwrapping `FieldInfo` objects in `build_handler_kwargs()` is the minimal fix that handles the gap without pulling in FastAPI's full dependency injection machinery.

**Why log successful tool calls with duration and result length?**
Route dispatch calls are invisible to the frontend (no HTTP request appears in browser dev tools). Logging each call with method, path, endpoint name, duration, and result length provides observability for debugging slow API calls, identifying frequently used endpoints, and diagnosing failures -- without adding overhead to the user-facing experience.

**Why Starlette `route.matches()` instead of regex?**
Using the framework's own matching mechanism ensures correct handling of all path converter types (`:path`, `:int`, etc.) without reimplementing parsing logic.

**Why auto-inject conversation_id instead of adding it to tool declarations?**
The LLM does not inherently know its own conversation ID, and exposing internal identifiers in tool schemas adds complexity and error surface. By injecting `conversation_id` at the dispatch layer, the tool interface stays clean and the LLM cannot provide an incorrect value. Two injection mechanisms exist:

1. for Pydantic body models (POST handlers), any model with an `Optional[str]` field named `conversation_id` defaulting to `None` receives the value after `model_validate_json()`
2. for scalar handler parameters (GET handlers), any parameter named `conversation_id` receives the value directly, bypassing query param lookup to prevent LLM override

Used by `CreateDraftRequest` in `api/gmail/models.py` (workspace attachment resolution) and Gmail Simple API GET handlers (URL replacement caching and URL lookup) when those handlers are reached through route dispatch; the dedicated Gmail Simple tool handlers pass `conversation_id` directly instead.

## Constraints

- Only `curl_proxy_get` and `curl_proxy_post` tool names are supported (mapped in `_TOOL_METHOD_MAP`). The local tools (the `TOOL_CALL_REGISTRY` dynamic tools dispatched via the `tool_call` meta tool or their backward-compatibility direct-name paths, plus always-direct tools: `load_gmail_attachment`, `list_skills`, `search_skills`, `load_skills`, `run_script`, `run_python`) are handled by `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` before reaching route dispatch. See [Conversation Loop and Tool Integration -- Tool Declarations](gemini-api.md#tool-declarations) for the registry roster.
- `wait_for_handles` is top-level only. Sub-agents do not see it (excluded from the dynamic tools section in `chat/gemini_api/system_prompt.py`), and Slack-driven runs short-circuit the dispatch arm with a structured error before any future is registered. See [Wait Handles Architecture](wait-handles.md) for the full mechanism and [Slack Socket Mode](slack-socket-mode.md) for why Slack uses `send_slack_reply_and_get_response` instead.
- URLs must target `localhost/api/*` on an allowed port (`8000`, the configured proxy port from `get_proxy_port()`, or the sandbox tool API port from `get_sandbox_port()` in `chat/gemini_api/constants.py`) -- all other hosts, ports, and path prefixes are rejected
- Paths listed in `_BLOCKED_PROXY_PATHS` (see [Blocked Proxy Paths](#blocked-proxy-paths)) are rejected with a JSON error before route resolution. These internal-only endpoints are accessible via direct tool call dispatch or direct HTTP from sandbox scripts, but not through the generic `curl_proxy_get`/`curl_proxy_post` tools
- The `Depends` resolution is explicit, not generic -- only `get_current_user` is recognized (the Telegram client dependency went away with the `/api/telegram/*` routes); other dependencies are logged and skipped
- The fake `Request` object is minimal; handlers that use advanced `Request` features beyond `.body()`, `.headers`, and `.base_url` may not work correctly
- `build_handler_kwargs()` is async because reading the request body for Pydantic body models requires `await`
