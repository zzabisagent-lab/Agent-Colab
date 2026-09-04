"""Separate a memory leak from allocator growth (V-P7-04, development plan §20 P7-04).

A 24-hour soak reports that memory grew. It cannot say *what* grew, and the difference decides
whether there is a defect: retained Python objects are a leak and must be fixed, while anonymous
memory the allocator holds over a flat heap is not, however alike the two look from outside.

Two probes live here because they need no test fixtures and are useful against any environment:

``command-path``
    Drives the real command bus, policy check and Event store in-process against a real database
    and reads the Python heap and live object count directly. This is the one that decides
    "leak or not": a handler, cache or registry retaining one object per command moves both
    numbers in step with the loop.
``trim``
    Calls ``malloc_trim(0)`` and reports what it returned. Distinguishes free memory the allocator
    is holding, which trimming releases, from memory genuinely still in use.

    uv run python -m tools.memory_diagnostics command-path
    uv run python -m tools.memory_diagnostics trim

The two probes that need a running server are tests rather than tools, because they need the load
harness: ``tests/e2e/test_http_path_memory.py`` drives a real single worker over loopback and
watches for a plateau, and ``tests/e2e/test_soak.py`` reads the 24-hour run. Findings from all four
are recorded in ``evidence/phase-7/soak/memory-investigation.md``.

``AGENT_COLAB_TEST_DATABASE_URL`` is required for ``command-path``; it creates its own Workspace
inside that database, so a run never disturbs another.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sys
import tracemalloc
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROC = Path("/proc")
CRITERIA = ({"statement": "probe", "check_type": "evidence", "required": True},)
PERMISSIONS = ["task.create", "task.read", "task.delegate", "task.cancel"]


def private_kb(pid: int | str = "self") -> int:
    """Private (unshared) resident memory: what this process holds on its own.

    Summed ``VmRSS`` counts a shared page once per process that maps it, which overstates a
    pre-forked pool by its whole shared set. Private memory counts each page once, for whoever
    holds it.
    """
    rollup = (PROC / str(pid) / "smaps_rollup").read_text(encoding="utf-8")
    return sum(
        int(line.split()[1])
        for line in rollup.split("\n")
        if line.startswith(("Private_Clean:", "Private_Dirty:"))
    )


def status_kb(field: str, pid: int | str = "self") -> int:
    """One ``/proc/<pid>/status`` size field in kilobytes, e.g. ``VmRSS`` or ``RssAnon``."""
    status = (PROC / str(pid) / "status").read_text(encoding="utf-8")
    return int(status.split(f"{field}:")[1].split()[0])


@dataclass(frozen=True)
class ProbeWorkspace:
    """A disposable Workspace with one principal permitted to drive the write path."""

    runtime: Any
    principal: Any
    workspace_id: str
    channel_id: str


def build_workspace(database_url: str) -> ProbeWorkspace:
    """Seed a Workspace, account, channel and role, and return a Runtime bound to them."""
    import datetime as dt

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from server.api.dispatch import Runtime
    from server.application.authz import BusAuthorizer
    from server.application.bus import Principal
    from server.db.engine import make_engine, make_session_factory, run_migrations
    from server.domain.clock import FixedClock
    from server.policy.repository import PostgresPolicyRepository

    tag = uuid.uuid4().hex[:8]
    workspace, account, channel = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    account_id, role = f"acct-mem-{tag}", f"mem-role-{tag}"
    clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    # A disposable test database is often empty between runs; migrating is idempotent.
    run_migrations(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, 'memory probe')"),
            {"i": workspace, "w": f"ws-mem-{tag}"},
        )
        conn.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, :a, :w, 'human', 'memory probe')"
            ),
            {"i": account, "a": account_id, "w": workspace},
        )
        conn.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, 'work', 'memory probe')"
            ),
            {"i": channel, "c": f"chan-mem-{tag}", "w": workspace},
        )
        conn.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": channel, "a": account},
        )
    with Session(engine) as session, session.begin():
        repo = PostgresPolicyRepository()
        repo.create_role(session, workspace, role, "memory probe")
        repo.commit_role_version(session, role, PERMISSIONS, [], {"max_risk": "MEDIUM"}, account)
        repo.assign_role(session, account, role, account, clock.now())
    return ProbeWorkspace(
        runtime=Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(workspace)),
        principal=Principal(account_id, str(account), "human", f"sha256:{account_id}"),
        workspace_id=str(workspace),
        channel_id=str(channel),
    )


def run_commands(probe: ProbeWorkspace, count: int, offset: int = 0) -> None:
    """Execute ``count`` real CreateTask commands through the full dispatch path."""
    from server.api.dispatch import execute_command
    from server.application import tasks as tasks_app

    for index in range(count):
        run = offset + index
        execute_command(
            probe.runtime,
            probe.principal,
            tasks_app.CreateTask(
                f"memory probe {run}", probe.channel_id, "research", "LOW", criteria=CRITERIA
            ),
            idempotency_key=f"mem-{probe.workspace_id[:8]}-{run}",
            correlation_id="memory-probe",
        )


def command_path(warmup: int, commands: int) -> dict[str, Any]:
    """Retention in the write path. Warm-up first: first use of anything allocates once."""
    probe = build_workspace(os.environ["AGENT_COLAB_TEST_DATABASE_URL"])
    run_commands(probe, warmup)
    gc.collect()
    tracemalloc.start()
    heap_before, _ = tracemalloc.get_traced_memory()
    objects_before = len(gc.get_objects())
    private_before = private_kb()

    run_commands(probe, commands, offset=warmup)

    gc.collect()
    heap_after, _ = tracemalloc.get_traced_memory()
    objects_after = len(gc.get_objects())
    tracemalloc.stop()
    return {
        "probe": "command-path",
        "commands": commands,
        "heap_growth_mb": round((heap_after - heap_before) / 1e6, 3),
        "object_growth": objects_after - objects_before,
        "bytes_per_command": round((heap_after - heap_before) / commands, 1),
        "objects_per_command": round((objects_after - objects_before) / commands, 3),
        "private_growth_kb": private_kb() - private_before,
    }


def trim() -> dict[str, Any]:
    """What the allocator is holding free rather than returning to the operating system."""
    gc.collect()
    rss_before, private_before = status_kb("VmRSS"), private_kb()
    returned = ctypes.CDLL("libc.so.6").malloc_trim(0)
    return {
        "probe": "trim",
        "malloc_trim_returned": int(returned),
        "rss_released_kb": rss_before - status_kb("VmRSS"),
        "private_released_kb": private_before - private_kb(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="probe", required=True)
    command = sub.add_parser("command-path", help="retention in the write path")
    command.add_argument("--warmup", type=int, default=200)
    command.add_argument("--commands", type=int, default=4000)
    sub.add_parser("trim", help="free memory the allocator has not returned")
    args = parser.parse_args(argv)
    result = command_path(args.warmup, args.commands) if args.probe == "command-path" else trim()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
