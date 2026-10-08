"""Cached Fireworks AI model catalog for the admin typeahead and metadata
snapshots.

Fireworks (https://fireworks.ai) serves open-weight models behind an
OpenAI-compatible chat-completions API (``https://api.fireworks.ai/inference/v1``;
the ``fireworks`` instance kind in config/inference_providers.py). The
catalog is built from two authenticated endpoints (both need the instance's
bearer API key -- unlike OpenRouter's public list):

- ``GET /inference/v1/models`` (OpenAI-style ``{"data": [...]}``) is the
  authoritative list of ids the key can call serverlessly: the shared
  ``accounts/fireworks/models/<name>`` models, the ``accounts/fireworks/
  routers/<name>`` speed/cost routers and the ``auto`` / ``firerouter/auto``
  meta-routers, each with ``supports_chat`` / ``supports_tools`` /
  ``supports_image_input`` flags, ``context_length`` and a ``kind``
  (``HF_BASE_MODEL``, ``ROUTER``, ``EMBEDDING_MODEL``...). Embedding /
  reranker models (``kind: EMBEDDING_MODEL``, which Fireworks still marks
  ``supports_chat``) and entries without chat support are dropped; routers
  are kept and flagged.
- ``GET /v1/accounts/fireworks/models`` (the management API, paginated) is
  consulted best-effort for the human ``displayName`` and the
  ``deprecationDate`` of the shared models; a failure there only costs the
  friendly names. It exposes no pricing (``serverlessModes`` is empty on
  every public entry, checked 2026-10-08), so Fireworks snapshots carry
  ``pricing: None`` and the cost reports show "no estimate" for them.

Settings > Inference Providers uses the result the same two ways as the
OpenRouter catalog: the "Add model" typeahead on a Fireworks card searches
it (``GET /admin/inference-providers/instances/<id>/catalog?q=``), and when
a model is added the admin endpoint snapshots its metadata
(:func:`openrouter_catalog.catalog_snapshot`, which applies unchanged
because the entries share the OpenRouter row shape) into the instance
config so nothing at request time depends on the catalog.

The normalized list is cached in ``data/fireworks_catalog.json`` for
:data:`CATALOG_TTL_SECONDS` (the serverless list is the same for every
key, so one cache serves every Fireworks instance); a failed fetch keeps
serving the stale cache -- or an empty list when there never was one --
with the error reported alongside, so the UI falls back to custom-id entry
(a dedicated deployment's ``accounts/<account>/deployments/<id>`` id is
always a custom id: the serverless list does not include it).
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

INFERENCE_MODELS_URL = "https://api.fireworks.ai/inference/v1/models"
MANAGEMENT_MODELS_URL = "https://api.fireworks.ai/v1/accounts/fireworks/models"
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15
# Largest page the management API allows; a bound on pages keeps a runaway
# ``nextPageToken`` from looping forever.
PAGE_SIZE = 200
MAX_PAGES = 25


class FireworksCatalogError(RuntimeError):
    """The catalog could not be fetched (network, auth, unexpected shape)."""


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _is_router(raw: dict, wire_id: str) -> bool:
    kind = raw.get("kind")
    if isinstance(kind, str) and kind:
        return kind == "ROUTER"
    return "/routers/" in wire_id or wire_id.endswith("/auto") or wire_id == "auto"


def friendly_name(wire_id: str, router: bool = False) -> str:
    """A readable default name for an id the management list does not name:
    the last path segment, ``accounts/fireworks/routers/glm-5p3-fast`` ->
    ``glm-5p3-fast (router)`` / ``firerouter/auto`` -> ``auto (router)``
    for routers."""
    base = wire_id.strip().rsplit("/", 1)[-1]
    return f"{base} (router)" if router else base


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


def _normalize_entry(raw, details: dict | None = None, today: datetime.date | None = None) -> dict | None:
    """Normalize one ``/inference/v1/models`` entry; None when unusable or
    not a chat model.

    ``details`` maps a resource name to its management-list entry, used for
    the display name and the deprecation date (a deprecated model is
    dropped). ``context_length`` prefers the inference list's figure, then
    the management entry's ``contextLength``.
    """
    if not isinstance(raw, dict):
        return None
    wire_id = raw.get("id")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    wire_id = wire_id.strip()
    if raw.get("supports_chat") is False or raw.get("kind") == "EMBEDDING_MODEL":
        return None
    detail = (details or {}).get(wire_id)
    detail = detail if isinstance(detail, dict) else {}
    if _deprecated(detail, today or datetime.date.today()):
        return None

    # Same convention as self-hosted discovery: the typeahead flags a model
    # whose capabilities lack "tools" and renders the context itself.
    capabilities: list[str] = []
    if raw.get("supports_tools") or detail.get("supportsTools"):
        capabilities.append("tools")
    if raw.get("supports_image_input") or detail.get("supportsImageInput"):
        capabilities.append("vision")
    context_length = _positive_int(raw.get("context_length")) or _positive_int(detail.get("contextLength"))
    bits = ["vision"] if "vision" in capabilities else []
    router = _is_router(raw, wire_id)
    if router:
        bits.append("router")
    name = detail.get("displayName")
    return {
        "id": wire_id,
        "name": name.strip() if isinstance(name, str) and name.strip() else friendly_name(wire_id, router),
        "context_length": context_length,
        # Fireworks publishes no per-model output cap; the request uses the
        # conservative default (DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS).
        "max_completion_tokens": None,
        # No API exposes serverless prices (see module docstring).
        "pricing": None,
        "detail": " · ".join(bits),
        "capabilities": capabilities,
    }


def _cached_entry(raw) -> dict | None:
    """Validate one ALREADY-normalized cache entry."""
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


def _get_json(url: str, api_key: str):
    """GET ``url`` with the bearer key; raises :class:`FireworksCatalogError`
    with the Fireworks ``error.message`` (or the status) on failure."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return json.load(response)
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
                error = parsed.get("error") if isinstance(parsed, dict) else None
                detail = str(error.get("message") or "") if isinstance(error, dict) else ""
                if not detail and isinstance(parsed, dict):
                    detail = str(parsed.get("message") or "")
            except ValueError:
                detail = ""
            detail = detail or body[:200]
        raise FireworksCatalogError(
            f"HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise FireworksCatalogError(str(exc) or exc.__class__.__name__) from exc


def fetch_inference_models(api_key: str) -> list[dict]:
    """The raw ``/inference/v1/models`` entries (raises on failure)."""
    payload = _get_json(INFERENCE_MODELS_URL, api_key)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise FireworksCatalogError("Unexpected model list response shape")
    return [m for m in data if isinstance(m, dict)]


def fetch_model_details(api_key: str) -> dict[str, dict]:
    """The management list of the shared ``fireworks`` account keyed by
    resource name (raises on failure). Follows ``nextPageToken`` until the
    list ends or :data:`MAX_PAGES` is reached."""
    details: dict[str, dict] = {}
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        query: dict = {"pageSize": PAGE_SIZE}
        if page_token:
            query["pageToken"] = page_token
        payload = _get_json(f"{MANAGEMENT_MODELS_URL}?{urllib.parse.urlencode(query)}", api_key)
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise FireworksCatalogError("Unexpected catalog response shape")
        for raw in models:
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                details[raw["name"]] = raw
        page_token = payload.get("nextPageToken") or None
        if not page_token:
            break
    return details


def fetch_catalog(api_key: str) -> list[dict]:
    """Download and normalize the live serverless catalog (blocking; raises
    :class:`FireworksCatalogError` on failure).

    The inference list is required; the management list is best effort
    (its failure is logged and the entries keep their derived names).
    """
    if not api_key:
        raise FireworksCatalogError("No API key saved yet.")
    raw_models = fetch_inference_models(api_key)
    try:
        details = fetch_model_details(api_key)
    except FireworksCatalogError as exc:
        logger.info("Fireworks model details unavailable, using derived names: %s", exc)
        details = {}
    today = datetime.date.today()
    models: list[dict] = []
    seen: set[str] = set()
    for raw in raw_models:
        entry = _normalize_entry(raw, details, today)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    if not models:
        raise FireworksCatalogError("Model list contained no chat models")
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
