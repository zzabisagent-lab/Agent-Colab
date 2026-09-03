"""Restore gate: the service stays closed until tombstone reconciliation (P7-03, V-P7-20).

A restore is two operations — load the data, then reconcile the key-tombstone ledger — and the
instance must not serve between them, or a destroyed DEK could be resolved from restored rows.
``tools/restore.py`` writes a marker beside the sealed bootstrap state before it loads anything
and removes it only after reconciliation reports no unknown entries; readiness fails while the
marker exists, so an operator who restores and walks away never opens a half-restored instance.

The marker holds no secret: the backup id, when the restore started and what is still outstanding.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

MARKER_NAME = "restore-pending.json"
MARKER_ENV = "AGENT_COLAB_RESTORE_MARKER"
DEFAULT_STATE_DIR = Path("/var/lib/agent-colab/bootstrap")


def marker_path(bootstrap_state_path: str | os.PathLike[str] | None = None) -> Path:
    """``AGENT_COLAB_RESTORE_MARKER`` wins, else the marker sits beside the bootstrap state."""
    override = os.environ.get(MARKER_ENV)
    if override:
        return Path(override)
    if bootstrap_state_path:
        return Path(bootstrap_state_path).parent / MARKER_NAME
    return DEFAULT_STATE_DIR / MARKER_NAME


def mark_pending(path: Path, *, backup_id: str, reason: str, now: dt.datetime) -> Path:
    """Close the gate. Owner-only, because it names the backup being restored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "backup_id": backup_id,
        "reason": reason,
        "started_at": now.astimezone(dt.UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def pending(path: Path) -> dict[str, Any] | None:
    """The open gate's marker, or None when the instance may serve. A corrupt marker still
    closes the gate: an unreadable restore state is never read as 'finished'."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"backup_id": "", "reason": "MARKER_UNREADABLE", "started_at": ""}
    return dict(data) if isinstance(data, dict) else {"reason": "MARKER_INVALID"}


def clear_pending(path: Path) -> bool:
    """Open the gate; returns False when there was nothing to clear."""
    if not path.exists():
        return False
    path.unlink()
    return True
