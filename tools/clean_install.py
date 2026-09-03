"""V-P7-01: a clean production-like install on empty volumes, configured through the Wizard.

Brings up `deploy/production/compose.yaml` with empty volumes, waits for health, then drives the
Setup Wizard exactly as `docs/operations/release-build.md` prescribes: issue a token, configure the
database, key, Owner and integration sections, run preflight, read the redacted diff and bootstrap
to LOCKED. Reports the wall-clock time so the 30-minute criterion is measurable. No secret value is
printed: the Owner material is reported as "received" only.

Usage:
    uv run python -m tools.clean_install --check      # is the environment able to run this?
    uv run python -m tools.clean_install --run        # bring up, configure, verify, tear down
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "production" / "compose.yaml"
DEADLINE_S = 30 * 60


def _docker(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    """Docker in this environment needs the `docker` group, so commands run through `sg`."""
    command = " ".join(["docker", *args])
    return subprocess.run(
        ["sg", "docker", "-c", command],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def available() -> tuple[bool, str]:
    if shutil.which("sg") is None:
        return False, "sg is not available"
    probe = _docker("version", "--format", "{{.Server.Version}}", check=False, timeout=60)
    if probe.returncode != 0:
        return False, f"docker is not usable: {probe.stderr.strip()[:120]}"
    if not os.environ.get("COLAB_SERVER_IMAGE"):
        return False, "COLAB_SERVER_IMAGE is not set (build a release first)"
    for name in ("COLAB_MATTERMOST_URL", "COLAB_MATTERMOST_BOT_TOKEN"):
        if not os.environ.get(name):
            return False, f"{name} is not set (Mattermost is a mandatory integration)"
    return True, f"docker {probe.stdout.strip()}"


@dataclass
class InstallReport:
    ok: bool
    seconds: float
    state: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "seconds": round(self.seconds, 1),
            "deadline_seconds": DEADLINE_S,
            "state": self.state,
            "steps": self.steps,
            "detail": self.detail,
        }


SNIPPET = """
import json, sys, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
data = None if body == "-" else body.encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080" + path, data=data,
    headers={"Content-Type": "application/json"}, method=method,
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        print(json.dumps({"status": r.status, "body": json.loads(r.read() or b"{}")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "body": {}}))
"""


def _call(
    compose: str, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Setup binds loopback (§8.3), so the Wizard runs inside the container, as the runbook says."""
    payload = "-" if body is None else json.dumps(body)
    result = _docker(
        *compose.split(),
        "exec",
        "-T",
        "server",
        "python",
        "-c",
        f"'{SNIPPET}'",
        method,
        path,
        f"'{payload}'",
        check=False,
        timeout=180,
    )
    line = (result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else "{}"
    parsed = json.loads(line)
    if int(parsed.get("status", 500)) >= 400:
        raise RuntimeError(f"{method} {path} -> {parsed.get('status')}")
    return dict(parsed.get("body", {}))


def _host_refused(base: str, path: str) -> bool:
    """The published port must not expose the Wizard: loopback is inside the container."""
    try:
        request = urllib.request.Request(  # noqa: S310 - fixed loopback base
            f"{base}{path}", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30):  # noqa: S310
            return False
    except urllib.error.HTTPError as exc:
        return exc.code in (403, 404)
    except OSError:
        return True


def run_install(port: int = 8080, keep: bool = False) -> InstallReport:
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    env_file = ROOT / "deploy" / "production" / ".env.clean-install"
    password = uuid.uuid4().hex  # a fresh instance password, never reused or printed
    env_file.write_text(
        f"COLAB_DB_PASSWORD={password}\nCOLAB_SERVER_PORT={port}\n"
        f"COLAB_SERVER_IMAGE={os.environ['COLAB_SERVER_IMAGE']}\n",
        encoding="utf-8",
    )
    compose = f"compose -f {COMPOSE} --env-file {env_file} -p colab-clean"
    try:
        _docker(*compose.split(), "down", "-v", "--remove-orphans", check=False, timeout=300)
        _docker(*compose.split(), "up", "-d", "--wait", "--wait-timeout", "600")
        if not _host_refused(base, "/setup/token"):
            return InstallReport(
                False,
                time.monotonic() - started,
                "UNKNOWN",
                detail="the Wizard answered a non-loopback caller",
            )
        state = _call(compose, "GET", "/setup/state")
        if state.get("state") != "UNINITIALIZED":
            return InstallReport(
                False,
                time.monotonic() - started,
                str(state.get("state")),
                detail="volumes were not empty",
            )
        token = str(_call(compose, "POST", "/setup/token")["token"])
        sections = [
            (
                "db",
                {
                    "db_host": "postgres",
                    "db_port": 5432,
                    "db_name": "colab",
                    "db_user": "colab",
                    "db_password": password,
                },
            ),
            (
                "keys",
                {
                    "secrets.provider": "local",
                    "secrets.master_key_path": "/var/lib/agent-colab/keys/master.key",
                },
            ),
            ("owner", {"account_id": "acct-owner", "display_name": "System Owner"}),
            (
                "integrations",
                {
                    "instance.name": "Agent-Colab",
                    "instance.base_url": base,
                    # Mattermost is a mandatory integration: preflight refuses an instance that
                    # cannot reach its conversation channel. The container reaches a host service
                    # through the bridge gateway, not through the host's loopback.
                    "mattermost.url": os.environ["COLAB_MATTERMOST_URL"],
                    "mattermost.team": os.environ.get("COLAB_MATTERMOST_TEAM", "colab"),
                    "mattermost.bot_token": os.environ["COLAB_MATTERMOST_BOT_TOKEN"],
                    "storage.artifact_root": "/var/lib/agent-colab/artifacts",
                    "storage.document_root": "/var/lib/agent-colab/documents",
                    "ops.channel_id": "ops",
                },
            ),
        ]
        for section, values in sections:
            _call(compose, "POST", "/setup/configure", {"section": section, "values": values})
        preflight = _call(compose, "POST", "/setup/preflight")
        steps = list(preflight.get("steps", []))
        if not preflight.get("ok"):
            return InstallReport(
                False,
                time.monotonic() - started,
                str(preflight.get("state")),
                steps,
                "preflight failed",
            )
        _call(compose, "GET", "/setup/diff")  # the redacted diff an operator reads before applying
        result = _call(compose, "POST", "/setup/bootstrap", {"token": token})
        owner_material = "received" if result.get("owner") else "missing"
        final = _call(compose, "GET", "/setup/state")
        elapsed = time.monotonic() - started
        locked = final.get("state") == "LOCKED"
        try:  # after LOCKED the bootstrap endpoint must be gone
            _call(compose, "POST", "/setup/bootstrap", {"token": token})
            relocked = False
        except RuntimeError:
            relocked = True
        return InstallReport(
            bool(locked and relocked and owner_material == "received" and elapsed < DEADLINE_S),
            elapsed,
            str(final.get("state")),
            steps,
            f"owner material {owner_material}; bootstrap sealed: {relocked}",
        )
    finally:
        if not keep:
            _docker(*compose.split(), "down", "-v", "--remove-orphans", check=False, timeout=300)
        env_file.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report whether this host can run it")
    parser.add_argument("--run", action="store_true", help="perform the clean install")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--keep", action="store_true", help="leave the stack up afterwards")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    ok, detail = available()
    if args.check or not args.run:
        print(json.dumps({"available": ok, "detail": detail}))
        return 0 if ok else 1
    if not ok:
        print(json.dumps({"available": False, "detail": detail}))
        return 1
    report = run_install(port=args.port, keep=args.keep)
    payload = report.as_dict()
    print(json.dumps(payload, indent=2))
    if args.out:
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
