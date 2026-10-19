"""Cached OpenAI model catalog for the ``openai`` instance kind.

OpenAI's API (``https://api.openai.com/v1``; the ``openai`` kind in
config/inference_providers.py, a bearer API key) lists every model the key
can call at ``GET /v1/models`` -- chat models, but also embeddings, speech,
image, realtime and moderation models, with no field saying which is
which, and no token limits or prices. The list is reduced to the chat
families by id (:func:`is_chat_model`: ``gpt-*`` / ``o<n>*`` /
``chatgpt-*`` ids minus the audio, realtime, image, search, transcription,
embedding, moderation and Responses-API-only (``codex``, ``-pro``,
``deep-research``, ``computer-use``) variants).

Limits, prices and reasoning levels come from :data:`KNOWN_MODELS`, a
hand-maintained table keyed by model family (a dated snapshot such as
``gpt-4.1-2025-04-14`` matches its undated family); it mirrors the public
price list and documented limits as of the date on the table. A model the
table lacks is still listed with a name derived from its id, the
conservative instance defaults and no price ("no estimate" in the cost
reports). The table's undated ids that the key can call are what
:func:`suggested_wire_ids` offers the first-run setup card.

Requests to OpenAI go through the shared ``OpenRouterProvider`` (OpenAI
message format), with two OpenAI-specific request shapes: the output cap
is sent as ``max_completion_tokens`` (the reasoning models reject
``max_tokens``) and a picked thinking level as the top-level
``reasoning_effort`` -- see ``_is_openai`` in chat/llm/openrouter_provider.py.

Served and cached like the other key-fetched catalogs (see
chat/llm/catalog_cache.py): ``data/openai_catalog.json``, 24h TTL,
stale-on-failure.
"""

import logging
import re

from chat.llm.catalog_cache import CatalogError, get_json, serve_catalog
from config.paths import OPENAI_CATALOG_FILE

logger = logging.getLogger(__name__)

MODELS_URL = "https://api.openai.com/v1/models"

_CHAT_ID_RE = re.compile(r"^(gpt-|o\d|chatgpt-)")
_EXCLUDED_FRAGMENTS = (
    "realtime", "audio", "tts", "transcribe", "image", "search", "instruct",
    "embedding", "moderation", "codex", "computer-use", "deep-research",
    "-pro", "whisper", "dall-e", "davinci", "babbage",
)
_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# Limits ($/1M tokens, context / output in tokens) and reasoning levels per
# model family, from OpenAI's model and pricing pages, checked 2025-08-08.
# ``reasoning`` is the OpenRouter-shaped object instance snapshots carry
# (see chat.llm.config.thinking_efforts_from_reasoning): the reasoning
# models take an effort level and cannot run with reasoning off, so none
# of them offers ``none``.
_REASONING_LEVELS = {
    "supported_efforts": ["low", "medium", "high"], "mandatory": True,
    "default_effort": "medium", "default_enabled": True,
}
_GPT5_REASONING_LEVELS = {
    "supported_efforts": ["minimal", "low", "medium", "high"], "mandatory": True,
    "default_effort": "medium", "default_enabled": True,
}
KNOWN_MODELS: dict[str, dict] = {
    "gpt-5": {
        "name": "GPT-5", "context_length": 400_000, "max_completion_tokens": 128_000,
        "pricing": {"prompt": 1.25, "completion": 10.00, "cache_read": 0.125},
        "reasoning": _GPT5_REASONING_LEVELS,
    },
    "gpt-5-mini": {
        "name": "GPT-5 mini", "context_length": 400_000, "max_completion_tokens": 128_000,
        "pricing": {"prompt": 0.25, "completion": 2.00, "cache_read": 0.025},
        "reasoning": _GPT5_REASONING_LEVELS,
    },
    "gpt-5-nano": {
        "name": "GPT-5 nano", "context_length": 400_000, "max_completion_tokens": 128_000,
        "pricing": {"prompt": 0.05, "completion": 0.40, "cache_read": 0.005},
        "reasoning": _GPT5_REASONING_LEVELS,
    },
    "gpt-4.1": {
        "name": "GPT-4.1", "context_length": 1_047_576, "max_completion_tokens": 32_768,
        "pricing": {"prompt": 2.00, "completion": 8.00, "cache_read": 0.50},
        "reasoning": None,
    },
    "gpt-4.1-mini": {
        "name": "GPT-4.1 mini", "context_length": 1_047_576, "max_completion_tokens": 32_768,
        "pricing": {"prompt": 0.40, "completion": 1.60, "cache_read": 0.10},
        "reasoning": None,
    },
    "gpt-4.1-nano": {
        "name": "GPT-4.1 nano", "context_length": 1_047_576, "max_completion_tokens": 32_768,
        "pricing": {"prompt": 0.10, "completion": 0.40, "cache_read": 0.025},
        "reasoning": None,
    },
    "gpt-4o": {
        "name": "GPT-4o", "context_length": 128_000, "max_completion_tokens": 16_384,
        "pricing": {"prompt": 2.50, "completion": 10.00, "cache_read": 1.25},
        "reasoning": None,
    },
    "gpt-4o-mini": {
        "name": "GPT-4o mini", "context_length": 128_000, "max_completion_tokens": 16_384,
        "pricing": {"prompt": 0.15, "completion": 0.60, "cache_read": 0.075},
        "reasoning": None,
    },
    "o3": {
        "name": "o3", "context_length": 200_000, "max_completion_tokens": 100_000,
        "pricing": {"prompt": 2.00, "completion": 8.00, "cache_read": 0.50},
        "reasoning": _REASONING_LEVELS,
    },
    "o4-mini": {
        "name": "o4-mini", "context_length": 200_000, "max_completion_tokens": 100_000,
        "pricing": {"prompt": 1.10, "completion": 4.40, "cache_read": 0.275},
        "reasoning": _REASONING_LEVELS,
    },
}


def is_chat_model(wire_id: str) -> bool:
    """Whether an id from ``/v1/models`` is a chat-completions model."""
    lowered = wire_id.lower()
    if not _CHAT_ID_RE.match(lowered):
        return False
    return not any(fragment in lowered for fragment in _EXCLUDED_FRAGMENTS)


def family_of(wire_id: str) -> str:
    """``gpt-4.1-2025-04-14`` -> ``gpt-4.1``; undated ids are their own family."""
    return _DATE_SUFFIX_RE.sub("", wire_id)


def friendly_name(wire_id: str) -> str:
    """``gpt-5.2-mini-2026-03-01`` -> ``GPT-5.2 mini (2026-03-01)``."""
    family = family_of(wire_id)
    date = wire_id[len(family) + 1:] if len(wire_id) > len(family) else ""
    known = KNOWN_MODELS.get(family)
    base = known["name"] if known else (
        family.replace("gpt-", "GPT-", 1) if family.startswith("gpt-") else family
    )
    return f"{base} ({date})" if date else base


def _normalize_entry(raw) -> dict | None:
    """One ``/v1/models`` entry as a catalog row; None when unusable or not
    a chat model."""
    if not isinstance(raw, dict):
        return None
    wire_id = raw.get("id")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    wire_id = wire_id.strip()
    if not is_chat_model(wire_id):
        return None
    known = KNOWN_MODELS.get(family_of(wire_id))
    entry: dict = {
        "id": wire_id,
        "name": friendly_name(wire_id),
        "context_length": known["context_length"] if known else None,
        "max_completion_tokens": known["max_completion_tokens"] if known else None,
        "pricing": dict(known["pricing"]) if known and known.get("pricing") else None,
        "reasoning": dict(known["reasoning"]) if known and known.get("reasoning") else None,
        "detail": "reasoning" if known and known.get("reasoning") else "",
        # Every chat model kept here takes tools; vision is not published
        # per model, so only the known families claim it.
        "capabilities": ["tools", "vision"] if known else ["tools"],
    }
    return entry


def fetch_catalog(api_key: str) -> list[dict]:
    """Download and normalize the live model list (blocking; raises
    :class:`CatalogError` on failure)."""
    if not api_key:
        raise CatalogError("No API key saved yet.")
    payload = get_json(MODELS_URL, {"Authorization": f"Bearer {api_key}"}, label="OpenAI")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise CatalogError("Unexpected model list response shape")
    models: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        entry = _normalize_entry(raw)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    if not models:
        raise CatalogError("Model list contained no chat models")
    # Known families first in table order (undated before dated), then the
    # rest by id.
    order = {family: index for index, family in enumerate(KNOWN_MODELS)}

    def _sort_key(model: dict):
        family = family_of(model["id"])
        return (family not in order, model["id"] != family, order.get(family, 0), model["id"])

    models.sort(key=_sort_key)
    return models


def get_catalog(api_key: str | None, refresh: bool = False) -> dict:
    """``{"models", "fetched_at", "stale", "error"}`` -- see
    :func:`catalog_cache.serve_catalog`. Blocking; call via ``asyncio.to_thread``."""
    return serve_catalog(
        OPENAI_CATALOG_FILE,
        lambda: fetch_catalog(api_key or ""),
        refresh=refresh,
        label="OpenAI",
    )


def suggested_wire_ids(models: list[dict]) -> list[str]:
    """The ids the first-run setup card adds for a new OpenAI instance: the
    undated ids of :data:`KNOWN_MODELS` the key can call, in table order."""
    listed = {m["id"] for m in models}
    return [family for family in KNOWN_MODELS if family in listed]
