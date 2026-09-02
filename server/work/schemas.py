"""Validators for the adapter/work-item contract schemas (schemas/adapters, P0-11)."""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
ADAPTER_SCHEMAS = {
    "work_item": "adapters/work-item.v1.schema.json",
    "delivery_receipt": "adapters/delivery-receipt.v1.schema.json",
    "work_result": "adapters/work-result.v1.schema.json",
    "usage": "adapters/usage.v1.schema.json",
    "webhook_envelope": "adapters/webhook-envelope.v1.schema.json",
    "probe_response": "adapters/probe-response.v1.schema.json",
    "heartbeat": "adapters/heartbeat.v1.schema.json",
    "pricing": "api/pricing.v1.schema.json",
}


class AdapterSchemaError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _load(rel: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMAS_DIR / rel).read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for rel in ADAPTER_SCHEMAS.values():
        schema = _load(rel)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        # relative $ref like "usage.v1.schema.json#/$defs/usage" resolves against the $id base
    return registry


@cache
def validator(name: str) -> Draft202012Validator:
    schema = _load(ADAPTER_SCHEMAS[name])
    return Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())


def validate(name: str, instance: Any) -> None:
    """Raise ``AdapterSchemaError`` with code ``<NAME>_SCHEMA_INVALID`` on the first violation."""
    errors = sorted(validator(name).iter_errors(instance), key=lambda e: (list(e.path), e.message))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "<root>"
        raise AdapterSchemaError(f"{name.upper()}_SCHEMA_INVALID", f"{path}: {first.message}")
