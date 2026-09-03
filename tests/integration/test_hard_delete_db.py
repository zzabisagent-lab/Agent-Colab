"""P4-11 hard-delete workflow.
V-P4-22: single approval, skipped waiting period and direct DELETE attempts are rejected; dual
approval (distinct Humans, MFA re-auth) and the waiting period are enforced; tombstones and
AuditEvents are preserved. V-P4-25: only DEK destruction and display redaction happen; Event
bytes/hashes are unchanged."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.application import accounts as acc
from server.application import hard_delete as hd
from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.store import AppendRequest
from server.main import create_app
from server.secrets.envelope import CryptoError
from tests.integration.phase4_admin_seed import (
    T0,
    Seed,
    audit_actions,
    clear_reauth,
    install_reauth,
    run,
    seed,
)

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "hdel")


@pytest.fixture(autouse=True)
def _no_reauth() -> Iterator[None]:
    clear_reauth()
    yield
    clear_reauth()


def _events_dump(engine: Engine, ws: Any) -> list[tuple[Any, ...]]:
    with Session(engine) as s:
        return [
            tuple(r)
            for r in s.execute(
                text(
                    "SELECT event_id, aggregate_type, aggregate_id, aggregate_seq, type, "
                    "payload::text, "
                    "sensitive_payload_ciphertext, sensitive_payload_key_ref, previous_hash, "
                    "content_hash, "
                    "occurred_at FROM events WHERE workspace_id = :w ORDER BY recorded_seq"
                ),
                {"w": ws},
            ).all()
        ]


def _seed_target(engine: Engine, sd: Seed, rt: Any, account_id: str) -> str:
    """A human Account with sensitive (encrypted) Event payloads → one DEK to shred."""
    res = run(
        rt,
        sd.principal("admin3"),
        acc.CreateAccount(account_id, "Victim"),
        f"hd-create-{account_id}",
    )
    assert res.event_id
    with Session(engine) as s, s.begin():
        store = rt.store_for(s)
        for i in range(2):
            store.append(
                AppendRequest(
                    workspace_id=str(sd.ws),
                    aggregate_type="account",
                    aggregate_id=account_id,
                    type="ACCOUNT_UPDATED",
                    actor_account_id=str(sd.accounts["admin3"]),
                    correlation_id=f"corr-sens-{i}",
                    idempotency_scope="account:update",
                    idempotency_key=f"sens-{account_id}-{i}",
                    payload={"account_id": account_id, "fields": ["display_name"]},
                    sensitive={"previous_display_name": f"secret name {i}"},
                )
            )
        key_ref = s.execute(
            text(
                "SELECT key_ref FROM sensitive_keys WHERE target_type = 'account' AND "
                "target_id = :a"
            ),
            {"a": account_id},
        ).scalar_one()
    return str(key_ref)


def test_dual_approval_waiting_period_and_immutability(engine: Engine, sd: Seed) -> None:
    clock = FixedClock(T0)
    rt = sd.runtime(engine, clock)
    key_ref = _seed_target(engine, sd, rt, "acct-hdel-victim")
    admin3, admin1, admin2, member = (
        sd.principal(n) for n in ("admin3", "admin1", "admin2", "member")
    )
    req = run(
        rt,
        admin3,
        hd.RequestHardDelete("account", "acct-hdel-victim", "GDPR erasure request"),
        "hd-req-1",
    )
    request_id = req.resource_id
    assert req.data["quorum_required"] == 2 and req.data["waiting_period_hours"] == 24
    # execution before any approval
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-0")
    assert exc.value.code == "HARD_DELETE_NOT_APPROVED"
    # approval without MFA re-authentication fails closed
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ApproveHardDelete(request_id), "hd-appr-0")
    assert exc.value.code == "REAUTH_REQUIRED" and exc.value.status == 401
    install_reauth(sd.accounts["admin1"], sd.accounts["admin2"], sd.accounts["admin3"], at=T0)
    # the requester cannot approve their own request
    with pytest.raises(ApiError) as exc:
        run(rt, admin3, hd.ApproveHardDelete(request_id), "hd-appr-self")
    assert exc.value.code == "SELF_APPROVAL_FORBIDDEN"
    # a member without admin.hard_delete is denied with zero state change
    with pytest.raises(ApiError):
        run(rt, member, hd.ApproveHardDelete(request_id), "hd-appr-member")
    first = run(rt, admin1, hd.ApproveHardDelete(request_id), "hd-appr-1")
    assert first.data == {
        "approvals_recorded": 1,
        "quorum_required": 2,
        "approval_status": "PENDING",
    }
    # single approval is not enough; the same Human cannot approve twice
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-1")
    assert exc.value.code == "HARD_DELETE_NOT_APPROVED"
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ApproveHardDelete(request_id), "hd-appr-1b")
    assert exc.value.code == "APPROVER_DUPLICATE"
    second = run(rt, admin2, hd.ApproveHardDelete(request_id), "hd-appr-2")
    assert second.data["approval_status"] == "APPROVED" and second.data["approvals_recorded"] == 2
    assert second.data["executable_at"] == (T0 + dt.timedelta(hours=24)).isoformat()
    # the waiting period cannot be skipped
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-2")
    assert exc.value.code == "HARD_DELETE_WAITING_PERIOD"
    clock.advance(dt.timedelta(hours=23, minutes=59))
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-3")
    assert exc.value.code == "HARD_DELETE_WAITING_PERIOD"
    before = _events_dump(engine, sd.ws)
    with Session(engine) as s:
        s.execute(text("SELECT 1"))
        assert rt.crypto and rt.store_for(s)  # crypto configured
        wrapped_before = s.execute(
            text("SELECT wrapped_dek IS NOT NULL FROM sensitive_keys WHERE key_ref = :k"),
            {"k": key_ref},
        ).scalar_one()
        assert wrapped_before
    clock.advance(dt.timedelta(minutes=2))
    install_reauth(
        sd.accounts["admin1"], sd.accounts["admin2"], sd.accounts["admin3"], at=clock.now()
    )
    done = run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-4")
    assert done.data["keys_destroyed"] == [key_ref]
    assert (
        done.data["ledger_entry_hash"]
        and done.data["event_hash_before"] != done.data["event_hash_after"]
    )
    # V-P4-25: every pre-existing Event row is byte-for-byte identical; only new rows were appended
    after = _events_dump(engine, sd.ws)
    assert after[: len(before)] == before
    new_types = [row[4] for row in after[len(before) :]]
    assert new_types == ["ACCOUNT_HARD_DELETED", "HARD_DELETE_EXECUTED"]
    with Session(engine) as s:
        status, wrapped = s.execute(
            text("SELECT status, wrapped_dek FROM sensitive_keys WHERE key_ref = :k"),
            {"k": key_ref},
        ).one()
        assert status == "destroyed" and wrapped is None
        assert (
            s.execute(
                text("SELECT count(*) FROM key_tombstones WHERE key_ref = :k"), {"k": key_ref}
            ).scalar_one()
            == 1
        )
        tomb = s.execute(
            text(
                "SELECT event_hash_before, event_hash_after, keys_destroyed FROM "
                "hard_delete_tombstones WHERE request_id = :r"
            ),
            {"r": request_id},
        ).one()
        assert tomb[2] == [key_ref]
        ciphertext, ref = s.execute(
            text(
                "SELECT sensitive_payload_ciphertext, sensitive_payload_key_ref FROM "
                "events WHERE aggregate_id = 'acct-hdel-victim' AND "
                "sensitive_payload_ciphertext IS NOT NULL LIMIT 1"
            )
        ).one()
        with pytest.raises(CryptoError) as cerr:
            rt.crypto.decrypt(s, str(ref), bytes(ciphertext))
        assert cerr.value.code == "KEY_DESTROYED"
        acct = s.execute(
            text("SELECT status, display_name FROM accounts WHERE account_id = 'acct-hdel-victim'")
        ).one()
        assert acct == ("DELETED", hd.REDACTED)
        # tombstone rows are immutable
        with pytest.raises(Exception):  # noqa: B017 - trigger raises a DB error
            with s.begin():
                s.execute(
                    text("DELETE FROM hard_delete_tombstones WHERE request_id = :r"),
                    {"r": request_id},
                )
    replay = run(rt, admin1, hd.ExecuteHardDelete(request_id), "hd-exec-5")
    assert replay.replayed
    trail = audit_actions(engine, sd.ws, request_id)
    assert trail[0] == "hard_delete.request" and trail[-1] == "hard_delete.execute"
    assert "hard_delete.approved" in trail
    view = hd.request_view  # read model
    with Session(engine) as s:
        v = view(s, request_id)
    assert v and v["status"] == "EXECUTED" and v["tombstone"]["keys_destroyed"] == [key_ref]


def test_reject_and_cancel_paths(engine: Engine, sd: Seed) -> None:
    clock = FixedClock(T0)
    rt = sd.runtime(engine, clock)
    _seed_target(engine, sd, rt, "acct-hdel-keep")
    admin3, admin1 = sd.principal("admin3"), sd.principal("admin1")
    req = run(
        rt,
        admin3,
        hd.RequestHardDelete("account", "acct-hdel-keep", "mistaken request"),
        "hd-req-2",
    )
    install_reauth(sd.accounts["admin1"], at=T0)
    rej = run(
        rt, admin1, hd.ApproveHardDelete(req.resource_id, "REJECT", "NOT_JUSTIFIED"), "hd-rej-1"
    )
    assert rej.data["approval_status"] == "REJECTED"
    with Session(engine) as s:
        rejected = hd.request_view(s, req.resource_id)
        assert rejected is not None and rejected["status"] == "REJECTED"
        assert (
            s.execute(
                text("SELECT status FROM sensitive_keys WHERE target_id = 'acct-hdel-keep'")
            ).scalar_one()
            == "active"
        )
    with pytest.raises(ApiError) as exc:
        run(rt, admin1, hd.ExecuteHardDelete(req.resource_id), "hd-exec-rej")
    assert exc.value.code == "HARD_DELETE_NOT_APPROVED"
    req2 = run(
        rt, admin3, hd.RequestHardDelete("account", "acct-hdel-keep", "second thoughts"), "hd-req-3"
    )
    cancelled = run(rt, admin3, hd.CancelHardDelete(req2.resource_id), "hd-cancel-1")
    assert cancelled.event_id
    with Session(engine) as s:
        cancelled_view = hd.request_view(s, req2.resource_id)
        assert cancelled_view is not None and cancelled_view["status"] == "CANCELLED"


def test_direct_delete_endpoints_are_refused(database_url: str, sd: Seed) -> None:
    app = create_app(
        Settings(database_url=database_url, base_url="http://t", master_key_b64=sd.master_key_b64)
    )
    with TestClient(app) as client:
        h = sd.headers("admin1", "hd-api-1")
        for path in (
            "/api/v1/accounts/acct-hdel-victim",
            "/api/v1/hard-delete/targets/artifact/art-x",
        ):
            r = client.delete(path, headers=h)
            assert r.status_code == 405 and r.json()["code"] == "HARD_DELETE_WORKFLOW_REQUIRED", (
                path
            )
        r = client.post(
            "/api/v1/hard-delete/requests",
            json={"target_type": "account", "target_id": "acct-nope", "reason": "x y z"},
            headers=h,
        )
        assert r.status_code == 404 and r.json()["code"] == "HARD_DELETE_TARGET_NOT_FOUND"
        r = client.get("/api/v1/hard-delete/requests", headers=h)
        assert r.status_code == 200 and {i["status"] for i in r.json()["items"]} >= {
            "EXECUTED",
            "REJECTED",
        }
        assert (
            client.get(
                "/api/v1/hard-delete/requests", headers=sd.headers("member", "r")
            ).status_code
            == 404
        )
