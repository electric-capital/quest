
## Architecture

| File | Summary |
|------|---------|
| [overview.md](architecture/overview.md) | System map: code map of the top-level directories with their owning docs, request lifecycles, storage, cross-cutting decisions |
| [admin-impersonation.md](architecture/admin-impersonation.md) | Admin impersonation of another user: session cookie field, endpoints, UI state while impersonating |
| [admin-system-monitor.md](architecture/admin-system-monitor.md) | Admin System Reports dashboard: conversations, costs, users, models, guides and total-usage sections, cost estimation |
| [auth.md](architecture/auth.md) | Sign-in (Google OAuth or email/password), sessions and cookies, OAuth state, Google service scopes, login admission |
| [conversation-flags.md](architecture/conversation-flags.md) | Per-conversation opt-in flags set at conversation start: composer popover, magic first line, registry, gating |
| [data-paths.md](architecture/data-paths.md) | Data directory layout, the id-to-path resolvers for conversations, projects and docs, and ownership guards for routes |
| [encryption-at-rest.md](architecture/encryption-at-rest.md) | Encryption of stored secrets: password-wrapped data key, encrypted columns and credential files, startup unlock |
| [expensive-resume.md](architecture/expensive-resume.md) | Warning before resuming a long-idle, long-context conversation: rules, cost estimate, send gate, warning card |
| [memory-suggestions.md](architecture/memory-suggestions.md) | Non-blocking `suggest_memory` tool, the review bubble/panel above the composer, and the guidance on what the model should suggest |
| [project-memories.md](architecture/project-memories.md) | Project-scoped memories: the store, read/suggest tools, the create_project_memory card, the project bubble and Project Settings table |
| [feature-gates.md](architecture/feature-gates.md) | Admin on/off switches for optional features, per-user access lists, and how each gate is enforced |
| [database.md](architecture/database.md) | SQLite schema via SQLAlchemy and Alembic: tables, store modules, migrations, encrypted columns, LLM call analytics |
| [frontend.md](architecture/frontend.md) | React + Vite frontend: contexts and state, routing, sidebar, composer, chat panel, messages, right-panel file cards, theming |
| [gemini-api.md](architecture/gemini-api.md) | Conversation loop and tools: tool tiers and dispatch, system prompt, sub-agents, authed requests, suspend, error surfacing |
| [realtime.md](architecture/realtime.md) | Persistent multiplexed WebSocket: subscribe and catch-up, send and stop, durable and transient events, per-user globals |
| [guides.md](architecture/guides.md) | Deprecated guides (system prompt presets): snapshot mechanism, feature gate, conversion to skills |
| [llm-providers.md](architecture/llm-providers.md) | LLM provider layer: Vertex Gemini and Claude, OpenAI-compatible instances, model registry, streaming, refusal fallbacks |
| [inference-providers.md](architecture/inference-providers.md) | Admin Inference Providers panel: Vertex AI models, admin-added provider instances and their catalogs, model health checks |
| [model-selection.md](architecture/model-selection.md) | Admin Model Selection table: composer menu slots and descriptors, private and public allow flags enforced per turn |
| [logging.md](architecture/logging.md) | Unified logging configuration: format, levels, user identification convention |
| [oauth-popup.md](architecture/oauth-popup.md) | OAuth popup window flow for connector buttons in Settings, using postMessage |
| [action-requests.md](architecture/action-requests.md) | Agent-proposed writes that need user approval: request types, validation, approval cards, blocking and resume |
| [project-db.md](architecture/project-db.md) | Per-project SQLite database behind the project_db_query tool: limits, ATTACH blocking, table browser |
| [projects.md](architecture/projects.md) | Projects: conversation and project workspaces, instructions, skills, conversion from a chat, archiving, doc access |
| [public-projects.md](architecture/public-projects.md) | Public projects: internet-enabled sandbox with internal resources cut off, enforcement layers, public prompt and image |
| [quest-docs.md](architecture/quest-docs.md) | Quest Docs (in-app markdown docs): storage, access rule, sharing, editing, history, model tools, approvals, frontend |
| [quest-tables.md](architecture/quest-tables.md) | Quest Tables (in-app typed tables): metadata tables, on-disk path, modes, store rules and concurrency tokens, feature gate |
| [plugins.md](architecture/plugins.md) | Plugin system: discovery, manifest validation, atomic registration, what a plugin can register, in-tree plugins |
| [route-dispatch.md](architecture/route-dispatch.md) | Internal route dispatch for tool calls: conversation_id injection, blocked proxy paths |
| [run-modes.md](architecture/run-modes.md) | Run modes (local, staging, prod): capability flags, zero-credential local mode, dev-config pre-baking, prod bootstrap |
| [routines.md](architecture/routines.md) | Routines: one-click project prompts with model and skill auto-loads, completion signal, agent create/edit, costs |
| [scheduling.md](architecture/scheduling.md) | Routine scheduling daemon: schedule types, restart-safe due times, catch-up and missed runs, run ledger, pausing |
| [script-runner.md](architecture/script-runner.md) | Sandboxed script execution in Podman: restricted and public images, mounts, sandbox tool API and tokens, seccomp |
| [service-credentials.md](architecture/service-credentials.md) | Per-service upstream credential store and the schema-driven admin Service Credentials editor |
| [settings-data-connections.md](architecture/settings-data-connections.md) | Settings > Data Connections: data-driven connector rows, add-connection picker, per-connection cards |
| [user-connections.md](architecture/user-connections.md) | Multiple named connections per user and plugin service: data model, connection argument, routes, prompt paragraph |
| [skill-library.md](architecture/skill-library.md) | Skills: user, shared, public, project and system skills, auto-load tiers, discovery and loading tools, write path |
| [slack-pending-request-notifier.md](architecture/slack-pending-request-notifier.md) | Background daemon that DMs users on Slack about open action requests, with per-user settings |
| [slack-socket-mode.md](architecture/slack-socket-mode.md) | Slack-driven conversations over Socket Mode: DM reply tool, thread mapping, user resolution, read-only web view |
| [user-subagents.md](architecture/user-subagents.md) | Cross-user subagents: double approval, read-only run in the target's account, return card, gating |
| [voice-input.md](architecture/voice-input.md) | Dictated prompts: composer mic recording, server-side Gemini transcription, failure reports, feature gate |
| [wait-handles.md](architecture/wait-handles.md) | DB-backed wait handles for tools that block on user input: kinds, suspend sentinels, restart-safe resume |

## API

| File | Summary |
|------|---------|
| [chat-api.md](api/chat-api.md) | Conversation REST endpoints, the persistent WebSocket protocol, and the Data Connections endpoints |
| [api-client-usage.md](api/api-client-usage.md) | Frontend TypeScript API client: request helpers, types, error handling |
| [admin-system-monitor-api.md](api/admin-system-monitor-api.md) | Admin System Reports endpoints: conversation, user, model, guide and usage reports, cost conventions |
| [airtable-api.md](api/airtable-api.md) | Airtable read access via authed_get with a personal access token |
| [calendar-api.md](api/calendar-api.md) | Google Calendar reads via authed_get and the create and edit event action requests |
| [docs-api.md](api/docs-api.md) | Google Docs reads via authed_get and Drive, plus exporting a Doc into the workspace |
| [drive-api.md](api/drive-api.md) | Google Drive reads, download and export tools, Save to Drive, upload and folder action requests |
| [federal-register-api.md](api/federal-register-api.md) | Federal Register read access via authed_get, no credentials |
| [file-browser-api.md](api/file-browser-api.md) | Workspace file endpoints for conversation and project spaces: list, upload, download, sanitized images, copy between spaces |
| [gcp-api.md](api/gcp-api.md) | Read-only Google Cloud access (projects, Compute, GKE, Logging, IAM) and the approval-gated VM reset |
| [gmail-api.md](api/gmail-api.md) | Gmail tools (read, draft, send-to-self, labels), raw Gmail API reads, and email HTML sanitizing |
| [inference-api.md](api/inference-api.md) | One-shot inference endpoint for internal apps: per-user tokens, read-only headless runs |
| [project-db-api.md](api/project-db-api.md) | REST endpoints for browsing and deleting project database tables |
| [quest-docs-api.md](api/quest-docs-api.md) | Quest Docs UI routes: list, create, read, edit, assets, sharing, history |
| [ramp-api.md](api/ramp-api.md) | Ramp spend-management read access via authed_get over per-user OAuth |
| [projects-api.md](api/projects-api.md) | Project REST endpoints: CRUD, conversion from a conversation, files, archiving, public flag, doc sources |
| [routines-api.md](api/routines-api.md) | Routine REST endpoints: CRUD, skill auto-loads, optimistic concurrency, cost report |
| [schedules-api.md](api/schedules-api.md) | Routine schedule REST endpoints: schedule types, next due time, recent runs, optimistic concurrency |
| [sheets-api.md](api/sheets-api.md) | Google Sheets reads, spreadsheet export, and the approval-gated cell-range edit |
| [skills-api.md](api/skills-api.md) | Skill Library REST endpoints: CRUD, sharing, auto-load, search |
| [slides-api.md](api/slides-api.md) | Read-only Google Slides access via authed_get and deck export |
| [tasks-api.md](api/tasks-api.md) | Google Tasks read access via authed_get |

## Setup

| File | Summary |
|------|---------|
| [development.md](setup/development.md) | Local development setup: run.py launcher, data directory, server config, shared dev-config pre-baking |
| [development-workflows.md](setup/development-workflows.md) | Development workflow: PR-based branching, releases and tags, frontend checks, pytest layout |
| [production.md](setup/production.md) | Production deployment: first-run bootstrap wizard, serving, HTTPS options |
| [ramp-setup.md](setup/ramp-setup.md) | Creating and configuring a Ramp OAuth app for Quest |

## Plugins

Plugin-specific docs live inside each plugin's directory under `plugins/<dir>/docs/`.

| File | Summary |
|------|---------|
| [github-api.md](../plugins/github/docs/github-api.md) | GitHub plugin: reads via authed_get, job logs, workflow, comment and issue-state action requests, OAuth tokens |
| [github-app-setup.md](../plugins/github/docs/github-app-setup.md) | Creating a GitHub OAuth App or GitHub App for Quest, and its token handling |
| [m365-mail-api.md](../plugins/m365/docs/m365-mail-api.md) | Microsoft 365 plugin: Outlook mail tools over Microsoft Graph with refreshing OAuth tokens |
| [office365-setup.md](../plugins/m365/docs/office365-setup.md) | Creating the Microsoft Entra ID app registration for the Microsoft 365 plugin |
| [slack-api.md](../plugins/slack/docs/slack-api.md) | Slack plugin: read tools, self-DM, message action requests, OAuth, admin credentials |
| [slack-app-setup.md](../plugins/slack/docs/slack-app-setup.md) | Creating and configuring the Slack app for Quest |
| [twitter-api.md](../plugins/twitter/docs/twitter-api.md) | Twitter/X plugin: reads via authed_get, PKCE OAuth tokens, DM action request |
| [twitter-x-setup.md](../plugins/twitter/docs/twitter-x-setup.md) | Creating the Twitter/X OAuth 2.0 app with PKCE |
| [twilio-api.md](../plugins/twilio/docs/twilio-api.md) | Twilio plugin: phone verification, self-SMS with templates or trusted mode, approval-gated SMS to others |
| [twilio-setup.md](../plugins/twilio/docs/twilio-setup.md) | Configuring Twilio for Quest: credentials, trusted-channel switch, phone verification |
| [unifi-api.md](../plugins/unifi/docs/unifi-api.md) | UniFi plugin: read-only Network and Protect access on a local console, camera snapshots |
| [unifi-setup.md](../plugins/unifi/docs/unifi-setup.md) | Connecting Quest to a UniFi OS console: host, TLS, per-user API keys |
| [iru-api.md](../plugins/iru/docs/iru-api.md) | Iru (Kandji) plugin: read-only Apple device management tools |
| [iru-setup.md](../plugins/iru/docs/iru-setup.md) | Connecting Quest to an Iru tenant: API URL and per-user tokens |
| [telegram-api.md](../plugins/telegram/docs/telegram-api.md) | Telegram plugin: in-popup Telegram login, read tools, send-message action request |
| [telegram-setup.md](../plugins/telegram/docs/telegram-setup.md) | Configuring Telegram for Quest: MTProto app credentials and per-user login |
| [tailscale-api.md](../plugins/tailscale/docs/tailscale-api.md) | Tailscale plugin: read-only tailnet configuration via authed_get with per-user tokens |
| [tailscale-setup.md](../plugins/tailscale/docs/tailscale-setup.md) | Connecting Quest to a tailnet with a read-scoped OAuth client or an API token |
| [sec-edgar-api.md](../plugins/sec_edgar/docs/sec-edgar-api.md) | SEC EDGAR plugin: credential-free read access to filings and XBRL data |
| [google-admin-api.md](../plugins/google_admin/docs/google-admin-api.md) | Google Workspace Admin plugin: read-only directory, devices and Meet call-quality tools |
| [google-admin-setup.md](../plugins/google_admin/docs/google-admin-setup.md) | Configuring Google Workspace Admin: Admin SDK, redirect URI, admin connection |
| [imap-api.md](../plugins/imap/docs/imap-api.md) | IMAP plugin: read-only mailboxes, several accounts per user, verified TLS only |
| [imap-setup.md](../plugins/imap/docs/imap-setup.md) | Configuring IMAP: the admin server list, provider notes, adding mailboxes |

## Root

| File | Summary |
|------|---------|
| [README.md](README.md) | Entry point: what Quest is and where to start in the docs |
| [STYLE_GUIDE.md](STYLE_GUIDE.md) | How to write and update docs: pointers over copies, end-state voice, one owning doc, index rules, size budgets, checks |
