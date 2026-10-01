# Google Workspace Admin API

## Overview

Read-only access to a Google Workspace account's directory (users, groups, org units, domains, custom user schemas) and device inventory (ChromeOS, mobile, and Endpoint Verification laptops/desktops), packaged as the in-tree `plugins/google_admin` plugin (plugin id `google_admin`; see [Plugins](../../../docs/architecture/plugins.md)). Setup in [Google Workspace Admin Setup](google-admin-setup.md).

The plugin contributes no tools and no action requests. Every read is a plain Google API GET the model composes through `authed_get` against two plugin-registered service entries, guided by the `system:google_admin` skill. The user-centred use case drives the surface: a person's profile and security state, the groups they belong to, their org unit, and the devices they use.

## Connection

A per-user OAuth connection that is **separate from Google Services** (the core Gmail/Drive/Calendar connector), although it borrows the same Google OAuth client:

- The admin scopes are useless to anyone who is not a Workspace administrator, so they are not in `GOOGLE_SERVICE_SCOPES` (`auth/config.py`), where adding them would flag every user for re-consent. Only the people who connect this row are asked for them.
- The authorizing Google account does not have to be the Quest login. Administrators commonly hold a dedicated admin account, so the flow shows the account picker and records the connected account in `oauth_blob.account` rather than failing closed on a mismatch the way `auth/google_services.py` does.
- The authorize request omits `include_granted_scopes`, so the stored token carries only the read-only admin scopes and never inherits the write-capable scopes of the same user's Google Services grant.

Router: `plugins/google_admin/oauth.py` (`/auth/google_admin?popup=1` connect convention, signed session-bound state cookie from `auth/oauth_state.py`, shared popup pages). Token JSON lives in `user_service_credentials.oauth_blob` (service `google_admin`): access token, refresh token, `expires_at`, the granted `scope`/`scopes`, `account`, `authorized_at`.

`get_google_admin_token()` in `plugins/google_admin/upstream.py` refreshes within 5 minutes of expiry under a per-user asyncio lock and persists the new access token. Google does not rotate refresh tokens, so the stored one is carried over. A failed refresh returns `None`, which surfaces as the `google_admin_oauth_required` reconnect error.

The `needs_reauth` hook compares the granted scopes against `GOOGLE_ADMIN_SCOPES`, so a scope unticked on Google's consent screen, or a later widening of the scope list, shows the "Update Available" badge on the connector row.

Disconnecting deletes the stored row but does not call Google's revoke endpoint: a revoke removes the user's whole grant to the OAuth client, which would also end the Google Services connection of the same account.

## Scopes

Defined in `GOOGLE_ADMIN_SCOPES` (`plugins/google_admin/upstream.py`); every one is a `.readonly` variant, asserted by `test_scopes_are_all_read_only`. `admin.directory.user.security` (third-party app grants, app passwords, backup verification codes) is deliberately not requested because Google offers no read-only variant of it.

What an account can actually read is still decided by Google: the calls succeed only for the resources the connected account's admin role covers.

## Service Entries (via authed_get)

Both entries are declared in `plugins/google_admin/manifest.py` with the plugin's own credential loader and injector, `requires_user`, and the `google_admin_oauth_required` missing-credentials error. Neither defines `allowed_post_endpoints`, so `authed_post` rejects every POST to these hosts.

| Host | API | Reachable (GET only) |
|------|-----|----------------------|
| `admin.googleapis.com` | Admin SDK Directory API (`/admin/directory/v1`) | Users and their aliases; groups, aliases, members and membership checks; org units; domains and domain aliases; custom user schemas; the customer record; ChromeOS devices (list, get, count); mobile devices (list, get) |
| `cloudidentity.googleapis.com` | Cloud Identity Devices API (`/v1/devices`) | Devices, device users (including the `devices/-` wildcard), client states |

Id segments use `[^/:]+`, which excludes Google's `:`-encoded custom methods; the one custom method allowed, the read-only `chromeos:countChromeOsDevices`, is spelled out. Org unit paths are the exception: they are multi-segment and the id form contains a colon (`id:...`), so that pattern accepts any number of segments but refuses dot segments, literal or percent-encoded.

Not allow-listed although Google serves them as GETs: `users/{key}/tokens`, `/asps` and `/verificationCodes` (the last returns live backup codes), user photos, admin roles and role assignments, calendar resources, printers, and ChromeOS command status. On `cloudidentity.googleapis.com` only `/v1/devices` is reachable; the groups, policies, and invitations APIs on the same host are not.

## Skill

`system:google_admin` (`plugins/google_admin/instructions.md`), gated on `google_admin` (server enabled AND user connected): the path tables with their query parameters, the user / ChromeOS / mobile search syntaxes, a "everything about one user" call sequence across both APIs, the `fields=` and `output_file` guidance for large pages, and how to read Google's 403 variants (missing admin privilege, API not enabled, insufficient scope).

## Key Files

- `plugins/google_admin/manifest.py` — manifest: the `enabled` admin switch, oauth user connection, the two service entries with their allow-lists, the skill
- `plugins/google_admin/upstream.py` — scopes, server-config predicates, token blob and refresh, `authed_get` loader and injector
- `plugins/google_admin/oauth.py` — OAuth router (start, callback, disconnect)
- `plugins/google_admin/instructions.md` — `system:google_admin` skill content
- `plugins/google_admin/tests/` — allow-list and request tests (`test_google_admin_services.py`), token and config tests (`test_google_admin_upstream.py`), OAuth flow tests (`test_google_admin_oauth_flow.py`)

## Constraints

- **Read-only, twice over:** the grant holds only `.readonly` scopes and the service entries allow GET paths only. No directory or device change is possible through this plugin.
- **Workspace administrators only:** a non-admin can complete the connection, but Google answers the calls with 403.
- **APIs must be enabled:** the Admin SDK API and the Cloud Identity API must be enabled in the GCP project that owns the Google OAuth client, or Google returns 403 `SERVICE_DISABLED`.
- **Size gate:** user and device pages exceed the `authed_get` response size gate unless trimmed with `fields=` or written to the workspace with `output_file` (see [Large Response Protection](../../../docs/architecture/gemini-api.md#large-response-protection)).
- **Not in public projects:** plugin services are blocked there like every other plugin surface.

## Design Decisions

**Why a plugin with its own connection instead of more Google Services scopes?**
Slides and GCP were added by extending `GOOGLE_SERVICE_SCOPES`, which makes every user re-consent. That is the wrong trade for scopes only administrators can use, and it would put directory-wide access on every user's consent screen. A separate connection keeps the grant opt-in and lets an administrator connect a dedicated admin account.

**Why reuse the core Google OAuth client instead of a second client?**
The deployment already has a Google OAuth client for sign-in and Google Services. A second client would mean a second consent-screen configuration to maintain for no isolation benefit: tokens are scoped per authorization, not per client. The cost is one more redirect URI on the existing client, which the admin `enabled` switch acknowledges.

**Why `authed_get` service entries instead of dedicated tools?**
Both APIs are fixed-host, GET-only REST APIs with server-side search, field trimming and paging, which is exactly what `authed_get` proxies. Dedicated tools would re-implement that surface. Tools become worthwhile for auto-paginated whole-directory exports or cross-API joins, which are not built.

**Why the Cloud Identity Devices API as well as the Directory API?**
The Directory API only knows ChromeOS and mobile devices. Laptops and desktops reporting through Endpoint Verification, and the device-to-user bindings, exist only in Cloud Identity.
