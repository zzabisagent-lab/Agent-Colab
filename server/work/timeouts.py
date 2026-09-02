"""Timeout sweep for the durable inbox (development plan §7B.1, §7D.3, §21.1; P1-12).

``sweep`` applies the pure timing model ``server.work.state.next_action`` to every open item at
the injected clock time and performs the resulting transitions:

- DELIVERED without ack for 60 s → re-queued for redelivery (at most 3 redeliveries), then
  ``EXPIRED`` with reason ``ACK_TIMEOUT``;
- any open item past its deadline → ``EXPIRED`` with reason ``DEADLINE``;
- an assignment acked but not accepted within 120 s → a ``REROUTE_REQUIRED`` outcome for the
  router (P3-14 performs the re-routing; the item itself is untouched here).

The sweep never sleeps; it is driven by a scheduler or by tests with a ``FixedClock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore
from server.work import inbox
from server.work.state import NextAction, next_action


@dataclass(frozen=True)
class SweepOutcome:
    work_item_id: str
    action: str  # REDELIVER | EXPIRE | REROUTE_REQUIRED | WAITING_REQUIRED
    reason: str


@dataclass
class SweepReport:
    requeued: list[SweepOutcome] = field(default_factory=list)
    expired: list[SweepOutcome] = field(default_factory=list)
    reroute_required: list[SweepOutcome] = field(default_factory=list)
    waiting_required: list[SweepOutcome] = field(default_factory=list)

    @property
    def outcomes(self) -> list[SweepOutcome]:
        return self.requeued + self.expired + self.reroute_required + self.waiting_required


def sweep(
    session: Session,
    store: EventStore,
    *,
    clock: Clock,
    actor_account_id: str,
    agent_id: str | None = None,
    reroute_counts: dict[str, int] | None = None,
) -> SweepReport:
    """Apply timeouts to open items (optionally one Agent's). Deterministic for a given clock."""
    now = clock.now()
    report = SweepReport()
    counts = reroute_counts or {}
    for item in inbox.open_items(session, agent_id=agent_id):
        decision = next_action(
            item.status,
            item.delivered_at,
            item.acked_at,
            now,
            item.delivery_count,
            kind=item.kind,
            accepted_at=item.accepted_at,
            reroute_count=counts.get(item.work_item_id, 0),
            deadline=item.deadline,
        )
        if decision.action is NextAction.NONE:
            continue
        if decision.action is NextAction.REDELIVER:
            inbox.requeue_for_redelivery(session, item.work_item_id, clock=clock)
            report.requeued.append(SweepOutcome(item.work_item_id, "REDELIVER", decision.reason))
        elif decision.action is NextAction.EXPIRE:
            reason = "DEADLINE" if decision.reason == "DEADLINE_EXCEEDED" else "ACK_TIMEOUT"
            inbox.expire(
                session,
                store,
                item.work_item_id,
                reason,
                actor_account_id=actor_account_id,
                clock=clock,
            )
            report.expired.append(SweepOutcome(item.work_item_id, "EXPIRE", reason))
        elif decision.action is NextAction.REROUTE:
            report.reroute_required.append(
                SweepOutcome(item.work_item_id, "REROUTE_REQUIRED", decision.reason)
            )
        elif decision.action is NextAction.WAITING:
            report.waiting_required.append(
                SweepOutcome(item.work_item_id, "WAITING_REQUIRED", decision.reason)
            )
    return report
