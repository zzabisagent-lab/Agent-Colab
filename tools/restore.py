"""Restore with tombstone reconciliation (P4-11, P7-03; development plan §9.3; V-P4-29, V-P7-20).

``python -m tools.restore --manifest <backup.manifest.json> --database-url <empty target url>``
restores every part the manifest lists — database, storage roots, settings inventory — and then,
before the service is opened, applies the exported key-tombstone ledger: every DEK destroyed after
the backup was taken is shredded again and tombstoned, so a restore can never resurrect a
hard-deleted secret. Event rows are untouched.

The gate is explicit. A marker is written before anything is loaded and removed only after
reconciliation reports no unknown entries, and readiness fails while it exists (V-P7-20), so an
interrupted restore leaves the instance closed rather than half-open.

``--dump``/``--ledger`` still restore a single dump for callers that predate manifests.
Exit code 1 when the ledger fails verification or reconciliation reports problems.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

from server.ops import restore_gate
from tools.backup import libpq_url, pg_bin, pg_env, sha256_of


def _pg_restore(dump: Path, database_url: str) -> None:
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


def _reconcile(database_url: str, ledger_path: Path) -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from server.application.hard_delete import reconcile_tombstones, verify_exported_ledger
    from server.db.engine import normalize_url

    entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    problems = verify_exported_ledger(entries)
    if problems:
        return {"verified": False, "problems": problems}
    engine = create_engine(normalize_url(database_url))
    try:
        with Session(engine) as session, session.begin():
            outcome = reconcile_tombstones(session, entries, dt.datetime.now(dt.UTC))
    finally:
        engine.dispose()
    return {"verified": True, "entries": len(entries), **outcome}


def _reconciled(report: dict[str, Any] | None) -> bool:
    """True when the gate may open: verified and nothing left unknown."""
    if report is None:
        return True  # nothing to reconcile: no ledger was part of this backup
    return bool(report.get("verified")) and not report.get("unknown")


def run_restore(dump: Path, database_url: str, ledger_path: Path | None) -> dict[str, object]:
    """Restore a single dump (no manifest); kept for callers that predate full-scope backups."""
    _pg_restore(dump, database_url)
    report: dict[str, object] = {"restored": str(dump), "ledger": None}
    if ledger_path is not None:
        report["ledger"] = _reconcile(database_url, ledger_path)
    return report


def run_full_restore(
    manifest_path: Path,
    database_url: str,
    *,
    storage_targets: dict[str, Path] | None = None,
    marker: Path | None = None,
    verify_checksums: bool = True,
) -> dict[str, Any]:
    """Restore every part of a manifest, then reconcile before opening the gate."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = {p["name"]: p for p in manifest.get("parts", [])}
    gate = marker or restore_gate.marker_path()
    restore_gate.mark_pending(
        gate,
        backup_id=str(manifest.get("backup_id", "")),
        reason="RECONCILIATION_PENDING",
        now=dt.datetime.now(dt.UTC),
    )
    report: dict[str, Any] = {
        "backup_id": manifest.get("backup_id"),
        "schema_revision": manifest.get("schema_revision"),
        "parts": [],
        "checksum_mismatches": [],
        "ledger": None,
        "gate_open": False,
    }
    if verify_checksums:
        for name, part in parts.items():
            path = Path(part["path"])
            if not path.exists():
                report["checksum_mismatches"].append({"part": name, "reason": "MISSING"})
            elif sha256_of(path) != part["sha256"]:
                report["checksum_mismatches"].append({"part": name, "reason": "SHA256_MISMATCH"})
        if report["checksum_mismatches"]:
            return report  # the gate stays closed: a corrupt backup is never opened

    _pg_restore(Path(parts["database"]["path"]), database_url)
    report["parts"].append("database")

    targets = storage_targets or {}
    for name in ("artifacts", "documents"):
        part = parts.get(name)
        if part is None:
            continue
        target = targets.get(name) or Path(part.get("root", ""))
        if not str(target):
            continue
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(part["path"], "r:gz") as tar:
            tar.extractall(target, filter="data")
        report["parts"].append(name)

    ledger_part = parts.get("ledger")
    if ledger_part is not None:
        report["ledger"] = _reconcile(database_url, Path(ledger_part["path"]))
        report["parts"].append("ledger")
    if _reconciled(report["ledger"]):
        restore_gate.clear_pending(gate)
        report["gate_open"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dump", type=Path, default=None)
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--document-root", type=Path, default=None)
    parser.add_argument("--marker", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.database_url:
        print("restore: --database-url or AGENT_COLAB_DATABASE_URL required", file=sys.stderr)
        return 2
    if not args.manifest and not args.dump:
        print("restore: --manifest or --dump required", file=sys.stderr)
        return 2
    if args.manifest:
        targets: dict[str, Path] = {}
        if args.artifact_root:
            targets["artifacts"] = args.artifact_root
        if args.document_root:
            targets["documents"] = args.document_root
        report = run_full_restore(
            args.manifest, args.database_url, storage_targets=targets, marker=args.marker
        )
        print(json.dumps(report, default=str))
        return 0 if report["gate_open"] else 1
    report = dict(run_restore(args.dump, args.database_url, args.ledger))
    print(json.dumps(report, default=str))
    ledger = report.get("ledger")
    if isinstance(ledger, dict) and (not ledger.get("verified", True) or ledger.get("unknown")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
