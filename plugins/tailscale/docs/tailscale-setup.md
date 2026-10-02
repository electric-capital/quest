# Tailscale Setup

How to connect Quest to a tailnet for the `plugins/tailscale` plugin. See [tailscale-api.md](tailscale-api.md) for what the integration does.

## Requirements

- A Tailscale account with the **Owner, Admin, IT admin, or Network admin** role on the tailnet: only those roles can create API access tokens or OAuth clients.
- The Quest server must reach `https://api.tailscale.com` over the internet.
- Nothing to configure server-side. The Tailscale connector is available in every deployment's Settings > Data Connections.

## User: credential

Pick one of the two credential kinds. The OAuth client is recommended: it never expires and can be limited to read scopes on Tailscale's side.

### Option A: OAuth client (recommended, read-only, no expiry)

1. In the Tailscale admin console: Settings > **Trust credentials** (older consoles: Settings > OAuth clients) > **Generate OAuth client**.
2. Give it a description (e.g. `Quest read-only`) and tick only **read** scopes. For the full plugin surface: Devices > Core (read), Devices > Routes (read), Devices > Posture attributes (read), Policy file (read), DNS (read), Keys > Auth keys (read) and OAuth keys (read), Users (read), Feature settings (read), Logs > Network (read), Services (read), Webhooks (read), Posture integrations (read). Skip any you do not need; a missing scope only makes that one call answer `403`.
3. Do **not** grant write scopes. The plugin cannot use them (every reachable path is a GET), and a client without them cannot be abused if the secret leaks.
4. Copy the **client secret** (`tskey-client-...`, shown once). The client id is embedded in it; you do not need to copy it separately.
5. In Quest: Settings > Data Connections > "+ Add Connection" > **Tailscale**, paste the secret, save. It is stored encrypted for that user only.

### Option B: API access token (simplest, expires)

1. Admin console: Settings > **Keys** > **Generate access token**. Choose an expiry (1 to 90 days).
2. Copy the token (`tskey-api-...`, shown once) and paste it into Settings > Data Connections > Tailscale in Quest.
3. The token has every right of your account, including writes; Quest only ever issues GETs with it, but prefer Option A where policy allows.
4. When it expires, calls start answering `401`: generate a new token and paste it over the old one in the same row.

Ask Quest "Which devices are on our tailnet?" to confirm the connection; **Disconnect** on the row removes the stored credential.

## Troubleshooting

- *"Expected a Tailscale API access token starting with 'tskey-api-' or an OAuth client secret starting with 'tskey-client-'"* when saving: the pasted value is not one of the two supported kinds. `tskey-auth-...` is a device auth key for enrolling machines and does not work against the API.
- *`401`* on every call: an expired or revoked API access token, or a deleted OAuth client. Re-create and re-paste.
- *`403`* on one endpoint while others work (OAuth client): that endpoint's read scope was not granted. Add it to the OAuth client in the admin console; existing clients can be edited.
- *`403`* with an API access token: the account's role does not cover that resource (for example an IT admin reading the policy file).
- *"Tailscale is not connected (or the stored credential no longer works)"* right after pasting an OAuth secret: the token exchange failed. Check the server log for `[Tailscale] OAuth token exchange failed` and the HTTP status Tailscale returned.
- *`response_too_large`* on `/devices` or `/logging/network`: expected for larger tailnets; the model should re-request with `output_file` and analyse the saved file.
