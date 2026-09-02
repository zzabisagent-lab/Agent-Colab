"""Event contract: schema fixtures (V-P0-05), aggregate/Event contract (V-P0-13), hash chain."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from server.events.contract import ContractError, default_registry
from server.events.hashing import compute_content_hash, verify_chain
from tools import gen_event_fixtures, gen_event_schemas

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "events"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid_streams_pass_contract_and_chain(path: Path) -> None:
    events = json.loads(path.read_text(encoding="utf-8"))
    registry = default_registry()
    for ev in events:
        info = registry.validate(ev)
        assert info.aggregate_type == ev["aggregate_type"]
    assert verify_chain(events) == []


@pytest.mark.parametrize("path", INVALID, ids=[p.stem for p in INVALID])
def test_invalid_fixtures_return_stable_codes(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ContractError) as exc:
        default_registry().validate(case["event"])
    assert exc.value.code == case["expected_code"]


def test_generated_schemas_and_fixtures_have_no_drift() -> None:
    assert gen_event_schemas.main(["--check"]) == 0
    assert gen_event_fixtures.main(["--check"]) == 0


def test_every_event_type_maps_to_a_known_aggregate_with_states() -> None:
    registry = default_registry()
    assert len(registry.event_types) >= 89  # spec §9.3 list plus documented extensions
    for info in registry.event_types.values():
        agg = registry.aggregates[info.aggregate_type]
        assert agg["id_prefix"] and agg["states"], info.name
        assert 0 <= info.phase <= 7


def test_spec_event_list_is_fully_covered() -> None:
    spec = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "baseline"
        / "agent-colab-project-spec_en-v8.md"
    ).read_text(encoding="utf-8")
    section = spec[spec.index("### 9.3 Event Principles") : spec.index("## 10. Mattermost")]
    import re

    listed = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", section))
    registry = default_registry()
    missing = listed - set(registry.event_types)
    assert not missing, missing
    extensions = {n for n, i in registry.event_types.items() if i.extension}
    assert set(registry.event_types) - listed == extensions


def test_authority_declarations_exclude_projections() -> None:
    authority = default_registry().authority
    for key in (
        "approval_consumption",
        "run_uniqueness",
        "permissions",
        "idempotency",
        "verification_independence",
    ):
        assert key in authority
    assert "projection" not in authority["approval_consumption"].lower().replace("never", "")
    assert "never" in authority["permissions"]


def test_tamper_detection_on_every_hashed_field() -> None:
    events = json.loads((FIXTURES / "valid" / "task-stream.json").read_text(encoding="utf-8"))
    ev = events[2]
    for field in (
        "payload",
        "previous_hash",
        "actor_account_id",
        "occurred_at",
        "idempotency_key",
        "type",
    ):
        tampered = copy.deepcopy(ev)
        tampered[field] = (
            {"x": 1}
            if field == "payload"
            else (
                "0" * 64
                if field == "previous_hash"
                else "TASK_STARTED"
                if field == "type"
                else "tampered"
            )
        )
        assert compute_content_hash(tampered) != ev["content_hash"], field
    broken = copy.deepcopy(events)
    broken[1]["payload"]["assignment_revision"] = 2
    assert any("content_hash mismatch" in p for p in verify_chain(broken))
    broken2 = copy.deepcopy(events)
    del broken2[3]
    assert verify_chain(broken2)


def test_ciphertext_is_part_of_the_hash() -> None:
    events = json.loads(
        (FIXTURES / "valid" / "secret-grant-with-ciphertext.json").read_text(encoding="utf-8")
    )
    ev = copy.deepcopy(events[0])
    ev["sensitive_payload_ciphertext"] = "AQIDBA=="
    assert compute_content_hash(ev) != events[0]["content_hash"]
