"""P4-11 backup/restore (V-P4-29): backup before deletion → hard delete → restore into an empty
database → after tombstone reconciliation the destroyed DEK is not decryptable or reactivated,
while Event hashes are preserved."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from server.application import hard_delete as hd
from server.db.engine import make_engine, normalize_url
from server.domain.clock import FixedClock
from server.secrets.envelope import CryptoError
from tests.conftest import TEST_URL
from tests.integration.phase4_admin_seed import T0, Seed, install_reauth, run, seed
from tests.integration.test_hard_delete_db import _seed_target
from tools.backup import pg_bin, run_backup
from tools.restore import run_restore

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "bkr")


@pytest.fixture
def empty_db() -> Iterator[str]:
    assert TEST_URL
    base = normalize_url(TEST_URL)
    name = "colab_restore_" + uuid.uuid4().hex[:10]
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.rsplit("/", 1)[0] + f"/{name}"
    finally:
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        maint.dispose()


def test_restore_reconciles_tombstones_and_keeps_event_hashes(
    database_url: str, engine: Engine, sd: Seed, tmp_path: Path, empty_db: str
) -> None:
    if not (pg_bin() / "pg_dump").exists():
        pytest.skip("pg_dump not available")
    clock = FixedClock(T0)
    rt = sd.runtime(engine, clock)
    key_ref = _seed_target(engine, sd, rt, "acct-bkr-victim")
    # 1. backup taken while the DEK is still active (ledger exported alongside, kept separately)
    manifest = run_backup(
        database_url, tmp_path / "backups", record=True, ledger=True, created_by="test"
    )
    assert manifest["includes_master_key"] is False and manifest["includes_ledger"] is True
    with Session(engine) as s:
        hashes_before = {
            str(r[0]): str(r[1])
            for r in s.execute(
                text("SELECT event_id, content_hash FROM events WHERE workspace_id = :w"),
                {"w": sd.ws},
            ).all()
        }
        assert (
            s.execute(
                text("SELECT count(*) FROM backups WHERE backup_id = :b"),
                {"b": manifest["backup_id"]},
            ).scalar_one()
            == 1
        )
    # 2. hard delete (dual approval + waiting period)
    req = run(
        rt,
        sd.principal("admin3"),
        hd.RequestHardDelete("account", "acct-bkr-victim", "erasure"),
        "bkr-req",
    )
    install_reauth(sd.accounts["admin1"], sd.accounts["admin2"], at=T0)
    run(rt, sd.principal("admin1"), hd.ApproveHardDelete(req.resource_id), "bkr-a1")
    run(rt, sd.principal("admin2"), hd.ApproveHardDelete(req.resource_id), "bkr-a2")
    clock.advance(dt.timedelta(hours=25))
    install_reauth(sd.accounts["admin1"], at=clock.now())
    done = run(rt, sd.principal("admin1"), hd.ExecuteHardDelete(req.resource_id), "bkr-exec")
    assert done.data["keys_destroyed"] == [key_ref]
    # 3. the ledger exported *after* the deletion is what a restore must consult
    with Session(engine) as s:
        entries = hd.export_ledger(s)
    assert (
        any(e["key_ref"] == key_ref for e in entries) and hd.verify_exported_ledger(entries) == []
    )
    ledger_path = tmp_path / "ledger-after.json"
    ledger_path.write_text(json.dumps(entries), encoding="utf-8")
    # 4. restore the pre-deletion dump into an empty database and reconcile
    report = run_restore(Path(str(manifest["path"])), empty_db, ledger_path)
    ledger_report = report["ledger"]
    assert isinstance(ledger_report, dict)
    assert ledger_report["verified"] is True and key_ref in ledger_report["shredded"]
    restored = create_engine(normalize_url(empty_db))
    try:
        with Session(restored) as s:
            status, wrapped = s.execute(
                text("SELECT status, wrapped_dek FROM sensitive_keys WHERE key_ref = :k"),
                {"k": key_ref},
            ).one()
            assert status == "destroyed" and wrapped is None  # never resurrected
            assert (
                s.execute(
                    text("SELECT count(*) FROM key_tombstones WHERE key_ref = :k"), {"k": key_ref}
                ).scalar_one()
                == 1
            )
            ciphertext, ref = s.execute(
                text(
                    "SELECT sensitive_payload_ciphertext, sensitive_payload_key_ref FROM events "
                    "WHERE aggregate_id = 'acct-bkr-victim' AND "
                    "sensitive_payload_ciphertext IS NOT "
                    "NULL LIMIT 1"
                )
            ).one()
            with pytest.raises(CryptoError) as exc:
                sd.crypto().decrypt(s, str(ref), bytes(ciphertext))
            assert exc.value.code == "KEY_DESTROYED"
            hashes_restored = {
                str(r[0]): str(r[1])
                for r in s.execute(
                    text("SELECT event_id, content_hash FROM events WHERE workspace_id = :w"),
                    {"w": sd.ws},
                ).all()
            }
        assert hashes_restored == hashes_before  # Event hashes preserved through backup/restore
        # reconciliation is idempotent
        with Session(restored) as s, s.begin():
            again = hd.reconcile_tombstones(s, entries, clock.now())
        assert again["shredded"] == [] and key_ref in again["already_destroyed"]
    finally:
        restored.dispose()
