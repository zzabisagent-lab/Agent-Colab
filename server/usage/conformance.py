"""Usage reporting conformance (development plan §7C; P3-15, V-P3-26).

Every ``work_result``, ``invoke`` result and heartbeat carries either ``usage`` or a
``usage_unavailable`` reason. This module normalizes adapter usage into ``usage_records`` through
:func:`server.usage.records.record_usage` (cost_units computed from the active pricing, unknown
model → default rate with ``source=estimated``) and measures the ``usage_unavailable`` ratio per
Agent over a window. Adapter packages call :func:`record_result_usage` (work results) and
:func:`record_heartbeat_usage` (heartbeats; the registry package's heartbeat command uses it).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.usage.pricing import UsageError
from server.usage.records import UsageRecord, record_usage

log = logging.getLogger(__name__)
USAGE_FIELDS = ("model", "input_tokens", "output_tokens", "tool_calls", "wall_time_ms")
DEFAULT_UNAVAILABLE_REASON = "ADAPTER_NO_METERING"
KNOWN_UNAVAILABLE_REASONS = frozenset(
    {
        "ADAPTER_NO_METERING",
        "ADAPTER_METERING_FAILED",
        "MODEL_UNKNOWN",
        "PARTIAL_RESULT",
        "CANCELLED",
    }
)


@dataclass(frozen=True)
class NormalizedUsage:
    usage: dict[str, Any] | None
    unavailable_reason: str | None
    conformant: bool  # usage present, or an explicit reason given
    problems: tuple[str, ...] = ()


def normalize_usage(payload: dict[str, Any] | None) -> NormalizedUsage:
    """Extract ``usage`` / ``usage_unavailable`` from a result, heartbeat or invoke payload.

    A payload without either is non-conformant: it is recorded as unavailable with the default
    reason so the ratio reflects it (V-P3-26) and the caller can fail conformance (CS-04).
    """
    payload = payload or {}
    usage = payload.get("usage")
    unavailable = payload.get("usage_unavailable")
    problems: list[str] = []
    if isinstance(usage, dict):
        clean: dict[str, Any] = {}
        for key in USAGE_FIELDS:
            if key not in usage:
                problems.append(f"usage.{key} missing")
            clean[key] = usage.get(key, 0 if key != "model" else "unknown")
        for key in ("input_tokens", "output_tokens", "tool_calls", "wall_time_ms"):
            if not isinstance(clean[key], int) or clean[key] < 0:
                problems.append(f"usage.{key} must be a non-negative integer")
        if usage.get("cost_units") is not None:
            if not isinstance(usage["cost_units"], int) or usage["cost_units"] < 0:
                problems.append("usage.cost_units must be a non-negative integer")
            else:
                clean["cost_units"] = usage["cost_units"]
        if problems:
            return NormalizedUsage(None, "ADAPTER_METERING_FAILED", False, tuple(problems))
        return NormalizedUsage(clean, None, True)
    if isinstance(unavailable, dict) and unavailable.get("reason"):
        reason = str(unavailable["reason"])
        return NormalizedUsage(None, reason, True)
    if isinstance(unavailable, str) and unavailable:
        return NormalizedUsage(None, unavailable, True)
    return NormalizedUsage(None, DEFAULT_UNAVAILABLE_REASON, False, ("usage and reason missing",))


def record_result_usage(
    session: Session,
    *,
    workspace_id: str,
    account_id: str,
    agent_id: str,
    work_item_id: str,
    payload: dict[str, Any],
    task_id: str | None = None,
    brainstorm_id: str | None = None,
    clock: Clock | None = None,
) -> UsageRecord | None:
    """Record the usage carried by a work result (or an invoke result) exactly once per result.

    Returns None when no pricing version is activated yet (Setup activates one): usage
    accounting must never block result intake, and the gap is logged for the operator.
    """
    norm = normalize_usage(payload)
    try:
        return record_usage(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            agent_id=agent_id,
            work_item_id=work_item_id,
            usage=norm.usage,
            usage_unavailable_reason=norm.unavailable_reason,
            task_id=task_id,
            brainstorm_id=brainstorm_id,
            clock=clock,
        )
    except UsageError as exc:
        if exc.code == "PRICING_NOT_ACTIVATED":
            log.warning("usage not recorded for %s: pricing not activated", work_item_id)
            return None
        # the pricing layer rejected the report: keep the ratio honest with an unavailable row
        return record_usage(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            agent_id=agent_id,
            work_item_id=work_item_id,
            usage=None,
            usage_unavailable_reason="ADAPTER_METERING_FAILED",
            task_id=task_id,
            brainstorm_id=brainstorm_id,
            clock=clock,
        )


def record_heartbeat_usage(
    session: Session,
    *,
    workspace_id: str,
    account_id: str,
    agent_id: str,
    usage_since_last: dict[str, Any] | None,
    clock: Clock | None = None,
) -> UsageRecord:
    """Record ``usage_since_last`` from a heartbeat (usage or an explicit reason)."""
    norm = normalize_usage(
        usage_since_last
        if usage_since_last
        and ("usage" in usage_since_last or "usage_unavailable" in usage_since_last)
        else {"usage": usage_since_last}
        if usage_since_last
        else {}
    )
    return record_usage(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        agent_id=agent_id,
        work_item_id=None,
        usage=norm.usage,
        usage_unavailable_reason=norm.unavailable_reason,
        clock=clock,
    )


@dataclass(frozen=True)
class UnavailableRatio:
    agent_id: str
    total: int
    unavailable: int
    estimated: int

    @property
    def ratio(self) -> float:
        return 0.0 if self.total == 0 else self.unavailable / self.total


def usage_unavailable_ratio(
    session: Session,
    agent_id: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    *,
    workspace_id: str | None = None,
) -> UnavailableRatio:
    """Share of usage records without measured usage for one Agent in ``[start, end)``."""
    params: dict[str, Any] = {"a": agent_id, "s": window_start, "e": window_end}
    params["w"] = uuid.UUID(workspace_id) if workspace_id is not None else None
    row = session.execute(
        text(
            "SELECT count(*), count(*) FILTER (WHERE source = 'unavailable'), "
            "count(*) FILTER (WHERE source = 'estimated') FROM usage_records "
            "WHERE agent_id = :a AND reported_at >= :s AND reported_at < :e "
            "AND (CAST(:w AS uuid) IS NULL OR workspace_id = :w)"
        ),
        params,
    ).one()
    return UnavailableRatio(agent_id, int(row[0]), int(row[1]), int(row[2]))
