"""Generic append-only hash chains with daily anchors (development plan §6.4, spec §15.20).

A chain is a table whose rows carry ``previous_hash``/``content_hash``; ``content_hash`` is the
SHA-256 of the canonical JSON of the row's immutable fields plus ``previous_hash``. Anchors record
the last row/hash of a chain per day in ``audit_hash_anchors`` (separate storage or a signed Git
record is added by P7-03). Verification recomputes every hash and compares with anchors.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.events.hashing import sha256_hex


def chain_hash(fields: dict[str, Any], previous_hash: str | None) -> str:
    return sha256_hex(canonical_json({**fields, "previous_hash": previous_hash}))


@dataclass(frozen=True)
class ChainSpec:
    """How to read a chained table: name, ordering column, and the hashed field names."""

    table: str
    order_column: str
    hashed_fields: tuple[str, ...]
    chain_name: str


AUDIT_CHAIN = ChainSpec(
    table="audit_events",
    order_column="id",
    hashed_fields=(
        "audit_id",
        "workspace_id",
        "actor_account_id",
        "actor_label",
        "action",
        "target_type",
        "target_id",
        "result",
        "error_code",
        "correlation_id",
        "redacted_metadata",
        "occurred_at",
    ),
    chain_name="audit",
)

VERIFICATION_CHAIN = ChainSpec(
    table="verification_revisions",
    order_column="id",
    hashed_fields=(
        "revision_id",
        "verification_id",
        "revision",
        "result",
        "submitted_by_account_id",
        "submitter_credential_fingerprint",
        "report_sha256",
        "event_id",
        "created_at",
    ),
    chain_name="verification_revisions",
)

TOMBSTONE_CHAIN = ChainSpec(
    table="key_tombstones",
    order_column="id",
    hashed_fields=(
        "key_ref",
        "workspace_id",
        "target_type",
        "target_id",
        "reason",
        "requested_by",
        "audit_event_id",
        "destroyed_at",
    ),
    chain_name="key_tombstones",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat(timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if hasattr(value, "hex") and not isinstance(value, bytes | str):  # uuid.UUID
        return str(value)
    return value


def hashed_row_fields(spec: ChainSpec, row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(row.get(k)) for k in spec.hashed_fields}


def last_hash(session: Session, spec: ChainSpec) -> str | None:
    row = session.execute(
        text(
            f"SELECT content_hash FROM {spec.table} ORDER BY {spec.order_column} DESC LIMIT 1"  # noqa: S608
        )
    ).first()
    return None if row is None else str(row[0])


def verify_chain(session: Session, spec: ChainSpec) -> list[str]:
    """Recompute every hash in order; return problems (empty = intact)."""
    rows = session.execute(
        text(f"SELECT * FROM {spec.table} ORDER BY {spec.order_column} ASC")  # noqa: S608
    ).mappings()
    problems: list[str] = []
    previous: str | None = None
    for row in rows:
        expected = chain_hash(hashed_row_fields(spec, dict(row)), previous)
        rid = row[spec.order_column]
        if row["previous_hash"] != previous:
            problems.append(f"{spec.table}#{rid}: previous_hash does not match the preceding row")
        if row["content_hash"] != expected:
            problems.append(f"{spec.table}#{rid}: content_hash mismatch (tampered)")
        previous = str(row["content_hash"])
    return problems


def record_anchor(session: Session, spec: ChainSpec, anchor_date: dt.date) -> str | None:
    """Anchor the current chain head for ``anchor_date``; returns the anchor hash or None."""
    row = session.execute(
        text(
            f"SELECT {spec.order_column} AS rid, content_hash FROM {spec.table} "
            f"ORDER BY {spec.order_column} DESC LIMIT 1"
        )
    ).first()
    if row is None:
        return None
    anchor_hash = sha256_hex(
        canonical_json(
            {
                "chain": spec.chain_name,
                "anchor_date": anchor_date.isoformat(),
                "last_row_id": int(row[0]),
                "last_hash": str(row[1]),
            }
        )
    )
    session.execute(
        text(
            "INSERT INTO audit_hash_anchors "
            "(chain, anchor_date, last_row_id, last_hash, anchor_hash) "
            "VALUES (:c, :d, :r, :h, :a)"
        ),
        {
            "c": spec.chain_name,
            "d": anchor_date,
            "r": int(row[0]),
            "h": str(row[1]),
            "a": anchor_hash,
        },
    )
    return anchor_hash


def verify_anchors(session: Session, spec: ChainSpec) -> list[str]:
    """Every anchor must match the hash currently stored at its anchored row (and recomputed)."""
    problems: list[str] = []
    anchors = session.execute(
        text(
            "SELECT anchor_date, last_row_id, last_hash, anchor_hash FROM audit_hash_anchors "
            "WHERE chain = :c ORDER BY anchor_date"
        ),
        {"c": spec.chain_name},
    ).all()
    if not anchors:
        return problems
    rows = {
        int(r[spec.order_column]): dict(r)
        for r in session.execute(
            text(f"SELECT * FROM {spec.table} ORDER BY {spec.order_column} ASC")  # noqa: S608
        ).mappings()
    }
    # recompute the chain up to each anchored row
    recomputed: dict[int, str] = {}
    previous: str | None = None
    for rid in sorted(rows):
        previous = chain_hash(hashed_row_fields(spec, rows[rid]), previous)
        recomputed[rid] = previous
    for anchor_date, last_row_id, last_hash_value, anchor_hash in anchors:
        stored = rows.get(int(last_row_id), {}).get("content_hash")
        if stored != last_hash_value:
            problems.append(f"anchor {anchor_date}: stored hash at row {last_row_id} differs")
        if recomputed.get(int(last_row_id)) != last_hash_value:
            problems.append(f"anchor {anchor_date}: recomputed chain differs at row {last_row_id}")
        expected_anchor = sha256_hex(
            canonical_json(
                {
                    "chain": spec.chain_name,
                    "anchor_date": anchor_date.isoformat(),
                    "last_row_id": int(last_row_id),
                    "last_hash": str(last_hash_value),
                }
            )
        )
        if expected_anchor != anchor_hash:
            problems.append(f"anchor {anchor_date}: anchor_hash mismatch")
    return problems
