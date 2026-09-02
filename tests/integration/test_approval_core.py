"""P1-08 on the real database: V-P1-15 (subjects), V-P1-16 (bounded consume), V-P1-22 (ledger over
projection), V-P1-32 (approver eligibility and quorum)."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bus
from server.application.approvals import (
    CancelApproval,
    ConsumeApproval,
    DecideApproval,
    ExpireApprovals,
    RequestApproval,
    RevokeApproval,
)
from server.application.authz import AllowAllAuthorizer
from server.approvals import service
from server.approvals.model import ApprovalStatus, Subject
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.policy.authorization import Authorizer
from server.policy.repository import PostgresPolicyRepository
from server.projections.approvals import rebuild_approvals, snapshot_hash

pytestmark = pytest.mark.db

NOW = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
WS = uuid.uuid4()
CHAN = uuid.uuid4()
IDS: dict[str, uuid.UUID] = {
    n: uuid.uuid4()
    for n in (
        "acct-ap-req",
        "acct-ap-h1",
        "acct-ap-h2",
        "acct-ap-h3",
        "acct-ap-low",
        "acct-ap-none",
        "acct-ap-alias",
        "acct-ap-agent",
        "acct-ap-impl",
        "acct-ap-svc",
        "acct-ap-admin",
    )
}
TYPES = {"acct-ap-agent": "agent", "acct-ap-impl": "agent", "acct-ap-svc": "service"}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    repo = PostgresPolicyRepository()
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ap', 'ap')"),
            {"i": WS},
        )
        for name, acc in IDS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "  # noqa: E501
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": TYPES.get(name, "human")},
            )
        for agent, owner in (("agent-ap-1", "acct-ap-agent"), ("agent-ap-impl", "acct-ap-impl")):
            s.execute(
                text(
                    "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, display_name) "  # noqa: E501
                    "VALUES (:i, :g, :w, :a, 'mcp', 'active', :g)"
                ),
                {"i": uuid.uuid4(), "g": agent, "w": WS, "a": IDS[owner]},
            )
        s.execute(
            text(
                "INSERT INTO account_aliases (account_id, alias_of_account_id, reason) VALUES (:a, :b, 'same person')"  # noqa: E501
            ),
            {"a": IDS["acct-ap-alias"], "b": IDS["acct-ap-req"]},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-ap', :w, 'approval', 'ap')"
            ),
            {"i": CHAN, "w": WS},
        )
        for name in (
            "acct-ap-req",
            "acct-ap-h1",
            "acct-ap-h2",
            "acct-ap-h3",
            "acct-ap-low",
            "acct-ap-none",
            "acct-ap-alias",
            "acct-ap-agent",
            "acct-ap-impl",
            "acct-ap-admin",
        ):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHAN, "a": IDS[name]},
            )
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, title, domain, risk, "  # noqa: E501
                "status, created_at, updated_at) VALUES ('task-ap-1', :w, 'task-ap-1', :c, 'T', 'research', 'LOW', "  # noqa: E501
                "'RUNNING', now(), now())"
            ),
            {"w": WS, "c": CHAN},
        )
        repo.create_role(s, WS, "role-ap-approver", "Approver")
        repo.commit_role_version(
            s,
            "role-ap-approver",
            ["approval.decide", "approval.request", "approval.read"],
            [],
            {"max_risk": "CRITICAL"},
            IDS["acct-ap-admin"],
        )
        repo.create_role(s, WS, "role-ap-low", "Low approver")
        repo.commit_role_version(
            s, "role-ap-low", ["approval.decide"], [], {"max_risk": "LOW"}, IDS["acct-ap-admin"]
        )
        repo.create_role(s, WS, "role-ap-admin", "Admin")
        repo.commit_role_version(
            s,
            "role-ap-admin",
            ["admin.accounts", "approval.revoke", "approval.request"],
            [],
            {"max_risk": "CRITICAL"},
            IDS["acct-ap-admin"],
        )
        since = NOW - dt.timedelta(days=1)
        for name in (
            "acct-ap-req",
            "acct-ap-h1",
            "acct-ap-h2",
            "acct-ap-h3",
            "acct-ap-alias",
            "acct-ap-agent",
            "acct-ap-impl",
        ):
            repo.assign_role(s, IDS[name], "role-ap-approver", IDS["acct-ap-admin"], since)
        repo.assign_role(s, IDS["acct-ap-low"], "role-ap-low", IDS["acct-ap-admin"], since)
        repo.assign_role(s, IDS["acct-ap-admin"], "role-ap-admin", IDS["acct-ap-admin"], since)
    yield eng
    eng.dispose()


def _ctx(
    session: Session, who: str, key: str, clock: FixedClock | None = None, **extras: Any
) -> bus.CommandContext:
    clock = clock or FixedClock(NOW)
    principal = bus.Principal(
        who,
        str(IDS[who]),
        TYPES.get(who, "human"),
        f"fp-{who}",
        mfa_verified=bool(extras.pop("mfa", False)),
    )
    return bus.CommandContext(
        session=session,
        store=PostgresEventStore(session, clock=clock),
        authorizer=AllowAllAuthorizer(),
        clock=clock,
        principal=principal,
        workspace_id=str(WS),
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        extras={"policy_authorizer": Authorizer(PostgresPolicyRepository(), clock=clock), **extras},
    )


def _request(engine: Engine, key: str, who: str = "acct-ap-req", **over: Any) -> str:
    fields: dict[str, Any] = {
        "subject_type": "task",
        "subject_id": "task-ap-1",
        "action": "tool:task_delegate",
        "channel_uuid": str(CHAN),
    }
    fields.update(over)
    with Session(engine) as s, s.begin():
        return bus.execute(RequestApproval(**fields), _ctx(s, who, key)).resource_id


def _decide(
    engine: Engine, approval_id: str, who: str, key: str, decision: str = "APPROVE", **extras: Any
) -> bus.CommandResult:
    with Session(engine) as s, s.begin():
        return bus.execute(DecideApproval(approval_id, decision), _ctx(s, who, key, **extras))


def _status(engine: Engine, approval_id: str) -> str:
    with engine.connect() as c:
        return str(
            c.execute(
                text("SELECT status FROM approval_grants WHERE approval_id = :a"),
                {"a": approval_id},
            ).scalar_one()
        )


def _events(engine: Engine, approval_id: str) -> list[str]:
    with engine.connect() as c:
        return [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT type FROM events WHERE aggregate_type = 'approval' AND aggregate_id = :a ORDER BY aggregate_seq"  # noqa: E501
                ),
                {"a": approval_id},
            ).all()
        ]


# ---------------------------------------------------------------- V-P1-15 subjects
def test_task_and_action_subjects_fixed_schedule_run_not_active(engine: Engine) -> None:
    task_apr = _request(engine, "s-task")
    action_apr = _request(
        engine,
        "s-action",
        subject_type="action",
        subject_id="external_send",
        action="api:external_link_approve",
    )
    assert _status(engine, task_apr) == "PENDING" and _status(engine, action_apr) == "PENDING"
    assert _events(engine, task_apr) == ["APPROVAL_REQUESTED"]
    for st in ("schedule", "run"):
        with Session(engine) as s, s.begin():
            with pytest.raises(bus.CommandError) as exc:
                bus.execute(
                    RequestApproval(st, "sch-1", "tool:schedule_run_now"),
                    _ctx(s, "acct-ap-req", f"s-{st}"),
                )
            assert exc.value.code == "SUBJECT_TYPE_NOT_ACTIVE"
    with engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM approval_grants WHERE subject_type IN ('schedule','run')"
                )
            ).scalar_one()
            == 0
        )
    with Session(engine) as s, s.begin():
        with pytest.raises(bus.CommandError) as exc2:
            bus.execute(
                RequestApproval("task", "task-missing", "tool:task_delegate"),
                _ctx(s, "acct-ap-req", "s-missing"),
            )
        assert exc2.value.code == "SUBJECT_NOT_FOUND"
    # the catalog risk applies unless a higher risk is given
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT risk FROM approval_grants WHERE approval_id = :a"), {"a": task_apr}
            ).scalar_one()
            == "MEDIUM"
        )
    high = _request(engine, "s-high", risk="HIGH")
    with engine.connect() as c:
        assert c.execute(
            text("SELECT risk, quorum_required FROM approval_grants WHERE approval_id = :a"),
            {"a": high},
        ).first() == ("HIGH", 1)
    # idempotent re-request returns the same grant
    assert _request(engine, "s-task") == task_apr


# ---------------------------------------------------------------- V-P1-32 eligibility / quorum
def test_approver_eligibility_and_quorum(engine: Engine) -> None:
    apr = _request(engine, "e-1", implementing_agent_account_uuid=str(IDS["acct-ap-impl"]))
    for who, code in (
        ("acct-ap-req", "SELF_APPROVAL_FORBIDDEN"),
        ("acct-ap-impl", "SELF_APPROVAL_FORBIDDEN"),
        ("acct-ap-alias", "SELF_APPROVAL_FORBIDDEN"),
        ("acct-ap-none", "APPROVER_NOT_ELIGIBLE"),
        ("acct-ap-low", "APPROVER_ROLE_RISK_TOO_LOW"),
    ):
        with pytest.raises(bus.CommandError) as exc:
            _decide(engine, apr, who, f"e-1-{who}")
        assert exc.value.code == code, who
    assert _status(engine, apr) == "PENDING" and _events(engine, apr) == ["APPROVAL_REQUESTED"]
    with engine.connect() as c:
        denied = c.execute(
            text("SELECT error_code FROM audit_events WHERE target_id = :a AND result = 'DENY'"),
            {"a": apr},
        ).all()
    assert {r[0] for r in denied} >= {"SELF_APPROVAL_FORBIDDEN", "APPROVER_ROLE_RISK_TOO_LOW"}
    # MEDIUM: one approval (an Agent may decide MEDIUM) reaches quorum 1
    r = _decide(engine, apr, "acct-ap-agent", "e-1-agent")
    assert r.data["status"] == "APPROVED" and _events(engine, apr)[-1] == "APPROVAL_GRANTED"
    with pytest.raises(bus.CommandError) as exc2:
        _decide(engine, apr, "acct-ap-h1", "e-1-late")
    assert exc2.value.code == "APPROVAL_NOT_PENDING"
    # HIGH: zero Agent approvals; Human needs re-authentication
    high = _request(engine, "e-2", risk="HIGH")
    with pytest.raises(bus.CommandError) as exc3:
        _decide(engine, high, "acct-ap-agent", "e-2-agent", reauth_verified=True)
    assert exc3.value.code == "HUMAN_APPROVER_REQUIRED"
    with pytest.raises(bus.CommandError) as exc4:
        _decide(engine, high, "acct-ap-h1", "e-2-noreauth")
    assert exc4.value.code == "REAUTH_REQUIRED"
    assert (
        _decide(engine, high, "acct-ap-h1", "e-2-h1", reauth_verified=True).data["status"]
        == "APPROVED"
    )
    # CRITICAL: two different Humans; the same Human twice is rejected
    crit = _request(engine, "e-3", risk="CRITICAL")
    first = _decide(engine, crit, "acct-ap-h1", "e-3-h1", reauth_verified=True)
    assert first.data == {"status": "PENDING", "approvals_recorded": 1, "quorum_required": 2}
    assert _status(engine, crit) == "PENDING"
    with pytest.raises(bus.CommandError) as exc5:
        _decide(engine, crit, "acct-ap-h1", "e-3-h1-again", reauth_verified=True)
    assert exc5.value.code == "APPROVER_DUPLICATE"
    second = _decide(engine, crit, "acct-ap-h2", "e-3-h2", reauth_verified=True)
    assert second.data["status"] == "APPROVED" and second.data["approvals_recorded"] == 2
    with engine.connect() as c:
        deciders = c.execute(
            text("SELECT decided_by FROM approval_decisions WHERE approval_id = :a"), {"a": crit}
        ).all()
        agent_approvals_high = c.execute(
            text(
                "SELECT count(*) FROM approval_decisions d JOIN approval_grants g ON g.approval_id = d.approval_id "  # noqa: E501
                "JOIN accounts a ON a.id = d.decided_by WHERE g.risk IN ('HIGH','CRITICAL') AND a.account_type <> 'human'"  # noqa: E501
            )
        ).scalar_one()
    assert {uuid.UUID(str(r[0])) for r in deciders} == {IDS["acct-ap-h1"], IDS["acct-ap-h2"]}
    assert agent_approvals_high == 0
    # rejection
    rej = _request(engine, "e-4")
    assert _decide(engine, rej, "acct-ap-h3", "e-4-rej", "REJECT").data["status"] == "REJECTED"
    assert _events(engine, rej)[-1] == "APPROVAL_REJECTED"


# ---------------------------------------------------------------- V-P1-16 bounded consume
def _consume(
    engine: Engine,
    apr: str,
    key: str,
    who: str = "acct-ap-svc",
    subject: tuple[str, str] = ("task", "task-ap-1"),
    clock: FixedClock | None = None,
) -> bus.CommandResult:
    with Session(engine) as s, s.begin():
        return bus.execute(
            ConsumeApproval(apr, key, subject[0], subject[1]),
            _ctx(s, who, f"c-{apr}-{key}", clock=clock),
        )


def test_bounded_consume_concurrency_expiry_revocation(engine: Engine) -> None:
    apr = _request(engine, "b-1", max_uses=3)
    with pytest.raises(bus.CommandError) as pending:
        _consume(engine, apr, "k0")
    assert pending.value.code == "APPROVAL_NOT_USABLE"
    _decide(engine, apr, "acct-ap-h1", "b-1-h1")
    results: list[Any] = []
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(_consume(engine, apr, f"use-{i}").data["used_count"])
        except bus.CommandError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r for r in results if isinstance(r, int)) == [1, 2, 3]
    assert results.count("APPROVAL_EXHAUSTED") == 7
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
                {"a": apr},
            ).scalar_one()
            == 3
        )
    assert _status(engine, apr) == "CONSUMED"
    assert _events(engine, apr).count("APPROVAL_CONSUMED") == 3
    # idempotent retry of an already recorded consumption returns the same result without a new row
    with engine.connect() as c:
        winner_key = str(
            c.execute(
                text(
                    "SELECT consumption_key FROM approval_consumptions WHERE approval_id = :a "
                    "ORDER BY id LIMIT 1"
                ),
                {"a": apr},
            ).scalar_one()
        )
    replay = _consume(engine, apr, winner_key)
    assert replay.replayed or replay.data["used_count"] == 3
    # scope mismatch: another Task / action cannot reuse the grant
    apr2 = _request(engine, "b-2", max_uses=2)
    _decide(engine, apr2, "acct-ap-h1", "b-2-h1")
    with pytest.raises(bus.CommandError) as scope:
        _consume(engine, apr2, "other", subject=("task", "task-other"))
    assert scope.value.code == "APPROVAL_SCOPE_MISMATCH"
    assert _consume(engine, apr2, "ok").data["status"] == "PARTIALLY_CONSUMED"
    # expired: consume after expiry and the expiry job
    late = FixedClock(NOW + dt.timedelta(hours=25))
    with pytest.raises(bus.CommandError) as exp:
        _consume(engine, apr2, "late", clock=late)
    assert exp.value.code == "APPROVAL_NOT_USABLE"
    with Session(engine) as s, s.begin():
        r = bus.execute(ExpireApprovals(), _ctx(s, "acct-ap-admin", "expire-1", clock=late))
    assert apr2 in r.data["expired"] and r.data["escalated_to"] == "role-administrator"
    assert _status(engine, apr2) == "EXPIRED" and _events(engine, apr2)[-2:] == [
        "APPROVAL_EXPIRED",
        "APPROVAL_ESCALATED",
    ]
    # revoked
    apr3 = _request(engine, "b-3", max_uses=5)
    _decide(engine, apr3, "acct-ap-h2", "b-3-h2")
    _consume(engine, apr3, "one")
    with Session(engine) as s, s.begin():
        bus.execute(RevokeApproval(apr3, "SECURITY"), _ctx(s, "acct-ap-admin", "revoke-3"))
    with pytest.raises(bus.CommandError) as rev:
        _consume(engine, apr3, "two")
    assert rev.value.code == "APPROVAL_NOT_USABLE"
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
                {"a": apr3},
            ).scalar_one()
            == 1
        )
    # cancel by a non-requester, non-admin is forbidden; the requester may cancel
    apr4 = _request(engine, "b-4")
    with Session(engine) as s, s.begin():
        with pytest.raises(bus.CommandError) as forb:
            bus.execute(CancelApproval(apr4), _ctx(s, "acct-ap-h1", "cancel-4-bad"))
        assert forb.value.code == "APPROVAL_CANCEL_FORBIDDEN"
    with Session(engine) as s, s.begin():
        bus.execute(CancelApproval(apr4), _ctx(s, "acct-ap-req", "cancel-4"))
    assert _status(engine, apr4) == "CANCELLED"
    with pytest.raises(bus.CommandError) as term:
        with Session(engine) as s, s.begin():
            bus.execute(RevokeApproval(apr4), _ctx(s, "acct-ap-admin", "revoke-4"))
    assert term.value.code == "APPROVAL_TERMINAL"


# ---------------------------------------------------------------- V-P1-22 ledger authority over projection  # noqa: E501
def test_consume_ignores_stale_or_deleted_projection(engine: Engine) -> None:
    apr = _request(engine, "p-1", max_uses=2)
    _decide(engine, apr, "acct-ap-h1", "p-1-h1")
    with engine.begin() as c:  # corrupt / delete the display projection
        c.execute(
            text(
                "UPDATE approvals_projection SET used_count = 0, status = 'APPROVED', max_uses = 99 WHERE approval_id = :a"  # noqa: E501
            ),
            {"a": apr},
        )
        c.execute(text("DELETE FROM approvals_projection WHERE approval_id = :a"), {"a": apr})
    results: list[Any] = []
    barrier = threading.Barrier(6)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(_consume(engine, apr, f"p-{i}").data["used_count"])
        except bus.CommandError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (
        sorted(r for r in results if isinstance(r, int)) == [1, 2]
        and results.count("APPROVAL_EXHAUSTED") == 4
    )
    rows_sql = (
        "SELECT approval_id, status, used_count, max_uses, decided_by, last_event_id "
        "FROM approvals_projection WHERE workspace_id = :ws ORDER BY approval_id"
    )
    with Session(engine) as s, s.begin():
        before = snapshot_hash(s, str(WS))
        rows_before = [tuple(r) for r in s.execute(text(rows_sql), {"ws": WS}).all()]
        rebuild_approvals(s, str(WS))
        after = snapshot_hash(s, str(WS))
        rows_after = [tuple(r) for r in s.execute(text(rows_sql), {"ws": WS}).all()]
        row = s.execute(
            text("SELECT used_count, status FROM approvals_projection WHERE approval_id = :a"),
            {"a": apr},
        ).first()
        ledger = s.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"), {"a": apr}
        ).scalar_one()
    assert rows_before == rows_after
    assert row == (2, "CONSUMED") and ledger == 2 and before == after


def test_service_consume_signature_for_execution_paths(engine: Engine) -> None:
    """The scheduler/tasks call the service function directly inside their own transaction."""
    apr = _request(engine, "svc-1", max_uses=1)
    _decide(engine, apr, "acct-ap-h1", "svc-1-h1")
    with Session(engine) as s, s.begin():
        r = service.consume_approval(
            s,
            PostgresEventStore(s, clock=FixedClock(NOW)),
            FixedClock(NOW),
            approval_id=apr,
            consumption_key="run-1",
            consumed_by=IDS["acct-ap-svc"],
            consumed_for=Subject("task", "task-ap-1"),
            correlation_id="corr-svc",
        )
        assert r.used_count == 1 and r.status is ApprovalStatus.CONSUMED
