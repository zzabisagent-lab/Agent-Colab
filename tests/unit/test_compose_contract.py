"""Static checks of the Compose dev stack (P0-04). Runtime health is V-P0-04 (needs Docker)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_defines_all_services_with_healthchecks() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"postgres", "server", "web-admin", "clamav"}
    for name, svc in services.items():
        assert "healthcheck" in svc, name
    assert "ports" not in services["postgres"], "PostgreSQL must not be published"
    assert all(p.startswith("127.0.0.1:") for p in services["server"]["ports"])
    assert all(p.startswith("127.0.0.1:") for p in services["web-admin"]["ports"])


def test_images_are_pinned() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    for svc in compose["services"].values():
        if "image" in svc:
            assert ":" in svc["image"] and not svc["image"].endswith(":latest")
    for df in (ROOT / "deploy" / "dev").glob("Dockerfile.*"):
        for line in df.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                assert ":" in line and "latest" not in line, line
