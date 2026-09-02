"""Schedule contract fixtures (P0-08 / V-P0-11): cron, timezone/DST, occurrence keys, versions,
status tables, action templates, concurrency, missed runs, retry, cancel."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain import defaults
from server.schedules import contract as c
from server.schedules import cron
from server.schedules.occurrence import (
    manual_idempotency_key,
    occurrence_key,
    retry_idempotency_key,
)
from server.schedules.validate import (
    validate_action_template,
    validate_agent_selection,
    validate_schedule_run,
    validate_schedule_version,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "schedule"


def _load(name: str) -> Any:
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)


CRON = _load("cron-cases.yaml")
DST = _load("dst-cases.yaml")
KEYS = _load("occurrence-key-cases.yaml")
TRANSITIONS = _load("transitions.yaml")
POLICIES = _load("policies.yaml")
TEMPLATES = _load("action-templates.yaml")
VERSION_VALID = json.loads((FIX / "schedule-version.valid.json").read_text(encoding="utf-8"))
VERSION_INVALID = _load("schedule-version.invalid.yaml")["cases"]
RUN_CASES = _load("schedule-run-cases.yaml")


# --- cron grammar -------------------------------------------------------------------------


@pytest.mark.parametrize("case", CRON["valid"], ids=[x["expr"].strip() for x in CRON["valid"]])
def test_cron_valid(case: dict[str, Any]) -> None:
    parsed = cron.parse(case["expr"])
    if "normalized" in case:
        assert parsed.expression == case["normalized"]
    if "minutes" in case:
        assert len(parsed.minutes) == case["minutes"]
    if "hours" in case:
        assert len(parsed.hours) == case["hours"]
    for key, attr in (
        ("minute_set", "minutes"),
        ("hour_set", "hours"),
        ("dom_set", "days_of_month"),
        ("month_set", "months"),
        ("dow_set", "days_of_week"),
    ):
        if key in case:
            assert sorted(getattr(parsed, attr)) == case[key]
    if "dow_count" in case:
        assert len(parsed.days_of_week) == case["dow_count"]
    if "dom_restricted" in case:
        assert parsed.dom_restricted is case["dom_restricted"]
    if "dow_restricted" in case:
        assert parsed.dow_restricted is case["dow_restricted"]
    if "min_interval" in case:
        assert parsed.min_interval_minutes() == case["min_interval"]


@pytest.mark.parametrize("case", CRON["invalid"], ids=[f"{x['expr']!r}" for x in CRON["invalid"]])
def test_cron_invalid_stable_codes(case: dict[str, Any]) -> None:
    with pytest.raises(cron.CronError) as exc:
        if "min_interval" in case:
            cron.validate(case["expr"], case["min_interval"])
        else:
            cron.parse(case["expr"])
    assert exc.value.code == case["code"]


@pytest.mark.parametrize("case", CRON["interval_ok"])
def test_cron_interval_accepts_at_boundary(case: dict[str, Any]) -> None:
    assert cron.validate(case["expr"], case["min_interval"]).expression == case["expr"]


def test_cron_default_minimum_interval_is_five_minutes() -> None:
    assert defaults.SCHEDULE_MIN_INTERVAL_MINUTES_DEFAULT == 5
    assert defaults.SCHEDULE_MIN_INTERVAL_MINUTES_FLOOR == 1
    with pytest.raises(cron.CronError) as exc:
        cron.validate("*/4 * * * *")
    assert exc.value.code == "CRON_INTERVAL_TOO_SHORT"
    assert cron.validate("*/5 * * * *")


@pytest.mark.parametrize(
    "case", CRON["day_semantics"], ids=[x["note"] for x in CRON["day_semantics"]]
)
def test_cron_dom_dow_vixie_or(case: dict[str, Any]) -> None:
    parsed = cron.parse(case["expr"])
    day = dt.date.fromisoformat(case["date"])
    assert parsed.day_matches(day) is case["match"]
    assert parsed.matches(dt.datetime.combine(day, dt.time(0, 0))) is case["match"]


# --- timezone / DST ----------------------------------------------------------------------


@pytest.mark.parametrize("case", DST["cases"], ids=[x["name"] for x in DST["cases"]])
def test_next_occurrences_match_reference(case: dict[str, Any]) -> None:
    got = cron.next_occurrences(
        case["expr"], case["timezone"], _utc(case["after_utc"]), case["count"], "sch-fixture"
    )
    rendered = [
        {
            "local": o.local.strftime("%Y-%m-%dT%H:%M"),
            "utc": o.utc.strftime("%Y-%m-%dT%H:%M:%SZ") if o.utc else None,
            "reason": o.reason,
        }
        for o in got
    ]
    assert rendered == case["expect"]
    executable = [o for o in got if o.executable]
    assert len(executable) <= case["count"]
    for o in executable:
        assert o.utc is not None and o.utc > _utc(case["after_utc"])
        assert o.occurrence_key == occurrence_key("sch-fixture", case["timezone"], o.local)
    gaps = [o for o in got if not o.executable]
    assert all(o.reason == "DST_GAP" for o in gaps)


def test_next_occurrences_independent_of_process_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        got = cron.next_occurrences(
            "30 1 * * *", "America/New_York", _utc("2026-10-31T12:00:00Z"), 1
        )
        assert got[0].utc == _utc("2026-11-01T05:30:00Z") and got[0].reason == "DST_FOLD"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_next_occurrences_exclude_gaps_option_and_naive_rejected() -> None:
    got = cron.next_occurrences(
        "30 2 * * *", "America/New_York", _utc("2026-03-07T12:00:00Z"), 1, include_gaps=False
    )
    assert [o.reason for o in got] == [None]
    with pytest.raises(cron.CronError) as exc:
        cron.next_occurrences("0 0 * * *", "UTC", dt.datetime(2026, 1, 1), 1)
    assert exc.value.code == "TIMESTAMP_NAIVE"


@pytest.mark.parametrize("tz", DST["invalid_timezones"])
def test_invalid_timezones_rejected(tz: str) -> None:
    with pytest.raises(cron.CronError) as exc:
        cron.load_zone(tz)
    assert exc.value.code == "TIMEZONE_INVALID"


# --- occurrence keys -----------------------------------------------------------------------


@pytest.mark.parametrize("case", KEYS["cases"])
def test_occurrence_key_material(case: dict[str, Any]) -> None:
    expected = hashlib.sha256(case["material"].encode()).hexdigest()
    assert occurrence_key(case["schedule_id"], case["timezone"], case["local"]) == expected
    naive = dt.datetime.strptime(case["local"], "%Y-%m-%dT%H:%M")
    assert occurrence_key(case["schedule_id"], case["timezone"], naive) == expected


def test_fold_instants_share_one_key() -> None:
    f = KEYS["fold_same_key"]
    zone = cron.load_zone(f["timezone"])
    locals_seen = {
        _utc(u).astimezone(zone).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
        for u in f["utc_instants"]
    }
    assert locals_seen == {f["local"]}
    keys = {occurrence_key(f["schedule_id"], f["timezone"], loc) for loc in locals_seen}
    assert len(keys) == 1


@pytest.mark.parametrize("case", KEYS["distinct"])
def test_occurrence_keys_distinct(case: dict[str, Any]) -> None:
    assert occurrence_key(*case["a"]) != occurrence_key(*case["b"])


def test_manual_and_retry_keys() -> None:
    for m in KEYS["manual_keys"]:
        assert (
            manual_idempotency_key(m["schedule_id"], m["requester"], m["client_key"]) == m["expect"]
        )
    for r in KEYS["retry_keys"]:
        assert retry_idempotency_key(r["original_run_id"], r["retry_no"]) == r["expect"]
    for bad in KEYS["invalid_manual"]:
        with pytest.raises(ValueError):
            manual_idempotency_key(bad["schedule_id"], bad["requester"], bad["client_key"])
    for bad in KEYS["invalid_retry"]:
        with pytest.raises(ValueError):
            retry_idempotency_key(bad["original_run_id"], bad["retry_no"])
    with pytest.raises(ValueError):
        occurrence_key("sch-1", "UTC", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    with pytest.raises(ValueError):
        occurrence_key("sch-1", "UTC", "2026-01-01 00:00")


# --- status tables -------------------------------------------------------------------------


def test_schedule_transition_table_is_exactly_the_spec() -> None:
    allowed = {(a, b) for a, b in TRANSITIONS["schedule"]["allowed"]}
    table = {(s, t) for s, ts in c.SCHEDULE_TRANSITIONS.items() for t in ts}
    assert table == allowed
    for a, b in TRANSITIONS["schedule"]["allowed"]:
        assert c.schedule_transition(c.ScheduleStatus(a), c.ScheduleStatus(b)) == b
    for a, b in TRANSITIONS["schedule"]["rejected"]:
        with pytest.raises(c.ScheduleContractError) as exc:
            c.schedule_transition(c.ScheduleStatus(a), c.ScheduleStatus(b))
        assert exc.value.code == "SCHEDULE_TRANSITION_INVALID"


def test_run_transitions_and_terminal_set() -> None:
    assert set(c.RUN_TERMINAL) == set(TRANSITIONS["run"]["terminal"])
    for a, b in TRANSITIONS["run"]["allowed"]:
        assert c.run_transition(c.RunStatus(a), c.RunStatus(b)) == b
    for case in TRANSITIONS["run"]["rejected"]:
        with pytest.raises(c.ScheduleContractError) as exc:
            c.run_transition(c.RunStatus(case["from"]), c.RunStatus(case["to"]))
        assert exc.value.code == case["code"]
    for terminal in c.RUN_TERMINAL:
        assert c.RUN_TRANSITIONS[terminal] == frozenset()


@pytest.mark.parametrize(
    "case", TRANSITIONS["cancel"], ids=[x["from"] for x in TRANSITIONS["cancel"]]
)
def test_cancel_rules(case: dict[str, Any]) -> None:
    if "result" in case:
        assert c.cancel_run(c.RunStatus(case["from"])) == case["result"]
    else:
        with pytest.raises(c.ScheduleContractError) as exc:
            c.cancel_run(c.RunStatus(case["from"]))
        assert exc.value.code == case["code"]


@pytest.mark.parametrize("case", TRANSITIONS["run_kind"])
def test_run_kind_invariants(case: dict[str, Any]) -> None:
    kind = c.RunKind(case["kind"])
    if case.get("ok"):
        c.check_run_kind(kind, case["occurrence_key"], case["retry_of_run_id"])
    else:
        with pytest.raises(c.ScheduleContractError) as exc:
            c.check_run_kind(kind, case["occurrence_key"], case["retry_of_run_id"])
        assert exc.value.code == case["code"]


# --- policies ------------------------------------------------------------------------------


def test_policy_defaults_match_spec() -> None:
    d = POLICIES["defaults"]
    assert c.DEFAULT_CONCURRENCY == d["concurrency"]
    assert c.DEFAULT_MISSED_RUN == d["missed_run"]
    policy = c.RetryPolicy()
    assert policy.max_attempts == d["retry_max_attempts"]
    assert list(policy.backoff_seconds) == d["backoff"]
    assert policy.jitter_ratio == d["jitter"]
    assert defaults.SCHEDULE_REPLACE_CANCEL_TIMEOUT_S == d["replace_timeout_s"]


@pytest.mark.parametrize("case", POLICIES["concurrency"])
def test_concurrency_decisions(case: dict[str, Any]) -> None:
    out = c.decide_concurrency(
        c.ConcurrencyPolicy(case["policy"]), case["previous_active"], case.get("confirmed")
    )
    assert out.decision == case["decision"]
    assert out.error_code == case.get("error_code")


@pytest.mark.parametrize("case", POLICIES["replace_confirmation"])
def test_replace_confirmation_window(case: dict[str, Any]) -> None:
    confirmed = _utc(case["confirmed"]) if case["confirmed"] else None
    assert c.replace_cancel_confirmed(_utc(case["requested"]), confirmed) is case["ok"]


@pytest.mark.parametrize(
    "case", POLICIES["missed_runs"]["cases"], ids=lambda x: x["policy"] + str(x.get("limit", ""))
)
def test_missed_run_materialization(case: dict[str, Any]) -> None:
    missed = [
        c.MissedOccurrence(
            occurrence_key("sch-1", "UTC", _utc(t).strftime("%Y-%m-%dT%H:%M")), _utc(t)
        )
        for t in reversed(POLICIES["missed_runs"]["missed"])  # unsorted input on purpose
    ]
    plan = c.plan_missed_runs(
        c.MissedRunPolicy(case["policy"]),
        missed,
        _utc(POLICIES["missed_runs"]["now"]),
        case.get("window_seconds", 0),
        case.get("limit", 0),
    )
    created = [m.scheduled_for.strftime("%Y-%m-%dT%H:%M:%SZ") for m in plan.to_create]
    assert created == case["create"]
    assert (plan.warning is not None) is case["warning"]
    assert len(plan.to_create) + len(plan.skipped) == len(missed)
    assert created == sorted(created), "oldest first"


def test_missed_run_negative_backfill_rejected_and_empty_ok() -> None:
    now = _utc("2026-01-15T12:00:00Z")
    assert c.plan_missed_runs(c.MissedRunPolicy.RUN_ONCE, [], now).to_create == ()
    with pytest.raises(c.ScheduleContractError) as exc:
        c.plan_missed_runs(
            c.MissedRunPolicy.BACKFILL_LIMITED, [c.MissedOccurrence("k", now)], now, -1, 1
        )
    assert exc.value.code == "BACKFILL_INVALID"


@pytest.mark.parametrize("case", POLICIES["retry"])
def test_retry_backoff_decisions(case: dict[str, Any]) -> None:
    out = c.decide_retry(c.RetryPolicy(), case["attempt"], case["error"])
    assert out.decision == case["decision"]
    if case["decision"] == "RETRY":
        assert out.next_attempt_no == case["next"]
        assert out.delay_min_s == pytest.approx(case["delay_min"])
        assert out.delay_max_s == pytest.approx(case["delay_max"])


@pytest.mark.parametrize("bad", POLICIES["retry_policy_invalid"])
def test_retry_policy_bounds(bad: dict[str, Any]) -> None:
    with pytest.raises(c.ScheduleContractError) as exc:
        c.RetryPolicy(**bad)
    assert exc.value.code == "RETRY_POLICY_INVALID"


# --- schemas -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template", TEMPLATES["valid"], ids=[t["action"] for t in TEMPLATES["valid"]]
)
def test_action_template_valid(template: dict[str, Any]) -> None:
    assert validate_action_template(template) is template


@pytest.mark.parametrize(
    "case",
    TEMPLATES["invalid"],
    ids=[f"{i}-{x['code']}" for i, x in enumerate(TEMPLATES["invalid"])],
)
def test_action_template_rejections(case: dict[str, Any]) -> None:
    with pytest.raises(c.ScheduleContractError) as exc:
        validate_action_template(case["template"])
    assert exc.value.code == case["code"]


def test_agent_selection_schema() -> None:
    for sel in TEMPLATES["agent_selection_valid"]:
        assert validate_agent_selection(sel) is sel
    for case in TEMPLATES["agent_selection_invalid"]:
        with pytest.raises(c.ScheduleContractError) as exc:
            validate_agent_selection(case["selection"])
        assert exc.value.code == case["code"]


def test_schedule_version_valid_fixture() -> None:
    assert validate_schedule_version(copy.deepcopy(VERSION_VALID)) == VERSION_VALID


@pytest.mark.parametrize("case", VERSION_INVALID, ids=[x["name"] for x in VERSION_INVALID])
def test_schedule_version_rejections(case: dict[str, Any]) -> None:
    version = copy.deepcopy(VERSION_VALID)
    for key, value in case["patch"].items():
        if value is None:
            version.pop(key, None)
        else:
            version[key] = value
    if case["code"] is None:
        validate_schedule_version(version)
        return
    with pytest.raises(c.ScheduleContractError) as exc:
        validate_schedule_version(version)
    assert exc.value.code == case["code"]


@pytest.mark.parametrize("case", RUN_CASES["cases"], ids=[x["name"] for x in RUN_CASES["cases"]])
def test_schedule_run_schema(case: dict[str, Any]) -> None:
    run = copy.deepcopy(RUN_CASES["base"])
    run.update(case["patch"])
    if case["code"] is None:
        validate_schedule_run(run)
        return
    with pytest.raises(c.ScheduleContractError) as exc:
        validate_schedule_run(run)
    assert exc.value.code == case["code"]
