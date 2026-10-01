# GitHub API Documentation

This document describes how Quest accesses GitHub: reading repositories, issues, pull requests, commits, file contents, stargazers, search, and Actions (workflow runs, jobs, and job logs), plus three approval-gated writes (running a workflow, commenting on an issue or pull request, closing / reopening an issue).

## Overview

The GitHub integration is packaged as the in-tree `plugins/github` plugin (plugin id `github`; see [Plugins](../../../docs/architecture/plugins.md)) — the reference implementation for an **oauth-kind** per-user plugin connection.

The plugin's manifest registers the `api.github.com` entry into the `authed_get` service registry: reads are authenticated GET requests directly to the GitHub REST API at `https://api.github.com/...`, with the user's OAuth access token injected as a Bearer token, an `allowed_endpoints` regex list restricting reachable paths, and service-level `default_headers` supplying GitHub's required `User-Agent` and recommended `Accept: application/vnd.github+json` headers.

A dedicated `github_get_job_log` plugin tool (previously the core `get_github_job_log`) handles the special case of Actions job log downloads, which return a 302 to a signed third-party URL and would otherwise leak the user's bearer token.

Writes are action requests (see [Action Requests](../../../docs/architecture/action-requests.md)) registered by the plugin: `github_trigger_workflow`, `github_comment_on_issue` and `github_set_issue_state`. They call GitHub directly from their handlers after the user approves the card; the `authed_get` entry stays GET-only.

## Key Files

| File | Description |
|------|-------------|
| `plugins/github/manifest.py` | The `QuestPlugin` manifest: credential schema (OAuth app client id/secret), oauth-kind `user_connection`, the `api.github.com` service entry (`allowed_endpoints` regex list covering core repo paths plus Actions runs/jobs/workflows, `default_headers`), the `system:github` skill, the `github_get_job_log` tool, and the three action-request handlers |
| `plugins/github/upstream.py` | `GITHUB_SCOPES`, `load_github_client_config()` (store + legacy fallback), token-blob construction and the refreshing credential loader (`get_github_token()`), Bearer injector for the service entry, `connected`/`needs_reauth` hooks over the stored `oauth_blob`, and `github_request()` / `github_error_message()` -- the direct REST helper the action-request handlers use |
| `plugins/github/handlers.py` | The action-request handlers (`GitHubTriggerWorkflowHandler`, `GitHubCommentOnIssueHandler`, `GitHubSetIssueStateHandler`) with their param validation, proposal-time GitHub reads, card previews and execute calls |
| `plugins/github/oauth.py` | OAuth flow router (`/auth/github`, `/auth/github/callback`, `/auth/github/disconnect`), mounted by `mount_plugin_oauth_routers()` in quest.py |
| `plugins/github/tools.py` | `github_get_job_log` handler (302 redirect, two-hop fetch, workspace download) |
| `plugins/github/instructions.md` | LLM-facing `system:github` skill content (key paths, params, examples, and the "Write Operations" spec of the three action request types) |
| `plugins/github/tests/test_github_action_requests.py` | Handler tests: validation, proposal-time rejections and card contents, execute bodies and error conversion |
| `db/models.py` | `user_service_credentials` table -- the token JSON lives in the github row's `oauth_blob` (`access_token`, `token_type`, `scope`, granted `scopes` list, `authorized_at`, and for expiring GitHub App tokens `refresh_token`/`expires_at`/`refresh_token_expires_at`) |

## Authentication

GitHub reads require the user to connect their GitHub account via Settings > Data Connections.

**Token storage:** the `oauth_blob` of the user's `user_service_credentials` row for service `github`, populated by the OAuth callback in `plugins/github/oauth.py` (the pre-plugin `users.github_oauth` column was migrated into rows by Alembic revision `a7c3e91b52d8`).

The callback records the scopes GitHub actually **granted**, and the plugin's `needs_reauth` hook compares them against `GITHUB_SCOPES` so a widened scope list shows the "Update Available" re-authorize badge on the connector row.

The scope check only applies to classic OAuth App tokens (`gho_` prefix): when the configured client credentials belong to a **GitHub App**, the token exchange returns a `ghu_` user access token with an empty `scope` (permissions come from the app installation), so `needs_reauth` skips the comparison for `ghu_` tokens instead of flagging re-auth forever.

**Credential loading:** `load_github_credentials()` in `plugins/github/upstream.py` delegates to `get_github_token()`. Classic OAuth App tokens (no refresh token stored) are read straight off the user dict's attached `service_credentials["github"]` row. Expiring GitHub App tokens (8-hour `ghu_` access token + rotating 6-month `ghr_` refresh token) are read from the stored row and refreshed within 5 minutes of expiry under a per-user lock; see [GitHub App Setup](github-app-setup.md#token-architecture) for the refresh semantics. If the user has not connected GitHub, or an expired token cannot be refreshed, the handler returns the `missing_credentials_error` (`MISSING_CREDENTIALS_ERROR` in `upstream.py`) directing the user to Settings > Data Connections.

The GitHub service entry sets `requires_user: True` (per-user OAuth credentials) and `retry_on_401: True`: a 401 re-runs the loader, which re-reads the stored row and so picks up a token another turn or routine refreshed in the meantime.

## GitHub Read Access (via `authed_get`)

GitHub reads use `authed_get` with the GitHub REST API at `api.github.com`. The plugin's service entry (registered into `_SERVICE_REGISTRY` at load) defines the allowed endpoint patterns via regex validation -- requests to non-matching paths are rejected.

The allow-list covers core repository reads (repos, issues, pull requests, commits, contents, stargazers, search, org repo lists) and read-only GitHub Actions endpoints (workflow runs, individual runs, jobs for a run, jobs for a re-run attempt, single jobs with their step array, workflow definitions, and runs filtered by workflow).

See `plugins/github/manifest.py` for the full allowed endpoint list and `plugins/github/instructions.md` for the LLM-facing documentation (base URL, key paths, common query parameters, and example invocations).

Repo file contents (via `/repos/{owner}/{repo}/contents/...`), search results, and full PR/issue listings frequently exceed the 3 KB `authed_get` size gate. When the model wants to grep, parse, or otherwise post-process the response with `run_python` / `run_script`, passing `output_file` on the `authed_get` call writes the body under the hidden `.responses/` workspace subdirectory (non-destructively -- a name collision errors) and returns a small receipt whose `path` is the `.responses/...` location.

For binary file content fetched via `?raw=true` or the `download_url` from a contents response, `output_file` also unlocks `alt=media`-style downloads on hosts that support them. Job log downloads stay on the dedicated `github_get_job_log` tool because of the 302-to-Azure two-hop pattern (see below). See [Large Response Protection](../../../docs/architecture/gemini-api.md#large-response-protection).

### Default Headers

The GitHub registry entry declares a `default_headers` dict containing `User-Agent: Quest/1.0` and `Accept: application/vnd.github+json`. `_make_authed_request()` merges these into every outgoing GitHub request (both the initial request and the 401 retry branch) before caller-supplied headers and auth injection. GitHub's REST API rejects requests that do not include a `User-Agent`, and the versioned `Accept` header is GitHub's recommended way to pin a response format. See [Authenticated External API Requests](../../../docs/architecture/gemini-api.md#authenticated-external-api-requests-authed_get) for the service registry schema and header merge precedence.

## Reading Actions Job Logs (`github_get_job_log`)

Job log downloads use a dedicated tool rather than `authed_get` because the GitHub endpoint `/repos/{owner}/{repo}/actions/jobs/{job_id}/logs` returns a 302 redirect to a short-lived signed Azure Blob Storage URL with the actual log body.

The handler in `plugins/github/tools.py` (`_handle_github_get_job_log()`) makes the first hop with the user's GitHub bearer token, captures the redirect, and then follows the second hop in a fresh unauthenticated `httpx.AsyncClient` so that the bearer token is never sent to the third-party storage host.

The response body is also a plain-text log (not JSON) that can be many megabytes, which makes it a poor fit for the JSON-oriented `authed_get` response shape.

The handler writes the log into the conversation workspace. The default destination is `github-job-logs/github-job-{job_id}.log`. Callers may pass a `path` argument to override this: a value ending in `/` (or matching an existing directory) is treated as a directory and the default filename is appended; otherwise the value is treated as a full file path. Absolute paths and `..` traversal are rejected so logs cannot escape the workspace.

The tool result is a JSON object with the destination filename and relative path, the byte size, a short preview (~500 bytes), and a message instructing the LLM to use `get_workspace_file` to read the rest. The same large-response size gate that `authed_get` uses (3 KB default, overridable via `force_large_response`) applies to the preview payload, so a multi-megabyte log does not flood the conversation -- only the preview and metadata are surfaced inline. See [Authenticated External API Requests](../../../docs/architecture/gemini-api.md#authenticated-external-api-requests-authed_get) for the size gate semantics.

## Write Operations (Action Requests)

All three types follow the same shape in `plugins/github/handlers.py`:

1. `validate_params()` -- strict allow-list, URL-safe `owner` / `repo`, type-specific shape checks. Same-turn `Invalid parameters` on failure.
2. `validate_against_upstream()` -- reads the write's target from GitHub via `_proposal_read()`. A 404 rejects same-turn (no card); a successful read injects the target's identity into the params for the card. No connection, network errors, 401/403 and 5xx log a WARNING and defer to `execute()`.
3. `render_preview()` / `summary_snippet()` -- build the card from the injected fields, falling back to the raw identifiers when the read was deferred.
4. `execute()` -- the write, via `_write()`, which converts every failure into `RuntimeError` (request stays open) and appends a write-permission hint on 403/404.

The injected card fields (`workflow_name`, `workflow_path`, `ref_is_default`, `issue_title`, `issue_state`, `is_pull_request`) are absent from the allow-lists, so the model cannot supply them: a card can only name the target the write goes to.

| Type | GitHub call on Approve | Proposal-time checks | Card |
|------|------------------------|----------------------|------|
| `github_trigger_workflow` | `POST /repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches` with `ref` + optional `inputs` | workflow exists and is `active`; a missing `ref` is filled with the repo's default branch (rejected when it cannot be determined); the workflow file exists on the ref, has a `workflow_dispatch` trigger, and declares every supplied input | Workflow (GitHub display name + file name), Repository, Ref, Inputs (one `name = value` line each) |
| `github_comment_on_issue` | `POST /repos/{owner}/{repo}/issues/{n}/comments` | issue / pull request exists | Repository, Issue or Pull request (`#n title (state)`), Comment |
| `github_set_issue_state` | `PATCH /repos/{owner}/{repo}/issues/{n}` with `state` (+ `state_reason` when closing) | issue exists, is not a pull request, is not already in the target state | Repository, Issue, Current state, New state |

Type-specific notes:

- **Workflow identity.** `workflow` is the file name or numeric id, never the display name; `_workflow_label()` renders the name GitHub reports plus the file name (a workflow without a `name:` key reports its path as its name, in which case only the file name shows).
- **Workflow inputs** are normalized to strings (booleans to `true`/`false`), capped at GitHub's 25, then checked against the workflow file as it exists on the ref: `_parse_dispatch_inputs()` parses it with PyYAML (`yaml.safe_load`, off the event loop, files over 512 KB skipped; handles the string / list / mapping forms of `on` and YAML 1.1 reading the bare `on` key as boolean true) and `_check_inputs_against_declared()` rejects unknown names, missing required inputs without a default, values outside a `choice` input's `options`, and non-`true`/`false` / non-numeric values for `boolean` / `number` inputs. The error lists the declared inputs. `environment`-typed inputs are not checked against the repo's environments. A file that cannot be parsed falls back to a substring check for the trigger and leaves the inputs to GitHub's 422 at Approve time.
- **Dispatch result.** GitHub answers with the new run's id and URLs, returned as `run_id` / `url`; a bodyless 204 yields a message pointing at the workflow's runs list instead.
- **Pull requests.** Comments work on pull requests (GitHub models them as issues; the card labels the row "Pull request"). State changes refuse pull requests at proposal time and again in `execute()`, which re-reads the issue because the issues endpoint would otherwise close a PR addressed by its number.
- **Comment body** is posted verbatim under the user's own GitHub account -- no attribution footer is appended.

## OAuth Flow

The OAuth endpoints are the plugin's `oauth_router` (`plugins/github/oauth.py`: `/auth/github`, `/auth/github/callback`, `/auth/github/disconnect` -- the same URLs as before the plugin migration, so existing GitHub OAuth app registrations keep working), mounted under the plugin's `/auth/github` namespace by `mount_plugin_oauth_routers()`. The callback exchanges the code for an access token, stores the token JSON (including granted scopes) in the user's `user_service_credentials` row via `upsert_credential(..., oauth_blob=...)`, and invalidates sessions so the system prompt refreshes. See [GitHub App Setup](github-app-setup.md) for credential configuration.

## System Instructions Integration

The LLM-facing documentation is the `system:github` skill (content from `plugins/github/instructions.md`), gated on the `github` connected-services key -- like every plugin service, that key is true only when the server side is configured (admin client id/secret) AND the user has connected. The `github_get_job_log` tool is enumerated in the system prompt's dynamic tools section only for connected users (`requires_service="github"`).

## Constraints

- **Read-only `authed_get`:** Only the GET paths on the `allowed_endpoints` list are reachable; the only writes are the three approval-gated action request types
- **Write permissions:** Classic OAuth App tokens cover the writes with the existing `repo` scope (no re-consent). A GitHub App needs the Actions, Issues and Pull requests repository permissions set to read-and-write and an installation covering the repository; otherwise the write fails at Approve time with a 403/404 and the permission hint
- **Rate limiting:** GitHub enforces 5,000 requests per hour for authenticated users
- **File size:** The contents endpoint returns base64-encoded content for files under 1MB; for files 1-100MB, use the `download_url` from the response; files over 100MB are not retrievable via the contents endpoint
- **Issues vs PRs:** The issues endpoint also returns pull requests (GitHub models PRs as issues); use the `pulls` endpoint for PR-specific data
- **Search syntax:** Search queries support GitHub's search syntax (qualifiers like `repo:`, `language:`, `state:`, `author:`, `is:pr`, `is:issue`)
- **Pagination:** Maximum 100 results per page; GitHub silently caps larger values
- **Organization access:** Organization repos require the OAuth app to be approved by the org admin (see [GitHub App Setup](github-app-setup.md) troubleshooting)
- **Token scope:** The `repo` scope grants write permissions at the OAuth level (GitHub has no read-only repo scope), but the `allowed_endpoints` gate on the plugin's service entry restricts `authed_get` to read-only GitHub paths, and the only other callers of the token are the three action-request handlers
- **Allowed endpoints:** Only the read-only path patterns defined in the plugin's service entry are permitted; other GitHub API paths are rejected by the `allowed_endpoints` validation

## Out of Scope

The following GitHub capabilities are deliberately not exposed:

- **Writes beyond the three action request types:** No issue/PR creation or editing (titles, bodies, labels, assignees, milestones), no closing or merging pull requests, no review comments, no commits or pushes, no workflow re-runs, cancellations, or deletions.
- **Run-level zip log downloads:** The `/repos/{owner}/{repo}/actions/runs/{run_id}/logs` path is allow-listed defensively in case it is ever needed, but no dedicated tool exists to fetch and unpack the multi-job zip archive. Use `github_get_job_log` for individual jobs.
- **Actions write/admin surfaces:** No artifacts, secrets, variables, self-hosted runners, caches, or billing/usage endpoints.

## Design Decisions

**Why `authed_get` instead of proxy endpoints?**
GitHub reads are standard GitHub REST API GET requests. Using `authed_get` eliminates the need for 19 dedicated proxy endpoints, reduces backend code, centralizes credential loading in the service registry, provides server-side path gating via `allowed_endpoints`, and participates in the shared `authed_get` large-response protection (size gate + blob storage). It also aligns GitHub with the same pattern used by Google Calendar, Drive, Docs, Sheets, Tasks, Gmail Raw, and Airtable.

**Why refresh in the credential loader?**
Classic OAuth App tokens never expire, but deployments registered as a GitHub App get 8-hour user tokens, which previously went dead mid-day and broke unattended routines until the user clicked Reconnect (the popup closes itself because the app is already authorized). Refreshing in the loader -- the Twitter/X and Microsoft 365 pattern -- covers every caller (authed_get and `github_get_job_log`) and is a no-op for non-expiring tokens.

**Why service-level `default_headers`?**
GitHub's REST API rejects requests without a `User-Agent` header and recommends the versioned `Accept: application/vnd.github+json` header. Rather than hard-coding these in a GitHub-specific code path, the service registry descriptor schema includes a generic `default_headers` field that any service can populate. `_make_authed_request()` merges the service's default headers into every outgoing request with the precedence `default_headers < caller headers < inject_auth`, so callers can still override defaults and auth injection always wins.

**Why are writes action requests instead of allow-listed POST endpoints?**
A write must be reviewed by the user before it happens, and the review must describe the real target. Action requests give both: the handler reads the workflow / issue from GitHub and puts its name on the card, and nothing is sent until Approve. An `authed_post` allow-list would run without a card. The `authed_get` entry therefore stays GET-only, enforced server-side independent of the token's own scope.

**Why parse the workflow file at proposal time?**
GitHub has no endpoint that validates a dispatch without running it, and the workflow API does not return a workflow's declared inputs. The common dispatch failures -- a ref that does not carry the workflow, a workflow that cannot be run manually, a misspelled or missing input -- are all visible in the workflow file on that ref, so one extra read plus a YAML parse (the `pyyaml` dependency) turns them into same-turn rejections instead of a card that fails on Approve. The input checks are strict (exact names, `true`/`false` for booleans) because each has a form GitHub always accepts, so a rejection costs the model one correction and never blocks a valid run.

**Why does `github_set_issue_state` refuse pull requests?**
Closing a pull request is a different decision from closing an issue (it abandons a change), and GitHub's issues endpoint does not distinguish the two. The type is scoped to what its name says.

**Why a dedicated `github_get_job_log` tool instead of routing job logs through `authed_get`?**
Three reasons make job logs a poor fit for the generic `authed_get` path.

- First, the GitHub job-logs endpoint returns a 302 to a signed Azure Blob Storage URL; following that redirect with the user's GitHub bearer token still attached would leak the token to a third-party host. The dedicated handler does the second hop in a fresh unauthenticated `httpx.AsyncClient` so the token never leaves api.github.com.
- Second, the response body is a plain-text log (not JSON) and is often many megabytes -- pulling it inline would defeat the purpose of the `authed_get` size gate.
- Third, agents almost always want job logs as a workspace file they can grep and re-read, so the tool writes directly to the workspace and returns only metadata plus a short preview.

**Why is GitHub a plugin?**
Phase 5 of the plugin architecture ([Plugins](../../../docs/architecture/plugins.md)) needed a proving migration for the oauth-kind `UserConnectionSpec` -- a plugin-provided `/auth/<id>` router, token storage in `user_service_credentials.oauth_blob`, and the `needs_reauth` hook.

GitHub was the natural candidate: its OAuth flow was the simplest of the core connectors (refresh support for GitHub App tokens came later), and its whole surface (service entry, skill, one tool, credential schema) fits the manifest.

The tool was renamed `get_github_job_log` -> `github_get_job_log` for the `<id>_` prefix rule (tool names have no persistence, so the rename is free); the store key (`github.json`), connected-services key, `system:github` id, and OAuth URLs are all unchanged.
