"""V-P7-01: a clean production-like install on empty volumes, configured through the Wizard.

Brings up `deploy/production/compose.yaml` with empty volumes, waits for health, then drives the
Setup Wizard exactly as `docs/operations/release-build.md` prescribes: issue a token, configure the
database, key, Owner and integration sections, run preflight, read the redacted diff and bootstrap
to LOCKED. Reports the wall-clock time so the 30-minute criterion is measurable. No secret value is
printed: the Owner material is reported as "received" only.

The install provisions everything it needs. With no ``COLAB_SERVER_IMAGE`` it builds the
production server image from ``deploy/production/Dockerfile.server``; with no Mattermost URL it
starts the local Team Edition (``scripts/dev/mattermost-local.sh``) and uses its bot credentials,
which are read from ``~/.local/opt/mattermost/.spike-credentials`` and never printed. Only an
unusable Docker makes this unrunnable, and then the reason is stated.

Usage:
    uv run python -m tools.clean_install --check      # is the environment able to run this?
    uv run python -m tools.clean_install --run        # provision, bring up, configure, verify
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
DOCKERFILE = ROOT / "deploy" / "production" / "Dockerfile.server"
LOCAL_IMAGE = "agent-colab/server:local"
MATTERMOST_SCRIPT = ROOT / "scripts" / "dev" / "mattermost-local.sh"
MATTERMOST_LOCAL_URL = "http://127.0.0.1:8065"
MATTERMOST_CREDENTIALS = Path(
    os.environ.get(
        "COLAB_MATTERMOST_CREDENTIALS",
        str(Path.home() / ".local/opt/mattermost/.spike-credentials"),
    )
)
DEADLINE_S = 30 * 60


class InstallUnavailableError(RuntimeError):
    """Something the install needs could not be provisioned; the message says what."""


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
    """Docker is the only thing this cannot provision for itself."""
    if shutil.which("sg") is None:
        return False, "sg is not available (docker needs the docker group in this shell)"
    probe = _docker("version", "--format", "{{.Server.Version}}", check=False, timeout=60)
    if probe.returncode != 0:
        return False, f"docker is not usable: {probe.stderr.strip()[:120]}"
    if not COMPOSE.exists():
        return False, f"{COMPOSE.relative_to(ROOT)} is missing"
    return True, f"docker {probe.stdout.strip()}"


def ensure_image() -> tuple[str, str]:
    """The released server image, built from this source tree when none is named."""
    named = os.environ.get("COLAB_SERVER_IMAGE")
    if named:
        return named, "COLAB_SERVER_IMAGE"
    if not DOCKERFILE.exists():
        raise InstallUnavailableError(f"{DOCKERFILE.relative_to(ROOT)} is missing")
    build = _docker(
        "build", "-f", str(DOCKERFILE), "-t", LOCAL_IMAGE, str(ROOT), check=False, timeout=3600
    )
    if build.returncode != 0:
        tail = (build.stderr or build.stdout).strip().splitlines()
        raise InstallUnavailableError(f"image build failed: {tail[-1] if tail else 'no output'}")
    return LOCAL_IMAGE, "built from deploy/production/Dockerfile.server"


@dataclass(frozen=True)
class MattermostTarget:
    """Where the container reaches Mattermost. The token is never printed or reported."""

    url: str
    team: str
    token: str = field(repr=False)
    detail: str = ""


def _bridge_gateway() -> str:
    """The host address a container reaches: the docker bridge gateway, not the host loopback."""
    out = _docker(
        "network",
        "inspect",
        "bridge",
        "--format",
        "'{{(index .IPAM.Config 0).Gateway}}'",
        check=False,
        timeout=60,
    )
    gateway = out.stdout.strip() if out.returncode == 0 else ""
    return gateway or "172.17.0.1"


def _mattermost_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 - http(s) URL from configuration
            f"{url.rstrip('/')}/api/v4/system/ping", timeout=10
        ) as response:
            return bool(response.status == 200)
    except (OSError, urllib.error.HTTPError):
        return False


def _mattermost_api(url: str, token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - http(s) URL from configuration
        f"{url.rstrip('/')}/api/v4{path}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310
        return json.loads(response.read() or b"{}")


def _credentials() -> dict[str, str]:
    if not MATTERMOST_CREDENTIALS.exists():
        raise InstallUnavailableError(f"{MATTERMOST_CREDENTIALS} is missing")
    values: dict[str, str] = {}
    for line in MATTERMOST_CREDENTIALS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [k for k in ("ADMIN_TOKEN", "BOT_TOKEN", "BOT_USER_ID") if not values.get(k)]
    if missing:
        raise InstallUnavailableError(f"{MATTERMOST_CREDENTIALS} has no {', '.join(missing)}")
    return values


def ensure_mattermost() -> MattermostTarget:
    """Mattermost is a mandatory integration, so start the local instance when none is named."""
    url = os.environ.get("COLAB_MATTERMOST_URL", "")
    token = os.environ.get("COLAB_MATTERMOST_BOT_TOKEN", "")
    team = os.environ.get("COLAB_MATTERMOST_TEAM", "")
    if url and token:
        return MattermostTarget(url, team or "colab", token, "named by the environment")
    how = "local Team Edition, already running"
    if not _mattermost_up(MATTERMOST_LOCAL_URL):
        how = "local Team Edition started by scripts/dev/mattermost-local.sh"
        if not MATTERMOST_SCRIPT.exists():
            raise InstallUnavailableError(f"{MATTERMOST_SCRIPT.relative_to(ROOT)} is missing")
        subprocess.run(
            ["bash", str(MATTERMOST_SCRIPT), "start"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if not _mattermost_up(MATTERMOST_LOCAL_URL):
            raise InstallUnavailableError("scripts/dev/mattermost-local.sh start did not come up")
    creds = _credentials()
    if not team:  # a team the bot is a member of, created for this install
        team = f"clean{uuid.uuid4().hex[:8]}"
        created = _mattermost_api(
            MATTERMOST_LOCAL_URL,
            creds["ADMIN_TOKEN"],
            "/teams",
            {"name": team, "display_name": f"clean install {team}", "type": "O"},
        )
        _mattermost_api(
            MATTERMOST_LOCAL_URL,
            creds["ADMIN_TOKEN"],
            f"/teams/{created['id']}/members",
            {"team_id": created["id"], "user_id": creds["BOT_USER_ID"]},
        )
    return MattermostTarget(
        f"http://{_bridge_gateway()}:8065",
        team,
        creds["BOT_TOKEN"],
        how,
    )


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
        with urllib.request.urlopen(request, timeout=30):  # noqa: S310  # nosec B310 - fixed http loopback URL built above
            return False
    except urllib.error.HTTPError as exc:
        return exc.code in (403, 404)
    except OSError:
        return True


def run_install(port: int = 8080, keep: bool = False) -> InstallReport:
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    image, image_detail = ensure_image()
    mattermost = ensure_mattermost()
    env_file = ROOT / "deploy" / "production" / ".env.clean-install"
    password = uuid.uuid4().hex  # a fresh instance password, never reused or printed
    env_file.write_text(
        f"COLAB_DB_PASSWORD={password}\nCOLAB_SERVER_PORT={port}\nCOLAB_SERVER_IMAGE={image}\n",
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
                    "mattermost.url": mattermost.url,
                    "mattermost.team": mattermost.team,
                    "mattermost.bot_token": mattermost.token,
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
            f"owner material {owner_material}; bootstrap sealed: {relocked}; "
            f"image {image_detail}; mattermost {mattermost.detail}",
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
    try:
        report = run_install(port=args.port, keep=args.keep)
    except InstallUnavailableError as exc:
        print(json.dumps({"available": True, "provisioned": False, "detail": str(exc)}))
        return 1
    payload = report.as_dict()
    print(json.dumps(payload, indent=2))
    if args.out:
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
