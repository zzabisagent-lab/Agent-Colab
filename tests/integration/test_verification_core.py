"""P1-06: V-P1-12 (self-verification rejected), V-P1-13 (immutable revisions), V-P1-14 (complete
gate), V-P1-24 (identity snapshot immutable and reproducible)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

from server.application import bus
from server.application.authz import AllowAllAuthorizer
from server.application.criteria import ReviseCriteria, current_criteria
from server.application.tasks import (
    AcceptTask,
    CompleteTask,
    CreateTask,
    DelegateTask,
    StartTask,
    StartVerification,
    SubmitImplementation,
)
from server.application.verification import CreateVerificationRun, SubmitVerdict
from server.config import Settings
from server.db.engine import RUNTIME_ROLE, make_engine, make_engine_for_role
from server.documents.lifecycle import expected_document_id
from server.domain.clock import FixedClock
from server.events.chain import VERIFICATION_CHAIN, verify_chain
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import token_hash
from server.main import create_app
from server.verification.gate import require_verified, verification_gate
from server.verification.runs import (
    independence_from_snapshot,
    load_snapshot,
    snapshot_hash,
)

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
ACCOUNTS: dict[str, uuid.UUID] = {
    n: uuid.uuid4()
    for n in ("acct-vc-admin", "acct-vc-impl", "acct-vc-ver", "acct-vc-alias", "acct-vc-shared")
}
TOKENS = {
    "acct-vc-admin": ("tok-vc-admin", "sha256:vc-admin"),
    "acct-vc-impl": ("tok-vc-impl", "sha256:vc-impl"),
    "acct-vc-ver": ("tok-vc-ver", "sha256:vc-ver"),
    "acct-vc-alias": ("tok-vc-alias", "sha256:vc-alias"),
    "acct-vc-shared": ("tok-vc-shared", "sha256:vc-shared-fp"),
}
CLOCK = FixedClock(dt.datetime(2026, 3, 1, tzinfo=dt.UTC))


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-vc', 'vc')"),
            {"i": WS},
        )
        for name, acc in ACCOUNTS.items():
            typ = "service" if name.endswith("admin") else "agent"
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, "
                    "account_type, display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            token, fp = TOKENS[name]
            c.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, "
                    "fingerprint, token_hash) VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": fp, "h": token_hash(token)},
            )
        c.execute(
            text(
                "INSERT INTO account_aliases (account_id, alias_of_account_id, "
                "reason) VALUES (:a, :b, 'same operator')"
            ),
            {"a": ACCOUNTS["acct-vc-alias"], "b": ACCOUNTS["acct-vc-impl"]},
        )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, "
                "channel_type, display_name) VALUES (:i, 'chan-vc', :w, 'work', 'vc')"
            ),
            {"i": CHANNEL, "w": WS},
        )
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def client(database_url: str, engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url))
    app.state.runtime.workspace_id = str(WS)
    app.state.runtime.authorizer = AllowAllAuthorizer()
    app.state.runtime.clock = CLOCK
    with TestClient(app) as c:
        yield c


def _h(name: str, key: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKENS[name][0]}",
        "Idempotency-Key": key or uuid.uuid4().hex,
    }


def _create(client: TestClient, target_id: str, **over: Any) -> dict[str, Any]:
    body = {
        "target_type": "phase",
        "target_id": target_id,
        "implementer_account_id": "acct-vc-impl",
        "verifier_account_id": "acct-vc-ver",
        "implementer_credential_fingerprint": "sha256:vc-impl",
        "verifier_credential_fingerprint": "sha256:vc-ver",
        "target_commit": "abc123",
        "effective_policy_hash": "sha256:policy",
        "implementer_agent_id": "agent-vc-impl",
        "verifier_agent_id": "agent-vc-ver",
        "phase": 1,
    }
    body.update(over)
    r = client.post("/api/v1/verification-runs", json=body, headers=_h("acct-vc-admin"))
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _report(result: str, **over: Any) -> dict[str, Any]:
    rep: dict[str, Any] = {
        "result": result,
        "criteria_version": "v8.0",
        "tests": [
            {
                "id": "V-P1-01",
                "result": "PASS" if result == "PASSED" else "FAIL",
                "evidence_ref": "e/1",
            }
        ],
        "findings": []
        if result == "PASSED"
        else [{"id": "F-1", "severity": "High", "summary": "broken"}],
        "residual_risks": [],
    }
    rep.update(over)
    return rep


def _verdict(client: TestClient, vid: str, who: str, result: str, key: str | None = None) -> Any:
    return client.post(
        f"/api/v1/verification-runs/{vid}/verdict",
        json={"result": result, "report": _report(result)},
        headers=_h(who, key),
    )


def _count(engine: Engine, sql: str, **p: Any) -> int:
    with engine.connect() as c:
        return int(c.execute(text(sql), p).scalar_one())


# ---------------------------------------------------------------- V-P1-12


def test_self_verification_rejected_at_api_and_db_with_audit(
    client: TestClient, engine: Engine
) -> None:
    vid = _create(client, "phase-self")["verification_id"]
    client.post(f"/api/v1/verification-runs/{vid}/assign", headers=_h("acct-vc-admin"))
    client.post(f"/api/v1/verification-runs/{vid}/start", headers=_h("acct-vc-ver"))
    before = _count(
        engine,
        "SELECT count(*) FROM audit_events WHERE action = 'verification.self_submit_rejected'",
    )
    for who in ("acct-vc-impl", "acct-vc-alias"):
        r = _verdict(client, vid, who, "PASSED")
        assert r.status_code == 409 and r.json()["code"] == "SELF_VERIFICATION_FORBIDDEN", r.text
    # a different account presenting the implementer's credential fingerprint
    run2 = _create(client, "phase-shared", implementer_credential_fingerprint="sha256:vc-shared-fp")
    vid2 = run2["verification_id"]
    client.post(f"/api/v1/verification-runs/{vid2}/assign", headers=_h("acct-vc-admin"))
    client.post(f"/api/v1/verification-runs/{vid2}/start", headers=_h("acct-vc-ver"))
    r = _verdict(client, vid2, "acct-vc-shared", "PASSED")
    assert r.status_code == 409 and r.json()["code"] == "SELF_VERIFICATION_FORBIDDEN"
    after = _count(
        engine,
        "SELECT count(*) FROM audit_events WHERE action = 'verification.self_submit_rejected'",
    )
    assert after == before + 3
    assert (
        _count(
            engine,
            "SELECT count(*) FROM verification_revisions WHERE verification_id IN (:a, :b)",
            a=vid,
            b=vid2,
        )
        == 0
    )
    assert (
        _count(
            engine,
            "SELECT count(*) FROM events WHERE aggregate_id IN (:a, :b) AND "
            "type LIKE 'VERIFICATION_%'",
            a=vid,
            b=vid2,
        )
        == 0
    )
    # DB level: a run with the same implementer and verifier cannot even be created
    r = client.post(
        "/api/v1/verification-runs",
        json={**_body_same(), "verifier_account_id": "acct-vc-impl"},
        headers=_h("acct-vc-admin"),
    )
    assert r.status_code == 409 and r.json()["code"] == "VERIFIER_SAME_ACCOUNT"
    with pytest.raises(DBAPIError, match="ck_vr_distinct_accounts"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO verification_runs (id, verification_id, "
                "workspace_id, target_type, target_id, implementer_account_id, "
                "verifier_account_id, implementer_credential_fingerprint, "
                "verifier_credential_fingerprint, identity_graph_version, "
                "effective_policy_hash, criteria_version, target_commit, "
                "snapshot_hash, created_by_account_id) VALUES (:id, 'vr-vc-raw', :ws, "
                "'phase', 'p', :i, :i, 'a', 'b', 'g', 'p', 'v8.0', 'c', 's', :i)"
            ),
            {"id": uuid.uuid4(), "ws": WS, "i": ACCOUNTS["acct-vc-impl"]},
        )


def _body_same() -> dict[str, Any]:
    return {
        "target_type": "phase",
        "target_id": "phase-same",
        "implementer_account_id": "acct-vc-impl",
        "verifier_account_id": "acct-vc-ver",
        "implementer_credential_fingerprint": "sha256:vc-impl",
        "verifier_credential_fingerprint": "sha256:vc-ver",
        "target_commit": "abc",
        "effective_policy_hash": "p",
    }


# ---------------------------------------------------------------- V-P1-13


def test_fail_fix_recheck_pass_keeps_previous_revision_immutable(
    client: TestClient, engine: Engine, database_url: str
) -> None:
    vid = _create(client, "phase-rev")["verification_id"]
    assert (
        client.post(f"/api/v1/verification-runs/{vid}/assign", headers=_h("acct-vc-admin")).json()[
            "status"
        ]
        == "ASSIGNED"
    )
    assert (
        client.post(f"/api/v1/verification-runs/{vid}/start", headers=_h("acct-vc-ver")).json()[
            "status"
        ]
        == "RUNNING"
    )
    r1 = _verdict(client, vid, "acct-vc-ver", "FAILED", key="verdict-1")
    assert r1.status_code == 200 and r1.json()["status"] == "FAILED" and r1.json()["revision"] == 1
    replay = _verdict(client, vid, "acct-vc-ver", "FAILED", key="verdict-1")
    assert replay.json()["replayed"] is True and replay.json()["revision"] == 1
    with engine.connect() as c:
        row1 = c.execute(
            text(
                "SELECT content_hash, previous_hash, result FROM "
                "verification_revisions WHERE verification_id = :v AND revision = 1"
            ),
            {"v": vid},
        ).first()
    assert row1 is not None and row1[2] == "FAILED"
    # implementer alone may submit the fix; verifier cannot
    assert (
        client.post(
            f"/api/v1/verification-runs/{vid}/fix",
            json={"fix_commit": "def456"},
            headers=_h("acct-vc-ver"),
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/verification-runs/{vid}/fix",
            json={"fix_commit": "def456"},
            headers=_h("acct-vc-impl"),
        ).json()["status"]
        == "FIX_SUBMITTED"
    )
    assert (
        client.post(f"/api/v1/verification-runs/{vid}/recheck", headers=_h("acct-vc-admin")).json()[
            "status"
        ]
        == "RECHECK_ASSIGNED"
    )
    assert (
        client.post(f"/api/v1/verification-runs/{vid}/start", headers=_h("acct-vc-ver")).json()[
            "status"
        ]
        == "RUNNING"
    )
    r2 = _verdict(client, vid, "acct-vc-ver", "PASSED")
    assert r2.status_code == 200 and r2.json()["status"] == "PASSED" and r2.json()["revision"] == 2
    with engine.connect() as c:
        row1_after = c.execute(
            text(
                "SELECT content_hash, previous_hash, result FROM "
                "verification_revisions WHERE verification_id = :v AND revision = 1"
            ),
            {"v": vid},
        ).first()
        row2 = c.execute(
            text(
                "SELECT previous_hash FROM verification_revisions WHERE "
                "verification_id = :v AND revision = 2"
            ),
            {"v": vid},
        ).first()
        run = c.execute(
            text(
                "SELECT status, result, current_revision FROM "
                "verification_runs WHERE verification_id = :v"
            ),
            {"v": vid},
        ).first()
    assert row1_after == row1 and row2 is not None and run == ("PASSED", "PASSED", 2)
    with Session(engine) as s:
        assert verify_chain(s, VERIFICATION_CHAIN) == []
    # previous revision immutable: owner blocked by trigger, runtime role by permission
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE verification_revisions SET result = 'PASSED' WHERE "
                "verification_id = :v AND revision = 1"
            ),
            {"v": vid},
        )
    rt = make_engine_for_role(database_url, RUNTIME_ROLE)
    try:
        with pytest.raises(ProgrammingError, match="permission denied"), rt.begin() as c:
            c.execute(
                text("DELETE FROM verification_revisions WHERE verification_id = :v"), {"v": vid}
            )
    finally:
        rt.dispose()
    # terminal: no further verdicts
    assert _verdict(client, vid, "acct-vc-ver", "FAILED").json()["code"] == "VERIFICATION_TERMINAL"
    detail = client.get(f"/api/v1/verification-runs/{vid}", headers=_h("acct-vc-ver")).json()
    assert [r["revision"] for r in detail["revisions"]] == [1, 2] and detail["status"] == "PASSED"


# ---------------------------------------------------------------- V-P1-24


def test_identity_snapshot_is_immutable_and_reproducible(
    client: TestClient, engine: Engine
) -> None:
    created = _create(client, "phase-snap")
    vid = created["verification_id"]
    with Session(engine) as s:
        snap, h = load_snapshot(s, vid)
        run_hash = s.execute(
            text("SELECT snapshot_hash FROM verification_runs WHERE verification_id = :v"),
            {"v": vid},
        ).scalar_one()
    assert h == run_hash == created["snapshot_hash"] == snapshot_hash(snap)
    raw_before = json.dumps(snap, sort_keys=True)
    independence_from_snapshot(snap)
    # mutate the live identities: display name, credential rotation, a new alias edge
    with engine.begin() as c:
        c.execute(
            text("UPDATE accounts SET display_name = 'renamed' WHERE id = :a"),
            {"a": ACCOUNTS["acct-vc-ver"]},
        )
        c.execute(
            text(
                "UPDATE service_credentials SET status = 'revoked', revoked_at "
                "= now() WHERE account_id = :a"
            ),
            {"a": ACCOUNTS["acct-vc-ver"]},
        )
        c.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, "
                "token_hash) VALUES (:i, :a, 'sha256:vc-ver-rotated', :h)"
            ),
            {
                "i": uuid.uuid4(),
                "a": ACCOUNTS["acct-vc-ver"],
                "h": token_hash("tok-vc-ver-rotated"),
            },
        )
        c.execute(
            text(
                "INSERT INTO account_aliases (account_id, alias_of_account_id, "
                "reason) VALUES (:a, :b, 'late edge')"
            ),
            {"a": ACCOUNTS["acct-vc-shared"], "b": ACCOUNTS["acct-vc-ver"]},
        )
    with Session(engine) as s:
        snap_after, h_after = load_snapshot(s, vid)
    assert json.dumps(snap_after, sort_keys=True) == raw_before and h_after == h
    independence_from_snapshot(snap_after)
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE credential_identity_snapshots SET snapshot_hash = 'x' "
                "WHERE verification_id = :v"
            ),
            {"v": vid},
        )
    with pytest.raises(DBAPIError, match="VERIFICATION_SNAPSHOT_IMMUTABLE"), engine.begin() as c:
        c.execute(
            text("UPDATE verification_runs SET snapshot_hash = 'x' WHERE verification_id = :v"),
            {"v": vid},
        )
    # restore the verifier token for later tests
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE service_credentials SET status = 'active', revoked_at "
                "= NULL WHERE fingerprint = 'sha256:vc-ver'"
            )
        )


# ---------------------------------------------------------------- V-P1-14 (gate + Task wiring)


def _ctx(session: Session, who: str, key: str) -> bus.CommandContext:
    name = who
    return bus.CommandContext(
        session=session,
        store=PostgresEventStore(session, clock=CLOCK),
        authorizer=AllowAllAuthorizer(),
        clock=CLOCK,
        principal=bus.Principal(name, str(ACCOUNTS[name]), "agent", TOKENS[name][1]),
        workspace_id=str(WS),
        correlation_id="corr-vc",
        idempotency_key=key,
    )


def test_completion_gate_requires_passed_verification(client: TestClient, engine: Engine) -> None:
    with Session(engine) as s:
        assert verification_gate(s, "task", "task-vc-none").passed is False
        with pytest.raises(bus.CommandError) as exc:
            require_verified(s, "task", "task-vc-none")
        assert exc.value.code == "VERIFICATION_REQUIRED"
    # full Task flow through the bus: complete before verification is rejected, after PASSED allowed
    with Session(engine) as s, s.begin():
        created = bus.execute(
            CreateTask("gate", str(CHANNEL), "research"), _ctx(s, "acct-vc-admin", "t-create")
        )
        task_id = created.resource_id
        bus.execute(
            ReviseCriteria(
                task_id,
                ({"statement": "report attached", "check_type": "evidence", "required": True},),
            ),
            _ctx(s, "acct-vc-admin", "t-criteria"),
        )
        current = current_criteria(s, task_id)
        bus.execute(DelegateTask(task_id, "acct-vc-impl"), _ctx(s, "acct-vc-admin", "t-delegate"))
        bus.execute(AcceptTask(task_id), _ctx(s, "acct-vc-impl", "t-accept"))
        bus.execute(StartTask(task_id), _ctx(s, "acct-vc-impl", "t-start"))
        refs = tuple(f"{c.criteria_id}:evidence/1" for c in current.criteria)
        bus.execute(
            SubmitImplementation(task_id, refs, criteria_revision=current.revision),
            _ctx(s, "acct-vc-impl", "t-submit"),
        )
        with pytest.raises(bus.CommandError) as exc:
            bus.execute(
                CompleteTask(task_id, "doc-none"), _ctx(s, "acct-vc-admin", "t-complete-early")
            )
        assert exc.value.code == "COMPLETION_PREREQUISITE_MISSING"
        run = bus.execute(
            CreateVerificationRun(
                target_type="task",
                target_id=task_id,
                implementer_account_id="acct-vc-impl",
                verifier_account_id="acct-vc-ver",
                implementer_credential_fingerprint="sha256:vc-impl",
                verifier_credential_fingerprint="sha256:vc-ver",
                target_commit="abc",
                effective_policy_hash="p",
                task_id=task_id,
            ),
            _ctx(s, "acct-vc-admin", "t-vr"),
        )
        vid = run.resource_id
        bus.execute(StartVerification(task_id, vid), _ctx(s, "acct-vc-admin", "t-verifying"))
        assert verification_gate(s, "task", task_id).passed is False
        with pytest.raises(bus.CommandError) as exc:
            bus.execute(
                SubmitVerdict(vid, "PASSED", _report("PASSED")), _ctx(s, "acct-vc-impl", "t-self")
            )
        assert exc.value.code == "SELF_VERIFICATION_FORBIDDEN"
        # run must be RUNNING for a verdict (assign/start)
        from server.application.verification import AssignVerifier
        from server.application.verification import StartVerification as StartRun

        bus.execute(AssignVerifier(vid), _ctx(s, "acct-vc-admin", "t-assign"))
        bus.execute(StartRun(vid), _ctx(s, "acct-vc-ver", "t-run"))
        verdict = bus.execute(
            SubmitVerdict(vid, "PASSED", _report("PASSED")), _ctx(s, "acct-vc-ver", "t-pass")
        )
        assert verdict.data["status"] == "PASSED"
        gate = verification_gate(s, "task", task_id)
        assert gate.passed and gate.verification_id == vid and gate.revision == 1
        done = bus.execute(
            CompleteTask(task_id, expected_document_id(s, task_id) or "doc-missing"),
            _ctx(s, "acct-vc-admin", "t-complete"),
        )
        assert done.data.get("status") in ("COMPLETED", None)
        status = s.execute(
            text("SELECT status, verification_status FROM tasks_projection WHERE task_id = :t"),
            {"t": task_id},
        ).first()
        assert status == ("COMPLETED", "PASSED")
