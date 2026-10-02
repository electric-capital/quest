# Google Workspace Admin

Read the organization's Google Workspace directory (users, groups, org units, domains) and device inventory (ChromeOS and mobile devices) with `authed_get` against the Admin SDK Directory API. Authentication is automatic: the token of the Google account the user connected under Settings > Data Connections > Google Workspace Admin is injected and refreshed as needed.

**Strictly read-only.** Only the GET paths listed below are reachable, and the token holds read-only scopes. You cannot create, suspend, move, or delete users, change group membership, or act on devices (wipe, disable, deprovision); if the user asks for that, say it is not supported and point them to the Admin console (admin.google.com). Also not available: laptops and desktops reporting through Endpoint Verification (macOS, Windows, Linux -- only ChromeOS and mobile devices are covered), third-party app grants, app passwords, backup codes, admin role assignments, licenses, and audit / login reports.

Identity notes:
- This is a separate connection from Google Services (Gmail, Drive, ...). The connected account can be a dedicated admin account that differs from the user's Quest login email.
- Results reflect what that account may see. A `403` with `Not Authorized to access this resource/api` means the account lacks the admin privilege for that resource (a delegated admin may read users but not devices, for example); `403 ... API has not been used in project` / `SERVICE_DISABLED` means the Admin SDK API is not enabled for this deployment's Google Cloud project; `ACCESS_TOKEN_SCOPE_INSUFFICIENT` means the user should reconnect. Report these to the user instead of retrying.

## Directory API

**Base URL:** `https://admin.googleapis.com/admin/directory/v1`

Use `my_customer` wherever a customer id is expected (the `customer=` query parameter and the `{customer}` path segment): it is an alias for the connected account's own Workspace account.

| Path | Description | Query parameters |
|------|-------------|------------------|
| `/users` | List / search users. **Requires `customer=my_customer` or `domain=<domain>`** | `query`, `maxResults` (default 100, max 500), `pageToken`, `orderBy` (`email`, `familyName`, `givenName`), `sortOrder`, `projection` (`basic`, `full`, `custom` + `customFieldMask`), `showDeleted=true` (users deleted in the last 20 days), `viewType`, `fields` |
| `/users/{userKey}` | Get one user. `userKey` = primary email, alias email, or user id | `projection`, `customFieldMask`, `viewType`, `fields` |
| `/users/{userKey}/aliases` | A user's email aliases | |
| `/groups` | List / search groups. Pass `customer=my_customer` or `domain=`, **or `userKey=<email>` alone to list the groups one user belongs to** | `query`, `maxResults` (max 200), `pageToken`, `orderBy=email`, `sortOrder`, `fields` |
| `/groups/{groupKey}` | Get one group (email, alias or id) | `fields` |
| `/groups/{groupKey}/aliases` | A group's aliases | |
| `/groups/{groupKey}/members` | List members | `roles` (`OWNER`, `MANAGER`, `MEMBER`, comma-separated), `includeDerivedMembership=true` (also members of nested groups), `maxResults` (max 200), `pageToken` |
| `/groups/{groupKey}/members/{memberKey}` | One membership (role, type, status) | |
| `/groups/{groupKey}/hasMember/{memberKey}` | `{"isMember": bool}`, including membership through nested groups | |
| `/customer/{customer}/orgunits` | List org units | `type` (`all` = whole tree, `children` = direct children, `allIncludingParent`), `orgUnitPath` (subtree root; omit for the top) |
| `/customer/{customer}/orgunits/{orgUnitPath}` | Get one org unit. Path WITHOUT the leading slash (`corp/sales`), or `id:<orgUnitId>` | |
| `/customer/{customer}/domains` | Domains of the account (primary + secondary, verification state) | |
| `/customer/{customer}/domains/{domainName}` | Get one domain | |
| `/customer/{customer}/domainaliases` | Domain aliases | `parentDomainName` |
| `/customer/{customer}/schemas` | Custom user attribute schemas (explains the `customSchemas` block on users) | |
| `/customers/{customer}` | The Workspace account itself (primary domain, creation time, language). Note the plural `customers` | |
| `/customer/{customer}/devices/chromeos` | List / search ChromeOS devices | `query`, `orgUnitPath` (+ `includeChildOrgunits=true`), `maxResults` (default 100, max 300), `pageToken`, `orderBy` (`annotatedLocation`, `annotatedUser`, `lastSync`, `notes`, `serialNumber`, `status`), `sortOrder`, `projection` (`BASIC`, `FULL`), `fields` |
| `/customer/{customer}/devices/chromeos:countChromeOsDevices` | Count ChromeOS devices | `filter` (same syntax as `query` above), `orgUnitPath`, `includeChildOrgunits` |
| `/customer/{customer}/devices/chromeos/{deviceId}` | Get one ChromeOS device | `projection`, `fields` |
| `/customer/{customer}/devices/mobile` | List / search mobile devices (Android, iOS) | `query`, `maxResults` (max 100), `pageToken`, `orderBy` (`deviceId`, `email`, `lastSync`, `model`, `name`, `os`, `status`, `type`), `sortOrder`, `projection` (`BASIC`, `FULL`), `fields` |
| `/customer/{customer}/devices/mobile/{resourceId}` | Get one mobile device | `projection`, `fields` |

### What a user record holds

`primaryEmail`, `name`, `id`, `orgUnitPath`, `suspended` (+ `suspensionReason`), `archived`, `isAdmin` (super admin), `isDelegatedAdmin`, `isEnrolledIn2Sv`, `isEnforcedIn2Sv`, `creationTime`, `lastLoginTime` (`1970-01-01T00:00:00.000Z` = never signed in), `agreedToTerms`, `changePasswordAtNextLogin`, `aliases`, `nonEditableAliases`, `recoveryEmail`, `recoveryPhone`, `isMailboxSetup`, `thumbnailPhotoUrl`. With `projection=full` also `organizations` (title, department, cost center), `relations` (manager), `phones`, `addresses`, `locations`, `externalIds` (employee id), `customSchemas`. Passwords are never returned.

### Searching users (`query`)

Clauses are separated by spaces and ANDed. `field=value` is an exact match, `field:value` a contains-word match, and `field:prefix*` a prefix match (only on `email`, `givenName`, `familyName`). Quote values containing spaces with single quotes. A bare word matches `givenName`, `familyName` or `email`.

| Field | Operators | Notes |
|-------|-----------|-------|
| `name` | `=`, `:` | given + family name, e.g. `name:'Jane Smith'` |
| `email` | `=`, `:`, `:prefix*` | includes aliases |
| `givenName`, `familyName` | `=`, `:`, `:prefix*` | |
| `isAdmin`, `isDelegatedAdmin`, `isSuspended`, `isArchived`, `isEnrolledIn2Sv`, `isEnforcedIn2Sv`, `isGuest` | `=` | `true` / `false` |
| `orgUnitPath` | `=` | matches the org unit AND everything below it, e.g. `orgUnitPath='/Sales'` |
| `orgName`, `orgTitle`, `orgDepartment`, `orgCostCenter`, `orgDescription` | `=`, `:` | |
| `manager`, `directManager` | `=` | manager's email; `manager` matches the whole chain upward |
| `externalId`, `im` | `=`, `:` | |
| `phone` | `=` | |
| `address`, `addressLocality`, `addressRegion`, `addressCountry`, `addressPostalCode` | `:` (and `=` except `address`) | |
| `<schemaName>.<fieldName>` | `=`, `:`, ranges | custom attributes |

Groups are searched the same way with `query` on `/groups`: `email`, `name` (`=`, `:`, `:prefix*`) and `memberKey=<email>` (groups the address is a direct member of).

### Searching devices (`query`)

ChromeOS (`/devices/chromeos`): space-separated `operator:value` terms, e.g. `user:jane` (annotated user), `recent_user:jane@example.com`, `status:provisioned` (also `disabled`, `deprovisioned`), `id:<serial number>` (3+ characters), `asset_id:`, `location:`, `note:"loaned"`, `public_model_name:"Google Pixelbook Go"`, `sync:2026-01-01..2026-03-31` (last policy sync), `register:` (enrollment date), `last_user_activity:2026-06-01..2026-06-30`, `aue:` (auto-update expiration range), `chrome_version:`, `wifi_mac:`, `ethernet_mac:`.

Mobile (`/devices/mobile`): `field:value` terms with no space after the colon, space-separated, e.g. `email:jane@example.com`, `name:jane`, `serial:<serial>`, `os:ios`, `type:android`, `model:"pixel 8"`, `status:approved` (also `pending`, `blocked`), `owner:company` / `owner:byod`, `management_type:advanced`, `compromised_status:compromised`, `encryption_status:encrypted`, `imei:`, `sync:2026-01-01..` and `register:..2026-01-01` (date ranges: `d`, `d..d`, `d..`, `..d`).

## Finding everything about one user

Run these for a full picture of a person (all take the user's primary email):

```
# Profile, org unit, admin / 2-Step Verification / suspension state, last login
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users/jane@example.com?projection=full"})

# Groups the user belongs to
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/groups?userKey=jane@example.com&fields=groups(email,name,directMembersCount),nextPageToken"})

# Phones and tablets
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/devices/mobile?query=email:jane@example.com&projection=FULL"})

# ChromeOS devices the user recently signed in to
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/devices/chromeos?query=recent_user:jane@example.com&projection=BASIC"})
```

## More examples

```
# Find a user by partial name
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&query=name:'Jane'&fields=users(primaryEmail,name/fullName,orgUnitPath,suspended)"})

# Super admins
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&query=isAdmin=true&fields=users(primaryEmail,name/fullName,isEnrolledIn2Sv,lastLoginTime)"})

# Active users not enrolled in 2-Step Verification
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&query=isEnrolledIn2Sv=false%20isSuspended=false&maxResults=500&fields=users(primaryEmail,name/fullName,orgUnitPath,lastLoginTime),nextPageToken"})

# Everyone in an org unit (and below)
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&query=orgUnitPath='/Engineering'&maxResults=500&fields=users(primaryEmail,name/fullName,orgUnitPath),nextPageToken"})

# The whole org unit tree
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/orgunits?type=all&fields=organizationUnits(name,orgUnitPath,orgUnitId,parentOrgUnitPath)"})

# Members of a group, including nested groups
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/groups/eng@example.com/members?includeDerivedMembership=true&maxResults=200&fields=members(email,role,type,status),nextPageToken"})

# How many ChromeOS devices are provisioned
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/devices/chromeos:countChromeOsDevices?filter=status:provisioned"})

# Mobile devices, most recently synced first
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/devices/mobile?orderBy=lastSync&sortOrder=DESCENDING&maxResults=100&fields=mobiledevices(resourceId,email,model,os,type,status,lastSync),nextPageToken"})

# Export the full user list to the workspace for analysis (repeat with pageToken until no nextPageToken)
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&maxResults=500&projection=full", "output_file": "google-admin/users-page-1.json"})
```

## Important notes

- **Always trim with `fields=`.** User and device records are large and a page of them exceeds the response size limit. `fields` uses Google's partial-response syntax: `users(primaryEmail,name/fullName),nextPageToken`. Keep `nextPageToken` in the list or you cannot page.
- **Bulk questions** (counts across the whole directory, stale accounts, device fleet breakdowns): page through with `maxResults` at its maximum and `output_file`, then aggregate the saved files with `run_python`. Do not paste whole pages into the conversation.
- **Paging:** Directory API responses carry `nextPageToken`; pass it back as `pageToken` with the same other parameters. An absent `nextPageToken` means the last page.
- **URL-encode the query string:** spaces as `%20`. Single quotes, `=`, `:` and `*` inside `query` values may be left as-is.
- Directory API timestamps are RFC 3339 UTC. To find stale accounts, list users with `lastLoginTime` in `fields` and compare client-side; `lastLoginTime` is not searchable.
- `showDeleted=true` returns ONLY deleted users (recoverable for 20 days), not a mix.
- This data is personal and organizational information about the user's colleagues. Report what was asked for; do not volunteer recovery emails, phone numbers or home addresses unless they are relevant to the question.
