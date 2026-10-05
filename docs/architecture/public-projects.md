# Public Projects Architecture

This document describes **public projects**: a project mode chosen at creation time that inverts Quest's normal security posture. Conversations in a public project get an **internet-enabled script sandbox** and are **cut off from every internal resource** (skills, memories, connectors, action requests, sub-agents, authed APIs), so a public project can never become a data-exfiltration path for internal data.

## Overview

The flag is a single immutable boolean on the project row (`projects.public`, see [Database Architecture](database.md)). Every conversation path that carries a `project_id` inherits the gate automatically because `run_conversation_turn` fetches the project row each turn anyway. There is no per-conversation state and no conversion path: a project is public or private forever from creation.

The restriction is enforced at three independent layers — the trimmed tool schema is a convenience for the model, not the boundary:

1. **Tool tier** — `PUBLIC_TOOLS` in `chat/llm/tool_schemas.py`: only `tool_call`, `run_script`, `run_python`.
2. **Dispatch allowlist** (the security boundary) — `PUBLIC_TOOL_CALL_ALLOWLIST` in `chat/llm/tool_schemas.py`, hard-enforced at the top of `_dispatch_tool_call_inner()` in `chat/gemini_api/tool_dispatch.py` (`is_public` kwarg), plus loop-arm rejects in the conversation loop (`_PUBLIC_BLOCKED_LOOP_TOOLS` in `chat/gemini_api/turn_tools.py`, enforced before handler dispatch in `chat/gemini_api/conversation.py`: `agent_task*`, `create_action_request`, `wait_for_handles` via `tool_call`, and origin-specific tools).
3. **Sandbox profile** — a separate podman image with inverted networking and **no credential injection** (see below).

## Server-global feature gate

Public projects sit behind the `public_projects` [feature gate](feature-gates.md) (`FEATURE_PUBLIC_PROJECTS` in `config/feature_gates.py`), off by default. The gate supports **per-user access** (`PER_USER_ACCESS_FEATURES`): an admin can open it to all users or restrict it to an `allowed_users` email list from Settings > Features; every check below is per-user (`is_feature_enabled_for_user()` with the acting user's email), so for a user outside the list the gate behaves exactly as if it were closed.

While the gate is closed for a user: creating a public project 400s (`public_projects_disabled`), the New Project modal hides the checkbox (via the per-user `enabled_features` on `GET /me`), **existing public projects vanish from their UI** — dropped from `GET /projects` and 404ing on every by-id endpoint via `_get_visible_project()` in `chat/project_routes.py`, with their [Quest Docs](quest-docs.md) likewise 404ing (missing-doc body) on every `/app/api/docs/{id}` route via `_get_doc_for_ui()` in `chat/docs/routes.py` — and `run_conversation_turn` refuses new turns in their conversations with a durable disabled error. Nothing is deleted (a hidden project cannot even be deleted); restoring access (reopening the gate or re-adding the user) brings everything back.

## The flag (creation & API surface)

- `db/models.py` `Project.public`; created via `create_project(..., public=...)` in `db/project_store.py`; emitted by `_project_to_dict()` so every project endpoint returns it. `update_project()` never reads it → immutable.
- `POST /app/api/projects` accepts `public: bool = False` (`CreateProjectRequest` in `chat/project_routes.py`). `POST /projects/from-conversation` deliberately has **no** `public` field: converting moves an existing (potentially internal-data-bearing) workspace into the project, so public projects start only with an empty workspace.
- **Routines only behind a second gate**: public projects have no routines unless the `public_project_routines` [feature gate](feature-gates.md) (off by default, per-user capable, only effective together with `public_projects`) is open for the user -- a scheduled routine runs unattended in the internet-enabled sandbox. While closed, the routine and schedule endpoints (`chat/routine_routes.py` `get_routine_project()`, shared by `chat/schedule_routes.py`) return 400 `public_project_routines_disabled`, the routine list is empty, one-click runs are refused, the scheduler skips the project's schedules and `run_conversation_turn` refuses turns in routine-created conversations; rows are kept. While open, a routine there is prompt + model + schedule only: its runs are ordinary public conversations (no skill auto-loads -- enabling one 400s `public_project_no_skills` -- and no guide override applied), and the agent-side `create_routine`/`edit_routine` action requests stay unavailable (public conversations have no action requests; the pre-card and execute guards still reject public projects).
- **No project skills**: the write endpoints in `chat/project_skill_routes.py` reject public projects (400 `public_project_no_skills`, shared `_reject_public_project()` helper); list/read endpoints return empty. The `create_skill`/`edit_skill` action-request path is guarded twice — pre-card in `chat/action_request_types/skill_precard.py` and at execute time in `create_skill.py`/`edit_skill.py` — because a **private** conversation could otherwise target a skill at a public project.

## Runtime gating (per turn)

`run_conversation_turn` in `chat/gemini_api/conversation.py` derives `is_public` from the already-fetched project row (zero extra queries). When set:

- **Skills**: all three autoload tiers and composer-loaded `skill_ids` are skipped (skill bodies are internal data).
- **System prompt**: `get_public_project_system_prompt()` in `chat/gemini_api/system_prompt.py`. Keeps user identity, the project instructions (`projects.guide`), and docs for the public tool subset; drops the proxy preamble (which embeds the user's real API key), guide snapshots/custom prompts, all skills, memory prose, the system-skills enumeration, and connector docs. The dynamic-tools section is filtered to the allowlist. Routine runs (`is_routine`) swap the first-reply naming step for the routine note and drop `set_conversation_name`, as in the regular prompt.
- **Tool tier**: `PUBLIC_TOOLS` selected in the session-tools chain.
- **Dispatch**: `is_public=True` threaded into `_dispatch_tool_call()`, which hard-rejects anything outside the allowlist and passes `public=True` to the script handlers (the per-run sandbox token is still leased but never injected, see [Script Runner Architecture](script-runner.md#sandbox-tokens)).

The allowlist: `get_current_time`, the four workspace file tools, `set_conversation_name`, `get_response_content`, `project_db_query` (project-local data only), and `send_slack_dm_to_self` — the one connector write allowed in public projects, because it is outbound-only to a fixed recipient (the user themselves): it reads no message history or other internal data, and only sends model-composed text plus workspace files (which are already public-project accessible). The public system prompt's Boundaries block names it as the single connector exception.

The seven [Quest Docs](quest-docs.md) tools (`list_docs`, `search_docs`, `read_doc`, `create_doc`, `edit_doc`, `append_to_doc`, `add_doc_image`) are allowlisted too. Docs are mode-partitioned. In a public conversation the access rule (`resolve_doc_access` with `is_public=True`) hides every private doc, so a private doc id behaves exactly like a nonexistent one, and the conversation creates and writes only public docs, which by construction hold sandbox-originated content. Public writes are never approval-gated, because a public doc's verdict there is free or denied, which fits with `create_action_request` staying blocked. The tools and the prompt's "Quest Docs" paragraph appear only while the `docs` gate is open for the user (the `docs_enabled` argument of `get_public_project_system_prompt()`).

## Public sandbox profile (two separate images)

There are two podman images, each with its own Dockerfile + entrypoint, so the tooling inside can be customized independently (see [Script Runner Architecture](script-runner.md)):

| | Restricted (default) | Public |
|---|---|---|
| Image | `quest-script-runner-<mode>` (`Dockerfile.script-runner` + `script-runner-entry.sh`) | `quest-script-runner-public-<mode>` (`Dockerfile.script-runner-public` + `script-runner-entry-public.sh`) |
| Network | `slirp4netns:allow_host_loopback=true,outbound_addr=127.0.0.1,enable_ipv6=false` — no egress, no DNS; host reachable at 10.0.2.2 for the socat proxy bridge | `slirp4netns:allow_host_loopback=false,enable_ipv6=false` — internet + DNS work (`--dns=10.0.2.3 --dns-search=.` pins the slirp resolver so the host's resolv.conf never leaks in); host loopback unmapped |
| IPv6 | disabled in both: `enable_ipv6=false` on the slirp network + `--sysctl net.ipv6.conf.all.disable_ipv6=1` (no IPv6 stack in the container netns) + a fail-closed `ip6tables -A OUTPUT -j REJECT` in the entrypoint. Every other row here is IPv4-only, and slirp's default IPv6 stack (ULA address, default route, host loopback at `fd00::2`) bypassed all of it | same |
| Credentials | `QUEST_API_KEY` (ephemeral per-run sandbox token, `chat/sandbox_tokens.py`) + `QUEST_PORT` injected | **neither injected** — the load-bearing change: even if a network hole existed, the container has no credential for any internal API |
| iptables (entrypoint, before `setpriv` privilege drop) | allow only 10.0.2.2:proxy-port, reject other host access; fails closed without `iptables`/`ip6tables` | allow DNS to 10.0.2.3, then REJECT RFC1918 (10/8, 172.16/12, 192.168/16), link-local 169.254/16 (cloud metadata service!), and CGNAT 100.64/10 |

The argv builder is shared and unit-tested: `_build_script_podman_cmd(..., public=...)` in `chat/gemini_api/tool_handlers/sandbox.py`; image names in `chat/gemini_api/constants.py` (`get_public_script_runner_image()`). Both images are auto-built/rebuilt at startup by `run.py` (rebuild triggers on Dockerfile **or** entrypoint mtime).

The `/api/tool-call` script bridge (`chat/gemini_api/script_tool_call.py`) is moot in public containers: it requires the sandbox token that public containers never receive.

## Frontend

- `public` on the `Project` type (`frontend/src/api/types.ts`); checkbox in `NewProjectModal.tsx`.
- Globe badge on the project row + a "Public" chip in the drill-down header (`Sidebar.tsx`); the Routines section is hidden in the drill-down for public projects unless `public_project_routines` is in the per-user `enabled_features`. `NewRoutineModal.tsx` / `RoutineSettingsModal.tsx` take an `isPublicProject` prop: the model list is the public-allowed one, and the guide override picker and the Skills section are hidden.
- `ProjectSettingsModal.tsx`: Skills section hidden, read-only public note in General.
- `ChatPanel.tsx` fetches the project when the conversation reveals a `project_id` and passes `isPublicProject` to `<Composer>`. The prop drives a persistent amber warning banner above the input (`.public-project-warning`, desktop and mobile layouts alike) reminding the user that the conversation has internet access and anything typed may be sent to third-party websites, so private/sensitive information must stay out; it also hides the composer's Skill button (the backend ignores `skill_ids` for public conversations regardless). The banner is rendered by the shared `<Composer>`, so any host that composes into a public project shows it by passing the prop. `HomeComposer.tsx` passes the prop too (from the drilled project's `public` flag) so a public project's first chat gets the same treatment. The prop also selects the model menu's visibility: public conversations get their own admin-curated top level and only models the admin allows for public conversations are offered (Settings > Model Selection, whose public columns appear only while this gate is on -- see [Model Selection](model-selection.md); `run_conversation_turn` enforces the same rule per turn).

## Testing

`tests/test_public_projects.py`: podman argv builder profiles (credential withholding, network flags, image selection), dispatch allowlist rejects, loop blocklist coverage, public prompt leak checks, and store-level flag immutability. `tests/test_public_project_routines.py`: the `public_project_routines` gate across the routine/schedule routes, one-click runs, the scheduler, and the routine-run prompt.

## Out of scope (v1)

Provider-native web search/fetch tools and their cost tracking (devplan phases 4–5), "public-approved" authed services, file transfer between public and private workspaces, public↔private conversion, and sub-agents in public mode.
