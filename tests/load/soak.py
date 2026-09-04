"""The 24-hour soak runner (V-P7-04).

    uv run python -m tests.load.soak --minutes 1440 --samples soak-24h.jsonl

Creates a disposable database, seeds the §21.1 normal population, and drives real API processes,
real scheduler workers, real load generators and real Agent heartbeats for the requested duration,
appending one JSON sample per minute. The samples are the evidence: ``tests/e2e/test_soak.py``
reads the finished file and asserts the criterion against the whole series, and refuses any file
that does not cover 24 hours. ``--minutes`` exists for smoke runs; a smoke file will fail that
test by design, because a short window cannot demonstrate a day.

Nothing here is allowed to end the run early. A failed sample, a database hiccup or an unreadable
``/proc`` entry is recorded in the sample line and the loop continues; only the clock stops it.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from server.db.engine import make_engine
from tests.load import harness, samples
from tests.load.population import Population, seed_population
from tests.load.profile import PROFILES
from tests.load.run import disposable_database


class Sampler:
    """Builds one sample line per minute and appends it, never raising into the driver loop."""

    def __init__(
        self,
        engine: Engine,
        population: Population,
        database: str,
        path: Path,
        profile: str,
        target_seconds: float,
    ) -> None:
        self._engine = engine
        self._population = population
        self._database = database
        self._path = path
        self._profile = profile
        self._target_seconds = target_seconds
        self.written = 0
        self.errors = 0

    def __call__(self, ctx: samples.SampleContext) -> None:
        line: dict[str, Any] = {
            "at": samples.utc_now_iso(),
            "elapsed_s": round(ctx.elapsed_s, 1),
            "final": ctx.final,
            "profile": self._profile,
            "target_seconds": self._target_seconds,
            "db_cpu_pct": round(ctx.db_cpu_pct, 1),
        }
        try:
            writes, reads, errors = samples.generator_totals(ctx.out_dir)
            line.update({"writes": writes, "reads": reads, "errors": errors})
            line.update(samples.database_sample(self._engine, self._population.ws, self._database))
            server_tree = harness.process_tree(ctx.server_pids)
            worker_tree = harness.process_tree(ctx.worker_pids)
            line["server_rss_kb"] = harness.rss_kb(server_tree)
            line["worker_rss_kb"] = harness.rss_kb(worker_tree)
            line["server_private_kb"] = harness.private_kb(server_tree)
            line["worker_private_kb"] = harness.private_kb(worker_tree)
            line["sample_error"] = None
        except Exception as exc:  # a bad sample must not end a 24-hour run
            self.errors += 1
            line["sample_error"] = f"{type(exc).__name__}: {exc}"
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
                handle.flush()
            self.written += 1
        except OSError:  # pragma: no cover - the console still carries the run
            self.errors += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.load.soak", description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="normal")
    parser.add_argument(
        "--minutes", type=float, default=1440.0, help="24 hours; shorter runs are smoke only"
    )
    parser.add_argument("--workers", type=int, default=2, help="scheduler workers")
    parser.add_argument("--api-workers", type=int, default=harness.API_WORKERS)
    parser.add_argument("--samples", type=Path, required=True, help="JSON-lines sample file")
    parser.add_argument("--database-url", default=None, help="maintenance URL; else the test URL")
    parser.add_argument("--json", type=Path, default=None, help="write the summary as JSON")
    args = parser.parse_args(argv)

    import os

    maintenance = args.database_url or os.environ.get("AGENT_COLAB_TEST_DATABASE_URL")
    if not maintenance:
        parser.error("pass --database-url or set AGENT_COLAB_TEST_DATABASE_URL")
    profile = PROFILES[args.profile]
    args.samples.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    print(
        f"soak start {samples.utc_now_iso()} profile={profile.name} minutes={args.minutes} "
        f"samples={args.samples}",
        flush=True,
    )
    with disposable_database(maintenance) as url:
        engine = make_engine(url)
        try:
            population = seed_population(engine, profile)
            print(f"seeded workspace {population.ws}", flush=True)
            sampler = Sampler(
                engine,
                population,
                url.rsplit("/", 1)[-1],
                args.samples,
                profile.name,
                args.minutes * 60.0,
            )
            report = harness.run_soak(
                engine,
                url,
                population,
                profile,
                minutes=args.minutes,
                workers=args.workers,
                api_workers=args.api_workers,
                sample_sink=sampler,
                sample_period_s=60.0,
            )
        except Exception:  # report the failure with the elapsed time
            traceback.print_exc()
            print(f"soak FAILED after {time.time() - started:.0f}s", flush=True)
            return 2
        finally:
            engine.dispose()
    summary = report.summary()
    summary["samples_written"] = sampler.written
    summary["sample_errors"] = sampler.errors
    summary["wall_clock_s"] = round(time.time() - started, 1)
    print(json.dumps(summary, indent=2), flush=True)
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"soak done {samples.utc_now_iso()}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
