"""Integration preflight probes shared by Setup and Settings (development plan §8.3; V-P4-30).

Probes never persist anything and never echo secrets: results carry a stable code, a redacted
detail and recovery guidance. Probe factories are injectable so tests use fakes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GUIDANCE = {
    "mattermost": "Check the Mattermost base URL, that the bot token belongs to a bot account with "
    "the team, and that the server can reach Mattermost over the network.",
    "storage": "Create the directory (or its parent) and grant the service user write permission; "
    "paths must be absolute.",
    "secrets": "Check the secret provider configuration; the local provider needs a readable, "
    "owner-only master key file or key material entered in this session.",
    "db": "Check host/port/database/user and the password entered in this session; the database "
    "user must be able to create tables (migrations).",
}


@dataclass(frozen=True)
class ProbeResult:
    step: str
    ok: bool
    code: str
    detail: str = ""  # redacted; never a secret or a DSN with credentials
    guidance: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


MattermostProbe = Callable[[str, str], dict[str, Any]]  # (url, token) -> /users/me payload


def _default_mattermost_probe(url: str, token: str) -> dict[str, Any]:
    from server.channels.mattermost.client import HttpMattermostClient

    return HttpMattermostClient(url, token).me()


def probe_mattermost(
    url: str, token: str, team: str = "", *, probe: MattermostProbe | None = None
) -> ProbeResult:
    if not url or not token:
        return ProbeResult(
            "mattermost",
            False,
            "PREFLIGHT_MATTERMOST_INCOMPLETE",
            "url and bot token required",
            GUIDANCE["mattermost"],
        )
    try:
        me = (probe or _default_mattermost_probe)(url, token)
    except Exception as exc:  # network/auth failures are reported by type only
        name = type(exc).__name__
        code = (
            "PREFLIGHT_MATTERMOST_AUTH"
            if "401" in str(exc) or "403" in str(exc) or "Auth" in name
            else "PREFLIGHT_MATTERMOST_UNREACHABLE"
        )
        return ProbeResult("mattermost", False, code, name, GUIDANCE["mattermost"])
    if not me.get("is_bot", False) and not me.get("id"):
        return ProbeResult(
            "mattermost",
            False,
            "PREFLIGHT_MATTERMOST_PERMISSION",
            "token is not a bot account",
            GUIDANCE["mattermost"],
        )
    return ProbeResult(
        "mattermost",
        True,
        "OK",
        "",
        "",
        {"bot_user_id": me.get("id"), "username": me.get("username")},
    )


def probe_storage(paths: Mapping[str, str]) -> ProbeResult:
    for name, raw in paths.items():
        path = Path(raw)
        if not path.is_absolute():
            return ProbeResult(
                "storage", False, "PREFLIGHT_STORAGE_PATH_INVALID", name, GUIDANCE["storage"]
            )
        target = path if path.exists() else path.parent
        if not target.exists() or not os.access(target, os.W_OK):
            return ProbeResult(
                "storage", False, "PREFLIGHT_STORAGE_NOT_WRITABLE", name, GUIDANCE["storage"]
            )
    return ProbeResult("storage", True, "OK")


def probe_secret_provider(name: str, config: Mapping[str, Any]) -> ProbeResult:
    from server.secrets.provider import SecretError, provider_for

    try:
        health = provider_for(name, config).health()
    except SecretError as exc:
        return ProbeResult(
            "secrets", False, "PREFLIGHT_SECRET_PROVIDER_UNAVAILABLE", exc.code, GUIDANCE["secrets"]
        )
    except Exception as exc:
        return ProbeResult(
            "secrets",
            False,
            "PREFLIGHT_SECRET_PROVIDER_UNAVAILABLE",
            type(exc).__name__,
            GUIDANCE["secrets"],
        )
    if not health.ok:
        return ProbeResult(
            "secrets",
            False,
            "PREFLIGHT_SECRET_PROVIDER_UNHEALTHY",
            health.detail,
            GUIDANCE["secrets"],
        )
    return ProbeResult("secrets", True, "OK", "", "", {"provider": health.provider})


def as_dict(result: ProbeResult) -> dict[str, Any]:
    return {
        "step": result.step,
        "ok": result.ok,
        "code": result.code,
        "detail": result.detail,
        "guidance": result.guidance,
        **result.extra,
    }
