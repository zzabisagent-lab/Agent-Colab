"""Inbox change listeners (development plan §7B.3 ``colab://inbox/{agent_id}`` subscriptions).

``inbox`` calls :func:`inbox_changed` whenever an Agent's inbox gains a deliverable item. The MCP
transport registers a listener that turns this into ``notifications/resources/updated`` for the
sessions subscribed to that inbox. Listeners must never raise into the caller and must not block:
the call happens inside the command transaction, so a subscriber that reads immediately may still
see the previous state and simply polls again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)
_LISTENERS: list[Callable[[str], None]] = []


def register_listener(listener: Callable[[str], None]) -> Callable[[], None]:
    _LISTENERS.append(listener)

    def unregister() -> None:
        if listener in _LISTENERS:
            _LISTENERS.remove(listener)

    return unregister


def inbox_changed(agent_id: str) -> None:
    for listener in list(_LISTENERS):
        try:
            listener(agent_id)
        except Exception:  # a listener must never break the command path
            log.exception("inbox listener failed for %s", agent_id)
