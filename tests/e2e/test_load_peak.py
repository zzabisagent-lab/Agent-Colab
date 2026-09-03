"""V-P7-03: 3x the §21.1 normal profile sustained for 30 minutes against a real API process and
real scheduler workers — zero Event or Run loss or duplicates, 5xx below 1 %, write p95 at most
500 ms and read p95 at most 300 ms.

The duration is ``AGENT_COLAB_LOAD_MINUTES`` (default 30, the criterion). A shorter value runs the
same code path for a smoke check; the recorded evidence is the full 30-minute run.
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
from tests.load.profile import MAX_5XX_RATE, PEAK, READ_P95_MS, WRITE_P95_MS
from tests.load.run import check

pytestmark = pytest.mark.db
MINUTES = float(os.environ.get("AGENT_COLAB_LOAD_MINUTES", "30"))
WORKERS = int(os.environ.get("AGENT_COLAB_LOAD_WORKERS", "2"))


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def population(engine: Engine) -> Population:
    return seed_population(engine, PEAK)


def test_peak_load_meets_the_latency_and_loss_criteria(
    engine: Engine, database_url: str, population: Population
) -> None:
    report = harness.run_load(
        engine,
        database_url,
        population,
        PEAK,
        seconds=MINUTES * 60.0,
        workers=WORKERS,
    )
    summary = report.summary()
    print("peak load:", json.dumps(summary, indent=2))

    assert not check(summary), summary
    assert summary["write_p95_ms"] <= WRITE_P95_MS
    assert summary["read_p95_ms"] <= READ_P95_MS
    assert summary["error_rate"] <= MAX_5XX_RATE
    assert summary["duplicate_occurrences"] == 0 and summary["duplicate_events"] == 0
    assert summary["events_created"] >= summary["writes"], "every accepted write recorded an Event"
    if MINUTES >= 10:  # long enough for the five-minute schedule cycle to fire
        assert summary["runs_created"] > 0, "no Schedule Run materialised during the window"
