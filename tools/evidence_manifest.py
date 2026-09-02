"""Build the Implementer Evidence Manifest for a phase (validation plan §6.1).

``python -m tools.evidence_manifest --phase 0 [--known-gap "..."]*`` collects the latest
``SELF-*`` attempts under ``evidence/phase-<n>/``, the phase's requirements from the traceability
matrix, changed files and migrations since ``main``, and writes ``evidence/phase-<n>/manifest.yaml``
validated against ``schemas/documents/evidence-manifest.v1.schema.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys

import yaml
from jsonschema import Draft202012Validator

from tools.baseline import ROOT, load_baseline, phase_of

IMPLEMENTER = {
    "implementer_agent_id": "agent-claude-code",
    "implementer_account_id": "account-implementer-claude",
    "implementer_credential_fingerprint": "sha256:"
    + hashlib.sha256(b"claude-code:zzabisagent-lab").hexdigest(),
    "identity_graph_version": "identity-v8-001",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, cwd=ROOT
    ).stdout


def build(phase: int, known_gaps: list[str], reproduction: list[str]) -> dict[str, object]:
    b = load_baseline()
    evidence_dir = ROOT / "evidence" / f"phase-{phase}"
    tests_run: list[dict[str, str]] = []
    evidence_refs: list[str] = []
    expected_ids = [t for t in b.test_ids() if phase_of(t) == phase]
    for tid in expected_ids:
        base = evidence_dir / f"SELF-{tid}"
        attempts = sorted(base.glob("attempt-*/result.json")) if base.exists() else []
        if not attempts:
            tests_run.append({"id": f"SELF-{tid}", "command": "", "result": "not_run"})
            continue
        latest = json.loads(attempts[-1].read_text(encoding="utf-8"))
        ref = str(attempts[-1].parent.relative_to(ROOT))
        tests_run.append(
            {
                "id": f"SELF-{tid}",
                "command": " ".join(latest["command"]),
                "result": latest["result"],
                "evidence_ref": ref,
            }
        )
        evidence_refs.append(ref)
    commit = _git("rev-parse", "HEAD").strip()
    merge_base = _git("merge-base", "origin/main", "HEAD").strip()
    changed = [f for f in _git("diff", "--name-only", merge_base, "HEAD").splitlines() if f]
    migrations = [f for f in changed if f.startswith("migrations/versions/")]
    reqs = sorted(
        r.req_id for r in b.requirements.values() if any(phase_of(p) == phase for p in r.packages)
    )
    manifest: dict[str, object] = {
        "implementation_id": f"P{phase}-IMPLEMENT-001",
        "phase": phase,
        **IMPLEMENTER,
        "commit_sha": commit,
        "image_digests": [],
        "requirements": reqs,
        "changed_files": changed,
        "migrations": migrations,
        "tests_run": tests_run,
        "known_gaps": known_gaps,
        "rollback": f"git checkout {merge_base} (merge-base with main); drop test databases",
        "evidence_refs": evidence_refs,
        "reproduction": reproduction,
    }
    schema = json.loads(
        (ROOT / "schemas" / "documents" / "evidence-manifest.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, required=True)
    ap.add_argument("--known-gap", action="append", default=[])
    ap.add_argument("--repro", action="append", default=[])
    ap.add_argument("--print", action="store_true")
    ns = ap.parse_args(argv)
    manifest = build(ns.phase, ns.known_gap, ns.repro)
    out = ROOT / "evidence" / f"phase-{ns.phase}" / "manifest.yaml"
    out.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tests_run = manifest["tests_run"]
    assert isinstance(tests_run, list)
    not_run = [t["id"] for t in tests_run if t["result"] != "pass"]
    print(f"evidence_manifest: wrote {out.relative_to(ROOT)}; non-pass self-tests: {not_run}")
    if ns.print:
        print(out.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
