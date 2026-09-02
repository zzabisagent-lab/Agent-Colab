"""Re-authentication seam for critical actions (development plan §11.2, §7E, §8.1; P4-08/P4-09).

Setup reconfiguration (P4-03), HIGH+ approval decisions (P4-14), break-glass (P4-10) and hard
delete (P4-11) all call :func:`require_recent_mfa`. The MFA package (P4-09) supplies the real
verifier through :func:`set_verifier`; until then every check fails closed with
``REAUTH_REQUIRED`` so no critical action is possible without it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from server.application.bus import CommandError

DEFAULT_MAX_AGE_S = 300  # a critical action needs an MFA re-authentication within 5 minutes


@dataclass(frozen=True)
class ReauthProof:
    account_uuid: str
    verified_at: dt.datetime
    method: str  # totp | recovery_code | oidc
    session_id: str | None = None


# (account_uuid, session_id | None) -> latest proof or None
Verifier = Callable[[str, str | None], ReauthProof | None]
_verifier: Verifier | None = None


def set_verifier(verifier: Verifier | None) -> None:
    global _verifier
    _verifier = verifier


def require_recent_mfa(
    account_uuid: str,
    *,
    now: dt.datetime,
    session_id: str | None = None,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    action: str = "critical_action",
) -> ReauthProof:
    """Return the proof or raise ``REAUTH_REQUIRED`` (401) — fail closed without a verifier."""
    proof = _verifier(account_uuid, session_id) if _verifier is not None else None
    if proof is None or (now - proof.verified_at).total_seconds() > max_age_s:
        raise CommandError(
            "REAUTH_REQUIRED",
            f"{action} requires MFA re-authentication within {max_age_s} s",
            status=401,
            extra={"action": action, "max_age_s": max_age_s},
        )
    return proof
