"""Iru (formerly Kandji) dynamic tools (plugin module).

Read-only tools over the tenant's Iru Endpoint Management API (see
plugins/iru/upstream.py for the client and plugins/iru/fleet.py for the
reference resolvers + compact row views). Every handler:

- loads the admin config (tenant API URL) fresh, so admin edits apply
  immediately;
- reads the user's API token and answers a structured ``iru_not_connected``
  error when it is missing;
- converts :class:`IruError` into a JSON error object instead of raising,
  so one failing call never kills the run.

Nothing here writes to the tenant: no device actions, no record edits, and
the device *secrets* endpoints (FileVault key, bypass code, recovery
password, unlock PIN) are intentionally not wrapped.
"""

from __future__ import annotations

import json
import logging
import re

from config.plugin_tool_helpers import (
    arg_int as _arg_int,
    arg_str as _arg_str,
    intent_param as _intent,
    tool_error as _error,
)
from config.plugin_types import PluginTool

from plugins.iru import fleet
from plugins.iru.upstream import (
    API_PREFIX,
    MAX_COLLECTED_ITEMS,
    MAX_PAGE_LIMIT,
    NOT_CONNECTED_ERROR,
    IruError,
    cursor_from_next,
    get_user_api_token,
    is_uuid,
    load_iru_config,
    make_client,
    page_items,
)

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 100
DEFAULT_ACTIVITY_LIMIT = 25
MAX_APP_ROWS = 1000
VULN_PAGE_MAX = 50
AUDIT_PAGE_MAX = 500
THREAT_PAGE_MAX = 1000

DEVICE_SECTIONS = (
    "details", "status", "library_items", "apps", "activity", "commands",
    "parameters", "lost_mode",
)
DEVICE_PLATFORMS = ("Mac", "iPhone", "iPad", "AppleTV", "Android", "Windows")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iru_error(exc: IruError) -> str:
    extra: dict = {"status_code": exc.status_code}
    if isinstance(exc, fleet.IruLookupError) and exc.candidates:
        extra["candidates"] = exc.candidates
    if exc.retry_after is not None:
        extra["retry_after_seconds"] = exc.retry_after
    return _error(exc.code, str(exc), **extra)


def _arg_bool(value) -> bool | None:
    """Tri-state boolean argument: None when absent / unparseable."""
    if value is True or value is False:
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return None


def _arg_csv(value) -> str | None:
    """A list or comma-separated string argument -> comma-separated string."""
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ",".join(parts) or None
    return _arg_str(value)


def _arg_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = _arg_str(value)
    return [p.strip() for p in text.split(",") if p.strip()] if text else []


def _arg_filter(value) -> tuple[str | None, str | None]:
    """A Prism-style ``filter`` argument (object or JSON string) ->
    ``(serialized JSON, None)`` or ``(None, error message)``."""
    if value is None or value == "" or value == {}:
        return None, None
    if isinstance(value, dict):
        return json.dumps(value), None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None, "filter must be a JSON object (or an object literal)."
        if not isinstance(parsed, dict):
            return None, "filter must be a JSON object, e.g. {\"os_version\": {\"like\": [\"14\"]}}."
        return json.dumps(parsed), None
    return None, "filter must be a JSON object."


def _next_offset(rows: list, limit: int, offset: int) -> int | None:
    return offset + len(rows) if len(rows) >= limit else None


def _load_config_or_error() -> tuple[dict | None, str | None]:
    """The admin config, or a JSON error string when unconfigured."""
    from fastapi import HTTPException

    try:
        return load_iru_config(), None
    except HTTPException as exc:
        return None, _error("iru_not_configured", str(exc.detail))


def _client(ctx):
    """``(client, None)`` for the user's token, else ``(None, error)``."""
    config, error = _load_config_or_error()
    if error:
        return None, error
    token = get_user_api_token(ctx.user)
    if not token:
        return None, _error("iru_not_connected", NOT_CONNECTED_ERROR)
    return make_client(config, token), None


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

async def _tool_get_tenant_info(ctx, args: dict) -> str:
    config, error = _load_config_or_error()
    if error:
        return error
    token = get_user_api_token(ctx.user)
    out: dict = {"api_url": config["api_url"], "connected": bool(token)}
    if not token:
        out["note"] = NOT_CONNECTED_ERROR
        return json.dumps(out)
    try:
        async with make_client(config, token) as client:
            try:
                licensing = await client.get_json(f"{API_PREFIX}/settings/licensing")
                out["reachable"] = True
                out["licensing"] = licensing
            except IruError as exc:
                if exc.status_code != 403:
                    raise
                # Token lacks the licensing permission; prove it works on
                # the device list instead.
                out["licensing_note"] = str(exc)
                await client.get_json(f"{API_PREFIX}/devices", params={"limit": 1})
                out["reachable"] = True
            blueprints = await client.get_json(
                f"{API_PREFIX}/blueprints", params={"limit": 1},
            )
            if isinstance(blueprints, dict) and isinstance(blueprints.get("count"), int):
                out["blueprint_count"] = blueprints["count"]
    except IruError as exc:
        out["reachable"] = False
        out["error"] = exc.code
        out["message"] = str(exc)
    return json.dumps(out)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

async def _tool_list_devices(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    limit = _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=MAX_PAGE_LIMIT)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10**7)
    platform = _arg_str(args.get("platform"))
    if platform:
        match = next((p for p in DEVICE_PLATFORMS if p.lower() == platform.lower()), None)
        if match is None:
            return _error(
                "invalid_arguments",
                f"platform must be one of {', '.join(DEVICE_PLATFORMS)}.",
            )
        platform = match
    params: dict = {
        "serial_number": _arg_str(args.get("serial_number")),
        "device_name": _arg_str(args.get("device_name")),
        "platform": platform,
        "user_email": _arg_str(args.get("user_email")),
        "user_name": _arg_str(args.get("user_name")),
        "user_id": _arg_str(args.get("user_id")),
        "asset_tag": _arg_str(args.get("asset_tag")),
        "model": _arg_str(args.get("model")),
        "os_version": _arg_str(args.get("os_version")),
        "mac_address": _arg_str(args.get("mac_address")),
        "tag_name": _arg_str(args.get("tag_name")),
        "ordering": _arg_str(args.get("ordering")),
        "limit": limit,
        "offset": offset,
    }
    filevault = _arg_bool(args.get("filevault_enabled"))
    if filevault is not None:
        params["filevault_enabled"] = "true" if filevault else "false"
    try:
        async with client:
            blueprint_ref = _arg_str(args.get("blueprint"))
            blueprint_name = None
            if blueprint_ref:
                blueprint = await fleet.resolve_blueprint(client, blueprint_ref)
                params["blueprint_id"] = blueprint.get("id")
                blueprint_name = blueprint.get("name")
            payload = await client.get_json(f"{API_PREFIX}/devices", params=params)
            rows = [r for r in page_items(payload, "/devices") if isinstance(r, dict)]
    except IruError as exc:
        return _iru_error(exc)
    out = {
        "devices": [fleet.device_row(r) for r in rows],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
        "next_offset": _next_offset(rows, limit, offset),
        "filters": {k: v for k, v in params.items()
                    if v is not None and k not in ("limit", "offset")},
    }
    if blueprint_name:
        out["filters"]["blueprint_name"] = blueprint_name
    return json.dumps(out)


async def _device_section(client, device_id: str, section: str, args: dict):
    """Fetch one ``sections`` entry of iru_get_device."""
    base = f"{API_PREFIX}/devices/{device_id}"
    if section == "details":
        return await client.get_json(f"{base}/details")
    if section == "status":
        payload = await client.get_json(f"{base}/status")
        payload = payload if isinstance(payload, dict) else {}
        items = payload.get("library_items") or []
        params = payload.get("parameters") or []
        return {
            "library_items": [fleet.library_item_status_row(li) for li in items
                              if isinstance(li, dict)],
            "parameters": [fleet.parameter_status_row(p) for p in params
                           if isinstance(p, dict)],
        }
    if section == "library_items":
        payload = await client.get_json(f"{base}/library-items")
        items = (payload or {}).get("library_items") if isinstance(payload, dict) else payload
        rows = [fleet.library_item_status_row(li) for li in (items or [])
                if isinstance(li, dict)]
        by_status: dict[str, int] = {}
        for row in rows:
            key = str(row.get("status") or "UNKNOWN")
            by_status[key] = by_status.get(key, 0) + 1
        return {"count": len(rows), "by_status": by_status, "items": rows}
    if section == "apps":
        payload = await client.get_json(f"{base}/apps")
        apps = (payload or {}).get("apps") if isinstance(payload, dict) else payload
        apps = [a for a in (apps or []) if isinstance(a, dict)]
        search = fleet._lower(args.get("app_search"))
        rows = [fleet.app_row(a) for a in apps]
        if search:
            rows = [r for r in rows if search in fleet._lower(r.get("name"))
                    or search in fleet._lower(r.get("bundle_id"))]
        rows.sort(key=lambda r: fleet._lower(r.get("name")))
        return {
            "count": len(rows),
            "truncated": len(rows) > MAX_APP_ROWS,
            "apps": rows[:MAX_APP_ROWS],
        }
    if section == "activity":
        limit = _arg_int(args.get("activity_limit"), DEFAULT_ACTIVITY_LIMIT,
                         lo=1, hi=MAX_PAGE_LIMIT)
        payload = await client.get_json(f"{base}/activity", params={"limit": limit, "offset": 0})
        activity = payload.get("activity") if isinstance(payload, dict) else None
        activity = activity if isinstance(activity, dict) else (payload or {})
        rows = [fleet.activity_row(a) for a in (activity.get("results") or [])
                if isinstance(a, dict)]
        return {"total": activity.get("count"), "returned": len(rows), "events": rows}
    if section == "commands":
        limit = _arg_int(args.get("activity_limit"), DEFAULT_ACTIVITY_LIMIT,
                         lo=1, hi=MAX_PAGE_LIMIT)
        payload = await client.get_json(f"{base}/commands", params={"limit": limit, "offset": 0})
        rows = page_items(payload, "/commands") if isinstance(payload, (list, dict)) else []
        total = payload.get("count") if isinstance(payload, dict) else None
        return {"total": total, "returned": len(rows),
                "commands": [fleet.compact_json(r) for r in rows]}
    if section == "parameters":
        return await client.get_json(f"{base}/parameters")
    if section == "lost_mode":
        return await client.get_json(f"{base}/details/lostmode")
    raise IruError(f"Unknown section {section!r}.", code="invalid_arguments")


async def _tool_get_device(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    ref = _arg_str(args.get("device"))
    if not ref:
        return _error("invalid_arguments", "device (id, serial number, or name) is required.")
    sections = _arg_list(args.get("sections")) or ["details"]
    unknown = [s for s in sections if s not in DEVICE_SECTIONS]
    if unknown:
        return _error(
            "invalid_arguments",
            f"Unknown sections {unknown}; valid: {', '.join(DEVICE_SECTIONS)}.",
        )
    out: dict = {}
    try:
        async with client:
            record = await fleet.resolve_device(client, ref)
            device_id = record.get("device_id")
            out["device"] = record
            out["sections"] = {}
            for section in sections:
                try:
                    out["sections"][section] = await _device_section(
                        client, device_id, section, args,
                    )
                except IruError as exc:
                    if exc.status_code in (403, 404):
                        out["sections"][section] = {
                            "error": exc.code, "message": str(exc),
                            "status_code": exc.status_code,
                        }
                    else:
                        raise
    except IruError as exc:
        return _iru_error(exc)
    return json.dumps(out, default=str)


# ---------------------------------------------------------------------------
# Blueprints / library items
# ---------------------------------------------------------------------------

async def _tool_list_blueprints(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    limit = _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=MAX_PAGE_LIMIT)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10**7)
    params = {"name": _arg_str(args.get("name")), "limit": limit, "offset": offset}
    try:
        async with client:
            payload = await client.get_json(f"{API_PREFIX}/blueprints", params=params)
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/blueprints") if isinstance(r, dict)]
    return json.dumps({
        "blueprints": [fleet.blueprint_row(b) for b in rows],
        "count": len(rows),
        "total": payload.get("count") if isinstance(payload, dict) else None,
        "next_offset": _next_offset(rows, limit, offset),
    })


async def _tool_get_blueprint(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    ref = _arg_str(args.get("blueprint"))
    if not ref:
        return _error("invalid_arguments", "blueprint (id or name) is required.")
    try:
        async with client:
            blueprint = await fleet.resolve_blueprint(client, ref)
            items_payload = await client.get_json(
                f"{API_PREFIX}/blueprints/{blueprint['id']}/list-library-items",
            )
            items = [r for r in page_items(items_payload, "/list-library-items")
                     if isinstance(r, dict)]
    except IruError as exc:
        return _iru_error(exc)
    return json.dumps({
        "blueprint": blueprint,
        "library_items": items,
        "library_item_count": len(items),
    }, default=str)


async def _tool_get_library_item_status(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    item_id = _arg_str(args.get("library_item_id"))
    if not item_id or not is_uuid(item_id):
        return _error(
            "invalid_arguments",
            "library_item_id (a UUID from iru_get_blueprint or a device's "
            "library_items section) is required.",
        )
    status_filter = fleet._lower(args.get("status"))
    limit = _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=MAX_PAGE_LIMIT)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10**7)
    path = f"{API_PREFIX}/library/library-items/{item_id}/status"
    params: dict = {}
    try:
        async with client:
            device_ref = _arg_str(args.get("device"))
            if device_ref:
                device = await fleet.resolve_device(client, device_ref)
                params["computer_id"] = device.get("device_id")
            if status_filter:
                # The upstream endpoint has no status filter: collect every
                # page (capped) and filter here, then page the matches.
                all_rows = await client.get_all_offset_pages(
                    path, params=params, max_items=MAX_COLLECTED_ITEMS,
                )
                matches = [r for r in all_rows if isinstance(r, dict)
                           and fleet._lower(r.get("status")) == status_filter]
                rows = matches[offset:offset + limit]
                total = len(matches)
                scanned = len(all_rows)
            else:
                payload = await client.get_json(
                    path, params={**params, "limit": limit, "offset": offset},
                )
                rows = [r for r in page_items(payload, path) if isinstance(r, dict)]
                total = payload.get("count") if isinstance(payload, dict) else None
                scanned = None
    except IruError as exc:
        return _iru_error(exc)
    by_status: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status") or "UNKNOWN")
        by_status[key] = by_status.get(key, 0) + 1
    out = {
        "library_item_id": item_id,
        "statuses": [fleet.library_item_status_row(r) for r in rows],
        "count": len(rows),
        "total": total,
        "by_status_in_page": by_status,
        "next_offset": _next_offset(rows, limit, offset),
    }
    if status_filter:
        out["status_filter"] = status_filter.upper()
        out["scanned"] = scanned
        if scanned is not None and scanned >= MAX_COLLECTED_ITEMS:
            out["note"] = (
                f"Only the first {MAX_COLLECTED_ITEMS} status rows were scanned "
                "for the status filter."
            )
    return json.dumps(out)


# ---------------------------------------------------------------------------
# Users / tags
# ---------------------------------------------------------------------------

async def _tool_list_users(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    params: dict = {
        "email": _arg_str(args.get("email")),
        "cursor": _arg_str(args.get("cursor")),
    }
    archived = _arg_bool(args.get("archived"))
    if archived is not None:
        params["archived"] = "true" if archived else "false"
    try:
        async with client:
            payload = await client.get_json(f"{API_PREFIX}/users", params=params)
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/users") if isinstance(r, dict)]
    return json.dumps({
        "users": [fleet.user_row(u) for u in rows],
        "count": len(rows),
        "next_cursor": cursor_from_next(payload),
    })


async def _tool_get_user(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    ref = _arg_str(args.get("user"))
    if not ref:
        return _error("invalid_arguments", "user (id or exact email) is required.")
    try:
        async with client:
            user = await fleet.resolve_user(client, ref)
            devices_payload = await client.get_json(
                f"{API_PREFIX}/devices",
                params={"user_id": user.get("id"), "limit": MAX_PAGE_LIMIT},
            )
            devices = [fleet.device_row(d) for d in page_items(devices_payload, "/devices")
                       if isinstance(d, dict)]
    except IruError as exc:
        return _iru_error(exc)
    return json.dumps({"user": user, "devices": devices, "device_count": len(devices)},
                      default=str)


async def _tool_list_tags(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    try:
        async with client:
            payload = await client.get_json(
                f"{API_PREFIX}/tags", params={"search": _arg_str(args.get("search")) or ""},
            )
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/tags") if isinstance(r, dict)]
    return json.dumps({"tags": rows, "count": len(rows)})


# ---------------------------------------------------------------------------
# Prism (Visibility) reports
# ---------------------------------------------------------------------------

async def _tool_prism_query(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    category = fleet._lower(args.get("category")).replace(" ", "_")
    if category not in fleet.PRISM_CATEGORIES:
        return _error(
            "invalid_arguments",
            f"category must be one of: {', '.join(fleet.PRISM_CATEGORIES)}.",
        )
    filter_json, filter_error = _arg_filter(args.get("filter"))
    if filter_error:
        return _error("invalid_arguments", filter_error)
    limit = _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=MAX_PAGE_LIMIT)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10**7)
    fields = _arg_list(args.get("fields"))
    params = {
        "filter": filter_json,
        "blueprint_ids": _arg_csv(args.get("blueprint_ids")),
        "device_families": _arg_csv(args.get("device_families")),
        "sort_by": _arg_str(args.get("sort_by")),
        "limit": limit,
        "offset": offset,
    }
    try:
        async with client:
            payload = await client.get_json(f"{API_PREFIX}/prism/{category}", params=params)
            total = None
            if offset == 0 and not filter_json and not params["blueprint_ids"] \
                    and not params["device_families"]:
                try:
                    count = await client.get_json(
                        f"{API_PREFIX}/prism/count", params={"category": category},
                    )
                    total = count.get("count") if isinstance(count, dict) else None
                except IruError as exc:
                    if exc.status_code not in (403, 404):
                        raise
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, f"/prism/{category}") if isinstance(r, dict)]
    return json.dumps({
        "category": category,
        "rows": [fleet.project_fields(r, fields) for r in rows],
        "count": len(rows),
        "total_unfiltered": total,
        "next_offset": _next_offset(rows, limit, offset),
        "fields_available": sorted(rows[0].keys()) if rows and fields else None,
    }, default=str)


# ---------------------------------------------------------------------------
# Threats / vulnerabilities
# ---------------------------------------------------------------------------

async def _tool_list_threats(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    limit = _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=THREAT_PAGE_MAX)
    offset = _arg_int(args.get("offset"), 0, lo=0, hi=10**7)
    params = {
        "classification": fleet._lower(args.get("classification")) or None,
        "status": fleet._lower(args.get("status")) or None,
        "device_id": _arg_str(args.get("device_id")),
        "date_range": _arg_str(args.get("date_range")),
        "term": _arg_str(args.get("term")),
        "sort_by": _arg_str(args.get("sort_by")),
        "limit": limit,
        "offset": offset,
    }
    try:
        async with client:
            payload = await client.get_json(f"{API_PREFIX}/threat-details", params=params)
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/threat-details") if isinstance(r, dict)]
    meta = payload if isinstance(payload, dict) else {}
    return json.dumps({
        "threats": rows,
        "count": len(rows),
        "total": meta.get("count"),
        "malware_count": meta.get("malware_count"),
        "pup_count": meta.get("pup_count"),
        "next_offset": _next_offset(rows, limit, offset),
    }, default=str)


async def _tool_list_vulnerabilities(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    filter_json, filter_error = _arg_filter(args.get("filter"))
    if filter_error:
        return _error("invalid_arguments", filter_error)
    params = {
        "filter": filter_json,
        "sort_by": _arg_str(args.get("sort_by")),
        "page": _arg_int(args.get("page"), 1, lo=1, hi=10**6),
        "size": _arg_int(args.get("size"), VULN_PAGE_MAX, lo=1, hi=VULN_PAGE_MAX),
    }
    try:
        async with client:
            payload = await client.get_json(
                f"{API_PREFIX}/vulnerability-management/vulnerabilities", params=params,
            )
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/vulnerabilities") if isinstance(r, dict)]
    meta = payload if isinstance(payload, dict) else {}
    total = meta.get("total")
    page = params["page"]
    has_more = isinstance(total, int) and page * params["size"] < total
    return json.dumps({
        "vulnerabilities": rows,
        "count": len(rows),
        "total": total,
        "page": page,
        "next_page": page + 1 if has_more else None,
    }, default=str)


_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}")


async def _tool_get_vulnerability(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    # The id becomes a URL path segment, so it must be exactly a CVE id:
    # a prefix check alone would let "CVE-/../.." or "CVE-1?x" steer the
    # request to another path of the tenant API.
    cve_id = (_arg_str(args.get("cve_id")) or "").upper()
    if not _CVE_ID_RE.fullmatch(cve_id):
        return _error("invalid_arguments", "cve_id (e.g. CVE-2024-24795) is required.")
    page = _arg_int(args.get("page"), 1, lo=1, hi=10**6)
    size = _arg_int(args.get("size"), VULN_PAGE_MAX, lo=1, hi=VULN_PAGE_MAX)
    base = f"{API_PREFIX}/vulnerability-management/vulnerabilities/{cve_id}"
    try:
        async with client:
            description = await client.get_json(base)
            devices = await client.get_json(f"{base}/devices", params={"page": page, "size": size})
            software = await client.get_json(f"{base}/software", params={"page": page, "size": size})
    except IruError as exc:
        return _iru_error(exc)
    device_rows = [r for r in page_items(devices, "/devices") if isinstance(r, dict)]
    software_rows = [r for r in page_items(software, "/software") if isinstance(r, dict)]
    return json.dumps({
        "cve_id": cve_id,
        "vulnerability": description,
        "devices": device_rows,
        "devices_total": devices.get("total") if isinstance(devices, dict) else None,
        "software": software_rows,
        "software_total": software.get("total") if isinstance(software, dict) else None,
        "page": page,
    }, default=str)


# ---------------------------------------------------------------------------
# Audit log / ADE
# ---------------------------------------------------------------------------

async def _tool_list_audit_events(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    params = {
        "limit": _arg_int(args.get("limit"), DEFAULT_LIST_LIMIT, lo=1, hi=AUDIT_PAGE_MAX),
        "sort_by": _arg_str(args.get("sort_by")) or "-occurred_at",
        "start_date": _arg_str(args.get("start_date")),
        "end_date": _arg_str(args.get("end_date")),
        "cursor": _arg_str(args.get("cursor")),
    }
    try:
        async with client:
            payload = await client.get_json(f"{API_PREFIX}/audit/events", params=params)
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/audit/events") if isinstance(r, dict)]
    events = []
    for r in rows:
        row = dict(r)
        for key in ("new_state", "old_state", "metadata"):
            if key in row:
                row[key] = fleet.compact_json(row[key])
        events.append(row)
    return json.dumps({
        "events": events,
        "count": len(events),
        "next_cursor": cursor_from_next(payload),
    }, default=str)


async def _tool_list_ade_devices(ctx, args: dict) -> str:
    client, error = _client(ctx)
    if error:
        return error
    params = {
        "serial_number": _arg_str(args.get("serial_number")),
        "device_family": _arg_str(args.get("device_family")),
        "os": _arg_str(args.get("os")),
        "model": _arg_str(args.get("model")),
        "profile_status": fleet._lower(args.get("profile_status")) or None,
        "blueprint_id": _arg_str(args.get("blueprint_id")),
        "user_id": _arg_str(args.get("user_id")),
        "dep_account": _arg_str(args.get("ade_token_id")),
        "page": _arg_int(args.get("page"), 1, lo=1, hi=10**6),
    }
    try:
        async with client:
            payload = await client.get_json(
                f"{API_PREFIX}/integrations/apple/ade/devices", params=params,
            )
    except IruError as exc:
        return _iru_error(exc)
    rows = [r for r in page_items(payload, "/ade/devices") if isinstance(r, dict)]
    meta = payload if isinstance(payload, dict) else {}
    return json.dumps({
        "ade_devices": rows,
        "count": len(rows),
        "total": meta.get("count"),
        "page": params["page"],
        "next_page": params["page"] + 1 if meta.get("next") else None,
    }, default=str)


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------

def _tool(name: str, description: str, properties: dict, required: list[str],
          handler) -> PluginTool:
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
        requires_service="iru",
    )


_LIMIT_OFFSET = {
    "limit": {
        "type": "integer",
        "description": f"Rows per page (default {DEFAULT_LIST_LIMIT}, max {MAX_PAGE_LIMIT}).",
    },
    "offset": {
        "type": "integer",
        "description": "Row offset for paging (from a previous result's next_offset).",
    },
}

_FILTER_PARAM = {
    "type": "object",
    "description": (
        "Optional JSON filter object: {\"<column>\": {\"<op>\": <value>}} where op "
        "is eq (booleans), in / not_in (list of exact strings), like / not_like "
        "(list of substrings), lt / gt / lte / gte (dates), or or (date window). "
        "Example: {\"device__name\": {\"like\": [\"dev\"]}, \"apple_silicon\": {\"eq\": true}}."
    ),
    "additionalProperties": True,
}

GET_TENANT_INFO_TOOL = _tool(
    "iru_get_tenant_info",
    "Check the Iru connection: tenant API URL, whether the user's API token "
    "is stored and accepted, licensing / device counts per platform (when the "
    "token may read them), and the blueprint count. Call this first when an "
    "Iru request fails or when unsure whether Iru is connected.",
    {"intent_message": _intent("Check Iru connection")},
    [],
    _tool_get_tenant_info,
)

LIST_DEVICES_TOOL = _tool(
    "iru_list_devices",
    "List managed devices (Macs, iPhones, iPads, Apple TVs, plus Android / "
    "Windows when enabled) with compact rows: id, name, serial, platform, model, "
    "OS version, blueprint, assigned user, tags, last check-in, missing / MDM / "
    "agent flags. Every filter is optional; text filters are 'contains' matches "
    "except platform. Page with limit / offset (next_offset is set when more "
    "rows may exist).",
    {
        "serial_number": {"type": "string", "description": "Serial number (partial ok)."},
        "device_name": {"type": "string", "description": "Device name (partial)."},
        "platform": {
            "type": "string", "enum": list(DEVICE_PLATFORMS),
            "description": "Exact platform.",
        },
        "blueprint": {"type": "string", "description": "Blueprint id or name."},
        "user_email": {"type": "string", "description": "Assigned user's email (partial)."},
        "user_name": {"type": "string", "description": "Assigned user's display name (partial)."},
        "user_id": {"type": "string", "description": "Assigned user's Iru user id (exact)."},
        "asset_tag": {"type": "string", "description": "Asset tag."},
        "model": {"type": "string", "description": "Model string (partial), e.g. 'MacBook Pro'."},
        "os_version": {"type": "string", "description": "OS version (partial), e.g. '14.5'."},
        "mac_address": {"type": "string", "description": "MAC address."},
        "tag_name": {"type": "string", "description": "Exact tag name (case-sensitive)."},
        "filevault_enabled": {
            "type": "boolean",
            "description": "macOS only: true = FileVault on, false = off.",
        },
        "ordering": {
            "type": "string",
            "description": (
                "Sort field, e.g. 'device_name', '-last_check_in', 'serial_number', "
                "'os_version', 'platform'. A leading '-' reverses."
            ),
        },
        **_LIMIT_OFFSET,
        "intent_message": _intent("List managed Macs"),
    },
    [],
    _tool_list_devices,
)

GET_DEVICE_TOOL = _tool(
    "iru_get_device",
    "Full detail for one device by id, serial number, or device name. Always "
    "returns the device record; `sections` picks extra data: `details` "
    "(hardware, general, MDM, security, users, network -- the default), `status` "
    "(every library item + parameter with its PASS / ERROR / PENDING state and "
    "audit log), `library_items` (same list with per-status counts), `apps` "
    "(installed apps: name, version, bundle id, path; optional `app_search`), "
    "`activity` (recent events, `activity_limit`), `commands` (MDM command "
    "history), `parameters` (raw parameter report), `lost_mode` (iOS / iPadOS "
    "lost mode state). Ambiguous names return `iru_ambiguous_device` with "
    "candidates.",
    {
        "device": {"type": "string", "description": "Device id, serial number, or device name."},
        "sections": {
            "type": "array",
            "items": {"type": "string", "enum": list(DEVICE_SECTIONS)},
            "description": "Extra sections to include (default: [\"details\"]).",
        },
        "app_search": {
            "type": "string",
            "description": "With the apps section: keep only apps whose name or bundle id contains this.",
        },
        "activity_limit": {
            "type": "integer",
            "description": f"With activity / commands: rows to return (default {DEFAULT_ACTIVITY_LIMIT}, max {MAX_PAGE_LIMIT}).",
        },
        "intent_message": _intent("Inspect a managed Mac"),
    },
    ["device"],
    _tool_get_device,
)

LIST_BLUEPRINTS_TOOL = _tool(
    "iru_list_blueprints",
    "List blueprints (id, name, type classic / map, description, device count, "
    "enrollment-code state). `name` is a 'contains' filter.",
    {
        "name": {"type": "string", "description": "Blueprint name (partial)."},
        **_LIMIT_OFFSET,
        "intent_message": _intent("List Iru blueprints"),
    },
    [],
    _tool_list_blueprints,
)

GET_BLUEPRINT_TOOL = _tool(
    "iru_get_blueprint",
    "One blueprint (by id or name) with its full record and the library items "
    "assigned to it (id + name; use iru_get_library_item_status for per-device "
    "deployment state of an item).",
    {
        "blueprint": {"type": "string", "description": "Blueprint id or name."},
        "intent_message": _intent("Inspect a blueprint"),
    },
    ["blueprint"],
    _tool_get_blueprint,
)

GET_LIBRARY_ITEM_STATUS_TOOL = _tool(
    "iru_get_library_item_status",
    "Per-device deployment status of one library item (app, profile, script, "
    "...): device, blueprint, status (PASS / ERROR / PENDING / AVAILABLE / "
    "INCOMPATIBLE / ...), last report time, last audit log, log tail. Optionally "
    "restrict to one `device` (id / serial / name) or one `status` (scans up to "
    f"{MAX_COLLECTED_ITEMS} rows server-side and filters here).",
    {
        "library_item_id": {
            "type": "string",
            "description": "The library item id (UUID) from iru_get_blueprint or a device's library_items section.",
        },
        "device": {"type": "string", "description": "Optional device id, serial number, or name."},
        "status": {"type": "string", "description": "Optional status to keep, e.g. ERROR or PENDING."},
        **_LIMIT_OFFSET,
        "intent_message": _intent("Check app deployment status"),
    },
    ["library_item_id"],
    _tool_get_library_item_status,
)

LIST_USERS_TOOL = _tool(
    "iru_list_users",
    "List directory users (id, name, email, active / archived, device count, "
    "department, job title, directory integration). `email` is a 'contains' "
    "filter. Cursor-paged: pass `next_cursor` back as `cursor`.",
    {
        "email": {"type": "string", "description": "Email address (partial)."},
        "archived": {"type": "boolean", "description": "true = archived users only, false = active only."},
        "cursor": {"type": "string", "description": "Page cursor from a previous result's next_cursor."},
        "intent_message": _intent("List Iru users"),
    },
    [],
    _tool_list_users,
)

GET_USER_TOOL = _tool(
    "iru_get_user",
    "One directory user (by id or exact email) with the devices assigned to them.",
    {
        "user": {"type": "string", "description": "User id or exact email address."},
        "intent_message": _intent("Look up an Iru user"),
    },
    ["user"],
    _tool_get_user,
)

LIST_TAGS_TOOL = _tool(
    "iru_list_tags",
    "List device tags (id + name), optionally filtered by a `search` substring. "
    "Tag names are case-sensitive in iru_list_devices(tag_name).",
    {
        "search": {"type": "string", "description": "Optional substring filter."},
        "intent_message": _intent("List device tags"),
    },
    [],
    _tool_list_tags,
)

PRISM_QUERY_TOOL = _tool(
    "iru_prism_query",
    "Fleet-wide Prism (Visibility) report for one category -- one row per "
    "device (or per app / cert / profile / extension / local user ...) with the "
    "device's blueprint and assigned user on every row. Categories: "
    + ", ".join(fleet.PRISM_CATEGORIES) + ". Use `filter` (JSON, see schema) to "
    "narrow rows server-side, `fields` to keep only some columns (rows are wide; "
    "the first call returns `fields_available` when `fields` is set), "
    "`blueprint_ids` / `device_families` (Mac, iPhone, iPad, AppleTV) to scope. "
    "Page with limit / offset.",
    {
        "category": {
            "type": "string", "enum": list(fleet.PRISM_CATEGORIES),
            "description": "The Prism category.",
        },
        "filter": _FILTER_PARAM,
        "fields": {
            "type": "array", "items": {"type": "string"},
            "description": "Optional column names to keep, e.g. [\"device__name\", \"os_version\", \"serial_number\"].",
        },
        "blueprint_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Optional blueprint ids to scope to.",
        },
        "device_families": {
            "type": "array", "items": {"type": "string"},
            "description": "Optional device families: Mac, iPhone, iPad, AppleTV.",
        },
        "sort_by": {"type": "string", "description": "Column to sort by; leading '-' = descending."},
        **_LIMIT_OFFSET,
        "intent_message": _intent("Report FileVault status fleet-wide"),
    },
    ["category"],
    _tool_prism_query,
)

LIST_THREATS_TOOL = _tool(
    "iru_list_threats",
    "Endpoint Detection & Response threat detections (malware / PUP): threat "
    "name, classification, device, file path + hash, process, status "
    "(quarantined / not_quarantined / released), detection and quarantine dates.",
    {
        "classification": {"type": "string", "enum": ["malware", "pup"], "description": "Optional classification."},
        "status": {
            "type": "string", "enum": ["quarantined", "not_quarantined", "released"],
            "description": "Optional status.",
        },
        "device_id": {"type": "string", "description": "Optional device id."},
        "date_range": {"type": "string", "description": "Optional look-back window in days, e.g. '30'."},
        "term": {"type": "string", "description": "Optional search over device name, file hash, file path."},
        "sort_by": {
            "type": "string",
            "description": "threat_name, classification, device_name, process_name, process_owner, detection_date, or status; leading '-' = descending.",
        },
        "limit": {"type": "integer", "description": f"Rows per page (default {DEFAULT_LIST_LIMIT}, max {THREAT_PAGE_MAX})."},
        "offset": {"type": "integer", "description": "Row offset for paging."},
        "intent_message": _intent("List quarantined malware"),
    },
    [],
    _tool_list_threats,
)

LIST_VULNERABILITIES_TOOL = _tool(
    "iru_list_vulnerabilities",
    "Vulnerability Management: CVEs detected across the fleet (CVE id, CVSS "
    "score, severity, affected software, device count, known-exploit score, "
    "first / latest detection, status). Filterable columns: cve_id, software, "
    "severity, first_detection_date, status. Sort by age, cve_id, cvss_score, "
    "device_count, known_exploit, software, severity, status. Pages of up to "
    f"{VULN_PAGE_MAX}.",
    {
        "filter": _FILTER_PARAM,
        "sort_by": {"type": "string", "description": "Sort column; leading '-' = descending."},
        "page": {"type": "integer", "description": "Page number (1-based)."},
        "size": {"type": "integer", "description": f"Rows per page (max {VULN_PAGE_MAX})."},
        "intent_message": _intent("List critical CVEs"),
    },
    [],
    _tool_list_vulnerabilities,
)

GET_VULNERABILITY_TOOL = _tool(
    "iru_get_vulnerability",
    "One CVE: its description / scores plus the affected devices (name, serial, "
    "user, blueprint, installs, detected software version + path) and the "
    "affected software list.",
    {
        "cve_id": {"type": "string", "description": "The CVE id, e.g. CVE-2024-24795."},
        "page": {"type": "integer", "description": "Page of affected devices / software (1-based)."},
        "size": {"type": "integer", "description": f"Rows per page (max {VULN_PAGE_MAX})."},
        "intent_message": _intent("Inspect a CVE"),
    },
    ["cve_id"],
    _tool_get_vulnerability,
)

LIST_AUDIT_EVENTS_TOOL = _tool(
    "iru_list_audit_events",
    "The tenant audit log (Activity module): blueprint / library item changes, "
    "device lifecycle events, sensitive-data access (FileVault / recovery keys), "
    "directory and admin actions -- actor, action, target, old / new state, "
    "time. Newest first by default; cursor-paged (pass next_cursor as cursor).",
    {
        "start_date": {"type": "string", "description": "Optional start (YYYY-MM-DD or ISO datetime)."},
        "end_date": {"type": "string", "description": "Optional end (YYYY-MM-DD or ISO datetime)."},
        "sort_by": {"type": "string", "description": "occurred_at or id, leading '-' = descending (default -occurred_at)."},
        "limit": {"type": "integer", "description": f"Rows per page (default {DEFAULT_LIST_LIMIT}, max {AUDIT_PAGE_MAX})."},
        "cursor": {"type": "string", "description": "Page cursor from a previous result's next_cursor."},
        "intent_message": _intent("Review recent Iru audit events"),
    },
    [],
    _tool_list_audit_events,
)

LIST_ADE_DEVICES_TOOL = _tool(
    "iru_list_ade_devices",
    "Automated Device Enrollment (Apple Business Manager) devices: serial, "
    "model, family, assigned blueprint / user, profile status (assigned / "
    "pushed / removed / empty), assignment history, and whether / when the "
    "device actually enrolled (mdm_device). Page with `page` (300 per page).",
    {
        "serial_number": {"type": "string", "description": "Serial number (partial)."},
        "device_family": {"type": "string", "description": "Mac, iPhone, iPad, iPod, AppleTV, or Vision."},
        "os": {"type": "string", "description": "OSX, iOS, iPadOS, tvOS, or visionOS."},
        "model": {"type": "string", "description": "Model (partial), e.g. 'MacBook Air'."},
        "profile_status": {"type": "string", "enum": ["assigned", "empty", "pushed", "removed"], "description": "Optional profile status."},
        "blueprint_id": {"type": "string", "description": "Optional blueprint id."},
        "user_id": {"type": "string", "description": "Optional assigned user id."},
        "ade_token_id": {"type": "string", "description": "Optional ADE token (dep_account) id."},
        "page": {"type": "integer", "description": "Page number (1-based)."},
        "intent_message": _intent("List ADE devices not yet enrolled"),
    },
    [],
    _tool_list_ade_devices,
)

IRU_TOOLS: tuple[PluginTool, ...] = (
    GET_TENANT_INFO_TOOL,
    LIST_DEVICES_TOOL,
    GET_DEVICE_TOOL,
    LIST_BLUEPRINTS_TOOL,
    GET_BLUEPRINT_TOOL,
    GET_LIBRARY_ITEM_STATUS_TOOL,
    LIST_USERS_TOOL,
    GET_USER_TOOL,
    LIST_TAGS_TOOL,
    PRISM_QUERY_TOOL,
    LIST_THREATS_TOOL,
    LIST_VULNERABILITIES_TOOL,
    GET_VULNERABILITY_TOOL,
    LIST_AUDIT_EVENTS_TOOL,
    LIST_ADE_DEVICES_TOOL,
)

# Every tool is a read-only JSON report with no workspace dependency, so
# sandbox scripts may call all of them via POST /api/tool-call (e.g. to
# page through the whole fleet into a CSV).
SCRIPT_TOOL_ALLOWLIST = frozenset(t.spec["name"] for t in IRU_TOOLS)
