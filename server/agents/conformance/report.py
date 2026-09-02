"""Conformance report document (``schemas/documents/adapter-conformance-report.v1.schema.json``)."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_ID = "colab.adapter-conformance-report.v1"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "documents"
    / "adapter-conformance-report.v1.schema.json"
)

CHECK_TITLES: dict[str, str] = {
    "CS-01": "probe identity stability",
    "CS-02": "deliver idempotency",
    "CS-03": "ack/accept timing",
    "CS-04": "invoke result schema",
    "CS-05": "cancel",
    "CS-06": "heartbeat",
    "CS-07": "secret handle non-exposure",
    "CS-08": "correlation preserved",
    "CS-09": "retry duplicate prevention",
    "CS-10": "unsupported declared",
    "CS-11": "error normalization",
    "CS-12": "reconnect",
}


@dataclass
class CheckResult:
    id: str
    title: str
    result: str  # PASS | FAIL
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConformanceReport:
    adapter_type: str
    agent_id: str
    generated_at: str
    checks: list[CheckResult]
    schema_id: str = SCHEMA_ID
    harness: str = ""

    @property
    def result(self) -> str:
        return "PASS" if all(c.result == "PASS" for c in self.checks) else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "adapter_type": self.adapter_type,
            "agent_id": self.agent_id,
            "generated_at": self.generated_at,
            "harness": self.harness,
            "result": self.result,
            "checks": [asdict(c) for c in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)


def validate_report(doc: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(doc)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
