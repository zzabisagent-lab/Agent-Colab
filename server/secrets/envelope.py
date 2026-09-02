"""Envelope encryption for sensitive Event content (spec §11.2, §13; development plan §6.3).

A per-target data-encryption key (DEK, AES-256-GCM) encrypts the sensitive payload; the DEK is
wrapped by the instance master key and stored in ``sensitive_keys``. Hard delete destroys the
wrapped DEK (crypto-shredding) and appends a chained tombstone; Event bytes and hashes are never
touched (V-P1-20). Phase 4 moves the master key behind the Secret provider interface.
"""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.events.canonical import canonical_json
from server.events.chain import TOMBSTONE_CHAIN, chain_hash, hashed_row_fields, last_hash


class CryptoError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def new_master_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@dataclass(frozen=True)
class MasterKey:
    key_id: str
    key: bytes

    @classmethod
    def from_b64(cls, key_id: str, value: str) -> MasterKey:
        raw = base64.b64decode(value)
        if len(raw) != 32:
            raise CryptoError("MASTER_KEY_INVALID", "master key must be 32 bytes")
        return cls(key_id, raw)


class EnvelopeCrypto:
    def __init__(self, master: MasterKey, clock: Clock | None = None) -> None:
        self._master = master
        self._clock = clock or SystemClock()

    # -- DEK management -------------------------------------------------------------------
    def _wrap(self, dek: bytes, key_ref: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._master.key).encrypt(nonce, dek, key_ref.encode())

    def _unwrap(self, wrapped: bytes, key_ref: str) -> bytes:
        return AESGCM(self._master.key).decrypt(wrapped[:12], wrapped[12:], key_ref.encode())

    def key_ref_for(self, workspace_id: str, target_type: str, target_id: str) -> str:
        return f"dek://{workspace_id}/{target_type}/{target_id}"

    def ensure_dek(
        self, session: Session, workspace_id: str, target_type: str, target_id: str
    ) -> bytes:
        key_ref = self.key_ref_for(workspace_id, target_type, target_id)
        row = session.execute(
            text("SELECT wrapped_dek, status FROM sensitive_keys WHERE key_ref = :k FOR UPDATE"),
            {"k": key_ref},
        ).first()
        if row is not None:
            if row[1] == "destroyed":
                raise CryptoError("KEY_DESTROYED", key_ref)
            return self._unwrap(bytes(row[0]), key_ref)
        dek = AESGCM.generate_key(bit_length=256)
        session.execute(
            text(
                "INSERT INTO sensitive_keys (key_ref, workspace_id, target_type, target_id, "
                "wrapped_dek, "
                "master_key_id, status) VALUES (:k, :ws, :tt, :ti, :w, :m, 'active')"
            ),
            {
                "k": key_ref,
                "ws": uuid.UUID(workspace_id),
                "tt": target_type,
                "ti": target_id,
                "w": self._wrap(dek, key_ref),
                "m": self._master.key_id,
            },
        )
        return dek

    # -- payload encryption -----------------------------------------------------------------
    def encrypt(
        self,
        session: Session,
        workspace_id: str,
        target_type: str,
        target_id: str,
        plaintext: dict[str, Any],
    ) -> tuple[bytes, str]:
        key_ref = self.key_ref_for(workspace_id, target_type, target_id)
        dek = self.ensure_dek(session, workspace_id, target_type, target_id)
        nonce = os.urandom(12)
        ciphertext = nonce + AESGCM(dek).encrypt(nonce, canonical_json(plaintext), key_ref.encode())
        return ciphertext, key_ref

    def decrypt(self, session: Session, key_ref: str, ciphertext: bytes) -> dict[str, Any]:
        row = session.execute(
            text("SELECT wrapped_dek, status FROM sensitive_keys WHERE key_ref = :k"),
            {"k": key_ref},
        ).first()
        if row is None:
            raise CryptoError("KEY_UNKNOWN", key_ref)
        if row[1] == "destroyed" or row[0] is None:
            raise CryptoError("KEY_DESTROYED", key_ref)
        dek = self._unwrap(bytes(row[0]), key_ref)
        import json

        plain = AESGCM(dek).decrypt(ciphertext[:12], ciphertext[12:], key_ref.encode())
        result: dict[str, Any] = json.loads(plain)
        return result

    # -- crypto-shredding ------------------------------------------------------------------
    def destroy(
        self,
        session: Session,
        key_ref: str,
        requested_by: str,
        reason: str,
        audit_event_id: str | None = None,
    ) -> str:
        """Destroy the DEK (wrapped key removed), append a chained tombstone; return its hash."""
        row = session.execute(
            text(
                "SELECT workspace_id, target_type, target_id, status FROM sensitive_keys WHERE "
                "key_ref = :k FOR UPDATE"
            ),
            {"k": key_ref},
        ).first()
        if row is None:
            raise CryptoError("KEY_UNKNOWN", key_ref)
        if row[3] == "destroyed":
            raise CryptoError("KEY_ALREADY_DESTROYED", key_ref)
        session.execute(
            text(
                "UPDATE sensitive_keys SET wrapped_dek = NULL, status = 'destroyed', destroyed_at "
                "= now() WHERE key_ref = :k"
            ),
            {"k": key_ref},
        )
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('key_tombstones_chain'))"))
        previous = last_hash(session, TOMBSTONE_CHAIN)
        fields = {
            "key_ref": key_ref,
            "workspace_id": row[0],
            "target_type": row[1],
            "target_id": row[2],
            "reason": reason,
            "requested_by": uuid.UUID(requested_by),
            "audit_event_id": audit_event_id,
            "destroyed_at": self._clock.now(),
        }
        content_hash = chain_hash(hashed_row_fields(TOMBSTONE_CHAIN, fields), previous)
        session.execute(
            text(
                "INSERT INTO key_tombstones (key_ref, workspace_id, target_type, target_id, "
                "reason, requested_by, "
                "audit_event_id, previous_hash, content_hash, destroyed_at) VALUES (:key_ref, "
                ":workspace_id, :target_type, "
                ":target_id, :reason, :requested_by, :audit_event_id, :prev, :hash, :destroyed_at)"
            ),
            {**fields, "prev": previous, "hash": content_hash},
        )
        return content_hash
