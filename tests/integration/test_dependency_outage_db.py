"""V-P7-05: each provider except the database fails for 10 minutes and recovers.

Core writes keep succeeding throughout, the outbox rows the outage produced are preserved rather
than dropped or dead-lettered, the dashboard reports the dependency as failing within the 60 s
probe window, and after recovery the drain delivers each row exactly once.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels.outbox import Delivery, drain_channels, enqueue_delivery, requeue_dead
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.ops import dashboard, probes
from tests.integration.phase4_admin_seed import T0, Seed, seed

pytestmark = pytest.mark.db
OUTAGE = dt.timedelta(minutes=10)
# every dependency the §21.1 profile names except the database itself; postgres has its own Test
OUTAGE_PROBES = ("mattermost", "storage", "secret_provider", "telegram", "smtp")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "outage")


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AGENT_COLAB_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("AGENT_COLAB_MATTERMOST_URL", raising=False)
    yield
    for name in probes.PROBE_NAMES:
        probes.set_prober(name, None)


class FlakyProvider:
    """A channel provider that fails while `down`, then delivers idempotently per dedupe key."""

    prefix = "mattermost"

    def __init__(self) -> None:
        self.down = True
        self.delivered: dict[str, str] = {}
        self.attempts = 0

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        self.attempts += 1
        if self.down:
            raise RuntimeError("provider unreachable (simulated outage)")
        key = str(payload["dedupe_key"])
        self.delivered.setdefault(key, f"post-{len(self.delivered) + 1}")
        return self.delivered[key]


def _channel(engine: Engine, sd: Seed) -> tuple[uuid.UUID, str]:
    channel, pi = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider,"
                " base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team')"
            ),
            {"i": pi, "p": f"mm:outage:{pi.hex[:8]}", "w": sd.ws},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, :p, :e, 'work', 'outage')"
            ),
            {
                "i": channel,
                "c": f"chan-outage-{pi.hex[:6]}",
                "w": sd.ws,
                "p": pi,
                "e": f"ext-outage-{pi.hex[:6]}",
            },
        )
    return channel, f"mm:outage:{pi.hex[:8]}"


def _core_write(engine: Engine, sd: Seed, clock: FixedClock, n: int) -> str:
    """A domain Event append: the core must keep accepting work during a provider outage."""
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        result = store.append(
            AppendRequest(
                workspace_id=str(sd.ws),
                aggregate_type="task",
                aggregate_id=f"task-outage-{n}",
                type="TASK_CREATED",
                actor_account_id=str(sd.accounts["admin1"]),
                correlation_id=f"corr-outage-{n}",
                idempotency_scope="task:create",
                idempotency_key=f"outage-{n}",
                payload={
                    "task_id": f"task-outage-{n}",
                    "root_task_id": f"task-outage-{n}",
                    "channel_id": "chan-outage",
                    "title": "outage probe",
                    "domain": "general",
                    "risk": "LOW",
                },
            )
        )
    return result.event_id


@pytest.mark.parametrize("dependency", OUTAGE_PROBES)
def test_provider_outage_keeps_the_core_writing_and_drains_exactly_once(
    engine: Engine, sd: Seed, dependency: str
) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=6))
    _channel_uuid, provider_instance_id = _channel(engine, sd)
    provider = FlakyProvider()
    probes.set_prober(dependency, lambda _s: (False, f"{dependency} unreachable (injected)"))

    # 1. the dashboard reflects the failure within the probe window (60 s). The probe cache is
    # instance-level, so age it out first; a fresh entry from another Test would mask the failure.
    with Session(engine) as s, s.begin():
        s.execute(text("DELETE FROM dependency_probes"))
    with Session(engine) as s, s.begin():
        before = dashboard.overview(s, sd.ws, clock)
    assert any(a["dependency"] == dependency for a in before["alerts"]), before["alerts"]
    failing = next(d for d in before["dependencies"] if d["name"] == dependency)
    assert failing["status"] == "failed"

    # 2. ten minutes of outage: core writes succeed and each enqueues one outbox row
    events, keys = [], []
    for minute in range(10):
        events.append(_core_write(engine, sd, clock, f"{dependency}-{minute}"))
        key = f"outage:{dependency}:{minute}:{uuid.uuid4().hex[:6]}"
        with Session(engine) as s, s.begin():
            enqueue_delivery(
                s,
                workspace_id=str(sd.ws),
                source_event_id=events[-1],
                delivery=Delivery(
                    "mattermost.post",
                    f"mattermost:ext-{dependency}",
                    {"message": f"during outage {minute}"},
                    key,
                    subject_type="task",
                    subject_id=f"task-outage-{dependency}-{minute}",
                    role="reply",
                ),
                provider_instance_id=provider_instance_id,
                external_channel_id=f"ext-{dependency}",
                now=clock.now(),
            )
        keys.append(key)
        with Session(engine) as s, s.begin():  # drains keep failing while the provider is down
            drain_channels(s, {"mattermost": provider}, clock, str(sd.ws))
        clock.advance(dt.timedelta(minutes=1))

    assert len(set(events)) == 10  # every core write was accepted
    with Session(engine) as s:
        pending, dead = s.execute(
            text(
                "SELECT count(*) FILTER (WHERE status IN ('pending','failed')), "
                "count(*) FILTER (WHERE status = 'dead') FROM delivery_outbox "
                "WHERE dedupe_key = ANY(:k)"
            ),
            {"k": keys},
        ).one()
    # every row is preserved: still retryable, or dead-lettered by the backoff budget and revived
    # on recovery below. Nothing is dropped.
    assert pending + dead == 10, (pending, dead)

    # 3. recovery: the maintenance requeue revives what the outage dead-lettered, and within five
    # minutes of drains every row is delivered exactly once
    provider.down = False
    probes.set_prober(dependency, None)
    with Session(engine) as s, s.begin():
        revived = requeue_dead(s, str(sd.ws), clock, reason="dependency probe recovered")
    assert revived == dead, (revived, dead)
    for _ in range(5):
        with Session(engine) as s, s.begin():
            drain_channels(s, {"mattermost": provider}, clock, str(sd.ws))
        clock.advance(dt.timedelta(minutes=1))
    assert sorted(provider.delivered) == sorted(keys)
    with Session(engine) as s:
        sent = s.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE dedupe_key = ANY(:k) AND status='sent'"
            ),
            {"k": keys},
        ).scalar_one()
    assert sent == 10

    # 4. and the dashboard reports recovery once the cache goes stale
    clock.advance(dt.timedelta(seconds=probes.STALE_S + 1))
    with Session(engine) as s, s.begin():
        after = dashboard.overview(s, sd.ws, clock)
    assert not any(a["dependency"] == dependency for a in after["alerts"]), after["alerts"]
