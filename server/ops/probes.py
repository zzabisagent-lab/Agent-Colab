"""Dependency probes for the operations dashboard (P4-02; development plan §11.1 Overview).

Each probe returns ``ok`` (True/False, or None when the dependency is optional and not configured)
with a short detail and latency. Results are cached in ``dependency_probes`` and considered stale
after ``STALE_S`` (60 s, V-P4-16): a dashboard read re-probes stale entries, so an injected failure
is visible within one staleness window. Probers are injectable (tests inject failures).
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock

STALE_S = 60
TIMEOUT_S = 3.0
PROBE_NAMES = ("postgres", "secret_provider", "mattermost", "storage", "telegram", "smtp")


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool | None  # None: optional dependency not configured
    detail: str
    latency_ms: int
    checked_at: dt.datetime

    @property
    def status(self) -> str:
        if self.ok is None:
            return "unconfigured"
        return "ok" if self.ok else "failed"


Prober = Callable[[Session], tuple[bool | None, str]]


def _timed(fn: Callable[[], tuple[bool | None, str]]) -> tuple[bool | None, str, int]:
    started = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # a probe never raises: failures are the signal
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
    return ok, detail, int((time.perf_counter() - started) * 1000)


def probe_postgres(session: Session) -> tuple[bool | None, str]:
    session.execute(text("SELECT 1")).scalar_one()
    return True, "reachable"


def probe_secret_provider(session: Session) -> tuple[bool | None, str]:
    name = os.environ.get("AGENT_COLAB_SECRET_PROVIDER")
    if not name:
        return None, "not configured"
    from server.secrets import provider as sp

    try:
        prov = sp.provider_for(name, {})
        health = prov.health()
        return bool(health.ok), health.detail or name
    except sp.SecretError as exc:
        return False, exc.code


def probe_mattermost(session: Session) -> tuple[bool | None, str]:
    url = os.environ.get("AGENT_COLAB_MATTERMOST_URL")
    if not url:
        return None, "not configured"
    response = httpx.get(f"{url.rstrip('/')}/api/v4/system/ping", timeout=TIMEOUT_S)
    if response.status_code != 200:
        return False, f"ping returned {response.status_code}"
    return True, "ping ok"


def probe_storage(session: Session) -> tuple[bool | None, str]:
    root = Path(os.environ.get("AGENT_COLAB_ARTIFACT_ROOT", "/var/lib/agent-colab/artifacts"))
    root.mkdir(parents=True, exist_ok=True)
    marker = root / f".probe-{uuid.uuid4().hex[:8]}"
    marker.write_bytes(b"ok")
    marker.unlink()
    return True, f"writable: {root}"


def probe_telegram(session: Session) -> tuple[bool | None, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None, "not configured"
    response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT_S)
    if response.status_code != 200:
        return False, f"getMe returned {response.status_code}"  # never the token
    return True, "getMe ok"


def probe_smtp(session: Session) -> tuple[bool | None, str]:
    host = os.environ.get("AGENT_COLAB_SMTP_HOST")
    if not host:
        return None, "not configured"
    port = int(os.environ.get("AGENT_COLAB_SMTP_PORT", "25"))
    with socket.create_connection((host, port), timeout=TIMEOUT_S):
        return True, f"tcp connect ok ({host}:{port})"


DEFAULT_PROBERS: dict[str, Prober] = {
    "postgres": probe_postgres,
    "secret_provider": probe_secret_provider,
    "mattermost": probe_mattermost,
    "storage": probe_storage,
    "telegram": probe_telegram,
    "smtp": probe_smtp,
}
_PROBERS: dict[str, Prober] = dict(DEFAULT_PROBERS)


def set_prober(name: str, prober: Prober | None) -> None:
    """Replace (or restore with None) one prober — used to inject dependency failures in tests."""
    if prober is None:
        _PROBERS[name] = DEFAULT_PROBERS[name]
    else:
        _PROBERS[name] = prober


def cached(session: Session) -> dict[str, ProbeResult]:
    rows = session.execute(
        text("SELECT name, ok, detail, latency_ms, checked_at FROM dependency_probes")
    ).all()
    return {str(r[0]): ProbeResult(str(r[0]), r[1], str(r[2]), int(r[3] or 0), r[4]) for r in rows}


def run_probes(
    session: Session,
    *,
    clock: Clock | None = None,
    refresh: bool = False,
    names: tuple[str, ...] = PROBE_NAMES,
) -> list[ProbeResult]:
    """Return current results, re-probing entries that are missing, stale or force-refreshed."""
    now = (clock or SystemClock()).now()
    known = cached(session)
    out: list[ProbeResult] = []
    for name in names:
        previous = known.get(name)
        fresh = previous is not None and (now - previous.checked_at).total_seconds() < STALE_S
        if fresh and not refresh and previous is not None:
            out.append(previous)
            continue
        prober = _PROBERS[name]

        def _run(p: Prober = prober) -> tuple[bool | None, str]:
            return p(session)

        ok, detail, latency = _timed(_run)
        result = ProbeResult(name, ok, detail, latency, now)
        session.execute(
            text(
                "INSERT INTO dependency_probes (name, ok, detail, latency_ms, checked_at) "
                "VALUES (:n, :ok, :d, :l, :t) ON CONFLICT (name) DO UPDATE SET ok = EXCLUDED.ok, "
                "detail = EXCLUDED.detail, latency_ms = EXCLUDED.latency_ms, "
                "checked_at = EXCLUDED.checked_at"
            ),
            {"n": name, "ok": ok, "d": detail, "l": latency, "t": now},
        )
        out.append(result)
    return out


def alerts(results: list[ProbeResult]) -> list[dict[str, Any]]:
    return [
        {
            "dependency": r.name,
            "severity": "critical" if r.name in ("postgres", "secret_provider") else "warning",
            "detail": r.detail,
            "since": r.checked_at.isoformat(),
        }
        for r in results
        if r.ok is False
    ]


def as_dict(r: ProbeResult) -> dict[str, Any]:
    return {
        "name": r.name,
        "status": r.status,
        "ok": r.ok,
        "detail": r.detail,
        "latency_ms": r.latency_ms,
        "checked_at": r.checked_at.isoformat(),
    }
