"""Reconciliation of the local sealed store with the DB ``setup_state`` record — P0-09.

After DB connection and migration the state moves to the DB; on failure both sides are
reconciled without regressing to a lower stage, and secrets are written to neither. The result
is the higher stage; once the DB holds CONFIGURED or later, the local file keeps only the LOCKED
marker and minimal recovery metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.setup.errors import SetupError
from server.setup.state import STAGE_ORDINAL, SetupState

_DB_AUTHORITATIVE = frozenset({SetupState.CONFIGURED, SetupState.LOCKED, SetupState.RECONFIGURING})


@dataclass(frozen=True)
class ReconcileResult:
    state: SetupState
    stage_ordinal: int
    source: str  # "local" | "db" | "both"
    local_document: dict[str, Any] | None  # what the local file must contain afterwards
    action: str


def _state_of(record: dict[str, Any] | None) -> SetupState | None:
    if record is None:
        return None
    return SetupState(record["state"])


def reconcile(
    local: dict[str, Any] | None,
    db: dict[str, Any] | None,
    lock_marker: dict[str, Any] | None = None,
) -> ReconcileResult:
    """``lock_marker`` is the document to write locally when the DB is authoritative."""
    local_state, db_state = _state_of(local), _state_of(db)
    if local_state is None and db_state is None:
        return ReconcileResult(SetupState.UNINITIALIZED, 0, "both", None, "start_uninitialized")
    if db_state is None:
        assert local is not None and local_state is not None
        return ReconcileResult(
            local_state, STAGE_ORDINAL[local_state], "local", local, "keep_local"
        )
    if (
        local is not None
        and db is not None
        and local.get("recovery_metadata", {}).get("instance_id")
        and db.get("instance_id")
        and local["recovery_metadata"]["instance_id"] != db["instance_id"]
    ):
        raise SetupError("RECONCILE_CONFLICT", "local store and DB describe different instances")
    if db_state in _DB_AUTHORITATIVE:
        if local_state is not None and STAGE_ORDINAL[local_state] > STAGE_ORDINAL[db_state]:
            raise SetupError("RECONCILE_CONFLICT", f"local {local_state} ahead of DB {db_state}")
        if lock_marker is None:
            raise SetupError("RECONCILE_CONFLICT", "lock marker document required")
        return ReconcileResult(
            db_state, STAGE_ORDINAL[db_state], "db", lock_marker, "db_authoritative_lock_marker"
        )
    # DB holds a pre-configuration record (migration ran, bootstrap not committed)
    if local_state is None:
        return ReconcileResult(db_state, STAGE_ORDINAL[db_state], "db", None, "adopt_db")
    assert local is not None
    if STAGE_ORDINAL[local_state] >= STAGE_ORDINAL[db_state]:
        if local_state is SetupState.BOOTSTRAP_FAILED or db_state is SetupState.BOOTSTRAP_FAILED:
            failed = {**local, "state": SetupState.BOOTSTRAP_FAILED.value, "stage_ordinal": 2}
            return ReconcileResult(SetupState.BOOTSTRAP_FAILED, 2, "both", failed, "keep_failure")
        return ReconcileResult(
            local_state, STAGE_ORDINAL[local_state], "local", local, "keep_local"
        )
    advanced = {**local, "state": db_state.value, "stage_ordinal": STAGE_ORDINAL[db_state]}
    return ReconcileResult(db_state, STAGE_ORDINAL[db_state], "db", advanced, "advance_to_db")
