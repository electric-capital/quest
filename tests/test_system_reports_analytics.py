"""Tests for the System Reports analytics queries (admin dashboard).

Covers the read-side additions behind the System Reports page:

1. ``get_most_expensive_conversations()`` -- date-range-filtered, known-cost
   ranking over the raw ``llm_calls_*`` tables (top-N cost analysis), and
   ``get_usage_by_user()`` -- the per-user fold behind the Users report incl.
   the routine cost split and its per-routine breakdown, and
   ``get_usage_by_model()`` -- the per-model fold behind the Models report
   (audience counts, routine + sub-agent cost shares, top users).
2. ``get_latest_context_tokens_for_conversations()`` -- batched
   latest-top-level-call context size across both provider tables.
3. ``ChatStorage.count_user_message_active_days()`` -- distinct UTC days
   with user messages, read from chat_history.json.

Store tests use an isolated SQLite file, matching the repo's existing
async-test convention (asyncio.run, reload engine/models against a temp
DATABASE_PATH) established in test_raw_token_usage_capture.py.
"""

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from importlib import reload
from pathlib import Path

import pytest
from sqlalchemy import update


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(monkeypatch):
    """Point engine + models at a fresh sqlite file with the current schema."""
    tmpdir = tempfile.mkdtemp(prefix="quest_system_reports_test_")
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


def _record_anthropic(store, models_mod, conversation_id, output_tokens,
                      user_id=1, call_type=None, input_tokens=100):
    """One priced Anthropic call; cost scales with output_tokens."""
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=user_id,
        model="claude-opus-4-8",
        call_type=call_type or models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=10,
        provider="anthropic",
        raw_usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    ))


def _record_gemini(store, models_mod, conversation_id, prompt, candidates,
                   model="gemini-3.5-flash", user_id=1, call_type=None):
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=user_id,
        model=model,
        call_type=call_type or models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=prompt,
        output_tokens=candidates,
        duration_ms=10,
        provider="gemini",
        raw_usage={
            "prompt_token_count": prompt,
            "candidates_token_count": candidates,
        },
    ))


def _set_created_at(store, row_cls, row_id, dt):
    """Backdate a recorded call so date-range filters can be exercised."""
    async def _do():
        async with store.AsyncSessionLocal() as db:
            await db.execute(
                update(row_cls).where(row_cls.id == row_id).values(created_at=dt)
            )
            await db.commit()
    _run(_do())


# ---------------------------------------------------------------------------
# get_most_expensive_conversations
# ---------------------------------------------------------------------------

def test_most_expensive_ranks_by_cost_and_attributes_owner(_isolated_db):
    store, models_mod = _isolated_db
    cheap, mid, dear = (str(uuid.uuid4()) for _ in range(3))

    _record_anthropic(store, models_mod, cheap, output_tokens=1_000, user_id=11)
    _record_anthropic(store, models_mod, mid, output_tokens=10_000, user_id=12)
    _record_anthropic(store, models_mod, dear, output_tokens=100_000, user_id=13)

    ranked = _run(store.get_most_expensive_conversations())

    assert [r["conversation_id"] for r in ranked] == [dear, mid, cheap]
    assert [r["user_id"] for r in ranked] == [13, 12, 11]
    # Row shape matches get_usage_by_model_for_conversations entries.
    top = ranked[0]
    assert top["models"][0]["model"] == "claude-opus-4-8"
    assert top["total"]["call_count"] == 1
    # Opus 4.8: 100 in * $5 + 100K out * $25 per 1M.
    assert top["total"]["estimated_cost_usd"] == pytest.approx(2.5005, abs=1e-6)


def test_most_expensive_respects_limit(_isolated_db):
    store, models_mod = _isolated_db
    convs = [str(uuid.uuid4()) for _ in range(3)]
    for i, conv in enumerate(convs):
        _record_anthropic(store, models_mod, conv, output_tokens=(i + 1) * 1_000)

    ranked = _run(store.get_most_expensive_conversations(limit=2))
    assert len(ranked) == 2
    assert [r["conversation_id"] for r in ranked] == [convs[2], convs[1]]


def test_most_expensive_date_range_filters_calls(_isolated_db):
    """Out-of-range calls are excluded both from ranking and from the sums."""
    store, models_mod = _isolated_db
    old_conv = str(uuid.uuid4())
    mixed_conv = str(uuid.uuid4())

    old_row = _record_anthropic(store, models_mod, old_conv, output_tokens=100_000)
    old_in_mixed = _record_anthropic(store, models_mod, mixed_conv, output_tokens=100_000)
    _record_anthropic(store, models_mod, mixed_conv, output_tokens=1_000)

    january = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    for row in (old_row, old_in_mixed):
        _set_created_at(store, models_mod.LlmCallAnthropic, row["id"], january)

    ranked = _run(store.get_most_expensive_conversations(
        start=datetime(2026, 6, 1, tzinfo=timezone.utc)
    ))

    # old_conv has no calls in range; mixed_conv keeps only the small call.
    assert [r["conversation_id"] for r in ranked] == [mixed_conv]
    assert ranked[0]["total"]["call_count"] == 1
    assert ranked[0]["models"][0]["metrics"]["output_tokens"] == 1_000

    # An end bound before the recent call flips the picture: only the two
    # January calls count.
    ranked = _run(store.get_most_expensive_conversations(
        end=datetime(2026, 2, 1, tzinfo=timezone.utc)
    ))
    assert sorted(r["conversation_id"] for r in ranked) == sorted([old_conv, mixed_conv])
    for row in ranked:
        assert row["total"]["call_count"] == 1
        assert row["models"][0]["metrics"]["output_tokens"] == 100_000


def test_most_expensive_ranks_partial_pricing_by_known_cost(_isolated_db):
    """Unpriced models rank by what CAN be priced; the total still nulls out."""
    store, models_mod = _isolated_db
    partial = str(uuid.uuid4())
    priced = str(uuid.uuid4())

    # Huge unpriced usage + a tiny priced call: known cost is tiny.
    _record_gemini(store, models_mod, partial, prompt=5_000_000, candidates=100_000,
                   model="gemini-unpriced-model")
    _record_anthropic(store, models_mod, partial, output_tokens=100)
    # Medium priced-only conversation: known cost beats `partial`'s.
    _record_anthropic(store, models_mod, priced, output_tokens=50_000)

    ranked = _run(store.get_most_expensive_conversations())

    assert [r["conversation_id"] for r in ranked] == [priced, partial]
    partial_row = ranked[1]
    # Partial-pricing convention: the conversation total shows no dollar sum.
    assert partial_row["total"]["estimated_cost_usd"] is None
    unpriced_entry = next(
        m for m in partial_row["models"] if m["model"] == "gemini-unpriced-model"
    )
    assert unpriced_entry["estimated_cost_usd"] is None


# ---------------------------------------------------------------------------
# get_usage_by_user
# ---------------------------------------------------------------------------

def test_usage_by_user_groups_and_splits_routine_cost(_isolated_db):
    """Per-user fold: model merge across conversations + routine cost split."""
    store, models_mod = _isolated_db
    chat_a, chat_b, routine_chat = (str(uuid.uuid4()) for _ in range(3))
    other_chat = str(uuid.uuid4())

    # User 1: two non-routine conversations on the same model + one routine.
    _record_anthropic(store, models_mod, chat_a, output_tokens=1_000, user_id=1)
    _record_anthropic(store, models_mod, chat_b, output_tokens=2_000, user_id=1)
    _record_anthropic(store, models_mod, routine_chat, output_tokens=4_000, user_id=1)
    # User 2: one conversation on a different model.
    _record_gemini(store, models_mod, other_chat, prompt=500, candidates=50, user_id=2)

    result = _run(store.get_usage_by_user(
        routine_id_by_conversation={routine_chat: "routine-1"}
    ))

    assert set(result) == {1, 2}
    user1 = result[1]
    assert user1["conversation_count"] == 2
    assert user1["routine_conversation_count"] == 1
    # One merged model entry across all three conversations.
    assert [m["model"] for m in user1["models"]] == ["claude-opus-4-8"]
    assert user1["models"][0]["call_count"] == 3
    assert user1["models"][0]["metrics"]["output_tokens"] == 7_000
    assert user1["total"]["call_count"] == 3
    # Opus 4.8 list price: $5/M in, $25/M out; 100 input tokens per call.
    in_cost = 300 * 5 / 1_000_000
    assert user1["total"]["estimated_cost_usd"] == pytest.approx(
        in_cost + 7_000 * 25 / 1_000_000, abs=1e-9
    )
    assert user1["cost_excluding_routines_usd"] == pytest.approx(
        200 * 5 / 1_000_000 + 3_000 * 25 / 1_000_000, abs=1e-9
    )
    assert user1["cost_routines_usd"] == pytest.approx(
        100 * 5 / 1_000_000 + 4_000 * 25 / 1_000_000, abs=1e-9
    )
    assert user1["known_cost_usd"] == pytest.approx(
        user1["cost_excluding_routines_usd"] + user1["cost_routines_usd"], abs=1e-9
    )

    user2 = result[2]
    assert user2["conversation_count"] == 1
    assert user2["routine_conversation_count"] == 0
    assert user2["cost_routines_usd"] == 0.0
    assert [m["model"] for m in user2["models"]] == ["gemini-3.5-flash"]


def test_usage_by_user_tracks_cost_source_per_split(_isolated_db):
    """Each split and routine entry says whether its figure came from
    provider-reported amounts, list-price estimates, or both."""
    store, models_mod = _isolated_db
    chat, routine_chat = str(uuid.uuid4()), str(uuid.uuid4())

    def _openrouter(conversation_id, cost):
        raw_usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        if cost is not None:
            raw_usage["cost"] = cost
        _run(store.record_api_call(
            conversation_id=conversation_id,
            user_id=1,
            model="openrouter:deepseek/deepseek-v4-flash-0731",
            call_type=models_mod.ApiCallType.TOP_LEVEL,
            input_tokens=1_000_000,
            output_tokens=0,
            duration_ms=1,
            provider="openrouter",
            raw_usage=raw_usage,
        ))

    # Non-routine chat: reported only.
    _openrouter(chat, cost=0.3)
    # Routine chat: one reported OpenRouter call + one estimated Opus call.
    _openrouter(routine_chat, cost=0.2)
    _record_anthropic(store, models_mod, routine_chat, output_tokens=0, user_id=1)

    user = _run(store.get_usage_by_user(
        routine_id_by_conversation={routine_chat: "routine-1"}
    ))[1]
    assert user["cost_excluding_routines_usd"] == pytest.approx(0.3)
    assert user["cost_excluding_routines_source"] == "reported"
    # 100 Opus input tokens at $5/M.
    assert user["cost_routines_usd"] == pytest.approx(0.2 + 0.0005)
    assert user["cost_routines_source"] == "mixed"
    assert user["total"]["cost_source"] == "mixed"
    (routine,) = user["routines"]
    assert routine["cost_usd"] == pytest.approx(0.2005)
    assert routine["cost_source"] == "mixed"
    by_model = {m["model"]: m for m in user["models"]}
    assert by_model["openrouter:deepseek/deepseek-v4-flash-0731"]["cost_source"] == "reported"
    assert by_model["claude-opus-4-8"]["cost_source"] == "estimated"


def test_usage_by_user_unpriced_model_nulls_only_its_split(_isolated_db):
    """An unpriced model poisons its own cost split; the other stays exact."""
    store, models_mod = _isolated_db
    plain_chat, routine_chat = str(uuid.uuid4()), str(uuid.uuid4())

    _record_gemini(store, models_mod, plain_chat, prompt=1_000, candidates=100,
                   model="gemini-unpriced-model", user_id=1)
    _record_anthropic(store, models_mod, routine_chat, output_tokens=1_000, user_id=1)

    result = _run(store.get_usage_by_user(
        routine_id_by_conversation={routine_chat: "routine-1"}
    ))

    user1 = result[1]
    assert user1["cost_excluding_routines_usd"] is None
    assert user1["cost_routines_usd"] == pytest.approx(
        100 * 5 / 1_000_000 + 1_000 * 25 / 1_000_000, abs=1e-9
    )
    # The user-level total keeps the usual any-model-unpriced null.
    assert user1["total"]["estimated_cost_usd"] is None
    # Known cost still counts what CAN be priced.
    assert user1["known_cost_usd"] == pytest.approx(
        user1["cost_routines_usd"], abs=1e-9
    )


def test_usage_by_user_breaks_routine_cost_out_per_routine(_isolated_db):
    """The routine split is itemized per routine, priciest first, with the
    null-on-unpriced convention applied per routine (an unpriced model in one
    routine never hides the other routine's figure)."""
    store, models_mod = _isolated_db
    plain_chat = str(uuid.uuid4())
    cheap_run, pricey_run_a, pricey_run_b, mixed_run = (
        str(uuid.uuid4()) for _ in range(4)
    )

    _record_anthropic(store, models_mod, plain_chat, output_tokens=1_000, user_id=1)
    _record_anthropic(store, models_mod, cheap_run, output_tokens=500, user_id=1)
    # Two runs of the same routine fold into one entry.
    _record_anthropic(store, models_mod, pricey_run_a, output_tokens=5_000, user_id=1)
    _record_anthropic(store, models_mod, pricey_run_b, output_tokens=3_000, user_id=1)
    # A routine with one priced + one unpriced model: null cost, but its
    # known cost still ranks it.
    _record_anthropic(store, models_mod, mixed_run, output_tokens=2_000, user_id=1)
    _record_gemini(store, models_mod, mixed_run, prompt=10, candidates=10,
                   model="gemini-unpriced-model", user_id=1)

    result = _run(store.get_usage_by_user(routine_id_by_conversation={
        cheap_run: "routine-cheap",
        pricey_run_a: "routine-pricey",
        pricey_run_b: "routine-pricey",
        mixed_run: "routine-mixed",
    }))

    user1 = result[1]
    assert user1["conversation_count"] == 1
    assert user1["routine_conversation_count"] == 4
    # The unpriced model sits in the routine split, so that split nulls
    # while the non-routine split stays exact.
    assert user1["cost_routines_usd"] is None
    assert user1["cost_excluding_routines_usd"] == pytest.approx(
        100 * 5 / 1_000_000 + 1_000 * 25 / 1_000_000, abs=1e-9
    )

    def opus_cost(calls, output_tokens):
        return calls * 100 * 5 / 1_000_000 + output_tokens * 25 / 1_000_000

    routines = user1["routines"]
    assert [r["routine_id"] for r in routines] == [
        "routine-pricey", "routine-mixed", "routine-cheap",
    ]
    pricey, mixed, cheap = routines
    assert pricey["conversation_count"] == 2
    assert pricey["cost_usd"] == pytest.approx(opus_cost(2, 8_000), abs=1e-9)
    assert pricey["known_cost_usd"] == pytest.approx(pricey["cost_usd"], abs=1e-9)
    assert mixed["conversation_count"] == 1
    assert mixed["cost_usd"] is None
    assert mixed["known_cost_usd"] == pytest.approx(opus_cost(1, 2_000), abs=1e-9)
    assert cheap["conversation_count"] == 1
    assert cheap["cost_usd"] == pytest.approx(opus_cost(1, 500), abs=1e-9)
    # The itemized known costs add up to the user's routine share of the
    # ranking key.
    assert sum(r["known_cost_usd"] for r in routines) == pytest.approx(
        user1["known_cost_usd"] - user1["cost_excluding_routines_usd"], abs=1e-9
    )


def test_usage_by_user_no_routines_yields_empty_breakdown(_isolated_db):
    store, models_mod = _isolated_db
    _record_anthropic(store, models_mod, str(uuid.uuid4()), output_tokens=10, user_id=3)

    result = _run(store.get_usage_by_user())

    assert result[3]["routines"] == []
    assert result[3]["cost_routines_usd"] == 0.0


def test_usage_by_user_date_range_filters_calls(_isolated_db):
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    old_row = _record_anthropic(store, models_mod, conv, output_tokens=100_000, user_id=7)
    _record_anthropic(store, models_mod, conv, output_tokens=1_000, user_id=7)
    _set_created_at(store, models_mod.LlmCallAnthropic, old_row["id"],
                    datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))

    result = _run(store.get_usage_by_user(
        start=datetime(2026, 6, 1, tzinfo=timezone.utc)
    ))

    assert set(result) == {7}
    assert result[7]["total"]["call_count"] == 1
    assert result[7]["models"][0]["metrics"]["output_tokens"] == 1_000
    assert result[7]["conversation_count"] == 1


# ---------------------------------------------------------------------------
# get_usage_by_model
# ---------------------------------------------------------------------------

def test_usage_by_model_folds_users_conversations_and_splits(_isolated_db):
    """Per-model fold: one row per model merged across conversations and
    call types, distinct user/conversation counts, the routine share from
    the conversation mapping and the sub-agent share from call_type."""
    store, models_mod = _isolated_db
    chat_a, chat_b, routine_chat, gemini_chat = (str(uuid.uuid4()) for _ in range(4))
    sub_agent = models_mod.ApiCallType.SUB_AGENT

    # Opus: user 1 in two chats (one with a sub-agent call), user 2 in a
    # routine chat -- 4 calls, 2 users, 3 conversations.
    _record_anthropic(store, models_mod, chat_a, output_tokens=1_000, user_id=1)
    _record_anthropic(store, models_mod, chat_a, output_tokens=2_000, user_id=1,
                      call_type=sub_agent)
    _record_anthropic(store, models_mod, chat_b, output_tokens=3_000, user_id=1)
    _record_anthropic(store, models_mod, routine_chat, output_tokens=4_000, user_id=2)
    # Gemini: a sub-agent spawned inside the Opus chat plus user 3's own chat.
    _record_gemini(store, models_mod, chat_a, prompt=500, candidates=50, user_id=1,
                   call_type=sub_agent)
    _record_gemini(store, models_mod, gemini_chat, prompt=500, candidates=50, user_id=3)

    result = _run(store.get_usage_by_model(
        routine_id_by_conversation={routine_chat: "routine-1"}
    ))

    assert [m["model"] for m in result] == ["claude-opus-4-8", "gemini-3.5-flash"]
    opus = result[0]
    assert opus["provider"] == "anthropic"
    assert opus["user_count"] == 2
    assert opus["conversation_count"] == 3
    assert opus["usage"]["call_count"] == 4
    assert opus["usage"]["metrics"]["output_tokens"] == 10_000
    assert "call_type" not in opus["usage"]
    # Opus 4.8 list price: $5/M in, $25/M out; 100 input tokens per call.
    def opus_cost(calls, out):
        return calls * 100 * 5 / 1_000_000 + out * 25 / 1_000_000
    assert opus["usage"]["estimated_cost_usd"] == pytest.approx(opus_cost(4, 10_000))
    assert opus["known_cost_usd"] == pytest.approx(opus_cost(4, 10_000))
    assert opus["cost_routines_usd"] == pytest.approx(opus_cost(1, 4_000))
    assert opus["cost_routines_source"] == "estimated"
    assert opus["cost_subagents_usd"] == pytest.approx(opus_cost(1, 2_000))
    assert opus["cost_subagents_source"] == "estimated"
    # Top users ranked by cost: user 1 ($ for 3 calls / 6K out) over user 2.
    assert [(u["user_id"], u["call_count"]) for u in opus["users"]] == [(1, 3), (2, 1)]
    assert opus["users"][0]["cost_usd"] == pytest.approx(opus_cost(3, 6_000))
    assert opus["users"][0]["cost_source"] == "estimated"

    gemini = result[1]
    assert gemini["user_count"] == 2
    assert gemini["conversation_count"] == 2
    assert gemini["usage"]["call_count"] == 2
    assert gemini["cost_routines_usd"] == 0.0
    assert gemini["cost_routines_source"] is None
    # The sub-agent call inside the Opus chat lands on the Gemini row.
    assert gemini["cost_subagents_usd"] == pytest.approx(
        gemini["usage"]["estimated_cost_usd"] / 2
    )


def test_usage_by_model_unpriced_model_nulls_its_own_row_only(_isolated_db):
    store, models_mod = _isolated_db
    plain_chat, other_chat = str(uuid.uuid4()), str(uuid.uuid4())
    _record_gemini(store, models_mod, plain_chat, prompt=1_000, candidates=100,
                   model="gemini-unpriced-model", user_id=1,
                   call_type=models_mod.ApiCallType.SUB_AGENT)
    _record_anthropic(store, models_mod, other_chat, output_tokens=1_000, user_id=1)

    result = _run(store.get_usage_by_model(
        routine_id_by_conversation={plain_chat: "routine-1"}
    ))

    # The priced model ranks first; the unpriced one has no known cost.
    assert [m["model"] for m in result] == ["claude-opus-4-8", "gemini-unpriced-model"]
    unpriced = result[1]
    assert unpriced["usage"]["estimated_cost_usd"] is None
    assert unpriced["known_cost_usd"] == 0.0
    assert unpriced["cost_routines_usd"] is None
    assert unpriced["cost_subagents_usd"] is None
    assert unpriced["users"][0]["cost_usd"] is None
    assert unpriced["user_count"] == 1
    assert result[0]["usage"]["estimated_cost_usd"] is not None


def test_usage_by_model_caps_top_users_and_filters_range(_isolated_db):
    store, models_mod = _isolated_db
    for user_id in range(1, 5):
        _record_anthropic(store, models_mod, str(uuid.uuid4()),
                          output_tokens=user_id * 1_000, user_id=user_id)
    old_conv = str(uuid.uuid4())
    old_row = _record_anthropic(store, models_mod, old_conv, output_tokens=100_000,
                                user_id=9)
    _set_created_at(store, models_mod.LlmCallAnthropic, old_row["id"],
                    datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))

    (opus,) = _run(store.get_usage_by_model(
        start=datetime(2026, 6, 1, tzinfo=timezone.utc), top_users=2
    ))

    # The backdated call is outside the window: 4 users, priciest two listed.
    assert opus["user_count"] == 4
    assert opus["usage"]["call_count"] == 4
    assert [u["user_id"] for u in opus["users"]] == [4, 3]


def test_usage_by_model_empty_window(_isolated_db):
    store, _models_mod = _isolated_db
    assert _run(store.get_usage_by_model()) == []


def test_collect_usage_buckets_default_key_unchanged(_isolated_db):
    """Without ``by_call_type`` the bucket key stays (conversation, model)
    and entries carry no call_type -- the existing callers' contract."""
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())
    _record_anthropic(store, models_mod, conv, output_tokens=10, user_id=1)
    _record_anthropic(store, models_mod, conv, output_tokens=10, user_id=1,
                      call_type=models_mod.ApiCallType.SUB_AGENT)

    by_key, user_ids = _run(store._collect_usage_buckets())
    assert set(by_key) == {(conv, "claude-opus-4-8")}
    assert by_key[(conv, "claude-opus-4-8")]["call_count"] == 2
    assert "call_type" not in by_key[(conv, "claude-opus-4-8")]
    assert user_ids == {conv: 1}

    by_key, _ = _run(store._collect_usage_buckets(by_call_type=True))
    assert set(by_key) == {
        (conv, "claude-opus-4-8", "top_level"),
        (conv, "claude-opus-4-8", "sub_agent"),
    }


# ---------------------------------------------------------------------------
# get_latest_context_tokens_for_conversations
# ---------------------------------------------------------------------------

def test_latest_context_batched_per_provider_formulas(_isolated_db):
    store, models_mod = _isolated_db
    gemini_conv = str(uuid.uuid4())
    anthropic_conv = str(uuid.uuid4())
    empty_conv = str(uuid.uuid4())

    # Gemini: context = latest top-level call's prompt_token_count; a later
    # sub-agent call must not mask it.
    _record_gemini(store, models_mod, gemini_conv, prompt=10_000, candidates=100)
    _record_gemini(store, models_mod, gemini_conv, prompt=25_000, candidates=100)
    _record_gemini(store, models_mod, gemini_conv, prompt=500, candidates=10,
                   call_type=models_mod.ApiCallType.SUB_AGENT)

    # Anthropic: context = input + cache_read + cache_creation of the latest
    # top-level call.
    _run(store.record_api_call(
        conversation_id=anthropic_conv,
        user_id=1,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=1_000,
        output_tokens=50,
        duration_ms=10,
        provider="anthropic",
        raw_usage={
            "input_tokens": 1_000,
            "output_tokens": 50,
            "cache_read_input_tokens": 40_000,
            "cache_creation_input_tokens": 2_000,
        },
    ))

    result = _run(store.get_latest_context_tokens_for_conversations(
        [gemini_conv, anthropic_conv, empty_conv]
    ))

    assert result[gemini_conv] == 25_000
    assert result[anthropic_conv] == 1_000 + 40_000 + 2_000
    assert empty_conv not in result
    assert _run(store.get_latest_context_tokens_for_conversations([])) == {}


def test_latest_context_prefers_newer_call_across_providers(_isolated_db):
    """A model switch mid-conversation: the later provider's call wins."""
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    gemini_row = _record_gemini(store, models_mod, conv, prompt=90_000, candidates=100)
    anthropic_row = _record_anthropic(store, models_mod, conv, output_tokens=10,
                                      input_tokens=30_000)
    _set_created_at(store, models_mod.LlmCallGemini, gemini_row["id"],
                    datetime(2026, 3, 1, tzinfo=timezone.utc))
    _set_created_at(store, models_mod.LlmCallAnthropic, anthropic_row["id"],
                    datetime(2026, 3, 2, tzinfo=timezone.utc))

    result = _run(store.get_latest_context_tokens_for_conversations([conv]))
    assert result[conv] == 30_000

    # Flip the order: now the Gemini call is the latest.
    _set_created_at(store, models_mod.LlmCallAnthropic, anthropic_row["id"],
                    datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = _run(store.get_latest_context_tokens_for_conversations([conv]))
    assert result[conv] == 90_000


# ---------------------------------------------------------------------------
# ChatStorage.count_user_message_active_days
# ---------------------------------------------------------------------------

def _write_chat_history(tmp_path: Path, messages: list[dict]) -> Path:
    chat_file = tmp_path / "chat_history.json"
    chat_file.write_text(json.dumps({"messages": messages}))
    return chat_file


def test_active_days_counts_distinct_user_message_dates(tmp_path, monkeypatch):
    from chat.storage import ChatStorage

    chat_file = _write_chat_history(tmp_path, [
        {"role": "user", "content": "a", "timestamp": "2026-08-01T09:00:00+00:00"},
        {"role": "user", "content": "b", "timestamp": "2026-08-01T17:30:00+00:00"},
        {"role": "assistant", "content": "r", "timestamp": "2026-08-02T10:00:00+00:00"},
        {"role": "user", "content": "c", "timestamp": "2026-08-05T08:00:00+00:00"},
        # Malformed rows must not break the count.
        {"role": "user", "content": "no timestamp"},
        "not-a-dict",
    ])
    monkeypatch.setattr(
        ChatStorage, "_get_chat_history_file", staticmethod(lambda _cid: chat_file)
    )

    # Two user days (Aug 1, Aug 5); the assistant-only Aug 2 does not count.
    assert ChatStorage.count_user_message_active_days("conv") == 2


def test_active_days_missing_history_counts_zero(tmp_path, monkeypatch):
    from chat.storage import ChatStorage

    monkeypatch.setattr(
        ChatStorage,
        "_get_chat_history_file",
        staticmethod(lambda _cid: tmp_path / "does-not-exist.json"),
    )
    assert ChatStorage.count_user_message_active_days("conv") == 0
    assert ChatStorage.user_message_active_days("conv") == set()


def test_active_days_set_clips_to_inclusive_range(tmp_path, monkeypatch):
    """The ranged variant returns the day set clipped to [start, end]."""
    from chat.storage import ChatStorage

    chat_file = _write_chat_history(tmp_path, [
        {"role": "user", "content": "a", "timestamp": "2026-08-01T09:00:00+00:00"},
        {"role": "user", "content": "b", "timestamp": "2026-08-05T08:00:00+00:00"},
        {"role": "user", "content": "c", "timestamp": "2026-08-09T23:59:59+00:00"},
    ])
    monkeypatch.setattr(
        ChatStorage, "_get_chat_history_file", staticmethod(lambda _cid: chat_file)
    )

    assert ChatStorage.user_message_active_days("conv") == {
        "2026-08-01", "2026-08-05", "2026-08-09",
    }
    # Both bounds are inclusive ISO dates.
    assert ChatStorage.user_message_active_days(
        "conv", "2026-08-05", "2026-08-09"
    ) == {"2026-08-05", "2026-08-09"}
    assert ChatStorage.user_message_active_days("conv", None, "2026-08-04") == {
        "2026-08-01",
    }
    assert ChatStorage.user_message_active_days("conv", "2026-08-10", None) == set()
