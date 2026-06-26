"""Coverage for the AG-UI roster endpoint (POST /estimates/draft/roster/agui).

Drives the endpoint through FastAPI's TestClient with the roster agent stubbed,
parses the SSE event stream, and asserts the AG-UI lifecycle: a successful run
emits RUN_STARTED → STATE_SNAPSHOT (carrying the proposed roster) → RUN_FINISHED,
and a failing run emits RUN_STARTED → RUN_ERROR. `proposal_to_roster` runs for
real on the stubbed proposal, so the snapshot exercises the actual mapping.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agents.roster_agent import ProjectPlanItem, ProposedRole, RosterProposal
from agents.roster_agui import _extract_inputs
from models.twin_outputs import Phase, RoleCategory, RoleSeniority


@pytest.fixture()
def client() -> TestClient:
    from main import app

    return TestClient(app)


def _run_input() -> dict:
    # camelCase, as the @ag-ui/client HttpAgent sends it.
    return {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "raw_input": "A HIPAA patient portal for a regional clinic.",
            "stage2": {"industry": "healthcare", "project_type": "greenfield"},
        },
    }


def test_extract_inputs_parses_selected_phases_and_drops_unknowns() -> None:
    from ag_ui.core import RunAgentInput

    payload = _run_input()
    payload["forwardedProps"]["selected_phases"] = ["development", "qa_testing", "bogus"]
    stage2, raw_input, selected_phases = _extract_inputs(RunAgentInput.model_validate(payload))
    assert raw_input.startswith("A HIPAA")
    assert stage2.industry == "healthcare"
    # Valid phases parsed in order; the unknown "bogus" is dropped (never raises).
    assert selected_phases == [Phase.DEVELOPMENT, Phase.QA_TESTING]


def test_extract_inputs_defaults_phase_scope_to_empty_when_absent() -> None:
    from ag_ui.core import RunAgentInput

    _, _, selected_phases = _extract_inputs(RunAgentInput.model_validate(_run_input()))
    assert selected_phases == []


def _parse_sse_events(body: str) -> list[dict]:
    events: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


def test_roster_agui_streams_snapshot_then_finished(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def fake_agent(
        stage2, raw_input: str, custom_roles=None, selected_phases=None
    ) -> RosterProposal:
        return RosterProposal(
            project_plan=[ProjectPlanItem(workstream="Core build", summary="Build it")],
            staffing_rationale="Lean regulated team",
            roles=[
                ProposedRole(
                    description="Senior PM",
                    category=RoleCategory.PRODUCT,
                    seniority=RoleSeniority.SENIOR,
                    percentage=30,
                ),
                ProposedRole(
                    description="Senior engineer",
                    category=RoleCategory.ENGINEERING,
                    seniority=RoleSeniority.SENIOR,
                    percentage=50,
                ),
                ProposedRole(
                    description="QA engineer",
                    category=RoleCategory.QA,
                    seniority=RoleSeniority.MID,
                    percentage=20,
                ),
            ],
        )

    monkeypatch.setattr("agents.roster_agui.run_roster_agent", fake_agent)

    res = client.post("/estimates/draft/roster/agui", json=_run_input())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse_events(res.text)
    types = [e["type"] for e in events]
    assert types == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]

    snapshot = next(e for e in events if e["type"] == "STATE_SNAPSHOT")["snapshot"]
    roles = snapshot["roster"]["roles"]
    assert len(roles) == 3
    assert len({r["role_id"] for r in roles}) == 3  # unique ids
    assert sum(r["percentage"] for r in roles) == 100
    assert all(r["rate_per_hour"] > 0 for r in roles)  # rates from the table
    assert snapshot["staffing_rationale"] == "Lean regulated team"
    assert [p["workstream"] for p in snapshot["project_plan"]] == ["Core build"]


def test_roster_agui_emits_run_error_on_agent_failure(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def failing_agent(
        stage2, raw_input: str, custom_roles=None, selected_phases=None
    ) -> RosterProposal:
        raise RuntimeError("sonnet unavailable")

    monkeypatch.setattr("agents.roster_agui.run_roster_agent", failing_agent)

    res = client.post("/estimates/draft/roster/agui", json=_run_input())
    assert res.status_code == 200

    types = [e["type"] for e in _parse_sse_events(res.text)]
    assert types == ["RUN_STARTED", "RUN_ERROR"]
