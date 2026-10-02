# Google Workspace Admin Setup

## Overview

The Google Workspace Admin plugin (`plugins/google_admin`) has no credentials of its own. It reuses the Google OAuth client the deployment already has for sign-in and Google Services (the `google_oauth` credential-store entry), so setup is three changes to that client's Google Cloud project plus one switch in Quest. See [Google Workspace Admin API](google-admin-api.md) for what the connection can read.

## Google Cloud Project

In the Google Cloud project that owns the OAuth client (Google Cloud console):

- **APIs & Services > Library:** enable the **Admin SDK API**. Without it Google answers every call with 403 `SERVICE_DISABLED`.
- **APIs & Services > Credentials > the OAuth client > Authorized redirect URIs:** add `<app base URL>/auth/google-admin/callback` (a hyphen, like `/auth/google-services/callback`). The base URL is resolved by `oauth_base_url()` in `auth/config.py` (the `app_base_url` key, else the request host with the `oauth_hostname` override), the same as the existing `/auth/google-services/callback` entry.
- **OAuth consent screen > Data access:** add the scopes listed in `GOOGLE_ADMIN_SCOPES` (`plugins/google_admin/upstream.py`). For an Internal consent screen this is bookkeeping; an External one must list them to pass verification, since the admin scopes are sensitive.

## Quest Configuration

An admin ticks **Settings > Service Credentials > Google Workspace Admin > Enable** (the schema-declared plugin card, stored in `data/service_credentials/google_admin.json`). The save is rejected while no Google OAuth client is configured (`validate_google_admin_credentials`).

Until the switch is on and a Google OAuth client exists (`google_admin_is_configured`), the Google Workspace Admin row is hidden from every user's Data Connections screen, the `system:google_admin` skill is not advertised, and existing tokens are no longer refreshed.

Local mode can pre-bake the switch through the `service_credentials` mapping of the shared parent-directory `dev-config.json` (see [Run Modes](../../../docs/architecture/run-modes.md)).

## Connecting

A Workspace administrator opens **Settings > Data Connections > Add Connection > Google Workspace Admin**. The popup shows Google's account picker, so a dedicated admin account can be chosen even when it differs from the Quest login, then the consent screen listing the read-only scopes. See [OAuth Popup Flow](../../../docs/architecture/oauth-popup.md).

Which data is readable follows the connected account's admin role in the Google Admin console: a super admin reads everything, while a delegated admin reads only the resources its role grants (user, group and org unit privileges for the directory; mobile and Chrome device management privileges for devices) and gets 403 on the rest.

## Troubleshooting

- **`redirect_uri_mismatch` in the popup:** the callback URL above is missing from the OAuth client, or the app is being browsed through a different origin than the registered one.
- **"Update Available" badge right after connecting:** a scope was unticked on Google's consent screen. Reconnect and leave every box ticked.
- **403 `Not Authorized to access this resource/api`:** the connected account lacks the admin privilege for that resource.
- **403 `SERVICE_DISABLED`:** the Admin SDK API is not enabled in the OAuth client's project.
- **`Error 400: invalid_scope`, "Some requested scopes cannot be shown":** a scope in `GOOGLE_ADMIN_SCOPES` is one Google will not put on a user consent screen (this is why the Cloud Identity devices scope is not requested). The error names the scope; it appears only after an account is chosen.
- **The row disappeared:** the admin switch was turned off, or the Google OAuth client was removed.
