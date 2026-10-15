"""Google Cloud Platform (GCP) read-only API instructions for the system prompt.

GCP reads are handled via the ``authed_get`` tool (GET requests) and its
POST-capable sibling ``authed_post`` (for the GCP reads Google only exposes
as POST: searching organizations, reading log entries, and the project- and
billing-account-level IAM policy reads). Both ride on the existing per-user Google Services OAuth
credentials -- the same Bearer token used by
Gmail/Calendar/Drive/Docs/Sheets/Slides/Tasks -- granted the full
``cloud-platform`` scope (Compute Engine and GKE/Container reject the
read-only variant, so the full scope is required). Access is kept strictly
READ-ONLY at the APPLICATION layer, not by the OAuth scope: only the
whitelisted read-shaped GET paths and the read-shaped POST verbs
(``organizations:search``, ``entries:list``, project and billing-account
``:getIamPolicy``) are reachable. ``getIamPolicy`` is read-shaped despite being a POST: its body
carries only ``GetPolicyOptions`` (a policy-version hint) and the verb returns
the policy -- the mutating counterpart is the separate ``:setIamPolicy`` verb,
which the allow-list anchors never match.

The single Compute Engine WRITE -- hard-resetting a stuck VM -- does not go
through authed_get/authed_post at all: it is the approval-gated
``reset_gcp_instance`` action request
(chat/action_request_types/reset_gcp_instance.py), executed only after the
user clicks Approve on the inline card.
"""

RESOURCE_MANAGER_BASE = "https://cloudresourcemanager.googleapis.com"
COMPUTE_BASE = "https://compute.googleapis.com"
CONTAINER_BASE = "https://container.googleapis.com"
LOGGING_BASE = "https://logging.googleapis.com"
BILLING_BASE = "https://cloudbilling.googleapis.com"
BUDGETS_BASE = "https://billingbudgets.googleapis.com"


# ---------------------------------------------------------------------------
# Instruction text for the GCP APIs
# ---------------------------------------------------------------------------

def get_instructions(base_url: str) -> str:
    """Google Cloud API documentation section (accessed via authed_get / authed_post)."""
    return f"""## Google Cloud Platform (via authed_get / authed_post)

Read Google Cloud (GCP) resources using `authed_get` (for GET endpoints) and
`authed_post` (for the GCP reads Google only exposes as POST). Authentication
is handled automatically via the user's connected Google Services account.
**Read-only:** only the GET paths and the POST verbs documented below are
reachable -- no create/update/delete. The single exception is hard-resetting a
stuck VM, which goes through the approval-gated `reset_gcp_instance` action
request (see "GCP Write Operations" below), never through
`authed_get`/`authed_post`.

**Required `{{project}}` parameter:** Most GCP calls need a GCP project id (e.g.
`my-project-123`, NOT the numeric project number for most paths). If you don't
know the project id, either ask the user or call `GET {RESOURCE_MANAGER_BASE}/v1/projects`
first to list the projects the user can see.

**Re-consent note:** GCP requires the full `cloud-platform` OAuth scope (Compute
Engine and GKE reject the read-only variant, so the full scope is needed; access
stays read-only because only the read paths below are reachable). If a call
returns a 403 with a scope/permission error, the user must reconnect Google
Services in Settings > Data Connections to grant the `cloud-platform` scope.

**API enablement note:** The relevant GCP API (Compute Engine, Kubernetes
Engine, Cloud Logging, Resource Manager, Cloud Billing, Cloud Billing Budget)
must be enabled on the target project. If it isn't, Google returns a 403 "API
not enabled" error -- surface that to the user; it is a project-side setting,
not a Quest issue.

### Cloud Resource Manager (projects & organizations)

**Base URL:** `{RESOURCE_MANAGER_BASE}`

| Path | Tool | Description |
|------|------|-------------|
| `/v1/projects` | `authed_get` | List projects the caller can see (supports `filter=`) |
| `/v1/projects/{{projectId}}` | `authed_get` | Get one project (v1) |
| `/v3/projects/{{projectId}}` | `authed_get` | Get one project (v3 resource shape) |
| `/v3/folders/{{folderId}}` | `authed_get` | Get one folder (context) |
| `/v1/organizations/{{orgId}}` | `authed_get` | Get one organization by id |
| `/v1/organizations:search` | **`authed_post`** | **Search/list organizations** (optional `{{"query": "..."}}` body; empty body lists all visible orgs) |
| `/v1/projects/{{projectId}}:getIamPolicy` | **`authed_post`** | **Project-level IAM policy** (who has which roles on the project; empty body, or `{{"options": {{"requestedPolicyVersion": 3}}}}` to see conditional bindings) |
| `/v3/projects/{{projectId}}:getIamPolicy` | **`authed_post`** | Project-level IAM policy (v3 shape, same semantics) |

To discover org ids, use the `authed_post` `organizations:search` call (an empty
body lists every org the caller can see). Once you know an org id, `authed_get`
`GET /v1/organizations/{{orgId}}` fetches it.

**Who has access to a project/VM:** IAM bindings are usually set at the project
level, so `POST /v1/projects/{{projectId}}:getIamPolicy` answers "who can access
this project (and by inheritance its VMs)". For instance-level bindings on one
VM, additionally use the Compute Engine `getIamPolicy` GET below (often empty --
instance-level IAM is rare). Neither call shows bindings inherited from the
folder/organization above the project.

### Compute Engine (VM instances & disks)

**Base URL:** `{COMPUTE_BASE}`

| Path | Tool | Description |
|------|------|-------------|
| `/compute/v1/projects/{{project}}/aggregated/instances` | `authed_get` | **All VMs across all zones** in a project (recommended for "show me all VMs") |
| `/compute/v1/projects/{{project}}/zones/{{zone}}/instances` | `authed_get` | List instances in one zone |
| `/compute/v1/projects/{{project}}/zones/{{zone}}/instances/{{instance}}` | `authed_get` | Get one instance |
| `/compute/v1/projects/{{project}}/zones/{{zone}}/instances/{{instance}}/getIamPolicy` | `authed_get` | Instance-level IAM policy (may be empty -- most access comes from project-level IAM, see Resource Manager above) |
| `/compute/v1/projects/{{project}}/aggregated/disks` | `authed_get` | **All disks across all zones** in a project (recommended for "show me all disks" / orphaned-disk audits) |
| `/compute/v1/projects/{{project}}/zones/{{zone}}/disks` | `authed_get` | List disks in one zone |
| `/compute/v1/projects/{{project}}/zones/{{zone}}/disks/{{disk}}` | `authed_get` | Get one disk |
| `/compute/v1/projects/{{project}}/zones` | `authed_get` | List zones (context) |
| `/compute/v1/projects/{{project}}/regions` | `authed_get` | List regions (context) |

Prefer `aggregated/instances` for a project-wide VM list and `aggregated/disks`
for a project-wide disk list. Compute paginates with `maxResults` + `pageToken`;
trim fields with the `fields=` query parameter to stay under the response size
gate.

**Finding orphaned disks:** use `aggregated/disks` and look for disks whose
`users` field is empty or absent -- a disk with no `users` is not attached to any
VM and is a candidate orphan (often still incurring storage cost).

### GCP Write Operations (via create_action_request)

The one supported GCP write is hard-resetting a Compute Engine VM. It goes
through `create_action_request` so the user approves before anything happens.
Never try to reach `:reset` (or any other `:`-verb) via `authed_post` -- write
verbs are not allow-listed there. `create_action_request` is top-level only --
if you are running as a sub-agent, do not call it; return the proposed
`request_type` and `params` to the parent via `agent_task_response` instead.

#### `reset_gcp_instance` -- hard-reset a stuck VM

Use when a VM is wedged/unresponsive (no SSH, frozen serial console, hung OS)
and needs a power-cycle. This is a HARD reset -- like pulling the power plug:
the machine loses unsaved in-memory state and boots fresh from its disk
(disks are untouched). It is not a graceful reboot; prefer asking the user to
reboot from inside the guest when the OS is still responsive.

Parameters:
- `project` (string, required): GCP project id (e.g. `my-project-123`, not the
  numeric project number).
- `zone` (string, required): Zone the instance lives in (e.g. `us-central1-a`).
- `instance` (string, required): Instance name.

Before proposing, read the instance (`authed_get` on
`{COMPUTE_BASE}/compute/v1/projects/{{project}}/zones/{{zone}}/instances/{{instance}}`)
to confirm the ids and check `status` -- reset applies only to a machine that
is powered on; a TERMINATED/SUSPENDED instance is rejected at proposal time.
The approval card shows the live instance status and machine type; on Approve
the reset is issued and the returned Compute operation name is reported. The
VM typically takes a minute or two to come back up -- re-read the instance (or
poke its service) afterwards to confirm recovery.

Example params:
```json
{{
  "project": "my-project-123",
  "zone": "us-central1-a",
  "instance": "web-server-1"
}}
```

### Kubernetes Engine / GKE (clusters)

**Base URL:** `{CONTAINER_BASE}`

| Path | Tool | Description |
|------|------|-------------|
| `/v1/projects/{{project}}/locations/{{location}}/clusters` | `authed_get` | List clusters in a location (`location` may be `-` for ALL locations) |
| `/v1/projects/{{project}}/locations/{{location}}/clusters/{{cluster}}` | `authed_get` | Get one cluster |
| `/v1/projects/{{project}}/locations/{{location}}/clusters/{{cluster}}/nodePools` | `authed_get` | List node pools |
| `/v1/projects/{{project}}/locations/{{location}}/clusters/{{cluster}}/nodePools/{{nodePool}}` | `authed_get` | Get one node pool |

Use `-` as the location to list clusters across all regions/zones in the project.
Full cluster objects are large -- use `output_file` for the cluster list.

### Cloud Logging (read log entries & metadata)

**Base URL:** `{LOGGING_BASE}`

| Path | Tool | Description |
|------|------|-------------|
| `/v2/entries:list` | **`authed_post`** | **Read log entries** (the actual log lines). Body: `resourceNames`, `filter`, `orderBy`, `pageSize` |
| `/v2/projects/{{project}}/logs` | `authed_get` | List log names |
| `/v2/projects/{{project}}/sinks` | `authed_get` | List sinks |
| `/v2/projects/{{project}}/sinks/{{sink}}` | `authed_get` | Get one sink |
| `/v2/projects/{{project}}/metrics` | `authed_get` | List log-based metrics |
| `/v2/projects/{{project}}/metrics/{{metric}}` | `authed_get` | Get one log-based metric |

Reading actual log lines is `authed_post` `POST /v2/entries:list` with a body:

- `resourceNames` (array, required) -- e.g. `["projects/{{project}}"]`.
- `filter` (string) -- Logging query-language filter, e.g.
  `severity>=ERROR AND timestamp>="2026-06-01T00:00:00Z"`.
- `orderBy` (string) -- e.g. `"timestamp desc"`.
- `pageSize` (int) and `pageToken` (string) for pagination.

The response carries `entries[]` + `nextPageToken`. Log pulls can be large --
prefer `output_file` and a tight `filter` + small `pageSize`.

### Cloud Billing (billing accounts, project billing, pricing, budgets)

**Base URL:** `{BILLING_BASE}` (budgets: `{BUDGETS_BASE}`)

| Path | Tool | Description |
|------|------|-------------|
| `/v1/billingAccounts` | `authed_get` | **List billing accounts** the user can see (`filter=master_billing_account=billingAccounts/{{id}}` narrows to one reseller's subaccounts) |
| `/v1/billingAccounts/{{accountId}}` | `authed_get` | Get one billing account (`displayName`, `open`, `masterBillingAccount`, `currencyCode`) |
| `/v1/billingAccounts/{{accountId}}/projects` | `authed_get` | **Projects billed to this account** (`projectBillingInfo[]` with `projectId`, `billingEnabled`) |
| `/v1/billingAccounts/{{accountId}}/subAccounts` | `authed_get` | Subaccounts of a reseller (master) billing account |
| `/v1/organizations/{{orgId}}/billingAccounts` | `authed_get` | Billing accounts owned by an organization |
| `/v1/projects/{{projectId}}/billingInfo` | `authed_get` | **Which account a project bills to** and whether billing is enabled |
| `/v1/billingAccounts/{{accountId}}:getIamPolicy` | **`authed_post`** | **Billing-account IAM policy** (who is a billing admin / viewer / user; empty body or `{{"options": {{"requestedPolicyVersion": 3}}}}`) |
| `/v1/services` | `authed_get` | List billable Google Cloud services (public catalog; `serviceId` + `displayName`) |
| `/v1/services/{{serviceId}}/skus` | `authed_get` | **SKUs + public list prices** for one service (`currencyCode=`, `startTime=`/`endTime=` for historical pricing) |
| `/v1beta/skus/{{skuId}}/price` | `authed_get` | Public price for one SKU (`currencyCode=`) |
| `/v1beta/billingAccounts/{{accountId}}/services` | `authed_get` | Services visible to a billing account |
| `/v1beta/billingAccounts/{{accountId}}/skus` | `authed_get` | SKUs visible to a billing account (`filter=service="services/{{serviceId}}"`) |
| `/v1beta/billingAccounts/{{accountId}}/skus/{{skuId}}/price` | `authed_get` | **Account-specific price** for one SKU (reflects negotiated discounts) |
| `{BUDGETS_BASE}/v1/billingAccounts/{{accountId}}/budgets` | `authed_get` | **List budgets** on a billing account (amount, threshold rules, filters) |
| `{BUDGETS_BASE}/v1/billingAccounts/{{accountId}}/budgets/{{budgetId}}` | `authed_get` | Get one budget |

Billing account ids look like `012345-6789AB-CDEF01` (the resource name is
`billingAccounts/012345-6789AB-CDEF01`; use only the id part in the path).
Start with `GET /v1/billingAccounts` to discover them, or
`GET /v1/projects/{{projectId}}/billingInfo` when you know the project.

**What the Billing API does NOT give you:** actual spend / cost history. The
Cloud Billing API covers accounts, project links, IAM, the price catalog, and
budget definitions (a budget object holds its amount and alert thresholds, not
the current spend against it). Itemised cost and usage data lives only in the
BigQuery billing export, which Quest cannot query. If the user asks "how much
did we spend last month", say so and point them to the Billing console's
Reports page or their BigQuery export rather than guessing from SKU prices.

**Permissions:** the user needs the Billing Account Viewer role (or higher) on
an account to see it; accounts the user has no role on are silently absent from
`GET /v1/billingAccounts`, and direct reads of them 403 with `PERMISSION_DENIED`.
`projects/{{projectId}}/billingInfo` needs `resourcemanager.projects.get` on the
project. The pricing catalog (`/v1/services`, `/v1/services/{{id}}/skus`,
`/v1beta/skus/{{id}}/price`) is public and needs no billing role.

The SKU catalog is large (`/v1/services/{{id}}/skus` for Compute Engine runs to
thousands of entries) -- always pass `output_file`, and narrow with `pageSize`
and `currencyCode`. Billing and Budget endpoints paginate with `pageSize` +
`pageToken`.

### Response size, `fields`, pagination, and `output_file`

GCP responses (aggregated instance lists, full cluster objects, large log pulls)
commonly exceed the ~3 KB `authed_get`/`authed_post` size gate. To handle this:

- Trim with the Google `fields=` query parameter (GET) to request only what you need.
- Use `output_file` to stream large payloads to the workspace, then read them
  with `get_workspace_file` / `run_python`.
- GCP list endpoints paginate: Compute uses `maxResults` + `pageToken`; Resource
  Manager / Container / Logging / Billing / Budgets use `pageSize` + `pageToken`.

### Example `authed_get` calls

```
# List projects the user can see
tool_call(tool_name="authed_get", arguments={{"url": "{RESOURCE_MANAGER_BASE}/v1/projects"}})

# Get one project
tool_call(tool_name="authed_get", arguments={{"url": "{RESOURCE_MANAGER_BASE}/v1/projects/my-project-123"}})

# All VMs in a project (aggregated across zones) -- large, so stream to workspace
tool_call(tool_name="authed_get", arguments={{"url": "{COMPUTE_BASE}/compute/v1/projects/my-project-123/aggregated/instances", "output_file": "vms.json"}})

# Get one VM instance
tool_call(tool_name="authed_get", arguments={{"url": "{COMPUTE_BASE}/compute/v1/projects/my-project-123/zones/us-central1-a/instances/my-vm"}})

# All disks in a project (aggregated across zones) -- find orphaned disks (empty `users`); large, so stream to workspace
tool_call(tool_name="authed_get", arguments={{"url": "{COMPUTE_BASE}/compute/v1/projects/my-project-123/aggregated/disks", "output_file": "disks.json"}})

# List zones (context)
tool_call(tool_name="authed_get", arguments={{"url": "{COMPUTE_BASE}/compute/v1/projects/my-project-123/zones"}})

# List GKE clusters across all locations
tool_call(tool_name="authed_get", arguments={{"url": "{CONTAINER_BASE}/v1/projects/my-project-123/locations/-/clusters", "output_file": "clusters.json"}})

# Get one GKE cluster
tool_call(tool_name="authed_get", arguments={{"url": "{CONTAINER_BASE}/v1/projects/my-project-123/locations/us-central1/clusters/my-cluster"}})

# Instance-level IAM policy for one VM (plain GET; often empty)
tool_call(tool_name="authed_get", arguments={{"url": "{COMPUTE_BASE}/compute/v1/projects/my-project-123/zones/us-central1-a/instances/my-vm/getIamPolicy"}})

# List billing accounts the user can see
tool_call(tool_name="authed_get", arguments={{"url": "{BILLING_BASE}/v1/billingAccounts"}})

# Which billing account does a project bill to, and is billing enabled?
tool_call(tool_name="authed_get", arguments={{"url": "{BILLING_BASE}/v1/projects/my-project-123/billingInfo"}})

# All projects billed to one account
tool_call(tool_name="authed_get", arguments={{"url": "{BILLING_BASE}/v1/billingAccounts/012345-6789AB-CDEF01/projects"}})

# Budgets configured on a billing account
tool_call(tool_name="authed_get", arguments={{"url": "{BUDGETS_BASE}/v1/billingAccounts/012345-6789AB-CDEF01/budgets"}})

# Public SKU price list for Compute Engine (service id 6F81-5844-456A) -- very large, stream to workspace
tool_call(tool_name="authed_get", arguments={{"url": "{BILLING_BASE}/v1/services/6F81-5844-456A/skus?currencyCode=USD&pageSize=500", "output_file": "compute_skus.json"}})
```

### Example `authed_post` calls

```
# Search/list organizations (empty body lists all visible orgs)
tool_call(tool_name="authed_post", arguments={{"url": "{RESOURCE_MANAGER_BASE}/v1/organizations:search", "body": {{}}}})

# Search organizations with a query filter
tool_call(tool_name="authed_post", arguments={{"url": "{RESOURCE_MANAGER_BASE}/v1/organizations:search", "body": {{"query": "domain:example.com"}}}})

# Project-level IAM policy: who has which roles on the project (v3 shows conditional bindings)
tool_call(tool_name="authed_post", arguments={{"url": "{RESOURCE_MANAGER_BASE}/v1/projects/my-project-123:getIamPolicy", "body": {{"options": {{"requestedPolicyVersion": 3}}}}}})

# Read recent error logs for a project -- large, so stream to workspace
tool_call(tool_name="authed_post", arguments={{"url": "{LOGGING_BASE}/v2/entries:list", "body": {{"resourceNames": ["projects/my-project-123"], "filter": "severity>=ERROR", "orderBy": "timestamp desc", "pageSize": 50}}, "output_file": "error_logs.json"}})

# Billing-account IAM policy: who are the billing admins / viewers / users
tool_call(tool_name="authed_post", arguments={{"url": "{BILLING_BASE}/v1/billingAccounts/012345-6789AB-CDEF01:getIamPolicy", "body": {{}}}})
```

**Important Notes:**
- Read-only: only the GET paths and the POST verbs (`organizations:search`,
  `entries:list`, project and billing-account `:getIamPolicy`) above are
  reachable. Write verbs (incl. `:setIamPolicy`) are rejected. The sole write
  is the approval-gated `reset_gcp_instance` action request.
- Billing: the API exposes accounts, project links, IAM, prices and budget
  definitions -- not actual spend. Don't estimate cost history from SKU prices.
- Most paths require a `{{project}}` id -- list projects or ask the user first.
- If a call 403s with a scope/permission error, the user must reconnect Google
  Services in Settings to grant the `cloud-platform` scope.
- The relevant GCP API must be enabled on the target project, or Google returns a
  403 "API not enabled" error."""
