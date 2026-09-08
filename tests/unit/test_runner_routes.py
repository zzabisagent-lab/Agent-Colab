"""REST surfaces retain authentication and route commands through the existing bus."""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.deps import current_principal
from server.api.errors import ApiError
from server.api.v1 import work
from server.application.work import WorkPoll, WorkStart
from server.identity.principals import Principal


def test_poll_and_start_dispatch(monkeypatch):
    app = FastAPI()
    app.include_router(work.router)
    principal = Principal("acct-a", "00000000-0000-0000-0000-000000000001", "agent", "fp")
    app.dependency_overrides[current_principal] = lambda: principal
    dispatch = Mock(return_value={"items": []})
    monkeypatch.setattr(work, "dispatch", dispatch)
    client = TestClient(app)
    assert (
        client.post("/api/v1/work/poll", json={"agent_id": "agent-a", "max_items": 1}).status_code
        == 200
    )
    assert dispatch.call_args.args[1:] == (principal, WorkPoll(agent_id="agent-a", max_items=1))
    assert client.post("/api/v1/work/wi-12345678/start").status_code == 200
    assert isinstance(dispatch.call_args.args[2], WorkStart)
    assert (
        client.post("/api/v1/work/poll", json={"agent_id": "agent-a", "max_items": 0}).status_code
        == 422
    )


def test_poll_requires_agent_service_token():
    request = Mock()
    with pytest.raises(ApiError):
        work.poll(work.PollBody(agent_id="agent-a"), request, Principal("a", "a", "human", "fp"))
