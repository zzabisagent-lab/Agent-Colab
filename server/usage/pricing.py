"""Pricing table and cost_units computation (development plan §7C, spec §4.2).

Integer arithmetic only; each component is rounded up (ceil) independently so that the same
usage always yields the same cost_units for a given pricing version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from server.domain import defaults
from server.work.schemas import AdapterSchemaError, validate

PRICING_PATH = Path(__file__).resolve().parents[2] / "policy" / "pricing.yaml"

USAGE_UNAVAILABLE_REASONS = frozenset({"ADAPTER_NO_METERING", "MODEL_UNKNOWN", "ERROR"})


class UsageError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Rate:
    input_per_1k_tokens: int
    output_per_1k_tokens: int
    per_tool_call: int
    per_wall_second: int


@dataclass(frozen=True)
class Pricing:
    version: str
    default: Rate
    models: dict[str, Rate]

    def rate_for(self, model: str) -> tuple[Rate, bool]:
        """Return (rate, known)."""
        rate = self.models.get(model)
        return (rate, True) if rate is not None else (self.default, False)


def _rate(spec: dict[str, Any]) -> Rate:
    return Rate(
        int(spec["input_per_1k_tokens"]),
        int(spec["output_per_1k_tokens"]),
        int(spec["per_tool_call"]),
        int(spec["per_wall_second"]),
    )


def pricing_from_dict(data: dict[str, Any]) -> Pricing:
    """Validate against schemas/api/pricing.v1.schema.json and build the table."""
    try:
        validate("pricing", data)
    except AdapterSchemaError as exc:
        raise UsageError("PRICING_INVALID", exc.detail) from exc
    if data["cost_units_per_credit"] != defaults.COST_UNITS_PER_CREDIT:
        raise UsageError("PRICING_INVALID", "cost_units_per_credit must be 1000000")
    return Pricing(
        version=data["version"],
        default=_rate(data["default"]),
        models={name: _rate(spec) for name, spec in data["models"].items()},
    )


def load_pricing(path: Path = PRICING_PATH) -> Pricing:
    return pricing_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def cost_from_rate(rate: Rate, usage: dict[str, Any]) -> int:
    tokens_in = int(usage["input_tokens"])
    tokens_out = int(usage["output_tokens"])
    return (
        _ceil_div(tokens_in * rate.input_per_1k_tokens, 1000)
        + _ceil_div(tokens_out * rate.output_per_1k_tokens, 1000)
        + int(usage["tool_calls"]) * rate.per_tool_call
        + _ceil_div(int(usage["wall_time_ms"]) * rate.per_wall_second, 1000)
    )


@dataclass(frozen=True)
class CostResult:
    cost_units: int
    source: str  # reported | computed | estimated
    pricing_version: str
    model: str


def compute_cost_units(report: dict[str, Any], pricing: Pricing) -> CostResult | None:
    """Compute cost for a §7C report (``{"usage": ...}`` or ``{"usage_unavailable": ...}``).

    Returns None when usage is unavailable with a valid reason; raises ``USAGE_REQUIRED`` when
    neither usage nor a reason is present, ``USAGE_INVALID`` on schema violations.
    """
    try:
        validate("usage", report)
    except AdapterSchemaError as exc:
        if "usage" not in report and "usage_unavailable" not in report:
            raise UsageError(
                "USAGE_REQUIRED", "usage or usage_unavailable reason required"
            ) from exc
        raise UsageError("USAGE_INVALID", exc.detail) from exc
    if "usage_unavailable" in report:
        return None
    usage = report["usage"]
    model = str(usage["model"])
    rate, known = pricing.rate_for(model)
    if "cost_units" in usage:
        return CostResult(int(usage["cost_units"]), "reported", pricing.version, model)
    cost = cost_from_rate(rate, usage)
    return CostResult(cost, "computed" if known else "estimated", pricing.version, model)
