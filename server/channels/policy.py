"""Telegram command policy (development plan §7A.6, spec §10.2, REQ-BRDG-004).

Telegram is an auxiliary channel: commands are **read/reply only by default**. A Bridge opens
command execution per Mattermost channel with ``allow_commands``; even then only the restricted
§7A.6 grammar (``task show|list``, ``approve show``, ``doc show``) is accepted unless the Bridge
``content_policy.telegram_commands`` opens further verbs. Permission checks stay with the Policy
Engine: this module only decides whether a verb may *reach* the command bus from Telegram.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

TELEGRAM_COMMANDS_DISABLED = "TELEGRAM_COMMANDS_DISABLED"
TELEGRAM_VERB_NOT_ALLOWED = "TELEGRAM_VERB_NOT_ALLOWED"
OK = "OK"

#: Verbs every command-enabled Bridge accepts (development plan §7A.6).
DEFAULT_ALLOWED_VERBS: frozenset[str] = frozenset(
    {"task.show", "task.list", "approve.show", "doc.show"}
)
#: Verbs that never execute from Telegram, whatever the Bridge policy says (§7A.6 keeps the
#: `link` challenge on the primary channel and Phase-later resources are gated by the Router).
NEVER_ALLOWED_RESOURCES: frozenset[str] = frozenset({"link"})
#: Read-only notice cadence (spec §10.2: read/reply only → the user is told at most hourly).
NOTICE_INTERVAL = dt.timedelta(hours=1)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str = OK

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass(frozen=True)
class TelegramCommandPolicy:
    """Effective command policy of one Bridge.

    ``allowed_verbs`` holds ``"<resource>.<verb>"`` keys; the §7A.6 defaults are always part of
    the set of a command-enabled Bridge, ``content_policy.telegram_commands`` only *adds* verbs.
    """

    allow_commands: bool = False
    allowed_verbs: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_VERBS)

    @classmethod
    def from_bridge(
        cls, allow_commands: bool, content_policy: Mapping[str, Any] | None = None
    ) -> TelegramCommandPolicy:
        """Build the policy from the ``telegram_bridges`` row columns."""
        extra = _extra_verbs((content_policy or {}).get("telegram_commands"))
        return cls(bool(allow_commands), DEFAULT_ALLOWED_VERBS | extra)

    def verb_key(self, resource: str, verb: str) -> str:
        return f"{resource}.{verb}"


def _extra_verbs(section: Any) -> frozenset[str]:
    """``telegram_commands`` may be a list of verb keys or ``{"allowed_verbs": [...]}``."""
    if section is None:
        return frozenset()
    raw: Iterable[Any]
    if isinstance(section, Mapping):
        raw = section.get("allowed_verbs") or section.get("verbs") or ()
    elif isinstance(section, (list, tuple, set, frozenset)):
        raw = section
    else:
        return frozenset()
    out: set[str] = set()
    for item in raw:
        key = str(item).strip().lower().replace(" ", ".").replace("_", "-")
        if not key or "." not in key:
            continue
        resource, _, verb = key.partition(".")
        if resource in NEVER_ALLOWED_RESOURCES:
            continue
        out.add(f"{resource}.{verb}")
    return frozenset(out)


def evaluate(policy: TelegramCommandPolicy, resource: str, verb: str) -> PolicyDecision:
    """Whether ``<resource> <verb>`` may be executed from Telegram under ``policy``.

    Order: commands disabled → verb outside the allowed set → allowed. Explicit permission
    (Policy Engine) is checked afterwards by the command bus, never here.
    """
    if not policy.allow_commands:
        return PolicyDecision(False, TELEGRAM_COMMANDS_DISABLED)
    key = policy.verb_key(resource, verb)
    if resource in NEVER_ALLOWED_RESOURCES or key not in policy.allowed_verbs:
        return PolicyDecision(False, TELEGRAM_VERB_NOT_ALLOWED)
    return PolicyDecision(True, OK)


def notice_bucket(now: dt.datetime, interval: dt.timedelta = NOTICE_INTERVAL) -> int:
    """Stable bucket index of ``now``; one notice per user per bucket (dedupe key component)."""
    return int(now.timestamp() // interval.total_seconds())


def notice_dedupe_key(
    provider_instance_id: str, chat_id: str, user_id: str, now: dt.datetime
) -> str:
    """Outbox dedupe key for the read-only notice: at most once per user per hour per chat."""
    return f"tg-notice:{provider_instance_id}:{chat_id}:{user_id}:{notice_bucket(now)}"
