"""Event contract validation: envelope schema, type registry, payload schema, hash (P0-03).

Every check returns a stable error code (``ContractError.code``) so REST, MCP, and fixtures
report identical failures (V-P0-05, V-P0-13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from server.events.hashing import compute_content_hash

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas" / "events"


class ContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class EventTypeInfo:
    name: str
    aggregate_type: str
    phase: int
    schema_version: int
    extension: bool


class SchemaRegistry:
    """Loads the envelope schema, the Event registry, and per-type payload schemas."""

    def __init__(self, schemas_dir: Path = SCHEMAS_DIR) -> None:
        self._dir = schemas_dir
        self._envelope = Draft202012Validator(
            json.loads((schemas_dir / "envelope.v1.schema.json").read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        )
        registry = json.loads((schemas_dir / "registry.v1.json").read_text(encoding="utf-8"))
        self.aggregates: dict[str, dict[str, Any]] = registry["aggregates"]
        self.authority: dict[str, str] = registry["authority"]
        self._types: dict[str, EventTypeInfo] = {}
        self._payload_validators: dict[str, Draft202012Validator] = {}
        for name, spec in registry["events"].items():
            self._types[name] = EventTypeInfo(
                name,
                spec["aggregate_type"],
                spec["phase"],
                spec["schema_version"],
                spec["extension"],
            )
            schema = json.loads((schemas_dir / spec["payload_schema"]).read_text(encoding="utf-8"))
            self._payload_validators[name] = Draft202012Validator(
                schema, format_checker=FormatChecker()
            )

    @property
    def event_types(self) -> dict[str, EventTypeInfo]:
        return dict(self._types)

    def validate_envelope(self, event: dict[str, Any]) -> None:
        errors = sorted(self._envelope.iter_errors(event), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            path = "/".join(str(p) for p in first.path) or "<root>"
            raise ContractError("SCHEMA_INVALID", f"{path}: {first.message}")

    def validate_type(self, event: dict[str, Any]) -> EventTypeInfo:
        info = self._types.get(event["type"])
        if info is None:
            raise ContractError("UNKNOWN_EVENT_TYPE", event["type"])
        if event.get("schema_version") != info.schema_version:
            raise ContractError("UNSUPPORTED_SCHEMA_VERSION", str(event.get("schema_version")))
        if event["aggregate_type"] != info.aggregate_type:
            raise ContractError(
                "AGGREGATE_TYPE_MISMATCH",
                f"{event['type']} belongs to {info.aggregate_type}, got {event['aggregate_type']}",
            )
        if event["aggregate_type"] not in self.aggregates:
            raise ContractError("UNKNOWN_AGGREGATE_TYPE", event["aggregate_type"])
        prefix = self.aggregates[event["aggregate_type"]]["id_prefix"]
        if not event["aggregate_id"].startswith(prefix):
            raise ContractError("AGGREGATE_ID_INVALID", f"expected prefix {prefix}")
        scope_aggregate = event["idempotency_scope"].split(":", 1)[0]
        if scope_aggregate != event["aggregate_type"]:
            raise ContractError("IDEMPOTENCY_SCOPE_INVALID", event["idempotency_scope"])
        errors = sorted(self._payload_validators[info.name].iter_errors(event["payload"]), key=str)
        if errors:
            path = "/".join(str(p) for p in errors[0].path) or "<root>"
            raise ContractError("PAYLOAD_INVALID", f"{path}: {errors[0].message}")
        return info

    def validate_hash(self, event: dict[str, Any]) -> None:
        try:
            expected = compute_content_hash(event)
        except ValueError as exc:  # canonicalization or base64 failure
            raise ContractError("HASH_INPUT_INVALID", str(exc)) from exc
        if event.get("content_hash") != expected:
            raise ContractError("HASH_MISMATCH", "content_hash does not match canonical content")

    def validate(self, event: dict[str, Any]) -> EventTypeInfo:
        """Full contract validation in a fixed order: envelope → type/payload → hash."""
        self.validate_envelope(event)
        info = self.validate_type(event)
        self.validate_hash(event)
        return info


@lru_cache(maxsize=1)
def default_registry() -> SchemaRegistry:
    return SchemaRegistry()
