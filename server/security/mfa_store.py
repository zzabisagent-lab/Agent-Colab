"""Storage interface for MFA enrollments and recovery codes (P4-03 writes, P4-09 reads).

The TOTP secret is envelope-encrypted with a per-account DEK (``sensitive_keys``); only the
recovery code's SHA-256 is stored. Nothing here logs or returns secret values.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.secrets.envelope import EnvelopeCrypto


def save_totp_enrollment(
    session: Session,
    crypto: EnvelopeCrypto,
    workspace_id: uuid.UUID,
    account_uuid: uuid.UUID,
    secret_b32: str,
    now: dt.datetime,
    *,
    confirmed: bool = False,
) -> str:
    """Encrypt and store the TOTP secret; returns the DEK key_ref."""
    ciphertext, key_ref = crypto.encrypt(
        session, str(workspace_id), "mfa", str(account_uuid), {"secret_b32": secret_b32}
    )
    session.execute(
        text(
            "INSERT INTO mfa_enrollments (account_id, method, secret_ciphertext, key_ref, "
            "enrolled_at, confirmed_at) VALUES (:a, 'totp', :c, :k, :n, :cf) "
            "ON CONFLICT (account_id, method) DO UPDATE SET "
            "secret_ciphertext = EXCLUDED.secret_ciphertext, "
            "key_ref = EXCLUDED.key_ref, enrolled_at = "
            "EXCLUDED.enrolled_at, confirmed_at = EXCLUDED.confirmed_at"
        ),
        {
            "a": account_uuid,
            "c": ciphertext,
            "k": key_ref,
            "n": now,
            "cf": now if confirmed else None,
        },
    )
    return key_ref


def load_totp_secret(
    session: Session, crypto: EnvelopeCrypto, account_uuid: uuid.UUID
) -> str | None:
    row = session.execute(
        text(
            "SELECT secret_ciphertext, key_ref FROM mfa_enrollments "
            "WHERE account_id = :a AND method = 'totp'"
        ),
        {"a": account_uuid},
    ).first()
    if row is None:
        return None
    return str(crypto.decrypt(session, str(row[1]), bytes(row[0]))["secret_b32"])


def confirm_totp(session: Session, account_uuid: uuid.UUID, now: dt.datetime) -> None:
    session.execute(
        text(
            "UPDATE mfa_enrollments SET confirmed_at = "
            "COALESCE(confirmed_at, :n) WHERE account_id = :a AND method = 'totp'"
        ),
        {"a": account_uuid, "n": now},
    )


def save_recovery_code_hash(
    session: Session, account_uuid: uuid.UUID, code_hash: str, now: dt.datetime
) -> None:
    session.execute(
        text("INSERT INTO recovery_codes (account_id, code_hash, created_at) VALUES (:a, :h, :n)"),
        {"a": account_uuid, "h": code_hash, "n": now},
    )


def consume_recovery_code(
    session: Session, account_uuid: uuid.UUID, code_hash: str, now: dt.datetime
) -> bool:
    """Mark an unused recovery code as used; False when unknown or already used."""
    result = session.execute(
        text(
            "UPDATE recovery_codes SET used_at = :n WHERE account_id = "
            ":a AND code_hash = :h AND used_at IS NULL"
        ),
        {"a": account_uuid, "h": code_hash, "n": now},
    )
    return int(result.rowcount or 0) == 1  # type: ignore[attr-defined]
