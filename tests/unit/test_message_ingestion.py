"""P2-15 unit: redaction scanner (DLP boundary), ingestion scope rule, deterministic ids."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from server.channels.ingestion import (
    DEFAULT_SCANNER,
    IngestionError,
    in_ingestion_scope,
    message_id_for,
)

CASES = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "messages" / "redaction-cases.yaml"
    ).read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_redaction_cases(case: dict[str, Any]) -> None:
    result = DEFAULT_SCANNER.scan(case["body"])
    assert list(result.findings) == case["expect_kinds"], result
    for fragment in case.get("must_not_contain", []):
        assert fragment not in result.text, (fragment, result.text)
    for fragment in case.get("must_contain", []):
        assert fragment in result.text, (fragment, result.text)
    assert result.clean == (not case["expect_kinds"])


def test_findings_never_carry_values() -> None:
    result = DEFAULT_SCANNER.scan("password=Sup3rS3cret! CANARY-NOT-A-SECRET-9")
    assert all("Sup3r" not in f and "CANARY" not in f for f in result.findings)
    assert "<redacted:" in result.text and result.text.count("<redacted:") == 2


def test_scope_rule() -> None:
    assert in_ingestion_scope(
        documentation_policy="task_threads", in_bound_thread=True, bridge_relayed=False
    )
    assert in_ingestion_scope(
        documentation_policy="task_threads", in_bound_thread=False, bridge_relayed=True
    )
    assert not in_ingestion_scope(
        documentation_policy="task_threads", in_bound_thread=False, bridge_relayed=False
    )
    assert in_ingestion_scope(
        documentation_policy="full_channel", in_bound_thread=False, bridge_relayed=False
    )
    with pytest.raises(IngestionError) as exc:
        in_ingestion_scope(
            documentation_policy="everything", in_bound_thread=True, bridge_relayed=False
        )
    assert exc.value.code == "DOCUMENTATION_POLICY_INVALID"


def test_message_ids_are_deterministic_per_source_and_conversation() -> None:
    a = message_id_for("mattermost", "p1", "conv-1")
    assert (
        a == message_id_for("mattermost", "p1", "conv-1") and a.startswith("msg-") and len(a) == 28
    )
    assert (
        a
        != message_id_for("mattermost", "p1", "conv-2")
        != message_id_for("telegram", "p1", "conv-2")
    )
