"""Sidecar configuration from the environment (no config file: nothing sensitive on disk)."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SidecarError

ENV_BROKER_URL = "AGENT_COLAB_SIDECAR_BROKER_URL"
ENV_TOKEN = "AGENT_COLAB_SIDECAR_TOKEN"  # noqa: S105 - env var name  # nosec B105
ENV_CLIENT_CERT = "AGENT_COLAB_SIDECAR_CLIENT_CERT"
ENV_CLIENT_KEY = "AGENT_COLAB_SIDECAR_CLIENT_KEY"
ENV_RUNTIME_DIR = "AGENT_COLAB_SIDECAR_RUNTIME_DIR"
ENV_POLL_INTERVAL = "AGENT_COLAB_SIDECAR_POLL_INTERVAL_S"
ENV_PREFER_SSE = "AGENT_COLAB_SIDECAR_PREFER_SSE"
INSTANCE_FILE = "instance-id"


def runtime_dir(env: Mapping[str, str]) -> Path | None:
    """``AGENT_COLAB_SIDECAR_RUNTIME_DIR`` or ``$XDG_RUNTIME_DIR/agent-colab-sidecar`` (tmpfs)."""
    if env.get(ENV_RUNTIME_DIR):
        return Path(env[ENV_RUNTIME_DIR])
    if env.get("XDG_RUNTIME_DIR"):
        return Path(env["XDG_RUNTIME_DIR"]) / "agent-colab-sidecar"
    return None


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def load_instance_id(directory: Path | None) -> str:
    """Stable per-host instance id (``sc-<hex>``), kept in an owner-only file; in memory only when
    no runtime directory is configured (then it changes on every start)."""
    if directory is None:
        return "sc-" + secrets.token_hex(12)
    _ensure_private_dir(directory)
    marker = directory / INSTANCE_FILE
    if marker.exists():
        mode = stat.S_IMODE(marker.stat().st_mode)
        if mode & 0o077:
            raise SidecarError("CONFIG_INVALID", f"{marker} must be owner-only (found {mode:o})")
        value = marker.read_text(encoding="utf-8").strip()
        if value.startswith("sc-") and len(value) >= 11:
            return value
    value = "sc-" + secrets.token_hex(12)
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(marker, 0o600)
    return value


@dataclass(frozen=True)
class SidecarConfig:
    broker_url: str
    instance_id: str
    token: str | None = field(default=None, repr=False)
    client_cert: str | None = None
    client_key: str | None = field(default=None, repr=False)
    runtime_dir: Path | None = None
    poll_interval_s: float = 5.0
    prefer_sse: bool = True

    @property
    def auth_method(self) -> str:
        if self.client_cert:
            return "mtls"
        return "service_token" if self.token else "none"

    def describe(self) -> dict[str, object]:
        """Redacted view for ``status``: never the token or key material."""
        return {
            "broker_url": self.broker_url,
            "instance_id": self.instance_id,
            "auth_method": self.auth_method,
            "runtime_dir": None if self.runtime_dir is None else str(self.runtime_dir),
            "poll_interval_s": self.poll_interval_s,
            "prefer_sse": self.prefer_sse,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SidecarConfig:
        env = dict(os.environ if env is None else env)
        url = env.get(ENV_BROKER_URL, "").strip()
        if not url.startswith(("http://", "https://")):
            raise SidecarError("CONFIG_INVALID", f"{ENV_BROKER_URL} must be an http(s) URL")
        token = env.get(ENV_TOKEN) or None
        cert, key = env.get(ENV_CLIENT_CERT) or None, env.get(ENV_CLIENT_KEY) or None
        if not token and not cert:
            raise SidecarError("CONFIG_INVALID", f"{ENV_TOKEN} or {ENV_CLIENT_CERT} is required")
        if cert and not key:
            raise SidecarError("CONFIG_INVALID", f"{ENV_CLIENT_KEY} is required with a cert")
        directory = runtime_dir(env)
        try:
            interval = float(env.get(ENV_POLL_INTERVAL, "5"))
        except ValueError as exc:
            raise SidecarError("CONFIG_INVALID", f"{ENV_POLL_INTERVAL} must be a number") from exc
        interval = min(max(interval, 0.1), 5.0)  # §9.4: revocation detected within 5 s
        return cls(
            broker_url=url.rstrip("/"),
            instance_id=load_instance_id(directory),
            token=token,
            client_cert=cert,
            client_key=key,
            runtime_dir=directory,
            poll_interval_s=interval,
            prefer_sse=env.get(ENV_PREFER_SSE, "1") not in ("0", "false", "no"),
        )
