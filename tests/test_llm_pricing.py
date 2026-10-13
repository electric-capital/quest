"""Tests for the static price table in db/llm_pricing.py."""

import pytest

from chat.llm.config import MODEL_REGISTRY
from db.llm_pricing import (
    _ANTHROPIC_PRICING,
    _FIREWORKS_PRICING,
    _GEMINI_PRICING,
    _OPENROUTER_PRICING,
    LONG_CONTEXT_THRESHOLD,
    estimate_cost_usd,
    long_context_threshold,
    long_context_threshold_overrides,
)


def test_every_non_deprecated_registry_model_is_priced():
    """A registry model without a price row silently reports "no estimate"
    in every dashboard, so adding a model must add its rates too."""
    tables = {
        "anthropic": _ANTHROPIC_PRICING,
        "gemini": _GEMINI_PRICING,
        "openrouter": _OPENROUTER_PRICING,
    }
    missing = [
        model_id for model_id, entry in MODEL_REGISTRY.items()
        if not entry.get("deprecated") and model_id not in tables[entry["provider"]]
    ]
    assert missing == []


def test_gemini_4_argon_prices_at_the_standard_list_rate_with_no_long_context_tier():
    metrics = {
        "prompt_token_count": 1_000_000,
        "cached_content_token_count": 250_000,
        "candidates_token_count": 100_000,
        "thoughts_token_count": 50_000,
    }
    # 750K uncached input at $4 + 250K cached at $0.20 + 150K output/thinking at $20
    expected = 3.00 + 0.05 + 3.00
    assert estimate_cost_usd("gemini", "gemini-4-argon", metrics) == pytest.approx(expected)
    # Every input token bills at the same rate: no long-context tier.
    assert "long_context" not in _GEMINI_PRICING["gemini-4-argon"]
    assert estimate_cost_usd(
        "gemini", "gemini-4-argon", metrics, long_context=True,
    ) == pytest.approx(expected)


def test_anthropic_default_cache_read_is_ten_percent_of_input():
    cost = estimate_cost_usd(
        "anthropic", "claude-opus-5", {"cache_read_input_tokens": 1_000_000},
    )
    assert cost == pytest.approx(0.50)  # $5 input * 0.10


def test_opus_5_5_cache_read_override_is_five_percent_of_input():
    cost = estimate_cost_usd(
        "anthropic", "claude-opus-5-5", {"cache_read_input_tokens": 1_000_000},
    )
    assert cost == pytest.approx(0.20)  # $4 input * 0.05


def test_sonnet_5_5_prices_like_sonnet_5_with_standard_cache_rates():
    metrics = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation_5m_input_tokens": 1_000_000,
        "cache_creation_1h_input_tokens": 1_000_000,
    }
    # $2 input + $10 output + $0.20 read + $2.50 (5m write) + $4 (1h write)
    assert estimate_cost_usd("anthropic", "claude-sonnet-5-5", metrics) == pytest.approx(18.70)
    assert estimate_cost_usd("anthropic", "claude-sonnet-5", metrics) == pytest.approx(18.70)


_HAIKU_5_5_METRICS = {
    "input_tokens": 1_000_000,
    "output_tokens": 1_000_000,
    "cache_read_input_tokens": 1_000_000,
    "cache_creation_input_tokens": 2_000_000,
    "cache_creation_5m_input_tokens": 1_000_000,
    "cache_creation_1h_input_tokens": 1_000_000,
}


def test_haiku_5_5_short_prompt_rates():
    # $0.10 input + $0.50 output + $0.01 read + $0.125 (5m) + $0.20 (1h)
    assert estimate_cost_usd(
        "anthropic", "claude-haiku-5-5", _HAIKU_5_5_METRICS,
    ) == pytest.approx(0.935)


def test_haiku_5_5_long_prompt_rates():
    # $0.50 input + $2.50 output + $0.05 read + $0.625 (5m) + $1 (1h)
    assert estimate_cost_usd(
        "anthropic", "claude-haiku-5-5", _HAIKU_5_5_METRICS, long_context=True,
    ) == pytest.approx(4.675)


def test_haiku_5_5_switches_tier_above_100k_prompt_tokens():
    assert long_context_threshold("anthropic", "claude-haiku-5-5") == 100_000
    assert long_context_threshold_overrides("anthropic") == {
        "claude-haiku-5-5": 100_000,
    }
    # Everything else keeps the default threshold.
    assert long_context_threshold("anthropic", "claude-opus-5-5") == LONG_CONTEXT_THRESHOLD
    assert long_context_threshold("gemini", "gemini-3.1-pro-preview") == LONG_CONTEXT_THRESHOLD
    assert long_context_threshold("openrouter", "openrouter:x/y") == LONG_CONTEXT_THRESHOLD
    assert long_context_threshold_overrides("gemini") == {}


def test_opus_5_5_cache_writes_use_standard_multipliers():
    cost = estimate_cost_usd(
        "anthropic", "claude-opus-5-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_creation_input_tokens": 2_000_000,
            "cache_creation_5m_input_tokens": 1_000_000,
            "cache_creation_1h_input_tokens": 1_000_000,
        },
    )
    # $4 input + $20 output + $5 (5m write) + $8 (1h write)
    assert cost == pytest.approx(37.0)


_FIREWORKS_METRICS = {
    "prompt_tokens": 2_000_000,  # includes the 1M cached below
    "cached_prompt_tokens": 1_000_000,
    "completion_tokens": 1_000_000,
}


def test_fireworks_table_keys_are_fireworks_wire_ids_with_full_rates():
    for wire_id, entry in _FIREWORKS_PRICING.items():
        assert wire_id.startswith("accounts/fireworks/"), wire_id
        assert set(entry) == {"input", "output", "cache_read"}, wire_id
        assert entry["cache_read"] < entry["input"] < entry["output"], wire_id
    # The two namespaces must never share a key (the lookup chains them).
    assert not set(_FIREWORKS_PRICING) & set(_OPENROUTER_PRICING)


def test_fireworks_rows_price_from_the_static_table():
    # $3 x 1M uncached + $0.30 x 1M cached + $15 x 1M output
    assert estimate_cost_usd(
        "openrouter", "fireworks:accounts/fireworks/models/kimi-k3", _FIREWORKS_METRICS,
    ) == pytest.approx(18.30)
    # The speed router bills at its own "Fast" rates, not the base model's.
    assert estimate_cost_usd(
        "openrouter", "fireworks:accounts/fireworks/routers/kimi-k3-fast", _FIREWORKS_METRICS,
    ) == pytest.approx(27.45)
    # Unqualified (legacy) and differently-labelled instance ids resolve alike.
    assert estimate_cost_usd(
        "openrouter", "accounts/fireworks/models/kimi-k3", _FIREWORKS_METRICS,
    ) == pytest.approx(18.30)
    assert estimate_cost_usd(
        "openrouter", "fw2:accounts/fireworks/models/kimi-k3", _FIREWORKS_METRICS,
    ) == pytest.approx(18.30)


def test_fireworks_meta_routers_and_unlisted_ids_have_no_estimate():
    for model in ("fireworks:auto", "fireworks:firerouter/auto",
                  "fireworks:accounts/acme/deployments/abc123"):
        assert estimate_cost_usd("openrouter", model, _FIREWORKS_METRICS) is None


def test_instance_snapshot_pricing_wins_over_the_static_tables(monkeypatch):
    import config.inference_providers as ip

    monkeypatch.setattr(
        ip, "instance_model_pricing",
        lambda wire_id: {"prompt": 1.0, "completion": 2.0} if wire_id.endswith("kimi-k3") else None,
    )
    # $1 x 1M uncached + $1 x 1M cached (no cache_read -> input rate) + $2 x 1M
    assert estimate_cost_usd(
        "openrouter", "fireworks:accounts/fireworks/models/kimi-k3", _FIREWORKS_METRICS,
    ) == pytest.approx(4.0)
