"""Sealed pre-DB bootstrap store (spec §12, development plan §8.1) — P0-09.

The local file records only setup state, the setup-token hash/fingerprint, failure counters,
and non-secret configuration pointers. It is owner-only (dir 0700, file 0600), written
atomically, schema-validated, scanned for secret-looking content, and never allows the stage
ordinal to go backwards.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from server.domain.clock import Clock, isoformat_utc
from server.setup.errors import SetupError
from server.setup.state import STAGE_ORDINAL, SetupState

DEFAULT_PATH = Path("/var/lib/agent-colab/bootstrap/state.json")
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "api"
    / "setup"
    / "bootstrap-state.v1.schema.json"
)

# Keys that may never appear (case-insensitive substring match), except the explicit allowlist.
_DENIED_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|private|api_key|apikey|client_secret|master_key_value|"
    r"totp|recovery_code|dsn|connection_string|credential|bearer|session_cookie)",
    re.I,
)
_ALLOWED_KEYS = frozenset({"token_hash", "token_fingerprint", "secret_provider"})
_DSN_WITH_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----")
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _shannon_entropy(text: str) -> float:
    counts = Counter(text)
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def looks_like_secret(value: str) -> bool:
    """Heuristic used by the denylist; hex-64 hashes are excluded by the caller."""
    if _DSN_WITH_CREDENTIALS.search(value) or _PEM_BLOCK.search(value):
        return True
    if value.startswith("/") and "=" not in value:
        return False  # an absolute filesystem pointer (allowed config pointer), not key material
    return bool(_BASE64ISH.match(value)) and _shannon_entropy(value) >= 4.0


def scan_for_secrets(document: Any, path: str = "$") -> list[str]:
    """Return JSON paths that carry denied keys or secret-looking values (never the values)."""
    findings: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            here = f"{path}.{key}"
            if key not in _ALLOWED_KEYS and _DENIED_KEY_PATTERN.search(key):
                findings.append(f"{here}: denied key")
                continue
            if key == "token_hash" and isinstance(value, str) and _HEX64.match(value):
                continue
            findings.extend(scan_for_secrets(value, here))
    elif isinstance(document, list):
        for i, item in enumerate(document):
            findings.extend(scan_for_secrets(item, f"{path}[{i}]"))
    elif isinstance(document, str) and looks_like_secret(document):
        findings.append(f"{path}: secret-looking value")
    return findings


class BootstrapStore:
    """Owner-only, schema-validated, atomically written, stage-monotonic local state file."""

    def __init__(self, path: Path = DEFAULT_PATH, clock: Clock | None = None) -> None:
        self.path = path
        self._clock = clock
        self._validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))

    # ----- documents -------------------------------------------------------------------
    def initial_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state": SetupState.UNINITIALIZED.value,
            "stage_ordinal": 0,
            "token_hash": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_fingerprint": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_expires_at": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_used": False,  # nosec B105 - hash/flag placeholder, never a secret value
            "config_pointers": {},
            "failure_counters": {},
            "last_failure": None,
            "lock_marker": False,
            "recovery_metadata": {},
            "updated_at": self._now(),
        }

    def lock_marker_document(self, recovery_metadata: dict[str, str]) -> dict[str, Any]:
        """After DB migration only the LOCKED marker and minimal recovery metadata remain."""
        return {
            "schema_version": 1,
            "state": SetupState.LOCKED.value,
            "stage_ordinal": STAGE_ORDINAL[SetupState.LOCKED],
            "token_hash": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_fingerprint": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_expires_at": None,  # nosec B105 - hash/flag placeholder, never a secret value
            "token_used": True,  # nosec B105 - hash/flag placeholder, never a secret value
            "config_pointers": {},
            "failure_counters": {},
            "last_failure": None,
            "lock_marker": True,
            "recovery_metadata": {"setup_state_location": "db", **recovery_metadata},
            "updated_at": self._now(),
        }

    # ----- validation ------------------------------------------------------------------
    def validate(self, document: dict[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(document), key=lambda e: list(e.path))
        if errors:
            where = "/".join(str(p) for p in errors[0].path) or "<root>"
            raise SetupError("BOOTSTRAP_STORE_SCHEMA_INVALID", f"{where}: {errors[0].message}")
        findings = scan_for_secrets(document)
        if findings:
            raise SetupError("BOOTSTRAP_STORE_SECRET_REJECTED", "; ".join(findings))
        expected = STAGE_ORDINAL[SetupState(document["state"])]
        if document["stage_ordinal"] != expected:
            raise SetupError(
                "BOOTSTRAP_STORE_SCHEMA_INVALID",
                f"stage_ordinal {document['stage_ordinal']} != {expected} for {document['state']}",
            )
        if document["lock_marker"] and document["state"] != SetupState.LOCKED.value:
            raise SetupError("BOOTSTRAP_STORE_SCHEMA_INVALID", "lock_marker requires LOCKED")

    # ----- filesystem ------------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            raise SetupError("BOOTSTRAP_STORE_MISSING", str(self.path))
        self._check_permissions()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SetupError("BOOTSTRAP_STORE_CORRUPT", type(exc).__name__) from exc
        if not isinstance(document, dict):
            raise SetupError("BOOTSTRAP_STORE_CORRUPT", "not an object")
        self.validate(document)
        return document

    def write(self, document: dict[str, Any], *, allow_retry_regression: bool = False) -> None:
        document = {**document, "updated_at": self._now()}
        self.validate(document)
        if self.path.exists():
            current = self.read()
            if document["stage_ordinal"] < current["stage_ordinal"]:
                retry = (
                    allow_retry_regression
                    and current["state"] == SetupState.BOOTSTRAP_FAILED.value
                    and document["state"] == SetupState.PREFLIGHT_PASSED.value
                )
                if not retry:
                    raise SetupError(
                        "BOOTSTRAP_STORE_STAGE_REGRESSION",
                        f"{current['state']} -> {document['state']} lowers the setup stage",
                    )
        self._atomic_write(document)

    def _atomic_write(self, document: dict[str, Any]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(document, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        os.chmod(self.path, 0o600)

    def _check_permissions(self) -> None:
        for target, allowed in ((self.path.parent, 0o700), (self.path, 0o600)):
            st = os.stat(target)
            if st.st_uid != os.geteuid():
                raise SetupError("BOOTSTRAP_STORE_PERMISSIONS_INVALID", f"{target} not owned")
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise SetupError(
                    "BOOTSTRAP_STORE_PERMISSIONS_INVALID",
                    f"{target} mode {oct(stat.S_IMODE(st.st_mode))} exceeds {oct(allowed)}",
                )

    def _now(self) -> str:
        if self._clock is None:
            raise SetupError("SETUP_CLOCK_REQUIRED", "BootstrapStore needs a Clock to write")
        return isoformat_utc(self._clock.now())
