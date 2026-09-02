"""Append-only work item receipts (development plan §7B.1; table ``work_item_receipts``).

Exactly one ``result`` receipt can exist per work item: the partial unique index
``work_item_receipts_one_result_idx`` makes a second insert impossible, which is how results are
accepted exactly once even under concurrent submissions (V-P1-29, CS-09).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

RECEIPT_KINDS = frozenset(
    {"delivery", "ack", "accept", "reject", "result", "duplicate_result", "cancel_ack"}
)


@dataclass(frozen=True)
class Receipt:
    receipt_id: int
    work_item_id: str
    receipt_kind: str
    delivery_no: int | None
    result_ref: str | None
    result_sha256: str | None


class DuplicateResultError(Exception):
    """A ``result`` receipt already exists for the work item."""


def record_receipt(
    session: Session,
    work_item_id: str,
    receipt_kind: str,
    *,
    delivery_no: int | None = None,
    result_ref: str | None = None,
    result_sha256: str | None = None,
    usage: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> Receipt:
    """Insert one receipt row; a second ``result`` receipt raises ``DuplicateResultError``."""
    if receipt_kind not in RECEIPT_KINDS:
        raise ValueError(f"unknown receipt kind {receipt_kind}")
    params = {
        "w": work_item_id,
        "k": receipt_kind,
        "d": delivery_no,
        "r": result_ref,
        "s": result_sha256,
        "u": json.dumps(usage) if usage is not None else None,
        "x": json.dumps(detail or {}),
    }
    stmt = text(
        "INSERT INTO work_item_receipts (work_item_id, receipt_kind, delivery_no, result_ref, "
        "result_sha256, usage, detail) VALUES (:w, :k, :d, :r, :s, CAST(:u AS jsonb), "
        "CAST(:x AS jsonb)) RETURNING id"
    )
    try:
        with session.begin_nested():
            receipt_id = session.execute(stmt, params).scalar_one()
    except IntegrityError as exc:
        if receipt_kind == "result":
            raise DuplicateResultError(work_item_id) from exc
        raise
    return Receipt(
        int(receipt_id), work_item_id, receipt_kind, delivery_no, result_ref, result_sha256
    )


def result_receipt_of(session: Session, work_item_id: str) -> Receipt | None:
    row = session.execute(
        text(
            "SELECT id, delivery_no, result_ref, result_sha256 FROM work_item_receipts "
            "WHERE work_item_id = :w AND receipt_kind = 'result'"
        ),
        {"w": work_item_id},
    ).first()
    if row is None:
        return None
    return Receipt(int(row[0]), work_item_id, "result", row[1], row[2], row[3])


def receipts_of(session: Session, work_item_id: str) -> list[Receipt]:
    rows = session.execute(
        text(
            "SELECT id, receipt_kind, delivery_no, result_ref, result_sha256 "
            "FROM work_item_receipts WHERE work_item_id = :w ORDER BY id"
        ),
        {"w": work_item_id},
    ).all()
    return [Receipt(int(r[0]), work_item_id, str(r[1]), r[2], r[3], r[4]) for r in rows]
