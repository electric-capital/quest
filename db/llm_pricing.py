"""Static USD price table + per-call cost estimation for LLM API calls.

Read-side companion to ``db/llm_call_store.py``: the raw ``llm_calls_*``
tables store provider-native token counts verbatim, and this module turns
those counts into estimated dollars. Keeping the table here (not in
``chat/llm/``) keeps the db layer free of provider-SDK imports.

Rates are public list prices per 1M tokens (checked 2026-07-30) and are an
ESTIMATE, not billing truth:

- Batch discounts, negotiated/committed-use pricing, and Vertex vs direct-API
  differences are not modeled (Vertex list prices match the providers' own).
- Claude Sonnet 5's launch $2/$10 promo became its standard list rate
  (the scheduled 2026-09-01 increase to $3/$15 was cancelled; checked
  2026-09-28). Sonnet 5.5 launched at the same $2/$10.
- Gemini 3.6/3.7/3.8 Flash have an introductory $0.75/$3.75 promo through
  2026-12-31; the table carries the standard $1.50/$7.50 list rate, so
  estimates run high until the promo ends.
- Gemini explicit-cache storage ($/1M-token-hours) is not modeled -- only
  per-call token rates.

Tier pricing: models with a ``long_context`` block bill at higher rates when
a single call's context exceeds ``LONG_CONTEXT_THRESHOLD`` tokens (Gemini:
``prompt_token_count``; Anthropic: input + cache_read + cache_creation).
Every tier-priced model today keys on the same 200K threshold; a future
model with a different threshold needs a per-entry override AND a matching
change to the grouped read query in ``db/llm_call_store.py``.
"""

from typing import Optional

# Per-call context size above which a model's ``long_context`` rates apply.
LONG_CONTEXT_THRESHOLD = 200_000

# Gemini rates per 1M tokens. ``cache_read`` is the rate for
# ``cached_content_token_count`` (which prompt_token_count includes);
# thoughts bill at the output rate, tool_use_prompt at the input rate.
_GEMINI_PRICING: dict[str, dict] = {
    "gemini-3.1-pro-preview": {
        "input": 2.00, "output": 12.00, "cache_read": 0.20,
        "long_context": {"input": 4.00, "output": 18.00, "cache_read": 0.40},
    },
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05},
    "gemini-3.1-flash-lite-preview": {"input": 0.25, "output": 1.50, "cache_read": 0.025},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "cache_read": 0.15},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-3.6-flash": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
    "gemini-3.7-flash": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
    "gemini-3.8-flash": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
}

# Anthropic rates per 1M tokens. Cache rates derive from the input rate via
# the standard multipliers below (read 0.1x; write 1.25x for 5m TTL, 2x for
# 1h TTL). An entry may override the read multiplier with ``cache_read_mult``
# (Opus 5.5 reads cache at 0.05x its input rate, checked 2026-09-22). No
# current Claude model carries a long-context premium.
_ANTHROPIC_PRICING: dict[str, dict] = {
    "claude-haiku-4.5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read_mult": 0.05},
}

_ANTHROPIC_CACHE_READ_MULT = 0.10
_ANTHROPIC_CACHE_WRITE_5M_MULT = 1.25
_ANTHROPIC_CACHE_WRITE_1H_MULT = 2.00

# OpenRouter rates per 1M tokens. Instance-served models are priced from
# the pricing snapshot the admin endpoint copied from the OpenRouter
# catalog when the model was added to its instance
# (``instance_model_pricing`` in config/inference_providers.py -- looked up
# by wire id, i.e. with the ``<instance>:`` qualifier stripped, so rows
# from every instance of the same model price alike). This static table
# is the fallback for models no configured instance lists any more
# (historical rows): the routed provider's list price as shown on
# openrouter.ai, checked 2026-08-21. ``input`` is the rate for uncached
# prompt tokens, ``cache_read`` for the ``cached_prompt_tokens`` subset;
# reasoning tokens bill at the output rate (they are already included in
# ``completion_tokens``). Models with neither simply report "no estimate"
# in the dashboards.
_OPENROUTER_PRICING: dict[str, dict] = {
    "deepseek/deepseek-v4-flash-0731": {
        "input": 0.08, "output": 0.18, "cache_read": 0.016,
    },
    "qwen/qwen3.8-27b": {
        "input": 0.45, "output": 3.20, "cache_read": 0.05,
    },
}


def _openrouter_entry(model: str) -> Optional[dict]:
    """Pricing entry for an OpenRouter row: instance snapshot, then the
    static table. ``model`` may be qualified (``openrouter:vendor/x``) or a
    bare legacy id."""
    from config.inference_providers import instance_model_pricing, split_model_id

    wire_id = split_model_id(model)[1]
    snapshot = instance_model_pricing(wire_id)
    if snapshot is not None:
        entry = {"input": snapshot["prompt"], "output": snapshot["completion"]}
        if "cache_read" in snapshot:
            entry["cache_read"] = snapshot["cache_read"]
        return entry
    return _OPENROUTER_PRICING.get(wire_id)


def _rates_for(entry: dict, long_context: bool) -> dict:
    if long_context and "long_context" in entry:
        return entry["long_context"]
    return entry


def estimate_cost_usd(
    provider: str,
    model: str,
    metrics: dict,
    long_context: bool = False,
) -> Optional[float]:
    """Estimate the USD cost of provider-native token counts.

    ``metrics`` carries the provider's native usage fields (missing/None
    fields count as 0). The math is linear in every field, so it is valid
    for a single call AND for sums of calls -- as long as all summed calls
    share the same pricing tier, which is why the read query groups by the
    per-call long-context flag.

    Args:
        provider: "gemini", "anthropic", or "openrouter".
        model: Model id as recorded in the llm_calls_* rows (registry id).
        metrics: Native usage fields for one call or a same-tier sum.
        long_context: Whether these calls exceeded LONG_CONTEXT_THRESHOLD.

    Returns:
        Estimated USD cost, or None when the model has no pricing entry
        (unknown/legacy models -- callers should surface "no estimate", not 0).
    """
    def tokens(field: str) -> int:
        return metrics.get(field) or 0

    if provider == "gemini":
        entry = _GEMINI_PRICING.get(model)
        if entry is None:
            return None
        rates = _rates_for(entry, long_context)
        cached = tokens("cached_content_token_count")
        # prompt_token_count includes the cached portion; bill the remainder
        # (and tool-use prompt tokens) at the input rate.
        uncached_input = max(tokens("prompt_token_count") - cached, 0)
        cost = (
            (uncached_input + tokens("tool_use_prompt_token_count")) * rates["input"]
            + cached * rates["cache_read"]
            + (tokens("candidates_token_count") + tokens("thoughts_token_count"))
            * rates["output"]
        )
        return cost / 1_000_000

    if provider == "anthropic":
        entry = _ANTHROPIC_PRICING.get(model)
        if entry is None:
            return None
        rates = _rates_for(entry, long_context)
        creation = tokens("cache_creation_input_tokens")
        creation_5m = tokens("cache_creation_5m_input_tokens")
        creation_1h = tokens("cache_creation_1h_input_tokens")
        # Rows without the TTL split (older SDKs, degraded streams) leave the
        # 5m/1h columns NULL while cache_creation is populated; bill that
        # unsplit remainder at the cheaper 5m rate (5m is the default TTL).
        unsplit = max(creation - creation_5m - creation_1h, 0)
        in_rate = rates["input"]
        read_mult = entry.get("cache_read_mult", _ANTHROPIC_CACHE_READ_MULT)
        cost = (
            tokens("input_tokens") * in_rate
            + tokens("cache_read_input_tokens") * in_rate * read_mult
            + (creation_5m + unsplit) * in_rate * _ANTHROPIC_CACHE_WRITE_5M_MULT
            + creation_1h * in_rate * _ANTHROPIC_CACHE_WRITE_1H_MULT
            + tokens("output_tokens") * rates["output"]
        )
        return cost / 1_000_000

    if provider == "openrouter":
        entry = _openrouter_entry(model)
        if entry is None:
            return None
        rates = _rates_for(entry, long_context)
        cached = tokens("cached_prompt_tokens")
        # prompt_tokens includes the cached portion; bill the remainder at
        # the input rate. completion_tokens already includes reasoning.
        uncached_input = max(tokens("prompt_tokens") - cached, 0)
        cost = (
            uncached_input * rates["input"]
            + cached * rates.get("cache_read", rates["input"])
            + tokens("completion_tokens") * rates["output"]
        )
        return cost / 1_000_000

    return None
