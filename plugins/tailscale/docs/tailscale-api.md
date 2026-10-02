# Tailscale API

## Overview

Read-only access to a tailnet's configuration for Tailscale administrators, packaged as the in-tree `plugins/tailscale` plugin (plugin id `tailscale`; see [Plugins](../../../docs/architecture/plugins.md)). Setup in [Tailscale Setup](tailscale-setup.md).

The plugin contributes no tools and no action requests. Every read is a plain Tailscale API GET the model composes through `authed_get` against one plugin-registered service entry (`api.tailscale.com`), guided by the `system:tailscale` skill: devices with their routes and posture attributes, the policy file (ACLs / grants, JSON or raw HuJSON), DNS, users and roles, auth keys and OAuth clients (metadata only), tailnet settings, contacts, webhooks, device-posture integrations, Tailscale Services, the organization's tailnet list, and network flow logs.

## Connection

The generic **api_key-kind** per-user connection (`POST /auth/service-key/tailscale`, row in `user_service_credentials.secret`, encrypted at rest). There is **no admin card**: `api.tailscale.com` is a fixed public host and every credential is the user's own, so the plugin declares no `credential_schema` and `plugin_server_available()` treats it as always available. The connector row therefore appears in every deployment's Data Connections picker, like Airtable's.

One pasted value, two accepted shapes, told apart by prefix in `plugins/tailscale/upstream.py`:

- **API access token** (`tskey-api-...`, admin console > Settings > Keys): injected as the Bearer token verbatim. Carries the full rights of the admin who minted it and expires after at most 90 days; the user pastes a new one afterwards.
- **OAuth client secret** (`tskey-client-...`, admin console > Settings > Trust credentials): the least-privilege option. OAuth clients never expire and carry only the scopes chosen at creation, so a client made with read scopes holds a token that cannot write regardless of the allow-list. `get_tailscale_token()` exchanges the secret at `POST /api/v2/oauth/token` (client-credentials grant; the client id is the segment embedded in the secret, `oauth_client_id()`), caches the one-hour access token in a process-local per-user cache, refreshes it within five minutes of expiry under a per-user asyncio lock, and drops the cache when the stored secret changes. A failed exchange (revoked client, transport error) reads as "not connected", surfacing the `tailscale_token_required` reconnect error instead of a raw exception.

`validate_credential()` (the connection's `validate_key` hook) accepts only those two prefixes, refuses whitespace, and names the mistake when a device auth key (`tskey-auth-...`) is pasted. Whether Tailscale accepts the value is found out on the first call.

## Service Entry (via authed_get)

One entry, `api.tailscale.com`, declared in `plugins/tailscale/manifest.py` with the plugin's loader and injector, `requires_user`, a `User-Agent` default header, and the `tailscale_token_required` missing-credentials error. It defines no `allowed_post_endpoints`, so `authed_post` rejects every POST to the host, including `/acl/validate`, `/acl/preview` and `/oauth/token`.

Reachable (GET only, all under `/api/v2`): `tailnet/{t}/devices`, `device/{id}` (+ `/routes`, `/attributes`), `tailnet/{t}/acl`, the five `tailnet/{t}/dns/*` reads, `tailnet/{t}/keys` (+ `/{id}`), `tailnet/{t}/users` and `users/{id}`, `tailnet/{t}/settings`, `tailnet/{t}/contacts`, `organizations/{org}/tailnets`, `tailnet/{t}/webhooks` and `webhooks/{id}`, `tailnet/{t}/posture/integrations` and `posture/integrations/{id}`, `tailnet/{t}/vip-services` (+ `/{name}`), and `tailnet/{t}/logging/network`.

Every path segment uses the `_SEG` pattern: any non-slash text (tailnet names like `alice@example.com`, service names like `svc:web`) except dot segments, literal or percent-encoded, so a path cannot climb out of its resource. The caller-header allow-list in `chat/gemini_api/authed_get.py` lets the model send `Accept: application/hujson` to get the policy file with its comments.

Not allow-listed although Tailscale serves them as GETs:

- User invites and device invites: each row carries the `inviteUrl` that accepts the invite, a live credential.
- Log-streaming destinations (`logging/{type}/stream`, `/stream/status`): the destination record can include the destination's credentials.
- Everything under `/oauth/`.

## Skill

`system:tailscale` (`plugins/tailscale/instructions.md`), gated on `tailscale` (always server-available, so effectively on the user's connection): the path table with query parameters and response fields, the `-` tailnet convention, the HuJSON `Accept` override, the OAuth-scope reading of 403s and the expiry reading of 401s, and `output_file` guidance for device lists and flow logs, which have no server-side paging or field selection.

## Key Files

- `plugins/tailscale/manifest.py` — manifest: api_key user connection, the service entry with its allow-list, the skill
- `plugins/tailscale/upstream.py` — credential shapes and validation, OAuth client-secret exchange and token cache, `authed_get` loader and injector
- `plugins/tailscale/instructions.md` — `system:tailscale` skill content
- `plugins/tailscale/tests/` — allow-list and request tests (`test_tailscale_services.py`), credential and token-exchange tests (`test_tailscale_upstream.py`)

## Constraints

- **Read-only:** GET paths only, no POST allow-list. With an OAuth client the token is read-scoped as well.
- **Administrators only:** Tailscale issues API access tokens and OAuth clients only to Owner / Admin / IT admin / Network admin roles; a member cannot connect.
- **Token expiry:** API access tokens last at most 90 days and must be re-pasted; OAuth clients do not expire.
- **Size gate:** `/devices` has no paging or field selection and `/logging/network` is a bulk export, so both usually need `output_file` (see [Large Response Protection](../../../docs/architecture/gemini-api.md#large-response-protection)).
- **Not in public projects:** plugin services are blocked there like every other plugin surface.

## Design Decisions

**Why an `authed_get` service entry instead of dedicated tools?**
The Tailscale API is a fixed-host, GET-shaped REST API with small JSON documents, which is exactly what `authed_get` proxies; the Google Workspace Admin plugin set the precedent. Tools would earn their keep for auto-aggregated fleet reports (devices by OS, stale keys) or a joined flow-log view, which are left for later.

**Why no admin card?**
Nothing is deployment-specific: the host is fixed and credentials are per user. An `enabled` switch would only exist to hide the connector row, which no other credential-free connector (Airtable) does either.

**Why accept OAuth client secrets in the api_key field instead of a second connection or an oauth-kind router?**
Tailscale's OAuth is the machine-to-machine client-credentials grant, not a browser flow, so there is nothing for a popup router to do, and the client id is recoverable from the secret, so a single pasted value suffices. Supporting it matters because it is the only way to hold a credential that is read-only on the Tailscale side and never expires; a personal API access token is all-powerful and dies within 90 days.

**Why exclude invites and log-streaming destinations?**
The same rule as the Iru plugin's device secrets and the Google Workspace Admin plugin's backup codes: a GET whose response is itself a credential (an invite's accept URL, a log destination's token) must not land in a conversation transcript, even for an administrator who could read it in the console.

**Why is the cache process-local?**
Exchanged tokens live one hour and are cheap to re-mint; persisting them would add an encrypted column for no gain. A restart costs one extra token request per connected user.
