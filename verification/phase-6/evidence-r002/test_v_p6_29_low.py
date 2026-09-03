"""Independent V-P6-29 probe for the LOW-risk half of the button criterion."""

from tests.integration.test_approval_collaboration_db import (
    _card,
    _press,
    _request_approval,
    _status,
    engine,
)


def test_low_risk_button_approves(engine):
    approval_id = _request_approval(
        engine,
        "r002-low-risk",
        action="tool:task_progress",
    )
    card = _card(engine, approval_id)
    assert card is not None
    assert "risk LOW" in card["message"]
    assert set(card["props"]["buttons"]) == {"approve", "reject"}
    result = _press(engine, "approver", "approve", approval_id)
    assert result.executed and result.code == "OK"
    assert _status(engine, approval_id) == "APPROVED"
