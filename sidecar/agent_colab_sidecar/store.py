"""In-memory secret store: values live in ``bytearray`` buffers that are zeroed on revocation,
expiry and exit; the store cannot be pickled or otherwise serialized."""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import SidecarError

log = logging.getLogger("agent_colab_sidecar.store")


class Injector(Protocol):
    """Anything holding a copy of a value in a child/socket; ``invalidate`` must be idempotent."""

    def invalidate(self, reason: str) -> None: ...


def zero(buffer: bytearray) -> None:
    """Overwrite then release the buffer's bytes in place."""
    length = len(buffer)
    if length:
        buffer[:] = bytes(length)
        del buffer[:]


@dataclass
class _Entry:
    handle: str
    value: bytearray = field(repr=False)
    expires_at_mono: float
    injectors: list[Injector] = field(default_factory=list)


class SecretStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        atexit.register(self.wipe_all)

    # never serialized
    def __getstate__(self) -> Any:
        raise TypeError("SecretStore is never serialized")

    def __reduce__(self) -> Any:
        raise TypeError("SecretStore is never serialized")

    def put(self, lease_id: str, handle: str, value: bytearray, ttl_s: float) -> None:
        with self._lock:
            self._entries[lease_id] = _Entry(handle, value, self._clock() + max(ttl_s, 0.0))

    def attach(self, lease_id: str, injector: Injector) -> None:
        with self._lock:
            self._entry(lease_id).injectors.append(injector)

    def _entry(self, lease_id: str) -> _Entry:
        entry = self._entries.get(lease_id)
        if entry is None:
            raise SidecarError("LEASE_UNKNOWN", lease_id)
        return entry

    def view(self, lease_id: str) -> memoryview:
        """Read-only view of a live value (raises ``LEASE_UNKNOWN`` after revocation/expiry)."""
        with self._lock:
            entry = self._entry(lease_id)
            if self._clock() >= entry.expires_at_mono:
                self.revoke(lease_id, "expired")
                raise SidecarError("LEASE_UNKNOWN", lease_id)
            return memoryview(entry.value).toreadonly()

    def lease_ids(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def revoke(self, lease_id: str, reason: str = "revoked") -> bool:
        """Zero the value and invalidate every injector; True when the lease was live."""
        with self._lock:
            entry = self._entries.pop(lease_id, None)
        if entry is None:
            return False
        for injector in entry.injectors:
            try:
                injector.invalidate(reason)
            except Exception:
                log.exception("injector cleanup failed for lease %s", lease_id)
        zero(entry.value)
        log.info(
            "lease %s %s: buffer zeroed, %d injector(s) invalidated",
            lease_id,
            reason,
            len(entry.injectors),
        )
        return True

    def expire(self, now: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        with self._lock:
            due = [lid for lid, e in self._entries.items() if now >= e.expires_at_mono]
        for lease_id in due:
            self.revoke(lease_id, "expired")
        return due

    def wipe_all(self) -> None:
        for lease_id in self.lease_ids():
            self.revoke(lease_id, "shutdown")
