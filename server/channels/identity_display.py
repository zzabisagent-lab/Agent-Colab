"""Agent identity display (P2-14; development plan §7A.4, spec §8.7).

Agent utterances are posted by the Agent-Colab bot. When the Mattermost configuration allows
overrides (probed at preflight/P2-01), posts carry ``override_username``/``override_icon_url``;
otherwise the ``[agent-name] `` prefix is used. Only the server sets display identity: any
identity fields inside an Agent result payload are stripped and audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from server.channels.mattermost.provider import ProviderInstance
from server.observability.audit import append_audit

INJECTED_KEYS = frozenset(
    {"override_username", "override_icon_url", "display_name", "username", "icon_url"}
)


@dataclass(frozen=True)
class DisplayIdentity:
    mode: str  # override | prefix
    display_name: str
    override_username: str | None = None
    override_icon_url: str | None = None
    prefix: str | None = None


def display_for_agent(
    provider: ProviderInstance, agent_display_name: str, icon_url: str | None = None
) -> DisplayIdentity:
    name = agent_display_name.strip() or "agent"
    if provider.identity_display == "override":
        return DisplayIdentity("override", name, override_username=name, override_icon_url=icon_url)
    return DisplayIdentity("prefix", name, prefix=f"[{name}] ")


def apply_display(post_payload: dict[str, Any], identity: DisplayIdentity) -> dict[str, Any]:
    """Return a new post payload with the server-decided display identity applied."""
    out = dict(post_payload)
    props = dict(out.get("props") or {})
    if identity.mode == "override":
        props["override_username"] = identity.override_username
        if identity.override_icon_url:
            props["override_icon_url"] = identity.override_icon_url
        props["from_webhook"] = "true"
        out["props"] = props
        return out
    message = str(out.get("message", ""))
    prefix = identity.prefix or ""
    if not message.startswith(prefix):
        out["message"] = prefix + message
    if props:
        out["props"] = props
    return out


def strip_injected_identity(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove identity fields an Agent put in its payload (top level and ``props``)."""
    removed: list[str] = []
    clean = dict(payload)
    for key in sorted(INJECTED_KEYS):
        if key in clean:
            clean.pop(key)
            removed.append(key)
    props = clean.get("props")
    if isinstance(props, dict):
        new_props = dict(props)
        for key in sorted(INJECTED_KEYS):
            if key in new_props:
                new_props.pop(key)
                removed.append(f"props.{key}")
        clean["props"] = new_props
    return clean, removed


def audit_injection(
    session: Session,
    *,
    workspace_id: Any,
    agent_label: str,
    subject_id: str,
    removed: list[str],
    correlation_id: str,
) -> str:
    return append_audit(
        session,
        action="agent.identity_injection_ignored",
        target_type="post",
        target_id=subject_id,
        result="IGNORED",
        actor_label=agent_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        metadata={"removed_keys": removed},
    )
