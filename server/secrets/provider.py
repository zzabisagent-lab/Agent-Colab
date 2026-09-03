"""Secret Broker provider interface (development plan §9.1; P4-05/P4-06).

``put/lease/resolve/revoke/rotate/health`` are the contract every provider implements. Values are
bytes in memory only: no provider logs, Events, errors or handles ever carry a value, a length or
a hash of it. The mandatory v8 provider is the encrypted local provider (P4-05); an external
provider can be registered with :func:`register_provider`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

SECRET_ERROR_CODES = (
    "SECRET_NOT_FOUND",
    "SECRET_SCOPE_DENIED",
    "SECRET_LEASE_EXPIRED",
    "SECRET_HANDLE_USED",
    "SECRET_HANDLE_REVOKED",
    "SECRET_HANDLE_HOST_MISMATCH",
    "SECRET_EXPOSURE_APPROVAL_REQUIRED",
    "SECRET_PROVIDER_UNAVAILABLE",
)


class SecretError(Exception):
    """Stable, value-free secret failure (never includes the secret or its length)."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in SECRET_ERROR_CODES:
            raise ValueError(f"unknown secret error code {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SecretRef:
    secret_ref: str  # sec-... (opaque, no value material)
    version: int
    provider: str
    metadata: Mapping[str, Any] = field(default_factory=dict)  # name, owner, rotation, tags


@dataclass(frozen=True)
class LeaseScope:
    """What a lease may be used for (§9.3): Task, action, Agent, optional sidecar instance."""

    agent_id: str
    task_id: str | None = None
    action: str | None = None
    work_item_id: str | None = None
    sidecar_instance_id: str | None = None  # host binding (§9.4)


@dataclass(frozen=True)
class Lease:
    lease_id: str
    handle: str  # sh-<hex>: the one-time handle handed to the Adapter/sidecar
    secret_ref: str
    scope: LeaseScope
    issued_at: dt.datetime
    expires_at: dt.datetime
    single_use: bool = True


@dataclass(frozen=True)
class ResolveContext:
    """Who is resolving: the authenticated Adapter/sidecar identity."""

    agent_id: str
    sidecar_instance_id: str | None = None
    task_id: str | None = None
    action: str | None = None
    work_item_id: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    ok: bool
    detail: str = ""
    checked_at: dt.datetime | None = None


@runtime_checkable
class SecretProvider(Protocol):
    name: str

    def put(self, name: str, value: bytes, metadata: Mapping[str, Any]) -> SecretRef: ...

    def lease(
        self, secret_ref: str, scope: LeaseScope, ttl: dt.timedelta, *, single_use: bool = True
    ) -> Lease: ...

    def resolve(self, handle: str, context: ResolveContext) -> bytes: ...

    def revoke(self, grant_or_lease_id: str) -> int: ...  # number of leases/handles invalidated

    def rotate(self, secret_ref: str, value: bytes) -> SecretRef: ...

    def health(self) -> ProviderHealth: ...


ProviderFactory = Callable[[Mapping[str, Any]], SecretProvider]
_PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    if name in _PROVIDERS and not replace:
        raise ValueError(f"secret provider {name!r} already registered")
    _PROVIDERS[name] = factory


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def provider_for(name: str, config: Mapping[str, Any]) -> SecretProvider:
    try:
        factory = _PROVIDERS[name]
    except KeyError as exc:
        raise SecretError("SECRET_PROVIDER_UNAVAILABLE", f"provider {name}") from exc
    return factory(config)
