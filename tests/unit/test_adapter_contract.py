"""P3-03: Adapter contract — stable error codes, identity hash, registry, MCP-client adapter."""

from __future__ import annotations

import datetime as dt

import pytest

from server.agents.adapters import contract as c
from server.agents.adapters.mcp_client import McpClientAdapter, identity_hash
from server.agents.conformance.harness import SimulatedAgent, VirtualClock


def _adapter(**endpoint: object) -> tuple[McpClientAdapter, SimulatedAgent]:
    clock = VirtualClock()
    agent = SimulatedAgent(clock)
    ep: dict[str, object] = {"agent_id": "agent-c", "capabilities": ["cap-echo"], **endpoint}
    return McpClientAdapter(ep, agent), agent


def test_error_codes_are_closed_set() -> None:
    with pytest.raises(ValueError):
        c.AdapterError("SOMETHING_ELSE")
    err = c.AdapterError("ADAPTER_TIMEOUT", "x", retryable=True)
    assert err.code == "ADAPTER_TIMEOUT" and err.retryable


def test_registry_and_unknown_type() -> None:
    import server.agents.adapters  # noqa: F401 - built-ins register on import

    assert "mcp" in c.adapter_types()
    with pytest.raises(c.AdapterError) as exc:
        c.adapter_for("no-such-type", {})
    assert exc.value.code == "CAPABILITY_UNSUPPORTED"
    assert isinstance(
        c.adapter_for("mcp", {"agent_id": "a", "_port": SimulatedAgent(VirtualClock())}),
        McpClientAdapter,
    )


def test_probe_identity_is_stable_and_pull_only() -> None:
    adapter, _ = _adapter()
    p1, p2 = adapter.probe(), adapter.probe()
    assert (
        p1.identity_hash
        == p2.identity_hash
        == identity_hash("agent-c", "mcp", ["cap-echo"], ["pull"], "supported")
    )
    assert p1.delivery_modes == (c.DeliveryMode.PULL,)
    assert isinstance(adapter, c.Adapter)


def test_unsupported_tool_and_secret_advertisement() -> None:
    adapter, agent = _adapter(secret_handles=False)
    assert adapter.probe().secret_handles == "unsupported"
    with pytest.raises(c.AdapterError) as exc:
        adapter.invoke(
            "cap-other", {}, agent.now() + dt.timedelta(minutes=1), (), correlation_id="k"
        )
    assert exc.value.code == "CAPABILITY_UNSUPPORTED"
    item = c.WorkItemView(
        "wi-s1",
        "invoke",
        "agent-c",
        None,
        "corr",
        agent.now() + dt.timedelta(hours=1),
        "colab://work/wi-s1/payload",
        ("sec-1",),
        "colab.work-result.v1",
        "wi-s1",
        {"tool": "cap-echo"},
    )
    assert adapter.deliver(item).rejection_code == "CAPABILITY_UNSUPPORTED"
    assert not any("sec-1" in line for line in agent.log_lines)


def test_invoke_echoes_correlation_and_usage_and_times_out() -> None:
    adapter, agent = _adapter()
    res = adapter.invoke(
        "cap-echo",
        {"input": {"n": 1}},
        agent.now() + dt.timedelta(minutes=5),
        (),
        correlation_id="corr-1",
    )
    assert (
        res.correlation_id == "corr-1"
        and res.result["echo"] == {"n": 1}
        and res.usage.model == "sim-1"
    )
    agent.result_delay_s = 3600
    with pytest.raises(c.AdapterError) as exc:
        adapter.invoke(
            "cap-echo",
            {"input": {"n": 2}},
            agent.now() + dt.timedelta(minutes=5),
            (),
            correlation_id="corr-2",
        )
    assert exc.value.code == "ADAPTER_TIMEOUT" and exc.value.retryable


def test_normalize_error_mapping() -> None:
    adapter, _ = _adapter()
    cases = [
        (TimeoutError("t"), "ADAPTER_TIMEOUT"),
        (ConnectionError("c"), "ADAPTER_UNREACHABLE"),
        (PermissionError("p"), "ADAPTER_AUTH_FAILED"),
        (ValueError("v"), "ADAPTER_BAD_RESPONSE"),
        (RuntimeError("RATE_LIMIT exceeded"), "ADAPTER_RATE_LIMITED"),
        (RuntimeError("boom"), "ADAPTER_INTERNAL"),
    ]
    assert [adapter.normalize_error(e).code for e, _ in cases] == [code for _, code in cases]
