"""Brainstorm session commands on the bus (P6-02, P6-09; development plan §7F, spec §8.3).

The session opener is the facilitator. The server distributes Agent turns round-robin as
``brainstorm_turn`` work items; Humans speak freely and their plain utterances are recorded as
``IDEA``. Any limit breach rejects the offending contribution *and* pauses the session for
facilitator guidance, which is what §7F prescribes and V-P6-26 checks. Summaries are drafted by a
non-participant Agent where possible and are never posted before the facilitator approves them;
Decisions are recorded by the facilitator alone and taskify creates one Task per action item with
mandatory acceptance criteria (§7D.1).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from server.application import bus
from server.application import tasks as tasks_app
from server.application.artifacts import RegisterArtifact
from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.brainstorm import decisions as dec
from server.brainstorm import engine as eng
from server.brainstorm import limits as lim
from server.brainstorm import summary as summ
from server.brainstorm import taskify as tsk
from server.brainstorm import turns as trn
from server.channels import members as mem
from server.events.store import AppendRequest
from server.observability.audit import append_audit
from server.work import inbox

GUIDANCE_KIND = "notification"


# ------------------------------------------------------------------ commands
@dataclass(frozen=True)
class StartBrainstorm(Command):
    channel_id: str
    topic: str
    participants: tuple[str, ...] = ()
    limits: dict[str, Any] = field(default_factory=dict)
    brainstorm_id: str | None = None
    idempotency_scope: str = "brainstorm:open"


@dataclass(frozen=True)
class JoinBrainstorm(Command):
    brainstorm_id: str
    account_id: str
    idempotency_scope: str = "brainstorm:join"


@dataclass(frozen=True)
class ContributeTurn(Command):
    brainstorm_id: str
    body: str
    contribution_type: str | None = None
    work_item_id: str | None = None
    idempotency_scope: str = "brainstorm:contribute"


@dataclass(frozen=True)
class PauseBrainstorm(Command):
    brainstorm_id: str
    reason_code: str = "FACILITATOR_PAUSE"
    idempotency_scope: str = "brainstorm:pause"


@dataclass(frozen=True)
class ResumeBrainstorm(Command):
    brainstorm_id: str
    limits: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "brainstorm:resume"


@dataclass(frozen=True)
class CloseBrainstorm(Command):
    brainstorm_id: str
    idempotency_scope: str = "brainstorm:close"


@dataclass(frozen=True)
class SummarizeBrainstorm(Command):
    brainstorm_id: str
    body: str | None = None
    idempotency_scope: str = "brainstorm:summarize"


@dataclass(frozen=True)
class ApproveSummary(Command):
    summary_id: str
    post: bool = True
    idempotency_scope: str = "brainstorm:summary_approve"


@dataclass(frozen=True)
class RecordDecision(Command):
    brainstorm_id: str
    statement: str
    rationale: str
    source_event_ids: tuple[str, ...] = ()
    action_items: tuple[dict[str, Any], ...] = ()
    vote: dict[str, Any] | None = None
    decision_id: str | None = None
    idempotency_scope: str = "decision:record"


@dataclass(frozen=True)
class TaskifyDecision(Command):
    decision_id: str
    domain: str = "general"
    risk: str = "LOW"
    idempotency_scope: str = "decision:taskify"


# ------------------------------------------------------------------ helpers
def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _wrap(exc: eng.EngineError | dec.DecisionError | summ.SummaryError) -> CommandError:
    return CommandError(exc.code, exc.detail, status=exc.status)


def _state(ctx: CommandContext, brainstorm_id: str) -> eng.BrainstormState:
    try:
        return eng.require(ctx.session, _ws(ctx), brainstorm_id)
    except eng.EngineError as exc:
        raise _wrap(exc) from exc


def _account(ctx: CommandContext, account_id: str) -> tuple[uuid.UUID, str]:
    try:
        return mem.account_ref(ctx.session, _ws(ctx), account_id)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


def _agent_id_of(ctx: CommandContext, account_uuid: uuid.UUID) -> str | None:
    row = ctx.session.execute(
        text("SELECT agent_id FROM agents WHERE account_id = :a"), {"a": account_uuid}
    ).first()
    return None if row is None else str(row[0])


def _facilitator_only(ctx: CommandContext, state: eng.BrainstormState) -> None:
    if str(state.facilitator_uuid) != ctx.principal.account_uuid:
        raise CommandError("BRAINSTORM_FACILITATOR_ONLY", state.brainstorm_id, status=403)


def _append(
    ctx: CommandContext,
    cmd: Command,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    channel_uuid: uuid.UUID | None = None,
) -> Any:
    stream = ctx.store.stream(ctx.workspace_id, aggregate_type, aggregate_id)
    return ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            expected_seq=len(stream) + 1,
            channel_id=None if channel_uuid is None else str(channel_uuid),
            payload=payload,
        )
    )


def _audit(ctx: CommandContext, action: str, target_id: str, **meta: Any) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type="brainstorm",
        target_id=target_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _derived(ctx: CommandContext, suffix: str) -> CommandContext:
    """A nested command context: same actor and transaction, its own idempotency key."""
    return bus.CommandContext(
        session=ctx.session,
        store=ctx.store,
        authorizer=ctx.authorizer,
        clock=ctx.clock,
        principal=ctx.principal,
        workspace_id=ctx.workspace_id,
        correlation_id=ctx.correlation_id,
        idempotency_key=f"{ctx.idempotency_key}:{suffix}"[:200],
        extras=dict(ctx.extras),
    )


def _result(res: Any, resource_id: str, aggregate: str, **data: Any) -> CommandResult:
    return CommandResult(
        resource_id,
        res.event_id,
        res.aggregate_seq,
        aggregate,
        replayed=res.replayed,
        data=data,
    )


def _deliver_next_turn(ctx: CommandContext, state: eng.BrainstormState) -> str | None:
    """Queue the ``brainstorm_turn`` work item for the Agent whose turn it now is."""
    fresh = _state(ctx, state.brainstorm_id)
    if fresh.status != "OPEN":
        return None
    participant = eng.next_agent(fresh)
    if participant is None or participant.agent_id is None:
        return None
    now = ctx.clock.now()
    item = inbox.enqueue(
        ctx.session,
        ctx.store,
        workspace_id=ctx.workspace_id,
        kind="brainstorm_turn",
        agent_id=participant.agent_id,
        payload=trn.turn_payload(fresh, participant),
        deadline=now + dt.timedelta(seconds=trn.DEFAULT_TURN_DEADLINE_S),
        expected_result_schema=trn.TURN_RESULT_SCHEMA,
        correlation_id=ctx.correlation_id,
        idempotency_key=f"{fresh.brainstorm_id}:turn:{fresh.turn_no + 1}",
        actor_account_id=ctx.principal.account_uuid,
        clock=ctx.clock,
        brainstorm_id=fresh.brainstorm_id,
    )
    return str(item.work_item_id)


def _pause(
    ctx: CommandContext, cmd: Command, state: eng.BrainstormState, reason_code: str, detail: str
) -> Any:
    """PAUSED + guidance request to the facilitator (§7F)."""
    now = ctx.clock.now()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=state.brainstorm_id,
        event_type="BRAINSTORM_PAUSED",
        payload={
            "brainstorm_id": state.brainstorm_id,
            "reason_code": reason_code,
            "detail": detail,
        },
        channel_uuid=state.channel_uuid,
    )
    eng.set_status(
        ctx.session,
        state.brainstorm_id,
        "PAUSED",
        now=now,
        reason=reason_code,
        event_id=res.event_id,
    )
    _request_guidance(ctx, state, reason_code, detail, res.event_id)
    _audit(ctx, "brainstorm.paused", state.brainstorm_id, reason_code=reason_code)
    return res


def _pause_on_breach(
    ctx: CommandContext, cmd: Command, state: eng.BrainstormState, reason_code: str, detail: str
) -> None:
    """Pause in its own transaction: the offending contribution is rejected, so the caller's
    transaction rolls back and would otherwise take the pause and the guidance request with it."""
    from sqlalchemy.orm import Session as SaSession

    from server.events.postgres_store import PostgresEventStore

    bind = ctx.session.get_bind()
    with SaSession(bind) as own, own.begin():
        own_ctx = bus.CommandContext(
            session=own,
            store=PostgresEventStore(own, clock=ctx.clock),
            authorizer=ctx.authorizer,
            clock=ctx.clock,
            principal=ctx.principal,
            workspace_id=ctx.workspace_id,
            correlation_id=ctx.correlation_id,
            idempotency_key=f"{ctx.idempotency_key}:pause",
        )
        fresh = eng.load(own, _ws(own_ctx), state.brainstorm_id)
        if fresh is None or fresh.status != "OPEN":  # already paused by a concurrent breach
            return
        _pause(own_ctx, cmd, fresh, reason_code, detail)


def _request_guidance(
    ctx: CommandContext,
    state: eng.BrainstormState,
    reason_code: str,
    detail: str,
    event_id: str,
) -> None:
    from server.notifications import outbox as notify

    notify.enqueue(
        ctx.session,
        workspace_id=ctx.workspace_id,
        kind=GUIDANCE_KIND,
        destination=f"account:{state.facilitator_uuid}",
        dedupe_key=f"bs-guidance:{state.brainstorm_id}:{event_id}",
        payload={
            "reason": "BRAINSTORM_GUIDANCE_REQUESTED",
            "brainstorm_id": state.brainstorm_id,
            "reason_code": reason_code,
            "detail": detail,
            "topic": state.topic,
            "channel_id": state.channel_id,
        },
        source_event_id=event_id,
        next_attempt_at=ctx.clock.now(),
    )


# ------------------------------------------------------------------ handlers
@handles(StartBrainstorm)
def start_brainstorm(cmd: StartBrainstorm, ctx: CommandContext) -> CommandResult:
    require_permission(
        ctx, "brainstorm.open", action="command:brainstorm.start", channel_id=cmd.channel_id
    )
    if not cmd.topic.strip():
        raise CommandError("BRAINSTORM_TOPIC_REQUIRED", "topic must not be empty", status=400)
    try:
        channel = mem.channel_ref(ctx.session, _ws(ctx), cmd.channel_id)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    try:
        limits = lim.parse(cmd.limits)
    except lim.LimitsError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc
    brainstorm_id = cmd.brainstorm_id or eng.new_brainstorm_id()
    now = ctx.clock.now()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=brainstorm_id,
        event_type="BRAINSTORM_OPENED",
        payload={
            "brainstorm_id": brainstorm_id,
            "channel_id": channel.channel_id,
            "topic": cmd.topic,
            "facilitator_account_id": ctx.principal.account_id,
            "limits": limits.as_dict(),
            "participants": list(cmd.participants),
        },
        channel_uuid=channel.id,
    )
    if res.replayed:
        return _result(res, brainstorm_id, "brainstorm", replayed=True)
    eng.insert(
        ctx.session,
        brainstorm_id=brainstorm_id,
        workspace_id=_ws(ctx),
        channel_uuid=channel.id,
        topic=cmd.topic,
        facilitator=uuid.UUID(ctx.principal.account_uuid),
        limits=limits,
        now=now,
    )
    seated: list[str] = []
    for account_id in cmd.participants:
        account_uuid, account_type = _account(ctx, account_id)
        role = "agent" if account_type == "agent" else "human"
        eng.add_participant(
            ctx.session,
            brainstorm_id=brainstorm_id,
            account_uuid=account_uuid,
            role=role,
            agent_id=_agent_id_of(ctx, account_uuid) if role == "agent" else None,
            now=now,
        )
        seated.append(account_id)
    eng.set_status(ctx.session, brainstorm_id, "OPEN", now=now, event_id=res.event_id)
    state = _state(ctx, brainstorm_id)
    work_item_id = _deliver_next_turn(ctx, state)
    _audit(ctx, "brainstorm.opened", brainstorm_id, participants=seated, topic=cmd.topic)
    return _result(
        res, brainstorm_id, "brainstorm", participants=seated, next_work_item_id=work_item_id
    )


@handles(JoinBrainstorm)
def join_brainstorm(cmd: JoinBrainstorm, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.start",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if state.status == "CLOSED":
        raise CommandError("BRAINSTORM_CLOSED", cmd.brainstorm_id, status=409)
    account_uuid, account_type = _account(ctx, cmd.account_id)
    role = "agent" if account_type == "agent" else "human"
    now = ctx.clock.now()
    seat = eng.add_participant(
        ctx.session,
        brainstorm_id=cmd.brainstorm_id,
        account_uuid=account_uuid,
        role=role,
        agent_id=_agent_id_of(ctx, account_uuid) if role == "agent" else None,
        now=now,
    )
    _audit(ctx, "brainstorm.joined", cmd.brainstorm_id, account_id=cmd.account_id, role=role)
    return CommandResult(cmd.brainstorm_id, "", 0, "brainstorm", data={"seat": seat, "role": role})


@handles(ContributeTurn)
def contribute_turn(cmd: ContributeTurn, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.contribute",
        action="command:brainstorm.contribute",
        channel_id=state.channel_id,
    )
    if state.status != "OPEN":
        raise CommandError(
            "BRAINSTORM_NOT_OPEN", f"{cmd.brainstorm_id} is {state.status}", status=409
        )
    if not cmd.body.strip():
        raise CommandError("BRAINSTORM_BODY_REQUIRED", "contribution must not be empty", status=400)
    actor = uuid.UUID(ctx.principal.account_uuid)
    participant = state.participant(actor)
    if participant is None:
        raise CommandError("BRAINSTORM_NOT_A_PARTICIPANT", ctx.principal.account_id, status=403)
    is_agent = participant.role == "agent"
    contribution_type = (cmd.contribution_type or ("IDEA" if not is_agent else "")).upper()
    if is_agent and not cmd.contribution_type:
        raise CommandError(
            "BRAINSTORM_CONTRIBUTION_TYPE_REQUIRED",
            "Agents declare IDEA|CHALLENGE|QUESTION|GUIDANCE",
            status=400,
        )
    if contribution_type not in eng.CONTRIBUTION_TYPES:
        raise CommandError("BRAINSTORM_CONTRIBUTION_TYPE_INVALID", contribution_type, status=400)
    breach = lim.check(
        state.limits,
        lim.TurnState(
            total_turns=state.turn_no,
            contributor_turns=participant.turns_taken,
            consecutive_turns=state.consecutive_turns,
            is_last_contributor=state.last_contributor == actor,
            spent_cost_units=eng.spent_cost_units(ctx.session, state.brainstorm_id),
            started_at=state.started_at,
            now=ctx.clock.now(),
        ),
        is_agent=is_agent,
    )
    if breach is not None:
        _pause_on_breach(ctx, cmd, state, breach.code.value, breach.detail)
        raise CommandError(breach.code.value, breach.detail, status=409)
    if is_agent:
        expected = eng.next_agent(state)
        if expected is not None and expected.account_uuid != actor:
            raise CommandError(
                "BRAINSTORM_NOT_YOUR_TURN",
                f"{expected.account_id} holds turn {state.turn_no + 1}",
                status=409,
            )
    now = ctx.clock.now()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=state.brainstorm_id,
        event_type="IDEA_RECORDED",
        payload={
            "brainstorm_id": state.brainstorm_id,
            "contribution_type": contribution_type,
            "contributor_account_id": ctx.principal.account_id,
            "turn_no": state.turn_no + 1,
            "body": cmd.body,
        },
        channel_uuid=state.channel_uuid,
    )
    if res.replayed:
        return _result(res, state.brainstorm_id, "brainstorm", replayed=True)
    turn_id = trn.record(
        ctx.session,
        state,
        account_uuid=actor,
        contribution_type=contribution_type,
        body=cmd.body,
        event_id=res.event_id,
        work_item_id=cmd.work_item_id,
        now=now,
    )
    eng.advance(ctx.session, state, contributor=actor, is_agent=is_agent, now=now)
    work_item_id = _deliver_next_turn(ctx, state)
    return _result(
        res,
        state.brainstorm_id,
        "brainstorm",
        turn_id=turn_id,
        turn_no=state.turn_no + 1,
        contribution_type=contribution_type,
        next_work_item_id=work_item_id,
    )


@handles(PauseBrainstorm)
def pause_brainstorm(cmd: PauseBrainstorm, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.pause",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if state.status != "OPEN":
        raise CommandError("BRAINSTORM_NOT_OPEN", state.status, status=409)
    res = _pause(ctx, cmd, state, cmd.reason_code, "facilitator paused the session")
    return _result(res, state.brainstorm_id, "brainstorm", status="PAUSED")


@handles(ResumeBrainstorm)
def resume_brainstorm(cmd: ResumeBrainstorm, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.resume",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if state.status != "PAUSED":
        raise CommandError("BRAINSTORM_NOT_PAUSED", state.status, status=409)
    merged = dict(state.limits.as_dict())
    merged.update({k: v for k, v in (cmd.limits or {}).items()})
    try:
        limits = lim.parse(merged)
    except lim.LimitsError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc
    now = ctx.clock.now()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=state.brainstorm_id,
        event_type="BRAINSTORM_RESUMED",
        payload={"brainstorm_id": state.brainstorm_id, "limits": limits.as_dict()},
        channel_uuid=state.channel_uuid,
    )
    if res.replayed:
        return _result(res, state.brainstorm_id, "brainstorm", replayed=True)
    ctx.session.execute(
        text(
            "UPDATE brainstorms SET limits = CAST(:l AS jsonb), consecutive_turns = 0, "
            "updated_at = :n WHERE brainstorm_id = :b"
        ),
        {"l": eng.dump_json(limits.as_dict()), "n": now, "b": state.brainstorm_id},
    )
    eng.set_status(
        ctx.session, state.brainstorm_id, "OPEN", now=now, reason=None, event_id=res.event_id
    )
    work_item_id = _deliver_next_turn(ctx, state)
    _audit(ctx, "brainstorm.resumed", state.brainstorm_id, limits=limits.as_dict())
    return _result(
        res,
        state.brainstorm_id,
        "brainstorm",
        status="OPEN",
        limits=limits.as_dict(),
        next_work_item_id=work_item_id,
    )


@handles(CloseBrainstorm)
def close_brainstorm(cmd: CloseBrainstorm, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.close",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if state.status == "CLOSED":
        raise CommandError("BRAINSTORM_CLOSED", cmd.brainstorm_id, status=409)
    now = ctx.clock.now()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=state.brainstorm_id,
        event_type="BRAINSTORM_CLOSED",
        payload={
            "brainstorm_id": state.brainstorm_id,
            "turn_count": state.turn_no,
            "decision_count": len(dec.list_for(ctx.session, state.brainstorm_id)),
        },
        channel_uuid=state.channel_uuid,
    )
    if res.replayed:
        return _result(res, state.brainstorm_id, "brainstorm", replayed=True)
    eng.set_status(
        ctx.session, state.brainstorm_id, "CLOSED", now=now, reason=None, event_id=res.event_id
    )
    _audit(ctx, "brainstorm.closed", state.brainstorm_id, turn_count=state.turn_no)
    return _result(res, state.brainstorm_id, "brainstorm", status="CLOSED")


@handles(SummarizeBrainstorm)
def summarize_brainstorm(cmd: SummarizeBrainstorm, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.summarize",
        action="command:brainstorm.summarize",
        channel_id=state.channel_id,
    )
    if state.status == "CLOSED":
        raise CommandError("BRAINSTORM_CLOSED", cmd.brainstorm_id, status=409)
    candidate, is_participant = summ.choose_summarizer(
        ctx.session,
        state,
        workspace_id=ctx.workspace_id,
        authorizer=ctx.authorizer,
        correlation_id=ctx.correlation_id,
    )
    summarizer_account_id = candidate.account_id if candidate else ctx.principal.account_id
    summarizer_uuid = (
        uuid.UUID(candidate.account_uuid) if candidate else uuid.UUID(ctx.principal.account_uuid)
    )
    body = cmd.body or summ.draft_body(state, eng.transcript(ctx.session, state.brainstorm_id))
    artifact = bus.execute(
        RegisterArtifact(
            filename=f"{state.brainstorm_id}-summary.md",
            mime="text/markdown",
            content=body.encode("utf-8"),
        ),
        _derived(ctx, "summary-artifact"),
    )
    summary_id = summ.new_summary_id()
    res = _append(
        ctx,
        cmd,
        aggregate_type="brainstorm",
        aggregate_id=state.brainstorm_id,
        event_type="SUMMARY_RECORDED",
        payload={
            "brainstorm_id": state.brainstorm_id,
            "summarizer_account_id": summarizer_account_id,
            "artifact_id": artifact.resource_id,
            "summary_id": summary_id,
            "summarizer_is_participant": is_participant,
        },
        channel_uuid=state.channel_uuid,
    )
    if res.replayed:
        return _result(res, state.brainstorm_id, "brainstorm", replayed=True)
    summ.insert(
        ctx.session,
        summary_id=summary_id,
        brainstorm_id=state.brainstorm_id,
        author=summarizer_uuid,
        body=body,
        artifact_id=str(artifact.resource_id),
        event_id=res.event_id,
        now=ctx.clock.now(),
    )
    _audit(
        ctx,
        "brainstorm.summarized",
        state.brainstorm_id,
        summary_id=summary_id,
        summarizer=summarizer_account_id,
        summarizer_is_participant=is_participant,
    )
    return _result(
        res,
        summary_id,
        "brainstorm",
        summary_id=summary_id,
        status="DRAFT",
        artifact_id=artifact.resource_id,
        summarizer_account_id=summarizer_account_id,
        summarizer_is_participant=is_participant,
        posted=False,
    )


@handles(ApproveSummary)
def approve_summary(cmd: ApproveSummary, ctx: CommandContext) -> CommandResult:
    record = summ.load(ctx.session, cmd.summary_id)
    if record is None:
        raise CommandError("SUMMARY_NOT_FOUND", cmd.summary_id, status=404)
    state = _state(ctx, str(record["brainstorm_id"]))
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.summarize",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if record["status"] != "DRAFT":
        raise CommandError("SUMMARY_NOT_DRAFT", str(record["status"]), status=409)
    posted = _post_summary(ctx, state, record) if cmd.post else False
    summ.approve(
        ctx.session,
        cmd.summary_id,
        approver=uuid.UUID(ctx.principal.account_uuid),
        now=ctx.clock.now(),
        posted=posted,
    )
    _audit(ctx, "brainstorm.summary_approved", state.brainstorm_id, summary_id=cmd.summary_id)
    return CommandResult(
        cmd.summary_id,
        "",
        0,
        "brainstorm",
        data={"summary_id": cmd.summary_id, "status": "APPROVED", "posted": posted},
    )


def _post_summary(ctx: CommandContext, state: eng.BrainstormState, record: dict[str, Any]) -> bool:
    """Post the approved summary to the session's channel; no target means nothing to post to."""
    from server.channels.outbox import Delivery, enqueue_delivery
    from server.channels.task_cards import channel_target

    target = channel_target(ctx.session, str(state.channel_uuid))
    if target is None:
        return False
    enqueue_delivery(
        ctx.session,
        workspace_id=ctx.workspace_id,
        source_event_id=record.get("event_id"),
        delivery=Delivery(
            "mattermost.post",
            f"mattermost:{target.external_channel_id}",
            {"message": str(record["body"])},
            f"bs-summary:{record['summary_id']}",
            subject_type="brainstorm",
            subject_id=state.brainstorm_id,
            role="summary",
        ),
        provider_instance_id=target.provider_instance_id,
        external_channel_id=target.external_channel_id,
        now=ctx.clock.now(),
    )
    return True


@handles(RecordDecision)
def record_decision(cmd: RecordDecision, ctx: CommandContext) -> CommandResult:
    state = _state(ctx, cmd.brainstorm_id)
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.decide",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    if state.status == "CLOSED":
        raise CommandError("BRAINSTORM_CLOSED", cmd.brainstorm_id, status=409)
    if not cmd.statement.strip() or not cmd.rationale.strip():
        raise CommandError(
            "DECISION_STATEMENT_REQUIRED", "statement and rationale are required", status=400
        )
    try:
        action_items = dec.validate_action_items(list(cmd.action_items))
        vote = dec.validate_vote(cmd.vote)
    except dec.DecisionError as exc:
        raise _wrap(exc) from exc
    decision_id = cmd.decision_id or dec.new_decision_id()
    res = _append(
        ctx,
        cmd,
        aggregate_type="decision",
        aggregate_id=decision_id,
        event_type="DECISION_RECORDED",
        payload={
            "decision_id": decision_id,
            "brainstorm_id": state.brainstorm_id,
            "statement": cmd.statement,
            "decided_by": ctx.principal.account_id,
            "rationale": cmd.rationale,
            "source_event_ids": list(cmd.source_event_ids),
            "action_item_count": len(action_items),
            "vote": vote,
        },
        channel_uuid=state.channel_uuid,
    )
    if res.replayed:
        return _result(res, decision_id, "decision", replayed=True)
    dec.insert(
        ctx.session,
        decision_id=decision_id,
        brainstorm_id=state.brainstorm_id,
        workspace_id=_ws(ctx),
        statement=cmd.statement,
        rationale=cmd.rationale,
        source_event_ids=list(cmd.source_event_ids),
        action_items=action_items,
        vote=vote,
        decided_by=uuid.UUID(ctx.principal.account_uuid),
        event_id=res.event_id,
        now=ctx.clock.now(),
    )
    _audit(
        ctx,
        "brainstorm.decided",
        state.brainstorm_id,
        decision_id=decision_id,
        action_items=len(action_items),
    )
    return _result(
        res, decision_id, "decision", decision_id=decision_id, action_items=len(action_items)
    )


@handles(TaskifyDecision)
def taskify_decision(cmd: TaskifyDecision, ctx: CommandContext) -> CommandResult:
    record = dec.load(ctx.session, _ws(ctx), cmd.decision_id)
    if record is None:
        raise CommandError("DECISION_NOT_FOUND", cmd.decision_id, status=404)
    state = _state(ctx, str(record["brainstorm_id"]))
    require_permission(
        ctx,
        "brainstorm.facilitate",
        action="command:brainstorm.taskify",
        channel_id=state.channel_id,
    )
    _facilitator_only(ctx, state)
    action_items = list(record["action_items"] or [])
    if not action_items:
        raise CommandError(
            "DECISION_HAS_NO_ACTION_ITEMS",
            "record the Decision with action_items before taskify",
            status=409,
        )
    existing = {int(t["item_index"]): str(t["task_id"]) for t in record["tasks"]}
    created: list[dict[str, Any]] = []
    now = ctx.clock.now()
    for index, item in enumerate(action_items):
        if index in existing:
            created.append({"task_id": existing[index], "item_index": index, "replayed": True})
            continue
        criteria = tuple(item["criteria"])
        result = bus.execute(
            tasks_app.CreateTask(
                title=str(item["statement"]),
                channel_id=str(state.channel_uuid),
                domain=cmd.domain,
                risk=cmd.risk,
                criteria=criteria,
            ),
            _derived(ctx, f"taskify-{cmd.decision_id}-{index}"),
        )
        tsk.link(
            ctx.session,
            decision_id=cmd.decision_id,
            task_id=result.resource_id,
            action_item=str(item["statement"]),
            item_index=index,
            now=now,
        )
        created.append({"task_id": result.resource_id, "item_index": index, "replayed": False})
    tsk.mark_taskified(ctx.session, cmd.decision_id)
    _audit(
        ctx,
        "brainstorm.taskified",
        state.brainstorm_id,
        decision_id=cmd.decision_id,
        tasks=[c["task_id"] for c in created],
    )
    return CommandResult(
        cmd.decision_id,
        "",
        0,
        "decision",
        data={"decision_id": cmd.decision_id, "tasks": created},
    )


# ------------------------------------------------------------------ reads
def brainstorm_view(ctx: CommandContext, brainstorm_id: str) -> dict[str, Any]:
    require_permission(ctx, "brainstorm.read", action="command:brainstorm.show")
    state = _state(ctx, brainstorm_id)
    view = state.view()
    view["summaries"] = summ.list_for(ctx.session, brainstorm_id)
    view["decisions"] = dec.list_for(ctx.session, brainstorm_id)
    return view


def list_brainstorms(ctx: CommandContext, status: str | None = None) -> list[dict[str, Any]]:
    require_permission(ctx, "brainstorm.read", action="command:brainstorm.show")
    return eng.list_sessions(ctx.session, _ws(ctx), status=status)


def transcript_view(ctx: CommandContext, brainstorm_id: str) -> list[dict[str, Any]]:
    require_permission(ctx, "brainstorm.read", action="command:brainstorm.show")
    _state(ctx, brainstorm_id)
    return eng.transcript(ctx.session, brainstorm_id)


def decision_view(ctx: CommandContext, decision_id: str) -> dict[str, Any]:
    require_permission(ctx, "brainstorm.read", action="command:brainstorm.show")
    record = dec.load(ctx.session, _ws(ctx), decision_id)
    if record is None:
        raise CommandError("DECISION_NOT_FOUND", decision_id, status=404)
    return record
