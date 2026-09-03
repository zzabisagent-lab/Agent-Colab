"""`/colab brainstorm ...` handlers (P6-02/P6-09, development plan §7A.2 grammar, §7F).

Registered into the Command Router's resource extension point by :func:`register`, which also
lifts ``brainstorm`` out of the router's "later Phase" gate. The grammar itself is fixed in P0-10
and is not changed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from server.application import brainstorm as bs
from server.application import bus
from server.channels import commands as grammar
from server.channels import router as rt
from server.channels.mattermost import provider as prov
from server.identity.principals import Principal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from server.channels.router import CommandResponse, Router, SlashRequest

LIMIT_OPTIONS = {
    "turns-per-agent": "turns_per_agent",
    "max-consecutive": "max_consecutive",
    "total-turns": "total_turns",
    "budget": "budget_cost_units",
    "time": "time_limit_minutes",
}


def _limits(args: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for option, key in LIMIT_OPTIONS.items():
        value = args.get(option, args.get(option.replace("-", "_")))
        if value is not None:
            out[key] = int(value)
    return out


def _target(parsed: grammar.ParsedCommand) -> str:
    if not parsed.target_id:
        raise bus.CommandError("BRAINSTORM_TARGET_REQUIRED", "brainstorm id required", status=400)
    return str(parsed.target_id)


def _ok(
    key: str, result: bus.CommandResult, parsed: grammar.ParsedCommand, **fields: Any
) -> CommandResponse:
    message = rt.render(key, **fields)
    if result.replayed:
        message = f"{message} ({rt.render('command.replay')})"
    return rt.CommandResponse(
        "in_channel", message, "OK", result.resource_id, result.event_id, None, parsed
    )


def _start(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    args = parsed.args
    channel = router._internal_channel(session, inst, req)
    mentions = args.get("participants") or []
    if isinstance(mentions, str):
        mentions = [mentions]
    participants = tuple(router._account_for_mention(session, inst, m) for m in mentions)
    result = router._run(
        principal,
        bs.StartBrainstorm(
            channel_id=str(channel["channel_id"]),
            topic=str(args["topic"]),
            participants=participants,
            limits=_limits(args),
        ),
        req,
    )
    return _ok(
        "reply.brainstorm_opened",
        result,
        parsed,
        brainstorm_id=result.resource_id,
        topic=str(args["topic"]),
    )


def _contribute(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    args = parsed.args
    result = router._run(
        principal,
        bs.ContributeTurn(
            brainstorm_id=_target(parsed),
            body=str(args["text"]),
            contribution_type=args.get("type"),
        ),
        req,
    )
    return _ok(
        "reply.brainstorm_contribution",
        result,
        parsed,
        brainstorm_id=_target(parsed),
        contribution_type=str(result.data.get("contribution_type", "IDEA")),
        turn_no=result.data.get("turn_no", 0),
    )


def _summarize(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    result = router._run(principal, bs.SummarizeBrainstorm(brainstorm_id=_target(parsed)), req)
    return _ok(
        "reply.brainstorm_summary_drafted",
        result,
        parsed,
        summary_id=str(result.data.get("summary_id", result.resource_id)),
        summarizer=str(result.data.get("summarizer_account_id", "")),
    )


def _decide(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    args = parsed.args
    sources = args.get("source") or []
    if isinstance(sources, str):
        sources = [sources]
    result = router._run(
        principal,
        bs.RecordDecision(
            brainstorm_id=_target(parsed),
            statement=str(args["statement"]),
            rationale=str(args.get("rationale", "")),
            source_event_ids=tuple(str(s) for s in sources),
        ),
        req,
    )
    return _ok(
        "reply.brainstorm_decided",
        result,
        parsed,
        decision_id=result.resource_id,
        statement=str(args["statement"]),
    )


def _taskify(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    decision_id = parsed.args.get("decision")
    if not decision_id:
        raise bus.CommandError(
            "DECISION_REQUIRED", "--decision dec-… selects the Decision to taskify", status=400
        )
    result = router._run(principal, bs.TaskifyDecision(decision_id=str(decision_id)), req)
    tasks = [str(t["task_id"]) for t in result.data.get("tasks", [])]
    return _ok(
        "reply.brainstorm_taskified",
        result,
        parsed,
        decision_id=str(decision_id),
        tasks=", ".join(tasks) or "-",
    )


def _pause(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    result = router._run(principal, bs.PauseBrainstorm(brainstorm_id=_target(parsed)), req)
    return _ok("reply.brainstorm_paused", result, parsed, brainstorm_id=_target(parsed))


def _resume(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    result = router._run(
        principal,
        bs.ResumeBrainstorm(brainstorm_id=_target(parsed), limits=_limits(parsed.args)),
        req,
    )
    return _ok("reply.brainstorm_resumed", result, parsed, brainstorm_id=_target(parsed))


def _close(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    result = router._run(principal, bs.CloseBrainstorm(brainstorm_id=_target(parsed)), req)
    return _ok("reply.brainstorm_closed", result, parsed, brainstorm_id=_target(parsed))


def _show(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    brainstorm_id = _target(parsed)
    from server.api.dispatch import to_bus_principal

    ctx = bus.CommandContext(
        session=session,
        store=router._runtime.store_for(session),
        authorizer=router._runtime.authorizer,
        clock=router._runtime.clock,
        principal=to_bus_principal(principal),
        workspace_id=router._runtime.resolve_workspace(session, principal.account_uuid),
        correlation_id="brainstorm:show",
        idempotency_key="read",
    )
    view = bs.brainstorm_view(ctx, brainstorm_id)
    message = rt.render(
        "reply.brainstorm_status",
        brainstorm_id=brainstorm_id,
        status=str(view["status"]),
        turn_no=view["turn_no"],
        participants=len(view["participants"]),
    )
    return rt.CommandResponse("ephemeral", message, "OK", brainstorm_id, "", None, parsed)


HANDLERS = {
    "start": _start,
    "contribute": _contribute,
    "summarize": _summarize,
    "decide": _decide,
    "taskify": _taskify,
    "pause": _pause,
    "resume": _resume,
    "close": _close,
    "show": _show,
}


def register() -> None:
    """Mount the brainstorm verbs on the Command Router (idempotent)."""
    for verb, handler in HANDLERS.items():
        rt.RESOURCE_HANDLERS[("brainstorm", verb)] = handler


def unregister() -> None:
    for verb in HANDLERS:
        rt.RESOURCE_HANDLERS.pop(("brainstorm", verb), None)
