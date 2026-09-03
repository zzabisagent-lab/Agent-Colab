"""The production ``Verifier`` for :mod:`server.security.reauth` (installed by ``create_app``)."""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from server.db.engine import session_scope
from server.domain.clock import SystemClock
from server.security import mfa
from server.security.reauth import ReauthProof, Verifier


def build_verifier(session_factory: Any, clock_source: Any = None) -> Verifier:
    """``clock_source`` is the app (its ``state.clock``/``state.runtime.clock`` is read per call)
    or a Clock; None means the system clock."""

    def _now() -> dt.datetime:
        if clock_source is None:
            return SystemClock().now()
        if hasattr(clock_source, "now") and not hasattr(clock_source, "state"):
            return cast(dt.datetime, clock_source.now())  # a Clock
        state = getattr(clock_source, "state", None)
        clock = getattr(state, "clock", None) or getattr(
            getattr(state, "runtime", None), "clock", None
        )
        return (clock or SystemClock()).now()

    def verifier(account_uuid: str, session_id: str | None) -> ReauthProof | None:
        now = _now()
        with session_scope(session_factory) as session:
            proof = mfa.latest_proof(session, account_uuid, session_id, now)
            if proof is None and session_id is not None:
                return None
            if proof is None:
                return None
            verified_at, method = proof
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=dt.UTC)
            return ReauthProof(account_uuid, verified_at, method, session_id)

    return verifier
