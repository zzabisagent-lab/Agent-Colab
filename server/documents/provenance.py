"""Provenance linking of the documentation pipeline (development plan §10.1 ``LINK_PROVENANCE``).

Every source a version was built from is recorded once with the content hash it had at freeze
time, so V-P6-14 can prove that no link is broken or missing: re-resolving each reference must
find the row *and* the same checksum. A reference whose source disappeared or changed is reported
as unresolved rather than silently dropped.

Reference types follow the citation syntax of §10.4: ``evt`` Events, ``art`` Artifacts, ``dec``
Decisions, ``vr`` VerificationRuns, ``run`` Schedule Runs, ``msg`` Messages.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

REF_TYPES = ("evt", "art", "dec", "vr", "run", "msg")
CITATION = re.compile(r"\[\[(evt|art|dec|vr|run|msg):([^\]\s]+)\]\]")

# ref_type -> (table, id column, checksum expression). A table that a phase has not created yet
# resolves to "missing", never to a crash.
_RESOLVERS: dict[str, tuple[str, str, str]] = {
    "evt": ("events", "event_id", "content_hash"),
    "art": ("artifacts", "artifact_id", "sha256"),
    "vr": ("verification_runs", "verification_id", "snapshot_hash"),
    "run": ("schedule_runs", "run_id", "version_hash"),
    "msg": ("messages", "message_id", "coalesce(body_key_ref, message_id)"),
    # a Decision keeps no second copy of its content, so its checksum is the hash of the Event
    # that recorded it; the subquery keeps the existence check on one real table (P6-09)
    "dec": (
        "brainstorm_decisions",
        "decision_id",
        "(SELECT e.content_hash FROM events e WHERE e.event_id = brainstorm_decisions.event_id)",
    ),
}


@dataclass(frozen=True)
class Ref:
    ref_type: str
    ref_id: str
    checksum: str


@dataclass(frozen=True)
class Unresolved:
    ref_type: str
    ref_id: str
    reason: str  # MISSING | CHECKSUM_CHANGED


def citations_in(markdown: str) -> list[tuple[str, str]]:
    """Every ``[[type:id]]`` citation in document order, de-duplicated."""
    seen: list[tuple[str, str]] = []
    for kind, ref_id in CITATION.findall(markdown):
        if (kind, ref_id) not in seen:
            seen.append((kind, ref_id))
    return seen


def _table_exists(session: Session, table: str) -> bool:
    """A table a later phase will create is "missing", never an error.

    This is a plain lookup rather than a try/except around the query: rolling a caller's
    transaction back to recover from a missing table would discard the work that produced the
    document in the first place.
    """
    return bool(
        session.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{table}"}).scalar_one()
    )


def _checksum(session: Session, ref_type: str, ref_id: str) -> str | None:
    table, id_col, checksum_expr = _RESOLVERS[ref_type]
    if not _table_exists(session, table):
        return None
    row = session.execute(
        text(f"SELECT {checksum_expr} FROM {table} WHERE {id_col} = :i"),  # noqa: S608
        {"i": ref_id},
    ).first()
    return None if row is None else str(row[0])


def resolve(session: Session, refs: Iterable[tuple[str, str]]) -> tuple[list[Ref], list[str]]:
    """Look up the current checksum of every reference; returns (resolved, missing ids)."""
    found: list[Ref] = []
    missing: list[str] = []
    for ref_type, ref_id in refs:
        if ref_type not in _RESOLVERS:
            missing.append(f"{ref_type}:{ref_id}")
            continue
        checksum = _checksum(session, ref_type, ref_id)
        if checksum is None:
            missing.append(f"{ref_type}:{ref_id}")
        else:
            found.append(Ref(ref_type, ref_id, checksum))
    return found, missing


def from_manifest(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """The references a built manifest claims, in a stable order."""
    prov = manifest.get("provenance", {}) or {}
    out: list[tuple[str, str]] = []
    for key, kind in (
        ("event_ids", "evt"),
        ("artifact_ids", "art"),
        ("verification_ids", "vr"),
        ("decision_ids", "dec"),
        ("message_ids", "msg"),
    ):
        for ref_id in prov.get(key, []) or []:
            out.append((kind, str(ref_id)))
    run_id = prov.get("schedule_run_id")
    if run_id:
        out.append(("run", str(run_id)))
    for ref_id in prov.get("schedule_run_ids", []) or []:
        out.append(("run", str(ref_id)))
    return out


def record(session: Session, document_id: str, version: int, refs: Iterable[Ref]) -> int:
    written = 0
    for ref in refs:
        session.execute(
            text(
                "INSERT INTO document_provenance (document_id, version, ref_type, ref_id, "
                "checksum, resolved) VALUES (:d, :v, :t, :i, :c, true) "
                "ON CONFLICT (document_id, version, ref_type, ref_id) DO NOTHING"
            ),
            {"d": document_id, "v": version, "t": ref.ref_type, "i": ref.ref_id, "c": ref.checksum},
        )
        written += 1
    return written


def stored(session: Session, document_id: str, version: int) -> list[Ref]:
    rows = session.execute(
        text(
            "SELECT ref_type, ref_id, checksum FROM document_provenance "
            "WHERE document_id = :d AND version = :v ORDER BY ref_type, ref_id"
        ),
        {"d": document_id, "v": version},
    ).all()
    return [Ref(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def verify(session: Session, document_id: str, version: int) -> list[Unresolved]:
    """Re-resolve every recorded reference (V-P6-14): empty result means zero broken links."""
    problems: list[Unresolved] = []
    for ref in stored(session, document_id, version):
        current = _checksum(session, ref.ref_type, ref.ref_id)
        if current is None:
            problems.append(Unresolved(ref.ref_type, ref.ref_id, "MISSING"))
        elif current != ref.checksum:
            problems.append(Unresolved(ref.ref_type, ref.ref_id, "CHECKSUM_CHANGED"))
    return problems


def manifest_hash(source_manifest: dict[str, Any]) -> str:
    """Stable hash of the frozen source id set (used as the freeze fingerprint)."""
    from server.events.canonical import canonical_json

    return hashlib.sha256(canonical_json(source_manifest)).hexdigest()


__all__ = [
    "CITATION",
    "REF_TYPES",
    "Ref",
    "Unresolved",
    "citations_in",
    "from_manifest",
    "manifest_hash",
    "record",
    "resolve",
    "stored",
    "verify",
]
