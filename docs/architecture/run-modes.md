# Run Modes

## Overview

Quest runs in one of three modes, selected by the `QUEST_ENV` environment variable (set by `run.py`): `local`, `staging`, and `prod`. The legacy value `dev` is accepted as an alias for `local`. All mode resolution and mode-dependent behavior flags live in `config/environment.py`; feature code calls its semantic helpers (`allow_dev_login()`, `enforce_domain()`, `cookie_name()`, `cookie_secure()`, `image_suffix()`) instead of comparing mode strings, so every behavioral difference between modes is auditable in one file.

## Mode Matrix

| Concern | local | staging | prod |
|---|---|---|---|
| Launch | `run.py` / `run.py --local` (default; `--dev` deprecated alias) | `run.py --staging` | `run.py --prod` |
| Port | 9000 (`dev_config.json` `dev_port` override) | 8000 | 8000 |
| Login | canned-account picker + free email via `/auth/dev-login`; Google OAuth optional | Google OAuth | Google OAuth |
| Domain restriction | off | on | on |
| Database / data dir | fresh throwaway per run under `data/local-runs/`, seeded; `--keep-data` reuses the latest | persistent | persistent |
| Session cookie | `quest_session_local` | `quest_session_staging`, `Secure` | `quest_session` |
| npm install | `npm install` | `npm ci` | `npm ci` |
| Container images | `quest-script-runner-local`; build failures non-fatal | `-staging` suffix, required | `-prod` suffix, required |
| Missing credentials | degrade gracefully (see below) | fail like prod | HTTP 500s on affected features |
| Log level | DEBUG for `chat`/`auth`/`quest` | from `logging_config.json` | from `logging_config.json` |

Unknown or unset `QUEST_ENV` values resolve to `prod` (the most restrictive mode).

## Local Mode

Local mode is designed to boot a fully usable app from a fresh clone with **zero credential files** and no access to internal infrastructure.

### Throwaway data directory

`run.py --local` creates a timestamped directory under `data/local-runs/`, exports it as `QUEST_DATA_DIR` (which `config/paths.py` honors above `server_config.json`'s `data_dir`), runs migrations against it, and seeds demo data. The `LOCAL_RUNS_KEEP` most recent run directories are retained; older ones are pruned at startup. `--keep-data` reuses the most recent run directory (or `--keep-data DIR` a specific one) so state survives restarts while iterating. Because the DB, `chats/`, `projects/`, `secret_key`, and the credential-encryption key file `encryption_key.json` all live inside the run directory, the DB rows and their on-disk artifacts are discarded together.

Local mode needs no encryption password: `config/encryption.py` falls back to the fixed, public `LOCAL_DEV_PASSWORD` sentinel (an explicit `QUEST_ENCRYPTION_PASSWORD` still wins), and the sentinel is refused in staging/prod -- see [Encryption at Rest](encryption-at-rest.md).

### Shared dev config pre-baking

Before the server starts, `run.py --local` reads an optional `dev-config.json` in the checkout's **parent** directory (one shared file per box, never committed) and pre-bakes it into the run's data directory:

- a `vertex_service_account` key object is materialized and exported as `GOOGLE_APPLICATION_CREDENTIALS` (ADC for both Vertex integrations),
- each `service_credentials` entry (e.g. `google_oauth`) is copied into the per-service credential store unless already present, so those services show up configured in admin Settings,
- each `inference_credentials` entry (e.g. `openrouter`, `fireworks`) is likewise copied into the inference-credential store at `<data_dir>/inference_credentials/<provider>.json` unless already present, so API-key LLM backends come up pre-configured as an instance of the kind the id names, with the model list seeded from the entry's optional `models` list (see [Inference Providers](inference-providers.md)) -- both pre-baked files are written as plaintext by the stdlib-only `run.py` and encrypted by the `config.encryption init` step that follows --, and
- the env exports `oauth_hostname` -> `QUEST_OAUTH_HOSTNAME` (OAuth callbacks use a hostname instead of the raw request IP) and `allowed_login_domain` -> `QUEST_ALLOWED_LOGIN_DOMAIN` (the Google login callback checks this domain instead of the company one -- local-only override, the check never skips) are applied.

See [Shared Dev Config in Local Mode](../setup/development.md#shared-dev-config-dev-configjson-in-local-mode).

### Seed data

`scripts/seed_local.py` runs automatically when the database file is fresh. It creates four canned users -- `admin@quest.local` (Ada Admin), `alice@quest.local` (Alice Chen), `bob@quest.local` (Bob Rivera), `heidi@quest.local` (Heidi Highvolume) -- plus memories, a guide, a skill, a project with a workspace file, and seeded conversations (written through `chat/storage.py` so DB rows and `chat_history.json` files stay coupled).

Heidi is a high-volume account for exercising the paged sidebar list and its server-side filters: 140 standalone web conversations (the oldest 10 archived) with `last_message_at` spread over ~3 months, 12 Slack-origin and 12 Inference-API-origin conversations, and a project with 8 conversations.

The canned admin email is defined as `LOCAL_CANNED_ADMIN_EMAIL` in `config/environment.py` and is the default `admin_emails` entry in local mode when no `server_config.json` is present (`load_server_config()` in `config/server_config.py`).

### Canned-account login

The sign-in screen (`frontend/src/components/SignInScreen.tsx`) fetches `GET /auth/dev-accounts` (local-only, 404 otherwise; `auth/dev_login.py`) and renders one-click account buttons (admins first), falling back to the free-text email form. Both paths POST to the existing `/auth/dev-login`, which mints the same session cookie as Google login.

### Graceful degradation

Local mode must start and stay usable with no credentials:

- **Connectors**: per-user connectors already fail closed (`connected: false` in `GET /app/api/connectors`, 401s on use). `run.py --local` prints a startup summary of which optional credentials were found.
- **Plugin services**: plugins are configured through the per-service credential store, edited at Settings > Service Credentials. A plugin whose server-side credentials are missing keeps its tools erroring cleanly and its Data Connections row hidden (`available: false` on the connectors payload); plugin `post_load` hooks may migrate legacy credential locations at startup. See [service-credentials.md](service-credentials.md) and [plugins.md](plugins.md).
- **LLM models**: `GET /app/api/config` returns `available_models` (from `get_available_models()` in `chat/llm/config.py` -- enabled models whose credentials are present: `gemini_vertex.vertex_project_id` for Gemini, `anthropic.vertex_project_id` for Claude, the instance's API key for OpenRouter instance models, minus the admin's disabled models and models with a failing verdict in the model-health store; see [inference-providers.md](inference-providers.md)) plus `models`, the metadata catalog the frontend builds its pickers from. The frontend filters the composer model picker to the available models and disables sending with a notice when none are available. The dev-config `inference_credentials` mapping pre-bakes one key file per provider-instance id (`"openrouter"` is the legacy instance seeded with the historical curated models; any other id becomes an empty OpenRouter instance to fill in from Settings).
- **Container runtime**: Podman image validation failures are non-fatal in local mode; `run_script`/`run_python` are simply unavailable for the run.

## Staging Mode

Staging behaves like prod -- domain restriction on, real credentials, persistent data dir -- with a distinct cookie name (`quest_session_staging`) and the `Secure` cookie attribute, so staging and prod sessions never collide in a shared browser. It is intended to be launched by deploy automation (`run.py --staging`). Prod cookies are intentionally left non-`Secure` until the prod fronting is confirmed HTTPS-only (see `cookie_secure()` in `config/environment.py`).

## Prod Mode

`run.py --prod`, port 8000, `quest_session` cookie, all credentials required for their respective features.

### First-run bootstrap

When the deployment is missing `admin_emails` or Google OAuth client credentials, `run.py --prod` runs an interactive configuration wizard (`scripts/bootstrap_prod.py`, stdlib-only) before the build steps; non-interactive unconfigured startups abort with instructions (`QUEST_SKIP_BOOTSTRAP=1` starts anyway), and `--bootstrap` re-runs the wizard on a configured deployment.

The wizard writes `server_config.json` (admin emails, `allowed_login_domain`, `app_base_url`, Vertex project id), the `google_oauth` per-service credential store file, and a service-account key copy at `<data_dir>/vertex-service-account.json` that `run.py` exports as `GOOGLE_APPLICATION_CREDENTIALS` in staging/prod (`setup_server_vertex_credentials()`). See [Production Deployment](../setup/production.md).
