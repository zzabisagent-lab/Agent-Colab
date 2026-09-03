"""P6-05 provenance and process accuracy.

V-P6-10 the document's Process section reproduces the Event sequence with no omissions or
distortions; V-P6-11 every resource field is a value or a standard ``UNAVAILABLE_<REASON>``;
V-P6-14 every source link resolves with the checksum it had at freeze time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application.documents import on_implementation_submitted, on_verification_terminal
from server.db.engine import make_engine
from server.documents import finalizer, provenance
from server.documents.builder import document_id_for_task
from server.documents.lifecycle import DocumentActor
from tests.integration.document_seed import DocSeed

pytestmark = pytest.mark.db
UNAVAILABLE = re.compile(r"^UNAVAILABLE_[A-Z_]+$")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> DocSeed:
    sd = DocSeed("dprov", tmp_path_factory.mktemp("docs"))
    sd.create(engine)
    return sd


def _actor(seed: DocSeed, key: str) -> DocumentActor:
    return DocumentActor(str(seed.accounts[seed.admin]), f"corr-{seed.tag}", key)


def test_process_section_reproduces_every_event(engine: Engine, seed: DocSeed) -> None:
    """V-P6-10: no Event of the frozen window is omitted and none is invented."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "prov1", "Provenance task")
        vid = seed.verification(s, task_id, "prov1")
        seed.verdict(s, vid, "PASSED", "prov1-verdict")
        result = on_verification_terminal(seed.ctx(s, seed.admin, "prov1-final"), task_id, vid)
    document_id = document_id_for_task(task_id)
    markdown, manifest = seed.store.read_version(
        str(seed.ws), document_id, int(result.data["version"])
    )
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT event_id, type, recorded_seq FROM events WHERE (task_id = :t OR "
                "(aggregate_type = 'task' AND aggregate_id = :t)) AND recorded_seq <= :seq "
                "ORDER BY recorded_seq"
            ),
            {"t": task_id, "seq": manifest["source_freeze_event_seq"]},
        ).all()
    process = markdown.split("## Process and Key Events")[1].split("## ")[0]
    cited = re.findall(r"\[\[evt:([^\]]+)\]\]", process)
    order = {str(r[0]): int(r[2]) for r in rows}
    # zero omissions: every Event of the frozen window is rendered, with its own type
    for event_id, event_type, _seq in rows:
        assert f"`{event_type}` [[evt:{event_id}]]" in process, event_type
    # zero distortions: nothing invented, and the rendered order is the recorded order
    assert set(cited) <= set(order)
    assert [order[c] for c in cited] == sorted(order[c] for c in cited)
    frozen = set(manifest["provenance"]["event_ids"])
    assert set(re.findall(r"\[\[evt:([^\]]+)\]\]", markdown)) <= frozen


def test_every_resource_field_has_a_value_or_a_reason(engine: Engine, seed: DocSeed) -> None:
    """V-P6-11: no resource field is silently missing."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "prov2", "Resource task")
        result = on_implementation_submitted(seed.ctx(s, seed.admin, "prov2-draft"), task_id)
    document_id = document_id_for_task(task_id)
    _, manifest = seed.store.read_version(str(seed.ws), document_id, int(result.data["version"]))
    resources = manifest["resources"]
    expected = {
        "agents",
        "models",
        "tools",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "wall_ms",
        "cost_units",
        "artifacts",
        "sources",
    }
    assert expected <= set(resources)
    for key, value in resources.items():
        if isinstance(value, str):
            assert UNAVAILABLE.match(value), f"{key} is neither a value nor a standard reason"
        else:
            assert value is not None and value != [], key


def test_provenance_links_resolve_with_their_checksums(engine: Engine, seed: DocSeed) -> None:
    """V-P6-14: every recorded reference still resolves to the same content hash."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "prov3", "Link task")
        result = finalizer.draft_task(
            s,
            seed.ctx(s, seed.admin, "prov3-draft").store,
            seed.store,
            task_id=task_id,
            actor=_actor(seed, "prov3-draft"),
            now=seed.clock.now(),
        )
    assert result.unresolved == [], "a freshly built document has no broken links"
    assert result.provenance_refs > 0
    with Session(engine) as s:
        stored = provenance.stored(s, result.document_id, result.version)
        assert stored, "provenance rows are recorded for the version"
        assert provenance.verify(s, result.document_id, result.version) == []
        # every recorded checksum is the source's real content hash
        for ref in stored:
            if ref.ref_type == "evt":
                content_hash = s.execute(
                    text("SELECT content_hash FROM events WHERE event_id = :i"), {"i": ref.ref_id}
                ).scalar_one()
                assert ref.checksum == content_hash
    # a reference whose source changed is reported rather than silently accepted
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "UPDATE document_provenance SET checksum = :c WHERE document_id = :d "
                "AND version = :v AND ref_type = 'evt'"
            ),
            {"c": "0" * 64, "d": result.document_id, "v": result.version},
        )
    with Session(engine) as s:
        problems = provenance.verify(s, result.document_id, result.version)
    assert problems and {p.reason for p in problems} == {"CHECKSUM_CHANGED"}
