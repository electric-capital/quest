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
real metadata for -- the registry-matched Claude / Gemini models and the
known OpenAI families for the direct-API kinds (each catalog module's
``suggested_wire_ids``), the curated legacy ids for the OpenRouter-shaped
routers, the statically priced serverless models for Fireworks, and every
model a self-hosted server reports. A kind with nothing to suggest leaves
the instance connected but empty, and the card sends the admin to Settings
to pick models.
"""

import asyncio

from config.inference_providers import INSTANCE_KINDS, effective_api_key

# Catalog sources read with the instance's own key, all exposing
# ``get_catalog(api_key, refresh=False)`` and (except Fireworks) a
# ``suggested_wire_ids(models)`` for the quick setup.
KEY_FETCHED_SOURCES = ("fireworks", "anthropic", "gemini", "openai")
# Public, keyless lists -- ``get_catalog(refresh=False)``.
PUBLIC_SOURCES = ("nearai", "trustedrouter")

# Suggestions for the OpenRouter-shaped routers (OpenRouter, NEAR AI,
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
    if source in ("anthropic", "gemini", "openai"):
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
