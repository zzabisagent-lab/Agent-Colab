"""Brainstorm session limits (development plan §7F, spec §8.3).

Pure decision functions: per-Agent turns, consecutive same-Agent turns, total turns, budget in
``cost_units`` and elapsed wall time. A breach both rejects the offending contribution and pauses
the session for facilitator guidance, which is what §7F prescribes ("the session becomes
``PAUSED`` ... and guidance is requested") and what V-P6-26 checks ("consecutive utterance
rejected; PAUSED + guidance request on overrun").
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

DEFAULTS: dict[str, int] = {
    "turns_per_agent": 5,
    "max_consecutive": 1,
    "total_turns": 40,
    "budget_cost_units": 0,  # 0 = unlimited
    "time_limit_minutes": 0,  # 0 = unlimited
}
LIMIT_KEYS: tuple[str, ...] = tuple(DEFAULTS)


class BreachCode(StrEnum):
    """Stable ``reason_code`` values carried by ``BRAINSTORM_PAUSED``."""

    TURNS_PER_AGENT_EXCEEDED = "TURNS_PER_AGENT_EXCEEDED"
    MAX_CONSECUTIVE_EXCEEDED = "MAX_CONSECUTIVE_EXCEEDED"
    TOTAL_TURNS_EXCEEDED = "TOTAL_TURNS_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIME_LIMIT_EXCEEDED = "TIME_LIMIT_EXCEEDED"


class LimitsError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Limits:
    turns_per_agent: int = DEFAULTS["turns_per_agent"]
    max_consecutive: int = DEFAULTS["max_consecutive"]
    total_turns: int = DEFAULTS["total_turns"]
    budget_cost_units: int = DEFAULTS["budget_cost_units"]
    time_limit_minutes: int = DEFAULTS["time_limit_minutes"]

    def as_dict(self) -> dict[str, int]:
        return {key: int(getattr(self, key)) for key in LIMIT_KEYS}


def parse(raw: Any) -> Limits:
    """Validate a limits mapping; unknown keys and non-positive bounds are rejected."""
    values = dict(DEFAULTS)
    if raw:
        if not isinstance(raw, dict):
            raise LimitsError("BRAINSTORM_LIMITS_INVALID", "limits must be an object")
        unknown = sorted(set(raw) - set(LIMIT_KEYS))
        if unknown:
            raise LimitsError("BRAINSTORM_LIMITS_INVALID", f"unknown keys: {unknown}")
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise LimitsError("BRAINSTORM_LIMITS_INVALID", f"{key} must be an integer")
            floor = 0 if key in ("budget_cost_units", "time_limit_minutes") else 1
            if value < floor:
                raise LimitsError("BRAINSTORM_LIMITS_INVALID", f"{key} must be >= {floor}")
            values[key] = value
    return Limits(**values)


@dataclass(frozen=True)
class TurnState:
    """What the engine knows when a contribution arrives."""

    total_turns: int  # turns already recorded
    contributor_turns: int  # turns already recorded by this contributor
    consecutive_turns: int  # consecutive turns already taken by this contributor
    is_last_contributor: bool
    spent_cost_units: int
    started_at: dt.datetime
    now: dt.datetime


@dataclass(frozen=True)
class Breach:
    code: BreachCode
    detail: str


def check(limits: Limits, state: TurnState, *, is_agent: bool) -> Breach | None:
    """The first limit this contribution would breach, or None when it may proceed.

    Per-Agent turn limits apply to Agent participants only: §7F distributes *Agent* turns and
    lets Human participants speak freely, so a Human utterance is bounded by the session-wide
    total, budget and time limits alone.
    """
    consecutive = state.consecutive_turns + 1 if state.is_last_contributor else 1
    if limits.max_consecutive and consecutive > limits.max_consecutive:
        return Breach(
            BreachCode.MAX_CONSECUTIVE_EXCEEDED,
            f"{consecutive} consecutive turns exceeds {limits.max_consecutive}",
        )
    if is_agent and limits.turns_per_agent and state.contributor_turns + 1 > limits.turns_per_agent:
        return Breach(
            BreachCode.TURNS_PER_AGENT_EXCEEDED,
            f"turn {state.contributor_turns + 1} exceeds {limits.turns_per_agent}",
        )
    if limits.total_turns and state.total_turns + 1 > limits.total_turns:
        return Breach(
            BreachCode.TOTAL_TURNS_EXCEEDED,
            f"turn {state.total_turns + 1} exceeds {limits.total_turns}",
        )
    if limits.budget_cost_units and state.spent_cost_units > limits.budget_cost_units:
        return Breach(
            BreachCode.BUDGET_EXCEEDED,
            f"{state.spent_cost_units} cost_units exceeds {limits.budget_cost_units}",
        )
    if limits.time_limit_minutes:
        elapsed = (state.now - state.started_at).total_seconds() / 60.0
        if elapsed > limits.time_limit_minutes:
            return Breach(
                BreachCode.TIME_LIMIT_EXCEEDED,
                f"{elapsed:.1f} minutes exceeds {limits.time_limit_minutes}",
            )
    return None
