"""Injectable clock (development plan §21, ADR-0004).

Production code never calls ``datetime.now`` directly; it receives a ``Clock``. Tests use
``FixedClock`` or ``SteppingClock`` and never wait in real time.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

UTC = dt.UTC


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """Current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FixedClock:
    """A clock frozen at ``at`` until ``advance``/``set`` is called."""

    def __init__(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._now = at.astimezone(UTC)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, delta: dt.timedelta) -> dt.datetime:
        self._now += delta
        return self._now

    def set(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("aware datetime required")
        self._now = at.astimezone(UTC)


class SteppingClock(FixedClock):
    """Advances by ``step`` on every ``now()`` call (deterministic monotonic timestamps)."""

    def __init__(self, at: dt.datetime, step: dt.timedelta = dt.timedelta(seconds=1)) -> None:
        super().__init__(at)
        self._step = step

    def now(self) -> dt.datetime:
        current = self._now
        self._now = current + self._step
        return current


def isoformat_utc(value: dt.datetime) -> str:
    """RFC 3339 with millisecond precision and a ``Z`` suffix; the only timestamp format stored."""
    if value.tzinfo is None:
        raise ValueError("aware datetime required")
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
