"""P3-01 unit rules: identity/endpoint validation, lifecycle fold determinism, offline timing."""

from __future__ import annotations

import datetime as dt

import pytest

from server.agents import registry as reg

T0 = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


def _ev(seq: int, etype: str, payload: dict[str, object], at: dt.datetime) -> dict[str, object]:
    return {
        "event_id": f"evt-{seq:04d}",
        "type": etype,
        "aggregate_seq": seq,
        "occurred_at": at.isoformat(),
        "payload": payload,
    }


def test_agent_id_and_adapter_type_validation() -> None:
    reg.validate_agent_id("agent-research-1")
    for bad in ("research", "agent-", "agent-Upper", "acct-x"):
        with pytest.raises(reg.RegistryError) as exc:
            reg.validate_agent_id(bad)
        assert exc.value.code == "AGENT_ID_INVALID"
    with pytest.raises(reg.RegistryError) as exc2:
        reg.validate_adapter_type("local_process")  # out of scope for v8 (§4.2)
    assert exc2.value.code == "AGENT_ADAPTER_TYPE_INVALID"


def test_endpoint_rejects_secret_values_but_accepts_references() -> None:
    reg.reject_secret_values({"url": "https://agent.example/hook", "signing_key_ref": "sec-1"})
    reg.reject_secret_values({"auth": {"token_ref": "sec-2", "scheme": "bearer"}})
    for endpoint in (
        {"token": "abc123"},
        {"auth": {"api_key": "k"}},
        {"url": "sk-live-not-a-real-secret"},
    ):
        with pytest.raises(reg.RegistryError) as exc:
            reg.reject_secret_values(endpoint)
        assert exc.value.code == "AGENT_ENDPOINT_SECRET_VALUE"


def test_limits_and_delivery_modes_validation() -> None:
    assert reg.validate_limits({"concurrent_tasks": 2, "daily_cost_units": 0}) == {
        "concurrent_tasks": 2,
        "daily_cost_units": 0,
    }
    with pytest.raises(reg.RegistryError) as exc:
        reg.validate_limits({"max_tokens": 1})
    assert exc.value.code == "AGENT_LIMIT_KEY_INVALID"
    with pytest.raises(reg.RegistryError) as exc2:
        reg.validate_limits({"concurrent_tasks": -1})
    assert exc2.value.code == "AGENT_LIMIT_VALUE_INVALID"
    with pytest.raises(reg.RegistryError):
        reg.validate_limits({"concurrent_tasks": True})
    assert reg.validate_delivery_modes(["pull", "push", "pull"]) == ["pull", "push"]
    with pytest.raises(reg.RegistryError):
        reg.validate_delivery_modes(["stdio"])


def test_fold_lifecycle_and_chain_hash_are_deterministic() -> None:
    events = [
        _ev(1, "AGENT_REGISTERED", {"agent_id": "agent-a"}, T0),
        _ev(2, "AGENT_ACTIVATED", {"agent_id": "agent-a"}, T0 + dt.timedelta(seconds=1)),
        _ev(
            3,
            "AGENT_HEARTBEAT_RECORDED",
            {"agent_id": "agent-a", "capacity": 3, "capabilities": ["cap-x"]},
            T0 + dt.timedelta(seconds=2),
        ),
        _ev(
            4,
            "AGENT_MARKED_OFFLINE",
            {"agent_id": "agent-a", "missed_heartbeats": 3},
            T0 + dt.timedelta(seconds=100),
        ),
        _ev(
            5,
            "AGENT_HEARTBEAT_RECORDED",
            {"agent_id": "agent-a", "capacity": 2},
            T0 + dt.timedelta(seconds=130),
        ),
        _ev(6, "AGENT_SUSPENDED", {"agent_id": "agent-a", "reason_code": "X"}, T0),
    ]
    s3 = reg.fold("agent-a", events[:3])
    assert (s3.status, s3.online, s3.capacity, s3.capabilities) == ("active", True, 3, ("cap-x",))
    s4 = reg.fold("agent-a", events[:4])
    assert (s4.status, s4.online, s4.missed_heartbeats) == ("offline", False, 3)
    s5 = reg.fold("agent-a", events[:5])  # returning heartbeat: active/online again
    assert (s5.status, s5.online, s5.capacity, s5.capabilities) == ("active", True, 2, ("cap-x",))
    s6 = reg.fold("agent-a", events)
    assert (s6.status, s6.online) == ("suspended", False)
    # hash chain: deterministic, order-sensitive, one link per lifecycle Event
    assert reg.fold("agent-a", events).lifecycle_hash == s6.lifecycle_hash
    assert len(s6.history) == 6 and s6.history[-1]["lifecycle_hash"] == s6.lifecycle_hash
    shuffled = [events[0], events[2], events[1], *events[3:]]
    assert reg.fold("agent-a", shuffled).lifecycle_hash != s6.lifecycle_hash
    # unrelated Event types never enter the chain
    with_noise = [*events, _ev(7, "TASK_CREATED", {}, T0)]
    assert reg.fold("agent-a", with_noise).lifecycle_hash == s6.lifecycle_hash


def test_offline_timing_norms() -> None:
    last = T0
    assert not reg.is_offline_due(last, T0 + dt.timedelta(seconds=89))
    assert reg.missed_heartbeats_at(last, T0 + dt.timedelta(seconds=89)) == 2
    assert reg.is_offline_due(last, T0 + dt.timedelta(seconds=90))  # 3rd miss / 90 s
    assert reg.missed_heartbeats_at(last, T0 + dt.timedelta(seconds=90)) == 3
    assert reg.is_offline_due(None, T0)
