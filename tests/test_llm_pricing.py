"""Tests for the static price table in db/llm_pricing.py."""

from datetime import datetime, timezone

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
    pricing_period_starts,
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


def test_gemini_4_carbon_is_priced_like_argon():
    metrics = {
        "prompt_token_count": 1_000_000,
        "cached_content_token_count": 250_000,
        "candidates_token_count": 100_000,
        "thoughts_token_count": 50_000,
    }
    # 750K uncached input at $4 + 250K cached at $0.20 + 150K output/thinking at $20
    assert estimate_cost_usd("gemini", "gemini-4-carbon", metrics) == pytest.approx(6.05)
    assert _GEMINI_PRICING["gemini-4-carbon"] == _GEMINI_PRICING["gemini-4-argon"]


def test_gemini_4_hydrogen_prices_at_the_flash_rate_with_a_five_percent_cache_read():
    metrics = {
        "prompt_token_count": 1_000_000,
        "cached_content_token_count": 400_000,
        "candidates_token_count": 100_000,
        "thoughts_token_count": 100_000,
    }
    # 600K uncached input at $1.50 + 400K cached at $0.075 + 200K output/thinking at $7.50
    expected = 0.90 + 0.03 + 1.50
    assert estimate_cost_usd("gemini", "gemini-4-hydrogen", metrics) == pytest.approx(expected)
    # No long-context tier: a 1M-token prompt bills at the same rate.
    assert estimate_cost_usd(
        "gemini", "gemini-4-hydrogen", metrics, long_context=True,
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
        lambda wire_id, at=None: {"prompt": 1.0, "completion": 2.0} if wire_id.endswith("kimi-k3") else None,
    )
    # $1 x 1M uncached + $1 x 1M cached (no cache_read -> input rate) + $2 x 1M
    assert estimate_cost_usd(
        "openrouter", "fireworks:accounts/fireworks/models/kimi-k3", _FIREWORKS_METRICS,
    ) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Date-ranged rate sets
# ---------------------------------------------------------------------------

_DATED_ENTRY = [
    {
        "input": 1.00, "output": 4.00,
        "long_context_threshold": 100_000,
        "long_context": {"input": 2.00, "output": 8.00},
    },
    {
        "effective_from": "2026-01-01",
        "input": 1.50, "output": 6.00,
        "long_context_threshold": 100_000,
        "long_context": {"input": 3.00, "output": 12.00},
    },
]


def test_tables_are_well_formed():
    """Every entry is one rate set or a dated list: later sets carry an
    ascending ``effective_from``, every set has input + output rates, and a
    model's long-context threshold never changes between its sets (the
    tier flag is computed per model)."""
    for table in (_GEMINI_PRICING, _ANTHROPIC_PRICING, _OPENROUTER_PRICING, _FIREWORKS_PRICING):
        for model, entry in table.items():
            sets = entry if isinstance(entry, list) else [entry]
            assert sets, model
            starts = [rate_set.get("effective_from") for rate_set in sets[1:]]
            assert all(isinstance(start, str) for start in starts), model
            parsed = [datetime.fromisoformat(start) for start in starts]
            assert parsed == sorted(parsed), model
            assert len({rate_set.get("long_context_threshold") for rate_set in sets}) == 1, model
            for rate_set in sets:
                assert {"input", "output"} <= set(rate_set), model


def test_single_rate_set_entries_are_one_open_ended_period():
    metrics = {"input_tokens": 1_000_000}
    for at in (None, datetime.min, datetime(2020, 1, 1), datetime(2030, 1, 1)):
        assert estimate_cost_usd("anthropic", "claude-opus-5", metrics, at=at) == pytest.approx(5.0)


def test_date_ranged_entry_prices_each_period_at_its_own_rates(monkeypatch):
    monkeypatch.setitem(_ANTHROPIC_PRICING, "claude-test", _DATED_ENTRY)
    metrics = {"input_tokens": 1_000_000}
    cost = lambda **kw: estimate_cost_usd("anthropic", "claude-test", metrics, **kw)

    # The last moment of the first period, the first of the second.
    assert cost(at=datetime(2025, 12, 31, 23, 59, 59)) == pytest.approx(1.0)
    assert cost(at=datetime(2026, 1, 1)) == pytest.approx(1.5)
    # datetime.min always selects the open-ended first set; None = today.
    assert cost(at=datetime.min) == pytest.approx(1.0)
    assert cost() == pytest.approx(1.5)
    # A tz-aware moment is compared as UTC.
    assert cost(at=datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)) == pytest.approx(1.0)
    # The long-context tier lives inside each rate set.
    assert cost(long_context=True, at=datetime(2025, 6, 1)) == pytest.approx(2.0)
    assert cost(long_context=True, at=datetime(2026, 6, 1)) == pytest.approx(3.0)
    # The threshold is read from the first set.
    assert long_context_threshold("anthropic", "claude-test") == 100_000
    assert long_context_threshold_overrides("anthropic")["claude-test"] == 100_000
    assert pricing_period_starts("anthropic") == {"claude-test": [datetime(2026, 1, 1)]}


def test_openrouter_periods_merge_static_dates_and_retired_snapshots(monkeypatch):
    import config.inference_providers as ip

    monkeypatch.setitem(_OPENROUTER_PRICING, "vendor/dated", [
        {"input": 1.0, "output": 2.0},
        {"effective_from": "2026-03-01", "input": 2.0, "output": 4.0},
    ])
    monkeypatch.setattr(ip, "instance_model_pricing", lambda wire_id, at=None: None)
    monkeypatch.setattr(ip, "instance_pricing_period_starts", lambda: {
        "vendor/snap": [datetime(2026, 5, 1)],
        "vendor/dated": [datetime(2026, 9, 1)],
    })
    metrics = {"prompt_tokens": 1_000_000}
    assert estimate_cost_usd(
        "openrouter", "openrouter:vendor/dated", metrics, at=datetime(2026, 2, 28),
    ) == pytest.approx(1.0)
    assert estimate_cost_usd(
        "openrouter", "openrouter:vendor/dated", metrics, at=datetime(2026, 3, 1),
    ) == pytest.approx(2.0)
    assert pricing_period_starts("openrouter") == {
        "vendor/snap": [datetime(2026, 5, 1)],
        "vendor/dated": [datetime(2026, 3, 1), datetime(2026, 9, 1)],
    }


def test_retired_instance_snapshot_prices_calls_made_under_it(monkeypatch):
    import config.inference_providers as ip

    def snapshot_at(wire_id, at=None):
        if at is not None and at < datetime(2026, 5, 1):
            return {"prompt": 1.0, "completion": 1.0}
        return {"prompt": 2.0, "completion": 2.0}

    monkeypatch.setattr(ip, "instance_model_pricing", snapshot_at)
    metrics = {"prompt_tokens": 1_000_000}
    assert estimate_cost_usd(
        "openrouter", "x:vendor/snap", metrics, at=datetime(2026, 4, 30),
    ) == pytest.approx(1.0)
    assert estimate_cost_usd("openrouter", "x:vendor/snap", metrics) == pytest.approx(2.0)
