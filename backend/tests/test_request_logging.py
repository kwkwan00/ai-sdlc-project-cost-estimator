"""The HTTP request-logging middleware logs every request and is streaming-safe."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

_LOGGER = "observability.request_logging"


@pytest.fixture()
def client() -> TestClient:
    from main import app

    return TestClient(app)


def test_logs_method_path_status_and_latency(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert client.get("/health").status_code == 200

    lines = [r.getMessage() for r in caplog.records if "http GET /health" in r.getMessage()]
    assert lines, f"no access log for GET /health: {[r.getMessage() for r in caplog.records]}"
    assert "→ 200" in lines[0]
    assert "ms)" in lines[0]  # latency is logged


def test_streaming_endpoint_still_streams_through_middleware(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A pure-ASGI middleware must NOT buffer/break SSE. The AG-UI roster endpoint
    must still stream its full event sequence AND get an access-log line."""
    from models.twin_outputs import RoleCategory, RoleSeniority
    from roster_agent import ProposedRole, RosterProposal

    async def fake_agent(stage2, raw_input: str, custom_roles=None) -> RosterProposal:
        return RosterProposal(
            project_plan=[],
            staffing_rationale="r",
            roles=[
                ProposedRole(
                    description="Eng",
                    category=RoleCategory.ENGINEERING,
                    seniority=RoleSeniority.SENIOR,
                    percentage=100,
                )
            ],
        )

    monkeypatch.setattr("roster_agui.run_roster_agent", fake_agent)

    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {"raw_input": "x", "stage2": {"industry": "healthcare"}},
    }
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        res = client.post("/estimates/draft/roster/agui", json=body)

    assert res.status_code == 200
    events = [json.loads(line[5:]) for line in res.text.splitlines() if line.startswith("data:")]
    assert [e["type"] for e in events] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    assert any(
        "http POST /estimates/draft/roster/agui → 200" in r.getMessage()
        for r in caplog.records
    )
