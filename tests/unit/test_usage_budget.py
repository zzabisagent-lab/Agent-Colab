"""P1-14 unit tests: pricing computation, usage report validation, budget arithmetic (V-P1-30)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from server.usage.budget import BudgetScope, settlement_status, would_exceed
from server.usage.pricing import UsageError, compute_cost_units, load_pricing
from server.usage.records import build_report
from server.usage.versions import table_sha256

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "budget-cases.yaml"
CASES = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
PRICING = load_pricing()


@pytest.mark.parametrize(
    "case", CASES["usage_cases"], ids=[c["name"] for c in CASES["usage_cases"]]
)
def test_usage_cost_computation(case: dict[str, Any]) -> None:
    usage = case.get("usage")
    reason = case.get("usage_unavailable_reason")
    if "expect_error" in case:
        with pytest.raises(UsageError) as exc:
            compute_cost_units(build_report(usage, reason), PRICING)
        assert exc.value.code == case["expect_error"]
        return
    result = compute_cost_units(build_report(usage, reason), PRICING)
    if case["expect"]["source"] == "unavailable":
        assert result is None
        return
    assert result is not None
    assert (result.cost_units, result.source) == (
        case["expect"]["cost_units"],
        case["expect"]["source"],
    )
    assert result.pricing_version == "pricing-v1"


def test_hand_computed_example_matches_formula() -> None:
    # generic-small: 1234 in, 567 out, 3 calls, 4500 ms -> 186 + 341 + 300 + 23 = 850
    r = compute_cost_units(
        build_report(
            {
                "model": "generic-small",
                "input_tokens": 1234,
                "output_tokens": 567,
                "tool_calls": 3,
                "wall_time_ms": 4500,
            },
            None,
        ),
        PRICING,
    )
    assert r is not None and r.cost_units == 850
    assert (
        -(-1234 * 150 // 1000) == 186
        and -(-567 * 600 // 1000) == 341
        and -(-4500 * 5 // 1000) == 23
    )


def test_build_report_requires_usage_or_reason() -> None:
    with pytest.raises(UsageError) as exc:
        build_report(None, None)
    assert exc.value.code == "USAGE_REQUIRED"
    assert build_report(None, "ERROR") == {"usage_unavailable": {"reason": "ERROR"}}


@pytest.mark.parametrize("case", CASES["reservation_cases"])
def test_reservation_arithmetic(case: dict[str, int | bool]) -> None:
    assert (
        would_exceed(
            int(case["used"]), int(case["reserved"]), int(case["estimate"]), int(case["limit"])
        )
        is case["exceed"]
    )


@pytest.mark.parametrize("case", CASES["settlement_cases"])
def test_settlement_status(case: dict[str, Any]) -> None:
    assert settlement_status(case["actual"], case["available"]) == case["status"]


def test_scope_aggregate_id_has_budget_prefix() -> None:
    assert BudgetScope("agent_daily", "agent-x").aggregate_id() == "bud-agent_daily:agent-x"


def test_table_sha256_is_canonical() -> None:
    assert table_sha256({"b": 1, "a": 2}) == table_sha256({"a": 2, "b": 1})
