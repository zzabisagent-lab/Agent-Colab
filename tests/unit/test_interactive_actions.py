"""P2-12 unit rules: signed contexts, validation order/codes, button mapping, identity stripping."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels import actions
from server.channels.actions import ActionContext, ActionError, ActionHandler, parse_context

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mattermost" / "actions-cases.yaml"
CASES = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
SECRET = b"unit-test-action-secret"
NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)


def _ctx(action: str = "accept", subject_id: str = "task-1") -> ActionContext:
    return ActionContext("task", subject_id, action, int(NOW.timestamp()), "nonce-1")


def test_signed_context_round_trip_and_button_actions() -> None:
    ctx = _ctx()
    raw = ctx.as_button_context(SECRET)
    parsed, signature, body = parse_context(raw)
    assert parsed == ctx and signature == actions.sign_context(SECRET, ctx)
    actions.validate_context(parsed, signature, body, secret=SECRET, now=NOW)
    buttons = actions.build_button_actions(
        SECRET, subject_type="task", subject_id="task-1", buttons=("accept", "cancel"), now=NOW
    )
    assert [b["name"] for b in buttons] == ["Accept", "Cancel"]
    assert all(b["integration"]["context"]["signature"] for b in buttons)
    assert len({b["integration"]["context"]["nonce"] for b in buttons}) == 2
    props = actions.attach_button_contexts(
        {"buttons": ["accept"]}, subject_type="task", subject_id="task-1", now=NOW, secret=SECRET
    )
    assert props["attachments"][0]["actions"][0]["integration"]["context"]["action"] == "accept"
    assert actions.attach_button_contexts(
        {"buttons": []}, subject_type="task", subject_id="t", now=NOW, secret=SECRET
    ) == {"buttons": []}


@pytest.mark.parametrize(
    "case", CASES["context_rejections"], ids=[c["name"] for c in CASES["context_rejections"]]
)
def test_context_rejections(case: dict[str, Any]) -> None:
    raw = _ctx().as_button_context(SECRET)
    mutate = case["mutate"]
    if mutate == "signature":
        raw["signature"] = "0" * 64
    elif mutate == "subject_id":
        raw["subject_id"] = "task-other"
    elif mutate == "body_sha256":
        raw["body_sha256"] = "f" * 64
    elif mutate == "issued_at":
        raw["issued_at"] = int((NOW - dt.timedelta(minutes=6)).timestamp())
        raw["signature"] = ActionContext(
            "task", "task-1", "accept", raw["issued_at"], "nonce-1"
        ).signature(SECRET)
    elif mutate == "drop_nonce":
        raw.pop("nonce")
    with pytest.raises(ActionError) as exc:
        ctx, sig, body = parse_context(raw)
        actions.validate_context(ctx, sig, body, secret=SECRET, now=NOW)
    assert exc.value.code == case["expect"] and exc.value.status == case["status"]


def test_wrong_secret_is_a_signature_error_not_a_crash() -> None:
    raw = _ctx().as_button_context(b"other-secret")
    ctx, sig, body = parse_context(raw)
    with pytest.raises(ActionError) as exc:
        actions.validate_context(ctx, sig, body, secret=SECRET, now=NOW)
    assert exc.value.code == "CALLBACK_SIGNATURE_INVALID"


class _Row:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return None if self._value is None else (self._value,)


class _FakeSession:
    """Answers the two lookups the planner makes (approval risk, active verification run)."""

    def __init__(self, risk: str | None = None, run_id: str | None = None) -> None:
        self.risk, self.run_id = risk, run_id

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Row:
        sql = str(statement)
        if "approval_grants" in sql:
            return _Row(self.risk)
        return _Row(self.run_id)


@pytest.mark.parametrize(
    "case", CASES["buttons"], ids=[f"{c['action']}:{c['subject_id']}" for c in CASES["buttons"]]
)
def test_button_mapping(case: dict[str, Any]) -> None:
    handler = ActionHandler(runtime=None, secret=SECRET)  # type: ignore[arg-type]
    session = _FakeSession(risk=case.get("risk"), run_id=None)
    ctx = ActionContext(
        case["subject_type"], case["subject_id"], case["action"], int(NOW.timestamp()), "n"
    )
    plan = handler._plan(session, ctx)  # type: ignore[arg-type]
    expect = case["expect"]
    if "guidance" in expect:
        assert plan.command is None and plan.code == expect["guidance"] and plan.guidance
    else:
        assert plan.command is not None and type(plan.command).__name__ == expect["command"]
        if "decision" in expect:
            assert plan.command.decision == expect["decision"]


def test_verify_buttons_target_the_active_run() -> None:
    handler = ActionHandler(runtime=None, secret=SECRET)  # type: ignore[arg-type]
    session = _FakeSession(run_id="vr-9")
    plan = handler._plan(session, ActionContext("task", "task-1", "verify_fail", 0, "n"))  # type: ignore[arg-type]
    assert plan.command is not None and plan.command.result == "FAILED"
    report = plan.command.report
    assert report["result"] == "FAILED" and report["tests"][0]["result"] == "FAIL"


def test_secret_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(actions.ACTION_SECRET_ENV, raising=False)
    assert actions.action_secret() is None
    with pytest.raises(ActionError) as exc:
        ActionHandler(runtime=None)._secret()  # type: ignore[arg-type]
    assert exc.value.code == "ACTION_SECRET_UNCONFIGURED"
    monkeypatch.setenv(actions.ACTION_SECRET_ENV, "s")
    assert actions.action_secret() == b"s"


def test_unused_sqlalchemy_imports_are_real() -> None:  # keeps the fake-session test honest
    assert Session is not None and text is not None
