"""Adapter Conformance Suite CS-01..CS-12 (validation plan §11.1; P3-05)."""

from server.agents.conformance import (
    harness_push as _harness_push,  # noqa: F401 - registers webhook/bot harnesses
)
from server.agents.conformance.report import CheckResult, ConformanceReport
from server.agents.conformance.suite import run_suite

__all__ = ["CheckResult", "ConformanceReport", "run_suite"]
