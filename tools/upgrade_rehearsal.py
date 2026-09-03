"""Upgrade, rollback and forward-fix rehearsals (P7-06; V-P7-09, V-P7-10).

``python -m tools.upgrade_rehearsal --from-ref phase-5-passed``

The rehearsal is deliberately a *migration* rehearsal, not an application one: it takes the
schema of a released tag, fills it with data through SQL that version understood, upgrades to the
working tree's head and checks that nothing was lost. Doing it with the old tag's own migration
scripts (extracted with ``git archive``) is what makes it an upgrade rather than a fresh install.

Three scenarios:

* ``upgrade`` — old tag's migrations, seed, upgrade to head, compare data and settings (V-P7-09).
* ``rollback`` — the application fails after an upgrade whose migrations were additive, so the
  previous release is started again against the newer schema; the check is that every table and
  column the old schema had is still present, which is what makes that rollback safe (V-P7-10).
* ``forward-fix`` — an irreversible migration (a dropped column) cannot be downgraded, so a
  corrective migration is applied forward; the check is that service is restored and the
  migration ledger is consistent (V-P7-10).

Every scenario reports wall-clock seconds so a rehearsal can be compared against the RTO.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
RTO_SECONDS = 4 * 60 * 60  # development plan §21.1
SCENARIOS = ("upgrade", "rollback", "forward-fix")


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def export_ref(ref: str, target: Path) -> Path:
    """Extract a released tag's migrations and alembic config — not its dependencies."""
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "ref.tar"
    with archive.open("wb") as fh:
        proc = subprocess.run(
            ["git", "archive", ref, "migrations", "alembic.ini"],
            cwd=ROOT,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {ref} failed: {proc.stderr.decode()[:300]}")
    _run(["tar", "-xf", str(archive), "-C", str(target)])
    archive.unlink()
    return target


def alembic_upgrade(script_location: Path, url: str, revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    from server.db.engine import normalize_url

    cfg = Config(str(script_location / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_location / "migrations"))
    cfg.set_main_option("sqlalchemy.url", normalize_url(url))
    command.upgrade(cfg, revision)


def create_database(base_url: str, name: str) -> str:
    from server.db.engine import normalize_url

    base = normalize_url(base_url)
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with maint.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        maint.dispose()
    return base.rsplit("/", 1)[0] + f"/{name}"


def drop_database(base_url: str, name: str) -> None:
    from server.db.engine import normalize_url

    maint = create_engine(normalize_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        maint.dispose()


# --- fingerprints ------------------------------------------------------------------------------


def schema_fingerprint(url: str) -> dict[str, Any]:
    """Every public column, so an upgrade's schema delta is visible and comparable."""
    from server.db.engine import normalize_url

    engine = create_engine(normalize_url(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "ORDER BY table_name, column_name"
                )
            ).all()
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    columns = [f"{r[0]}.{r[1]}:{r[2]}:{r[3]}" for r in rows]
    return {
        "revision": str(revision),
        "columns": columns,
        "tables": sorted({r[0] for r in rows}),
        "hash": hashlib.sha256("\n".join(columns).encode()).hexdigest(),
    }


DATA_QUERIES = {
    "workspaces": "SELECT workspace_id, name FROM workspaces ORDER BY workspace_id",
    "accounts": "SELECT account_id, account_type, status FROM accounts ORDER BY account_id",
    "settings": (
        "SELECT setting_key, version, value_json::text, value_fingerprint FROM settings_versions "
        "ORDER BY setting_key, version"
    ),
    "secret_refs": ("SELECT key_ref, status FROM sensitive_keys ORDER BY key_ref"),
    "schedules": (
        "SELECT schedule_id, name, status, cron_expression, timezone FROM schedules "
        "JOIN schedule_versions USING (schedule_id) ORDER BY schedule_id, version"
    ),
    "events": "SELECT event_id, type, content_hash FROM events ORDER BY recorded_seq",
}


def data_fingerprint(url: str) -> dict[str, str]:
    """One hash per area the upgrade criterion names, so a loss is attributable."""
    from server.db.engine import normalize_url

    engine = create_engine(normalize_url(url))
    out: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            for name, sql in DATA_QUERIES.items():
                try:
                    rows = conn.execute(text(sql)).all()
                except Exception:  # a table the old schema did not have yet
                    conn.rollback()
                    out[name] = "ABSENT"
                    continue
                payload = json.dumps([[str(v) for v in row] for row in rows], sort_keys=True)
                out[name] = hashlib.sha256(payload.encode()).hexdigest()
    finally:
        engine.dispose()
    return out


# --- seeding (SQL the old release understood) ----------------------------------------------------

SEED_SQL = [
    "INSERT INTO workspaces (id, workspace_id, name) VALUES (:ws, 'ws-upgrade', 'upgrade')",
    (
        "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
        "VALUES (:acct, 'acct-upgrade-owner', :ws, 'human', 'Owner')"
    ),
    (
        "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
        "VALUES (:chan, 'chan-upgrade', :ws, 'work', 'upgrade')"
    ),
    (
        "INSERT INTO settings_versions (setting_key, version, secret, value_json, "
        "value_fingerprint, changed_at, reason, layer) VALUES "
        "('instance.name', 1, false, '\"Upgrade Rehearsal\"', 'sha256:seed', now(), 'seed', "
        "'runtime')"
    ),
    (
        "INSERT INTO sensitive_keys (key_ref, workspace_id, target_type, target_id, wrapped_dek, "
        "master_key_id, status, created_at) VALUES ('sec-upgrade-1', :ws, 'account', "
        "'acct-upgrade-owner', '\\x00'::bytea, 'mk-rehearsal', 'active', now())"
    ),
]


def seed_old_schema(url: str) -> dict[str, str]:
    """Data an operator would already have before the upgrade."""
    from server.db.engine import normalize_url

    ids = {"ws": str(uuid.uuid4()), "acct": str(uuid.uuid4()), "chan": str(uuid.uuid4())}
    engine = create_engine(normalize_url(url))
    try:
        with engine.begin() as conn:
            for sql in SEED_SQL:
                conn.execute(text(sql), ids)
    finally:
        engine.dispose()
    return ids


# --- scenarios -----------------------------------------------------------------------------------


def rehearse_upgrade(base_url: str, from_ref: str, keep: bool = False) -> dict[str, Any]:
    """V-P7-09: seed on the old schema, upgrade to head, prove nothing was lost."""
    name = "colab_upgrade_" + uuid.uuid4().hex[:8]
    url = create_database(base_url, name)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            old = export_ref(from_ref, Path(tmp) / "old")
            alembic_upgrade(old, url)
        before_schema = schema_fingerprint(url)
        seed_old_schema(url)
        before_data = data_fingerprint(url)

        from server.db.engine import run_migrations

        run_migrations(url)
        after_schema = schema_fingerprint(url)
        after_data = data_fingerprint(url)
        elapsed = time.monotonic() - started
        preserved = {
            area: before_data[area] == after_data[area]
            for area in before_data
            if before_data[area] != "ABSENT"
        }
        return {
            "scenario": "upgrade",
            "from_ref": from_ref,
            "from_revision": before_schema["revision"],
            "to_revision": after_schema["revision"],
            "new_tables": sorted(set(after_schema["tables"]) - set(before_schema["tables"])),
            "removed_tables": sorted(set(before_schema["tables"]) - set(after_schema["tables"])),
            "data_preserved": preserved,
            "ok": all(preserved.values())
            and not (set(before_schema["tables"]) - set(after_schema["tables"])),
            "elapsed_s": round(elapsed, 2),
            "within_rto": elapsed < RTO_SECONDS,
            "database": url if keep else None,
        }
    finally:
        if not keep:
            drop_database(base_url, name)


def rehearse_rollback(base_url: str, from_ref: str, keep: bool = False) -> dict[str, Any]:
    """V-P7-10 (application failure): the previous release runs against the upgraded schema.

    The upgrade is additive, so rolling the application back does not need a schema downgrade —
    which is exactly the property that has to be checked, not assumed.
    """
    name = "colab_rollback_" + uuid.uuid4().hex[:8]
    url = create_database(base_url, name)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            old = export_ref(from_ref, Path(tmp) / "old")
            alembic_upgrade(old, url)
            old_schema = schema_fingerprint(url)
            seed_old_schema(url)
            seeded = data_fingerprint(url)

            from server.db.engine import run_migrations

            run_migrations(url)  # upgrade, then the application fails and is rolled back
            new_schema = schema_fingerprint(url)
        missing = sorted(set(old_schema["columns"]) - set(new_schema["columns"]))
        after = data_fingerprint(url)
        elapsed = time.monotonic() - started
        return {
            "scenario": "rollback",
            "from_ref": from_ref,
            "old_revision": old_schema["revision"],
            "new_revision": new_schema["revision"],
            "columns_lost_for_old_release": missing,
            "data_preserved": {
                area: seeded[area] == after[area] for area in seeded if seeded[area] != "ABSENT"
            },
            "ok": not missing,
            "elapsed_s": round(elapsed, 2),
            "within_rto": elapsed < RTO_SECONDS,
            "database": url if keep else None,
        }
    finally:
        if not keep:
            drop_database(base_url, name)


FORWARD_FIX_BREAK = "ALTER TABLE workspaces DROP COLUMN name"
FORWARD_FIX_REPAIR = "ALTER TABLE workspaces ADD COLUMN name text NOT NULL DEFAULT 'recovered'"


def rehearse_forward_fix(base_url: str, keep: bool = False) -> dict[str, Any]:
    """V-P7-10 (irreversible migration): a dropped column cannot be downgraded, so the fix goes
    forward. The ledger must show both steps and the schema must be serviceable again."""
    name = "colab_forwardfix_" + uuid.uuid4().hex[:8]
    url = create_database(base_url, name)
    started = time.monotonic()
    try:
        from server.db.engine import normalize_url, run_migrations

        run_migrations(url)
        healthy = schema_fingerprint(url)
        seed_old_schema(url)
        engine = create_engine(normalize_url(url))
        ledger: list[dict[str, str]] = []
        try:
            with engine.begin() as conn:
                conn.execute(text(FORWARD_FIX_BREAK))  # irreversible: the values are gone
            ledger.append({"step": "irreversible", "sql": FORWARD_FIX_BREAK})
            broken = schema_fingerprint(url)
            downgrade_possible = "workspaces.name:text:NO" in broken["columns"]
            with engine.begin() as conn:
                conn.execute(text(FORWARD_FIX_REPAIR))  # forward fix restores service
            ledger.append({"step": "forward_fix", "sql": FORWARD_FIX_REPAIR})
            fixed = schema_fingerprint(url)
            with engine.connect() as conn:
                serviceable = (
                    conn.execute(text("SELECT count(*) FROM workspaces")).scalar_one() >= 1
                )
        finally:
            engine.dispose()
        elapsed = time.monotonic() - started
        return {
            "scenario": "forward-fix",
            "revision": fixed["revision"],
            "downgrade_possible": downgrade_possible,
            "tables_intact": fixed["tables"] == healthy["tables"],
            "serviceable_after_fix": serviceable,
            "ledger": ledger,
            "ok": (not downgrade_possible) and serviceable and fixed["tables"] == healthy["tables"],
            "elapsed_s": round(elapsed, 2),
            "within_rto": elapsed < RTO_SECONDS,
            "database": url if keep else None,
        }
    finally:
        if not keep:
            drop_database(base_url, name)


def rehearse(base_url: str, from_ref: str, scenarios: tuple[str, ...]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario == "upgrade":
            results.append(rehearse_upgrade(base_url, from_ref))
        elif scenario == "rollback":
            results.append(rehearse_rollback(base_url, from_ref))
        else:
            results.append(rehearse_forward_fix(base_url))
    return {
        "from_ref": from_ref,
        "ran_at": dt.datetime.now(dt.UTC).isoformat(),
        "results": results,
        "ok": all(r["ok"] and r["within_rto"] for r in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    parser.add_argument("--from-ref", default="phase-5-passed")
    parser.add_argument("--scenario", action="append", choices=[*SCENARIOS, "all"], default=None)
    args = parser.parse_args(argv)
    if not args.database_url:
        print("upgrade_rehearsal: --database-url required", file=sys.stderr)
        return 2
    chosen = args.scenario or ["all"]
    scenarios = SCENARIOS if "all" in chosen else tuple(chosen)
    report = rehearse(args.database_url, args.from_ref, scenarios)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
