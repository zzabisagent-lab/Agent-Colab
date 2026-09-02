"""Generate per-type Event payload schemas and the registry from ``schemas/events/catalog.v1.yaml``.

``python -m tools.gen_event_schemas`` writes files; ``--check`` fails on drift (CI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from tools.baseline import ROOT

CATALOG = ROOT / "schemas" / "events" / "catalog.v1.yaml"
TYPES_DIR = ROOT / "schemas" / "events" / "types"
REGISTRY = ROOT / "schemas" / "events" / "registry.v1.json"
SCHEMA_ID_BASE = "https://agent-colab.dev/schemas/events"

_TIMESTAMP_FIELDS = {
    "expires_at",
    "deadline",
    "scheduled_for",
    "lease_expires_at",
    "executable_after",
}
_INT_FIELDS = {
    "assignment_revision",
    "depth",
    "role_version",
    "version",
    "revision",
    "quorum_count",
    "used_count",
    "turn_no",
    "delivery_no",
    "cost_units",
    "limit_cost_units",
    "requested_cost_units",
    "size",
    "attempt_no",
    "missed_heartbeats",
    "capacity",
    "criteria_revision",
}
_ARRAY_FIELDS = {"changed_fields", "evidence_refs", "satisfied_children", "finding_ids"}
_OBJECT_FIELDS = {"scope", "limits"}
_BOOL_FIELDS = {"secret"}


def _field_schema(name: str) -> dict[str, Any]:
    if name in _TIMESTAMP_FIELDS:
        return {
            "type": "string",
            "format": "date-time",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        }
    if name in _INT_FIELDS:
        return {"type": "integer", "minimum": 0}
    if name in _ARRAY_FIELDS:
        return {"type": "array", "items": {"type": "string"}}
    if name in _OBJECT_FIELDS:
        return {"type": "object"}
    if name in _BOOL_FIELDS:
        return {"type": "boolean"}
    if name in {"sha256", "snapshot_hash", "policy_snapshot_hash", "permissions_hash"}:
        return {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {"type": "string", "minLength": 1}


def render(catalog: dict[str, Any]) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    registry: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_ID_BASE}/registry.v1.json",
        "title": "Agent-Colab Event registry v1",
        "version": catalog["version"],
        "aggregates": catalog["aggregates"],
        "authority": catalog["authority"],
        "events": {},
    }
    for name, spec in catalog["events"].items():
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_ID_BASE}/types/{name}.v1.schema.json",
            "title": f"{name} payload v1",
            "description": (
                f"Non-sensitive payload of {name} ({spec['aggregate']} aggregate, "
                f"Phase {spec['phase']}). Sensitive content goes to the encrypted envelope."
            ),
            "type": "object",
            "properties": {f: _field_schema(f) for f in spec["required"]},
            "required": list(spec["required"]),
            "additionalProperties": True,
        }
        if spec.get("extension"):
            schema["x-extension"] = spec["extension"]
        files[f"{name}.v1.schema.json"] = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        registry["events"][name] = {
            "aggregate_type": spec["aggregate"],
            "phase": spec["phase"],
            "schema_version": 1,
            "payload_schema": f"types/{name}.v1.schema.json",
            "extension": bool(spec.get("extension")),
        }
    return files, json.dumps(registry, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ns = ap.parse_args(argv)
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    files, registry = render(catalog)
    expected: dict[Path, str] = {TYPES_DIR / n: c for n, c in files.items()}
    expected[REGISTRY] = registry
    drift = [p for p, c in expected.items() if not p.exists() or p.read_text(encoding="utf-8") != c]
    stale = [p for p in TYPES_DIR.glob("*.schema.json") if p not in expected]
    if ns.check:
        for p in drift + stale:
            print(f"SCHEMA DRIFT: {p.relative_to(ROOT)}")
        print(f"gen_event_schemas: {len(files)} types, {len(drift)} drifted, {len(stale)} stale")
        return 1 if (drift or stale) else 0
    TYPES_DIR.mkdir(parents=True, exist_ok=True)
    for p, c in expected.items():
        p.write_text(c, encoding="utf-8")
    for p in stale:
        p.unlink()
    print(f"gen_event_schemas: wrote {len(files)} type schemas and the registry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
