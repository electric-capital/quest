# Auth Submodule Architecture

This document describes the `auth/` submodule, which contains all OAuth flows, session management, credential handling, and authentication-related configuration for the Quest platform.

## Overview

The `auth/` submodule handles Google OAuth (app login and Google Services), Airtable token management, generic per-user plugin API keys (`auth/service_key.py`), per-connection management for multi-connection plugins (`auth/connections.py`, see [User Connections](user-connections.md)), dev-only email login (for testing multi-user features without Google accounts), session cookie serialization, FastAPI auth dependencies, and OAuth popup HTML generators. The `auth/__init__.py` re-exports commonly used symbols and exposes FastAPI routers that the main app registers.

## Router Registration

The main app in `quest.py` registers the routers from the auth submodule:

1. `google_login_router` -- App login endpoints (`/auth/`, `/auth/login`, `/auth/callback`, `/auth/login-url`, `/auth/logout`)
2. `google_services_router` -- Google Services OAuth (`/auth/google-services`, `/auth/google-services/callback`)
3. `airtable_router` -- Airtable token management (`/auth/airtable/save-token`, `/auth/airtable/remove-token`)
4. `ramp_router` -- Ramp OAuth (`/auth/ramp`, see [Ramp API](../api/ramp-api.md))
5. `service_key_router` -- single-connection plugin API keys (`/auth/service-key/{service}`, `.../remove`)
6. `connections_router` -- multi-connection plugin connections (`POST /auth/connections/{service}`, `PUT`/`DELETE /auth/connections/{service}/{id}`; cookie or API-key auth)
7. `dev_login_router` -- Dev-only email login (`POST /auth/dev-login`)
8. `password_login_router` -- Email/password sign-in (`/auth/password/*`, see Sign-in Methods below)

Plugin connection routers (each confined to its plugin's `/auth/<id>` prefix, OAuth or not) are mounted separately by `mount_plugin_oauth_routers()` in quest.py after plugin load -- see [Plugin Architecture](plugins.md). All of these must be registered before the API routes and the catch-all static routes so route priority holds.

## Sign-in Methods

A deployment signs users in with exactly one of two methods, chosen by the `login_method` key in `server_config.json` and read fresh on every call by `login_method()` / `is_password_login()` in `auth/config.py`:

- `"google"` -- Google OAuth (`auth/google_login.py`). Also what an absent or unknown value means, so a deployment without the key keeps Google sign-in.
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
- **Session binding.** `set_session_cookie()` in `auth/session.py` puts the password fingerprint into the cookie (`pw`); because each hash has a fresh salt, a cookie issued under an older password never verifies again. Setting a password (`set_password_hash()`) also advances the account's session epoch (see Session Cookies below), so a password change or reset signs out every session of the account, Google-issued ones included. `POST /auth/password/change` (current password required when one is set; refused while impersonating) re-issues the caller's cookie under the new epoch. Stop-impersonation re-binds the admin's restored cookie the same way.
- **Outgoing email** is optional: the core `smtp` credential service (`config/service_specs.py`, admin Settings > Service Credentials > "Outgoing email (SMTP)": host, port, implicit TLS, username/password, from address). `auth/mailer.py` sends plain-text mail via `smtplib` in a thread, upgrades with STARTTLS when offered, and refuses to send credentials over an unencrypted connection. `GET /app/api/config` reports `login_method` and `password_self_service` (SMTP configured) so the sign-in screen shows either "Forgot password? / Create an account" or "ask an administrator for a sign-in link".
- `GET /app/api/me` returns `has_password`, and Settings > Password is listed only under password sign-in.

## OAuth State Cookies

Every browser-redirect OAuth flow -- the app login (`auth/google_login.py`), Google Services (`auth/google_services.py`), Ramp (`auth/ramp.py`) and each oauth-kind plugin connector (`plugins/<id>/oauth.py`) -- mints its CSRF `state` nonce through the shared helper in `auth/oauth_state.py` rather than writing its own cookie.

- `mint_oauth_state(cookie_name, user_id=..., popup=..., extra=...)` returns the nonce (passed as the provider's `state` query parameter) plus a signed cookie value; `.attach(response)` sets it as HttpOnly, SameSite=Lax, `Secure` whenever the session cookie is (`COOKIE_SECURE`), with a 10-minute `max_age` (`STATE_TTL_SECONDS`).
- The payload is signed with a timed `itsdangerous` serializer under its own salt and carries `csrf` (the nonce), `uid` (the session user's id, `None` for the app login where no session exists yet), `popup`, and flow-specific extras -- the Twitter/X flow keeps its PKCE `code_verifier` here, so it never leaves the server in plaintext.
- `verify_oauth_state(request, cookie_name, state, user_id=...)` in the callback checks signature, age, a constant-time nonce comparison, and that `uid` matches the logged-in user (connector callbacks resolve the session first, then verify). Any failure renders a "Security Error" page and drops the cookie via `clear_oauth_state()` before any token exchange; every success and failure path also clears the cookie so a state cannot be presented twice.
- The app login callback refuses any callback that does not carry the nonce minted with the sign-in URL (`GET /auth/login-url` and the `/auth/` page both set the cookie).

**Design decision**: a signed, session-bound cookie gives the same tamper resistance as a server-side state store without a new table or TTL sweeper -- an attacker who can overwrite the browser's cookies (sibling subdomain, plain-HTTP network position) can only break a flow, not steer its nonce or verifier. Strict single-use inside the 10-minute window is the one property a server store would add; PKCE plus the session binding make that marginal.

## OAuth Callback Hostname Override

Every OAuth start and callback handler builds its `redirect_uri` via `oauth_base_url(request)` in `auth/config.py` instead of reading `request.base_url` directly. With no configuration it returns the request's own base URL.

When the deployment's full public URL is configured -- the top-level `app_base_url` key in `server_config.json` (env override: `QUEST_APP_BASE_URL`), read via `get_app_base_url()` in `config/server_config.py` -- it is used verbatim as the callback base, taking precedence over everything below. This is the same key that app-generated absolute links (e.g. the Slack "created using Quest" attribution) use, so a production deployment configures its public address exactly once; the prod bootstrap wizard prompts for it.

Otherwise, when the optional top-level `oauth_hostname` key in `server_config.json` (env override: `QUEST_OAUTH_HOSTNAME`) is set, the request's hostname is replaced by that value while the scheme and port are preserved -- so a dev server reached at `http://10.1.2.3:9000` generates callbacks like `http://dev.example.com:9000/auth/callback`. The hostname-only form suits multi-checkout dev boxes sharing one `dev-config.json`: each instance runs on its own port, which a single full URL could not express.

This exists because providers such as Google reject redirect URIs containing raw IP addresses. Developers add an `/etc/hosts` entry pointing the override hostname at the server's IP, register the hostname-based callback URLs with the provider, and browse the app through the hostname (required anyway: session and OAuth state cookies are host-scoped, so starting a flow on the IP origin and returning to the hostname origin would drop them).

## Google Account Identity (`users.google_sub`)

The app-login callback (`auth/google_login.py`) persists the Google account's stable subject identifier alongside the email: the v2 userinfo endpoint's `id` field is the OAuth `sub` claim, stored in the nullable, unique `users.google_sub` column (Alembic revision `b8d41f6a9c27`). Both the new-user and existing-user paths write it, so an account without a sub gets one on its next Google login. Dev logins (`auth/dev_login.py`) and local seeding (`scripts/seed_local.py`) generate deterministic `dev-`-prefixed fake subs so local flows exercise the column.

Integrations that resolve users by Google identity read this column first; a plugin can additionally perform a lazy backfill for users who have not re-logged-in (refresh the stored login token via `get_valid_credentials()`, fetch userinfo, persist the sub).

## Logging

The `auth` logger namespace is configured at INFO level by the unified logging configuration in `chat/logging_config.py` and `logging_config.json`. Auth submodule files use `logger = logging.getLogger(__name__)` (resolving to e.g. `auth.google_login`, `auth.slack`), which inherits from the `auth` logger. Log entries include `user=<email>` for OAuth events, token refresh, login, service authorization, API key resets, and plugin service-key saves. See [Logging Architecture](logging.md).

## Session Cookies

`COOKIE_NAME` in `auth/config.py` comes from `environment.cookie_name()` in `config/environment.py`, so local, staging and prod sessions never collide in one browser (for example the same hostname on different ports); the per-mode names and the rule for the `Secure` attribute (`cookie_secure()`: staging, or an `https://` `app_base_url`) are in [Run Modes](run-modes.md). The unauthenticated `GET /app/api/config` reports the run mode as `quest_env`, which the frontend uses for its local-mode app name.

Every session cookie is issued by `set_session_cookie()` or `set_impersonation_cookie()` in `auth/session.py` and verified by `get_user_from_cookie()` there (its docstring lists the strict type checks). The payload carries `COOKIE_VERSION` (bumping it signs everyone out), the user id, the account's session epoch (`e`), and optionally the password fingerprint (`pw`, see Session binding above) and the impersonation fields (`imp`, `ie`, see below).

- **Lifetime is enforced server-side.** The cookie is signed with a timed `itsdangerous` serializer (`get_session_serializer()`), and verification rejects a signature older than the cookie's lifetime: `SESSION_MAX_AGE_SECONDS` for a normal session, `IMPERSONATION_MAX_AGE_SECONDS` when the payload carries `imp`. `session_max_age()` is the one place that picks the lifetime; issuing uses the same value as the browser `max_age`.
- **Revocation by session epoch.** `users.session_epoch` is the account's session generation. A cookie whose `e` differs from the stored value is rejected; the check runs on the user row `get_user_from_cookie()` loads anyway, so it costs no extra query. `bump_session_epoch()` in `db/user_store.py` advances it, which ends every session of the account on every device: on `POST /app/api/logout` and `GET /auth/logout` (`revoke_cookie_session()`), on `POST /app/api/logout-and-disconnect` (`revoke_sessions()`), and inside `set_password_hash()`.
- **Sign-out targets the session's owner** (`session_owner_id()`): signing out of an impersonation ends the admin's sessions, never the impersonated user's.

The persistent WebSocket re-validates its cookie on a heartbeat (`_revalidate_auth()` in `chat/realtime/socket.py`), so an expired or revoked session also loses its open socket.

## Dev Login

Local mode only (`environment.allow_dev_login()`; 404 in staging and prod, hiding the endpoints): `GET /auth/dev-accounts` returns the account roster for the sign-in screen's canned-account picker, and `POST /auth/dev-login` signs in any email, creating the user with a deterministic name from the `FIRST_NAMES` / `LAST_NAMES` lists in `auth/dev_login.py` and a `dev-`-prefixed fake Google sub. It sets the same versioned session cookie as the Google login. A user who already has `google_oauth` credentials gets 403 and must use Google sign-in. The sign-in flow is described in [Run Modes](run-modes.md).

## Admission Checks

`check_user_allowed()` in `chat/auth.py` admits every email in local mode (so dev-login accounts outside the allowed domain work). Elsewhere it applies `is_login_allowed()` from `auth/config.py`: an email is admitted when it is on the allowed domain OR (case-insensitively) in the optional `allowed_login_emails` whitelist in `server_config.json` -- individual accounts outside any single domain, e.g. personal `@gmail.com` users on a family deployment; the prod bootstrap wizard prompts for both. The Google login callback (`auth/google_login.py`) always enforces `is_login_allowed()`, in every mode.

The domain is resolved by `allowed_login_domain()`:

- local mode only, `QUEST_ALLOWED_LOGIN_DOMAIN` (pre-baked by `run.py` from the shared parent-directory `dev-config.json`, so a real Google sign-in with a test account can complete; ignored in staging/prod);
- then the `allowed_login_domain` key in `server_config.json` in all modes (written by the prod bootstrap wizard so a deployment for another organization can admit its own domain);
- then the hardcoded `ALLOWED_DOMAIN`.

Outside local mode the same rule runs on every API request: `require_user_allowed()` in `chat/auth.py` raises 403 `access_denied` and is called from every request-authenticating dependency (`get_current_user` and `get_current_user_cookie_or_apikey` in `auth/session.py`, and `get_current_user_cookie_or_apikey` plus its identical `_checked` alias in `chat/auth.py`). So the script tool-call bridge, the `/api/authed-get|post` proxy, the Gmail Simple routes and the `/api/instructions` + `/api/reset-api-key` root endpoints reject an offboarded user's still-valid cookie or `users.api_key` exactly like the `/app/api/*` routes. A session cookie stays valid for up to its lifetime unless the account signs out (see Session Cookies), and an API key never expires, so this per-request check is what cuts off an offboarded user immediately.

UI messages describe the restriction via `login_restriction_description()` ("@domain accounts", or "approved accounts" when a whitelist is configured); the whitelist itself is never enumerated on unauthenticated surfaces such as the sign-in page or the `login_restriction` field of `GET /app/api/config`.

## Admin Impersonation

An impersonation cookie (`set_impersonation_cookie()`) adds `imp` (the admin's user id) and `ie` (the admin's session epoch) to the session payload and expires one hour after issue, server-side. `get_user_from_cookie()` re-checks on every request that both accounts' epochs are current and the impersonator is still an admin, and annotates the user dict with the impersonator's identity, which `GET /app/api/me` exposes. The endpoints, the routes an impersonation session may not use (`forbid_impersonation`), the per-request audit line, the frontend and the rationale are in [Admin Impersonation](admin-impersonation.md).

## Design Decisions

**Why a separate dev-login endpoint instead of a mock Google OAuth flow?**
A real Google OAuth flow requires valid credentials, a Google Cloud project, and browser redirects. For local multi-user testing, a simple email-based login is faster and requires no external dependencies. The endpoints are inert outside local mode (404), so there is no security surface in production.

**Why block users with existing Google OAuth credentials?**
If a user has already authenticated via Google, allowing dev-login for the same email could create session confusion (two auth paths for one account). Blocking dev-login for such users forces them to use the real Google flow, keeping the auth state consistent.

**Why per-mode cookie names instead of one name?**
A local or staging instance often runs on the same hostname as production (e.g. via Tailscale). With one cookie name, signing in to one instance overwrites the other's session cookie and signs the user out of it.

**Why an `auth/` package instead of keeping auth in `quest.py`?**
Auth is large and changes on its own schedule; a package keeps the flows navigable on their own. `auth/__init__.py` re-exports the commonly used symbols so consumers write `from auth import get_current_user` without knowing the internal layout.

**Why separate `config.py` from `session.py`?**
Configuration constants and credential loaders (`config.py`) are stateless and widely imported. Session management (`session.py`) involves cookie serialization and FastAPI dependencies. Separating them prevents circular imports and keeps each file focused on a single concern.

**Why keep `google_credentials.py` separate from `google_login.py` and `google_services.py`?**
Credential management functions (`get_valid_service_credentials()`, `make_authenticated_request()`) are used by API endpoint modules (Gmail, Drive, Docs, Sheets, Tasks), not just by the OAuth flow endpoints. Keeping them in a shared module avoids duplication and ensures consistent credential handling across all consumers.

**Why separate Google sign-in from the Google Services connection?**
Sign-in requests only the identity scopes in `LOGIN_SCOPES`; API access is a second grant at `/auth/google-services` with the scopes in `GOOGLE_SERVICE_SCOPES` (`auth/config.py`), stored in its own `google_services_oauth` field. Users can reconnect or widen the services grant without touching their session or any other connection, and the connection works under either sign-in method. One combined grant at sign-in was rejected because every added API scope would push all users through a fresh sign-in consent.

**Why store granted (not requested) Google Services scopes at the callback?**
`auth_google_services_callback()` in `google_services.py` persists `credentials.granted_scopes` (falling back to `GOOGLE_SERVICE_SCOPES` only when Google returns no `scope` field), not the requested set. The `needs_reauth` connector check compares the stored scopes against `GOOGLE_SERVICE_SCOPES`; storing the requested set verbatim would mask a partial grant (a user de-selecting a scope, or a token-upgrade re-consent) and leave a connector showing "Connected" while calls 403 with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. This matters most for the broad `cloud-platform` GCP scope -- see [GCP API Documentation](../api/gcp-api.md).

**Why does the Google Services callback fail closed when UserInfo does not answer?**
`auth_google_services_callback()` proves the authorizing Google account is the logged-in Quest account by calling the UserInfo endpoint with the freshly exchanged token and comparing the returned email (case-insensitively) against the session email. A non-200 UserInfo response, or a 200 without an `email`, aborts the flow without persisting anything: an unverifiable token could belong to another Google account picked at the consent screen, and storing it would silently bind that account's data to the Quest user.

So the check can actually succeed, `get_google_services_oauth_flow()` requests `GOOGLE_SERVICES_IDENTITY_SCOPES` (`openid` + `userinfo.email`) alongside `GOOGLE_SERVICE_SCOPES` -- `openid` must be requested explicitly because Google adds it to the granted set whenever a `userinfo.*` scope is requested, and google-auth-oauthlib raises "Scope has changed" on `fetch_token` if requested and granted differ; the identity scopes are deliberately kept out of `GOOGLE_SERVICE_SCOPES` because the `needs_reauth` subset check compares stored scopes against that list and widening it would flag every existing connection for re-consent.

The cross-user variant of this (an attacker-minted callback URL replayed in a victim's browser) is separately blocked by the session-bound `state` cookie above.

**Why do `get_valid_credentials()` and `get_valid_service_credentials()` mutate the `user` dict in-place?**
Within a single WebSocket session, the same `user` dict is passed to multiple API calls. If a token refresh writes the new token to the database but does not update the in-memory `user` dict, subsequent calls in the same session still see the stale expired token and trigger redundant refreshes to Google. Mutating `user["google_oauth"]` (or `user["google_services_oauth"]`) in-place after each refresh ensures all downstream callers in the same session see the fresh token immediately.

**Why a per-account session epoch instead of a server-side session table?**
A counter on the `users` row the cookie check already loads gives server-side revocation with no extra query per request, no new table and no sweeper for expired rows. The cost is granularity: revocation is per account, so signing out ends the session on every device rather than only the current one.

**Why only one sign-in method at a time?**
Running both would give every account two independent credentials with different recovery paths and make "who may sign in" depend on which door someone used. A single switch also makes the Google transition a clean cutover: password sessions end, and the Google callback resolves the same rows by email.

**Why are passwords only set through one-time links, never at sign-up?**
Without a link that proves control of the address, anyone could register an allowed-domain email before its owner -- including an address in `admin_emails`. Admin-issued links (and the bootstrap wizard for the first admins) work without any email setup; self-service links rely on SMTP delivery for the ownership proof.

**Why scrypt from `hashlib` instead of a password-hashing package?**
The bootstrap wizard must hash the first admin password before any dependency is installed (it is stdlib-only like `run.py`), and the app must verify the same format. `hashlib.scrypt` is memory-hard and available everywhere Python links OpenSSL.

## Constraints

- The `auth/` submodule handles only authentication, authorization, and credential management. API endpoint logic (Gmail, Airtable, etc.) lives in the `api/` directory, and GitHub, Slack, Twitter/X and Telegram access is plugin-provided (see [Plugins](plugins.md)).
- `quest.py` keeps the `/api/instructions` and `/api/reset-api-key` endpoints and static file serving; the instruction text comes from `api/instructions.py` (`get_instructions_content()`, `get_user_connected_services()`) and the `get_instructions()` function of each API module.
- The dev-login endpoints work only in local mode and return 404 elsewhere.
