"""Pre-DB secret handles (spec §12): process-memory only, 15-minute TTL — P0-09.

Values entered before the DB exists (DB password, initial key material) live only in this
store. A restarted process starts with an empty store by construction, so the operator must
re-enter them. Handles cannot be pickled, printed, or logged with their value.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from typing import Any, NoReturn

from server.domain.clock import Clock
from server.domain.defaults import SETUP_PRE_DB_HANDLE_TTL_MIN
from server.setup.errors import SetupError


@dataclass(eq=False)
class SecretHandle:
    handle_id: str
    name: str
    expires_at: dt.datetime
    _value: bytearray = field(repr=False)

    def __repr__(self) -> str:
        return f"SecretHandle(id={self.handle_id}, name={self.name}, value=<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretHandle cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("SecretHandle cannot be serialized")

    def wipe(self) -> None:
        for i in range(len(self._value)):
            self._value[i] = 0
        self._value = bytearray()


class PreDbHandleStore:
    def __init__(self, clock: Clock, ttl_minutes: int = SETUP_PRE_DB_HANDLE_TTL_MIN) -> None:
        self._clock = clock
        self._ttl = dt.timedelta(minutes=ttl_minutes)
        self._handles: dict[str, SecretHandle] = {}

    def put(self, name: str, value: bytes | str) -> SecretHandle:
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        handle = SecretHandle(
            handle_id="h-" + secrets.token_hex(16),
            name=name,
            expires_at=self._clock.now() + self._ttl,
            _value=bytearray(raw),
        )
        self._handles[handle.handle_id] = handle
        return handle

    def resolve(self, handle_id: str) -> bytes:
        self.purge_expired()
        handle = self._handles.get(handle_id)
        if handle is None:
            raise SetupError(
                "SETUP_HANDLE_EXPIRED", "handle unknown or expired; re-enter the value"
            )
        return bytes(handle._value)

    def revoke(self, handle_id: str) -> None:
        handle = self._handles.pop(handle_id, None)
        if handle is not None:
            handle.wipe()

    def purge_expired(self) -> int:
        now = self._clock.now()
        expired = [h for h in self._handles.values() if now >= h.expires_at]
        for h in expired:
            self.revoke(h.handle_id)
        return len(expired)

    def __len__(self) -> int:
        return len(self._handles)

    def __reduce__(self) -> NoReturn:
        raise TypeError("PreDbHandleStore cannot be serialized")

    def snapshot_for_store(self) -> dict[str, Any]:
        """The only thing that may leave the process: handle ids and expiry, never values."""
        return {
            h.handle_id: {"name": h.name, "expires_at": h.expires_at.isoformat()}
            for h in self._handles.values()
        }
