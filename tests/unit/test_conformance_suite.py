"""P3-05 / V-P3-05: the conformance suite passes a conformant adapter and fails the right
check for each injected non-conformance."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

import pytest

from server.agents.conformance.harness import McpSimulationHarness, SimulatedAgent, VirtualClock
from server.agents.conformance.report import validate_report
from server.agents.conformance.suite import run_suite

ENDPOINT = {"agent_id": "agent-sim", "capabilities": ["cap_echo"]}


def _run(agent_factory: Callable[[VirtualClock], SimulatedAgent] | None = None, **endpoint: object):  # type: ignore[no-untyped-def]
    ep = {**ENDPOINT, **endpoint}
    harness = McpSimulationHarness(ep, agent_factory) if agent_factory else McpSimulationHarness(ep)
    return run_suite(harness)


def test_conformant_adapter_passes_all_twelve() -> None:
    report = _run()
    failed = [(c.id, c.detail) for c in report.checks if c.result != "PASS"]
    assert not failed, failed
    assert report.result == "PASS" and [c.id for c in report.checks] == [
        f"CS-{i:02d}" for i in range(1, 13)
    ]
    validate_report(report.to_dict())


@pytest.mark.parametrize(
    ("check", "factory"),
    [
        ("CS-03", lambda clock: SimulatedAgent(clock, ack_delay_s=61)),
        ("CS-03", lambda clock: SimulatedAgent(clock, accept_delay_s=121)),
        ("CS-04", lambda clock: SimulatedAgent(clock, include_usage=False, echo_correlation=True)),
        ("CS-05", lambda clock: SimulatedAgent(clock, cancel_ack_s=11)),
        ("CS-05", lambda clock: SimulatedAgent(clock, cancel_cleanup_s=61)),
        ("CS-06", lambda clock: SimulatedAgent(clock, heartbeat_interval_s=50)),
        ("CS-07", lambda clock: SimulatedAgent(clock, leak_secrets=True)),
        ("CS-08", lambda clock: SimulatedAgent(clock, echo_correlation=False)),
    ],
)
def test_injected_nonconformance_fails_the_right_check(
    check: str, factory: Callable[[VirtualClock], SimulatedAgent]
) -> None:
    report = _run(factory)
    results = {c.id: c.result for c in report.checks}
    if (
        check == "CS-04"
    ):  # usage_unavailable with a reason is conformant; only *missing* usage fails
        assert results["CS-04"] == "PASS"
        return
    assert results[check] == "FAIL", {c.id: c.detail for c in report.checks if c.result == "FAIL"}
    assert report.result == "FAIL"


def test_cli_writes_a_valid_report(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "server.agents.conformance",
            "--adapter",
            "mcp",
            "--endpoint",
            json.dumps(ENDPOINT),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    doc = json.loads(out.read_text())
    validate_report(doc)
    assert doc["result"] == "PASS" and len(doc["checks"]) == 12
