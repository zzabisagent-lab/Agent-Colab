"""Setup Wizard service (development plan §8.1-§8.3; P4-03).

Composes the Phase 0 setup primitives into the HTTP-facing flow:

* state machine + sealed local store + DB ``setup_state`` (reconciled on load);
* setup token guard (CSPRNG 256-bit, 30-minute TTL, single-use, 5 failures / 15 min per source);
* pre-DB handles for the DB password and initial key material (process memory, 15-minute TTL);
* preflight probes (DB, secret provider/master key, Mattermost, storage) with guidance;
* the ordered, crash-safe apply: ``DB/migration → key provider → [Owner/TOTP/recovery code →
  integration settings → CONFIGURED/LOCKED]`` where the bracketed steps share ONE transaction, so
  a kill after any step leaves no partial Owner or CONFIGURED record (V-P4-04, V-P4-28);
* reconfiguration from LOCKED (maintenance mode + recovery code + MFA re-auth, 30-minute session).

No secret value is persisted or returned twice: the Owner service token, the TOTP secret and the
recovery code appear exactly once in the successful bootstrap response.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.db.engine import make_engine, make_session_factory, run_migrations
from server.domain.clock import Clock, isoformat_utc
from server.identity.principals import issue_service_token
from server.observability.audit import append_audit
from server.policy.catalog import default_catalog
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import EnvelopeCrypto, MasterKey, new_master_key
from server.security import reauth
from server.security.mfa_store import save_recovery_code_hash, save_totp_enrollment
from server.settings.preflight import (
    MattermostProbe,
    ProbeResult,
    probe_mattermost,
    probe_secret_provider,
    probe_storage,
)
from server.settings.registry import SettingsError, spec_for, validate
from server.settings.store import SettingsStore
from server.setup.bootstrap_store import BootstrapStore
from server.setup.errors import SetupError
from server.setup.handles import PreDbHandleStore
from server.setup.order import ApplyOrder, ApplyStep
from server.setup.reconcile import reconcile
from server.setup.state import (
    STAGE_ORDINAL,
    ReconfigurationProof,
    ReconfigurationSession,
    SetupState,
    SetupStateMachine,
)
from server.setup.token import SetupTokenGuard, TokenRecord, token_fingerprint
from server.setup.transport import (
    CHECK_PASSED,
    TransportDecision,
    TransportRequest,
    evaluate_transport,
)

log = logging.getLogger(__name__)
OWNER_ROLE = "role-system-owner"
INTEGRATION_KEYS = (
    "instance.name",
    "instance.base_url",
    "instance.default_timezone",
    "instance.default_language",
    "mattermost.url",
    "mattermost.team",
    "storage.artifact_root",
    "storage.document_root",
    "secrets.provider",
    "secrets.master_key_path",
    "ops.channel_id",
)
_SECRET_INPUT_KEYS = ("db_password", "master_key_b64", "mattermost.bot_token")


class ProcessKilledError(SetupError):
    """Test hook only: models a process kill right after an apply step (V-P4-04/V-P4-28)."""


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def recovery_code_hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").upper().encode("ascii")).hexdigest()


def new_recovery_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def new_totp_secret_b32() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


@dataclass
class RejectionLog:
    """Redacted rejection entries (ip, token fingerprint, code, time) — one per rejection."""

    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SetupService:
    clock: Clock
    store_path: Path
    bind_host: str = "127.0.0.1"
    master_key_id: str = "mk-local-1"
    trust_proxy: bool = False
    allowlist: tuple[str, ...] = ()
    session_factory: Any = None  # set when the DB exists (before or after bootstrap)
    crypto: EnvelopeCrypto | None = None
    mattermost_probe: MattermostProbe | None = None
    fail_after: ApplyStep | None = None  # test hook: simulate a process kill right after a step
    on_configured: Callable[[Any, EnvelopeCrypto | None], None] | None = None
    machine: SetupStateMachine = field(init=False)
    guard: SetupTokenGuard = field(init=False)
    handles: PreDbHandleStore = field(init=False)
    store: BootstrapStore = field(init=False)
    order: ApplyOrder = field(default_factory=ApplyOrder)
    pointers: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)  # non-secret inputs; secrets → handles
    handle_ids: dict[str, str] = field(default_factory=dict)
    rejections: RejectionLog = field(default_factory=RejectionLog)
    instance_id: str = field(default_factory=lambda: "inst-" + uuid.uuid4().hex[:12])
    last_preflight: list[ProbeResult] = field(default_factory=list)
    _db_url: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.machine = SetupStateMachine(self.clock)
        self.guard = SetupTokenGuard(self.clock)
        self.handles = PreDbHandleStore(self.clock)
        self.store = BootstrapStore(self.store_path, self.clock)

    # ------------------------------------------------------------------ persistence
    def load(self) -> SetupState:
        """Reconcile the sealed local store with the DB record (never regressing)."""
        local = self.store.read() if self.store.exists() else None
        db_record = self._db_record()
        lock_marker = self.store.lock_marker_document({"instance_id": self.instance_id})
        if db_record is not None:
            self.instance_id = str(db_record.get("instance_id") or self.instance_id)
            lock_marker = self.store.lock_marker_document({"instance_id": self.instance_id})
        result = reconcile(local, db_record, lock_marker)
        self.machine.state = result.state
        if result.local_document is not None and result.local_document != local:
            self.store.write(result.local_document)
        if local is not None:
            self.pointers = dict(local.get("config_pointers", {}))
            if local.get("token_hash") and local.get("token_fingerprint"):
                self.guard.load(
                    TokenRecord(
                        token_hash=str(local["token_hash"]),
                        token_fingerprint=str(local["token_fingerprint"]),
                        issued_at=self.clock.now(),
                        expires_at=dt.datetime.strptime(
                            str(local["token_expires_at"]), "%Y-%m-%dT%H:%M:%S.%fZ"
                        ).replace(tzinfo=dt.UTC),
                        used=bool(local.get("token_used")),
                    )
                )
        if self.machine.state is SetupState.RECONFIGURING:
            self._restore_reconfiguration_session()
        if self.machine.state is SetupState.BOOTSTRAPPING:
            # the previous process died mid-bootstrap: nothing partial survived (single
            # transaction), so the operator re-enters the handles and retries
            retry = self.guard.issue()
            self.machine.fail_bootstrap(
                "INTERRUPTED", "SETUP_INTERRUPTED", retry.record.token_fingerprint
            )
            self.persist_local()
        return self.machine.state

    def _db_record(self) -> dict[str, Any] | None:
        if self.session_factory is None:
            return None
        with self.session_factory() as session:
            try:
                row = session.execute(
                    text("SELECT state, stage_ordinal, instance_id FROM setup_state WHERE id = 1")
                ).first()
            except Exception:
                session.rollback()
                return None  # migrations not applied yet: the DB has no opinion
            if row is None:
                return None
            return {"state": str(row[0]), "stage_ordinal": int(row[1]), "instance_id": row[2]}

    def _persist_db_state(self, session: Session, state: SetupState, **cols: Any) -> None:
        now = self.clock.now()
        session.execute(
            text(
                "INSERT INTO setup_state (id, state, stage_ordinal, instance_id, configured_at, "
                "locked_at, endpoint_lock, last_failure, updated_at) VALUES (1, :s, :o, :i, "
                ":c, :l, CAST(:e AS jsonb), CAST(:f AS jsonb), :now) "
                "ON CONFLICT (id) DO UPDATE SET "
                "state = EXCLUDED.state, stage_ordinal = EXCLUDED.stage_ordinal, "
                "configured_at = COALESCE(EXCLUDED.configured_at, setup_state.configured_at), "
                "locked_at = COALESCE(EXCLUDED.locked_at, setup_state.locked_at), "
                "endpoint_lock = EXCLUDED.endpoint_lock, last_failure = EXCLUDED.last_failure, "
                "updated_at = EXCLUDED.updated_at"
            ),
            {
                "s": state.value,
                "o": STAGE_ORDINAL[state],
                "i": self.instance_id,
                "c": cols.get("configured_at"),
                "l": cols.get("locked_at"),
                "e": _json(cols.get("endpoint_lock", {})),
                "f": _json(cols.get("last_failure")),
                "now": now,
            },
        )

    def local_document(self) -> dict[str, Any]:
        base = self.store.read() if self.store.exists() else self.store.initial_document()
        record = self.guard.record
        doc = {
            **base,
            "state": self.machine.state.value,
            "stage_ordinal": STAGE_ORDINAL[self.machine.state],
            "config_pointers": dict(self.pointers),
            "failure_counters": self.guard.failure_counters(),
            "last_failure": None
            if self.machine.failure is None
            else {
                "failed_step": self.machine.failure.failed_step,
                "error_code": self.machine.failure.error_code,
                "retry_token_fingerprint": self.machine.failure.retry_token_fingerprint,
                "failed_at": self.machine.failure.failed_at,
            },
            "lock_marker": False,
            # every not-yet-audited rejection is kept until the DB takes it over (F-P4-002)
            "rejection_log": [e for e in self.rejections.entries if not e.get("audited")][-500:],
        }
        if record is not None:
            doc.update(record.as_store_fields())
        else:
            doc.update(
                {
                    "token_hash": None,  # nosec B105 - literal flag, not a value
                    "token_fingerprint": None,  # nosec B105 - literal flag, not a value
                    "token_expires_at": None,  # nosec B105 - literal flag, not a value
                    "token_used": False,  # nosec B105 - literal flag, not a value
                }
            )
        return doc

    def persist_local(self, *, allow_retry_regression: bool = False) -> None:
        self.store.write(self.local_document(), allow_retry_regression=allow_retry_regression)

    # ------------------------------------------------------------------ transport
    def transport(
        self,
        remote_addr: str,
        *,
        forwarded_proto: str | None,
        client_cert_verified: bool,
        presented_token: str | None,
    ) -> TransportDecision:
        token_check = "SETUP_TOKEN_MISSING"  # noqa: S105 - a code, not a value  # nosec B105 - error code
        if presented_token:
            try:
                self.guard.verify(presented_token, remote_addr, consume=False)
                token_check = CHECK_PASSED
            except SetupError as exc:
                token_check = exc.code
        request = TransportRequest(
            bind_is_loopback=_is_loopback(self.bind_host),
            remote_addr=remote_addr,
            tls_terminated_by_proxy=bool(self.trust_proxy and forwarded_proto == "https"),
            client_mtls_verified=bool(self.trust_proxy and client_cert_verified),
            allowlist=self.allowlist,
            token_check=token_check,
        )
        return evaluate_transport(request)

    # ------------------------------------------------------------------ token
    def issue_token(self) -> str:
        if self.machine.state not in (
            SetupState.UNINITIALIZED,
            SetupState.BOOTSTRAP_FAILED,
            SetupState.PREFLIGHT_PASSED,
        ):
            raise SetupError("SETUP_LOCKED", "tokens are issued before configuration only")
        issued = self.guard.issue()
        self.persist_local()
        return issued.value

    def reject(self, code: str, ip: str, presented: str) -> None:
        """One redacted entry per rejection: local log always, DB audit when a DB exists."""
        entry = {
            "id": f"rej-{uuid.uuid4().hex[:16]}",
            "at": isoformat_utc(self.clock.now()),
            "ip": ip,
            "token_fingerprint": token_fingerprint(presented) if presented else "",
            "code": code,
            "audited": False,
        }
        self.rejections.entries.append(entry)
        try:
            self.persist_local()
        except SetupError as exc:  # the in-memory log still holds the entry
            log.warning("rejection log not persisted: %s", exc.code)
        if self.session_factory is not None:
            try:
                with self.session_factory() as session, session.begin():
                    self._audit_rejection(session, entry)
                entry["audited"] = True
                self.persist_local()
            except Exception as exc:  # audit table may not exist before migration
                log.warning(
                    "rejection audit not written (%s); local log holds it", type(exc).__name__
                )

    def _audit_rejection(self, session: Session, entry: dict[str, Any]) -> None:
        """One redacted ``setup.token_rejected`` AuditEvent per rejection (idempotent by id)."""
        exists = session.execute(
            text(
                "SELECT 1 FROM audit_events WHERE action = 'setup.token_rejected' "
                "AND redacted_metadata->>'id' = :i"
            ),
            {"i": entry["id"]},
        ).first()
        if exists:
            return
        append_audit(
            session,
            action="setup.token_rejected",
            target_type="setup",
            target_id="bootstrap",
            result="DENY",
            actor_label=f"ip:{entry['ip']}",
            correlation_id="setup",
            error_code=str(entry["code"]),
            metadata={k: v for k, v in entry.items() if k != "audited"},
            clock=self.clock,
        )

    def migrate_rejections_to_audit(self, session: Session) -> int:
        """Move every sealed pre-DB rejection into audit_events (atomic with the caller's
        transaction; safe to retry). Returns the number of entries newly audited."""
        moved = 0
        for entry in self.rejections.entries:
            if entry.get("audited"):
                continue
            entry.setdefault("id", f"rej-{uuid.uuid4().hex[:16]}")
            self._audit_rejection(session, entry)
            entry["audited"] = True
            moved += 1
        return moved

    # ------------------------------------------------------------------ inputs
    def configure(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        """Store non-secret inputs; secret inputs become 15-minute in-memory handles."""
        if self.machine.state not in (
            SetupState.UNINITIALIZED,
            SetupState.PREFLIGHT_PASSED,
            SetupState.BOOTSTRAP_FAILED,
        ):
            raise SetupError(
                "SETUP_LOCKED", "configuration inputs are accepted before bootstrap only"
            )
        out: dict[str, Any] = {}
        if section == "db":
            for key in ("db_host", "db_name", "db_user"):
                self.pointers[key] = str(values[key])
            self.pointers["db_port"] = int(values.get("db_port", 5432))
            password = values.get("db_password")
            if password:
                self.handle_ids["db_password"] = self.handles.put(
                    "db_password", str(password)
                ).handle_id
            elif "db_password" in values:  # empty string: local trust auth
                self.handle_ids["db_password"] = self.handles.put("db_password", "").handle_id
            out = {k: self.pointers[k] for k in ("db_host", "db_port", "db_name", "db_user")}
            out["password_handle"] = self.handle_ids.get("db_password") is not None
        elif section == "keys":
            self.pointers["secret_provider"] = str(values.get("secrets.provider", "local"))
            if self.pointers["secret_provider"] == "local":  # noqa: S105 - provider name
                self.pointers["secret_provider"] = "local_encrypted"  # noqa: S105 - provider name  # nosec B105 - provider name
            path = str(
                values.get("secrets.master_key_path", spec_for("secrets.master_key_path").default)
            )
            validate(spec_for("secrets.master_key_path"), path)
            self.pointers["master_key_path"] = path
            material = values.get("master_key_b64")
            if material:
                self.handle_ids["master_key_b64"] = self.handles.put(
                    "master_key_b64", str(material)
                ).handle_id
            out = {
                "secret_provider": self.pointers["secret_provider"],
                "master_key_path": path,
                "key_material_handle": "master_key_b64" in self.handle_ids,
            }
        elif section == "owner":
            self.inputs["owner_account_id"] = str(values.get("account_id", "acct-owner"))
            self.inputs["owner_display_name"] = str(values.get("display_name", "System Owner"))
            out = {
                "account_id": self.inputs["owner_account_id"],
                "display_name": self.inputs["owner_display_name"],
            }
        elif section == "integrations":
            for key in INTEGRATION_KEYS:
                if key in values:
                    self.inputs[key] = validate(spec_for(key), values[key])
            if values.get("mattermost.bot_token"):
                self.handle_ids["mattermost.bot_token"] = self.handles.put(
                    "mattermost.bot_token", str(values["mattermost.bot_token"])
                ).handle_id
            for src, dst in (
                ("instance.name", "instance_name"),
                ("instance.base_url", "base_url"),
                ("instance.default_language", "language"),
                ("instance.default_timezone", "timezone"),
                ("storage.artifact_root", "artifact_path"),
                ("storage.document_root", "document_path"),
            ):
                if src in self.inputs:
                    self.pointers[dst] = self.inputs[src]
            out = {k: v for k, v in self.inputs.items() if k in INTEGRATION_KEYS}
            out["bot_token_handle"] = "mattermost.bot_token" in self.handle_ids
        else:
            raise SetupError("SETUP_SECTION_UNKNOWN", section)
        self.persist_local(allow_retry_regression=False)
        return out

    def _secret(self, name: str) -> str | None:
        handle_id = self.handle_ids.get(name)
        if handle_id is None:
            return None
        return self.handles.resolve(handle_id).decode("utf-8")

    def db_url(self) -> str:
        """Built in memory from pointers + the password handle; never persisted or logged."""
        if self._db_url:
            return self._db_url
        missing = [k for k in ("db_host", "db_name", "db_user") if k not in self.pointers]
        if missing:
            raise SetupError("SETUP_INPUT_MISSING", ",".join(missing))
        password = self._secret("db_password")
        if password is None:
            raise SetupError("SETUP_HANDLE_EXPIRED", "database password: re-enter the value")
        auth = self.pointers["db_user"] + (f":{password}" if password else "")
        host = f"{self.pointers['db_host']}:{self.pointers.get('db_port', 5432)}"
        return f"postgresql://{auth}@{host}/{self.pointers['db_name']}"

    # ------------------------------------------------------------------ preflight
    def preflight(self) -> list[ProbeResult]:
        if self.machine.state not in (
            SetupState.UNINITIALIZED,
            SetupState.PREFLIGHT_PASSED,
            SetupState.BOOTSTRAP_FAILED,
        ):
            raise SetupError("SETUP_LOCKED", "preflight is closed after configuration")
        results = [
            self._probe_db(),
            self._probe_keys(),
            self._probe_mattermost(),
            self._probe_storage(),
        ]
        self.last_preflight = results
        if all(r.ok for r in results) and self.machine.state is not SetupState.PREFLIGHT_PASSED:
            self.machine.transition(SetupState.PREFLIGHT_PASSED, "preflight ok")
            self.persist_local(allow_retry_regression=True)
        return results

    def _probe_db(self) -> ProbeResult:
        from server.settings.preflight import GUIDANCE

        try:
            url = self.db_url()
        except SetupError as exc:
            return ProbeResult("db", False, exc.code, exc.detail, GUIDANCE["db"])
        engine = make_engine(url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                has_tables = conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                ).scalar_one()
                can_create = conn.execute(
                    text(
                        "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
                    )
                ).scalar_one()
        except Exception as exc:
            return ProbeResult(
                "db", False, "PREFLIGHT_DB_UNREACHABLE", type(exc).__name__, GUIDANCE["db"]
            )
        finally:
            engine.dispose()
        if not can_create:
            return ProbeResult(
                "db",
                False,
                "PREFLIGHT_DB_PERMISSION",
                "CREATE privilege required for migrations",
                GUIDANCE["db"],
            )
        return ProbeResult(
            "db",
            True,
            "OK",
            "",
            "",
            {"existing_tables": int(has_tables), "migration": "dry-run ok"},
        )

    def _probe_keys(self) -> ProbeResult:
        provider = self.pointers.get("secret_provider", "local_encrypted")
        if provider == "local_encrypted":
            path = Path(
                self.pointers.get("master_key_path", spec_for("secrets.master_key_path").default)
            )
            material = self._secret("master_key_b64")
            if material is not None:
                try:
                    MasterKey.from_b64(self.master_key_id, material)
                except Exception as exc:
                    return ProbeResult(
                        "secrets",
                        False,
                        "PREFLIGHT_KEY_MATERIAL_INVALID",
                        type(exc).__name__,
                        "Enter 32 bytes of base64 key material or leave it empty "
                        "to generate a key.",
                    )
            parent = path.parent
            if path.exists():
                st = path.stat()
                if st.st_mode & 0o077:
                    return ProbeResult(
                        "secrets",
                        False,
                        "PREFLIGHT_KEY_FILE_PERMISSIONS",
                        str(oct(st.st_mode & 0o777)),
                        "The master key file must be owner-only (0600).",
                    )
            elif not (parent.exists() and os.access(parent, os.W_OK)) and not (
                parent.parent.exists() and os.access(parent.parent, os.W_OK)
            ):
                return ProbeResult(
                    "secrets",
                    False,
                    "PREFLIGHT_KEY_PATH_NOT_WRITABLE",
                    str(parent),
                    "Create the key directory with owner-only permissions.",
                )
            # before the key step the registered local provider cannot be healthy yet: its
            # answer is reported, the readiness decision is the key material/path check above
            local = probe_secret_provider("local", {"master_key_path": str(path)})
            return ProbeResult(
                "secrets",
                True,
                "OK",
                "",
                "",
                {"provider": "local_encrypted", "local_provider_health": local.code},
            )
        return probe_secret_provider(provider, {})

    def _probe_mattermost(self) -> ProbeResult:
        url = self.inputs.get("mattermost.url", "")
        token = self._secret("mattermost.bot_token") or ""
        return probe_mattermost(
            url, token, self.inputs.get("mattermost.team", ""), probe=self.mattermost_probe
        )

    def _probe_storage(self) -> ProbeResult:
        return probe_storage(
            {
                "artifact_root": self.inputs.get(
                    "storage.artifact_root", spec_for("storage.artifact_root").default
                ),
                "document_root": self.inputs.get(
                    "storage.document_root", spec_for("storage.document_root").default
                ),
            }
        )

    # ------------------------------------------------------------------ diff
    def diff(self) -> dict[str, Any]:
        """Redacted view of everything bootstrap will apply (secrets as presence flags only)."""
        return {
            "state": self.machine.state.value,
            "db": {k: self.pointers.get(k) for k in ("db_host", "db_port", "db_name", "db_user")},
            "db_password": "handle" if "db_password" in self.handle_ids else "missing",
            "keys": {
                "secret_provider": self.pointers.get("secret_provider", "local_encrypted"),
                "master_key_path": self.pointers.get("master_key_path"),
                "key_material": "handle" if "master_key_b64" in self.handle_ids else "generate",
            },
            "owner": {
                "account_id": self.inputs.get("owner_account_id", "acct-owner"),
                "display_name": self.inputs.get("owner_display_name", "System Owner"),
            },
            "integrations": {k: self.inputs.get(k, spec_for(k).default) for k in INTEGRATION_KEYS},
            "mattermost.bot_token": "handle"
            if "mattermost.bot_token" in self.handle_ids
            else "missing",
            "apply_order": [s.name for s in ApplyStep],
        }

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, presented_token: str, ip: str) -> dict[str, Any]:
        self.machine.require_bootstrap_open()
        try:
            self.guard.verify(presented_token, ip, consume=True)
        except SetupError as exc:
            self.reject(exc.code, ip, presented_token)
            raise
        if self.machine.state is SetupState.UNINITIALIZED:
            raise SetupError("SETUP_PREFLIGHT_REQUIRED", "run preflight first")
        if self.machine.state is SetupState.BOOTSTRAP_FAILED:
            self.machine.transition(SetupState.PREFLIGHT_PASSED, "retry")
        self.machine.transition(SetupState.BOOTSTRAPPING, "bootstrap")
        self.persist_local()
        self.order = ApplyOrder()
        step = ApplyStep.DB_MIGRATION
        try:
            self._apply_db_migration()
            step = ApplyStep.KEY_PROVIDER
            self._apply_key_provider()
            step = ApplyStep.OWNER_TOTP
            secrets_once = self._apply_owner_integrations_commit()
        except ProcessKilledError:
            raise  # the process is gone: no response, no failure record (restart reconciles)
        except Exception as exc:
            return self._fail(step, exc)
        # committed: the DB is authoritative from here (a kill before the local write is healed
        # by reconcile() on restart)
        self.machine.transition(SetupState.CONFIGURED, "committed")
        self.machine.transition(SetupState.LOCKED, "endpoint locked")
        self._wipe_handles()
        self.store.write(self.store.lock_marker_document({"instance_id": self.instance_id}))
        return {
            "state": self.machine.state.value,
            "instance_id": self.instance_id,
            "owner": {
                "account_id": self.inputs.get("owner_account_id", "acct-owner"),
                "service_token": secrets_once["service_token"],
                "totp_secret_b32": secrets_once["totp_secret_b32"],
                "otpauth_uri": secrets_once["otpauth_uri"],
                "recovery_code": secrets_once["recovery_code"],
            },
            "shown_once": True,
        }

    def _injected_kill(self, step: ApplyStep) -> None:
        if self.fail_after is step:
            self.fail_after = None
            raise ProcessKilledError("SETUP_PROCESS_KILLED", f"simulated kill after {step.name}")

    def _apply_db_migration(self) -> None:
        self.order.begin(ApplyStep.DB_MIGRATION)
        url = self.db_url()
        run_migrations(url)
        self._db_url = url
        if self.session_factory is None:
            self.session_factory = make_session_factory(make_engine(url))
        with self.session_factory() as session, session.begin():
            self._persist_db_state(session, SetupState.BOOTSTRAPPING)
            # every sealed pre-DB rejection becomes an authoritative AuditEvent (F-P4-002)
            self.migrate_rejections_to_audit(session)
        self.order.complete(ApplyStep.DB_MIGRATION)
        self._injected_kill(ApplyStep.DB_MIGRATION)

    def _apply_key_provider(self) -> None:
        self.order.begin(ApplyStep.KEY_PROVIDER)
        path = Path(
            self.pointers.get("master_key_path", spec_for("secrets.master_key_path").default)
        )
        material = self._secret("master_key_b64")
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if material is not None and material != existing:
                raise SetupError(
                    "SETUP_KEY_CONFLICT",
                    "a different master key already exists at the configured path",
                )
            material = existing
        else:
            material = material or new_master_key()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(material + "\n")
        self.crypto = EnvelopeCrypto(MasterKey.from_b64(self.master_key_id, material), self.clock)
        provider = self.pointers.get("secret_provider", "local_encrypted")
        if provider != "local_encrypted":
            health = probe_secret_provider(provider, {})
            if not health.ok:
                raise SetupError("SETUP_SECRET_PROVIDER_UNAVAILABLE", health.code)
        self.order.complete(ApplyStep.KEY_PROVIDER)
        self._injected_kill(ApplyStep.KEY_PROVIDER)

    def _apply_owner_integrations_commit(self) -> dict[str, str]:
        """Owner/TOTP/recovery code, integration settings and the CONFIGURED/LOCKED commit share
        one transaction: a kill anywhere inside leaves no Owner record and no CONFIGURED state."""
        assert self.session_factory is not None and self.crypto is not None
        now = self.clock.now()
        owner_public = self.inputs.get("owner_account_id", "acct-owner")
        display = self.inputs.get("owner_display_name", "System Owner")
        with self.session_factory() as session, session.begin():
            self.order.begin(ApplyStep.OWNER_TOTP)
            if session.execute(
                text("SELECT 1 FROM accounts WHERE account_type = 'human' LIMIT 1")
            ).first():
                raise SetupError(
                    "SETUP_OWNER_EXISTS", "an Owner already exists; bootstrap is not repeatable"
                )
            ws_row = session.execute(
                text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            ).first()
            if ws_row is None:
                ws = uuid.uuid4()
                session.execute(
                    text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :n)"),
                    {
                        "i": ws,
                        "w": "ws-" + self.instance_id[5:],
                        "n": self.inputs.get("instance.name", "Agent-Colab"),
                    },
                )
            else:
                ws = ws_row[0]
            owner = uuid.uuid4()
            session.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, status, "
                    "display_name, auth_subject) VALUES (:i, :a, :w, 'human', 'ACTIVE', :d, :s)"
                ),
                {
                    "i": owner,
                    "a": owner_public,
                    "w": ws,
                    "d": display,
                    "s": f"local:{owner_public}",
                },
            )
            service_token, _fp = issue_service_token(
                session,
                owner_public,
                actor_label="setup",
                correlation_id="setup:bootstrap",
                clock=self.clock,
            )
            self._grant_owner_role(session, ws, owner, now)
            totp_secret = new_totp_secret_b32()
            save_totp_enrollment(session, self.crypto, ws, owner, totp_secret, now)
            recovery = new_recovery_code()
            save_recovery_code_hash(session, owner, recovery_code_hash(recovery), now)
            append_audit(
                session,
                action="setup.owner_created",
                target_type="account",
                target_id=owner_public,
                result="OK",
                actor_label="setup",
                correlation_id="setup:bootstrap",
                workspace_id=ws,
                actor_account_id=owner,
                metadata={"mfa": "totp", "recovery_codes": 1},
                clock=self.clock,
            )
            self.order.complete(ApplyStep.OWNER_TOTP)
            self._injected_kill(ApplyStep.OWNER_TOTP)  # inside the transaction: rolls back
            self.order.begin(ApplyStep.INTEGRATIONS)
            self._apply_integration_settings(session, ws, owner)
            self.order.complete(ApplyStep.INTEGRATIONS)
            self._injected_kill(ApplyStep.INTEGRATIONS)
            self.order.begin(ApplyStep.COMMIT)
            self._persist_db_state(
                session,
                SetupState.LOCKED,
                configured_at=now,
                locked_at=now,
                endpoint_lock={
                    "bind": "loopback" if _is_loopback(self.bind_host) else "remote",
                    "setup_bootstrap": "closed",
                    "locked_by": owner_public,
                },
            )
            append_audit(
                session,
                action="setup.locked",
                target_type="setup",
                target_id="bootstrap",
                result="OK",
                actor_label="setup",
                correlation_id="setup:bootstrap",
                workspace_id=ws,
                actor_account_id=owner,
                metadata={"instance_id": self.instance_id},
                clock=self.clock,
            )
            self.order.complete(ApplyStep.COMMIT)
        self._injected_kill(ApplyStep.COMMIT)  # after the commit: only the local marker is missing
        if self.on_configured is not None:
            self.on_configured(self.session_factory, self.crypto)
        issuer = self.inputs.get("instance.name", "Agent-Colab").replace(" ", "%20")
        return {
            "service_token": service_token,
            "totp_secret_b32": totp_secret,
            "otpauth_uri": f"otpauth://totp/{issuer}:{owner_public}?secret={totp_secret}&issuer={issuer}&digits=6&period=30",
            "recovery_code": recovery,
        }

    def _grant_owner_role(
        self, session: Session, ws: uuid.UUID, owner: uuid.UUID, now: dt.datetime
    ) -> None:
        catalog = default_catalog()
        raw = catalog.roles_raw["roles"][OWNER_ROLE]
        repo = PostgresPolicyRepository()
        if (
            session.execute(
                text("SELECT 1 FROM roles WHERE role_id = :r"), {"r": OWNER_ROLE}
            ).first()
            is None
        ):
            repo.create_role(session, ws, OWNER_ROLE, str(raw["display_name"]))
            repo.commit_role_version(
                session,
                OWNER_ROLE,
                list(raw["permissions"]),
                list(raw.get("deny", [])),
                dict(raw.get("constraints", {})),
                owner,
            )
        repo.assign_role(session, owner, OWNER_ROLE, owner, now)

    def _apply_integration_settings(
        self, session: Session, ws: uuid.UUID, owner: uuid.UUID
    ) -> None:
        store = SettingsStore(self.crypto, self.clock)
        values: dict[str, Any] = {
            k: self.inputs.get(k, spec_for(k).default) for k in INTEGRATION_KEYS
        }
        values["secrets.provider"] = (
            "local"
            if self.pointers.get("secret_provider", "local_encrypted") == "local_encrypted"
            else self.pointers["secret_provider"]
        )
        values["secrets.master_key_path"] = self.pointers.get(
            "master_key_path", spec_for("secrets.master_key_path").default
        )
        token = self._secret("mattermost.bot_token")
        if token:
            values["mattermost.bot_token"] = token
        for key, value in values.items():
            store.set(
                session,
                key,
                value,
                workspace_id=ws,
                changed_by=owner,
                actor_label="setup",
                correlation_id="setup:bootstrap",
                reason="setup default",
                layer="setup_default",
            )

    def _fail(self, step: ApplyStep, exc: Exception) -> dict[str, Any]:
        code = exc.code if isinstance(exc, SetupError | SettingsError) else "SETUP_STEP_FAILED"
        if self.order.in_progress is step:
            self.order.fail(step)
        retry = self.guard.issue()  # stripped of sensitive data: the response carries no secrets
        self.machine.fail_bootstrap(step.name, code, retry.record.token_fingerprint)
        self.persist_local()
        if self.session_factory is not None and step is not ApplyStep.DB_MIGRATION:
            try:
                with self.session_factory() as session, session.begin():
                    self._persist_db_state(
                        session,
                        SetupState.BOOTSTRAP_FAILED,
                        last_failure={
                            "failed_step": step.name,
                            "error_code": code,
                            "retry_token_fingerprint": retry.record.token_fingerprint,
                        },
                    )
            except Exception as db_exc:  # the local store already holds the failure record
                log.warning("failure not recorded in DB: %s", type(db_exc).__name__)
        return {
            "state": self.machine.state.value,
            "failed_step": step.name,
            "error_code": code,
            "detail": type(exc).__name__
            if code == "SETUP_STEP_FAILED"
            else getattr(exc, "detail", ""),
            "owner_created": False,
            "retry_token": retry.value,
            "retry_token_fingerprint": retry.record.token_fingerprint,
            "guidance": "Re-enter the database password and key material (in-memory handles "
            "expire), run preflight, then bootstrap again with the retry token.",
        }

    def _wipe_handles(self) -> None:
        for handle_id in list(self.handle_ids.values()):
            self.handles.revoke(handle_id)
        self.handle_ids.clear()

    # ------------------------------------------------------------------ reconfiguration
    def open_reconfiguration(
        self,
        *,
        owner_account_uuid: str,
        owner_account_id: str,
        recovery_code: str,
        maintenance_active: bool,
        session_id: str | None = None,
    ) -> tuple[ReconfigurationSession, str]:
        """Open a 30-minute session; the single-use recovery code is rotated and the new one is
        returned exactly once with the session."""
        assert self.session_factory is not None
        try:
            self.machine.expire_session_if_due()
        except SetupError:
            pass  # an expired session was closed; LOCKED again
        now = self.clock.now()
        with self.session_factory() as session, session.begin():
            row = session.execute(
                text(
                    "SELECT id FROM recovery_codes WHERE account_id = :a AND code_hash = :h "
                    "AND used_at IS NULL"
                ),
                {"a": uuid.UUID(owner_account_uuid), "h": recovery_code_hash(recovery_code)},
            ).first()
            recovery_ok = row is not None
            if recovery_ok:
                session.execute(
                    text("UPDATE recovery_codes SET used_at = :n WHERE id = :i"),
                    {"n": now, "i": row[0]},
                )
            try:
                reauth.require_recent_mfa(owner_account_uuid, now=now, action="setup_reconfigure")
                mfa_ok = True
            except Exception:
                mfa_ok = False
            proof = ReconfigurationProof(
                maintenance_mode_enabled=maintenance_active,
                recovery_code_verified=recovery_ok,
                mfa_reauth_verified=mfa_ok,
                owner_account_id=owner_account_id,
            )
            sid = session_id or ("reconf-" + secrets.token_hex(8))
            try:
                opened = self.machine.open_reconfiguration(proof, sid)
            except SetupError as exc:
                append_audit(
                    session,
                    action="setup.reconfigure_denied",
                    target_type="setup",
                    target_id="reconfigure",
                    result="DENY",
                    actor_label=owner_account_id,
                    correlation_id=sid,
                    error_code=exc.code,
                    actor_account_id=uuid.UUID(owner_account_uuid),
                    metadata={"missing": proof.missing()},
                    clock=self.clock,
                )
                if recovery_ok:
                    session.execute(
                        text("UPDATE recovery_codes SET used_at = NULL WHERE id = :i"),
                        {"i": row[0]},
                    )
                raise
            session.execute(
                text(
                    "INSERT INTO setup_reconfiguration_sessions (session_id, owner_account_id, "
                    "opened_at, "
                    "expires_at) VALUES (:s, :o, :a, :e)"
                ),
                {
                    "s": sid,
                    "o": uuid.UUID(owner_account_uuid),
                    "a": opened.opened_at,
                    "e": opened.expires_at,
                },
            )
            self._persist_db_state(session, SetupState.RECONFIGURING)
            next_code = new_recovery_code()
            save_recovery_code_hash(
                session, uuid.UUID(owner_account_uuid), recovery_code_hash(next_code), now
            )
            append_audit(
                session,
                action="setup.reconfigure_opened",
                target_type="setup",
                target_id="reconfigure",
                result="OK",
                actor_label=owner_account_id,
                correlation_id=sid,
                actor_account_id=uuid.UUID(owner_account_uuid),
                metadata={
                    "expires_at": isoformat_utc(opened.expires_at),
                    "recovery_code_rotated": True,
                },
                clock=self.clock,
            )
        return opened, next_code

    def _restore_reconfiguration_session(self) -> None:
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.execute(
                text(
                    "SELECT s.session_id, a.account_id, s.opened_at, s.expires_at FROM "
                    "setup_reconfiguration_sessions s JOIN accounts a ON a.id = s.owner_account_id "
                    "WHERE s.closed_at IS NULL ORDER BY s.opened_at DESC LIMIT 1"
                )
            ).first()
        if row is None:
            self.machine.state = SetupState.LOCKED
            return
        self.machine.session = ReconfigurationSession(str(row[0]), str(row[1]), row[2], row[3])
        try:
            self.machine.expire_session_if_due()
        except SetupError:
            self._close_session_row(str(row[0]), "expired")

    def require_session(self, session_id: str, owner_account_id: str) -> ReconfigurationSession:
        try:
            active = self.machine.require_reconfiguring(session_id)
        except SetupError as exc:
            if exc.code == "SETUP_SESSION_EXPIRED":
                self._close_session_row(session_id, "expired")
            raise
        if active.owner_account_id != owner_account_id:
            raise SetupError("SETUP_LOCKED", "session belongs to another account")
        return active

    def apply_reconfiguration(
        self,
        session_id: str,
        owner_account_uuid: str,
        owner_account_id: str,
        changes: dict[str, Any],
        store: Any = None,
    ) -> list[dict[str, Any]]:
        assert self.session_factory is not None
        self.require_session(session_id, owner_account_id)
        settings_store = SettingsStore(self.crypto, self.clock)
        out: list[dict[str, Any]] = []
        with self.session_factory() as session, session.begin():
            ws = session.execute(
                text("SELECT workspace_id FROM accounts WHERE id = :a"),
                {"a": uuid.UUID(owner_account_uuid)},
            ).scalar_one()
            for key, value in changes.items():
                validate(spec_for(key), value)  # every change validated before any apply
            event_store = store(session) if callable(store) else store
            for key, value in changes.items():
                stored = settings_store.set(
                    session,
                    key,
                    value,
                    workspace_id=ws,
                    changed_by=uuid.UUID(owner_account_uuid),
                    actor_label=owner_account_id,
                    correlation_id=session_id,
                    reason="reconfiguration",
                    store=event_store,
                )
                out.append({"key": key, "version": stored.version})
        return out

    def close_reconfiguration(
        self, session_id: str, owner_account_id: str, reason: str = "completed"
    ) -> None:
        self.require_session(session_id, owner_account_id)
        self.machine.close_reconfiguration(reason)
        self._close_session_row(session_id, reason)

    def _close_session_row(self, session_id: str, reason: str) -> None:
        if self.session_factory is None:
            return
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "UPDATE setup_reconfiguration_sessions SET closed_at = :n, close_reason = :r "
                    "WHERE session_id = :s AND closed_at IS NULL"
                ),
                {"n": self.clock.now(), "r": reason, "s": session_id},
            )
            self._persist_db_state(session, SetupState.LOCKED)

    # ------------------------------------------------------------------ views
    def state_view(self) -> dict[str, Any]:
        if self.machine.state is SetupState.RECONFIGURING:
            try:
                self.machine.expire_session_if_due()
            except SetupError:
                pass  # the view reports the resulting LOCKED state
        return {
            "state": self.machine.state.value,
            "stage_ordinal": STAGE_ORDINAL[self.machine.state],
            "instance_id": self.instance_id,
            "bootstrap_open": self.machine.state
            in (
                SetupState.UNINITIALIZED,
                SetupState.PREFLIGHT_PASSED,
                SetupState.BOOTSTRAPPING,
                SetupState.BOOTSTRAP_FAILED,
            ),
            "owner_created_visible": self.order.owner_created_visible,
            "token_issued": self.guard.record is not None and not self.guard.record.used,
            "last_failure": None
            if self.machine.failure is None
            else {
                "failed_step": self.machine.failure.failed_step,
                "error_code": self.machine.failure.error_code,
                "failed_at": self.machine.failure.failed_at,
            },
            "preflight": [
                {"step": r.step, "ok": r.ok, "code": r.code} for r in self.last_preflight
            ],
            "apply_log": list(self.order.log),
            "reconfiguration": None
            if self.machine.session is None
            else {
                "session_id": self.machine.session.session_id,
                "expires_at": isoformat_utc(self.machine.session.expires_at),
            },
        }


def _json(value: Any) -> str | None:
    import json

    return None if value is None else json.dumps(value, sort_keys=True)
