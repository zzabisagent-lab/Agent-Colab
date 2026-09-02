"""Service-token resolution (Phase 0 skeleton; hardened by P1-05).

The actor is always derived from the presented credential, never from the body or a header
claiming an identity (development plan §3.1). Tokens are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Principal:
    account_id: str
    account_uuid: str
    account_type: str
    credential_fingerprint: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_service_token(session: Session, token: str) -> Principal | None:
    row = session.execute(
        text(
            "SELECT a.account_id, a.id, a.account_type, c.fingerprint FROM service_credentials c "
            "JOIN accounts a ON a.id = c.account_id "
            "WHERE c.token_hash = :h AND c.status = 'active' AND a.status = 'ACTIVE'"
        ),
        {"h": token_hash(token)},
    ).first()
    if row is None:
        return None
    return Principal(str(row[0]), str(row[1]), str(row[2]), str(row[3]))
