"""Occurrence keys and Run idempotency keys (spec §8.6, development plan §6.6, §10A.3).

``occurrence_key = SHA256(schedule_id | timezone | YYYY-MM-DDTHH:mm)``. The DST fold offset is
not part of the key, so both UTC instants of a duplicated local minute share one key and the Run
is materialized once. Manual and retry Runs have ``occurrence_key = NULL`` and a deterministic
idempotency key instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

_WALL_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def wall_minute(local: dt.datetime | str) -> str:
    if isinstance(local, str):
        if not _WALL_MINUTE.match(local):
            raise ValueError(f"wall-clock minute must be YYYY-MM-DDTHH:mm, got {local!r}")
        return local
    if local.tzinfo is not None:
        raise ValueError("wall-clock minute must be naive local time")
    return local.strftime("%Y-%m-%dT%H:%M")


def occurrence_key(schedule_id: str, timezone: str, local: dt.datetime | str) -> str:
    material = f"{schedule_id}|{timezone}|{wall_minute(local)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def manual_idempotency_key(schedule_id: str, requester_account_id: str, client_key: str) -> str:
    for part in (schedule_id, requester_account_id, client_key):
        if not part or ":" in part:
            raise ValueError("idempotency key parts must be non-empty and contain no ':'")
    return f"MANUAL:{schedule_id}:{requester_account_id}:{client_key}"


def retry_idempotency_key(original_run_id: str, retry_no: int) -> str:
    if not original_run_id or ":" in original_run_id or retry_no < 1:
        raise ValueError("retry key requires an original run id and retry_no >= 1")
    return f"RETRY:{original_run_id}:{retry_no}"


def scheduled_idempotency_key(schedule_id: str, key: str) -> str:
    return f"SCHEDULED:{schedule_id}:{key}"
