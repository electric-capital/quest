"""Tests for the admin Total Usage report.

1. ``build_usage_report()`` in chat/usage_report.py -- the pure fold of
   per-day usage buckets into rolling windows with previous-period
   companions, the lifetime total and the daily/weekly/monthly series.
2. ``get_daily_usage_buckets()`` / the ``by_day`` split of
   ``_collect_usage_buckets()`` in db/llm_call_store.py -- the per-UTC-day
   grouped query feeding it.

Store tests use the isolated-SQLite convention of
tests/test_system_reports_analytics.py.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from importlib import reload

import pytest
from sqlalchemy import update

from chat.usage_report import (
    DAILY_SERIES_DAYS,
    MONTHLY_SERIES_MONTHS,
    WEEKLY_SERIES_WEEKS,
    WINDOW_DAYS,
    build_usage_report,
)

# A Wednesday, so the weekly (Monday-start) bucketing is observable.
NOW = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)

BUCKET_KEYS = {
    "start", "end", "conversations", "routine_conversations", "active_users",
    "new_users", "call_count", "total_tokens", "cost_usd", "cost_source",
    "known_cost_usd",
}


def _bucket(day, conv="c1", user=1, cost=1.0, source="estimated", tokens=100, calls=1):
    return {
        "day": day,
        "conversation_id": conv,
        "user_id": user,
        "model": "m",
        "provider": "anthropic",
        "call_count": calls,
        "total_tokens": tokens,
        "cost_usd": cost,
        "cost_source": source if cost is not None else None,
    }


def _window(report, days):
    return next(w for w in report["windows"] if w["days"] == days)


# ---------------------------------------------------------------------------
# build_usage_report
# ---------------------------------------------------------------------------

def test_empty_report_is_zero_filled_and_gap_free():
    report = build_usage_report([], set(), {}, now=NOW)

    assert report["today"] == "2026-10-07"
    assert [w["days"] for w in report["windows"]] == list(WINDOW_DAYS)
    for window in report["windows"]:
        for side in ("current", "previous"):
            assert set(window[side]) == BUCKET_KEYS
            assert window[side]["conversations"] == 0
            assert window[side]["cost_usd"] == 0.0
            assert window[side]["cost_source"] is None
    assert len(report["series"]["daily"]) == DAILY_SERIES_DAYS
    assert len(report["series"]["weekly"]) == WEEKLY_SERIES_WEEKS
    assert len(report["series"]["monthly"]) == MONTHLY_SERIES_MONTHS
    assert report["lifetime"]["start"] == "2026-10-07"
    assert report["lifetime"]["end"] == "2026-10-07"


def test_window_bounds_include_today_and_previous_period_abuts():
    report = build_usage_report([], set(), {}, now=NOW)

    today = _window(report, 1)
    assert (today["current"]["start"], today["current"]["end"]) == ("2026-10-07", "2026-10-07")
    assert (today["previous"]["start"], today["previous"]["end"]) == ("2026-10-06", "2026-10-06")

    week = _window(report, 7)
    assert (week["current"]["start"], week["current"]["end"]) == ("2026-10-01", "2026-10-07")
    assert (week["previous"]["start"], week["previous"]["end"]) == ("2026-09-24", "2026-09-30")


def test_rows_land_in_current_or_previous_window_only():
    rows = [
        _bucket("2026-10-07", conv="today"),
        _bucket("2026-10-01", conv="edge-current"),    # first day of last 7
        _bucket("2026-09-30", conv="edge-previous"),   # last day of prior 7
        _bucket("2026-09-24", conv="first-previous"),
        _bucket("2026-09-23", conv="outside"),
    ]
    report = build_usage_report(rows, set(), {}, now=NOW)

    week = _window(report, 7)
    assert week["current"]["conversations"] == 2
    assert week["previous"]["conversations"] == 2
    # The 30-day window swallows all of them.
    month = _window(report, 30)
    assert month["current"]["conversations"] == 5
    assert month["previous"]["conversations"] == 0
    assert report["lifetime"]["conversations"] == 5
    assert report["lifetime"]["start"] == "2026-09-23"


def test_distinct_counts_across_days_and_routine_split():
    rows = [
        # One conversation active on two days counts once; two users.
        _bucket("2026-10-06", conv="c1", user=1),
        _bucket("2026-10-07", conv="c1", user=1),
        _bucket("2026-10-07", conv="c2", user=2),
        # A routine run by user 3: counts as a conversation, not as an
        # active user.
        _bucket("2026-10-07", conv="r1", user=3),
    ]
    report = build_usage_report(rows, {"r1"}, {}, now=NOW)

    current = _window(report, 7)["current"]
    assert current["conversations"] == 3
    assert current["routine_conversations"] == 1
    assert current["active_users"] == 2
    assert current["call_count"] == 4
    assert current["total_tokens"] == 400


def test_unpriced_model_nulls_cost_but_keeps_known_portion():
    rows = [
        _bucket("2026-10-07", conv="c1", cost=2.0),
        _bucket("2026-10-07", conv="c2", cost=None),
        _bucket("2026-10-06", conv="c3", cost=0.5, source="reported"),
    ]
    report = build_usage_report(rows, set(), {}, now=NOW)

    today = _window(report, 1)
    assert today["current"]["cost_usd"] is None
    assert today["current"]["cost_source"] is None
    assert today["current"]["known_cost_usd"] == 2.0
    # Yesterday had only reported spend.
    assert today["previous"]["cost_usd"] == 0.5
    assert today["previous"]["cost_source"] == "reported"

    # The series point for today is partial too, yesterday's intact.
    daily = report["series"]["daily"]
    assert daily[-1]["start"] == "2026-10-07"
    assert daily[-1]["cost_usd"] is None
    assert daily[-1]["known_cost_usd"] == 2.0
    assert daily[-2]["cost_usd"] == 0.5


def test_mixed_sources_degrade_to_mixed():
    rows = [
        _bucket("2026-10-07", conv="c1", cost=1.0, source="estimated"),
        _bucket("2026-10-07", conv="c2", cost=1.0, source="reported"),
    ]
    report = build_usage_report(rows, set(), {}, now=NOW)
    assert _window(report, 1)["current"]["cost_source"] == "mixed"
    assert _window(report, 1)["current"]["cost_usd"] == 2.0


def test_weekly_series_buckets_monday_start_weeks_ending_with_this_week():
    rows = [
        _bucket("2026-10-05", conv="mon"),   # Monday of the current week
        _bucket("2026-10-07", conv="wed"),   # today
        _bucket("2026-10-04", conv="sun"),   # Sunday of the previous week
    ]
    report = build_usage_report(rows, set(), {}, now=NOW)

    weekly = report["series"]["weekly"]
    assert (weekly[-1]["start"], weekly[-1]["end"]) == ("2026-10-05", "2026-10-11")
    assert weekly[-1]["conversations"] == 2
    assert (weekly[-2]["start"], weekly[-2]["end"]) == ("2026-09-28", "2026-10-04")
    assert weekly[-2]["conversations"] == 1
    # Contiguous: every period starts the day after the previous ended.
    for prev, cur in zip(weekly, weekly[1:]):
        assert datetime.fromisoformat(cur["start"]).date() - datetime.fromisoformat(prev["end"]).date() == timedelta(days=1)


def test_monthly_series_uses_calendar_months_and_drops_older_rows():
    rows = [
        _bucket("2026-10-01", conv="this-month"),
        _bucket("2026-09-30", conv="last-month"),
        _bucket("2024-10-15", conv="too-old"),   # 25 months back
    ]
    report = build_usage_report(rows, set(), {}, now=NOW)

    monthly = report["series"]["monthly"]
    assert (monthly[-1]["start"], monthly[-1]["end"]) == ("2026-10-01", "2026-10-31")
    assert monthly[-1]["conversations"] == 1
    assert (monthly[-2]["start"], monthly[-2]["end"]) == ("2026-09-01", "2026-09-30")
    assert monthly[-2]["conversations"] == 1
    assert monthly[0]["start"] == "2024-11-01"
    assert sum(p["conversations"] for p in monthly) == 2
    # Lifetime still sees the old row.
    assert report["lifetime"]["conversations"] == 3
    assert report["lifetime"]["start"] == "2024-10-15"


def test_daily_series_covers_exactly_the_last_n_days():
    first = (NOW - timedelta(days=DAILY_SERIES_DAYS - 1)).date().isoformat()
    before = (NOW - timedelta(days=DAILY_SERIES_DAYS)).date().isoformat()
    rows = [_bucket(first, conv="first"), _bucket(before, conv="before")]
    report = build_usage_report(rows, set(), {}, now=NOW)

    daily = report["series"]["daily"]
    assert daily[0]["start"] == first
    assert daily[0]["conversations"] == 1
    assert daily[-1]["start"] == "2026-10-07"
    assert sum(p["conversations"] for p in daily) == 1


def test_new_users_counted_by_signup_day_per_bucket():
    signups = {
        1: datetime(2026, 10, 7, 1, 0),                       # today (naive UTC)
        2: datetime(2026, 10, 6, 23, 59, tzinfo=timezone.utc),  # yesterday
        3: datetime(2025, 1, 1),                              # long ago
    }
    report = build_usage_report([], set(), signups, now=NOW)

    today = _window(report, 1)
    assert today["current"]["new_users"] == 1
    assert today["previous"]["new_users"] == 1
    assert _window(report, 365)["current"]["new_users"] == 2
    assert report["lifetime"]["new_users"] == 3
    # Lifetime starts at the earliest signup when no calls precede it.
    assert report["lifetime"]["start"] == "2025-01-01"
    assert report["series"]["daily"][-1]["new_users"] == 1


# ---------------------------------------------------------------------------
# get_daily_usage_buckets (store)
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_usage_report_test_")
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


def _record_anthropic(store, models_mod, conversation_id, output_tokens, user_id=1):
    """One priced Anthropic call; returns the row id."""
    return _run(store.record_api_call(
        conversation_id=conversation_id,
        user_id=user_id,
        model="claude-opus-4-8",
        call_type=models_mod.ApiCallType.TOP_LEVEL,
        input_tokens=100,
        output_tokens=output_tokens,
        duration_ms=10,
        provider="anthropic",
        raw_usage={
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    ))["id"]


def _set_created_at(store, row_cls, row_id, dt):
    async def _do():
        async with store.AsyncSessionLocal() as db:
            await db.execute(
                update(row_cls).where(row_cls.id == row_id).values(created_at=dt)
            )
            await db.commit()
    _run(_do())


def test_daily_buckets_split_on_utc_day_and_merge_within_a_day(_isolated_db):
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())

    first = _record_anthropic(store, models_mod, conv, output_tokens=1_000, user_id=7)
    second = _record_anthropic(store, models_mod, conv, output_tokens=1_000, user_id=7)
    third = _record_anthropic(store, models_mod, conv, output_tokens=2_000, user_id=7)
    _set_created_at(store, models_mod.LlmCallAnthropic, first, datetime(2026, 3, 1, 23, 59, tzinfo=timezone.utc))
    _set_created_at(store, models_mod.LlmCallAnthropic, second, datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
    _set_created_at(store, models_mod.LlmCallAnthropic, third, datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc))

    rows = _run(store.get_daily_usage_buckets())

    by_day = {r["day"]: r for r in rows}
    assert set(by_day) == {"2026-03-01", "2026-03-02"}
    assert by_day["2026-03-01"]["call_count"] == 2
    assert by_day["2026-03-02"]["call_count"] == 1
    for row in rows:
        assert row["conversation_id"] == conv
        assert row["user_id"] == 7
        assert row["model"] == "claude-opus-4-8"
        assert row["provider"] == "anthropic"
        assert row["cost_source"] == "estimated"
    # Opus 4.8 output at $25/M: 2K tokens on each day (+ 100 in each call).
    assert by_day["2026-03-01"]["cost_usd"] == pytest.approx(0.05 + 2 * 0.0005, abs=1e-6)
    assert by_day["2026-03-02"]["cost_usd"] == pytest.approx(0.05 + 0.0005, abs=1e-6)


def test_daily_buckets_respect_window(_isolated_db):
    store, models_mod = _isolated_db
    conv = str(uuid.uuid4())
    inside = _record_anthropic(store, models_mod, conv, output_tokens=10)
    outside = _record_anthropic(store, models_mod, conv, output_tokens=10)
    _set_created_at(store, models_mod.LlmCallAnthropic, inside, datetime(2026, 5, 10, tzinfo=timezone.utc))
    _set_created_at(store, models_mod.LlmCallAnthropic, outside, datetime(2026, 5, 20, tzinfo=timezone.utc))

    rows = _run(store.get_daily_usage_buckets(
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 15, tzinfo=timezone.utc),
    ))
    assert [r["day"] for r in rows] == ["2026-05-10"]
