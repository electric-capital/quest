# Admin Impersonation

Admin users can impersonate other users to debug issues or view the application as that user. The feature spans session management, dedicated API endpoints, and the frontend admin menu.

## Overview

Admins (users whose email is in the `admin_emails` list in `server_config.json`) can select another user from a picker and start an impersonation session. During impersonation, the admin sees the app as the target user while the session cookie carries both identities. Impersonation is time-limited (1 hour) and validated on every request. An impersonation session cannot use the routes that issue or reveal credentials or destroy the account, and every request it makes is written to the log under the admin's name.

## Key Files

**Backend:**
- `auth/session.py` -- Cookie deserialization with `imp` field handling; annotates user dict with `_impersonator_uid`, `_impersonator_email`, `_impersonator_name`. Also holds the `forbid_impersonation` dependency and the audit line (`ImpersonationAuditMiddleware` plus the hook in `get_user_from_cookie()`)
- `quest.py` -- Installs `ImpersonationAuditMiddleware` on the app
- `config/plugins.py` -- `mount_plugin_oauth_routers()` adds `forbid_impersonation` to every plugin connection router
- `chat/auth.py` -- `is_admin()` function used to validate impersonator privileges
- `chat/routes/admin.py` -- Impersonation endpoints (`admin_list_users`, `admin_impersonate`, `admin_stop_impersonation`)
- `chat/routes/user.py` -- `GET /app/api/me` exposes impersonation state fields
- `db/user_store.py` -- `list_all_users()` returns all users with safe fields (id, email, name)

**Frontend:**
- `frontend/src/components/AdminOpsMenu.tsx` -- The admin menu hosting the user picker and the end-impersonation action; rendered inline in the sidebar's `UserInfoBar.tsx`, or floating on the System Reports page, which has no sidebar
- `frontend/src/contexts/AuthContext.tsx` -- `isImpersonating`, `impersonatorEmail`, `impersonatorName` state values
- `frontend/src/api/client.ts` -- `fetchAdminUsers()`, `impersonateUser()`, `stopImpersonation()`
- `frontend/src/api/types.ts` -- `is_impersonating`, `impersonator_email`, `impersonator_name` on the `/me` response type

## Impersonation Flow

1. Admin opens the admin menu and chooses to impersonate -- `frontend/src/components/AdminOpsMenu.tsx`
2. Frontend fetches user list via `GET /app/api/admin/users` -- `chat/routes/admin.py` (`admin_list_users`). Returns all users except the requesting admin
3. Admin selects a target user. Frontend sends `POST /app/api/admin/impersonate` with `{user_id}` -- `chat/routes/admin.py` (`admin_impersonate`)
4. Backend validates admin status, prevents nested impersonation, creates a signed cookie `{v, uid: target, imp: admin}` with 1-hour max_age -- `auth/session.py`
5. Frontend reloads; `GET /app/api/me` returns the target user's identity plus `is_impersonating: true`, `impersonator_email`, `impersonator_name` -- `chat/routes/user.py`
6. AuthContext stores the impersonation state, and AdminOpsMenu shows it and hides the admin-only items while it lasts -- `frontend/src/contexts/AuthContext.tsx`, `frontend/src/components/AdminOpsMenu.tsx`

## Ending Impersonation

1. Admin ends the impersonation from AdminOpsMenu -- `frontend/src/components/AdminOpsMenu.tsx`
2. Frontend sends `POST /app/api/admin/stop-impersonation` -- `chat/routes/admin.py` (`admin_stop_impersonation`)
3. Backend reads the current cookie to extract `_impersonator_uid`, creates a new normal cookie for the admin with 30-day max_age
4. Frontend navigates to `/` (to avoid stale conversation URLs from the impersonated user's session) and reloads

## Session Validation

On every request, `get_user_from_cookie()` in `auth/session.py` checks if the cookie contains an `imp` field. If present:

- Validates `imp` is a positive integer
- Looks up the impersonator user via `get_user_by_id()`
- Confirms the impersonator is still an admin via `is_admin()`
- If any check fails, the cookie is treated as invalid (returns `None`), effectively ending the impersonation

This means revoking an admin's access (removing their email from `admin_emails` in `server_config.json`) immediately invalidates all their active impersonation sessions.

## Forbidden Routes

Routes that issue, reveal, rotate or revoke credentials, change the password or destroy the account declare `forbid_impersonation` (`auth/session.py`), which answers 403 `impersonation_forbidden` to an impersonation session:

- the `users.api_key` surfaces: `GET /api/instructions` (embeds the key) and `POST /api/reset-api-key` in `quest.py`, `POST /auth/reset-api-key` in `auth/google_login.py`, and the deprecated `GET /app/api/user` in `chat/routes/user.py`
- every inference API key route (`chat/routes/inference_api_keys.py`)
- password change (`auth/password_login.py`), logout-and-disconnect and delete-account (`chat/routes/user.py`)
- every credential-connection route: the core routers declare the dependency on the router itself (`auth/connections.py`, `auth/service_key.py`, `auth/airtable.py`, `auth/ramp.py`, `auth/google_services.py`), and `mount_plugin_oauth_routers()` adds it to each plugin's `/auth/<id>` router

The dependency reads only the signed cookie's `imp` claim, with no database lookup, so it runs before the route's own user resolution. Any correctly signed cookie with an `imp` field counts, including one whose impersonation no longer validates.

Everything else stays available to an impersonation session: conversations, files, user settings, connector status, the tools acting on the target's data, logout and stop-impersonation. Those requests are covered by the audit line. In the frontend only the password form (`frontend/src/components/settings/PasswordSection.tsx`) is hidden while impersonating; the other settings that call forbidden routes stay visible and their requests fail with the 403.

## Audit Log

Every request served under impersonation writes one INFO line on the `auth.session` logger, prefixed `[impersonation]`, naming the admin's and the target's email and user id plus the request method and path (never the query string, which can carry OAuth codes). The target appears as `user=<email>`, following the [Logging](logging.md) convention.

- `get_user_from_cookie()` writes the line once it has validated the impersonation, so a request that resolves no identity writes none.
- `ImpersonationAuditMiddleware` (pure ASGI, installed in `quest.py`) gives each HTTP request and WebSocket connection a record, held in a context variable, with its method and path, and limits it to one line. A WebSocket logs once at connect (method `WEBSOCKET`); its heartbeat re-validations do not repeat it.
- A request refused by `forbid_impersonation` never resolves the identity, so the dependency logs its own WARNING `[impersonation] refused` line with both user ids. The `[admin] ... started impersonating ...` line from `admin_impersonate` maps the ids to emails.

## Constraints

- Credential and account routes return 403 `impersonation_forbidden` while impersonating (see [Forbidden Routes](#forbidden-routes))
- Nested impersonation is blocked: if the current session already has `_impersonator_uid`, `POST /admin/impersonate` returns 400
- Impersonation cookies expire after 1 hour (vs. 30 days for normal sessions)
- The user list endpoint (`GET /admin/users`) excludes the requesting admin from results
- All three admin impersonation endpoints require admin status; non-admins receive 403

## Design Decisions

**Why carry the impersonator ID in the cookie rather than a separate server-side session store?**
The existing auth architecture is stateless (signed cookies, no server-side session table). Adding impersonation via the same cookie mechanism keeps the architecture consistent and avoids introducing session storage infrastructure. The cookie is signed and validated on every request, so tamper resistance is maintained.

**Why refuse credential and account routes instead of trusting the support use?**
Anything an impersonation session could mint or store (an API key, an inference key, a connection credential) outlives the one-hour window, and revoking every connection or deleting the account cannot be undone. Hiding these in the frontend would leave the routes callable, so the server refuses them.

**Why check the cookie claim rather than the resolved user?**
Routes resolve the user in different ways (dependencies, inline `get_user_from_cookie()`, plugin routers). A request-level check on the signed `imp` claim covers all of them, runs before any of them, needs no database lookup, and fails closed.

**Why write the audit line in `get_user_from_cookie()` rather than in the middleware?**
Only identity resolution knows that the impersonation is valid and has both users' emails. The middleware only supplies the request's method and path.

**Why a 1-hour max_age for impersonation cookies?**
Impersonation is a privileged operation. A short-lived cookie limits the blast radius if an admin forgets to end an impersonation session or if the cookie is somehow leaked.

**Why validate the impersonator on every request instead of just at impersonation start?**
An admin's access can be revoked at any time by editing `server_config.json`. Validating on every request ensures revocation takes effect immediately, not just when the impersonation cookie expires.

**Why navigate to `/` when ending impersonation?**
The admin may be viewing a conversation URL that belongs to the impersonated user. Navigating to the root avoids 404s or permission errors when the session switches back to the admin's own identity.
