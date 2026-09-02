"""V-P3-12 product neutrality: a third-party adapter type registered only through
``AGENT_COLAB_ADAPTER_PLUGINS=tests.fixtures.adapters.mock_plugin:register`` — no core change."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from server.agents.adapters.mcp_client import McpClientAdapter
from server.agents.conformance.harness import McpSimulationHarness, register_harness

ADAPTER_TYPE = "mockvendor"


class MockVendorAdapter(McpClientAdapter):
    adapter_type: str = ADAPTER_TYPE


def _factory(endpoint: Mapping[str, Any]) -> Any:
    return MockVendorAdapter(endpoint, endpoint["_port"], adapter_type=ADAPTER_TYPE)


def register(register_adapter_type: Callable[..., None]) -> None:
    register_adapter_type(ADAPTER_TYPE, _factory, replace=True)
    register_harness(
        ADAPTER_TYPE, lambda endpoint: McpSimulationHarness(endpoint, adapter_type=ADAPTER_TYPE)
    )
