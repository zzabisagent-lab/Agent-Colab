"""Verifier assignment engine (development plan §7D.2; P3-13).

eligible = ``verification.submit`` permission ∧ Task-domain capability ∧ independent of the
implementer (Account, alias, credential; ``server.verification.independence``) ∧ (Agents) active,
online with capacity ∧ Human requirement (risk ``HIGH``/``CRITICAL`` or channel policy
``risk_policy.requires_human_approval`` containing ``verification``).
score = domain match (2) + inverse recent load (1) + Human preference (1 when required);
ties break by ascending ``account_id``. The chosen Verifier receives a ``verification_assignment``
work item (Agents: durable inbox; Humans: notification rule ``VERIFIER_ASSIGNED`` → DM/work item)
carrying criteria, evidence manifest, Artifact refs, target commit/digest and read-only access
information. Not accepted within 10 minutes → the next candidate (a new VerificationRun with its
own identity snapshot; the offer is recorded in ``verifier_assignments``); none left →
``VERIFIER_ASSIGNMENT_EXHAUSTED`` (Administrator notification) and the Task goes ``WAITING``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import routing
from server.application import bus
from server.domain import defaults
from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore, EventStoreError
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
)
from server.verification.runs import VerificationRun, alias_graph, load_run

HUMAN_REQUIRED_RISKS = frozenset({"HIGH", "CRITICAL"})
ACCEPT_TIMEOUT = dt.timedelta(minutes=defaults.VERIFIER_ACCEPT_TIMEOUT_MIN)
WORK_DEADLINE = dt.timedelta(hours=24)


@dataclass(frozen=True)
class VerifierCandidate:
    account_uuid: str
    account_id: str
    account_type: str
    agent_id: str | None
    credential_fingerprint: str
    score: int
    domain_match: bool
    load: int
    human: bool

    def snapshot(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "score": self.score,
            "domain_match": self.domain_match,
            "load": self.load,
        }


def human_required(session: Session, workspace_id: str, channel_id: str | None, risk: str) -> bool:
    if risk in HUMAN_REQUIRED_RISKS:
        return True
    if not channel_id:
        return False
    row = session.execute(
        text(
            "SELECT t.definition FROM channels c JOIN channel_templates t "
            "ON t.workspace_id = c.workspace_id AND t.template_id = c.template_id "
            "WHERE c.workspace_id = :ws AND (c.channel_id = :cid OR c.id::text = :cid)"
        ),
        {"ws": uuid.UUID(workspace_id), "cid": channel_id},
    ).first()
    if row is None:
        return False
    definition = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    actions = definition.get("risk_policy", {}).get("requires_human_approval", [])
    return "verification" in actions


def _fingerprint(session: Session, account_uuid: str) -> str | None:
    row = session.execute(
        text(
            "SELECT fingerprint FROM service_credentials WHERE account_id = :a "
            "AND status = 'active' ORDER BY fingerprint LIMIT 1"
        ),
        {"a": uuid.UUID(account_uuid)},
    ).first()
    return str(row[0]) if row else None


def eligible_verifiers(
    session: Session,
    *,
    workspace_id: str,
    domain: str | None,
    risk: str,
    channel_id: str | None,
    implementer: Identity,
    authorizer: Any,
    exclude_accounts: Iterable[str] = (),
    correlation_id: str = "-",
) -> list[VerifierCandidate]:
    """Eligible Verifiers ordered by score desc, account_id asc (deterministic)."""
    excluded = {str(a) for a in exclude_accounts}
    need_human = human_required(session, workspace_id, channel_id, risk)
    graph = alias_graph(session, workspace_id)
    channel_uuid = _channel_uuid(session, workspace_id, channel_id)
    rows = (
        session.execute(
            text(
                "SELECT ac.id, ac.account_id, ac.account_type, ag.agent_id, "
                "ag.status AS agent_status, "
                "ag.online, ag.capacity, "
                "EXISTS (SELECT 1 FROM agent_capabilities c JOIN capabilities cp "
                "  ON cp.capability_id = c.capability_id WHERE c.agent_id = ag.agent_id "
                "  AND cp.domain = :dom) AS domain_match "
                "FROM accounts ac LEFT JOIN agents ag ON ag.account_id = ac.id "
                "WHERE ac.workspace_id = :ws AND ac.account_type IN ('human','agent') "
                "ORDER BY ac.account_id"
            ),
            {"ws": uuid.UUID(workspace_id), "dom": domain},
        )
        .mappings()
        .all()
    )
    out: list[VerifierCandidate] = []
    for r in rows:
        account_uuid, account_id = str(r["id"]), str(r["account_id"])
        if account_uuid in excluded or account_id in excluded:
            continue
        is_agent = r["account_type"] == "agent"
        if need_human and is_agent:
            continue
        if is_agent:
            if r["agent_id"] is None or r["agent_status"] != "active" or not r["online"]:
                continue
            if not r["domain_match"]:
                continue  # Task-domain capability is mandatory for Agent Verifiers
        fingerprint = _fingerprint(session, account_uuid) or f"none:{account_id}"
        try:
            check_independence(
                implementer,
                Identity(account_uuid, fingerprint, r["agent_id"]),
                alias_graph=graph,
            )
        except VerificationIndependenceError:
            continue
        load = routing.load_of(session, account_uuid)
        if is_agent and int(r["capacity"] or 0) - load <= 0:
            continue
        if not routing._policy_allows(
            authorizer,
            session,
            account_id,
            permission="verification.submit",
            channel_id=channel_uuid,
            domain=domain,
            required_capability=None,
            correlation_id=correlation_id,
        ):
            continue
        domain_match = (
            bool(r["domain_match"])
            if is_agent
            else _human_domain_match(
                authorizer, session, account_id, domain, channel_uuid, correlation_id
            )
        )
        score = (2 if domain_match else 0) + (1 if load == 0 else 0) + (1 if need_human else 0)
        out.append(
            VerifierCandidate(
                account_uuid,
                account_id,
                str(r["account_type"]),
                r["agent_id"],
                fingerprint,
                score,
                domain_match,
                load,
                not is_agent,
            )
        )
    out.sort(key=lambda c: (-c.score, c.account_id))
    return out


def _human_domain_match(
    authorizer: Any,
    session: Session,
    account_id: str,
    domain: str | None,
    channel_uuid: str | None,
    correlation_id: str,
) -> bool:
    """A Human matches the domain when a Role scoped to that domain grants verification."""
    if domain is None:
        return False
    engine = getattr(authorizer, "authorizer", None)
    repo = getattr(engine, "repository", None)
    if repo is None or not hasattr(repo, "principal"):
        return False
    principal = repo.principal(session, account_id)
    if principal is None:
        return False
    clock = getattr(engine, "clock", None)
    now = clock.now() if clock is not None else dt.datetime.now(dt.UTC)
    for role in repo.effective_roles(session, principal, now):
        domains = getattr(role.constraints, "domains", None)
        if domains and domain in domains and "verification.submit" in role.permissions:
            return True
    return False


def _channel_uuid(session: Session, workspace_id: str, channel_id: str | None) -> str | None:
    if not channel_id:
        return None
    row = session.execute(
        text(
            "SELECT id FROM channels WHERE workspace_id = :ws AND "
            "(channel_id = :c OR id::text = :c)"
        ),
        {"ws": uuid.UUID(workspace_id), "c": channel_id},
    ).first()
    return str(row[0]) if row else None


# ---------------------------------------------------------------- offers


def offered_accounts(session: Session, task_id: str) -> list[str]:
    rows = session.execute(
        text("SELECT account_id FROM verifier_assignments WHERE task_id = :t"), {"t": task_id}
    ).all()
    return [str(r[0]) for r in rows]


def next_rank(session: Session, task_id: str) -> int:
    return int(
        session.execute(
            text(
                "SELECT COALESCE(max(candidate_rank), 0) + 1 FROM "
                "verifier_assignments WHERE task_id = :t"
            ),
            {"t": task_id},
        ).scalar_one()
    )


def resolve_auto_assign(cmd: Any, ctx: bus.CommandContext) -> Any:
    """Fill the verifier identity of a ``CreateVerificationRun(auto_assign=True)`` command."""
    from server.application.tasks import load_task

    if not cmd.task_id:
        raise bus.CommandError("VERIFICATION_TARGET_INVALID", "auto_assign needs task_id", 400)
    task = load_task(ctx, cmd.task_id)
    impl_row = ctx.session.execute(
        text("SELECT id FROM accounts WHERE account_id = :a AND workspace_id = :w"),
        {"a": cmd.implementer_account_id, "w": uuid.UUID(ctx.workspace_id)},
    ).first()
    if impl_row is None:
        raise bus.CommandError("ACCOUNT_NOT_FOUND", cmd.implementer_account_id, status=404)
    implementer = Identity(
        str(impl_row[0]), cmd.implementer_credential_fingerprint, cmd.implementer_agent_id
    )
    found = eligible_verifiers(
        ctx.session,
        workspace_id=ctx.workspace_id,
        domain=task.domain or None,
        risk=task.risk,
        channel_id=task.channel_id,
        implementer=implementer,
        authorizer=ctx.authorizer,
        exclude_accounts=offered_accounts(ctx.session, cmd.task_id),
        correlation_id=ctx.correlation_id,
    )
    chosen = found[0] if found else None
    routing.record_decision(
        ctx.session,
        workspace_id=ctx.workspace_id,
        purpose="verification",
        candidate_set=[],
        selected=None,
        reason_code="SELECTED" if chosen else "NO_CANDIDATE",
        correlation_id=ctx.correlation_id,
        actor_label=ctx.principal.account_id,
        task_id=cmd.task_id,
        domain=task.domain or None,
        actor_account_uuid=ctx.principal.account_uuid,
        clock=ctx.clock,
    )
    ctx.extras["verifier_candidates"] = [c.snapshot() for c in found]
    if chosen is None:
        raise bus.CommandError(
            "VERIFIER_NONE_ELIGIBLE",
            "no eligible Verifier",
            status=409,
            extra={"task_id": cmd.task_id},
        )
    ctx.extras["verifier_candidate"] = chosen
    return dataclasses.replace(
        cmd,
        verifier_account_id=chosen.account_id,
        verifier_credential_fingerprint=chosen.credential_fingerprint,
        verifier_agent_id=chosen.agent_id,
    )


def record_offer(
    ctx: bus.CommandContext, run: VerificationRun, candidate: VerifierCandidate
) -> str | None:
    """Persist the offer (10-minute acceptance window) and deliver the work item to Agents."""
    from server.application.criteria import current_criteria
    from server.work import inbox

    now = ctx.clock.now()
    task_id = run.task_id or run.target_id
    work_item_id: str | None = None
    payload = _assignment_payload(ctx.session, ctx.store, ctx.workspace_id, run, current_criteria)
    if candidate.agent_id is not None:
        item = inbox.enqueue(
            ctx.session,
            ctx.store,
            workspace_id=ctx.workspace_id,
            kind="verification_assignment",
            agent_id=candidate.agent_id,
            payload=payload,
            deadline=now + WORK_DEADLINE,
            expected_result_schema="colab.verification-verdict.v1",
            correlation_id=ctx.correlation_id,
            idempotency_key=f"verify:{run.verification_id}",
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
            task_id=run.task_id,
        )
        work_item_id = item.work_item_id
    ctx.session.execute(
        text(
            "INSERT INTO verifier_assignments (workspace_id, task_id, verification_id, "
            "candidate_rank, account_id, agent_id, score, work_item_id, offered_at, "
            "accept_deadline, status) VALUES (:ws, :t, :v, :r, :a, :g, :s, :w, :now, :dl, "
            "'offered') ON CONFLICT (task_id, candidate_rank) DO NOTHING"
        ),
        {
            "ws": uuid.UUID(ctx.workspace_id),
            "t": task_id,
            "v": run.verification_id,
            "r": next_rank(ctx.session, task_id),
            "a": uuid.UUID(candidate.account_uuid),
            "g": candidate.agent_id,
            "s": candidate.score,
            "w": work_item_id,
            "now": now,
            "dl": now + ACCEPT_TIMEOUT,
        },
    )
    return work_item_id


def _assignment_payload(
    session: Session,
    store: EventStore,
    workspace_id: str,
    run: VerificationRun,
    current_criteria: Any,
) -> dict[str, Any]:
    task_id = run.task_id or run.target_id
    criteria: list[dict[str, Any]] = []
    try:
        rev = current_criteria(session, task_id)
        criteria = [dataclasses.asdict(c) for c in rev.criteria]
    except Exception:
        criteria = []
    evidence: list[str] = []
    for ev in store.stream(workspace_id, "task", task_id):
        if ev["type"] == "IMPLEMENTATION_SUBMITTED":
            evidence = list(ev.get("payload", {}).get("evidence_refs", []))
    artifacts = [
        str(r[0])
        for r in session.execute(
            text(
                "SELECT a.artifact_id FROM artifacts a JOIN events e ON "
                "e.event_id = a.source_event_id "
                "WHERE e.task_id = :t ORDER BY a.artifact_id"
            ),
            {"t": task_id},
        ).all()
    ]
    return {
        "verification_id": run.verification_id,
        "task_id": task_id,
        "criteria": criteria,
        "evidence_manifest": evidence,
        "artifact_refs": artifacts,
        "target_commit": run.target_commit,
        "snapshot_hash": run.snapshot_hash,
        "read_only_access": {
            "task": f"colab://task/{task_id}",
            "artifacts": [f"colab://artifact/{a}" for a in artifacts],
            "events": f"/api/v1/events?task_id={task_id}",
        },
    }


# ---------------------------------------------------------------- timeouts


@dataclass(frozen=True)
class TimeoutOutcome:
    task_id: str
    verification_id: str
    code: str  # REASSIGNED | EXHAUSTED | ACCEPTED
    next_verification_id: str | None = None


def sweep_timeouts(
    session: Session,
    store: EventStore,
    *,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    authorizer: Any,
) -> list[TimeoutOutcome]:
    """Offers not accepted within 10 minutes → next candidate; none → EXHAUSTED + WAITING."""
    from server.application import verification as ver_app
    from server.application.tasks import MarkWaiting

    now = clock.now()
    rows = (
        session.execute(
            text(
                "SELECT id, task_id, verification_id, account_id, work_item_id FROM "
                "verifier_assignments WHERE workspace_id = :ws AND status = 'offered' "
                "AND accept_deadline <= :now ORDER BY id"
            ),
            {"ws": uuid.UUID(workspace_id), "now": now},
        )
        .mappings()
        .all()
    )
    outcomes: list[TimeoutOutcome] = []
    for r in rows:
        run = load_run(session, str(r["verification_id"]))
        if run.status.value in ("RUNNING", "PASSED", "FAILED", "BLOCKED", "FIX_SUBMITTED"):
            _set_offer(session, int(r["id"]), "accepted", now)
            outcomes.append(TimeoutOutcome(str(r["task_id"]), run.verification_id, "ACCEPTED"))
            continue
        _set_offer(session, int(r["id"]), "timed_out", now)
        if r["work_item_id"]:
            _cancel_item(session, store, str(r["work_item_id"]), actor, clock)
        ctx = _ctx(
            session,
            store,
            clock,
            workspace_id,
            actor,
            authorizer,
            f"vr-timeout:{run.verification_id}",
        )
        bus.execute(ver_app.CancelVerification(run.verification_id, "VERIFIER_ACCEPT_TIMEOUT"), ctx)
        task_id = str(r["task_id"])
        try:
            ctx2 = _ctx(
                session,
                store,
                clock,
                workspace_id,
                actor,
                authorizer,
                f"vr-next:{task_id}:{next_rank(session, task_id)}",
            )
            res = bus.execute(
                ver_app.CreateVerificationRun(
                    target_type=run.target_type,
                    target_id=run.target_id,
                    implementer_account_id=_public_id(session, run.implementer_account_id),
                    verifier_account_id="",
                    implementer_credential_fingerprint=run.implementer_credential_fingerprint,
                    verifier_credential_fingerprint="",
                    target_commit=run.target_commit,
                    effective_policy_hash=run.effective_policy_hash,
                    criteria_version=run.criteria_version,
                    identity_graph_version=run.identity_graph_version,
                    implementer_agent_id=run.implementer_agent_id,
                    phase=run.phase,
                    task_id=run.task_id,
                    auto_assign=True,
                ),
                ctx2,
            )
            outcomes.append(
                TimeoutOutcome(task_id, run.verification_id, "REASSIGNED", res.resource_id)
            )
        except bus.CommandError as exc:
            if exc.code != "VERIFIER_NONE_ELIGIBLE":
                raise
            _exhausted(session, store, clock, workspace_id, actor, run, task_id)
            _set_offer(session, int(r["id"]), "exhausted", now)  # the last offer ended the search
            _set_offer_status_by_task(session, task_id, "exhausted", now)
            ctx3 = _ctx(
                session, store, clock, workspace_id, actor, authorizer, f"vr-exhausted:{task_id}"
            )
            try:
                bus.execute(MarkWaiting(task_id, "NO_VERIFIER"), ctx3)
            except bus.CommandError as wait_exc:
                if wait_exc.code not in ("TASK_TRANSITION_INVALID", "TASK_TERMINAL"):
                    raise
            outcomes.append(TimeoutOutcome(task_id, run.verification_id, "EXHAUSTED"))
    return outcomes


def _exhausted(
    session: Session,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    run: VerificationRun,
    task_id: str,
) -> None:
    stream = store.stream(workspace_id, "verification_run", run.verification_id)
    try:
        store.append(
            AppendRequest(
                workspace_id=workspace_id,
                aggregate_type="verification_run",
                aggregate_id=run.verification_id,
                type="VERIFIER_ASSIGNMENT_EXHAUSTED",
                actor_account_id=actor.account_uuid,
                correlation_id=f"vr-exhausted:{task_id}",
                idempotency_scope="verification_run:exhausted",
                idempotency_key=f"exhausted:{run.verification_id}",
                payload={
                    "verification_id": run.verification_id,
                    "task_id": task_id,
                    "offers": len(offered_accounts(session, task_id)),
                },
                task_id=run.task_id,
                caused_by=stream[-1]["event_id"] if stream else None,
                expected_seq=(stream[-1]["aggregate_seq"] + 1) if stream else 1,
            )
        )
    except EventStoreError as exc:
        if exc.code != "IDEMPOTENCY_CONFLICT":
            raise


def _public_id(session: Session, account_uuid: str) -> str:
    row = session.execute(
        text("SELECT account_id FROM accounts WHERE id = :a"), {"a": uuid.UUID(account_uuid)}
    ).first()
    return str(row[0]) if row else account_uuid


def _set_offer(session: Session, offer_id: int, status: str, now: dt.datetime) -> None:
    session.execute(
        text("UPDATE verifier_assignments SET status = :s, resolved_at = :now WHERE id = :i"),
        {"s": status, "now": now, "i": offer_id},
    )


def _set_offer_status_by_task(
    session: Session, task_id: str, status: str, now: dt.datetime
) -> None:
    session.execute(
        text(
            "UPDATE verifier_assignments SET status = :s, resolved_at = :now WHERE task_id = :t "
            "AND status = 'offered'"
        ),
        {"s": status, "now": now, "t": task_id},
    )


def _cancel_item(
    session: Session, store: EventStore, work_item_id: str, actor: bus.Principal, clock: Clock
) -> None:
    from server.work import inbox
    from server.work.state import WorkItemError

    try:
        inbox.cancel(
            session,
            store,
            work_item_id,
            "VERIFIER_ACCEPT_TIMEOUT",
            actor_account_id=actor.account_uuid,
            clock=clock,
        )
    except WorkItemError:
        pass


def _ctx(
    session: Session,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    authorizer: Any,
    key: str,
) -> bus.CommandContext:
    return bus.CommandContext(
        session=session,
        store=store,
        authorizer=authorizer,
        clock=clock,
        principal=actor,
        workspace_id=workspace_id,
        correlation_id=key,
        idempotency_key=key,
    )
