"""Cached OpenRouter model catalog for the admin typeahead and metadata
snapshots.

OpenRouter publishes its full model list at ``GET /api/v1/models`` (public,
no key needed): ~450 entries with the wire id, a human name, the context
window, the routed provider's output cap, and per-token prices. Settings >
Inference Providers uses it two ways:

- the "Add model" typeahead on an OpenRouter instance card searches it
  (``GET /admin/inference-providers/openrouter/catalog?q=``), and
- when a model is added to an instance the admin endpoint snapshots its
  metadata (:func:`catalog_snapshot`) into the instance config so the app
  never needs the catalog at request time: context/output limits feed the
  ``ModelSpec`` (chat/llm/config.py) and the pricing feeds
  ``db/llm_pricing.py``.

The list is cached in ``data/openrouter_catalog.json`` and refreshed at most
every :data:`CATALOG_TTL_SECONDS` (or on an explicit refresh); a failed
fetch keeps serving the stale cache -- and an empty list when there has
never been one -- with the error reported alongside, so the UI can fall
back to custom-id entry. Only what the typeahead and snapshots need is
kept per entry.
"""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request

from config.paths import OPENROUTER_CATALOG_FILE

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15
_PER_MILLION = 1_000_000


def _rate_per_million(value) -> float | None:
    """OpenRouter prices are strings in $/token; convert to $/1M tokens."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0:
        return None
    return rate * _PER_MILLION


def _normalize_entry(raw) -> dict | None:
    """Keep the fields the typeahead and snapshots use; None when unusable.

    Also exported as :func:`normalize_openrouter_entry` for catalogs that
    publish the OpenRouter row shape (NEAR AI Cloud's model list)."""
    if not isinstance(raw, dict):
        return None
    wire_id = raw.get("id")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    pricing_raw = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    pricing = None
    prompt = _rate_per_million(pricing_raw.get("prompt"))
    completion = _rate_per_million(pricing_raw.get("completion"))
    if prompt is not None and completion is not None:
        pricing = {"prompt": prompt, "completion": completion}
        cache_read = _rate_per_million(pricing_raw.get("input_cache_read"))
        if cache_read is not None:
            pricing["cache_read"] = cache_read
    name = raw.get("name")
    context_length = raw.get("context_length")
    max_completion = top.get("max_completion_tokens")
    return {
        "id": wire_id.strip(),
        "name": name.strip() if isinstance(name, str) and name.strip() else wire_id.strip(),
        "context_length": (
            int(context_length)
            if isinstance(context_length, (int, float)) and context_length > 0
            else None
        ),
        "max_completion_tokens": (
            int(max_completion)
            if isinstance(max_completion, (int, float)) and max_completion > 0
            else None
        ),
        "pricing": pricing,
    }


normalize_openrouter_entry = _normalize_entry


def _cached_entry(raw) -> dict | None:
    """Validate one ALREADY-normalized cache entry (the cache stores the
    output of :func:`_normalize_entry`, so it must not be normalized again:
    its prices are per-1M and its output cap is top-level)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    pricing = raw.get("pricing")
    if not (isinstance(pricing, dict) and "prompt" in pricing and "completion" in pricing):
        pricing = None
    name = raw.get("name")
    context_length = raw.get("context_length")
    max_completion = raw.get("max_completion_tokens")
    return {
        "id": raw["id"],
        "name": name if isinstance(name, str) and name else raw["id"],
        "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
        "max_completion_tokens": (
            int(max_completion) if isinstance(max_completion, (int, float)) else None
        ),
        "pricing": pricing,
    }


def _read_cache() -> dict | None:
    try:
        with open(OPENROUTER_CATALOG_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable OpenRouter catalog cache: %s", OPENROUTER_CATALOG_FILE)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (_cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def _write_cache(cache: dict) -> None:
    OPENROUTER_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=OPENROUTER_CATALOG_FILE.parent, prefix=".openrouter_catalog.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, OPENROUTER_CATALOG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_catalog() -> list[dict]:
    """Download and normalize the live catalog (blocking; raises on failure)."""
    request = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Unexpected catalog response shape")
    models = [m for m in (_normalize_entry(x) for x in data) if m]
    if not models:
        raise ValueError("Catalog response listed no models")
    return models


def get_catalog(refresh: bool = False) -> dict:
    """Return ``{"models", "fetched_at", "stale", "error"}`` (blocking).

    Serves the cache while it is younger than the TTL, otherwise (or on
    ``refresh``) re-fetches. A failed fetch returns whatever cache exists
    (``stale: True``) with the error text; with no cache at all ``models``
    is empty. Call via ``asyncio.to_thread`` from request handlers.
    """
    cache = _read_cache()
    fresh = cache is not None and (time.time() - cache["fetched_at"]) < CATALOG_TTL_SECONDS
    if fresh and not refresh:
        return {**cache, "stale": False, "error": None}
    try:
        models = fetch_catalog()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("OpenRouter catalog fetch failed: %s", error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logger.warning("Failed to write OpenRouter catalog cache", exc_info=True)
    return {**cache, "stale": False, "error": None}


def search_catalog(models: list[dict], query: str, limit: int = 20) -> list[dict]:
    """Case-insensitive substring match on id and name; id-prefix hits first."""
    q = (query or "").strip().lower()
    if not q:
        return models[:limit]
    prefix, other = [], []
    for model in models:
        wire_id = model["id"].lower()
        if wire_id.startswith(q):
            prefix.append(model)
        elif q in wire_id or q in model["name"].lower():
            other.append(model)
    return (prefix + other)[:limit]


def catalog_snapshot(models: list[dict], wire_id: str) -> dict | None:
    """The metadata snapshot for one wire id, or None when not listed."""
    for model in models:
        if model["id"] == wire_id:
            return {
                "name": model["name"],
                "context_length": model["context_length"],
                "max_completion_tokens": model["max_completion_tokens"],
                "pricing": model["pricing"],
            }
    return None
