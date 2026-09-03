"""Recovery rehearsals (P7-03): V-P7-07 restore into an empty environment, V-P7-08 projection
rebuild parity, V-P7-19 backup retention across daily/weekly/monthly boundaries on an injectable
clock, and V-P7-20 a pre-hard-delete backup that must not resurrect destroyed keys.

Every test creates and drops its own database, so a restore target is genuinely empty.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from server.application import hard_delete as hd
from server.db.engine import make_engine, normalize_url
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.ops import restore_gate
from server.projections.approvals import rebuild_approvals
from server.projections.approvals import snapshot_hash as approvals_snapshot
from server.projections.runner import rebuild as rebuild_projection
from server.projections.runner import snapshot_hash as projection_snapshot
from server.secrets.envelope import CryptoError
from tests.conftest import TEST_URL
from tests.integration import phase7_recovery_seed as rs
from tests.integration.phase4_admin_seed import T0, install_reauth, run
from tools.backup import apply_retention, plan_retention, run_backup
from tools.restore import run_full_restore

pytestmark = pytest.mark.db

RTO_SECONDS = 4 * 60 * 60  # development plan §21.1 default RTO


@pytest.fixture
def fresh_db() -> Iterator[str]:
    """An empty database, dropped afterwards — the restore target."""
    assert TEST_URL
    base = normalize_url(TEST_URL)
    name = "colab_recover_" + uuid.uuid4().hex[:10]
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.rsplit("/", 1)[0] + f"/{name}"
    finally:
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        maint.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


def _has_pg_tools() -> bool:
    from tools.backup import pg_bin

    return (pg_bin() / "pg_dump").exists() and (pg_bin() / "pg_restore").exists()


def test_restore_into_an_empty_environment_reproduces_state_and_hashes(
    database_url: str, engine: Engine, fresh_db: str, tmp_path: Path
) -> None:
    """V-P7-07: a full-scope backup restores every part; Schedule, Run, Event, Approval and
    ExternalIdentity state and hashes match, well inside the RTO."""
    if not _has_pg_tools():
        pytest.skip("pg_dump/pg_restore not available")
    clock = FixedClock(T0)
    rec = rs.build(engine, f"rec{uuid.uuid4().hex[:4]}", clock)
    with Session(engine) as s:
        before = rs.state_hashes(s, rec.seed.ws)
        settings_before = rs.settings_fingerprints(s)
        events_before = rs.state_rows(s, rec.seed.ws, "events")

    manifest = run_backup(
        database_url,
        tmp_path / "backups",
        record=True,
        ledger=True,
        created_by="rehearsal",
        storage=True,
    )
    assert manifest["includes_master_key"] is False
    assert manifest["includes_storage"] is True and manifest["schema_revision"]
    names = {p["name"] for p in manifest["parts"]}
    assert {"database", "artifacts", "documents", "settings", "ledger"} <= names
    for part in manifest["parts"]:
        assert part["sha256"] and part["size_bytes"] >= 0

    started = time.monotonic()
    report = run_full_restore(
        Path(str(manifest["manifest_path"])),
        fresh_db,
        storage_targets={
            "artifacts": tmp_path / "restored-artifacts",
            "documents": tmp_path / "restored-documents",
        },
        marker=tmp_path / "restore-pending.json",
    )
    elapsed = time.monotonic() - started
    assert report["checksum_mismatches"] == [], report
    assert report["gate_open"] is True  # reconciliation ran, so the instance may open
    assert elapsed < RTO_SECONDS
    print(f"restore of {manifest['size_bytes']} bytes completed in {elapsed:.1f}s (RTO 4 h)")

    restored = create_engine(normalize_url(fresh_db))
    try:
        with Session(restored) as s:
            after = rs.state_hashes(s, rec.seed.ws)
            assert after == before, {
                k: (before[k], after[k]) for k in before if before[k] != after[k]
            }
            assert rs.settings_fingerprints(s) == settings_before
            assert rs.state_rows(s, rec.seed.ws, "events") == events_before
            # the named identifiers survive, not just the hashes
            assert (
                s.execute(
                    text("SELECT status FROM schedules WHERE schedule_id = :s"),
                    {"s": rec.schedule_id},
                ).scalar_one()
                == "ENABLED"
            )
            assert (
                s.execute(
                    text("SELECT count(*) FROM schedule_runs WHERE run_id = :r"),
                    {"r": rec.run_id},
                ).scalar_one()
                == 1
            )
            assert (
                s.execute(
                    text("SELECT status FROM external_identity_links WHERE link_id = :l"),
                    {"l": rec.link_id},
                ).scalar_one()
                == "active"
            )
    finally:
        restored.dispose()


def test_projection_rebuild_from_a_full_replay_is_identical(engine: Engine) -> None:
    """V-P7-08: replaying every Event rebuilds tasks, approvals, agents and schedules to the
    same snapshot the live instance holds."""
    clock = FixedClock(T0)
    rec = rs.build(engine, f"reb{uuid.uuid4().hex[:4]}", clock)
    ws = str(rec.seed.ws)
    with Session(engine) as s:
        live = {
            "tasks": projection_snapshot(s, "tasks", ws),
            "approvals": approvals_snapshot(s, ws),
            "agents": _agents_hash(s, rec.seed.ws),
            "schedules": _schedule_hash(s, rec.seed.ws),
        }
    with Session(engine) as s, s.begin():
        rebuilt_tasks = rebuild_projection(s, "tasks", ws)
        rebuild_approvals(s, ws)
        rebuilt_approvals = approvals_snapshot(s, ws)
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        from server.agents.registry import rebuild as rebuild_agents

        rebuilt_agents = rebuild_agents(s, store, ws, clock.now())
    with Session(engine) as s:
        rebuilt_schedules = _schedule_hash(s, rec.seed.ws)
    assert rebuilt_tasks == live["tasks"]
    assert rebuilt_approvals == live["approvals"]
    assert rebuilt_agents == live["agents"]
    assert rebuilt_schedules == live["schedules"]


def _agents_hash(session: Session, ws: uuid.UUID) -> str:
    from server.agents.registry import state_hash

    return state_hash(session, ws)


def _schedule_hash(session: Session, ws: uuid.UUID) -> str:
    """Schedule aggregate state: the versions' pinned hashes plus each Run's identity."""
    import hashlib

    from server.events.canonical import canonical_json

    payload = [
        rs.state_rows(session, ws, "schedules"),
        rs.state_rows(session, ws, "schedule_versions"),
        rs.state_rows(session, ws, "schedule_runs"),
    ]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _record_backup(engine: Engine, backup_id: str, created_at: dt.datetime) -> None:
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO backups (backup_id, path, size_bytes, sha256, created_at, created_by,"
                " tool_version, includes_master_key, includes_ledger) VALUES (:i, :p, 1, :h, :t, "
                "'retention', 'test', false, false)"
            ),
            {
                "i": backup_id,
                "p": f"/tmp/{backup_id}.dump",
                "h": "0" * 64,
                "t": created_at,
            },
        )


def _configure_retention(engine: Engine, windows: dict[str, int]) -> None:
    """Set the retention settings the way the Settings API would, so the tool reads them."""
    with Session(engine) as s, s.begin():
        for bucket, value in windows.items():
            key = f"backup.retention_{bucket}"
            s.execute(text("DELETE FROM settings_versions WHERE setting_key = :k"), {"k": key})
            s.execute(
                text(
                    "INSERT INTO settings_versions (setting_key, version, secret, value_json, "
                    "value_fingerprint, changed_at, reason, layer) VALUES "
                    "(:k, 1, false, CAST(:v AS jsonb), :f, now(), 'retention rehearsal', 'runtime')"
                ),
                {"k": key, "v": json.dumps(value), "f": f"sha256:{bucket}"},
            )


def test_retention_windows_are_applied_on_an_injectable_clock(
    database_url: str, engine: Engine, tmp_path: Path
) -> None:
    """V-P7-19: crossing daily, weekly and monthly boundaries prunes the catalog exactly as the
    settings prescribe, without waiting for real time."""
    policy = {"daily": 2, "weekly": 2, "monthly": 2}
    origin = dt.datetime(2026, 1, 1, 3, 0, tzinfo=dt.UTC)
    catalog = [(f"bk-day-{n}", origin + dt.timedelta(days=n)) for n in range(10)]
    catalog += [(f"bk-month-{n}", origin - dt.timedelta(days=32 * (n + 1))) for n in range(4)]

    now = origin + dt.timedelta(days=9)
    plan = plan_retention(catalog, policy, now)
    kept = set(plan["keep"])
    assert len(plan["keep_by_bucket"]["daily"]) == 2  # the two most recent days
    assert "bk-day-9" in kept and "bk-day-8" in kept
    assert len(plan["keep_by_bucket"]["monthly"]) == 2
    assert plan["expire"], "older backups outside every window must expire"
    assert kept.isdisjoint(set(plan["expire"]))

    # crossing a boundary: a backup taken in a new day displaces the oldest kept daily
    crossed = [*catalog, ("bk-day-10", origin + dt.timedelta(days=10))]
    after_boundary = plan_retention(crossed, policy, origin + dt.timedelta(days=10, hours=1))
    assert after_boundary["keep_by_bucket"]["daily"] == ["bk-day-10", "bk-day-9"]
    assert "bk-day-8" in after_boundary["expire"]  # was kept before the boundary, expires after

    # a clock that moved backwards never deletes: everything newer than "now" is kept
    early = plan_retention(catalog, policy, origin - dt.timedelta(days=400))
    assert set(early["expire"]) == set()

    # the production path: the configured windows come from settings, and rows and files really
    # disappear. One per bucket keeps exactly the newest day, week and month.
    _configure_retention(engine, {"daily": 1, "weekly": 1, "monthly": 1})
    stamp = uuid.uuid4().hex[:6]
    ids = [f"bk-ret-{stamp}-{n}" for n in range(4)]
    for n, backup_id in enumerate(ids):
        _record_backup(engine, backup_id, origin + dt.timedelta(days=n))
        (tmp_path / f"{backup_id}.dump").write_bytes(b"x")
        (tmp_path / f"{backup_id}.manifest.json").write_text("{}", encoding="utf-8")
    applied = apply_retention(database_url, tmp_path, now=origin + dt.timedelta(days=3, hours=1))
    assert applied["policy"] == {"daily": 1, "weekly": 1, "monthly": 1}
    expired_here = [b for b in applied["expire"] if b in ids]
    assert expired_here, applied
    for backup_id in expired_here:
        assert not (tmp_path / f"{backup_id}.dump").exists()
        assert not (tmp_path / f"{backup_id}.manifest.json").exists()
    with Session(engine) as s:
        remaining = {
            str(r[0])
            for r in s.execute(
                text("SELECT backup_id FROM backups WHERE backup_id = ANY(:ids)"), {"ids": ids}
            ).all()
        }
    assert remaining == {b for b in ids if b not in expired_here}


def test_a_pre_deletion_backup_never_resurrects_destroyed_keys(
    database_url: str, engine: Engine, fresh_db: str, tmp_path: Path
) -> None:
    """V-P7-20: the restored instance stays closed until reconciliation, and afterwards the
    destroyed DEK neither decrypts nor reactivates while Event bytes are unchanged."""
    if not _has_pg_tools():
        pytest.skip("pg_dump/pg_restore not available")
    from tests.integration.test_hard_delete_db import _seed_target

    clock = FixedClock(T0)
    prefix = f"res{uuid.uuid4().hex[:4]}"
    rec = rs.build(engine, prefix, clock)
    sd = rec.seed
    rt = sd.runtime(engine, clock)
    victim = f"acct-{prefix}-victim"
    key_ref = _seed_target(engine, sd, rt, victim)

    manifest = run_backup(
        database_url,
        tmp_path / "backups",
        record=False,
        ledger=True,
        created_by="pre-delete",
        storage=True,
    )
    with Session(engine) as s:
        events_before = rs.state_rows(s, sd.ws, "events")

    req = run(rt, sd.principal("admin3"), hd.RequestHardDelete("account", victim, "erasure"), "r1")
    install_reauth(sd.accounts["admin1"], sd.accounts["admin2"], at=T0)
    run(rt, sd.principal("admin1"), hd.ApproveHardDelete(req.resource_id), "a1")
    run(rt, sd.principal("admin2"), hd.ApproveHardDelete(req.resource_id), "a2")
    clock.advance(dt.timedelta(hours=25))
    install_reauth(sd.accounts["admin1"], at=clock.now())
    done = run(rt, sd.principal("admin1"), hd.ExecuteHardDelete(req.resource_id), "x1")
    assert key_ref in done.data["keys_destroyed"]

    # the ledger a restore must consult is the one exported after the deletion
    with Session(engine) as s:
        entries = hd.export_ledger(s)
    ledger_after = tmp_path / "ledger-after.json"
    ledger_after.write_text(json.dumps(entries), encoding="utf-8")
    patched = json.loads(Path(str(manifest["manifest_path"])).read_text(encoding="utf-8"))
    for part in patched["parts"]:
        if part["name"] == "ledger":
            from tools.backup import sha256_of

            part["path"] = str(ledger_after)
            part["sha256"] = sha256_of(ledger_after)
            part["size_bytes"] = ledger_after.stat().st_size
    patched_path = tmp_path / "patched.manifest.json"
    patched_path.write_text(json.dumps(patched), encoding="utf-8")

    marker = tmp_path / "restore-pending.json"
    report = run_full_restore(
        patched_path,
        fresh_db,
        storage_targets={"artifacts": tmp_path / "ra", "documents": tmp_path / "rd"},
        marker=marker,
    )
    assert report["gate_open"] is True and restore_gate.pending(marker) is None
    ledger_report = report["ledger"]
    assert isinstance(ledger_report, dict)
    assert ledger_report["verified"] is True and key_ref in ledger_report["shredded"]

    restored = create_engine(normalize_url(fresh_db))
    try:
        with Session(restored) as s:
            status, wrapped = s.execute(
                text("SELECT status, wrapped_dek FROM sensitive_keys WHERE key_ref = :k"),
                {"k": key_ref},
            ).one()
            assert status == "destroyed" and wrapped is None
            ciphertext, ref = s.execute(
                text(
                    "SELECT sensitive_payload_ciphertext, sensitive_payload_key_ref FROM events "
                    "WHERE aggregate_id = :a AND sensitive_payload_ciphertext IS NOT NULL LIMIT 1"
                ),
                {"a": victim},
            ).one()
            with pytest.raises(CryptoError) as exc:
                sd.crypto().decrypt(s, str(ref), bytes(ciphertext))
            assert exc.value.code == "KEY_DESTROYED"
            assert rs.state_rows(s, sd.ws, "events") == events_before  # bytes unchanged
    finally:
        restored.dispose()


def test_the_gate_keeps_a_half_restored_instance_closed(tmp_path: Path) -> None:
    """V-P7-20 (gate): while reconciliation is outstanding the marker exists and readiness fails;
    an unreadable marker also keeps the gate shut."""
    marker = tmp_path / "restore-pending.json"
    assert restore_gate.pending(marker) is None
    restore_gate.mark_pending(
        marker, backup_id="bk-1", reason="RECONCILIATION_PENDING", now=dt.datetime.now(dt.UTC)
    )
    state: dict[str, Any] | None = restore_gate.pending(marker)
    assert state is not None and state["backup_id"] == "bk-1"
    assert oct(marker.stat().st_mode)[-3:] == "600"
    marker.write_text("{not json", encoding="utf-8")
    corrupt = restore_gate.pending(marker)
    assert corrupt is not None and corrupt["reason"] == "MARKER_UNREADABLE"
    assert restore_gate.clear_pending(marker) is True
    assert restore_gate.pending(marker) is None
    assert restore_gate.clear_pending(marker) is False
