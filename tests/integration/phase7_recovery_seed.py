"""A representative workspace for the Phase 7 recovery tests (P7-03, V-P7-07/08/19/20).

Everything the restore criterion names — Tasks, Approvals, Schedules with Versions and Runs,
ExternalIdentity links, Artifacts, Documents and the Events behind them — created through the
command bus so the Event hash chain is real, not fixture rows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.application import artifacts as art
from server.application import schedules as sch
from server.application import tasks as tk
from server.application.approvals import RequestApproval
from server.artifacts.storage import ArtifactStorage
from server.domain.clock import FixedClock
from server.events.canonical import canonical_json
from tests.integration.phase4_admin_seed import VALID_FROM, Seed, run, seed

SCHEDULE_PERMS = ["schedule.manage", "schedule.run", "schedule.read"]
CRITERIA = ({"statement": "evidence attached", "check_type": "evidence", "required": True},)


@dataclass
class Recovery:
    """What was seeded, so a restore can be compared against it by identifier."""

    seed: Seed
    channel: uuid.UUID
    channel_id: str
    task_id: str
    approval_id: str
    schedule_id: str
    run_id: str
    artifact_id: str
    link_id: str


def _grant_schedule_permissions(engine: Engine, sd: Seed) -> None:
    from server.policy.repository import PostgresPolicyRepository

    repo = PostgresPolicyRepository()
    role = f"role-{sd.prefix}-sched"
    with Session(engine) as s, s.begin():
        repo.create_role(s, sd.ws, role, "scheduler")
        repo.commit_role_version(
            s,
            role,
            [*SCHEDULE_PERMS, "task.create", "task.read", "artifact.write", "artifact.read"],
            [],
            {},
            sd.accounts["admin1"],
        )
        for name in ("admin1", "svc"):
            repo.assign_role(s, sd.accounts[name], role, sd.accounts["admin1"], VALID_FROM)


def build(engine: Engine, prefix: str, clock: FixedClock) -> Recovery:
    """Seed the workspace and return the identifiers a restore must reproduce exactly."""
    sd = seed(engine, prefix)
    _grant_schedule_permissions(engine, sd)
    channel = uuid.uuid4()
    channel_id = f"chan-{prefix}"
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, 'work', :c)"
            ),
            {"i": channel, "c": channel_id, "w": sd.ws},
        )
        for name in ("admin1", "admin2", "admin3", "member", "svc"):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": channel, "a": sd.accounts[name]},
            )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider,"
                " base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team')"
            ),
            {"i": uuid.uuid4(), "p": f"mm:{prefix}", "w": sd.ws},
        )
        provider = s.execute(
            text("SELECT id FROM provider_instances WHERE provider_instance_id = :p"),
            {"p": f"mm:{prefix}"},
        ).scalar_one()
        link_id = f"link-{prefix}-1"
        s.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, account_id, verification_method, status, verified_at) "
                "VALUES (:i, :l, :p, :e, :a, 'admin_approval', 'active', :t)"
            ),
            {
                "i": uuid.uuid4(),
                "l": link_id,
                "p": provider,
                "e": f"mm-{prefix}-admin1",
                "a": sd.accounts["admin1"],
                "t": VALID_FROM,
            },
        )

    rt: Runtime = sd.runtime(engine, clock)
    task_id = str(
        run(
            rt,
            sd.principal("admin1"),
            tk.CreateTask(f"{prefix} task", str(channel), "research", "LOW", criteria=CRITERIA),
            f"{prefix}-task",
        ).resource_id
    )
    approval_id = str(
        run(
            rt,
            sd.principal("admin1"),
            RequestApproval(
                subject_type="task",
                subject_id=task_id,
                action="tool:task_delegate",
                channel_uuid=str(channel),
            ),
            f"{prefix}-approval",
        ).resource_id
    )
    artifact_id = str(
        run(
            rt,
            sd.principal("admin1"),
            art.RegisterArtifact(
                filename=f"{prefix}-evidence.txt", mime="text/plain", content=b"recovery evidence"
            ),
            f"{prefix}-artifact",
            artifact_storage=ArtifactStorage(),
        ).resource_id
    )
    schedule_id = str(
        run(
            rt,
            sd.principal("admin1"),
            sch.CreateSchedule(
                name=f"{prefix} nightly",
                cron_expression="0 3 * * *",
                timezone="UTC",
                channel_id=channel_id,
                execution_principal_id=f"acct-{prefix}-svc",
                agent_selection={"mode": "capability", "required_capabilities": ["cap-recovery"]},
                action_template={
                    "schema_id": "action-template.v1",
                    "action": "task_create",
                    "input": {"title": f"{prefix} run", "domain": "research", "risk": "LOW"},
                },
            ),
            f"{prefix}-schedule",
        ).resource_id
    )
    run(rt, sd.principal("admin1"), sch.EnableSchedule(schedule_id=schedule_id), f"{prefix}-enable")
    run_id = str(
        run(
            rt,
            sd.principal("admin1"),
            sch.RunScheduleNow(schedule_id=schedule_id),
            f"{prefix}-runnow",
        ).resource_id
    )
    return Recovery(
        seed=sd,
        channel=channel,
        channel_id=channel_id,
        task_id=task_id,
        approval_id=approval_id,
        schedule_id=schedule_id,
        run_id=run_id,
        artifact_id=artifact_id,
        link_id=link_id,
    )


# --- comparison ---------------------------------------------------------------------------------

# What V-P7-07 requires to be equal after a restore, table by table, with a deterministic order.
STATE_QUERIES: dict[str, str] = {
    "events": (
        "SELECT event_id, aggregate_type, aggregate_id, aggregate_seq, type, content_hash, "
        "previous_hash FROM events WHERE workspace_id = :w ORDER BY recorded_seq"
    ),
    "schedules": (
        "SELECT schedule_id, name, status, current_version_id::text, next_run_at "
        "FROM schedules WHERE workspace_id = :w ORDER BY schedule_id"
    ),
    "schedule_versions": (
        "SELECT v.schedule_version_id, v.schedule_id, v.version, v.snapshot_hash, "
        "v.cron_expression, v.timezone FROM schedule_versions v JOIN schedules s "
        "ON s.schedule_id = v.schedule_id WHERE s.workspace_id = :w "
        "ORDER BY v.schedule_id, v.version"
    ),
    "schedule_runs": (
        "SELECT run_id, schedule_id, run_kind, status, occurrence_key, idempotency_key, "
        "version_hash FROM schedule_runs WHERE workspace_id = :w ORDER BY run_id"
    ),
    "approvals": (
        "SELECT approval_id, subject_type, subject_id, action, risk, status, quorum_required "
        "FROM approval_grants WHERE workspace_id = :w ORDER BY approval_id"
    ),
    "external_identity_links": (
        "SELECT l.link_id, l.external_user_id, l.account_id::text, l.status, "
        "l.verification_method FROM external_identity_links l JOIN accounts a "
        "ON a.id = l.account_id WHERE a.workspace_id = :w ORDER BY l.link_id"
    ),
    "tasks": (
        "SELECT task_id, title, status, risk, domain FROM tasks_projection "
        "WHERE workspace_id = :w ORDER BY task_id"
    ),
    "artifacts": (
        "SELECT artifact_id, storage_uri, mime, size, status FROM artifacts "
        "WHERE workspace_id = :w ORDER BY artifact_id"
    ),
}


def _plain(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return value


def state_hashes(session: Session, workspace_id: uuid.UUID) -> dict[str, str]:
    """One canonical hash per table, so a mismatch names what differs."""
    out: dict[str, str] = {}
    for name, sql in STATE_QUERIES.items():
        rows = session.execute(text(sql), {"w": workspace_id}).all()
        payload = [[_plain(v) for v in row] for row in rows]
        out[name] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return out


def state_rows(session: Session, workspace_id: uuid.UUID, table: str) -> list[list[Any]]:
    rows = session.execute(text(STATE_QUERIES[table]), {"w": workspace_id}).all()
    return [[_plain(v) for v in row] for row in rows]


def settings_fingerprints(session: Session) -> dict[str, str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (setting_key) setting_key, value_fingerprint FROM "
            "settings_versions ORDER BY setting_key, version DESC"
        )
    ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
