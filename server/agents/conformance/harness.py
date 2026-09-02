"""Conformance harness: a virtual clock plus a simulated Agent behind an :class:`InboxPort`.

The suite drives any adapter through the same twelve checks; the harness supplies what the
adapter cannot know by itself — when the simulated Agent acked/accepted, how many side effects a
work item produced, which failure is injected, what the Agent logged, and the disconnect /
reconnect switch. Other adapter types ship their own :class:`Harness` (same protocol) built on
their transport double; :class:`McpSimulationHarness` is the built-in one for ``mcp``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.agents.adapters.contract import (
    Adapter,
    AdapterError,
    CancelAck,
    DeliveryReceipt,
    Heartbeat,
    Usage,
    WorkItemView,
    adapter_for,
    load_plugins,
)

FAILURE_KINDS = ("timeout", "unreachable", "auth", "bad_response", "rate_limited")
EXPECTED_ERROR_CODES = {
    "timeout": "ADAPTER_TIMEOUT",
    "unreachable": "ADAPTER_UNREACHABLE",
    "auth": "ADAPTER_AUTH_FAILED",
    "bad_response": "ADAPTER_BAD_RESPONSE",
    "rate_limited": "ADAPTER_RATE_LIMITED",
}


class VirtualClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 1, tzinfo=dt.UTC)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)


class Harness(Protocol):
    """What the suite needs from an adapter type's test double."""

    adapter_type: str
    clock: VirtualClock

    def adapter(self) -> Adapter: ...

    def side_effects(self, work_item_id: str) -> int: ...

    def results(self, work_item_id: str) -> int: ...

    def acked_at(self, work_item_id: str) -> dt.datetime | None: ...

    def accepted_at(self, work_item_id: str) -> dt.datetime | None: ...

    def logs(self) -> list[str]: ...

    def inject_failure(self, kind: str | None) -> None: ...

    def disconnect(self) -> None: ...

    def reconnect(self) -> list[str]: ...  # work item ids re-received after reconnect

    def heartbeats(self, count: int) -> list[Heartbeat]: ...

    def cancel_timing(self, ack: CancelAck) -> tuple[float, float]: ...  # (ack_s, cleanup_s)


@dataclass
class _Item:
    view: WorkItemView
    delivered_at: dt.datetime
    acked_at: dt.datetime | None = None
    accepted_at: dt.datetime | None = None
    result: dict[str, Any] | None = None
    deliveries: int = 1


@dataclass
class SimulatedAgent:
    """An MCP-client Agent living behind an in-memory inbox (the harness's InboxPort)."""

    clock: VirtualClock
    ack_delay_s: float = 5.0
    accept_delay_s: float = 20.0
    result_delay_s: float = 30.0
    cancel_ack_s: float = 3.0
    cancel_cleanup_s: float = 20.0
    heartbeat_interval_s: float = 30.0
    include_usage: bool = True
    echo_correlation: bool = True
    leak_secrets: bool = False
    items: dict[str, _Item] = field(default_factory=dict)
    side_effect_log: dict[str, int] = field(default_factory=dict)
    result_count: dict[str, int] = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    failure: str | None = None
    connected: bool = True
    pending_redelivery: list[str] = field(default_factory=list)
    heartbeat_log: list[Heartbeat] = field(default_factory=list)

    # ---- InboxPort ---------------------------------------------------------------------
    def now(self) -> dt.datetime:
        return self.clock.now()

    def enqueue(self, item: WorkItemView) -> DeliveryReceipt:
        self._raise_injected()
        existing = self.items.get(item.work_item_id)
        if existing is not None:  # redelivery: same receipt, no new side effect
            existing.deliveries += 1
            return DeliveryReceipt(
                item.work_item_id, existing.delivered_at, receipt_id=item.work_item_id
            )
        now = self.clock.now()
        self.items[item.work_item_id] = _Item(item, now)
        self.log_lines.append(
            f"delivered {item.work_item_id} kind={item.kind} corr={item.correlation_id}"
        )
        if not self.connected:
            self.pending_redelivery.append(item.work_item_id)
            return DeliveryReceipt(item.work_item_id, now, receipt_id=item.work_item_id)
        self._process(item.work_item_id)
        return DeliveryReceipt(item.work_item_id, now, receipt_id=item.work_item_id)

    def _process(self, work_item_id: str) -> None:
        rec = self.items[work_item_id]
        base = rec.delivered_at
        rec.acked_at = base + dt.timedelta(seconds=self.ack_delay_s)
        if rec.view.kind in ("task_assignment", "subtask_assignment"):
            rec.accepted_at = base + dt.timedelta(seconds=self.accept_delay_s)
        if rec.view.kind == "invoke" and rec.result is None:
            self.side_effect_log[work_item_id] = self.side_effect_log.get(work_item_id, 0) + 1
            secrets = list(rec.view.secret_handles)
            if self.leak_secrets and secrets:
                self.log_lines.append(f"using secret {secrets[0]}")
            else:
                self.log_lines.append(f"using {len(secrets)} secret handle(s)")
            corr = rec.view.correlation_id if self.echo_correlation else "corr-lost"
            doc: dict[str, Any] = {
                "schema_id": "colab.work-result.v1",
                "work_item_id": work_item_id,
                "correlation_id": corr,
                "task_id": rec.view.task_id,
                "status": "SUCCEEDED",
                "result": {
                    "echo": dict(rec.view.payload.get("input", {})),
                    "tool": rec.view.payload.get("tool"),
                },
                "events": [],
                "artifacts": [],
            }
            if self.include_usage:
                doc["usage"] = {
                    "model": "sim-1",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "tool_calls": 1,
                    "wall_time_ms": 40,
                }
            else:
                doc["usage_unavailable"] = {"reason": "ADAPTER_NO_METERING"}
            if self.leak_secrets and secrets:
                doc["result"]["debug"] = secrets[0]
            rec.result = doc
            self.result_count[work_item_id] = self.result_count.get(work_item_id, 0) + 1

    def await_result(self, work_item_id: str, deadline: dt.datetime) -> dict[str, Any] | None:
        rec = self.items.get(work_item_id)
        if rec is None or rec.result is None:
            return None
        ready = rec.delivered_at + dt.timedelta(seconds=self.result_delay_s)
        if ready > deadline:
            return None
        if self.clock.now() < ready:
            self.clock.advance((ready - self.clock.now()).total_seconds())
        return rec.result

    def cancel(self, target_id: str, correlation_id: str) -> CancelAck:
        self._raise_injected()
        now = self.clock.now()
        self.log_lines.append(f"cancel {target_id} corr={correlation_id}")
        return CancelAck(
            target_id,
            now + dt.timedelta(seconds=self.cancel_ack_s),
            now + dt.timedelta(seconds=self.cancel_ack_s + self.cancel_cleanup_s),
        )

    def latest_heartbeat(self, agent_id: str) -> Heartbeat | None:
        self._raise_injected()
        hb = Heartbeat(
            self.clock.now(),
            "ok",
            capacity=3,
            usage_since_last=Usage(
                model="sim-1", input_tokens=1, output_tokens=1, tool_calls=0, wall_time_ms=1
            )
            if self.include_usage
            else Usage(usage_unavailable="ADAPTER_NO_METERING"),
            capabilities=("cap-echo",),
        )
        self.heartbeat_log.append(hb)
        return hb

    # ---- fault injection / connectivity -------------------------------------------------
    def _raise_injected(self) -> None:
        kind = self.failure
        if kind is None:
            return
        self.failure = None  # one-shot
        if kind == "timeout":
            raise TimeoutError("simulated timeout")
        if kind == "unreachable":
            raise ConnectionError("simulated connection refused")
        if kind == "auth":
            raise PermissionError("simulated 401")
        if kind == "bad_response":
            raise ValueError("simulated malformed JSON")
        if kind == "rate_limited":
            raise RuntimeError("RATE_LIMIT exceeded (429)")
        raise RuntimeError(kind)

    def reconnect(self) -> list[str]:
        self.connected = True
        redelivered = list(self.pending_redelivery)
        self.pending_redelivery.clear()
        for wid in redelivered:
            self.items[wid].deliveries += 1
            self._process(wid)
        return redelivered


@dataclass
class McpSimulationHarness:
    """Built-in harness for the ``mcp`` adapter type (SimulatedAgent behind the inbox port)."""

    endpoint: Mapping[str, Any]
    agent_factory: Callable[[VirtualClock], SimulatedAgent] = lambda clock: SimulatedAgent(clock)
    adapter_type: str = "mcp"
    clock: VirtualClock = field(default_factory=VirtualClock)

    def __post_init__(self) -> None:
        self.agent = self.agent_factory(self.clock)
        self._adapter = adapter_for(self.adapter_type, {**self.endpoint, "_port": self.agent})

    def adapter(self) -> Adapter:
        return self._adapter

    def side_effects(self, work_item_id: str) -> int:
        return self.agent.side_effect_log.get(work_item_id, 0)

    def results(self, work_item_id: str) -> int:
        return self.agent.result_count.get(work_item_id, 0)

    def acked_at(self, work_item_id: str) -> dt.datetime | None:
        rec = self.agent.items.get(work_item_id)
        return None if rec is None else rec.acked_at

    def accepted_at(self, work_item_id: str) -> dt.datetime | None:
        rec = self.agent.items.get(work_item_id)
        return None if rec is None else rec.accepted_at

    def logs(self) -> list[str]:
        lines = list(self.agent.log_lines)
        for rec in self.agent.items.values():
            if rec.result is not None:
                lines.append(json.dumps(rec.result, sort_keys=True))
        return lines

    def inject_failure(self, kind: str | None) -> None:
        self.agent.failure = kind

    def disconnect(self) -> None:
        self.agent.connected = False

    def reconnect(self) -> list[str]:
        return self.agent.reconnect()

    def heartbeats(self, count: int) -> list[Heartbeat]:
        out: list[Heartbeat] = []
        for _ in range(count):
            out.append(self._adapter.heartbeat())
            self.clock.advance(self.agent.heartbeat_interval_s)
        return out

    def cancel_timing(self, ack: CancelAck) -> tuple[float, float]:
        issued = self.clock.now()
        return (
            (ack.acknowledged_at - issued).total_seconds(),
            (ack.cleanup_deadline - ack.acknowledged_at).total_seconds(),
        )


_HARNESSES: dict[str, Callable[[Mapping[str, Any]], Harness]] = {
    "mcp": lambda endpoint: McpSimulationHarness(endpoint),
}


def register_harness(adapter_type: str, factory: Callable[[Mapping[str, Any]], Harness]) -> None:
    _HARNESSES[adapter_type] = factory


def harness_for(adapter_type: str, endpoint: Mapping[str, Any]) -> Harness:
    load_plugins()  # plugin registrars may register a harness alongside their adapter type
    try:
        factory = _HARNESSES[adapter_type]
    except KeyError as exc:
        raise AdapterError("CAPABILITY_UNSUPPORTED", f"no harness for {adapter_type}") from exc
    return factory(endpoint)
