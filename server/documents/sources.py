"""Source freeze and collection for every documentable subject (development plan §10.1-§10.2).

Tasks keep the Phase 1 collector in :mod:`server.documents.builder`. This module adds the other
three subjects — a closed Brainstorm, a terminal Schedule Run, and a Schedule period — and the
freeze ledger they all share: the exact set of source ids a version was built from, hashed, so a
later rebuild can be proven to have used the same sources.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.documents.provenance import manifest_hash

SUBJECT_TYPES = ("task", "brainstorm", "schedule_run", "schedule_period")


class SourceError(ValueError):
    """A subject cannot be documented; ``code`` is the stable generation reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Freeze:
    freeze_id: str
    subject_type: str
    subject_id: str
    workspace_id: str
    frozen_at: dt.datetime
    up_to_recorded_seq: int
    source_manifest: dict[str, Any]
    manifest_hash: str


@dataclass
class SubjectSources:
    subject_type: str
    subject_id: str
    workspace_id: str
    title: str
    freeze: Freeze
    events: list[dict[str, Any]] = field(default_factory=list)
    rows: dict[str, Any] = field(default_factory=dict)  # subject-specific authority rows
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    accounts: dict[str, str] = field(default_factory=dict)
    usage: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def document_id_for(subject_type: str, subject_id: str) -> str:
    return "doc-" + hashlib.sha256(f"{subject_type}|{subject_id}".encode()).hexdigest()[:16]


def _max_seq(session: Session, aggregate_type: str, aggregate_id: str) -> int:
    seq = session.execute(
        text(
            "SELECT COALESCE(MAX(recorded_seq), 0) FROM events "
            "WHERE aggregate_type = :t AND aggregate_id = :i"
        ),
        {"t": aggregate_type, "i": aggregate_id},
    ).scalar_one()
    return int(seq)


def _events(
    session: Session, aggregate_type: str, aggregate_id: str, seq: int
) -> list[dict[str, Any]]:
    from server.events.postgres_store import _COLUMNS, row_to_event

    rows = (
        session.execute(
            text(
                f"SELECT {_COLUMNS} FROM events WHERE aggregate_type = :t "  # noqa: S608
                "AND aggregate_id = :i AND recorded_seq <= :s ORDER BY recorded_seq"
            ),
            {"t": aggregate_type, "i": aggregate_id, "s": seq},
        )
        .mappings()
        .all()
    )
    return [row_to_event(r) for r in rows]


def _accounts(session: Session, uuids: set[str]) -> dict[str, str]:
    if not uuids:
        return {}
    out: dict[str, str] = {}
    for r in session.execute(
        text(
            "SELECT id::text, account_id, account_type FROM accounts "
            "WHERE id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": sorted(uuids)},
    ):
        out[str(r[0])] = f"{r[1]} ({r[2]})"
    return out


def _artifacts_for(session: Session, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT a.artifact_id, a.mime, a.size, a.sha256, a.status, l.relation "
            "FROM artifact_links l JOIN artifacts a ON a.artifact_id = l.artifact_id "
            "WHERE l.subject_type = :st AND l.subject_id = :si "
            "ORDER BY a.artifact_id, l.relation"
        ),
        {"st": subject_type, "si": subject_id},
    ).mappings()
    return [dict(r) for r in rows]


def _table_exists(session: Session, name: str) -> bool:
    return bool(
        session.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}).scalar_one()
    )


# ---------------------------------------------------------------- freeze ledger


def record_freeze(session: Session, freeze: Freeze, document_id: str | None = None) -> None:
    session.execute(
        text(
            "INSERT INTO document_freezes (freeze_id, workspace_id, document_id, subject_type, "
            "subject_id, frozen_at, up_to_recorded_seq, source_manifest, manifest_hash) "
            "VALUES (:f, CAST(:w AS uuid), :d, :st, :si, :at, :seq, CAST(:m AS jsonb), :h) "
            "ON CONFLICT (freeze_id) DO NOTHING"
        ),
        {
            "f": freeze.freeze_id,
            "w": freeze.workspace_id,
            "d": document_id,
            "st": freeze.subject_type,
            "si": freeze.subject_id,
            "at": freeze.frozen_at,
            "seq": freeze.up_to_recorded_seq,
            "m": json.dumps(freeze.source_manifest, ensure_ascii=False, sort_keys=True),
            "h": freeze.manifest_hash,
        },
    )


def record_failure(
    session: Session,
    *,
    workspace_id: str | None,
    subject_type: str,
    subject_id: str,
    reason_code: str,
    detail: str,
    now: dt.datetime,
) -> None:
    """One stable reason code per failed generation attempt (V-P6-20 rate report)."""
    session.execute(
        text(
            "INSERT INTO document_generation_failures (workspace_id, subject_type, subject_id, "
            "reason_code, detail, at) VALUES (CAST(:w AS uuid), :st, :si, :r, :d, :at)"
        ),
        {
            "w": workspace_id,
            "st": subject_type,
            "si": subject_id,
            "r": reason_code,
            "d": detail[:500],
            "at": now,
        },
    )


def _freeze(
    subject_type: str,
    subject_id: str,
    workspace_id: str,
    now: dt.datetime,
    seq: int,
    manifest: dict[str, Any],
) -> Freeze:
    return Freeze(
        freeze_id="frz-" + uuid.uuid4().hex[:20],
        subject_type=subject_type,
        subject_id=subject_id,
        workspace_id=workspace_id,
        frozen_at=now,
        up_to_recorded_seq=seq,
        source_manifest=manifest,
        manifest_hash=manifest_hash(manifest),
    )


# ---------------------------------------------------------------- Schedule Run


def collect_schedule_run(session: Session, run_id: str, now: dt.datetime) -> SubjectSources:
    row = (
        session.execute(
            text(
                "SELECT r.run_id, r.workspace_id::text AS workspace_id, r.schedule_id, "
                "r.run_kind, r.status, r.scheduled_for, r.started_at, r.finished_at, "
                "r.error_code, r.planner_note, r.attempt_count, r.task_id, r.occurrence_key, "
                "r.version_hash, r.retry_of_run_id, r.requested_by::text AS requested_by, "
                "s.name AS schedule_name, v.version AS version_no, v.cron_expression, v.timezone "
                "FROM schedule_runs r JOIN schedules s ON s.schedule_id = r.schedule_id "
                "JOIN schedule_versions v ON v.id = r.schedule_version_id WHERE r.run_id = :r"
            ),
            {"r": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SourceError("SCHEDULE_RUN_NOT_FOUND", run_id)
    run = dict(row)
    seq = _max_seq(session, "schedule_run", run_id)
    events = _events(session, "schedule_run", run_id, seq)
    attempts = [
        dict(a)
        for a in session.execute(
            text(
                "SELECT attempt_no, result, error_code, started_at, finished_at "
                "FROM schedule_run_attempts WHERE run_id = :r ORDER BY attempt_no"
            ),
            {"r": run_id},
        ).mappings()
    ]
    artifacts = _artifacts_for(session, "schedule_run", run_id)
    task: dict[str, Any] | None = None
    if run["task_id"]:
        trow = (
            session.execute(
                text(
                    "SELECT task_id, title, status, risk, domain, verification_status "
                    "FROM tasks_projection WHERE task_id = :t"
                ),
                {"t": run["task_id"]},
            )
            .mappings()
            .first()
        )
        task = dict(trow) if trow else None
        artifacts += _artifacts_for(session, "task", str(run["task_id"]))
    usage = [
        dict(u)
        for u in session.execute(
            text(
                "SELECT agent_id, model, input_tokens, output_tokens, tool_calls, wall_ms, "
                "cost_units, source, unavailable_reason FROM usage_records WHERE run_id = :r "
                "ORDER BY id"
            ),
            {"r": run_id},
        ).mappings()
    ]
    manifest = {
        "subject": {"type": "schedule_run", "id": run_id},
        "up_to_recorded_seq": seq,
        "event_ids": [e["event_id"] for e in events],
        "artifact_ids": [a["artifact_id"] for a in artifacts],
        "task_id": run["task_id"],
        "schedule_id": run["schedule_id"],
        "version_hash": run["version_hash"],
        "attempts": len(attempts),
    }
    freeze = _freeze("schedule_run", run_id, str(run["workspace_id"]), now, seq, manifest)
    src = SubjectSources(
        subject_type="schedule_run",
        subject_id=run_id,
        workspace_id=str(run["workspace_id"]),
        title=f"Schedule Run {run_id} — {run['schedule_name']}",
        freeze=freeze,
        events=events,
        rows={"run": run, "attempts": attempts, "task": task},
        artifacts=artifacts,
        usage=usage,
    )
    src.accounts = _accounts(
        session,
        {e["actor_account_id"] for e in events}
        | ({run["requested_by"]} if run["requested_by"] else set()),
    )
    if run["error_code"]:
        src.limitations.append(f"Run ended with error code `{run['error_code']}`.")
    if run["planner_note"]:
        src.limitations.append(f"Planner note: {run['planner_note']}.")
    if not run["task_id"]:
        src.limitations.append("No Task was created by this Run.")
    if str(run["status"]) not in ("SUCCEEDED",):
        src.limitations.append(f"Terminal status is {run['status']}, not SUCCEEDED.")
    return src


# ---------------------------------------------------------------- Schedule period


def period_subject_id(schedule_id: str, period: str, start: dt.datetime) -> str:
    return f"{schedule_id}:{period}:{start.date().isoformat()}"


def collect_schedule_period(
    session: Session,
    schedule_id: str,
    *,
    period: str,
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> SubjectSources:
    srow = (
        session.execute(
            text(
                "SELECT schedule_id, workspace_id::text AS workspace_id, name, status "
                "FROM schedules WHERE schedule_id = :s"
            ),
            {"s": schedule_id},
        )
        .mappings()
        .first()
    )
    if srow is None:
        raise SourceError("SCHEDULE_NOT_FOUND", schedule_id)
    schedule = dict(srow)
    runs = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT run_id, run_kind, status, scheduled_for, started_at, finished_at, "
                "error_code, task_id, attempt_count FROM schedule_runs "
                "WHERE schedule_id = :s AND scheduled_for >= :a AND scheduled_for < :b "
                "ORDER BY scheduled_for, run_id"
            ),
            {"s": schedule_id, "a": start, "b": end},
        ).mappings()
    ]
    subject_id = period_subject_id(schedule_id, period, start)
    artifacts: list[dict[str, Any]] = []
    for run in runs:
        artifacts += _artifacts_for(session, "schedule_run", str(run["run_id"]))
        if run["task_id"]:
            artifacts += _artifacts_for(session, "task", str(run["task_id"]))
    usage = [
        dict(u)
        for u in session.execute(
            text(
                "SELECT agent_id, model, input_tokens, output_tokens, tool_calls, wall_ms, "
                "cost_units, source, unavailable_reason FROM usage_records "
                "WHERE run_id = ANY(:ids) ORDER BY id"
            ),
            {"ids": [str(r["run_id"]) for r in runs] or [""]},
        ).mappings()
    ]
    manifest = {
        "subject": {"type": "schedule_period", "id": subject_id},
        "schedule_id": schedule_id,
        "period": period,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "schedule_run_ids": [str(r["run_id"]) for r in runs],
        "artifact_ids": [a["artifact_id"] for a in artifacts],
    }
    freeze = _freeze("schedule_period", subject_id, str(schedule["workspace_id"]), now, 0, manifest)
    src = SubjectSources(
        subject_type="schedule_period",
        subject_id=subject_id,
        workspace_id=str(schedule["workspace_id"]),
        title=f"{schedule['name']} — {period} summary {start.date().isoformat()}",
        freeze=freeze,
        rows={"schedule": schedule, "runs": runs, "period": period, "start": start, "end": end},
        artifacts=artifacts,
        usage=usage,
    )
    if not runs:
        src.limitations.append("No Runs were scheduled in this window.")
    failed = [r for r in runs if str(r["status"]) not in ("SUCCEEDED",)]
    if failed:
        src.limitations.append(
            f"{len(failed)} of {len(runs)} Runs did not succeed: "
            + ", ".join(sorted({str(r["status"]) for r in failed}))
        )
    return src


# ---------------------------------------------------------------- Brainstorm


def collect_brainstorm(session: Session, brainstorm_id: str, now: dt.datetime) -> SubjectSources:
    if not _table_exists(session, "brainstorms"):
        raise SourceError("BRAINSTORM_TABLES_MISSING", "migration 0019 has not been applied")
    row = (
        session.execute(
            text(
                "SELECT brainstorm_id, workspace_id::text AS workspace_id, topic, status, "
                "facilitator_account_id::text AS facilitator, channel_id::text AS channel_id "
                "FROM brainstorms WHERE brainstorm_id = :b"
            ),
            {"b": brainstorm_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SourceError("BRAINSTORM_NOT_FOUND", brainstorm_id)
    session_row = dict(row)
    seq = _max_seq(session, "brainstorm", brainstorm_id)
    events = _events(session, "brainstorm", brainstorm_id, seq)
    turns = [
        dict(t)
        for t in session.execute(
            text(
                "SELECT seq AS turn_no, account_id::text AS account_id, contribution_type, body, "
                "event_id FROM brainstorm_turns WHERE brainstorm_id = :b ORDER BY seq"
            ),
            {"b": brainstorm_id},
        ).mappings()
    ]
    summaries = [
        dict(s)
        for s in session.execute(
            text(
                "SELECT summary_id, body, status = 'APPROVED' AS approved, event_id "
                "FROM brainstorm_summaries "
                "WHERE brainstorm_id = :b ORDER BY summary_id"
            ),
            {"b": brainstorm_id},
        ).mappings()
    ]
    decisions = [
        dict(d)
        for d in session.execute(
            text(
                "SELECT decision_id, statement, rationale, source_event_ids, event_id "
                "FROM brainstorm_decisions WHERE brainstorm_id = :b ORDER BY decision_id"
            ),
            {"b": brainstorm_id},
        ).mappings()
    ]
    artifacts = _artifacts_for(session, "brainstorm", brainstorm_id)
    manifest = {
        "subject": {"type": "brainstorm", "id": brainstorm_id},
        "up_to_recorded_seq": seq,
        "event_ids": [e["event_id"] for e in events],
        "artifact_ids": [a["artifact_id"] for a in artifacts],
        "decision_ids": [str(d["decision_id"]) for d in decisions],
        "turns": len(turns),
    }
    freeze = _freeze(
        "brainstorm", brainstorm_id, str(session_row["workspace_id"]), now, seq, manifest
    )
    src = SubjectSources(
        subject_type="brainstorm",
        subject_id=brainstorm_id,
        workspace_id=str(session_row["workspace_id"]),
        title=f"Brainstorm {brainstorm_id} — {session_row['topic']}",
        freeze=freeze,
        events=events,
        rows={
            "session": session_row,
            "turns": turns,
            "summaries": summaries,
            "decisions": decisions,
        },
        artifacts=artifacts,
    )
    src.accounts = _accounts(
        session,
        {e["actor_account_id"] for e in events}
        | {str(t["account_id"]) for t in turns if t["account_id"]}
        | ({str(session_row["facilitator"])} if session_row["facilitator"] else set()),
    )
    if str(session_row["status"]) != "CLOSED":
        src.limitations.append(f"Session status is {session_row['status']}, not CLOSED.")
    if not any(s["approved"] for s in summaries):
        src.limitations.append("No facilitator-approved summary was recorded.")
    if not decisions:
        src.limitations.append("No Decision was recorded in this session.")
    return src


__all__ = [
    "SUBJECT_TYPES",
    "Freeze",
    "SourceError",
    "SubjectSources",
    "collect_brainstorm",
    "collect_schedule_period",
    "collect_schedule_run",
    "document_id_for",
    "period_subject_id",
    "record_failure",
    "record_freeze",
]
