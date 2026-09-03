"""Encrypted local Secret provider (development plan §9.2; P4-05).

Each secret version is AES-256-GCM encrypted under its own data-encryption key (DEK); the DEK is
wrapped by the instance master key, which lives only in the environment or an owner-only key
file and never in the database. A database dump therefore contains ciphertext and wrapped DEKs
only (V-P4-10/V-P4-17). Values are handled as ``bytes`` in memory; nothing here logs, hashes or
measures a value.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.secrets.envelope import MasterKey
from server.secrets.provider import (
    Lease,
    LeaseScope,
    ProviderHealth,
    ResolveContext,
    SecretError,
    SecretRef,
    register_provider,
)

PROVIDER_NAME = "local"
ENV_MASTER_KEY = "AGENT_COLAB_MASTER_KEY_B64"  # nosec B105 - environment variable name
ENV_MASTER_KEY_FILE = "AGENT_COLAB_MASTER_KEY_FILE"  # nosec B105 - environment variable name
ENV_MASTER_KEY_ID = "AGENT_COLAB_MASTER_KEY_ID"  # nosec B105 - environment variable name


def dek_id_for(secret_ref: str, version: int) -> str:
    return f"dek://secret/{secret_ref}/v{version}"


def load_master_key(env: Mapping[str, str] | None = None) -> MasterKey:
    """Master key from an owner-only key file or the environment; never from the database."""
    env = os.environ if env is None else env
    key_id = env.get(ENV_MASTER_KEY_ID, "mk-local-1")
    path = env.get(ENV_MASTER_KEY_FILE)
    if path:
        file = Path(path)
        try:
            mode = file.stat().st_mode
        except OSError as exc:
            raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "master key file missing") from exc
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecretError(
                "SECRET_PROVIDER_UNAVAILABLE", "master key file must be owner-only (0600)"
            )
        try:
            return MasterKey.from_b64(key_id, file.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "master key file invalid") from exc
    value = env.get(ENV_MASTER_KEY)
    if not value:
        raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "no master key configured")
    try:
        return MasterKey.from_b64(key_id, value)
    except ValueError as exc:
        raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "master key invalid") from exc


def _wrap(master: MasterKey, dek: bytes, dek_id: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(master.key).encrypt(nonce, dek, dek_id.encode())


def _unwrap(master: MasterKey, wrapped: bytes, dek_id: str) -> bytes:
    return AESGCM(master.key).decrypt(wrapped[:12], wrapped[12:], dek_id.encode())


def _encrypt(dek: bytes, value: bytes, dek_id: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(dek).encrypt(nonce, value, dek_id.encode())


def _decrypt(dek: bytes, ciphertext: bytes, dek_id: str) -> bytes:
    return AESGCM(dek).decrypt(ciphertext[:12], ciphertext[12:], dek_id.encode())


# ---------------------------------------------------------------- session-level operations


def put_secret(
    session: Session,
    master: MasterKey,
    *,
    workspace_id: uuid.UUID,
    name: str,
    value: bytes,
    metadata: Mapping[str, Any],
    created_by: uuid.UUID | None,
    now: dt.datetime,
    secret_ref: str | None = None,
) -> SecretRef:
    """Register a new secret (version 1). Metadata must not contain the value."""
    ref = secret_ref or f"sec-{uuid.uuid4().hex[:24]}"
    if session.execute(
        text("SELECT 1 FROM secrets WHERE workspace_id = :w AND name = :n"),
        {"w": workspace_id, "n": name},
    ).first():
        raise SecretError("SECRET_NOT_FOUND", f"name {name!r} already registered")
    session.execute(
        text(
            "INSERT INTO secrets (secret_ref, workspace_id, name, provider, current_version, "
            "metadata, status, created_by, created_at) VALUES (:r, :w, :n, :p, 1, "
            "CAST(:m AS jsonb), 'registered', :c, :t)"
        ),
        {
            "r": ref,
            "w": workspace_id,
            "n": name,
            "p": PROVIDER_NAME,
            "m": __import__("json").dumps(dict(metadata)),
            "c": created_by,
            "t": now,
        },
    )
    _store_version(session, master, ref, 1, value, now)
    return SecretRef(ref, 1, PROVIDER_NAME, dict(metadata))


def _store_version(
    session: Session, master: MasterKey, ref: str, version: int, value: bytes, now: dt.datetime
) -> None:
    dek_id = dek_id_for(ref, version)
    dek = AESGCM.generate_key(bit_length=256)
    session.execute(
        text(
            "INSERT INTO secret_versions (secret_ref, version, dek_id, ciphertext, wrapped_dek, "
            "master_key_id, status, created_at) VALUES (:r, :v, :d, :c, :w, :m, 'active', :t)"
        ),
        {
            "r": ref,
            "v": version,
            "d": dek_id,
            "c": _encrypt(dek, value, dek_id),
            "w": _wrap(master, dek, dek_id),
            "m": master.key_id,
            "t": now,
        },
    )


def rotate_secret(
    session: Session,
    master: MasterKey,
    *,
    secret_ref: str,
    value: bytes,
    now: dt.datetime,
) -> SecretRef:
    row = session.execute(
        text(
            "SELECT current_version, metadata, status FROM secrets WHERE secret_ref = :r FOR UPDATE"
        ),
        {"r": secret_ref},
    ).first()
    if row is None or row[2] == "retired":
        raise SecretError("SECRET_NOT_FOUND", secret_ref)
    version = int(row[0]) + 1
    _store_version(session, master, secret_ref, version, value, now)
    session.execute(
        text(
            "UPDATE secrets SET current_version = :v, status = 'rotated', rotated_at = :t "
            "WHERE secret_ref = :r"
        ),
        {"v": version, "t": now, "r": secret_ref},
    )
    return SecretRef(secret_ref, version, PROVIDER_NAME, dict(row[1] or {}))


def read_secret_bytes(
    session: Session, master: MasterKey, secret_ref: str, version: int | None = None
) -> tuple[bytes, int]:
    """Decrypt one version (current by default). Destroyed DEKs can never be decrypted again."""
    if version is None:
        cur = session.execute(
            text("SELECT current_version FROM secrets WHERE secret_ref = :r"), {"r": secret_ref}
        ).first()
        if cur is None:
            raise SecretError("SECRET_NOT_FOUND", secret_ref)
        version = int(cur[0])
    row = session.execute(
        text(
            "SELECT dek_id, ciphertext, wrapped_dek, status FROM secret_versions "
            "WHERE secret_ref = :r AND version = :v"
        ),
        {"r": secret_ref, "v": version},
    ).first()
    if row is None:
        raise SecretError("SECRET_NOT_FOUND", f"{secret_ref} v{version}")
    if row[3] == "destroyed" or row[2] is None:
        raise SecretError("SECRET_HANDLE_REVOKED", f"{secret_ref} v{version} destroyed")
    try:
        dek = _unwrap(master, bytes(row[2]), str(row[0]))
        return _decrypt(dek, bytes(row[1]), str(row[0])), version
    except Exception as exc:  # wrong master key: never leak which step failed
        raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "decryption failed") from exc


def destroy_version(session: Session, secret_ref: str, version: int, now: dt.datetime) -> str:
    """Crypto-shred one version (wrapped DEK removed); returns its dek_id for the ledger."""
    row = session.execute(
        text(
            "SELECT dek_id FROM secret_versions WHERE secret_ref = :r AND version = :v FOR UPDATE"
        ),
        {"r": secret_ref, "v": version},
    ).first()
    if row is None:
        raise SecretError("SECRET_NOT_FOUND", f"{secret_ref} v{version}")
    session.execute(
        text(
            "UPDATE secret_versions SET wrapped_dek = NULL, status = 'destroyed', "
            "destroyed_at = :t WHERE secret_ref = :r AND version = :v"
        ),
        {"t": now, "r": secret_ref, "v": version},
    )
    return str(row[0])


def secret_view(session: Session, secret_ref: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT secret_ref, workspace_id, name, provider, "
                "current_version, metadata, status, "
                "created_at, rotated_at FROM secrets WHERE secret_ref = :r"
            ),
            {"r": secret_ref},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "secret_ref": row["secret_ref"],
        "name": row["name"],
        "provider": row["provider"],
        "current_version": int(row["current_version"]),
        "metadata": dict(row["metadata"] or {}),
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "rotated_at": row["rotated_at"].isoformat() if row["rotated_at"] else None,
    }


def list_secrets(session: Session, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    refs = session.execute(
        text("SELECT secret_ref FROM secrets WHERE workspace_id = :w ORDER BY name"),
        {"w": workspace_id},
    ).all()
    return [v for r in refs if (v := secret_view(session, str(r[0]))) is not None]


# ---------------------------------------------------------------- §9.1 provider object


class LocalSecretProvider:
    """§9.1 provider over a session factory (lease/resolve/revoke delegate to the Broker)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        session_factory: Callable[[], Session],
        master: MasterKey,
        *,
        workspace_id: uuid.UUID,
        clock: Clock | None = None,
        created_by: uuid.UUID | None = None,
    ) -> None:
        self._factory = session_factory
        self._master = master
        self._workspace = workspace_id
        self._clock = clock or SystemClock()
        self._created_by = created_by

    @property
    def master(self) -> MasterKey:
        return self._master

    def put(self, name: str, value: bytes, metadata: Mapping[str, Any]) -> SecretRef:
        with self._factory() as s, s.begin():
            return put_secret(
                s,
                self._master,
                workspace_id=self._workspace,
                name=name,
                value=value,
                metadata=metadata,
                created_by=self._created_by,
                now=self._clock.now(),
            )

    def lease(
        self, secret_ref: str, scope: LeaseScope, ttl: dt.timedelta, *, single_use: bool = True
    ) -> Lease:
        from server.secrets import broker

        with self._factory() as s, s.begin():
            return broker.issue_lease(
                s,
                workspace_id=self._workspace,
                secret_ref=secret_ref,
                scope=scope,
                ttl=ttl,
                single_use=single_use,
                now=self._clock.now(),
                actor_label="provider",
                correlation_id="provider:lease",
            )

    def resolve(self, handle: str, context: ResolveContext) -> bytes:
        from server.secrets import broker

        with self._factory() as s, s.begin():
            return broker.resolve(
                s,
                self._master,
                workspace_id=self._workspace,
                handle=handle,
                context=context,
                now=self._clock.now(),
                actor_uuid=None,
                actor_label=context.agent_id,
                correlation_id="provider:resolve",
                store=None,
            )

    def revoke(self, grant_or_lease_id: str) -> int:
        from server.secrets import broker

        with self._factory() as s, s.begin():
            kind = "grant" if grant_or_lease_id.startswith("grant-") else "lease"
            return len(
                broker.revoke(
                    s,
                    workspace_id=self._workspace,
                    kind=kind,
                    target_id=grant_or_lease_id,
                    reason="PROVIDER_REVOKE",
                    now=self._clock.now(),
                    actor_label="provider",
                    correlation_id="provider:revoke",
                    store=None,
                    actor_uuid=None,
                )
            )

    def rotate(self, secret_ref: str, value: bytes) -> SecretRef:
        with self._factory() as s, s.begin():
            return rotate_secret(
                s, self._master, secret_ref=secret_ref, value=value, now=self._clock.now()
            )

    def health(self) -> ProviderHealth:
        try:
            with self._factory() as s:
                s.execute(text("SELECT count(*) FROM secrets")).scalar_one()
            # the master key must round-trip a probe DEK without touching any stored secret
            probe = AESGCM.generate_key(bit_length=256)
            ok = _unwrap(self._master, _wrap(self._master, probe, "probe"), "probe") == probe
            return ProviderHealth(PROVIDER_NAME, ok, "" if ok else "master key probe failed")
        except Exception as exc:
            return ProviderHealth(PROVIDER_NAME, False, type(exc).__name__)


def _factory(config: Mapping[str, Any]) -> LocalSecretProvider:
    factory = config["session_factory"]
    master = config.get("master_key") or load_master_key()
    return LocalSecretProvider(
        factory,
        master,
        workspace_id=uuid.UUID(str(config["workspace_id"])),
        clock=config.get("clock"),
    )


register_provider(PROVIDER_NAME, _factory)


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode()
