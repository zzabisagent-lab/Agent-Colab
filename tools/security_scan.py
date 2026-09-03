"""Run the release security scans and write one JSON report per scan (P7-05, V-P7-11).

Four scans, each writing ``evidence/phase-7/scans/<name>.json``:

* **sast** — bandit over ``server`` and ``sidecar`` at every severity.
* **dependency** — pip-audit against the installed project environment (OSV), plus the pnpm
  lockfile audit for the JavaScript tree.
* **container** — Trivy against every image a release ships (server and web-admin), each pinned
  to the image id the release manifest records.
* **dynamic** — checks against a running instance: security headers, unauthenticated access to
  admin routes, error-body leakage and cookie flags.

The gate is zero High or Critical. A scanner that cannot run records ``SCANNER_UNAVAILABLE`` with
the reason and does not silently pass: ``--require`` names the scans that must actually have run.
The container scan goes further and blocks outright when it cannot cover both release images, so
it can never report a clean result while an image went unscanned or a tag drifted from the
manifest.

    uv run python -m tools.security_scan --all
    uv run python -m tools.security_scan --scan sast dependency --require sast dependency
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "evidence" / "phase-7" / "scans"
BLOCKING = ("HIGH", "CRITICAL")
# Every image a release ships. The container gate must scan all of them: it cannot pass while one
# is unscanned, whatever the manifest happens to pin (V-P7-11).
REQUIRED_IMAGES = ("server", "web-admin")


def _report(name: str, findings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    blocking = [f for f in findings if str(f.get("severity", "")).upper() in BLOCKING]
    return {
        "schema_id": "colab.security-scan.v1",
        "scan": name,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ran": extra.pop("ran", True),
        "findings": findings,
        "counts": {
            severity: sum(1 for f in findings if str(f.get("severity", "")).upper() == severity)
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
        },
        "blocking": len(blocking),
        **extra,
    }


def _unavailable(name: str, reason: str) -> dict[str, Any]:
    return _report(name, [], ran=False, reason_code="SCANNER_UNAVAILABLE", reason=reason)


def _blocked(name: str, code: str, reason: str) -> dict[str, Any]:
    """A scan that could not cover what it must. Blocking, not merely unavailable.

    A release gate that reports "unavailable" when it cannot scan can be passed by removing the
    scanner. An image the gate never looked at is indistinguishable from an image full of
    vulnerabilities, so it counts as a blocking finding (V-P7-11).
    """
    return _report(
        name,
        [{"id": code, "severity": "CRITICAL", "title": reason}],
        reason_code=code,
        reason=reason,
    )


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    command = " ".join(["docker", *args])
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if probe.returncode != 0 and shutil.which("sg"):
        return subprocess.run(
            ["sg", "docker", "-c", command], capture_output=True, text=True, check=False
        )
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def scan_sast() -> dict[str, Any]:
    """Bandit at every severity; the repository's own config carries the documented skips."""
    result = subprocess.run(
        [
            "uv",
            "run",
            "bandit",
            "-q",
            "-c",
            "pyproject.toml",
            "-r",
            "server",
            "sidecar",
            "-f",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        return _unavailable("sast", result.stderr[-400:] or "bandit produced no output")
    document = json.loads(result.stdout)
    findings = [
        {
            "id": issue.get("test_id"),
            "severity": str(issue.get("issue_severity", "UNKNOWN")).upper(),
            "confidence": issue.get("issue_confidence"),
            "title": issue.get("issue_text"),
            "location": f"{issue.get('filename')}:{issue.get('line_number')}",
        }
        for issue in document.get("results", [])
    ]
    return _report(
        "sast", findings, tool="bandit", scanned=document.get("metrics", {}).get("_totals", {})
    )


def scan_dependency() -> dict[str, Any]:
    """pip-audit over the installed environment, and the pnpm audit over the locked JS tree."""
    findings: list[dict[str, Any]] = []
    tools: list[str] = []

    py = subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "pip-audit",
            "pip-audit",
            "-f",
            "json",
            "--progress-spinner",
            "off",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if py.stdout.strip():
        tools.append("pip-audit")
        for dep in json.loads(py.stdout).get("dependencies", []):
            for vuln in dep.get("vulns", []):
                findings.append(
                    {
                        "id": vuln.get("id"),
                        # OSV severity is not always populated; an unrated advisory is not "low"
                        "severity": str(vuln.get("severity") or "UNKNOWN").upper(),
                        "title": f"{dep.get('name')} {dep.get('version')}",
                        "fix_versions": vuln.get("fix_versions", []),
                    }
                )
    js = subprocess.run(
        ["pnpm", "audit", "--json", "--prod"],
        cwd=ROOT / "web-admin",
        capture_output=True,
        text=True,
        check=False,
    )
    if js.stdout.strip():
        tools.append("pnpm audit")
        for line in js.stdout.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            advisories = entry.get("advisories") or {}
            for advisory in advisories.values():
                findings.append(
                    {
                        "id": advisory.get("github_advisory_id") or advisory.get("id"),
                        "severity": str(advisory.get("severity", "UNKNOWN")).upper(),
                        "title": advisory.get("module_name"),
                    }
                )
    if not tools:
        return _unavailable("dependency", "neither pip-audit nor pnpm audit produced output")
    return _report("dependency", findings, tools=tools)


def scan_container(manifest: Path) -> dict[str, Any]:
    """Trivy against every image the release manifest pins, and every image a release must ship.

    Three ways this used to pass without telling the truth, all now blocking: the manifest pinning
    only one of the two images, Trivy failing, and a tag that has drifted away from the image the
    manifest recorded. The last one is the reason an independent verifier can find vulnerabilities
    where this scan found none: the tag it resolved was not the image the manifest describes.
    """
    if not manifest.exists():
        return _blocked(
            "container",
            "RELEASE_MANIFEST_MISSING",
            f"{manifest} does not exist; build a release before scanning it",
        )
    images = [i for i in json.loads(manifest.read_text()).get("images", []) if i.get("built")]
    by_name = {str(i.get("name")): i for i in images}
    absent = [name for name in REQUIRED_IMAGES if name not in by_name]
    if absent:
        return _blocked(
            "container",
            "IMAGE_NOT_SCANNED",
            f"the manifest pins no built image named {', '.join(absent)}; "
            f"every release image must be scanned ({', '.join(REQUIRED_IMAGES)})",
        )
    probe = _docker(["version", "--format", "{{.Server.Version}}"])
    if probe.returncode != 0:
        return _blocked(
            "container", "DOCKER_UNAVAILABLE", "docker is not available, so no image was scanned"
        )
    findings: list[dict[str, Any]] = []
    scanned: list[dict[str, str]] = []
    for name in REQUIRED_IMAGES:
        image = by_name[name]
        tag = str(image["tag"])
        recorded = str(image.get("image_id") or "")
        resolved = _docker(["image", "inspect", "--format", "{{.Id}}", tag])
        local_id = resolved.stdout.strip()
        if resolved.returncode != 0 or not local_id:
            return _blocked(
                "container",
                "IMAGE_NOT_PRESENT",
                f"{tag} is not present locally, so it was not scanned",
            )
        if recorded and local_id != recorded:
            return _blocked(
                "container",
                "IMAGE_DIGEST_DRIFT",
                f"{tag} resolves to {local_id} but the manifest records {recorded}; "
                "the scanned image would not be the released image",
            )
        run = _docker(
            [
                "run",
                "--rm",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "aquasec/trivy:latest",
                "image",
                "--quiet",
                "--format",
                "json",
                "--severity",
                "HIGH,CRITICAL",
                tag,
            ]
        )
        if run.returncode != 0 or not run.stdout.strip():
            return _blocked(
                "container", "TRIVY_FAILED", f"trivy failed for {tag}: {run.stderr[-300:]}"
            )
        scanned.append({"name": name, "tag": tag, "image_id": local_id})
        for result in json.loads(run.stdout).get("Results") or []:
            for vuln in result.get("Vulnerabilities") or []:
                findings.append(
                    {
                        "id": vuln.get("VulnerabilityID"),
                        "severity": str(vuln.get("Severity", "UNKNOWN")).upper(),
                        "title": f"{vuln.get('PkgName')} {vuln.get('InstalledVersion')}",
                        "image": tag,
                        "fixed_version": vuln.get("FixedVersion"),
                    }
                )
    return _report("container", findings, tool="trivy", images=scanned)


ADMIN_ROUTES = (
    "/api/v1/accounts",
    "/api/v1/settings",
    "/api/v1/secrets",
    "/api/v1/audit",
    "/api/v1/ops/overview",
)


def scan_dynamic(base_url: str | None = None, client: Any = None) -> dict[str, Any]:
    """Probe a running instance: an in-process app unless a base URL is given."""
    import httpx

    from server.config import Settings
    from server.main import create_app

    findings: list[dict[str, Any]] = []
    database_url = os.environ.get("AGENT_COLAB_TEST_DATABASE_URL")
    if client is not None:  # a caller-supplied client (used to prove the checks actually fire)
        pass
    elif base_url:
        client = httpx.Client(base_url=base_url, timeout=10.0)
    else:
        # in-process: TestClient drives the real ASGI app synchronously, middleware included
        from fastapi.testclient import TestClient

        app = create_app(Settings(database_url=database_url))
        client = TestClient(app, raise_server_exceptions=False)

    with client:
        health = client.get("/healthz")
        headers = {k.lower(): v for k, v in health.headers.items()}
        for header, expected in (
            ("content-security-policy", "default-src"),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", ""),
        ):
            if header not in headers:
                findings.append(
                    {
                        "id": f"HEADER_MISSING_{header}",
                        "severity": "MEDIUM",
                        "title": f"response is missing {header}",
                    }
                )
            elif expected and expected not in headers[header]:
                findings.append(
                    {
                        "id": f"HEADER_WEAK_{header}",
                        "severity": "LOW",
                        "title": f"{header} does not contain {expected}",
                    }
                )
        for route in ADMIN_ROUTES:
            response = client.get(route)
            if response.status_code < 400:
                findings.append(
                    {
                        "id": "UNAUTHENTICATED_ADMIN_ROUTE",
                        "severity": "CRITICAL",
                        "title": f"{route} answered {response.status_code} without credentials",
                    }
                )
            body = response.text.lower()
            for secret_marker in ("password=", "postgresql://", "traceback (most recent call"):
                if secret_marker in body:
                    findings.append(
                        {
                            "id": "ERROR_BODY_LEAK",
                            "severity": "HIGH",
                            "title": f"{route} response contains {secret_marker!r}",
                        }
                    )
        login = client.post("/api/v1/auth/sessions", json={"service_token": "not-a-real-token"})
        if login.status_code < 400:
            findings.append(
                {
                    "id": "AUTH_ACCEPTS_UNKNOWN_TOKEN",
                    "severity": "CRITICAL",
                    "title": "an unknown service token was accepted",
                }
            )
        for cookie in login.headers.get_list("set-cookie"):
            lowered = cookie.lower()
            if "httponly" not in lowered and "csrf" not in lowered:
                findings.append(
                    {
                        "id": "COOKIE_NOT_HTTPONLY",
                        "severity": "MEDIUM",
                        "title": f"session cookie without HttpOnly: {cookie.split('=')[0]}",
                    }
                )
            if "samesite" not in lowered:
                findings.append(
                    {
                        "id": "COOKIE_NO_SAMESITE",
                        "severity": "LOW",
                        "title": f"cookie without SameSite: {cookie.split('=')[0]}",
                    }
                )
    return _report("dynamic", findings, base_url=base_url or "in-process")


SCANS = {
    "sast": lambda args: scan_sast(),
    "dependency": lambda args: scan_dependency(),
    "container": lambda args: scan_container(args.manifest),
    "dynamic": lambda args: scan_dynamic(args.base_url),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", nargs="*", choices=sorted(SCANS), default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--require", nargs="*", choices=sorted(SCANS), default=[])
    parser.add_argument("--manifest", type=Path, default=ROOT / "release" / "manifest.json")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    selected = sorted(SCANS) if args.all or not args.scan else args.scan
    args.out_dir.mkdir(parents=True, exist_ok=True)
    reports = {}
    for name in selected:
        report = SCANS[name](args)
        (args.out_dir / f"{name}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        reports[name] = report

    blocking = {n: r["blocking"] for n, r in reports.items() if r["blocking"]}
    missing = [n for n in args.require if not reports.get(n, {}).get("ran")]
    summary = {
        "scans": {
            n: {"ran": r["ran"], "blocking": r["blocking"], "counts": r["counts"]}
            for n, r in reports.items()
        },
        "blocking": blocking,
        "required_but_unavailable": missing,
        "ok": not blocking and not missing,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
