"""Provider configuration, model registry, and model resolution.

Maps model ids to ``ModelSpec`` records (provider, wire id, limits, per-model
knobs) and manages provider singleton instances. Two model sources feed the
same resolver:

- the fixed Vertex catalog below (``MODEL_REGISTRY``: every Gemini and
  Anthropic model, bare ids), filtered by the admin's Vertex disabled set;
- admin-configured provider instances (``config/inference_providers.py``:
  OpenRouter configurations with their own key, and self-hosted endpoints
  with their own base URL / API type, each with its own model list),
  whose models carry qualified ids ``<instance_id>:<wire_id>``.

Consumers should go through :func:`resolve_model` / the helper functions
rather than indexing ``MODEL_REGISTRY`` directly, so instance models
resolve too.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# The fixed Vertex catalog: every Gemini and Anthropic model runs on Vertex
# AI, auth via ADC + project/region. OpenRouter (and future API-key) models
# are NOT in here -- they live in admin-configured provider instances
# (config/inference_providers.py) and resolve through resolve_model(). The Gemini developer-endpoint ("genapi") transport was
# removed: thought signatures are not portable across the Vertex / AI
# Studio border, so mixing backends in one conversation history broke
# mid-conversation model switches with signature-validation errors.
# ``vertex_model_id`` is the publisher id used on Vertex (defaults to the
# registry key when absent). Used by both Gemini-on-Vertex and Anthropic.
#
# ``deprecated`` marks a model that stays fully runnable (existing
# conversations, routines, and stored defaults keep working) but is excluded
# from get_available_models() so it disappears from the new-conversation
# picker. The FE mirrors the flag in frontend/src/constants/models.ts.
#
# ``thinking_effort`` (Anthropic only) turns on adaptive thinking for the
# model and sets the ``output_config.effort`` level sent with every request
# ("low" | "medium" | "high" | "xhigh" | "max"). Models without the key run
# with thinking off (the pre-4.6-era behavior: omitting the ``thinking``
# param disables it on Opus 4.7/4.8). Keep the value constant for a model:
# effort shapes the rendered prompt, so changing it between requests of one
# conversation invalidates the Anthropic prompt cache.
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "gemini-3.1-pro-preview": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.1 Pro",
        "vertex_model_id": "gemini-3.1-pro-preview",
        "deprecated": True,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    "gemini-3-flash-preview": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3 Flash",
        "vertex_model_id": "gemini-3-flash-preview",
        "deprecated": True,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    "gemini-3.1-flash-lite-preview": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.1 Flash-Lite",
        # Historically ran on the genapi backend under the ``-preview`` id;
        # the Vertex publisher catalog serves the same model without the
        # suffix (the ``-preview`` id 404s on Vertex).
        "vertex_model_id": "gemini-3.1-flash-lite",
        "deprecated": True,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    "gemini-3.5-flash": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.5 Flash",
        "vertex_model_id": "gemini-3.5-flash",
        "deprecated": True,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
    },
    "gemini-3.5-flash-lite": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.5 Flash-Lite",
        "vertex_model_id": "gemini-3.5-flash-lite",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
    },
    "gemini-3.6-flash": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.6 Flash",
        "vertex_model_id": "gemini-3.6-flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    "gemini-3.7-flash": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.7 Flash",
        "vertex_model_id": "gemini-3.7-flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    # Released 2026-09-02 (1,048,576-token input window, 65,536 output,
    # thinking low/medium/high -- ``minimal`` is rejected). Limits kept in
    # line with the other Flash entries.
    "gemini-3.8-flash": {
        "provider": "gemini",
        "backend": "vertex",
        "display_name": "Gemini 3.8 Flash",
        "vertex_model_id": "gemini-3.8-flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_000,
    },
    "claude-haiku-4.5": {
        "provider": "anthropic",
        "display_name": "Claude Haiku 4.5",
        "vertex_model_id": "claude-haiku-4-5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
    },
    "claude-sonnet-4-6": {
        "provider": "anthropic",
        "display_name": "Claude Sonnet 4.6",
        "vertex_model_id": "claude-sonnet-4-6",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
    },
    "claude-opus-4-6": {
        "provider": "anthropic",
        "display_name": "Claude Opus 4.6",
        "vertex_model_id": "claude-opus-4-6",
        "max_input_tokens": 200_000,
        "max_output_tokens": 16_384,
    },
    "claude-opus-4-7": {
        "provider": "anthropic",
        "display_name": "Claude Opus 4.7",
        "vertex_model_id": "claude-opus-4-7",
        "vertex_region": "global",
        "max_input_tokens": 200_000,
        "max_output_tokens": 16_384,
    },
    "claude-opus-4-8": {
        "provider": "anthropic",
        "display_name": "Claude Opus 4.8",
        "vertex_model_id": "claude-opus-4-8",
        "vertex_region": "global",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "thinking_effort": "medium",
    },
    "claude-sonnet-5": {
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5",
        "vertex_model_id": "claude-sonnet-5",
        "vertex_region": "global",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
    },
    # Released 2026-09-28. Same 1M/128K limits and $2/$10 list price as
    # Sonnet 5 (same tokenizer too). Like Opus 5.5, thinking can no longer
    # be switched off -- ``thinking: disabled`` and manual ``budget_tokens``
    # both 400; the lowest setting is ``between_tools`` -- so
    # ``thinking_effort`` is pinned explicitly at the model's own "high"
    # default (the same level Sonnet 5 runs at here when the API default
    # applies) rather than left to the API. Forced tool use (``tool_choice``
    # any/tool) is rejected; the provider only ever uses the default auto
    # choice. Notes longer than a sentence or two written between tool
    # calls arrive as progress-update ``thinking`` blocks (empty at the
    # default display), so that narration is not streamed to the UI. Its
    # thinking blocks are bound to the model and the conversation: a refusal
    # fallback or mid-conversation switch to another model runs without
    # them (dropped server-side, not an error).
    "claude-sonnet-5-5": {
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5.5",
        "vertex_model_id": "claude-sonnet-5-5",
        "vertex_region": "global",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "thinking_effort": "high",
        # Declines in more categories than Sonnet 5 (cyber, frontier_llm,
        # bio, reasoning_extraction, general_harms). Anthropic's own
        # server-side default retries cyber/frontier_llm declines on
        # Sonnet 5, so that is the client-side chain here too.
        "refusal_fallback_models": ["claude-sonnet-5"],
    },
    "claude-opus-5": {
        "provider": "anthropic",
        "display_name": "Claude Opus 5",
        "vertex_model_id": "claude-opus-5",
        "vertex_region": "global",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        # Opus 5 ships with elevated safety classifiers that can decline a
        # request (HTTP 200, stop_reason "refusal") -- benign security work
        # occasionally trips them. Requests refused this way are retried on
        # these models, in order, via the Anthropic SDK's client-side
        # refusal-fallback middleware (Vertex has no server-side `fallbacks`
        # param). Opus 4.8 is Anthropic's recommended fallback for
        # cyber-category refusals. Entries must be Anthropic registry models.
        "refusal_fallback_models": ["claude-opus-4-8"],
    },
    # Released 2026-09-22. Same 1M/128K limits as Opus 5 at a lower list
    # price ($4/$20 per 1M tokens). Adaptive thinking is ALWAYS on for this
    # model -- ``thinking: disabled`` and manual ``budget_tokens`` both 400
    # -- so ``thinking_effort`` is set explicitly (the model's own default
    # is "medium"; Opus 5's was "high") rather than left to the API. Forced
    # tool use (``tool_choice`` any/tool) is rejected; the provider only
    # ever uses the default auto choice. Text the model writes between tool
    # calls arrives as progress-update ``thinking`` blocks (empty at the
    # default display), so between-tool-call narration is not streamed to
    # the UI on this model. Its thinking blocks can only be read back by
    # itself (and Fable/Mythos 5.1 on the Claude API): a refusal fallback or
    # mid-conversation switch to another model runs without them (dropped
    # server-side, not an error).
    "claude-opus-5-5": {
        "provider": "anthropic",
        "display_name": "Claude Opus 5.5",
        "vertex_model_id": "claude-opus-5-5",
        "vertex_region": "global",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "thinking_effort": "medium",
        # Broader safety classifiers than Opus 5 (cyber + bio +
        # reasoning_extraction). Same client-side refusal-fallback chain.
        "refusal_fallback_models": ["claude-opus-4-8"],
    },
}


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

# Vertex model families, keyed by LLMProvider name, with the server_config
# section that must carry a project id for the family to count as
# configured and the label shown in pickers/admin cards.
VERTEX_FAMILIES: dict[str, dict[str, str]] = {
    "anthropic": {"section": "anthropic", "label": "Claude on Vertex"},
    "gemini": {"section": "gemini_vertex", "label": "Gemini on Vertex"},
}


@dataclass(frozen=True)
class ModelSpec:
    """Everything the app needs to know about one selectable model.

    ``id`` is the stored/qualified id (what conversations, routines and
    user defaults carry); ``wire_id`` is what goes in the API request.
    ``enabled`` reflects the admin toggle (Vertex disabled set / instance
    model flag); ``listed`` is False for an instance model the admin has
    removed from its instance but that old conversations still reference.
    """
    id: str
    wire_id: str
    provider: str
    backend: str
    display_name: str
    provider_label: str
    max_input_tokens: int
    max_output_tokens: int
    instance_id: str | None = None
    deprecated: bool = False
    enabled: bool = True
    listed: bool = True
    vertex_region: str | None = None
    thinking_effort: str | None = None
    refusal_fallback_models: tuple[str, ...] = field(default_factory=tuple)

    @property
    def family(self) -> str | None:
        """Vertex family key (``anthropic`` / ``gemini_vertex``); None otherwise."""
        if self.instance_id is not None:
            return None
        return VERTEX_FAMILIES[self.provider]["section"]


def _vertex_spec(model_id: str, entry: dict, disabled: set[str]) -> ModelSpec:
    provider = entry["provider"]
    return ModelSpec(
        id=model_id,
        wire_id=entry.get("vertex_model_id", model_id),
        provider=provider,
        backend="vertex",
        display_name=entry.get("display_name", model_id),
        provider_label=VERTEX_FAMILIES[provider]["label"],
        max_input_tokens=entry.get("max_input_tokens", 0),
        max_output_tokens=entry.get("max_output_tokens", 8192),
        deprecated=bool(entry.get("deprecated")),
        enabled=model_id not in disabled,
        vertex_region=entry.get("vertex_region"),
        thinking_effort=entry.get("thinking_effort"),
        refusal_fallback_models=tuple(entry.get("refusal_fallback_models") or ()),
    )


def _instance_spec(instance: dict, model: dict | None, wire_id: str) -> ModelSpec:
    from config.inference_providers import (
        DEFAULT_INSTANCE_CONTEXT_LENGTH,
        DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS,
        INSTANCE_KINDS,
        MAX_INSTANCE_OUTPUT_TOKENS,
        qualify_model_id,
    )

    kind = INSTANCE_KINDS[instance["kind"]]
    model = model or {}
    max_output = model.get("max_completion_tokens") or DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS
    return ModelSpec(
        id=qualify_model_id(instance["id"], wire_id),
        wire_id=wire_id,
        provider=kind["provider"],
        backend=kind["backend"],
        display_name=model.get("name") or wire_id,
        provider_label=instance["label"],
        max_input_tokens=model.get("context_length") or DEFAULT_INSTANCE_CONTEXT_LENGTH,
        max_output_tokens=min(max_output, MAX_INSTANCE_OUTPUT_TOKENS),
        instance_id=instance["id"],
        enabled=bool(model) and model.get("enabled", True),
        listed=bool(model),
    )


def resolve_model(model_id: str) -> ModelSpec | None:
    """Resolve any stored model id to its spec; None when unknown.

    Order: the Vertex registry (bare ids), then ``<instance_id>:<wire_id>``
    against the configured instances (a model the instance no longer lists
    still resolves, unlisted and disabled, so old conversations keep their
    provider), then a bare legacy OpenRouter id mapped onto the
    ``openrouter`` instance. An id whose instance does not exist is unknown.
    """
    from config.inference_providers import (
        canonical_model_id,
        get_instance,
        split_model_id,
        vertex_disabled_models,
    )

    if not isinstance(model_id, str) or not model_id:
        return None
    entry = MODEL_REGISTRY.get(model_id)
    if entry is not None:
        return _vertex_spec(model_id, entry, vertex_disabled_models())
    instance_id, wire_id = split_model_id(canonical_model_id(model_id))
    if instance_id is None:
        return None
    instance = get_instance(instance_id)
    if instance is None:
        return None
    listed = next((m for m in instance["models"] if m["id"] == wire_id), None)
    return _instance_spec(instance, listed, wire_id)


def list_model_specs() -> list[ModelSpec]:
    """Every known model: registry order, then each instance's models in
    admin order. Includes deprecated and disabled models (for lookups)."""
    from config.inference_providers import load_inference_config

    config = load_inference_config()
    disabled = set(config["vertex"]["disabled_models"])
    specs = [_vertex_spec(mid, entry, disabled) for mid, entry in MODEL_REGISTRY.items()]
    for instance in config["instances"]:
        for model in instance["models"]:
            specs.append(_instance_spec(instance, model, model["id"]))
    return specs


def get_provider_for_model(model_id: str) -> str:
    """Return the provider name for a model id.

    Returns:
        Provider name string: 'gemini', 'anthropic', or 'openrouter'.

    Raises:
        ValueError: If the model id is unknown.
    """
    spec = resolve_model(model_id)
    if spec is None:
        raise ValueError(
            f"Unknown model: {model_id}. "
            f"Valid models: {[s.id for s in list_model_specs() if not s.deprecated]}"
        )
    return spec.provider


def get_backend_for_model(model_id: str) -> str | None:
    """Return the SDK transport backend label for cost analytics.

    Vertex-served models (all Gemini and Anthropic models) resolve to
    ``"vertex"``; instance-served models to their kind's backend label
    (``"openrouter"``, ``"local"``). Historical analytics rows recorded under ``"genapi"``
    keep that label in the DB. ``None`` for unknown models so callers can
    record without raising.
    """
    spec = resolve_model(model_id)
    return spec.backend if spec else None


def get_model_display_name(model_id: str) -> str:
    """Human-readable name, falling back to the raw id for unknown models."""
    spec = resolve_model(model_id)
    return spec.display_name if spec else model_id


def get_max_input_tokens(model_id: str) -> int:
    """Context-window ceiling, 0 for unknown models."""
    spec = resolve_model(model_id)
    return spec.max_input_tokens if spec else 0


def model_instance_id(model_id: str) -> str | None:
    """The instance qualifier of a stored model id (None for Vertex ids and
    bare legacy OpenRouter ids). A pure parse -- never raises -- so call
    sites can pass it to :func:`get_provider_instance` unconditionally."""
    from config.inference_providers import split_model_id

    return split_model_id(model_id)[0]


def _instance_credentialed(instance_id: str, cache: dict[str, bool]) -> bool:
    from config.inference_providers import get_instance, instance_configured

    if instance_id not in cache:
        instance = get_instance(instance_id)
        cache[instance_id] = instance is not None and instance_configured(instance)
    return cache[instance_id]


def get_configured_models() -> list[str]:
    """Return the model ids that are enabled AND whose credentials appear
    configured.

    This is a config-presence check, not a live probe:

    - Vertex models need their family's ``vertex_project_id`` (Gemini:
      ``gemini_vertex``, Anthropic: ``anthropic``) and must not be in the
      admin's Vertex disabled set.
    - Instance models need their instance configured (an API key in the
      credential store for OpenRouter, a base URL for a self-hosted
      endpoint -- ``instance_configured()``) and their per-model
      ``enabled`` flag.

    Deprecated Vertex models are excluded regardless: they are still
    runnable (existing conversations/routines keep working) but must not
    be offered for new selection.

    This is the universe the health sweeps check (``run_startup_checks``);
    the picker goes through :func:`get_available_models`, which additionally
    drops models with a failing health verdict. Order matches
    :func:`list_model_specs`.
    """
    from config.server_config import load_server_config

    server_config = load_server_config()
    key_cache: dict[str, bool] = {}
    configured = []
    for spec in list_model_specs():
        if spec.deprecated or not spec.enabled:
            continue
        if spec.instance_id is None:
            section = VERTEX_FAMILIES[spec.provider]["section"]
            if server_config[section]["vertex_project_id"]:
                configured.append(spec.id)
        elif _instance_credentialed(spec.instance_id, key_cache):
            configured.append(spec.id)
    return configured


def get_available_models() -> list[str]:
    """Return the model ids that are configured AND not known-unhealthy.

    Starts from :func:`get_configured_models` and drops every model whose
    latest verdict in the model-health store (``chat/llm/health.py``) is a
    failure. A model that was never checked -- e.g. right after boot while
    the startup sweep is still running, or when ``data/model_health.json``
    was deleted -- stays included: only a recorded failing check hides a
    model, so an absent verdict can never empty the picker.

    Health only filters *selection* (the frontend model picker via
    GET /app/api/config); existing conversations and routines on an
    unhealthy model keep running and fail at the provider, same as before.
    Recovery is automatic: the startup sweep, admin per-model rechecks, and
    the post-credential-save recheck all update the store, and this function
    re-reads it on every call.
    """
    from chat.llm.health import get_model_health_store

    statuses = get_model_health_store().get_all()
    return [
        model_id
        for model_id in get_configured_models()
        # Only an explicit failing verdict hides a model; a missing or
        # malformed entry keeps it visible.
        if (statuses.get(model_id) or {}).get("ok") is not False
    ]


def public_model_catalog() -> list[dict]:
    """Model metadata for the frontend (``models`` on GET /app/api/config).

    Every known model incl. deprecated/disabled ones, so the UI can still
    label and size old conversations; selection is governed separately by
    ``available_models``. Each entry also carries the admin's Model
    Selection settings (``config/model_selection.py``): the private and
    public top-level menu slots + ``descriptor`` and the ``allow_private``
    / ``allow_public`` usage flags the composer filters its menu by. While
    public mode is off the flags are reported as allowed (the stored
    values are masked, not changed) and ``public_slot`` as null.
    """
    from config.model_selection import (
        public_mode_enabled,
        read_model_selection,
        selection_for,
    )

    selection = read_model_selection()
    public_mode = public_mode_enabled()
    catalog = []
    for spec in list_model_specs():
        entry = selection_for(spec.id, selection)
        catalog.append({
            "id": spec.id,
            "display_name": spec.display_name,
            "provider": spec.provider,
            "provider_label": spec.provider_label,
            "max_input_tokens": spec.max_input_tokens,
            "deprecated": spec.deprecated,
            "slot": entry["slot"],
            "public_slot": entry["public_slot"] if public_mode else None,
            "descriptor": entry["descriptor"],
            "allow_private": entry["allow_private"] if public_mode else True,
            "allow_public": entry["allow_public"] if public_mode else True,
        })
    return catalog


# ---------------------------------------------------------------------------
# Provider singletons
# ---------------------------------------------------------------------------

_provider_instances: dict[tuple[str, str | None], Any] = {}


def get_provider_instance(provider_name: str, instance_id: str | None = None) -> "LLMProvider":
    """Return the LLMProvider singleton for ``(provider_name, instance_id)``.

    Vertex providers (``gemini``, ``anthropic``) have a single instance and
    ignore ``instance_id``. Instance-backed providers (``openrouter``) get
    one object per configured instance, each reading its own key/endpoint;
    ``instance_id=None`` selects the legacy ``openrouter`` instance so
    callers that only know the provider name keep working. A self-hosted
    instance whose API type is ``ollama`` gets the native-transport
    ``OllamaProvider`` subclass instead of the openai-SDK
    ``OpenRouterProvider`` (the admin endpoint drops the cached object when
    the API type changes). Lazily created.

    Raises:
        ValueError: If the provider name is unknown.
    """
    from config.inference_providers import LEGACY_OPENROUTER_INSTANCE_ID, get_instance

    if provider_name in ("gemini", "anthropic"):
        instance_id = None
    elif provider_name == "openrouter" and instance_id is None:
        instance_id = LEGACY_OPENROUTER_INSTANCE_ID

    key = (provider_name, instance_id)
    if key in _provider_instances:
        return _provider_instances[key]

    if provider_name == "gemini":
        from chat.llm.gemini_provider import GeminiProvider
        instance = GeminiProvider()
    elif provider_name == "anthropic":
        from chat.llm.anthropic_provider import AnthropicProvider
        instance = AnthropicProvider()
    elif provider_name == "openrouter":
        configured = get_instance(instance_id)
        if configured is not None and configured.get("api_type") == "ollama":
            from chat.llm.ollama_provider import OllamaProvider
            instance = OllamaProvider(instance_id)
        else:
            from chat.llm.openrouter_provider import OpenRouterProvider
            instance = OpenRouterProvider(instance_id)
    else:
        raise ValueError(
            f"Unknown provider: {provider_name}. "
            "Valid providers: gemini, anthropic, openrouter"
        )
    _provider_instances[key] = instance
    return instance


def drop_provider_instance(provider_name: str, instance_id: str | None) -> None:
    """Forget a cached provider object (after its instance is deleted)."""
    _provider_instances.pop((provider_name, instance_id), None)


def reset_provider_client_caches() -> None:
    """Drop cached SDK clients on providers that support it.

    Called when inference-provider credentials change through the admin UI so
    the new credentials take effect without a server restart. Providers opt in
    by implementing ``reset_cached_clients()``.
    """
    for instance in _provider_instances.values():
        reset = getattr(instance, "reset_cached_clients", None)
        if callable(reset):
            reset()


def get_provider_for_model_instance(model_id: str) -> "LLMProvider":
    """Return the provider object that serves ``model_id``.

    Combines :func:`resolve_model` and :func:`get_provider_instance`.

    Raises:
        ValueError: If the model id is unknown.
    """
    spec = resolve_model(model_id)
    if spec is None:
        raise ValueError(f"Unknown model: {model_id}")
    return get_provider_instance(spec.provider, spec.instance_id)
