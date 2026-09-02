"""Interactive card actions (P2-12; development plan §7A.1, §7A.3, §7.5; spec §8.7).

Card buttons are conveniences: the server signs the ``context`` it embeds in every button
(HMAC-SHA256 over ``timestamp|nonce|sha256(subject/action)`` with a per-instance secret), and at
callback time it validates signature → 5-minute timestamp → body hash → one-time nonce, resolves
the principal from the Mattermost user's active ExternalIdentityLink, and executes the mapped
command through the same bus as REST/MCP (policy re-evaluated at callback time). A duplicate click
of the same button (same nonce) replays the original outcome through the idempotency key and never
produces a second side effect. Every rejection is audited with redacted metadata.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import approvals as approvals_app
from server.application import bus
from server.application import tasks as tasks_app
from server.application import verification as verification_app
from server.channels import contract
from server.channels.mattermost import provider as prov
from server.db.engine import session_scope
from server.domain.clock import Clock, SystemClock
from server.domain.defaults import CALLBACK_TIMESTAMP_TOLERANCE_S
from server.events.canonical import canonical_json
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError, Principal
from server.observability.audit import append_audit

ACTION_SECRET_ENV = "AGENT_COLAB_MATTERMOST_ACTION_SECRET"  # noqa: S105 - env var name  # nosec B105 - environment variable name, not a secret
BUTTON_LABELS: dict[str, str] = {
    "accept": "Accept",
    "submit": "Submit",
    "approve": "Approve",
    "reject": "Reject",
    "verify_pass": "Verify: pass",  # nosec B105 - button label
    "verify_fail": "Verify: fail",
    "cancel": "Cancel",
}
HUMAN_ONLY_RISKS = ("HIGH", "CRITICAL")

MESSAGES: dict[str, str] = {
    "action.unlinked": (
        "Link your Mattermost user to an Agent-Colab Account first: `/colab link start`."
    ),
    "action.submit_usage": (
        "Submission needs evidence per acceptance criterion. Use "
        '`/colab task submit {task_id} --evidence "<criteria_id>:<ref>"`.'
    ),
    "action.high_risk_web": (
        "Approval {approval_id} is {risk}: decide it in the web console after MFA "
        "re-authentication. Buttons cannot approve HIGH or CRITICAL actions."
    ),
    "action.done": "{label} recorded for {subject_id} (event {event_id}).",
    "action.replayed": "{label} for {subject_id} was already processed (event {event_id}).",
    "action.rejected": "{label} for {subject_id} rejected: {code}.",
    "action.no_verification": "No verification run is in progress for {task_id}.",
}


class ActionError(Exception):
    """Transport-neutral rejection of a callback (status + stable code)."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


def render(key: str, **fields: Any) -> str:
    try:
        return MESSAGES[key].format(**fields)
    except (KeyError, IndexError):
        return MESSAGES.get(key, key)


# --- signed button contexts ---------------------------------------------------------------------


@dataclass(frozen=True)
class ActionContext:
    subject_type: str
    subject_id: str
    action: str
    issued_at: int  # unix seconds
    nonce: str

    def body_sha256(self) -> str:
        body = {
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "action": self.action,
        }
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def signature(self, secret: bytes) -> str:
        return contract.sign(secret, self.issued_at, self.nonce, self.body_sha256())

    def as_button_context(self, secret: bytes) -> dict[str, Any]:
        return {
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "action": self.action,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "body_sha256": self.body_sha256(),
            "signature": self.signature(secret),
        }


def action_secret() -> bytes | None:
    """Per-instance signing secret (Secret reference resolved from the environment in Phase 2)."""
    value = os.environ.get(ACTION_SECRET_ENV)
    return value.encode("utf-8") if value else None


def sign_context(secret: bytes, ctx: ActionContext) -> str:
    return ctx.signature(secret)


def new_nonce() -> str:
    return uuid.uuid4().hex


def build_button_actions(
    secret: bytes,
    *,
    subject_type: str,
    subject_id: str,
    buttons: tuple[str, ...] | list[str],
    now: dt.datetime,
    callback_url: str = "/api/v1/providers/mattermost/actions",
) -> list[dict[str, Any]]:
    """Mattermost message-attachment actions with server-signed integration contexts."""
    issued = int(now.timestamp())
    actions: list[dict[str, Any]] = []
    for button in buttons:
        ctx = ActionContext(subject_type, subject_id, button, issued, new_nonce())
        actions.append(
            {
                "id": f"{subject_id}:{button}",
                "name": BUTTON_LABELS.get(button, button),
                "type": "button",
                "integration": {"url": callback_url, "context": ctx.as_button_context(secret)},
            }
        )
    return actions


def attach_button_contexts(
    props: dict[str, Any],
    *,
    subject_type: str,
    subject_id: str,
    now: dt.datetime,
    secret: bytes | None = None,
) -> dict[str, Any]:
    """Add signed button actions to card props (no-op without a signing secret)."""
    key = secret if secret is not None else action_secret()
    buttons = list(props.get("buttons", []))
    if key is None or not buttons:
        return props
    actions = build_button_actions(
        key, subject_type=subject_type, subject_id=subject_id, buttons=buttons, now=now
    )
    return {**props, "attachments": [{"actions": actions}]}


# --- callback handling --------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRequest:
    provider_instance_id: str
    user_id: str
    channel_id: str
    post_id: str
    context: dict[str, Any]
    trigger_id: str = ""


@dataclass(frozen=True)
class ActionResponse:
    ephemeral_text: str
    code: str = "OK"
    event_id: str | None = None
    resource_id: str | None = None
    replayed: bool = False
    executed: bool = False
    update: dict[str, Any] | None = None

    def as_mattermost(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ephemeral_text": self.ephemeral_text}
        if self.update is not None:
            out["update"] = self.update
        return out


def parse_context(raw: dict[str, Any]) -> tuple[ActionContext, str, str]:
    """Return (context, signature, claimed body hash) or raise ``ActionError`` (400)."""
    try:
        ctx = ActionContext(
            str(raw["subject_type"]),
            str(raw["subject_id"]),
            str(raw["action"]),
            int(raw["issued_at"]),
            str(raw["nonce"]),
        )
        return ctx, str(raw["signature"]), str(raw["body_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ActionError(400, "CALLBACK_CONTEXT_INVALID", str(exc)) from exc


def validate_context(
    ctx: ActionContext, signature: str, body_sha256: str, *, secret: bytes, now: dt.datetime
) -> None:
    """signature → timestamp → body hash (the nonce is consumed by the caller, last)."""
    if not hmac.compare_digest(signature, ctx.signature(secret)):
        raise ActionError(401, contract.CALLBACK_SIGNATURE_INVALID, "hmac")
    issued = dt.datetime.fromtimestamp(ctx.issued_at, tz=dt.UTC)
    if abs((now - issued).total_seconds()) > CALLBACK_TIMESTAMP_TOLERANCE_S:
        raise ActionError(403, contract.CALLBACK_TIMESTAMP_EXPIRED, "outside tolerance")
    if not hmac.compare_digest(body_sha256, ctx.body_sha256()):
        raise ActionError(401, contract.CALLBACK_BODY_HASH_MISMATCH)


@dataclass
class ActionHandler:
    runtime: Runtime
    clock: Clock = field(default_factory=SystemClock)
    secret: bytes | None = None

    def _secret(self) -> bytes:
        key = self.secret if self.secret is not None else action_secret()
        if key is None:
            raise ActionError(503, "ACTION_SECRET_UNCONFIGURED", ACTION_SECRET_ENV)
        return key

    def _audit(
        self,
        session: Session,
        inst: prov.ProviderInstance | None,
        req: ActionRequest,
        *,
        action: str,
        result: str,
        code: str | None,
        actor: Principal | None,
        subject: str = "-",
    ) -> None:
        append_audit(
            session,
            action=action,
            target_type="mattermost_action",
            target_id=subject,
            result=result,
            actor_label=actor.account_id if actor else f"mm:{req.user_id}",
            correlation_id=f"action:{req.trigger_id or req.post_id}",
            workspace_id=inst.workspace_id if inst else None,
            actor_account_id=uuid.UUID(actor.account_uuid) if actor else None,
            error_code=code,
            metadata={
                "provider_instance_id": req.provider_instance_id,
                "external_user_id": req.user_id,
                "post_id": req.post_id,
                "button": str(req.context.get("action", "?")),
            },
            clock=self.clock,
        )

    def handle(self, req: ActionRequest) -> ActionResponse:
        """Validate, resolve the principal, execute once; raise ``ActionError`` on rejection."""
        now = self.clock.now()
        with session_scope(self.runtime.session_factory) as session:
            inst = prov.load_instance(session, req.provider_instance_id)
            if inst is None or inst.status != "active":
                self._audit_autonomous(None, req, "action.rejected", "PROVIDER_INSTANCE_UNKNOWN")
                raise ActionError(403, "PROVIDER_INSTANCE_UNKNOWN", req.provider_instance_id)
            try:
                ctx, signature, body_sha256 = parse_context(req.context)
                validate_context(ctx, signature, body_sha256, secret=self._secret(), now=now)
            except ActionError as exc:
                self._audit_autonomous(inst, req, "action.rejected", exc.code)
                raise
            replay = not prov.consume_nonce(session, inst.id, ctx.nonce, self.clock)
            principal = self._principal(session, req)
            if principal is None:
                self._audit(
                    session,
                    inst,
                    req,
                    action="action.unlinked",
                    result="IGNORED",
                    code="EXTERNAL_IDENTITY_NOT_ACTIVE",
                    actor=None,
                    subject=ctx.subject_id,
                )
                return ActionResponse(render("action.unlinked"), "EXTERNAL_IDENTITY_NOT_ACTIVE")
            plan = self._plan(session, ctx)
        if plan.guidance is not None:
            return ActionResponse(plan.guidance, plan.code)
        assert plan.command is not None
        idem = f"action:{req.provider_instance_id}:{req.post_id}:{ctx.action}:{ctx.nonce}"
        label = BUTTON_LABELS.get(ctx.action, ctx.action)
        try:
            result = execute_command(
                self.runtime,
                principal,
                plan.command,
                idempotency_key=idem,
                correlation_id=f"action:{req.trigger_id or req.post_id}",
                extras={"reauth_verified": False},
            )
        except ApiError as exc:
            self._audit_autonomous(inst, req, "action.denied", exc.code, principal, ctx.subject_id)
            return ActionResponse(
                render("action.rejected", label=label, subject_id=ctx.subject_id, code=exc.code),
                exc.code,
            )
        key = "action.replayed" if (result.replayed or replay) else "action.done"
        return ActionResponse(
            render(key, label=label, subject_id=ctx.subject_id, event_id=result.event_id),
            "OK",
            event_id=result.event_id,
            resource_id=result.resource_id,
            replayed=result.replayed or replay,
            executed=True,
        )

    # -- helpers ---------------------------------------------------------------------------

    def _audit_autonomous(
        self,
        inst: prov.ProviderInstance | None,
        req: ActionRequest,
        action: str,
        code: str,
        actor: Principal | None = None,
        subject: str = "-",
    ) -> None:
        """Audit in its own transaction so the row survives the rejected callback."""
        with session_scope(self.runtime.session_factory) as session:
            self._audit(
                session,
                inst,
                req,
                action=action,
                result="REJECTED",
                code=code,
                actor=actor,
                subject=subject,
            )

    def _principal(self, session: Session, req: ActionRequest) -> Principal | None:
        service = sql_service(session, self.runtime.store_for(session), self.clock)
        try:
            return service.resolve_command_principal(req.provider_instance_id, req.user_id)
        except IdentityError:
            return None

    def _plan(self, session: Session, ctx: ActionContext) -> ActionPlan:
        action, subject = ctx.action, ctx.subject_id
        if action == "accept":
            return ActionPlan(tasks_app.AcceptTask(task_id=subject))
        if action == "cancel":
            return ActionPlan(tasks_app.RequestCancel(task_id=subject, reason_code="BUTTON"))
        if action == "submit":
            return ActionPlan(
                None, render("action.submit_usage", task_id=subject), "EVIDENCE_REQUIRED"
            )
        if action in ("approve", "reject"):
            row = session.execute(
                text("SELECT risk FROM approval_grants WHERE approval_id = :a"), {"a": subject}
            ).first()
            risk = str(row[0]) if row else "HIGH"
            if risk in HUMAN_ONLY_RISKS:
                return ActionPlan(
                    None,
                    render("action.high_risk_web", approval_id=subject, risk=risk),
                    "REAUTH_REQUIRED",
                )
            return ActionPlan(
                approvals_app.DecideApproval(
                    approval_id=subject,
                    decision="APPROVE" if action == "approve" else "REJECT",
                    reason_code="REJECTED_BY_APPROVER",
                )
            )
        if action in ("verify_pass", "verify_fail"):
            run = session.execute(
                text(
                    "SELECT verification_id FROM verification_runs WHERE task_id = :t "
                    "AND status IN ('ASSIGNED', 'RUNNING') ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": subject},
            ).first()
            if run is None:
                return ActionPlan(
                    None,
                    render("action.no_verification", task_id=subject),
                    "VERIFICATION_NOT_FOUND",
                )
            verdict = "PASSED" if action == "verify_pass" else "FAILED"
            report = {
                "result": verdict,
                "criteria_version": "v8.0",
                "tests": [
                    {
                        "id": "BUTTON",
                        "result": "PASS" if verdict == "PASSED" else "FAIL",
                        "evidence_ref": f"mattermost:button:{subject}",
                    }
                ],
                "findings": [],
                "residual_risks": [],
            }
            return ActionPlan(
                verification_app.SubmitVerdict(
                    verification_id=str(run[0]), result=verdict, report=report
                )
            )
        return ActionPlan(None, f"Unknown action {action}", "ACTION_UNKNOWN")


@dataclass(frozen=True)
class ActionPlan:
    command: bus.Command | None
    guidance: str | None = None
    code: str = "OK"


def handle_action(
    runtime: Runtime, req: ActionRequest, clock: Clock | None = None
) -> ActionResponse:
    return ActionHandler(runtime, clock or SystemClock()).handle(req)
