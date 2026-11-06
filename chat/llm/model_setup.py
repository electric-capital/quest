"""Per-kind catalog dispatch and the first-run model suggestions.

Two admin paths need "the model list of this instance": the Settings >
Inference Providers typeahead and metadata snapshots
(``chat/routes/admin.py``), and the first-run **quick setup** behind the
composer's model setup card (``POST /admin/inference-providers/quick-setup``),
which connects a provider from one API key (or server URL) and picks the
models to add for it so the deployment can chat right away. Both dispatch
on the kind's ``catalog`` source (``INSTANCE_KINDS`` in
config/inference_providers.py) through :func:`fetch_instance_catalog`.

:func:`suggested_models` is the quick setup's pick: the models Quest has
real metadata for -- the registry-matched Claude / Gemini models, the
known OpenAI families and NEAR AI's curated open-weight pair (each catalog
module's ``suggested_wire_ids``), the curated legacy ids for the other
OpenRouter-shaped routers, the statically priced serverless models for
Fireworks, and every model a self-hosted server reports. A kind with nothing to suggest leaves
the instance connected but empty, and the card sends the admin to Settings
to pick models.
"""

import asyncio

from config.inference_providers import INSTANCE_KINDS, effective_api_key

# Catalog sources read with the instance's own key, all exposing
# ``get_catalog(api_key, refresh=False)`` and (except Fireworks) a
# ``suggested_wire_ids(models)`` for the quick setup.
KEY_FETCHED_SOURCES = ("fireworks", "anthropic", "gemini", "openai")
# Public, keyless lists -- ``get_catalog(refresh=False)``; the NEAR AI one
# also exposes ``suggested_wire_ids(models)``.
PUBLIC_SOURCES = ("nearai", "trustedrouter")
# Sources whose catalog module picks the quick setup's models itself.
_CURATED_SOURCES = ("anthropic", "gemini", "openai", "nearai")

# Suggestions for the remaining OpenRouter-shaped routers (OpenRouter,
# TrustedRouter): the curated ids the legacy OpenRouter configuration
# served, when the router lists them.
_ROUTER_SUGGESTIONS_LIMIT = 4


def catalog_module(source: str):
    """The catalog module for a kind's ``catalog`` source (``server`` has
    none: self-hosted discovery is live, see chat/llm/local_catalog.py)."""
    if source == "fireworks":
        from chat.llm import fireworks_catalog as module
    elif source == "anthropic":
        from chat.llm import anthropic_catalog as module
    elif source == "gemini":
        from chat.llm import gemini_catalog as module
    elif source == "openai":
        from chat.llm import openai_catalog as module
    elif source == "nearai":
        from chat.llm import nearai_catalog as module
    elif source == "trustedrouter":
        from chat.llm import trustedrouter_catalog as module
    elif source == "openrouter":
        from chat.llm import openrouter_catalog as module
    else:
        raise ValueError(f"No catalog module for source {source!r}")
    return module


async def fetch_instance_catalog(instance: dict, refresh: bool = False) -> dict:
    """``{"models", "error"}`` for an instance, from wherever its kind's
    typeahead looks: the shared OpenRouter list, a public router list, the
    vendor list fetched with the instance's key (``error`` = "No API key
    saved yet." before one is stored) or the self-hosted server itself
    (``error`` set when it cannot be reached or has no URL yet).

    Cached catalogs are served from their cache unless ``refresh``; the
    self-hosted discovery is always live.
    """
    source = INSTANCE_KINDS[instance["kind"]]["catalog"]
    if source == "server":
        from chat.llm.local_catalog import discover_models

        if not instance.get("base_url"):
            return {"models": [], "error": "No server URL configured yet."}
        result = await discover_models(instance)
        return {"models": result["models"], "error": result["error"]}
    module = catalog_module(source)
    if source in KEY_FETCHED_SOURCES:
        api_key = effective_api_key(instance["id"])[0]
        if not api_key:
            return {"models": [], "error": "No API key saved yet."}
        catalog = await asyncio.to_thread(module.get_catalog, api_key, refresh)
    else:
        catalog = await asyncio.to_thread(module.get_catalog, refresh)
    return {"models": catalog["models"], "error": catalog["error"]}


def suggested_models(instance: dict, models: list[dict]) -> list[str]:
    """The wire ids the quick setup adds to a freshly connected instance
    (see the module docstring); ``[]`` when the kind has no basis for a
    pick or ``models`` is empty."""
    if not models:
        return []
    source = INSTANCE_KINDS[instance["kind"]]["catalog"]
    if source == "server":
        return [m["id"] for m in models]
    if source in _CURATED_SOURCES:
        return catalog_module(source).suggested_wire_ids(models)
    listed = {m["id"]: m for m in models}
    if source == "fireworks":
        from db.llm_pricing import fireworks_priced_wire_ids

        picked = [
            wire_id for wire_id in fireworks_priced_wire_ids()
            if wire_id in listed and "tools" in (listed[wire_id].get("capabilities") or [])
        ]
        return picked[:_ROUTER_SUGGESTIONS_LIMIT]
    from config.inference_providers import _LEGACY_OPENROUTER_MODELS

    picked = [m["id"] for m in _LEGACY_OPENROUTER_MODELS if m["id"] in listed]
    return picked[:_ROUTER_SUGGESTIONS_LIMIT]


class QuickSetupError(ValueError):
    """A quick-setup request that cannot be carried out; ``code`` is the
    API error code (``unknown_kind`` / ``invalid_params``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def quick_setup_provider(
    kind: str, *, api_key: str = "", base_url: str = "", api_type: str = "",
) -> dict:
    """Connect a provider from one credential and make its models usable.

    Shared by the composer's model setup card
    (``POST /admin/inference-providers/quick-setup``) and the first-run setup
    screen (``POST /app/api/setup/inference/provider``). In one step: an
    instance of ``kind`` is created -- or an existing instance of the kind
    that serves no available model yet is reused, so a retry after a
    rejected key never piles up empty instances -- the API key (or server
    URL + API type) is stored, the kind's catalog is fetched fresh with it,
    and the quick-setup suggestions (:func:`suggested_models`) are added.
    The first suggestion is added alone and health-checked live first; only
    when it passes are the rest added (rechecked in the background), so a
    bad key leaves the instance connected but empty rather than flashing
    unchecked models into the picker. Unlike models added from Settings,
    these start **allowed in private conversations**: the admin is
    connecting the deployment's first model for exactly that.

    Returns ``{instance, added, check, catalog_error}``: the stored instance
    dict, the qualified ids added, the first model's health verdict (None
    when the kind had nothing to suggest) and the catalog fetch error, if
    any. Raises :class:`QuickSetupError` for an unknown kind or a missing
    credential / server URL.
    """
    from chat.llm.config import (
        drop_provider_instance,
        get_available_models,
        get_configured_models,
        reset_provider_client_caches,
    )
    from chat.llm.health import get_model_health_store, schedule_model_rechecks
    from chat.llm.openrouter_catalog import catalog_snapshot
    from config.inference_providers import (
        DEFAULT_LOCAL_API_TYPE,
        LOCAL_API_TYPES,
        list_instances,
        new_instance_id,
        normalize_base_url,
        qualify_model_id,
        read_inference_credentials,
        upsert_instance,
        write_inference_credentials,
    )

    # Resolve the request's kind to the registry's own key object: the
    # instance id (and so the credential file name) derives from the kind
    # name, and taking it from INSTANCE_KINDS rather than the request keeps
    # request data out of the path.
    kind_name = next((name for name in INSTANCE_KINDS if name == kind), None)
    if kind_name is None:
        raise QuickSetupError("unknown_kind", f"Unknown inference provider kind: {kind}")
    spec = INSTANCE_KINDS[kind_name]
    api_key = (api_key or "").strip()
    base_url = normalize_base_url(base_url) if base_url else None
    api_type = api_type or DEFAULT_LOCAL_API_TYPE
    if spec["endpoint"]:
        if base_url is None:
            raise QuickSetupError("invalid_params", "base_url must be an http:// or https:// server URL.")
        if api_type not in LOCAL_API_TYPES:
            raise QuickSetupError(
                "invalid_params", f"Unknown api_type: {api_type} (one of {sorted(LOCAL_API_TYPES)}).",
            )
    elif not api_key:
        raise QuickSetupError("invalid_params", f"An API key is required for {spec['label']}.")

    available = set(get_available_models())
    instance = next(
        (
            inst for inst in list_instances()
            if inst["kind"] == kind_name
            and not any(m.startswith(f"{inst['id']}:") for m in available)
        ),
        None,
    )
    if instance is None:
        instance = {"id": new_instance_id(kind_name), "kind": kind_name, "label": "", "models": []}
    if spec["endpoint"]:
        instance["base_url"] = base_url
        instance["api_type"] = api_type
    instance = upsert_instance(instance)
    if api_key:
        stored = dict(read_inference_credentials(instance["id"]) or {})
        stored["api_key"] = api_key
        write_inference_credentials(instance["id"], stored)
    reset_provider_client_caches()
    drop_provider_instance(spec["provider"], instance["id"])

    catalog = await fetch_instance_catalog(instance, refresh=True)
    existing = {m["id"] for m in instance["models"]}
    suggested = [w for w in suggested_models(instance, catalog["models"]) if w not in existing]
    added: list[str] = []
    check = None

    def _entry(wire_id: str) -> dict:
        return {"id": wire_id, "enabled": True, **(catalog_snapshot(catalog["models"], wire_id) or {})}

    if suggested:
        probe = suggested[0]
        instance["models"].append(_entry(probe))
        instance = upsert_instance(instance)
        probe_id = qualify_model_id(instance["id"], probe)
        check = await get_model_health_store().run_check(probe_id)
        if check["ok"]:
            added.append(probe_id)
            for wire_id in suggested[1:]:
                instance["models"].append(_entry(wire_id))
                added.append(qualify_model_id(instance["id"], wire_id))
            instance = upsert_instance(instance)
            prefix = f"{instance['id']}:"
            schedule_model_rechecks([
                m for m in get_configured_models() if m.startswith(prefix) and m != probe_id
            ])
        else:
            instance["models"] = [m for m in instance["models"] if m["id"] != probe]
            instance = upsert_instance(instance)
            await get_model_health_store().forget([probe_id])
    elif instance["models"]:
        # Nothing new to suggest but the reused instance already lists
        # models: recheck them under the new credential.
        prefix = f"{instance['id']}:"
        schedule_model_rechecks([m for m in get_configured_models() if m.startswith(prefix)])

    return {
        "instance": instance,
        "added": added,
        "check": check,
        "catalog_error": catalog["error"],
    }


def provider_kind_views() -> tuple[list[dict], list[dict]]:
    """``(kinds, api_types)`` as the provider-picking forms want them: the
    composer's model setup card (via ``GET /admin/inference-providers``) and
    the first-run setup screen. ``catalog`` tells a card where its "Add
    model" typeahead looks; ``hint`` / ``key_required`` shape the key field."""
    from config.inference_providers import LOCAL_API_TYPES

    kinds = [
        {
            "kind": kind,
            "label": spec["label"],
            "endpoint": spec["endpoint"],
            "catalog": spec["catalog"],
            "hint": spec["hint"],
            "key_required": spec["key_required"],
        }
        for kind, spec in INSTANCE_KINDS.items()
    ]
    api_types = [
        {"id": api_type, "label": spec["label"], "description": spec["description"]}
        for api_type, spec in LOCAL_API_TYPES.items()
    ]
    return kinds, api_types
