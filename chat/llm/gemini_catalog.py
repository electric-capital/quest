"""Cached Gemini Developer API model catalog for the ``gemini`` instance kind.

The Gemini Developer API (``https://generativelanguage.googleapis.com``;
the ``gemini`` kind in config/inference_providers.py, authenticated with a
Google AI Studio key in the ``x-goog-api-key`` header) lists its models at
``GET /v1beta/models`` (paginated with ``pageToken``). Unlike the other
vendors it publishes real limits per entry: ``inputTokenLimit`` /
``outputTokenLimit``, the ``displayName`` and the
``supportedGenerationMethods``. Only entries that support
``generateContent`` are kept, and the non-chat families the method list
does not rule out (embeddings, image / video / speech generation, the
Live API audio models) are dropped by id.

Wire ids are the entry's ``name`` without its ``models/`` prefix
(``gemini-3.8-flash``). Pricing and thinking levels come from the Vertex
registry entry serving the same model (``registry_entry_for_wire_id`` in
chat/llm/config.py; the static Gemini table in db/llm_pricing.py as the
pricing snapshot) -- a model the registry does not know has no price and
no thinking sublevel but keeps the API's own limits. Entries matching a
registry model carry ``registry_id``, which drives
:func:`suggested_wire_ids` (the models the first-run setup card adds).

Served and cached like the other key-fetched catalogs (see
chat/llm/catalog_cache.py): ``data/gemini_catalog.json``, 24h TTL,
stale-on-failure.
"""

import logging
import urllib.parse

from chat.llm.catalog_cache import CatalogError, get_json, serve_catalog
from config.paths import GEMINI_CATALOG_FILE

logger = logging.getLogger(__name__)

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
PAGE_SIZE = 100
MAX_PAGES = 20
NAME_PREFIX = "models/"

# Id fragments of the non-chat model families the API lists with
# ``generateContent`` support (or without a method list at all).
_EXCLUDED_FRAGMENTS = (
    "embedding", "aqa", "imagen", "veo", "-tts", "image-generation",
    "-live", "native-audio", "audio", "robotics", "computer-use",
)


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _normalize_entry(raw) -> dict | None:
    """One ``/v1beta/models`` entry as a catalog row; None when unusable or
    not a chat model."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    wire_id = name.strip()
    if wire_id.startswith(NAME_PREFIX):
        wire_id = wire_id[len(NAME_PREFIX):]
    if not wire_id:
        return None
    methods = raw.get("supportedGenerationMethods")
    if isinstance(methods, list) and "generateContent" not in methods:
        return None
    lowered = wire_id.lower()
    if any(fragment in lowered for fragment in _EXCLUDED_FRAGMENTS):
        return None
    from chat.llm.config import reasoning_from_registry_entry, registry_entry_for_wire_id
    from db.llm_pricing import static_pricing_snapshot

    match = registry_entry_for_wire_id("gemini", wire_id)
    display = raw.get("displayName")
    entry: dict = {
        "id": wire_id,
        "name": display.strip() if isinstance(display, str) and display.strip() else wire_id,
        "context_length": _positive_int(raw.get("inputTokenLimit")),
        "max_completion_tokens": _positive_int(raw.get("outputTokenLimit")),
        "pricing": None,
        "reasoning": None,
        "detail": "",
        # Function calling and images are supported across the Gemini
        # chat line; the list publishes no per-model flags.
        "capabilities": ["tools", "vision"],
    }
    if match is not None:
        registry_id, registry = match
        entry["registry_id"] = registry_id
        entry["context_length"] = entry["context_length"] or registry.get("max_input_tokens")
        entry["max_completion_tokens"] = (
            entry["max_completion_tokens"] or registry.get("max_output_tokens")
        )
        entry["pricing"] = static_pricing_snapshot("gemini", registry_id)
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
    :class:`CatalogError` on failure). Follows ``nextPageToken`` up to
    :data:`MAX_PAGES`."""
    if not api_key:
        raise CatalogError("No API key saved yet.")
    headers = {"x-goog-api-key": api_key}
    models: list[dict] = []
    seen: set[str] = set()
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        query: dict = {"pageSize": PAGE_SIZE}
        if page_token:
            query["pageToken"] = page_token
        payload = get_json(f"{MODELS_URL}?{urllib.parse.urlencode(query)}", headers, label="Gemini")
        data = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise CatalogError("Unexpected model list response shape")
        for raw in data:
            entry = _normalize_entry(raw)
            if entry is not None and entry["id"] not in seen:
                seen.add(entry["id"])
                models.append(entry)
        page_token = payload.get("nextPageToken") or None
        if not page_token:
            break
    if not models:
        raise CatalogError("Model list contained no chat models")
    return models


def get_catalog(api_key: str | None, refresh: bool = False) -> dict:
    """``{"models", "fetched_at", "stale", "error"}`` -- see
    :func:`catalog_cache.serve_catalog`. Blocking; call via ``asyncio.to_thread``."""
    return serve_catalog(
        GEMINI_CATALOG_FILE,
        lambda: fetch_catalog(api_key or ""),
        refresh=refresh,
        label="Gemini",
    )


def suggested_wire_ids(models: list[dict]) -> list[str]:
    """The ids the first-run setup card adds for a new Gemini instance:
    every model the registry knows and does not mark deprecated, in
    catalog order (one per registry entry)."""
    from chat.llm.config import MODEL_REGISTRY

    picked: list[str] = []
    seen_registry: set[str] = set()
    for model in models:
        registry_id = model.get("registry_id")
        if not registry_id or registry_id in seen_registry:
            continue
        if MODEL_REGISTRY.get(registry_id, {}).get("deprecated"):
            continue
        seen_registry.add(registry_id)
        picked.append(model["id"])
    return picked
