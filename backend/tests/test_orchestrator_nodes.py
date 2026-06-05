"""Tests for orchestrator nodes with pure logic.

Skipped:
- parse_input (calls Claude; covered by integration test)
- await_user_answers (uses LangGraph interrupt; covered by end-to-end test)
- merge_pass2 (no-op)
"""

from __future__ import annotations

import pytest

from models.project_schema import CustomRole, RoleRoster, Stage2Context
from models.twin_outputs import (
    Gap,
    HourRange,
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)
from orchestrator.nodes.commercial_processing import commercial_processing
from orchestrator.nodes.consistency_check import consistency_check
from orchestrator.nodes.merge_pass1 import merge_pass1
from orchestrator.nodes.synthesize_estimate import synthesize_estimate


def _default_role_hours(total: float) -> list[RoleHours]:
    """Match the percentages of RoleRoster.default() (20/10/50/20)."""
    splits = [
        ("sr_product", "Senior product manager", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 0.20),
        ("jr_product", "Junior product manager", RoleCategory.PRODUCT, RoleSeniority.JUNIOR, 0.10),
        ("sr_engineer", "Senior software engineer", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 0.50),
        ("jr_engineer", "Junior software engineer", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 0.20),
    ]
    return [
        RoleHours(
            role_id=rid,
            role_description=desc,
            category=cat,
            seniority=sen,
            hours=total * share,
        )
        for rid, desc, cat, sen, share in splits
    ]


def _phase(
    phase: Phase, ai_mid: float = 100, manual_mid: float = 120, gaps: list[Gap] | None = None
) -> PhaseEstimate:
    return PhaseEstimate(
        phase=phase,
        twin_name=f"{phase.value}_twin",
        algorithm="test",
        ai_assisted_hours=HourRange(optimistic=ai_mid * 0.8, most_likely=ai_mid, pessimistic=ai_mid * 1.3),
        manual_only_hours=HourRange(optimistic=manual_mid * 0.8, most_likely=manual_mid, pessimistic=manual_mid * 1.3),
        ai_assisted_role_hours=_default_role_hours(ai_mid),
        manual_only_role_hours=_default_role_hours(manual_mid),
        gaps=gaps or [],
        confidence=0.7,
    )


# ---- merge_pass1 ----

@pytest.mark.asyncio
async def test_merge_pass1_dedupes_gaps_by_topic_and_keeps_higher_impact() -> None:
    gap_a = Gap(topic="integration_count", question_text="How many integrations?", impact_hours=80, suggested_default="3")
    gap_b = Gap(topic="integration_count", question_text="Number of external systems?", impact_hours=120, suggested_default="4")
    state = {
        "pass1_estimates": [
            _phase(Phase.DEVELOPMENT, gaps=[gap_a]),
            _phase(Phase.DEPLOYMENT, gaps=[gap_b]),
        ]
    }
    result = await merge_pass1(state)
    qs = result["clarifying_questions"]
    assert len(qs) == 1
    # The higher-impact phrasing wins.
    assert qs[0].impact_hours == 120
    assert Phase.DEVELOPMENT in qs[0].source_phases
    assert Phase.DEPLOYMENT in qs[0].source_phases


@pytest.mark.asyncio
async def test_merge_pass1_sorts_questions_by_impact_descending() -> None:
    state = {
        "pass1_estimates": [
            _phase(Phase.DISCOVERY, gaps=[
                Gap(topic="low", question_text="?", impact_hours=10, suggested_default="x"),
                Gap(topic="hi", question_text="?", impact_hours=200, suggested_default="x"),
                Gap(topic="mid", question_text="?", impact_hours=80, suggested_default="x"),
            ])
        ]
    }
    result = await merge_pass1(state)
    impacts = [q.impact_hours for q in result["clarifying_questions"]]
    assert impacts == sorted(impacts, reverse=True)


@pytest.mark.asyncio
async def test_merge_pass1_caps_at_10_questions() -> None:
    gaps = [Gap(topic=f"t{i}", question_text="?", impact_hours=float(i), suggested_default="x") for i in range(20)]
    state = {"pass1_estimates": [_phase(Phase.DEVELOPMENT, gaps=gaps)]}
    result = await merge_pass1(state)
    assert len(result["clarifying_questions"]) == 10


# ---- consistency_check ----

@pytest.mark.asyncio
async def test_consistency_warns_when_qa_share_is_below_15_percent() -> None:
    state = {
        "pass2_estimates": [
            _phase(Phase.DEVELOPMENT, ai_mid=1000),
            _phase(Phase.QA_TESTING, ai_mid=50),  # ~5% — too low
        ]
    }
    result = await consistency_check(state)
    warnings = result["parsed_context"]["consistency_warnings"]
    assert len(warnings) == 1
    assert "QA share" in warnings[0]


@pytest.mark.asyncio
async def test_consistency_warns_when_qa_share_is_above_55_percent() -> None:
    state = {
        "pass2_estimates": [
            _phase(Phase.DEVELOPMENT, ai_mid=100),
            _phase(Phase.QA_TESTING, ai_mid=200),  # ~67%
        ]
    }
    result = await consistency_check(state)
    warnings = result["parsed_context"]["consistency_warnings"]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_consistency_emits_no_warning_when_qa_share_is_healthy() -> None:
    state = {
        "pass2_estimates": [
            _phase(Phase.DEVELOPMENT, ai_mid=600),
            _phase(Phase.QA_TESTING, ai_mid=300),  # 33%
        ]
    }
    result = await consistency_check(state)
    assert result["parsed_context"]["consistency_warnings"] == []


# ---- commercial_processing ----

@pytest.mark.asyncio
async def test_commercial_processing_applies_per_role_rates_from_roster() -> None:
    # Phase with ai_mid=100 splits 20/10/50/20 (default roster percentages).
    phase = _phase(Phase.DEVELOPMENT, ai_mid=100, manual_mid=120)
    custom_roster = RoleRoster(
        roles=[
            CustomRole(
                role_id="sr_product",
                description="Senior product manager",
                category=RoleCategory.PRODUCT,
                seniority=RoleSeniority.SENIOR,
                rate_per_hour=200,
                percentage=20,
            ),
            CustomRole(
                role_id="jr_product",
                description="Junior product manager",
                category=RoleCategory.PRODUCT,
                seniority=RoleSeniority.JUNIOR,
                rate_per_hour=100,
                percentage=10,
            ),
            CustomRole(
                role_id="sr_engineer",
                description="Senior software engineer",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.SENIOR,
                rate_per_hour=250,
                percentage=50,
            ),
            CustomRole(
                role_id="jr_engineer",
                description="Junior software engineer",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.JUNIOR,
                rate_per_hour=150,
                percentage=20,
            ),
        ]
    )
    state = {
        "pass2_estimates": [phase],
        "stage2": Stage2Context(roster=custom_roster),
    }
    result = await commercial_processing(state)
    parsed = result["parsed_context"]
    # 20*200 + 10*100 + 50*250 + 20*150 = 4000 + 1000 + 12500 + 3000 = 20500
    assert parsed["total_cost_ai_assisted_usd"] == 20500
    # 24*200 + 12*100 + 60*250 + 24*150 = 4800 + 1200 + 15000 + 3600 = 24600
    assert parsed["total_cost_manual_only_usd"] == 24600
    # Roster is echoed into parsed_context for downstream consumers.
    assert len(parsed["roster"]) == 4


@pytest.mark.asyncio
async def test_commercial_processing_uses_default_roster_when_stage2_missing() -> None:
    state = {"pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=100)], "stage2": None}
    result = await commercial_processing(state)
    # Just verify it produces a non-zero cost using defaults.
    assert result["parsed_context"]["total_cost_ai_assisted_usd"] > 0


@pytest.mark.asyncio
async def test_commercial_processing_role_with_no_rate_contributes_zero() -> None:
    """An unpriced role (rate_per_hour=0) should add nothing to the total."""
    phase = _phase(Phase.DEVELOPMENT, ai_mid=100)
    free_roster = RoleRoster(
        roles=[
            CustomRole(
                role_id="sr_product",
                description="Senior product manager",
                category=RoleCategory.PRODUCT,
                seniority=RoleSeniority.SENIOR,
                rate_per_hour=0,
                percentage=20,
            ),
            CustomRole(
                role_id="jr_product",
                description="Junior product manager",
                category=RoleCategory.PRODUCT,
                seniority=RoleSeniority.JUNIOR,
                rate_per_hour=0,
                percentage=10,
            ),
            CustomRole(
                role_id="sr_engineer",
                description="Senior software engineer",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.SENIOR,
                rate_per_hour=0,
                percentage=50,
            ),
            CustomRole(
                role_id="jr_engineer",
                description="Junior software engineer",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.JUNIOR,
                rate_per_hour=0,
                percentage=20,
            ),
        ]
    )
    state = {
        "pass2_estimates": [phase],
        "stage2": Stage2Context(roster=free_roster),
    }
    result = await commercial_processing(state)
    assert result["parsed_context"]["total_cost_ai_assisted_usd"] == 0


# ---- synthesize_estimate ----

@pytest.mark.asyncio
async def test_synthesize_aggregates_hour_ranges_across_phases() -> None:
    state = {
        "pass2_estimates": [
            _phase(Phase.DISCOVERY, ai_mid=200, manual_mid=240),
            _phase(Phase.DEVELOPMENT, ai_mid=800, manual_mid=1000),
        ],
        "parsed_context": {
            "total_cost_ai_assisted_usd": 100_000,
            "total_cost_manual_only_usd": 130_000,
        },
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    result = await synthesize_estimate(state)
    final = result["final_estimate"]
    assert final.total_ai_assisted_hours.most_likely == 1000
    assert final.total_manual_only_hours.most_likely == 1240
    assert final.ai_cost_saved_usd == 30_000
    # Confidence is the average of per-phase confidences.
    assert final.confidence == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_synthesize_derives_headcount_per_user_defined_role() -> None:
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=1000)],
        "parsed_context": {},
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    result = await synthesize_estimate(state)
    final = result["final_estimate"]
    # One entry per role on the default roster.
    assert len(final.headcount_by_role) == len(RoleRoster.default().roles)
    # At least one head per role with non-zero hours.
    assert sum(h.headcount for h in final.headcount_by_role) >= 4
    # Each row self-describes — description + tags travelled through from the roster.
    for h in final.headcount_by_role:
        assert h.role_description
        assert h.category in RoleCategory
        assert h.seniority in RoleSeniority


@pytest.mark.asyncio
async def test_synthesize_handles_empty_pass2() -> None:
    state = {"pass2_estimates": [], "parsed_context": {}, "stage2": None}
    result = await synthesize_estimate(state)
    final = result["final_estimate"]
    assert final.total_ai_assisted_hours.most_likely == 0
    assert final.confidence == 0.0
