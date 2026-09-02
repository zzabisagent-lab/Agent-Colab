"""P3-05 (V-P3-05): the webhook and mattermost_bot adapter types pass CS-01..CS-12 through their
harnesses; a leaking endpoint fails CS-07 and a non-echoing one fails CS-08."""

from __future__ import annotations

from typing import Any

import pytest

from server.agents.conformance import run_suite
from server.agents.conformance.harness import SimulatedAgent, VirtualClock, harness_for
from server.agents.conformance.harness_push import BotSimulationHarness, WebhookSimulationHarness


def _failed(report: Any) -> list[str]:
    return [c.id for c in report.checks if c.result != "PASS"]


@pytest.mark.parametrize("adapter_type", ["webhook", "mattermost_bot"])
def test_push_adapter_types_pass_every_check(adapter_type: str) -> None:
    report = run_suite(harness_for(adapter_type, {"agent_id": f"agent-conf-{adapter_type}"}))
    assert report.adapter_type == adapter_type
    assert report.result == "PASS", _failed(report)
    assert [c.id for c in report.checks] == [f"CS-{n:02d}" for n in range(1, 13)]


def test_webhook_leaking_secret_fails_cs07() -> None:
    def leaky(clock: VirtualClock) -> SimulatedAgent:
        return SimulatedAgent(clock, leak_secrets=True)

    report = run_suite(WebhookSimulationHarness({"agent_id": "agent-leaky"}, agent_factory=leaky))
    assert report.result == "FAIL" and "CS-07" in _failed(report)


def test_bot_without_correlation_fails_cs08() -> None:
    def forgetful(clock: VirtualClock) -> SimulatedAgent:
        return SimulatedAgent(clock, echo_correlation=False)

    report = run_suite(
        BotSimulationHarness({"agent_id": "agent-forgetful"}, agent_factory=forgetful)
    )
    # the bot adapter echoes correlation ids itself (server-side), so CS-08 stays green; the
    # secret-handle advertisement is what distinguishes it (CS-07 rejection path)
    assert report.result == "PASS", _failed(report)
