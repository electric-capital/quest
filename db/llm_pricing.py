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
- Claude Haiku 5.5 is priced by prompt length: $0.10/$0.50 up to 100K
  prompt tokens, $0.50/$2.50 above (checked 2026-10-07).
- Gemini 3.6/3.7/3.8 Flash have an introductory $0.75/$3.75 promo through
  2026-12-31; the table carries the standard $1.50/$7.50 list rate, so
  estimates run high until the promo ends.
- Gemini 4 Argon launched (2026-10-13) at an introductory $2/$10 (cached
  input $0.10) through 2026-11-30, applied by Google as 50% credits back
  rather than a lower rate; the table carries the $4/$20 (cached $0.20)
  list rate that starts 2026-12-01, so estimates run high until then. The
  global-endpoint rate is used; the us/eu multi-regions bill 10% more.
- Gemini explicit-cache storage ($/1M-token-hours) is not modeled -- only
  per-call token rates.
- Fireworks AI publishes no prices through its API, so its rows are priced
  from the hand-maintained ``_FIREWORKS_PRICING`` mirror of the public
  serverless price list (checked 2026-10-12), Standard serving path; the
  Priority path and 50%-off batch inference are not modeled.

Tier pricing: models with a ``long_context`` block bill at higher rates when
a single call's context exceeds the model's threshold (Gemini:
``prompt_token_count``; Anthropic: input + cache_read + cache_creation).
The threshold is ``LONG_CONTEXT_THRESHOLD`` unless the entry overrides it
with ``long_context_threshold`` (Claude Haiku 5.5: 100K). Resolve it with
:func:`long_context_threshold`; the grouped read query in
``db/llm_call_store.py`` builds its per-call tier flag from
:func:`long_context_threshold_overrides`.
"""

from typing import Optional

# Default per-call context size above which a model's ``long_context`` rates
# apply (an entry's ``long_context_threshold`` overrides it).
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
    # Global-endpoint list rate; no long-context tier (every input token
    # bills at the same rate). Thinking tokens bill at the output rate.
    "gemini-4-argon": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
}

# Anthropic rates per 1M tokens. Cache rates derive from the input rate via
# the standard multipliers below (read 0.1x; write 1.25x for 5m TTL, 2x for
# 1h TTL). An entry may override the read multiplier with ``cache_read_mult``
# (Opus 5.5 reads cache at 0.05x its input rate, checked 2026-09-22). The
# multipliers apply to the tier's input rate, so a ``long_context`` block
# only needs input/output.
_ANTHROPIC_PRICING: dict[str, dict] = {
    "claude-haiku-4.5": {"input": 1.00, "output": 5.00},
    "claude-haiku-5-5": {
        "input": 0.10, "output": 0.50,
        "long_context_threshold": 100_000,
        "long_context": {"input": 0.50, "output": 2.50},
    },
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

# Fireworks AI serverless rates per 1M tokens, keyed by the wire id a
# ``fireworks`` instance calls (``accounts/fireworks/models/<name>`` and the
# ``accounts/fireworks/routers/<name>-fast`` speed routers). Fireworks
# exposes no prices through its API (the catalog snapshots carry ``pricing:
# None``), so like the Vertex tables above this is a hand-maintained mirror
# of the public list: the "Standard" serving path rates on
# https://docs.fireworks.ai/serverless/pricing, cross-checked against each
# model's page on fireworks.ai/models/fireworks/<name> (checked 2026-10-12).
# The ``-fast`` routers bill at their own higher "Fast" path rates; the
# "Priority" path and the "(US)" variants are not selectable here and not
# modeled. Same token convention as OpenRouter: ``input`` is the rate for
# uncached prompt tokens, ``cache_read`` for the ``cached_prompt_tokens``
# subset of ``prompt_tokens``, reasoning tokens bill at the output rate
# inside ``completion_tokens``. Deliberately unpriced (no estimate):
# ``auto`` / ``firerouter/auto`` (the meta-routers pick a different model per
# request) and ids the pricing page does not list.
_FIREWORKS_PRICING: dict[str, dict] = {
    "accounts/fireworks/models/deepseek-v4p1-flash": {
        "input": 0.30, "output": 1.20, "cache_read": 0.006,
    },
    "accounts/fireworks/models/ember-1": {
        "input": 3.00, "output": 15.00, "cache_read": 0.30,
    },
    "accounts/fireworks/models/glm-5p2": {
        "input": 1.40, "output": 4.40, "cache_read": 0.14,
    },
    "accounts/fireworks/models/glm-5p3": {
        "input": 1.40, "output": 4.40, "cache_read": 0.26,
    },
    "accounts/fireworks/models/glm-5p3-flash": {
        "input": 0.15, "output": 0.50, "cache_read": 0.03,
    },
    "accounts/fireworks/models/gpt-oss-120b": {
        "input": 0.15, "output": 0.60, "cache_read": 0.015,
    },
    "accounts/fireworks/models/inkling": {
        "input": 1.00, "output": 4.05, "cache_read": 0.17,
    },
    "accounts/fireworks/models/kimi-k3": {
        "input": 3.00, "output": 15.00, "cache_read": 0.30,
    },
    "accounts/fireworks/models/minimax-m3": {
        "input": 0.30, "output": 1.20, "cache_read": 0.06,
    },
    "accounts/fireworks/models/nemotron-3-ultra-nvfp4": {
        "input": 0.60, "output": 2.40, "cache_read": 0.12,
    },
    "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b": {
        "input": 0.05, "output": 0.20, "cache_read": 0.01,
    },
    "accounts/fireworks/models/qwen3p8-max": {
        "input": 2.00, "output": 6.00, "cache_read": 0.25,
    },
    "accounts/fireworks/routers/glm-5p3-fast": {
        "input": 2.10, "output": 6.60, "cache_read": 0.39,
    },
    "accounts/fireworks/routers/kimi-k3-fast": {
        "input": 4.50, "output": 22.50, "cache_read": 0.45,
    },
}


def _openrouter_entry(model: str) -> Optional[dict]:
    """Pricing entry for a row of the shared ``llm_calls_openrouter`` table
    (every OpenAI-compatible instance kind): the instance snapshot, then the
    static OpenRouter table, then the static Fireworks table -- the wire-id
    namespaces (``vendor/model`` vs ``accounts/fireworks/...``) never
    collide. ``model`` may be qualified (``openrouter:vendor/x``,
    ``fireworks:accounts/fireworks/models/x``) or a bare legacy id."""
    from config.inference_providers import instance_model_pricing, split_model_id

    wire_id = split_model_id(model)[1]
    snapshot = instance_model_pricing(wire_id)
    if snapshot is not None:
        entry = {"input": snapshot["prompt"], "output": snapshot["completion"]}
        if "cache_read" in snapshot:
            entry["cache_read"] = snapshot["cache_read"]
        return entry
    return _OPENROUTER_PRICING.get(wire_id) or _FIREWORKS_PRICING.get(wire_id)


_STATIC_PRICING: dict[str, dict[str, dict]] = {
    "gemini": _GEMINI_PRICING,
    "anthropic": _ANTHROPIC_PRICING,
}


def long_context_threshold(provider: str, model: str) -> int:
    """Per-call context size above which ``model``'s long-context rates apply."""
    entry = _STATIC_PRICING.get(provider, {}).get(model) or {}
    return entry.get("long_context_threshold", LONG_CONTEXT_THRESHOLD)


def long_context_threshold_overrides(provider: str) -> dict[str, int]:
    """Models of ``provider`` whose threshold differs from the default."""
    return {
        model: entry["long_context_threshold"]
        for model, entry in _STATIC_PRICING.get(provider, {}).items()
        if "long_context_threshold" in entry
    }


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
        long_context: Whether these calls exceeded the model's
            :func:`long_context_threshold`.

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
