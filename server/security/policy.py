"""Security policy values (development plan §11.2, §8.2 layering).

Values come from the environment (``AGENT_COLAB_SECURITY_*``) with built-in defaults; the settings
package (P4-04) can install a reader with :func:`set_settings_reader` so encrypted runtime
settings take precedence over the defaults (emergency env still wins).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

SettingsReader = Callable[[str], str | None]
_reader: SettingsReader | None = None

DEFAULTS: dict[str, str] = {
    "security.session_idle_s": "1800",
    "security.session_absolute_s": str(8 * 3600),
    "security.reauth_max_age_s": "300",
    "security.mfa_members": "false",
    "security.breakglass_ttl_s": "3600",
    "security.rate_limit_failures": "6",
    "security.rate_limit_window_s": "900",
    "security.rate_limit_block_s": "900",
    "security.hsts": "auto",
}
MFA_REQUIRED_ROLES = frozenset({"role-system-owner", "role-administrator"})


def set_settings_reader(reader: SettingsReader | None) -> None:
    global _reader
    _reader = reader


def value(key: str) -> str:
    env_key = "AGENT_COLAB_" + key.upper().replace(".", "_")
    env = os.environ.get(env_key)
    if env is not None and env != "":
        return env  # emergency env
    if _reader is not None:
        stored = _reader(key)
        if stored is not None:
            return stored
    return DEFAULTS[key]


def int_value(key: str) -> int:
    return int(value(key))


def bool_value(key: str) -> bool:
    return value(key).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SecurityPolicy:
    session_idle_s: int
    session_absolute_s: int
    reauth_max_age_s: int
    mfa_members: bool
    breakglass_ttl_s: int
    rate_limit_failures: int
    rate_limit_window_s: int
    rate_limit_block_s: int


def current_policy() -> SecurityPolicy:
    return SecurityPolicy(
        session_idle_s=int_value("security.session_idle_s"),
        session_absolute_s=int_value("security.session_absolute_s"),
        reauth_max_age_s=int_value("security.reauth_max_age_s"),
        mfa_members=bool_value("security.mfa_members"),
        breakglass_ttl_s=int_value("security.breakglass_ttl_s"),
        rate_limit_failures=int_value("security.rate_limit_failures"),
        rate_limit_window_s=int_value("security.rate_limit_window_s"),
        rate_limit_block_s=int_value("security.rate_limit_block_s"),
    )
