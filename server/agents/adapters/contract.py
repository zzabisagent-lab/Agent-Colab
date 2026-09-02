"""Agent Adapter contract (development plan §7.3; P3-03).

Every adapter type implements ``Adapter``. Adapter *types* are registered by name so that a new
type participates through registration only (V-P3-12 product neutrality): built-ins register
themselves on import, external plugins are listed in ``AGENT_COLAB_ADAPTER_PLUGINS`` as
``module:attribute`` entries. Secret handle *values* never enter this layer's logs or results.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

STABLE_ERROR_CODES = (
    "ADAPTER_TIMEOUT",
    "ADAPTER_UNREACHABLE",
    "ADAPTER_AUTH_FAILED",
    "ADAPTER_BAD_RESPONSE",
    "ADAPTER_RATE_LIMITED",
    "CAPABILITY_UNSUPPORTED",
    "ADAPTER_CANCELLED",
    "ADAPTER_INTERNAL",
)


class DeliveryMode(StrEnum):
    PUSH = "push"
    PULL = "pull"


class AdapterError(Exception):
    """Normalized adapter failure with a stable code (§7.3 normalize_error, CS-11)."""

    def __init__(self, code: str, detail: str = "", retryable: bool = False) -> None:
        if code not in STABLE_ERROR_CODES:
            raise ValueError(f"unknown adapter error code {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class Probe:
    """``probe()`` result: identity and advertised contract."""

    agent_id: str
    adapter_type: str
    runtime: Mapping[str, Any]  # product/model/version/host (optional, informational)
    capabilities: tuple[str, ...]  # capability ids the adapter can execute
    unsupported: tuple[str, ...]  # explicitly declared unsupported tools/features
    delivery_modes: tuple[DeliveryMode, ...]
    limits: Mapping[str, int]  # adapter-side limits (concurrent, rate) if any
    secret_handles: str  # "supported" | "unsupported"
    identity_hash: str  # stable over repeated probes (CS-01)


@dataclass(frozen=True)
class Usage:
    """§7C usage; ``cost_units`` may be absent (server computes from pricing)."""

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    wall_time_ms: int = 0
    cost_units: int | None = None
    usage_unavailable: str | None = None  # reason code when no usage could be measured


@dataclass(frozen=True)
class DeliveryReceipt:
    work_item_id: str
    accepted_at: dt.datetime | None
    rejection_code: str | None = None  # CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER
    receipt_id: str | None = None  # provider-side receipt (webhook 202 receipt, post id ...)


@dataclass(frozen=True)
class InvokeResult:
    result: Mapping[str, Any]
    usage: Usage
    events: Sequence[Mapping[str, Any]] = ()
    artifacts: Sequence[Mapping[str, Any]] = ()
    correlation_id: str | None = None
    task_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class CancelAck:
    target_id: str
    acknowledged_at: dt.datetime
    cleanup_deadline: dt.datetime  # ≤ 60 s after acknowledgement


@dataclass(frozen=True)
class Heartbeat:
    reported_at: dt.datetime
    health: str  # ok | degraded | draining
    capacity: int
    usage_since_last: Usage
    capabilities: tuple[str, ...] = ()  # re-confirmed after a returning heartbeat


@dataclass(frozen=True)
class WorkItemView:
    """Transport-neutral view of a §7B.1 work item handed to ``deliver``."""

    work_item_id: str
    kind: str
    agent_id: str
    task_id: str | None
    correlation_id: str
    deadline: dt.datetime
    payload_ref: str
    secret_handles: tuple[str, ...]
    expected_result_schema: str
    idempotency_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    """§7.3 contract. Implementations must be idempotent per work_item_id and never log secrets."""

    adapter_type: str

    def probe(self) -> Probe: ...

    def deliver(self, item: WorkItemView) -> DeliveryReceipt: ...

    def invoke(
        self,
        tool: str,
        payload: Mapping[str, Any],
        deadline: dt.datetime,
        secret_handles: Sequence[str],
        *,
        correlation_id: str,
    ) -> InvokeResult: ...

    def cancel(self, target_id: str) -> CancelAck: ...

    def heartbeat(self) -> Heartbeat: ...

    def normalize_error(self, exc: BaseException) -> AdapterError: ...


AdapterFactory = Callable[[Mapping[str, Any]], Adapter]  # endpoint config → adapter instance
_TYPES: dict[str, AdapterFactory] = {}


def register_adapter_type(name: str, factory: AdapterFactory, *, replace: bool = False) -> None:
    """Register an adapter type by name (built-ins and plugins alike)."""
    if name in _TYPES and not replace:
        raise ValueError(f"adapter type {name!r} already registered")
    _TYPES[name] = factory


def adapter_types() -> tuple[str, ...]:
    _load_plugins()
    return tuple(sorted(_TYPES))


def adapter_for(adapter_type: str, endpoint: Mapping[str, Any]) -> Adapter:
    _load_plugins()
    try:
        factory = _TYPES[adapter_type]
    except KeyError as exc:
        raise AdapterError("CAPABILITY_UNSUPPORTED", f"adapter type {adapter_type}") from exc
    return factory(endpoint)


_PLUGINS_LOADED = False


def _load_plugins() -> None:
    """Load ``module:attribute`` plugin registrars from AGENT_COLAB_ADAPTER_PLUGINS once."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    for entry in filter(None, os.environ.get("AGENT_COLAB_ADAPTER_PLUGINS", "").split(",")):
        module_name, _, attr = entry.strip().partition(":")
        module = importlib.import_module(module_name)
        registrar = getattr(module, attr or "register")
        registrar(register_adapter_type)


def reset_plugins_for_tests() -> None:
    global _PLUGINS_LOADED
    _PLUGINS_LOADED = False
