"""Self-test evidence recorder.

``python -m tools.evidence run V-P0-10 -- <command...>`` runs the command and stores an immutable
record under ``evidence/phase-<n>/SELF-<TEST-ID>/`` with the command, exit code, captured output,
git commit, and timestamp. Records are never edited; a re-run creates a new numbered attempt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

from tools.baseline import ROOT, phase_of


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, cwd=ROOT
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def record(test_id: str, command: list[str], env: dict[str, str] | None = None) -> int:
    phase = phase_of(test_id)
    base = ROOT / "evidence" / f"phase-{phase}" / f"SELF-{test_id}"
    base.mkdir(parents=True, exist_ok=True)
    attempt = 1 + len([p for p in base.iterdir() if p.is_dir() and p.name.startswith("attempt-")])
    out_dir = base / f"attempt-{attempt:02d}"
    out_dir.mkdir()
    started = dt.datetime.now(dt.UTC)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        check=False,
    )
    finished = dt.datetime.now(dt.UTC)
    output = proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
    (out_dir / "output.log").write_text(output, encoding="utf-8")
    status = "pass" if proc.returncode == 0 else "fail"
    result = {
        "test_id": test_id,
        "evidence_id": f"SELF-{test_id}",
        "attempt": attempt,
        "command": command,
        "exit_code": proc.returncode,
        "result": status,
        "commit_sha": _git_sha(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    (out_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"[{result['result'].upper()}] {test_id} attempt {attempt} -> {out_dir.relative_to(ROOT)}"
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("test_id")
    run.add_argument("command", nargs=argparse.REMAINDER)
    ns = ap.parse_args(argv)
    command = [c for c in ns.command if c != "--"]
    if not command:
        ap.error("command required after --")
    return record(ns.test_id, command)


if __name__ == "__main__":
    sys.exit(main())
