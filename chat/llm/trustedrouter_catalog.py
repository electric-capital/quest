"""Cached TrustedRouter model catalog for the admin typeahead and metadata
snapshots.

TrustedRouter (https://trustedrouter.com) is an OpenAI-compatible router at
``https://api.trustedrouter.com/v1`` (the ``trustedrouter`` instance kind in
config/inference_providers.py). Its model list, ``GET /v1/models``, is
**public** (no key) and uses the OpenRouter row shape: ``<vendor>/<model>``
ids (plus TrustedRouter's own ``trustedrouter/<name>`` routes such as
``trustedrouter/auto``), a human ``name``, ``context_length``,
``top_provider.max_completion_tokens`` and per-token price strings in
``pricing.prompt`` / ``pricing.completion`` / ``pricing.input_cache_read``,
so :func:`openrouter_catalog.normalize_openrouter_entry` does the base
normalization and the snapshots carry real prices. Like OpenRouter's, each
entry carries ``architecture.input_modalities`` / ``output_modalities`` and
``supported_parameters`` (``tools``, ``reasoning``...), which this module
uses to keep only chat models -- image / video / speech generators,
embedding models and other non-text outputs are dropped, and so are the
embedding / reranker models that are still text-in text-out -- and to flag
``tools`` / ``vision`` capabilities for the typeahead (an entry with no
parameter list is treated as "unknown", not "no tools"). The list is large
(~650 entries, ~4 MB), which the 24h cache below absorbs.

Settings > Inference Providers uses the result the same two ways as the
OpenRouter catalog: the "Add model" typeahead on a TrustedRouter card
searches it (``GET /admin/inference-providers/instances/<id>/catalog?q=``,
served without a stored key because the list is public), and when a model
is added the admin endpoint snapshots its metadata
(:func:`openrouter_catalog.catalog_snapshot`) into the instance config so
nothing at request time depends on the catalog.

The normalized list is cached in ``data/trustedrouter_catalog.json`` for
:data:`CATALOG_TTL_SECONDS` (one cache serves every TrustedRouter instance);
a failed fetch keeps serving the stale cache -- or an empty list when there
never was one -- with the error reported alongside, so the UI falls back to
custom-id entry.
"""

import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request

from chat.llm.openrouter_catalog import normalize_openrouter_entry
from config.paths import TRUSTEDROUTER_CATALOG_FILE

logger = logging.getLogger(__name__)

CATALOG_URL = "https://api.trustedrouter.com/v1/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15

# Text-in text-out entries that are nevertheless not chat models
# (embedding / reranker models); matched on the wire id.
_NON_CHAT_ID_RE = re.compile(r"(^|[/-])(embedding|reranker?)([-/]|$)", re.IGNORECASE)


class TrustedRouterCatalogError(RuntimeError):
    """The catalog could not be fetched (network, unexpected shape)."""


def _modalities(raw: dict, key: str) -> list[str]:
    """The lower-cased modality list under ``key`` (``input_modalities`` /
    ``output_modalities``): the nested ``architecture`` object's list (the
    OpenRouter row shape), falling back to a top-level list of the same
    name; empty when neither is a list."""
    architecture = raw.get("architecture")
    value = architecture.get(key) if isinstance(architecture, dict) else None
    if not isinstance(value, list):
        value = raw.get(key)
    if not isinstance(value, list):
        return []
    return [m.strip().lower() for m in value if isinstance(m, str)]


def is_chat_model(raw: dict) -> bool:
    """Whether a raw entry is a text chat model worth listing.

    Keeps entries that take text in and produce text out (a missing
    modality list counts as text, so a sparse entry is never hidden) and
    drops the embedding / reranker ids by name.
    """
    inputs = _modalities(raw, "input_modalities")
    outputs = _modalities(raw, "output_modalities")
    if inputs and "text" not in inputs:
        return False
    if outputs and "text" not in outputs:
        return False
    wire_id = raw.get("id")
    return not (isinstance(wire_id, str) and _NON_CHAT_ID_RE.search(wire_id))


def _normalize_entry(raw) -> dict | None:
    """Normalize one ``/v1/models`` entry; None when unusable or not a chat
    model. The OpenRouter-shaped base fields (id, name, context, output
    cap, $/1M prices) come from the shared normalizer; ``capabilities``
    (``tools`` / ``vision``) and the typeahead ``detail`` are added from
    the ``supported_parameters`` and modality lists."""
    if not isinstance(raw, dict) or not is_chat_model(raw):
        return None
    entry = normalize_openrouter_entry(raw)
    if entry is None:
        return None
    features = raw.get("supported_parameters")
    features = [f.lower() for f in features if isinstance(f, str)] if isinstance(features, list) else []
    vision = "image" in _modalities(raw, "input_modalities")
    capabilities: list[str] | None
    if features:
        capabilities = ["tools"] if "tools" in features else []
        if vision:
            capabilities.append("vision")
    else:
        # No parameter list published: unknown, so the typeahead must
        # not claim "no tool support".
        capabilities = ["vision"] if vision else None
    bits = ["vision"] if vision else []
    if "reasoning" in features:
        bits.append("reasoning")
    entry["detail"] = " · ".join(bits)
    entry["capabilities"] = capabilities
    return entry


def _cached_entry(raw) -> dict | None:
    """Validate one ALREADY-normalized cache entry."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    pricing = raw.get("pricing")
    if not (isinstance(pricing, dict) and "prompt" in pricing and "completion" in pricing):
        pricing = None
    name = raw.get("name")
    context_length = raw.get("context_length")
    max_completion = raw.get("max_completion_tokens")
    capabilities = raw.get("capabilities")
    return {
        "id": raw["id"],
        "name": name if isinstance(name, str) and name else raw["id"],
        "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
        "max_completion_tokens": (
            int(max_completion) if isinstance(max_completion, (int, float)) else None
        ),
        "pricing": pricing,
        "detail": raw.get("detail") if isinstance(raw.get("detail"), str) else "",
        "capabilities": (
            [c for c in capabilities if isinstance(c, str)] if isinstance(capabilities, list) else None
        ),
    }


def _read_cache() -> dict | None:
    try:
        with open(TRUSTEDROUTER_CATALOG_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable TrustedRouter catalog cache: %s", TRUSTEDROUTER_CATALOG_FILE)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (_cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def _write_cache(cache: dict) -> None:
    TRUSTEDROUTER_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=TRUSTEDROUTER_CATALOG_FILE.parent, prefix=".trustedrouter_catalog.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, TRUSTEDROUTER_CATALOG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_catalog() -> list[dict]:
    """Download and normalize the live catalog (blocking; raises
    :class:`TrustedRouterCatalogError` on failure). No key: the list is public."""
    request = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise TrustedRouterCatalogError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise TrustedRouterCatalogError(str(exc) or exc.__class__.__name__) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise TrustedRouterCatalogError("Unexpected model list response shape")
    models: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        entry = _normalize_entry(raw)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    if not models:
        raise TrustedRouterCatalogError("Model list contained no chat models")
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
    except TrustedRouterCatalogError as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("TrustedRouter catalog fetch failed: %s", error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logger.warning("Failed to write TrustedRouter catalog cache", exc_info=True)
    return {**cache, "stale": False, "error": None}
