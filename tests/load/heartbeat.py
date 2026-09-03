"""Agent heartbeats for a soak run (P7-04).

The soak criterion watches heartbeat behaviour, and heartbeats are not a side effect of API
traffic: something has to send them. This process beats for every seeded Agent through the real
``POST /api/v1/agents/{id}/heartbeat`` endpoint at the interval the product's own liveness rule
assumes, so a stalled sweep, a heartbeat that stops being recorded, or an Agent drifting offline
shows up in the samples as staleness rather than having to be inferred.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

from tests.load.samples import HEARTBEAT_INTERVAL_S

#: A §7C usage block (schemas/adapters/usage.v1.schema.json). Every heartbeat carries one, so the
#: soak exercises usage costing and the usage_records it writes, not just the liveness update.
USAGE = {
    "model": "generic-small",
    "input_tokens": 120,
    "output_tokens": 40,
    "tool_calls": 1,
    "wall_time_ms": 250,
}


def beat(
    base: str,
    token: str,
    agent_ids: list[str],
    seconds: float,
    interval_s: float = HEARTBEAT_INTERVAL_S,
    progress: Path | None = None,
) -> dict[str, int]:
    sent = failed = 0
    deadline = time.monotonic() + seconds
    with httpx.Client(base_url=base, timeout=30.0) as client:
        next_at = time.monotonic()
        while time.monotonic() < deadline:
            for agent_id in agent_ids:
                try:
                    response = client.post(
                        f"/api/v1/agents/{agent_id}/heartbeat",
                        json={"health": "ok", "capacity": 50, "usage": USAGE},
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Idempotency-Key": f"hb-{uuid.uuid4().hex}",
                        },
                    )
                    ok = response.status_code < 400
                except httpx.HTTPError:  # a transient transport failure is counted, not fatal
                    ok = False
                sent += int(ok)
                failed += int(not ok)
            if progress is not None:
                tmp = progress.with_suffix(".tmp")
                try:
                    tmp.write_text(json.dumps({"sent": sent, "failed": failed}), encoding="utf-8")
                    tmp.replace(progress)
                except OSError:  # pragma: no cover - advisory only
                    pass
            next_at += interval_s
            sleep = next_at - time.monotonic()
            if sleep > 0:
                time.sleep(min(sleep, max(0.0, deadline - time.monotonic())))
            else:  # fell behind: resynchronise rather than burst
                next_at = time.monotonic()
    return {"sent": sent, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.load.heartbeat")
    parser.add_argument("--base", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--agents", required=True, help="comma-separated agent ids")
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--interval", type=float, default=HEARTBEAT_INTERVAL_S)
    parser.add_argument("--progress", type=Path, default=None)
    args = parser.parse_args(argv)
    result = beat(
        args.base,
        args.token,
        [a for a in args.agents.split(",") if a],
        args.seconds,
        args.interval,
        args.progress,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
