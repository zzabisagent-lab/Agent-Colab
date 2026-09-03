"""CLI for the load and soak harness (P7-04).

    uv run python -m tests.load.run --profile peak --minutes 30
    uv run python -m tests.load.run --profile smoke --minutes 1 --json report.json

Creates a disposable database, seeds the §21.1 population, drives the profile for the requested
wall-clock time against a real API process and real scheduler workers, then prints the measured
report and exits non-zero when a §21.1 criterion is missed.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from server.db.engine import make_engine, normalize_url, run_migrations
from tests.load import harness
from tests.load.population import seed_population
from tests.load.profile import MAX_5XX_RATE, PROFILES, READ_P95_MS, WRITE_P95_MS


@contextmanager
def disposable_database(maintenance_url: str) -> Iterator[str]:
    """A fresh migrated database, dropped when the run ends."""
    base = normalize_url(maintenance_url)
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"colab_load_{uuid.uuid4().hex[:10]}"
    with maint.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.rsplit("/", 1)[0] + f"/{name}"
    try:
        run_migrations(url)
        yield url
    finally:
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        maint.dispose()


def check(summary: dict[str, Any]) -> list[str]:
    """§21.1 criteria: latency percentiles, error rate, and zero loss or duplicates."""
    failures: list[str] = []
    if summary["write_p95_ms"] > WRITE_P95_MS:
        failures.append(f"write p95 {summary['write_p95_ms']} ms > {WRITE_P95_MS:.0f} ms")
    if summary["read_p95_ms"] > READ_P95_MS:
        failures.append(f"read p95 {summary['read_p95_ms']} ms > {READ_P95_MS:.0f} ms")
    if summary["error_rate"] > MAX_5XX_RATE:
        failures.append(f"5xx rate {summary['error_rate']:.3%} > {MAX_5XX_RATE:.0%}")
    if summary["duplicate_occurrences"]:
        failures.append(f"{summary['duplicate_occurrences']} occurrence(s) ran more than once")
    if summary["duplicate_events"]:
        failures.append(f"{summary['duplicate_events']} duplicated Event identity(ies)")
    if summary["writes"] and summary["events_created"] <= 0:
        failures.append("writes were accepted but no Event was recorded")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.load.run", description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="peak")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=2, help="scheduler workers")
    parser.add_argument(
        "--api-workers", type=int, default=harness.API_WORKERS, help="API processes"
    )
    parser.add_argument("--database-url", default=None, help="maintenance URL; else the test URL")
    parser.add_argument("--json", type=Path, default=None, help="write the report as JSON")
    args = parser.parse_args(argv)

    import os

    maintenance = args.database_url or os.environ.get("AGENT_COLAB_TEST_DATABASE_URL")
    if not maintenance:
        parser.error("pass --database-url or set AGENT_COLAB_TEST_DATABASE_URL")
    profile = PROFILES[args.profile]
    with disposable_database(maintenance) as url:
        engine = make_engine(url)
        try:
            population = seed_population(engine, profile)
            report = harness.run_load(
                engine,
                url,
                population,
                profile,
                seconds=args.minutes * 60.0,
                workers=args.workers,
                api_workers=args.api_workers,
            )
        finally:
            engine.dispose()
    summary = report.summary()
    print(json.dumps(summary, indent=2))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    failures = check(summary)
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
