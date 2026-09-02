"""Pricing versions (development plan §7C): the pricing table is versioned, immutable once
activated, and every usage record pins the version it was computed with."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.usage.pricing import PRICING_PATH, Pricing, UsageError, pricing_from_dict


def table_sha256(table: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(table)).hexdigest()


def activate_pricing(
    session: Session, table: dict[str, Any], activated_by: str | None = None
) -> str:
    """Insert the table as ``pricing_versions[table.version]``; idempotent for identical content.

    Re-activating a version id with different content is ``PRICING_VERSION_IMMUTABLE``.
    """
    pricing = pricing_from_dict(table)
    digest = table_sha256(table)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('pricing_versions'))"))
    existing = session.execute(
        text("SELECT table_sha256 FROM pricing_versions WHERE pricing_version = :v"),
        {"v": pricing.version},
    ).scalar()
    if existing is not None:
        if existing != digest:
            raise UsageError("PRICING_VERSION_IMMUTABLE", pricing.version)
        return pricing.version
    import json

    session.execute(
        text(
            "INSERT INTO pricing_versions "
            "(pricing_version, table_json, table_sha256, activated_by) "
            "VALUES (:v, CAST(:t AS jsonb), :h, :a)"
        ),
        {
            "v": pricing.version,
            "t": json.dumps(table),
            "h": digest,
            "a": uuid.UUID(activated_by) if activated_by else None,
        },
    )
    return pricing.version


def activate_from_file(
    session: Session, path: Path = PRICING_PATH, activated_by: str | None = None
) -> str:
    table = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(table, dict):
        raise UsageError("PRICING_INVALID", "pricing file must be a mapping")
    return activate_pricing(session, table, activated_by)


def current_pricing(session: Session) -> Pricing:
    """The most recently activated pricing version (``PRICING_NOT_ACTIVATED`` if none)."""
    row = session.execute(
        text(
            "SELECT table_json FROM pricing_versions "
            "ORDER BY activated_at DESC, pricing_version DESC LIMIT 1"
        )
    ).first()
    if row is None:
        raise UsageError("PRICING_NOT_ACTIVATED", "activate policy/pricing.yaml first")
    table: dict[str, Any] = row[0]
    return pricing_from_dict(table)
