# System Architecture Overview

Quest is a chat application in which each conversation runs a tool-calling agent loop on behalf of one signed-in user. A single FastAPI process (`quest.py`) serves the React single-page app, the REST API under `/app/api`, the persistent WebSocket `WS /app/api/stream` and the `/auth` routes on one port. Models come from three provider families: Gemini and Claude on Vertex AI, and admin-configured OpenAI-compatible provider instances (see [LLM Provider Abstraction](llm-providers.md) and [Inference Providers](inference-providers.md)).

This doc is the map: where each piece lives, how a turn moves through them, and the decisions that cut across subsystems. Every subsystem has its own doc, listed in `docs/index.md`; detail belongs there.

## System Shape

```
 Browser: React SPA (frontend/src)
      │  REST /app/api/*  ·  WS /app/api/stream  ·  /auth/*
      ▼
 FastAPI app, one process, one port (quest.py)
      │  lifespan daemons: routine scheduler, Slack Socket Mode,
      │  Slack request notifier, replay-buffer eviction, model health checks
      ▼
 Conversation loop (chat/gemini_api) ──► LLM providers (chat/llm):
      │                                   Vertex Gemini · Vertex Claude ·
      │                                   OpenAI-compatible instances
      ├──► tools: workspace files, memories, skills, Quest Docs,
      │    connected services (authed_get, plugins), sub-agents
      ├──► action request ──► approval card ──► wait handle ──► resume
      └──► Podman script runner ──► sandbox tool API (loopback port)
      ▼
 Storage (data dir): SQLite · per-conversation JSON + workspace ·
                     per-project workspace + project DB · Quest Docs
```

## Code Map

| Path | Role | Owning doc |
|------|------|------------|
| `quest.py` | App assembly: router registration, lifespan daemons, SPA and static serving | [Production](../setup/production.md) |
| `run.py` | Stdlib-only launcher: mode selection, frontend build, migrations, image checks, uvicorn | [Run Modes](run-modes.md), [Development](../setup/development.md) |
| `config/` | Stdlib-only configuration, one module per concern (run mode, data paths, server config, encryption, feature gates, provider instances, credential specs, plugin loader) | the concern's doc, e.g. [Run Modes](run-modes.md), [Data Paths](data-paths.md), [Encryption at Rest](encryption-at-rest.md), [Feature Gates](feature-gates.md) |
| `auth/` | Sign-in methods, session cookies, OAuth flows, per-user keys and connections | [Auth](auth.md), [User Connections](user-connections.md) |
| `chat/realtime/` | Persistent WebSocket, event bus, replay buffer | [Realtime](realtime.md) |
| `chat/gemini_api/` | Conversation loop, tool dispatch, sub-agents, system prompt, chat sessions | [Conversation Loop](gemini-api.md) |
| `chat/llm/` | Provider abstraction, model registry, tool schemas and tiers | [LLM Provider Abstraction](llm-providers.md) |
| `chat/routes/`, `chat/*_routes.py` | REST endpoints | [Chat API](../api/chat-api.md) and the per-feature docs in `docs/api/` |
| `chat/storage.py`, `chat/conversation_access.py` | `chat_history.json` persistence, the id-to-path resolvers, ownership guards | [Data Paths](data-paths.md) |
| `chat/file_routes.py`, `chat/file_storage.py` | Workspace file browser and copy routes | [File Browser API](../api/file-browser-api.md) |
| `chat/action_request_types/` | Approval-gated write handlers | [Action Requests](action-requests.md) |
| `chat/wait_handles/` | Blocking-tool resume machinery | [Wait Handles](wait-handles.md) |
| `chat/system_skills/` | `system:*` skill catalog and prompt enumeration | [Skill Library](skill-library.md) |
| `chat/docs/` | Quest Docs | [Quest Docs](quest-docs.md) |
| `chat/tables/` | Quest Tables | [Quest Tables](quest-tables.md) |
| `chat/artifact_access.py` | The access procedure shared by Quest Docs and Quest Tables | [Quest Docs -- Access Rule](quest-docs.md#access-rule) |
| `chat/scheduler.py`, `chat/routine_runs.py` | Scheduled and one-click routine runs | [Scheduling](scheduling.md), [Routines](routines.md) |
| `chat/slack_socket_mode.py`, `chat/slack_driven_runtime.py`, `chat/slack_notifier.py` | Slack-driven conversations, pending-request DMs | [Slack Socket Mode](slack-socket-mode.md), [Slack Pending-Request Notifier](slack-pending-request-notifier.md) |
| `chat/sandbox_api.py`, `chat/sandbox_tokens.py` | Loopback tool API for sandbox scripts and its per-run tokens | [Script Runner](script-runner.md) |
| `chat/route_dispatch.py` | In-process dispatch of `curl_proxy_*` tool calls to `/api/*` handlers | [Route Dispatch](route-dispatch.md) |
| `chat/user_subagent.py`, `chat/inference_api.py` | Headless run kinds: cross-user subagents, the inference endpoint | [User Subagents](user-subagents.md), [Inference API](../api/inference-api.md) |
| `api/` | Instruction text for the core connectors (Google services, Airtable, Ramp and others), the Gmail endpoint functions, and `get_user_connected_services()` | the service docs in `docs/api/`, e.g. [Gmail API](../api/gmail-api.md) |
| `plugins/` | Filesystem-discovered integrations | [Plugins](plugins.md) and each plugin's own `docs/` |
| `db/`, `alembic/` | SQLAlchemy models, async stores, migrations | [Database](database.md) |
| `frontend/src/` | SPA: contexts, conversation store, WebSocket services, components | [Frontend](frontend.md) |
| `Dockerfile.script-runner*`, `script-runner-*` | Sandbox images and entrypoints | [Script Runner](script-runner.md), [Public Projects](public-projects.md) |
| `chat/browser_sessions.py`, `chat/gemini_api/tool_handlers/browser.py`, `Dockerfile.browser-sidecar`, `browser-sidecar-agent.py` | Headless browser containers for public conversations and their tools | [Browser for Public Projects](public-browser.md) |
| `scripts/` | Production bootstrap wizard, local seeding, release tagging | [Production](../setup/production.md), [Run Modes](run-modes.md), [Development Workflows](../setup/development-workflows.md) |
| `logging_config.json`, `chat/logging_config.py` | Unified logging and the `user=<email>` convention | [Logging](logging.md) |

## Request Lifecycles

### A chat turn

1. The composer sends `{op: "send_message"}` on the persistent socket (`WebSocketManager.sendMessage()` -> `frontend/src/services/PersistentWebSocket.ts`). From the home composer, the opening message first creates the conversation over REST (`frontend/src/components/HomeComposer.tsx`).
2. `_handle_send_message()` in `chat/realtime/socket.py` rejects the send while a run is active for the conversation, appends the user message, replies `send_message_accepted`, re-reads the user from the database and registers the run in `_active_send_runs`.
3. `_run_send_message()` runs `run_conversation_turn()` (`chat/gemini_api/conversation.py`), which resolves the model (`resolve_model()` in `chat/llm/config.py`), builds the system prompt (`chat/gemini_api/system_prompt.py`) and gets the in-memory chat session (`get_or_create_chat()` in `chat/gemini_api/session.py`).
4. The provider streams. Tool calls go through `_dispatch_tool_call()` (`chat/gemini_api/tool_dispatch.py`) or the loop-level handlers in `TURN_TOOL_HANDLERS` (`chat/gemini_api/turn_tools.py`).
5. Events whose type is in `FLUSH_EVENT_TYPES` are appended to `chat_history.json` with a `seq` by `make_flush_callback()` (`chat/_flush_helper.py`) and announced as `message_appended`; text deltas and sub-agent updates go out as transient events. The client fetches the tail over REST, and `send_message_finished` ends the turn.

The client store (`frontend/src/store/conversationStore.ts`) keys state per conversation, so events keep landing in the right conversation while the user switches views. A run outlives the socket that started it; only `stop` cancels it ([Realtime -- Send Lock and Stop Semantics](realtime.md#send-lock-and-stop-semantics)).

### A write that needs approval

1. The model calls `create_action_request`; the handler validates the parameters, writes an `action_requests` row plus a linked wait handle, and the loop suspends via `SuspendForActionRequest`, leaving an approval card in the transcript.
2. The user's Approve / Revise / Stop reaches `POST /app/api/action-requests/{request_id}/resolve` (`chat/action_request_routes.py`), which executes or records the decision and resolves the wait handle.
3. `maybe_kick_resume()` (`chat/wait_handles/resume.py`) runs a headless continuation that answers the dangling tool call; viewing tabs follow it over the socket.

See [Action Requests](action-requests.md) and [Wait Handles](wait-handles.md).

### Sign-in and request authentication

Exactly one sign-in method is active, Google OAuth or email/password (`login_method()` in `auth/config.py`); local mode adds a canned-account dev login. Every method ends in the signed session cookie set by `set_session_cookie()` (`auth/session.py`), whose name depends on the run mode (`cookie_name()` in `config/environment.py`). `/app/api/*` accepts that cookie or the user's API key as a bearer token, and every authenticating dependency re-checks the login policy through `require_user_allowed()` (`chat/auth.py`). See [Auth](auth.md).

## Storage and Isolation

- **SQLite** (`db/engine.py`, `db/models.py`, Alembic migrations run by `run.py`) holds all relational state: users and their credentials, conversation metadata, projects, routines, action requests and the other models in `db/models.py`. See [Database](database.md).
- **Per-conversation files** under `chats/<conversation_id>/`: the `chat_history.json` transcript, the SDK history and the conversation workspace. Ownership lives in the `conversations` table, not in the directory layout.
- **Workspaces**: every conversation has its own conversation workspace; each project adds a shared project workspace that tools and the UI reach only on purpose (`proj://` paths, the `/project` sandbox mount, the Project Files card, the copy routes). See [Projects -- Workspace Resolution](projects.md#workspace-resolution).
- **Paths** come only from the `ChatStorage` resolvers in `chat/storage.py`, under a data directory that `config/paths.py` lets a deployment relocate. See [Data Paths](data-paths.md).

## Security Model

- **Auth boundary**: session cookie or API key on the main port; ephemeral per-run tokens on the loopback sandbox port ([Script Runner -- Sandbox tokens](script-runner.md#sandbox-tokens)); named bearer tokens for the inference endpoint ([Inference API](../api/inference-api.md)). Cookie payloads are strictly type-checked (`get_user_from_cookie()` in `auth/session.py`).
- **Login policy**: the allowed domain or the `allowed_login_emails` list (`is_login_allowed()` in `auth/config.py`), enforced on every request.
- **Secrets at rest**: per-user secret columns and the credential store files are encrypted under a password-wrapped data key; the uncovered files are listed in [Encryption at Rest](encryption-at-rest.md).
- **Paths**: canonical-id and containment checks inside the resolvers ([Data Paths](data-paths.md)).
- **Code execution**: only in rootless Podman containers with a no-symlink seccomp profile; the restricted image has no egress and reaches only the sandbox tool API ([Script Runner](script-runner.md)).
- **Public projects**: an internet-enabled sandbox, cut off from skills, memories, connectors and every other internal resource ([Public Projects](public-projects.md)); optionally a headless browser container per conversation under the same network rules ([Browser for Public Projects](public-browser.md)).
- **Quest Docs and Tables**: one access procedure, `resolve_core()` in `chat/artifact_access.py`, behind `resolve_doc_access()` and `resolve_table_access()` ([Quest Docs](quest-docs.md), [Quest Tables](quest-tables.md#access)).

## Run Modes and Serving

`run.py` starts Quest in local, staging or prod mode (`config/environment.py`); its module docstring lists the launch steps. In every mode FastAPI serves the built SPA from `frontend/dist/` on the API's port. See [Run Modes](run-modes.md), [Development](../setup/development.md) and [Production -- Static File Serving](../setup/production.md#static-file-serving).

## Cross-Cutting Design Decisions

**One process, one port.** The API, SPA, WebSocket bus, scheduler and Slack worker share one uvicorn process, and the in-memory bus, the per-conversation send lock, the `chat_history.json` writes and the Slack Socket Mode worker all rely on that ([Realtime -- Constraints](realtime.md#constraints)). A separate frontend server and a multi-process deployment behind a shared bus were rejected: a deployment serves one organization's users, which one process carries, and the split would add a second deploy unit and a Redis-style dependency.

**The agent loop runs in the server process.** The loop calls Quest's own tool handlers directly, so the model gets no shell, no general network client and none of the user's service tokens: `authed_get` injects OAuth tokens and API keys server-side against per-service allow-lists, and code runs only in the sandbox ([Conversation Loop](gemini-api.md)). Running an agent runtime per conversation in a container that holds the user's credentials was rejected because it puts those tokens and an open network path within the model's reach.

**The prompt follows the user's connections.** The system prompt enumerates only the `system:*` skills whose service the user has connected (`get_user_connected_services()` in `api/instructions.py`, `build_system_skills_enumeration()` in `chat/system_skills/loader.py`); API documentation loads on demand through `load_skills`. Inlining every service's documentation in each prompt was rejected: it costs context and invites calls to services the user never authorized. Connect and disconnect paths invalidate cached sessions so the next turn sees the change ([Conversation Loop -- System Prompt](gemini-api.md#system-prompt)).

**External writes are proposals.** A write that reaches another person or shared external state goes through `create_action_request` and blocks the run until the user decides ([Action Requests](action-requests.md)). The approval-free writes are the tools flagged mutating (`"mutating": True` on `TOOL_CALL_REGISTRY` specs in `chat/llm/tool_schemas.py`, `PluginTool.mutating` in `config/plugin_types.py`), which inference API runs refuse. Letting the model write directly was rejected because a mistaken send cannot be taken back.

**Conversation workspace by default, project workspace on purpose.** Everything a conversation produces lands in its own workspace; the shared project workspace is written only through explicit `proj://` paths, the `/project` mount or the UI. One shared project workspace for all of a project's conversations was rejected because deliverables got buried under every run's scratch files ([Projects](projects.md)).

**SQLite plus per-conversation JSON.** Relational state lives in SQLite; transcripts are append-heavy per-conversation files whose write also stamps the `seq` the realtime layer replays from. A database server was rejected because SQLite needs no separate service and fits the single-process deployment ([Database -- Design Decisions](database.md#design-decisions)); transcript rows in the database were rejected because the file write is what keeps body and `seq` atomic ([Realtime -- Design Decisions](realtime.md#design-decisions)).

**Integrations are plugins.** A connector is a directory under `plugins/` whose manifest fans out into skills, tools, action requests, `authed_get` entries, the admin credential card and the user's connection row, so adding one needs no core edit ([Plugins](plugins.md)); a few core connectors (Google services, Airtable, Ramp) sit outside the loader. Editing core routes, prompt text and settings UI per integration was rejected because a fork could not add a proprietary connector without patching core.
