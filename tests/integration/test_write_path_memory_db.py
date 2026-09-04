"""The write path must not accumulate Python objects (V-P7-04, the leak half).

The 24-hour soak measures memory from outside the process, where a pre-forked worker pool makes
summed RSS climb on its own as inherited copy-on-write pages stop being shared. That artefact is
real but is not a leak, and no external measurement can tell the two apart on its own. This does
it from inside: it drives the real command path thousands of times in one process and asserts that
the Python heap and the object count come back to where they started.

Nothing here is mocked — the bus, the policy check, the Event store and PostgreSQL are all real.
If a handler, a cache or a registry retained one object per command, both numbers would climb in
step with the loop.
"""

from __future__ import annotations

import datetime as dt
import gc
import tracemalloc
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import tasks as tasks_app
from server.application.authz import BusAuthorizer
from server.application.bus import Principal
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACCT = uuid.uuid4()
CHANNEL = uuid.uuid4()
CLOCK = FixedClock(dt.datetime(2026, 9, 4, tzinfo=dt.UTC))
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)
PERMS = ["task.create", "task.read", "task.delegate", "task.cancel"]

#: Enough commands that a per-command retention of even a few hundred bytes is unmistakable,
#: and few enough to stay a fast test. A one-kilobyte-per-command leak shows up as +4 MB.
WARMUP = 200
MEASURED = 4000
#: Import machinery, first-use caches and the connection pool all allocate once. The measurement
#: starts after the warm-up, so what is left is per-command retention.
HEAP_GROWTH_LIMIT_MB = 1.0
OBJECT_GROWTH_LIMIT = 2000


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :n, 'leak')"),
            {"i": WS, "n": f"ws-leak-{WS.hex[:8]}"},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, :a, :w, 'human', 'leak')"
            ),
            {"i": ACCT, "a": f"acct-leak-{ACCT.hex[:8]}", "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, 'work', 'leak')"
            ),
            {"i": CHANNEL, "c": f"chan-leak-{CHANNEL.hex[:8]}", "w": WS},
        )
        c.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": CHANNEL, "a": ACCT},
        )
    with Session(eng) as s, s.begin():
        repo = PostgresPolicyRepository()
        role = f"leak-role-{WS.hex[:8]}"
        repo.create_role(s, WS, role, "leak")
        repo.commit_role_version(s, role, PERMS, [], {"max_risk": "MEDIUM"}, ACCT)
        repo.assign_role(s, ACCT, role, ACCT, CLOCK.now())
    yield eng
    eng.dispose()


def test_thousands_of_commands_retain_no_python_objects(engine: Engine) -> None:
    runtime = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))
    who = Principal(f"acct-leak-{ACCT.hex[:8]}", str(ACCT), "human", "sha256:leak")
    run = 0

    def create(count: int) -> None:
        nonlocal run
        for _ in range(count):
            run += 1
            execute_command(
                runtime,
                who,
                tasks_app.CreateTask(
                    f"leak {run}", str(CHANNEL), "research", "LOW", criteria=CRITERIA
                ),
                idempotency_key=f"leak-{WS.hex[:8]}-{run}",
                correlation_id="leak",
            )

    create(WARMUP)
    gc.collect()
    tracemalloc.start()
    heap_before, _ = tracemalloc.get_traced_memory()
    objects_before = len(gc.get_objects())

    create(MEASURED)

    gc.collect()
    heap_after, _ = tracemalloc.get_traced_memory()
    objects_after = len(gc.get_objects())
    tracemalloc.stop()

    heap_growth_mb = (heap_after - heap_before) / 1e6
    object_growth = objects_after - objects_before
    assert heap_growth_mb <= HEAP_GROWTH_LIMIT_MB, (
        f"the Python heap grew {heap_growth_mb:.2f} MB across {MEASURED} commands "
        f"({heap_growth_mb * 1e6 / MEASURED:.0f} bytes retained per command)"
    )
    assert object_growth <= OBJECT_GROWTH_LIMIT, (
        f"{object_growth} Python objects survived {MEASURED} commands "
        f"({object_growth / MEASURED:.2f} per command)"
    )
