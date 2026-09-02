"""Structured work messages for the Mattermost bot adapter (development plan §7B.2; P3-12).

Delivery: a work item becomes one post in the Task thread — ``@bot`` mention plus a
``colab.work-item.v1`` JSON code block — enqueued through the channel outbox (same transaction
as the DELIVERED transition, at-least-once on the wire, exactly once per ``dedupe_key``).

Intake: ``BotReplyIntake`` runs as a post hook on the Mattermost WebSocket path. A thread reply by
a *linked bot Agent* that carries a ``colab.work-result.v1`` JSON code block becomes exactly one
``WorkResult`` command; a ``/colab`` command in the reply goes to the Command Router; anything
malformed yields an ephemeral error with zero side effects (audited). Replies by unlinked users
are never interpreted.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.outbox import Delivery, enqueue_delivery
from server.channels.telegram.bridge import MattermostPostView
from server.domain.clock import Clock
from server.observability.audit import append_audit
from server.work import inbox
from server.work.state import WorkItemError

PostHook = Callable[[Session, Clock, MattermostPostView], bool]
POST_HOOKS: list[PostHook] = []
ROLE = "work_message"
_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def register_post_hook(hook: PostHook) -> None:
    if hook not in POST_HOOKS:
        POST_HOOKS.append(hook)


def unregister_post_hook(hook: PostHook) -> None:
    if hook in POST_HOOKS:
        POST_HOOKS.remove(hook)


def run_post_hooks(session: Session, clock: Clock, view: MattermostPostView) -> bool:
    """True when a hook consumed the post (it is then not relayed as chat)."""
    return any(hook(session, clock, view) for hook in list(POST_HOOKS))


# ----------------------------------------------------------------------------- delivery


def render_work_message(envelope: dict[str, Any], bot_username: str) -> str:
    body = json.dumps(envelope, indent=2, sort_keys=True)
    return (
        f"@{bot_username} work item `{envelope['work_item_id']}` ({envelope['kind']})\n"
        f"```json\n{body}\n```\n"
        "Reply in this thread with a `colab.work-result.v1` JSON code block or a `/colab` command."
    )


def dedupe_key_for(provider_instance_id: str, work_item_id: str, delivery_no: int) -> str:
    return f"workmsg:{provider_instance_id}:{work_item_id}:{delivery_no}"


def enqueue_work_message(
    session: Session,
    *,
    workspace_id: str,
    envelope: dict[str, Any],
    delivery_no: int,
    provider_instance_id: str,
    external_channel_id: str,
    root_post_id: str | None,
    bot_username: str,
    now: Any,
    source_event_id: str | None = None,
) -> str | None:
    """Enqueue the structured work message in the Task thread; None when already enqueued."""
    work_item_id = str(envelope["work_item_id"])
    return enqueue_delivery(
        session,
        workspace_id=workspace_id,
        source_event_id=source_event_id,
        delivery=Delivery(
            "mattermost.post",
            f"mattermost:{external_channel_id}",
            {
                "message": render_work_message(envelope, bot_username),
                "root_id": root_post_id,
                "props": {
                    "agent_colab": {
                        "subject_type": "work_item",
                        "subject_id": work_item_id,
                        "work_message": True,
                        "delivery_no": delivery_no,
                    }
                },
            },
            dedupe_key_for(provider_instance_id, work_item_id, delivery_no),
            subject_type="work_item",
            subject_id=work_item_id,
            role=ROLE,
            root_post_id=root_post_id,
        ),
        provider_instance_id=provider_instance_id,
        external_channel_id=external_channel_id,
        now=now,
    )


def thread_of_work_message(
    session: Session, provider_instance_id: str, work_item_id: str
) -> tuple[str | None, str | None]:
    """(root_post_id, post_id) of the latest work message for the item, if any."""
    row = session.execute(
        text(
            "SELECT root_post_id, post_id FROM channel_posts WHERE provider_instance_id = :pi "
            "AND subject_type = 'work_item' AND subject_id = :w AND role = :r "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"pi": provider_instance_id, "w": work_item_id, "r": ROLE},
    ).first()
    return (None, None) if row is None else (row[0], row[1])


# ------------------------------------------------------------------------------- intake


@dataclass(frozen=True)
class ReplyOutcome:
    handled: bool
    code: str = "NOT_A_WORK_REPLY"
    work_item_id: str | None = None
    event_id: str | None = None


def extract_json_block(message: str) -> dict[str, Any] | None:
    """The first JSON code block as an object; ``None`` if absent; raises ValueError if broken."""
    match = _JSON_BLOCK.search(message)
    if match is None:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("JSON block is not an object")
    return data


@dataclass
class BotReplyIntake:
    """Post hook turning bot thread replies into work results / commands (zero side effects
    for malformed replies)."""

    runtime: Any  # server.api.dispatch.Runtime
    outcomes: list[ReplyOutcome] = field(default_factory=list)

    def __call__(self, session: Session, clock: Clock, view: MattermostPostView) -> bool:
        outcome = self.handle(session, clock, view)
        self.outcomes.append(outcome)
        return outcome.handled

    # -- helpers ------------------------------------------------------------------------
    def _agent_for_poster(self, session: Session, view: MattermostPostView) -> Any | None:
        from server.agents.push_common import load_agent
        from server.identity.external_commands import try_resolve_external_principal

        principal = try_resolve_external_principal(session, view.provider_instance_id, view.user_id)
        if principal is None:
            return None
        # the link resolver labels principals "human"; the agents row decides Agent-ness
        row = session.execute(
            text("SELECT agent_id FROM agents WHERE account_id = :a"),
            {"a": uuid.UUID(principal.account_uuid)},
        ).first()
        return None if row is None else load_agent(session, str(row[0]))

    def _ephemeral_error(
        self, session: Session, clock: Clock, view: MattermostPostView, agent: Any, code: str
    ) -> None:
        now = clock.now()
        enqueue_delivery(
            session,
            workspace_id=agent.workspace_uuid,
            source_event_id=None,
            delivery=Delivery(
                "mattermost.ephemeral",
                f"mattermost:{view.channel_ext_id}",
                {
                    "ephemeral": True,
                    "user_id": view.user_id,
                    "message": f"{code}: the reply was not accepted; no changes were made.",
                    "root_id": view.root_id,
                },
                f"botreply-error:{view.post_id}",
            ),
            provider_instance_id=view.provider_instance_id,
            external_channel_id=view.channel_ext_id,
            now=now,
        )
        append_audit(
            session,
            action="work.bot_reply_rejected",
            target_type="post",
            target_id=view.post_id,
            result="REJECTED",
            actor_label=agent.agent_id,
            correlation_id=f"botreply:{view.post_id}",
            workspace_id=uuid.UUID(agent.workspace_uuid),
            actor_account_id=uuid.UUID(agent.account_uuid),
            error_code=code,
            metadata={"root_id": view.root_id},
            clock=clock,
        )

    def handle(self, session: Session, clock: Clock, view: MattermostPostView) -> ReplyOutcome:
        if not view.root_id:
            return ReplyOutcome(False)
        agent = self._agent_for_poster(session, view)
        if agent is None or agent.adapter_type != "mattermost_bot":
            return ReplyOutcome(False)
        stripped = view.message.strip()
        if stripped.startswith("/colab "):
            return self._route_command(session, clock, view, agent, stripped)
        try:
            doc = extract_json_block(view.message)
        except ValueError:
            self._ephemeral_error(session, clock, view, agent, "WORK_RESULT_MALFORMED")
            return ReplyOutcome(True, "WORK_RESULT_MALFORMED")
        if doc is None:
            return ReplyOutcome(False)  # ordinary chat by the bot
        if doc.get("schema_id") != "colab.work-result.v1":
            self._ephemeral_error(session, clock, view, agent, "WORK_RESULT_SCHEMA_INVALID")
            return ReplyOutcome(True, "WORK_RESULT_SCHEMA_INVALID")
        work_item_id = str(doc.get("work_item_id", ""))
        root, _post = thread_of_work_message(session, view.provider_instance_id, work_item_id)
        if root is None or root != view.root_id:
            self._ephemeral_error(session, clock, view, agent, "WORK_MESSAGE_THREAD_MISMATCH")
            return ReplyOutcome(True, "WORK_MESSAGE_THREAD_MISMATCH", work_item_id)
        return self._submit_result(session, clock, view, agent, work_item_id, doc)

    def _submit_result(
        self,
        session: Session,
        clock: Clock,
        view: MattermostPostView,
        agent: Any,
        work_item_id: str,
        doc: dict[str, Any],
    ) -> ReplyOutcome:
        from server.application import bus
        from server.application.work import WorkResult

        ctx = bus.CommandContext(
            session=session,
            store=self.runtime.store_for(session),
            authorizer=self.runtime.authorizer,
            clock=clock,
            principal=bus.Principal(
                account_id=agent.account_id,
                account_uuid=agent.account_uuid,
                account_type="agent",
                credential_fingerprint="external_link:mattermost",
                agent_id=agent.agent_id,
            ),
            workspace_id=agent.workspace_uuid,
            correlation_id=f"botreply:{view.post_id}",
            idempotency_key=f"botreply:{view.post_id}",
        )
        try:
            result = bus.execute(WorkResult(work_item_id=work_item_id, result=doc), ctx)
        except bus.CommandError as exc:
            self._ephemeral_error(session, clock, view, agent, exc.code)
            return ReplyOutcome(True, exc.code, work_item_id)
        code = str(result.data.get("code", "RESULT_ACCEPTED")) if result.data else "RESULT_ACCEPTED"
        return ReplyOutcome(True, code, work_item_id, result.event_id or None)

    def _route_command(
        self, session: Session, clock: Clock, view: MattermostPostView, agent: Any, text_in: str
    ) -> ReplyOutcome:
        from server.channels.router import SlashRequest, route

        response = route(
            self.runtime,
            SlashRequest(
                provider_instance_id=view.provider_instance_id,
                team_id="",
                channel_id=view.channel_ext_id,
                user_id=view.user_id,
                user_name=view.user_label,
                command="/colab",
                text=text_in.removeprefix("/colab ").strip(),
                trigger_id=f"botreply-{view.post_id}",
                post_id=view.post_id,
                root_id=view.root_id,
            ),
            clock,
        )
        if response.response_type == "ephemeral" and response.code != "OK":
            self._ephemeral_error(session, clock, view, agent, response.code)
        return ReplyOutcome(True, response.code, event_id=response.event_id)


# --------------------------------------------------------------- bot delivery channel


def deliver_to_bot(
    session: Session,
    store: Any,
    item: inbox.WorkItem,
    *,
    agent: Any,
    clock: Clock,
    actor_account_id: str,
) -> tuple[int, bool]:
    """Mark one QUEUED item delivered and enqueue its work message in the Task thread.

    Returns ``(delivery_no, enqueued)``. Items that carry secret handles are rejected
    (``CAPABILITY_UNSUPPORTED``) because the bot adapter advertises ``secret_handles:
    unsupported``; no message is posted for them.
    """
    from server.agents.push_common import mark_delivered, view_of
    from server.channels.outbox import card_post_id
    from server.channels.task_cards import channel_target

    if item.secret_handles:
        _item, delivery_no, _changed = mark_delivered(
            session,
            store,
            item.work_item_id,
            actor_account_id=actor_account_id,
            clock=clock,
            detail={"transport": "mattermost_bot", "rejection_code": "CAPABILITY_UNSUPPORTED"},
        )
        inbox.reject(
            session,
            store,
            item.work_item_id,
            item.agent_id,
            "CAPABILITY_UNSUPPORTED",
            actor_account_id=agent.account_uuid,
            clock=clock,
        )
        return delivery_no, False
    target = None
    if item.task_id:
        row = session.execute(
            text("SELECT channel_id FROM tasks_projection WHERE task_id = :t"), {"t": item.task_id}
        ).first()
        if row is not None and row[0] is not None:
            target = channel_target(session, str(row[0]))
    if target is None:
        raise WorkItemError("WORK_MESSAGE_NO_CHANNEL", item.work_item_id)
    _item, delivery_no, _changed = mark_delivered(
        session,
        store,
        item.work_item_id,
        actor_account_id=actor_account_id,
        clock=clock,
        detail={"transport": "mattermost_bot"},
    )
    root = card_post_id(session, target.provider_instance_id, "task", str(item.task_id))
    from server.agents.adapters.webhook import envelope_for

    envelope = envelope_for(view_of(item, delivery_no))
    bot_username = str(agent.endpoint.get("bot_username") or agent.agent_id)
    enqueued = enqueue_work_message(
        session,
        workspace_id=item.workspace_id,
        envelope=envelope,
        delivery_no=delivery_no,
        provider_instance_id=target.provider_instance_id,
        external_channel_id=target.external_channel_id,
        root_post_id=root,
        bot_username=bot_username,
        now=clock.now(),
    )
    return delivery_no, enqueued is not None
