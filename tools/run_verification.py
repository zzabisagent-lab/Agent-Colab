"""Run an independent Codex verification for a phase (validation plan §7.5, ADR-0005).

Creates a read-only git worktree at the target commit, a disposable verifier database, renders the
prompt from ``tools/verify/PROMPT-TEMPLATE.md`` with the §4.2 inputs, runs ``codex exec`` as a
separate non-interactive process with a fresh context, then copies the report **unmodified** into
``verification/phase-<n>/`` with its SHA-256 and the raw run log. The implementer never edits the
report; ``--revision`` creates a new revision, never an overwrite.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from tools.baseline import ROOT, load_baseline, phase_of

IMPLEMENTER_FP = "sha256:" + hashlib.sha256(b"claude-code:zzabisagent-lab").hexdigest()
VERIFIER_FP = "sha256:" + hashlib.sha256(b"codex:chatgpt-login").hexdigest()
WORKTREES = Path(os.environ.get("AGENT_COLAB_VERIFY_ROOT", str(Path.home() / ".local" / "verify")))


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, cwd=cwd
    ).stdout


def _policy_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "policy").glob("*.yaml")):
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


def _env_fingerprint() -> str:
    parts = [
        subprocess.run(
            ["uname", "-srm"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
    ]
    return " | ".join(p for p in parts if p)


def _worktree_modifications(worktree: Path, phase: int) -> list[str]:
    """Files changed/added by the verifier outside its verification directory (must be empty)."""
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        cwd=worktree,
        check=False,
    ).stdout
    allowed = (f"verification/phase-{phase}/", f"evidence/phase-{phase}/manifest.yaml", ".venv/")
    return [line[3:] for line in out.splitlines() if line[3:] and not line[3:].startswith(allowed)]


def prepare_worktree(commit: str, phase: int, revision: int) -> Path:
    WORKTREES.mkdir(parents=True, exist_ok=True)
    path = WORKTREES / f"phase-{phase}-r{revision:03d}"
    if path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=ROOT, check=False)
        shutil.rmtree(path, ignore_errors=True)
    _git("worktree", "add", "--detach", str(path), commit)
    subprocess.run(
        ["uv", "sync", "--all-extras", "--frozen"], cwd=path, check=True, capture_output=True
    )
    return path


def render_prompt(
    phase: int,
    commit: str,
    revision: int,
    report_path: str,
    evidence_dir: str,
    db_url: str,
    extra_env: str,
) -> str:
    b = load_baseline()
    last = max(int(t.split("-")[-1]) for t in b.test_ids() if phase_of(t) == phase)
    manifest = yaml.safe_load(
        (ROOT / "evidence" / f"phase-{phase}" / "manifest.yaml").read_text(encoding="utf-8")
    )
    repro = manifest.get("reproduction") or [
        "make bootstrap",
        "make lint",
        "make test",
        "make check-docs",
    ]
    template = (ROOT / "tools" / "verify" / "PROMPT-TEMPLATE.md").read_text(encoding="utf-8")
    environment = (
        f"- Working root: this worktree (git commit {commit}). `uv` environment synced; run "
        f"commands with `uv run ...` or `make ...` (PATH includes ~/.local/bin).\n"
        f"- Disposable PostgreSQL 16 for your own tests: "
        f"`AGENT_COLAB_TEST_DATABASE_URL={db_url}` (exported; tests create and drop their own "
        f"databases). psql: `pg16 psql -d colab_verify`.\n"
        f"- Docker Engine 29 + Compose v2 are installed; this user is in the docker group but "
        f"non-login shells need `sg docker -c '<command>'` (or `newgrp docker`). Compose must be "
        f"run with `--env-file deploy/dev/compose.env` (`make compose-up` / `make compose-down`); "
        f"the repository `.env` is a deployment-secrets file and must never be read or printed. "
        f"No root. Telegram: `.env` holds TELEGRAM_BOT_TOKEN and two forum-enabled test chats "
        f"(TELEGRAM_TEST_CHAT_A/B) for read-only re-checks; never print their values.\n"
        f"- Tools: gitleaks, jq, rg, uv, pnpm, node 22, python 3.12.\n{extra_env}"
    )
    return template.format(
        phase=phase,
        commit=commit,
        last=f"{last:02d}",
        environment=environment,
        reproduction="\n".join(f"{i}. `{s}`" for i, s in enumerate(repro, 1)),
        report_path=report_path,
        verification_id=f"VR-P{phase}-{revision:03d}",
        implementer_fp=IMPLEMENTER_FP,
        verifier_fp=VERIFIER_FP,
        policy_hash=_policy_hash(),
        evidence_dir=evidence_dir,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, required=True)
    ap.add_argument("--commit", default=None)
    ap.add_argument("--revision", type=int, required=True)
    ap.add_argument("--timeout", type=int, default=3 * 3600)
    ap.add_argument("--db-url", default="postgresql://colab@127.0.0.1:54329/colab_verify")
    ap.add_argument("--extra-env", default="")
    ap.add_argument("--model", default=None)
    ap.add_argument(
        "--no-sandbox",
        action="store_true",
        help="run codex without its process sandbox (host blocks unprivileged user namespaces); "
        "isolation then relies on the detached worktree, separate DB, and the post-run check",
    )
    ns = ap.parse_args(argv)
    commit = ns.commit or _git("rev-parse", "HEAD").strip()
    phase, rev = ns.phase, ns.revision
    out_dir = ROOT / "verification" / f"phase-{phase}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_name = f"VR-P{phase}-{rev:03d}.yaml"
    if (out_dir / report_name).exists():
        print(f"refusing to overwrite existing report {report_name}; use a new --revision")
        return 2
    worktree = prepare_worktree(commit, phase, rev)
    run_dir = out_dir / f"run-r{rev:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_out = run_dir / "manifest.yaml"
    subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "tools.evidence_manifest",
            "--phase",
            str(phase),
            "--commit",
            commit,
            "--out",
            str(manifest_out),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wt_manifest = worktree / "evidence" / f"phase-{phase}" / "manifest.yaml"
    wt_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_out, wt_manifest)
    wt_report = f"verification/phase-{phase}/{report_name}"
    wt_evidence = f"verification/phase-{phase}/evidence-r{rev:03d}"
    (worktree / wt_evidence).mkdir(parents=True, exist_ok=True)
    prompt = render_prompt(phase, commit, rev, wt_report, wt_evidence, ns.db_url, ns.extra_env)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    started = dt.datetime.now(dt.UTC)
    env = {
        **os.environ,
        "AGENT_COLAB_TEST_DATABASE_URL": ns.db_url,
        "PATH": f"{Path.home() / '.local' / 'bin'}:{os.environ.get('PATH', '')}",
    }
    sandbox_args = (
        ["--dangerously-bypass-approvals-and-sandbox"]
        if ns.no_sandbox
        else ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    )
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(worktree),
        *sandbox_args,
        "--color",
        "never",
        "--json",
        "-o",
        str(run_dir / "last-message.txt"),
    ]
    if ns.model:
        cmd += ["-m", ns.model]
    with (
        (run_dir / "events.jsonl").open("w", encoding="utf-8") as events,
        (run_dir / "stderr.log").open("w", encoding="utf-8") as stderr,
        (run_dir / "prompt.md").open("r", encoding="utf-8") as prompt_file,
    ):
        proc = subprocess.run(
            cmd,
            stdin=prompt_file,
            stdout=events,
            stderr=stderr,
            env=env,
            timeout=ns.timeout,
            check=False,
        )
    finished = dt.datetime.now(dt.UTC)
    src = worktree / wt_report
    meta: dict[str, object] = {
        "verification_id": f"VR-P{phase}-{rev:03d}",
        "phase": phase,
        "revision": rev,
        "target_commit": commit,
        "worktree": str(worktree),
        "codex_exit_code": proc.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "environment_fingerprint": _env_fingerprint(),
        "report_present": src.exists(),
        "sandbox": "none (host AppArmor blocks bubblewrap userns)"
        if ns.no_sandbox
        else "workspace-write",
        "worktree_modifications_outside_verification": _worktree_modifications(worktree, phase),
    }
    if src.exists():
        data = src.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        (out_dir / report_name).write_bytes(data)
        (out_dir / f"{report_name}.sha256").write_text(
            f"{digest}  {report_name}\n", encoding="utf-8"
        )
        copied = hashlib.sha256((out_dir / report_name).read_bytes()).hexdigest()
        meta["report_sha256"] = digest
        meta["copied_unmodified"] = copied == digest
        ev_src = worktree / wt_evidence
        if ev_src.exists():
            shutil.copytree(ev_src, out_dir / f"evidence-r{rev:03d}", dirs_exist_ok=True)
        try:
            report = yaml.safe_load(data)
            schema = json.loads(
                (ROOT / "schemas" / "documents" / "verifier-report.v1.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            errors = sorted(Draft202012Validator(schema).iter_errors(report), key=str)
            meta["schema_errors"] = [e.message for e in errors][:20]
            meta["result"] = report.get("result") if isinstance(report, dict) else None
        except yaml.YAMLError as exc:
            meta["schema_errors"] = [f"YAML parse error: {exc}"]
            meta["result"] = None
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0 if meta.get("report_present") else 1


if __name__ == "__main__":
    sys.exit(main())
