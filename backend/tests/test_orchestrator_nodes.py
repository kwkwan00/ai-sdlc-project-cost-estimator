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
#
# The deterministic layer-1 tests below disable the LLM semantic-consolidation
# layer (force an empty API key → early return) so they stay network-free and
# deterministic. Layer 2 is covered separately with a stubbed call_structured.


class _FakeMergeSettings:
    def __init__(self, key: str = "", model: str = "claude-haiku-4-5") -> None:
        self.anthropic_api_key = key
        self.anthropic_model_merge = model


def _disable_llm_consolidation(monkeypatch) -> None:
    from orchestrator.nodes import merge_pass1 as mp

    monkeypatch.setattr(mp, "get_settings", lambda: _FakeMergeSettings(key=""))


def _stub_llm_consolidation(monkeypatch, clusters=None, *, raise_exc=None, calls=None) -> None:
    from orchestrator.nodes import merge_pass1 as mp

    monkeypatch.setattr(mp, "get_settings", lambda: _FakeMergeSettings(key="test-key"))

    async def _fake_call_structured(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return mp._ConsolidationResult(clusters=clusters or [])

    monkeypatch.setattr(mp, "call_structured", _fake_call_structured)


@pytest.mark.asyncio
async def test_merge_pass1_dedupes_gaps_by_topic_and_keeps_higher_impact(monkeypatch) -> None:
    _disable_llm_consolidation(monkeypatch)
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
async def test_merge_pass1_sorts_questions_by_impact_descending(monkeypatch) -> None:
    _disable_llm_consolidation(monkeypatch)
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
async def test_merge_pass1_caps_at_10_questions(monkeypatch) -> None:
    _disable_llm_consolidation(monkeypatch)
    gaps = [Gap(topic=f"t{i}", question_text="?", impact_hours=float(i), suggested_default="x") for i in range(20)]
    state = {"pass1_estimates": [_phase(Phase.DEVELOPMENT, gaps=gaps)]}
    result = await merge_pass1(state)
    assert len(result["clarifying_questions"]) == 10


# ---- merge_pass1: layer-2 semantic consolidation ----


def _sizing_state() -> dict:
    """Three distinct-topic gaps; two are semantically the same dev-sizing ask."""
    return {
        "pass1_estimates": [
            _phase(Phase.DEVELOPMENT, gaps=[
                Gap(topic="function_points", question_text="What is the function point count?", impact_hours=200, suggested_default="420"),
            ]),
            _phase(Phase.DEPLOYMENT, gaps=[
                Gap(topic="dr_posture", question_text="Single or multi-region DR?", impact_hours=80, suggested_default="single"),
            ]),
            _phase(Phase.CODE_REVIEW, gaps=[
                Gap(topic="ksloc", question_text="Is there a KSLOC estimate to anchor review?", impact_hours=40, suggested_default="38"),
            ]),
        ]
    }


@pytest.mark.asyncio
async def test_merge_pass1_consolidates_near_duplicate_questions(monkeypatch) -> None:
    from orchestrator.nodes.merge_pass1 import _GapCluster

    # Cluster the two sizing questions (indices 0 + 2); leave DR (index 1) alone.
    clusters = [
        _GapCluster(member_indices=[0, 2], merged_question="What is the Development sizing (function points and KSLOC)?"),
        _GapCluster(member_indices=[1], merged_question="Single or multi-region DR?"),
    ]
    _stub_llm_consolidation(monkeypatch, clusters)
    result = await merge_pass1(_sizing_state())
    qs = result["clarifying_questions"]
    # 3 candidates → 2 questions after merging the sizing pair.
    assert len(qs) == 2
    merged = next(q for q in qs if "Development sizing" in q.text)
    # Highest-impact member's magnitude + default survive; phases are unioned.
    assert merged.impact_hours == 200
    assert merged.suggested_default == "420"
    assert Phase.DEVELOPMENT in merged.source_phases
    assert Phase.CODE_REVIEW in merged.source_phases


@pytest.mark.asyncio
async def test_merge_pass1_falls_back_when_llm_returns_non_partition(monkeypatch) -> None:
    from orchestrator.nodes.merge_pass1 import _GapCluster

    # Drops index 2 → not a partition of {0,1,2} → must fall back to topic-dedup.
    clusters = [_GapCluster(member_indices=[0, 1], merged_question="bad")]
    _stub_llm_consolidation(monkeypatch, clusters)
    result = await merge_pass1(_sizing_state())
    assert len(result["clarifying_questions"]) == 3  # unchanged


@pytest.mark.asyncio
async def test_merge_pass1_falls_back_when_llm_errors(monkeypatch) -> None:
    _stub_llm_consolidation(monkeypatch, raise_exc=RuntimeError("boom"))
    result = await merge_pass1(_sizing_state())
    assert len(result["clarifying_questions"]) == 3  # unchanged


@pytest.mark.asyncio
async def test_merge_pass1_skips_llm_below_candidate_threshold(monkeypatch) -> None:
    calls: list = []
    _stub_llm_consolidation(monkeypatch, raise_exc=RuntimeError("should not be called"), calls=calls)
    # Two distinct-topic gaps < _MIN_CANDIDATES_FOR_LLM (3) → no LLM round-trip.
    state = {
        "pass1_estimates": [
            _phase(Phase.DEVELOPMENT, gaps=[Gap(topic="a", question_text="A?", impact_hours=50, suggested_default="x")]),
            _phase(Phase.DEPLOYMENT, gaps=[Gap(topic="b", question_text="B?", impact_hours=60, suggested_default="y")]),
        ]
    }
    result = await merge_pass1(state)
    assert len(result["clarifying_questions"]) == 2
    assert calls == []  # LLM never invoked


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
    warnings = result["consistency_warnings"]
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
    warnings = result["consistency_warnings"]
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
    assert result["consistency_warnings"] == []


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
    # 20*200 + 10*100 + 50*250 + 20*150 = 4000 + 1000 + 12500 + 3000 = 20500
    assert result["total_cost_ai_assisted_usd"] == 20500
    # 24*200 + 12*100 + 60*250 + 24*150 = 4800 + 1200 + 15000 + 3600 = 24600
    assert result["total_cost_manual_only_usd"] == 24600


@pytest.mark.asyncio
async def test_commercial_processing_uses_default_roster_when_stage2_missing() -> None:
    state = {"pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=100)], "stage2": None}
    result = await commercial_processing(state)
    # Just verify it produces a non-zero cost using defaults.
    assert result["total_cost_ai_assisted_usd"] > 0


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
    assert result["total_cost_ai_assisted_usd"] == 0


# ---- synthesize_estimate ----

@pytest.mark.asyncio
async def test_synthesize_aggregates_hour_ranges_across_phases() -> None:
    state = {
        "pass2_estimates": [
            _phase(Phase.DISCOVERY, ai_mid=200, manual_mid=240),
            _phase(Phase.DEVELOPMENT, ai_mid=800, manual_mid=1000),
        ],
        "total_cost_ai_assisted_usd": 100_000,
        "total_cost_manual_only_usd": 130_000,
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
async def test_synthesize_attaches_per_role_cost_breakdown() -> None:
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=1000, manual_mid=1200)],
        "parsed_context": {},
        "stage2": Stage2Context(),  # default roster carries real rates
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    rates = {r.role_id: r.rate_per_hour for r in RoleRoster.default().roles}
    priced = [r for r in final.headcount_by_role if r.ai_assisted_hours > 0]
    assert priced, "expected at least one role with hours"
    for row in priced:
        assert row.rate_per_hour == rates[row.role_id]
        # cost = hours × rate, and the manual scenario costs more (more hours).
        assert row.ai_assisted_cost_usd == pytest.approx(row.ai_assisted_hours * row.rate_per_hour)
        assert row.manual_only_cost_usd == pytest.approx(row.manual_only_hours * row.rate_per_hour)
        assert row.manual_only_hours > row.ai_assisted_hours


@pytest.mark.asyncio
async def test_synthesize_handles_empty_pass2() -> None:
    state = {"pass2_estimates": [], "stage2": None}
    result = await synthesize_estimate(state)
    final = result["final_estimate"]
    assert final.total_ai_assisted_hours.most_likely == 0
    assert final.confidence == 0.0


@pytest.mark.asyncio
async def test_synthesize_surfaces_consistency_warnings() -> None:
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=1000)],
        "consistency_warnings": ["QA share is only 5% of total effort; expected 30-40%."],
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    assert final.consistency_warnings == [
        "QA share is only 5% of total effort; expected 30-40%."
    ]


@pytest.mark.asyncio
async def test_synthesize_defaults_consistency_warnings_to_empty() -> None:
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=1000)],
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    assert final.consistency_warnings == []
