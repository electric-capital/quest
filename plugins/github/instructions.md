## GitHub API (via authed_get)

Reads go through `authed_get`; the three supported writes (running a workflow, commenting on an issue or pull request, closing / reopening an issue) are approval-gated action requests -- see **Write Operations (Require Approval)** below.

Access the GitHub REST API using `authed_get` with the full GitHub API URL. Authentication is handled automatically -- the user's GitHub OAuth token is injected as a Bearer header, and GitHub's required `User-Agent` and `Accept: application/vnd.github+json` headers are attached for you.

**Base URL:** `https://api.github.com`

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/user` | Get the authenticated user's profile |
| `/user/repos` | List repositories for the authenticated user |
| `/user/orgs` | List organizations for the authenticated user |
| `/repos/{owner}/{repo}` | Get a single repository |
| `/repos/{owner}/{repo}/branches` | List branches |
| `/repos/{owner}/{repo}/stargazers` | List users who have starred the repo (send `Accept: application/vnd.github.star+json` to also get `starred_at`) |
| `/repos/{owner}/{repo}/issues` | List issues (also returns PRs) |
| `/repos/{owner}/{repo}/issues/{issue_number}` | Get a single issue |
| `/repos/{owner}/{repo}/issues/{issue_number}/comments` | List issue comments |
| `/repos/{owner}/{repo}/pulls` | List pull requests |
| `/repos/{owner}/{repo}/pulls/{pull_number}` | Get a single pull request |
| `/repos/{owner}/{repo}/pulls/{pull_number}/files` | List changed files on a PR |
| `/repos/{owner}/{repo}/pulls/{pull_number}/reviews` | List reviews on a PR |
| `/repos/{owner}/{repo}/commits` | List commits |
| `/repos/{owner}/{repo}/commits/{ref}` | Get a single commit by SHA or ref |
| `/repos/{owner}/{repo}/contents/{path}` | Get file or directory contents (omit `{path}` for the repo root) |
| `/repos/{owner}/{repo}/actions/runs` | List workflow runs (CI/CD) |
| `/repos/{owner}/{repo}/actions/runs/{run_id}` | Get a single workflow run |
| `/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` | List jobs for a workflow run |
| `/repos/{owner}/{repo}/actions/jobs/{job_id}` | Get a single job (response includes the `steps` array) |
| `/repos/{owner}/{repo}/actions/workflows` | List workflows defined in the repo |
| `/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs` | List runs for a specific workflow (numeric ID or file name like `ci.yml`) |
| `/orgs/{org}/repos` | List repositories for an organization |
| `/search/repositories` | Search repositories |
| `/search/issues` | Search issues and pull requests |
| `/search/code` | Search code |

**Common Parameters:**
- `per_page`: Results per page (max 100; GitHub silently caps at 100)
- `page`: Page number for pagination
- `state`: Issue/PR state filter (`open`, `closed`, `all`)
- `labels`: Comma-separated issue label names
- `sort`: Sort field (varies by endpoint -- e.g. `created`, `updated`, `pushed`, `full_name`, `comments`, `stars`, `forks`)
- `direction` / `order`: Sort direction (`asc`, `desc`)
- `since` / `until`: ISO 8601 timestamp filters (commits, issues)
- `head` / `base`: Branch filters for pull requests (`head` is `user:ref-name` or `org:ref-name`)
- `sha` / `path` / `author` / `committer`: Commit listing filters
- `ref`: Branch, tag, or commit SHA for `contents` (defaults to the default branch)
- `type`: Repository type filter (`all`, `owner`, `public`, `private`, `member` for user repos; `all`, `public`, `private`, `forks`, `sources`, `member` for org repos)
- `q`: Search query (required for `search/*` endpoints; supports GitHub search qualifiers like `repo:`, `language:`, `state:`)
- Actions runs filters: `branch`, `event`, `status` (e.g. `failure`, `success`, `in_progress`), `actor`, `created`, `head_sha`, `workflow_id`
- Actions jobs filter: `filter=latest|all` (defaults to `latest` on `/actions/runs/{run_id}/jobs`)

**Example tool calls:**

```
# Get the authenticated user's profile
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/user"})

# List the authenticated user's repos (most recently updated first)
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/user/repos?per_page=10&sort=updated"})

# List the authenticated user's organizations
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/user/orgs"})

# List an org's repos
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/orgs/my-org/repos?per_page=10"})

# Get a repository
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo"})

# List branches
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/branches?per_page=50"})

# List who starred a repo -- one page of 100 user objects, saved for run_python post-processing
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/stargazers?per_page=100&page=1", "output_file": "stargazers-1.json"})

# Same, with the time each star was given (items become {"starred_at": ..., "user": {...}})
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/stargazers?per_page=100&page=1", "headers": {"Accept": "application/vnd.github.star+json"}, "output_file": "stargazers-1.json"})

# List open issues
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/issues?state=open&per_page=10"})

# Get a single issue and its comments
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/issues/42"})
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/issues/42/comments"})

# List open PRs
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/pulls?state=open"})

# Get a PR, its changed files, and its reviews
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/pulls/123"})
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/pulls/123/files"})
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/pulls/123/reviews"})

# List recent commits
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/commits?per_page=10"})

# Get a single commit
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/commits/abc123"})

# Get a file's contents (base64-encoded for files under 1MB)
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/contents/README.md"})

# Get a file from a specific branch
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/contents/src/main.py?ref=develop"})

# List a directory's contents
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/contents/src"})

# Search repositories
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/search/repositories?q=fastapi+language:python&per_page=5"})

# Search issues/PRs
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/search/issues?q=bug+repo:owner/repo+state:open"})

# Search code
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/search/code?q=def+main+repo:owner/repo"})

# --- GitHub Actions (CI/CD) ---

# List the 5 most recent failed workflow runs on main
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/runs?branch=main&status=failure&per_page=5"})

# Get a single workflow run
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/runs/12345678"})

# List jobs for a run
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/runs/12345678/jobs"})

# Get a single job -- response includes the 'steps' array with per-step status
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/jobs/98765432"})

# List workflows defined in the repo
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/workflows"})

# List runs for a specific workflow (by filename)
tool_call(tool_name="authed_get", arguments={"url": "https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/runs?per_page=5"})
```

**Reading Actions job logs:**

Per-job plain-text logs are served by GitHub as a 302 redirect to a short-lived signed URL on a third-party host, so they are NOT reachable via `authed_get`. Use the dedicated `github_get_job_log` tool instead. By default it downloads the log into the `github-job-logs/` folder inside the conversation workspace as `github-job-logs/github-job-{job_id}.log` (the folder is created automatically) so repeated downloads do not clutter the workspace root. Pass the optional `path` argument to override the destination: if `path` ends with `/` or names an existing directory, the log is placed inside it as `github-job-{job_id}.log`; otherwise `path` is treated as the full destination file path. Absolute paths and `..` traversal are rejected. The tool returns a short preview inline, and falls back to a `response_too_large` error for large logs unless `force_large_response=true` is passed. After downloading, read slices with `get_workspace_file` or grep with `run_python`/`run_script`.

```
# Download a job's log to the default location (github-job-logs/github-job-98765432.log)
tool_call(tool_name="github_get_job_log", arguments={"owner": "owner", "repo": "repo", "job_id": "98765432"})

# Download into a custom directory (log ends up at logs/ci/github-job-98765432.log)
tool_call(tool_name="github_get_job_log", arguments={"owner": "owner", "repo": "repo", "job_id": "98765432", "path": "logs/ci/"})

# Download with an explicit filename
tool_call(tool_name="github_get_job_log", arguments={"owner": "owner", "repo": "repo", "job_id": "98765432", "path": "logs/failed-build.log"})
```

**Write Operations (Require Approval):**

GitHub writes are action requests created with the `create_action_request` tool: the user sees a card describing the exact change and it only happens once they approve. Do NOT attempt a direct POST/PATCH -- `authed_get` is GET-only. `create_action_request` is top-level only -- if you are running as a sub-agent, do not call it; return the proposed `request_type` and `params` to the parent via `agent_task_response` instead.

Each request is checked against GitHub when you propose it; a wrong repo, workflow, ref or issue number comes back immediately as `Invalid parameters` (no card), so look the target up with `authed_get` first.

Action request type: `github_trigger_workflow` -- run a GitHub Actions workflow (a `workflow_dispatch` event).

Parameters:
- `owner`, `repo` (required): The repository.
- `workflow` (required): The workflow's file name (e.g. `deploy.yml`) or numeric id -- NOT its display name. Find it via `/repos/{owner}/{repo}/actions/workflows` (`path` / `id`); the approval card shows the workflow's display name for you.
- `ref` (optional): Branch or tag to run on (not a commit SHA). Defaults to the repository's default branch.
- `inputs` (optional): Object of workflow input values, e.g. `{"environment": "staging", "dry_run": true}`. Max 25 inputs; values are sent as strings (booleans as `true`/`false`). Omitted inputs take the workflow's defaults.

Before proposing a run with inputs, read the workflow file (`/repos/{owner}/{repo}/contents/.github/workflows/{file}`) and use exactly the input names declared under `on.workflow_dispatch.inputs` (respecting `required`, `type` and `options`) -- GitHub rejects unknown or missing required inputs when the user approves. Only workflows with a `workflow_dispatch` trigger can be run.

The result carries the new run's `run_id` and `url` when GitHub returns them; follow the run with `/repos/{owner}/{repo}/actions/runs/{run_id}` and its jobs.

Action request type: `github_comment_on_issue` -- post a comment on an issue or on a pull request's conversation tab.

Parameters:
- `owner`, `repo` (required): The repository.
- `issue_number` (required): The issue or pull request number.
- `body` (required): The comment text, GitHub-flavored markdown (max 65,536 characters). It is posted exactly as shown on the card, under the user's own GitHub account.

Action request type: `github_set_issue_state` -- close or reopen an issue (pull requests are refused).

Parameters:
- `owner`, `repo` (required): The repository.
- `issue_number` (required): The issue number.
- `state` (required): `closed` or `open` (reopen).
- `state_reason` (optional, only with `closed`): `completed` (default) or `not_planned`.

To close an issue with an explanation, issue the `github_comment_on_issue` and `github_set_issue_state` requests in the same response so the user gets both cards at once.

```
# Run the deploy workflow on main with inputs
create_action_request(request_type="github_trigger_workflow", params={"owner": "owner", "repo": "repo", "workflow": "deploy.yml", "ref": "main", "inputs": {"environment": "staging"}}, reasoning="Deploy the fix to staging as requested")

# Comment on issue (or PR) #42
create_action_request(request_type="github_comment_on_issue", params={"owner": "owner", "repo": "repo", "issue_number": 42, "body": "Fixed in #57 -- please retest."}, reasoning="Tell the reporter the fix has landed")

# Close issue #42 as not planned
create_action_request(request_type="github_set_issue_state", params={"owner": "owner", "repo": "repo", "issue_number": 42, "state": "closed", "state_reason": "not_planned"}, reasoning="The user decided not to pursue this")
```

**Important Notes:**
- Requires GitHub to be connected in Settings > Data Connections.
- Read-only via `authed_get`: only GET paths on the allow-list in the GitHub plugin's `api.github.com` service entry are reachable. The only writes are the three action request types above; everything else (creating or editing issues and PRs, labels, pushes, merges, re-running or cancelling runs) is not exposed.
- Rate limit: 5,000 requests/hour for authenticated users.
- The `/repos/{owner}/{repo}/contents/{path}` endpoint returns base64-encoded content for files under 1MB. For files 1-100MB, use the `download_url` from the response. Files over 100MB are not retrievable via the contents endpoint.
- The issues endpoint also returns pull requests (GitHub models PRs as issues). Use the `pulls` endpoint for PR-specific data.
- Pagination: maximum `per_page=100`; GitHub silently caps larger values.
- Stargazers are listed oldest star first, each with a full user object (~1 KB), so any page with more than a couple of users exceeds the inline size limit: pass `output_file` and pull out `login` (and `starred_at`) with `run_python`. The repo's `stargazers_count` (from `/repos/{owner}/{repo}`) tells you how many pages to fetch.
- Search queries support GitHub's search syntax (qualifiers like `repo:`, `language:`, `state:`, `author:`, `is:pr`, `is:issue`).
- For repositories inside organizations that require third-party OAuth app approval, the org admin must approve the Quest OAuth app before its repos are accessible.
- GitHub Actions: listing workflows/runs/jobs and reading per-job logs is supported, and a workflow can be run via the `github_trigger_workflow` action request; re-running, canceling runs and deleting logs are intentionally NOT exposed. Run-level zip log downloads (`/actions/runs/{run_id}/logs`) are also intentionally not surfaced in this iteration -- use per-job logs via `github_get_job_log` instead.
- GitHub Actions endpoints use the same OAuth scope as the rest of the GitHub integration (`repo`); no extra permission is needed.
- A write that fails on approval with a 403/404 usually means the GitHub connection lacks write access to that repository (a GitHub App without the Actions / Issues / Pull requests write permission or not installed on the repo, or an organization that has not approved the OAuth app) -- tell the user rather than retrying.
