"""Cached Claude API model catalog for the ``anthropic`` instance kind.

Anthropic's own API (``https://api.anthropic.com``; the ``anthropic`` kind in
config/inference_providers.py) lists the models a key can call at
``GET /v1/models`` (paginated with ``after_id``, authenticated with the
``x-api-key`` header plus the ``anthropic-version`` date header). Each
entry carries only an ``id`` -- an alias such as ``claude-sonnet-5`` or a
dated snapshot such as ``claude-haiku-4-5-20251001`` -- and a
``display_name``: no token limits, no prices, no capability flags.

The rest of a row is borrowed from the Vertex registry
(``registry_entry_for_wire_id`` in chat/llm/config.py matches the id to
the ``MODEL_REGISTRY`` entry serving the same model on Vertex): context
window and output cap, the thinking levels as an OpenRouter-shaped
``reasoning`` object (so the composer offers the same levels as for the
Vertex model) and today's list price from the static Anthropic table in
db/llm_pricing.py as the pricing snapshot. A model the registry does not
know yet keeps conservative defaults (200K context, 8K output, no price,
no thinking sublevel) and can still be added; it shows "no estimate" in
the cost reports. Entries matching a registry model carry ``registry_id``,
which also drives :func:`suggested_wire_ids` -- the models the first-run
setup card adds automatically.

Served and cached like the other key-fetched catalogs (see
chat/llm/catalog_cache.py): ``data/anthropic_catalog.json``, 24h TTL, the
same list for every key, stale-on-failure.
"""

import logging
import urllib.parse

from chat.llm.catalog_cache import CatalogError, get_json, serve_catalog
from config.paths import ANTHROPIC_CATALOG_FILE

logger = logging.getLogger(__name__)

MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
PAGE_SIZE = 100
MAX_PAGES = 20

# Limits assumed for a Claude model the registry does not describe.
DEFAULT_CONTEXT_LENGTH = 200_000
DEFAULT_MAX_COMPLETION_TOKENS = 8_192


def _normalize_entry(raw) -> dict | None:
    """One ``/v1/models`` entry as a catalog row; None when unusable."""
    if not isinstance(raw, dict):
        return None
    wire_id = raw.get("id")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    wire_id = wire_id.strip()
    from chat.llm.config import reasoning_from_registry_entry, registry_entry_for_wire_id
    from db.llm_pricing import static_pricing_snapshot

    match = registry_entry_for_wire_id("anthropic", wire_id)
    display = raw.get("display_name")
    entry: dict = {
        "id": wire_id,
        "name": display.strip() if isinstance(display, str) and display.strip() else wire_id,
        "context_length": DEFAULT_CONTEXT_LENGTH,
        "max_completion_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
        "pricing": None,
        "reasoning": None,
        "detail": "",
        # Every Claude model the API lists takes tools and images.
        "capabilities": ["tools", "vision"],
    }
    if match is not None:
        registry_id, registry = match
        entry["registry_id"] = registry_id
        entry["context_length"] = registry.get("max_input_tokens") or DEFAULT_CONTEXT_LENGTH
        entry["max_completion_tokens"] = (
            registry.get("max_output_tokens") or DEFAULT_MAX_COMPLETION_TOKENS
        )
        entry["pricing"] = static_pricing_snapshot("anthropic", registry_id)
        entry["reasoning"] = reasoning_from_registry_entry(registry)
        bits = []
        if registry.get("deprecated"):
            bits.append("deprecated")
        if entry["reasoning"]:
            bits.append("thinking")
        entry["detail"] = " · ".join(bits)
    return entry


def fetch_catalog(api_key: str) -> list[dict]:
    """Download and normalize the live model list (blocking; raises
    :class:`CatalogError` on failure). Follows ``has_more`` / ``last_id``
    pagination up to :data:`MAX_PAGES`."""
    if not api_key:
        raise CatalogError("No API key saved yet.")
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}
    models: list[dict] = []
    seen: set[str] = set()
    after_id: str | None = None
    for _ in range(MAX_PAGES):
        query: dict = {"limit": PAGE_SIZE}
        if after_id:
            query["after_id"] = after_id
        payload = get_json(f"{MODELS_URL}?{urllib.parse.urlencode(query)}", headers, label="Anthropic")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise CatalogError("Unexpected model list response shape")
        for raw in data:
            entry = _normalize_entry(raw)
            if entry is not None and entry["id"] not in seen:
                seen.add(entry["id"])
                models.append(entry)
        if not payload.get("has_more") or not isinstance(payload.get("last_id"), str):
            break
        after_id = payload["last_id"]
    if not models:
        raise CatalogError("Model list contained no models")
    return models


def get_catalog(api_key: str | None, refresh: bool = False) -> dict:
    """``{"models", "fetched_at", "stale", "error"}`` -- see
    :func:`catalog_cache.serve_catalog`. Blocking; call via ``asyncio.to_thread``."""
    return serve_catalog(
        ANTHROPIC_CATALOG_FILE,
        lambda: fetch_catalog(api_key or ""),
        refresh=refresh,
        label="Anthropic",
    )


def suggested_wire_ids(models: list[dict]) -> list[str]:
    """The ids the first-run setup card adds for a new Anthropic instance:
    every undated alias the registry knows and does not mark deprecated
    (the dated snapshots duplicate them), in catalog order."""
    from chat.llm.config import MODEL_REGISTRY

    picked: list[str] = []
    seen_registry: set[str] = set()
    for model in models:
        registry_id = model.get("registry_id")
        if not registry_id or registry_id in seen_registry:
            continue
        if MODEL_REGISTRY.get(registry_id, {}).get("deprecated"):
            continue
        tail = model["id"].rsplit("-", 1)[-1]
        if len(tail) == 8 and tail.isdigit():
            continue
        seen_registry.add(registry_id)
        picked.append(model["id"])
    return picked
