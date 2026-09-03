"""Adapter injection (development plan §9.3/§9.4; P4-07).

In-process adapters (``mcp`` pull with an in-memory handle, ``webhook``) obtain bytes through
:class:`InMemoryHandleStore`, which resolves through the Broker, keeps the value only in a
``bytearray`` and zeroes it on revoke (listener on :data:`server.secrets.leases.LIVE`), at Task
end or on explicit cleanup — the in-memory counterpart of the sidecar. :class:`SecretLogFilter`
scrubs any live value from log records so no logger can print one.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.secrets import broker
from server.secrets import leases as ls
from server.secrets.envelope import MasterKey
from server.secrets.provider import ResolveContext, SecretError

_STORES: list[InMemoryHandleStore] = []
_STORES_LOCK = threading.Lock()


class InMemoryHandleStore:
    """Resolve handles for in-process adapters; values live only in wipeable buffers."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        master: MasterKey,
        *,
        workspace_id: uuid.UUID,
        clock: Clock | None = None,
    ) -> None:
        self._factory = session_factory
        self._master = master
        self._workspace = workspace_id
        self._clock = clock or SystemClock()
        self._values: dict[str, bytearray] = {}  # lease_id -> value
        self._handles: dict[str, str] = {}  # handle hash -> lease_id
        self._lock = threading.Lock()
        self.wiped: list[tuple[str, str]] = []  # (lease_id, reason)
        ls.LIVE.subscribe(self._on_revoked)
        with _STORES_LOCK:
            _STORES.append(self)

    # -- resolve ---------------------------------------------------------------------------
    def resolve(self, handle: str, context: ResolveContext, *, store: Any = None) -> bytes:
        with self._factory() as s, s.begin():
            actor_uuid = self._agent_account(s, context.agent_id)
            if store is None and actor_uuid is not None:
                from server.events.postgres_store import PostgresEventStore

                store = PostgresEventStore(s, clock=self._clock)
            value = broker.resolve(
                s,
                self._master,
                workspace_id=self._workspace,
                handle=handle,
                context=context,
                now=self._clock.now(),
                actor_uuid=actor_uuid,
                actor_label=context.agent_id,
                correlation_id=f"inject:{context.work_item_id or context.agent_id}",
                store=store,
            )
            lease_id = ls.LIVE.lease_id(ls.handle_hash(handle)) or self._lease_id_of(s, handle)
        buf = bytearray(value)
        with self._lock:
            self._values[lease_id] = buf
            self._handles[ls.handle_hash(handle)] = lease_id
        return bytes(buf)

    @staticmethod
    def _agent_account(session: Session, agent_id: str) -> str | None:
        from sqlalchemy import text

        row = session.execute(
            text("SELECT account_id FROM agents WHERE agent_id = :a"), {"a": agent_id}
        ).first()
        return None if row is None else str(row[0])

    @staticmethod
    def _lease_id_of(session: Session, handle: str) -> str:
        from sqlalchemy import text

        row = session.execute(
            text("SELECT lease_id FROM secret_leases WHERE handle_hash = :h"),
            {"h": ls.handle_hash(handle)},
        ).first()
        return str(row[0]) if row else "lease-unknown"

    # -- cleanup ---------------------------------------------------------------------------
    def _wipe(self, lease_ids: Iterable[str], reason: str) -> int:
        n = 0
        with self._lock:
            for lid in list(lease_ids):
                buf = self._values.pop(lid, None)
                if buf is not None:
                    for i in range(len(buf)):
                        buf[i] = 0
                    n += 1
                    self.wiped.append((lid, reason))
            for h, lid in list(self._handles.items()):
                if lid in set(lease_ids):
                    del self._handles[h]
        return n

    def _on_revoked(self, lease_ids: list[str], reason: str) -> None:
        self._wipe(lease_ids, reason)

    def cleanup(self, reason: str = "CLEANUP") -> int:
        with self._lock:
            ids = list(self._values)
        return self._wipe(ids, reason)

    def live_lease_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._values)

    def holds(self, lease_id: str) -> bool:
        with self._lock:
            return lease_id in self._values and any(self._values[lease_id])

    def live_values(self) -> list[bytes]:
        with self._lock:
            return [bytes(v) for v in self._values.values() if any(v)]

    def close(self) -> None:
        self.cleanup("CLOSE")
        with _STORES_LOCK:
            if self in _STORES:
                _STORES.remove(self)


def live_values() -> list[bytes]:
    with _STORES_LOCK:
        stores = list(_STORES)
    out: list[bytes] = []
    for st in stores:
        out.extend(st.live_values())
    return out


class SecretLogFilter(logging.Filter):
    """Scrub live secret values (and registered canaries) from every log record."""

    def __init__(self, extra_values: Callable[[], Iterable[bytes]] | None = None) -> None:
        super().__init__("agent_colab_secret_scrub")
        self._extra = extra_values

    def _values(self) -> list[str]:
        raw = list(live_values())
        if self._extra is not None:
            raw.extend(self._extra())
        out: list[str] = []
        for v in raw:
            try:
                s = v.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if s:
                out.append(s)
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        values = self._values()
        if not values:
            return True
        msg = record.getMessage()
        scrubbed = msg
        for v in values:
            scrubbed = scrubbed.replace(v, "<secret-redacted>")
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


def install_log_filter(
    extra_values: Callable[[], Iterable[bytes]] | None = None,
) -> SecretLogFilter:
    """Attach the scrubber to every handler of the root logger (idempotent)."""
    root = logging.getLogger()
    flt = next((f for f in root.filters if isinstance(f, SecretLogFilter)), None)
    if flt is None:
        flt = SecretLogFilter(extra_values)
        root.addFilter(flt)
    elif extra_values is not None:
        flt._extra = extra_values
    for handler in root.handlers:  # handlers attached since the last call get the scrubber too
        if flt not in handler.filters:
            handler.addFilter(flt)
    return flt


__all__ = [
    "InMemoryHandleStore",
    "SecretError",
    "SecretLogFilter",
    "install_log_filter",
    "live_values",
]
