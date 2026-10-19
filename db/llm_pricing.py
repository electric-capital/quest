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

Price changes: a table entry is either ONE rate set (the dict shape above,
in force for all time) or a LIST of rate sets ordered by ``effective_from``
(a ``YYYY-MM-DD`` date, UTC midnight; the first set may omit it and covers
everything earlier). When a provider changes a price, never edit a rate in
place -- append a rate set carrying the date the provider announced, so
calls made before it keep the rate that was actually charged::

    "vendor/model": [
        {"input": 1.00, "output": 4.00},                                 # until 2026-11-30
        {"effective_from": "2026-12-01", "input": 1.50, "output": 6.00},  # from then on
    ],

:func:`estimate_cost_usd` picks the rate set in force at ``at`` (the
call's ``created_at``; ``None`` = today's rates), and the grouped read
query groups rows by pricing period via :func:`pricing_period_starts` so
every bucket it prices is single-rate. ``long_context_threshold`` and the
``long_context`` tier belong to the rate set, but the threshold itself
must be the same in every set of a model (the tier flag is computed per
model, not per period) -- :func:`long_context_threshold` reads the first
set. Instance pricing snapshots keep their retired rates the same way
(``pricing_history`` in config/inference_providers.py).
"""

from datetime import date, datetime, timezone
from typing import Optional

# A ``YYYY-MM-DD`` effective_from parses to this time of day: UTC midnight,
# comparable with the naive-UTC ``created_at`` of the llm_calls_* rows.
_BASE_PERIOD = datetime.min

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


def _naive_utc(moment: datetime) -> datetime:
    """``moment`` as naive UTC (the llm_calls_* ``created_at`` convention)."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def utc_now() -> datetime:
    """Current time as naive UTC, comparable with ``effective_from`` dates
    and the ``until`` stamps of retired instance snapshots."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rate_sets(entry) -> list[dict]:
    """An entry's rate sets, oldest first (a single dict is one open-ended set)."""
    return list(entry) if isinstance(entry, list) else [entry]


def effective_from(rate_set: dict) -> Optional[datetime]:
    """When ``rate_set`` starts applying (naive UTC), None for an open-ended first set."""
    raw = rate_set.get("effective_from")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _naive_utc(raw)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    return _naive_utc(datetime.fromisoformat(raw))


def _period_starts(entry) -> list[datetime]:
    """The dated boundaries of a multi-period entry, ascending (empty for one set)."""
    return sorted(
        start for start in (effective_from(rate_set) for rate_set in _rate_sets(entry)[1:])
        if start is not None
    )


def rate_set_at(entry, at: Optional[datetime]) -> dict:
    """The rate set of ``entry`` in force at ``at`` (None = now).

    The latest set whose ``effective_from`` is at or before ``at``; the
    first set when none is (``datetime.min`` therefore always selects it).
    """
    sets = _rate_sets(entry)
    moment = utc_now() if at is None else _naive_utc(at)
    chosen = sets[0]
    for rate_set in sets[1:]:
        start = effective_from(rate_set)
        if start is not None and start <= moment:
            chosen = rate_set
    return chosen


def _openrouter_entry(model: str, at: Optional[datetime] = None) -> Optional[dict]:
    """Rate set in force at ``at`` for a row of the shared
    ``llm_calls_openrouter`` table (every OpenAI-compatible instance kind):
    the instance snapshot (or the retired snapshot that covered ``at``),
    then the static OpenRouter table, then the static Fireworks table --
    the wire-id namespaces (``vendor/model`` vs ``accounts/fireworks/...``)
    never collide. ``model`` may be qualified (``openrouter:vendor/x``,
    ``fireworks:accounts/fireworks/models/x``) or a bare legacy id."""
    from config.inference_providers import instance_model_pricing, split_model_id

    wire_id = split_model_id(model)[1]
    snapshot = instance_model_pricing(wire_id, at=at)
    if snapshot is not None:
        entry = {"input": snapshot["prompt"], "output": snapshot["completion"]}
        if "cache_read" in snapshot:
            entry["cache_read"] = snapshot["cache_read"]
        return entry
    static = _OPENROUTER_PRICING.get(wire_id) or _FIREWORKS_PRICING.get(wire_id)
    return rate_set_at(static, at) if static is not None else None


_STATIC_PRICING: dict[str, dict[str, dict]] = {
    "gemini": _GEMINI_PRICING,
    "anthropic": _ANTHROPIC_PRICING,
}

# Cache-read rate assumed for a Gemini API instance model whose pricing
# snapshot has no ``cache_read`` (the Gemini list rates read cache at a
# tenth of the input rate).
_GEMINI_DEFAULT_CACHE_READ_MULT = 0.10


def _vendor_registry_entry(provider: str, wire_id: str) -> Optional[dict]:
    """The static-table entry of the Vertex registry model a direct-API
    wire id names (``claude-sonnet-5-20260801`` -> ``claude-sonnet-5``)."""
    from chat.llm.config import registry_entry_for_wire_id

    match = registry_entry_for_wire_id(provider, wire_id)
    if match is None:
        return None
    return _STATIC_PRICING.get(provider, {}).get(match[0])


def fireworks_priced_wire_ids() -> list[str]:
    """The Fireworks wire ids the static table prices, in table order (the
    first-run quick setup offers these for a new Fireworks instance)."""
    return list(_FIREWORKS_PRICING)


def static_pricing_snapshot(provider: str, registry_id: str) -> Optional[dict]:
    """Today's list rates of a Vertex registry model as an instance pricing
    snapshot (``{prompt, completion, cache_read}`` $/1M tokens), or None.

    The direct-API catalogs (``chat/llm/anthropic_catalog.py``,
    ``chat/llm/gemini_catalog.py``) stamp this on the models an
    ``anthropic`` / ``gemini`` instance adds, so their rows are priced like
    the Vertex rows of the same model. Anthropic's cache-read rate is the
    input rate times the entry's read multiplier; Gemini publishes it.
    """
    entry = _STATIC_PRICING.get(provider, {}).get(registry_id)
    if entry is None:
        return None
    rates = rate_set_at(entry, None)
    snapshot = {"prompt": float(rates["input"]), "completion": float(rates["output"])}
    if provider == "anthropic":
        snapshot["cache_read"] = rates["input"] * rates.get("cache_read_mult", _ANTHROPIC_CACHE_READ_MULT)
    elif rates.get("cache_read") is not None:
        snapshot["cache_read"] = float(rates["cache_read"])
    return snapshot


def _vendor_instance_entry(provider: str, model: str, at: Optional[datetime]) -> Optional[dict]:
    """Rate set in force at ``at`` for a Gemini / Anthropic row whose model
    is an ``<instance>:<wire_id>`` id of a direct-API instance kind: the
    instance's pricing snapshot (current or the retired one covering ``at``,
    ``instance_model_pricing``), else the static table entry of the registry
    model the wire id names. None for Vertex (bare) ids and unknown models.

    A snapshot carries ``{prompt, completion, cache_read?}``; it is turned
    into the provider's native rate-set shape -- Anthropic bills cache reads
    through a multiplier on the input rate, Gemini through a flat rate.
    """
    from config.inference_providers import instance_model_pricing, split_model_id

    instance_id, wire_id = split_model_id(model)
    if instance_id is None:
        return None
    snapshot = instance_model_pricing(wire_id, at=at)
    if snapshot is None:
        static = _vendor_registry_entry(provider, wire_id)
        return rate_set_at(static, at) if static is not None else None
    entry = {"input": snapshot["prompt"], "output": snapshot["completion"]}
    cache_read = snapshot.get("cache_read")
    if provider == "anthropic":
        if cache_read is not None and snapshot["prompt"] > 0:
            entry["cache_read_mult"] = cache_read / snapshot["prompt"]
    else:
        entry["cache_read"] = (
            cache_read if cache_read is not None
            else snapshot["prompt"] * _GEMINI_DEFAULT_CACHE_READ_MULT
        )
    return entry


def long_context_threshold(provider: str, model: str) -> int:
    """Per-call context size above which ``model``'s long-context rates apply."""
    entry = _STATIC_PRICING.get(provider, {}).get(model)
    if entry is None and provider in _STATIC_PRICING:
        from config.inference_providers import split_model_id

        instance_id, wire_id = split_model_id(model)
        if instance_id is not None:
            entry = _vendor_registry_entry(provider, wire_id)
    if entry is None:
        return LONG_CONTEXT_THRESHOLD
    return _rate_sets(entry)[0].get("long_context_threshold", LONG_CONTEXT_THRESHOLD)


def long_context_threshold_overrides(provider: str) -> dict[str, int]:
    """Models of ``provider`` whose threshold differs from the default."""
    overrides = {}
    for model, entry in _STATIC_PRICING.get(provider, {}).items():
        first = _rate_sets(entry)[0]
        if "long_context_threshold" in first:
            overrides[model] = first["long_context_threshold"]
    return overrides


def pricing_period_starts(provider: str) -> dict[str, list[datetime]]:
    """Models of ``provider`` whose rates changed over time, each with the
    ascending naive-UTC moments a new rate set took effect.

    For ``"openrouter"`` the keys are wire ids (the ``llm_calls_openrouter``
    rows carry ``<instance>:<wire_id>`` or a bare legacy id) and the
    moments come from the static tables plus the retired instance pricing
    snapshots (``instance_pricing_period_starts()``). The grouped read
    query turns this into a per-row pricing-period column so no bucket it
    prices spans a rate change.
    """
    if provider == "openrouter":
        from config.inference_providers import instance_pricing_period_starts

        starts: dict[str, list[datetime]] = {}
        for table in (_OPENROUTER_PRICING, _FIREWORKS_PRICING):
            for wire_id, entry in table.items():
                if _period_starts(entry):
                    starts[wire_id] = _period_starts(entry)
        for wire_id, moments in instance_pricing_period_starts().items():
            starts[wire_id] = sorted(set(starts.get(wire_id, [])) | set(moments))
        return starts
    return {
        model: _period_starts(entry)
        for model, entry in _STATIC_PRICING.get(provider, {}).items()
        if _period_starts(entry)
    }


def _rates_for(rate_set: dict, long_context: bool) -> dict:
    if long_context and "long_context" in rate_set:
        return rate_set["long_context"]
    return rate_set


def estimate_cost_usd(
    provider: str,
    model: str,
    metrics: dict,
    long_context: bool = False,
    at: Optional[datetime] = None,
) -> Optional[float]:
    """Estimate the USD cost of provider-native token counts.

    ``metrics`` carries the provider's native usage fields (missing/None
    fields count as 0). The math is linear in every field, so it is valid
    for a single call AND for sums of calls -- as long as all summed calls
    share the same pricing tier AND the same pricing period, which is why
    the read query groups by the per-call long-context flag and by
    pricing period.

    Args:
        provider: "gemini", "anthropic", or "openrouter".
        model: Model id as recorded in the llm_calls_* rows: a registry id
            for Vertex rows, a qualified ``<instance>:<wire_id>`` for rows
            of an instance (incl. the direct-API ``anthropic`` / ``gemini``
            kinds, priced from their snapshot or the registry model they
            name -- see :func:`_vendor_instance_entry`).
        metrics: Native usage fields for one call or a same-tier,
            same-period sum.
        long_context: Whether these calls exceeded the model's
            :func:`long_context_threshold`.
        at: When the calls were made (the rows' naive-UTC ``created_at``);
            picks the rate set in force then. None = today's rates.

    Returns:
        Estimated USD cost, or None when the model has no pricing entry
        (unknown/legacy models -- callers should surface "no estimate", not 0).
    """
    def tokens(field: str) -> int:
        return metrics.get(field) or 0

    if provider == "gemini":
        entry = _GEMINI_PRICING.get(model)
        if entry is None:
            # A Gemini API instance model (``gemini:<wire_id>``)
            rate_set = _vendor_instance_entry(provider, model, at)
            if rate_set is None:
                return None
        else:
            rate_set = rate_set_at(entry, at)
        rates = _rates_for(rate_set, long_context)
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
            # A Claude API instance model (``anthropic:<wire_id>``)
            rate_set = _vendor_instance_entry(provider, model, at)
            if rate_set is None:
                return None
        else:
            rate_set = rate_set_at(entry, at)
        rates = _rates_for(rate_set, long_context)
        creation = tokens("cache_creation_input_tokens")
        creation_5m = tokens("cache_creation_5m_input_tokens")
        creation_1h = tokens("cache_creation_1h_input_tokens")
        # Rows without the TTL split (older SDKs, degraded streams) leave the
        # 5m/1h columns NULL while cache_creation is populated; bill that
        # unsplit remainder at the cheaper 5m rate (5m is the default TTL).
        unsplit = max(creation - creation_5m - creation_1h, 0)
        in_rate = rates["input"]
        read_mult = rate_set.get("cache_read_mult", _ANTHROPIC_CACHE_READ_MULT)
        cost = (
            tokens("input_tokens") * in_rate
            + tokens("cache_read_input_tokens") * in_rate * read_mult
            + (creation_5m + unsplit) * in_rate * _ANTHROPIC_CACHE_WRITE_5M_MULT
            + creation_1h * in_rate * _ANTHROPIC_CACHE_WRITE_1H_MULT
            + tokens("output_tokens") * rates["output"]
        )
        return cost / 1_000_000

    if provider == "openrouter":
        entry = _openrouter_entry(model, at)
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
