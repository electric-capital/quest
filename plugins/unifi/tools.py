"""UniFi dynamic tools (plugin module).

Read-only tools over the two local Integration APIs (see
plugins/unifi/upstream.py for the client, network.py / protect.py for the
per-application helpers). Every handler:

- loads the admin config (host + TLS switch) fresh, so admin edits apply
  immediately;
- picks the user's NETWORK or PROTECT API key for the application it
  talks to and answers a structured ``unifi_not_connected`` error when
  that key is missing (the user may have added only one of the two);
- converts :class:`UnifiError` into a JSON error object instead of raising,
  so one failing call never kills the run.

Network tools: controller info, sites, connectivity status, devices,
clients. Protect tools: cameras, live snapshots (saved to the workspace and
attached to the model request as an image), other Protect devices.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from config.plugin_types import PluginTool

from plugins.unifi import network as net
from plugins.unifi import protect as prot
from plugins.unifi.upstream import (
    MISSING_KEYS_ERROR,
    UnifiError,
    get_network_api_key,
    get_protect_api_key,
    load_unifi_config,
    make_client,
    verify_tls_enabled,
)

logger = logging.getLogger(__name__)

# Per-device statistics fan-out cap for unifi_list_devices(include_statistics).
MAX_STATISTICS_FANOUT = 40
# Default / max client rows returned by unifi_list_clients.
DEFAULT_CLIENT_LIMIT = 100
MAX_CLIENT_LIMIT = 500

_SNAPSHOT_DIR = "unifi-snapshots"
_FILENAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _error(code: str, message: str, **extra) -> str:
    return json.dumps({"error": code, "message": message, **extra})


def _unifi_error(exc: UnifiError) -> str:
    return _error(exc.code, str(exc), status_code=exc.status_code)


def _arg_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _arg_bool(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _arg_int(value, default: int, *, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _load_config_or_error() -> tuple[dict | None, str | None]:
    """The admin config, or a JSON error string when unconfigured."""
    from fastapi import HTTPException

    try:
        return load_unifi_config(), None
    except HTTPException as exc:
        return None, _error("unifi_not_configured", str(exc.detail))


def _network_client(ctx):
    """``(client, None)`` for the user's Network key, else ``(None, error)``."""
    config, error = _load_config_or_error()
    if error:
        return None, error
    key = get_network_api_key(ctx.user)
    if not key:
        return None, _error(
            "unifi_not_connected",
            "No UniFi Network API key is stored for this user. " + MISSING_KEYS_ERROR,
            application="network",
        )
    return make_client(config, key), None


def _protect_client(ctx):
    """``(client, None)`` for the user's Protect key, else ``(None, error)``."""
    config, error = _load_config_or_error()
    if error:
        return None, error
    key = get_protect_api_key(ctx.user)
    if not key:
        return None, _error(
            "unifi_not_connected",
            "No UniFi Protect API key is stored for this user. " + MISSING_KEYS_ERROR,
            application="protect",
        )
    return make_client(config, key), None


# ---------------------------------------------------------------------------
# Controller info
# ---------------------------------------------------------------------------

async def _tool_get_controller_info(ctx, args: dict) -> str:
    config, error = _load_config_or_error()
    if error:
        return error
    network_key = get_network_api_key(ctx.user)
    protect_key = get_protect_api_key(ctx.user)
    out: dict = {
        "host": config["host"],
        "verify_tls": verify_tls_enabled(config),
        "network": {"connected": bool(network_key)},
        "protect": {"connected": bool(protect_key)},
    }
    if network_key:
        try:
            async with make_client(config, network_key) as client:
                info = await client.get_json(f"{net.NETWORK_API_PREFIX}/info")
                sites = await net.list_sites(client)
            out["network"].update({
                "reachable": True,
                "application_version": (info or {}).get("applicationVersion"),
                "sites": [net.site_view(s) for s in sites],
            })
        except UnifiError as exc:
            out["network"].update({"reachable": False, "error": exc.code, "message": str(exc)})
    if protect_key:
        try:
            async with make_client(config, protect_key) as client:
                info = await prot.get_protect_info(client)
            out["protect"].update({
                "reachable": True,
                "application_version": info.get("applicationVersion"),
            })
        except UnifiError as exc:
            out["protect"].update({"reachable": False, "error": exc.code, "message": str(exc)})
    if not network_key and not protect_key:
        out["note"] = MISSING_KEYS_ERROR
    return json.dumps(out)


# ---------------------------------------------------------------------------
# Network: sites / status / devices / clients
# ---------------------------------------------------------------------------

async def _tool_list_sites(ctx, args: dict) -> str:
    client, error = _network_client(ctx)
    if error:
        return error
    try:
        async with client:
            sites = await net.list_sites(client)
    except UnifiError as exc:
        return _unifi_error(exc)
    return json.dumps({"sites": [net.site_view(s) for s in sites], "count": len(sites)})


async def _tool_get_network_status(ctx, args: dict) -> str:
    client, error = _network_client(ctx)
    if error:
        return error
    try:
        async with client:
            site = await net.resolve_site(client, _arg_str(args.get("site")))
            devices = await net.list_devices(client, site["id"])
            clients = await net.list_clients(client, site["id"])

            gateways = [d for d in devices if net.is_gateway(d)]
            gateway_views = []
            for gw in gateways:
                view = net.device_view(gw)
                if net._lower(gw.get("state")) == "online":
                    try:
                        stats = await net.get_device_statistics(client, site["id"], gw["id"])
                        view["statistics"] = net.statistics_view(stats)
                    except UnifiError as exc:
                        view["statistics_error"] = str(exc)
                gateway_views.append(view)

            health: dict = {}
            health_error = None
            site_ref = site.get("internalReference")
            if site_ref:
                try:
                    health = net.health_view(await net.get_site_health(client, str(site_ref)))
                except UnifiError as exc:
                    health_error = str(exc)
    except UnifiError as exc:
        return _unifi_error(exc)

    by_state: dict[str, int] = {}
    for d in devices:
        state = str(d.get("state") or "UNKNOWN")
        by_state[state] = by_state.get(state, 0) + 1
    offline = [net.device_view(d) for d in devices if net._lower(d.get("state")) != "online"]
    by_type: dict[str, int] = {}
    for c in clients:
        ctype = str(c.get("type") or "UNKNOWN")
        by_type[ctype] = by_type.get(ctype, 0) + 1

    out = {
        "site": net.site_view(site),
        "overall_status": net.overall_status(health, devices),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "gateways": gateway_views,
        "devices": {"total": len(devices), "by_state": by_state, "not_online": offline},
        "clients": {"total": len(clients), "by_type": by_type},
    }
    if health:
        out["subsystems"] = health
    if health_error:
        out["subsystems_note"] = (
            "Per-subsystem WAN/LAN/WLAN health (legacy stat/health endpoint) "
            f"was unavailable: {health_error}. Status above is derived from "
            "gateway and device states only."
        )
    return json.dumps(out)


async def _tool_list_devices(ctx, args: dict) -> str:
    client, error = _network_client(ctx)
    if error:
        return error
    state_filter = net._lower(_arg_str(args.get("state")))
    include_stats = _arg_bool(args.get("include_statistics"))
    try:
        async with client:
            site = await net.resolve_site(client, _arg_str(args.get("site")))
            devices = await net.list_devices(client, site["id"])
            if state_filter:
                devices = [d for d in devices if net._lower(d.get("state")) == state_filter]
            views = [net.device_view(d) for d in devices]
            stats_note = None
            if include_stats:
                targets = [d for d in devices if net._lower(d.get("state")) == "online"]
                if len(targets) > MAX_STATISTICS_FANOUT:
                    stats_note = (
                        f"Statistics fetched for the first {MAX_STATISTICS_FANOUT} online "
                        "devices only; use unifi_get_device for the rest."
                    )
                    targets = targets[:MAX_STATISTICS_FANOUT]
                by_id = {v["id"]: v for v in views}
                for d in targets:
                    try:
                        stats = await net.get_device_statistics(client, site["id"], d["id"])
                        by_id[d["id"]]["statistics"] = net.statistics_view(stats)
                    except UnifiError as exc:
                        by_id[d["id"]]["statistics_error"] = str(exc)
    except UnifiError as exc:
        return _unifi_error(exc)
    out = {"site": net.site_view(site), "count": len(views), "devices": views}
    if state_filter:
        out["state_filter"] = state_filter.upper()
    if stats_note:
        out["note"] = stats_note
    return json.dumps(out)


async def _tool_get_device(ctx, args: dict) -> str:
    needle = _arg_str(args.get("device"))
    if not needle:
        return _error("invalid_arguments", "`device` (id, name, MAC, or IP) is required.")
    client, error = _network_client(ctx)
    if error:
        return error
    try:
        async with client:
            site = await net.resolve_site(client, _arg_str(args.get("site")))
            devices = await net.list_devices(client, site["id"])
            match = net.find_device(devices, needle)
            if match is None:
                return _error(
                    "unifi_unknown_device",
                    f"No device matches {needle!r} in site {site.get('name')!r}.",
                    available=[{"id": d.get("id"), "name": d.get("name")} for d in devices],
                )
            detail = await net.get_device(client, site["id"], match["id"])
            view = net.device_view(detail)
            view["detail"] = {
                k: v for k, v in detail.items()
                if k not in ("id", "name", "model", "state", "ipAddress", "macAddress", "features")
            }
            if net._lower(detail.get("state")) == "online":
                try:
                    stats = await net.get_device_statistics(client, site["id"], match["id"])
                    view["statistics"] = net.statistics_view(stats)
                    view["statistics_raw"] = stats
                except UnifiError as exc:
                    view["statistics_error"] = str(exc)
    except UnifiError as exc:
        return _unifi_error(exc)
    return json.dumps({"site": net.site_view(site), "device": view})


async def _tool_list_clients(ctx, args: dict) -> str:
    client, error = _network_client(ctx)
    if error:
        return error
    client_type = _arg_str(args.get("type"))
    if client_type and client_type.upper() not in net.CLIENT_TYPES:
        return _error(
            "invalid_arguments",
            f"`type` must be one of {', '.join(net.CLIENT_TYPES)}.",
        )
    search = _arg_str(args.get("search"))
    limit = _arg_int(args.get("limit"), DEFAULT_CLIENT_LIMIT, lo=1, hi=MAX_CLIENT_LIMIT)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10 ** 6)
    try:
        async with client:
            site = await net.resolve_site(client, _arg_str(args.get("site")))
            rows = await net.list_clients(client, site["id"])
            devices = await net.list_devices(client, site["id"]) if rows else []
    except UnifiError as exc:
        return _unifi_error(exc)
    filtered = net.filter_clients(rows, client_type=client_type, search=search)
    device_names = {d.get("id"): d.get("name") for d in devices}
    page = filtered[offset:offset + limit]
    views = []
    for row in page:
        view = net.client_view(row)
        uplink = row.get("uplinkDeviceId")
        if uplink and device_names.get(uplink):
            view["uplink_device_name"] = device_names[uplink]
        views.append(view)
    out = {
        "site": net.site_view(site),
        "total_connected": len(rows),
        "matched": len(filtered),
        "offset": offset,
        "returned": len(views),
        "clients": views,
    }
    if offset + limit < len(filtered):
        out["next_offset"] = offset + limit
    if client_type:
        out["type_filter"] = client_type.upper()
    if search:
        out["search"] = search
    return json.dumps(out)


async def _tool_get_client(ctx, args: dict) -> str:
    needle = _arg_str(args.get("client"))
    if not needle:
        return _error("invalid_arguments", "`client` (id, name, MAC, or IP) is required.")
    client, error = _network_client(ctx)
    if error:
        return error
    try:
        async with client:
            site = await net.resolve_site(client, _arg_str(args.get("site")))
            rows = await net.list_clients(client, site["id"])
            match = net.find_client(rows, needle)
            if match is None:
                return _error(
                    "unifi_unknown_client",
                    f"No connected client matches {needle!r} in site {site.get('name')!r}. "
                    "Only currently connected clients are listed by the Network API.",
                )
            detail = await net.get_client(client, site["id"], match["id"])
    except UnifiError as exc:
        return _unifi_error(exc)
    view = net.client_view(detail)
    view["detail"] = {k: v for k, v in detail.items() if k not in ("id", "name", "type")}
    return json.dumps({"site": net.site_view(site), "client": view})


# ---------------------------------------------------------------------------
# Protect: cameras / snapshots / other devices
# ---------------------------------------------------------------------------

async def _tool_list_cameras(ctx, args: dict) -> str:
    client, error = _protect_client(ctx)
    if error:
        return error
    try:
        async with client:
            cameras = await prot.list_cameras(client)
    except UnifiError as exc:
        return _unifi_error(exc)
    views = [prot.camera_view(c) for c in cameras]
    connected = sum(1 for c in cameras if prot.is_camera_connected(c))
    return json.dumps({
        "count": len(views),
        "connected": connected,
        "disconnected": len(views) - connected,
        "cameras": views,
    })


async def _tool_get_camera(ctx, args: dict) -> str:
    needle = _arg_str(args.get("camera"))
    if not needle:
        return _error("invalid_arguments", "`camera` (id or name) is required.")
    client, error = _protect_client(ctx)
    if error:
        return error
    try:
        async with client:
            cameras = await prot.list_cameras(client)
            match = prot.find_camera(cameras, needle)
            if match is None:
                return _error(
                    "unifi_unknown_camera",
                    f"No camera matches {needle!r}.",
                    available=[{"id": c.get("id"), "name": c.get("name")} for c in cameras],
                )
            detail = await prot.get_camera(client, match["id"])
    except UnifiError as exc:
        return _unifi_error(exc)
    view = prot.camera_view(detail)
    view["detail"] = {k: v for k, v in detail.items() if k not in ("id", "name", "state")}
    return json.dumps({"camera": view})


def _snapshot_filename(camera_name: str, camera_id: str, when: datetime) -> str:
    base = _FILENAME_CLEAN_RE.sub("-", camera_name or camera_id or "camera").strip("-.")
    base = base[:60] or "camera"
    return f"{base}-{when.strftime('%Y%m%d-%H%M%S')}.jpg"


def _resolve_snapshot_destination(workspace_root: Path, path_arg: str | None,
                                  default_filename: str) -> Path | str:
    """Workspace-relative destination for a snapshot, or an error string.

    Same semantics as the other plugins' save tools: default directory
    ``unifi-snapshots/``; a trailing slash (or an existing directory) means
    "put the default filename inside"; absolute paths and ``..`` rejected.
    """
    if not path_arg:
        rel_path = Path(_SNAPSHOT_DIR) / default_filename
    else:
        candidate = Path(path_arg)
        if candidate.is_absolute():
            return "Invalid path: absolute paths are not allowed. Provide a workspace-relative path."
        if any(part == ".." for part in candidate.parts):
            return "Invalid path: parent-directory traversal ('..') is not allowed."
        treat_as_dir = path_arg.endswith("/") or path_arg.endswith(os.sep)
        if not treat_as_dir:
            probe = workspace_root / candidate
            if probe.exists() and probe.is_dir():
                treat_as_dir = True
        rel_path = candidate / default_filename if treat_as_dir else candidate
        if rel_path.suffix.lower() not in (".jpg", ".jpeg"):
            rel_path = rel_path.with_name(rel_path.name + ".jpg")

    file_path = (workspace_root / rel_path).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        return "Invalid path: resolved destination is outside the conversation workspace."
    return file_path


async def _tool_get_camera_snapshot(ctx, args: dict):
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _publish_file_list_changed,
    )

    needle = _arg_str(args.get("camera"))
    if not needle:
        return _error("invalid_arguments", "`camera` (id or name) is required.")
    if not ctx.conversation_id:
        return _error(
            "no_workspace",
            "unifi_get_camera_snapshot requires a conversation workspace, "
            "which this call has no access to.",
        )
    high_quality = _arg_bool(args.get("high_quality"))

    client, error = _protect_client(ctx)
    if error:
        return error
    try:
        async with client:
            cameras = await prot.list_cameras(client)
            match = prot.find_camera(cameras, needle)
            if match is None:
                return _error(
                    "unifi_unknown_camera",
                    f"No camera matches {needle!r}.",
                    available=[{"id": c.get("id"), "name": c.get("name")} for c in cameras],
                )
            if not prot.is_camera_connected(match):
                return _error(
                    "unifi_camera_offline",
                    f"Camera {match.get('name')!r} is {match.get('state')}; "
                    "a live snapshot needs a CONNECTED camera.",
                    camera={"id": match.get("id"), "name": match.get("name"), "state": match.get("state")},
                )
            data, content_type = await prot.get_snapshot(
                client, match["id"], high_quality=high_quality,
            )
    except UnifiError as exc:
        return _unifi_error(exc)

    now = datetime.now(timezone.utc)
    workspace_root = (
        await conversation_workspace_dir(ctx.conversation_id)
    ).resolve()
    destination = _resolve_snapshot_destination(
        workspace_root, _arg_str(args.get("path")),
        _snapshot_filename(str(match.get("name") or ""), str(match.get("id") or ""), now),
    )
    if isinstance(destination, str):
        return _error("invalid_path", destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except Exception as exc:
        return _error("workspace_write_failed", f"Failed to save snapshot to workspace: {exc}")
    _publish_file_list_changed(
        ctx.user["id"], "conversation", ctx.conversation_id, ctx.project_id,
    )

    rel_written = destination.relative_to(workspace_root).as_posix()
    mime_type = "image/jpeg" if "jpeg" in content_type.lower() or "jpg" in content_type.lower() else (
        mimetypes.guess_type(destination.name)[0] or "image/jpeg"
    )

    extra_parts = []
    attached = False
    provider = getattr(ctx, "provider", None)
    if provider is not None:
        try:
            uploaded = await provider.upload_file(
                file_path=str(destination), mime_type=mime_type,
                display_name=destination.name, model=getattr(ctx, "model", "") or "",
            )
            if uploaded is not None:
                extra_parts.append(provider.make_file_part(uploaded))
                attached = True
        except Exception as exc:  # never let an attach failure lose the file
            logger.warning("[unifi] snapshot attach failed (%s): %s", destination.name, exc)

    result = {
        "status": "success",
        "camera": {"id": match.get("id"), "name": match.get("name"), "state": match.get("state")},
        "captured_at": now.isoformat(),
        "high_quality": high_quality,
        "path": rel_written,
        "size_bytes": len(data),
        "content_type": mime_type,
        "attached_to_response": attached,
        "message": (
            f"Snapshot saved to the workspace as '{rel_written}' ({len(data):,} bytes). "
            + ("The image is included in this response so you can describe it. "
               if attached else
               "Use get_workspace_file to view it. ")
            + f"Show it to the user inline with ![{match.get('name') or 'snapshot'}]({rel_written})."
        ),
    }
    return json.dumps(result), extra_parts


async def _tool_list_protect_devices(ctx, args: dict) -> str:
    kind = net._lower(_arg_str(args.get("kind")))
    if kind not in prot.PROTECT_DEVICE_KINDS:
        return _error(
            "invalid_arguments",
            f"`kind` must be one of {', '.join(prot.PROTECT_DEVICE_KINDS)}.",
        )
    client, error = _protect_client(ctx)
    if error:
        return error
    try:
        async with client:
            rows = await prot.list_protect_devices(client, kind)
    except UnifiError as exc:
        return _unifi_error(exc)
    return json.dumps({"kind": kind, "count": len(rows), "devices": rows})


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------

def _intent(example: str) -> dict:
    return {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent (max 50 characters). "
            f"Example: '{example}'."
        ),
    }


_SITE_PARAM = {
    "type": "string",
    "description": (
        "Optional Network site: its id, internal reference (e.g. 'default'), "
        "or name. Omit on single-site controllers."
    ),
}


def _tool(name: str, description: str, properties: dict, required: list[str],
          handler, *, application: str) -> PluginTool:
    return PluginTool(
        spec={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
        requires_service="unifi",
    )


GET_CONTROLLER_INFO_TOOL = _tool(
    "unifi_get_controller_info",
    "Check the UniFi controller connection: host, which application API keys "
    "the user has stored (Network / Protect), whether each application is "
    "reachable, its version, and the Network sites. Call this first when a "
    "UniFi request fails or when unsure which applications are connected.",
    {"intent_message": _intent("Check UniFi connection")},
    [],
    _tool_get_controller_info, application="both",
)

LIST_SITES_TOOL = _tool(
    "unifi_list_sites",
    "List the Network application's sites (id, internal reference, name). "
    "Most controllers have exactly one ('default'); other Network tools then "
    "need no `site` argument.",
    {"intent_message": _intent("List UniFi sites")},
    [],
    _tool_list_sites, application="network",
)

GET_NETWORK_STATUS_TOOL = _tool(
    "unifi_get_network_status",
    "Network connectivity overview for a site: overall status (ok / degraded / "
    "down), the gateway(s) with live uplink throughput, uptime and load, "
    "per-subsystem health (WAN, LAN, WLAN, WWW internet, VPN: status, WAN IP, "
    "latency, speedtest, client counts) when the controller exposes it, device "
    "counts by state with the not-online devices listed, and client counts by "
    "type. Use this for 'is the internet up?', 'how is the network doing?'.",
    {"site": _SITE_PARAM, "intent_message": _intent("Check network status")},
    [],
    _tool_get_network_status, application="network",
)

LIST_DEVICES_TOOL = _tool(
    "unifi_list_devices",
    "List UniFi devices (gateways, switches, access points) in a site with "
    "their state (ONLINE / OFFLINE / PENDING_ADOPTION / ...), model, IP, MAC, "
    "firmware, and port/radio counts. Optionally filter by `state` and add "
    "live statistics (uptime, CPU/memory, uplink rates) per online device.",
    {
        "site": _SITE_PARAM,
        "state": {
            "type": "string",
            "description": "Optional state filter, e.g. ONLINE or OFFLINE.",
        },
        "include_statistics": {
            "type": "boolean",
            "description": (
                "Also fetch statistics/latest for each online device "
                f"(uptime, CPU, memory, uplink rates; first {MAX_STATISTICS_FANOUT} devices)."
            ),
        },
        "intent_message": _intent("List UniFi devices"),
    },
    [],
    _tool_list_devices, application="network",
)

GET_DEVICE_TOOL = _tool(
    "unifi_get_device",
    "Full detail for one UniFi device (by id, name, MAC, or IP): adoption / "
    "provisioning times, firmware, uplink, interfaces (ports with speed and "
    "PoE, radios), plus live statistics when online.",
    {
        "device": {
            "type": "string",
            "description": "The device id, name, MAC address, or IP address.",
        },
        "site": _SITE_PARAM,
        "intent_message": _intent("Inspect UniFi device"),
    },
    ["device"],
    _tool_get_device, application="network",
)

LIST_CLIENTS_TOOL = _tool(
    "unifi_list_clients",
    "List the clients currently connected to the network: name, type (WIRED / "
    "WIRELESS / VPN / TELEPORT), IP, MAC, connection time, and the UniFi "
    "device they are attached to. Filter by `type` and/or a `search` "
    "substring over name, IP, and MAC; page with `limit`/`offset`.",
    {
        "site": _SITE_PARAM,
        "type": {
            "type": "string",
            "description": "Optional client type filter: WIRED, WIRELESS, VPN, or TELEPORT.",
        },
        "search": {
            "type": "string",
            "description": "Optional case-insensitive substring matched against name, IP, and MAC.",
        },
        "limit": {
            "type": "integer",
            "description": f"Rows per page (default {DEFAULT_CLIENT_LIMIT}, max {MAX_CLIENT_LIMIT}).",
        },
        "offset": {
            "type": "integer",
            "description": "Row offset for paging (from a previous result's next_offset).",
        },
        "intent_message": _intent("List connected clients"),
    },
    [],
    _tool_list_clients, application="network",
)

GET_CLIENT_TOOL = _tool(
    "unifi_get_client",
    "Full detail for one currently connected client (by id, name, MAC, or IP).",
    {
        "client": {
            "type": "string",
            "description": "The client id, name, MAC address, or IP address.",
        },
        "site": _SITE_PARAM,
        "intent_message": _intent("Inspect network client"),
    },
    ["client"],
    _tool_get_client, application="network",
)

LIST_CAMERAS_TOOL = _tool(
    "unifi_list_cameras",
    "List UniFi Protect cameras with their connection state (CONNECTED / "
    "DISCONNECTED), model, last-seen time, recording / mic / HDR settings, "
    "and enabled feature flags.",
    {"intent_message": _intent("List Protect cameras")},
    [],
    _tool_list_cameras, application="protect",
)

GET_CAMERA_TOOL = _tool(
    "unifi_get_camera",
    "Full detail for one Protect camera (by id or name): all settings the "
    "Protect API exposes (video mode, smart detection, OSD, LED, LCD, "
    "feature flags, ...).",
    {
        "camera": {"type": "string", "description": "The camera id or name."},
        "intent_message": _intent("Inspect Protect camera"),
    },
    ["camera"],
    _tool_get_camera, application="protect",
)

GET_CAMERA_SNAPSHOT_TOOL = _tool(
    "unifi_get_camera_snapshot",
    "Capture a LIVE JPEG snapshot from a Protect camera (by id or name). The "
    "image is saved to the conversation workspace (default "
    f"'{_SNAPSHOT_DIR}/<camera>-<timestamp>.jpg') and attached to this "
    "response so you can describe what the camera sees; show it to the user "
    "inline with markdown `![name](path)`. Use `high_quality: true` for a "
    "full-resolution frame (larger file). The camera must be CONNECTED.",
    {
        "camera": {"type": "string", "description": "The camera id or name."},
        "high_quality": {
            "type": "boolean",
            "description": "Request a full-resolution frame instead of the default preview size.",
        },
        "path": {
            "type": "string",
            "description": (
                "Optional workspace-relative destination. A trailing '/' or an "
                "existing directory means 'put the default filename inside'; "
                "otherwise it is the file path (a .jpg suffix is added if "
                "missing). Absolute paths and '..' are rejected."
            ),
        },
        "intent_message": _intent("Grab camera snapshot"),
    },
    ["camera"],
    _tool_get_camera_snapshot, application="protect",
)

LIST_PROTECT_DEVICES_TOOL = _tool(
    "unifi_list_protect_devices",
    "List the other Protect device families: `kind` = nvrs (the NVR / console "
    "itself: version, storage, uptime), sensors, lights, chimes, viewers, or "
    "liveviews. Raw Protect API objects are returned.",
    {
        "kind": {
            "type": "string",
            "enum": list(prot.PROTECT_DEVICE_KINDS),
            "description": "Which Protect device family to list.",
        },
        "intent_message": _intent("List Protect devices"),
    },
    ["kind"],
    _tool_list_protect_devices, application="protect",
)

UNIFI_TOOLS: tuple[PluginTool, ...] = (
    GET_CONTROLLER_INFO_TOOL,
    LIST_SITES_TOOL,
    GET_NETWORK_STATUS_TOOL,
    LIST_DEVICES_TOOL,
    GET_DEVICE_TOOL,
    LIST_CLIENTS_TOOL,
    GET_CLIENT_TOOL,
    LIST_CAMERAS_TOOL,
    GET_CAMERA_TOOL,
    GET_CAMERA_SNAPSHOT_TOOL,
    LIST_PROTECT_DEVICES_TOOL,
)

# Read-only tools sandbox scripts may call via POST /api/tool-call. The
# snapshot tool needs a conversation workspace (scripts have none) and the
# raw Protect dumps are large, so both stay tool-only.
SCRIPT_TOOL_ALLOWLIST = frozenset({
    "unifi_get_controller_info",
    "unifi_list_sites",
    "unifi_get_network_status",
    "unifi_list_devices",
    "unifi_get_device",
    "unifi_list_clients",
    "unifi_get_client",
    "unifi_list_cameras",
})
