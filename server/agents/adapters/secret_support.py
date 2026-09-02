"""Secret-handle support per adapter type (development plan §7B.2; V-P3-23).

Routing excludes adapters that advertise ``secret_handles: unsupported`` from Tasks that carry
secret handles; the Mattermost bot adapter is the built-in unsupported case.
"""

from __future__ import annotations

SECRET_HANDLE_SUPPORT: dict[str, bool] = {
    "mcp": True,
    "webhook": True,
    "mattermost_bot": False,
}


def supports_secret_handles(adapter_type: str) -> bool:
    """False for adapter types that advertise ``secret_handles: unsupported`` (default False)."""
    return SECRET_HANDLE_SUPPORT.get(adapter_type, False)


def declare_secret_handle_support(adapter_type: str, supported: bool) -> None:
    """Plugins declare their support when registering (V-P3-12)."""
    SECRET_HANDLE_SUPPORT[adapter_type] = supported
