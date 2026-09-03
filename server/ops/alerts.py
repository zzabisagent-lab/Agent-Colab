"""Operational alerting: rules → signals → hourly-deduplicated notifications (P7-02, V-P7-14).

The rule set lives in ``policy/alert-rules.yaml`` so an operator can read and change thresholds
without touching code; every critical rule names the runbook to open. Signals come from the same
tables the ops dashboard reads, so an alert and the dashboard row it refers to always agree, and
both carry the correlation id of the evaluation that produced them.

Emission reuses the Phase 5 ledger ``schedule_alert_emissions`` (its columns — Workspace, key,
hour bucket — are alert-generic) so an alert is sent to the ops channel at most once an hour.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.observability.logs import current_correlation_id
from server.ops import dashboard

RULES_PATH = Path(__file__).resolve().parents[2] / "policy" / "alert-rules.yaml"
COMPARISONS = ("gt", "gte", "eq")
AUTH_WINDOW_S = 3600


@dataclass(frozen=True)
class Rule:
    key: str
    signal: str
    comparison: str
    threshold: float
    severity: str
    runbook: str
    detail: str

    def fires(self, value: float) -> bool:
        if self.comparison == "gt":
            return value > self.threshold
        if self.comparison == "gte":
            return value >= self.threshold
        return value == self.threshold


@dataclass
class Evaluation:
    correlation_id: str
    signals: dict[str, float] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)


class AlertRuleError(ValueError):
    """A malformed rule file: refuse to alert on rules we cannot read."""


def _rule(raw: Mapping[str, Any]) -> Rule:
    try:
        rule = Rule(
            key=str(raw["key"]),
            signal=str(raw["signal"]),
            comparison=str(raw["comparison"]),
            threshold=float(raw["threshold"]),
            severity=str(raw["severity"]),
            runbook=str(raw.get("runbook", "")),
            detail=str(raw.get("detail", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AlertRuleError(f"invalid alert rule: {raw!r}") from exc
    if rule.comparison not in COMPARISONS:
        raise AlertRuleError(f"{rule.key}: unknown comparison {rule.comparison}")
    if rule.severity not in ("info", "warning", "critical"):
        raise AlertRuleError(f"{rule.key}: unknown severity {rule.severity}")
    if rule.severity == "critical" and not rule.runbook:
        raise AlertRuleError(f"{rule.key}: a critical alert must name a runbook")
    return rule


@lru_cache(maxsize=4)
def load_rules(path: str | None = None) -> tuple[tuple[Rule, ...], tuple[str, ...]]:
    """The rule set and the declared runbook ids (cached; pass a path in tests)."""
    document = yaml.safe_load(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    rules = tuple(_rule(r) for r in document.get("rules", []))
    runbooks = tuple(str(r) for r in document.get("runbooks", []))
    unknown = sorted({r.runbook for r in rules if r.runbook} - set(runbooks))
    if unknown:
        raise AlertRuleError(f"rules reference undeclared runbooks: {unknown}")
    return rules, runbooks


def _scalar(session: Session, sql: str, params: Mapping[str, Any]) -> float:
    return float(session.execute(text(sql), dict(params)).scalar_one() or 0)


def _table_exists(session: Session, name: str) -> bool:
    return session.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None


def signals(
    session: Session, workspace_id: uuid.UUID | str, clock: Clock, *, refresh: bool = False
) -> tuple[dict[str, float], dict[str, Any]]:
    """Current values of every signal the rules can watch, plus the overview they came from."""
    ws = uuid.UUID(str(workspace_id))
    over = dashboard.overview(session, ws, clock, refresh=refresh)
    out: dict[str, float] = {}
    for dep in over["dependencies"]:
        if dep["ok"] is not None:
            out[f"dependency_down.{dep['name']}"] = 0.0 if dep["ok"] else 1.0
    pending = sum(int(s.get("pending", 0)) for s in over["outbox"].values())
    dead = sum(int(s.get("dead", 0)) for s in over["outbox"].values())
    out["outbox_pending"] = float(pending)
    out["outbox_dead"] = float(dead)
    out["hard_delete_pending"] = float(over["hard_delete_requests_pending"])
    schedules = over.get("schedules") or {}
    if schedules:
        delay = schedules.get("start_delay_s") or {}
        out["schedule_start_delay_p95"] = float(delay.get("p95", 0.0))
        out["schedule_stuck_leases"] = float(schedules.get("stuck_leases", 0))
        out["schedule_budget_alerts"] = float(schedules.get("budget_alerts", 0))
    if _table_exists(session, "bridge_dead_letters"):
        out["bridge_dead_letters"] = _scalar(
            session,
            "SELECT count(*) FROM bridge_dead_letters WHERE workspace_id = :w "
            "AND replayed_at IS NULL",
            {"w": ws},
        )
    since = clock.now() - dt.timedelta(seconds=AUTH_WINDOW_S)
    out["auth_rate_limited"] = _scalar(
        session,
        "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND occurred_at >= :since "
        "AND action LIKE '%rate_limited'",
        {"w": ws, "since": since},
    )
    out["secret_canary_findings"] = _scalar(
        session,
        "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND occurred_at >= :since "
        "AND action = 'secret.canary_detected'",
        {"w": ws, "since": since},
    )
    return out, over


def evaluate(
    session: Session,
    workspace_id: uuid.UUID | str,
    clock: Clock,
    *,
    refresh: bool = False,
    rules_path: str | None = None,
) -> tuple[Evaluation, dict[str, Any]]:
    """Raise every rule whose signal is available and over its threshold."""
    values, over = signals(session, workspace_id, clock, refresh=refresh)
    rules, _ = load_rules(rules_path)
    evaluation = Evaluation(correlation_id=current_correlation_id())
    evaluation.signals = values
    for rule in rules:
        if rule.signal not in values:  # a signal this deployment cannot compute
            continue
        value = values[rule.signal]
        if rule.fires(value):
            evaluation.alerts.append(
                {
                    "key": rule.key,
                    "severity": rule.severity,
                    "runbook": rule.runbook,
                    "signal": rule.signal,
                    "value": value,
                    "threshold": rule.threshold,
                    "detail": rule.detail,
                    "correlation_id": evaluation.correlation_id,
                }
            )
    return evaluation, over


def _hour_bucket(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


def emit(
    session: Session,
    workspace_id: uuid.UUID | str,
    alerts: Sequence[Mapping[str, Any]],
    clock: Clock,
    *,
    ops_channel: str | None,
) -> tuple[list[str], list[str]]:
    """Send each alert to the ops channel at most once an hour; returns (emitted, suppressed)."""
    from server.notifications.outbox import enqueue

    ws = uuid.UUID(str(workspace_id))
    now = clock.now()
    bucket = _hour_bucket(now)
    emitted: list[str] = []
    suppressed: list[str] = []
    for alert in alerts:
        key = str(alert["key"])
        inserted = session.execute(
            text(
                "INSERT INTO schedule_alert_emissions (workspace_id, alert_key, hour_bucket, "
                "severity, payload, emitted_at) VALUES (:w, :k, :b, :sev, CAST(:p AS jsonb), :now) "
                "ON CONFLICT (workspace_id, alert_key, hour_bucket) DO NOTHING RETURNING id"
            ),
            {
                "w": ws,
                "k": key,
                "b": bucket,
                "sev": str(alert.get("severity", "warning")),
                "p": json.dumps(dict(alert), default=str),
                "now": now,
            },
        ).first()
        if inserted is None:
            suppressed.append(key)
            continue
        emitted.append(key)
        if ops_channel:
            runbook = str(alert.get("runbook", ""))
            enqueue(
                session,
                str(ws),
                "notification",
                f"mattermost:{ops_channel}",
                f"ops-alert|{key}|{bucket.strftime('%Y%m%dT%H')}",
                {
                    "event_type": "OPS_ALERT",
                    "alert_key": key,
                    "severity": str(alert.get("severity", "warning")),
                    "runbook": runbook,
                    "correlation_id": str(alert.get("correlation_id", "-")),
                    "message": (
                        f":rotating_light: {key}: {alert.get('detail', '')}"
                        + (f" (runbook: {runbook})" if runbook else "")
                    ),
                    "value": alert.get("value"),
                    "threshold": alert.get("threshold"),
                },
                None,
                now,
            )
    return emitted, suppressed


def evaluate_and_emit(
    session: Session,
    workspace_id: uuid.UUID | str,
    clock: Clock,
    *,
    ops_channel: str | None,
    refresh: bool = False,
) -> tuple[Evaluation, dict[str, Any], tuple[list[str], list[str]]]:
    """One maintenance-tick step: signals → rules → hourly-deduplicated emission."""
    evaluation, over = evaluate(session, workspace_id, clock, refresh=refresh)
    result = emit(session, workspace_id, evaluation.alerts, clock, ops_channel=ops_channel)
    return evaluation, over, result
