"""The write path must not accumulate Python objects (V-P7-04, the leak half).

The 24-hour soak measures memory from outside the process, where it cannot tell retained objects
from memory the allocator is holding. This measures from inside: it drives the real command path
thousands of times in one process and asserts that the Python heap and the live object count come
back to where they started.

Nothing here is mocked — the bus, the policy check, the Event store and PostgreSQL are all real.
If a handler, a cache or a registry retained one object per command, both numbers would climb in
step with the loop.

The probe itself is ``tools.memory_diagnostics``, so the same measurement can be re-run against any
environment rather than only inside pytest.
"""

from __future__ import annotations

import pytest

from tools import memory_diagnostics

pytestmark = pytest.mark.db

#: Enough commands that a per-command retention of even a few hundred bytes is unmistakable, and
#: few enough to stay a fast test. A one-kilobyte-per-command leak shows up as +4 MB.
WARMUP = 200
COMMANDS = 4000
#: Import machinery, first-use caches and the connection pool all allocate once, before the
#: measurement starts. What is left is per-command retention.
HEAP_GROWTH_LIMIT_MB = 1.0
OBJECT_GROWTH_LIMIT = 2000


@pytest.fixture(scope="module")
def measurement(database_url: str) -> dict[str, float]:
    import os

    os.environ["AGENT_COLAB_TEST_DATABASE_URL"] = database_url
    return memory_diagnostics.command_path(WARMUP, COMMANDS)


def test_thousands_of_commands_retain_no_python_objects(measurement: dict[str, float]) -> None:
    assert measurement["heap_growth_mb"] <= HEAP_GROWTH_LIMIT_MB, (
        f"the Python heap grew {measurement['heap_growth_mb']} MB across {COMMANDS} commands "
        f"({measurement['bytes_per_command']} bytes retained per command)"
    )
    assert measurement["object_growth"] <= OBJECT_GROWTH_LIMIT, (
        f"{measurement['object_growth']} Python objects survived {COMMANDS} commands "
        f"({measurement['objects_per_command']} per command)"
    )
