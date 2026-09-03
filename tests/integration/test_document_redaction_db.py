"""P6-05 redaction (V-P6-13): a canary reaching a document source never survives into the
canonical bytes, the manifest, the stored file or a log line; only per-rule counts are kept."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bus
from server.application.documents import on_implementation_submitted
from server.application.tasks import ReportProgress
from server.db.engine import make_engine
from server.documents import redaction
from server.documents.builder import document_id_for_task
from server.secrets import canary
from tests.integration.document_seed import DocSeed

pytestmark = pytest.mark.db
CANARY = canary.canary_value(6013)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> DocSeed:
    sd = DocSeed("dred", tmp_path_factory.mktemp("docs"))
    sd.create(engine)
    return sd


def test_canary_in_a_source_never_reaches_the_document(
    engine: Engine, seed: DocSeed, caplog: pytest.LogCaptureFixture
) -> None:
    """A progress report carrying a canary is redacted before the bytes are hashed and stored."""
    canary.register_canary("sec-doc-redaction", 6013)
    try:
        with caplog.at_level(logging.DEBUG), Session(engine) as s, s.begin():
            task_id = seed.implement(s, "red1", "Redaction task", submit=False)
            bus.execute(
                ReportProgress(task_id, f"deployed with token {CANARY} in the config"),
                seed.ctx(s, seed.impl, "red1-progress"),
            )
            seed.submit(s, task_id, "red1-submit")
            result = on_implementation_submitted(seed.ctx(s, seed.admin, "red1-draft"), task_id)
        document_id = document_id_for_task(task_id)
        markdown, manifest = seed.store.read_version(
            str(seed.ws), document_id, int(result.data["version"])
        )
        # the canonical bytes, the manifest and the file on disk are clean
        assert CANARY not in markdown
        assert "[redacted: secret]" in markdown
        assert CANARY not in str(manifest)
        path = seed.store.root / str(seed.ws) / document_id / f"v{result.data['version']}.md"
        assert CANARY not in path.read_text(encoding="utf-8")
        # the source Event still holds it, so the redaction is in the document, not in history
        with Session(engine) as s:
            payloads = s.execute(
                text(
                    "SELECT payload::text FROM events WHERE task_id = :t "
                    "AND type = 'TASK_PROGRESS_REPORTED'"
                ),
                {"t": task_id},
            ).all()
        assert any(CANARY in str(p[0]) for p in payloads)
        # only counts and a salted sample hash are recorded, never the value
        counts = manifest["redactions"]
        assert [c["rule"] for c in counts] == ["canary"] and counts[0]["count"] >= 1
        assert CANARY not in str(counts)
        with Session(engine) as s:
            rows = redaction.counts_for(s, document_id, int(result.data["version"]))
        assert [r.rule for r in rows] == ["canary"]
        assert rows[0].sample_hash == counts[0]["sample_hash"]
        # V-P6-13: the canary scanner finds nothing in the document surfaces
        with Session(engine) as s:
            hits = canary.scan(
                s,
                seed.ws,
                documents=[(document_id, markdown)],
                document_root=seed.store.root,
                log_lines=[r.getMessage() for r in caplog.records],
            )
        document_hits = [h for h in hits if h.location.startswith(("document:", "file:", "log:"))]
        assert document_hits == []
    finally:
        canary.clear_registry()


def test_redaction_is_deterministic_and_rule_ordered() -> None:
    """The pass is a pure transform: the same input always yields the same bytes and counts."""
    body = f"contact ops@example.test about {canary.canary_value(1)} and card 4111 1111 1111 1111"
    first, counts_a = redaction.redact(body)
    second, counts_b = redaction.redact(body)
    assert first == second and counts_a == counts_b
    assert canary.canary_value(1) not in first and "ops@example.test" not in first
    assert [c.rule for c in counts_a] == ["canary", "email", "card"]
    # re-running on already redacted text changes nothing further
    assert redaction.redact(first)[0] == first
