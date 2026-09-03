"""Database backup (P4-02/P4-11; development plan §9.3, §11.1 Backup/Restore).

``python -m tools.backup --database-url <url> --out <dir> [--record] [--ledger]``

Runs ``pg_dump`` (custom format) into ``<dir>/<backup_id>.dump`` and writes a manifest with the
SHA-256 digest. The master key and the ledger signing key are never part of a backup (they live in
the environment / OS secret store); with ``--ledger`` the key-tombstone ledger is exported next to
the dump as ``<backup_id>.ledger.json`` — kept separately from the dump so a restore can reconcile
destroyed DEKs (``tools/restore.py``). ``--record`` stores the metadata row in ``backups``.
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
import uuid
from pathlib import Path

TOOL_VERSION = "backup.py/1"


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


def run_backup(
    database_url: str, out_dir: Path, *, record: bool, ledger: bool, created_by: str
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_id = (
        "bk-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    )
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
    manifest: dict[str, object] = {
        "backup_id": backup_id,
        "path": str(dump),
        "size_bytes": dump.stat().st_size,
        "sha256": sha256_of(dump),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "created_by": created_by,
        "tool_version": TOOL_VERSION,
        "includes_master_key": False,
        "includes_ledger": False,
    }
    if ledger or record:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session

        from server.application.hard_delete import export_ledger
        from server.db.engine import normalize_url

        engine = create_engine(normalize_url(database_url))
        with Session(engine) as session, session.begin():
            if ledger:
                entries = export_ledger(session)
                ledger_path = out_dir / f"{backup_id}.ledger.json"
                ledger_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
                os.chmod(ledger_path, 0o600)
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
                        "t": dt.datetime.now(dt.UTC),
                        "b": created_by,
                        "v": TOOL_VERSION,
                        "l": bool(manifest["includes_ledger"]),
                    },
                )
        engine.dispose()
    (out_dir / f"{backup_id}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--record", action="store_true", help="store metadata in the backups table")
    parser.add_argument("--ledger", action="store_true", help="export the key-tombstone ledger")
    parser.add_argument("--created-by", default=os.environ.get("USER", "operator"))
    args = parser.parse_args(argv)
    if not args.database_url:
        print("backup: --database-url or AGENT_COLAB_DATABASE_URL required", file=sys.stderr)
        return 2
    manifest = run_backup(
        args.database_url,
        args.out,
        record=args.record,
        ledger=args.ledger,
        created_by=args.created_by,
    )
    print(
        json.dumps({k: v for k, v in manifest.items() if k != "path"} | {"path": manifest["path"]})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
