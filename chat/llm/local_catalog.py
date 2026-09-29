"""Live model discovery for self-hosted inference instances.

Unlike OpenRouter's published catalog (chat/llm/openrouter_catalog.py),
what a self-hosted server can run is whatever is loaded or pulled on that
box right now, so nothing is cached: the admin "Add model" typeahead on a
self-hosted card asks the server each time, and the admin endpoint takes a
metadata snapshot from the same answer when a model is added.

Per API type (``LOCAL_API_TYPES`` in config/inference_providers.py):

- ``openai``: ``GET <base_url>/v1/models``. The ``id`` is what the request
  must name (llama.cpp: the model path or ``--alias``; vLLM: the served
  model name; LM Studio: the model key). llama.cpp also reports the
  loaded context size in ``meta.n_ctx`` (older builds only the training
  size ``n_ctx_train``; ``GET /props`` has the server-wide ``n_ctx`` as a
  fallback), so the snapshot's ``context_length`` is exact there and
  ``None`` -- meaning the conservative default -- elsewhere.
- ``ollama``: ``GET <base_url>/api/tags`` for the pulled models (name,
  family, parameter size, quantization) and, when the tag entry lacks it,
  ``POST /api/show`` for the training context length (``model_info
  "<arch>.context_length"``) and capabilities. The snapshot's
  ``context_length`` is what the provider will request as ``num_ctx``:
  the training size capped at ``DEFAULT_OLLAMA_CONTEXT_LENGTH`` (Ollama
  sizes the KV cache from it), editable per model afterwards.

Neither kind of server publishes a marketing name, so ``name`` is derived
from the id (:func:`friendly_name`) and the admin can rename the model in
the card. Pricing is the explicit zero of ``LOCAL_MODEL_PRICING``. Every
entry has the same keys as an OpenRouter catalog entry (plus ``detail``
and ``capabilities`` for the typeahead), so ``catalog_snapshot()`` and
``search_catalog()`` from the OpenRouter module apply unchanged.
"""

import asyncio
import logging
import os
import re
from typing import Any

from config.inference_providers import (
    DEFAULT_OLLAMA_CONTEXT_LENGTH,
    LOCAL_MODEL_PRICING,
    effective_api_key,
)

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_SECONDS = 10
# Bound on per-model /api/show lookups so a box with hundreds of pulled
# models still answers the typeahead promptly.
_MAX_SHOW_LOOKUPS = 40
_SHOW_CONCURRENCY = 4


class DiscoveryError(RuntimeError):
    """The server could not be asked for its models (down, wrong URL, auth)."""


def friendly_name(wire_id: str) -> str:
    """A readable default name for a server-reported model id.

    ``qwen2.5:0.5b`` -> ``Qwen2.5 0.5b``; ``/models/llama-3.2-3b-instruct-q4_k_m.gguf``
    -> ``Llama 3.2 3b Instruct Q4 K M``. Only a starting point -- the admin
    can rename the model.
    """
    base = wire_id.strip()
    if "/" in base or "\\" in base:
        base = os.path.basename(base.replace("\\", "/")) or base
    if base.lower().endswith(".gguf"):
        base = base[:-5]
    words = [w for w in re.split(r"[\s:_\-]+", base) if w]
    if not words:
        return wire_id
    return " ".join(w[0].upper() + w[1:] if w[0].isalpha() else w for w in words)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _entry(
    wire_id: str,
    *,
    context_length: int | None,
    detail: str,
    capabilities: list[str] | None,
) -> dict:
    return {
        "id": wire_id,
        "name": friendly_name(wire_id),
        "context_length": context_length,
        "max_completion_tokens": None,
        "pricing": dict(LOCAL_MODEL_PRICING),
        "detail": detail,
        "capabilities": capabilities,
    }


def _headers(instance: dict) -> dict:
    headers = {"Accept": "application/json"}
    api_key, _source = effective_api_key(instance["id"])
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _format_params(n_params: Any) -> str | None:
    n = _positive_int(n_params)
    if n is None:
        return None
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B params"
    return f"{n / 1_000_000:.0f}M params"


async def _get_json(client, url: str, headers: dict, *, method: str = "GET", json_body=None) -> Any:
    import httpx

    try:
        response = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"{url}: {exc}") from exc
    if response.status_code >= 400:
        text = response.text.strip()
        raise DiscoveryError(f"{url}: HTTP {response.status_code}{' ' + text[:200] if text else ''}")
    try:
        return response.json()
    except ValueError as exc:
        raise DiscoveryError(f"{url}: response is not JSON") from exc


async def _discover_openai(client, instance: dict) -> list[dict]:
    base_url = instance["base_url"]
    headers = _headers(instance)
    payload = await _get_json(client, f"{base_url}/v1/models", headers)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise DiscoveryError(f"{base_url}/v1/models: unexpected response shape")

    entries: list[dict] = []
    missing_context = False
    for raw in data:
        if not isinstance(raw, dict):
            continue
        wire_id = raw.get("id")
        if not isinstance(wire_id, str) or not wire_id.strip():
            continue
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        # llama.cpp: n_ctx is the loaded context (the real ceiling),
        # n_ctx_train the model's trained size.
        context_length = _positive_int(meta.get("n_ctx")) or _positive_int(meta.get("n_ctx_train"))
        if context_length is None:
            missing_context = True
        bits = [b for b in (_format_params(meta.get("n_params")), meta.get("ftype")) if b]
        owner = raw.get("owned_by")
        if isinstance(owner, str) and owner and owner not in ("library", "organization-owner"):
            bits.append(owner)
        entries.append(_entry(
            wire_id.strip(),
            context_length=context_length,
            detail=" · ".join(str(b) for b in bits),
            capabilities=None,
        ))

    if entries and missing_context:
        # llama.cpp (single-model mode) reports the server-wide context in
        # /props; other servers 404 here, which just leaves None.
        try:
            props = await _get_json(client, f"{base_url}/props", headers)
        except DiscoveryError:
            props = None
        settings = props.get("default_generation_settings") if isinstance(props, dict) else None
        n_ctx = _positive_int(settings.get("n_ctx")) if isinstance(settings, dict) else None
        if n_ctx:
            for entry in entries:
                if entry["context_length"] is None:
                    entry["context_length"] = n_ctx
    return entries


def _ollama_context(model_info: Any) -> int | None:
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if isinstance(key, str) and key.endswith(".context_length"):
            return _positive_int(value)
    return None


def ollama_requested_context(training_context: int | None) -> int:
    """The ``num_ctx`` snapshotted for a pulled model: its training size
    capped at the default (KV-cache memory grows with it)."""
    if training_context is None:
        return DEFAULT_OLLAMA_CONTEXT_LENGTH
    return min(training_context, DEFAULT_OLLAMA_CONTEXT_LENGTH)


async def _discover_ollama(client, instance: dict) -> list[dict]:
    base_url = instance["base_url"]
    headers = _headers(instance)
    payload = await _get_json(client, f"{base_url}/api/tags", headers)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise DiscoveryError(f"{base_url}/api/tags: unexpected response shape")

    raw_entries: list[tuple[str, dict, int | None, list[str] | None]] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        wire_id = raw.get("name") or raw.get("model")
        if not isinstance(wire_id, str) or not wire_id.strip():
            continue
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), list) else None
        raw_entries.append((
            wire_id.strip(), details, _positive_int(details.get("context_length")), capabilities,
        ))

    # Older Ollama versions report neither context nor capabilities in
    # /api/tags; ask /api/show for those (bounded, a few at a time).
    semaphore = asyncio.Semaphore(_SHOW_CONCURRENCY)

    async def _show(wire_id: str) -> dict | None:
        async with semaphore:
            try:
                payload = await _get_json(
                    client, f"{base_url}/api/show", headers,
                    method="POST", json_body={"model": wire_id},
                )
            except DiscoveryError as exc:
                logger.debug("Ollama /api/show failed for %s: %s", wire_id, exc)
                return None
        return payload if isinstance(payload, dict) else None

    needs_show = [
        wire_id for wire_id, _details, context, capabilities in raw_entries
        if context is None or capabilities is None
    ][:_MAX_SHOW_LOOKUPS]
    shown = dict(zip(needs_show, await asyncio.gather(*(_show(w) for w in needs_show))))

    entries: list[dict] = []
    for wire_id, details, context, capabilities in raw_entries:
        info = shown.get(wire_id) or {}
        if context is None:
            context = _ollama_context(info.get("model_info"))
        if capabilities is None and isinstance(info.get("capabilities"), list):
            capabilities = [c for c in info["capabilities"] if isinstance(c, str)]
        bits = [
            details.get("family"), details.get("parameter_size"), details.get("quantization_level"),
        ]
        detail = " · ".join(str(b) for b in bits if b)
        if context:
            detail = f"{detail} · trained on {context // 1000}K ctx" if detail else f"trained on {context // 1000}K ctx"
        entries.append(_entry(
            wire_id,
            context_length=ollama_requested_context(context),
            detail=detail,
            capabilities=capabilities,
        ))
    return entries


async def discover_models(instance: dict) -> dict:
    """Ask a self-hosted instance's server for its models.

    Returns ``{"models": [...], "error": str | None}``; ``models`` is empty
    with ``error`` set when the server cannot be reached or answers
    unexpectedly (the UI then offers custom-id entry only). Never raises
    for server problems; raises ``ValueError`` for an instance with no
    base URL (a caller bug).
    """
    if not instance.get("base_url"):
        raise ValueError("Instance has no base URL")
    import httpx

    api_type = instance.get("api_type") or "openai"
    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SECONDS) as client:
            if api_type == "ollama":
                models = await _discover_ollama(client, instance)
            else:
                models = await _discover_openai(client, instance)
    except DiscoveryError as exc:
        logger.info("Model discovery failed for instance %s: %s", instance["id"], exc)
        return {"models": [], "error": str(exc)}
    return {"models": models, "error": None}


def default_snapshot(instance: dict) -> dict:
    """Snapshot for a custom id the server did not list (or an unreachable
    server): zero pricing and, for Ollama, the default requested context
    so ``num_ctx`` is always explicit."""
    snapshot: dict = {"pricing": dict(LOCAL_MODEL_PRICING)}
    if instance.get("api_type") == "ollama":
        snapshot["context_length"] = DEFAULT_OLLAMA_CONTEXT_LENGTH
    return snapshot
