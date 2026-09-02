"""CS-01..CS-12 checks (validation plan §11.1) executed against an Adapter through a Harness."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from server.agents.adapters.contract import (
    STABLE_ERROR_CODES,
    Adapter,
    AdapterError,
    WorkItemView,
)
from server.agents.conformance.harness import EXPECTED_ERROR_CODES, FAILURE_KINDS, Harness
from server.agents.conformance.report import CHECK_TITLES, CheckResult, ConformanceReport, now_iso
from server.usage.conformance import normalize_usage
from server.work.schemas import AdapterSchemaError, validate

ACK_LIMIT_S = 60
ACCEPT_LIMIT_S = 120
CANCEL_ACK_LIMIT_S = 10
CANCEL_CLEANUP_LIMIT_S = 60
HEARTBEAT_INTERVAL_S = 30


def _item(
    harness: Harness,
    adapter: Adapter,
    kind: str,
    *,
    tool: str = "cap_echo",
    secrets: tuple[str, ...] = (),
    n: int = 0,
) -> WorkItemView:
    wid = f"wi-{uuid.uuid4().hex[:24]}"  # schema-valid id (colab.work-item.v1)
    now = harness.clock.now()
    return WorkItemView(
        work_item_id=wid,
        kind=kind,
        agent_id=adapter.probe().agent_id,
        task_id=f"task-cs-{n}",
        correlation_id=f"corr-cs-{uuid.uuid4().hex[:8]}",
        deadline=now + dt.timedelta(hours=1),
        payload_ref=f"colab://work/{wid}/payload",
        secret_handles=secrets,
        expected_result_schema="colab.work-result.v1",
        idempotency_key=wid,
        payload={"tool": tool, "input": {"n": n}},
    )


def _check(check_id: str, fn: Callable[[], dict[str, Any]]) -> CheckResult:
    try:
        evidence = fn()
    except AssertionError as exc:
        return CheckResult(check_id, CHECK_TITLES[check_id], "FAIL", str(exc))
    except AdapterError as exc:
        return CheckResult(check_id, CHECK_TITLES[check_id], "FAIL", f"{exc.code}: {exc.detail}")
    except Exception as exc:  # a crashing adapter fails the check, never the suite
        return CheckResult(check_id, CHECK_TITLES[check_id], "FAIL", f"{type(exc).__name__}: {exc}")
    return CheckResult(check_id, CHECK_TITLES[check_id], "PASS", evidence=evidence)


def run_suite(harness: Harness) -> ConformanceReport:
    adapter = harness.adapter()
    probe = adapter.probe()
    checks: list[CheckResult] = []

    def cs01() -> dict[str, Any]:
        probes = [adapter.probe() for _ in range(3)]
        hashes = {p.identity_hash for p in probes}
        assert len(hashes) == 1, f"identity changed across probes: {hashes}"
        assert len({p.capabilities for p in probes}) == 1, "capabilities changed across probes"
        assert len({p.delivery_modes for p in probes}) == 1, "delivery_modes changed across probes"
        assert probe.secret_handles in ("supported", "unsupported")
        return {
            "identity_hash": probe.identity_hash,
            "delivery_modes": [m.value for m in probe.delivery_modes],
        }

    def cs02() -> dict[str, Any]:
        item = _item(harness, adapter, "invoke", n=2)
        r1 = adapter.deliver(item)
        r2 = adapter.deliver(item)
        assert r1.work_item_id == r2.work_item_id and r1.rejection_code == r2.rejection_code, (
            "receipts differ"
        )
        assert harness.side_effects(item.work_item_id) == 1, (
            f"side effects: {harness.side_effects(item.work_item_id)}"
        )
        return {"work_item_id": item.work_item_id, "side_effects": 1}

    def cs03() -> dict[str, Any]:
        item = _item(harness, adapter, "task_assignment", n=3)
        delivered = harness.clock.now()
        adapter.deliver(item)
        acked, accepted = (
            harness.acked_at(item.work_item_id),
            harness.accepted_at(item.work_item_id),
        )
        assert acked is not None, "no ack"
        ack_s = (acked - delivered).total_seconds()
        assert ack_s <= ACK_LIMIT_S, f"ack after {ack_s} s"
        assert accepted is not None, "no accept"
        accept_s = (accepted - delivered).total_seconds()
        assert accept_s <= ACCEPT_LIMIT_S, f"accept after {accept_s} s"
        return {"ack_s": ack_s, "accept_s": accept_s}

    def cs04() -> dict[str, Any]:
        res = adapter.invoke(
            "cap_echo",
            {"input": {"n": 4}},
            harness.clock.now() + dt.timedelta(minutes=5),
            (),
            correlation_id="corr-cs04",
        )
        doc = {
            "schema_id": "colab.work-result.v1",
            "work_item_id": "wi-" + uuid.uuid4().hex[:24],
            "correlation_id": res.correlation_id or "corr-cs04",
            "status": "SUCCEEDED",
            "result": dict(res.result),
            "events": list(res.events),
            "artifacts": list(res.artifacts),
        }
        usage: dict[str, Any] = {}
        if res.usage.usage_unavailable:
            usage["usage_unavailable"] = {"reason": res.usage.usage_unavailable}
        else:
            usage["usage"] = {
                "model": res.usage.model or "unknown",
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "tool_calls": res.usage.tool_calls,
                "wall_time_ms": res.usage.wall_time_ms,
            }
        doc.update(usage)
        try:
            validate("work_result", doc)
        except AdapterSchemaError as exc:
            raise AssertionError(f"result violates schema: {exc.detail}") from exc
        norm = normalize_usage(doc)
        assert norm.conformant, f"usage missing and no reason: {norm.problems}"
        return {"usage": usage}

    def cs05() -> dict[str, Any]:
        item = _item(harness, adapter, "invoke", n=5)
        adapter.deliver(item)
        ack = adapter.cancel(item.work_item_id)
        ack_s, cleanup_s = harness.cancel_timing(ack)
        assert ack_s <= CANCEL_ACK_LIMIT_S, f"cancel ack after {ack_s} s"
        assert cleanup_s <= CANCEL_CLEANUP_LIMIT_S, f"cleanup {cleanup_s} s after ack"
        return {"ack_s": ack_s, "cleanup_s": cleanup_s}

    def cs06() -> dict[str, Any]:
        beats = harness.heartbeats(4)
        gaps = [(b.reported_at - a.reported_at).total_seconds() for a, b in pairwise(beats)]
        assert all(abs(g - HEARTBEAT_INTERVAL_S) <= 5 for g in gaps), f"heartbeat gaps {gaps}"
        assert all(isinstance(b.capacity, int) and b.capacity >= 0 for b in beats), (
            "capacity missing"
        )
        assert all(b.health in ("ok", "degraded", "draining") for b in beats), "health invalid"
        assert all(
            b.usage_since_last.usage_unavailable or b.usage_since_last.model is not None
            for b in beats
        ), "heartbeat without usage or reason"
        return {"gaps_s": gaps, "capacity": beats[-1].capacity}

    def cs07() -> dict[str, Any]:
        handle = f"sh-{uuid.uuid4().hex}"  # a §9 lease handle id (never a secret value)
        item = _item(harness, adapter, "invoke", secrets=(handle,), n=7)
        receipt = adapter.deliver(item)
        if probe.secret_handles == "unsupported":  # noqa: S105 - advertisement value  # nosec B105 - advertisement value
            assert receipt.rejection_code == "CAPABILITY_UNSUPPORTED", (
                "unsupported adapter accepted a secret item"
            )
        else:
            assert receipt.rejection_code is None, receipt.rejection_code
        leaks = [line for line in harness.logs() if handle in line]
        assert not leaks, f"secret handle value appeared {len(leaks)} time(s)"
        return {"secret_handles": probe.secret_handles, "leaks": 0}

    def cs08() -> dict[str, Any]:
        total, echoed = 10, 0
        for n in range(total):
            corr = f"corr-cs08-{n}"
            res = adapter.invoke(
                "cap_echo",
                {"input": {"n": n}, "task_id": f"task-cs08-{n}"},
                harness.clock.now() + dt.timedelta(minutes=5),
                (),
                correlation_id=corr,
            )
            echoed += int(res.correlation_id == corr and (res.task_id in (None, f"task-cs08-{n}")))
        assert echoed == total, f"correlation echoed {echoed}/{total}"
        return {"echoed": echoed, "total": total}

    def cs09() -> dict[str, Any]:
        item = _item(harness, adapter, "invoke", n=9)
        adapter.deliver(item)
        harness.inject_failure(None)
        adapter.deliver(item)  # transport retry
        adapter.deliver(item)
        assert harness.side_effects(item.work_item_id) == 1, "duplicate side effect on redelivery"
        assert harness.results(item.work_item_id) <= 1, "duplicate result on redelivery"
        return {"deliveries": 3, "side_effects": 1}

    def cs10() -> dict[str, Any]:
        try:
            adapter.invoke(
                "tool_not_advertised",
                {},
                harness.clock.now() + dt.timedelta(minutes=1),
                (),
                correlation_id="corr-cs10",
            )
        except AdapterError as exc:
            assert exc.code == "CAPABILITY_UNSUPPORTED", exc.code
            return {"code": exc.code}
        raise AssertionError("unadvertised tool was executed")

    def cs11() -> dict[str, Any]:
        codes: dict[str, str] = {}
        for kind in FAILURE_KINDS:
            harness.inject_failure(kind)
            try:
                adapter.heartbeat()
            except BaseException as exc:
                err = adapter.normalize_error(exc)
                codes[kind] = err.code
            else:
                raise AssertionError(f"injected {kind} failure was swallowed")
        assert all(c in STABLE_ERROR_CODES for c in codes.values()), codes
        assert codes == EXPECTED_ERROR_CODES, f"unexpected mapping {codes}"
        return {"codes": codes}

    def cs12() -> dict[str, Any]:
        harness.disconnect()
        item = _item(harness, adapter, "invoke", n=12)
        adapter.deliver(item)
        redelivered = harness.reconnect()
        assert item.work_item_id in redelivered, "un-acked item not re-received after reconnect"
        assert harness.results(item.work_item_id) == 1, (
            f"results after reconnect: {harness.results(item.work_item_id)}"
        )
        return {"redelivered": redelivered}

    for check_id, fn in (
        ("CS-01", cs01),
        ("CS-02", cs02),
        ("CS-03", cs03),
        ("CS-04", cs04),
        ("CS-05", cs05),
        ("CS-06", cs06),
        ("CS-07", cs07),
        ("CS-08", cs08),
        ("CS-09", cs09),
        ("CS-10", cs10),
        ("CS-11", cs11),
        ("CS-12", cs12),
    ):
        checks.append(_check(check_id, fn))
    return ConformanceReport(
        adapter_type=probe.adapter_type,
        agent_id=probe.agent_id,
        generated_at=now_iso(),
        checks=checks,
        harness=type(harness).__name__,
    )
