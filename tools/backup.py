"""Full-scope backup (P4-02/P4-11, P7-03; development plan §9.3, §11.1 Backup/Restore).

``python -m tools.backup --database-url <url> --out <dir> [--record] [--ledger] [--storage]``

A backup is one manifest listing every part with its size and SHA-256:

* the database (``pg_dump`` custom format),
* the artifact and document storage roots (``--storage``, one ``.tar.gz`` each),
* the key-tombstone ledger (``--ledger``), kept beside the dump so a restore can reconcile
  destroyed DEKs before the service opens,
* a settings inventory — key, version, layer and value fingerprint, never a value,

plus the schema revision the dump was taken at, so a restore knows which code can read it. The
master key and the ledger signing key are never part of a backup: they live in the environment or
the OS secret store, which is what makes a stolen dump useless (V-P4-17).

``--apply-retention`` prunes the catalog to the configured daily/weekly/monthly windows. It takes
``--now`` so a test can cross a boundary without waiting; nothing else about the path differs, so
what the test exercises is what production runs (V-P7-19).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TOOL_VERSION = "backup.py/2"

# Retention defaults (development plan §11.1): how many backups to keep per bucket. Settings
# `backup.retention_daily|weekly|monthly` override them; the RPO stays 24 h either way.
RETENTION_DEFAULTS = {"daily": 7, "weekly": 4, "monthly": 6}
RETENTION_KEYS = {b: f"backup.retention_{b}" for b in RETENTION_DEFAULTS}


def pg_bin() -> Path:
    """PostgreSQL client binaries: ``AGENT_COLAB_PG_BIN``, else the user-space pg16 layout."""
    env = os.environ.get("AGENT_COLAB_PG_BIN")
    if env:
        return Path(env)
    candidate = (
        Path.home() / ".local" / "pg16" / "root" / "usr" / "lib" / "postgresql" / "16" / "bin"
    )
    return candidate if candidate.exists() else Path("/usr/bin")


def pg_env() -> dict[str, str]:
    env = dict(os.environ)
    lib = Path.home() / ".local" / "pg16" / "root" / "usr" / "lib" / "x86_64-linux-gnu"
    if lib.exists():
        env["LD_LIBRARY_PATH"] = f"{lib}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
    return env


def libpq_url(url: str) -> str:
    """SQLAlchemy driver URLs (``postgresql+psycopg://``) → the libpq form pg tools accept."""
    return re.sub(r"^postgresql\+[a-z0-9]+://", "postgresql://", url)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _part(name: str, kind: str, path: Path) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_of(path),
    }


def storage_roots() -> dict[str, Path]:
    """The artifact and document roots this instance writes to."""
    from server.artifacts.storage import DEFAULT_ROOT as ARTIFACT_DEFAULT
    from server.documents.store import DEFAULT_ROOT as DOCUMENT_DEFAULT

    return {
        "artifacts": Path(os.environ.get("AGENT_COLAB_ARTIFACT_ROOT", str(ARTIFACT_DEFAULT))),
        "documents": Path(os.environ.get("AGENT_COLAB_DOCUMENT_ROOT", str(DOCUMENT_DEFAULT))),
    }


def _archive_root(root: Path, target: Path) -> Path:
    """Tar a storage root reproducibly enough to checksum; missing roots archive as empty."""
    with tarfile.open(target, "w:gz") as tar:
        if root.exists():
            for entry in sorted(root.rglob("*")):
                tar.add(entry, arcname=str(entry.relative_to(root)), recursive=False)
    os.chmod(target, 0o600)
    return target


def schema_revision(session: Any) -> str:
    from sqlalchemy import text

    row = session.execute(text("SELECT version_num FROM alembic_version")).first()
    return "" if row is None else str(row[0])


def settings_inventory(session: Any) -> list[dict[str, Any]]:
    """Key, latest version, layer and value fingerprint for every setting — never a value."""
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT DISTINCT ON (setting_key) setting_key, version, layer, secret, "
            "value_fingerprint FROM settings_versions ORDER BY setting_key, version DESC"
        )
    ).all()
    return [
        {
            "key": str(r[0]),
            "version": int(r[1]),
            "layer": str(r[2]),
            "secret": bool(r[3]),
            "value_fingerprint": str(r[4]),
        }
        for r in rows
    ]


def run_backup(
    database_url: str,
    out_dir: Path,
    *,
    record: bool,
    ledger: bool,
    created_by: str,
    storage: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Take a backup; returns the manifest (also written as ``<backup_id>.manifest.json``)."""
    taken_at = now or dt.datetime.now(dt.UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_id = "bk-" + taken_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    dump = out_dir / f"{backup_id}.dump"
    proc = subprocess.run(
        [
            str(pg_bin() / "pg_dump"),
            "--format=custom",
            "--no-owner",
            f"--file={dump}",
            libpq_url(database_url),
        ],
        check=False,
        env=pg_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    os.chmod(dump, 0o600)
    parts: list[dict[str, Any]] = [_part("database", "pg_dump", dump)]
    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "path": str(dump),
        "size_bytes": dump.stat().st_size,
        "sha256": sha256_of(dump),
        "created_at": taken_at.isoformat(),
        "created_by": created_by,
        "tool_version": TOOL_VERSION,
        "includes_master_key": False,
        "includes_ledger": False,
        "includes_storage": False,
    }
    if storage:
        for name, root in storage_roots().items():
            archive = _archive_root(root, out_dir / f"{backup_id}.{name}.tar.gz")
            part = _part(name, "storage", archive)
            part["root"] = str(root)
            parts.append(part)
        manifest["includes_storage"] = True

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from server.db.engine import normalize_url

    engine = create_engine(normalize_url(database_url))
    try:
        with Session(engine) as session, session.begin():
            manifest["schema_revision"] = schema_revision(session)
            inventory = settings_inventory(session)
            inventory_path = out_dir / f"{backup_id}.settings.json"
            inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            os.chmod(inventory_path, 0o600)
            parts.append(_part("settings", "inventory", inventory_path))
            manifest["settings_count"] = len(inventory)
            if ledger:
                from server.application.hard_delete import export_ledger

                entries = export_ledger(session)
                ledger_path = out_dir / f"{backup_id}.ledger.json"
                ledger_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
                os.chmod(ledger_path, 0o600)
                parts.append(_part("ledger", "ledger", ledger_path))
                manifest["ledger_path"] = str(ledger_path)
                manifest["ledger_entries"] = len(entries)
                manifest["includes_ledger"] = True
            if record:
                session.execute(
                    text(
                        "INSERT INTO backups (backup_id, path, size_bytes, sha256, created_at, "
                        "created_by, tool_version, includes_master_key, includes_ledger) VALUES "
                        "(:i, :p, :s, :h, :t, :b, :v, false, :l)"
                    ),
                    {
                        "i": backup_id,
                        "p": str(dump),
                        "s": manifest["size_bytes"],
                        "h": manifest["sha256"],
                        "t": taken_at,
                        "b": created_by,
                        "v": TOOL_VERSION,
                        "l": bool(manifest["includes_ledger"]),
                    },
                )
    finally:
        engine.dispose()
    manifest["parts"] = parts
    manifest_path = out_dir / f"{backup_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


# --- retention ----------------------------------------------------------------------------------


def retention_policy(session: Any | None) -> dict[str, int]:
    """Configured windows, falling back to the built-in defaults."""
    policy = dict(RETENTION_DEFAULTS)
    if session is None:
        return policy
    from sqlalchemy import text

    for bucket, key in RETENTION_KEYS.items():
        row = session.execute(
            text(
                "SELECT value_json FROM settings_versions WHERE setting_key = :k "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"k": key},
        ).first()
        if row is not None and row[0] is not None:
            try:
                policy[bucket] = int(row[0])
            except (TypeError, ValueError):
                continue
    return policy


def _bucket_keys(when: dt.datetime) -> dict[str, str]:
    local = when.astimezone(dt.UTC)
    iso_year, iso_week, _ = local.isocalendar()
    return {
        "daily": local.strftime("%Y-%m-%d"),
        "weekly": f"{iso_year}-W{iso_week:02d}",
        "monthly": local.strftime("%Y-%m"),
    }


def plan_retention(
    catalog: Sequence[tuple[str, dt.datetime]], policy: dict[str, int], now: dt.datetime
) -> dict[str, Any]:
    """Which backups to keep and why, newest first.

    One backup per bucket is kept — the newest in that day, ISO week and month — for as many
    buckets as the policy allows. A backup kept by any bucket is kept; the rest expire. Anything
    newer than ``now`` is kept untouched: a clock that moved backwards must not delete data.
    """
    ordered = sorted(catalog, key=lambda row: row[1], reverse=True)
    keep: dict[str, list[str]] = {b: [] for b in policy}
    seen: dict[str, set[str]] = {b: set() for b in policy}
    for backup_id, created_at in ordered:
        if created_at > now:
            for bucket in policy:
                if backup_id not in keep[bucket]:
                    keep[bucket].append(backup_id)
            continue
        keys = _bucket_keys(created_at)
        for bucket, limit in policy.items():
            key = keys[bucket]
            if key in seen[bucket] or len(seen[bucket]) >= limit:
                continue
            seen[bucket].add(key)
            keep[bucket].append(backup_id)
    kept = {b for ids in keep.values() for b in ids}
    expired = [backup_id for backup_id, _ in ordered if backup_id not in kept]
    return {"policy": policy, "keep": sorted(kept), "keep_by_bucket": keep, "expire": expired}


def _catalog(session: Any) -> list[tuple[str, dt.datetime]]:
    from sqlalchemy import text

    rows = session.execute(
        text("SELECT backup_id, created_at FROM backups ORDER BY created_at DESC")
    ).all()
    return [(str(r[0]), r[1]) for r in rows]


def apply_retention(
    database_url: str, out_dir: Path, *, now: dt.datetime, dry_run: bool = False
) -> dict[str, Any]:
    """Prune the catalog to the configured windows; deletes the rows and every part on disk."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from server.db.engine import normalize_url

    engine = create_engine(normalize_url(database_url))
    try:
        with Session(engine) as session, session.begin():
            plan = plan_retention(_catalog(session), retention_policy(session), now)
            if dry_run:
                return plan | {"deleted_files": []}
            deleted: list[str] = []
            for backup_id in plan["expire"]:
                for path in sorted(out_dir.glob(f"{backup_id}.*")):
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
                session.execute(text("DELETE FROM backups WHERE backup_id = :i"), {"i": backup_id})
            return plan | {"deleted_files": deleted}
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--record", action="store_true", help="store metadata in the backups table")
    parser.add_argument("--ledger", action="store_true", help="export the key-tombstone ledger")
    parser.add_argument("--storage", action="store_true", help="include artifact/document roots")
    parser.add_argument("--created-by", default=os.environ.get("USER", "operator"))
    parser.add_argument(
        "--apply-retention", action="store_true", help="prune the catalog instead of backing up"
    )
    parser.add_argument("--dry-run", action="store_true", help="with --apply-retention: plan only")
    parser.add_argument("--now", default=None, help="ISO instant used as 'now' (retention)")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("backup: --database-url or AGENT_COLAB_DATABASE_URL required", file=sys.stderr)
        return 2
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    if args.apply_retention:
        print(
            json.dumps(
                apply_retention(args.database_url, args.out, now=now, dry_run=args.dry_run),
                default=str,
            )
        )
        return 0
    manifest = run_backup(
        args.database_url,
        args.out,
        record=args.record,
        ledger=args.ledger,
        created_by=args.created_by,
        storage=args.storage,
        now=now if args.now else None,
    )
    print(json.dumps(manifest, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
