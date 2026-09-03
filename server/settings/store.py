"""Layered, versioned settings store (development plan §8.2; P4-04).

Resolution: ``emergency env > runtime version > setup default version > built-in default``.
Every change is validated before apply, stored as a new immutable version (secret values are
envelope-encrypted; non-secret values are JSON), audited with a *redacted* old/new diff linked by
version, and mirrored as a ``SETTING_CHANGED`` Event. Secret values are never re-displayed: views
carry only a fingerprint and the version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.events.store import AppendRequest, EventStore, EventStoreError
from server.observability.audit import append_audit
from server.secrets.envelope import EnvelopeCrypto
from server.settings.registry import (
    REGISTRY,
    SettingsError,
    SettingSpec,
    SettingView,
    env_name,
    spec_for,
    validate,
)

REDACTED = "<redacted>"


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def _aggregate_id(key: str) -> str:
    return "set-" + key.replace(".", "-")


@dataclass(frozen=True)
class StoredVersion:
    key: str
    version: int
    secret: bool
    value: Any  # decrypted only when the caller asks for the effective value
    value_fingerprint: str
    changed_by: uuid.UUID | None
    changed_at: dt.datetime
    reason: str
    layer: str
    audit_id: str | None


class SettingsStore:
    """All methods run inside the caller's session/transaction."""

    def __init__(self, crypto: EnvelopeCrypto | None, clock: Clock | None = None) -> None:
        self._crypto = crypto
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ reads
    def latest(self, session: Session, key: str, *, decrypt: bool = False) -> StoredVersion | None:
        row = session.execute(
            text(
                "SELECT version, secret, value_json, value_ciphertext, key_ref, value_fingerprint, "
                "changed_by, changed_at, reason, layer, audit_id FROM settings_versions "
                "WHERE setting_key = :k ORDER BY version DESC LIMIT 1"
            ),
            {"k": key},
        ).first()
        return None if row is None else self._row(key, row, session, decrypt)

    def version(
        self, session: Session, key: str, version: int, *, decrypt: bool = False
    ) -> StoredVersion | None:
        row = session.execute(
            text(
                "SELECT version, secret, value_json, value_ciphertext, key_ref, value_fingerprint, "
                "changed_by, changed_at, reason, layer, audit_id FROM settings_versions "
                "WHERE setting_key = :k AND version = :v"
            ),
            {"k": key, "v": version},
        ).first()
        return None if row is None else self._row(key, row, session, decrypt)

    def _row(self, key: str, row: Any, session: Session, decrypt: bool) -> StoredVersion:
        secret = bool(row[1])
        if secret:
            value: Any = REDACTED
            if decrypt:
                if self._crypto is None:
                    raise SettingsError("SETTING_CRYPTO_UNAVAILABLE", key)
                value = self._crypto.decrypt(session, str(row[4]), bytes(row[3]))["value"]
        else:
            value = row[2]
        return StoredVersion(
            key=key,
            version=int(row[0]),
            secret=secret,
            value=value,
            value_fingerprint=str(row[5]),
            changed_by=row[6],
            changed_at=row[7],
            reason=str(row[8] or ""),
            layer=str(row[9]),
            audit_id=row[10],
        )

    def effective(
        self, session: Session, key: str, *, decrypt: bool = False
    ) -> tuple[Any, str, int]:
        """(value, layer, version) after applying the precedence rule."""
        spec = spec_for(key)
        env_value = os.environ.get(env_name(key))
        if env_value is not None and env_value != "":
            return validate(spec, env_value), "emergency_env", 0
        stored = self.latest(session, key, decrypt=decrypt)
        if stored is not None:
            return stored.value, stored.layer, stored.version
        return spec.default, "builtin", 0

    def value(self, session: Session, key: str) -> Any:
        """Effective value with secrets decrypted (server-internal use only)."""
        return self.effective(session, key, decrypt=True)[0]

    def view(self, session: Session, key: str) -> SettingView:
        spec = spec_for(key)
        value, layer, version = self.effective(session, key)
        stored = self.latest(session, key)
        extra: dict[str, Any] = {}
        if spec.secret:
            extra["configured"] = bool(stored is not None or (layer == "emergency_env"))
            if stored is not None:
                extra["fingerprint"] = stored.value_fingerprint
            value = REDACTED if extra["configured"] else ""
        return SettingView(
            key=key,
            scope=spec.scope,
            type=str(spec.type),
            secret=spec.secret,
            restart_required=spec.restart_required,
            layer=layer,
            version=version,
            value=value,
            changed_by=None
            if stored is None or stored.changed_by is None
            else str(stored.changed_by),
            changed_at=None if stored is None else isoformat_utc(stored.changed_at),
            description=spec.description,
            extra=extra,
        )

    def views(self, session: Session) -> list[SettingView]:
        return [self.view(session, key) for key in sorted(REGISTRY)]

    # ------------------------------------------------------------------ writes
    def redacted_diff(
        self, spec: SettingSpec, old: StoredVersion | None, new_value: Any
    ) -> dict[str, Any]:
        """Old/new for the audit trail; secrets carry fingerprints only. The dictionary avoids
        the audit layer's redacted key names (``value``/``key``), keeping non-secret diffs
        legible."""
        if spec.secret:
            return {
                "setting": spec.key,
                "before": None
                if old is None
                else {"version": old.version, "fingerprint": old.value_fingerprint},
                "after": {"fingerprint": fingerprint(new_value)},
                "secret": True,
            }
        return {
            "setting": spec.key,
            "before": None if old is None else {"version": old.version, "was": old.value},
            "after": {"is": new_value},
            "secret": False,
        }

    def set(
        self,
        session: Session,
        key: str,
        value: Any,
        *,
        workspace_id: uuid.UUID,
        changed_by: uuid.UUID | None,
        actor_label: str,
        correlation_id: str,
        reason: str = "",
        layer: str = "runtime",
        store: EventStore | None = None,
    ) -> StoredVersion:
        """Validate, then persist a new version + audit (+ Event). Raises before any write."""
        spec = spec_for(key)
        normalized = validate(spec, value)  # rejected before apply (V-P4-05)
        old = self.latest(session, key)
        diff = self.redacted_diff(spec, old, normalized)
        version = 1 if old is None else old.version + 1
        fp = fingerprint(normalized)
        now = self._clock.now()
        audit_id = append_audit(
            session,
            action="settings.change",
            target_type="setting",
            target_id=key,
            result="OK",
            actor_label=actor_label,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            actor_account_id=changed_by,
            metadata={
                "diff": diff,
                "version": version,
                "previous_version": None if old is None else old.version,
                "reason": reason,
                "layer": layer,
            },
            clock=self._clock,
        )
        event_id: str | None = None
        if store is not None and changed_by is not None:
            try:
                res = store.append(
                    AppendRequest(
                        workspace_id=str(workspace_id),
                        aggregate_type="setting",
                        aggregate_id=_aggregate_id(key),
                        type="SETTING_CHANGED",
                        actor_account_id=str(changed_by),
                        correlation_id=correlation_id,
                        idempotency_scope="setting:change",
                        idempotency_key=f"{key}:{version}:{fp}",
                        payload={"setting_key": key, "version": version, "secret": spec.secret},
                    )
                )
                event_id = res.event_id
            except EventStoreError as exc:  # the audit row is the authority; the Event mirrors it
                if exc.code != "IDEMPOTENCY_CONFLICT":
                    raise
        params: dict[str, Any] = {
            "k": key,
            "v": version,
            "s": spec.secret,
            "fp": fp,
            "by": changed_by,
            "at": now,
            "r": reason,
            "layer": layer,
            "aud": audit_id,
            "ev": event_id,
        }
        if spec.secret:
            if self._crypto is None:
                raise SettingsError("SETTING_CRYPTO_UNAVAILABLE", key)
            ciphertext, key_ref = self._crypto.encrypt(
                session, str(workspace_id), "setting", key, {"value": normalized}
            )
            params.update({"vj": None, "vc": ciphertext, "kr": key_ref})
        else:
            params.update({"vj": json.dumps(normalized), "vc": None, "kr": None})
        session.execute(
            text(
                "INSERT INTO settings_versions (setting_key, version, secret, value_json, "
                "value_ciphertext, key_ref, value_fingerprint, changed_by, changed_at, reason, "
                "layer, audit_id, event_id) VALUES (:k, :v, :s, CAST(:vj AS jsonb), :vc, :kr, "
                ":fp, :by, :at, :r, :layer, :aud, :ev)"
            ),
            params,
        )
        stored = self.latest(session, key)
        assert stored is not None
        return stored

    def rollback(
        self,
        session: Session,
        key: str,
        to_version: int,
        *,
        workspace_id: uuid.UUID,
        changed_by: uuid.UUID | None,
        actor_label: str,
        correlation_id: str,
        store: EventStore | None = None,
    ) -> StoredVersion:
        """Rollback = a new version carrying the old value (history is immutable)."""
        target = self.version(session, key, to_version, decrypt=True)
        if target is None:
            raise SettingsError("SETTING_VERSION_UNKNOWN", f"{key}@{to_version}")
        return self.set(
            session,
            key,
            target.value,
            workspace_id=workspace_id,
            changed_by=changed_by,
            actor_label=actor_label,
            correlation_id=correlation_id,
            reason=f"rollback to version {to_version}",
            store=store,
        )

    def history(self, session: Session, key: str) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                "SELECT version, secret, value_json, value_fingerprint, changed_by, changed_at, "
                "reason, layer, audit_id, event_id FROM settings_versions WHERE setting_key = :k "
                "ORDER BY version"
            ),
            {"k": key},
        ).all()
        return [
            {
                "version": int(r[0]),
                "value": REDACTED if r[1] else r[2],
                "fingerprint": r[3],
                "changed_by": None if r[4] is None else str(r[4]),
                "changed_at": isoformat_utc(r[5]),
                "reason": r[6],
                "layer": r[7],
                "audit_id": r[8],
                "event_id": r[9],
            }
            for r in rows
        ]
