"""Verifier-only adapter: supply exported live-test credentials without reading repository .env."""

from __future__ import annotations

import os
import sys

import pytest


REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TEST_CHAT_A", "TELEGRAM_TEST_CHAT_B")


class ExportedEnvironmentPlugin:
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        values = {name: os.environ[name] for name in REQUIRED if os.environ.get(name)}
        if len(values) != len(REQUIRED):
            raise RuntimeError("required Telegram test variables are not all exported")
        for item in items:
            module = item.module
            if hasattr(module, "_load_env"):
                module._load_env = lambda values=values: dict(values)
            item.own_markers[:] = [mark for mark in item.own_markers if mark.name != "skipif"]


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "-rs",
                "tests/integration/test_bridge_live.py",
                "tests/integration/test_telegram_provider.py",
            ],
            plugins=[ExportedEnvironmentPlugin()],
        )
    )
