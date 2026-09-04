"""The HTTP path must reach a memory plateau (V-P7-04, the leak half).

The 24-hour soak answers this too, but only after a day, and its reading is confounded: eight
forked API workers, scheduler workers and heartbeats all warm up together, so a first/last pair
cannot separate warm-up from a leak. This drives the real server process over loopback with a
*single* worker and watches the shape instead.

Warm-up and a leak both add memory. They differ in what happens next: caches fill and allocator
arenas reach steady state, then stop; a leak under steady load keeps its slope. So the second half
of the run is compared with the first, which needs no advance knowledge of either rate.

Its companion, ``tests/integration/test_write_path_memory_db.py``, measures the command path from
inside the interpreter. This one measures everything the command path does not: routing, request
and response models, authentication, the JSON codecs and the connection pool.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

from server.db.engine import make_engine
from tests.load import harness
from tests.load.population import seed_population
from tests.load.profile import PROFILES

pytestmark = pytest.mark.db

#: Two requests per iteration (one write, one read). The plateau is not reached until roughly six
#: thousand requests — caches and allocator arenas are still filling before that — so a shorter run
#: measures warm-up and reports it as retention. Twelve batches of 350 puts the whole second half
#: past that point, which is what makes the steady-state figure mean anything.
BATCHES, PER_BATCH = 12, 350
#: Steady-state retention. The measured figure is ~20 bytes/request and falling to zero; a leak
#: worth the name is orders of magnitude above this.
BYTES_PER_REQUEST_LIMIT = 400


def _private_kb(pid: int) -> int:
    total = 0
    for line in (Path("/proc") / str(pid) / "smaps_rollup").read_text().split("\n"):
        if line.startswith(("Private_Clean:", "Private_Dirty:")):
            total += int(line.split()[1])
    return total


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


def test_private_memory_plateaus_under_sustained_http_traffic(engine: Engine) -> None:
    pop = seed_population(engine, PROFILES["normal"], tag=f"hp{uuid.uuid4().hex[:6]}")
    channels = [str(c) for c in pop.channel_uuids[:10]]
    assert channels, "the seeded population must expose channels to write to"
    pids: list[int] = []
    series: list[int] = []

    with harness.running_server(str(engine.url), workers=1, pids=pids) as base:
        with httpx.Client(base_url=base, timeout=30.0) as client:
            head = {"Authorization": f"Bearer {pop.owner_token}"}
            sent = 0
            for _ in range(BATCHES):
                for _ in range(PER_BATCH):
                    sent += 1
                    written = client.post(
                        "/api/v1/tasks",
                        json={
                            "title": f"probe {sent}",
                            "channel_id": channels[sent % len(channels)],
                            "domain": "research",
                            "risk": "LOW",
                            "criteria": [
                                {"statement": "p", "check_type": "evidence", "required": True}
                            ],
                        },
                        headers={**head, "Idempotency-Key": f"probe-{uuid.uuid4().hex}"},
                    )
                    assert written.status_code < 400, written.text[:200]
                    read = client.get("/api/v1/tasks?limit=20", headers=head)
                    assert read.status_code < 400, read.text[:200]
                series.append(sum(_private_kb(p) for p in harness.process_tree(pids)))

    assert len(series) == BATCHES and all(series), "private memory was not sampled"
    half = len(series) // 2
    # The first batch is warm-up by definition; the comparison starts after it.
    first_half = series[half] - series[1]
    second_half = series[-1] - series[half]
    requests_in_second_half = (len(series) - 1 - half) * PER_BATCH * 2

    assert second_half <= first_half, (
        f"private memory grew {second_half} kB in the second half against {first_half} kB in the "
        "first: growth is not decelerating, which is what a leak looks like"
    )
    per_request = max(second_half, 0) * 1024 / requests_in_second_half
    assert per_request <= BYTES_PER_REQUEST_LIMIT, (
        f"{per_request:.0f} bytes retained per request at steady state, past "
        f"{BYTES_PER_REQUEST_LIMIT}"
    )
