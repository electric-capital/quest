"""Tests for raw provider token-usage capture (cost-analytics groundwork).

Covers the raw-first per-provider storage design (devplan 00129, superseding
the 00106 single-table capture):

1. ``GeminiProvider.get_usage()`` surfaces a lossless ``raw_usage`` dict
   that INCLUDES the fields the coalesced summary drops -- ``thoughts_token_count``,
   ``tool_use_prompt_token_count``, ``total_token_count``.
2. ``AnthropicProvider.get_usage()`` preserves the priced cache_read /
   cache_creation split in ``raw_usage``, plus the cache-creation TTL split
   when the SDK reports it.
3. ``record_api_call()`` dispatches on provider to the matching raw table
   (``llm_calls_gemini`` / ``llm_calls_anthropic``), copying the raw_usage
   keys into native columns verbatim, with a coalesced-param fallback for
   degraded streams and warn-and-skip for unknown providers.
4. ``get_usage_by_model_for_conversations()`` returns provider-discriminated
   per-model entries with native ``metrics`` sums plus a coarse total.

Provider tests are pure mocks (no SDK/network). The store tests use an
isolated SQLite file, matching the repo's existing async-test convention
(asyncio.run, reload engine/models against a temp DATABASE_PATH).
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from importlib import reload
from types import SimpleNamespace

import pytest

from chat.llm.gemini_provider import GeminiProvider
from chat.llm.anthropic_provider import AnthropicProvider


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Provider raw_usage accessors
# ---------------------------------------------------------------------------

def test_gemini_raw_usage_includes_dropped_fields():
    """Gemini raw_usage must carry thoughts/tool/total tokens (dropped today)."""
    provider = GeminiProvider()
    session = SimpleNamespace(_llm_last_usage=SimpleNamespace(
        prompt_token_count=1000,
        candidates_token_count=200,
        cached_content_token_count=300,
        thoughts_token_count=50,
        tool_use_prompt_token_count=10,
        total_token_count=1260,
    ))

    usage = provider.get_usage(session)

    # Coalesced summary unchanged (backward compatible).
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.cached_tokens == 300

    # Raw dict preserves the provider-native field names, including the three
    # that the coalesced fields drop.
    assert usage.raw_usage == {
        "prompt_token_count": 1000,
        "candidates_token_count": 200,
        "cached_content_token_count": 300,
        "thoughts_token_count": 50,
        "tool_use_prompt_token_count": 10,
        "total_token_count": 1260,
    }


def test_gemini_raw_usage_skips_unset_fields():
    """Fields the SDK left as None are omitted, not coerced to 0."""
    provider = GeminiProvider()
    session = SimpleNamespace(_llm_last_usage=SimpleNamespace(
        prompt_token_count=500,
        candidates_token_count=100,
        cached_content_token_count=None,
        thoughts_token_count=None,
        tool_use_prompt_token_count=None,
        total_token_count=600,
    ))

    usage = provider.get_usage(session)

    assert usage.raw_usage == {
        "prompt_token_count": 500,
        "candidates_token_count": 100,
        "total_token_count": 600,
    }


def test_gemini_raw_usage_empty_when_no_usage_object():
    """A missing provider usage object yields an empty raw_usage, not an error."""
    provider = GeminiProvider()
    usage = provider.get_usage(SimpleNamespace(_llm_last_usage=None))
    assert usage.raw_usage == {}
    assert usage.input_tokens == 0


def test_anthropic_raw_usage_preserves_cache_split():
    """Anthropic raw_usage must keep the cache_read / cache_creation split."""
    provider = AnthropicProvider()
    session = SimpleNamespace(last_usage={
        "input_tokens": 800,
        "output_tokens": 150,
        "cache_read_tokens": 400,
        "cache_creation_tokens": 100,
        "cached_tokens": 500,
    })

    usage = provider.get_usage(session)

    # Coalesced summary unchanged.
    assert usage.input_tokens == 800
    assert usage.cache_read_tokens == 400
    assert usage.cache_creation_tokens == 100

    # Raw dict uses Anthropic's native field names and keeps the split; no
    # single "total" field exists for Anthropic, and the TTL-split keys are
    # omitted when the SDK never reported them.
    assert usage.raw_usage == {
        "input_tokens": 800,
        "output_tokens": 150,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
    }
    assert "total_token_count" not in usage.raw_usage
    assert "cache_creation_5m_input_tokens" not in usage.raw_usage
    assert "cache_creation_1h_input_tokens" not in usage.raw_usage


def test_anthropic_raw_usage_carries_cache_creation_ttl_split():
    """The 5m/1h cache-creation TTL split lands in raw_usage when captured."""
    provider = AnthropicProvider()
    session = SimpleNamespace(last_usage={
        "input_tokens": 800,
        "output_tokens": 150,
        "cache_read_tokens": 400,
        "cache_creation_tokens": 100,
        "cached_tokens": 500,
        "cache_creation_5m_tokens": 80,
        "cache_creation_1h_tokens": 20,
    })

    usage = provider.get_usage(session)

    # Coalesced fields unaffected by the split capture.
    assert usage.cache_creation_tokens == 100

    assert usage.raw_usage["cache_creation_5m_input_tokens"] == 80
    assert usage.raw_usage["cache_creation_1h_input_tokens"] == 20


# ---------------------------------------------------------------------------
# record_api_call provider dispatch to the raw tables
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(monkeypatch):
    """Point engine + models at a fresh sqlite file with the current schema."""
    tmpdir = tempfile.mkdtemp(prefix="quest_raw_usage_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.llm_call_store as store_mod
    reload(store_mod)

    models_mod.Base.metadata.create_all(engine_mod.engine)

    yield store_mod, models_mod

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_record_api_call_gemini_lands_in_gemini_table(_isolated_db):
    """provider="gemini" writes native columns verbatim to llm_calls_gemini."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=42,
        model="gemini-3.1-pro-preview",
        call_type=models_mod.ApiCallType.SUB_AGENT,
        input_tokens=1000,
        output_tokens=200,
        duration_ms=1234,
        cached_tokens=300,
        provider="gemini",
        backend="genapi",
        level=2,
        raw_usage={"prompt_token_count": 1000, "thoughts_token_count": 50},
    ))

    assert row["provider"] == "gemini"
    assert row["backend"] == "genapi"
    assert row["level"] == 2
    assert row["raw_usage"] == {"prompt_token_count": 1000, "thoughts_token_count": 50}
    # Native columns copied verbatim from raw_usage; absent keys stay NULL
    # (the coalesced params are NOT used when raw_usage is present).
    assert row["prompt_token_count"] == 1000
    assert row["thoughts_token_count"] == 50
    assert row["candidates_token_count"] is None
    assert row["cached_content_token_count"] is None
    assert row["total_token_count"] is None

    # Round-trips from the DB via the query helper.
    rows = _run(store.get_conversation_usage(conversation_id))
    assert len(rows) == 1
    assert rows[0]["provider"] == "gemini"
    assert rows[0]["prompt_token_count"] == 1000
    assert rows[0]["raw_usage"] == {"prompt_token_count": 1000, "thoughts_token_count": 50}


def test_record_api_call_anthropic_lands_in_anthropic_table(_isolated_db):
    """provider="anthropic" writes native columns incl. the TTL split."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=7,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=36,
        output_tokens=500,
        duration_ms=100,
        cached_tokens=900,
        provider="anthropic",
        backend="vertex",
        raw_usage={
            "input_tokens": 36,
            "output_tokens": 500,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 200,
            "cache_creation_5m_input_tokens": 150,
            "cache_creation_1h_input_tokens": 50,
        },
    ))

    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 36
    assert row["output_tokens"] == 500
    assert row["cache_read_input_tokens"] == 700
    assert row["cache_creation_input_tokens"] == 200
    assert row["cache_creation_5m_input_tokens"] == 150
    assert row["cache_creation_1h_input_tokens"] == 50

    rows = _run(store.get_conversation_usage(conversation_id))
    assert len(rows) == 1
    assert rows[0]["cache_creation_5m_input_tokens"] == 150


def test_record_api_call_openrouter_lands_in_openrouter_table(_isolated_db):
    """provider="openrouter" writes native columns to llm_calls_openrouter."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=9,
        model="deepseek/deepseek-v4-flash-0731",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=120,
        output_tokens=30,
        duration_ms=456,
        cached_tokens=100,
        provider="openrouter",
        backend="openrouter",
        raw_usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "cached_prompt_tokens": 100,
            "reasoning_tokens": 5,
        },
    ))

    assert row["provider"] == "openrouter"
    assert row["backend"] == "openrouter"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 30
    assert row["cached_prompt_tokens"] == 100
    assert row["reasoning_tokens"] == 5
    assert row["total_tokens"] == 150

    rows = _run(store.get_conversation_usage(conversation_id))
    assert len(rows) == 1
    assert rows[0]["provider"] == "openrouter"
    assert rows[0]["prompt_tokens"] == 120
    # No accounting fields in raw_usage -> the reported-cost columns stay NULL.
    assert row["cost"] is None
    assert row["upstream_inference_cost"] is None


def _record_openrouter(store, models_mod, conversation_id, prompt, completion,
                       cost=None, upstream=None, is_byok=None, cached=0,
                       model="openrouter:deepseek/deepseek-v4-flash-0731",
                       user_id=9):
    raw_usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_prompt_tokens": cached,
    }
    if cost is not None:
        raw_usage["cost"] = cost
    if upstream is not None:
        raw_usage["upstream_inference_cost"] = upstream
    if is_byok is not None:
        raw_usage["is_byok"] = is_byok
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=user_id,
        model=model,
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=prompt,
        output_tokens=completion,
        duration_ms=1,
        cached_tokens=cached,
        provider="openrouter",
        backend="openrouter",
        raw_usage=raw_usage,
    ))


def test_record_api_call_openrouter_persists_reported_cost(_isolated_db):
    """The OpenRouter accounting fields land in their own columns verbatim."""
    store, models_mod = _isolated_db
    row = _record_openrouter(
        store, models_mod, str(uuid.uuid4()), 1000, 100,
        cost=0.00025, upstream=0.0001, is_byok=False,
    )
    assert row["cost"] == pytest.approx(0.00025)
    assert row["upstream_inference_cost"] == pytest.approx(0.0001)
    assert row["is_byok"] is False
    assert row["raw_usage"]["cost"] == pytest.approx(0.00025)


def test_usage_by_model_prefers_reported_openrouter_cost(_isolated_db):
    """Rows with a reported amount are summed as-is and the entry/total are
    flagged ``reported``; the list-price estimate is not consulted for
    them. The upstream charge only counts on BYOK rows -- OpenRouter also
    reports it (equal to ``cost``) on ordinary requests."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    # Ordinary request: upstream mirrors cost and must NOT double count.
    _record_openrouter(store, models_mod, conversation_id, 1_000_000, 1_000_000,
                       cost=0.5, upstream=0.5, is_byok=False)
    # BYOK request: OpenRouter's fee + the upstream provider's charge.
    _record_openrouter(store, models_mod, conversation_id, 1_000_000, 1_000_000,
                       cost=0.25, upstream=0.75, is_byok=True)
    # No is_byok reported at all: cost only.
    _record_openrouter(store, models_mod, conversation_id, 1_000_000, 1_000_000,
                       cost=0.1, upstream=0.1)

    entry = _run(store.get_usage_by_model_for_conversations([conversation_id]))[
        conversation_id
    ]
    (m,) = entry["models"]
    assert m["call_count"] == 3
    # 0.5 + (0.25 + 0.75) + 0.1 -- NOT the static-table estimate of these
    # tokens (3M * 0.08 + 3M * 0.18 = $0.78).
    assert m["estimated_cost_usd"] == pytest.approx(1.6)
    assert m["cost_source"] == "reported"
    assert entry["total"]["estimated_cost_usd"] == pytest.approx(1.6)
    assert entry["total"]["cost_source"] == "reported"


def test_usage_by_model_mixes_reported_and_estimated_rows(_isolated_db):
    """Pre-capture rows (NULL cost) stay estimated alongside reported rows:
    the model entry sums both and is flagged ``mixed``; a token-only
    provider in the same conversation is ``estimated`` and the total
    degrades to ``mixed``."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    _record_openrouter(store, models_mod, conversation_id, 1_000_000, 0, cost=0.5)
    # Static fallback table: deepseek-v4-flash-0731 = $0.08/M uncached input.
    _record_openrouter(store, models_mod, conversation_id, 1_000_000, 0)
    _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=9,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.SUB_AGENT,
        input_tokens=1_000_000,
        output_tokens=0,
        duration_ms=1,
        provider="anthropic",
        raw_usage={"input_tokens": 1_000_000, "output_tokens": 0},
    ))

    entry = _run(store.get_usage_by_model_for_conversations([conversation_id]))[
        conversation_id
    ]
    by_model = {m["model"]: m for m in entry["models"]}
    openrouter = by_model["openrouter:deepseek/deepseek-v4-flash-0731"]
    assert openrouter["call_count"] == 2
    assert openrouter["metrics"]["prompt_tokens"] == 2_000_000
    assert openrouter["estimated_cost_usd"] == pytest.approx(0.5 + 0.08)
    assert openrouter["cost_source"] == "mixed"
    assert by_model["claude-opus-4-8"]["cost_source"] == "estimated"
    assert entry["total"]["estimated_cost_usd"] == pytest.approx(0.58 + 5.0)
    assert entry["total"]["cost_source"] == "mixed"


def test_usage_by_model_unpriced_reported_model_still_costs(_isolated_db):
    """A model with no pricing entry anywhere is still priced when the
    provider reported the amount -- only its estimate-needing rows null."""
    store, models_mod = _isolated_db
    reported_chat, unpriced_chat = str(uuid.uuid4()), str(uuid.uuid4())
    _record_openrouter(store, models_mod, reported_chat, 10, 10, cost=0.001,
                       model="openrouter:vendor/unlisted-model")
    _record_openrouter(store, models_mod, unpriced_chat, 10, 10,
                       model="openrouter:vendor/unlisted-model")

    result = _run(store.get_usage_by_model_for_conversations(
        [reported_chat, unpriced_chat]
    ))
    assert result[reported_chat]["models"][0]["estimated_cost_usd"] == pytest.approx(0.001)
    assert result[reported_chat]["models"][0]["cost_source"] == "reported"
    assert result[unpriced_chat]["models"][0]["estimated_cost_usd"] is None
    assert result[unpriced_chat]["models"][0]["cost_source"] is None
    assert result[unpriced_chat]["total"]["cost_source"] is None


def test_record_api_call_infers_provider_from_model_prefix(_isolated_db):
    """provider=None routes claude* models to the Anthropic table."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=7,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
        raw_usage={"input_tokens": 10, "output_tokens": 5},
    ))
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 10

    # And non-claude models default to the Gemini table.
    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=7,
        model="gemini-3-flash-preview",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=20,
        output_tokens=3,
        duration_ms=100,
        raw_usage={"prompt_token_count": 20, "candidates_token_count": 3},
    ))
    assert row["provider"] == "gemini"
    assert row["prompt_token_count"] == 20


def test_record_api_call_unknown_provider_skips_without_raising(_isolated_db):
    """An unknown provider is a warn-and-skip, never a raised exception."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    row = _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=7,
        model="mystery-model-1",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
        provider="openai",
    ))
    assert row is None
    assert _run(store.get_conversation_usage(conversation_id)) == []


def test_record_api_call_empty_raw_usage_falls_back_to_coalesced_params(_isolated_db):
    """A degraded stream (no usage object) still records magnitudes."""
    store, models_mod = _isolated_db
    conv_gemini = str(uuid.uuid4())
    conv_anthropic = str(uuid.uuid4())

    # Gemini fallback: prompt <- input, candidates <- output,
    # cached_content <- cached; the rest stay NULL.
    row = _run(store.record_api_call(
        conversation_id=conv_gemini,
        user_id=1,
        model="gemini-3.1-pro",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=1000,
        output_tokens=200,
        duration_ms=10,
        cached_tokens=300,
        provider="gemini",
    ))
    assert row["prompt_token_count"] == 1000
    assert row["candidates_token_count"] == 200
    assert row["cached_content_token_count"] == 300
    assert row["thoughts_token_count"] is None
    assert row["total_token_count"] is None
    assert row["raw_usage"] is None

    # Anthropic fallback: cache_read <- cached_tokens (merged approximation),
    # creation columns NULL.
    row = _run(store.record_api_call(
        conversation_id=conv_anthropic,
        user_id=1,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=36,
        output_tokens=500,
        duration_ms=10,
        cached_tokens=900,
        provider="anthropic",
    ))
    assert row["input_tokens"] == 36
    assert row["output_tokens"] == 500
    assert row["cache_read_input_tokens"] == 900
    assert row["cache_creation_input_tokens"] is None
    assert row["raw_usage"] is None


# ---------------------------------------------------------------------------
# Per-model batch aggregation (admin system-monitor token breakdown)
# ---------------------------------------------------------------------------

def _record_gemini(store, models_mod, conversation_id, model, call_type,
                   prompt, candidates, cached=0, thoughts=0, tool_use=0):
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=1,
        model=model,
        call_type=call_type,
        input_tokens=prompt,
        output_tokens=candidates,
        duration_ms=10,
        cached_tokens=cached,
        provider="gemini",
        raw_usage={
            "prompt_token_count": prompt,
            "candidates_token_count": candidates,
            "cached_content_token_count": cached,
            "thoughts_token_count": thoughts,
            "tool_use_prompt_token_count": tool_use,
            "total_token_count": prompt + candidates + thoughts + tool_use,
        },
    ))


def _record_anthropic(store, models_mod, conversation_id, model, call_type,
                      input_tokens, output_tokens, cache_read=0,
                      cache_creation=0, creation_5m=0, creation_1h=0):
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=1,
        model=model,
        call_type=call_type,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=10,
        cached_tokens=cache_read + cache_creation,
        provider="anthropic",
        raw_usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "cache_creation_5m_input_tokens": creation_5m,
            "cache_creation_1h_input_tokens": creation_1h,
        },
    ))


def test_usage_by_model_gemini_native_metrics(_isolated_db):
    """A Gemini conversation yields native metric sums + the Gemini total formula."""
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    _record_gemini(store, models_mod, conv, "gemini-3.1-pro",
                   models_mod.ApiCallType.TOP_LEVEL,
                   prompt=1000, candidates=200, cached=300, thoughts=50, tool_use=10)
    _record_gemini(store, models_mod, conv, "gemini-3.1-pro",
                   models_mod.ApiCallType.TOP_LEVEL,
                   prompt=2000, candidates=100, cached=1500, thoughts=25, tool_use=0)

    result = _run(store.get_usage_by_model_for_conversations([conv]))

    assert set(result.keys()) == {conv}
    entry = result[conv]
    assert len(entry["models"]) == 1
    m = entry["models"][0]
    assert m["model"] == "gemini-3.1-pro"
    assert m["provider"] == "gemini"
    assert m["call_count"] == 2
    assert m["metrics"] == {
        "prompt_token_count": 3000,
        "cached_content_token_count": 1800,
        "candidates_token_count": 300,
        "thoughts_token_count": 75,
        "tool_use_prompt_token_count": 10,
    }
    # total = prompt + candidates + thoughts + tool_use_prompt.
    assert m["total_tokens"] == 3000 + 300 + 75 + 10
    # "gemini-3.1-pro" is not a registry model, so it has no pricing entry:
    # the model entry AND the conversation total surface None, not 0.
    assert m["estimated_cost_usd"] is None
    assert entry["total"] == {
        "call_count": 2,
        "total_tokens": m["total_tokens"],
        "estimated_cost_usd": None,
        "cost_source": None,
    }


def test_usage_by_model_anthropic_native_metrics(_isolated_db):
    """An Anthropic conversation yields native sums incl. the TTL split."""
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    _record_anthropic(store, models_mod, conv, "claude-opus-4-8",
                      models_mod.ApiCallType.TOP_LEVEL,
                      input_tokens=36, output_tokens=500,
                      cache_read=700, cache_creation=200,
                      creation_5m=150, creation_1h=50)
    _record_anthropic(store, models_mod, conv, "claude-opus-4-8",
                      models_mod.ApiCallType.TOP_LEVEL,
                      input_tokens=4, output_tokens=100,
                      cache_read=900, cache_creation=0)

    result = _run(store.get_usage_by_model_for_conversations([conv]))

    entry = result[conv]
    m = entry["models"][0]
    assert m["provider"] == "anthropic"
    assert m["call_count"] == 2
    assert m["metrics"] == {
        "input_tokens": 40,
        "output_tokens": 600,
        "cache_read_input_tokens": 1600,
        "cache_creation_input_tokens": 200,
        "cache_creation_5m_input_tokens": 150,
        "cache_creation_1h_input_tokens": 50,
    }
    # total = input + output + cache_read + cache_creation.
    assert m["total_tokens"] == 40 + 600 + 1600 + 200
    # Opus 4.8 at $5/$25 per 1M: input 40*5 + output 600*25 + cache-read
    # 1600*0.5 + cache-write 150*6.25 (5m) + 50*10 (1h) = 17437.5 uUSD.
    assert m["estimated_cost_usd"] == pytest.approx(0.0174375, abs=1e-6)
    assert entry["total"]["call_count"] == 2
    assert entry["total"]["total_tokens"] == m["total_tokens"]
    assert entry["total"]["estimated_cost_usd"] == pytest.approx(0.0174375, abs=1e-6)


def test_usage_by_model_mixed_providers_merge_and_sort(_isolated_db):
    """Gemini top-level + Claude sub-agent rows merge into per-model entries."""
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    # Heavier Anthropic top-level, lighter Gemini sub-agent.
    _record_anthropic(store, models_mod, conv, "claude-opus-4-8",
                      models_mod.ApiCallType.TOP_LEVEL,
                      input_tokens=100, output_tokens=1000, cache_read=5000)
    _record_gemini(store, models_mod, conv, "gemini-3-flash-preview",
                   models_mod.ApiCallType.SUB_AGENT,
                   prompt=500, candidates=50, cached=100, thoughts=10)

    result = _run(store.get_usage_by_model_for_conversations([conv]))
    entry = result[conv]

    # Sorted by descending total_tokens: Opus (6100) before Flash (560).
    assert [(m["model"], m["provider"]) for m in entry["models"]] == [
        ("claude-opus-4-8", "anthropic"),
        ("gemini-3-flash-preview", "gemini"),
    ]
    assert entry["models"][0]["total_tokens"] == 6100
    assert entry["models"][1]["total_tokens"] == 560

    # Coarse conversation total equals the entries' sums; both models are
    # priced, so the total cost is their sum: Opus (100*5 + 1000*25 +
    # 5000*0.5 = 28000 uUSD) + Flash ((500-100)*0.5 + 100*0.05 + 60*3
    # = 385 uUSD).
    assert entry["total"]["call_count"] == 2
    assert entry["total"]["total_tokens"] == 6660
    assert entry["models"][0]["estimated_cost_usd"] == pytest.approx(0.028, abs=1e-6)
    assert entry["models"][1]["estimated_cost_usd"] == pytest.approx(0.000385, abs=1e-6)
    assert entry["total"]["estimated_cost_usd"] == pytest.approx(0.028385, abs=1e-6)


def test_usage_by_model_isolates_conversations_and_handles_missing(_isolated_db):
    """The batch query keeps conversations separate; empty conv is absent."""
    store, models_mod = _isolated_db
    conv_a = str(uuid.uuid4())
    conv_b = str(uuid.uuid4())
    conv_empty = str(uuid.uuid4())

    _record_gemini(store, models_mod, conv_a, "gemini-3.1-pro",
                   models_mod.ApiCallType.TOP_LEVEL, prompt=100, candidates=10)
    _record_gemini(store, models_mod, conv_b, "gemini-3.1-flash",
                   models_mod.ApiCallType.TOP_LEVEL, prompt=5, candidates=1)

    result = _run(store.get_usage_by_model_for_conversations(
        [conv_a, conv_b, conv_empty]
    ))

    # Conversations with no recorded calls simply don't appear.
    assert set(result.keys()) == {conv_a, conv_b}
    assert result[conv_a]["total"]["total_tokens"] == 110
    assert result[conv_b]["total"]["total_tokens"] == 6
    # No cross-contamination between conversations.
    assert [m["model"] for m in result[conv_a]["models"]] == ["gemini-3.1-pro"]


def test_usage_by_model_empty_input_returns_empty(_isolated_db):
    store, _models_mod = _isolated_db
    assert _run(store.get_usage_by_model_for_conversations([])) == {}


def test_usage_by_model_prices_long_context_per_call(_isolated_db):
    """Tier pricing keys on each call's own context size, not the summed one.

    Two gemini-3.1-pro-preview calls: 100K prompt (normal tier: $2/$12) and
    300K prompt (long-context tier: $4/$18). Correct per-call pricing gives
    0.212 + 1.218 = 1.43; pricing the 400K sum in the long tier would give
    1.636 -- so this assert fails if the tier grouping ever regresses.
    """
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    _record_gemini(store, models_mod, conv, "gemini-3.1-pro-preview",
                   models_mod.ApiCallType.TOP_LEVEL,
                   prompt=100_000, candidates=1_000)
    _record_gemini(store, models_mod, conv, "gemini-3.1-pro-preview",
                   models_mod.ApiCallType.TOP_LEVEL,
                   prompt=300_000, candidates=1_000)

    result = _run(store.get_usage_by_model_for_conversations([conv]))
    entry = result[conv]

    # The two tier buckets merge back into ONE displayed model entry.
    assert len(entry["models"]) == 1
    m = entry["models"][0]
    assert m["call_count"] == 2
    assert m["metrics"]["prompt_token_count"] == 400_000
    assert m["metrics"]["candidates_token_count"] == 2_000
    assert m["estimated_cost_usd"] == pytest.approx(1.43, abs=1e-6)
    assert entry["total"]["estimated_cost_usd"] == pytest.approx(1.43, abs=1e-6)


def test_usage_by_model_prices_haiku_5_5_at_its_100k_threshold(_isolated_db):
    """Haiku 5.5 switches tier above 100K prompt tokens, not the default 200K.

    Calls: 50K input (short tier: $0.10/$0.50) and 150K split across input,
    cache read and cache creation (long tier: $0.50/$2.50, cache read 0.1x,
    5m write 1.25x). A 150K call would stay in the short tier under the
    default threshold, so this fails if the per-model CASE regresses.
    """
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    _record_anthropic(store, models_mod, conv, "claude-haiku-5-5",
                      models_mod.ApiCallType.TOP_LEVEL,
                      input_tokens=50_000, output_tokens=1_000)
    _record_anthropic(store, models_mod, conv, "claude-haiku-5-5",
                      models_mod.ApiCallType.TOP_LEVEL,
                      input_tokens=50_000, output_tokens=1_000,
                      cache_read=60_000, cache_creation=40_000,
                      creation_5m=40_000)

    result = _run(store.get_usage_by_model_for_conversations([conv]))
    m = result[conv]["models"][0]
    short = (50_000 * 0.10 + 1_000 * 0.50) / 1e6
    long_ = (
        50_000 * 0.50 + 60_000 * 0.05 + 40_000 * 0.625 + 1_000 * 2.50
    ) / 1e6
    assert m["call_count"] == 2
    assert m["estimated_cost_usd"] == pytest.approx(short + long_, abs=1e-9)


# ---------------------------------------------------------------------------
# Pricing table (db/llm_pricing.py) -- pure functions, no DB
# ---------------------------------------------------------------------------

def test_estimate_cost_gemini_flat_rates_and_unknown_model():
    from db.llm_pricing import estimate_cost_usd

    metrics = {
        "prompt_token_count": 1_000_000,
        "cached_content_token_count": 400_000,
        "candidates_token_count": 100_000,
        "thoughts_token_count": 50_000,
        "tool_use_prompt_token_count": 10_000,
    }
    # gemini-3.5-flash: uncached (600K + 10K tool-use) * $1.50 + cached
    # 400K * $0.15 + out (100K + 50K thoughts) * $9.00, per 1M.
    expected = (610_000 * 1.50 + 400_000 * 0.15 + 150_000 * 9.00) / 1e6
    assert estimate_cost_usd("gemini", "gemini-3.5-flash", metrics) == pytest.approx(expected)

    # Unknown models yield None (no estimate), never 0.
    assert estimate_cost_usd("gemini", "gemini-nonexistent", metrics) is None
    assert estimate_cost_usd("anthropic", "claude-nonexistent", metrics) is None
    assert estimate_cost_usd("mystery", "gemini-3.5-flash", metrics) is None


def test_estimate_cost_gemini_long_context_tier():
    from db.llm_pricing import estimate_cost_usd

    metrics = {"prompt_token_count": 300_000, "candidates_token_count": 1_000}
    normal = estimate_cost_usd("gemini", "gemini-3.1-pro-preview", metrics)
    long_ctx = estimate_cost_usd(
        "gemini", "gemini-3.1-pro-preview", metrics, long_context=True
    )
    assert normal == pytest.approx((300_000 * 2.00 + 1_000 * 12.00) / 1e6)
    assert long_ctx == pytest.approx((300_000 * 4.00 + 1_000 * 18.00) / 1e6)

    # Models without a long_context block price both tiers identically.
    flat = {"prompt_token_count": 500_000, "candidates_token_count": 1_000}
    assert estimate_cost_usd("gemini", "gemini-3.6-flash", flat) == pytest.approx(
        estimate_cost_usd("gemini", "gemini-3.6-flash", flat, long_context=True)
    )


def test_estimate_cost_anthropic_cache_ttl_split_and_unsplit_fallback():
    from db.llm_pricing import estimate_cost_usd

    # Full TTL split: 5m tokens bill at 1.25x input, 1h at 2x input.
    split = {
        "input_tokens": 1_000,
        "output_tokens": 2_000,
        "cache_read_input_tokens": 100_000,
        "cache_creation_input_tokens": 20_000,
        "cache_creation_5m_input_tokens": 15_000,
        "cache_creation_1h_input_tokens": 5_000,
    }
    expected = (
        1_000 * 5.00                # input
        + 100_000 * 5.00 * 0.10     # cache read
        + 15_000 * 5.00 * 1.25      # 5m cache write
        + 5_000 * 5.00 * 2.00       # 1h cache write
        + 2_000 * 25.00             # output
    ) / 1e6
    assert estimate_cost_usd("anthropic", "claude-opus-4-8", split) == pytest.approx(expected)

    # No TTL split recorded (legacy rows / degraded streams): the unsplit
    # creation total bills at the default 5m write rate.
    unsplit = {
        "input_tokens": 1_000,
        "output_tokens": 2_000,
        "cache_read_input_tokens": 100_000,
        "cache_creation_input_tokens": 20_000,
    }
    expected_unsplit = (
        1_000 * 5.00
        + 100_000 * 5.00 * 0.10
        + 20_000 * 5.00 * 1.25
        + 2_000 * 25.00
    ) / 1e6
    assert estimate_cost_usd("anthropic", "claude-opus-4-8", unsplit) == pytest.approx(expected_unsplit)


def test_pricing_covers_every_registry_model():
    """Every model in MODEL_REGISTRY must have a pricing entry, so the
    dashboard never silently shows n/a for a model users can pick today.
    (Removing a retired model from the registry is fine -- its pricing entry
    stays behind for historical rows.)"""
    from chat.llm.config import MODEL_REGISTRY
    from db.llm_pricing import estimate_cost_usd

    for model_id, entry in MODEL_REGISTRY.items():
        cost = estimate_cost_usd(entry["provider"], model_id, {"input_tokens": 1})
        assert cost is not None, f"no pricing entry for registry model {model_id}"
