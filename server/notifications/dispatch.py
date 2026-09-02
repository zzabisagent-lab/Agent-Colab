"""Convenience entry points: run the rules engine for Events and drain the outbox."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore
from server.notifications.outbox import DrainResult, Provider, drain
from server.notifications.rules import NotificationEngine, NotificationRecord


def dispatch_events(
    session: Session, engine: NotificationEngine, events: Iterable[dict[str, Any]]
) -> list[NotificationRecord]:
    records: list[NotificationRecord] = []
    for event in events:
        records.extend(engine.on_event(session, event))
    return records


def deliver_pending(
    session: Session,
    provider: Provider,
    store: EventStore,
    clock: Clock,
    service_account_uuid: str,
    workspace_id: str,
) -> DrainResult:
    return drain(session, provider, store, clock, service_account_uuid, workspace_id)
