# UniFi (Network + Protect) Integration

This document describes Quest's read-only UniFi integration: network connectivity status, UniFi devices and their state, connected clients, Protect cameras, and live camera snapshots, all against a UniFi OS console reached directly on the local network. It is packaged as the in-tree `plugins/unifi` plugin (plugin id `unifi`; see [Plugin Architecture](../../../docs/architecture/plugins.md)).

## Overview

Three things shape the plugin:

1. **Direct controller access, never unifi.ui.com.** The admin enters the controller's `https://` host once (Settings > Service Credentials > UniFi). Every request goes to `https://<host>/proxy/network/integration/v1/...` (Network) or `https://<host>/proxy/protect/integration/v1/...` (Protect), the official local *Integration APIs*, with the user's key in an `X-API-KEY` header. Consoles ship a self-signed certificate, so TLS verification is off unless the admin turns the `verify_tls` switch on.
2. **Two per-user API keys.** UniFi issues a separate API key per application (Network, Protect), created inside each app under Settings > Control Plane > Integrations. The generic `api_key` connection kind stores one secret, so the user connection is declared as the `oauth` kind (the Twilio pattern): `plugins/unifi/connect.py` mounts a popup page under `/auth/unifi` where the user pastes one or both keys; each key is tested live against its application before it is stored in the `user_service_credentials` row's `oauth_blob`. A user may connect only Network or only Protect; tools for the other application return `unifi_not_connected` naming the missing `application`.
3. **Read-only.** Every tool is a GET (snapshots included), so there are no action-request handlers and no approval cards. Nothing on the controller is changed.

## Key Files

| File | Description |
|------|-------------|
| `plugins/unifi/manifest.py` | The plugin manifest: credential schema (`host`, `verify_tls` bool), the oauth-kind user connection wired to the key-entry router, the `system:unifi` skill, the eleven `unifi_*` tools, and the script-bridge allowlist |
| `plugins/unifi/upstream.py` | Admin config loader (`load_unifi_config`, `normalize_host`, `validate_unifi_credentials`, `verify_tls_enabled`), per-user key helpers (`get_network_api_key` / `get_protect_api_key` / `unifi_connected` / `mask_key`), the `UnifiClient` httpx wrapper (`X-API-KEY` header, `UnifiError` / `UnifiAuthError` mapping, `get_paged` offset/limit walker capped at `MAX_COLLECTED_ITEMS`), and the `verify_network_key` / `verify_protect_key` probes |
| `plugins/unifi/network.py` | Network application helpers: site resolution (`resolve_site` by id / internal reference / name), device and client views (`device_view`, `client_view`, `statistics_view`), gateway detection (`is_gateway`), client-side filters, the legacy `stat/health` reader (`get_site_health`, `health_view`), and `overall_status` |
| `plugins/unifi/protect.py` | Protect application helpers: `camera_view`, `find_camera`, `get_snapshot` (JPEG, `highQuality` flag, `MAX_SNAPSHOT_BYTES` cap), `get_protect_info`, and `list_protect_devices` over `PROTECT_DEVICE_KINDS` |
| `plugins/unifi/connect.py` | The `/auth/unifi` router: popup page, `POST /auth/unifi/keys`, `POST /auth/unifi/disconnect` |
| `plugins/unifi/tools.py` | The tool handlers and specs (`UNIFI_TOOLS`, `SCRIPT_TOOL_ALLOWLIST`) |
| `plugins/unifi/instructions.md` | The `system:unifi` skill body (LLM-facing) |
| `plugins/unifi/tests/` | Plugin test suite; `fake_controller.py` is an `httpx.MockTransport` console serving both APIs |

## Configuration

- Admin: Settings > Service Credentials > UniFi (store file `data/service_credentials/unifi.json`, written by the generic `PUT /admin/service-credentials/unifi`). Fields: `host` (normalized by `normalize_host` to an `https://` origin -- bare IPs/hostnames get the scheme, paths/queries/credentials/`http://` are rejected) and `verify_tls` (bool, default off). `is_configured` = host present; the connector row is hidden until then. See [unifi-setup.md](unifi-setup.md).
- Both settings are re-read from the store on every tool call and every key save.

## Connection Flow (`/auth/unifi`)

Routes are the plugin's router (`plugins/unifi/connect.py`), mounted by `mount_plugin_oauth_routers()`; every route is session-cookie authed (no API-key auth, so the LLM's `curl_proxy_*` tools cannot reach them).

1. Data Connections "Connect" opens `GET /auth/unifi?popup=1` -- a self-contained page showing the controller host, the masked stored keys with the application version recorded at verification time, a Network field, a Protect field, per-key "remove" checkboxes, "Test & save", and "Disconnect".
2. `POST /auth/unifi/keys` `{"network_api_key"?, "protect_api_key"?}` -- a missing/`null` field keeps the stored key, `""` removes it, anything else is trimmed and verified (`GET .../network/integration/v1/info` / `GET .../protect/integration/v1/meta/info`) before the row is written.
   - A rejected key returns 400 `invalid_network_api_key` / `invalid_protect_api_key` (nothing stored); an unreachable controller returns 502 with the `UnifiError.code`; removing the last key returns 400 `no_keys`.
   - The blob becomes `{network_api_key?, protect_api_key?, network_version?, protect_version?, verified_at}` and `invalidate_user_sessions()` refreshes the system prompt.
   - The page posts `oauth_callback_success` to the opener (the same message the OAuth popups send) and closes.
3. `POST /auth/unifi/disconnect` -- deletes the row (the generic oauth-kind connector row has no disconnect button, so the popup hosts one).

`unifi_connected` is true when either key is present.

## Tools

All tools are advertised only while `connected_services["unifi"]` is true (host configured AND at least one key stored). Each answers a JSON object; failures are `{"error": <code>, "message", "status_code"?}` with codes `unifi_not_configured`, `unifi_not_connected` (+ `application`), `unifi_auth_failed`, `unifi_unreachable`, `unifi_timeout`, `unifi_not_found`, `unifi_request_failed`, `unifi_unknown_site` / `_device` / `_client` / `_camera` (+ `available` list), `unifi_camera_offline`, `invalid_arguments`, `invalid_path`.

Network (need the Network key; `site` optional -- id, internal reference such as `default`, or name, resolved by `resolve_site`):

- `unifi_get_controller_info()` -- host, per-application connected/reachable/version, Network sites. Works with either key.
- `unifi_list_sites()`
- `unifi_get_network_status(site?)` -- `overall_status` (`ok`/`degraded`/`down`/`unknown`), `gateways[]` with `statistics` (uptime, CPU/memory, uplink tx/rx bps), `subsystems` keyed `wan`/`www`/`lan`/`wlan`/`vpn` from the legacy `GET /proxy/network/api/s/<ref>/stat/health` (status, WAN IP, latency, throughput, speedtest, counts), `devices` (total, by_state, not_online), `clients` (total, by_type). When the controller does not accept the API key on the legacy endpoint the report carries `subsystems_note` and status derives from gateway/device states only.
- `unifi_list_devices(site?, state?, include_statistics?)` -- `device_view` rows (bulky `interfaces` reduced to port/radio counts); statistics fan-out capped at `MAX_STATISTICS_FANOUT` online devices.
- `unifi_get_device(device, site?)` -- match by id / name / MAC / IP; full detail under `detail`, `statistics` + `statistics_raw` when online.
- `unifi_list_clients(site?, type?, search?, limit?, offset?)` -- every page of the controller's connected-client list is collected, then filtered client-side (`filter_clients`) and paged (`DEFAULT_CLIENT_LIMIT` / `MAX_CLIENT_LIMIT`), with `uplink_device_name` resolved from the device list. Only currently connected clients exist in the Integration API.
- `unifi_get_client(client, site?)`

Protect (need the Protect key):

- `unifi_list_cameras()` -- `camera_view` rows (state, model, last seen, mic/recording/HDR, truthy feature flags, smart-detect object types) plus connected/disconnected counts.
- `unifi_get_camera(camera)` -- by id or name; full object under `detail`.
- `unifi_get_camera_snapshot(camera, high_quality?, path?)` -- refuses DISCONNECTED cameras, fetches `GET .../cameras/{id}/snapshot?highQuality=`, writes the JPEG to the conversation workspace (default `unifi-snapshots/<camera>-<UTC timestamp>.jpg`; `path` semantics match the other plugins' save tools, `.jpg` appended when missing, traversal rejected), publishes `file_list_changed`, and attaches the image to the tool result via `provider.upload_file` + `make_file_part` (`attached_to_response` false when the provider cannot, e.g. an attach failure -- the file is kept either way).
  - The result's `message` tells the model to render `![name](path)` inline (see the workspace-image convention in [frontend.md](../../../docs/architecture/frontend.md)).
  - Requires a conversation workspace.
- `unifi_list_protect_devices(kind)` -- raw objects from `/nvrs`, `/sensors`, `/lights`, `/chimes`, `/viewers`, `/liveviews`.

Script bridge (`SCRIPT_TOOL_ALLOWLIST`): the controller-info, site, status, device, client, and camera-list tools; the snapshot tool (needs a workspace) and the raw Protect dump are tool-only.

## UniFi API Surface

- Network Integration API (`NETWORK_API_PREFIX`): `/info`, `/sites`, `/sites/{siteId}/devices`, `/sites/{siteId}/devices/{deviceId}`, `/sites/{siteId}/devices/{deviceId}/statistics/latest`, `/sites/{siteId}/clients`, `/sites/{siteId}/clients/{clientId}`. List endpoints answer `{offset, limit, count, totalCount, data}` and are walked by `UnifiClient.get_paged` (`PAGE_LIMIT` per page). Errors answer `{statusCode, statusName, message}`.
- Legacy Network API (`NETWORK_LEGACY_PREFIX`): only `/s/{internalReference}/stat/health` (`{meta: {rc}, data: [...]}`), and only because the Integration API has no per-subsystem health yet. Treated as optional.
- Protect Integration API (`PROTECT_API_PREFIX`): `/meta/info`, `/cameras`, `/cameras/{id}`, `/cameras/{id}/snapshot`, `/nvrs`, `/sensors`, `/lights`, `/chimes`, `/viewers`, `/liveviews`.
- No `authed_get` service entry: the host is admin-configured at runtime (the `_SERVICE_REGISTRY` keys are static hostnames) and the two-key selection needs per-request logic, both of which the plugin contract routes to `tools`.

## Design Decisions

**Why an `oauth`-kind connection for pasted API keys?** The generic `api_key` kind holds exactly one secret and UniFi needs two (one per application). The oauth shape is the only manifest shape that mounts plugin routes (`/auth/<id>`) and gives a popup-based connector row with no core FE changes, and it lets the plugin verify each key against the controller before storing it.

**Why keep the legacy `stat/health` call?** "Is the internet up?" is the first question people ask and the Integration API v1 exposes no WAN/WWW subsystem health; the legacy endpoint accepts the same API key on current Network releases. It is wrapped so that a controller that refuses it degrades to a device-state-derived status instead of failing the tool.

**Why client-side filtering and paging for clients?** The Integration API's filter grammar varies across releases; collecting every page (capped) and filtering locally is version-proof and keeps `search` matching name, IP, and MAC uniformly.

**Why is TLS verification off by default?** Practically every UniFi console on a LAN presents a self-signed certificate; requiring verification would make the plugin unusable out of the box. The admin can turn it on for a console with a real certificate.

**Why no writes?** Reboots, port power-cycles, client blocks, and camera settings are all write verbs on the same APIs; they belong behind approval-gated action requests and are deliberately out of scope for this first version.

## Tests

`plugins/unifi/tests/`: upstream helpers and the httpx client against the `FakeController` mock transport, every tool (gating, site resolution, filters, paging, snapshot save/attach/path rules), the key-entry router (verify-before-store, keep/remove semantics, rejection codes, page), and registry wiring via the `unifi_plugin` fixture.
