"""P6-08 recurring summaries and the Schedule Run document (V-P6-09).

A terminal Run gets a document carrying its status, the Task it created, its Artifacts and its
limitations; a Schedule with a period policy gets one summary document per closed window.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application.documents import on_schedule_period_closed, on_schedule_run_terminal
from server.db.engine import make_engine
from server.documents import sources, summaries
from server.documents.templates import SECTION_KEYS, heading_for
from tests.integration.document_seed import T0, DocSeed

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> DocSeed:
    sd = DocSeed("dper", tmp_path_factory.mktemp("docs"))
    sd.create(engine)
    return sd


def _headings(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]


def test_terminal_run_document_carries_status_task_and_limitations(
    engine: Engine, seed: DocSeed
) -> None:
    """V-P6-09, Run half."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "per1", "Run task")
        run_id = seed.schedule_run(
            s, "p1", status="FAILED", task_id=task_id, error_code="RETRY_EXHAUSTED"
        )
        result = on_schedule_run_terminal(seed.ctx(s, seed.admin, "per1-doc"), run_id)
    assert result is not None
    document_id = sources.document_id_for("schedule_run", run_id)
    markdown, manifest = seed.store.read_version(str(seed.ws), document_id, result.version)
    assert _headings(markdown) == [heading_for(k, None) for k in SECTION_KEYS]
    assert manifest["doc_type"] == "schedule_run"
    assert "Terminal status: FAILED" in markdown
    assert f"Task `{task_id}`" in markdown
    # limitations are explicit, not implied
    assert "RETRY_EXHAUSTED" in markdown
    assert any("not SUCCEEDED" in line for line in manifest["limitations"])
    assert f"[[run:{run_id}]]" in markdown
    with Session(engine) as s:
        freeze = s.execute(
            text("SELECT subject_type, document_id FROM document_freezes WHERE freeze_id = :f"),
            {"f": result.freeze_id},
        ).one()
    assert freeze[0] == "schedule_run" and freeze[1] == document_id


def test_run_document_is_idempotent_for_the_same_sources(engine: Engine, seed: DocSeed) -> None:
    with Session(engine) as s, s.begin():
        run_id = seed.schedule_run(s, "p2", status="SUCCEEDED")
        first = on_schedule_run_terminal(seed.ctx(s, seed.admin, "per2-a"), run_id)
    with Session(engine) as s, s.begin():
        second = on_schedule_run_terminal(seed.ctx(s, seed.admin, "per2-b"), run_id)
    assert first is not None and second is not None
    assert second.version == first.version and second.replayed
    assert second.sha256 == first.sha256


def test_period_summary_covers_every_run_in_the_window(engine: Engine, seed: DocSeed) -> None:
    """V-P6-09, period half: one document per closed window, listing each Run and the failures."""
    start = T0.replace(hour=0, minute=0)
    end = start + dt.timedelta(days=1)
    with Session(engine) as s, s.begin():
        ok_run = seed.schedule_run(s, "p3", status="SUCCEEDED")
        schedule_id = f"sch-{seed.tag}-p3"
        # a second Run of the same Schedule in the same window, this one failed
        s.execute(
            text(
                "INSERT INTO schedule_runs (id, run_id, workspace_id, schedule_id, "
                "schedule_version_id, run_kind, occurrence_key, scheduled_for, status, "
                "attempt_count, idempotency_key, version_hash, started_at, finished_at, "
                "error_code) SELECT gen_random_uuid(), :r, workspace_id, schedule_id, "
                "schedule_version_id, 'SCHEDULED', :o, :at, 'FAILED', 1, :k, version_hash, "
                ":at, :at, 'MAX_DURATION_EXCEEDED' FROM schedule_runs WHERE run_id = :src"
            ),
            {
                "r": f"{ok_run}-b",
                "o": "occ-period-b",
                "at": start + dt.timedelta(hours=3),
                "k": "idem-period-b",
                "src": ok_run,
            },
        )
        result = on_schedule_period_closed(
            seed.ctx(s, seed.admin, "per3-doc"),
            schedule_id,
            period="daily",
            start=start,
            end=end,
        )
    assert result is not None
    document_id = sources.document_id_for("schedule_period", result.subject_id)
    markdown, manifest = seed.store.read_version(str(seed.ws), document_id, result.version)
    assert manifest["doc_type"] == "period"
    assert _headings(markdown) == [heading_for(k, None) for k in SECTION_KEYS]
    assert f"[[run:{ok_run}]]" in markdown and f"[[run:{ok_run}-b]]" in markdown
    assert "SUCCEEDED: 1 Run(s)" in markdown and "FAILED: 1 Run(s)" in markdown
    assert any("did not succeed" in line for line in manifest["limitations"])
    assert "MAX_DURATION_EXCEEDED" in markdown
    assert result.unresolved == []


def test_period_windows_are_closed_and_utc() -> None:
    moment = dt.datetime(2026, 5, 4, 12, 30, tzinfo=dt.UTC)  # a Monday
    daily = summaries.window_for("daily", moment)
    assert daily.start == dt.datetime(2026, 5, 3, tzinfo=dt.UTC)
    assert daily.end == dt.datetime(2026, 5, 4, tzinfo=dt.UTC)
    weekly = summaries.window_for("weekly", moment)
    assert weekly.start == dt.datetime(2026, 4, 27, tzinfo=dt.UTC)
    assert weekly.end == dt.datetime(2026, 5, 4, tzinfo=dt.UTC)
    monthly = summaries.window_for("monthly", moment)
    assert monthly.start == dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    assert monthly.end == dt.datetime(2026, 5, 1, tzinfo=dt.UTC)
    assert summaries.window_for("daily", moment).period == "daily"


def test_schedules_asking_for_a_period_summary_are_listed(engine: Engine, seed: DocSeed) -> None:
    with Session(engine) as s, s.begin():
        seed.schedule_run(s, "p4", status="SUCCEEDED")
    with Session(engine) as s:
        due = summaries.due_schedules(s, str(seed.ws))
    assert any(d["schedule_id"] == f"sch-{seed.tag}-p4" for d in due)
    assert {d["period"] for d in due} == {"daily"}
