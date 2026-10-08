# Public Projects Architecture

This document describes **public projects**: a project mode chosen at creation time that inverts Quest's normal security posture. Conversations in a public project get an **internet-enabled script sandbox** and are **cut off from every internal resource** (skills, memories, connectors, action requests, sub-agents, authed APIs), so a public project can never become a data-exfiltration path for internal data.

## Overview

The flag is a single immutable boolean on the project row (`projects.public`, see [Database Architecture](database.md)). Every conversation path that carries a `project_id` inherits the gate automatically because `run_conversation_turn` fetches the project row each turn anyway. There is no per-conversation state and no conversion path: a project is public or private forever from creation.

The restriction is enforced at three independent layers — the trimmed tool schema is a convenience for the model, not the boundary:

1. **Tool tier** — `PUBLIC_TOOLS` in `chat/llm/tool_schemas.py`: only `tool_call`, `run_script`, `run_python`.
2. **Dispatch allowlist** (the security boundary) — `PUBLIC_TOOL_CALL_ALLOWLIST` in `chat/llm/tool_schemas.py`, hard-enforced at the top of `_dispatch_tool_call_inner()` in `chat/gemini_api/tool_dispatch.py` (`is_public` kwarg), plus loop-arm rejects in the conversation loop (`_PUBLIC_BLOCKED_LOOP_TOOLS` in `chat/gemini_api/turn_tools.py`, enforced before handler dispatch in `chat/gemini_api/conversation.py`: `agent_task*`, `create_action_request`, `wait_for_handles` via `tool_call`, and origin-specific tools).
3. **Sandbox profile** — a separate podman image with inverted networking and **no credential injection** (see below).

## A default feature (no gate)

Public projects are available to every user on every install: there is no feature gate and no admin switch. The New Project modal always offers the "Public project" checkbox, `POST /projects` always accepts `public: true`, and public projects are listed and reachable like any other project the user owns. (A `public_projects` gate existed until October 2026; `read_feature_gates()` ignores the stale key an older `feature_gates.json` may still hold and the next write drops it.) The one remaining admin switch in this area is the `public_project_routines` gate below, which covers unattended runs in the internet-enabled sandbox rather than the projects themselves.

## The flag (creation & API surface)

- `db/models.py` `Project.public`; created via `create_project(..., public=...)` in `db/project_store.py`; emitted by `_project_to_dict()` so every project endpoint returns it. `update_project()` never reads it → immutable.
- `POST /app/api/projects` accepts `public: bool = False` (`CreateProjectRequest` in `chat/project_routes.py`). `POST /projects/from-conversation` deliberately has **no** `public` field: converting attaches an existing (potentially internal-data-bearing) conversation and its conversation workspace to the project, so public projects start only empty.
- **Routines only behind a gate**: public projects have no routines unless the `public_project_routines` [feature gate](feature-gates.md) (off by default, per-user capable) is open for the user -- a scheduled routine runs unattended in the internet-enabled sandbox. While closed, the routine and schedule endpoints (`chat/routine_routes.py` `get_routine_project()`, shared by `chat/schedule_routes.py`) return 400 `public_project_routines_disabled`, the routine list is empty, one-click runs are refused, the scheduler skips the project's schedules and `run_conversation_turn` refuses turns in routine-created conversations; rows are kept. While open, a routine there is prompt + model + schedule only: its runs are ordinary public conversations (no skill auto-loads -- enabling one 400s `public_project_no_skills` -- and no guide override applied), and the agent-side `create_routine`/`edit_routine` action requests stay unavailable (public conversations have no action requests; the pre-card and execute guards still reject public projects).
- **No project skills**: the write endpoints in `chat/project_skill_routes.py` reject public projects (400 `public_project_no_skills`, shared `_reject_public_project()` helper); list/read endpoints return empty. The `create_skill`/`edit_skill` action-request path is guarded twice — pre-card in `chat/action_request_types/skill_precard.py` and at execute time in `create_skill.py`/`edit_skill.py` — because a **private** conversation could otherwise target a skill at a public project.

## Runtime gating (per turn)

`run_conversation_turn` in `chat/gemini_api/conversation.py` derives `is_public` from the already-fetched project row (zero extra queries). When set:

- **Skills**: all three autoload tiers and composer-loaded `skill_ids` are skipped (skill bodies are internal data).
- **System prompt**: `get_public_project_system_prompt()` in `chat/gemini_api/system_prompt.py`. Keeps user identity, the project instructions (`projects.guide`), and docs for the public tool subset; drops the proxy preamble (which embeds the user's real API key), guide snapshots/custom prompts, all skills, memory prose, the system-skills enumeration, and connector docs. The dynamic-tools section is filtered to the allowlist. A public conversation always has a project, so the prompt always carries the "Two file spaces" paragraph (this conversation's workspace at `/workspace` vs the project workspace at `/project`, see [Conversation Loop -- Workspace Notice Flags](gemini-api.md#workspace-notice-flags)) plus the legacy note when the conversation's `legacy_shared_workspace` flag is set (the converted note never applies: a public project cannot be created from a conversation); the "files the user uploads ... are fair game" rule covers both spaces. Routine runs (`is_routine`) swap the first-reply naming step for the routine note and drop `set_conversation_name`, as in the regular prompt.
- **Tool tier**: `PUBLIC_TOOLS` selected in the session-tools chain.
- **Dispatch**: `is_public=True` threaded into `_dispatch_tool_call()`, which hard-rejects anything outside the allowlist and passes `public=True` to the script handlers (the per-run sandbox token is still leased but never injected, see [Script Runner Architecture](script-runner.md#sandbox-tokens)).

The allowlist: `get_current_time`, the four workspace file tools (allowlisted so replayed calls still dispatch, but hidden from the public prompt via `PROJECT_HIDDEN_WORKSPACE_TOOLS`, and left out of the "Available dynamic tools" list in the dispatch rejection message), `set_conversation_name`, `get_response_content`, `project_db_query` and the five scheme-qualified file tools (`list_files`, `read_file`, `write_file`, `edit_file`, `copy_file` over `chat://` and `proj://` -- project-local data only, listed in the public and public-routine prompts like in every other project tier, see [Conversation Loop -- Scheme-Qualified File Tools](gemini-api.md#scheme-qualified-file-tools)), and `send_slack_dm_to_self` — the one connector write allowed in public projects, because it is outbound-only to a fixed recipient (the user themselves): it reads no message history or other internal data, and only sends model-composed text plus conversation-workspace files (which are already public-project accessible; project files are copied over with `copy_file` from `proj://` to `chat://` first). The public system prompt's Boundaries block names it as the single connector exception.

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

Both images get the same two mounts in a public-project conversation: the conversation workspace at `/workspace` and the project workspace at `/project`, both read-write (see [Script Runner Architecture](script-runner.md)). The argv builder is shared and unit-tested: `_build_script_podman_cmd(..., public=..., project_dir=...)` in `chat/gemini_api/tool_handlers/sandbox.py`; image names in `chat/gemini_api/constants.py` (`get_public_script_runner_image()`). Both images are auto-built/rebuilt at startup by `run.py` (rebuild triggers on Dockerfile **or** entrypoint mtime).

The `/api/tool-call` script bridge (`chat/gemini_api/script_tool_call.py`) is moot in public containers: it requires the sandbox token that public containers never receive.

## Frontend

- `public` on the `Project` type (`frontend/src/api/types.ts`); checkbox in `NewProjectModal.tsx`.
- Globe badge on the project row + a "Public" chip in the drill-down header (`Sidebar.tsx`); the Routines section is hidden in the drill-down for public projects unless `public_project_routines` is in the per-user `enabled_features`. `NewRoutineModal.tsx` / `RoutineSettingsModal.tsx` take an `isPublicProject` prop: the model list is the public-allowed one, and the guide override picker and the Skills section are hidden.
- `ProjectSettingsModal.tsx`: Skills section hidden, read-only public note in General.
- `ChatPanel.tsx` fetches the project when the conversation reveals a `project_id` and passes `isPublicProject` to `<Composer>`. The prop drives a persistent amber warning banner above the input (`.public-project-warning`, desktop and mobile layouts alike) reminding the user that the conversation has internet access and anything typed may be sent to third-party websites, so private/sensitive information must stay out; it also hides the composer's Skill button (the backend ignores `skill_ids` for public conversations regardless). The banner is rendered by the shared `<Composer>`, so any host that composes into a public project shows it by passing the prop. `HomeComposer.tsx` passes the prop too (from the drilled project's `public` flag) so a public project's first chat gets the same treatment. The prop also selects the model menu's visibility: public conversations get their own admin-curated top level and only models the admin allows for public conversations are offered (Settings > Model Selection -- see [Model Selection](model-selection.md); `run_conversation_turn` enforces the same rule per turn).

## Testing

`tests/test_public_projects.py`: podman argv builder profiles (credential withholding, network flags, image selection), dispatch allowlist rejects, loop blocklist coverage, public prompt leak checks, and store-level flag immutability. `tests/test_public_project_routines.py`: the `public_project_routines` gate across the routine/schedule routes, one-click runs, the scheduler, and the routine-run prompt.

## Out of scope (v1)

Provider-native web search/fetch tools and their cost tracking (devplan phases 4–5), "public-approved" authed services, file transfer between public and private workspaces, public↔private conversion, and sub-agents in public mode.
