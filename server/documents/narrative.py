"""Narrative layer of the documentation pipeline (development plan §10.4 layer 2, P6-10).

Layer 1 is authoritative. A Documentation Agent holding the ``document.narrate`` capability may
add prose to *Discussion, Alternatives, Decisions and Rationale* and *Shortcomings, Risks and Open
Questions*; every paragraph must cite a frozen source and may not contradict a skeleton figure
(:mod:`server.documents.citations`). Generation cost is recorded in ``usage_records`` under the
``document_id`` scope (§10.4).

If no Agent is available, the Agent declines, or the narrative fails the linter, the
skeleton-only document stays a valid draft and the reason is recorded — the pipeline never blocks
on layer 2 and never lets an unlinted paragraph into a document.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.documents.citations import LintResult, lint, skeleton_facts

NARRATE_CAPABILITY = "document.narrate"
REASON_NO_AGENT = "NARRATIVE_NO_AGENT_AVAILABLE"
REASON_DECLINED = "NARRATIVE_AGENT_DECLINED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_DECLINED = "DECLINED"
STATUS_UNAVAILABLE = "UNAVAILABLE"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NarrativeAgent:
    agent_id: str
    account_uuid: str


@dataclass(frozen=True)
class NarrativeRequest:
    document_id: str
    version: int
    subject_type: str
    subject_id: str
    skeleton_markdown: str
    manifest: Mapping[str, Any]
    known_refs: list[tuple[str, str]]


@dataclass(frozen=True)
class NarrativeDraft:
    body: str
    usage: dict[str, Any] | None = None  # §7C usage of the generation, when the Agent reports it


class NarrativeProvider(Protocol):
    """How the server asks a Documentation Agent for prose. Returning ``None`` is a decline."""

    def narrate(
        self, agent: NarrativeAgent, request: NarrativeRequest
    ) -> NarrativeDraft | None: ...


@dataclass(frozen=True)
class NarrativeOutcome:
    status: str
    body: str = ""
    reason_code: str | None = None
    agent: NarrativeAgent | None = None
    citations: list[tuple[str, str]] | None = None
    lint: LintResult | None = None

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED


_provider: NarrativeProvider | None = None


def set_provider(provider: NarrativeProvider | None) -> None:
    """Install the Documentation Agent transport (none by default: skeleton-only documents)."""
    global _provider
    _provider = provider


def select_agent(session: Session, workspace_id: str) -> NarrativeAgent | None:
    """An active, online Agent holding ``document.narrate``; ties break by ascending agent_id."""
    row = session.execute(
        text(
            "SELECT a.agent_id, a.account_id::text FROM agents a "
            "JOIN agent_capabilities ac ON ac.agent_id = a.agent_id "
            "JOIN capabilities c ON c.capability_id = ac.capability_id "
            "WHERE a.workspace_id = CAST(:w AS uuid) AND a.status = 'active' AND a.online "
            "AND c.tool = :cap ORDER BY a.agent_id LIMIT 1"
        ),
        {"w": workspace_id, "cap": NARRATE_CAPABILITY},
    ).first()
    return None if row is None else NarrativeAgent(str(row[0]), str(row[1]))


def _store(
    session: Session,
    document_id: str,
    version: int,
    outcome: NarrativeOutcome,
) -> None:
    import json

    session.execute(
        text(
            "INSERT INTO document_narratives (document_id, version, author_account_id, status, "
            "body, citations, accepted, reason_code) VALUES (:d, :v, CAST(:a AS uuid), :s, :b, "
            "CAST(:c AS jsonb), :ok, :r) ON CONFLICT (document_id, version) DO NOTHING"
        ),
        {
            "d": document_id,
            "v": version,
            "a": outcome.agent.account_uuid if outcome.agent else None,
            "s": outcome.status,
            "b": outcome.body,
            "c": json.dumps([f"{t}:{i}" for t, i in (outcome.citations or [])]),
            "ok": outcome.accepted,
            "r": outcome.reason_code,
        },
    )


def generate(
    session: Session,
    request: NarrativeRequest,
    *,
    workspace_id: str,
    provider: NarrativeProvider | None = None,
    clock: Any = None,
) -> NarrativeOutcome:
    """Ask a Documentation Agent for prose, lint it and record the outcome (never raises)."""
    active = provider or _provider
    if active is None:
        outcome = NarrativeOutcome(STATUS_UNAVAILABLE, reason_code=REASON_NO_AGENT)
        _store(session, request.document_id, request.version, outcome)
        return outcome
    agent = select_agent(session, workspace_id)
    if agent is None:
        outcome = NarrativeOutcome(STATUS_UNAVAILABLE, reason_code=REASON_NO_AGENT)
        _store(session, request.document_id, request.version, outcome)
        return outcome
    try:
        draft = active.narrate(agent, request)
    except Exception as exc:  # an Agent failure is a decline, never a pipeline failure
        outcome = NarrativeOutcome(STATUS_DECLINED, reason_code=REASON_DECLINED, agent=agent)
        _store(session, request.document_id, request.version, outcome)
        del exc
        return outcome
    if draft is None or not draft.body.strip():
        outcome = NarrativeOutcome(STATUS_DECLINED, reason_code=REASON_DECLINED, agent=agent)
        _store(session, request.document_id, request.version, outcome)
        return outcome
    result = lint(draft.body, known_refs=request.known_refs, facts=skeleton_facts(request.manifest))
    if draft.usage is not None:
        _record_usage(session, request, agent, draft, workspace_id=workspace_id, clock=clock)
    if not result.ok:
        outcome = NarrativeOutcome(
            STATUS_REJECTED,
            body="",
            reason_code=result.reason_code,
            agent=agent,
            citations=result.citations,
            lint=result,
        )
        _store(session, request.document_id, request.version, outcome)
        return outcome
    outcome = NarrativeOutcome(
        STATUS_ACCEPTED,
        body=draft.body.strip(),
        agent=agent,
        citations=result.citations,
        lint=result,
    )
    _store(session, request.document_id, request.version, outcome)
    return outcome


def _record_usage(
    session: Session,
    request: NarrativeRequest,
    agent: NarrativeAgent,
    draft: NarrativeDraft,
    *,
    workspace_id: str,
    clock: Any,
) -> None:
    """Generation cost belongs to the document scope (§10.4); a pricing gap is not fatal."""
    from server.usage.records import record_usage

    try:  # a savepoint, so a pricing gap cannot discard the caller's document writes
        with session.begin_nested():
            record_usage(
                session,
                workspace_id=workspace_id,
                account_id=agent.account_uuid,
                agent_id=agent.agent_id,
                work_item_id=None,
                usage=draft.usage,
                document_id=request.document_id,
                task_id=request.subject_id if request.subject_type == "task" else None,
                clock=clock,
            )
    except Exception:  # no activated pricing version in this environment
        log.warning("narrative usage not recorded for %s", request.document_id)


def stored(session: Session, document_id: str, version: int) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT status, body, citations, accepted, reason_code FROM document_narratives "
                "WHERE document_id = :d AND version = :v"
            ),
            {"d": document_id, "v": version},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


__all__ = [
    "NARRATE_CAPABILITY",
    "REASON_DECLINED",
    "REASON_NO_AGENT",
    "STATUS_ACCEPTED",
    "STATUS_DECLINED",
    "STATUS_REJECTED",
    "STATUS_UNAVAILABLE",
    "NarrativeAgent",
    "NarrativeDraft",
    "NarrativeOutcome",
    "NarrativeProvider",
    "NarrativeRequest",
    "generate",
    "select_agent",
    "set_provider",
    "stored",
]
