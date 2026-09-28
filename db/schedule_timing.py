"""Pure occurrence math for routine schedules.

An *anchored* schedule (hourly / daily / weekly) has a fixed set of
wall-clock occurrences: every hour at ``hourly_minute`` (UTC), every day at
``daily_time_local`` in ``timezone``, or on the chosen ``weekly_days`` at
``daily_time_local`` in ``timezone``. The scheduler stores the next
occurrence as ``routine_schedules.next_due_at`` and fires when the clock
passes it, so a poll that arrives late -- e.g. because the server was down
at the scheduled minute -- still finds the run instead of missing a narrow
match window.

``every_n_minutes`` schedules are not anchored: they are relative to the
previous run and never need catching up, so none of these helpers apply to
them.

Local times are resolved with :mod:`zoneinfo` per occurrence date, so DST is
handled directly: a local time that falls in a spring-forward gap maps to the
instant one gap-length later (09:30 never skips a day), and a time repeated
by a fall-back fold uses its first occurrence.

All returned datetimes are timezone-aware UTC. No I/O, no DB access.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ANCHORED_TYPES = ("hourly", "daily", "weekly")
SCHEDULE_TYPES = ("daily", "weekly", "hourly", "every_n_minutes")

# How late an anchored occurrence may still start. Past this, the occurrence
# is recorded as missed instead of run (a weekly report 30 hours late is
# usually worse than a visible "missed" row).
CATCH_UP_GRACE = {
    "hourly": timedelta(minutes=45),
    "daily": timedelta(hours=6),
    "weekly": timedelta(hours=24),
}

# Mon=0 .. Sun=6, matching ``date.weekday()``.
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def is_anchored(schedule_type: str | None) -> bool:
    return schedule_type in ANCHORED_TYPES


def as_utc(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime (SQLite round-trip) as UTC; normalize aware ones."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_weekly_days(value) -> list[int]:
    """Parse the stored ``"0,2,4"`` form (or a list) into sorted weekday ints."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return sorted({int(p) for p in parts})
    return sorted({int(v) for v in value})


def format_weekly_days(days) -> str | None:
    if days is None:
        return None
    return ",".join(str(d) for d in sorted(set(days)))


def validate_weekly_days(days) -> list[int]:
    """Validate a list of weekday ints (Mon=0 .. Sun=6); returns it sorted/deduped."""
    if not isinstance(days, (list, tuple)) or not days:
        raise ValueError(
            "A weekly schedule requires weekly_days: a non-empty list of "
            "weekday numbers (0=Monday .. 6=Sunday)."
        )
    for d in days:
        if not isinstance(d, int) or isinstance(d, bool) or not (0 <= d <= 6):
            raise ValueError(
                "weekly_days entries must be integers 0-6 (0=Monday .. 6=Sunday)."
            )
    return sorted(set(days))


def describe_weekly_days(days) -> str:
    days = parse_weekly_days(days)
    if days == list(range(7)):
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "weekdays"
    if days == [5, 6]:
        return "weekends"
    return ", ".join(WEEKDAY_NAMES[d] for d in days)


def _local_candidates(schedule: dict, around: datetime) -> list[datetime]:
    """UTC instants of daily/weekly occurrences within ~8 days of ``around``."""
    time_local = schedule.get("daily_time_local")
    tz_name = schedule.get("timezone")
    if not time_local or not tz_name:
        raise ValueError("schedule has no local time / timezone configured")
    tz = ZoneInfo(tz_name)
    h, m = int(time_local[:2]), int(time_local[3:])
    weekly = schedule.get("schedule_type") == "weekly"
    days = set(parse_weekly_days(schedule.get("weekly_days"))) if weekly else None
    if weekly and not days:
        raise ValueError("weekly schedule has no weekly_days configured")

    center: date = around.astimezone(tz).date()
    out = []
    for offset in range(-8, 9):
        d = center + timedelta(days=offset)
        if days is not None and d.weekday() not in days:
            continue
        # fold=0: first occurrence of an ambiguous time; a nonexistent (gap)
        # time resolves with the pre-transition offset, i.e. shifts forward.
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
        out.append(local_dt.astimezone(timezone.utc))
    return sorted(out)


def occurrence_at_or_before(schedule: dict, t: datetime) -> datetime:
    """Latest occurrence ``<= t``. Raises ValueError on a misconfigured schedule."""
    t = as_utc(t)
    stype = schedule.get("schedule_type")
    if stype == "hourly":
        minute = schedule.get("hourly_minute")
        if minute is None:
            raise ValueError("hourly schedule has no minute configured")
        cand = t.replace(minute=minute, second=0, microsecond=0)
        if cand > t:
            cand -= timedelta(hours=1)
        return cand
    if stype in ("daily", "weekly"):
        past = [c for c in _local_candidates(schedule, t) if c <= t]
        if not past:
            raise ValueError("no occurrence found")
        return past[-1]
    raise ValueError(f"schedule_type '{stype}' has no fixed occurrences")


def occurrence_after(schedule: dict, t: datetime) -> datetime:
    """Earliest occurrence strictly ``> t``. Raises ValueError on a misconfigured schedule."""
    t = as_utc(t)
    stype = schedule.get("schedule_type")
    if stype == "hourly":
        minute = schedule.get("hourly_minute")
        if minute is None:
            raise ValueError("hourly schedule has no minute configured")
        cand = t.replace(minute=minute, second=0, microsecond=0)
        if cand <= t:
            cand += timedelta(hours=1)
        return cand
    if stype in ("daily", "weekly"):
        future = [c for c in _local_candidates(schedule, t) if c > t]
        if not future:
            raise ValueError("no occurrence found")
        return future[0]
    raise ValueError(f"schedule_type '{stype}' has no fixed occurrences")


def occurrences_between(
    schedule: dict, start: datetime, end: datetime, limit: int = 1000,
) -> list[datetime]:
    """Occurrences ``o`` with ``start <= o < end`` (at most ``limit``)."""
    start, end = as_utc(start), as_utc(end)
    out: list[datetime] = []
    occ = occurrence_after(schedule, start - timedelta(microseconds=1))
    while occ < end and len(out) < limit:
        out.append(occ)
        occ = occurrence_after(schedule, occ)
    return out
