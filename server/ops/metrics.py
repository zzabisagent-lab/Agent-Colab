"""Prometheus-style text exposition of the numbers the ops dashboard already computes (P7-02).

No new dependency: the exposition format is three lines per metric family (``# HELP``, ``# TYPE``,
then samples), which is short enough to format directly. Every value comes from
:mod:`server.ops.dashboard` or :mod:`server.schedules.metrics`, so a scrape and the dashboard can
never disagree — there is one source of truth and two renderings of it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.ops import dashboard

PREFIX = "agent_colab"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(pairs: Mapping[str, Any]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(pairs.items()))
    return "{" + inner + "}"


class Exposition:
    """Collects metric families and renders them in a stable order."""

    def __init__(self) -> None:
        self._families: list[tuple[str, str, str, list[tuple[dict[str, Any], float]]]] = []

    def gauge(
        self,
        name: str,
        help_text: str,
        samples: Iterable[tuple[dict[str, Any], float]] | float,
    ) -> None:
        rows = [({}, float(samples))] if isinstance(samples, int | float) else list(samples)
        self._families.append((f"{PREFIX}_{name}", "gauge", help_text, rows))

    def render(self) -> str:
        lines: list[str] = []
        for name, kind, help_text, rows in self._families:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in rows:
                lines.append(f"{name}{_labels(labels)} {value:g}")
        return "\n".join(lines) + "\n"


def _percentile_samples(block: Mapping[str, Any]) -> list[tuple[dict[str, Any], float]]:
    return [
        ({"quantile": q}, float(block.get(key, 0.0)))
        for q, key in (("0.5", "p50"), ("0.95", "p95"), ("1.0", "max"))
    ]


def render(
    session: Session, workspace_id: uuid.UUID | str, clock: Clock, *, refresh: bool = False
) -> str:
    """The full exposition for one Workspace, built from the ops overview."""
    ws = uuid.UUID(str(workspace_id))
    over = dashboard.overview(session, ws, clock, refresh=refresh)
    out = Exposition()

    out.gauge(
        "dependency_up",
        "1 when a dependency probe passed, 0 when it failed (unconfigured probes are omitted).",
        [
            ({"dependency": d["name"]}, 1.0 if d["ok"] else 0.0)
            for d in over["dependencies"]
            if d["ok"] is not None
        ],
    )
    out.gauge(
        "dependency_latency_ms",
        "Latency of the last dependency probe in milliseconds.",
        [({"dependency": d["name"]}, float(d["latency_ms"])) for d in over["dependencies"]],
    )
    out.gauge(
        "alerts_active",
        "Dependency alerts currently raised, by severity.",
        _by(over["alerts"], "severity"),
    )
    out.gauge(
        "tasks",
        "Tasks in the projection, by status.",
        [({"status": k}, float(v)) for k, v in sorted(over["tasks"]["by_status"].items())],
    )
    out.gauge(
        "agents",
        "Registered Agents, by status.",
        [({"status": k}, float(v)) for k, v in sorted(over["agents"]["by_status"].items())],
    )
    out.gauge("agents_online", "Agents currently online.", float(over["agents"]["online"]))
    out.gauge(
        "outbox_rows",
        "Delivery outbox rows that have not been sent, by provider kind and status.",
        [
            ({"kind": kind, "status": status}, float(count))
            for kind, statuses in sorted(over["outbox"].items())
            for status, count in sorted(statuses.items())
        ],
    )
    out.gauge(
        "hard_delete_requests_pending",
        "Hard-delete requests awaiting approval or their waiting period.",
        float(over["hard_delete_requests_pending"]),
    )
    out.gauge(
        "maintenance_mode",
        "1 while maintenance mode refuses non-administrator writes.",
        1.0 if over["maintenance"].get("active") else 0.0,
    )

    schedules = over.get("schedules") or {}
    if schedules:
        out.gauge(
            "schedule_runs_due", "Runs waiting to be claimed.", float(schedules.get("due", 0))
        )
        out.gauge("schedule_runs_running", "Runs in flight.", float(schedules.get("running", 0)))
        out.gauge(
            "schedule_run_failures",
            "Runs that failed or timed out in the metrics window.",
            float(schedules.get("failures", 0)),
        )
        out.gauge(
            "schedule_stuck_leases",
            "Claimed Runs whose lease expired without recovery.",
            float(schedules.get("stuck_leases", 0)),
        )
        for name, key in (
            ("schedule_start_delay_seconds", "start_delay_s"),
            ("schedule_lag_seconds", "lag_s"),
        ):
            block = schedules.get(key)
            if isinstance(block, Mapping):
                out.gauge(name, f"Schedule {key} distribution.", _percentile_samples(block))
    return out.render()


def _by(items: Iterable[Mapping[str, Any]], field: str) -> list[tuple[dict[str, Any], float]]:
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.get(field, "unknown"))] = counts.get(str(item.get(field, "unknown")), 0) + 1
    return [({field: k}, float(v)) for k, v in sorted(counts.items())]
