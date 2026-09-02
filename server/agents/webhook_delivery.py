"""Webhook push delivery through the transactional outbox (development plan §7B.2; P3-11).

``WebhookDeliveryChannel.deliver`` enqueues one ``webhook.deliver`` outbox row per QUEUED work
item *generation* (``delivery_count + 1``) in the caller's transaction. ``drain_webhooks`` sends
due rows with a fresh signature per attempt (the 5-minute timestamp window forbids reusing a
signed body), retries endpoint failures with the outbox backoff (1, 5, 25, 125, 625 s; dead after
5 attempts) and, on ``202`` + receipt, marks the item DELIVERED exactly once for that generation.
A retry after a successful send finds the ``delivery`` receipt and produces no second side
effect (V-P3-06, CS-09). Rejections in the receipt become ``WORK_ITEM_REJECTED``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from server.agents.adapters.contract import Adapter, AdapterError, adapter_for
from server.agents.push_common import (
    delivery_receipt_for,
    load_agent,
    mark_delivered,
    view_of,
)
from server.channels.outbox import ChannelDrainResult, Delivery, drain_channels, enqueue_delivery
from server.domain.clock import Clock
from server.events.store import EventStore
from server.work import inbox
from server.work.push import DeliveryReceipt
from server.work.state import WorkItemState

KIND = "webhook.deliver"
AdapterFactory = Callable[[str, dict[str, Any]], Adapter]  # (agent_id, endpoint) → adapter


def default_adapter_factory(agent_id: str, endpoint: dict[str, Any]) -> Adapter:
    return adapter_for("webhook", {**endpoint, "agent_id": agent_id})


def dedupe_key_for(work_item_id: str, delivery_no: int) -> str:
    return f"webhook:{work_item_id}:{delivery_no}"


@dataclass
class WebhookDeliveryChannel:
    """Push channel for webhook Agents: one outbox row per QUEUED item generation."""

    mode = "push"
    actor_account_id: str  # service Account that performs deliveries

    def deliver(
        self, session: Session, store: EventStore, items: Sequence[inbox.WorkItem], *, clock: Clock
    ) -> list[DeliveryReceipt]:
        out: list[DeliveryReceipt] = []
        now = clock.now()
        for item in items:
            if item.status is not WorkItemState.QUEUED:
                continue
            generation = item.delivery_count + 1
            enqueue_delivery(
                session,
                workspace_id=item.workspace_id,
                source_event_id=None,
                delivery=Delivery(
                    KIND,
                    f"webhook:{item.agent_id}",
                    {"work_item_id": item.work_item_id, "delivery_no": generation},
                    dedupe_key_for(item.work_item_id, generation),
                ),
                provider_instance_id="webhook",
                external_channel_id=item.agent_id,
                now=now,
            )
            out.append(DeliveryReceipt(item.work_item_id, generation, True))
        return out


@dataclass
class WebhookOutboxProvider:
    """Outbox provider for ``webhook:`` destinations; bound to one drain transaction."""

    session: Session
    store: EventStore
    clock: Clock
    actor_account_id: str
    adapter_factory: AdapterFactory = default_adapter_factory
    adapters: dict[str, Adapter] = field(default_factory=dict)
    sends: int = 0

    def _adapter(self, agent_id: str) -> Adapter:
        if agent_id not in self.adapters:
            agent = load_agent(self.session, agent_id)
            if agent is None or agent.adapter_type != "webhook":
                raise AdapterError("ADAPTER_UNREACHABLE", f"{agent_id} is not a webhook Agent")
            if agent.status in ("revoked", "suspended"):
                raise AdapterError("ADAPTER_AUTH_FAILED", f"{agent_id} is {agent.status}")
            endpoint = {**agent.endpoint, "credential_ref": agent.credential_ref}
            self.adapters[agent_id] = self.adapter_factory(agent_id, endpoint)
        return self.adapters[agent_id]

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        work_item_id = str(payload["work_item_id"])
        delivery_no = int(payload["delivery_no"])
        existing = delivery_receipt_for(self.session, work_item_id, delivery_no)
        if existing is not None:  # already sent for this generation: no second side effect
            return str((existing[1] or {}).get("receipt_id") or existing[0])
        item = inbox.load(self.session, work_item_id)
        if item.status is not WorkItemState.QUEUED or item.delivery_count + 1 != delivery_no:
            return f"stale:{work_item_id}:{delivery_no}"  # superseded generation; nothing to send
        agent_id = destination.split(":", 1)[1]
        adapter = self._adapter(agent_id)
        try:
            receipt = adapter.deliver(view_of(item, delivery_no))
        except AdapterError:
            raise
        except Exception as exc:  # pragma: no cover - adapters normalize their own errors
            raise adapter.normalize_error(exc) from exc
        self.sends += 1
        detail = {
            "transport": "webhook",
            "receipt_id": receipt.receipt_id,
            "accepted_at": receipt.accepted_at.isoformat() if receipt.accepted_at else None,
            "rejection_code": receipt.rejection_code,
        }
        mark_delivered(
            self.session,
            self.store,
            work_item_id,
            actor_account_id=self.actor_account_id,
            clock=self.clock,
            detail=detail,
        )
        if receipt.rejection_code:
            agent = load_agent(self.session, agent_id)
            inbox.reject(
                self.session,
                self.store,
                work_item_id,
                agent_id,
                receipt.rejection_code,
                actor_account_id=agent.account_uuid if agent else self.actor_account_id,
                clock=self.clock,
            )
        return str(receipt.receipt_id or f"receipt:{work_item_id}:{delivery_no}")


def drain_webhooks(
    session: Session,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    *,
    actor_account_id: str,
    adapter_factory: AdapterFactory = default_adapter_factory,
    batch: int = 100,
) -> ChannelDrainResult:
    """Send due ``webhook.deliver`` rows; endpoint failures only touch the outbox row."""
    provider = WebhookOutboxProvider(session, store, clock, actor_account_id, adapter_factory)
    return drain_channels(
        session,
        {"webhook": provider},
        clock,
        workspace_id,
        batch=batch,
        kinds_prefix=("webhook.",),
    )


def next_backoff(attempts: int) -> dt.timedelta:
    from server.channels.outbox import BACKOFF_S

    return dt.timedelta(seconds=BACKOFF_S[min(max(attempts - 1, 0), len(BACKOFF_S) - 1)])
