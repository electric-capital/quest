"""Cached Fireworks AI model catalog for the admin typeahead and metadata
snapshots.

Fireworks (https://fireworks.ai) serves open-weight models behind an
OpenAI-compatible chat-completions API (``https://api.fireworks.ai/inference/v1``;
the ``fireworks`` instance kind in config/inference_providers.py). Its
public serverless catalog is the model list of the shared ``fireworks``
account, ``GET https://api.fireworks.ai/v1/accounts/fireworks/models``
(the management API -- a different path prefix from inference), which
unlike OpenRouter's list REQUIRES a bearer API key, so the catalog is
fetched with the instance's own stored key. Each entry carries the resource
name that doubles as the wire id (``accounts/fireworks/models/<name>``), a
display name, the context window, tool / image support flags and, under
``serverlessModes[].skuInfos``, the per-1M-token prices.

Settings > Inference Providers uses it the same two ways as the OpenRouter
catalog: the "Add model" typeahead on a Fireworks card searches it
(``GET /admin/inference-providers/instances/<id>/catalog?q=``), and when a
model is added the admin endpoint snapshots its metadata
(:func:`openrouter_catalog.catalog_snapshot`, which applies unchanged
because the entries share the OpenRouter row shape) into the instance
config so nothing at request time depends on the catalog.

The normalized list is cached in ``data/fireworks_catalog.json`` for
:data:`CATALOG_TTL_SECONDS` (the catalog is the same public list for every
key, so one cache serves every Fireworks instance); a failed fetch keeps
serving the stale cache -- or an empty list when there never was one --
with the error reported alongside, so the UI falls back to custom-id entry
(a dedicated deployment's ``accounts/<account>/deployments/<id>`` id is
always a custom id: the public catalog does not list it).
"""

import datetime
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from config.paths import FIREWORKS_CATALOG_FILE

logger = logging.getLogger(__name__)

CATALOG_URL = "https://api.fireworks.ai/v1/accounts/fireworks/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15
# Largest page the API allows; a bound on pages keeps a runaway
# ``nextPageToken`` from looping forever.
PAGE_SIZE = 200
MAX_PAGES = 25

_PER_MILLION = 1_000_000


class FireworksCatalogError(RuntimeError):
    """The catalog could not be fetched (network, auth, unexpected shape)."""


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _money_to_float(amount) -> float | None:
    """``{"units": "0", "nanos": 90000}`` (Google ``Money``) -> 0.00009."""
    if not isinstance(amount, dict):
        return None
    units = amount.get("units", 0)
    nanos = amount.get("nanos", 0)
    try:
        value = float(units or 0) + float(nanos or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _unit_multiplier(unit) -> float | None:
    """Scale an SKU's unit to $/1M tokens; None for non-token units."""
    text = str(unit or "").strip().lower().replace(",", "")
    if not text or "token" not in text:
        return None
    if text.startswith("1m") or text.startswith("1000000"):
        return 1.0
    if text.startswith("1k") or text.startswith("1000 "):
        return 1000.0
    if text.startswith("1 ") or text == "token" or text == "tokens":
        return float(_PER_MILLION)
    return None


def _pricing_from_sku_infos(sku_infos) -> dict | None:
    """Map Fireworks' SKU rows onto the ``{prompt, completion, cache_read?}``
    snapshot shape ($/1M tokens).

    SKUs are free-text names such as ``LLM input tokens (uncached)``, ``LLM
    input tokens (cached)`` and ``LLM output tokens``; matched by keyword so
    a renamed SKU degrades to "no pricing" rather than a wrong number.
    """
    if not isinstance(sku_infos, list):
        return None
    prompt = completion = cache_read = None
    for info in sku_infos:
        if not isinstance(info, dict):
            continue
        sku = str(info.get("sku") or "").lower()
        multiplier = _unit_multiplier(info.get("unit"))
        amount = _money_to_float(info.get("amount"))
        if multiplier is None or amount is None or "token" not in sku:
            continue
        rate = amount * multiplier
        if "output" in sku or "completion" in sku:
            if completion is None:
                completion = rate
        elif "input" in sku or "prompt" in sku:
            if "cached" in sku and "uncached" not in sku:
                if cache_read is None:
                    cache_read = rate
            elif prompt is None:
                prompt = rate
    if prompt is None or completion is None:
        return None
    pricing = {"prompt": prompt, "completion": completion}
    if cache_read is not None:
        pricing["cache_read"] = cache_read
    return pricing


def _deprecated(raw: dict, today: datetime.date) -> bool:
    """True when the entry's ``deprecationDate`` (a Google ``Date``) has
    passed; a missing or malformed date is "not deprecated"."""
    date = raw.get("deprecationDate")
    if not isinstance(date, dict):
        return False
    try:
        year, month, day = int(date.get("year", 0)), int(date.get("month", 0)), int(date.get("day", 0))
        if not year:
            return False
        return datetime.date(year, month or 1, day or 1) <= today
    except (TypeError, ValueError):
        return False


def _normalize_entry(raw, today: datetime.date | None = None) -> dict | None:
    """Keep the fields the typeahead and snapshots use; None when the entry
    is unusable or not serverless-callable (not READY, no serverless mode,
    deprecated)."""
    if not isinstance(raw, dict):
        return None
    wire_id = raw.get("name")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    wire_id = wire_id.strip()
    state = raw.get("state")
    if isinstance(state, str) and state and state != "READY":
        return None
    serverless_modes = raw.get("serverlessModes")
    serverless_modes = serverless_modes if isinstance(serverless_modes, list) else []
    if not raw.get("supportsServerless") and not serverless_modes:
        return None
    if _deprecated(raw, today or datetime.date.today()):
        return None

    pricing = None
    for mode in serverless_modes:
        if isinstance(mode, dict):
            pricing = _pricing_from_sku_infos(mode.get("skuInfos"))
            if pricing is not None:
                break

    # Same convention as self-hosted discovery: the typeahead flags a model
    # whose capabilities lack "tools" and renders the context itself.
    capabilities: list[str] = []
    if raw.get("supportsTools"):
        capabilities.append("tools")
    if raw.get("supportsImageInput"):
        capabilities.append("vision")
    context_length = _positive_int(raw.get("contextLength"))
    bits = ["vision"] if "vision" in capabilities else []
    name = raw.get("displayName")
    return {
        "id": wire_id,
        "name": name.strip() if isinstance(name, str) and name.strip() else wire_id.rsplit("/", 1)[-1],
        "context_length": context_length,
        # Fireworks publishes no per-model output cap; the request uses the
        # conservative default (DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS).
        "max_completion_tokens": None,
        "pricing": pricing,
        "detail": " · ".join(bits),
        "capabilities": capabilities,
    }


def _cached_entry(raw) -> dict | None:
    """Validate one ALREADY-normalized cache entry (prices per 1M)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    pricing = raw.get("pricing")
    if not (isinstance(pricing, dict) and "prompt" in pricing and "completion" in pricing):
        pricing = None
    name = raw.get("name")
    context_length = raw.get("context_length")
    capabilities = raw.get("capabilities")
    return {
        "id": raw["id"],
        "name": name if isinstance(name, str) and name else raw["id"],
        "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
        "max_completion_tokens": None,
        "pricing": pricing,
        "detail": raw.get("detail") if isinstance(raw.get("detail"), str) else "",
        "capabilities": [c for c in capabilities if isinstance(c, str)] if isinstance(capabilities, list) else [],
    }


def _read_cache() -> dict | None:
    try:
        with open(FIREWORKS_CATALOG_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable Fireworks catalog cache: %s", FIREWORKS_CATALOG_FILE)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (_cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def _write_cache(cache: dict) -> None:
    FIREWORKS_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=FIREWORKS_CATALOG_FILE.parent, prefix=".fireworks_catalog.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, FIREWORKS_CATALOG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _fetch_page(api_key: str, page_token: str | None) -> dict:
    query = {"pageSize": PAGE_SIZE}
    if page_token:
        query["pageToken"] = page_token
    request = urllib.request.Request(
        f"{CATALOG_URL}?{urllib.parse.urlencode(query)}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - best-effort error text
            pass
        detail = ""
        if body:
            try:
                parsed = json.loads(body)
                detail = str((parsed.get("error") or {}).get("message") or "") if isinstance(parsed, dict) else ""
            except ValueError:
                detail = ""
            detail = detail or body[:200]
        raise FireworksCatalogError(
            f"HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise FireworksCatalogError(str(exc) or exc.__class__.__name__) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise FireworksCatalogError("Unexpected catalog response shape")
    return payload


def fetch_catalog(api_key: str) -> list[dict]:
    """Download and normalize the live serverless catalog (blocking; raises
    :class:`FireworksCatalogError` on failure). Follows ``nextPageToken``
    until the list ends or :data:`MAX_PAGES` is reached."""
    if not api_key:
        raise FireworksCatalogError("No API key saved yet.")
    today = datetime.date.today()
    models: list[dict] = []
    seen: set[str] = set()
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        payload = _fetch_page(api_key, page_token)
        for raw in payload["models"]:
            entry = _normalize_entry(raw, today)
            if entry is not None and entry["id"] not in seen:
                seen.add(entry["id"])
                models.append(entry)
        page_token = payload.get("nextPageToken") or None
        if not page_token:
            break
    if not models:
        raise FireworksCatalogError("Catalog response listed no serverless models")
    models.sort(key=lambda m: m["id"])
    return models


def get_catalog(api_key: str | None, refresh: bool = False) -> dict:
    """Return ``{"models", "fetched_at", "stale", "error"}`` (blocking).

    Serves the cache while it is younger than the TTL, otherwise (or on
    ``refresh``) re-fetches with ``api_key``. A failed fetch -- including
    "no key" -- returns whatever cache exists (``stale: True``) with the
    error text; with no cache at all ``models`` is empty. Call via
    ``asyncio.to_thread`` from request handlers.
    """
    cache = _read_cache()
    fresh = cache is not None and (time.time() - cache["fetched_at"]) < CATALOG_TTL_SECONDS
    if fresh and not refresh:
        return {**cache, "stale": False, "error": None}
    try:
        models = fetch_catalog(api_key or "")
    except FireworksCatalogError as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("Fireworks catalog fetch failed: %s", error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logger.warning("Failed to write Fireworks catalog cache", exc_info=True)
    return {**cache, "stale": False, "error": None}
