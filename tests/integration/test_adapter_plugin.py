"""V-P3-12 product neutrality: a mock adapter type registered only via
``AGENT_COLAB_ADAPTER_PLUGINS`` participates in the conformance suite without core changes."""

from __future__ import annotations

import subprocess
import sys

import pytest

from server.agents.adapters import contract


def test_plugin_type_is_unknown_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_COLAB_ADAPTER_PLUGINS", raising=False)
    contract.reset_plugins_for_tests()
    assert "mockvendor" not in contract.adapter_types()


def test_plugin_registers_and_passes_the_suite() -> None:
    env = {"AGENT_COLAB_ADAPTER_PLUGINS": "tests.fixtures.adapters.mock_plugin:register"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "server.agents.conformance",
            "--adapter",
            "mockvendor",
            "--endpoint",
            '{"agent_id": "agent-vendor", "capabilities": ["cap_echo"]}',
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**__import__("os").environ, **env},
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert '"adapter_type": "mockvendor"' in proc.stdout and '"result": "PASS"' in proc.stdout
    # the core registry was not edited: no built-in module names the plugin type
    from pathlib import Path

    core = Path("server/agents").rglob("*.py")
    assert not any("mockvendor" in p.read_text() for p in core)
