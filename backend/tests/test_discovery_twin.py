"""Tests for the Discovery Analyst twin's pure-math functions.

The Claude call itself is exercised indirectly by tests/test_graph.py (which
patches parse_input but lets the twin run with a real LLM is out of scope for unit
tests). For deterministic testing we call the math functions directly.

The reference fixture is the planning-outline §3.1.3 healthcare worked example:
  - 3/3/2 simple/average/complex use cases  → UUCW = 75
  - 3/1/2 simple/average/complex actors      → UAW  = 11
  - TFactor = 42  → TCF = 1.02
  - EFactor = 24  → ECF = 0.68
  - UCP  ≈ 59.6
  - Productivity = 24 hrs/UCP, Phase ratio = 0.10
  - Stakeholders: 4 groups (1.15) × gatekeeper (1.2) × pre-aligned (1.0)
  - Adjusted mid ≈ 199 hours
"""

from __future__ import annotations

import pytest

from models.project_schema import RoleRoster
from models.twin_outputs import Phase
from orchestrator.nodes.discovery_analyst import (
    AlignmentDifficulty,
    DecisionMakerAccessibility,
    DiscoveryUCPInputs,
    _stakeholder_multiplier,
    build_phase_estimate,
    compute_ucp_hours,
    pert_range,
)


def _fixture_inputs(**overrides) -> DiscoveryUCPInputs:
    defaults = dict(
        simple_use_cases=3,
        average_use_cases=3,
        complex_use_cases=2,
        simple_actors=3,
        average_actors=1,
        complex_actors=2,
        tfactor=42,
        efactor=24,
        stakeholder_group_count=4,
        decision_maker_accessibility=DecisionMakerAccessibility.GATEKEEPER,
        alignment_difficulty=AlignmentDifficulty.PRE_ALIGNED,
        phase_ratio_hint=0.10,
        productivity_factor=24.0,
        assumptions=["Mid-size healthcare scope inferred"],
        risks=["Stakeholder count may be higher"],
        gaps=[],
        confidence=0.7,
        notes="healthcare fixture",
    )
    defaults.update(overrides)
    return DiscoveryUCPInputs(**defaults)


# ---- compute_ucp_hours ----

def test_ucp_matches_planning_outline_worked_example() -> None:
    inputs = _fixture_inputs()
    mid, b = compute_ucp_hours(inputs)
    assert b["uucw"] == 75
    assert b["uaw"] == 11
    assert b["tcf"] == pytest.approx(1.02, abs=0.001)
    assert b["ecf"] == pytest.approx(0.68, abs=0.001)
    assert b["ucp"] == pytest.approx(59.6, abs=0.5)
    # Worked example: 199 hours (with rounding at each step).
    # Without intermediate rounding we get ~197.5; allow a 5% tolerance.
    assert mid == pytest.approx(199, rel=0.05)


def test_ucp_uucw_uaw_formula() -> None:
    inputs = _fixture_inputs(
        simple_use_cases=10, average_use_cases=0, complex_use_cases=0,
        simple_actors=0, average_actors=0, complex_actors=5,
    )
    _, b = compute_ucp_hours(inputs)
    assert b["uucw"] == 50  # 10 * 5
    assert b["uaw"] == 15  # 5 * 3


# ---- stakeholder multiplier ----

def test_stakeholder_multiplier_single_group_baseline() -> None:
    inputs = _fixture_inputs(
        stakeholder_group_count=2,
        decision_maker_accessibility=DecisionMakerAccessibility.READILY_AVAILABLE,
        alignment_difficulty=AlignmentDifficulty.PRE_ALIGNED,
    )
    assert _stakeholder_multiplier(inputs) == 1.0


def test_stakeholder_multiplier_compounds_factors() -> None:
    inputs = _fixture_inputs(
        stakeholder_group_count=6,  # 1.35
        decision_maker_accessibility=DecisionMakerAccessibility.EXECUTIVE_ONLY_OR_MULTI_TZ,  # 1.4
        alignment_difficulty=AlignmentDifficulty.COMPETING_PRIORITIES,  # 1.25
    )
    assert _stakeholder_multiplier(inputs) == pytest.approx(1.35 * 1.4 * 1.25)


# ---- PERT range ----

def test_pert_range_orders_optimistic_below_pessimistic() -> None:
    r = pert_range(100.0)
    assert r.optimistic < r.most_likely < r.pessimistic
    assert r.most_likely == 100.0


# ---- build_phase_estimate end-to-end ----

def test_build_phase_estimate_at_zero_reduction_has_no_ai_savings() -> None:
    inputs = _fixture_inputs()
    est = build_phase_estimate(inputs, effective_reduction=0.0, roster=RoleRoster.default())

    assert est.phase is Phase.DISCOVERY
    assert est.algorithm == "UCP"
    # At zero reduction, AI hours == manual hours.
    assert est.ai_assisted_hours.most_likely == est.manual_only_hours.most_likely
    assert est.ai_assisted_hours.most_likely == pytest.approx(199, rel=0.05)


def test_build_phase_estimate_applies_30pct_reduction() -> None:
    inputs = _fixture_inputs()
    est = build_phase_estimate(inputs, effective_reduction=0.30, roster=RoleRoster.default())

    # AI mid should be 70% of manual mid.
    ratio = est.ai_assisted_hours.most_likely / est.manual_only_hours.most_likely
    assert ratio == pytest.approx(0.7, abs=0.001)


def test_build_phase_estimate_negative_reduction_makes_ai_slower() -> None:
    inputs = _fixture_inputs()
    est = build_phase_estimate(inputs, effective_reduction=-0.10, roster=RoleRoster.default())

    # Negative reduction → AI hours exceed manual hours.
    assert est.ai_assisted_hours.most_likely > est.manual_only_hours.most_likely


def test_build_phase_estimate_role_hours_sum_to_total() -> None:
    inputs = _fixture_inputs()
    est = build_phase_estimate(
        inputs, effective_reduction=0.30, roster=RoleRoster.default()
    )

    total_ai = sum(rh.hours for rh in est.ai_assisted_role_hours)
    total_manual = sum(rh.hours for rh in est.manual_only_role_hours)
    assert total_ai == pytest.approx(est.ai_assisted_hours.most_likely, abs=1e-3)
    assert total_manual == pytest.approx(est.manual_only_hours.most_likely, abs=1e-3)
    # One entry per roster role, including any zeroed by overrides.
    assert len(est.ai_assisted_role_hours) == len(RoleRoster.default().roles)


def test_build_phase_estimate_carries_assumptions_risks_gaps() -> None:
    from models.twin_outputs import Gap

    inputs = _fixture_inputs(
        assumptions=["a1", "a2"],
        risks=["r1"],
        gaps=[Gap(topic="t", question_text="q?", impact_hours=50, suggested_default="x")],
    )
    est = build_phase_estimate(inputs, effective_reduction=0.30, roster=RoleRoster.default())
    assert [a.text for a in est.assumptions] == ["a1", "a2"]
    assert [r.description for r in est.risks] == ["r1"]
    assert [g.topic for g in est.gaps] == ["t"]
