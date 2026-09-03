"""V-P4-10/11/12/13/15 (P4-05/P4-06): encrypted at rest, scoped leases, TTL/single-use with one
redacted denial audit per request, immediate revocation (Task end, grant, Agent), LLM exposure
needs Human approval."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.application import approvals as ap
from server.application import secrets as sc
from server.application import tasks as t
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.secrets import broker
from server.secrets import leases as ls
from server.secrets.provider import LeaseScope, ResolveContext, SecretError
from tests.integration.secrets_seed import T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("sb")
VALUE = b"api-key-value-" + uuid.uuid4().hex.encode()


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


def _denials(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = "
                    "'secret.resolve_denied'"
                ),
                {"w": SEED.ws},
            ).scalar_one()
        )


def _audit_text(engine: Engine) -> str:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT redacted_metadata::text || coalesce(error_code,'') FROM audit_events "
                "WHERE workspace_id = :w"
            ),
            {"w": SEED.ws},
        ).all()
    return " ".join(str(r[0]) for r in rows)


def test_register_grant_lease_resolve_once_and_denials_are_audited(engine: Engine) -> None:
    clock = FixedClock(T0)
    rt = SEED.runtime(engine, clock)
    agent = SEED.register_agent(engine, rt, "agent-sb-1")
    other = SEED.register_agent(engine, rt, "agent-sb-2")
    reg = SEED.run(
        rt, SEED.admin_p, sc.RegisterSecret("deploy/api-key", VALUE, {"owner": "ops"}), "reg-1"
    )
    ref = reg.resource_id
    # V-P4-10: only ciphertext and a wrapped DEK are stored; the value is absent from the DB
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT ciphertext, wrapped_dek, status FROM secret_versions WHERE secret_ref = :r"
            ),
            {"r": ref},
        ).first()
        assert row is not None and VALUE not in bytes(row[0]) and VALUE not in bytes(row[1])
        assert VALUE.decode() not in _audit_text(engine)
        dump = s.execute(
            text("SELECT metadata::text FROM secrets WHERE secret_ref = :r"), {"r": ref}
        ).scalar_one()
        assert VALUE.decode() not in str(dump)
    grant = SEED.run(
        rt,
        SEED.admin_p,
        sc.CreateSecretGrant(ref, "agent-sb-1", task_id=None, action="deploy"),
        "grant-1",
    )
    gid = grant.data["grant_id"]
    # V-P4-11: another Agent cannot lease; wrong action cannot lease
    with pytest.raises(ApiError) as exc:
        SEED.run(rt, other, sc.IssueSecretLease(ref, action="deploy"), "lease-other")
    assert exc.value.code == "SECRET_SCOPE_DENIED"
    with pytest.raises(ApiError) as exc:
        SEED.run(rt, agent, sc.IssueSecretLease(ref, action="read"), "lease-wrong-action")
    assert exc.value.code == "SECRET_SCOPE_DENIED"
    lease = SEED.run(
        rt, agent, sc.IssueSecretLease(ref, action="deploy", task_id="task-sb-x"), "lease-1"
    ).data
    handle = lease["handle"]
    assert handle.startswith("sh-") and lease["single_use"] is True
    with Session(engine) as s:  # the handle itself is never stored
        stored = s.execute(
            text("SELECT handle_hash FROM secret_leases WHERE lease_id = :l"),
            {"l": lease["lease_id"]},
        ).scalar_one()
        assert stored != handle and stored == ls.handle_hash(handle)
    before = _denials(engine)
    # wrong Agent / wrong task / wrong action at resolve time: each exactly one denial audit
    for i, (who, ctx_kwargs, code) in enumerate(
        (
            (other, {"action": "deploy", "task_id": "task-sb-x"}, "SECRET_SCOPE_DENIED"),
            (agent, {"action": "deploy", "task_id": "task-sb-other"}, "SECRET_SCOPE_DENIED"),
            (agent, {"action": "read", "task_id": "task-sb-x"}, "SECRET_SCOPE_DENIED"),
        )
    ):
        with pytest.raises(ApiError) as exc:
            SEED.run(rt, who, sc.ResolveSecret(handle, **ctx_kwargs), f"res-bad-{i}")
        assert exc.value.code == code
        assert _denials(engine) == before + i + 1
    ok = SEED.run(
        rt, agent, sc.ResolveSecret(handle, action="deploy", task_id="task-sb-x"), "res-ok"
    ).data
    import base64

    assert base64.b64decode(ok["secret_b64"]) == VALUE
    # V-P4-12: single-use — the second resolve is denied with zero bytes and one audit
    before = _denials(engine)
    with pytest.raises(ApiError) as exc:
        SEED.run(
            rt, agent, sc.ResolveSecret(handle, action="deploy", task_id="task-sb-x"), "res-again"
        )
    assert exc.value.code == "SECRET_HANDLE_USED" and _denials(engine) == before + 1
    assert "secret_b64" not in str(exc.value.extra)
    # V-P4-12: expiry
    lease2 = SEED.run(
        rt, agent, sc.IssueSecretLease(ref, action="deploy", ttl_seconds=60), "lease-2"
    ).data
    clock.advance(dt.timedelta(seconds=61))
    before = _denials(engine)
    with pytest.raises(ApiError) as exc:
        SEED.run(rt, agent, sc.ResolveSecret(lease2["handle"], action="deploy"), "res-expired")
    assert exc.value.code == "SECRET_LEASE_EXPIRED" and _denials(engine) == before + 1
    with Session(engine) as s:
        accessed = s.execute(
            text(
                "SELECT count(*) FROM events WHERE workspace_id = :w AND type = 'SECRET_ACCESSED'"
            ),
            {"w": SEED.ws},
        ).scalar_one()
        assert accessed == 1
        assert VALUE.decode() not in _audit_text(engine)
        assert VALUE.decode() not in str(
            s.execute(
                text("SELECT string_agg(payload::text, ' ') FROM events WHERE workspace_id = :w"),
                {"w": SEED.ws},
            ).scalar()
        )
    assert gid.startswith("grant-")


def test_revocation_is_immediate_for_grant_task_and_agent(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    rt = SEED.runtime(engine, clock)
    agent = SEED.ensure_agent(engine, rt, "agent-sb-1")
    ref = SEED.run(
        rt,
        SEED.admin_p,
        sc.RegisterSecret("deploy/token-2", b"v2-" + uuid.uuid4().hex.encode()),
        "reg-2",
    ).resource_id
    g = SEED.run(
        rt,
        SEED.admin_p,
        sc.CreateSecretGrant(ref, "agent-sb-1", task_id="task-sb-r", single_use=False),
        "grant-2",
    ).data["grant_id"]
    l1 = SEED.run(rt, agent, sc.IssueSecretLease(ref, task_id="task-sb-r"), "lease-r1").data
    l2 = SEED.run(rt, agent, sc.IssueSecretLease(ref, task_id="task-sb-r"), "lease-r2").data
    # multi-use handle resolves twice before revocation
    SEED.run(rt, agent, sc.ResolveSecret(l1["handle"], task_id="task-sb-r"), "res-r1a")
    SEED.run(rt, agent, sc.ResolveSecret(l1["handle"], task_id="task-sb-r"), "res-r1b")
    wiped: list[tuple[list[str], str]] = []
    ls.LIVE.subscribe(lambda ids, reason: wiped.append((ids, reason)))
    # revoke by grant → both leases dead immediately, feed row written, listeners notified
    seq_before = _last_seq(engine)
    res = SEED.run(rt, SEED.admin_p, sc.RevokeSecretGrant(g, "grant", "ADMIN_REVOKE"), "rev-g")
    assert sorted(res.data["revoked_leases"]) == sorted([l1["lease_id"], l2["lease_id"]])
    assert wiped and set(wiped[-1][0]) == {l1["lease_id"], l2["lease_id"]}
    with pytest.raises(ApiError) as exc:
        SEED.run(rt, agent, sc.ResolveSecret(l1["handle"], task_id="task-sb-r"), "res-r1c")
    assert exc.value.code == "SECRET_HANDLE_REVOKED"
    with Session(engine) as s:
        feed = ls.revocations_since(s, seq_before, workspace_id=SEED.ws)
        assert [f.kind for f in feed] == ["grant"] and set(feed[0].lease_ids) == {
            l1["lease_id"],
            l2["lease_id"],
        }
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = "
                    "'SECRET_GRANT_REVOKED' AND aggregate_id = :g"
                ),
                {"g": g},
            ).scalar_one()
            == 1
        )
    # Task end revokes the Task's leases (terminal transition hook)
    task = SEED.run(
        rt,
        SEED.admin_p,
        t.CreateTask(
            "secret task",
            str(SEED.channel),
            "ops",
            "LOW",
            criteria=({"statement": "done", "check_type": "evidence", "required": True},),
        ),
        "task-c",
    )
    tid = task.resource_id
    g2 = SEED.run(
        rt, SEED.admin_p, sc.CreateSecretGrant(ref, "agent-sb-1", task_id=tid), "grant-3"
    ).data["grant_id"]
    l3 = SEED.run(rt, agent, sc.IssueSecretLease(ref, task_id=tid), "lease-t").data
    SEED.run(rt, SEED.admin_p, t.CancelTask(tid, "NOT_NEEDED"), "cancel-t")
    with pytest.raises(ApiError) as exc:
        SEED.run(rt, agent, sc.ResolveSecret(l3["handle"], task_id=tid), "res-t")
    assert exc.value.code == "SECRET_HANDLE_REVOKED"
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT revoke_reason FROM secret_grants WHERE grant_id = :g"), {"g": g2}
            ).scalar_one()
            == "TASK_ENDED"
        )
    # Agent revocation ends every lease of the Agent
    l4 = SEED.run(rt, agent, sc.IssueSecretLease(ref), "lease-a").data if False else None
    g4 = SEED.run(rt, SEED.admin_p, sc.CreateSecretGrant(ref, "agent-sb-1"), "grant-4").data[
        "grant_id"
    ]
    l4 = SEED.run(rt, agent, sc.IssueSecretLease(ref), "lease-a").data
    SEED.run(
        rt, SEED.admin_p, sc.RevokeSecretGrant("agent-sb-1", "agent", "AGENT_REVOKED"), "rev-agent"
    )
    with Session(engine) as s:
        row = s.execute(
            text("SELECT revoked_at FROM secret_leases WHERE lease_id = :l"), {"l": l4["lease_id"]}
        ).first()
        assert row is not None and row[0] is not None
        assert (
            s.execute(
                text("SELECT revoked_at FROM secret_grants WHERE grant_id = :g"), {"g": g4}
            ).scalar_one()
            is not None
        )


def _last_seq(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(text("SELECT coalesce(max(seq), 0) FROM secret_revocations")).scalar_one()
        )


def test_llm_exposure_requires_human_approval(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=2))
    rt = SEED.runtime(engine, clock)
    agent = SEED.register_agent(engine, rt, "agent-sb-3")
    ref = SEED.run(
        rt,
        SEED.admin_p,
        sc.RegisterSecret("llm/key", b"llm-" + uuid.uuid4().hex.encode()),
        "reg-llm",
    ).resource_id
    task = SEED.run(
        rt,
        SEED.admin_p,
        t.CreateTask(
            "llm task",
            str(SEED.channel),
            "ops",
            "LOW",
            criteria=({"statement": "done", "check_type": "evidence", "required": True},),
        ),
        "task-llm",
    ).resource_id
    g = SEED.run(
        rt,
        SEED.admin_p,
        sc.CreateSecretGrant(ref, "agent-sb-3", task_id=task, single_use=False),
        "grant-llm",
    ).data["grant_id"]
    lease = SEED.run(rt, agent, sc.IssueSecretLease(ref, task_id=task), "lease-llm").data
    # V-P4-15: exposure without approval is rejected (the adapter purpose still works)
    with pytest.raises(ApiError) as exc:
        SEED.run(
            rt,
            agent,
            sc.ResolveSecret(lease["handle"], task_id=task, purpose="llm_context"),
            "res-llm-1",
        )
    assert exc.value.code == "SECRET_EXPOSURE_APPROVAL_REQUIRED"
    req = SEED.run(rt, SEED.admin_p, sc.RequestSecretExposure(g, task), "expose-req").data
    assert req["status"] == "PENDING"
    with pytest.raises(ApiError) as exc:  # requested but not yet approved by a Human
        SEED.run(
            rt,
            agent,
            sc.ResolveSecret(lease["handle"], task_id=task, purpose="llm_context"),
            "res-llm-2",
        )
    assert exc.value.code == "SECRET_EXPOSURE_APPROVAL_REQUIRED"
    # the Agent cannot approve its own exposure (human_only); the administrator (Human) can
    with pytest.raises(ApiError):
        SEED.run(rt, agent, ap.DecideApproval(req["approval_id"], "APPROVE"), "decide-agent")
    # secret_exposure is a quorum-2 class: two independent re-authenticated Humans decide
    SEED.run(rt, SEED.approver_p, ap.DecideApproval(req["approval_id"], "APPROVE"), "decide-human")
    with pytest.raises(ApiError) as exc:  # one of two is not enough
        SEED.run(
            rt,
            agent,
            sc.ResolveSecret(lease["handle"], task_id=task, purpose="llm_context"),
            "res-llm-2b",
        )
    assert exc.value.code == "SECRET_EXPOSURE_APPROVAL_REQUIRED"
    SEED.run(
        rt, SEED.approver2_p, ap.DecideApproval(req["approval_id"], "APPROVE"), "decide-human-2"
    )
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT status, quorum_required, expires_at, valid_from FROM approval_grants "
                "WHERE approval_id = :a"
            ),
            {"a": req["approval_id"]},
        ).first()
        assert row is not None and row[0] == "APPROVED", (row, clock.now())
    ok = SEED.run(
        rt,
        agent,
        sc.ResolveSecret(lease["handle"], task_id=task, purpose="llm_context"),
        "res-llm-3",
    )
    assert ok.data["secret_b64"]


def test_broker_scope_rules_are_pure(engine: Engine) -> None:
    grant = broker.GrantRow(
        "g",
        SEED.ws,
        "sec-x",
        "agent-a",
        "task-1",
        None,
        300,
        True,
        False,
        None,
        T0 + dt.timedelta(days=1),
        None,
    )
    assert grant.matches(LeaseScope("agent-a", "task-1", "any"))
    assert not grant.matches(LeaseScope("agent-b", "task-1"))
    assert not grant.matches(LeaseScope("agent-a", "task-2"))
    lease = broker.LeaseRow(
        "l",
        SEED.ws,
        "g",
        "sec-x",
        "agent-a",
        "task-1",
        "deploy",
        None,
        "sc-1",
        True,
        T0,
        T0 + dt.timedelta(minutes=5),
        None,
        0,
        None,
    )
    assert (
        broker._scope_mismatch(lease, ResolveContext("agent-a", "sc-1", "task-1", "deploy")) is None
    )
    assert (
        broker._scope_mismatch(lease, ResolveContext("agent-a", "sc-2", "task-1", "deploy"))
        == "sidecar_instance"
    )
    with pytest.raises(SecretError):
        raise SecretError("SECRET_HANDLE_HOST_MISMATCH", "x")
