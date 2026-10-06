# Quest Documentation

Welcome to the Quest documentation. This chat application provides a ChatGPT-style web interface for multiple LLM providers (Google Gemini and Anthropic Claude on Vertex AI), integrated into the Quest API proxy platform.

## Overview

Quest (branded as "DevQuest" in the UI when `QUEST_ENV=dev`) is a full-stack web application that combines:

- **Backend**: FastAPI-based REST and WebSocket API
- **Frontend**: React + TypeScript single-page application (built with Vite)
- **AI Integration**: Multi-provider LLM abstraction layer (`chat/llm/`) supporting Google Gemini (via `google-genai` SDK) and Anthropic Claude (via `anthropic[vertex]` SDK on Vertex AI)
  - models available: Gemini 3.1 Flash-Lite, Gemini 3 Flash, Gemini 3.5 Flash, Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7, and Claude Opus 4.8 (Claude Opus 4.8 has a 1M-token input window; the other Claude models are 200K. Gemini 3 Pro is deprecated and silently remapped to Gemini 3.1 Pro, which is itself deprecated -- Vertex-backed, hidden from the model selector, but still runnable by existing conversations and routines).
  - Authenticated external API access via `authed_get` tool for server-side credential injection (supports Gmail Raw API, CoinGecko Pro API, Google Calendar API, Google Drive API, and Google Docs API with per-user OAuth and 401 retry) with large response protection (size gate, file-based blob storage, chunked reading via `get_response_content`), a `POST /api/authed-get` proxy endpoint for sandbox script access, and `download_drive_file` tool for Drive binary content downloads
- **Authentication**: Two-tier Google OAuth (app login + separate Google Services authorization with `tasks.readonly` and `drive.file` scopes), per-service OAuth for plugin connectors (e.g. Slack, GitHub, Twitter/X), inline sign-in on `/`, dev-only email login (`POST /auth/dev-login` when `QUEST_ENV=dev`) for testing multi-user features without Google accounts
- **Storage**: SQLite database for user data, memories, guides, projects, routines, routine schedules, conversation metadata including routine association, archive status, custom names, and model selection, action requests, skills with sharing and auto-load at both user and project levels (via SQLAlchemy + Alembic, with FTS5 for memory search), JSON files for chat message histories and per-conversation loaded skills

The application provides isolated chat workspaces where each conversation maintains its own Gemini state, allowing users to have multiple independent chat sessions. Conversations can be organized into Projects that share a workspace, optional project guide, and optional project-level skills (auto-loaded into all project conversations).

Projects can have Routines -- canned prompts that run in one click to create a new conversation with an auto-sent prompt, optional guide override, and optional model specification. Routines can have automatic Schedules attached, allowing them to run on a timer (daily, hourly, or at a fixed interval) without user interaction.

Conversations created by routines are linked back via `routine_id` and grouped under collapsible routine entries in the sidebar for organized navigation. Conversations can be renamed via an inline rename UI in the sidebar; custom names are stored in a `custom_name` column on the `Conversation` model and take priority over auto-generated titles.

## Quick Links

### Architecture Documentation
- [System Architecture Overview](architecture/overview.md) - High-level architecture and component interaction
- [Auth Submodule](architecture/auth.md) - OAuth flows, session management, and credential handling (`auth/` package)
- [Frontend Architecture](architecture/frontend.md) - React frontend implementation and build tooling
- [OAuth Popup Flow](architecture/oauth-popup.md) - OAuth popup window architecture, postMessage protocol, and popup flag propagation
- [Settings Data Connections](architecture/settings-data-connections.md) - Settings UI OAuth connector pattern (connected = badge + Reconnect; not connected = badge + Connect; no Disconnect button)
- [Service Credentials](architecture/service-credentials.md) - Per-service upstream credential store in the data directory (0600 files, encrypted at rest), admin-only Settings editor, startup migration from `server_credentials.json`
- [Encryption at Rest](architecture/encryption-at-rest.md) - Password-wrapped data key (`encryption_key.json`), AES-256-GCM envelopes for every secret column and credential file, startup password resolution (env / file / local sentinel / prompt), the single in-place migration with pre-encryption backup
- [Inference Providers](architecture/inference-providers.md) - Admin-only Settings panel for LLM backends: editable API-key providers (Gemini API) in a per-provider store, read-only detected Vertex AI environment
- [Model Selection](architecture/model-selection.md) - Admin-only Settings table layering per-model presentation and usage rules over the provider config: composer-menu top-level slot + descriptor, private/public conversation allow flags enforced per turn
- [Route Dispatch](architecture/route-dispatch.md) - Internal tool call dispatch, including `conversation_id` auto-injection into Pydantic body models and blocked proxy paths for internal-only endpoints
- [LLM Provider Abstraction](architecture/llm-providers.md) - Multi-provider abstraction layer (`chat/llm/`), model registry, provider-agnostic tool definitions, Gemini and Anthropic provider implementations, Vertex AI configuration
- [Gemini API Integration](architecture/gemini-api.md) - Conversation loop, streaming, chat route wiring, memory read tools, skill discovery and loading tools (`list_skills`, `search_skills`, `load_skills`), Slack channel search
  - authenticated external API requests (`authed_get` with Gmail Raw API, CoinGecko Pro, Google Calendar, Google Drive, and Google Docs support, per-user OAuth, 401 retry, large response protection with `get_response_content` chunked reading, and `POST /api/authed-get` proxy endpoint for sandbox scripts), `download_drive_file` for Drive binary content downloads, `archive_gmail_message` for Gmail message archiving with `[Quest]/archived` label, shared tool dispatch
  - generic durable wait-handle mechanism (`wait_for_handles` tool plus the `tool_wait_handles` table; `send_slack_reply_and_get_response` and `create_action_request` -- including the `create_memory` request type -- block via the same row -- see [Wait Handles Architecture](architecture/wait-handles.md)), large data processing strategy (Scout/Batch/Aggregate pattern), and inline Python execution (`run_python`)
- [Conversation Flags](architecture/conversation-flags.md) - Per-conversation opt-in behaviors set at the start of a conversation via a magic `%%flags[...]` first line (web-only); verbatim-persist vs stripped-to-model vs clean-title; flags registry and threading into the run; first flag `nested_subagents`
- [Database Architecture](architecture/database.md) - SQLite database, SQLAlchemy ORM, Alembic migrations, encrypted secret columns
- [Guides Architecture](architecture/guides.md) - Named system prompt presets (guides; deprecated in favor of skills -- no composer selector or default fallback, routine overrides and existing snapshots only), per-conversation snapshot mechanism, guide management in Settings with convert-to-skill
- [Projects Architecture](architecture/projects.md) - Projects that group conversations with a shared workspace, optional project guide, and optional project-level skills with auto-load, project-aware workspace resolution, project guide injection into system prompts
- [Public Projects Architecture](architecture/public-projects.md) - Creation-time public project mode: internet-enabled script sandbox, conversations cut off from all internal resources (skills, memories, connectors, action requests), dispatch-time tool allowlist, separate public podman image
- [Quest Docs Architecture](architecture/quest-docs.md) - User- and project-owned markdown documents stored as `doc.md` + `assets/` + `revisions/` under `DOCS_DIR`, private/public modes mirroring public projects, the single access rule (`resolve_doc_access`), the seven doc tools, the `write_doc` action request for shared private docs, and the `docs` feature gate
- [Run Modes](architecture/run-modes.md) - The three run modes (local/staging/prod): central environment module, local-mode throwaway seeded database, canned-account login, and graceful degradation without credentials
- [Routines Architecture](architecture/routines.md) - Canned prompts attached to projects that run in one click, combining a prompt, optional guide override, and optional model specification
- [Scheduling Architecture](architecture/scheduling.md) - Automatic routine scheduling with daily, hourly, and interval-based schedule types, background asyncio scheduler daemon, timezone-aware DST handling
- [Action Requests Architecture](architecture/action-requests.md) - Agent-proposed write operations to external services (e.g., Slack messages, Telegram messages, calendar invites, plus plugin-registered types) with user approval, handler registry pattern with server-side preview rendering, non-blocking inline chat UI
- [Slack Pending-Request Notifier](architecture/slack-pending-request-notifier.md) - Background daemon that DMs users via the Slack bot about action requests left unanswered, at most once per tunable per-user interval (default 60 minutes, disableable in Settings > Slack)
- [Plugin Architecture](architecture/plugins.md) - Filesystem-discovered integration plugins (`plugins/*/plugin.py`): manifest dataclasses, loader validation and registry fan-out, schema-driven config surfaces, combined server+user gating, and external plugin roots (loaded via `QUEST_PLUGIN_PATH`)
- [Voice Input](architecture/voice-input.md) - Dictated prompts: composer mic button, browser `MediaRecorder` capture, server-side transcription on the deployment's own Gemini Vertex model behind the admin `voice_input` feature gate (cannot be enabled without a configured Gemini Vertex model)
- [Wait Handles Architecture](architecture/wait-handles.md) - Durable DB-backed wait handles plus the generic `wait_for_handles` tool that lets the model block on tools awaiting human input, with restart-resilient suspend / resume and headless continuation on resolve
- [Skill Library Architecture](architecture/skill-library.md) - Reusable DB-backed skill definitions with four-level visibility (private, shared, public, project), sharing via junction table, SET NULL on creator deletion, user auto-load via `user_skill_autoloads` junction table, project auto-load via `project_skill_autoloads` junction table
  - auto-loaded skill injection into system prompts (user + project auto-loads merged with deduplication), per-conversation skill loading via Skill Selector Modal with `<conversation_skills_loaded>` XML injection
  - tabbed settings UI ("My Skills" / "Shared with Me"), project skills UI in ProjectSettingsModal ("Project Skills" / "My Skills" / "Shared with Me"), LLM skill discovery and loading tools (`list_skills`, `search_skills`, `load_skills`).
  - Also covers hardcoded **system skills** (`system:*` ids) in `chat/system_skills/catalog.py` that carry per-backend API docs and are loadable on demand via the same tools; enumerated in the system prompt and filtered by `connected_services` / project status
- [Script Runner Architecture](architecture/script-runner.md) - Ephemeral Podman containers for executing workspace scripts (`run_script`) and inline Python code (`run_python`) with sandboxed networking and iptables-based host port isolation
- [Logging Architecture](architecture/logging.md) - Unified logging configuration across all modules, including the `large_tool_results` file-based channel for oversized tool call results

### Setup Guides
- [Development Setup](setup/development.md) - How to set up and run the development environment
- [Development Workflows](setup/development-workflows.md) - Syntax checking, pytest test suite, code validation, and development tooling
- [Production Deployment](setup/production.md) - Production deployment guide
- [Slack App Setup](../plugins/slack/docs/slack-app-setup.md) - Creating and configuring Slack integration (served by the plugins/slack plugin)
- [GitHub App Setup](../plugins/github/docs/github-app-setup.md) - Creating and configuring GitHub OAuth integration
- [Twitter/X App Setup](../plugins/twitter/docs/twitter-x-setup.md) - Creating and configuring Twitter/X OAuth 2.0 integration
- [Ramp App Setup](setup/ramp-setup.md) - Creating and configuring Ramp OAuth integration
- [Telegram Setup](../plugins/telegram/docs/telegram-setup.md) - Configuring the Telegram integration (served by the plugins/telegram plugin)
- [Tailscale Setup](../plugins/tailscale/docs/tailscale-setup.md) - Connecting a tailnet with a read-scoped OAuth client or an API access token (served by the plugins/tailscale plugin)

### API Reference
- [Chat API](api/chat-api.md) - REST endpoints for conversation CRUD plus the persistent multiplexed WebSocket (`WS /app/api/stream`) carrying chat traffic and live updates
- [Realtime Architecture](architecture/realtime.md) - Persistent multiplexed WebSocket protocol, seq-based catchup/resync, replay buffer, and per-user globals
- [Projects API](api/projects-api.md) - Project CRUD and project-conversation endpoints
- [Routines API](api/routines-api.md) - Routine CRUD endpoints for project routines (canned prompts)
- [Schedules API](api/schedules-api.md) - Schedule CRUD endpoints for routine scheduling (automatic execution)
- [Skills API](api/skills-api.md) - Skill Library CRUD, sharing, auto-load, search, and user search endpoints (create, list, get, update, delete skills; search skills by keyword; get/toggle auto-load; list shared-with-me; add, list, remove shares; type-ahead user search); project skill CRUD and project auto-load endpoints
- [File Browser API](api/file-browser-api.md) - File management endpoints for conversation workspaces (list, upload, download, download folder as zip, read content, file info, delete, save to Drive)
- [Inference API](api/inference-api.md) - One-shot non-streaming inference endpoint (`POST /api/inference`) for internal applications, authenticated by named per-user bearer tokens from Settings > Inference API; runs a single prompt headlessly as the user and returns the final markdown captured via the `return_final_response` tool
- [Gmail API](api/gmail-api.md) - Gmail Simple dynamic tools (`get_gmail_messages`, `list_gmail_labels`, `get_gmail_message_urls`, `create_gmail_draft`, `send_gmail_to_self`; HTTP `/api/gmail-simple/*` endpoints kept for sandboxed scripts) and Gmail Raw API access via `authed_get` (read messages with attachment metadata, automatic email cleanup and URL replacement for token reduction, URL lookup, fetch raw attachments, create drafts with drive/workspace/gmail attachment and markdown body support, forward draft workflow, send emails to self, archive messages with `[Quest]/archived` label via `archive_gmail_message` dynamic tool)
- [Drive API](api/drive-api.md) - Google Drive access via `authed_get` (metadata reads), `download_drive_file` tool (binary content downloads), Save to Drive from the file browser (markdown to Google Docs conversion), and the `upload_to_drive` / `create_drive_folder` action requests (raw-bytes workspace-file uploads and folder creation, with on-the-fly destination-folder creation on upload)
- [Docs API](api/docs-api.md) - Google Docs access via `authed_get` (document reads) and Drive API (document listing), plus `google_export_doc` (export a Doc to the workspace as pdf/docx/odt/rtf/txt/md/html/epub/zip)
- [Quest Docs API](api/quest-docs-api.md) - Quest Docs UI endpoints under `/app/api/docs` (list with keyset paging, create, read, rename with optimistic concurrency, mode switch, embedded images, md/zip download, delete) and the `doc_list_changed` / `doc_changed` realtime events; not the Google Docs integration
- [Sheets API](api/sheets-api.md) - Google Sheets access via `authed_get` (spreadsheet reads) and Drive API (spreadsheet listing)
- [Calendar API](api/calendar-api.md) - Google Calendar access via `authed_get` (reads) and the `create_calendar_invite` / `edit_calendar_event` action requests (writes), includes cross-org calendar visibility
- [Tasks API](api/tasks-api.md) - Google Tasks access via `authed_get` (read-only task lists and tasks)
- [Telegram API](../plugins/telegram/docs/telegram-api.md) - Telegram dialogs, messages, contacts as dedicated `telegram_*` dynamic tools (no HTTP routes), the Telethon login flow, and the send message action request (plugins/telegram plugin)
- [Airtable API](api/airtable-api.md) - Airtable read-only API endpoints for bases, tables, records, and comments
- [GitHub API](../plugins/github/docs/github-api.md) - GitHub read-only API endpoints for repos, issues, PRs, commits, file contents, and search
- [Federal Register API](api/federal-register-api.md) - Federal Register read-only access via `authed_get` (documents, agencies, public-inspection docs); free public US government API with no authentication, surfaced via the ungated `system:federal_register` skill
- [SEC EDGAR API](api/sec-edgar-api.md) - SEC EDGAR read-only access via `authed_get` (company submissions and XBRL financial facts on `data.sec.gov`, plus the two `www.sec.gov` ticker→CIK map files); free public US government API with no authentication and a required SEC `User-Agent` header, surfaced via the ungated `system:sec_edgar` skill
- [Twitter/X API](../plugins/twitter/docs/twitter-api.md) - Twitter/X DM, bookmarks, and tweet lookup endpoints, plus action-request-based DM sending
- [Tailscale API](../plugins/tailscale/docs/tailscale-api.md) - Tailscale tailnet configuration read-only via `authed_get` (devices, routes, policy file, DNS, users, keys, settings, webhooks, flow logs) over a per-user API access token or OAuth client secret, surfaced via the gated `system:tailscale` skill
- [Ramp API](api/ramp-api.md) - Ramp spend-management read-only access via `authed_get` (transactions, cards, bills, reimbursements, vendors, statements, accounting, audit logs) over per-user OAuth, surfaced via the gated `system:ramp` skill
- [Frontend API Client Usage](api/api-client-usage.md) - How to use the TypeScript API client

## Features

### Backend
- Chat message storage with JSON-based persistence (`data/chats/{conversation_id}/chat_history.json`); conversation ownership, last-message timestamps, archive status, custom names, and model selection stored in SQLite (`conversations` table via `db/conversation_store.py`)
- User data stored in SQLite via SQLAlchemy ORM (`db/` package), with Alembic migrations (`alembic/`)
- User memories: markdown text blobs (max 4KB) with full-text search via SQLite FTS5, inline editing, and soft-delete via archiving
  - `Memory` model in `db/models.py` with `updated_at` (DateTime, nullable) and `archived` (Boolean, default false) fields; data access layer in `db/memory_store.py`
  - CRUD + update + archive/unarchive API endpoints at `/app/api/memories` (see `chat/memory_routes.py`)
  - FTS5 virtual table (`memories_fts`) with sync triggers for search (Alembic migration `94c49d92fea7`); `updated_at`, `archived`, and composite index added in migration `861a7abe9a23`
  - `list_memories()` and `search_memories()` exclude archived memories by default
  - Cascade deletion of memories when user account is deleted
- Skill Library: reusable skill definitions (instructions/prompts) with four-level visibility (private, shared, public, project), sharing via junction table, user auto-load via `user_skill_autoloads` junction table, project auto-load via `project_skill_autoloads` junction table, system prompt injection for auto-loaded skills (user + project auto-loads merged with deduplication), and per-conversation skill loading via the Skill Selector Modal
  - `Skill`, `SkillShare`, `UserSkillAutoload`, and `ProjectSkillAutoload` models in `db/models.py`; `SkillVisibility` enum; data access layer in `db/skill_store.py` (includes `get_user_autoloaded_skills()` and `get_project_autoloaded_skills()` for auto-loaded skill resolution, `list_shared_with_me_skills()` for shared skills listing, `search_accessible_skills()` for keyword search, `get_accessible_skills_by_ids()` for batch fetch, project skill CRUD functions)
  - 13 REST API endpoints for user-level CRUD + auto-load + share management + search + user search (see `chat/skill_routes.py`): 12 at `/app/api/skills` plus `GET /app/api/users/search` for type-ahead autocomplete; plus `GET /app/api/conversations/{id}/loaded-skills` for per-conversation loaded skills retrieval (see `chat/routes/conversations.py`)
  - 7 REST API endpoints for project skill CRUD + project auto-load (see `chat/project_skill_routes.py`): at `/app/api/projects/{project_id}/skills`
  - User auto-loaded skill IDs stored in `user_skill_autoloads` junction table; project auto-loaded skill IDs stored in `project_skill_autoloads` junction table; both resolved on each message and merged with deduplication, injected into system prompt as "Enabled Skills" section
  - Project skills have `visibility='project'` and a non-NULL `project_id` column; explicitly deleted before project deletion (non-cascading FK)
  - Conversation-loaded skills: users can load skills into a specific conversation via the "+ Skill" button in ChatPanel; selected skills are sent as `skill_ids` in the WebSocket payload, resolved via `get_accessible_skills_by_ids()`, and injected as a `<conversation_skills_loaded>` XML section in the user message envelope; persisted to `loaded_skills.json` per conversation via `ChatStorage` in `chat/storage.py`
  - Users can auto-load any accessible skill (own, shared, or public), not just their own
  - Skills are NOT snapshotted (unlike guides) -- each message uses current auto-loaded skills and current content; inaccessible skills silently excluded
  - Sub-agents receive the same pre-resolved skills content as the parent
  - `creator_id` uses `ON DELETE SET NULL` so skills persist when the creator is deleted
  - Alembic migrations: `92ea0f869803` creates `skills` and `skill_shares` tables; `503d2eb4046f` creates `user_skill_autoloads` table; `cc3c9d86f70f` adds `skills.project_id` column and creates `project_skill_autoloads` table
- Workspace management for isolated Gemini environments
- Slack integration with two-tier token architecture, packaged as the in-tree `plugins/slack` plugin (tools, action requests, OAuth flow, `system:slack` skill, admin credential card -- the Slack-driven Socket Mode runtime stays core)
  - Shared bot token: Installed once by an org admin, stored in the `slack` service credential store entry (`bot_token` alongside `client_id`/`client_secret`). Used for write operations (`chat.postMessage`) and org-wide queries (`auth.teams.list`)
  - Per-user OAuth: Grants user tokens only (`access_token`, `default_team_id`, `user_id`, `authorized_at`) for read operations (search, channel history, user listings). Any user can connect
  - Read access: Dedicated dynamic tools dispatched via `tool_call` (`search_slack_messages`, `list_slack_conversations`, `get_slack_conversation_history`, `get_slack_conversation_replies`, `get_slack_user_info`, `list_slack_users`, `list_slack_teams`) wrapping the endpoint functions in `plugins/slack/upstream.py` directly -- no per-method HTTP routes (sandbox scripts invoke the same tools via the script tool-call bridge `POST /api/tool-call`; `/api/slack` is a blocked proxy prefix so cached sessions get a use-the-dedicated-tool error) (user token). `team_id` parameter on `list_slack_conversations`, `search_slack_messages`, and `list_slack_users` for cross-workspace queries
  - Channel search: `find_slack_channel` dynamic tool (dispatched via `tool_call` meta tool) in `plugins/slack/tools.py` searches channels by name substring (case-insensitive) across all workspaces. Uses `auth.teams.list` (bot token) to resolve E-prefix Enterprise IDs to T-prefix workspace IDs, then `conversations.list` (user token) to fetch channels. No new scopes needed (uses existing `channels:read` and `groups:read`)
  - Write access (self-messaging): Send DMs to authenticated user via the `send_slack_dm_to_self` dynamic tool (shared bot token, messages from Quest bot, no approval needed, optional workspace-file attachments; also the sole connector tool in public-project conversations). This is the preferred path for self-messaging (e.g., "send me a message", "DM me", "remind me") and works in automated/scheduled routines where there is no human to approve action requests. No HTTP route (the old `POST /api/slack-simple/dm-self` is retired); sandbox scripts send text-only self-DMs via the `/api/tool-call` bridge
  - Write access (others/channels): Send messages to other people or Slack channels via `send_slack_message` action requests (user token, requires `chat:write` and `im:write` scopes, Block Kit formatted with dynamic Quest attribution including sender's first name, channel names resolved at creation time for preview display, optional `thread_ts` parameter for thread replies with thread context preview)
  - Org-wide queries: List workspaces via the `list_slack_teams` tool (shared bot token)
  - Connector status checks for `access_token` in the user's `user_service_credentials` slack row (`oauth_blob`; the plugin's `slack_connected` predicate)
  - All list reads cap results to `MAX_LIMIT = 50` per request (`plugins/slack/upstream.py`); when the LLM requests a higher limit, a `ToolResultWithNotices` advisory directs it to paginate via `next_cursor` (merged into the tool's JSON result under a `notices` key)
  - Shared `httpx.AsyncClient` connection pool in `plugins/slack/upstream.py` (`max_connections=20`, `max_keepalive_connections=10`) with `_slack_request_with_retry()` helper (up to 3 attempts on timeouts and HTTP 429 rate limits)
- GitHub integration with per-user OAuth (packaged as the in-tree `plugins/github` plugin)
  - Per-user OAuth: Each user connects via standard GitHub OAuth App flow, granting `repo` and `read:org` scopes. Tokens do not expire (no refresh logic needed)
  - Read access: 19 GET-only proxy endpoints covering repos, issues, PRs, commits, file contents, org repos, and search (repositories, issues, code)
  - OAuth flow: `plugins/github/oauth.py` handles authorize and callback; popup-based with `?popup=1` support
  - Connector status checks the user's `user_service_credentials` github row (`oauth_blob.access_token`) AND the admin-configured OAuth app credentials
- Twitter/X integration with per-user OAuth 2.0 PKCE, packaged as the in-tree `plugins/twitter` plugin
  - Per-user OAuth 2.0 PKCE: Each user connects individually. Tokens expire after ~2 hours; refresh tokens are rotated on every use and renewed automatically (per-user refresh lock)
  - Read access: read-only `authed_get` allow-list on `api.twitter.com` covering user profile/lookup, DM events (all conversations and per-conversation), DM conversation metadata, bookmarked tweets, and single tweet lookup
  - Send access: DM sending via `send_twitter_dm` action requests (requires user approval); `SendTwitterDmHandler` in `plugins/twitter/handlers.py` (grandfathered unprefixed type name)
  - OAuth flow: `plugins/twitter/oauth.py` handles authorize, callback, and disconnect; popup-based with `?popup=1` support
  - Connector status checks the user's `user_service_credentials` twitter row (`oauth_blob.access_token`) AND the admin-configured OAuth app credentials
  - Access tier: DM endpoints require the Twitter/X Basic API tier ($100/month); bookmarks and tweet lookup work on all tiers
  - Scope migration: connections granted before `bookmark.read` was added get the re-authorize badge and must reconnect to pick up the new scope
- Ramp integration with per-user OAuth 2.0
  - Per-user OAuth: Each user connects individually via the authorization-code flow (confidential client, no PKCE), granting every read scope plus `offline_access`; `cards:read_vault` (full card numbers) is excluded. Tokens expire; refresh tokens rotate on every use and are renewed automatically
  - Read access: `authed_get` against `https://api.ramp.com/developer/v1/...` (transactions, cards, bills, reimbursements, vendors, statements, accounting, audit logs), gated by a read-only allow-list that excludes card-vault, token, and webhook paths
  - OAuth flow: `auth/ramp.py` handles authorize, callback, and disconnect; popup-based with `?popup=1` support
  - Connector status checks for presence of `ramp_oauth` on user record; connector hidden until an admin configures Ramp client credentials in Settings > Service Credentials
- Script runner: `run_script` and `run_python` tools execute scripts in ephemeral Podman containers with sandboxed networking (iptables rules restrict host access to only the API proxy port; no external internet)
  - workspace mounted at `/workspace`, API proxy access via `localhost:<port>` with injected `QUEST_API_KEY` (an ephemeral per-run sandbox token from `chat/sandbox_tokens.py`, valid only while the container runs and accepted only by the sandbox tool API port), `QUEST_PORT`, `QUEST_RUN_UID`, and `QUEST_RUN_GID` env vars.
  - `run_script` runs workspace files (Python, Bash); `run_python` runs inline Python code piped via stdin, avoiding throwaway `.py` files for one-off tasks. Both share the same container, networking, mounts, and security.
  - Pre-installed Python libraries: requests, openpyxl, python-docx, matplotlib, seaborn (`MPLBACKEND=Agg` for headless chart rendering), pypdf (PDF merge/split/rotate/extract), PyPDFForm (inspect/fill PDF form fields).
  - The entrypoint runs as root inside the container to set up iptables, then drops to the unprivileged user via `setpriv` with all capabilities cleared. See `Dockerfile.script-runner`, `script-runner-entry.sh`
- Container image management in `run.py` - auto-builds the missing script-runner Podman image at startup, rebuilds when the Dockerfile is modified, uses `--network=host` for builds. Image names include an environment suffix based on `QUEST_ENV` (e.g., `quest-script-runner-dev`/`quest-script-runner-prod`)

### API Layer
- REST endpoints for conversation management (list, create, get history, search)
- WebSocket endpoint for real-time chat streaming
- API routing using FastAPI APIRouter pattern
- Memory CRUD + archive/unarchive endpoints
- Guide CRUD endpoints
- Project CRUD and project-conversation endpoints
- Routine CRUD endpoints
- Schedule CRUD endpoints
- Skill Library CRUD + auto-load + search + share management + shared-with-me + user search endpoints; project skill CRUD + project auto-load endpoints; per-conversation loaded-skills endpoint
- Action request list (with optional enriched context and server-rendered preview fields), get, resolve, and count endpoints
- Conversation search endpoint (`GET /app/api/search?q={query}`) with case-insensitive matching and highlighted snippets
- Unauthenticated app config endpoint (`GET /app/api/config`) exposing `QUEST_ENV` for frontend branding and dev login UI visibility
- Unauthenticated version endpoint (`GET /app/api/version`) returning the git commit hash and the release version derived from the nearest `v<semver>` git tag, captured at process startup (see `config/version.py`; shown in Settings > About)
- Connector status, user info (includes `is_admin` flag), settings, and account management endpoints
- Admin operations endpoint (`POST /app/api/admin/shutdown`) for graceful server shutdown (requires admin auth)
- Admin System Reports endpoints (`GET /app/api/admin/system-monitor/latest-active-conversations`, `.../usage-report` and `.../most-expensive-conversations`) listing the most recently active conversations, instance-wide usage totals with previous-period comparisons and time series, and the most expensive conversations across all users; see [Admin System Reports API](api/admin-system-monitor-api.md)

### LLM Integration

#### Provider Abstraction Layer
- Multi-provider LLM abstraction in `chat/llm/` package
  - `LLMProvider` abstract base class with unified interface for session management, streaming, tool results, usage tracking, history persistence, text part creation (`make_text_part()` for advisory `extra_parts`), and turn warning injection
  - `UsageStats` dataclass with `cache_creation_tokens` and `cache_read_tokens` fields for provider-specific cache breakdown; `compute_new_input_tokens()` helper for uniform new-input calculation across providers (Gemini: `input - cached`; Anthropic: `input + cache_creation`); `compute_total_context_tokens()` helper for total context window usage (Gemini: raw `input_tokens`; Anthropic: `input + cache_read + cache_creation`)
  - Model registry in `chat/llm/config.py` mapping model IDs to providers (Gemini, Anthropic)
  - Provider-agnostic tool definitions in JSON Schema format (`chat/llm/tool_schemas.py`) with converter functions for each provider's native format, `provider_descriptions` support for provider-specific tool description overrides, and `TOOL_CALL_REGISTRY` for dynamic tools dispatched via the meta `tool_call` tool
  - Lazy singleton provider instances -- providers are only instantiated when first needed
  - See [LLM Provider Abstraction](architecture/llm-providers.md) for details

#### Gemini Provider
- `GeminiProvider` in `chat/llm/gemini_provider.py` wrapping the `google-genai` SDK (Vertex AI mode)
  - Google Cloud Application Default Credentials (ADC) authentication
  - Inline `Part.from_bytes` attachment support for analysis of binary/large files

#### Anthropic Provider
- `AnthropicProvider` in `chat/llm/anthropic_provider.py` wrapping the `anthropic[vertex]` SDK
  - Google Cloud Application Default Credentials (ADC) -- no API key needed
  - Vertex AI project and region configured in `server_config.json` (`anthropic.vertex_project_id`, `anthropic.vertex_region`)
  - File support for images (JPEG, PNG, GIF, WebP) and PDFs via base64-encoded inline content blocks in tool results, with size caps from `chat/llm/file_limits.py` (see [LLM Provider Abstraction -- Attachment Size Limits](architecture/llm-providers.md#attachment-size-limits))
  - Prompt caching via `cache_control: {"type": "ephemeral"}` breakpoints on system prompt, last tool definition, and last 2 user messages (4 breakpoints, the Vertex AI per-request limit); cache stats logged at DEBUG level; session data never mutated; `get_usage()` populates `UsageStats` with `cache_creation_tokens` and `cache_read_tokens` from session usage

#### Conversation Loop
- Route Dispatch: Internal tool call dispatch for direct endpoint invocation (`chat/route_dispatch.py`)
  - `validate_url()`, `resolve_route()`, `build_handler_kwargs()`, `execute_tool_call()` pipeline
  - `execute_tool_call()` returns `tuple[str, list[str]]` with result and advisory notices extracted from `ToolResultWithNotices` wrappers (`chat/tool_notices.py`)
  - Auto-injects `conversation_id` into Pydantic body models that declare the field
  - See [Route Dispatch Architecture](architecture/route-dispatch.md) for details
- Conversation Loop: Provider-agnostic streaming conversation loop (`chat/gemini_api/` package)
  - Three-tier tool declarations (9 base, 13 top-level, 10 sub-agent) defined in `chat/llm/tool_schemas.py`, with a meta `tool_call` tool for dynamic dispatch of the registry tools (workspace tools, memory read tools, `get_current_time`, `download_drive_file`, `archive_gmail_message`, `set_conversation_name`, `authed_get`, `get_response_content`, `project_db_query`, plus plugin-registered tools like the Slack family)
  - Streaming conversation loop with tool execution
  - Sub-agent spawning (single, parallel, and template-based parallel) with per-agent model selection (can mix providers), model-specific turn limits via `get_sub_agent_turn_limits(model)` (200K-token models: 20/15, 1M-token models: 60/50) with warning injection via `inject_turn_warning()`, sub-agent tool call events streamed to frontend for tree display, persisted as metadata on tool_result messages
  - Memory read tools (`memory_search`, `memory_list`); memory writes ride on `create_action_request(request_type="create_memory", ...)`
  - Skill discovery and loading tools (`list_skills`, `search_skills`, `load_skills`) for on-demand skill browsing and retrieval via `db/skill_store.py`
  - Action request tool (`create_action_request`) for proposing external write operations with user approval, including calendar invites via `create_calendar_invite`
  - Workspace file tools (list, get, write) with unsupported MIME type pre-checking (Office, OpenDocument, archives, executables are denied before upload with structured error responses suggesting `run_script`/`run_python` with python-docx/openpyxl)
  - Script runner tools (`run_script`, `run_python`) for executing workspace scripts and inline Python code in sandboxed Podman containers
  - MIME type rejection recovery in the conversation loop (`ClientError` catch strips file URI parts and retries the turn)
  - Conditional system instructions and user identity injection
  - See [Gemini API Integration](architecture/gemini-api.md) for details
- Chat Route Wiring: `send_message` handling on the persistent WebSocket
  - Disconnect resilience and partial message recovery
  - Interrupted message preservation: on user stop, partial assistant text and completed tool calls are saved to `sdk_history.json` with an interruption marker, so the model sees prior partial output on the next turn

### Frontend

#### Build and Development Environment
- Vite + React 19 + TypeScript configuration
- Development proxy for API integration
- Build tooling and hot module replacement

#### API Client
- REST API client with error handling
- WebSocket streaming hook for real-time messaging
- Session cookie authentication (no API key stored in browser)
- Comprehensive error handling strategy

#### UI Components
- Sidebar (wrapped with `React.memo`): Requests section with `RequestsBadge` component (event-driven refresh via `requestEvents.ts` with 30-second polling fallback and visibility-change awareness for open request count), Search section with magnifier icon (opens SearchModal via Cmd/Ctrl+K), Projects section with drill-down navigation (auto-selects latest project conversation on entry, restores previous top-level conversation on back), always-visible routines section (with create/settings/play controls), conversations list, routine grouping, conversation archive and rename, project conversation polling (30-second auto-refresh for server-created conversations)
- SearchModal: Debounced typeahead search across conversation messages, keyboard navigation, result display with highlighted snippets, click-to-navigate with scroll-to-message
- ChatPanel (wrapped with `React.memo`): Message display with `data-message-index` attributes for scroll targeting, input, streaming, model selector (locks to same-provider models after first message), "+ Skill" button for per-conversation skill loading via SkillSelectorModal, context usage indicator (fixed-size circular meter with color-coded thresholds and hover tooltip on the composer controls row) that opens the system prompt via SystemPromptModal on click, scroll-to-message with highlight animation
- Message: Markdown rendering with syntax highlighting (190+ languages) and `remark-breaks` for consistent newline preservation, unified token display showing `NEW INPUT` with provider-aware hover tooltips (Gemini: total/cached/new; Anthropic: non-cached/cache_creation/cache_read/new), sub-agent line with `SUBAGENTS {new_input} / {output} ({N} calls)` and tooltip, backward-compatible fallback to `INPUT (cached)` for older stats
- Streaming UI: Real-time markdown (with `remark-breaks` for consistent newline rendering), typing indicator, blinking cursor
- Error Display: Collapsible stack traces, dismiss button
- Tool Use Display: Collapsible tool invocations, grouped consecutive calls, sub-agent tool call tree (expandable tree structure nested under `agent_task`/`agent_task_parallel`/`agent_task_parallel_template` parent calls, with per-agent grouping for parallel tasks, persisted in `chat_history.json` and hydrated on reload)
- Action Request Display: Inline rendering of agent-proposed actions with Approve / Revise / Stop buttons (Stop discards the request and halts the conversation until the user's next message; only `subagent_return` cards show Deny); renders server-provided `preview_fields` generically (no type-specific frontend rendering logic); falls back to fetching preview from API for older messages lacking `preview_fields`; approve button label driven by server-provided `approve_label` (e.g., "Send" for messaging requests)
- Requests View: Full-pane single view for all action requests across conversations, with filter tabs (Open default/Executed/Denied/Stopped/All) showing server-provided counts, request cards with Approve / Revise / Stop buttons (approve label from server), routine/project context display, conversation navigation links, generic server-rendered preview field display, live updates via WebSocket subscription + 30s polling fallback, and non-disruptive "N new requests" banner
- File Browser: Workspace file management panel with file and folder drag-and-drop upload, inline file viewing via FileViewerModal (text files: `.md`, `.py`, `.txt`; images: `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.bmp`, `.ico`, `.avif`; PDFs via the pdf.js-based PdfViewer with thumbnail rail; `.md` files render as HTML by default via react-markdown with a "Show rendered" toggle for raw source), Save to Drive for `.md` files (converts markdown to Google Docs), folder download as zip, and file/folder deletion via meatball menu with confirmation dialog
- Stop/Interrupt: Stop button during streaming, partial response recovery
- Admin Ops Menu: Wrench icon button in the sidebar's user info bar next to the settings gear (inline variant; the System Reports page, which has no sidebar, keeps the floating fixed bottom-right variant), only visible to admin users (emails in `server_config.json` `admin_emails`), upward dropdown with "System Reports" (navigates to `/admin/system-reports`, hidden during impersonation), "Impersonate user", and "Shut down server" with two-step confirmation
- Admin System Reports: Admin-only operator dashboard at `/admin/system-reports` (legacy `/admin/system-monitor` redirects; deep links served by the `/admin/{rest:path}` SPA catch-all in `quest.py`) -- shell page with a left-hand section nav.
  - "Latest Conversations" polls the latest active conversations across all users every 60s with tab-visibility pause, in-flight dedupe, a live "Updated Xs ago" header timer, and a manual refresh button; "Total Usage" is a period-comparison matrix (today / 7 / 30 / 90 / 365 days / all time against conversations, active users, new users, calls, tokens and cost, each with its change vs the previous period) over daily / weekly / monthly trend charts; "Cost Analysis" shows the top-30 most-expensive conversations for a selectable date range.
  - Both show per-conversation token usage, estimated cost, latest context size, and active user-message days. See [Admin System Reports Architecture](architecture/admin-system-monitor.md)

#### App Integration
- App layout with two-column + file browser layout, conditional rendering of RequestsView vs chat panel, global Cmd/Ctrl+K keyboard shortcut for search
- Chat state management (conversationStore, WebSocketManager, the per-responsibility contexts under `frontend/src/contexts/`) with direct event subscriptions for component refresh
- Model selector (Gemini 3.1 Flash-Lite, Gemini 3 Flash, Gemini 3.5 Flash, Claude Haiku 4.5, Claude Sonnet 4.6, Claude Opus 4.6, Claude Opus 4.7, Claude Opus 4.8; deprecated models such as Gemini 3.1 Pro are hidden but stay usable on conversations that already have them) with provider locking after first message (filters dropdown to same-provider models) and context usage indicator
- Authentication integration (session cookie, inline sign-in, dev email login when `QUEST_ENV=dev`)
- Static file serving and production deployment (single-port)

#### User Profile and Settings
- UserInfoBar component in sidebar
- SettingsModal: thin shell (modal chrome, nav sidebar, section routing) with per-section sub-components in `frontend/src/components/settings/` -- Data Connections (OAuth connectors, API key connectors), Memories, Guides, Skills (tabbed "My Skills" / "Shared with Me" interface, CRUD, visibility, sharing with type-ahead, auto-load toggle with server-backed persistence), Sign Out
- SkillSelectorModal: multi-select modal for loading skills into the current conversation (triggered by "+ Skill" button in ChatPanel); fetches all accessible skills, client-side keyword filter, auto-loaded/loaded badges, skill persistence per conversation
- OAuth popup flows for connector management
- Conditional system instructions based on connected services
- User identity in system prompts

## Project Structure

```
quest/
├── auth/                    # Auth submodule (OAuth flows, session management, credentials)
│   ├── __init__.py         # Re-exports + router references (7 routers including dev_login_router; plugin OAuth routers mounted separately)
│   ├── config.py           # Constants, scopes, environment-based cookie name (QUEST_ENV), cookie version, credential loaders (including load_coingecko_api_key()); QUEST_ENV also drives UI branding via /app/api/config and enables dev email login
│   ├── session.py          # Cookie serializers, FastAPI auth dependencies, strict cookie validation (bool exclusion, positive-int uid check)
│   ├── google_login.py     # Google app login flow (5 endpoints)
│   ├── google_services.py  # Google Services OAuth flow (2 endpoints)
│   ├── dev_login.py        # Dev-only email login (POST /auth/dev-login, QUEST_ENV=dev only); deterministic name generation from canned lists, 404 in non-dev mode, 403 for users with Google OAuth credentials
│   ├── google_credentials.py # Credential management + make_authenticated_request()
│   └── popup_helpers.py    # OAuth popup HTML generators
├── api/                     # API endpoint modules and instruction text
│   ├── __init__.py         # Re-exports get_instructions_content, get_user_connected_services from api.instructions
│   ├── instructions.py     # Aggregation layer: get_instructions_content(), get_user_connected_services(); composes per-service get_instructions() into full instruction document
│   ├── gmail/              # Gmail package (endpoints, models, helpers, instructions)
│   │   ├── __init__.py    # Re-exports all 16 public symbols for backward compatibility
│   │   ├── constants.py   # API URLs, size limits, regexes, unicode sets
│   │   ├── models.py      # Pydantic models (DraftAttachment, CreateDraftRequest, SendEmailToSelfRequest)
│   │   ├── helpers.py     # 15 pure helper functions (clean_email_markdown, replace_urls_with_identifiers, etc.)
│   │   ├── raw_endpoints.py     # Batch POST proxy endpoint (batch_request); 7 former GET proxy endpoints migrated to authed_get
│   │   ├── simple_endpoints.py  # 6 LLM-friendly read endpoints
│   │   ├── draft_endpoints.py   # Draft creation, send-to-self, attachment resolvers
│   │   └── instructions.py      # 4 instruction text generators, get_instructions()
│   ├── calendar.py         # Calendar get_instructions() for system prompt (no endpoints; reads use authed_get)
│   ├── drive.py            # Drive get_instructions() for system prompt (no endpoints; reads use authed_get, binary downloads use download_drive_file)
│   ├── docs.py             # Docs endpoints and get_instructions()
│   ├── sheets.py           # Sheets get_instructions() for system prompt (no endpoints; reads use authed_get)
│   ├── tasks.py            # Tasks get_instructions() for system prompt (no endpoints; reads use authed_get)
│   └── airtable.py         # Airtable endpoints and get_instructions()
├── plugins/                 # Filesystem-discovered integration plugins (see architecture/plugins.md)
│   ├── README.md           # Plugin authoring guide (layout, rules, gitignore note)
│   ├── _example/           # Single-file smoke-test fixture (loaded only by tests)
│   └── github/, m365/, slack/, twitter/, twilio/, unifi/, iru/, telegram/, google_admin/, tailscale/  # In-tree plugins (see architecture/plugins.md)
├── config/                  # Configuration modules
│   └── paths.py            # Centralized data-directory path constants (PROJECT_ROOT, DATA_DIR, DATABASE_PATH, CHATS_DIR, PROJECTS_DIR, SECRET_KEY_FILE, LOG_DIR); reads optional data_dir from server_config.json
├── db/                      # Database layer (user data, memories, guides, projects, routines, routine schedules, conversation metadata, action requests, skills, skill shares, skill auto-loads, API call usage)
│   ├── __init__.py         # Package init
│   ├── engine.py           # Dual sync/async SQLAlchemy engines (both with PRAGMA foreign_keys=ON): sync engine + SessionLocal + get_db() for Alembic only; async engine (sqlite+aiosqlite) + AsyncSessionLocal + get_async_db() for all store modules
│   ├── models.py           # User, Memory, Guide, Project, Routine (includes model column), RoutineSchedule, Conversation (includes model column), ActionRequest, Skill, SkillShare, UserSkillAutoload, LlmCallGemini, and LlmCallAnthropic ORM models; ActionRequestType, ModelId, ApiCallType, and SkillVisibility enums; indexes declared in __table_args__ to keep Base.metadata in sync with DB
│   ├── user_store.py       # User data access layer, async (get_user_by_*, create_user, update_user_field, search_users, etc.)
│   ├── memory_store.py     # Memory data access layer, async (create, get, list, search, update, archive, unarchive, delete) with FTS5 search
│   ├── guide_store.py      # Guide data access layer, async (create, get, get_default, list, update, delete, ensure_default, delete_all_user_guides)
│   ├── project_store.py    # Project data access layer, async (create, get, list, update, delete, delete_all_user_projects)
│   ├── routine_store.py    # Routine data access layer, async (create, get, list, update, delete, delete_all_project_routines, delete_all_user_routines); create/update accept model parameter with ellipsis sentinel
│   ├── schedule_store.py   # Schedule data access layer, async (create, get, list_enabled, update, delete, mark_run_started/completed/failed, clear_stale_running_flags)
│   ├── conversation_store.py # Conversation metadata access layer, async (create with optional project_id and routine_id, get_meta, list_meta with include_archived filtering, update_last_message_at, delete, delete_all_user_conversations, list_project_conversations_meta with include_archived filtering, get_project_for_conversation, archive_conversation, unarchive_conversation, rename_conversation)
│   ├── action_request_store.py # Action request data access layer, async (create_action_request, get_action_request, list_action_requests, resolve_action_request, delete_all_user_action_requests, count_action_requests, count_action_requests_by_status, list_action_requests_enriched)
│   └── llm_call_store.py   # Per-provider raw LLM call token usage data access layer, async (record_api_call provider dispatcher, get_conversation_usage, get_usage_by_model_for_conversations)
├── alembic/                 # Database migrations
│   ├── env.py              # Alembic environment (imports Base.metadata from db/models.py; PRAGMA foreign_keys=ON for cascade-safe migrations; include_name callback excludes FTS5 virtual/shadow tables from autogenerate; process_revision_directives hook suppresses spurious PK nullable detection caused by SQLite quirk)
│   └── versions/           # Migration scripts
├── alembic.ini              # Alembic config (database URL: sqlite:///data/quest.db, overridden at runtime by alembic/env.py using DATABASE_PATH from config/paths)
├── chat/                    # Chat backend modules
│   ├── __init__.py         # Package init (logging delegated to logging_config.py)
│   ├── logging_config.py   # Unified logging configuration for all loggers (includes `auth` and `large_tool_results` logger namespaces); creates `data/logs/` at import time
│   ├── auth.py             # Authentication helpers (imports from auth.config, auth.session); `check_user_allowed()` relaxes domain restriction in dev mode (QUEST_ENV=dev); `is_admin()` case-insensitive check against `admin_emails` from server config
│   ├── file_routes.py      # File browser API endpoints (list, upload, download, download folder as zip, read content, file info, delete, save to Drive; flat and folder upload with optional `paths` parameter)
│   ├── file_storage.py     # File operations, path validation, filename sanitization, `save_uploaded_file_with_path()` for folder uploads, `_check_dir_conflicts()` for directory conflict detection, `get_file_content()` for text file viewing, `count_workspace_item_files()` for file/folder info, `delete_workspace_item()` for deletion, `create_folder_zip()` for folder zip download
│   ├── gemini_api/         # Conversation loop and tool dispatch submodule package
│   │   ├── __init__.py    # Re-exports: run_conversation_turn, remove_chat_session, invalidate_user_sessions, cancel_pending_wait_handles_for_conversation
│   │   ├── constants.py   # Constants (limits including `SUB_AGENT_TURN_WARNING_THRESHOLD`, deprecated model mapping, file thresholds, unsupported MIME type deny-list), `get_sub_agent_turn_limits(model)` for model-specific turn limits, `get_script_runner_image()` for env-suffixed Podman image name
│   │   ├── authed_get.py      # Handler for authed_get: authenticated GET requests to external APIs with automatic credential injection via _SERVICE_REGISTRY (hostname-based service matching with path-prefix scoping; supports Gmail Raw API, CoinGecko Pro API, Google Calendar API, Google Drive API, and Google Docs API with per-user OAuth, 401 retry, alt=media blocking in tool call path); also defines POST /api/authed-get proxy endpoint (AuthedGetRequest model, authed_get_endpoint handler) for sandbox script access
│   │   ├── tool_handlers/      # Local tool handler package, one module per tool domain; `__init__.py` re-exports every handler and helper for tool_dispatch.py and outside callers
│   │   │   ├── _common.py       # Shared workspace helpers: _get_workspace_dir, _publish_file_list_changed, _parse_content_disposition_filename, _sanitize_workspace_filename
│   │   │   ├── misc.py          # get_current_time, set_conversation_name
│   │   │   ├── workspace.py     # list/get/write/edit_workspace_file, load_gmail_attachment
│   │   │   ├── drive.py         # download_drive_file, google_export_doc
│   │   │   ├── gmail_labels.py  # archive_gmail_message, list_gmail_quest_labels, modify_gmail_labels
│   │   │   ├── gmail_simple.py  # Gmail Simple tools wrapping api/gmail endpoint functions
│   │   │   ├── memory.py        # memory_search, memory_list
│   │   │   ├── skills.py        # list/search/load_skills, list_my_skills, get_skill
│   │   │   ├── routines.py      # list_routines
│   │   │   ├── sandbox.py       # run_script, run_python, _build_script_podman_cmd
│   │   │   ├── project_db.py    # project_db_query
│   │   │   └── response_blobs.py # get_response_content chunked reading
│   │   ├── tool_dispatch.py    # Shared tool call routing (_dispatch_tool_call) with tool_call meta tool dispatch branch (includes download_drive_file, archive_gmail_message, set_conversation_name, and authed_get dispatch)
│   │   ├── system_prompt.py    # System prompt builders (top-level and sub-agent) with skills_content injection and auto-generated dynamic tools section from TOOL_CALL_REGISTRY
│   │   ├── history.py         # SDK history persistence (save/load sdk_history.json)
│   │   ├── session.py         # Client singleton, session store, pending confirmations
│   │   ├── sub_agent.py       # Sub-agent execution (single and parallel) with skills_content passthrough, emits sub_agent_tool_use/sub_agent_tool_result WebSocket events
│   │   ├── turn_tools.py      # Per-tool handler functions for the loop-handled dispatch arms (TURN_TOOL_HANDLERS registry: agent_task/parallel/template spawns, send_slack_reply_and_get_response, return_to_caller, return_final_response, create_action_request, tool_call:wait_for_handles), the suspend sentinels (SuspendForWaitHandles/SuspendForSlackReply/SuspendForActionRequest/FinishInferenceResponse), wait-handle validation + result building, the shared action-request card tail, and the _emit_durable persist-before-emit helper
│   │   └── conversation.py   # Main run_conversation_turn() entry point and conversation loop, resolves auto-loaded skills from user settings, resolves conversation-loaded skills from skill_ids parameter and injects as <conversation_skills_loaded> XML section in user message envelope, persists loaded skill IDs via ChatStorage, persists system prompt to system_prompt.txt via ChatStorage.set_system_prompt(), captures sub-agent events and persists them as sub_agent_tool_calls metadata on tool_result messages, emits context_tokens and max_context_tokens in stats event via compute_total_context_tokens(), emits conversation_updated WebSocket event when set_conversation_name succeeds
│   ├── llm/                # LLM provider abstraction layer
│   │   ├── __init__.py    # Re-exports: LLMProvider, StreamEvent, UsageStats, ToolSpec, get_provider_for_model, get_provider_instance, MODEL_REGISTRY
│   │   ├── base.py        # Abstract LLMProvider base class, shared types (StreamEvent, UsageStats with cache_creation_tokens/cache_read_tokens, ToolSpec), compute_new_input_tokens() helper, and compute_total_context_tokens() helper for context window usage
│   │   ├── config.py      # Model registry (MODEL_REGISTRY), provider lookup, singleton management
│   │   ├── gemini_provider.py    # GeminiProvider implementation (google-genai SDK)
│   │   ├── anthropic_provider.py # AnthropicProvider implementation (anthropic[vertex] SDK, Vertex AI)
│   │   └── tool_schemas.py      # Provider-agnostic tool definitions (JSON Schema), TOOL_CALL_SPEC, TOOL_CALL_REGISTRY (dynamic tools including edit_workspace_file, download_drive_file, archive_gmail_message, set_conversation_name, and authed_get), converter functions (to_gemini_declarations, to_anthropic_tools)
│   ├── tool_notices.py     # ToolResultWithNotices dataclass for attaching advisory notices to route handler responses
│   ├── route_dispatch.py   # Internal route dispatch for tool calls with logging, conversation_id auto-injection, ToolResultWithNotices extraction, _BLOCKED_PROXY_PATHS blocklist for internal-only endpoints
│   ├── memory_routes.py    # Memory CRUD + update + archive/unarchive REST endpoints (/app/api/memories)
│   ├── guide_routes.py     # Guide CRUD REST endpoints (/app/api/guides), invalidates sessions on content change
│   ├── project_routes.py   # Project CRUD and project-conversation REST endpoints (/app/api/projects)
│   ├── routine_routes.py   # Routine CRUD REST endpoints (/app/api/projects/{project_id}/routines); create/update accept model and clear_model fields
│   ├── schedule_routes.py  # Schedule CRUD REST endpoints (/app/api/projects/{project_id}/routines/{routine_id}/schedule)
│   ├── action_request_routes.py # Action request REST endpoints (/app/api/action-requests): list (with optional enriched context), count, counts (grouped by status), get, resolve; all responses enriched with preview_fields, display_name, and approve_label via _enrich_with_preview()
│   ├── action_request_types/    # Action request handler package
│   │   ├── __init__.py          # Re-exports for backward compatibility
│   │   ├── base.py              # ActionRequestHandler ABC (render_preview(), approve_label)
│   │   ├── registry.py          # Handler registry dict, get_preview_for_request()
│   │   ├── helpers.py           # _extract_first_name()
│   │   ├── create_calendar_invite.py # CreateCalendarInviteHandler (Google Calendar invite creation, writable scope check, preview helpers)
│   │   └── edit_calendar_event.py   # EditCalendarEventHandler (PATCH an existing event; expected_updated read-before-write check at proposal + approve time, current_event snapshot for the before/after card)
│   ├── scheduler.py        # Background asyncio scheduler daemon (polls for due schedules, executes routines headlessly, passes routine_id to conversation creation, passes routine model to run_conversation_turn); tracks spawned execution tasks in _active_execution_tasks set; shutdown() cancels all active tasks; _execute_scheduled_run() handles CancelledError
│   ├── routes.py           # FastAPI routes (REST + WebSocket, passes guide_id, project_id, and skill_ids to Gemini API, GET /conversations/{id}/loaded-skills endpoint for per-conversation loaded skills, GET /conversations/{id}/system-prompt endpoint for system prompt viewer, UserSettingsUpdate with custom_system_prompt, conversation archive/unarchive/rename endpoints, search endpoint, include_archived query param, unauthenticated /app/api/config and /app/api/version endpoints, admin shutdown endpoint POST /app/api/admin/shutdown, /app/api/me returns is_admin flag, _save_interrupted_sdk_history() for preserving partial assistant responses on cancellation)
│   └── storage.py          # Chat persistence layer (includes utc_timestamp() helper, guide snapshot methods, project workspace helpers, include_archived pass-through on list methods, rename_conversation pass-through, search_conversations() for file-based message scanning, get_loaded_skill_ids()/add_loaded_skill_ids() for per-conversation loaded skills persistence, get_workspace_read_paths()/add_workspace_read_paths() for the per-conversation workspace_reads.json sidecar gating edit_workspace_file, set_system_prompt()/get_system_prompt_text() for per-conversation system prompt persistence to system_prompt.txt)
├── frontend/                # React frontend application
│   ├── src/
│   │   ├── api/            # API client and types
│   │   │   ├── client.ts   # REST API client functions (includes guide CRUD, skill CRUD + sharing + auto-load: fetchSkills, createSkill, updateSkill, deleteSkill, fetchSkillShares, addSkillShares, removeSkillShare, searchUsers, fetchAutoloadedSkillIds, setSkillAutoload, fetchSharedWithMeSkills; fetchConversationLoadedSkills for per-conversation loaded skills; fetchSystemPrompt for system prompt viewer; project CRUD, routine CRUD, schedule CRUD: fetchRoutineSchedule, createRoutineSchedule, updateRoutineSchedule, deleteRoutineSchedule; createProjectConversation accepts optional routineId; archiveConversation, unarchiveConversation, renameConversation; fetchConversations and fetchProjectConversations accept includeArchived param; action request functions: fetchActionRequests, fetchActionRequestsEnriched, fetchActionRequestCount, fetchActionRequestCounts, fetchActionRequest, resolveActionRequest; searchConversations; triggerAdminShutdown)
│   │   │   ├── config.ts   # API configuration and endpoints (includes guides(), guide(id), skills(), skill(id), skillAutoload(skillId), skillsAutoloaded(), skillsSharedWithMe(), skillShares(skillId), skillShare(skillId, userId), userSearch(query), conversationLoadedSkills(conversationId), conversationSystemPrompt(conversationId), projectRoutines(projectId), projectRoutine(projectId, routineId), routineSchedule(projectId, routineId), actionRequestsCount(), actionRequestsCounts(), adminShutdown() builders)
│   │   │   ├── fileApi.ts  # File browser API client functions (includes `uploadFilesWithPaths()` for folder uploads, `fetchFileContent()` for text viewing, `downloadFolder()` for folder zip download, and `saveToDrive()` for saving .md files to Google Drive)
│   │   │   └── types.ts    # TypeScript type definitions (includes Guide, GuidesListResponse, GuideSnapshot, Skill, SkillsListResponse, SkillShare, SkillSharesResponse, UserSearchResult, UserSearchResponse, UserSettings, Routine with model field, RoutineSchedule, RoutinesListResponse, WebSocketSendMessage with guide_id and skill_ids, Conversation with routine_id, archived, custom_name, and model, ActionRequest with preview_fields/display_name/approve_label, ActionRequestMessage with preview_fields/approve_label, EnrichedActionRequest, EnrichedActionRequestsListResponse, ActionRequestCountResponse, ActionRequestCountsResponse, FileContentResponse, ConnectorStatus, ConnectorsResponse, SearchResult, SearchResponse, StreamEvent with conversation_updated type (conversation_id and custom_name fields), SubAgentToolCallInfo, SubAgentToolUseMessage, SubAgentToolResultMessage, PersistedSubAgentEvent, UsageStats with new_input_tokens/provider/cache_creation_tokens/cache_read_tokens/context_tokens/max_context_tokens and per-call-type breakdown fields)
│   │   ├── constants/      # Shared constants
│   │   │   └── models.ts   # AVAILABLE_MODELS array with model IDs, display names, provider tags, maxInputTokens, and an optional deprecated flag per model; SELECTABLE_MODELS filtered view (excludes deprecated models, e.g. Gemini 3.1 Pro) used by all pickers; getProviderForModel() helper; getModelDisplayName() helper for resolving model IDs to human-readable names; isDeprecatedModel() helper; DEPRECATED_MODEL_MAP for silent remapping of retired models
│   │   ├── components/     # React UI components
│   │   │   ├── Sidebar.tsx        # Sidebar with Requests section (badge counter with event-driven refresh via requestEvents.ts and 30-second polling fallback), Search section with magnifier icon (opens SearchModal), projects section (drill-down navigation with conversation auto-selection and top-level conversation restore), always-visible routines section, and conversations list (includes UserInfoBar); subscribes directly to `webSocketManager.onStreamComplete` for conversation list refresh using stale-while-revalidate (`silentLoadConversations`); subscribes to `webSocketManager.onConversationRenamed` for real-time sidebar name updates when the model sets a name via `set_conversation_name`; routine conversation grouping with collapsible entries; model propagation when running routines; conversation archive and rename with meatball menu and optimistic UI updates; per-section filter popover (filter icon with "Show Archived" and "Show Slack Conversations" checkboxes, both unchecked by default, no persistence); inline rename UI (text input, Enter to save, Escape to cancel); 30-second project conversation polling for server-created conversations (with visibility-aware scheduling and in-flight guard)
│   │   │   ├── sidebar/           # Sidebar section components (Sidebar.tsx is the shell; data lives in the hooks above)
│   │   │   │   ├── ConversationsSection.tsx  # Top-level Conversations header + paged list + auto-paging Load more row
│   │   │   │   ├── ProjectPanel.tsx          # Drilled project: header, Routines section, grouped conversation list
│   │   │   │   ├── ProjectsSection.tsx       # Projects list / Create Project call-to-action
│   │   │   │   ├── RoutinesSection.tsx       # Routine rows (schedule indicator, settings, run)
│   │   │   │   ├── ConversationRow.tsx       # One conversation row (origin badge, title / inline rename, options menu); shared by all three lists
│   │   │   │   ├── ConversationFilterMenu.tsx  # Section-header filter popover (Show Archived / Slack / Inference)
│   │   │   │   ├── RequestsBadge.tsx         # Open-request count badge on the brand-bar inbox button
│   │   │   │   └── icons.tsx                 # The sidebar's inline SVG glyphs
│   │   │   ├── SearchModal.tsx    # Search modal with debounced typeahead, keyboard navigation, highlighted result snippets, click-to-navigate
│   │   │   ├── SearchModal.css    # SearchModal styles
│   │   │   ├── ChatPanel.tsx      # Main chat interface (uses useConversation, "+ Skill" button for conversation skill loading, model selector hydrated from server with provider locking after first message and PATCH-back on change, context usage indicator via ContextIndicator component with system prompt viewer, data-message-index attributes, scroll-to-message with highlight animation)
│   │   │   ├── ChatPanel.css      # ChatPanel styles (includes scroll-to-message highlight animation, model-selector-row layout, "+ Skill" button)
│   │   │   ├── SkillSelectorModal.tsx # Skill selector modal for loading skills into a conversation (multi-select, keyword filter, auto-loaded/loaded badges)
│   │   │   ├── SkillSelectorModal.css # Skill selector modal styles (overlay, search input, skill rows, badges, footer buttons)
│   │   │   ├── ContextIndicator.tsx # Context usage indicator component (fixed-size SVG ring that fills with usage, color-coded thresholds, hover tooltip with percentage + raw token counts, click opens SystemPromptModal); rendered in the composer controls row
│   │   │   ├── ContextIndicator.css # ContextIndicator styles (ring track/fill colours for normal/warning/danger via CSS variables, tooltip positioning, click target, dark/light mode)
│   │   │   ├── SystemPromptModal.tsx # Full-screen modal for viewing the system prompt used in the current conversation (fetches from API on open, styled like FileViewerModal)
│   │   │   ├── SystemPromptModal.css # SystemPromptModal styles (overlay, modal layout, pre-formatted content)
│   │   │   ├── Message.tsx        # Message component with markdown, unified token display with provider-aware hover tooltips
│   │   │   ├── Message.css        # Message, stats, and tooltip styles (includes .stats-tooltip-container and .stats-tooltip)
│   │   │   ├── ToolUseMessage.tsx # Tool use display component with sub-agent tool call tree (SubAgentToolCallEntry, SubAgentSection sub-components)
│   │   │   ├── ToolUseMessage.css # ToolUseMessage styles including sub-agent tree structure
│   │   │   ├── ToolCallGroup.tsx  # Groups consecutive tool calls into collapsible units, passes subAgentToolCalls to ToolUseMessage
│   │   │   ├── ToolCallGroup.css  # ToolCallGroup styles (dark/light mode)
│   │   │   ├── FileBrowser.tsx    # File browser panel component (file and folder drag-and-drop upload with structured error display, opens FileViewerModal for viewable files including images and PDFs, folder download as zip with notification bar); subscribes to `persistentWebSocket.onGlobalEvent` for `file_list_changed` per-user globals (200ms debounce) and silent-refreshes mid-turn after each workspace mutation, filtered by active conversation / project
│   │   │   ├── FileBrowser.css    # File browser styles (includes multi-line error display and zipping notification bar)
│   │   │   ├── FileViewerModal.tsx # Full-screen modal for viewing text files, images, and PDFs with download button and Save to Drive for .md files; PDFs fetched as ArrayBuffer with a 100 MB preview cap; .md files render as HTML by default via react-markdown (reusing the chat renderer's markdownComponents) with a "Show rendered" header toggle for raw source
│   │   │   ├── FileViewerModal.css # FileViewerModal styles (includes checkerboard background for image transparency)
│   │   │   ├── PdfViewer.tsx      # pdf.js-based PDF viewer (thumbnail rail + fit-to-width pages, IntersectionObserver-lazy canvas rendering with eviction, self-hosted worker/cmaps/fonts/wasm assets)
│   │   │   ├── PdfViewer.css      # PdfViewer styles (two-pane layout, thumbnail rail, dark/light mode)
│   │   │   ├── UserInfoBar.tsx    # User profile bar at bottom of sidebar
│   │   │   ├── UserInfoBar.css    # UserInfoBar styles
│   │   │   ├── SettingsModal.tsx   # Settings modal shell: modal chrome, nav sidebar, section routing to settings/ sub-components
│   │   │   ├── SettingsModal.css   # SettingsModal shell/layout styles and shared utility classes
│   │   │   ├── settings/           # Per-section settings sub-components (each manages own state and data fetching)
│   │   │   │   ├── index.ts                  # Barrel re-exports all section components
│   │   │   │   ├── DataConnectionsSection.tsx # OAuth connectors, API key forms, popup handling
│   │   │   │   ├── DataConnectionsSection.css # Data connections styles (connector rows, badges, buttons)
│   │   │   │   ├── MemoriesSection.tsx        # Memory CRUD with archive toggle
│   │   │   │   ├── MemoriesSection.css        # Memories section styles
│   │   │   │   ├── GuidesSection.tsx          # Guide CRUD with default guide handling
│   │   │   │   ├── GuidesSection.css          # Guides section styles
│   │   │   │   ├── SkillsSection.tsx          # Tabbed skill management ("My Skills" / "Shared with Me"), CRUD, visibility, sharing with type-ahead, auto-load toggle
│   │   │   │   ├── SkillsSection.css          # Skills section styles (tabs, skill cards, visibility badges, sharing UI, auto-load toggle)
│   │   │   │   ├── SignOutSection.tsx          # Logout, disconnect, delete account
│   │   │   │   └── SignOutSection.css          # Sign out section styles
│   │   │   ├── SignInScreen.tsx   # Inline sign-in screen for unauthenticated users (heading uses appName from AppConfigContext); in dev mode (isDevMode), shows email input + "Dev Login" button for email-based login via POST /auth/dev-login
│   │   │   ├── SignInScreen.css   # SignInScreen styles (includes dev login section: divider, form, input, button, note)
│   │   │   ├── ActionRequestMessage.tsx  # Inline action request UI with Approve / Revise / Stop buttons (non-blocking, database-persisted); renders server-provided preview_fields generically; falls back to fetching preview from API for older messages; approve button label from server-provided approve_label; calendar invites collapse to a Created summary; emits request count change events after resolution
│   │   │   ├── ActionRequestMessage.css  # ActionRequestMessage styles
│   │   │   ├── RequestsView.tsx         # Full-pane Requests view with filter tabs (Open default, Executed, Denied, Stopped, All), request cards, Approve / Revise / Stop, routine/project context, live updates via WebSocket + 30s polling, "N new requests" banner
│   │   │   ├── RequestsView.css         # RequestsView styles
│   │   │   ├── AdminOpsMenu.tsx         # Admin operations menu (wrench icon, visible to admins only); inline variant rendered in UserInfoBar next to the settings gear, floating bottom-right variant on the sidebar-less System Reports page; server shutdown with two-step confirmation; reads isAdmin from AuthContext
│   │   │   ├── AdminOpsMenu.css         # AdminOpsMenu styles (dark/light theme support)
│   │   │   ├── NewProjectModal.tsx       # Modal for creating a new project (name input)
│   │   │   ├── NewProjectModal.css       # NewProjectModal styles
│   │   │   ├── ProjectSettingsModal.tsx  # Project settings modal with sidebar navigation (General, Skills, Danger Zone); embeds ProjectSkillsSection
│   │   │   ├── ProjectSettingsModal.css  # ProjectSettingsModal styles (widened modal, row layout for sidebar)
│   │   │   ├── ProjectSkillsSection.tsx  # Project skills section with three tabs (Project Skills CRUD, My Skills read-only, Shared with Me read-only), project auto-load toggle
│   │   │   ├── NewRoutineModal.tsx       # Dedicated modal for creating new routines (name, prompt, guide, model)
│   │   │   ├── NewRoutineModal.css       # NewRoutineModal styles
│   │   │   ├── RoutineSettingsModal.tsx  # Dedicated modal (720px) with left nav sidebar (Prompt, Schedule, Delete sections), auto-growing prompt textarea
│   │   │   └── RoutineSettingsModal.css  # RoutineSettingsModal styles (left nav layout, section content, disabled schedule fields)
│   │   ├── contexts/       # React contexts
│   │   │   ├── AppProviders.tsx             # Composes the per-responsibility contexts below; App.tsx wraps the routes in it
│   │   │   ├── AppConfigContext.tsx         # Unauthenticated GET /app/api/config state (appName, isDevMode, sign-in method, model catalog + available_models, refreshModelCatalog) and the GET /app/api/version redeploy poll (updateAvailable)
│   │   │   ├── AuthContext.tsx              # Session check on mount (GET /app/api/me): isAuthenticated/isCheckingAuth, user identity, isAdmin, enabledFeatures, impersonation, connection flags, hasPassword, the one-shot `sessionSnapshot` other contexts hydrate from; opens/closes the persistent WebSocket with the session
│   │   │   ├── AppearanceContext.tsx        # Settings > Appearance colour scheme + colour theme (localStorage boot, /me hydration, PUT /settings)
│   │   │   ├── NavigationContext.tsx        # activeConversationId (URL mirror), showRequestsView, scrollToMessageIndex, settings modal state, pendingRoutineMessage / pendingFirstMessage hand-offs
│   │   │   ├── ProjectsContext.tsx          # Project list, URL-mirrored activeProjectId, Sidebar drilledProjectId
│   │   │   ├── GuidesContext.tsx            # Deprecated guide list for the Settings Guides section and routine guide-override dropdowns (fetched only while the guides gate is open)
│   │   │   ├── ConversationModelsContext.tsx  # Per-user default models (hydrated from /me, refreshDefaultModel / persistDefaultModel), per-conversation model overrides, provider locks persisted to localStorage
│   │   │   ├── ConversationSkillsContext.tsx  # Per-conversation queued/loaded skill state for the conversation skill loader
│   │   │   └── FileBrowserStateContext.tsx    # Per-conversation file-browser path/history
│   │   ├── hooks/          # React hooks
│   │   │   ├── useConversation.ts   # Per-conversation state subscription (exposes subAgentToolCalls map and context usage, hydrates sub-agent data, context usage, and loaded skills from persisted data on load)
│   │   │   ├── useFileBrowser.ts    # File browser state management (includes `uploadFilesWithPaths` for folder uploads, `buildUploadErrorMessage` for partial error display, `downloadFolder` for folder zip download, and `silentRefresh` for stale-while-revalidate file list updates)
│   │   │   ├── useTopLevelConversations.ts  # Sidebar data hook: paged standalone conversation list (filters, keyset paging, stale-while-revalidate refresh on WS events, optimistic edits)
│   │   │   ├── useProjectConversations.ts   # Sidebar data hook: per-project conversation lists (on-demand load, 30s visibility-aware poll while drilled, WS refresh, optimistic edits)
│   │   │   ├── useProjectRoutines.ts        # Sidebar data hook: per-project routine lists + routine_list_changed refresh
│   │   │   └── useConversationListActions.ts  # Per-list row UI state (open menu, inline rename) and optimistic archive/unarchive/rename for one Sidebar list
│   │   ├── services/       # Service singletons
│   │   │   ├── requestEvents.ts     # Lightweight pub/sub event emitter for request count change signals
│   │   │   └── WebSocketManager.ts  # Routes WebSocket messages to store (sendMessage accepts optional guideId and skillIds); emits request count change events on `action_request` messages; routes `sub_agent_tool_use` and `sub_agent_tool_result` events to conversationStore; handles `conversation_updated` events via `onConversationRenamed` callback mechanism for real-time sidebar name updates; hydrates context usage from stats events via setContextUsage(); exposes `onStreamComplete` and `onConversationRenamed` callback subscriptions (Sidebar, ProjectTables). The earlier `onToolResult` / `publishToolResult` plumbing was removed when FileBrowser migrated to subscribing for `file_list_changed` directly via `persistentWebSocket.onGlobalEvent`
│   │   ├── store/          # State management
│   │   │   └── conversationStore.ts # Observable per-conversation state (includes getConversationSnapshot for per-conversation change detection, subAgentToolCalls Map for sub-agent tool call tree display, contextTokens/maxContextTokens for context usage indicator)
│   │   ├── utils/          # Utility functions
│   │   │   ├── sidebarItems.ts      # Pure Sidebar list derivation: filter toggles, routine-run grouping, most-recent-first interleaving, run timestamps
│   │   │   ├── auth.ts          # Session check utility (checkSession(), return type includes is_admin)
│   │   │   ├── directoryTraversal.ts # Recursive directory traversal for folder drag-and-drop uploads via `webkitGetAsEntry()` API; exports `FileWithPath` and `extractFilesFromDataTransfer()`
│   │   │   ├── formatters.ts    # Formatting utilities (formatTimestamp, parseUTCTimestamp, formatNumber)
│   │   │   └── oauthPopup.ts    # OAuth popup window utility (openOAuthPopup())
│   │   ├── App.tsx         # Main app with react-router-dom Routes (/,  /chats/:id, /projects/:pid/:id); URL params synced to context via useEffect; navigate() for conversation selection; handleProjectIdLoaded redirect
│   │   └── main.tsx        # Application entry point; wraps App in BrowserRouter
│   ├── vite.config.ts      # Vite configuration (includes vite-plugin-static-copy targets that self-host the pdf.js cmaps/standard_fonts/wasm/iccs assets under dist/assets/pdfjs/)
│   └── package.json        # Frontend dependencies
├── data/
│   ├── quest.db           # SQLite database (user accounts, API keys, OAuth tokens, settings, memories, guides, projects, routines, routine schedules, conversation metadata with routine_id association, archived flag, and custom_name, action requests)
│   ├── logs/               # Application log files (created automatically at startup)
│   │   └── large_tool_results.jsonl  # Tool call results exceeding 2048 bytes (JSONL format, RotatingFileHandler, 50MB max, 3 backups)
│   ├── projects/           # Project workspaces (shared across conversations in a project)
│   │   └── {project_id}/
│   │       └── workspace/
│   └── chats/              # Chat storage (flat layout, one folder per conversation UUID)
│       └── {id}/
│           ├── chat_history.json
│           ├── sdk_history.json
│           ├── system_prompt.txt  # Persisted system prompt for the system prompt viewer (written on each message send)
│           └── workspace/  # Standalone conversations only; project conversations use data/projects/{project_id}/workspace/
├── docs/                    # This documentation
├── quest.py               # Main FastAPI application (registers auth/ routers including dev_login_router, guide_router, project_router, routine_router, and schedule_router; lifespan handler starts/stops the background scheduler, creates shutdown_event for admin-triggered shutdown, cancels all in-progress execution tasks via scheduler.shutdown(); `/api/instructions` and `/api/reset-api-key` endpoints; static file serving; instruction functions extracted to api/instructions.py and per-service get_instructions() in each API submodule), uses "quest" logger
├── logging_config.json     # Logging config for CLI-based uvicorn invocations (run.py); includes `large_tool_results` file handler
├── run.py                  # Startup script (--prod/--dev flags, sets QUEST_ENV, installs frontend deps and builds, syncs backend deps, runs migrations, creates `data/logs/` directory, auto-builds missing container images with env suffix); catches exit codes 130 (SIGINT) and 143 (SIGTERM) for clean "Server stopped." message
├── Dockerfile.script-runner # Podman image for run_script tool (Python 3.12-slim + curl, jq, bash, zip, unzip, requests, openpyxl, python-docx, matplotlib, seaborn, pypdf, PyPDFForm, socat, iptables)
├── script-runner-entry.sh  # Entrypoint for script-runner container (iptables setup, privilege drop via setpriv)
├── tests/                   # Pytest test suite (dev dependency: pytest>=8.0.0)
│   ├── test_email_cleanup.py     # 30 tests for clean_email_markdown() in api/gmail/helpers.py
│   ├── test_url_replacement.py  # 22 tests for replace_urls_with_identifiers() in api/gmail/helpers.py
│   └── test_syntax.py           # Parametrized syntax validation (py_compile on all .py files)
└── pyproject.toml          # Python dependencies (uv); dev group includes pytest>=8.0.0
```

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend Framework | FastAPI |
| Frontend Framework | React 19 |
| Build Tool | Vite 7 |
| Language (Frontend) | TypeScript |
| Language (Backend) | Python 3.12 |
| Package Manager (Backend) | uv |
| Package Manager (Frontend) | npm |
| AI Models | Google Gemini (3.1 Flash-Lite, 3 Flash, 3.5 Flash, 3.5 Flash-Lite, 3.6 Flash) and Anthropic Claude (Haiku 4.5 through Opus 5 on Vertex AI) -- user-selectable per conversation and per routine; Gemini 3 Pro silently remapped to Gemini 3.1 Pro, which is deprecated (Vertex-backed, hidden from pickers, still runnable on existing conversations/routines) |
| Authentication | Google OAuth 2.0 (two-tier: app login + Google Services), inline sign-in on `/`, dev-only email login (`QUEST_ENV=dev`) |
| User Storage | SQLite + SQLAlchemy + Alembic (FTS5 for memory search, guides table, projects table, routines table with model column, routine_schedules table, conversations table with routine_id FK, archived column, custom_name column, and model column, action_requests table) |
| Chat Storage | JSON files (message history, with file-based search via `ChatStorage.search_conversations()`, `sub_agent_tool_calls` metadata on `tool_result` messages for sub-agent display); SQLite `conversations` table (ownership + timestamps + project association + routine association + archive status + custom name) |
| Containerization | Podman (for script runner, env-suffixed images e.g. `quest-script-runner-dev`/`quest-script-runner-prod`) |

## Getting Started

To get started with development:

1. Read the [Development Setup Guide](setup/development.md)
2. Understand the [System Architecture](architecture/overview.md)
3. Review the [Chat API Documentation](api/chat-api.md)
