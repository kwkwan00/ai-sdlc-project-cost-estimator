"""Coverage for the team-roster proposal agent.

The LLM half (`run_roster_agent`) is exercised with a stubbed `call_structured`
so we don't hit the network. The bulk of the tests target the deterministic
backstop (`_make_unique_ids`, `_rebalance_to_100`,
`proposal_to_roster`) — the part that must be exact for the proposed roster to
satisfy `RoleRoster`'s unique-id + sum-100 validator and render in the wizard.
"""

from __future__ import annotations

import random

import pytest

import roster_agent
from models.project_schema import RoleRoster
from models.twin_outputs import RoleCategory, RoleSeniority
from roster_agent import (
    ProposedRole,
    RosterProposal,
    _make_unique_ids,
    _rebalance_to_100,
    proposal_to_roster,
    run_roster_agent,
)


def _role(category: RoleCategory, seniority: RoleSeniority, pct: float, desc: str = "X") -> ProposedRole:
    return ProposedRole(description=desc, category=category, seniority=seniority, percentage=pct)


# ---------- _make_unique_ids ----------


def test_make_unique_ids_suffixes_collisions() -> None:
    roles = [
        _role(RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 50),
        _role(RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 30),
        _role(RoleCategory.PRODUCT, RoleSeniority.SENIOR, 20),
    ]
    ids = _make_unique_ids(roles)
    assert ids == ["senior_engineering", "senior_engineering_2", "senior_product"]


def test_make_unique_ids_are_unique_and_within_64_chars() -> None:
    roles = [_role(RoleCategory.QA, RoleSeniority.MID, 10) for _ in range(6)]
    ids = _make_unique_ids(roles)
    assert len(set(ids)) == len(ids)
    assert all(len(i) <= 64 for i in ids)


# ---------- _rebalance_to_100 ----------


@pytest.mark.parametrize(
    "weights,expected",
    [
        ([50, 30, 20], [50, 30, 20]),
        ([1, 1, 1], [34, 33, 33]),
        ([7], [100]),
    ],
)
def test_rebalance_exact_cases(weights: list[float], expected: list[int]) -> None:
    assert _rebalance_to_100(weights) == expected


def test_rebalance_all_zero_splits_evenly_no_zeros() -> None:
    result = _rebalance_to_100([0, 0, 0])
    assert sum(result) == 100
    assert min(result) >= 1


def test_rebalance_clamps_negatives() -> None:
    result = _rebalance_to_100([-5, 200, 10])
    assert sum(result) == 100
    assert min(result) >= 1


def test_rebalance_eight_equal_weights() -> None:
    result = _rebalance_to_100([1] * 8)
    assert sum(result) == 100
    assert min(result) >= 1
    assert len(result) == 8


def test_rebalance_degenerate_single_dominant_no_zero_rows() -> None:
    # Model dumped everything on one role; the others must still get >=1%.
    result = _rebalance_to_100([100, 0, 0])
    assert sum(result) == 100
    assert min(result) >= 1


def test_rebalance_property_sums_to_100_no_zeros() -> None:
    rng = random.Random(0)
    for _ in range(200):
        n = rng.randint(1, 8)
        weights = [rng.uniform(0, 100) for _ in range(n)]
        result = _rebalance_to_100(weights)
        assert len(result) == n
        assert sum(result) == 100
        assert min(result) >= 1


# ---------- proposal_to_roster ----------


def test_proposal_to_roster_builds_valid_roster() -> None:
    proposal = RosterProposal(
        project_plan=[],
        staffing_rationale="r",
        roles=[
            _role(RoleCategory.PRODUCT, RoleSeniority.SENIOR, 20, "PM"),
            _role(RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 45, "Sr eng"),
            _role(RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 20, "Jr eng"),
            _role(RoleCategory.QA, RoleSeniority.MID, 15, "QA"),
        ],
    )
    roster = proposal_to_roster(proposal)
    ids = [r.role_id for r in roster.roles]
    assert len(set(ids)) == len(ids) == 4
    assert sum(r.percentage for r in roster.roles) == 100
    assert all(r.rate_per_hour > 0 for r in roster.roles)
    # Construction did not fall back to the default roster.
    assert ids != [r.role_id for r in RoleRoster.default().roles]


def test_proposal_to_roster_caps_at_eight_roles() -> None:
    roles = [_role(RoleCategory.ENGINEERING, RoleSeniority.MID, 10, f"e{i}") for i in range(10)]
    proposal = RosterProposal.model_construct(project_plan=[], staffing_rationale="", roles=roles)
    roster = proposal_to_roster(proposal)
    assert len(roster.roles) == 8
    assert sum(r.percentage for r in roster.roles) == 100


def test_proposal_to_roster_empty_roles_falls_back_to_default() -> None:
    # min_length=1 blocks an empty roles list via validation, so bypass it.
    proposal = RosterProposal.model_construct(project_plan=[], staffing_rationale="", roles=[])
    roster = proposal_to_roster(proposal)
    assert [r.role_id for r in roster.roles] == [r.role_id for r in RoleRoster.default().roles]


# ---------- run_roster_agent (LLM wiring) ----------


async def test_run_roster_agent_uses_sonnet_and_low_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models.project_schema import Stage2Context

    captured: dict = {}

    async def fake_call_structured(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return RosterProposal(
            project_plan=[],
            staffing_rationale="r",
            roles=[_role(RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100, "Eng")],
        )

    monkeypatch.setattr(roster_agent, "call_structured", fake_call_structured)

    out = await run_roster_agent(Stage2Context(industry="healthcare"), "Build a portal.")
    assert isinstance(out, RosterProposal)
    assert captured["model"] == roster_agent.get_settings().anthropic_model_roster
    assert "sonnet" in captured["model"]
    assert captured["effort"] == "low"
    assert captured["tool_name"] == "propose_team_roster"
