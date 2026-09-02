"""Delivery channel interface (development plan §7B.2).

Adapters advertise ``delivery_modes`` (push | pull). The server prefers push and falls back to
pull; the durable inbox (``server.work.inbox``) is the source of truth for both. Phase 1 ships the
protocol and the pull channel; the webhook push (P3-11), MCP long-poll transport (P3-10), and
Mattermost bot delivery (P3-12) implement ``DeliveryChannel`` in Phase 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore
from server.work import inbox


@dataclass(frozen=True)
class DeliveryReceipt:
    work_item_id: str
    delivery_no: int
    accepted: bool
    rejection_code: str | None = None


class DeliveryChannel(Protocol):
    """One delivery mode for one Agent."""

    mode: str  # "push" | "pull"

    def deliver(
        self, session: Session, store: EventStore, items: Sequence[inbox.WorkItem], *, clock: Clock
    ) -> list[DeliveryReceipt]: ...


class PullInbox:
    """Pull mode: the Agent polls; ``deliver`` is a no-op because ``inbox.poll`` delivers."""

    mode = "pull"

    def deliver(
        self, session: Session, store: EventStore, items: Sequence[inbox.WorkItem], *, clock: Clock
    ) -> list[DeliveryReceipt]:
        return [DeliveryReceipt(i.work_item_id, i.delivery_count, True) for i in items]
