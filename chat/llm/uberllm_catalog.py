"""Cached UberLLM model catalog for the admin typeahead and metadata
snapshots.

UberLLM (https://uberllm.dev) is an OpenAI-compatible router at
``https://api.uberllm.dev/v1`` (the ``uberllm`` instance kind in
config/inference_providers.py). Its chat model list, ``GET /v1/models``, is
**public** (no key) and uses the OpenRouter row shape -- ``<vendor>/<model>``
ids, ``context_length``, ``top_provider.max_completion_tokens``,
per-token price strings in ``pricing.prompt`` / ``pricing.completion`` /
``pricing.input_cache_read``, ``architecture.*_modalities`` and
``supported_parameters`` -- the same shape the TrustedRouter catalog reads,
so this module reuses that module's normalization (chat-model filter,
``tools`` / ``vision`` capabilities, null-for-unknown) instead of copying
it. Two UberLLM differences are handled here: the human name is in
``display_name`` (there is no ``name``), and rows marked ``available:
false`` (temporarily without a serving provider) are left out.

The normalized list is cached in ``data/uberllm_catalog.json`` for
:data:`CATALOG_TTL_SECONDS` (one cache serves every UberLLM instance); a
failed fetch keeps serving the stale cache -- or an empty list when there
never was one -- with the error reported alongside, so the UI falls back to
custom-id entry.
"""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request

from chat.llm import trustedrouter_catalog as _router_rows
from config.paths import UBERLLM_CATALOG_FILE

logger = logging.getLogger(__name__)

CATALOG_URL = "https://api.uberllm.dev/v1/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15

is_chat_model = _router_rows.is_chat_model


class UberLLMCatalogError(RuntimeError):
    """The catalog could not be fetched (network, unexpected shape)."""


def _normalize_entry(raw) -> dict | None:
    """Normalize one ``/v1/models`` entry; None when unusable, not a chat
    model, or marked unavailable."""
    if not isinstance(raw, dict) or raw.get("available") is False:
        return None
    if not isinstance(raw.get("name"), str) and isinstance(raw.get("display_name"), str):
        raw = {**raw, "name": raw["display_name"]}
    return _router_rows._normalize_entry(raw)


def _read_cache() -> dict | None:
    try:
        with open(UBERLLM_CATALOG_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable UberLLM catalog cache: %s", UBERLLM_CATALOG_FILE)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (_router_rows._cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def _write_cache(cache: dict) -> None:
    UBERLLM_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=UBERLLM_CATALOG_FILE.parent, prefix=".uberllm_catalog.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, UBERLLM_CATALOG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_catalog() -> list[dict]:
    """Download and normalize the live catalog (blocking; raises
    :class:`UberLLMCatalogError` on failure). No key: the list is public."""
    request = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise UberLLMCatalogError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise UberLLMCatalogError(str(exc) or exc.__class__.__name__) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise UberLLMCatalogError("Unexpected model list response shape")
    models: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        entry = _normalize_entry(raw)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    if not models:
        raise UberLLMCatalogError("Model list contained no chat models")
    models.sort(key=lambda m: m["id"].lower())
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
    except UberLLMCatalogError as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("UberLLM catalog fetch failed: %s", error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logger.warning("Failed to write UberLLM catalog cache", exc_info=True)
    return {**cache, "stale": False, "error": None}
