"""Tests for the static price table in db/llm_pricing.py."""

import pytest

from chat.llm.config import MODEL_REGISTRY
from db.llm_pricing import (
    _ANTHROPIC_PRICING,
    _GEMINI_PRICING,
    _OPENROUTER_PRICING,
    estimate_cost_usd,
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
