# Auth Submodule Architecture

This document describes the `auth/` submodule, which contains all OAuth flows, session management, credential handling, and authentication-related configuration for the Quest platform.

## Overview

Auth-related functionality was extracted from `quest.py` into a dedicated `auth/` submodule. The submodule handles Google OAuth (app login and Google Services), Airtable token management, generic per-user plugin API keys (`auth/service_key.py`), dev-only email login (for testing multi-user features without Google accounts), session cookie serialization, FastAPI auth dependencies, and OAuth popup HTML generators. The `auth/__init__.py` re-exports commonly used symbols and exposes FastAPI routers that the main app registers.

## Router Registration

The main app in `quest.py` registers the routers from the auth submodule:

1. `google_login_router` -- App login endpoints (`/auth/`, `/auth/login`, `/auth/callback`, `/auth/login-url`, `/auth/logout`)
2. `google_services_router` -- Google Services OAuth (`/auth/google-services`, `/auth/google-services/callback`)
3. `airtable_router` -- Airtable token management (`/auth/airtable/save-token`, `/auth/airtable/remove-token`)
4. `dev_login_router` -- Dev-only email login (`POST /auth/dev-login`)
5. `password_login_router` -- Email/password sign-in (`/auth/password/*`, see Sign-in Methods below)

Plugin OAuth routers (e.g. the Slack plugin's `/auth/slack`, `/auth/slack/callback`, `/auth/slack/disconnect`, the GitHub plugin's `/auth/github/*`, the Twitter/X plugin's `/auth/twitter`, `/auth/twitter/callback`, `/auth/twitter/disconnect`, and the Telegram plugin's non-OAuth login router `/auth/telegram`, `/auth/telegram/send-code`, `/auth/telegram/verify`, `/auth/telegram/2fa`, `/auth/telegram/disconnect`) are mounted separately by `mount_plugin_oauth_routers()` in quest.py after plugin load -- see [Plugin Architecture](plugins.md).

## Sign-in Methods

A deployment signs users in with exactly one of two methods, chosen by the `login_method` key in `server_config.json` and read fresh on every call by `login_method()` / `is_password_login()` in `auth/config.py`:

- `"google"` -- Google OAuth (`auth/google_login.py`). Also what an absent or unknown value means, so deployments that predate password sign-in are unchanged.
- `"password"` -- email + password accounts (`auth/password_login.py`). The prod bootstrap wizard's default for new deployments because it needs nothing set up outside Quest (see [Production](../setup/production.md#first-run-bootstrap)).

Enforcement of "one method at a time": every `/auth/password/*` route 404s `password_login_disabled` unless the method is password; under password sign-in `/auth/login-url` 404s `google_login_disabled`, `/auth/callback` renders a 404 error page, and the `/auth/` HTML page redirects to the SPA. Password-issued session cookies carry a `pw` field (see Session binding below) and `get_user_from_cookie()` in `auth/session.py` rejects them whenever the method is not password. The Google Services connector flow (`auth/google_services.py`) is independent of the sign-in method and works under both.

**Accounts are the same `users` rows under both methods, keyed by email.** The Google login callback already looks users up by email, so after a switch everyone signs in with the Google account of the same address and keeps their conversations, projects and connections; `google_sub` is filled in on that first Google login. Password hashes stay in place after a switch (unused), so reverting only takes a config edit.

**Switching.** Admin Settings > Sign-in (`frontend/src/components/settings/SignInSection.tsx`) calls `PUT /app/api/admin/sign-in/login-method` (`chat/routes/admin.py`), which only accepts `"google"`, refuses until the Google OAuth client is configured (`_google_login_configured()`), and writes the key via `update_server_config()` in `config/server_config.py` (atomic temp-file + rename, other keys preserved). Password sessions -- including the admin's own -- end immediately. Going back to password sign-in is deliberately a hand edit of `server_config.json` (`"login_method": "password"`); Google-issued sessions (no `pw` field) stay valid then, and users without a password can set one in Settings > Password without a current password.

### Email/password flow

Key files: `auth/password_login.py` (routes, rate limiting, startup hook), `config/password_hashing.py` (stdlib-only scrypt hashing, password policy, fingerprints, and the bootstrap pending-passwords file), `db/password_store.py` (hash + token data access), `auth/mailer.py` (SMTP), `frontend/src/components/SignInScreen.tsx` (`PasswordSignIn`), `frontend/src/components/SetPasswordScreen.tsx`, `frontend/src/components/settings/PasswordSection.tsx`.

- **Storage.** `users.password_hash` (nullable; `scrypt$n$r$p$salt$hash`, parameters carried in the string so costs can rise later) and the `password_tokens` table (one-time set-password links keyed by lowercased email, SHA-256 of the raw token only, purpose `invite`/`reset`, expiry, `used_at`). Migration `e5b2c8f14a37`. The hash never rides on user dicts -- `User.to_dict()` exposes only `password_fp`, a 16-hex-char fingerprint of it.
- **Passwords are only ever set through a one-time link** (`POST /auth/password/set`), which creates the account when missing (optional display name, default derived from the email) and signs the user in. Links come from: an admin (`POST /app/api/admin/sign-in/password-links`: 7-day `invite` link returned for the admin to hand over and optionally emailed; an address outside the admission policy is appended to `allowed_login_emails`, since inviting is how an admin grants access on a password deployment); self-service `POST /auth/password/request-link` (1-hour `reset` link, emailed in a background task -- for an address with no account this is sign-up, and receiving the email proves ownership; requires SMTP, answers the same generic message whether or not the address may sign in); and the bootstrap wizard for the first admins (`apply_pending_admin_passwords()`, run from the `quest.py` lifespan, which also purges stale tokens).
- **The link token travels in the URL fragment** (`/set-password#token=...`, built by `set_password_url()` on `oauth_base_url()`), so it never reaches server access logs or Referer headers; the page posts it to `POST /auth/password/link-info` / `/auth/password/set` in JSON bodies. `quest.py` serves the SPA at `/set-password`, routed to `SetPasswordScreen` outside the auth gate in `frontend/src/App.tsx`. Using a link or setting a new password voids every other outstanding link for the address (`set_password_hash()`), and consumption is an atomic conditional UPDATE.
- **Sign-in.** `POST /auth/password/login` verifies in a worker thread; unknown accounts spend the same scrypt time (`burn_verify_time()`) and get the same 401 `invalid_credentials` as a wrong password. In-memory sliding-window limits (`_SlidingWindow`): 10 failures per account and 50 per client IP per 15 minutes (429 `rate_limited`), one link email per address per minute and 10 link requests per IP per 15 minutes. Limits are per process and reset on restart. The admission policy (`check_user_allowed()`) is applied on login and on link use exactly as on every request.
- **Session binding.** `set_session_cookie()` in `auth/session.py` puts the password fingerprint into the cookie (`pw`). Because each hash has a fresh salt, a password change or reset signs out every session issued under the old password. `POST /auth/password/change` (current password required when one is set; refused while impersonating) re-issues the caller's cookie. Stop-impersonation re-binds the admin's restored cookie the same way.
- **Outgoing email** is optional: the core `smtp` credential service (`config/service_specs.py`, admin Settings > Service Credentials > "Outgoing email (SMTP)": host, port, implicit TLS, username/password, from address). `auth/mailer.py` sends plain-text mail via `smtplib` in a thread, upgrades with STARTTLS when offered, and refuses to send credentials over an unencrypted connection. `GET /app/api/config` reports `login_method` and `password_self_service` (SMTP configured) so the sign-in screen shows either "Forgot password? / Create an account" or "ask an administrator for a sign-in link".
- `GET /app/api/me` returns `has_password`, and Settings > Password is listed only under password sign-in.

## OAuth State Cookies

Every browser-redirect OAuth flow -- the app login (`auth/google_login.py`), Google Services (`auth/google_services.py`), Ramp (`auth/ramp.py`) and each oauth-kind plugin connector (`plugins/<id>/oauth.py`) -- mints its CSRF `state` nonce through the shared helper in `auth/oauth_state.py` rather than writing its own cookie.

- `mint_oauth_state(cookie_name, user_id=..., popup=..., extra=...)` returns the nonce (passed as the provider's `state` query parameter) plus a signed cookie value; `.attach(response)` sets it as HttpOnly, SameSite=Lax, `Secure` whenever the session cookie is (`COOKIE_SECURE`), with a 10-minute `max_age` (`STATE_TTL_SECONDS`).
- The payload is signed with a timed `itsdangerous` serializer under its own salt and carries `csrf` (the nonce), `uid` (the session user's id, `None` for the app login where no session exists yet), `popup`, and flow-specific extras -- the Twitter/X flow keeps its PKCE `code_verifier` here, so it never leaves the server in plaintext.
- `verify_oauth_state(request, cookie_name, state, user_id=...)` in the callback checks signature, age, a constant-time nonce comparison, and that `uid` matches the logged-in user (connector callbacks resolve the session first, then verify). Any failure renders a "Security Error" page and drops the cookie via `clear_oauth_state()` before any token exchange; every success and failure path also clears the cookie so a state cannot be presented twice.

**Design decision**: a signed, session-bound cookie gives the same tamper resistance as a server-side state store without a new table or TTL sweeper -- an attacker who can overwrite the browser's cookies (sibling subdomain, plain-HTTP network position) can only break a flow, not steer its nonce or verifier. Strict single-use inside the 10-minute window is the one property a server store would add; PKCE plus the session binding make that marginal. The app login callback previously accepted any `code` without a `state` check at all; it now refuses callbacks that do not carry the nonce minted with the sign-in URL (`GET /auth/login-url` and the `/auth/` page both set the cookie).

## OAuth Callback Hostname Override

Every OAuth start and callback handler builds its `redirect_uri` via `oauth_base_url(request)` in `auth/config.py` instead of reading `request.base_url` directly. With no configuration it returns the request's own base URL.

When the deployment's full public URL is configured -- the top-level `app_base_url` key in `server_config.json` (env override: `QUEST_APP_BASE_URL`), read via `get_app_base_url()` in `config/server_config.py` -- it is used verbatim as the callback base, taking precedence over everything below. This is the same key that app-generated absolute links (e.g. the Slack "created using Quest" attribution) use, so a production deployment configures its public address exactly once; the prod bootstrap wizard prompts for it.

Otherwise, when the optional top-level `oauth_hostname` key in `server_config.json` (env override: `QUEST_OAUTH_HOSTNAME`) is set, the request's hostname is replaced by that value while the scheme and port are preserved -- so a dev server reached at `http://10.1.2.3:9000` generates callbacks like `http://dev.example.com:9000/auth/callback`. The hostname-only form suits multi-checkout dev boxes sharing one `dev-config.json`: each instance runs on its own port, which a single full URL could not express.

This exists because providers such as Google reject redirect URIs containing raw IP addresses. Developers add an `/etc/hosts` entry pointing the override hostname at the server's IP, register the hostname-based callback URLs with the provider, and browse the app through the hostname (required anyway: session and OAuth state cookies are host-scoped, so starting a flow on the IP origin and returning to the hostname origin would drop them).

## Google Account Identity (`users.google_sub`)

The app-login callback (`auth/google_login.py`) persists the Google account's stable subject identifier alongside the email: the v2 userinfo endpoint's `id` field is the OAuth `sub` claim, stored in the nullable, unique `users.google_sub` column (Alembic revision `b8d41f6a9c27`). Both the new-user and existing-user paths write it, so accounts created before the column existed pick it up on their next Google login. Dev logins (`auth/dev_login.py`) and local seeding (`scripts/seed_local.py`) generate deterministic `dev-`-prefixed fake subs so local flows exercise the column.

Integrations that resolve users by Google identity read this column first; a plugin can additionally perform a lazy backfill for users who have not re-logged-in (refresh the stored login token via `get_valid_credentials()`, fetch userinfo, persist the sub).

## Logging

The `auth` logger namespace is configured at INFO level by the unified logging configuration in `chat/logging_config.py` and `logging_config.json`. Auth submodule files use `logger = logging.getLogger(__name__)` (resolving to e.g. `auth.google_login`, `auth.slack`), which inherits from the `auth` logger. Log entries include `user=<email>` for OAuth events, token refresh, login, service authorization, API key resets, and plugin service-key saves. See [Logging Architecture](logging.md).

## Environment-Based Cookie Names

`COOKIE_NAME` in `auth/config.py` is computed dynamically from the `QUEST_ENV` environment variable to prevent dev/prod cookie collisions when both environments share the same browser (e.g., same hostname on different ports).

- `_COOKIE_BASE_NAME = "quest_session"` is the base name
- `_quest_env` is read from `os.environ.get("QUEST_ENV", "prod")`
- `QUEST_ENV=prod` (or unset) results in `COOKIE_NAME = "quest_session"` (backward compatible)
- `QUEST_ENV=dev` results in `COOKIE_NAME = "quest_session_dev"`
- Any other value (e.g., `staging`) results in `COOKIE_NAME = "quest_session_staging"`

The `QUEST_ENV` variable is set automatically by `run.py` (`--prod` sets `prod`, `--dev` sets `dev`). See [Development Setup](../setup/development.md) for how to run the app.

In addition to cookie naming, `QUEST_ENV` also drives UI branding: the unauthenticated `GET /app/api/config` endpoint in `chat/routes/user.py` exposes the environment value, and the frontend uses it to display "DevQuest" (when `dev`) or "Quest" (otherwise) in the page title and headings. See [Chat API](../api/chat-api.md) for endpoint details.

## Dev Login

When `QUEST_ENV=dev`, the `POST /auth/dev-login` endpoint enables email-based login without Google OAuth, useful for testing multi-user features locally.

**Key Files:** `auth/dev_login.py` (endpoint), `chat/auth.py` (`check_user_allowed()` relaxation), `frontend/src/components/SignInScreen.tsx` (dev login UI), `frontend/src/contexts/AppConfigContext.tsx` (`isDevMode` boolean)

**Flow:**

1. Frontend fetches `GET /app/api/config` and detects `quest_env === "dev"`, sets `isDevMode = true` in AppConfigContext
2. SignInScreen shows an email input field and "Dev Login" button below the Google sign-in button (separated by an "or" divider)
3. User enters any email address and clicks "Dev Login"
4. Frontend POSTs `{"email": "..."}` to `/auth/dev-login` with `credentials: 'include'`
5. Backend validates email format, looks up existing user or creates a new one with a deterministic name from canned first/last name lists (MD5 hash of email selects indices into `FIRST_NAMES` and `LAST_NAMES` arrays in `auth/dev_login.py`)
6. If the user already has `google_oauth` credentials, the endpoint returns 403 (must use Google sign-in instead)
7. Backend sets the same versioned session cookie (`{"v": COOKIE_VERSION, "uid": user_id}`) as the Google OAuth login flow
8. Frontend reloads the page, session check succeeds, and the chat interface loads

**Access check relaxation:** `check_user_allowed()` in `chat/auth.py` returns `True` for all emails in local mode, allowing dev-login users with addresses outside the allowed domain to access the app. The Google login callback (`auth/google_login.py`) is stricter: it always enforces `is_login_allowed()` from `auth/config.py`, which admits an email when it is on the allowed domain OR (case-insensitively) in the optional `allowed_login_emails` whitelist in `server_config.json` -- individual accounts outside any single domain, e.g. personal `@gmail.com` users on a family deployment; the prod bootstrap wizard prompts for both.

The domain is resolved by `allowed_login_domain()`:

- local mode only, `QUEST_ALLOWED_LOGIN_DOMAIN` (pre-baked by `run.py` from the shared parent-directory `dev-config.json`, so a real Google sign-in with a test account can complete; the env var is ignored in staging/prod);
- then the `allowed_login_domain` key in `server_config.json` in all modes (written by the prod bootstrap wizard so a deployment for another organization can admit its own domain);
- then the hardcoded `ALLOWED_DOMAIN`.

Outside local mode `check_user_allowed()` enforces the same `is_login_allowed()` rule on every API request: `require_user_allowed()` in `chat/auth.py` raises 403 `access_denied` and is called from EVERY request-authenticating dependency (`get_current_user` and `get_current_user_cookie_or_apikey` in `auth/session.py`, `get_current_user_cookie_or_apikey` and its now-identical `_checked` alias in `chat/auth.py`).

So the script tool-call bridge, the `/api/authed-get|post` proxy, the Gmail Simple routes and the `/api/instructions` + `/api/reset-api-key` root endpoints reject an offboarded user's still-valid cookie or `users.api_key` exactly like the `/app/api/*` routes (finding #279231; session cookies never expire on their own, so this per-request check is the only revocation).

UI messages describe the restriction via `login_restriction_description()` ("@domain accounts", or "approved accounts" when a whitelist is configured -- the whitelist itself is never enumerated on unauthenticated surfaces like the sign-in page or `GET /app/api/config`, which now also returns a human-readable `login_restriction` field consumed by the sign-in screen).

**Non-dev mode:** The endpoint returns 404 when `QUEST_ENV` is not `dev`, hiding its existence in production. The frontend does not render the dev login UI unless `isDevMode` is true.

## Admin Impersonation

Admin users can impersonate other users to debug issues or view the app as that user. The session cookie format extends with an `imp` field carrying the impersonator's user ID: `{"v": COOKIE_VERSION, "uid": target_user_id, "imp": admin_user_id}`. Impersonation cookies have a 1-hour max_age (vs. 30 days for normal sessions).

When `get_user_from_cookie()` in `auth/session.py` detects the `imp` field, it validates the impersonator is still a valid admin (checks `is_admin()` from `chat/auth.py`). If validation passes, the returned user dict is annotated with `_impersonator_uid`, `_impersonator_email`, and `_impersonator_name`. If the impersonator is no longer valid or no longer admin, the cookie is treated as invalid.

The `GET /app/api/me` endpoint in `chat/routes/user.py` exposes `is_impersonating`, `impersonator_email`, and `impersonator_name` fields derived from these annotations.

See [Admin Impersonation](admin-impersonation.md) for the full feature description including endpoints and frontend behavior.

## Design Decisions

**Why a separate dev-login endpoint instead of a mock Google OAuth flow?**
A real Google OAuth flow requires valid credentials, a Google Cloud project, and browser redirects. For local multi-user testing, a simple email-based login is faster and requires no external dependencies. The endpoint is completely inert in non-dev mode (returns 404), so there is no security surface in production.

**Why block users with existing Google OAuth credentials?**
If a user has already authenticated via Google, allowing dev-login for the same email could create session confusion (two auth paths for one account). Blocking dev-login for such users forces them to use the real Google flow, keeping the auth state consistent.

**Why environment-based cookie names?**
In development, the backend often runs on the same hostname as production (e.g., via Tailscale). Without distinct cookie names, a dev login overwrites the production session cookie, logging the user out of production (and vice versa). Environment-suffixed cookie names isolate dev and prod sessions in the same browser.

**Why extract auth into a submodule?**
The `quest.py` file had grown to include ~1,700 lines of auth-related code alongside other application logic. Extracting auth into `auth/` improves code organization, makes auth flows independently navigable, and reduces the cognitive load when working on either auth or non-auth features.

**Why re-export from `__init__.py`?**
The `auth/__init__.py` re-exports commonly used symbols so that consumer code can import from `auth` directly (e.g., `from auth import get_current_user`) without needing to know the internal file structure. This also preserves backward compatibility for any code that previously imported from `quest`.

**Why separate `config.py` from `session.py`?**
Configuration constants and credential loaders (`config.py`) are stateless and widely imported. Session management (`session.py`) involves cookie serialization and FastAPI dependencies. Separating them prevents circular imports and keeps each file focused on a single concern.

**Why keep `google_credentials.py` separate from `google_login.py` and `google_services.py`?**
Credential management functions (`get_valid_service_credentials()`, `make_authenticated_request()`) are used by API endpoint modules (Gmail, Drive, Docs, Sheets, Tasks), not just by the OAuth flow endpoints. Keeping them in a shared module avoids duplication and ensures consistent credential handling across all consumers.

**Why store granted (not requested) Google Services scopes at the callback?**
`auth_google_services_callback()` in `google_services.py` persists `credentials.granted_scopes` (falling back to `GOOGLE_SERVICE_SCOPES` only when Google returns no `scope` field), not the requested set. The `needs_reauth` connector check compares the stored scopes against `GOOGLE_SERVICE_SCOPES`; storing the requested set verbatim would mask a partial grant (a user de-selecting a scope, or a token-upgrade re-consent) and leave a connector showing "Connected" while calls 403 with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. This matters most for the broad `cloud-platform` GCP scope -- see [GCP API Documentation](../api/gcp-api.md).

**Why does the Google Services callback fail closed when UserInfo does not answer?**
`auth_google_services_callback()` proves the authorizing Google account is the logged-in Quest account by calling the UserInfo endpoint with the freshly exchanged token and comparing the returned email (case-insensitively) against the session email. A non-200 UserInfo response, or a 200 without an `email`, aborts the flow without persisting anything: an unverifiable token could belong to another Google account picked at the consent screen, and storing it would silently bind that account's data to the Quest user.

So the check can actually succeed, `get_google_services_oauth_flow()` requests `GOOGLE_SERVICES_IDENTITY_SCOPES` (`openid` + `userinfo.email`) alongside `GOOGLE_SERVICE_SCOPES` -- `openid` must be requested explicitly because Google adds it to the granted set whenever a `userinfo.*` scope is requested, and google-auth-oauthlib raises "Scope has changed" on `fetch_token` if requested and granted differ; the identity scopes are deliberately kept out of `GOOGLE_SERVICE_SCOPES` because the `needs_reauth` subset check compares stored scopes against that list and widening it would flag every existing connection for re-consent.

The cross-user variant of this (an attacker-minted callback URL replayed in a victim's browser) is separately blocked by the session-bound `state` cookie above.

**Why do `get_valid_credentials()` and `get_valid_service_credentials()` mutate the `user` dict in-place?**
Within a single WebSocket session, the same `user` dict is passed to multiple API calls. If a token refresh writes the new token to the database but does not update the in-memory `user` dict, subsequent calls in the same session still see the stale expired token and trigger redundant refreshes to Google. Mutating `user["google_oauth"]` (or `user["google_services_oauth"]`) in-place after each refresh ensures all downstream callers in the same session see the fresh token immediately.

**Why only one sign-in method at a time?**
Running both would give every account two independent credentials with different recovery paths and make "who may sign in" depend on which door someone used. A single switch also makes the Google transition a clean cutover: password sessions end, and the Google callback resolves the same rows by email.

**Why are passwords only set through one-time links, never at sign-up?**
Without a link that proves control of the address, anyone could register an allowed-domain email before its owner -- including an address in `admin_emails`. Admin-issued links (and the bootstrap wizard for the first admins) work without any email setup; self-service links rely on SMTP delivery for the ownership proof.

**Why scrypt from `hashlib` instead of a password-hashing package?**
The bootstrap wizard must hash the first admin password before any dependency is installed (it is stdlib-only like `run.py`), and the app must verify the same format. `hashlib.scrypt` is memory-hard and available everywhere Python links OpenSSL.

## Constraints

- The `auth/` submodule handles only authentication, authorization, and credential management. API endpoint logic (Gmail, Airtable, etc.) remains in the `api/` directory (GitHub, Slack, Twitter/X and Telegram access is plugin-provided -- see [Plugins](plugins.md)). Plugin oauth-kind connection routers (e.g. the GitHub plugin's `/auth/github` flow) are mounted separately by `mount_plugin_oauth_routers()` after plugin load.
- `quest.py` still contains the `/api/instructions` endpoint, the `/api/reset-api-key` endpoint, and static file serving. Instruction functions (`get_instructions_content()`, `get_user_connected_services()`) and per-service instruction text have been extracted to the `api/` package (see `api/instructions.py` and the `get_instructions()` functions in each API submodule).
- The ten auth routers must be registered before API routes and catch-all static routes in the main app to ensure correct route priority.
- The dev login endpoint (`/auth/dev-login`) is only functional when `QUEST_ENV=dev`. In all other environments it returns 404.
