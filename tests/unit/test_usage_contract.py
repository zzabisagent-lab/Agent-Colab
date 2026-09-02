"""Usage schema, pricing.yaml schema, cost_units computation (V-P0-17, V-P1-30 baseline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain import defaults
from server.usage.pricing import (
    PRICING_PATH,
    UsageError,
    compute_cost_units,
    cost_from_rate,
    load_pricing,
    pricing_from_dict,
)
from server.work.schemas import AdapterSchemaError, validate

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "cases.json").read_text(
        encoding="utf-8"
    )
)
PRICING = load_pricing()


def test_pricing_yaml_is_valid_and_versioned() -> None:
    raw = yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8"))
    validate("pricing", raw)
    assert PRICING.version == "pricing-v1"
    assert raw["cost_units_per_credit"] == defaults.COST_UNITS_PER_CREDIT == 1_000_000
    assert "generic-medium" in PRICING.models


@pytest.mark.parametrize(
    "case", CASES["pricing_invalid"], ids=[c["name"] for c in CASES["pricing_invalid"]]
)
def test_invalid_pricing_tables(case: dict[str, Any]) -> None:
    with pytest.raises((UsageError, AdapterSchemaError)) as exc:
        pricing_from_dict(case["table"])
    assert getattr(exc.value, "code", "") in {"PRICING_INVALID", "PRICING_SCHEMA_INVALID"}


@pytest.mark.parametrize("case", CASES["cost_cases"], ids=[c["name"] for c in CASES["cost_cases"]])
def test_cost_units_computation(case: dict[str, Any]) -> None:
    result = compute_cost_units(case["report"], PRICING)
    if case["expect"] is None:
        assert result is None
    else:
        assert result is not None
        assert (result.cost_units, result.source) == (
            case["expect"]["cost_units"],
            case["expect"]["source"],
        )
        assert result.pricing_version == "pricing-v1"
        assert isinstance(result.cost_units, int)


@pytest.mark.parametrize(
    "case", CASES["cost_errors"], ids=[c["name"] for c in CASES["cost_errors"]]
)
def test_cost_errors(case: dict[str, Any]) -> None:
    with pytest.raises(UsageError) as exc:
        compute_cost_units(case["report"], PRICING)
    assert exc.value.code == case["code"]


def test_ceil_rounding_is_per_component_and_deterministic() -> None:
    rate = PRICING.models["generic-small"]  # 150/600 per 1k, 100 per call, 5 per second
    usage = {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0, "wall_time_ms": 1}
    assert cost_from_rate(rate, usage) == 1 + 1 + 0 + 1
    assert cost_from_rate(rate, usage) == cost_from_rate(rate, dict(usage))
    big = {
        "input_tokens": 10**9,
        "output_tokens": 10**9,
        "tool_calls": 10**6,
        "wall_time_ms": 10**9,
    }
    assert cost_from_rate(rate, big) == 150_000_000 + 600_000_000 + 100_000_000 + 5_000_000


def test_usage_schema_requires_exactly_one_branch() -> None:
    validate("usage", {"usage_unavailable": {"reason": "MODEL_UNKNOWN", "detail": "no meter"}})
    with pytest.raises(AdapterSchemaError):
        validate("usage", {})
    with pytest.raises(AdapterSchemaError):
        validate(
            "usage",
            {
                "usage": {
                    "model": "m",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "tool_calls": 0,
                    "wall_time_ms": 0,
                    "cost_units": 1.5,
                }
            },
        )
