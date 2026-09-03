"""Recurring per-Schedule period summaries (P6-08, development plan §10.2, V-P6-09).

A Schedule whose ``documentation_policy.period_summary`` is ``daily``/``weekly``/``monthly`` gets
one document per closed period covering every Run in the window: status counts, the Tasks and
Artifacts they produced, and the limitations of the period. The window boundaries are computed in
UTC from the period key, so the same period always produces the same subject id.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

PERIODS = ("daily", "weekly", "monthly")


@dataclass(frozen=True)
class Window:
    period: str
    start: dt.datetime
    end: dt.datetime


def window_for(period: str, moment: dt.datetime) -> Window:
    """The closed period immediately before ``moment`` (UTC boundaries)."""
    if period not in PERIODS:
        raise ValueError(f"unknown period {period}")
    day = moment.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return Window(period, day - dt.timedelta(days=1), day)
    if period == "weekly":
        start_of_week = day - dt.timedelta(days=day.weekday())
        return Window(period, start_of_week - dt.timedelta(days=7), start_of_week)
    first = day.replace(day=1)
    previous_end = first
    previous_start = (first - dt.timedelta(days=1)).replace(day=1)
    return Window(period, previous_start, previous_end)


def due_schedules(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Schedules whose current version asks for a period summary."""
    rows = session.execute(
        text(
            "SELECT s.schedule_id, s.name, v.documentation_policy->>'period_summary' AS period "
            "FROM schedules s JOIN schedule_versions v ON v.id = s.current_version_id "
            "WHERE s.workspace_id = CAST(:w AS uuid) "
            "AND v.documentation_policy->>'period_summary' IN ('daily','weekly','monthly') "
            "ORDER BY s.schedule_id"
        ),
        {"w": workspace_id},
    ).mappings()
    return [dict(r) for r in rows]


__all__ = ["PERIODS", "Window", "due_schedules", "window_for"]
