"""Agent Registry read model and lifecycle fold (P3-01; spec §4.2, development plan §7.3).

The ``agents`` row is an authority row created at registration; its *runtime* columns (status,
online, capacity, heartbeat bookkeeping, ``lifecycle_hash``) are a projection of the Agent's
``AGENT_*`` Event stream. ``fold`` is the single source of truth used both by the live command
handlers and by ``rebuild`` (V-P3-17: a rebuild reproduces identical state and lifecycle hash).
Secrets never live here: ``endpoint`` is rejected when it carries secret-looking values and only
``credential_ref`` (a Secret Broker reference) is stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.events.hashing import sha256_hex
from server.events.store import EventStore

ADAPTER_TYPES = ("mcp", "webhook", "mattermost_bot")
STATUSES = ("pending", "active", "suspended", "revoked", "offline")
LIMIT_KEYS = (
    "concurrent_tasks",
    "requests_per_minute",
    "brainstorm_turns",
    "daily_cost_units",
    "per_task_cost_units",
    "per_task_wall_ms",
)
HEALTH_VALUES = ("ok", "degraded", "draining")
HEARTBEAT_INTERVAL_S = 30
OFFLINE_AFTER_MISSES = 3
OFFLINE_AFTER_S = 90
LIFECYCLE_TYPES = (
    "AGENT_REGISTERED",
    "AGENT_UPDATED",
    "AGENT_ACTIVATED",
    "AGENT_SUSPENDED",
    "AGENT_REVOKED",
    "AGENT_HEARTBEAT_RECORDED",
    "AGENT_MARKED_OFFLINE",
)
AGENT_ID_RE = re.compile(r"^agent-[a-z0-9][a-z0-9-]{1,62}$")
_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|bearer)", re.I
)
_SECRET_VALUE_RE = re.compile(r"^(sk-|xox[abp]-|ghp_|AKIA|-----BEGIN )")


class RegistryError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 400) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


# ------------------------------------------------------------------ validation


def validate_agent_id(agent_id: str) -> None:
    if not AGENT_ID_RE.match(agent_id):
        raise RegistryError("AGENT_ID_INVALID", "expected agent-<slug>")


def validate_adapter_type(adapter_type: str) -> None:
    if adapter_type not in ADAPTER_TYPES:
        raise RegistryError("AGENT_ADAPTER_TYPE_INVALID", adapter_type)


def reject_secret_values(endpoint: Mapping[str, Any], path: str = "endpoint") -> None:
    """Endpoint config may reference secrets (``*_ref``) but never carry their values."""
    for key, value in endpoint.items():
        here = f"{path}.{key}"
        if isinstance(value, Mapping):
            reject_secret_values(value, here)
            continue
        if isinstance(value, str):
            if _SECRET_KEY_RE.search(str(key)) and not str(key).endswith("_ref") and value:
                raise RegistryError("AGENT_ENDPOINT_SECRET_VALUE", f"{here} must be a reference")
            if _SECRET_VALUE_RE.match(value):
                raise RegistryError("AGENT_ENDPOINT_SECRET_VALUE", f"{here} looks like a secret")


def validate_limits(limits: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in limits.items():
        if key not in LIMIT_KEYS:
            raise RegistryError("AGENT_LIMIT_KEY_INVALID", str(key))
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RegistryError("AGENT_LIMIT_VALUE_INVALID", f"{key} must be a non-negative int")
        out[key] = value
    return out


def validate_delivery_modes(modes: Iterable[str]) -> list[str]:
    out = sorted(set(modes))
    if not out or any(m not in ("push", "pull") for m in out):
        raise RegistryError("AGENT_DELIVERY_MODES_INVALID", "expected push and/or pull")
    return out


# ------------------------------------------------------------------ lifecycle fold


@dataclass(frozen=True)
class AgentState:
    """Runtime projection of one Agent, folded from its ``AGENT_*`` stream."""

    agent_id: str
    status: str = "pending"
    online: bool = False
    capacity: int = 1
    last_heartbeat_at: dt.datetime | None = None
    missed_heartbeats: int = 0
    lifecycle_hash: str = ""
    last_event_id: str | None = None
    last_aggregate_seq: int = 0
    capabilities: tuple[str, ...] = ()
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "online": self.online,
            "capacity": self.capacity,
            "last_heartbeat_at": (
                None if self.last_heartbeat_at is None else self.last_heartbeat_at.isoformat()
            ),
            "missed_heartbeats": self.missed_heartbeats,
            "lifecycle_hash": self.lifecycle_hash,
            "last_event_id": self.last_event_id,
            "last_aggregate_seq": self.last_aggregate_seq,
        }


def chain_hash(previous: str, event: Mapping[str, Any]) -> str:
    """SHA-256 chain over the lifecycle: previous hash + (event_id, type, seq, occurred_at)."""
    link = canonical_json(
        {
            "prev": previous,
            "event_id": event["event_id"],
            "type": event["type"],
            "aggregate_seq": event["aggregate_seq"],
            "occurred_at": str(event.get("occurred_at")),
        }
    )
    return hashlib.sha256(link).hexdigest()


def _as_dt(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def fold(agent_id: str, events: Iterable[Mapping[str, Any]]) -> AgentState:
    state = AgentState(agent_id=agent_id)
    history: list[dict[str, Any]] = []
    for ev in events:
        etype = str(ev["type"])
        if etype not in LIFECYCLE_TYPES:
            continue
        payload = dict(ev.get("payload") or {})
        occurred = _as_dt(ev.get("occurred_at"))
        if etype == "AGENT_REGISTERED":
            state = replace(state, status="pending")
        elif etype == "AGENT_ACTIVATED":
            state = replace(state, status="active")
        elif etype == "AGENT_SUSPENDED":
            state = replace(state, status="suspended", online=False)
        elif etype == "AGENT_REVOKED":
            state = replace(state, status="revoked", online=False)
        elif etype == "AGENT_HEARTBEAT_RECORDED":
            caps = payload.get("capabilities")
            state = replace(
                state,
                online=True,
                capacity=int(payload.get("capacity", state.capacity)),
                last_heartbeat_at=occurred,
                missed_heartbeats=0,
                status="active" if state.status in ("active", "offline") else state.status,
                capabilities=tuple(caps) if isinstance(caps, list) and caps else state.capabilities,
            )
        elif etype == "AGENT_MARKED_OFFLINE":
            state = replace(
                state,
                online=False,
                status="offline" if state.status in ("active", "offline") else state.status,
                missed_heartbeats=int(payload.get("missed_heartbeats", OFFLINE_AFTER_MISSES)),
            )
        digest = chain_hash(state.lifecycle_hash, ev)
        history.append(
            {
                "event_id": str(ev["event_id"]),
                "type": etype,
                "aggregate_seq": int(ev["aggregate_seq"]),
                "occurred_at": None if occurred is None else occurred.isoformat(),
                "status": state.status,
                "lifecycle_hash": digest,
            }
        )
        state = replace(
            state,
            lifecycle_hash=digest,
            last_event_id=str(ev["event_id"]),
            last_aggregate_seq=int(ev["aggregate_seq"]),
        )
    return replace(state, history=tuple(history))


# ------------------------------------------------------------------ rows


@dataclass(frozen=True)
class AgentRow:
    id: uuid.UUID
    agent_id: str
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    account_public_id: str
    adapter_type: str
    status: str
    display_name: str
    owner_account_id: uuid.UUID | None
    endpoint: dict[str, Any]
    credential_ref: str | None
    runtime_metadata: dict[str, Any]
    limits: dict[str, int]
    delivery_modes: list[str]
    capabilities_snapshot: dict[str, Any]
    capacity: int
    online: bool
    last_heartbeat_at: dt.datetime | None
    missed_heartbeats: int
    lifecycle_hash: str | None
    last_event_id: str | None
    last_aggregate_seq: int
    account_status: str


_COLS = (
    "ag.id, ag.agent_id, ag.workspace_id, ag.account_id, a.account_id, ag.adapter_type, "
    "ag.status, ag.display_name, ag.owner_account_id, ag.endpoint, ag.credential_ref, "
    "ag.runtime_metadata, ag.limits, ag.delivery_modes, ag.capabilities_snapshot, ag.capacity, "
    "ag.online, ag.last_heartbeat_at, ag.missed_heartbeats, ag.lifecycle_hash, ag.last_event_id, "
    "ag.last_aggregate_seq, a.status"
)


def _row(r: Any) -> AgentRow:
    return AgentRow(
        id=r[0],
        agent_id=str(r[1]),
        workspace_id=r[2],
        account_id=r[3],
        account_public_id=str(r[4]),
        adapter_type=str(r[5]),
        status=str(r[6]),
        display_name=str(r[7]),
        owner_account_id=r[8],
        endpoint=dict(r[9] or {}),
        credential_ref=None if r[10] is None else str(r[10]),
        runtime_metadata=dict(r[11] or {}),
        limits={k: int(v) for k, v in dict(r[12] or {}).items()},
        delivery_modes=list(r[13] or []),
        capabilities_snapshot=dict(r[14] or {}),
        capacity=int(r[15]),
        online=bool(r[16]),
        last_heartbeat_at=_as_dt(r[17]),
        missed_heartbeats=int(r[18]),
        lifecycle_hash=None if r[19] is None else str(r[19]),
        last_event_id=None if r[20] is None else str(r[20]),
        last_aggregate_seq=int(r[21]),
        account_status=str(r[22]),
    )


def load_agent(
    session: Session, workspace_id: uuid.UUID, agent_id: str, *, for_update: bool = False
) -> AgentRow | None:
    lock = " FOR UPDATE OF ag" if for_update else ""
    row = session.execute(
        text(
            f"SELECT {_COLS} FROM agents ag JOIN accounts a ON a.id = ag.account_id "  # noqa: S608
            f"WHERE ag.agent_id = :g AND ag.workspace_id = :ws{lock}"
        ),
        {"g": agent_id, "ws": workspace_id},
    ).first()
    return None if row is None else _row(row)


def agent_for_account(session: Session, account_uuid: uuid.UUID) -> AgentRow | None:
    row = session.execute(
        text(
            f"SELECT {_COLS} FROM agents ag JOIN accounts a ON a.id = ag.account_id "  # noqa: S608
            "WHERE ag.account_id = :a"
        ),
        {"a": account_uuid},
    ).first()
    return None if row is None else _row(row)


def list_agents(session: Session, workspace_id: uuid.UUID, limit: int = 100) -> list[AgentRow]:
    rows = session.execute(
        text(
            f"SELECT {_COLS} FROM agents ag JOIN accounts a ON a.id = ag.account_id "  # noqa: S608
            "WHERE ag.workspace_id = :ws ORDER BY ag.agent_id LIMIT :lim"
        ),
        {"ws": workspace_id, "lim": min(max(limit, 1), 100)},
    ).all()
    return [_row(r) for r in rows]


def public_view(row: AgentRow) -> dict[str, Any]:
    """API view; never includes credential material (only the reference name)."""
    return {
        "agent_id": row.agent_id,
        "account_id": row.account_public_id,
        "display_name": row.display_name,
        "adapter_type": row.adapter_type,
        "status": row.status,
        "online": row.online,
        "capacity": row.capacity,
        "owner_account_id": None if row.owner_account_id is None else str(row.owner_account_id),
        "endpoint": row.endpoint,
        "credential_ref": row.credential_ref,
        "runtime_metadata": row.runtime_metadata,
        "limits": row.limits,
        "delivery_modes": row.delivery_modes,
        "capabilities_snapshot": row.capabilities_snapshot,
        "last_heartbeat_at": (
            None if row.last_heartbeat_at is None else row.last_heartbeat_at.isoformat()
        ),
        "missed_heartbeats": row.missed_heartbeats,
        "lifecycle_hash": row.lifecycle_hash,
        "last_event_id": row.last_event_id,
        "last_aggregate_seq": row.last_aggregate_seq,
    }


def write_state(session: Session, state: AgentState, now: dt.datetime) -> None:
    """Persist the folded runtime state onto the ``agents`` row (read-after-write)."""
    session.execute(
        text(
            "UPDATE agents SET status = :st, online = :on, capacity = :cap, "
            "last_heartbeat_at = :hb, missed_heartbeats = :miss, lifecycle_hash = :lh, "
            "last_event_id = :le, last_aggregate_seq = :ls, updated_at = :now "
            "WHERE agent_id = :g"
        ),
        {
            "st": state.status,
            "on": state.online,
            "cap": state.capacity,
            "hb": state.last_heartbeat_at,
            "miss": state.missed_heartbeats,
            "lh": state.lifecycle_hash or None,
            "le": state.last_event_id,
            "ls": state.last_aggregate_seq,
            "now": now,
            "g": state.agent_id,
        },
    )


def refresh_state(
    session: Session, store: EventStore, workspace_id: str, agent_id: str, now: dt.datetime
) -> AgentState:
    """Fold the Agent's stream and write the runtime columns; returns the folded state."""
    state = fold(agent_id, store.stream(workspace_id, "agent", agent_id))
    write_state(session, state, now)
    return state


def state_hash(session: Session, workspace_id: uuid.UUID) -> str:
    """Canonical hash of every Agent's runtime state in the Workspace (V-P3-17)."""
    rows = session.execute(
        text(
            "SELECT agent_id, status, online, capacity, last_heartbeat_at, missed_heartbeats, "
            "lifecycle_hash, last_event_id, last_aggregate_seq FROM "
            "agents WHERE workspace_id = :ws "
            "ORDER BY agent_id"
        ),
        {"ws": workspace_id},
    ).all()
    plain = [
        {
            "agent_id": str(r[0]),
            "status": str(r[1]),
            "online": bool(r[2]),
            "capacity": int(r[3]),
            "last_heartbeat_at": None if r[4] is None else _as_dt(r[4]).isoformat(),  # type: ignore[union-attr]
            "missed_heartbeats": int(r[5]),
            "lifecycle_hash": r[6],
            "last_event_id": r[7],
            "last_aggregate_seq": int(r[8]),
        }
        for r in rows
    ]
    return sha256_hex(canonical_json(plain))


def rebuild(session: Session, store: EventStore, workspace_id: str, now: dt.datetime) -> str:
    """Recompute every Agent's runtime columns from Events; returns the state hash."""
    ws = uuid.UUID(workspace_id)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"projection:agents:{ws}"}
    )
    ids = [
        str(r[0])
        for r in session.execute(
            text("SELECT agent_id FROM agents WHERE workspace_id = :ws ORDER BY agent_id"),
            {"ws": ws},
        ).all()
    ]
    for agent_id in ids:
        refresh_state(session, store, workspace_id, agent_id, now)
    return state_hash(session, ws)


def lifecycle_history(store: EventStore, workspace_id: str, agent_id: str) -> AgentState:
    return fold(agent_id, store.stream(workspace_id, "agent", agent_id))


def missed_heartbeats_at(last_heartbeat_at: dt.datetime | None, now: dt.datetime) -> int:
    if last_heartbeat_at is None:
        return OFFLINE_AFTER_MISSES
    elapsed = (now - last_heartbeat_at).total_seconds()
    return max(0, int(elapsed // HEARTBEAT_INTERVAL_S))


def is_offline_due(last_heartbeat_at: dt.datetime | None, now: dt.datetime) -> bool:
    """§7.3: offline after 3 consecutive misses or 90 s without a heartbeat."""
    if last_heartbeat_at is None:
        return True
    return (now - last_heartbeat_at).total_seconds() >= OFFLINE_AFTER_S or (
        missed_heartbeats_at(last_heartbeat_at, now) >= OFFLINE_AFTER_MISSES
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
