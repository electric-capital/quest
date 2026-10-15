# Google Cloud (GCP) API Documentation

This document describes how Quest reads Google Cloud Platform resources (projects, organizations, IAM policies, Compute Engine VMs/disks, GKE clusters, Cloud Logging, and Cloud Billing accounts, pricing and budgets) read-only via the `authed_get` tool and its POST-capable sibling `authed_post`, plus the single approval-gated write: hard-resetting a stuck Compute Engine VM via the `reset_gcp_instance` action request.

## Overview

GCP reads ride on the existing per-user Google Services OAuth credentials -- the same Bearer token used by Gmail/Calendar/Drive/Docs/Sheets/Slides/Tasks -- so there is no separate connection step or Settings entry.

Most GCP reads are plain GET requests made via `authed_get`. The reads Google exposes only as POST (search organizations, read log entries, project-level IAM policy) go through `authed_post`, the POST-capable sibling registered alongside `authed_get` (see [Authenticated External API Requests](../architecture/gemini-api.md#authenticated-external-api-requests-authed_get)).

Six GCP hosts are registered in `_SERVICE_REGISTRY`, all sharing the Google Services loader/injector with `requires_user: True` and `retry_on_401: True`. Access is kept strictly **read-only at the application layer** by the per-host GET/POST allow-lists; the OAuth scope itself is the full `cloud-platform` scope (see Design Decisions).

The backend documentation lives in the gated `system:gcp` system skill rather than always-on prompt text.

## Key Files

| File | Description |
|------|-------------|
| `api/gcp.py` | `get_instructions(base_url)` for the `system:gcp` skill content (the six base-URL constants, per-API path tables, the GET-vs-`authed_post` split, the required `{project}` parameter, the re-consent + API-enablement notes, orphaned-disk guidance, the billing "no spend data" and permissions notes, size-gate / `output_file` guidance, example calls); no endpoint functions -- reads use `authed_get` / `authed_post` |
| `chat/action_request_types/reset_gcp_instance.py` | `ResetGcpInstanceHandler` -- the approval-gated `reset_gcp_instance` action request (hard-reset a stuck VM via the Compute `instances.reset` POST verb); proposal-time instance fetch + status/machine-type preview injection, `cloud-platform` scope pre-check at execute |
| `chat/gemini_api/authed_get.py` | The six GCP `_SERVICE_REGISTRY` entries (`cloudresourcemanager`, `compute`, `container`, `logging`, `cloudbilling`, `billingbudgets` `.googleapis.com`) with `allowed_endpoints` (GET) and, on three of them, the independent `allowed_post_endpoints`; the generalized `_make_authed_request()` (method + `json_body`, verb-scoped allow-list validation) and `handle_authed_post()` / `authed_post_endpoint()` |
| `chat/gemini_api/tool_dispatch.py` | `authed_post` dispatch branch wiring `body`, `force_large_response`, `output_file` into `handle_authed_post()` |
| `chat/llm/tool_schemas.py` | `authed_post` `TOOL_CALL_REGISTRY` entry; `authed_get` host list and `system:*` skill list extended with Google Cloud / `system:gcp` |
| `chat/system_skills/catalog.py` | Registers `system:gcp` (gated `requires="google_services"`) with content from `api.gcp.get_instructions`; sandbox-snippet note for the `/api/authed-post` proxy |
| `auth/config.py` | `cloud-platform` scope added to `GOOGLE_SERVICE_SCOPES` |
| `auth/google_services.py` | Google callback now stores **granted** (not requested) scopes |
| `quest.py` | `POST /api/authed-post` proxy route registration |
| `chat/route_dispatch.py` | `/api/authed-post` added to `_BLOCKED_PROXY_PATHS` |

## Authentication

GCP reads require the user to have connected Google Services; the six entries share the `_load_google_services_credentials` loader and `_inject_google_bearer_auth` injector with the other Google services, with `requires_user: True` and `retry_on_401: True`.

**OAuth scope required** (added to `GOOGLE_SERVICE_SCOPES` in `auth/config.py`):
- `https://www.googleapis.com/auth/cloud-platform` -- the **full** platform scope, not a read-only variant. The Cloud Billing and Billing Budget APIs accept it as well, so billing reads add no scope of their own (and no extra re-consent beyond the one below).

Because it is a new scope, **existing users must re-consent** to the Google Services OAuth grant before GCP requests will succeed; until re-consent, requests fail with a 403 scope/permission error (`ACCESS_TOKEN_SCOPE_INSUFFICIENT`).

Re-consent is detected by the `/connectors` `needs_reauth` check, which compares the stored scope set against `GOOGLE_SERVICE_SCOPES`.

The Google callback in `auth/google_services.py` now persists the scopes Google **actually granted** (`credentials.granted_scopes`, falling back to the requested set only when Google returns no `scope` field) rather than the requested set, so a partial grant (user de-selected a scope on the consent screen) cannot leave a connector showing "Connected" while calls 403.

## GCP Read Access

GCP reads use the full API URL against six hosts, each gated by `allowed_endpoints` (GET) regexes in `chat/gemini_api/authed_get.py` -- non-matching paths are rejected. The id segments use `[^/:]+` (not `[^/]+`) to exclude `:`, which is how GCP encodes custom/write methods (e.g. `instances/{id}:stop`, `clusters/{id}:setMasterAuth`), keeping every GET path read-only. See the registry entries for the exact pattern lists and `api/gcp.py` (`get_instructions()`) for the LLM-facing path tables and example calls.

| Host | API | Capabilities (read-only) |
|------|-----|--------------------------|
| `cloudresourcemanager.googleapis.com` | Cloud Resource Manager | List/get projects (v1 + v3), get folder, get org by id (GET); search/list organizations and project-level IAM policy (`projects/{id}:getIamPolicy`, v1 + v3) via `authed_post` |
| `compute.googleapis.com` | Compute Engine | VM instances (per-zone, per-instance, aggregated-across-zones; per-instance IAM policy via the slash-style `instances/{id}/getIamPolicy` GET), disks (per-zone, per-instance, aggregated-across-zones for orphaned-disk scans), zones, regions |
| `container.googleapis.com` | Kubernetes Engine (GKE) | List/get clusters and node pools (`location` may be `-` for all locations) |
| `logging.googleapis.com` | Cloud Logging | List/get log names, sinks, log-based metrics (GET); read log **entries** via `authed_post` (`entries:list`) |
| `cloudbilling.googleapis.com` | Cloud Billing | List/get billing accounts, their linked projects and subaccounts, an org's accounts, a project's `billingInfo`; the public service/SKU price catalog (v1) and account-specific SKU prices (v1beta) (GET); billing-account IAM policy via `authed_post` (`billingAccounts/{id}:getIamPolicy`) |
| `billingbudgets.googleapis.com` | Cloud Billing Budgets | List/get the budgets defined on a billing account |

Cloud Billing is accounts, links, IAM, prices and budget definitions only -- the API has no spend or cost-history endpoint (that data lives in the BigQuery billing export, which Quest does not query), and a budget object carries its amount and thresholds, not the spend against them. The skill says so explicitly so the model does not try to reconstruct cost history from SKU prices. Seeing an account requires at least Billing Account Viewer on it; the price catalog is public.

### POST Reads (via `authed_post`)

A few GCP reads exist only as POST verbs. They are reachable only through `authed_post` and only via the independent `allowed_post_endpoints` allow-list on their host (a GET can never reach them and vice-versa -- see [authed_post](../architecture/gemini-api.md#post-capable-sibling-authed_post)):

- `cloudresourcemanager.googleapis.com` -- `POST /v1/organizations:search` (optional `{"query": "..."}` body; empty body lists all visible orgs; the canonical way to discover org ids).
- `cloudresourcemanager.googleapis.com` -- `POST /v1/projects/{id}:getIamPolicy` and `POST /v3/projects/{id}:getIamPolicy` (project-level IAM policy: who has which roles on the project). POST-but-read-only: the request body schema is `GetIamPolicyRequest`, whose only field is `options` (a `requestedPolicyVersion` hint), so the call cannot carry a policy and cannot mutate -- it returns one. The mutating counterpart is the separate `:setIamPolicy` literal, which the anchored regexes never match (org-, folder-, and `testIamPermissions` verbs are also not allow-listed).
- `logging.googleapis.com` -- `POST /v2/entries:list` (body with `resourceNames`, `filter`, `orderBy`, `pageSize`; the only POST verb allow-listed for this host -- `entries:write` and other write verbs are unreachable).
- `cloudbilling.googleapis.com` -- `POST /v1/billingAccounts/{id}:getIamPolicy` (who is a billing admin/viewer/user on the account; same `GetIamPolicyRequest` reasoning as the project verb). The only POST verb on this host: `:setIamPolicy`, `:testIamPermissions`, `:create`, `:move` and the v1beta `:estimateCostScenario` are not allow-listed.

Instance-level IAM policy on a VM is different: Compute Engine encodes `getIamPolicy` as a plain **GET** path segment (`/compute/v1/projects/{p}/zones/{z}/instances/{i}/getIamPolicy`, no `:`), so it rides on the normal `authed_get` allow-list; the mutating `setIamPolicy` is a POST on a different segment and stays unreachable. The `system:gcp` skill notes that project-level IAM is where bindings usually live (instance-level policies are often empty) and that neither call shows bindings inherited from the folder/org above the project.

### Required `{project}` Parameter and API Enablement

Most GCP paths require a GCP **project id** (not the numeric project number). The skill directs the model to list projects (`GET /v1/projects`) or ask the user when it is unknown. Billing paths take a **billing account id** (`012345-6789AB-CDEF01`) instead, discovered via `GET /v1/billingAccounts` or a project's `billingInfo`. The relevant GCP API must also be enabled on the target project, or Google returns a 403 "API not enabled" error -- a project-side setting, not a Quest issue. Both notes are carried in `api/gcp.py` (`get_instructions()`).

## Write Operations (`reset_gcp_instance` action request)

The one supported GCP write is hard-resetting a stuck Compute Engine VM. It does **not** go through the `authed_get`/`authed_post` allow-lists (those stay strictly read-only; `:`-suffixed verbs like `:reset` are unreachable there). Instead it is the approval-gated `reset_gcp_instance` action request, handled by `ResetGcpInstanceHandler` in `chat/action_request_types/reset_gcp_instance.py` (core type `ActionRequestType.RESET_GCP_INSTANCE` in `db/models.py`, registered in `chat/action_request_types/__init__.py`) -- nothing is sent to Google until the user clicks the card's **Reset** button. See [Action Requests](../architecture/action-requests.md) for the generic approval mechanics.

- **Params:** `project` (GCP project id), `zone` (e.g. `us-central1-a`), `instance` (VM name). All required strings, format-checked (RFC1035 names; project ids admit legacy domain-scoped `example.com:proj` shapes) so the values are URL-path-safe.
- **Proposal-time validation** (`validate_against_upstream`): fetches the live instance (`fields=name,status,machineType`) over the user's Google OAuth token. A 404 (typo'd project/zone/instance) and a powered-off instance (`TERMINATED`/`SUSPENDED` -- reset only applies to a machine that is on) reject same-turn with no card; transient failures (network, 401/403, 5xx) log and defer to execute. On success the live `status` and short machine type are injected as server-only params (`instance_status`, `machine_type`, not in the model-suppliable allow-list) for the preview card.
- **Preview card:** Instance / Zone / Project rows, the live status and machine type when resolved, and a fixed "Effect" row spelling out that a reset is a hard power-cycle (unsaved in-memory data lost, disks untouched).
- **Execute:** pre-checks the Google connection and the `cloud-platform` scope (reauth message on a stale grant), then `POST {COMPUTE_BASE}/compute/v1/projects/{p}/zones/{z}/instances/{i}/reset` via `make_authenticated_request` (401-refresh built in). Returns the Compute operation name/status; Google-side rejections surface via `_extract_google_api_error`.
- **Model-facing spec** lives in the `system:gcp` skill's "GCP Write Operations" section (`api/gcp.py`), including the sub-agent escape-hatch reminder and guidance to read the instance first and to prefer an in-guest reboot when the OS still responds.

Tests: `tests/test_reset_gcp_instance_handler.py`.

## System Skill

GCP usage instructions are surfaced through the gated `system:gcp` skill (`requires="google_services"`), registered in `chat/system_skills/catalog.py` with its content built from `api/gcp.py` (`get_instructions()`). The skill is loadable only when the user has connected Google Services. The `authed_get`/`authed_post` tool schemas in `chat/llm/tool_schemas.py` and the backend prose in `chat/gemini_api/system_prompt.py` name Google Cloud and point the model at `system:gcp`. See [Skill Library](../architecture/skill-library.md).

## Response Size

Aggregated instance/disk lists, full cluster objects, log pulls, and SKU catalogs (thousands of entries per service) commonly exceed the ~3 KB `authed_get`/`authed_post` size gate. The skill directs the model to trim GET responses with the Google `fields=` query parameter and, for large payloads (logs, cluster lists, SKU lists), to pass `output_file` so the body is written under the hidden `.responses/` workspace subdirectory and bypasses the gate. GCP list endpoints paginate (`maxResults` + `pageToken` for Compute; `pageSize` + `pageToken` for the others). See [Large Response Protection](../architecture/gemini-api.md#large-response-protection).

## Constraints

- **Read-only (app-enforced):** only the allow-listed GET paths and the read-shaped POST verbs (`organizations:search`, `entries:list`, project and billing-account `:getIamPolicy`) are reachable; `:`-suffixed write verbs (incl. `:setIamPolicy`) are excluded by the `[^/:]+` id-segment convention and by the absence of write verbs from the POST allow-lists. The OAuth scope is full `cloud-platform`, so read-only is enforced by the allow-lists, **not** the token. The sole write path is the approval-gated `reset_gcp_instance` action request, which lives outside `authed_get`/`authed_post` entirely and executes only on user Approve.
- **Re-consent required:** the new `cloud-platform` scope requires existing users to reconnect Google Services; until then GCP calls 403 with a scope error.
- **`{project}` id required:** most paths need a project id; list projects or ask the user first.
- **API enablement:** the target GCP API must be enabled on the project or Google returns a 403 "API not enabled".
- **No spend data:** Cloud Billing exposes accounts, links, IAM, prices and budget definitions; cost history is BigQuery-export-only and out of reach.
- **Size gate:** large GCP responses must be trimmed with `fields=` (GET) or streamed via `output_file`.

## Design Decisions

**Why the full `cloud-platform` scope instead of a read-only variant?**
Compute Engine rejects `cloud-platform.read-only` and the GKE/Container API has no read-only scope at all, so the full `cloud-platform` scope is the only one that works across all the GCP APIs. Cloud Billing has its own `cloud-billing.readonly` scope, but it also accepts `cloud-platform`; requesting the narrower scope on top would add a consent prompt without narrowing anything the allow-lists do not already narrow. Read-only behavior is therefore enforced at the application layer by the GET/POST allow-lists, which permit only whitelisted read-shaped paths -- never by the OAuth scope. See the rationale comment in `auth/config.py`.

**Why store granted (not requested) OAuth scopes at the callback?**
The `needs_reauth` connector check compares stored scopes against `GOOGLE_SERVICE_SCOPES`. Recording the requested set verbatim would mask a partial grant (user de-selecting a scope on the consent screen), leaving a connector showing "Connected" while API calls 403 with `ACCESS_TOKEN_SCOPE_INSUFFICIENT` -- exactly the GCP symptom. Persisting `credentials.granted_scopes` makes the partial grant visible so re-consent is prompted.

**Why a separate `authed_post` tool instead of relaxing `authed_get`?**
GCP needs a small number of read-shaped POST verbs. Rather than let `authed_get` issue POSTs (which would blur the read-only GET surface), `authed_post` carries POSTs through an **independent** `allowed_post_endpoints` allow-list, strictly separate from the GET allow-list, so a GET can never reach a POST-only path and vice-versa. It is the POST-capable sibling of `authed_get`, not a general POST proxy. See [authed_post](../architecture/gemini-api.md#post-capable-sibling-authed_post).

**Why `authed_get`/`authed_post` instead of dedicated proxy endpoints?**
GCP reads are standard Google API requests. Reusing `authed_get`/`authed_post` inherits the shared credential injection, hostname matching, 401 retry, allow-list path gating, size gate, and `output_file` support, and aligns GCP with Google Calendar, Drive, Slides, and the other Google services.
