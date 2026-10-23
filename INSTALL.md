# Installation Guide

This guide walks a human (or agent) installer through setting up Quest in a **new environment**,
from a bare Debian machine to a running instance. It covers three things in order:

1. [Host dependencies](#1-host-dependencies-debian) -- OS packages and tools the app needs
2. [Google OAuth setup](#2-google-oauth-setup) -- optional: Google sign-in and the Google services
   connectors (a deployment can use email + password sign-in instead)
3. [Vertex AI setup](#3-vertex-ai-setup) -- optional: run Claude and Gemini on Google Cloud. A
   plain Anthropic, OpenAI or Gemini API key works without it (section 4)

For day-to-day development workflow see [docs/setup/development.md](docs/setup/development.md);
for production hardening (systemd, reverse proxy, backups) see
[docs/setup/production.md](docs/setup/production.md).

## What you are installing

Quest is a single-process FastAPI server that serves both the REST/WebSocket API and the built
React frontend as static files. State lives in a SQLite database plus JSON files under `data/`.
Scripts written by the LLM run in ephemeral rootless **Podman** containers.

The launcher `run.py` does almost everything (frontend build, Python deps, DB migrations,
script-runner image build, server start) in one command:

- `python3 run.py` -- **local mode**: port 9000, throwaway seeded database, canned-account login,
  no credentials required to boot. Good for a first smoke test.
- `python3 run.py --prod` -- **production mode**: port 8000, real sign-in, domain
  restriction. On an unconfigured machine this launches an interactive first-run wizard
  (`scripts/bootstrap_prod.py`) that prompts for everything in sections 2 and 3 below. Its
  default sign-in method is **email + password** (you choose the admin password in the wizard
  and invite users from Settings > Sign-in), which needs no Google OAuth client -- skip
  section 2 until you want Google services or Google sign-in. A password deployment can switch
  to Google sign-in later from Settings > Sign-in without losing any accounts.

## 1. Host dependencies (Debian)

Tested on Debian 12 (bookworm) and Debian 13 (trixie). Requirements:

| Software | Minimum version | Purpose |
|----------|-----------------|---------|
| Python   | 3.11+           | Backend runtime |
| Node.js  | 18+             | Frontend build (Vite) |
| npm      | 9+              | Frontend package manager |
| Podman   | 4+              | Rootless script-runner sandbox |
| uv       | latest          | Python package/venv manager |

Debian 12 ships Python 3.11, Node 18, npm 9, Podman 4.3; Debian 13 ships Python 3.13, Node 20,
Podman 5.4 -- both meet the minimums from the distro archive, no third-party apt repos needed.

### Packages

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv nodejs npm podman uidmap slirp4netns
```

Notes:

- `uidmap` and `slirp4netns` are required for **rootless** Podman. The script-runner sandbox
  explicitly uses slirp4netns networking (containers reach the host API via `10.0.2.2` with
  socat forwarding -- see [docs/architecture/script-runner.md](docs/architecture/script-runner.md)),
  so install `slirp4netns` even on newer Podman versions that default to pasta.
- No system-wide Python packages are needed; `uv` creates and manages `.venv/` from
  `pyproject.toml` / `uv.lock`.

### uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# restart your shell, or: source ~/.local/bin/env
uv --version
```

### Rootless Podman check

The server must run as an unprivileged user, and that user needs subordinate UID/GID ranges for
rootless containers. Debian's `adduser` normally sets these up; verify and fix if missing:

```bash
grep "^$(whoami):" /etc/subuid /etc/subgid || \
  sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$(whoami)"
podman run --rm docker.io/library/alpine echo ok
```

On first launch, `run.py` builds the sandbox images from `Dockerfile.script-runner`, which
pulls `python:3.12-slim` from Docker Hub -- the machine needs outbound network access at build
time. The images are rebuilt automatically whenever the Dockerfile changes.

### Clone and pre-flight

```bash
git clone <repo-url> quest && cd quest
uv sync                      # backend deps into .venv/ (run.py also does this)
cd frontend && npm ci && cd ..   # frontend deps (run.py also does this; npm install in local mode)
```

At this point `python3 run.py` (local mode) should boot a working instance on port 9000 with
canned test accounts and no credentials. Chat stays disabled until an LLM credential is
configured (section 3). Continue with sections 2 and 3 for a real deployment.

## 2. Google OAuth setup

Google OAuth serves two separate flows on the same OAuth client:

- **App login** (`/auth/callback`) -- identity only (`openid`, `email`, `profile`). Required for
  deployments using Google sign-in (`"login_method": "google"`); not used under email +
  password sign-in.
- **Google Services** (`/auth/google-services/callback`) -- broader per-user scopes for Gmail,
  Calendar, Drive, Docs, Sheets, Slides, Tasks, and read-only Google Cloud access. Optional;
  users connect it later from Settings. The full scope list is `GOOGLE_SERVICE_SCOPES` in
  `auth/config.py`.

### 2.1 Pick the public hostname first

OAuth redirect URIs are built from the deployment's public hostname, and **Google rejects
redirect URIs containing raw IP addresses**. Decide the hostname users will browse to (e.g.
`quest.yourdomain.com`) before creating the OAuth client. If the server is only reachable by IP
(e.g. an internal dev box), pick a hostname anyway, set it as `oauth_hostname` in
`server_config.json` (the bootstrap wizard prompts for it), and point the hostname at the
server's IP via DNS or `/etc/hosts` on client machines. See
[docs/architecture/auth.md](docs/architecture/auth.md).

### 2.2 Create the OAuth client in Google Cloud Console

1. In [Google Cloud Console](https://console.cloud.google.com/), pick (or create) a project.
2. Configure the **OAuth consent screen** (APIs & Services > OAuth consent screen). For a
   Google Workspace organization, choose **Internal** -- Quest enforces a single allowed login
   domain anyway, and Internal avoids Google's app-verification review for sensitive scopes.
3. Enable the APIs for the Google Services connectors you plan to use (APIs & Services >
   Library): Gmail API, Google Calendar API, Google Drive API, Google Docs API, Google Sheets
   API, Google Slides API, Google Tasks API. (Skippable if you only need app login.)
4. Create the client: APIs & Services > Credentials > **Create credentials > OAuth client ID**,
   application type **Web application**.
5. Register **both** authorized redirect URIs (replace host, keep both paths):
   - `https://quest.yourdomain.com/auth/callback`
   - `https://quest.yourdomain.com/auth/google-services/callback`

   For an HTTP dev instance on a non-standard port, register the port-qualified `http://` forms
   instead (e.g. `http://dev.example.com:9100/auth/callback`).
6. Note the **client ID** and **client secret**.

### 2.3 Give the credentials to Quest

Preferred paths, in order:

- **Production first run**: `python3 run.py --prod` launches the bootstrap wizard, which (when
  you pick Google sign-in) prompts
  for admin email(s), the allowed login domain, the public hostname, and the OAuth client
  id/secret, then writes them to the right places (`server_config.json` plus the per-service
  credential store under `data/service_credentials/`). It also prints the exact redirect URIs to
  register, so you can run it before or after step 2.2.
- **Already-running instance**: an admin can enter or rotate the credentials in
  **Settings > Service Credentials > Google OAuth** in the web UI.
- Legacy fallback: a `server_credentials.json` file in the repo root (see
  `server_credentials.example.json`); the per-service store takes precedence. Never commit it.

Login is domain-restricted in all non-local modes: only accounts on the configured
`allowed_login_domain` can sign in, and admin capabilities are granted to the `admin_emails`
listed in `server_config.json` (both set by the wizard).

## 3. Vertex AI setup

This section is **optional**. The built-in Claude and Gemini model registry runs on **Google
Cloud Vertex AI**, authenticated with Application Default Credentials (ADC), and needs a GCP
project with Vertex AI enabled and a service account key. Without Vertex, the first admin to
sign in gets a "Connect a model" card in place of the composer: paste an Anthropic, OpenAI or
Gemini API key (or an OpenRouter / Fireworks / NEAR AI / TrustedRouter key, or a self-hosted
server URL) and Quest adds that provider's models for everyone. Admins add or change providers
later in Settings > Inference Providers (see
[docs/architecture/inference-providers.md](docs/architecture/inference-providers.md)). You can
skip to [section 4](#4-run-it) and connect a key there.

### 3.1 In Google Cloud Console

1. Pick (or create) a GCP project with billing enabled. It can be the same project as the OAuth
   client or a different one.
2. Enable the **Vertex AI API** (`aiplatform.googleapis.com`).
3. For Claude models: in **Vertex AI > Model Garden**, open each Anthropic model you plan to
   serve (e.g. Claude Opus, Sonnet, Haiku) and click **Enable** to accept its terms. A model not
   enabled in Model Garden fails at call time with a "not enabled" error.
4. Create a **service account** (IAM & Admin > Service Accounts) and grant it the
   **Vertex AI User** role (`roles/aiplatform.user`) on the project.
5. Create a **JSON key** for the service account and copy the key file to the server.

### 3.2 Give the key and project id to Quest

- **Production first run**: the bootstrap wizard prompts for the key file path and the Vertex
  project id. It copies the key to `<data_dir>/vertex-service-account.json` and writes the
  project id to `server_config.json`; on every staging/prod startup `run.py` exports the key
  copy as `GOOGLE_APPLICATION_CREDENTIALS` automatically, so no manual env-var setup is needed.
  An externally-set `GOOGLE_APPLICATION_CREDENTIALS` wins if you prefer to manage ADC yourself.
- **Local mode**: put the key JSON (and optionally shared service credentials) in a
  `dev-config.json` in the checkout's *parent* directory -- see
  [docs/setup/development.md](docs/setup/development.md).

The relevant `server_config.json` keys (documented in `server_config.example.json`):

```json
{
  "anthropic":     { "vertex_project_id": "your-gcp-project", "vertex_region": "us-east5" },
  "gemini_vertex": { "vertex_project_id": "your-gcp-project", "vertex_region": "global" }
}
```

- `anthropic.vertex_project_id` is required for Claude models; `gemini_vertex.vertex_project_id`
  is required for Gemini models but falls back to the `anthropic` value, so one project id
  configured under `anthropic` enables both (the wizard writes just that).
- Regions rarely need changing: the Anthropic default is `us-east5`, but several newer Claude
  models are pinned per-model to the `global` endpoint; Gemini defaults to `global` and Gemini
  3.x is *only* served there (regional endpoints 404). See
  [docs/architecture/llm-providers.md](docs/architecture/llm-providers.md).

### 3.3 Verify

Start the server and check the startup log: `run.py` prints a credentials checklist ("Anthropic
on Vertex", "Gemini on Vertex", "Google OAuth", ...) and warns if the key file's `project_id`
disagrees with the configured Vertex project. Then sign in as an admin and open
**Settings > Inference Providers**: each model card shows a live health verdict (a minimal real
inference call per model) with a **Recheck** button, which catches Model-Garden-not-enabled and
quota errors with the provider's error text inline.

## 4. Run it

```bash
python3 run.py --prod
```

First run walks through the bootstrap wizard (sections 2-3), then builds the frontend, installs
deps, runs migrations, builds the script-runner image, and serves everything on port 8000.
Re-run the wizard any time with `python3 run.py --prod --bootstrap`.

For a permanent deployment, front the server with a reverse proxy for HTTPS (WebSocket upgrade
headers, >=5 min read timeout, large client body size for uploads) and manage the process with
systemd -- details in [docs/setup/production.md](docs/setup/production.md). Optional connectors
(Slack, GitHub, Twitter/X, Ramp, Telegram) are configured after first login in
**Settings > Service Credentials**; per-connector guides are under [docs/setup/](docs/setup/).
