"""Normative five-field cron grammar (spec §8.6, development plan §6.6, §10A.3).

Fields: minute 0-59, hour 0-23, day-of-month 1-31, month 1-12, day-of-week 0-6 (Sunday = 0).
Each field accepts ``*``, comma lists, hyphen ranges, and ``/`` steps on ``*`` or a range. Names,
a seconds field, ``? L W #``, ``@aliases`` and day-of-week 7 are rejected with stable codes.
When both day-of-month and day-of-week are restricted, Vixie OR semantics apply.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server.domain import defaults
from server.schedules.occurrence import occurrence_key

FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 6),
)
_EXTENDED = frozenset("?LW#")
_NUMBER = re.compile(r"^\d+$")
PREVIEW_HORIZON_DAYS = 366 * 5


class CronError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Occurrence:
    local: dt.datetime  # naive wall-clock minute in the schedule timezone
    utc: dt.datetime | None  # None only for DST_GAP entries
    occurrence_key: str
    reason: str | None = None  # None | "DST_GAP" | "DST_FOLD"

    @property
    def executable(self) -> bool:
        return self.utc is not None


@dataclass(frozen=True)
class CronExpression:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def day_matches(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days_of_month
        dow_ok = ((day.weekday() + 1) % 7) in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok  # Vixie OR
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def matches(self, local: dt.datetime) -> bool:
        return (
            local.minute in self.minutes
            and local.hour in self.hours
            and self.day_matches(local.date())
        )

    def times_of_day(self) -> list[int]:
        return sorted(h * 60 + m for h in self.hours for m in self.minutes)

    def min_interval_minutes(self) -> int:
        """Minimum gap between consecutive occurrences over a 28-year wall-clock window.

        The window (2024-2051) covers every weekday/leap-year combination; DST is ignored
        because the interval floor concerns wall-clock spacing.
        """
        times = self.times_of_day()
        best = 10**9
        if len(times) > 1:
            best = min(b - a for a, b in pairwise(times))
        first, last = times[0], times[-1]
        previous: dt.date | None = None
        day = dt.date(2024, 1, 1)
        end = dt.date(2051, 12, 31)
        while day <= end:
            if self.day_matches(day):
                if previous is not None:
                    gap = (day - previous).days * 1440 - last + first
                    best = min(best, gap)
                    if best <= 1:
                        break
                previous = day
            day += dt.timedelta(days=1)
        if previous is None:
            raise CronError("CRON_UNREACHABLE", f"{self.expression!r} never matches a date")
        return best


def _parse_field(field_text: str, name: str, low: int, high: int) -> tuple[frozenset[int], bool]:
    if field_text == "":
        raise CronError("CRON_TOKEN_INVALID", f"{name}: empty field")
    if any(ch in _EXTENDED for ch in field_text):
        raise CronError("CRON_EXTENDED_TOKEN_REJECTED", f"{name}: {field_text!r}")
    if re.search(r"[A-Za-z]", field_text):
        raise CronError("CRON_NAME_REJECTED", f"{name}: {field_text!r}")
    if field_text == "*":
        return frozenset(range(low, high + 1)), False
    values: set[int] = set()
    for part in field_text.split(","):
        if part == "":
            raise CronError("CRON_TOKEN_INVALID", f"{name}: empty list item in {field_text!r}")
        step = 1
        base = part
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not _NUMBER.match(step_text) or int(step_text) < 1:
                raise CronError("CRON_STEP_INVALID", f"{name}: step {step_text!r}")
            step = int(step_text)
            if base != "*" and "-" not in base:
                raise CronError("CRON_STEP_INVALID", f"{name}: step requires * or a range")
        if base == "*":
            start, stop = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            if not (_NUMBER.match(a) and _NUMBER.match(b)):
                raise CronError("CRON_TOKEN_INVALID", f"{name}: {part!r}")
            start, stop = int(a), int(b)
            if start > stop:
                raise CronError("CRON_RANGE_INVALID", f"{name}: {start}-{stop} reversed")
        else:
            if not _NUMBER.match(base):
                raise CronError("CRON_TOKEN_INVALID", f"{name}: {part!r}")
            start = stop = int(base)
        for value in (start, stop):
            if name == "day_of_week" and value == 7:
                raise CronError("CRON_DOW7_REJECTED", "day-of-week 7 is not allowed; use 0")
            if value < low or value > high:
                raise CronError("CRON_RANGE_INVALID", f"{name}: {value} outside {low}-{high}")
        values.update(range(start, stop + 1, step))
    return frozenset(values), True


def parse(expression: str) -> CronExpression:
    text = expression.strip()
    if text.startswith("@"):
        raise CronError("CRON_ALIAS_REJECTED", text)
    tokens = text.split()
    if len(tokens) == 6:
        raise CronError("CRON_SECONDS_REJECTED", "six fields given; seconds are not supported")
    if len(tokens) != 5:
        raise CronError("CRON_FIELD_COUNT", f"expected 5 fields, got {len(tokens)}")
    parsed = [
        _parse_field(tok, name, low, high)
        for tok, (name, low, high) in zip(tokens, FIELDS, strict=True)
    ]
    return CronExpression(
        expression=" ".join(tokens),
        minutes=parsed[0][0],
        hours=parsed[1][0],
        days_of_month=parsed[2][0],
        months=parsed[3][0],
        days_of_week=parsed[4][0],
        dom_restricted=parsed[2][1],
        dow_restricted=parsed[4][1],
    )


def validate(
    expression: str,
    min_interval_minutes: int = defaults.SCHEDULE_MIN_INTERVAL_MINUTES_DEFAULT,
) -> CronExpression:
    """Parse and enforce the minimum interval (default 5 min, floor 1 min)."""
    floor = defaults.SCHEDULE_MIN_INTERVAL_MINUTES_FLOOR
    if min_interval_minutes < floor:
        raise CronError("CRON_INTERVAL_FLOOR", f"minimum interval below {floor} minute")
    cron = parse(expression)
    interval = cron.min_interval_minutes()
    if interval < min_interval_minutes:
        raise CronError(
            "CRON_INTERVAL_TOO_SHORT",
            f"{cron.expression!r} fires every {interval} min; minimum is {min_interval_minutes}",
        )
    return cron


def load_zone(timezone: str) -> ZoneInfo:
    if not timezone or timezone != timezone.strip() or "\\" in timezone:
        raise CronError("TIMEZONE_INVALID", repr(timezone))
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError("TIMEZONE_INVALID", timezone) from exc


def resolve_local(local: dt.datetime, zone: ZoneInfo) -> tuple[dt.datetime | None, str | None]:
    """Map a naive wall-clock minute to its UTC instant.

    Returns ``(None, "DST_GAP")`` for a non-existent local time, ``(utc, "DST_FOLD")`` for a
    duplicated local time (the first, earlier UTC instant), else ``(utc, None)``.
    """
    first = local.replace(tzinfo=zone, fold=0)
    utc0 = first.astimezone(dt.UTC)
    if utc0.astimezone(zone).replace(tzinfo=None) != local:
        return None, "DST_GAP"
    utc1 = local.replace(tzinfo=zone, fold=1).astimezone(dt.UTC)
    if utc1 != utc0:
        return min(utc0, utc1), "DST_FOLD"
    return utc0, None


def next_occurrences(
    expression: str | CronExpression,
    timezone: str,
    after_utc: dt.datetime,
    count: int = 10,
    schedule_id: str = "preview",
    include_gaps: bool = True,
) -> list[Occurrence]:
    """Next ``count`` executable occurrences strictly after ``after_utc`` (plus DST_GAP entries)."""
    cron = parse(expression) if isinstance(expression, str) else expression
    zone = load_zone(timezone)
    if after_utc.tzinfo is None:
        raise CronError("TIMESTAMP_NAIVE", "after_utc must be timezone-aware")
    after_utc = after_utc.astimezone(dt.UTC)
    start_local = after_utc.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
    start_local += dt.timedelta(minutes=1)
    times = cron.times_of_day()
    out: list[Occurrence] = []
    executable = 0
    day = start_local.date()
    horizon = day + dt.timedelta(days=PREVIEW_HORIZON_DAYS)
    while executable < count and day <= horizon:
        if cron.day_matches(day):
            for tod in times:
                local = dt.datetime.combine(day, dt.time(tod // 60, tod % 60))
                if local < start_local:
                    continue
                utc, reason = resolve_local(local, zone)
                key = occurrence_key(schedule_id, timezone, local)
                if utc is None:
                    if include_gaps:
                        out.append(Occurrence(local, None, key, reason))
                    continue
                if utc <= after_utc:
                    continue
                out.append(Occurrence(local, utc, key, reason))
                executable += 1
                if executable >= count:
                    break
        day += dt.timedelta(days=1)
    return out
