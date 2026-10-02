# Tailscale

Read a tailnet's configuration with `authed_get` against the Tailscale API (`https://api.tailscale.com/api/v2`). Authentication is automatic: the credential the user stored under Settings > Data Connections > Tailscale is injected (an API access token as-is; an OAuth client secret is exchanged for a short-lived access token behind the scenes).

**Strictly read-only.** Only the GET paths listed below are reachable. You cannot authorize, rename, tag, delete or expire devices, edit the policy file, change DNS, create or revoke keys, manage users or invites, or change settings; if asked, say it is not supported and point the user to the admin console (login.tailscale.com/admin). Also deliberately unavailable: user and device **invites** (their rows contain the accept-URL), **log-streaming destinations** (may contain destination credentials), and the ACL `validate` / `preview` calls (POST).

Access notes:
- Results reflect what the stored credential may see. An API access token has the rights of the admin who created it. An OAuth client only has the scopes it was created with: a `403` on one endpoint while others work means that scope is missing (e.g. `policy_file:read` for `/acl`, `dns:read` for `/dns/*`, `devices:core:read` for devices, `users:read`, `auth_keys:read`, `logs:network:read`, `feature_settings:read`, ...). Say which scope is missing rather than retrying.
- A `401` means the credential was rejected: an API access token has expired (they live at most 90 days) or was revoked, or the OAuth client was deleted. Ask the user to re-enter it in Settings > Data Connections.
- Rate limits answer `429`; wait and narrow the request rather than looping.

## Paths

**Base URL:** `https://api.tailscale.com/api/v2`

Write `-` for `{tailnet}`: it means "the tailnet this credential belongs to". A tailnet name (`example.com`, `alice@gmail.com`, `alice.github`) also works. Device ids are the `nodeId` (`nABC123CNTRL`, preferred) or the legacy numeric `id` from the device list.

| Path | Description | Query parameters |
|------|-------------|------------------|
| `/tailnet/{tailnet}/devices` | All devices. Default fields: addresses, id, nodeId, user, name, hostname, clientVersion, updateAvailable, os, created, lastSeen, keyExpiryDisabled, expires, authorized, isExternal, machineKey, nodeKey, blocksIncomingConnections, tags, tailnetLockError, tailnetLockKey | `fields=all` adds advertisedRoutes, enabledRoutes, clientConnectivity (endpoints, DERP, latency, NAT traversal support), postureIdentity (serial numbers, hardware addresses), sshEnabled, distro |
| `/device/{deviceId}` | One device | `fields=all` as above |
| `/device/{deviceId}/routes` | `advertisedRoutes` vs `enabledRoutes` for a subnet router / exit node (`0.0.0.0/0` + `::/0` = exit node) | |
| `/device/{deviceId}/attributes` | Posture attributes (`attributes` map + `expiries`) | |
| `/tailnet/{tailnet}/acl` | The policy file: `acls`, `grants`, `groups`, `hosts`, `tagOwners`, `autoApprovers`, `ssh`, `nodeAttrs`, `tests`, `postures`, `ipsets`, `derpMap`, ... Returned as JSON (comments stripped). Pass header `Accept: application/hujson` for the raw file with comments, exactly as the admin console shows it | |
| `/tailnet/{tailnet}/dns/nameservers` | Global nameservers (`dns` list) | |
| `/tailnet/{tailnet}/dns/preferences` | `magicDNS` on/off | |
| `/tailnet/{tailnet}/dns/searchpaths` | Search domains | |
| `/tailnet/{tailnet}/dns/split-dns` | Split DNS: domain -> nameservers map | |
| `/tailnet/{tailnet}/dns/configuration` | Everything above in one document, incl. per-resolver `useWithExitNode` and `overrideLocalDNS` (alpha endpoint) | |
| `/tailnet/{tailnet}/keys` | Auth keys and OAuth clients of the credential's owner (ids only). The key *values* are never returned | `all=true` adds tailnet-wide keys other admins created |
| `/tailnet/{tailnet}/keys/{keyId}` | One key: `keyType` (`auth`/`client`/`federated`), `description`, `created`, `expires`, `revoked`, `invalid`, `capabilities` (reusable / ephemeral / preauthorized / tags) or `scopes` + `tags` for OAuth clients, `userId` | |
| `/tailnet/{tailnet}/users` | Users: `loginName`, `displayName`, `role` (owner, admin, it-admin, network-admin, billing-admin, auditor, member), `status` (active, idle, suspended, needs-approval, over-billing-limit), `type` (member / shared), `deviceCount`, `lastSeen`, `currentlyConnected`, `created` | `type=member|shared`, `role=<role>` |
| `/users/{userId}` | One user | |
| `/tailnet/{tailnet}/settings` | Tailnet settings: `devicesApprovalOn`, `devicesAutoUpdatesOn`, `devicesKeyDurationDays`, `usersApprovalOn`, `usersRoleAllowedToJoinExternalTailnets`, `networkFlowLoggingOn`, `regionalRoutingOn`, `postureIdentityCollectionOn`, `aclsExternallyManagedOn` (+ `aclsExternalLink`), `httpsEnabled` | |
| `/tailnet/{tailnet}/contacts` | `account`, `support`, `security` contact emails (+ verification state) | |
| `/organizations/-/tailnets` | The tailnets of the organization the credential belongs to (names for `{tailnet}`) | |
| `/tailnet/{tailnet}/webhooks` | Webhook endpoints: `endpointId`, `endpointUrl`, `providerType`, `subscriptions`, `creatorLoginName`, `created`, `lastModified`. Secrets are never returned | |
| `/webhooks/{endpointId}` | One webhook | |
| `/tailnet/{tailnet}/posture/integrations` | Device posture integrations (`provider`: falcon, fleet, huntress, intune, jamfpro, kandji, kolide, sentinelone; `cloudId`, `clientId`, `tenantId` -- no secrets) | |
| `/posture/integrations/{id}` | One integration | |
| `/tailnet/{tailnet}/vip-services` | Tailscale Services (stable virtual-IP services): `name` (`svc:web`), `displayName`, `addrs`, `ports`, `tags`, `comment`, `annotations` | |
| `/tailnet/{tailnet}/vip-services/{name}` | One service, e.g. `/vip-services/svc:web` | |
| `/tailnet/{tailnet}/logging/network` | Network flow logs: per-node samples with `virtualTraffic`, `subnetTraffic`, `exitTraffic`, `physicalTraffic` (`src`, `dst`, `proto`, tx/rx packets and bytes). Only when flow logging is on; retention 30 days | **`start` and `end` required**, RFC 3339 UTC, e.g. `start=2026-10-01T00:00:00Z&end=2026-10-01T01:00:00Z` |

## Examples

```
# Every device with routes + connectivity, saved for analysis
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/devices?fields=all", "output_file": "tailscale/devices.json"})

# Compact device list inline (default fields only)
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/devices"})

# One device, with subnet routes
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/device/nABC123CNTRL?fields=all"})
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/device/nABC123CNTRL/routes"})

# The policy file exactly as written (comments kept)
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/acl", "headers": {"Accept": "application/hujson"}})

# Users who are admins
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/users?role=admin"})

# Which auth keys and OAuth clients exist tailnet-wide (then GET each id for details)
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/keys?all=true"})

# DNS in one call
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/dns/configuration"})

# One hour of flow logs, to the workspace
tool_call(tool_name="authed_get", arguments={"url": "https://api.tailscale.com/api/v2/tailnet/-/logging/network?start=2026-10-01T09:00:00Z&end=2026-10-01T10:00:00Z", "output_file": "tailscale/flows-0900.json"})
```

## Important notes

- **Device lists are large.** The Tailscale API has no field selection or paging on `/devices`: a tailnet with more than a few dozen devices exceeds the inline response limit, especially with `fields=all`. Use `output_file` and analyse the saved JSON with `run_python` (count by OS, find expired keys, list subnet routers, devices not seen in 30 days, unauthorized devices, ...). Say how many devices you scanned.
- **Flow logs are very large.** Always use `output_file`, keep windows to an hour or less, and aggregate with `run_python`. `src`/`dst` are `ip:port`; map Tailscale IPs back to device names with the device list.
- `/keys` lists only ids. To describe keys, GET each `/keys/{keyId}`; batch them in one turn.
- `lastSeen` is empty while `connectedToControl` is true (the device is online right now). Device and key timestamps are RFC 3339 UTC; `expires` of `0001-01-01T00:00:00Z` means key expiry is disabled.
- Policy-file questions ("who can reach the database hosts?", "which tags can user X own?") are answered from `/acl`: resolve `groups`, `hosts`, `tagOwners` and `autoApprovers` yourself, and quote the relevant `acls` / `grants` entries. Explain the rules; do not guess at effective access when `postures` or `via` are involved.
- Keys, IP addresses, hostnames and user emails in these responses describe the user's own infrastructure and colleagues. Report what was asked; do not volunteer `machineKey` / `nodeKey` values, user emails or endpoint lists unless relevant.
