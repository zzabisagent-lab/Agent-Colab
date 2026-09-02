"""Setup state, token guard, sealed bootstrap store, handles, transport, order, reconcile.

Self-tests for P0-05/P0-09 (V-P0-12): starts without a DB, no secret values stored, rollback
never regresses the setup stage; plus the Phase 4 behaviours the contract must make possible.
"""

from __future__ import annotations

import datetime as dt
import os
import pickle
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain.clock import UTC, FixedClock
from server.setup.bootstrap_store import BootstrapStore, scan_for_secrets
from server.setup.errors import SetupError
from server.setup.handles import PreDbHandleStore
from server.setup.order import ApplyOrder, ApplyStep
from server.setup.reconcile import reconcile
from server.setup.state import (
    STAGE_ORDINAL,
    ReconfigurationProof,
    SetupState,
    SetupStateMachine,
)
from server.setup.token import SetupTokenGuard, token_fingerprint, token_hash
from server.setup.transport import CHECK_PASSED, TransportRequest, evaluate_transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "setup"
T0 = dt.datetime(2026, 1, 15, 10, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


STORE = _load("store-documents.yaml")
TRANSPORT = _load("transport-truth-table.yaml")
ORDER_RECONCILE = _load("order-and-reconcile.yaml")
FULL_PROOF = ReconfigurationProof(True, True, True, "acct-owner")


# ---------------------------------------------------------------- P0-05: state machine
def test_happy_path_never_regresses_and_locks() -> None:
    sm = SetupStateMachine(FixedClock(T0))
    ordinals = [sm.stage_ordinal]
    for target in (
        SetupState.PREFLIGHT_PASSED,
        SetupState.BOOTSTRAPPING,
        SetupState.CONFIGURED,
        SetupState.LOCKED,
    ):
        sm.transition(target)
        ordinals.append(sm.stage_ordinal)
    assert ordinals == sorted(ordinals) and sm.state is SetupState.LOCKED
    with pytest.raises(SetupError) as exc:
        sm.require_bootstrap_open()
    assert exc.value.code == "SETUP_LOCKED"


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (SetupState.LOCKED, SetupState.UNINITIALIZED),
        (SetupState.CONFIGURED, SetupState.BOOTSTRAPPING),
        (SetupState.LOCKED, SetupState.CONFIGURED),
        (SetupState.UNINITIALIZED, SetupState.CONFIGURED),
        (SetupState.PREFLIGHT_PASSED, SetupState.LOCKED),
        (SetupState.BOOTSTRAPPING, SetupState.RECONFIGURING),
    ],
)
def test_invalid_or_regressing_transitions_are_rejected(
    start: SetupState, target: SetupState
) -> None:
    sm = SetupStateMachine(FixedClock(T0), state=start)
    with pytest.raises(SetupError) as exc:
        sm.transition(target)
    assert exc.value.code == "SETUP_TRANSITION_INVALID"
    assert sm.state is start


def test_bootstrap_failure_keeps_retry_fingerprint_without_secrets() -> None:
    sm = SetupStateMachine(FixedClock(T0), state=SetupState.BOOTSTRAPPING)
    guard = SetupTokenGuard(FixedClock(T0))
    retry = guard.issue()
    failure = sm.fail_bootstrap(
        "KEY_PROVIDER", "KEY_PROVIDER_UNREACHABLE", retry.record.token_fingerprint
    )
    assert sm.state is SetupState.BOOTSTRAP_FAILED and STAGE_ORDINAL[sm.state] == 2
    assert retry.value not in repr(failure) and retry.value not in str(sm.history)
    sm.transition(SetupState.BOOTSTRAPPING)  # retry path
    sm.transition(SetupState.CONFIGURED)
    assert sm.failure is None


def test_reconfiguration_requires_all_proofs_and_expires_after_30_minutes() -> None:
    clock = FixedClock(T0)
    sm = SetupStateMachine(clock, state=SetupState.LOCKED)
    for proof in (
        ReconfigurationProof(False, True, True, "acct-owner"),
        ReconfigurationProof(True, False, True, "acct-owner"),
        ReconfigurationProof(True, True, False, "acct-owner"),
    ):
        with pytest.raises(SetupError) as exc:
            sm.open_reconfiguration(proof, "s1")
        assert exc.value.code == "SETUP_REAUTH_REQUIRED" and sm.state is SetupState.LOCKED
    session = sm.open_reconfiguration(FULL_PROOF, "s1")
    assert sm.state is SetupState.RECONFIGURING
    clock.advance(dt.timedelta(minutes=29))
    assert sm.require_reconfiguring("s1") is session
    with pytest.raises(SetupError) as exc:
        sm.require_reconfiguring("other-session")
    assert exc.value.code == "SETUP_LOCKED"
    clock.advance(dt.timedelta(minutes=1))
    with pytest.raises(SetupError) as exc:
        sm.require_reconfiguring("s1")
    assert exc.value.code == "SETUP_SESSION_EXPIRED"
    assert sm.state.value == "LOCKED"
    with pytest.raises(SetupError) as exc:
        sm.require_reconfiguring("s1")
    assert exc.value.code == "SETUP_LOCKED"
    # LOCKED again: a new legitimate session may be opened
    assert sm.open_reconfiguration(FULL_PROOF, "s2").session_id == "s2"
    sm.close_reconfiguration()
    assert sm.state.value == "LOCKED" and sm.session is None


# ---------------------------------------------------------------- P0-05: token guard
def test_token_is_256_bits_stored_as_hash_single_use_and_ttl() -> None:
    clock = FixedClock(T0)
    guard = SetupTokenGuard(clock)
    issued = guard.issue()
    assert len(bytes.fromhex(issued.value)) == 32
    assert guard.record is not None and guard.record.token_hash == token_hash(issued.value)
    assert issued.value not in repr(issued) and issued.value not in str(
        guard.record.as_store_fields()
    )
    assert guard.verify(issued.value, "10.0.0.1", consume=False).used is False
    assert guard.verify(issued.value, "10.0.0.1").used is True
    with pytest.raises(SetupError) as exc:
        guard.verify(issued.value, "10.0.0.1")
    assert exc.value.code == "SETUP_TOKEN_USED"
    fresh = guard.issue()
    clock.advance(dt.timedelta(minutes=30))
    with pytest.raises(SetupError) as exc:
        guard.verify(fresh.value, "10.0.0.1")
    assert exc.value.code == "SETUP_TOKEN_EXPIRED"
    with pytest.raises(SetupError) as exc:
        guard.verify("00" * 32, "10.0.0.1")
    assert exc.value.code == "SETUP_TOKEN_INVALID"


def test_five_failures_in_window_block_source_for_15_minutes_per_ip_and_fingerprint() -> None:
    clock = FixedClock(T0)
    guard = SetupTokenGuard(clock)
    issued = guard.issue()
    wrong = "ab" * 32
    for _ in range(5):
        with pytest.raises(SetupError) as exc:
            guard.verify(wrong, "203.0.113.9")
        assert exc.value.code == "SETUP_TOKEN_INVALID"
    with pytest.raises(SetupError) as exc:
        guard.verify(wrong, "203.0.113.9")
    assert exc.value.code == "SETUP_TOKEN_BLOCKED"
    # the block is per (ip, presented-token fingerprint): the genuine token is a different key
    assert token_fingerprint(issued.value) != token_fingerprint(wrong)
    assert guard.verify(issued.value, "203.0.113.9", consume=False).used is False
    # another IP with the same wrong token is independent
    with pytest.raises(SetupError) as exc:
        guard.verify(wrong, "203.0.113.10")
    assert exc.value.code == "SETUP_TOKEN_INVALID"
    clock.advance(dt.timedelta(minutes=15))
    with pytest.raises(SetupError) as exc:
        guard.verify(wrong, "203.0.113.9")
    assert exc.value.code == "SETUP_TOKEN_INVALID"  # block lifted, counting restarts
    counters = guard.failure_counters()
    assert all(len(k.split("|")[1]) == 8 for k in counters)
    assert wrong not in str(counters)


def test_failures_outside_15_minute_window_do_not_accumulate() -> None:
    clock = FixedClock(T0)
    guard = SetupTokenGuard(clock)
    guard.issue()
    for _ in range(4):
        with pytest.raises(SetupError):
            guard.verify("cd" * 32, "10.9.9.9")
    clock.advance(dt.timedelta(minutes=15, seconds=1))
    for _ in range(4):
        with pytest.raises(SetupError) as exc:
            guard.verify("cd" * 32, "10.9.9.9")
        assert exc.value.code == "SETUP_TOKEN_INVALID"


# ---------------------------------------------------------------- P0-09: sealed store
@pytest.fixture
def store(tmp_path: Path) -> BootstrapStore:
    return BootstrapStore(tmp_path / "bootstrap" / "state.json", FixedClock(T0))


@pytest.mark.parametrize("case", STORE["valid"], ids=[c["name"] for c in STORE["valid"]])
def test_valid_store_documents(store: BootstrapStore, case: dict[str, Any]) -> None:
    store.validate(case["document"])


@pytest.mark.parametrize("case", STORE["invalid"], ids=[c["name"] for c in STORE["invalid"]])
def test_invalid_store_documents(store: BootstrapStore, case: dict[str, Any]) -> None:
    with pytest.raises(SetupError) as exc:
        store.validate(case["document"])
    assert exc.value.code == case["code"]
    assert "CANARY" not in exc.value.detail and "BEGIN PRIVATE" not in exc.value.detail


def test_store_starts_without_a_db_and_holds_only_the_token_hash(store: BootstrapStore) -> None:
    clock = FixedClock(T0)
    guard = SetupTokenGuard(clock)
    issued = guard.issue()
    doc = {**store.initial_document(), **guard.record.as_store_fields()}  # type: ignore[union-attr]
    store.write(doc)
    raw = store.path.read_text(encoding="utf-8")
    assert issued.value not in raw and guard.record.token_hash in raw  # type: ignore[union-attr]
    assert "database" not in raw.lower()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    assert store.read()["token_hash"] == guard.record.token_hash  # type: ignore[union-attr]


def test_store_rejects_group_or_other_permissions(store: BootstrapStore) -> None:
    store.write(store.initial_document())
    os.chmod(store.path, 0o640)
    with pytest.raises(SetupError) as exc:
        store.read()
    assert exc.value.code == "BOOTSTRAP_STORE_PERMISSIONS_INVALID"
    os.chmod(store.path, 0o600)
    os.chmod(store.path.parent, 0o750)  # noqa: S103 - deliberately permissive to test detection
    with pytest.raises(SetupError) as exc:
        store.read()
    assert exc.value.code == "BOOTSTRAP_STORE_PERMISSIONS_INVALID"


def test_store_write_never_regresses_stage_except_failed_retry(store: BootstrapStore) -> None:
    doc = store.initial_document()
    store.write(doc)
    store.write({**doc, "state": "PREFLIGHT_PASSED", "stage_ordinal": 1})
    store.write({**doc, "state": "BOOTSTRAPPING", "stage_ordinal": 2})
    with pytest.raises(SetupError) as exc:
        store.write({**doc, "state": "PREFLIGHT_PASSED", "stage_ordinal": 1})
    assert exc.value.code == "BOOTSTRAP_STORE_STAGE_REGRESSION"
    assert store.read()["state"] == "BOOTSTRAPPING"
    failed = {
        **doc,
        "state": "BOOTSTRAP_FAILED",
        "stage_ordinal": 2,
        "last_failure": {
            "failed_step": "KEY_PROVIDER",
            "error_code": "KEY_PROVIDER_UNREACHABLE",
            "retry_token_fingerprint": "9f86d081",
            "failed_at": "2026-01-15T10:09:00.000Z",
        },
    }
    store.write(failed)
    with pytest.raises(SetupError):
        store.write({**doc, "state": "PREFLIGHT_PASSED", "stage_ordinal": 1})
    store.write(
        {**doc, "state": "PREFLIGHT_PASSED", "stage_ordinal": 1}, allow_retry_regression=True
    )
    store.write(store.lock_marker_document({"instance_id": "inst-0001"}))
    with pytest.raises(SetupError) as exc:
        store.write({**doc, "state": "CONFIGURED", "stage_ordinal": 3})
    assert exc.value.code == "BOOTSTRAP_STORE_STAGE_REGRESSION"
    marker = store.read()
    assert marker["lock_marker"] is True and marker["token_hash"] is None
    assert (
        marker["config_pointers"] == {}
        and marker["recovery_metadata"]["setup_state_location"] == "db"
    )


def test_store_write_is_atomic_and_leaves_no_temp_files(store: BootstrapStore) -> None:
    store.write(store.initial_document())
    with pytest.raises(SetupError):
        store.write(
            {
                **store.initial_document(),
                "config_pointers": {"instance_name": "Zx9kQ2mP7vL4nR8sT1wY6bC3dF5gH0jK"},
            }
        )
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["state.json"]


def test_secret_scanner_detects_values_but_allows_hashes() -> None:
    assert scan_for_secrets({"token_hash": "a" * 64}) == []
    assert scan_for_secrets({"secret_provider": "vault"}) == []
    assert scan_for_secrets({"x": {"db_password": "p"}}) == ["$.x.db_password: denied key"]
    assert scan_for_secrets(["postgresql://u:CANARY-NOT-A-SECRET-0002@h/db"]) == [
        "$[0]: secret-looking value"
    ]


# ---------------------------------------------------------------- P0-09: handles
def test_handles_live_in_memory_only_with_15_minute_ttl() -> None:
    clock = FixedClock(T0)
    handles = PreDbHandleStore(clock)
    h = handles.put("db_password", "CANARY-NOT-A-SECRET-0003")
    assert "CANARY" not in repr(h) and "CANARY" not in str(h)
    assert "CANARY" not in str(handles.snapshot_for_store())
    with pytest.raises(TypeError):
        pickle.dumps(h)
    with pytest.raises(TypeError):
        pickle.dumps(handles)
    assert handles.resolve(h.handle_id) == b"CANARY-NOT-A-SECRET-0003"
    clock.advance(dt.timedelta(minutes=15))
    with pytest.raises(SetupError) as exc:
        handles.resolve(h.handle_id)
    assert exc.value.code == "SETUP_HANDLE_EXPIRED" and len(handles) == 0
    # a restarted process has an empty store by construction
    assert len(PreDbHandleStore(clock)) == 0
    with pytest.raises(SetupError):
        PreDbHandleStore(clock).resolve(h.handle_id)


# ---------------------------------------------------------------- P0-09: transport
@pytest.mark.parametrize("case", TRANSPORT["cases"], ids=[c["name"] for c in TRANSPORT["cases"]])
def test_transport_truth_table(case: dict[str, Any]) -> None:
    remote = case.get("remote") or (
        TRANSPORT["remote_in_allowlist"]
        if case["allowlisted"]
        else TRANSPORT["remote_outside_allowlist"]
    )
    request = TransportRequest(
        bind_is_loopback=case["bind_loopback"],
        remote_addr=remote,
        tls_terminated_by_proxy=case["tls"],
        client_mtls_verified=case["mtls"],
        allowlist=tuple(TRANSPORT["allowlist"]),
        token_check=CHECK_PASSED if case["token"] == "OK" else case["token"],
    )
    decision = evaluate_transport(request)
    assert (decision.allowed, decision.code) == (case["allowed"], case["code"])
    assert evaluate_transport(request) == decision  # pure


def test_transport_covers_all_sixteen_remote_combinations() -> None:
    combos = {
        (c["tls"], c["mtls"], c["allowlisted"], c["token"] == "OK")
        for c in TRANSPORT["cases"]
        if c["name"].startswith("r-")
    }
    assert len(combos) == 16
    assert sum(1 for c in TRANSPORT["cases"] if c["allowed"] and c["name"].startswith("r-")) == 1


# ---------------------------------------------------------------- P0-09: order
@pytest.mark.parametrize(
    "case", ORDER_RECONCILE["order"], ids=[c["name"] for c in ORDER_RECONCILE["order"]]
)
def test_apply_order(case: dict[str, Any]) -> None:
    order = ApplyOrder()
    error: str | None = None
    for op, step in case["steps"]:
        try:
            getattr(order, op)(ApplyStep[step])
        except SetupError as exc:
            error = exc.code
            break
    assert error == case["error"]
    assert order.owner_created_visible is case["owner_visible"]
    assert order.committed is case["committed"]
    if "completed_after" in case:
        assert {s.name for s in order.completed} == set(case["completed_after"])


# ---------------------------------------------------------------- P0-09: reconcile
@pytest.mark.parametrize(
    "case", ORDER_RECONCILE["reconcile"], ids=[c["name"] for c in ORDER_RECONCILE["reconcile"]]
)
def test_reconcile(case: dict[str, Any], store: BootstrapStore) -> None:
    marker = store.lock_marker_document({"instance_id": "inst-1"})
    if "error" in case:
        with pytest.raises(SetupError) as exc:
            reconcile(case["local"], case["db"], marker)
        assert exc.value.code == case["error"]
        return
    result = reconcile(case["local"], case["db"], marker)
    assert result.state.value == case["expect"]["state"]
    assert result.action == case["expect"]["action"]
    local_state = None if result.local_document is None else result.local_document["state"]
    assert local_state == case["expect"]["local_state"]
    if case["local"] is not None:
        assert result.stage_ordinal >= case["local"]["stage_ordinal"], "regressed"
    if result.local_document is not None:
        assert scan_for_secrets(result.local_document) == []


def test_reconciled_lock_marker_is_writable_to_store(store: BootstrapStore) -> None:
    store.write({**store.initial_document(), "state": "BOOTSTRAPPING", "stage_ordinal": 2})
    result = reconcile(
        store.read(), {"state": "CONFIGURED"}, store.lock_marker_document({"instance_id": "i"})
    )
    assert result.local_document is not None
    store.write(result.local_document)
    assert store.read()["lock_marker"] is True
