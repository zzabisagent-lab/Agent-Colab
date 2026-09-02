"""Setup state machine (spec §12, development plan §8.1) — P0-05.

``UNINITIALIZED → PREFLIGHT_PASSED → BOOTSTRAPPING → CONFIGURED → LOCKED``; failure during
bootstrap moves to ``BOOTSTRAP_FAILED`` with a retry token stripped of sensitive data; a legitimate
``RECONFIGURING`` session opens from ``LOCKED`` only with maintenance mode, recovery code, and MFA
re-authentication proofs and returns to ``LOCKED`` automatically after 30 minutes. Every state
has a stage ordinal so that "never regress" is a checkable invariant.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

from server.domain.clock import Clock, isoformat_utc
from server.domain.defaults import SETUP_RECONFIGURE_SESSION_MIN
from server.setup.errors import SetupError


class SetupState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    CONFIGURED = "CONFIGURED"
    LOCKED = "LOCKED"
    RECONFIGURING = "RECONFIGURING"


STAGE_ORDINAL: dict[SetupState, int] = {
    SetupState.UNINITIALIZED: 0,
    SetupState.PREFLIGHT_PASSED: 1,
    SetupState.BOOTSTRAPPING: 2,
    SetupState.BOOTSTRAP_FAILED: 2,
    SetupState.CONFIGURED: 3,
    SetupState.LOCKED: 4,
    SetupState.RECONFIGURING: 4,
}

TRANSITIONS: dict[SetupState, frozenset[SetupState]] = {
    SetupState.UNINITIALIZED: frozenset({SetupState.PREFLIGHT_PASSED}),
    SetupState.PREFLIGHT_PASSED: frozenset({SetupState.BOOTSTRAPPING}),
    SetupState.BOOTSTRAPPING: frozenset({SetupState.CONFIGURED, SetupState.BOOTSTRAP_FAILED}),
    SetupState.BOOTSTRAP_FAILED: frozenset({SetupState.PREFLIGHT_PASSED, SetupState.BOOTSTRAPPING}),
    SetupState.CONFIGURED: frozenset({SetupState.LOCKED}),
    SetupState.LOCKED: frozenset({SetupState.RECONFIGURING}),
    SetupState.RECONFIGURING: frozenset({SetupState.LOCKED}),
}

BOOTSTRAP_OPEN_STATES = frozenset(
    {
        SetupState.UNINITIALIZED,
        SetupState.PREFLIGHT_PASSED,
        SetupState.BOOTSTRAPPING,
        SetupState.BOOTSTRAP_FAILED,
    }
)


def is_regression(current: SetupState, target: SetupState) -> bool:
    return STAGE_ORDINAL[target] < STAGE_ORDINAL[current]


@dataclass(frozen=True)
class BootstrapFailure:
    """Recorded on ``BOOTSTRAP_FAILED``; contains no secret material by construction."""

    failed_step: str
    error_code: str
    retry_token_fingerprint: str
    failed_at: str


@dataclass(frozen=True)
class ReconfigurationProof:
    """Evidence that the System Owner satisfied spec §12 before reconfiguration."""

    maintenance_mode_enabled: bool
    recovery_code_verified: bool
    mfa_reauth_verified: bool
    owner_account_id: str

    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.maintenance_mode_enabled:
            out.append("maintenance_mode")
        if not self.recovery_code_verified:
            out.append("recovery_code")
        if not self.mfa_reauth_verified:
            out.append("mfa_reauth")
        if not self.owner_account_id:
            out.append("owner_account")
        return out


@dataclass(frozen=True)
class ReconfigurationSession:
    session_id: str
    owner_account_id: str
    opened_at: dt.datetime
    expires_at: dt.datetime


@dataclass
class SetupStateMachine:
    """In-memory authority for the Setup state; persistence is the bootstrap store / setup_state."""

    clock: Clock
    state: SetupState = SetupState.UNINITIALIZED
    session_minutes: int = SETUP_RECONFIGURE_SESSION_MIN
    failure: BootstrapFailure | None = None
    session: ReconfigurationSession | None = None
    history: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def stage_ordinal(self) -> int:
        return STAGE_ORDINAL[self.state]

    def can_transition(self, target: SetupState) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: SetupState, reason: str = "") -> SetupState:
        if target not in TRANSITIONS[self.state]:
            raise SetupError(
                "SETUP_TRANSITION_INVALID", f"{self.state} -> {target} is not a defined transition"
            )
        if is_regression(self.state, target):
            # Only BOOTSTRAP_FAILED -> PREFLIGHT_PASSED lowers the ordinal; it is a retry path and
            # keeps the failure record, never a rollback of committed configuration.
            if self.state is not SetupState.BOOTSTRAP_FAILED:
                raise SetupError("SETUP_TRANSITION_INVALID", "stage regression is forbidden")
        previous = self.state
        self.state = target
        if target is not SetupState.BOOTSTRAP_FAILED and previous is SetupState.BOOTSTRAP_FAILED:
            pass  # failure record is kept until CONFIGURED for audit
        if target is SetupState.CONFIGURED:
            self.failure = None
        self.history.append((previous, target, isoformat_utc(self.clock.now()) + " " + reason))
        return self.state

    def require_bootstrap_open(self) -> None:
        """Bootstrap endpoints are 404/403 once configuration is committed (V-P4-03)."""
        if self.state not in BOOTSTRAP_OPEN_STATES:
            raise SetupError("SETUP_LOCKED", "bootstrap is closed after configuration")

    def fail_bootstrap(
        self, failed_step: str, error_code: str, retry_token_fingerprint: str
    ) -> BootstrapFailure:
        self.transition(SetupState.BOOTSTRAP_FAILED, reason=f"{failed_step}:{error_code}")
        self.failure = BootstrapFailure(
            failed_step=failed_step,
            error_code=error_code,
            retry_token_fingerprint=retry_token_fingerprint,
            failed_at=isoformat_utc(self.clock.now()),
        )
        return self.failure

    def open_reconfiguration(
        self, proof: ReconfigurationProof, session_id: str
    ) -> ReconfigurationSession:
        if self.state is not SetupState.LOCKED:
            raise SetupError(
                "SETUP_TRANSITION_INVALID", f"reconfiguration requires LOCKED, not {self.state}"
            )
        missing = proof.missing()
        if missing:
            raise SetupError("SETUP_REAUTH_REQUIRED", ",".join(missing))
        now = self.clock.now()
        self.session = ReconfigurationSession(
            session_id=session_id,
            owner_account_id=proof.owner_account_id,
            opened_at=now,
            expires_at=now + dt.timedelta(minutes=self.session_minutes),
        )
        self.transition(SetupState.RECONFIGURING, reason=f"session {session_id}")
        return self.session

    def require_reconfiguring(self, session_id: str) -> ReconfigurationSession:
        """Every reconfiguration action calls this; expiry returns the state to LOCKED."""
        self.expire_session_if_due()
        if self.state is not SetupState.RECONFIGURING or self.session is None:
            raise SetupError("SETUP_LOCKED", "no active reconfiguration session")
        if self.session.session_id != session_id:
            raise SetupError("SETUP_LOCKED", "session mismatch")
        return self.session

    def expire_session_if_due(self) -> bool:
        if self.state is SetupState.RECONFIGURING and self.session is not None:
            if self.clock.now() >= self.session.expires_at:
                self.close_reconfiguration(reason="expired")
                return True
        return False

    def close_reconfiguration(self, reason: str = "completed") -> None:
        if self.state is not SetupState.RECONFIGURING:
            raise SetupError("SETUP_TRANSITION_INVALID", "no reconfiguration session to close")
        self.session = None
        self.transition(SetupState.LOCKED, reason=reason)
        if reason == "expired":
            raise SetupError("SETUP_SESSION_EXPIRED", "reconfiguration session expired")
