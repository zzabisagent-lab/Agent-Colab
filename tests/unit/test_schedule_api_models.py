"""P5-01/P5-02 pure checks: REST body models, version snapshot hashing, next-run reference
fixtures (V-P5-02), DOM/DOW OR semantics (V-P5-29) and timezone handling (V-P5-03)."""

from __future__ import annotations

import datetime as dt
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from server.api.v1.schedules import CreateBody, PreviewBody
from server.application.schedules import DEFAULT_BUDGET_POLICY, DEFAULT_RETRY_POLICY
from server.schedules import cron
from server.schedules import store as st

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "schedule"


def _load(name: str) -> Any:
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)


def _content(**over: Any) -> dict[str, Any]:
    base = {
        "name": "Daily digest",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Seoul",
        "channel_id": "44444444-4444-4444-8444-444444444444",
        "execution_principal_id": "11111111-1111-4111-8111-111111111111",
        "agent_selection": {"mode": "capability", "required_capabilities": ["research.summarize"]},
        "action_template": {
            "schema_id": "action-template.v1",
            "action": "task_create",
            "input": {"title": "Digest", "domain": "research", "risk": "LOW"},
        },
        "concurrency_policy": "FORBID",
        "missed_run_policy": "RUN_ONCE",
        "backfill_limit": 0,
        "backfill_window_seconds": 0,
        "max_duration_seconds": 3600,
        "min_interval_minutes": 5,
        "retry_policy": dict(DEFAULT_RETRY_POLICY),
        "budget_policy": dict(DEFAULT_BUDGET_POLICY),
        "documentation_policy": {"draft": True},
        "starts_at": None,
        "ends_at": None,
    }
    base.update(over)
    return base


def test_snapshot_hash_is_canonical_and_content_addressed() -> None:
    a = st.snapshot_hash(_content())
    b = st.snapshot_hash(_content())
    assert a == b and len(a) == 64
    # key order and untracked extras never change the hash; a tracked field does
    reordered = dict(reversed(list(_content().items())))
    reordered["ignored_extra"] = "x"
    assert st.snapshot_hash(reordered) == a
    assert st.snapshot_hash(_content(cron_expression="0 10 * * 1-5")) != a


def test_create_body_rejects_invalid_policies() -> None:
    body = CreateBody(
        name="n",
        cron_expression="0 9 * * *",
        timezone="UTC",
        channel_id="chan-1",
        execution_principal_id="acct-1",
        agent_selection={"mode": "fixed", "agent_id": "agent-a"},
        action_template={"schema_id": "action-template.v1", "action": "task_create", "input": {}},
    )
    assert body.concurrency_policy == "FORBID" and body.missed_run_policy == "RUN_ONCE"
    for field, value in (
        ("concurrency_policy", "SOMETIMES"),
        ("missed_run_policy", "ALWAYS"),
        ("backfill_limit", -1),
        ("max_duration_seconds", 90_000),
        ("min_interval_minutes", 0),
    ):
        with pytest.raises(ValidationError):
            CreateBody(**{**body.model_dump(), field: value})
    with pytest.raises(ValidationError):
        PreviewBody(count=99)


def test_next_run_reference_fixture_matches_the_implementation() -> None:
    """V-P5-02: 50 expressions x next 10 against the reference computation (UTC)."""
    data = _load("next-run-cases.yaml")
    after = _utc(data["after_utc"])
    horizon_end = after + dt.timedelta(days=cron.PREVIEW_HORIZON_DAYS)
    assert len(data["cases"]) == 50
    for case in data["cases"]:
        ours = [
            o.utc
            for o in cron.next_occurrences(
                case["expr"], data["timezone"], after, count=10, include_gaps=False
            )
        ]
        reference = [_utc(t) for t in case["utc"]]
        inside = [d for d in reference if d <= horizon_end]
        assert ours == inside, case["expr"]
        assert case["horizon_limited"] == (len(inside) < 10)
        assert all(a < b for a, b in pairwise(ours))


def test_dom_dow_or_semantics_from_fixtures() -> None:
    """V-P5-29: with both day fields restricted the union fires (Vixie OR)."""
    after = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    both = cron.parse("0 12 15 * 1")  # the 15th OR any Monday
    fires = [
        o.local.date()
        for o in cron.next_occurrences("0 12 15 * 1", "UTC", after, count=10, include_gaps=False)
    ]
    assert both.dom_restricted and both.dow_restricted
    assert all(d.day == 15 or d.weekday() == 0 for d in fires)
    assert any(d.day == 15 and d.weekday() != 0 for d in fires)
    assert any(d.weekday() == 0 and d.day != 15 for d in fires)
    only_dom = cron.parse("0 12 15 * *")
    assert only_dom.dom_restricted and not only_dom.dow_restricted


def test_timezones_are_independent_of_the_server_zone() -> None:
    """V-P5-03: the same wall-clock rule maps to different UTC instants per IANA timezone."""
    after = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    seoul = cron.next_occurrences("0 9 * * *", "Asia/Seoul", after, count=1, include_gaps=False)[0]
    ny = cron.next_occurrences("0 9 * * *", "America/New_York", after, count=1, include_gaps=False)[
        0
    ]
    assert seoul.local.hour == ny.local.hour == 9
    assert seoul.utc is not None and ny.utc is not None
    assert seoul.utc.hour == 0 and ny.utc.hour == 13  # KST +9, EDT -4
    assert seoul.occurrence_key != ny.occurrence_key  # the timezone is part of the key


def test_run_view_serializes_timestamps_in_the_contract_form() -> None:
    when = dt.datetime(2026, 3, 1, 9, 0, 0, 123456, tzinfo=dt.UTC)
    assert st.iso_ms(when) == "2026-03-01T09:00:00.123Z"
    assert st.iso_ms(None) is None
    assert st.parse_ts("2026-03-01T09:00:00.123Z") == when.replace(microsecond=123000)
    assert json.dumps({"t": st.iso_ms(when)})  # JSON-safe
