# Google Workspace Admin

Read the organization's Google Workspace directory (users, groups, org units, domains, rooms), device inventory (ChromeOS and mobile devices), and Google Meet activity (calls, call quality, Meet hardware room devices, usage statistics). Directory reads use `authed_get` against the Admin SDK Directory API; Meet questions use the four `google_admin_meet_*` tools (see "Google Meet" below). Authentication is automatic: the token of the Google account the user connected under Settings > Data Connections > Google Workspace Admin is injected and refreshed as needed.

**Strictly read-only.** Only the GET paths listed below are reachable, and the token holds read-only scopes. You cannot create, suspend, move, or delete users, change group membership, act on devices (wipe, disable, deprovision), or control Meet hardware or meetings; if the user asks for that, say it is not supported and point them to the Admin console (admin.google.com). Also not available: laptops and desktops reporting through Endpoint Verification (macOS, Windows, Linux -- only ChromeOS and mobile devices are covered), third-party app grants, app passwords, backup codes, admin role assignments, licenses, and every audit / usage report other than Google Meet's (no login, admin, Drive or token audit logs).

Identity notes:
- This is a separate connection from Google Services (Gmail, Drive, ...). The connected account can be a dedicated admin account that differs from the user's Quest login email.
- Results reflect what that account may see. A `403` with `Not Authorized to access this resource/api` means the account lacks the admin privilege for that resource (a delegated admin may read users but not devices, for example; the Meet tools need the **Reports** privilege); `403 ... API has not been used in project` / `SERVICE_DISABLED` means the Admin SDK API is not enabled for this deployment's Google Cloud project; `ACCESS_TOKEN_SCOPE_INSUFFICIENT` (or the tools' `google_admin_reconnect_required`) means the user should reconnect -- connections made before Meet support was added lack the Meet scopes. Report these to the user instead of retrying.

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
| `/customer/{customer}/resources/calendars` | Rooms and other bookable resources (where Meet hardware is installed) | `query` (`name`, `buildingId`, `floorName`, `capacity`, `resourceCategory`, `featureInstances.feature.name`, `resourceEmail`; `=`, `!=`, `:` prefix with `*`, `AND`, `NOT`), `maxResults` (max 500), `pageToken`, `orderBy` (`resourceName`, `capacity`, `buildingId`, `floorName`), `fields` |
| `/customer/{customer}/resources/calendars/{resourceId}` | One room: `resourceName`, `generatedResourceName`, `resourceEmail`, `resourceCategory` (`CONFERENCE_ROOM`, `OTHER`), `capacity`, `buildingId`, `floorName`, `featureInstances` | `fields` |
| `/customer/{customer}/resources/buildings` (+ `/{buildingId}`) | Buildings: name, floors, address | `maxResults`, `pageToken` |
| `/customer/{customer}/resources/features` (+ `/{name}`) | Room features (e.g. a "Meet hardware" or "Video conferencing" feature, if the admin defined one) | `maxResults`, `pageToken` |

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

## Google Meet

Meet data comes from the Admin SDK Reports API: the **Meet audit log** (one `call_ended` record per participant session -- every time someone or a room device leaves a call -- carrying ~70 quality metrics) and the **Meet hardware audit log** (room device events). Records appear a few minutes after each participant leaves, so a call still in progress is invisible until people drop off, and Google keeps them for 6 months. A raw `call_ended` record alone fills the `authed_get` inline size limit, so use the tools, which page, flatten and summarize server-side:

| Tool | Answers |
|------|---------|
| `google_admin_meet_calls` | Which meetings happened: conferences newest first with meeting code, organizer, start/end, participant / external counts, device types, Meet hardware rooms present, end-call reason counts, and the median / max of the key quality metrics per call |
| `google_admin_meet_call_quality` | How the calls went: one row per participant session with Google's raw metric values -- packet loss, jitter, round-trip time, congestion, bandwidth, video resolution / frame rate -- plus transport, location, `end_call_reason` and rating; newest first, or ordered by one metric with `sort_by` (+ `sort_order`); plus a summary over every scanned row (median / p90 / max of the key metrics, medians by device type and by transport, end-call reason and rating counts) |
| `google_admin_meet_hardware` | How the room devices are doing: every Meet hardware device that logged activity in the window, with peripherals currently detached, missing / found, calls joined per platform (Meet, Zoom, Teams, Webex, SIP), restarts, software updates, app load errors, feedback, the device's Meet call metrics (median / p90 / max), and `concerns` (devices with concerns first); `device=` narrows to one device and adds its event timeline |
| `google_admin_meet_usage` | How much Meet is used: daily meetings, calls, call minutes, average meeting length, external and room-device calls, active users, totals over the range |

Shared arguments: the window is `days` (lookback, default 7, max 180) or `start_time` / `end_time` (a UTC date `YYYY-MM-DD` or RFC 3339). Call filters are ANDed: `meeting_code` (code or meet.google.com link), `conference_id`, `organizer_email`, `participant` (email, phone number, or Meet hardware device id), `device_type` (`web`, `android`, `ios`, `chromebox`, `chromebase`, `pstn_in`, ..., or `meet_hardware` for every room device).

How to read the results:
- **Meeting code vs conference:** a meeting code (`abc-defg-hij`) is reused by every occurrence of a recurring meeting; `conference_id` is one occurrence. Find the conference with `google_admin_meet_calls`, then drill in with `google_admin_meet_call_quality(conference_id=...)`.
- **Endpoints, not people:** someone who rejoins, or joins from a laptop and a phone, has several rows. Phone dial-ins and very short joins carry no network metrics.
- **Raw values, no grading:** the tools pass Google's numbers through unchanged and do not rate them; judge them yourself and quote the metric when you do. Units: `*_packet_loss_*` and `network_congestion` (share of time without enough upload bandwidth) are percent; `*_jitter_msec_*` and `network_rtt_msec_mean` milliseconds; `*_kbps_*` kilobits per second; `*_pixels` the long / short side of the video in pixels; `*_fps_*` frames per second; `*_seconds` how long that stream ran. `end_call_reason` is `normal`, `network_error`, `system_error` or `unknown`; `rating` is the participant's 1-5 end-of-call rating, when given.
- **Finding the worst sessions:** rows are capped by `limit`, so to find outliers among many sessions sort by the metric in question (`sort_by="audio_recv_packet_loss_mean"`, `"network_recv_jitter_msec_max"`, `"network_rtt_msec_mean"`; `sort_order="asc"` for lower-is-worse values such as `network_estimated_download_kbps_mean` or `video_recv_fps_mean`). The sort metric is added to every row even when it is not in the default set; `include_all_metrics=true` returns every metric.
- **Direction matters:** `*_recv_*` loss is what the participant received (their downlink, or someone else's bad uplink); `*_send_*` loss and `network_congestion` point at the participant's own uplink. Many participants of one call with receive loss, but none with send loss, suggests a single sender's network.
- **Truncation:** scans stop after the newest 5000 call records (10000 hardware events) and say `truncated` with `covered_from`; narrow the window or add filters when that happens.
- **Masking:** Google shows email, location and IP for the organization's own users; external participants may appear only by display name, and phone numbers are partially hidden.
- **Meet hardware coverage:** Google has no API listing Meet hardware, so `google_admin_meet_hardware` only knows devices that logged an event (joins, peripherals, restarts, ...) in the window -- a device that stayed idle or offline the whole time is absent; widen the window before concluding a device is gone. Device ids changed in March-April 2026, so older records carry the previous id. There is no live online/offline status: `in_call` and peripheral states are the last state seen in the log.
- **Rooms:** Meet hardware is not linked to a room by the API; match the device `display_name` to the room's `resourceName` from `/resources/calendars` (usually the same name) to report building, floor or capacity.
- **Usage days** are Pacific-time days, counted when meetings end, published 1-3 days late, and cover only meetings organized by the organization's users. Other `meet:` usage parameters: `num_calls_{android,chromebase,chromebox,ios,jamboard,web,unknown_client}`, `num_calls_by_{external,internal,pstn_in,pstn_out}_users`, `num_meetings_{android,...,web}`, `num_meetings_with_{2,3_to_5,6_to_10,11_to_15,16_to_25,26_to_50}_calls`, `num_meetings_with_{external,pstn_in,pstn_out}_users`, `total_call_minutes_{android,...,web}`, `total_call_minutes_by_{external,internal,pstn_in,pstn_out}_users`, `average_meeting_minutes_with_{2,...,26_to_50}_calls`, `lonely_meetings`, `max_concurrent_usage_{chromebase,chromebox}`, `num_7day_active_users`.

Typical sequences:

```
# "How was the quality of Jane's 10am meeting today?"
tool_call(tool_name="google_admin_meet_calls", arguments={"organizer_email": "jane@example.com", "days": 1})
tool_call(tool_name="google_admin_meet_call_quality", arguments={"conference_id": "<from the first call>"})

# "Which conference rooms have problems?"
tool_call(tool_name="google_admin_meet_hardware", arguments={"days": 14})

# "What happened to the Boardroom device yesterday?"
tool_call(tool_name="google_admin_meet_hardware", arguments={"device": "Boardroom", "days": 2})

# "Why does Sam keep dropping off calls?"
tool_call(tool_name="google_admin_meet_call_quality", arguments={"participant": "sam@example.com", "days": 30, "sort_by": "audio_recv_packet_loss_mean"})

# "Is call quality worse on room devices than on laptops this week?" (compare summary.by_device_type medians)
tool_call(tool_name="google_admin_meet_call_quality", arguments={"days": 7, "limit": 10})

# "Which sessions had the worst round-trip time today?"
tool_call(tool_name="google_admin_meet_call_quality", arguments={"days": 1, "sort_by": "network_rtt_msec_mean", "limit": 20})

# "How much did we use Meet last month?"
tool_call(tool_name="google_admin_meet_usage", arguments={"start_date": "2026-09-01", "end_date": "2026-09-30"})
```

For bulk analysis across many windows (for example quality per week over six months), call the tools from `run_python` through the tool-call bridge rather than one by one.

### Raw Reports API access

The tools cover the common questions. For anything else (other Meet events such as `presentation_started`, `recording_activity` or `room_check_in`), `authed_get` reaches the raw Reports API -- **write the response to the workspace with `output_file`** and read it with `run_python`, since even one record exceeds the inline limit.

**Base URL:** `https://admin.googleapis.com/admin/reports/v1`

| Path | Description | Query parameters |
|------|-------------|------------------|
| `/activity/users/all/applications/meet` | Meet audit records (`all`, or one user's email) | `eventName` (`call_ended`, `presentation_started`, `recording_activity`, `room_check_in`, ...), `filters` (`param==value`, also `<>` `<` `<=` `>` `>=` URL-encoded as `%3C%3E` `%3C` `%3C=` `%3E` `%3E=`; comma-separated clauses are ANDed; only work together with `eventName`; an unknown parameter is silently ignored), `startTime`, `endTime` (RFC 3339), `maxResults` (max 1000), `pageToken`, `orgUnitID` (`id:...`, filters by the actor's org unit), `fields` |
| `/activity/users/all/applications/meet_hardware` | Meet hardware audit records: event names carry an `EVENT_` prefix (`EVENT_MEET_CALL_JOINED`, `EVENT_CAMERA_DETACHED`, `EVENT_DEVICE_MISSING`, `EVENT_RESTART_APP`, `EVENT_OS_UPDATE`, `EVENT_FRONTEND_LOAD_TIMEOUT_ERROR`, ...); parameters `DEVICE_DISPLAY_NAME`, `DEVICE_ID`, `SERIAL_NUMBER`, `EVENT_DATA`, `AFFECTED_PERIPHERAL` | same as above |
| `/usage/dates/{YYYY-MM-DD}` | One Pacific-time day of the customer usage report. **Always pass `parameters=meet:num_calls,...`** -- without it every application's figures come back | `parameters`, `pageToken` |

```
# Who presented in a meeting (raw, saved to the workspace)
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/meet?eventName=presentation_started&filters=meeting_code==abc-defg-hij&startTime=2026-10-01T00:00:00Z", "output_file": "google-admin/presentations.json"})

# Endpoints with round-trip time above 300 ms (raw)
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/meet?eventName=call_ended&filters=network_rtt_msec_mean%3E300&maxResults=1000", "output_file": "google-admin/high-rtt.json"})

# Conference rooms in one building
tool_call(tool_name="authed_get", arguments={"url": "https://admin.googleapis.com/admin/directory/v1/customer/my_customer/resources/calendars?query=resourceCategory=CONFERENCE_ROOM%20AND%20buildingId=HQ&fields=items(resourceName,capacity,floorName,resourceEmail),nextPageToken"})
```

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
