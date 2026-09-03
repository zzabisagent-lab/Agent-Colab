"""`/colab schedule ...` handlers (development plan §7A.2 grammar, §10A.5).

The grammar of P0-10 advertises `schedule show|list|run-now|cancel-run|pause|resume`, and the
REST and MCP surfaces landed with Phase 5; this module mounts the same commands on the Command
Router so the documented Mattermost surface actually works. Registration also lifts `schedule`
out of the router's "later Phase" gate. The grammar itself is not changed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from server.application import bus
from server.application import schedules as sch
from server.channels import commands as grammar
from server.channels import router as rt
from server.channels.mattermost import provider as prov
from server.identity.principals import Principal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from server.channels.router import CommandResponse, Router, SlashRequest


ID_ARGS = ("schedule_id", "run_id", "schedule_or_run_id")


def _target(parsed: grammar.ParsedCommand, what: str = "schedule") -> str:
    """The grammar binds the id to a named argument; a thread target fills ``target_id``."""
    for name in ID_ARGS:
        value = parsed.args.get(name)
        if value:
            return str(value)
    if parsed.target_id:
        return str(parsed.target_id)
    raise bus.CommandError("SCHEDULE_TARGET_REQUIRED", f"{what} id required", status=400)


def _ok(
    key: str, result: bus.CommandResult, parsed: grammar.ParsedCommand, **fields: Any
) -> CommandResponse:
    message = rt.render(key, **fields)
    if result.replayed:
        message = f"{message} ({rt.render('command.replay')})"
    return rt.CommandResponse(
        "ephemeral", message, "OK", result.resource_id, result.event_id or "", None, parsed
    )


def _read_ctx(router: Router, session: Session, principal: Principal, what: str) -> Any:
    from server.api.dispatch import to_bus_principal

    return bus.CommandContext(
        session=session,
        store=router._runtime.store_for(session),
        authorizer=router._runtime.authorizer,
        clock=router._runtime.clock,
        principal=to_bus_principal(principal),
        workspace_id=router._runtime.resolve_workspace(session, principal.account_uuid),
        correlation_id=f"schedule:{what}",
        idempotency_key="read",
    )


def _show(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    target = _target(parsed, "schedule or run")
    ctx = _read_ctx(router, session, principal, "show")
    if target.startswith("run-"):
        view = sch.run_view(ctx, target)
        message = rt.render(
            "reply.schedule_run_status",
            run_id=target,
            status=str(view["status"]),
            schedule_id=str(view["schedule_id"]),
            scheduled_for=str(view.get("scheduled_for") or "-"),
        )
    else:
        view = sch.schedule_view(ctx, target)
        version = view.get("current_version") or {}
        message = rt.render(
            "reply.schedule_status",
            schedule_id=target,
            status=str(view["status"]),
            cron=str(version.get("cron_expression", "-")),
            timezone=str(version.get("timezone", "-")),
            next_run_at=str(view.get("next_run_at") or "-"),
        )
    return rt.CommandResponse("ephemeral", message, "OK", target, "", None, parsed)


def _list(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    ctx = _read_ctx(router, session, principal, "list")
    wanted = parsed.args.get("status")
    items = [s for s in sch.list_schedules(ctx) if not wanted or s["status"] == wanted]
    lines = [
        f"`{s['schedule_id']}` {s['name']} · {s['status']} · next {s.get('next_run_at') or '-'}"
        for s in items[:20]
    ]
    body = "\n".join(lines) if lines else rt.render("reply.schedule_none")
    return rt.CommandResponse("ephemeral", body, "OK", "", "", None, parsed)


def _run_now(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    schedule_id = _target(parsed)
    result = router._run(principal, sch.RunScheduleNow(schedule_id=schedule_id), req)
    return _ok("reply.schedule_run_now", result, parsed, schedule_id=schedule_id)


def _cancel_run(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    run_id = _target(parsed, "run")
    reason = str(parsed.args.get("reason") or "COMMAND")
    result = router._run(principal, sch.CancelScheduleRun(run_id=run_id, reason_code=reason), req)
    return _ok("reply.schedule_run_cancelled", result, parsed, run_id=run_id)


def _pause(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    schedule_id = _target(parsed)
    result = router._run(principal, sch.PauseSchedule(schedule_id=schedule_id), req)
    return _ok("reply.schedule_paused", result, parsed, schedule_id=schedule_id)


def _resume(
    router: Router,
    session: Session,
    inst: prov.ProviderInstance,
    req: SlashRequest,
    principal: Principal,
    parsed: grammar.ParsedCommand,
) -> CommandResponse:
    schedule_id = _target(parsed)
    result = router._run(principal, sch.ResumeSchedule(schedule_id=schedule_id), req)
    return _ok("reply.schedule_resumed", result, parsed, schedule_id=schedule_id)


HANDLERS = {
    "show": _show,
    "list": _list,
    "run-now": _run_now,
    "cancel-run": _cancel_run,
    "pause": _pause,
    "resume": _resume,
}


def register() -> None:
    """Mount the schedule verbs on the Command Router (idempotent)."""
    for verb, handler in HANDLERS.items():
        rt.RESOURCE_HANDLERS[("schedule", verb)] = handler


def unregister() -> None:
    for verb in HANDLERS:
        rt.RESOURCE_HANDLERS.pop(("schedule", verb), None)
