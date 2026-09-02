"""Task card and thread rendering (development plan §7A.3, §21.1 Renderer row; P2-03/P2-11).

Pure functions: Events and projection state in, post texts/props out. Delivery goes through the
outbox (``server/channels/outbox.py``). Rules:

- one root post per Task (the card), edited in place on every transition;
- one immutable thread reply per transition;
- progress posts are coalesced per Task in 10-second windows;
- bodies over 16,000 characters are stored as an Artifact and only linked;
- buttons are conveniences; the server re-checks permissions at callback time (P2-12).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from server.domain.defaults import (
    RENDERER_BODY_ARTIFACT_THRESHOLD_CHARS,
    RENDERER_PROGRESS_COALESCE_S,
)

STATUS_BADGE = {
    "OPEN": "🆕 OPEN",
    "DELEGATED": "📨 DELEGATED",
    "ACCEPTED": "🤝 ACCEPTED",
    "RUNNING": "🏃 RUNNING",
    "WAITING": "⏸️ WAITING",
    "IMPLEMENTED": "📦 IMPLEMENTED",
    "VERIFYING": "🔍 VERIFYING",
    "VERIFIED": "✅ VERIFIED",
    "COMPLETED": "🏁 COMPLETED",
    "CANCEL_REQUESTED": "🛑 CANCEL REQUESTED",
    "CANCELLED": "🚫 CANCELLED",
}

# buttons offered per status; the callback re-evaluates permissions (§7A.3)
BUTTONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    "DELEGATED": ("accept", "cancel"),
    "ACCEPTED": ("submit", "cancel"),
    "RUNNING": ("submit", "cancel"),
    "WAITING": ("submit", "cancel"),
    "IMPLEMENTED": ("verify_pass", "verify_fail", "cancel"),
    "VERIFYING": ("verify_pass", "verify_fail"),
    "OPEN": ("cancel",),
}

TRANSITION_MESSAGES: dict[str, str] = {
    "TASK_CREATED": "renderer.transition.created",
    "SUBTASK_CREATED": "renderer.transition.subtask_created",
    "TASK_DELEGATED": "renderer.transition.delegated",
    "TASK_REASSIGNED": "renderer.transition.reassigned",
    "TASK_ACCEPTED": "renderer.transition.accepted",
    "TASK_STARTED": "renderer.transition.started",
    "TASK_WAITING": "renderer.transition.waiting",
    "TASK_PROGRESS_REPORTED": "renderer.transition.progress",
    "TASK_JOIN_SATISFIED": "renderer.transition.join_satisfied",
    "TASK_CANCEL_REQUESTED": "renderer.transition.cancel_requested",
    "TASK_CANCELLED": "renderer.transition.cancelled",
    "IMPLEMENTATION_SUBMITTED": "renderer.transition.submitted",
    "TASK_VERIFICATION_STARTED": "renderer.transition.verifying",
    "VERIFICATION_PASSED": "renderer.transition.verification_passed",
    "VERIFICATION_FAILED": "renderer.transition.verification_failed",
    "VERIFICATION_BLOCKED": "renderer.transition.verification_blocked",
    "TASK_COMPLETED": "renderer.transition.completed",
    "APPROVAL_REQUESTED": "renderer.transition.approval_requested",
    "APPROVAL_GRANTED": "renderer.transition.approval_granted",
    "APPROVAL_REJECTED": "renderer.transition.approval_rejected",
    "DOCUMENT_DRAFTED": "renderer.transition.document_drafted",
    "DOCUMENT_FINALIZED": "renderer.transition.document_finalized",
    "DOCUMENT_ATTEMPT_FINALIZED": "renderer.transition.document_attempt",
    "ARTIFACT_REGISTERED": "renderer.transition.artifact_registered",
}

EN_DEFAULTS: dict[str, str] = {
    "renderer.transition.created": "Task created: {title}",
    "renderer.transition.subtask_created": "Sub-Task created: {title}",
    "renderer.transition.delegated": "Delegated to {assignee}",
    "renderer.transition.reassigned": "Reassigned to {assignee} ({reason_code})",
    "renderer.transition.accepted": "Accepted by {assignee}",
    "renderer.transition.started": "Work started",
    "renderer.transition.waiting": "Waiting: {reason_code}",
    "renderer.transition.progress": "Progress: {summary}",
    "renderer.transition.join_satisfied": "Join satisfied ({join_policy})",
    "renderer.transition.cancel_requested": "Cancel requested: {reason_code}",
    "renderer.transition.cancelled": "Cancelled: {reason_code}",
    "renderer.transition.submitted": "Implementation submitted (criteria rev. {criteria_revision})",
    "renderer.transition.verifying": "Verification started: {verification_id}",
    "renderer.transition.verification_passed": "Verification PASSED ({verification_id}, rev. {revision})",
    "renderer.transition.verification_failed": "Verification FAILED ({verification_id}, rev. {revision})",
    "renderer.transition.verification_blocked": "Verification BLOCKED ({verification_id}, rev. {revision})",
    "renderer.transition.completed": "Completed. Document: {document_id}",
    "renderer.transition.approval_requested": "Approval requested: {action} ({risk}) {approval_id}",
    "renderer.transition.approval_granted": "Approval granted: {approval_id}",
    "renderer.transition.approval_rejected": "Approval rejected: {approval_id}",
    "renderer.transition.document_drafted": "Document draft v{version} ({document_id})",
    "renderer.transition.document_finalized": "Document finalized v{version} ({document_id})",
    "renderer.transition.document_attempt": "Attempt document v{version} ({result})",
    "renderer.transition.artifact_registered": "Artifact registered: {artifact_id}",
    "renderer.card.title": "**{title}**",
    "renderer.card.status": "Status: {badge}",
    "renderer.card.assignee": "Assignee: {assignee}",
    "renderer.card.verification": "Verification: {verification_status}",
    "renderer.card.approvals": "Pending approvals: {approvals}",
    "renderer.card.progress": "Latest progress: {progress}",
    "renderer.card.links": "Artifacts/Documents: {links}",
    "renderer.card.subtasks": "Sub-Tasks: {subtasks}",
    "renderer.card.task_id": "Task `{task_id}` · risk {risk} · domain {domain}",
    "renderer.body.linked": "(body of {chars} characters stored as Artifact {artifact_id})",
    "renderer.progress.coalesced": "Progress ({count} updates): {summaries}",
}


def message(key: str, bundle: dict[str, str] | None = None, **kwargs: Any) -> str:
    template = (bundle or {}).get(key) or EN_DEFAULTS.get(key) or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


@dataclass(frozen=True)
class CardInput:
    task_id: str
    title: str
    status: str
    risk: str
    domain: str
    assignee: str | None = None
    verification_status: str | None = None
    pending_approvals: tuple[str, ...] = ()
    latest_progress: str | None = None
    links: tuple[str, ...] = ()
    subtasks: tuple[tuple[str, str], ...] = ()  # (task_id, status)
    join_policy: str | None = None
    actor_permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RenderedCard:
    text: str
    props: dict[str, Any]
    buttons: tuple[str, ...]


BUTTON_PERMISSION = {
    "accept": "task.accept",
    "submit": "task.submit",
    "approve": "approval.decide",
    "reject": "approval.decide",
    "verify_pass": "verification.submit",
    "verify_fail": "verification.submit",
    "cancel": "task.cancel",
}


def render_task_card(card: CardInput, bundle: dict[str, str] | None = None) -> RenderedCard:
    badge = STATUS_BADGE.get(card.status, card.status)
    lines = [
        message("renderer.card.title", bundle, title=card.title),
        message(
            "renderer.card.task_id",
            bundle,
            task_id=card.task_id,
            risk=card.risk,
            domain=card.domain,
        ),
        message("renderer.card.status", bundle, badge=badge),
        message("renderer.card.assignee", bundle, assignee=card.assignee or "—"),
        message(
            "renderer.card.verification",
            bundle,
            verification_status=card.verification_status or "—",
        ),
    ]
    if card.pending_approvals:
        lines.append(
            message("renderer.card.approvals", bundle, approvals=", ".join(card.pending_approvals))
        )
    if card.latest_progress:
        lines.append(message("renderer.card.progress", bundle, progress=card.latest_progress))
    if card.links:
        lines.append(message("renderer.card.links", bundle, links=", ".join(card.links)))
    if card.subtasks:
        joined = ", ".join(f"{t} ({s})" for t, s in card.subtasks)
        suffix = f" [{card.join_policy}]" if card.join_policy else ""
        lines.append(message("renderer.card.subtasks", bundle, subtasks=joined + suffix))
    if card.pending_approvals:
        offered = ("approve", "reject") + BUTTONS_BY_STATUS.get(card.status, ())
    else:
        offered = BUTTONS_BY_STATUS.get(card.status, ())
    buttons = tuple(b for b in offered if BUTTON_PERMISSION[b] in card.actor_permissions)
    props = {
        "agent_colab": {"subject_type": "task", "subject_id": card.task_id, "status": card.status},
        "buttons": list(buttons),
    }
    return RenderedCard("\n".join(lines), props, buttons)


def render_transition(
    event_type: str, payload: dict[str, Any], bundle: dict[str, str] | None = None
) -> str | None:
    """One immutable thread reply per transition; None for Event types that are not rendered."""
    key = TRANSITION_MESSAGES.get(event_type)
    if key is None:
        return None
    return message(
        key, bundle, **{k: v for k, v in payload.items() if isinstance(v, str | int | float)}
    )


@dataclass
class ProgressCoalescer:
    """Collect TASK_PROGRESS_REPORTED summaries per Task in ``window`` windows (default 10 s)."""

    window: dt.timedelta = dt.timedelta(seconds=RENDERER_PROGRESS_COALESCE_S)
    _windows: dict[str, tuple[dt.datetime, list[str]]] = field(default_factory=dict)

    def add(self, task_id: str, occurred_at: dt.datetime, summary: str) -> str | None:
        """Return the text to post now, or None when the summary joined an open window."""
        opened = self._windows.get(task_id)
        if opened is not None and occurred_at - opened[0] < self.window:
            opened[1].append(summary)
            return None
        flushed = self.flush(task_id)
        self._windows[task_id] = (occurred_at, [summary])
        return flushed

    def flush(self, task_id: str) -> str | None:
        opened = self._windows.pop(task_id, None)
        if opened is None:
            return None
        summaries = opened[1]
        if len(summaries) == 1:
            return message("renderer.transition.progress", None, summary=summaries[0])
        return message(
            "renderer.progress.coalesced",
            None,
            count=len(summaries),
            summaries=" | ".join(summaries),
        )

    def due(self, now: dt.datetime) -> list[tuple[str, str]]:
        """Windows that have expired at ``now``: (task_id, text) pairs, flushed."""
        out: list[tuple[str, str]] = []
        for task_id, (started, _) in list(self._windows.items()):
            if now - started >= self.window:
                text_out = self.flush(task_id)
                if text_out:
                    out.append((task_id, text_out))
        return out


@dataclass(frozen=True)
class BodyDecision:
    post_text: str
    artifact_body: str | None  # body to store as an Artifact when the post is only a link


def body_for_post(text_body: str, artifact_id_hint: str = "<artifact>") -> BodyDecision:
    """Bodies over the threshold are stored as an Artifact and only linked (§7A.3)."""
    if len(text_body) <= RENDERER_BODY_ARTIFACT_THRESHOLD_CHARS:
        return BodyDecision(text_body, None)
    link = message("renderer.body.linked", None, chars=len(text_body), artifact_id=artifact_id_hint)
    head = text_body[:500].rstrip()
    return BodyDecision(f"{head}…\n{link}", text_body)
