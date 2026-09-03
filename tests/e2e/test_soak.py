"""V-P7-04: the 24-hour soak, asserted against the recorded run.

The criterion is a day of sustained normal load with no leaks, no duplicates and nothing stuck.
A day cannot be spent inside a test, so the run and the assertions are separated: the run is
``tests/load/soak.py``, which drives real API processes, real scheduler workers, real load
generators and real Agent heartbeats for 24 hours and appends one JSON sample per minute; this
test reads the finished file and asserts the criterion over the whole series.

The separation is what makes the check honest. A soak fails through *growth* and through *state
that stops being cleaned up*, and both are invisible in a first/last pair — a leak that only bites
after eight hours, or leases that stop being reclaimed at hour twenty, look exactly like a healthy
run when only the endpoints are compared. Reading every minute makes them visible.

A shorter window is not a smaller version of this evidence, so a short file is rejected outright:
:func:`test_short_sample_file_is_rejected` pins that a run of less than 24 hours **fails** here
rather than being skipped, quietly accepted, or substituted for the real thing. ``--minutes`` on
the runner exists for smoke runs of the machinery; a smoke file cannot satisfy this test.

Set ``AGENT_COLAB_SOAK_SAMPLES`` to assert a different sample file.
"""

from __future__ import annotations

import itertools
import json
import os
import statistics
from pathlib import Path
from typing import Any

import pytest

from tests.load import samples
from tests.load.profile import MAX_5XX_RATE

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_FILE = Path(
    os.environ.get("AGENT_COLAB_SOAK_SAMPLES", ROOT / "evidence" / "phase-7" / "soak-24h.jsonl")
)
#: How many samples at each end are averaged for the memory trend. One minute of resident memory
#: is noisy; ten minutes is not, and a leak large enough to matter is visible in either.
EDGE = 10
#: A sample may fail to be taken — a query timing out under load is not a soak failure — but the
#: series has to be almost complete for it to be evidence.
MAX_SAMPLE_ERROR_RATE = 0.01


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _series(rows: list[dict[str, Any]], field: str) -> list[float]:
    return [float(r[field]) for r in rows if r.get(field) is not None]


def _longest_run(rows: list[dict[str, Any]], field: str) -> int:
    """The longest streak of consecutive samples where ``field`` was non-zero."""
    longest = current = 0
    for row in rows:
        current = current + 1 if row.get(field) else 0
        longest = max(longest, current)
    return longest


@pytest.fixture(scope="module")
def soak() -> list[dict[str, Any]]:
    if not SAMPLE_FILE.exists():
        pytest.fail(
            f"no soak samples at {SAMPLE_FILE}. Record a run first:\n"
            "  AGENT_COLAB_TEST_DATABASE_URL=... uv run python -m tests.load.soak "
            f"--profile normal --minutes 1440 --samples {SAMPLE_FILE}"
        )
    rows = samples.read_samples(SAMPLE_FILE)
    if not rows:
        pytest.fail(f"{SAMPLE_FILE} holds no samples")
    return rows


def test_sample_file_covers_the_full_24_hours(soak: list[dict[str, Any]]) -> None:
    """The criterion is a duration. A shorter window fails here; it is never skipped."""
    covered = samples.coverage_seconds(soak)
    required = samples.REQUIRED_SECONDS - samples.COVERAGE_SLACK_S
    assert soak[-1].get("final"), (
        "the last sample is not the closing one: the run did not finish cleanly"
    )
    assert covered >= required, (
        f"the soak covers {covered / 3600:.2f} h, and the criterion is 24 h. "
        "A shorter window does not demonstrate it."
    )
    assert soak[-1].get("profile") == "normal", "the soak must run the §21.1 normal profile"
    errors = sum(1 for row in soak if row.get("sample_error"))
    assert errors <= max(1, int(len(soak) * MAX_SAMPLE_ERROR_RATE)), (
        f"{errors} of {len(soak)} samples failed to be taken"
    )


def test_short_sample_file_is_rejected(tmp_path: Path) -> None:
    """A smoke run must not be able to stand in for the day."""
    short = tmp_path / "smoke.jsonl"
    short.write_text(
        json.dumps({"elapsed_s": 1800.0, "final": True, "profile": "normal"}) + "\n",
        encoding="utf-8",
    )
    rows = samples.read_samples(short)
    covered = samples.coverage_seconds(rows)
    assert covered < samples.REQUIRED_SECONDS - samples.COVERAGE_SLACK_S
    with pytest.raises(AssertionError, match="criterion is 24 h"):
        test_sample_file_covers_the_full_24_hours(rows)


def test_traffic_was_sustained_for_the_whole_window(soak: list[dict[str, Any]]) -> None:
    last = soak[-1]
    writes, reads = int(last["writes"]), int(last["reads"])
    assert writes > 0 and reads > 0, "no traffic was recorded"
    error_rate = int(last["errors"]) / max(1, writes + reads)
    assert error_rate <= MAX_5XX_RATE, f"5xx rate {error_rate:.3%} over the window"
    # traffic never stalled: every hour of the run added writes
    by_hour: dict[int, int] = {}
    for row in soak:
        by_hour[int(float(row["elapsed_s"]) // 3600)] = int(row["writes"])
    progress = [by_hour[hour] for hour in sorted(by_hour)]
    assert all(b > a for a, b in itertools.pairwise(progress)), (
        "writes stopped advancing during the run"
    )
    assert int(last["events"]) > 0, "writes were accepted but no Event was recorded"
    assert int(last["runs"]) > 0, "no scheduled Run was created in 24 hours"


def test_nothing_was_delivered_or_run_twice(soak: list[dict[str, Any]]) -> None:
    for field, message in (
        ("duplicate_occurrence_keys", "an occurrence ran more than once"),
        ("duplicate_events", "an Event identity was duplicated"),
        ("duplicate_deliveries", "a delivery was queued twice"),
        ("bridge_relay_duplicates", "a bridge relayed the same message twice"),
    ):
        worst = max(_series(soak, field), default=0.0)
        assert worst == 0, f"{message} ({field} peaked at {worst:.0f})"


def test_no_run_stayed_stuck_and_no_delivery_died(soak: list[dict[str, Any]]) -> None:
    assert max(_series(soak, "dead_letters"), default=0.0) == 0, "a delivery exhausted its retries"
    assert int(soak[-1]["stuck_claimed_runs"]) == 0, (
        "a claimed Run still held an expired lease when the run ended"
    )
    streak = _longest_run(soak, "stuck_claimed_runs")
    assert streak <= samples.STUCK_TOLERANCE_SAMPLES, (
        f"a claimed Run held an expired lease for {streak} consecutive minutes: "
        "leases stopped being reclaimed"
    )


def test_work_items_drained_to_zero(soak: list[dict[str, Any]]) -> None:
    open_items = _series(soak, "open_work_items")
    assert int(soak[-1]["open_work_items"]) == 0, (
        f"{soak[-1]['open_work_items']} work items were still open at the end"
    )
    # and the backlog never ran away: the peak is not a multiple of the plateau
    assert max(open_items, default=0.0) <= _median(open_items) + 100, (
        "open work items grew far beyond their steady state"
    )


def test_heartbeats_stayed_fresh(soak: list[dict[str, Any]]) -> None:
    ages = _series(soak, "heartbeat_age_s")
    assert ages, "no heartbeat age was recorded"
    assert max(ages) <= samples.HEARTBEAT_STALE_S, (
        f"the oldest Agent heartbeat reached {max(ages):.0f} s, past the "
        f"{samples.HEARTBEAT_STALE_S:.0f} s liveness threshold"
    )
    assert max(_series(soak, "stale_agents"), default=0.0) == 0, "an Agent went stale"
    beats = _series(soak, "heartbeats")
    assert beats[-1] > beats[0], "heartbeats stopped being recorded"


def test_memory_growth_stayed_bounded(soak: list[dict[str, Any]]) -> None:
    for who in ("worker", "server"):
        series = _series(soak, f"{who}_rss_kb")
        assert series, f"no {who} memory was sampled"
        first, last = _median(series[:EDGE]), _median(series[-EDGE:])
        assert first > 0
        growth = last / first
        assert growth <= samples.RSS_GROWTH_LIMIT, (
            f"{who} resident memory grew {growth:.3f}x over 24 h "
            f"({first:.0f} kB to {last:.0f} kB), past {samples.RSS_GROWTH_LIMIT}x"
        )
        assert max(series) / first <= samples.RSS_PEAK_LIMIT, (
            f"{who} resident memory peaked at {max(series) / first:.2f}x its opening level"
        )


def test_database_connections_were_returned(soak: list[dict[str, Any]]) -> None:
    """Connections should plateau. A soak fails when they climb and are never given back."""
    series = _series(soak, "db_connections")
    assert series, "no connection count was sampled"
    warm = [
        float(row["db_connections"])
        for row in soak
        if samples.WARM_FROM_S <= float(row["elapsed_s"]) <= samples.WARM_TO_S
        and row.get("db_connections") is not None
    ]
    baseline = _median(warm) if warm else _median(series[:EDGE])
    drift = _median(series[-60:]) - baseline
    assert drift <= samples.CONNECTION_DRIFT_LIMIT, (
        f"database connections drifted {drift:+.0f} between the first hour and the last "
        f"(baseline {baseline:.0f}): connections were not returned to the pool"
    )
    assert max(series) - baseline <= samples.CONNECTION_SPREAD_LIMIT, (
        f"database connections peaked {max(series) - baseline:.0f} above the warmed baseline"
    )
