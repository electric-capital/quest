<p align="center">
  <img src="assets/logo.svg" alt="Quest logo" width="120">
</p>

# Quest

A self-hosted AI agent workbench: a ChatGPT-style web app where people connect the services they
already use (email, documents, Slack, finance tools) and put LLM agents to work on them --
designed so that doing this with **sensitive data** is safe by default, and usable by people who
are not engineers.

See more on our [post](https://electriccapital.substack.com/p/open-sourcing-quest?r=1acoy&utm_campaign=post-expanded-share&utm_medium=web)
about why we decided to build our own harness.

Follow [@puntium](https://x.com/puntium) on X for latest updates and discussion.

## Overview

Quest runs agentic conversations against multiple LLM providers and gives the agent carefully
fenced access to upstream services through per-user OAuth or API keys. Around that core it
provides projects with shared workspaces, scheduled routines, a reusable skill library, agent
memories, sub-agents, a sandboxed script runner, and admin tooling for credentials, feature
gates, model health, and cost reporting.

**Models**: Anthropic Claude and Google Gemini (both via Vertex AI), plus additional models via
OpenRouter (currently DeepSeek and Qwen). Users pick a model per conversation; the
[LLM provider abstraction](docs/architecture/llm-providers.md) makes adding providers and models
a registry change.

**Connected services**: Gmail, Google Calendar / Drive / Docs / Sheets / Slides / Tasks, Google
Cloud, Slack, Telegram, Twitter/X, GitHub, Airtable, Ramp, SEC EDGAR, the Federal Register, and
proprietary integrations added via the plugin architecture below.

## Core design principles

Quest is built for users who need AI to work with sensitive internal data sources. The security
posture assumes the model can be confused or prompt-injected by content it reads, so the
guarantees come from the server-side design, not from trusting the model to behave:

### Private vs. public mode -- never both

By default every conversation is **private**: the agent can use the user's connected data
sources, but it has no path to the open internet. There is no generic "fetch this URL" tool, and
scripts run in a no-egress sandbox. A [public project](docs/architecture/public-projects.md)
inverts the posture: its conversations get an internet-enabled sandbox but are cut off from
every internal resource -- no skills, memories, connectors, action requests, sub-agents, or
authed APIs -- enforced at three independent layers (tool tier, dispatch-time allow-list, and a
separate credential-free container image). No conversation ever holds sensitive data and an open
channel to the internet at the same time.

### Closed exfiltration routes

Outbound access in private mode goes only to registered upstream services, via `authed_get` /
`authed_post` with per-service, verb-scoped endpoint allow-lists that are read-only by
construction. Scripts execute in ephemeral Podman containers with no network egress
([script runner](docs/architecture/script-runner.md)); their only way out is a loopback bridge
to an explicit allow-list of tools. Internal tool dispatch blocks sensitive endpoints, and the
public-mode sandbox additionally carries no credentials and firewalls off private address ranges
including the cloud metadata service.

### Writes require human confirmation

Reads are free; writes are not. Anything that changes the outside world -- sending a Slack,
Telegram, or Twitter message, creating a calendar invite, uploading to Drive, editing a
spreadsheet, saving a memory, editing a skill -- goes through an
[action request](docs/architecture/action-requests.md): the agent proposes the exact operation,
the UI renders a human-readable preview card (with diffs for edits), and the agent blocks until
the user clicks Approve, Revise, or Stop. Pending approvals survive restarts and page reloads.

### Built for less-technical users

Everything happens in the web UI: OAuth connect buttons in Settings, approval cards instead of
config files, readable previews instead of raw payloads, cost warnings before resuming expensive
conversations, and admin panels for credentials, feature gates (risky features are off by
default), model health, and per-user cost reports. Sign-in is email + password (the default for
new deployments, no external setup) or Google OAuth, restricted to an allowed domain or an
explicit email whitelist; a password deployment can switch to Google sign-in later and keep
every account.

## Plugin architecture for upstream services

Integrations are packaged as self-contained plugins: a directory under
[`plugins/`](plugins/README.md) with a `plugin.py` manifest, discovered from the filesystem at
startup -- no packaging, no entry points, no edits to core files, and no frontend changes
(plugin configuration UI is schema-rendered). A manifest can declare an admin credential schema,
a per-user connection (API key or OAuth), `authed_get` service entries with endpoint
allow-lists, system skills, tools, and action-request handlers. Plugins inherit the security
model: they cannot mount LLM-reachable HTTP routes, and public projects always block plugin
tools and services.

Reference plugins prove the surface: the in-tree `plugins/github/` (OAuth kind) and
`plugins/twitter/` (OAuth kind), plus external plugin roots loaded via `QUEST_PLUGIN_PATH`.
A fork adds a proprietary integration by committing a
directory into `plugins/` or pointing `QUEST_PLUGIN_PATH` at one. See the
[plugin architecture doc](docs/architecture/plugins.md).

## Security

We go to great lengths to create a secure environment: the private/public split, the closed
exfiltration routes, the human-confirmed writes, the no-egress script sandbox, and encryption at
rest for every stored secret are all enforced server-side rather than by trusting the model.
Even so, Quest is one layer of a larger system, and its guarantees hold only within a set of
assumptions:

- **A hardened, dedicated host.** The security architecture assumes Quest is the only thing
  running on its machine: no other services share the host, and nothing else has access to the
  host file system. The script sandbox mounts conversation workspaces from the host, and the
  data directory holds the database, workspaces, and the encryption key file, so a compromise
  of the host or of a co-located service is a compromise of Quest.
- **The deployer's environment.** A large part of the effective security posture is decided
  outside this repository: how sign-in is set up (the SSO provider, the allowed domain or email
  whitelist, whether accounts are protected by MFA), how the network is laid out (whether the
  server is reachable only from a trusted network, what TLS terminates in front of it, what the
  host can reach), how the Vertex AI and OAuth credentials are scoped, and who holds admin
  rights. Quest cannot compensate for a weak setup in any of these areas.
- **Upstream services and providers.** Connected services are accessed with the credentials each
  user grants; the least-privilege scopes Quest requests are only as effective as the upstream
  provider's enforcement of them.

We use the [V12](https://v12.sh/) security auditing tool for periodic security reviews of the
codebase.

## Quick Start

Prefer to watch? This short video walks through installing Quest and connecting it to
OpenRouter so you can start chatting:

<p align="center">
  <a href="https://www.youtube.com/watch?v=BOB2CvK1DhY">
    <img src="https://img.youtube.com/vi/BOB2CvK1DhY/maxresdefault.jpg" alt="How to quickly setup Quest with OpenRouter (YouTube)" width="640">
  </a>
</p>

Setting up a new machine (Debian packages, Google OAuth, Vertex AI)? Follow the
[Installation Guide](INSTALL.md).

1. Install prerequisites: Python 3.11+, Node.js 18+, [uv](https://github.com/astral-sh/uv),
   and Podman (optional in local mode; needed for the script sandbox)
2. Run `python3 run.py` -- local mode builds the frontend, installs dependencies, runs
   migrations, seeds a throwaway data directory with canned accounts, and starts the server on
   port 9000
3. Visit `http://localhost:9000/` and sign in with a canned dev account
4. Local mode boots with zero credentials; configure an LLM credential (Vertex AI service
   account or an OpenRouter key) to enable chat

For complete setup instructions, see the [Development Setup Guide](docs/setup/development.md);
for deployment, see the [Production Guide](docs/setup/production.md) (`run.py --prod` includes a
first-run bootstrap wizard).

## Documentation

Full documentation is in the [docs/](docs/) folder:

- [Installation Guide](INSTALL.md) -- New-environment install: Debian host dependencies, Google OAuth, Vertex AI
- [Documentation Home](docs/README.md) -- Table of contents and project overview
- [System Architecture](docs/architecture/overview.md) -- Architecture overview and data flows
- [LLM Providers](docs/architecture/llm-providers.md) -- Multi-provider abstraction (Anthropic, Gemini, OpenRouter)
- [Public Projects](docs/architecture/public-projects.md) -- The private/public security inversion
- [Action Requests](docs/architecture/action-requests.md) -- Human approval of agent writes
- [Script Runner](docs/architecture/script-runner.md) -- Sandboxed script execution
- [Plugins](docs/architecture/plugins.md) -- Upstream-service plugin architecture
- [API Reference](docs/README.md#api-reference) -- All API endpoint documentation

## License

Quest is open source, distributed under the [Apache License 2.0](LICENSE). You are free to use,
modify, and redistribute it under the terms of that license.
