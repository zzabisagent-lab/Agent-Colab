"""Database restore with tombstone reconciliation (P4-11; development plan §9.3; V-P4-29).

``python -m tools.restore --dump <file> --database-url <empty target url> --ledger <ledger.json>``

Restores the dump with ``pg_restore`` and then, before the service is opened, applies the exported
key-tombstone ledger: every DEK that was destroyed after the backup was taken is shredded again and
tombstoned, so a restore can never resurrect a hard-deleted secret. Event rows are untouched.
Exit code 1 when the ledger fails verification or reconciliation reports problems.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from tools.backup import libpq_url, pg_bin, pg_env


def run_restore(dump: Path, database_url: str, ledger_path: Path | None) -> dict[str, object]:
    subprocess.run(
        [
            str(pg_bin() / "pg_restore"),
            "--no-owner",
            "--exit-on-error",
            f"--dbname={libpq_url(database_url)}",
            str(dump),
        ],
        check=True,
        env=pg_env(),
        capture_output=True,
    )
    report: dict[str, object] = {"restored": str(dump), "ledger": None}
    if ledger_path is None:
        return report
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from server.application.hard_delete import reconcile_tombstones, verify_exported_ledger
    from server.db.engine import normalize_url

    entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    problems = verify_exported_ledger(entries)
    if problems:
        report["ledger"] = {"verified": False, "problems": problems}
        return report
    engine = create_engine(normalize_url(database_url))
    with Session(engine) as session, session.begin():
        outcome = reconcile_tombstones(session, entries, dt.datetime.now(dt.UTC))
    engine.dispose()
    report["ledger"] = {"verified": True, "entries": len(entries), **outcome}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dump", required=True, type=Path)
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    parser.add_argument("--ledger", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.database_url:
        print("restore: --database-url or AGENT_COLAB_DATABASE_URL required", file=sys.stderr)
        return 2
    report = run_restore(args.dump, args.database_url, args.ledger)
    print(json.dumps(report))
    ledger = report.get("ledger")
    if isinstance(ledger, dict) and (not ledger.get("verified", True) or ledger.get("unknown")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
