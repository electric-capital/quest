"""Cached NEAR AI Cloud model catalog for the admin typeahead and metadata
snapshots.

NEAR AI Cloud (https://cloud.near.ai) serves hosted frontier and open-weight
models -- several of them inside TEEs with attestation -- behind an
OpenAI-compatible chat-completions API at ``https://cloud-api.near.ai/v1``
(the ``nearai`` instance kind in config/inference_providers.py). Its model
list, ``GET /v1/models``, is **public** (no key, unlike Fireworks') and uses
the OpenRouter row shape: ``<vendor>/<model>`` ids, a human ``name``,
``context_length``, ``top_provider.max_completion_tokens`` (also mirrored as
``max_output_length``) and per-token price strings in ``pricing.prompt`` /
``pricing.completion`` / ``pricing.input_cache_read``, so
:func:`openrouter_catalog.normalize_openrouter_entry` does the base
normalization and the snapshots carry real prices (incl. the automatic
prompt-cache read rate). On top of that the list carries
``input_modalities`` / ``output_modalities`` and ``supported_features``
(``tools``, ``structured_outputs``, ``reasoning``...), which this module
uses to keep only chat models -- image generators (``output_modalities:
["image"]``), speech-to-text (``input_modalities: ["audio"]``), embedding
models (``output_modalities: ["embedding"]``) and other non-text outputs
are dropped, and so are the embedding / reranker models that are still
text-in text-out but not chat models -- and to flag ``tools`` / ``vision``
capabilities for the typeahead (an entry whose feature list is empty is
treated as "unknown", not "no tools": NEAR AI publishes empty feature lists
on freshly added models).

Settings > Inference Providers uses the result the same two ways as the
OpenRouter catalog: the "Add model" typeahead on a NEAR AI card searches it
(``GET /admin/inference-providers/instances/<id>/catalog?q=``, served
without a stored key because the list is public), and when a model is
added the admin endpoint snapshots its metadata
(:func:`openrouter_catalog.catalog_snapshot`) into the instance config so
nothing at request time depends on the catalog.

The normalized list is cached in ``data/nearai_catalog.json`` for
:data:`CATALOG_TTL_SECONDS` (one cache serves every NEAR AI instance); a
failed fetch keeps serving the stale cache -- or an empty list when there
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
from config.paths import NEARAI_CATALOG_FILE

logger = logging.getLogger(__name__)

CATALOG_URL = "https://cloud-api.near.ai/v1/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15

# Text-in text-out entries that are nevertheless not chat models (their
# descriptions say "embedding and ranking tasks"); matched on the wire id.
_NON_CHAT_ID_RE = re.compile(r"(^|[/-])(embedding|reranker?)([-/]|$)", re.IGNORECASE)


class NearAICatalogError(RuntimeError):
    """The catalog could not be fetched (network, unexpected shape)."""


def _modalities(raw: dict, key: str) -> list[str]:
    """The lower-cased modality list under ``key`` (``input_modalities`` /
    ``output_modalities``), falling back to the nested ``architecture``
    object's camelCase twin; empty when neither is a list."""
    value = raw.get(key)
    if not isinstance(value, list):
        architecture = raw.get("architecture")
        nested_key = "inputModalities" if key == "input_modalities" else "outputModalities"
        value = architecture.get(nested_key) if isinstance(architecture, dict) else None
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
    NEAR AI's feature and modality lists."""
    if not isinstance(raw, dict) or not is_chat_model(raw):
        return None
    entry = normalize_openrouter_entry(raw)
    if entry is None:
        return None
    features = raw.get("supported_features")
    features = [f.lower() for f in features if isinstance(f, str)] if isinstance(features, list) else []
    vision = "image" in _modalities(raw, "input_modalities")
    capabilities: list[str] | None
    if features:
        capabilities = ["tools"] if "tools" in features else []
        if vision:
            capabilities.append("vision")
    else:
        # No feature list published (yet): unknown, so the typeahead must
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
        with open(NEARAI_CATALOG_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable NEAR AI catalog cache: %s", NEARAI_CATALOG_FILE)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (_cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def _write_cache(cache: dict) -> None:
    NEARAI_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=NEARAI_CATALOG_FILE.parent, prefix=".nearai_catalog.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, NEARAI_CATALOG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_catalog() -> list[dict]:
    """Download and normalize the live catalog (blocking; raises
    :class:`NearAICatalogError` on failure). No key: the list is public."""
    request = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise NearAICatalogError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise NearAICatalogError(str(exc) or exc.__class__.__name__) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise NearAICatalogError("Unexpected model list response shape")
    models: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        entry = _normalize_entry(raw)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    if not models:
        raise NearAICatalogError("Model list contained no chat models")
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
    except NearAICatalogError as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("NEAR AI catalog fetch failed: %s", error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logger.warning("Failed to write NEAR AI catalog cache", exc_info=True)
    return {**cache, "stale": False, "error": None}
