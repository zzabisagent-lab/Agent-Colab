"""MFA (P4-09): TOTP enrollment, verification proofs, recovery codes, policy.

- TOTP secrets are envelope-encrypted with a per-account DEK (``server/secrets/envelope.py``) and
  are never returned after the one-time enrollment response.
- Recovery codes are stored as SHA-256 hashes only; each code is single-use.
- A successful verification writes a ``session_mfa`` proof (session-bound for cookie sessions,
  account-bound for Bearer API clients) that :mod:`server.security.reauth` consumes.
- MFA is mandatory for System Owner / Administrator Accounts (roles ``role-system-owner`` /
  ``role-administrator`` or any role granting an ``admin.*`` permission); Members follow
  ``security.mfa_members``; Agent and service Accounts are excluded.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application.bus import CommandError
from server.policy.repository import PrincipalInfo
from server.security import mfa_store, totp
from server.security import policy as secpolicy

RECOVERY_CODE_COUNT = 8
PROOF_TTL_S = 8 * 3600  # a proof never outlives the session it belongs to


class MfaError(CommandError):
    pass


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().replace("-", "").upper().encode()).hexdigest()


@dataclass(frozen=True)
class EnrollmentStatus:
    enrolled: bool
    confirmed: bool
    required: bool


# ------------------------------------------------------------------ policy
def mfa_required(session: Session, principal: PrincipalInfo, now: dt.datetime) -> bool:
    """System Owner / Administrator: mandatory. Members: per policy. Agents/services: never."""
    if principal.account_type != "human":
        return False
    from server.policy.repository import PostgresPolicyRepository

    roles = PostgresPolicyRepository().effective_roles(session, principal, now)
    for role in roles:
        if role.role_id in secpolicy.MFA_REQUIRED_ROLES:
            return True
        if any(p == "admin.*" or p.startswith("admin.") for p in role.permissions):
            return True
    return secpolicy.bool_value("security.mfa_members")


def principal_info(session: Session, account_uuid: str) -> PrincipalInfo:
    row = session.execute(
        text("SELECT account_id, workspace_id, account_type, status FROM accounts WHERE id = :i"),
        {"i": uuid.UUID(account_uuid)},
    ).first()
    if row is None:
        raise MfaError("ACCOUNT_NOT_FOUND", account_uuid, status=404)
    return PrincipalInfo(
        account_id=str(row[0]),
        account_uuid=uuid.UUID(account_uuid),
        workspace_uuid=row[1],
        account_type=str(row[2]),
        status=str(row[3]),
        agent_id=None,
    )


# ------------------------------------------------------------------ enrollment
def enrollment_status(session: Session, account_uuid: str, required: bool) -> EnrollmentStatus:
    row = session.execute(
        text("SELECT confirmed_at FROM mfa_enrollments WHERE account_id = :a AND method = 'totp'"),
        {"a": uuid.UUID(account_uuid)},
    ).first()
    return EnrollmentStatus(row is not None, row is not None and row[0] is not None, required)


def enroll_totp(
    session: Session,
    crypto: Any,
    *,
    workspace_id: str,
    account_uuid: str,
    account_label: str,
    issuer: str,
    now: dt.datetime,
) -> str:
    """Create (or replace an unconfirmed) TOTP enrollment; returns the otpauth URI exactly once."""
    if crypto is None:
        raise MfaError("MFA_CRYPTO_UNAVAILABLE", "master key not configured", status=503)
    existing = session.execute(
        text("SELECT confirmed_at FROM mfa_enrollments WHERE account_id = :a AND method = 'totp'"),
        {"a": uuid.UUID(account_uuid)},
    ).first()
    if existing is not None and existing[0] is not None:
        raise MfaError("MFA_ALREADY_ENROLLED", "confirmed enrollment exists", status=409)
    secret = totp.new_secret()
    mfa_store.save_totp_enrollment(
        session, crypto, uuid.UUID(workspace_id), uuid.UUID(account_uuid), secret, now
    )
    return totp.otpauth_uri(secret, account_label, issuer)


def _secret(session: Session, crypto: Any, account_uuid: str) -> tuple[str, bool]:
    row = session.execute(
        text("SELECT confirmed_at FROM mfa_enrollments WHERE account_id = :a AND method = 'totp'"),
        {"a": uuid.UUID(account_uuid)},
    ).first()
    if row is None:
        raise MfaError("MFA_NOT_ENROLLED", "no TOTP enrollment", status=403)
    if crypto is None:
        raise MfaError("MFA_CRYPTO_UNAVAILABLE", "master key not configured", status=503)
    secret = mfa_store.load_totp_secret(session, crypto, uuid.UUID(account_uuid))
    if secret is None:
        raise MfaError("MFA_NOT_ENROLLED", "no TOTP enrollment", status=403)
    return secret, row[0] is not None


def confirm_totp(
    session: Session, crypto: Any, account_uuid: str, code: str, now: dt.datetime
) -> None:
    secret, confirmed = _secret(session, crypto, account_uuid)
    if confirmed:
        raise MfaError("MFA_ALREADY_ENROLLED", "already confirmed", status=409)
    if not totp.verify(secret, code, now):
        raise MfaError("MFA_CODE_INVALID", "wrong code", status=401)
    mfa_store.confirm_totp(session, uuid.UUID(account_uuid), now)


def verify_totp(
    session: Session, crypto: Any, account_uuid: str, code: str, now: dt.datetime
) -> None:
    secret, confirmed = _secret(session, crypto, account_uuid)
    if not confirmed:
        raise MfaError("MFA_NOT_ENROLLED", "enrollment not confirmed", status=403)
    if not totp.verify(secret, code, now):
        raise MfaError("MFA_CODE_INVALID", "wrong code", status=401)


# ------------------------------------------------------------------ recovery codes
def issue_recovery_codes(session: Session, account_uuid: str, now: dt.datetime) -> list[str]:
    """Replace the account's recovery codes; the plaintext list is returned exactly once."""
    session.execute(
        text("DELETE FROM recovery_codes WHERE account_id = :a"), {"a": uuid.UUID(account_uuid)}
    )
    codes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(5).upper()
        code = f"{raw[:5]}-{raw[5:]}"
        codes.append(code)
        mfa_store.save_recovery_code_hash(session, uuid.UUID(account_uuid), _hash(code), now)
    return codes


def consume_recovery_code(session: Session, account_uuid: str, code: str, now: dt.datetime) -> None:
    """Single-use: a used or unknown code is rejected without revealing which."""
    if not mfa_store.consume_recovery_code(session, uuid.UUID(account_uuid), _hash(code), now):
        raise MfaError("MFA_RECOVERY_CODE_INVALID", "unknown or used recovery code", status=401)


# ------------------------------------------------------------------ proofs
def record_proof(
    session: Session,
    account_uuid: str,
    session_uuid: str | None,
    method: str,
    now: dt.datetime,
    ttl_s: int = PROOF_TTL_S,
) -> None:
    session.execute(
        text(
            "INSERT INTO session_mfa (account_id, session_id, method, verified_at, expires_at) "
            "VALUES (:a, :s, :m, :now, :exp)"
        ),
        {
            "a": uuid.UUID(account_uuid),
            "s": uuid.UUID(session_uuid) if session_uuid else None,
            "m": method,
            "now": now,
            "exp": now + dt.timedelta(seconds=ttl_s),
        },
    )
    if session_uuid:
        session.execute(
            text(
                "UPDATE account_sessions SET mfa_verified_at = :now, reauth_at = :now WHERE id = :s"
            ),
            {"now": now, "s": uuid.UUID(session_uuid)},
        )


def latest_proof(
    session: Session, account_uuid: str, session_uuid: str | None, now: dt.datetime
) -> tuple[dt.datetime, str] | None:
    """Latest unexpired proof for the session (or the account when no session is bound)."""
    params: dict[str, Any] = {"a": uuid.UUID(account_uuid), "now": now}
    if session_uuid:
        params["s"] = uuid.UUID(session_uuid)
        stmt = text(
            "SELECT verified_at, method FROM session_mfa WHERE account_id = :a "
            "AND session_id = :s AND expires_at > :now ORDER BY verified_at DESC LIMIT 1"
        )
    else:
        stmt = text(
            "SELECT verified_at, method FROM session_mfa WHERE account_id = :a "
            "AND session_id IS NULL AND expires_at > :now ORDER BY verified_at DESC LIMIT 1"
        )
    row = session.execute(stmt, params).first()
    return None if row is None else (row[0], str(row[1]))


def session_uuid_for(session: Session, cookie_token: str | None) -> str | None:
    if not cookie_token:
        return None
    from server.identity.principals import token_hash

    row = session.execute(
        text("SELECT id FROM account_sessions WHERE session_token_hash = :h"),
        {"h": token_hash(cookie_token)},
    ).first()
    return None if row is None else str(row[0])
