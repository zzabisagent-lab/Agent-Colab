"""Built-in adapter types register themselves on import (development plan §7.3, §7B.2).

Each module calls :func:`server.agents.adapters.contract.register_adapter_type`; a missing module
(a package still under construction) must not break the others, hence the guarded imports.
"""

from __future__ import annotations

import contextlib
import importlib

for _module in ("mcp_client", "webhook", "mattermost_bot"):
    with contextlib.suppress(ImportError):
        importlib.import_module(f"{__name__}.{_module}")
