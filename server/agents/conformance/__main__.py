"""CLI: ``python -m server.agents.conformance --adapter mcp --endpoint '{"agent_id": ...}'``."""

from __future__ import annotations

import argparse
import json
import sys

from server.agents.conformance.harness import harness_for
from server.agents.conformance.report import validate_report
from server.agents.conformance.suite import run_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.agents.conformance")
    parser.add_argument(
        "--adapter", required=True, help="adapter type (mcp|webhook|mattermost_bot|plugin)"
    )
    parser.add_argument(
        "--endpoint", default="{}", help="endpoint config as JSON (no secret values)"
    )
    parser.add_argument("--out", help="write the JSON report to this file")
    args = parser.parse_args(argv)
    import server.agents.adapters  # noqa: F401 - built-in adapter types register on import

    endpoint = json.loads(args.endpoint)
    endpoint.setdefault("agent_id", f"agent-conformance-{args.adapter}")
    endpoint.setdefault("capabilities", ["cap_echo"])
    report = run_suite(harness_for(args.adapter, endpoint))
    doc = report.to_dict()
    validate_report(doc)
    text = json.dumps(doc, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if report.result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
