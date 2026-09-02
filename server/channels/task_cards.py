"""Task card delivery hook (P2-11; development plan §7A.3).

Runs inside the command transaction right after a Task command appended its Event (see
``server/api/dispatch.py``): it enqueues the card post/patch and exactly one thread reply per
transition through the channel outbox, coalesces progress replies per Task in 10-second windows
at the outbox level, stores over-long bodies as Artifacts, posts a link card for sub-Tasks in the
parent thread, and binds the root post to the Task when the card is delivered.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.actions import attach_button_contexts
from server.channels.outbox import Delivery, card_post_id, enqueue_delivery
from server.channels.renderer import (
    CardInput,
    body_for_post,
    render_task_card,
    render_transition,
)
from server.domain.defaults import RENDERER_PROGRESS_COALESCE_S

TASK_EVENT_TYPES = frozenset(
    {
        "TASK_CREATED",
        "SUBTASK_CREATED",
        "TASK_DELEGATED",
        "TASK_REASSIGNED",
        "TASK_ACCEPTED",
        "TASK_STARTED",
        "TASK_WAITING",
        "TASK_PROGRESS_REPORTED",
        "TASK_JOIN_SATISFIED",
        "TASK_CANCEL_REQUESTED",
        "TASK_CANCELLED",
        "IMPLEMENTATION_SUBMITTED",
        "TASK_VERIFICATION_STARTED",
        "VERIFICATION_PASSED",
        "VERIFICATION_FAILED",
        "VERIFICATION_BLOCKED",
        "TASK_COMPLETED",
    }
)


@dataclass(frozen=True)
class ChannelTarget:
    provider_instance_uuid: str
    provider_instance_id: str
    external_channel_id: str
    language: str | None


def channel_target(session: Session, channel_uuid: str) -> ChannelTarget | None:
    row = session.execute(
        text(
            "SELECT p.id, p.provider_instance_id, c.external_channel_id, c.language FROM channels "
            "c "
            "JOIN provider_instances p ON p.id = c.provider_instance_id "
            "WHERE c.id = :c AND c.external_channel_id IS NOT NULL AND p.provider = 'mattermost'"
        ),
        {"c": uuid.UUID(channel_uuid)},
    ).first()
    if row is None:
        return None
    return ChannelTarget(str(row[0]), str(row[1]), str(row[2]), row[3])


def _task_row(session: Session, task_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT task_id, title, status, risk, domain, verification_status, "
                "latest_progress, "
                "parent_task_id, channel_id, join_policy FROM tasks_projection WHERE task_id = :t"
            ),
            {"t": task_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _card_input(session: Session, row: dict[str, Any]) -> CardInput:
    assignee = session.execute(
        text(
            "SELECT a.account_id FROM tasks_projection t JOIN accounts a ON a.id = "
            "t.assignee_account_id "
            "WHERE t.task_id = :t"
        ),
        {"t": row["task_id"]},
    ).scalar()
    approvals = [
        str(r[0])
        for r in session.execute(
            text(
                "SELECT approval_id FROM approval_grants WHERE subject_type = 'task' AND "
                "subject_id = :t "
                "AND status = 'PENDING' ORDER BY created_at"
            ),
            {"t": row["task_id"]},
        ).all()
    ]
    links = [
        str(r[0])
        for r in session.execute(
            text(
                "SELECT artifact_id FROM artifact_links WHERE subject_type = 'task' AND subject_id "
                "= :t"
            ),
            {"t": row["task_id"]},
        ).all()
    ]
    docs = session.execute(
        text("SELECT document_id FROM documents WHERE source_type = 'task' AND source_id = :t"),
        {"t": row["task_id"]},
    ).all()
    links += [str(r[0]) for r in docs]
    subtasks = [
        (str(r[0]), str(r[1]))
        for r in session.execute(
            text(
                "SELECT task_id, status FROM tasks_projection WHERE parent_task_id = :t ORDER BY "
                "task_id"
            ),
            {"t": row["task_id"]},
        ).all()
    ]
    join = row.get("join_policy") or {}
    return CardInput(
        task_id=row["task_id"],
        title=row["title"],
        status=row["status"],
        risk=row["risk"],
        domain=row["domain"],
        assignee=str(assignee) if assignee else None,
        verification_status=row.get("verification_status"),
        pending_approvals=tuple(approvals),
        latest_progress=row.get("latest_progress"),
        links=tuple(links),
        subtasks=tuple(subtasks),
        join_policy=str(join.get("policy"))
        if isinstance(join, dict) and join.get("policy")
        else None,
    )


def _coalesce_progress(
    session: Session, task_id: str, destination: str, summary: str, now: dt.datetime
) -> bool:
    """Append to an unsent progress reply opened within the window; True when coalesced."""
    row = session.execute(
        text(
            "SELECT id, payload, created_at FROM delivery_outbox WHERE destination = :d "
            "AND status = 'pending' AND kind = 'mattermost.post' AND payload->>'progress_task' = "
            ":t "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE"
        ),
        {"d": destination, "t": task_id},
    ).first()
    if row is None:
        return False
    created_at: dt.datetime = row[2]
    if now - created_at >= dt.timedelta(seconds=RENDERER_PROGRESS_COALESCE_S):
        return False
    payload = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    summaries = [*payload.get("progress_summaries", []), summary]
    payload["progress_summaries"] = summaries
    payload["message"] = f"Progress ({len(summaries)} updates): " + " | ".join(summaries)
    session.execute(
        text("UPDATE delivery_outbox SET payload = CAST(:p AS jsonb) WHERE id = :i"),
        {"p": json.dumps(payload), "i": row[0]},
    )
    return True


def _store_long_body(
    session: Session, workspace_id: str, actor_uuid: str, task_id: str, body: str, event_id: str
) -> str:
    """Bodies over 16k characters become an Artifact linked to the Task; the post links it."""
    from server.artifacts.storage import ArtifactStorage

    storage = ArtifactStorage()
    data = body.encode("utf-8")
    stored = storage.write(
        workspace_id, f"{task_id}-{event_id[:12]}.md", "text/markdown", io.BytesIO(data)
    )
    artifact_id = "art-" + hashlib.sha256(f"{task_id}|{event_id}".encode()).hexdigest()[:16]
    session.execute(
        text(
            "INSERT INTO artifacts (id, artifact_id, workspace_id, creator_account_id, "
            "storage_uri, mime, "
            "size, sha256, source_event_id) VALUES (:id, :a, :ws, :c, :uri, 'text/markdown', "
            ":size, :sha, :ev) "
            "ON CONFLICT (artifact_id) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "a": artifact_id,
            "ws": uuid.UUID(workspace_id),
            "c": uuid.UUID(actor_uuid),
            "uri": stored.storage_uri,
            "size": len(data),
            "sha": stored.sha256,
            "ev": event_id,
        },
    )
    session.execute(
        text(
            "INSERT INTO artifact_links (artifact_id, subject_type, subject_id, relation, "
            "linked_by) "
            "VALUES (:a, 'task', :t, 'thread_body', :b) ON CONFLICT DO NOTHING"
        ),
        {"a": artifact_id, "t": task_id, "b": uuid.UUID(actor_uuid)},
    )
    return artifact_id


def render_task_event(
    session: Session,
    *,
    workspace_id: str,
    actor_uuid: str,
    event: dict[str, Any],
    now: dt.datetime,
    bundle: dict[str, str] | None = None,
) -> list[str]:
    """Enqueue the card and thread deliveries for one Task Event; returns the dedupe keys."""
    if event["type"] not in TASK_EVENT_TYPES:
        return []
    task_id = str(event.get("task_id") or event["aggregate_id"])
    row = _task_row(session, task_id)
    if row is None or not row.get("channel_id"):
        return []
    target = channel_target(session, str(row["channel_id"]))
    if target is None:
        return []
    destination = f"mattermost:{target.external_channel_id}"
    keys: list[str] = []
    card = render_task_card(_card_input(session, row), bundle)
    card_props = attach_button_contexts(
        card.props, subject_type="task", subject_id=task_id, now=now
    )
    root = card_post_id(session, target.provider_instance_id, "task", task_id)
    if root is None:
        # the card is created once; later transitions patch it in place
        key = f"card:{target.provider_instance_id}:{task_id}"
        if enqueue_delivery(
            session,
            workspace_id=workspace_id,
            source_event_id=event["event_id"],
            delivery=Delivery(
                "mattermost.post",
                destination,
                {"message": card.text, "props": card_props},
                key,
                subject_type="task",
                subject_id=task_id,
                role="card",
            ),
            provider_instance_id=target.provider_instance_id,
            external_channel_id=target.external_channel_id,
            now=now,
        ):
            keys.append(key)
    else:
        suffix = f"{event['aggregate_seq']}:{event['event_id'][-8:]}"
        key = f"card:{target.provider_instance_id}:{task_id}:{suffix}"
        if enqueue_delivery(
            session,
            workspace_id=workspace_id,
            source_event_id=event["event_id"],
            delivery=Delivery(
                "mattermost.patch",
                destination,
                {"post_id": root, "message": card.text, "props": card_props},
                key,
            ),
            provider_instance_id=target.provider_instance_id,
            external_channel_id=target.external_channel_id,
            now=now,
        ):
            keys.append(key)
    if event["type"] in ("TASK_CREATED", "SUBTASK_CREATED"):
        if event["type"] == "SUBTASK_CREATED" and row.get("parent_task_id"):
            parent_root = card_post_id(
                session, target.provider_instance_id, "task", str(row["parent_task_id"])
            )
            link_key = f"linkcard:{target.provider_instance_id}:{task_id}"
            if enqueue_delivery(
                session,
                workspace_id=workspace_id,
                source_event_id=event["event_id"],
                delivery=Delivery(
                    "mattermost.post",
                    destination,
                    {
                        "message": f"Sub-Task `{task_id}`: {row['title']} ({row['status']})",
                        "root_id": parent_root,
                        "props": {
                            "agent_colab": {
                                "subject_type": "task",
                                "subject_id": task_id,
                                "link_card": True,
                            }
                        },
                    },
                    link_key,
                    subject_type="task",
                    subject_id=task_id,
                    role="link_card",
                    root_post_id=parent_root,
                ),
                provider_instance_id=target.provider_instance_id,
                external_channel_id=target.external_channel_id,
                now=now,
            ):
                keys.append(link_key)
        return keys  # the card itself is the creation log entry
    reply = render_transition(event["type"], event.get("payload", {}), bundle)
    if reply is None:
        return keys
    decision = body_for_post(reply)
    message = decision.post_text
    if decision.artifact_body is not None:
        artifact_id = _store_long_body(
            session, workspace_id, actor_uuid, task_id, decision.artifact_body, event["event_id"]
        )
        message = body_for_post(reply, artifact_id).post_text
    if event["type"] == "TASK_PROGRESS_REPORTED":
        summary = str(event.get("payload", {}).get("summary", ""))
        if decision.artifact_body is None and _coalesce_progress(
            session, task_id, destination, summary, now
        ):
            return keys
        payload: dict[str, Any] = {
            "message": message,
            "root_id": root,
            "progress_task": task_id,
            "progress_summaries": [summary] if decision.artifact_body is None else [],
        }
    else:
        payload = {"message": message, "root_id": root}
    reply_key = f"reply:{event['event_id']}"
    if enqueue_delivery(
        session,
        workspace_id=workspace_id,
        source_event_id=event["event_id"],
        delivery=Delivery(
            "mattermost.post",
            destination,
            payload,
            reply_key,
            subject_type="task",
            subject_id=task_id,
            role="reply",
            root_post_id=root,
        ),
        provider_instance_id=target.provider_instance_id,
        external_channel_id=target.external_channel_id,
        now=now,
    ):
        keys.append(reply_key)
    return keys


def bind_delivered_cards(session: Session) -> int:
    """Create thread bindings for delivered cards that are not bound yet; returns the count."""
    rows = session.execute(
        text(
            "SELECT cp.provider_instance_id, cp.post_id, cp.external_channel_id, cp.subject_type, "
            "cp.subject_id "
            "FROM channel_posts cp WHERE cp.role = 'card' AND cp.status = 'sent' AND cp.post_id IS "
            "NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM thread_bindings tb JOIN provider_instances p ON p.id = "
            "tb.provider_instance_id "
            "WHERE p.provider_instance_id = cp.provider_instance_id AND tb.subject_type = "
            "cp.subject_type "
            "AND tb.subject_id = cp.subject_id)"
        )
    ).all()
    n = 0
    for pi_text, post_id, ext, st, sid in rows:
        session.execute(
            text(
                "INSERT INTO thread_bindings (provider_instance_id, root_post_id, "
                "external_channel_id, subject_type, subject_id) "
                "SELECT id, :post, :ext, :st, :sid FROM provider_instances WHERE "
                "provider_instance_id = :pi "
                "ON CONFLICT DO NOTHING"
            ),
            {"post": post_id, "ext": ext, "st": st, "sid": sid, "pi": pi_text},
        )
        n += 1
    return n


def after_command(
    session: Session, *, workspace_id: str, actor_uuid: str, event_id: str, now: dt.datetime
) -> list[str]:
    """Dispatch hook: render the Event a command just appended (same transaction)."""
    from server.events.postgres_store import PostgresEventStore

    event = PostgresEventStore(session).get(event_id)
    if event is None:
        return []
    return render_task_event(
        session, workspace_id=workspace_id, actor_uuid=actor_uuid, event=event, now=now
    )
