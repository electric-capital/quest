# Admin Impersonation

Admin users can impersonate other users to debug issues or view the application as that user. The feature spans session management, dedicated API endpoints, and frontend UI changes.

## Overview

Admins (users whose email is in the `admin_emails` list in `server_config.json`) can select another user from a picker and start an impersonation session. During impersonation, the admin sees the app as the target user while the session cookie carries both identities. Impersonation is time-limited (1 hour) and validated on every request.

## Key Files

**Backend:**
- `auth/session.py` -- Cookie deserialization with `imp` field handling; annotates user dict with `_impersonator_uid`, `_impersonator_email`, `_impersonator_name`
- `chat/auth.py` -- `is_admin()` function used to validate impersonator privileges
- `chat/routes/admin.py` -- Impersonation endpoints (`admin_list_users`, `admin_impersonate`, `admin_stop_impersonation`)
- `chat/routes/user.py` -- `GET /app/api/me` exposes impersonation state fields
- `db/user_store.py` -- `list_all_users()` returns all users with safe fields (id, email, name)

**Frontend:**
- `frontend/src/components/AdminOpsMenu.tsx` -- Impersonation UI (user picker, amber button state, end impersonation button)
- `frontend/src/components/AdminOpsMenu.css` -- Amber/orange styling for impersonation state
- `frontend/src/contexts/AuthContext.tsx` -- `isImpersonating`, `impersonatorEmail`, `impersonatorName` state values
- `frontend/src/api/client.ts` -- `fetchAdminUsers()`, `impersonateUser()`, `stopImpersonation()` API functions
- `frontend/src/api/config.ts` -- Endpoint URL builders (`adminUsers`, `adminImpersonate`, `adminStopImpersonation`)
- `frontend/src/api/types.ts` -- `is_impersonating`, `impersonator_email`, `impersonator_name` on the `/me` response type

## Impersonation Flow

1. Admin clicks the wrench icon (AdminOpsMenu) and selects "Impersonate" -- `frontend/src/components/AdminOpsMenu.tsx`
2. Frontend fetches user list via `GET /app/api/admin/users` -- `chat/routes/admin.py` (`admin_list_users`). Returns all users except the requesting admin
3. Admin selects a target user. Frontend sends `POST /app/api/admin/impersonate` with `{user_id}` -- `chat/routes/admin.py` (`admin_impersonate`)
4. Backend validates admin status, prevents nested impersonation, creates a signed cookie `{v, uid: target, imp: admin}` with 1-hour max_age -- `auth/session.py`
5. Frontend reloads; `GET /app/api/me` returns the target user's identity plus `is_impersonating: true`, `impersonator_email`, `impersonator_name` -- `chat/routes/user.py`
6. AuthContext stores impersonation state; AdminOpsMenu renders amber button with impersonation info -- `frontend/src/contexts/AuthContext.tsx`, `frontend/src/components/AdminOpsMenu.tsx`

## Ending Impersonation

1. Admin clicks "End impersonation" in AdminOpsMenu -- `frontend/src/components/AdminOpsMenu.tsx`
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

## Constraints

- Nested impersonation is blocked: if the current session already has `_impersonator_uid`, `POST /admin/impersonate` returns 400
- Impersonation cookies expire after 1 hour (vs. 30 days for normal sessions)
- The user list endpoint (`GET /admin/users`) excludes the requesting admin from results
- All three admin impersonation endpoints require admin status; non-admins receive 403

## Design Decisions

**Why carry the impersonator ID in the cookie rather than a separate server-side session store?**
The existing auth architecture is stateless (signed cookies, no server-side session table). Adding impersonation via the same cookie mechanism keeps the architecture consistent and avoids introducing session storage infrastructure. The cookie is signed and validated on every request, so tamper resistance is maintained.

**Why a 1-hour max_age for impersonation cookies?**
Impersonation is a privileged operation. A short-lived cookie limits the blast radius if an admin forgets to end an impersonation session or if the cookie is somehow leaked.

**Why validate the impersonator on every request instead of just at impersonation start?**
An admin's access can be revoked at any time by editing `server_config.json`. Validating on every request ensures revocation takes effect immediately, not just when the impersonation cookie expires.

**Why navigate to `/` when ending impersonation?**
The admin may be viewing a conversation URL that belongs to the impersonated user. Navigating to the root avoids 404s or permission errors when the session switches back to the admin's own identity.
