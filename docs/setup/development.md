# Development Setup

## Overview

Quest development environment setup on a local machine. The application is a FastAPI backend serving a React frontend, with Podman used for the script runner sandbox.

## Key Files

- `run.py` -- Single-command launcher: builds frontend, installs deps, runs migrations (plus local-mode seeding), starts server. Accepts `--local` (port 9000, default; `--dev` is a deprecated alias), `--staging` (port 8000), or `--prod` (port 8000). See [Run Modes](../architecture/run-modes.md)
- `config/environment.py` -- Central run-mode resolution (`local`/`staging`/`prod`) and capability flags (dev login, domain enforcement, cookie name/secure, image suffix). All mode checks go through this module
- `scripts/seed_local.py` -- Seeds a fresh local-mode database with canned accounts (`admin@quest.local`, `alice@quest.local`, `bob@quest.local`, plus the high-volume `heidi@quest.local` for testing the paged sidebar) and demo data; run automatically by `run.py --local` on a fresh throwaway data dir
- `server_credentials.json` -- Legacy consolidated server credentials (Google OAuth, Slack, GitHub, CoinGecko). Loaded by `_load_server_credentials()` in `auth/config.py`. Google OAuth, Slack, GitHub, Twitter/X, and CoinGecko are now primarily managed in the admin-only Settings > Service Credentials section (see [Service Credentials](../architecture/service-credentials.md)); this file remains a read fallback. See `server_credentials.example.json` for structure
- `config/paths.py` -- Centralized data-directory path constants; honors the `QUEST_DATA_DIR` env var (highest precedence, set by `run.py --local`) then the optional `data_dir` from `server_config.json` (see [Data Paths](../architecture/data-paths.md))
- `server_config.json` -- Optional server configuration (default model, Anthropic Vertex AI, Gemini Vertex AI, admin emails, optional `data_dir` override, optional `app_base_url` public URL for app-generated links + OAuth callbacks, optional `oauth_hostname` override for OAuth callback URLs). See `server_config.example.json` for the documented schema. Loaded via `load_server_config()` in `config/server_config.py` (and `config/paths.py` for `data_dir`)
- `pyproject.toml` -- Python dependencies managed by `uv`
- `frontend/package.json` -- Frontend dependencies managed by `npm`
- `logging_config.json` -- Unified log format config, passed to uvicorn via `--log-config`
- `alembic.ini` / `alembic/` -- Database migration configuration and scripts
- `auth/config.py` -- Credential loading, cookie configuration, OAuth scope constants
- `chat/auth.py` -- Domain restriction (`check_user_allowed()`), admin check (`is_admin()`)
- `auth/dev_login.py` -- Local-only login endpoints: `POST /auth/dev-login` (email login) and `GET /auth/dev-accounts` (canned-account roster for the sign-in picker). Active only in local mode

## Software Requirements

| Software | Minimum Version | Purpose |
|----------|----------------|---------|
| Python | 3.11+ | Backend runtime |
| Node.js | 18+ | Frontend build tools |
| npm | 9+ | Frontend package manager |
| Podman | 4+ | Script runner containerization (rootless) |
| uv | Latest | Python package manager |

## Data Directory Layout

```
data/
├── quest.db               # SQLite database (users, API keys, OAuth tokens, settings, memories, guides, projects, routines, schedules, conversation metadata)
├── secret_key              # Cookie signing key (auto-generated)
├── encryption_key.json     # Password-wrapped data-encryption key for stored secrets (see architecture/encryption-at-rest.md)
├── service_credentials/    # Admin-managed per-service credential files (encrypted)
├── inference_credentials/  # Admin-managed per-provider LLM API keys (encrypted)
├── projects/
│   └── {project_id}/
│       └── workspace/
│           └── workspace/  # Project workspace shared by the project's conversations (doubled segment is historical)
└── chats/
    └── {conversation_id}/  # Per-conversation directory (flat layout, one UUID per folder)
        ├── chat_history.json
        ├── sdk_history.json
        └── workspace/      # Conversation workspace (every conversation, project ones included)
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `QUEST_ENV` | `prod` | Run mode: `local`, `staging`, or `prod` (legacy `dev` aliases to `local`). Controls session cookie name, UI branding, canned-account login, domain restriction, and container image suffixes -- all resolved through `config/environment.py`. Set automatically by `run.py`. See [Run Modes](../architecture/run-modes.md) |
| `QUEST_DATA_DIR` | unset | Overrides the data directory (highest precedence, above `server_config.json`'s `data_dir`). Set automatically by `run.py --local` to the per-run throwaway directory |
| `QUEST_ENCRYPTION_PASSWORD` | unset (local mode: fixed dev sentinel) | Password that unlocks `<data_dir>/encryption_key.json`, the key encrypting stored secrets. Staging/prod: required (or `QUEST_ENCRYPTION_PASSWORD_FILE`, or a TTY prompt from `run.py`). Local mode falls back to `LOCAL_DEV_PASSWORD` in `config/encryption.py`. See [Encryption at Rest](../architecture/encryption-at-rest.md) |
| `QUEST_ENCRYPTION_PASSWORD_FILE` | unset | Path to a file holding the encryption password (systemd `LoadCredential` style); consulted after the env var |
| `QUEST_ENCRYPTION_KEY_FILE` | `<data_dir>/encryption_key.json` | Overrides the key file location (used by the test suite) |
| `GEMINI_VERTEX_PROJECT_ID` | From `server_config.json` (or `anthropic.vertex_project_id` fallback) | Google Cloud project id for Gemini models (all served via Vertex AI) |
| `GEMINI_VERTEX_REGION` | `global` | Vertex location for Gemini. Gemini 3.x requires the `global` endpoint -- regional endpoints return 404 |
| `PORT` | 8000 | Backend server port |

Configuration priority: environment variables > `server_config.json` > built-in defaults.

## Server Configuration

Model settings are in `server_config.json`, loaded by `load_server_config()` in `config/server_config.py`.

- `model`: Default model name. Valid values: `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`, `gemini-3.5-flash` (all four deprecated -- still runnable but hidden from the model picker; prefer a current model), `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `claude-haiku-4.5`, `claude-haiku-5-5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-opus-5-5`

### Anthropic Vertex AI

To enable Claude models, add an `anthropic` section to `server_config.json` with `vertex_project_id` (required) and optionally `vertex_region` (default region: `us-east5`).

The `vertex_region` value is the default; individual models can override it via a per-model `vertex_region` in `MODEL_REGISTRY` (Claude Opus 4.7, Opus 4.8, and Sonnet 5 are pinned to the `global` endpoint because Vertex serves them only there -- see [LLM Provider Abstraction -- Per-Model Vertex Region](../architecture/llm-providers.md#per-model-vertex-region)).

Uses Google Cloud Application Default Credentials (ADC). If `vertex_project_id` is not configured and a user selects a Claude model, the backend raises a descriptive error. Gemini models work regardless of Anthropic configuration.

### Gemini Vertex AI

All Gemini models are served via Vertex AI and use Google Cloud Application Default Credentials (ADC) -- there is no Gemini API key. Add a `gemini_vertex` section to `server_config.json` with `vertex_project_id` and optionally `vertex_region` (default: `global` -- Gemini 3.x is only served on the global endpoint). If `vertex_project_id` is omitted it falls back to `anthropic.vertex_project_id`; the region is **not** inherited (Anthropic's `us-east5` is not a Gemini location). Env overrides: `GEMINI_VERTEX_PROJECT_ID`, `GEMINI_VERTEX_REGION`. See [LLM Provider Abstraction -- Gemini Vertex AI Configuration](../architecture/llm-providers.md#gemini-vertex-ai-configuration) for details.

### Shared Dev Config (`dev-config.json`) in Local Mode

In local mode, `run.py` looks for a `dev-config.json` file in the checkout's **parent** directory (so one shared file serves every quest checkout on the box) and pre-bakes its contents into the run's data directory before starting the server. The file lives outside the repository and must never be committed. Recognized keys:

- `vertex_service_account` -- the full service-account key JSON object. Both Vertex integrations authenticate via Google Cloud Application Default Credentials (ADC): the key is written to `<data_dir>/vertex-service-account.json` (mode 0600) and `GOOGLE_APPLICATION_CREDENTIALS` is set to point at it. An externally-set `GOOGLE_APPLICATION_CREDENTIALS` takes precedence, and the key is optional -- without it, ADC falls back to its usual sources (e.g. `gcloud auth application-default login` or the GCE metadata server). A bare key file named `vertex-service-account.json` in the parent directory (the pre-`dev-config.json` mechanism) is still honored as a deprecated fallback.
- `service_credentials` -- a mapping of service name to credentials object (e.g. `google_oauth`, in the same shape as its `server_credentials.json` section), copied verbatim into the per-service credential store at `<data_dir>/service_credentials/<service>.json` unless that file already exists (so credentials saved through the admin UI in a `--keep-data` directory are never clobbered). Pre-baked services show up ready-configured in the admin Settings > Service Credentials section. See [Service Credentials](../architecture/service-credentials.md).
- `inference_credentials` -- a mapping of inference provider name to credentials object (e.g. `openrouter`), copied verbatim into the inference-credential store at `<data_dir>/inference_credentials/<provider>.json` unless that file already exists. Pre-baked providers show up ready-configured in the admin Settings > Inference Providers section, and their models (e.g. DeepSeek V4 Flash via OpenRouter) appear in the model picker. See [Inference Providers](../architecture/inference-providers.md).
- `oauth_hostname` -- exported as `QUEST_OAUTH_HOSTNAME` (which `load_server_config()` honors over `server_config.json`), so OAuth callback URLs are built with this hostname instead of the raw request host -- Google rejects private-IP redirect URIs. An externally-set `QUEST_OAUTH_HOSTNAME` wins. See [Auth Architecture -- OAuth Callback Hostname Override](../architecture/auth.md#oauth-callback-hostname-override); note the port is taken from the incoming request, so the provider-registered redirect URIs must match each dev instance's port.
- `allowed_login_domain` -- exported as `QUEST_ALLOWED_LOGIN_DOMAIN`; the Google login callback checks the signed-in email against this domain instead of the hardcoded company domain (`ALLOWED_DOMAIN`), so developers can complete a real Google sign-in with a test account. The domain check itself always runs, and `allowed_login_domain()` in `auth/config.py` ignores the override outside local mode, so it can never widen staging/prod access.

Example:

```json
{
  "vertex_service_account": {"type": "service_account", "project_id": "...", "private_key": "..."},
  "service_credentials": {
    "google_oauth": {"web": {"client_id": "...", "client_secret": "...", "redirect_uris": ["..."]}}
  },
  "inference_credentials": {
    "openrouter": {"api_key": "sk-or-v1-..."}
  },
  "oauth_hostname": "dev.example.com",
  "allowed_login_domain": "example.dev"
}
```

At boot the server cross-checks the key file against the configured Vertex project ids (`check_vertex_credentials_consistency()` in `config/server_config.py`): a key with no `vertex_project_id` configured anywhere logs a warning (Vertex models silently hidden from the picker despite working credentials), and a configured project id that differs from the key's `project_id` logs an error (Vertex calls will likely fail at send time). Diagnostics only -- the server still boots.

## Credential Configuration

All server credentials live in `server_credentials.json` (see `server_credentials.example.json` for structure). At minimum:

- **Google OAuth** (`google_oauth.web`): Client ID and secret from Google Cloud Console. Redirect URIs: `http://localhost:8000/auth/callback` (app login) and `http://localhost:8000/auth/google-services/callback` (Google Services OAuth). Can also be set from the admin-only Settings > Service Credentials section instead of the file; at startup the section is migrated into the per-service store under the data directory, which takes precedence. See [Service Credentials](../architecture/service-credentials.md)

When the dev server is reached by raw IP (e.g. a remote dev box), Google rejects IP-based redirect URIs. Set the top-level `oauth_hostname` key in `server_config.json` (e.g. `dev.example.com`), add an `/etc/hosts` entry pointing that hostname at the server's IP, browse the app through the hostname, and register the hostname-based callback URLs (e.g. `http://dev.example.com:9000/auth/callback`) with the provider. See [Auth Architecture -- OAuth Callback Hostname Override](../architecture/auth.md#oauth-callback-hostname-override).
Optional sections (configured separately; Slack, GitHub, and Twitter/X can also be set from Settings > Service Credentials, which takes precedence):
- `slack` -- See [Slack App Setup](../../plugins/slack/docs/slack-app-setup.md)
- `github` -- See [GitHub App Setup](../../plugins/github/docs/github-app-setup.md)
- `coingecko.api_key` -- CoinGecko Pro API key (legacy location; prefer the Settings > Service Credentials card, which writes `data/service_credentials/coingecko.json`). Loaded by `load_coingecko_api_key()` in `auth/config.py` store-first, injected as `x-cg-pro-api-key` header on requests to `pro-api.coingecko.com`

`server_credentials.json` is in `.gitignore` and must never be committed.

## Authentication Architecture

Two-tier Google OAuth: app login uses minimal scopes (`openid`, `email`, `profile`), Google Services uses broader scopes for Gmail, Calendar, Drive, Docs, Sheets, Tasks. These are separate OAuth flows -- reconnecting services does not affect the login session. See [Auth Architecture](../architecture/auth.md).

In local mode, the sign-in screen shows a canned-account picker (one-click login as the seeded accounts) plus a free-text email login for testing multi-user features without any Google account. Domain restriction is disabled in local mode. See `check_user_allowed()` in `chat/auth.py` and `auth/dev_login.py`.

Session cookie name is mode-dependent: `quest_session_local` (local), `quest_session_staging` (staging), or `quest_session` (prod). See `config/environment.py`.

## Running the Application

The standard way to run is `python3 run.py` (local mode, port 9000, fresh throwaway seeded DB per run) or `python3 run.py --prod` (port 8000). This handles frontend builds, dependency installation, migrations, local seeding, script-runner image validation, and server startup. Use `--keep-data` to reuse the previous local run's data directory instead of starting fresh. Local mode boots with zero credential files -- missing services show as disconnected and chat is disabled until an LLM credential is configured. See [Run Modes](../architecture/run-modes.md).

For manual backend startup (e.g., debugging): `QUEST_ENV=local uv run uvicorn quest:app --reload --host 0.0.0.0 --port 8000 --log-config logging_config.json`. When starting manually, the frontend must be built first (`cd frontend && npm install && npm run build`) and migrations are not run automatically.

Access points:
- App: `http://localhost:9000/` (local) or `http://localhost:8000/` (staging/prod)
- API: `http://localhost:9000/app/api/` (local) or `http://localhost:8000/app/api/`
- Interactive API docs: `/api-docs` (Swagger UI) and `/api-redoc` (ReDoc), set in the `FastAPI(...)` constructor in `quest.py`; `/docs` is the frontend's Quest Docs route, not Swagger

## Static File Serving

The backend serves all frontend files. Route priority (see `quest.py`):
1. `/assets/*` -- FastAPI StaticFiles mount to `frontend/dist/assets/`
2. `/app/api/*` -- REST and WebSocket endpoints
3. `/` -- Serves `frontend/dist/index.html`
4. `/{filename}` -- Root-level static files from `frontend/dist/`
5. `/chat*` -- 301 redirect to `/*` (backward compatibility)

Vite config uses `base: '/'` in `frontend/vite.config.ts`.

## Design Decisions

**Route Handlers**:
1. `/assets/*` → FastAPI StaticFiles mount to `frontend/dist/assets/`
2. `/` → Serves `frontend/dist/index.html` (requires auth)
3. `/chats/*`, `/projects/*`, `/admin/*` → SPA catch-all routes serving `index.html` (deep link support for URL-based routing)
4. `/{filename}` → Serves root-level static files from `frontend/dist/`
5. `/chat*` → 301 redirect to `/*` (backward compatibility)

**Why uv for Python package management?**
Fast dependency resolution and virtual environment management. Creates `.venv/` automatically.

**Why a single run.py launcher?**
Eliminates manual multi-step setup. Handles frontend build, backend deps, migrations, script-runner image validation, and server startup in one command.

**Why separate Google OAuth flows for login vs services?**
App login needs only identity (fast sign-in). Google Services need broader API access that can be re-authorized independently without affecting the login session.

**Why environment-dependent cookie names?**
Prevents session collision when running dev and prod instances on the same hostname.
