"""Setting registry: every setting has scope, type, validation, restart flag and secret flag.

Precedence (development plan §8.2): ``emergency env > encrypted runtime setting > setup default
> built-in default``. Emergency env overrides use ``AGENT_COLAB_EMERGENCY_<KEY>`` with dots
replaced by underscores (e.g. ``AGENT_COLAB_EMERGENCY_MATTERMOST_URL``).
"""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse


class SettingsError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SettingType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    URL = "url"
    PATH = "path"
    ENUM = "enum"
    TIMEZONE = "timezone"
    LANGUAGE = "language"
    CHANNEL_REF = "channel_ref"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    scope: str  # instance | integration | storage | secrets | scheduler | ops | notifications
    type: SettingType
    default: Any
    description: str
    secret: bool = False
    restart_required: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    validator: Callable[[Any], None] | None = None
    preflight: str | None = None  # probe group re-run on change: mattermost | storage | secrets


_LANG = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_CHANNEL = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _validate_type(spec: SettingSpec, value: Any) -> Any:
    t = spec.type
    if t is SettingType.BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise SettingsError("SETTING_TYPE_INVALID", f"{spec.key}: boolean expected")
    if t is SettingType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            else:
                raise SettingsError("SETTING_TYPE_INVALID", f"{spec.key}: integer expected")
        if spec.minimum is not None and value < spec.minimum:
            raise SettingsError("SETTING_RANGE_INVALID", f"{spec.key}: below {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise SettingsError("SETTING_RANGE_INVALID", f"{spec.key}: above {spec.maximum}")
        return value
    if not isinstance(value, str):
        raise SettingsError("SETTING_TYPE_INVALID", f"{spec.key}: string expected")
    if t is SettingType.URL:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or "@" in parsed.netloc:
            raise SettingsError(
                "SETTING_ENDPOINT_INVALID", f"{spec.key}: http(s) URL without credentials expected"
            )
        return value.rstrip("/")
    if t is SettingType.PATH:
        if not value.startswith("/") or ".." in value.split("/"):
            raise SettingsError("SETTING_PATH_INVALID", f"{spec.key}: absolute path expected")
        return value
    if t is SettingType.ENUM:
        if value not in spec.choices:
            raise SettingsError("SETTING_ENUM_INVALID", f"{spec.key}: one of {list(spec.choices)}")
        return value
    if t is SettingType.TIMEZONE:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError("SETTING_TIMEZONE_INVALID", f"{spec.key}: {value!r}") from exc
        return value
    if t is SettingType.LANGUAGE:
        if not _LANG.match(value):
            raise SettingsError(
                "SETTING_LANGUAGE_INVALID", f"{spec.key}: IANA language tag expected"
            )
        return value
    if t is SettingType.CHANNEL_REF:
        if value and not _CHANNEL.match(value):
            raise SettingsError("SETTING_CHANNEL_INVALID", f"{spec.key}: channel id expected")
        return value
    if spec.secret and not value:
        raise SettingsError("SETTING_TYPE_INVALID", f"{spec.key}: non-empty secret expected")
    return value


def validate(spec: SettingSpec, value: Any) -> Any:
    """Return the normalized value or raise a stable ``SETTING_*`` error (before any apply)."""
    normalized = _validate_type(spec, value)
    if spec.validator is not None:
        spec.validator(normalized)
    return normalized


def _spec(*args: Any, **kwargs: Any) -> SettingSpec:
    return SettingSpec(*args, **kwargs)


REGISTRY: Mapping[str, SettingSpec] = {
    s.key: s
    for s in (
        _spec(
            "instance.name", "instance", SettingType.STRING, "Agent-Colab", "Instance display name"
        ),
        _spec(
            "instance.base_url",
            "instance",
            SettingType.URL,
            "http://127.0.0.1:8080",
            "Public base URL",
            restart_required=True,
        ),
        _spec(
            "instance.default_timezone",
            "instance",
            SettingType.TIMEZONE,
            "UTC",
            "Default IANA timezone",
        ),
        _spec(
            "instance.default_language", "instance", SettingType.LANGUAGE, "en", "Default language"
        ),
        _spec(
            "mattermost.url",
            "integration",
            SettingType.URL,
            "http://127.0.0.1:8065",
            "Mattermost base URL",
            preflight="mattermost",
        ),
        _spec(
            "mattermost.team",
            "integration",
            SettingType.STRING,
            "",
            "Mattermost team name",
            preflight="mattermost",
        ),
        _spec(
            "mattermost.bot_token",
            "integration",
            SettingType.STRING,
            "",
            "Mattermost bot token",
            secret=True,
            preflight="mattermost",
        ),
        _spec(
            "storage.artifact_root",
            "storage",
            SettingType.PATH,
            "/var/lib/agent-colab/artifacts",
            "Artifact store root",
            preflight="storage",
        ),
        _spec(
            "storage.document_root",
            "storage",
            SettingType.PATH,
            "/var/lib/agent-colab/documents",
            "Document store root",
            preflight="storage",
        ),
        _spec(
            "secrets.provider",
            "secrets",
            SettingType.ENUM,
            "local",
            "Secret provider",
            choices=("local", "vault", "infisical", "sops"),
            restart_required=True,
            preflight="secrets",
        ),
        _spec(
            "secrets.master_key_path",
            "secrets",
            SettingType.PATH,
            "/var/lib/agent-colab/keys/master.key",
            "Master key file (owner-only)",
            restart_required=True,
            preflight="secrets",
        ),
        _spec(
            "ops.channel_id",
            "ops",
            SettingType.CHANNEL_REF,
            "",
            "Ops announcement channel (external channel id)",
        ),
        _spec(
            "ops.maintenance_retry_after_s",
            "ops",
            SettingType.INTEGER,
            300,
            "Retry-After seconds during maintenance",
            minimum=1,
            maximum=86400,
        ),
        # Backup retention (P7-03): a backup kept by any window is kept. The RPO of 24 h means
        # the daily window must stay at 1 or more.
        _spec(
            "backup.retention_daily",
            "ops",
            SettingType.INTEGER,
            7,
            "Daily backups to keep (newest per day)",
            minimum=1,
            maximum=365,
        ),
        _spec(
            "backup.retention_weekly",
            "ops",
            SettingType.INTEGER,
            4,
            "Weekly backups to keep (newest per ISO week)",
            minimum=0,
            maximum=260,
        ),
        _spec(
            "backup.retention_monthly",
            "ops",
            SettingType.INTEGER,
            6,
            "Monthly backups to keep (newest per month)",
            minimum=0,
            maximum=120,
        ),
        _spec(
            "scheduler.poll_interval_s",
            "scheduler",
            SettingType.INTEGER,
            5,
            "Scheduler polling interval",
            minimum=1,
            maximum=3600,
        ),
        _spec(
            "scheduler.lease_s",
            "scheduler",
            SettingType.INTEGER,
            60,
            "Scheduler lease seconds",
            minimum=5,
            maximum=86400,
        ),
        _spec(
            "scheduler.min_interval_s",
            "scheduler",
            SettingType.INTEGER,
            60,
            "Minimum schedule interval",
            minimum=1,
        ),
        _spec(
            "scheduler.missed_run_policy",
            "scheduler",
            SettingType.ENUM,
            "skip",
            "Missed-run policy",
            choices=("skip", "run_once", "run_all"),
        ),
        _spec(
            "notifications.smtp_host",
            "notifications",
            SettingType.STRING,
            "",
            "SMTP host (empty disables)",
        ),
        _spec(
            "notifications.smtp_port",
            "notifications",
            SettingType.INTEGER,
            587,
            "SMTP port",
            minimum=1,
            maximum=65535,
        ),
        _spec(
            "notifications.smtp_password",
            "notifications",
            SettingType.STRING,
            "",
            "SMTP password",
            secret=True,
        ),
    )
}


def spec_for(key: str) -> SettingSpec:
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise SettingsError("SETTING_UNKNOWN", key) from exc


def env_name(key: str) -> str:
    return "AGENT_COLAB_EMERGENCY_" + key.upper().replace(".", "_")


@dataclass(frozen=True)
class SettingView:
    key: str
    scope: str
    type: str
    secret: bool
    restart_required: bool
    layer: str  # emergency_env | runtime | setup_default | builtin
    version: int
    value: Any  # redacted for secrets
    changed_by: str | None = None
    changed_at: str | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
