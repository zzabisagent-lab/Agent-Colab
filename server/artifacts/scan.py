"""Artifact malware scanning (P6-03; spec §9.1, §15.5).

``Scanner`` is the seam declared by :mod:`server.artifacts.storage`. Two implementations ship:

* :class:`ClamdScanner` speaks the ClamAV daemon protocol over a Unix socket (``INSTREAM``), used
  when ``AGENT_COLAB_CLAMAV_SOCKET`` points at a running ``clamd``. It is written against the wire
  protocol directly so no runtime dependency is added.
* :class:`SignatureScanner` is a pure-Python fallback that matches known-bad byte signatures
  (including the EICAR test string). It is what runs in environments without ClamAV, and what the
  tests use, so the quarantine path is exercised identically either way.

A scanner never raises to its caller: an unreachable daemon yields ``verdict="error"`` and the
artifact is quarantined with ``ARTIFACT_SCAN_UNAVAILABLE`` rather than being trusted.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from server.artifacts.storage import ScanResult

CHUNK = 64 * 1024
DEFAULT_TIMEOUT_S = 30.0
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
# Signature name -> byte marker. Names are reported, never the surrounding file bytes.
SIGNATURES: dict[str, bytes] = {
    "EICAR-Test-File": EICAR,
    "Colab-Test-Malware": b"COLAB-MALWARE-FIXTURE-DO-NOT-EXECUTE",
    "Suspicious-ELF-Dropper": b"\x7fELF" + b"\x02\x01\x01\x00" + b"dropper",
}


@dataclass(frozen=True)
class ScanReport:
    """Provenance of one scan, persisted in ``artifact_scan_results``."""

    scanner: str
    verdict: str  # clean | infected | error
    reason_code: str | None = None
    detail: str | None = None

    @property
    def clean(self) -> bool:
        return self.verdict == "clean"

    def as_scan_result(self) -> ScanResult:
        return ScanResult(clean=self.clean, reason_code=self.reason_code)


class SignatureScanner:
    """Streaming signature match; the default when no ClamAV daemon is configured."""

    name = "signature"

    def __init__(self, signatures: dict[str, bytes] | None = None) -> None:
        self._signatures = dict(signatures or SIGNATURES)
        self._overlap = max((len(v) for v in self._signatures.values()), default=1) - 1

    def report(self, path: Path) -> ScanReport:
        try:
            with path.open("rb") as fh:
                tail = b""
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    window = tail + chunk
                    for name, marker in self._signatures.items():
                        if marker in window:
                            return ScanReport(self.name, "infected", "ARTIFACT_MALWARE", name)
                    tail = window[-self._overlap :] if self._overlap else b""
        except OSError as exc:
            return ScanReport(self.name, "error", "ARTIFACT_SCAN_UNAVAILABLE", type(exc).__name__)
        return ScanReport(self.name, "clean")

    def scan(self, path: Path) -> ScanResult:
        return self.report(path).as_scan_result()


class ClamdScanner:
    """ClamAV daemon over a Unix socket using the ``INSTREAM`` command.

    The protocol is: send ``zINSTREAM\\0``, then length-prefixed chunks, then a zero length; the
    daemon answers ``stream: OK`` or ``stream: <signature> FOUND``.
    """

    name = "clamav"

    def __init__(self, socket_path: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    def report(self, path: Path) -> ScanReport:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout_s)
                sock.connect(self.socket_path)
                sock.sendall(b"zINSTREAM\0")
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
                sock.sendall((0).to_bytes(4, "big"))
                answer = sock.recv(4096).decode("utf-8", "replace").strip("\0 \n")
        except OSError as exc:  # socket.timeout is an OSError subclass
            return ScanReport(self.name, "error", "ARTIFACT_SCAN_UNAVAILABLE", type(exc).__name__)
        if answer.endswith("OK"):
            return ScanReport(self.name, "clean")
        if answer.endswith("FOUND"):
            signature = answer.split(":", 1)[-1].strip().removesuffix("FOUND").strip()
            return ScanReport(self.name, "infected", "ARTIFACT_MALWARE", signature)
        return ScanReport(self.name, "error", "ARTIFACT_SCAN_UNAVAILABLE", answer[:200])

    def scan(self, path: Path) -> ScanResult:
        return self.report(path).as_scan_result()


def default_scanner() -> SignatureScanner | ClamdScanner:
    """ClamAV when ``AGENT_COLAB_CLAMAV_SOCKET`` is set and present, else signature matching."""
    sock_path = os.environ.get("AGENT_COLAB_CLAMAV_SOCKET", "").strip()
    if sock_path and Path(sock_path).exists():
        return ClamdScanner(sock_path)
    return SignatureScanner()


def report_for(scanner: object, path: Path) -> ScanReport:
    """Scan provenance for any ``Scanner``; plain scanners are wrapped in a report."""
    reporter = getattr(scanner, "report", None)
    if callable(reporter):
        result = reporter(path)
        if isinstance(result, ScanReport):
            return result
    outcome: ScanResult = scanner.scan(path)  # type: ignore[attr-defined]
    name = str(getattr(scanner, "name", type(scanner).__name__))
    if outcome.clean:
        return ScanReport(name, "clean")
    return ScanReport(name, "infected", outcome.reason_code or "ARTIFACT_MALWARE")
