"""Instance-wide usage report (System Reports > Total Usage).

Folds the per-day usage buckets of ``get_daily_usage_buckets()`` into the
figures the Total Usage section shows: a comparison matrix of rolling
windows (today, last 7 / 30 / 90 / 365 days) each paired with the
preceding period of the same length, a lifetime total, and three
time series (daily / weekly / monthly) for the charts. Every figure is a
``bucket`` with the same keys so the frontend renders cells and chart
points alike.

Metric definitions (all keyed on the UTC calendar day of each LLM call,
the timezone the ``llm_calls_*`` rows are stamped in):

- ``conversations`` -- distinct conversations with at least one recorded
  call in the period, routine runs included (``routine_conversations`` is
  the routine-created subset, split on the surviving conversation rows'
  ``routine_id``; calls of deleted conversations count as non-routine).
- ``active_users`` -- distinct users with at least one call in a
  NON-routine conversation in the period. A scheduled routine running on
  someone's behalf is not that person using Quest, so routine-only
  activity does not make a user active.
- ``new_users`` -- users whose account was created in the period
  (``users.created_at``; deleted users are gone, so this undercounts
  historically).
- ``call_count`` / ``total_tokens`` -- the coarse cross-provider magnitudes
  of the other reports.
- ``cost_usd`` / ``cost_source`` -- the repo-wide null-on-unpriced
  convention: ``None`` when any model in the period lacks a pricing entry
  and reported no amount, with the usual reported / estimated / mixed
  provenance. ``known_cost_usd`` is the priceable portion (never None) so
  the charts can still plot a period that contains an unpriced model, and
  the UI marks such points as partial.

Window semantics match ``ReportDateRange``'s presets: "last N days"
includes today, so the current window is ``[today-N+1, today]`` and the
previous one the N days right before it. The weekly series buckets on
Monday-start ISO weeks and the monthly one on calendar months; both end
with the in-progress period that contains today.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from db.llm_call_store import add_cost

# Rolling windows in the comparison matrix, in display order. 1 = today
# (vs yesterday).
WINDOW_DAYS = (1, 7, 30, 90, 365)

# Time-series lengths, in periods, each ending with the period that
# contains today.
DAILY_SERIES_DAYS = 90
WEEKLY_SERIES_WEEKS = 52
MONTHLY_SERIES_MONTHS = 24


def _new_bucket() -> dict:
    return {
        "conversations": set(),
        "routine_conversations": set(),
        "active_users": set(),
        "new_users": 0,
        "call_count": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": None,
        "known_cost_usd": 0.0,
    }


def _fold_row(bucket: dict, row: dict, is_routine: bool) -> None:
    bucket["conversations"].add(row["conversation_id"])
    if is_routine:
        bucket["routine_conversations"].add(row["conversation_id"])
    else:
        bucket["active_users"].add(row["user_id"])
    bucket["call_count"] += row["call_count"]
    bucket["total_tokens"] += row["total_tokens"]
    cost = row["cost_usd"]
    if cost is not None:
        bucket["known_cost_usd"] += cost
    add_cost(bucket, "cost_usd", "cost_source", cost, row["cost_source"])


def _finish_bucket(bucket: dict, start: date, end: date, new_users: int) -> dict:
    """Serialize an accumulator: sets become counts, floats get rounded."""
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "conversations": len(bucket["conversations"]),
        "routine_conversations": len(bucket["routine_conversations"]),
        "active_users": len(bucket["active_users"]),
        "new_users": new_users,
        "call_count": bucket["call_count"],
        "total_tokens": bucket["total_tokens"],
        "cost_usd": (
            None if bucket["cost_usd"] is None else round(bucket["cost_usd"], 6)
        ),
        "cost_source": bucket["cost_source"],
        "known_cost_usd": round(bucket["known_cost_usd"], 6),
    }


def _as_utc_date(value: datetime) -> date:
    """Timestamps are stored naive-UTC; aware ones (tests) are converted."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.date()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(day: date, months: int) -> date:
    """First day of the month ``months`` after ``day``'s month."""
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _series_periods(today: date) -> dict[str, tuple[list[tuple[date, date]], Callable[[date], Optional[int]]]]:
    """Per granularity: the ordered (start, end) periods plus a day -> index map.

    The index function returns None for days outside the series (older than
    its first period, or in the future).
    """
    first_day = today - timedelta(days=DAILY_SERIES_DAYS - 1)
    daily = [(d, d) for d in (first_day + timedelta(days=i) for i in range(DAILY_SERIES_DAYS))]

    def daily_index(day: date) -> Optional[int]:
        i = (day - first_day).days
        return i if 0 <= i < DAILY_SERIES_DAYS else None

    this_week = _week_start(today)
    first_week = this_week - timedelta(weeks=WEEKLY_SERIES_WEEKS - 1)
    weekly = [
        (first_week + timedelta(weeks=i), first_week + timedelta(weeks=i, days=6))
        for i in range(WEEKLY_SERIES_WEEKS)
    ]

    def weekly_index(day: date) -> Optional[int]:
        i = (_week_start(day) - first_week).days // 7
        return i if 0 <= i < WEEKLY_SERIES_WEEKS else None

    this_month = _month_start(today)
    first_month = _add_months(this_month, -(MONTHLY_SERIES_MONTHS - 1))
    monthly = [
        (_add_months(first_month, i), _add_months(first_month, i + 1) - timedelta(days=1))
        for i in range(MONTHLY_SERIES_MONTHS)
    ]

    def monthly_index(day: date) -> Optional[int]:
        i = (day.year - first_month.year) * 12 + (day.month - first_month.month)
        return i if 0 <= i < MONTHLY_SERIES_MONTHS else None

    return {
        "daily": (daily, daily_index),
        "weekly": (weekly, weekly_index),
        "monthly": (monthly, monthly_index),
    }


def build_usage_report(
    buckets: list[dict],
    routine_conversation_ids: set[str],
    user_signup_dates: dict[int, datetime],
    now: Optional[datetime] = None,
) -> dict:
    """Fold per-day usage buckets into the Total Usage response (pure function).

    Args:
        buckets: ``get_daily_usage_buckets()`` output (all time).
        routine_conversation_ids: ids of surviving conversations created by
            a routine (``conversations.routine_id`` set).
        user_signup_dates: ``list_user_signup_dates()`` output.
        now: Reference instant for "today" (tests inject it); UTC.

    Returns:
        {
            "generated_at": ISO str,
            "today": "YYYY-MM-DD",
            "windows": [  # one per WINDOW_DAYS, in order
                {"days": N,
                 "current": bucket,    # [today-N+1, today]
                 "previous": bucket},  # [today-2N+1, today-N]
                ...
            ],
            "lifetime": bucket,  # every recorded call; start = earliest day
            "series": {
                "daily": [bucket, ...],    # DAILY_SERIES_DAYS points
                "weekly": [bucket, ...],   # WEEKLY_SERIES_WEEKS points
                "monthly": [bucket, ...],  # MONTHLY_SERIES_MONTHS points
            },
        }
        where every bucket is ``{"start", "end", "conversations",
        "routine_conversations", "active_users", "new_users",
        "call_count", "total_tokens", "cost_usd", "cost_source",
        "known_cost_usd"}`` (inclusive ISO dates; empty periods are
        zero-filled so the series are gap-free).
    """
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).date()

    # Window ranges: (start, end) inclusive, per window and side.
    window_ranges = []
    for days in WINDOW_DAYS:
        current = (today - timedelta(days=days - 1), today)
        previous = (today - timedelta(days=2 * days - 1), today - timedelta(days=days))
        window_ranges.append((days, current, previous))
    window_acc = [(_new_bucket(), _new_bucket()) for _ in WINDOW_DAYS]

    lifetime_acc = _new_bucket()
    earliest_day: Optional[date] = None

    series_specs = _series_periods(today)
    series_acc = {
        name: [_new_bucket() for _ in periods]
        for name, (periods, _index) in series_specs.items()
    }

    for row in buckets:
        day = date.fromisoformat(row["day"])
        is_routine = row["conversation_id"] in routine_conversation_ids

        _fold_row(lifetime_acc, row, is_routine)
        if earliest_day is None or day < earliest_day:
            earliest_day = day

        for (_days, current, previous), (cur_acc, prev_acc) in zip(window_ranges, window_acc):
            if current[0] <= day <= current[1]:
                _fold_row(cur_acc, row, is_routine)
            elif previous[0] <= day <= previous[1]:
                _fold_row(prev_acc, row, is_routine)

        for name, (_periods, index_of) in series_specs.items():
            i = index_of(day)
            if i is not None:
                _fold_row(series_acc[name][i], row, is_routine)

    signup_days = sorted(_as_utc_date(ts) for ts in user_signup_dates.values() if ts)

    def new_users_in(start: date, end: date) -> int:
        return sum(1 for d in signup_days if start <= d <= end)

    windows = [
        {
            "days": days,
            "current": _finish_bucket(cur_acc, *current, new_users_in(*current)),
            "previous": _finish_bucket(prev_acc, *previous, new_users_in(*previous)),
        }
        for (days, current, previous), (cur_acc, prev_acc)
        in zip(window_ranges, window_acc)
    ]

    lifetime_start = min(
        [d for d in (earliest_day, signup_days[0] if signup_days else None) if d],
        default=today,
    )
    lifetime = _finish_bucket(lifetime_acc, lifetime_start, today, len(signup_days))

    series = {
        name: [
            _finish_bucket(acc, start, end, new_users_in(start, end))
            for acc, (start, end) in zip(series_acc[name], periods)
        ]
        for name, (periods, _index) in series_specs.items()
    }

    return {
        "generated_at": now.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "today": today.isoformat(),
        "windows": windows,
        "lifetime": lifetime,
        "series": series,
    }
