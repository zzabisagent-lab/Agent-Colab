"""Lease/handle primitives shared by the Broker, the in-memory injector and the sidecar API.

* handles are ``sh-<32 hex>``; only ``sha256(handle)`` is stored (``secret_leases.handle_hash``)
* :class:`LiveHandles` is the in-process registry of handles that may still be resolved; a
  revocation clears it immediately and notifies listeners (in-memory injectors wipe bytes),
  which is how the ≤ 5 s cleanup of V-P4-13 is met inside one process
* the durable revocation feed (``secret_revocations``) is what sidecars poll or subscribe to
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets as _secrets
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_TTL = dt.timedelta(minutes=5)
MAX_TTL = dt.timedelta(hours=1)


def new_handle() -> str:
    return "sh-" + _secrets.token_hex(16)


def handle_hash(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def is_handle(value: str) -> bool:
    return value.startswith("sh-") and len(value) == 35


RevokeListener = Callable[[list[str], str], None]  # (lease_ids, reason)


class LiveHandles:
    """Process-local registry: lease ids currently resolvable, keyed by handle hash."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_hash: dict[str, str] = {}
        self._listeners: list[RevokeListener] = []

    def add(self, hash_: str, lease_id: str) -> None:
        with self._lock:
            self._by_hash[hash_] = lease_id

    def lease_id(self, hash_: str) -> str | None:
        with self._lock:
            return self._by_hash.get(hash_)

    def drop(self, hash_: str) -> None:
        with self._lock:
            self._by_hash.pop(hash_, None)

    def subscribe(self, listener: RevokeListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def revoke(self, lease_ids: Iterable[str], reason: str) -> int:
        ids = set(lease_ids)
        with self._lock:
            gone = [h for h, lid in self._by_hash.items() if lid in ids]
            for h in gone:
                del self._by_hash[h]
            listeners = list(self._listeners)
        for listener in listeners:
            listener(sorted(ids), reason)
        return len(gone)

    def clear(self) -> None:
        with self._lock:
            self._by_hash.clear()


LIVE = LiveHandles()


@dataclass(frozen=True)
class Revocation:
    seq: int
    kind: str
    target_id: str
    lease_ids: tuple[str, ...]
    reason: str
    occurred_at: dt.datetime


def record_revocation(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    target_id: str,
    lease_ids: Iterable[str],
    reason: str,
    now: dt.datetime,
) -> Revocation:
    ids = tuple(sorted(set(lease_ids)))
    seq = session.execute(
        text(
            "INSERT INTO secret_revocations (workspace_id, kind, target_id, lease_ids, reason, "
            "occurred_at) VALUES (:w, :k, :t, CAST(:l AS jsonb), :r, :n) RETURNING seq"
        ),
        {
            "w": workspace_id,
            "k": kind,
            "t": target_id,
            "l": json.dumps(list(ids)),
            "r": reason,
            "n": now,
        },
    ).scalar_one()
    return Revocation(int(seq), kind, target_id, ids, reason, now)


def revocations_since(
    session: Session, seq: int, *, workspace_id: uuid.UUID | None = None, limit: int = 200
) -> list[Revocation]:
    """Feed entries after ``seq`` (sidecars keep the last seq they saw)."""
    rows = session.execute(
        text(
            "SELECT seq, kind, target_id, lease_ids, reason, occurred_at FROM secret_revocations "
            "WHERE seq > :s AND (CAST(:w AS uuid) IS NULL OR workspace_id = :w) "
            "ORDER BY seq LIMIT :l"
        ),
        {"s": seq, "w": workspace_id, "l": limit},
    ).all()
    return [
        Revocation(int(r[0]), str(r[1]), str(r[2]), tuple(r[3] or ()), str(r[4]), r[5])
        for r in rows
    ]


def revocation_dict(rev: Revocation) -> dict[str, Any]:
    return {
        "seq": rev.seq,
        "kind": rev.kind,
        "target_id": rev.target_id,
        "lease_ids": list(rev.lease_ids),
        "reason": rev.reason,
        "occurred_at": rev.occurred_at.isoformat(),
    }
