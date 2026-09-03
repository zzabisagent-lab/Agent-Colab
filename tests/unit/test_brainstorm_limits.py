"""V-P6-03 (unit): the pure limit decisions of the Brainstorm engine (development plan §7F)."""

from __future__ import annotations

import datetime as dt

import pytest

from server.brainstorm.limits import (
    DEFAULTS,
    Breach,
    BreachCode,
    Limits,
    LimitsError,
    TurnState,
    check,
    parse,
)

T0 = dt.datetime(2026, 4, 6, 9, 0, tzinfo=dt.UTC)


def _state(**over: object) -> TurnState:
    base: dict[str, object] = {
        "total_turns": 0,
        "contributor_turns": 0,
        "consecutive_turns": 0,
        "is_last_contributor": False,
        "spent_cost_units": 0,
        "started_at": T0,
        "now": T0,
    }
    base.update(over)
    return TurnState(**base)  # type: ignore[arg-type]


def test_defaults_and_parsing() -> None:
    assert parse(None).as_dict() == DEFAULTS
    assert parse({"total_turns": 7}).total_turns == 7
    for bad in ({"unknown": 1}, {"total_turns": 0}, {"total_turns": "x"}, {"max_consecutive": -1}):
        with pytest.raises(LimitsError) as exc:
            parse(bad)
        assert exc.value.code == "BRAINSTORM_LIMITS_INVALID"
    assert parse({"budget_cost_units": 0}).budget_cost_units == 0  # 0 means unlimited


def test_within_limits_passes() -> None:
    assert check(Limits(), _state(), is_agent=True) is None


def test_consecutive_same_agent_is_the_first_breach() -> None:
    breach = check(
        Limits(max_consecutive=1),
        _state(is_last_contributor=True, consecutive_turns=1),
        is_agent=True,
    )
    assert isinstance(breach, Breach) and breach.code is BreachCode.MAX_CONSECUTIVE_EXCEEDED


def test_per_agent_turns_apply_to_agents_only() -> None:
    state = _state(contributor_turns=5)
    assert check(Limits(turns_per_agent=5), state, is_agent=True).code is (
        BreachCode.TURNS_PER_AGENT_EXCEEDED
    )
    assert check(Limits(turns_per_agent=5), state, is_agent=False) is None  # §7F: Humans speak


def test_total_budget_and_time_breaches() -> None:
    assert check(Limits(total_turns=3), _state(total_turns=3), is_agent=True).code is (
        BreachCode.TOTAL_TURNS_EXCEEDED
    )
    assert (
        check(Limits(budget_cost_units=100), _state(spent_cost_units=101), is_agent=True).code
        is BreachCode.BUDGET_EXCEEDED
    )
    assert (
        check(
            Limits(time_limit_minutes=30),
            _state(now=T0 + dt.timedelta(minutes=31)),
            is_agent=True,
        ).code
        is BreachCode.TIME_LIMIT_EXCEEDED
    )
    assert (
        check(
            Limits(time_limit_minutes=30),
            _state(now=T0 + dt.timedelta(minutes=30)),
            is_agent=True,
        )
        is None
    )  # exactly at the limit still runs


def test_zero_means_unlimited() -> None:
    unlimited = Limits(turns_per_agent=0, total_turns=0, budget_cost_units=0, time_limit_minutes=0)
    state = _state(total_turns=10_000, contributor_turns=999, spent_cost_units=10**9)
    assert check(unlimited, state, is_agent=True) is None


def test_router_registers_every_brainstorm_verb() -> None:
    """The `/colab brainstorm ...` verbs mount on the Router's resource extension point (§7A.2)."""
    from server.brainstorm import router_handlers
    from server.channels import router as rt

    router_handlers.register()
    try:
        verbs = {verb for resource, verb in rt.RESOURCE_HANDLERS if resource == "brainstorm"}
        assert verbs == {
            "start",
            "contribute",
            "summarize",
            "decide",
            "taskify",
            "pause",
            "resume",
            "close",
            "show",
        }
    finally:
        router_handlers.unregister()
    assert not [r for r, _ in rt.RESOURCE_HANDLERS if r == "brainstorm"]
