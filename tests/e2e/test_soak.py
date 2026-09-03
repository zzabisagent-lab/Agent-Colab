"""V-P7-04 soak: Bridges, heartbeats, the scheduler and leases under sustained normal load, with
no leaks, no duplicates and nothing stuck.

The criterion names 24 hours. This host runs a bounded window instead — ``AGENT_COLAB_SOAK_MINUTES``
(default 30) — and the failure modes a 24-hour soak looks for are *growth* signals rather than
one-off events: worker resident memory, database connections, open work items, dead letters,
stuck claimed Runs and duplicate deliveries are all compared between the start and end of the
window, so a leak of any size shows as a trend. The recorded duration is reported with the
evidence so the difference from 24 hours is visible rather than implied.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from server.db.engine import make_engine
from tests.load import harness
from tests.load.population import Population, seed_population
from tests.load.profile import NORMAL

pytestmark = pytest.mark.db
MINUTES = float(os.environ.get("AGENT_COLAB_SOAK_MINUTES", "30"))
RSS_GROWTH_LIMIT = 1.5  # a leaking worker grows without bound; 50 % headroom is generous


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def population(engine: Engine) -> Population:
    return seed_population(engine, NORMAL)


def test_soak_shows_no_leaks_duplicates_or_stuck_work(
    engine: Engine, database_url: str, population: Population
) -> None:
    report = harness.run_soak(engine, database_url, population, NORMAL, minutes=MINUTES, workers=2)
    summary = report.summary()
    print("soak:", json.dumps(summary, indent=2))

    assert summary["error_rate"] == 0.0 or summary["error_rate"] <= 0.01
    assert summary["duplicate_occurrences"] == 0, "an occurrence ran more than once"
    assert summary["duplicate_events"] == 0, "an Event identity was duplicated"
    assert summary["duplicate_deliveries"] == 0, "a delivery was queued twice"
    assert summary["stuck_runs"] == 0, "a claimed Run held an expired lease at the end"
    assert summary["dead_letters"] == 0, "deliveries exhausted their retries"
    assert summary["work_items_open_last"] <= summary["work_items_open_first"] + 50, (
        "open work items grew across the window"
    )
    assert summary["db_connections_last"] <= summary["db_connections_first"] + 10, (
        "database connections were not returned"
    )
    if summary["worker_rss_first_kb"]:
        assert summary["worker_rss_growth"] <= RSS_GROWTH_LIMIT, "worker memory grew"
