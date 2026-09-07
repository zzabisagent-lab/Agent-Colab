"""The write path must not accumulate Python objects (V-P7-04, the leak half).

The 24-hour soak measures memory from outside the process, where it cannot tell retained objects
from memory the allocator is holding. This measures from inside: it drives the real command path
thousands of times and asserts that the Python heap and the live object count come back to where
they started.

The measurement runs in a **subprocess**, which is not fastidiousness. Run in-process inside the
full suite, it reported 2,371 bytes retained per command against 55 when run alone: it was
measuring whatever several hundred other test modules had left in the interpreter — global
registries, warmed caches, module-level hooks — and not the write path at all. A memory
measurement is only meaningful in a process whose history it controls.

The probe itself is ``tools.memory_diagnostics``, so the same measurement can be re-run by hand
against any environment.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
#: Enough commands that a per-command retention of even a few hundred bytes is unmistakable, and
#: few enough to stay a fast test. A one-kilobyte-per-command leak shows up as +4 MB.
WARMUP = 200
COMMANDS = 4000
#: Import machinery, first-use caches and the connection pool all allocate once, before the
#: measurement starts. What is left is per-command retention.
HEAP_GROWTH_LIMIT_MB = 1.0
OBJECT_GROWTH_LIMIT = 2000
TIMEOUT_S = 900


@pytest.fixture(scope="module")
def measurement(database_url: str) -> dict[str, float]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.memory_diagnostics",
            "command-path",
            "--warmup",
            str(WARMUP),
            "--commands",
            str(COMMANDS),
        ],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "AGENT_COLAB_TEST_DATABASE_URL": database_url,
            "PYTHONPATH": str(ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_S,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr[-2000:]}"
    return dict(json.loads(result.stdout[result.stdout.index("{") :]))


def test_thousands_of_commands_retain_no_python_objects(measurement: dict[str, float]) -> None:
    assert measurement["heap_growth_mb"] <= HEAP_GROWTH_LIMIT_MB, (
        f"the Python heap grew {measurement['heap_growth_mb']} MB across {COMMANDS} commands "
        f"({measurement['bytes_per_command']} bytes retained per command)"
    )
    assert measurement["object_growth"] <= OBJECT_GROWTH_LIMIT, (
        f"{measurement['object_growth']} Python objects survived {COMMANDS} commands "
        f"({measurement['objects_per_command']} per command)"
    )
