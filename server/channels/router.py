"""Command Router (P2-10; development plan §7A.2, §3.1 Command Router).

``/colab <resource> <verb> ...`` (or ``@colab``) → Command Router → the same application command
handlers as REST/MCP. The router never interprets free text, resolves the principal only from
the Mattermost user's active ExternalIdentityLink, applies thread-context targets, uses the
provider idempotency key ``<provider_instance>:<post_id|trigger_id>``, and answers with an
ephemeral message (cause + correct example) or a public thread reply. Message texts live behind
keys (``MESSAGES``) so P2-16 can localize them.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command, to_bus_principal
from server.api.errors import ApiError
from server.application import approvals as approvals_app
from server.application import bus
from server.application import tasks as tasks_app
from server.application import verification as verification_app
from server.application import work as work_app
from server.channels import commands as grammar
from server.channels.mattermost import provider as prov
from server.db.engine import session_scope
from server.domain.clock import Clock, SystemClock
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError, Principal

MESSAGES: dict[str, str] = {
    "command.prefix_missing": (
        "Commands start with `/colab` (or `@colab`). Free text is never interpreted."
    ),
    "command.resource_unknown": "Unknown resource.",
    "command.verb_missing": "A verb is required.",
    "command.verb_unknown": "Unknown verb for this resource.",
    "command.unlinked_restricted": "Link your Mattermost user to an Agent-Colab Account first.",
    "command.args_extra": "Unexpected extra arguments.",
    "command.args_invalid": "Invalid arguments.",
    "command.target_required": "Target id required (or run the command inside the Task thread).",
    "command.not_available_phase": "This command becomes available in a later phase.",
    "command.link_pending": "Account linking is handled by the link challenge (P2-13).",
    "command.reject_no_work_item": "No delivered work item to reject for this Task.",
    "command.replay": "Already processed (same post).",
    "command.denied": "Not permitted.",
    "command.error": "Command failed.",
    "reply.task_created": "Task {task_id} created: {title}",
    "reply.subtask_created": "Sub-Task {task_id} created under {parent_task_id}: {title}",
    "reply.task_delegated": "Task {task_id} delegated to {assignee}",
    "reply.task_reassigned": "Task {task_id} reassigned to {assignee}",
    "reply.task_accepted": "Task {task_id} accepted",
    "reply.task_started": "Task {task_id} started",
    "reply.task_progress": "Progress on {task_id}: {summary}",
    "reply.task_submitted": "Implementation submitted for {task_id} (criteria revision {revision})",
    "reply.task_completed": "Task {task_id} completed",
    "reply.task_cancel_requested": "Cancellation requested for {task_id}",
    "reply.task_cancelled": "Task {task_id} cancelled",
    "reply.task_rejected": "Assignment rejected for {task_id}: {reason}",
    "reply.approval_requested": "Approval {approval_id} requested for {action} ({risk})",
    "reply.approval_decided": "Approval {approval_id}: {decision}",
    "reply.verification_assigned": (
        "Verification {verification_id} assigned to {verifier} for {task_id}"
    ),
    "reply.verdict": "Verification {verification_id}: {verdict}",
    "reply.notify": "Notification preferences updated: {setting}",
}


_LANGUAGE: ContextVar[str] = ContextVar("agent_colab_router_language", default="en")


def language_for_channel(
    session: Session, provider_instance_uuid: Any, external_channel_id: str
) -> str:
    """Channel language override, else the instance default (development plan §7H)."""
    from server.i18n import resolve_language

    row = session.execute(
        text(
            "SELECT language FROM channels WHERE provider_instance_id = :p "
            "AND external_channel_id = :c"
        ),
        {"p": provider_instance_uuid, "c": external_channel_id},
    ).first()
    instance_default = os.environ.get("AGENT_COLAB_DEFAULT_LANGUAGE", "en")
    return resolve_language(instance_default, row[0] if row else None)


def render(key: str, **fields: Any) -> str:
    """Localized message for the current request language; English text is the fallback."""
    from server.i18n import UnsupportedLanguageError, bundle

    language = _LANGUAGE.get()
    try:
        template = bundle(language).get(key) or MESSAGES.get(key, key)
    except UnsupportedLanguageError:
        template = MESSAGES.get(key, key)
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        return template


@dataclass(frozen=True)
class SlashRequest:
    """The form-encoded slash payload after provider validation (development plan §7A.2)."""

    provider_instance_id: str
    team_id: str
    channel_id: str  # external (Mattermost) channel id
    user_id: str  # external (Mattermost) user id
    user_name: str
    command: str
    text: str
    trigger_id: str = ""
    response_url: str = ""
    post_id: str | None = None
    root_id: str | None = None

    @property
    def full_text(self) -> str:
        cmd = self.command.strip()
        return f"{cmd} {self.text}".strip() if cmd else self.text


@dataclass(frozen=True)
class CommandResponse:
    response_type: str  # ephemeral | in_channel
    text: str
    code: str = "OK"
    resource_id: str | None = None
    event_id: str | None = None
    post_id: str | None = None
    parsed: grammar.ParsedCommand | None = field(default=None, compare=False)

    def as_mattermost(self) -> dict[str, Any]:
        return {"response_type": self.response_type, "text": self.text}


LinkHandler = Callable[
    [Session, Runtime, SlashRequest, grammar.ParsedCommand, Clock], CommandResponse
]
LINK_HANDLERS: dict[str, LinkHandler] = {}  # P2-13 registers "start" and "confirm"

# Resource-level extension point in the same style: a later Phase registers ``(resource, verb)``
# handlers, which also lifts that resource out of the PHASE_LATER gate below (P6-02: brainstorm).
ResourceHandler = Callable[
    ["Router", Session, prov.ProviderInstance, SlashRequest, Principal, grammar.ParsedCommand],
    "CommandResponse",
]
RESOURCE_HANDLERS: dict[tuple[str, str], ResourceHandler] = {}

PHASE_LATER: dict[str, int] = {"schedule": 5, "brainstorm": 6}
DOC_LATER_VERBS = {"review": 6, "publish": 6}


def ephemeral(key: str, code: str, detail: str = "", example: str = "") -> CommandResponse:
    parts = [render(key)]
    if detail:
        parts.append(f"({detail})")
    if example:
        parts.append(f"Example: `{example}`")
    return CommandResponse("ephemeral", " ".join(parts), code)


def _correlation(req: SlashRequest) -> str:
    return f"mm:{req.provider_instance_id}:{req.trigger_id or req.post_id or 'cmd'}"[:200]


def idempotency_key(req: SlashRequest) -> str:
    """``<provider_instance>:<post_id|trigger_id>`` (§7A.2), hashed to stay within 200 chars."""
    seed = req.post_id or req.trigger_id or req.full_text
    return f"{req.provider_instance_id}:{hashlib.sha256(seed.encode()).hexdigest()[:32]}"


class Router:
    def __init__(self, runtime: Runtime, clock: Clock | None = None) -> None:
        self._runtime = runtime
        self._clock = clock or SystemClock()

    # -- public entry -------------------------------------------------------------------------
    def route(self, req: SlashRequest) -> CommandResponse:
        with session_scope(self._runtime.session_factory) as session:
            inst = prov.load_instance(session, req.provider_instance_id)
            if inst is not None:
                _LANGUAGE.set(language_for_channel(session, inst.id, req.channel_id))
            if inst is None:
                return ephemeral(
                    "command.error", "PROVIDER_INSTANCE_UNKNOWN", req.provider_instance_id
                )
            principal = self._principal(session, req)
            thread = self._thread_context(session, inst, req)
            context = grammar.CommandContext(
                linked=principal is not None,
                thread_subject_kind=thread.subject_type if thread else None,
                thread_subject_id=thread.subject_id if thread else None,
            )
            try:
                parsed = grammar.parse_command(req.full_text, context)
            except grammar.CommandError as exc:
                return ephemeral(exc.message_key, exc.code, exc.detail, exc.example)
            if parsed.resource == "help":
                topic = parsed.args.get("resource") if parsed.args else None
                return CommandResponse("ephemeral", grammar.help_text(topic), "OK", parsed=parsed)
            if parsed.resource == "link":
                handler = LINK_HANDLERS.get(parsed.verb)
                if handler is None:
                    return CommandResponse(
                        "ephemeral", render("command.link_pending"), "LINK_PENDING", parsed=parsed
                    )
                return handler(session, self._runtime, req, parsed, self._clock)
            if principal is None:  # defence in depth: the parser already restricts unlinked users
                return ephemeral(
                    "command.unlinked_restricted",
                    "COMMAND_UNLINKED_RESTRICTED",
                    "",
                    "/colab link start",
                )
            registered = (parsed.resource, parsed.verb) in RESOURCE_HANDLERS
            if not registered and (
                parsed.resource in PHASE_LATER
                or (parsed.resource == "doc" and parsed.verb in DOC_LATER_VERBS)
            ):
                phase = PHASE_LATER.get(parsed.resource, DOC_LATER_VERBS.get(parsed.verb, 0))
                return ephemeral(
                    "command.not_available_phase", "COMMAND_NOT_AVAILABLE", f"Phase {phase}"
                )
            try:
                response = self._execute(session, inst, req, principal, parsed)
            except ApiError as exc:
                key = (
                    "command.denied"
                    if exc.status in (403, 404) and exc.code.endswith(("DENY", "DENIED"))
                    else "command.error"
                )
                return CommandResponse(
                    "ephemeral", f"{render(key)} {exc.code}: {exc.detail}", exc.code, parsed=parsed
                )
            except (bus.CommandError, IdentityError) as exc:
                return CommandResponse(
                    "ephemeral",
                    f"{render('command.error')} {exc.code}: {exc.detail}",
                    exc.code,
                    parsed=parsed,
                )
            return response

    # -- helpers ------------------------------------------------------------------------------
    def _principal(self, session: Session, req: SlashRequest) -> Principal | None:
        service = sql_service(session, self._runtime.store_for(session), self._clock)
        try:
            return service.resolve_command_principal(req.provider_instance_id, req.user_id)
        except IdentityError:
            return None

    def _thread_context(
        self, session: Session, inst: prov.ProviderInstance, req: SlashRequest
    ) -> prov.ThreadBinding | None:
        root = req.root_id or None
        if root is None and req.post_id:
            root = req.post_id
        if root is None:
            return None
        return prov.binding_for_post(session, inst.id, root)

    def _internal_channel(
        self, session: Session, inst: prov.ProviderInstance, req: SlashRequest
    ) -> Any:
        row = prov.internal_channel(session, inst.id, req.channel_id)
        if row is None:
            raise bus.CommandError("CHANNEL_NOT_IMPORTED", req.channel_id, status=404)
        return row

    def _run(
        self,
        principal: Principal,
        command: bus.Command,
        req: SlashRequest,
        suffix: str = "",
        **extras: Any,
    ) -> bus.CommandResult:
        return execute_command(
            self._runtime,
            principal,
            command,
            idempotency_key=idempotency_key(req) + suffix,
            correlation_id=_correlation(req),
            extras=extras,
        )

    def _account_for_mention(
        self, session: Session, inst: prov.ProviderInstance, mention: str
    ) -> str:
        """`@username` → Account (via the user's active link); `acct-…` is passed through."""
        if not mention.startswith("@"):
            return mention
        username = mention[1:]
        user = prov.client_for(inst).get_user_by_username(username)
        row = session.execute(
            text(
                "SELECT a.account_id FROM external_identity_links l JOIN accounts a ON a.id = "
                "l.account_id "
                "WHERE l.provider_instance_id = :p AND l.external_user_id = :u AND l.status = "
                "'active'"
            ),
            {"p": inst.id, "u": str(user["id"])},
        ).first()
        if row is None:
            raise bus.CommandError("ASSIGNEE_NOT_LINKED", mention, status=404)
        return str(row[0])

    def _task_status(self, session: Session, task_id: str) -> str | None:
        row = session.execute(
            text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
        ).first()
        return None if row is None else str(row[0])

    def _reply(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        key: str,
        parsed: grammar.ParsedCommand,
        result: bus.CommandResult,
        *,
        bind: str | None = None,
        **fields: Any,
    ) -> CommandResponse:
        """Post the public reply in the Task thread (bound root post) and bind new Tasks."""
        message = render(key, **fields)
        if result.replayed:  # same post processed before: no second Event, no second reply
            return CommandResponse(
                "in_channel",
                f"{message} ({render('command.replay')})",
                "OK",
                result.resource_id,
                result.event_id,
                None,
                parsed,
            )
        root_id: str | None = None
        if bind is None and parsed.target_id:
            existing = prov.binding_for_subject(session, inst.id, "task", parsed.target_id)
            root_id = existing.root_post_id if existing else req.root_id
        post_id: str | None = None
        try:
            client = prov.client_for(inst)
            post = client.create_post(req.channel_id, message, root_id=root_id)
            post_id = post.id
            if bind is not None:
                prov.bind_thread(session, inst.id, post.id, req.channel_id, "task", bind)
        except Exception as exc:
            message = f"{message} (delivery pending: {type(exc).__name__})"
        return CommandResponse(
            "in_channel", message, "OK", result.resource_id, result.event_id, post_id, parsed
        )

    # -- dispatch table -----------------------------------------------------------------------
    def _execute(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        registered = RESOURCE_HANDLERS.get((parsed.resource, parsed.verb))
        if registered is not None:
            return registered(self, session, inst, req, principal, parsed)
        handler = getattr(self, f"_cmd_{parsed.resource}_{parsed.verb.replace('-', '_')}", None)
        if handler is None:
            return ephemeral(
                "command.not_available_phase",
                "COMMAND_NOT_AVAILABLE",
                f"{parsed.resource} {parsed.verb}",
            )
        response: CommandResponse = handler(session, inst, req, principal, parsed)
        return response

    # task ------------------------------------------------------------------------------------
    def _cmd_task_create(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        a = parsed.args
        channel = self._internal_channel(session, inst, req)
        raw_criteria = a.get("criteria", [])
        if isinstance(raw_criteria, str):
            raw_criteria = [raw_criteria]
        criteria = tuple(
            {"statement": c, "check_type": "evidence", "required": True} for c in raw_criteria
        )
        policy = dict(channel["policy"] or {})
        domain = a.get("domain") or policy.get("task_domain", "general")
        risk = a.get("risk") or policy.get("risk_policy", {}).get("default_risk", "LOW")
        if a.get("parent"):
            cmd: bus.Command = tasks_app.CreateSubtask(
                parent_task_id=a["parent"],
                title=a["title"],
                domain=domain,
                risk=risk,
                criteria=criteria,
            )
        else:
            cmd = tasks_app.CreateTask(
                title=a["title"],
                channel_id=str(channel["id"]),
                domain=domain,
                risk=risk,
                criteria=criteria,
            )
        result = self._run(principal, cmd, req)
        task_id = result.resource_id
        key, extra = (
            ("reply.subtask_created", {"parent_task_id": a.get("parent")})
            if a.get("parent")
            else ("reply.task_created", {})
        )
        response = self._reply(
            session,
            inst,
            req,
            key,
            parsed,
            result,
            bind=task_id,
            task_id=task_id,
            title=a["title"],
            **extra,
        )
        if a.get("assignee"):
            assignee = self._account_for_mention(session, inst, a["assignee"])
            self._run(
                principal,
                tasks_app.DelegateTask(task_id=task_id, assignee_account_id=assignee),
                req,
                ":delegate",
            )
            response = CommandResponse(
                response.response_type,
                response.text + f"; delegated to {a['assignee']}",
                "OK",
                task_id,
                result.event_id,
                response.post_id,
                parsed,
            )
        return response

    def _cmd_task_delegate(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        assignee = self._account_for_mention(session, inst, parsed.args["to"])
        cmd = tasks_app.DelegateTask(
            task_id=parsed.args["task_id"],
            assignee_account_id=assignee,
            reason_code=parsed.args.get("reason") or "DELEGATED",
        )
        result = self._run(principal, cmd, req)
        return self._reply(
            session,
            inst,
            req,
            "reply.task_delegated",
            parsed,
            result,
            task_id=parsed.args["task_id"],
            assignee=parsed.args["to"],
        )

    def _cmd_task_reassign(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        assignee = self._account_for_mention(session, inst, parsed.args["to"])
        cmd = tasks_app.ReassignTask(
            task_id=parsed.args["task_id"],
            assignee_account_id=assignee,
            reason_code=parsed.args.get("reason") or "REASSIGNED",
        )
        result = self._run(principal, cmd, req)
        return self._reply(
            session,
            inst,
            req,
            "reply.task_reassigned",
            parsed,
            result,
            task_id=parsed.args["task_id"],
            assignee=parsed.args["to"],
        )

    def _cmd_task_accept(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        result = self._run(principal, tasks_app.AcceptTask(task_id=parsed.args["task_id"]), req)
        return self._reply(
            session,
            inst,
            req,
            "reply.task_accepted",
            parsed,
            result,
            task_id=parsed.args["task_id"],
        )

    def _cmd_task_reject(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        task_id = parsed.args["task_id"]
        row = session.execute(
            text(
                "SELECT work_item_id FROM work_items WHERE task_id = :t AND kind IN "
                "('task_assignment','subtask_assignment') "
                "AND status IN ('DELIVERED','ACKED','IN_PROGRESS') ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": task_id},
        ).first()
        if row is None:
            return ephemeral("command.reject_no_work_item", "WORK_ITEM_NOT_FOUND", task_id)
        result = self._run(
            principal,
            work_app.WorkReject(work_item_id=str(row[0]), reason_code=parsed.args["reason"]),
            req,
        )
        return self._reply(
            session,
            inst,
            req,
            "reply.task_rejected",
            parsed,
            result,
            task_id=task_id,
            reason=parsed.args["reason"],
        )

    def _cmd_task_progress(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        status = self._task_status(session, parsed.args["task_id"])
        if (
            status == "ACCEPTED"
        ):  # first progress report starts the Task (spec §8.2 ACCEPTED → RUNNING)
            self._run(principal, tasks_app.StartTask(task_id=parsed.args["task_id"]), req, ":start")
        result = self._run(
            principal,
            tasks_app.ReportProgress(
                task_id=parsed.args["task_id"], summary=parsed.args["message"]
            ),
            req,
        )
        return self._reply(
            session,
            inst,
            req,
            "reply.task_progress",
            parsed,
            result,
            task_id=parsed.args["task_id"],
            summary=parsed.args["message"],
        )

    def _cmd_task_submit(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        task_id = parsed.args["task_id"]
        refs = parsed.args.get("evidence", [])
        if isinstance(refs, str):
            refs = [refs]
        revision = session.execute(
            text(
                "SELECT COALESCE(MAX(revision), 0) FROM task_acceptance_criteria WHERE task_id = :t"
            ),
            {"t": task_id},
        ).scalar_one()
        criteria_ids = [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT criteria_id FROM task_acceptance_criteria WHERE task_id = :t "
                    "AND revision = :r ORDER BY criteria_id"
                ),
                {"t": task_id, "r": int(revision)},
            ).all()
        ]
        # a bare `--evidence <ref>` applies to every criterion of the current revision (§7D.1
        # needs one ref per criterion; `<criteria_id>:<ref>` targets a single criterion)
        expanded: list[str] = []
        for ref in refs:
            if ref.startswith("crit-") and ":" in ref:
                expanded.append(ref)
            else:
                expanded.extend(f"{cid}:{ref}" for cid in criteria_ids)
                expanded.append(ref)
        refs = expanded
        if self._task_status(session, task_id) == "ACCEPTED":
            self._run(principal, tasks_app.StartTask(task_id=task_id), req, ":start")
        cmd = tasks_app.SubmitImplementation(
            task_id=task_id, evidence_refs=tuple(refs), criteria_revision=int(revision)
        )
        result = self._run(principal, cmd, req)
        return self._reply(
            session,
            inst,
            req,
            "reply.task_submitted",
            parsed,
            result,
            task_id=task_id,
            revision=int(revision),
        )

    def _cmd_task_complete(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        from server.documents.lifecycle import expected_document_id

        task_id = parsed.args["task_id"]
        doc_id = expected_document_id(session, task_id) or "doc-missing"
        result = self._run(
            principal, tasks_app.CompleteTask(task_id=task_id, document_id=doc_id), req
        )
        return self._reply(
            session, inst, req, "reply.task_completed", parsed, result, task_id=task_id
        )

    def _cmd_task_cancel(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        task_id = parsed.args["task_id"]
        reason = parsed.args.get("reason") or "REQUESTED"
        status = self._task_status(session, task_id)
        if status in ("OPEN", "DELEGATED", "ACCEPTED"):
            result = self._run(
                principal, tasks_app.CancelTask(task_id=task_id, reason_code=reason), req
            )
            key = "reply.task_cancelled"
        else:
            result = self._run(
                principal, tasks_app.RequestCancel(task_id=task_id, reason_code=reason), req
            )
            key = "reply.task_cancel_requested"
        return self._reply(session, inst, req, key, parsed, result, task_id=task_id)

    def _cmd_task_show(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        row = (
            session.execute(
                text(
                    "SELECT task_id, title, status, verification_status, risk, domain, "
                    "latest_progress FROM tasks_projection WHERE task_id = :t"
                ),
                {"t": parsed.args["task_id"]},
            )
            .mappings()
            .first()
        )
        if row is None:
            return ephemeral("command.error", "TASK_NOT_FOUND", parsed.args["task_id"])
        text_out = (
            f"{row['task_id']} — {row['title']} [{row['status']}] risk={row['risk']} "
            f"domain={row['domain']} verification={row['verification_status'] or '-'}"
        )
        return CommandResponse("ephemeral", text_out, "OK", str(row["task_id"]), parsed=parsed)

    def _cmd_task_list(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        limit = int(parsed.args.get("limit") or 20)
        rows = session.execute(
            text(
                "SELECT t.task_id, t.title, t.status FROM tasks_projection t "
                "JOIN channels c ON c.id = t.channel_id "
                "WHERE c.provider_instance_id = :p AND c.external_channel_id = :ch "
                "AND (CAST(:status AS text) IS NULL OR t.status = :status) "
                "ORDER BY t.updated_at DESC LIMIT :lim"
            ),
            {"p": inst.id, "ch": req.channel_id, "status": parsed.args.get("status"), "lim": limit},
        ).all()
        lines = [f"- {r[0]} [{r[2]}] {r[1]}" for r in rows] or ["(no tasks)"]
        return CommandResponse("ephemeral", "\n".join(lines), "OK", parsed=parsed)

    # approve ---------------------------------------------------------------------------------
    def _cmd_approve_request(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        a = parsed.args
        cmd = approvals_app.RequestApproval(
            subject_type="task",
            subject_id=a["task_id"],
            action=a["action"],
            risk=a.get("risk"),
            resource_scope={"scope": a["scope"]} if a.get("scope") else {},
        )
        result = self._run(principal, cmd, req)
        return self._reply(
            session,
            inst,
            req,
            "reply.approval_requested",
            parsed,
            result,
            approval_id=result.resource_id,
            action=a["action"],
            risk=result.data.get("risk", a.get("risk") or "?"),
        )

    def _decide(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
        decision: str,
    ) -> CommandResponse:
        cmd = approvals_app.DecideApproval(
            approval_id=parsed.args["approval_id"],
            decision=decision,
            reason_code=parsed.args.get("reason") or "REJECTED_BY_APPROVER",
        )
        result = self._run(principal, cmd, req, reauth_verified=False)
        return self._reply(
            session,
            inst,
            req,
            "reply.approval_decided",
            parsed,
            result,
            approval_id=parsed.args["approval_id"],
            decision=decision,
        )

    def _cmd_approve_grant(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        return self._decide(session, inst, req, principal, parsed, "APPROVE")

    def _cmd_approve_reject(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        return self._decide(session, inst, req, principal, parsed, "REJECT")

    def _cmd_approve_show(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        row = (
            session.execute(
                text(
                    "SELECT approval_id, subject_type, subject_id, action, risk, status, "
                    "expires_at FROM approval_grants WHERE approval_id = :a"
                ),
                {"a": parsed.args["approval_id"]},
            )
            .mappings()
            .first()
        )
        if row is None:
            return ephemeral("command.error", "APPROVAL_NOT_FOUND", parsed.args["approval_id"])
        return CommandResponse(
            "ephemeral",
            f"{row['approval_id']} {row['action']} on {row['subject_type']} {row['subject_id']} "
            f"[{row['status']}] risk={row['risk']} expires={row['expires_at'].isoformat()}",
            "OK",
            str(row["approval_id"]),
            parsed=parsed,
        )

    def _cmd_approve_list(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        rows = session.execute(
            text(
                "SELECT approval_id, action, status FROM approval_grants WHERE workspace_id = :ws "
                "AND (CAST(:s AS text) IS NULL OR status = :s) ORDER BY created_at DESC LIMIT :lim"
            ),
            {
                "ws": inst.workspace_id,
                "s": parsed.args.get("status"),
                "lim": int(parsed.args.get("limit") or 20),
            },
        ).all()
        return CommandResponse(
            "ephemeral",
            "\n".join(f"- {r[0]} {r[1]} [{r[2]}]" for r in rows) or "(no approvals)",
            "OK",
            parsed=parsed,
        )

    # verify ----------------------------------------------------------------------------------
    def _latest_run(self, session: Session, task_id: str) -> Any:
        return (
            session.execute(
                text(
                    "SELECT verification_id, status, result FROM verification_runs WHERE "
                    "target_type = 'task' AND target_id = :t ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": task_id},
            )
            .mappings()
            .first()
        )

    def _cmd_verify_assign(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        task_id = parsed.args["task_id"]
        verifier = (
            self._account_for_mention(session, inst, parsed.args["to"])
            if parsed.args.get("to")
            else principal.account_id
        )
        row = session.execute(
            text(
                "SELECT a.account_id FROM tasks_projection t JOIN accounts a ON a.id = "
                "t.assignee_account_id WHERE t.task_id = :t"
            ),
            {"t": task_id},
        ).first()
        if row is None:
            return ephemeral("command.error", "TASK_ASSIGNEE_UNKNOWN", task_id)
        implementer = str(row[0])
        cmd = verification_app.CreateVerificationRun(
            target_type="task",
            target_id=task_id,
            task_id=task_id,
            implementer_account_id=implementer,
            verifier_account_id=verifier,
            implementer_credential_fingerprint="sha256:"
            + hashlib.sha256(f"account:{implementer}".encode()).hexdigest(),
            verifier_credential_fingerprint="sha256:"
            + hashlib.sha256(f"account:{verifier}".encode()).hexdigest(),
            target_commit="0" * 40,
            effective_policy_hash="sha256:" + "0" * 64,
        )
        result = self._run(principal, cmd, req)
        self._run(
            principal,
            verification_app.AssignVerifier(verification_id=result.resource_id),
            req,
            ":assign",
        )
        return self._reply(
            session,
            inst,
            req,
            "reply.verification_assigned",
            parsed,
            result,
            verification_id=result.resource_id,
            verifier=parsed.args.get("to") or principal.account_id,
            task_id=task_id,
        )

    def _verdict(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
        result_code: str,
    ) -> CommandResponse:
        task_id = parsed.args["task_id"]
        run = self._latest_run(session, task_id)
        if run is None:
            return ephemeral("command.error", "VERIFICATION_NOT_FOUND", task_id)
        vid = str(run["verification_id"])
        verifier_uuid = session.execute(
            text("SELECT verifier_account_id FROM verification_runs WHERE verification_id = :v"),
            {"v": vid},
        ).scalar_one()
        is_verifier = str(verifier_uuid) == principal.account_uuid
        if is_verifier and run["status"] in ("PLANNED", "ASSIGNED", "RECHECK_ASSIGNED"):
            if run["status"] == "PLANNED":
                self._run(
                    principal, verification_app.AssignVerifier(verification_id=vid), req, ":assign"
                )
            self._run(
                principal, verification_app.StartVerification(verification_id=vid), req, ":start"
            )
        refs = parsed.args.get("evidence", [])
        if isinstance(refs, str):
            refs = [refs]
        findings = parsed.args.get("finding", [])
        if isinstance(findings, str):
            findings = [findings]
        report = {
            "result": result_code,
            "criteria_version": "v8.0",
            "tests": [
                {
                    "id": f"CMD-{i + 1}",
                    "result": "PASS" if result_code == "PASSED" else "FAIL",
                    "evidence_ref": r,
                }
                for i, r in enumerate(refs)
            ]
            or [{"id": "CMD-1", "result": "NOT_RUN", "evidence_ref": "none"}],
            "findings": [
                {"id": f"F-{i + 1}", "severity": "Medium", "summary": f}
                for i, f in enumerate(findings)
            ],
            "residual_risks": [],
        }
        if result_code == "BLOCKED":
            report["reason_code"] = parsed.args.get("reason") or "EXTERNAL_CONDITION"
        result = self._run(
            principal,
            verification_app.SubmitVerdict(verification_id=vid, result=result_code, report=report),
            req,
        )
        return self._reply(
            session,
            inst,
            req,
            "reply.verdict",
            parsed,
            result,
            verification_id=vid,
            verdict=result_code,
        )

    def _cmd_verify_pass(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        return self._verdict(session, inst, req, principal, parsed, "PASSED")

    def _cmd_verify_fail(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        return self._verdict(session, inst, req, principal, parsed, "FAILED")

    def _cmd_verify_block(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        return self._verdict(session, inst, req, principal, parsed, "BLOCKED")

    def _cmd_verify_show(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        run = self._latest_run(session, parsed.args["task_id"])
        if run is None:
            return ephemeral("command.error", "VERIFICATION_NOT_FOUND", parsed.args["task_id"])
        return CommandResponse(
            "ephemeral",
            f"{run['verification_id']} [{run['status']}] result={run['result'] or '-'}",
            "OK",
            str(run["verification_id"]),
            parsed=parsed,
        )

    # doc -------------------------------------------------------------------------------------
    def _cmd_doc_show(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        rows = session.execute(
            text(
                "SELECT v.document_id, v.version, v.status, v.sha256 FROM document_versions v JOIN "
                "documents d ON d.document_id = v.document_id "
                "WHERE d.source_id = :s ORDER BY v.version"
            ),
            {"s": parsed.args["subject_id"]},
        ).all()
        if not rows:
            return ephemeral("command.error", "DOCUMENT_NOT_FOUND", parsed.args["subject_id"])
        lines = [f"- {r[0]} v{r[1]} {r[2]} sha256={r[3][:12]}…" for r in rows]
        return CommandResponse("ephemeral", "\n".join(lines), "OK", str(rows[-1][0]), parsed=parsed)

    # notify ----------------------------------------------------------------------------------
    def _prefs(
        self, session: Session, principal: Principal, muted: bool | None, digest: bool | None
    ) -> None:
        session.execute(
            text(
                "INSERT INTO notification_preferences (account_id, muted, digest) VALUES (:a, "
                "COALESCE(:m, false), COALESCE(:d, false)) "
                "ON CONFLICT (account_id) DO UPDATE SET muted = COALESCE(:m, "
                "notification_preferences.muted), "
                "digest = COALESCE(:d, notification_preferences.digest), updated_at = now()"
            ),
            {"a": uuid.UUID(principal.account_uuid), "m": muted, "d": digest},
        )

    def _cmd_notify_mute(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        self._prefs(session, principal, True, None)
        return CommandResponse(
            "ephemeral", render("reply.notify", setting="muted"), "OK", parsed=parsed
        )

    def _cmd_notify_unmute(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        self._prefs(session, principal, False, None)
        return CommandResponse(
            "ephemeral", render("reply.notify", setting="unmuted"), "OK", parsed=parsed
        )

    def _cmd_notify_digest(
        self,
        session: Session,
        inst: prov.ProviderInstance,
        req: SlashRequest,
        principal: Principal,
        parsed: grammar.ParsedCommand,
    ) -> CommandResponse:
        on = (parsed.args.get("interval") or "hourly") != "off"
        self._prefs(session, principal, None, on)
        return CommandResponse(
            "ephemeral",
            render("reply.notify", setting=f"digest {'hourly' if on else 'off'}"),
            "OK",
            parsed=parsed,
        )


def route(runtime: Runtime, request: SlashRequest, clock: Clock | None = None) -> CommandResponse:
    return Router(runtime, clock).route(request)


__all__ = [
    "LINK_HANDLERS",
    "MESSAGES",
    "RESOURCE_HANDLERS",
    "CommandResponse",
    "Router",
    "SlashRequest",
    "route",
    "to_bus_principal",
]
