# Production Deployment

## Overview

Quest production deployment architecture: a single FastAPI server on port 8000 serving both API endpoints and static frontend files, with cookie-based session authentication. No separate frontend server -- Vite builds static assets that FastAPI serves.

## Key Files

- `run.py` -- Single-command launcher (`python3 run.py --prod`). Prompts for the encryption password, builds frontend, installs deps, runs migrations, validates/rebuilds the script-runner Podman image, starts server on port 8000
- `auth/first_run.py` -- First-run setup routes and checks (see First-Run Setup below)
- `quest.py` -- FastAPI application entry point. Static file mounts, route registration, catch-all handlers
- `server_credentials.json` -- Legacy consolidated production credentials (Google OAuth with production redirect URIs, optional Slack/GitHub/CoinGecko). Loaded by `_load_server_credentials()` in `auth/config.py` as a read fallback; every section is migrated into the per-service store at startup and managed from Settings > Service Credentials afterwards
- `config/paths.py` -- Centralized data-directory path constants; reads optional `data_dir` from `server_config.json` (see [Data Paths](../architecture/data-paths.md))
- `server_config.json` -- Server configuration including `admin_emails`, default model, Anthropic Vertex AI settings, Gemini Vertex AI settings, and optional `data_dir` override
- `logging_config.json` -- Unified log format config passed to uvicorn via `--log-config`
- `auth/config.py` -- Cookie name logic (`quest_session` when `QUEST_ENV=prod`), credential loading
- `chat/auth.py` -- Domain restriction (`check_user_allowed()`), admin check (`is_admin()`)

## Environment Constraints

- Python 3.11+, Node.js 18+, Podman 4+ (script runner), uv
- Single port: 8000 (all traffic), plain HTTP -- put an HTTPS front in front of it; most OAuth providers reject non-HTTPS callback URLs (see Serving over HTTPS)
- `QUEST_ENV=prod` (set automatically by `run.py --prod`): uses `quest_session` cookie, "Quest" branding, domain restriction enforced, dev login disabled
- `data/` directory must exist and be writable by the server process. The data directory can be relocated by setting `data_dir` in `server_config.json` (see [Data Paths](../architecture/data-paths.md))
- `data/secret_key` -- auto-generated cookie signing key. Restrict permissions (`chmod 600`). Losing this invalidates all sessions
- `data/quest.db` -- SQLite database. Restrict permissions (`chmod 600`). Contains user API keys and OAuth tokens

## First-Run Setup

`python run.py --prod` on an unconfigured deployment starts the server as usual and
serves a setup screen in place of the sign-in screen until the deployment is
configured. The only command-line input is the credential-encryption password (see
below). "Unconfigured" means missing either of the two hard prerequisites a usable
instance cannot run without: `admin_emails` in `server_config.json` (without it
nobody can reach the admin UI, including Settings > Service Credentials) or a way to
sign in, which depends on the `login_method` key (see
[Sign-in Methods](../architecture/auth.md#sign-in-methods)): for `"password"` a
password for at least one admin account; for `"google"` -- also what an absent key
means -- Google OAuth client credentials. See `missing_required_config()` in
`auth/first_run.py`. `GET /app/api/config` reports the result as `setup_required`
and `frontend/src/App.tsx` renders `SetupScreen`
(`frontend/src/components/SetupScreen.tsx`) while it is true; the lifespan logs a
notice. Local mode never needs setup.

**Unlock.** The screen is unauthenticated (there are no accounts yet), so it is
unlocked with the encryption password the operator entered at startup
(`POST /app/api/setup/unlock`, checked against the key file by `verify_password()`
in `config/encryption.py`, rate-limited per client and in total). A correct
password returns a one-hour signed setup token plus the form defaults: existing
values on a partially configured deployment, and the request host as the public-URL
suggestion. Every setup route 404s once the deployment is configured.

**Questions.** The sign-in method (default `password`: no setup outside Quest; the
default is `google` on a deployment that already has Google OAuth credentials but no
`login_method`, an install that predates password sign-in); the operator's own
email, which becomes the first admin, plus other admins; the allowed login domain
(public mailbox domains are refused, see `PUBLIC_EMAIL_DOMAINS`) and additional
individual addresses, with admins outside the domain appended to
`allowed_login_emails` automatically; the public URL (`app_base_url`, used for
OAuth callback URLs and app-generated absolute links; a bare hostname gets
`https://`, a legacy `oauth_hostname` seeds the suggestion); then either the first
admin's password (checked against `password_problem()`) or the Google OAuth client
id/secret with both redirect URIs shown; and, optionally, a GCP service-account key
(pasted or picked as a file) with the Vertex project id. Skipping the key leaves the
composer's model setup card to connect an API-key provider after sign-in.

**Writes** (`POST /app/api/setup/complete`; everything is validated before anything
is written, and a problem names its field so the screen returns to that step):
`server_config.json` through `update_server_config()` (unrelated keys preserved),
the `google_oauth` per-service credential store file (encrypted), the key copy at
`<data_dir>/vertex-service-account.json` that `run.py` exports as
`GOOGLE_APPLICATION_CREDENTIALS` in staging/prod (`setup_server_vertex_credentials()`;
the completing request also exports it into the running process), and under password
sign-in the first admin's account and scrypt hash in the database, signing that
admin in with the response. Under Google sign-in the reload lands on the Google
sign-in screen.

**The encryption password** is resolved by `run.py` before any build step
(`resolve_encryption_password()`): `QUEST_ENCRYPTION_PASSWORD`, then
`QUEST_ENCRYPTION_PASSWORD_FILE`, then a hidden TTY prompt (asked twice on the first
run, when `<data_dir>/encryption_key.json` does not exist yet). Step 3 of startup
runs `uv run python -m config.encryption init` to create or verify the key file. A
non-interactive start with no password aborts. For systemd, put the password in a
root-only file and reference it via `QUEST_ENCRYPTION_PASSWORD_FILE` (or
`LoadCredential`), never in the unit file itself; an unconfigured deployment started
that way serves the setup screen like any other. See
[Encryption at Rest](../architecture/encryption-at-rest.md).

Admins and the public URL are `server_config.json` edits after setup. Connectors not
covered by setup (Slack, GitHub, Twitter/X, Ramp, CoinGecko) are configured after
first login by an admin in Settings > Service Credentials. On a password deployment
that includes outgoing email (SMTP, for self-service password resets and sign-up)
and, whenever wanted, the Google OAuth client (Google services connectors, and the
later switch to Google sign-in from Settings > Sign-in).

## Route Priority

Routes are registered in this order in `quest.py` (highest priority first):

1. `/assets/*` -- StaticFiles mount to `frontend/dist/assets/` (no auth)
2. `/app/api/*` -- REST and WebSocket endpoints (dual auth: cookie or API key)
3. `/` -- Serves `frontend/dist/index.html` (React handles auth state client-side)
4. `/{filename}` -- Root-level static files from `frontend/dist/` (no auth)
5. `/chat*` -- 301 redirect to `/*` (backward compatibility)

## Authentication Flow

HTML routes (`/`): serve `index.html`, React calls `GET /app/api/me` to check session, shows inline `SignInScreen` if not authenticated.

API routes (`/app/api/*`): check session cookie first, then `Authorization: Bearer <key>` header. Cookie name is `quest_session` in production (see `COOKIE_NAME` in `auth/config.py`). Access restriction: `is_login_allowed()` in `auth/config.py` -- the domain from `allowed_login_domain()` (`allowed_login_domain` in `server_config.json` when set, else the built-in `ALLOWED_DOMAIN` default) OR membership in the optional `allowed_login_emails` whitelist (enforced in `check_user_allowed()` in `chat/auth.py`).

Static assets (`/assets/*`, `/vite.svg`): no authentication.

## Server Configuration

`server_config.json` options:
- `admin_emails`: email addresses for admin users (case-insensitive check in `is_admin()` in `chat/auth.py`). Admins see AdminOpsMenu in frontend
- `allowed_login_domain`: email domain allowed to sign in (default: the built-in company domain -- see `allowed_login_domain()` in `auth/config.py`). Set by first-run setup so deployments for other organizations can admit their own domain
- `allowed_login_emails`: optional list of individual email addresses allowed to sign in in addition to the domain (see `is_login_allowed()` in `auth/config.py`), for accounts outside any single domain -- e.g. personal `@gmail.com` users on a family deployment. Also asked by first-run setup, and appended to by admin invite links on password deployments
- `login_method`: `"password"` (email + password accounts) or `"google"` (Google OAuth; also what an absent key means). Exactly one method is active. Written by first-run setup and by the admin switch to Google sign-in; see [Sign-in Methods](../architecture/auth.md#sign-in-methods)
- `model`, `anthropic.*`, `gemini_vertex.*`: same as development. See [Development Setup](development.md) for details

Production-specific: use a service account with Vertex AI API access for ADC (rather than `gcloud auth application-default login`). The same ADC credentials serve both Anthropic Vertex AI and Vertex-backed Gemini models. A key file at `<data_dir>/vertex-service-account.json` (written by first-run setup) is exported as `GOOGLE_APPLICATION_CREDENTIALS` automatically by `run.py` in staging/prod; an externally-set env var wins.

## Credential Configuration

`server_credentials.json` -- same structure as development but with production redirect URIs (e.g., `https://chat.yourdomain.com/auth/callback` and `https://chat.yourdomain.com/auth/google-services/callback`). See `server_credentials.example.json` for structure.

Twitter/X credentials are managed from Settings > Service Credentials (legacy fallback: a separate `twitter_credentials.json` file). See [Twitter/X Setup](../../plugins/twitter/docs/twitter-x-setup.md).

Configure the following sections in `server_credentials.json`:

- **`google_oauth.web`**: Google OAuth credentials with production redirect URIs. Update `redirect_uris` to match your production domain (e.g., `https://chat.yourdomain.com/auth/callback` and `https://chat.yourdomain.com/auth/google-services/callback`). Both URIs are required: one for app login and one for the separate Google Services OAuth flow.
- **`slack`** (optional): Slack client credentials and bot token -- see [Slack App Setup Guide](../../plugins/slack/docs/slack-app-setup.md).
- **`github`** (optional): GitHub OAuth App credentials -- see [GitHub App Setup Guide](../../plugins/github/docs/github-app-setup.md).
- **`coingecko`** (optional): CoinGecko Pro API key for authenticated cryptocurrency data requests via the `authed_get` tool. Get a key from [CoinGecko](https://www.coingecko.com/en/api/pricing). Preferably entered in Settings > Service Credentials (admin) instead of this file; a legacy `coingecko` section is migrated into the store on first startup.

The file is loaded by `_load_server_credentials()` in `auth/config.py` with caching. See `server_credentials.example.json` for the full structure.

### Step 4: Configure Server Settings (Optional)

Create `server_config.json` to configure admin users, the default model, and optional Vertex AI settings:

```bash
cat > server_config.json << 'EOF'
{
  "admin_emails": ["admin@yourdomain.com"],
  "gemini": {
    "model": "gemini-3.1-pro-preview"
  },
  "anthropic": {
    "vertex_project_id": "your-gcp-project-id",
    "vertex_region": "us-east5"
  },
  "gemini_vertex": {
    "vertex_project_id": "your-gcp-project-id",
    "vertex_region": "global"
  }
}
EOF
```

**Configuration Options**:
- `admin_emails`: Array of email addresses for admin users (default: empty list). Admin users see the AdminOpsMenu in the frontend and can trigger server operations like graceful shutdown. The check is case-insensitive (see `is_admin()` in `chat/auth.py`).
- `model`: Default model. Valid values are the keys of `MODEL_REGISTRY` in `chat/llm/config.py`; the schema default in `config/server_config.py` is a deprecated model (still runnable but hidden from the picker), so set a current one.
- `anthropic.vertex_project_id`: Google Cloud project ID for Anthropic Claude on Vertex AI (required only if Claude models are used).
- `anthropic.vertex_region`: Default Google Cloud region for the Vertex AI endpoint (default: `us-east5`). Individual models can override this via a per-model `vertex_region` in `MODEL_REGISTRY`; the newer Claude models are pinned to `global` because Vertex serves them only on the global endpoint (a regional request 429s). See [LLM Provider Abstraction -- Per-Model Vertex Region](../architecture/llm-providers.md#per-model-vertex-region).
- `gemini_vertex.vertex_project_id`: Google Cloud project ID for the Vertex-backed Gemini models (every `provider: "gemini"` registry entry). Falls back to `anthropic.vertex_project_id` if omitted.
- `gemini_vertex.vertex_region`: Vertex location for Gemini (default: `global`). Gemini 3.x requires the global endpoint; regional endpoints return 404. Env overrides: `GEMINI_VERTEX_PROJECT_ID`, `GEMINI_VERTEX_REGION`.

**Vertex AI (Anthropic and Gemini)**: Both Vertex providers use Google Cloud Application Default Credentials (ADC). In production, use a service account with Vertex AI API access. If `anthropic.vertex_project_id` is not configured and a user selects a Claude model, or `gemini_vertex.vertex_project_id` (with `anthropic` fallback) is not configured and a user selects a Gemini model, the backend raises a descriptive error.

### Step 5: Create Data Directory and Run Migrations

```bash
mkdir -p data/chats
```

Create or unlock the credential-encryption key, then run database migrations to create or update the SQLite database:

```bash
export QUEST_ENCRYPTION_PASSWORD=...   # or QUEST_ENCRYPTION_PASSWORD_FILE=/path
uv run python -m config.encryption init
uv run alembic upgrade head
```

This creates `data/quest.db` with the `users` table. Both steps must run before starting the server (`run.py` does them automatically); the migrations need the unlocked key because migration `c4e8a2d17f63` encrypts stored secrets -- on an existing database it first writes a `quest.db.pre-encryption-<timestamp>.bak` copy beside the database.

### Step 6: Script Runner Image

`run.py` automatically builds the `quest-script-runner-<mode>` Podman image from `Dockerfile.script-runner` if it is missing or stale (see [Script Runner](../architecture/script-runner.md)).

### Step 7: Build Frontend

```bash
cd frontend
npm ci
npm run build
cd ..
```

Use `npm ci` rather than `npm install` in production: it installs strictly from `package-lock.json` and refuses to mutate it, so resyncs don't leave a dirty lockfile that has to be reverted before the next pull. If it errors with a sync mismatch, regenerate the lockfile on a dev machine (`npm install` in `frontend/`), commit it, and resync. This creates production-optimized bundles in `frontend/dist/`.

### Step 8: Install Backend Dependencies

```bash
uv sync
```

### Step 9: Start Server

```bash
QUEST_ENV=prod uv run uvicorn quest:app --host 0.0.0.0 --port 8000 --log-config logging_config.json
```

**Do NOT use `--reload` in production.** The `QUEST_ENV=prod` prefix ensures the session cookie is named `quest_session` (the production default; omitting it has the same effect since `prod` is the default). The `--log-config` flag applies the unified logging format with colored level labels, timestamps, process IDs, and module names. See [Logging Architecture](../architecture/logging.md).

## Production Architecture

### Static File Serving

The backend serves all frontend files using FastAPI's StaticFiles:

```
Client Request                 FastAPI Handler
─────────────────              ───────────────
GET /                       → serve index.html (with auth check)
GET /chats/<uuid>           → serve index.html (SPA catch-all for deep links)
GET /projects/<pid>/<uuid>  → serve index.html (SPA catch-all for deep links)
GET /admin/system-monitor   → serve index.html (SPA catch-all for deep links)
GET /assets/index.js        → StaticFiles mount (no auth)
GET /vite.svg               → serve static file (no auth)
GET /chat/*                 → 301 redirect to /* (backward compatibility)
GET /app/api/conversations → REST API endpoint (dual auth: cookie or API key)
WS  /app/api/.../message   → WebSocket endpoint (dual auth: cookie or API key)
```

### Route Priority

Routes are registered in this order (highest priority first):

1. **Assets mount**: `/assets/*` → `frontend/dist/assets/`
2. **API routes**: `/app/api/*` → REST and WebSocket endpoints
3. **HTML entry**: `/` → `frontend/dist/index.html`
4. **SPA catch-all**: the `serve_spa_*` handlers in `quest.py`, one per client-side URL prefix → `frontend/dist/index.html` (enables deep links for URL-based routing; admin API routes are unaffected since they live under `/app/api/admin/...`)
5. **Static files**: `/{filename}` → `frontend/dist/{filename}`
6. **Backward compatibility**: `/chat*` → 301 redirect to `/*`

This ordering is critical to prevent route conflicts.

### Authentication Flow

**For HTML Routes** (`/`):
1. Serve `index.html` (the React frontend handles auth state)
2. Frontend calls `GET /app/api/me` to check session
3. If session valid → render chat interface
4. If session invalid → render inline `SignInScreen` component

**For API Routes** (`/app/api/*`):
1. Check for session cookie first (browser frontend; cookie name is `quest_session` in production, see `COOKIE_NAME` in `auth/config.py`)
2. If no cookie, check for API key in `Authorization: Bearer <key>` header (scripts/LLM)
3. If neither valid → return 401 Unauthorized
4. If valid → verify user email is allowed
5. If allowed → process request

**For Static Assets** (`/assets/*`, `/vite.svg`):
- No authentication required
- Public access for all static files

## Process Management

The server process should be managed by systemd or supervisor. The relevant configuration lives in:
- systemd: create a service file at `/etc/systemd/system/quest.service`
- supervisor: create a config file at `/etc/supervisor/conf.d/quest.conf`

Both need `QUEST_ENV=prod` in the environment and should run `uv run uvicorn quest:app --host 0.0.0.0 --port 8000 --log-config logging_config.json` (no `--reload` in production).

## Serving over HTTPS

Most OAuth providers refuse to register a plain `http://` redirect URI for anything other than
`localhost`: Google, Slack, Microsoft 365 and Twitter/X all require `https://` callback URLs
(GitHub is the notable exception). Since every browser-redirect OAuth flow (app login, Google
Services, Ramp and each oauth-kind plugin) builds its callback from the same base URL, serve Quest
over HTTPS in production -- it is also what protects the session cookie and lets the frontend use
`wss://` for the persistent WebSocket.

Two things to get right whichever route you pick:

- **Set `app_base_url` to the HTTPS URL** (`server_config.json`, env `QUEST_APP_BASE_URL`; first-run
  setup asks for it). `oauth_base_url()` in `auth/config.py` uses it verbatim. Without
  it, callbacks are built from the request's own scheme and host, which behind a TLS-terminating
  front is plain `http://` on `localhost:8000` -- the provider then rejects the mismatch. An
  `https://` `app_base_url` also gives the session cookie the `Secure` attribute (applied at
  startup; see [Run Modes](../architecture/run-modes.md)).
- **Register the callbacks under that URL** with each provider: `<app_base_url>/auth/callback` and
  `<app_base_url>/auth/google-services/callback` for Google (both required), plus
  `<app_base_url>/auth/<service>/callback` for each connector you configure (see the per-service
  setup guides). Callbacks are browser redirects, so the HTTPS URL only has to be reachable from
  your users' browsers, never from the provider's servers -- a private Tailscale hostname works.

The easiest ways to get HTTPS are Tailscale Serve (private to your tailnet) and a Cloudflare Tunnel
behind Zero Trust Access (public hostname, no open inbound ports). Neither needs a public IP, a DNS
record you manage, or certificate renewal. A classic reverse proxy is the third option. In every case
uvicorn keeps listening on plain HTTP port 8000 and the front terminates TLS; firewall 8000 so only
the front (running on the same host) can reach it.

### Tailscale Serve

Best when everyone who uses the instance can join your tailnet. The URL is only resolvable and
reachable from tailnet devices, which doubles as the access control.

1. Install Tailscale on the server and on each user's device, and in the Tailscale admin console
   enable **MagicDNS** and **HTTPS Certificates** (DNS page). This gives the machine a stable
   `<machine>.<tailnet>.ts.net` name and lets it fetch a Let's Encrypt certificate for it.
2. On the server run `tailscale serve --bg 8000`. Tailscale terminates TLS on port 443 of the
   machine's tailnet address, provisions the certificate on first request, proxies to
   `localhost:8000` and passes WebSockets through. The setting persists across reboots;
   `tailscale serve status` shows the URL and `tailscale serve reset` removes it.
3. Set `app_base_url` to `https://<machine>.<tailnet>.ts.net` (no port) and register the callback
   URLs under it with the OAuth providers. A localhost callback can stay registered alongside it for
   development.

`tailscale funnel` would expose the same endpoint to the public internet; it is not needed for
OAuth, since the callback is a browser redirect.

### Cloudflare Tunnel with Zero Trust Access

Best when users are outside your network or you want a hostname on your own domain. Requires a
domain whose DNS is on Cloudflare (the free plan is enough). `cloudflared` on the server opens an
outbound-only connection to Cloudflare, so no inbound port is opened and Cloudflare serves the
certificate.

1. In the Cloudflare Zero Trust dashboard go to **Networks > Tunnels**, create a tunnel with the
   *Cloudflared* connector and run the install command it shows on the Quest server
   (`cloudflared service install <token>` -- it registers a systemd service that starts on boot).
2. Under the tunnel's **Public Hostname** tab add the hostname (e.g. `quest.yourdomain.com`) with
   service type `HTTP` and URL `localhost:8000`. Cloudflare creates the DNS record; WebSockets pass
   through unchanged.
3. Under **Access > Applications** add a *Self-hosted* application for that hostname with an Allow
   policy such as "Emails ending in `@yourdomain.com`" (or a specific list). Without this the
   hostname is reachable by anyone on the internet -- Quest still enforces its own Google login, but
   the Access layer keeps unauthenticated traffic off the server entirely. Users get a one-time
   Cloudflare login (Google or email code) before Quest's sign-in screen.
4. Set `app_base_url` to `https://quest.yourdomain.com` and register the callback URLs under it.

Caveat: the Cloudflare proxy caps a single request body at 100 MB on free/pro plans, below the
default per-file upload limit; lower it under Settings > Limits to match.

### Reverse proxy (Nginx or Caddy)

For a server with a public IP and your own certificates (Caddy obtains them automatically). Key
requirements:
- WebSocket support: must proxy `Upgrade` and `Connection` headers (Caddy handles this automatically)
- Read timeout: at least 5 minutes (`proxy_read_timeout 300s` in Nginx) for long-running model responses
- Client body size: increase from default for file uploads to above the upload limit in Settings > Limits (`client_max_body_size 250M` in Nginx for the default)
- HTTPS only: redirect port 80 to 443; set `app_base_url` to the `https://` URL as above

## Logging

All log output uses a unified format configured in `chat/logging_config.py` (dict config) and `logging_config.json` (CLI equivalent). User-driven log entries include `user=<email>` for per-user filtering. See [Logging Architecture](../architecture/logging.md).

## Security Constraints

- Access restriction: only emails on the built-in default domain (`ALLOWED_DOMAIN` in `auth/config.py`) allowed by default. Override with `allowed_login_domain` and/or the `allowed_login_emails` whitelist in `server_config.json` (see `is_login_allowed()` in `auth/config.py`); some restriction is always enforced
- `data/secret_key`: cookie signing key. Restrict to `chmod 600`, back up (losing it invalidates all sessions)
- `data/quest.db`: contains API keys and OAuth tokens, encrypted at rest with the key in `data/encryption_key.json` (itself wrapped by the operator password). Restrict both to `chmod 600`. The encryption protects a copied file or backup, not a compromised running host -- see [Encryption at Rest](../architecture/encryption-at-rest.md)
- The encryption password: keep it out of the unit file and shell history (`QUEST_ENCRYPTION_PASSWORD_FILE` pointing at a root-only file); losing it makes every stored credential unrecoverable. Rotate with `uv run python -m config.encryption rotate-password`
- `data/quest.db.pre-encryption-*.bak`: the plaintext copy written by the encryption migration -- delete it once the upgrade is verified
- `server_credentials.json`: never commit to version control (in `.gitignore`)
- Firewall: expose only the HTTPS front (Tailscale Serve, `cloudflared`, or ports 80/443 on a reverse proxy); block direct access to 8000 from anything but that front on the same host (see Serving over HTTPS)
- Script-runner Podman containers run ephemerally with `--rm` and workspace volume mounts (see [Script Runner](../architecture/script-runner.md))

## Backup

What to back up:
- `data/` -- SQLite database, secret key, `encryption_key.json` (useless without the password, but the database is useless without it), chat histories, conversation and project workspaces
- The encryption password itself, in your secret manager -- it is not in `data/`
- `server_credentials.json` -- production credentials
- `twitter_credentials.json` -- Twitter/X credentials (if configured)

## Scaling Considerations

- Multiple uvicorn workers (`--workers N`, rule of thumb: `2 * CPU cores + 1`) can improve throughput but complicate WebSocket connection management
- Database scaling: migrate SQLite to PostgreSQL by changing `DATABASE_URL` in `db/engine.py` and `alembic.ini`
- Connection pooling: Slack proxy already uses a shared `httpx.AsyncClient` with pooling in `plugins/slack/upstream.py`

## Design Decisions

**Why single-server architecture?**
FastAPI serves both API and static files. No separate frontend server needed. Simplifies deployment to a single process.

**Why cookie-based auth instead of API key only?**
Browser frontend uses session cookies (`credentials: 'include'`). API key auth is the fallback for scripts and LLM access. Both paths are supported on all `/app/api/*` endpoints.

**Why no Docker Compose?**
The app spawns its own sandbox containers (script runner via Podman), making nested containerization complex. Single-server deployment with process manager is simpler.
