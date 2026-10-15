"""Tests for the expensive-resume warning (chat/expensive_resume.py).

Covers:

1. ``candidate_rules()`` -- the DB-free pre-check on model / idle time /
   first-message state that keeps the common send path free of llm_calls
   reads.
2. ``get_latest_context_tokens()`` -- the latest-top-level-call context
   query in db/llm_call_store.py (sub-agent rows excluded, latest row
   wins, per-provider context formula).
3. ``check_expensive_resume()`` end-to-end against an isolated SQLite DB:
   verdict shape, threshold boundaries, and the defensive None paths.

The store tests run against the shared ``isolated_db`` fixture (a fresh
SQLite file per test, see tests/db_support.py) with asyncio.run.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from chat.expensive_resume import (
    EXPENSIVE_RESUME_RULES,
    candidate_rules,
    check_expensive_resume,
)


def _run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _meta(model="claude-opus-4-8", idle_hours=3.0, last_message_seq=10):
    """Conversation meta dict shaped like get_conversation_meta's output."""
    last_at = NOW - timedelta(hours=idle_hours)
    return {
        "model": model,
        "last_message_at": last_at.isoformat(),
        "last_message_seq": last_message_seq,
    }


# ---------------------------------------------------------------------------
# candidate_rules: cheap DB-free pre-check
# ---------------------------------------------------------------------------

def test_candidate_rules_matches_idle_opus():
    rules, idle = candidate_rules("claude-opus-4-8", _meta()["last_message_at"], 10, now=NOW)
    assert rules == [EXPENSIVE_RESUME_RULES[0]]
    assert idle == 3 * 3600


def test_candidate_rules_matches_all_opus_generations():
    for model in ("claude-opus-4-6", "claude-opus-4-7", "claude-opus-5", "claude-opus-5-5"):
        rules, _ = candidate_rules(model, _meta()["last_message_at"], 10, now=NOW)
        assert rules, model


def test_candidate_rules_ignores_other_models():
    for model in ("claude-sonnet-5", "gemini-3.1-pro-preview", "claude-haiku-4-5-20251001"):
        rules, _ = candidate_rules(model, _meta()["last_message_at"], 10, now=NOW)
        assert rules == [], model


def test_candidate_rules_matches_exact_ids_only():
    """Models are matched by exact id, never by prefix -- an unlisted future
    id (Vertex naming schemes drift) must not silently match."""
    for model in ("claude-opus-6", "claude-opus-4-8-v2", "claude-opus"):
        rules, _ = candidate_rules(model, _meta()["last_message_at"], 10, now=NOW)
        assert rules == [], model


def test_candidate_rules_requires_min_idle_time():
    recent = (NOW - timedelta(hours=1)).isoformat()
    rules, _ = candidate_rules("claude-opus-4-8", recent, 10, now=NOW)
    assert rules == []
    # Exactly at the threshold matches (>=).
    boundary = (NOW - timedelta(hours=2)).isoformat()
    rules, idle = candidate_rules("claude-opus-4-8", boundary, 10, now=NOW)
    assert rules and idle == 2 * 3600


def test_candidate_rules_skips_fresh_and_modelless_conversations():
    assert candidate_rules(None, _meta()["last_message_at"], 10, now=NOW)[0] == []
    assert candidate_rules("claude-opus-4-8", _meta()["last_message_at"], 0, now=NOW)[0] == []
    assert candidate_rules("claude-opus-4-8", _meta()["last_message_at"], None, now=NOW)[0] == []


def test_candidate_rules_tolerates_bad_timestamps():
    assert candidate_rules("claude-opus-4-8", "not-a-date", 10, now=NOW)[0] == []
    assert candidate_rules("claude-opus-4-8", None, 10, now=NOW)[0] == []
    # Z-suffixed and naive strings both parse (naive treated as UTC).
    z_ts = (NOW - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    assert candidate_rules("claude-opus-4-8", z_ts, 10, now=NOW)[0]
    naive_ts = (NOW - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
    assert candidate_rules("claude-opus-4-8", naive_ts, 10, now=NOW)[0]


# ---------------------------------------------------------------------------
# Store query + end-to-end check against an isolated DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(isolated_db):
    import db.llm_call_store as store_mod

    return store_mod, isolated_db.models


def _record_anthropic(store, models_mod, conversation_id, call_type,
                      input_tokens, cache_read, cache_creation):
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=1,
        model="claude-opus-4-8",
        call_type=call_type,
        input_tokens=input_tokens,
        output_tokens=100,
        duration_ms=50,
        provider="anthropic",
        backend="vertex",
        raw_usage={
            "input_tokens": input_tokens,
            "output_tokens": 100,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    ))


def test_get_latest_context_tokens_uses_latest_top_level_row(_isolated_db):
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    # Older top-level call with a smaller context, then a newer larger one,
    # then a small sub-agent call recorded last (must not mask the
    # top-level context).
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 1_000, 100_000, 0)
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 2_000, 300_000, 48_000)
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.SUB_AGENT, 500, 0, 0)

    ctx = _run(store.get_latest_context_tokens(conversation_id, "anthropic"))
    assert ctx == 2_000 + 300_000 + 48_000


def test_get_latest_context_tokens_gemini_uses_prompt_tokens(_isolated_db):
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())

    _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=1,
        model="gemini-3.1-pro-preview",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=400_000,
        output_tokens=100,
        duration_ms=50,
        provider="gemini",
        backend="genapi",
        raw_usage={"prompt_token_count": 400_000, "candidates_token_count": 100},
    ))

    ctx = _run(store.get_latest_context_tokens(conversation_id, "gemini"))
    assert ctx == 400_000


def test_get_latest_context_tokens_none_paths(_isolated_db):
    store, _models_mod = _isolated_db
    assert _run(store.get_latest_context_tokens(str(uuid.uuid4()), "anthropic")) is None
    assert _run(store.get_latest_context_tokens(str(uuid.uuid4()), "unknown")) is None


def test_check_expensive_resume_returns_verdict(_isolated_db):
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 5_000, 320_000, 30_000)

    verdict = _run(check_expensive_resume(conversation_id, _meta(), now=NOW))
    assert verdict == {
        "model": "claude-opus-4-8",
        "context_tokens": 355_000,
        "idle_seconds": 3 * 3600,
        # 355K tokens re-written into the 5m prompt cache at Opus list
        # price: 355_000 * $5/1M * 1.25 = $2.22.
        "estimated_resume_cost_usd": 2.22,
        # No sdk_history.json exists for this synthetic conversation, so
        # the one-time compaction-cost estimate is unavailable.
        "estimated_compaction_cost_usd": None,
        "min_context_tokens": 300_000,
        "min_idle_seconds": 2 * 3600,
    }


def test_check_expensive_resume_below_context_threshold(_isolated_db):
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 5_000, 200_000, 30_000)

    assert _run(check_expensive_resume(conversation_id, _meta(), now=NOW)) is None


def test_check_expensive_resume_skips_recent_or_non_matching(_isolated_db):
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 5_000, 320_000, 30_000)

    # Recently active: no warning even though the context is huge.
    assert _run(check_expensive_resume(
        conversation_id, _meta(idle_hours=0.5), now=NOW)) is None
    # Non-matching model: never reaches the DB.
    assert _run(check_expensive_resume(
        conversation_id, _meta(model="claude-sonnet-5"), now=NOW)) is None


def test_check_expensive_resume_no_recorded_calls(_isolated_db):
    _store, _models_mod = _isolated_db
    assert _run(check_expensive_resume(str(uuid.uuid4()), _meta(), now=NOW)) is None


def test_check_expensive_resume_skips_project_conversations(_isolated_db):
    """Project conversations never warn -- the card's alternatives (duplicate
    workspace, create project) don't apply to them."""
    store, models_mod = _isolated_db
    conversation_id = str(uuid.uuid4())
    _record_anthropic(store, models_mod, conversation_id,
                      models_mod.ApiCallType.TOP_LEVEL, 5_000, 320_000, 30_000)

    meta = _meta()
    meta["project_id"] = str(uuid.uuid4())
    assert _run(check_expensive_resume(conversation_id, meta, now=NOW)) is None
