"""V-P7-14 observability: every defined alert fires within 60 s of the condition appearing, the
dashboard and the alert agree on what is wrong, and the correlation id is the same value in the
structured log, the alert and the ops-channel notification."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.observability import logs
from server.observability.audit import append_audit
from server.ops import alerts, dashboard, probes
from tests.integration.phase4_admin_seed import T0, Seed, seed

pytestmark = pytest.mark.db
OPS_CHANNEL = "ops-chan-p7"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "obs")


@pytest.fixture(autouse=True)
def _quiet_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AGENT_COLAB_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("AGENT_COLAB_MATTERMOST_URL", raising=False)
    yield
    for name in probes.PROBE_NAMES:
        probes.set_prober(name, None)


def _clean(engine: Engine, sd: Seed) -> None:
    """Each case starts quiet. Audit rows are immutable by design, so the audit-backed signals
    are windowed instead: a case that needs them advances the clock past the one-hour window."""
    with Session(engine) as s, s.begin():
        s.execute(
            text("DELETE FROM schedule_alert_emissions WHERE workspace_id = :w"), {"w": sd.ws}
        )
        s.execute(text("DELETE FROM delivery_outbox WHERE workspace_id = :w"), {"w": sd.ws})
        s.execute(text("DELETE FROM dependency_probes"))


def _evaluate(
    engine: Engine, sd: Seed, clock: FixedClock, *, refresh: bool = True
) -> tuple[alerts.Evaluation, dict[str, Any], tuple[list[str], list[str]]]:
    with Session(engine) as s, s.begin():
        return alerts.evaluate_and_emit(s, sd.ws, clock, ops_channel=OPS_CHANNEL, refresh=refresh)


def _keys(evaluation: alerts.Evaluation) -> set[str]:
    return {a["key"] for a in evaluation.alerts}


def test_rule_set_is_loadable_and_every_critical_alert_names_a_runbook() -> None:
    rules, runbooks = alerts.load_rules()
    assert rules and runbooks
    assert all(r.runbook for r in rules if r.severity == "critical")
    assert {r.runbook for r in rules if r.runbook} == set(runbooks)  # no orphan runbook


def test_dependency_failure_alerts_within_the_probe_window(engine: Engine, sd: Seed) -> None:
    """A failing probe raises its alert on the next refresh, and within 60 s without one."""
    _clean(engine, sd)
    clock = FixedClock(T0)
    assert "DEPENDENCY_DOWN_STORAGE" not in _keys(_evaluate(engine, sd, clock)[0])

    probes.set_prober("storage", lambda _s: (False, "disk full (injected)"))
    evaluation, over, (emitted, _) = _evaluate(engine, sd, clock)
    assert "DEPENDENCY_DOWN_STORAGE" in _keys(evaluation)
    assert "DEPENDENCY_DOWN_STORAGE" in emitted
    storage = next(d for d in over["dependencies"] if d["name"] == "storage")
    assert storage["ok"] is False and "disk full" in storage["detail"]  # dashboard agrees

    # without an explicit refresh the cached probe ages out inside the 60 s window
    _clean(engine, sd)
    clock2 = FixedClock(T0 + dt.timedelta(hours=1))
    probes.set_prober("storage", None)
    _evaluate(engine, sd, clock2)  # caches a healthy probe
    probes.set_prober("storage", lambda _s: (False, "disk full (injected)"))
    clock2.advance(dt.timedelta(seconds=probes.STALE_S + 1))
    assert "DEPENDENCY_DOWN_STORAGE" in _keys(_evaluate(engine, sd, clock2, refresh=False)[0])
    assert clock2.now() - T0 - dt.timedelta(hours=1) <= dt.timedelta(seconds=61)


def test_each_injected_failure_raises_its_own_alert(engine: Engine, sd: Seed) -> None:
    """The synthetic failure set: every condition raises exactly the rule that watches it."""
    clock = FixedClock(T0 + dt.timedelta(hours=2))

    # 1. outbox backlog
    _clean(engine, sd)
    with Session(engine) as s, s.begin():
        for n in range(600):
            s.execute(
                text(
                    "INSERT INTO delivery_outbox (outbox_id, workspace_id, kind, destination, "
                    "dedupe_key, payload, status, next_attempt_at) VALUES (:o, :w, "
                    "'mattermost.post', 'mattermost:c', :k, '{}'::jsonb, 'pending', :now)"
                ),
                {"o": f"obx-p7-{n}", "w": sd.ws, "k": f"p7-backlog-{n}", "now": clock.now()},
            )
    assert "OUTBOX_BACKLOG" in _keys(_evaluate(engine, sd, clock)[0])

    # 2. dead-lettered deliveries
    clock.advance(dt.timedelta(hours=2))
    _clean(engine, sd)
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO delivery_outbox (outbox_id, workspace_id, kind, destination, "
                "dedupe_key, payload, status, next_attempt_at) VALUES (:o, :w, "
                "'mattermost.post', 'mattermost:c', :k, '{}'::jsonb, 'dead', :now)"
            ),
            {"o": "obx-p7-dead", "w": sd.ws, "k": "p7-dead-1", "now": clock.now()},
        )
    assert "OUTBOX_DEAD_LETTERS" in _keys(_evaluate(engine, sd, clock)[0])

    # 3. repeated authentication rejections
    clock.advance(dt.timedelta(hours=2))
    _clean(engine, sd)
    with Session(engine) as s, s.begin():
        for _ in range(12):
            append_audit(
                s,
                action="auth.session.rate_limited",
                target_type="auth",
                target_id="session",
                result="DENY",
                actor_label="ip:10.0.0.1",
                correlation_id="corr-p7",
                workspace_id=sd.ws,
                clock=clock,
            )
    assert "AUTH_RATE_LIMITED" in _keys(_evaluate(engine, sd, clock)[0])

    # 4. a secret canary sighting
    clock.advance(dt.timedelta(hours=2))  # the auth rows above fall out of the one-hour window
    _clean(engine, sd)
    with Session(engine) as s, s.begin():
        append_audit(
            s,
            action="secret.canary_detected",
            target_type="document",
            target_id="doc-1",
            result="DENY",
            actor_label="scan",
            correlation_id="corr-p7",
            workspace_id=sd.ws,
            clock=clock,
        )
    raised = _evaluate(engine, sd, clock)[0]
    assert "SECRET_CANARY_DETECTED" in _keys(raised)
    canary = next(a for a in raised.alerts if a["key"] == "SECRET_CANARY_DETECTED")
    assert canary["severity"] == "critical" and canary["runbook"] == "secret-leak"


class _Capture(logging.Handler):
    """The command logger does not propagate (P7-02), so listen on it directly."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_alert_dashboard_and_log_share_one_correlation_id(engine: Engine, sd: Seed) -> None:
    """V-P7-14: state, cause and correlation id are consistent across the three surfaces."""
    _clean(engine, sd)
    clock = FixedClock(T0 + dt.timedelta(hours=3))
    probes.set_prober("postgres", lambda _s: (False, "connection refused (injected)"))
    correlation = "corr-p7-obs-1"
    token = logs.correlation_id.set(correlation)
    logs.install_json_logging()
    capture = _Capture()
    command_logger = logging.getLogger(logs.COMMAND_LOGGER_NAME)
    command_logger.addHandler(capture)
    try:
        logs.log_command(command="EvaluateAlerts", outcome="ok", duration_ms=1)
        evaluation, over, (emitted, _) = _evaluate(engine, sd, clock)
    finally:
        command_logger.removeHandler(capture)
        logs.correlation_id.reset(token)
        probes.set_prober("postgres", None)

    assert evaluation.correlation_id == correlation
    down = next(a for a in evaluation.alerts if a["key"] == "DEPENDENCY_DOWN_POSTGRES")
    assert down["correlation_id"] == correlation and down["runbook"] == "db-restore"
    postgres = next(d for d in over["dependencies"] if d["name"] == "postgres")
    assert postgres["ok"] is False and "connection refused" in postgres["detail"]
    assert [a["dependency"] for a in over["alerts"]] == ["postgres"]  # dashboard says the same
    assert any(r.correlation_id == correlation for r in capture.records)  # the log agrees
    rendered = json.loads(logs.JsonFormatter().format(capture.records[0]))
    assert rendered["correlation_id"] == correlation and rendered["outcome"] == "ok"

    with Session(engine) as s:
        payload = s.execute(
            text(
                "SELECT payload FROM delivery_outbox WHERE workspace_id = :w "
                "AND dedupe_key LIKE 'ops-alert|DEPENDENCY_DOWN_POSTGRES|%'"
            ),
            {"w": sd.ws},
        ).scalar_one()
    body = payload if isinstance(payload, dict) else json.loads(payload)
    assert body["correlation_id"] == correlation and body["runbook"] == "db-restore"
    assert "DEPENDENCY_DOWN_POSTGRES" in emitted


def test_alerts_are_emitted_once_per_hour(engine: Engine, sd: Seed) -> None:
    _clean(engine, sd)
    clock = FixedClock(T0 + dt.timedelta(hours=4))
    probes.set_prober("postgres", lambda _s: (False, "still down"))
    first = _evaluate(engine, sd, clock)[2]
    second = _evaluate(engine, sd, clock)[2]
    assert "DEPENDENCY_DOWN_POSTGRES" in first[0]
    assert "DEPENDENCY_DOWN_POSTGRES" in second[1]  # suppressed inside the same hour
    clock.advance(dt.timedelta(hours=1))
    assert "DEPENDENCY_DOWN_POSTGRES" in _evaluate(engine, sd, clock)[2][0]
    probes.set_prober("postgres", None)


def test_metrics_exposition_matches_the_dashboard(engine: Engine, sd: Seed) -> None:
    """The scrape and the overview are two renderings of one set of numbers (P7-02)."""
    from server.ops import metrics

    _clean(engine, sd)
    clock = FixedClock(T0 + dt.timedelta(hours=5))
    probes.set_prober("storage", lambda _s: (False, "disk full (injected)"))
    try:
        with Session(engine) as s:
            body = metrics.render(s, sd.ws, clock, refresh=True)
            over = dashboard.overview(s, sd.ws, clock)
    finally:
        probes.set_prober("storage", None)

    assert body.startswith("# HELP agent_colab_")
    assert 'agent_colab_dependency_up{dependency="storage"} 0' in body
    assert 'agent_colab_dependency_up{dependency="postgres"} 1' in body
    assert f"agent_colab_agents_online {over['agents']['online']:g}" in body
    # every family declares its type before its samples
    families = [line.split()[2] for line in body.splitlines() if line.startswith("# TYPE ")]
    assert families and all(f.startswith("agent_colab_") for f in families)
