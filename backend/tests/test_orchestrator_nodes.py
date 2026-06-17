"""Tests for orchestrator nodes with pure logic.

Skipped:
- parse_input (calls Claude; covered by integration test)
- await_user_answers (uses LangGraph interrupt; covered by end-to-end test)
- merge_pass2 (no-op)
"""

from __future__ import annotations

import math

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
from orchestrator.nodes.synthesize_estimate import (
    PHASE_CORRELATION,
    _combine_range,
    _combine_std,
    _distribute_team,
    _lognormal_band,
    _phase_correlation,
    synthesize_estimate,
)


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


def _dev_with_ksloc(ksloc: float) -> PhaseEstimate:
    # Development phase carrying a realized SLOC; QA share kept healthy by the caller's roster.
    return _phase(Phase.DEVELOPMENT, ai_mid=600).model_copy(update={"breakdown": {"ksloc": ksloc}})


@pytest.mark.asyncio
async def test_consistency_warns_when_dev_sloc_far_above_screen_envelope() -> None:
    # 10 screens caps net-new SLOC at ~700/screen; 20 KSLOC (2000/screen) is a gross over-count.
    state = {
        "pass2_estimates": [_dev_with_ksloc(20.0), _phase(Phase.QA_TESTING, ai_mid=300)],
        "stage2": Stage2Context(screen_count_estimate=10),
    }
    warns = (await consistency_check(state))["consistency_warnings"]
    assert any("boilerplate" in w and "/screen" in w for w in warns)


@pytest.mark.asyncio
async def test_consistency_warns_when_dev_sloc_far_below_screen_envelope() -> None:
    state = {
        "pass2_estimates": [_dev_with_ksloc(0.5), _phase(Phase.QA_TESTING, ai_mid=300)],
        "stage2": Stage2Context(screen_count_estimate=10),
    }
    warns = (await consistency_check(state))["consistency_warnings"]
    assert any("undersized" in w for w in warns)


@pytest.mark.asyncio
async def test_consistency_no_sloc_warning_when_dev_sloc_in_envelope() -> None:
    # 5 KSLOC for 10 screens (~500/screen) is squarely in the anchor band → no flag.
    state = {
        "pass2_estimates": [_dev_with_ksloc(5.0), _phase(Phase.QA_TESTING, ai_mid=300)],
        "stage2": Stage2Context(screen_count_estimate=10),
    }
    assert (await consistency_check(state))["consistency_warnings"] == []


@pytest.mark.asyncio
async def test_consistency_skips_sloc_check_without_screen_signal() -> None:
    # No screen count anywhere → nothing to check against, so even a huge SLOC is not flagged.
    state = {"pass2_estimates": [_dev_with_ksloc(50.0), _phase(Phase.QA_TESTING, ai_mid=300)]}
    assert (await consistency_check(state))["consistency_warnings"] == []


@pytest.mark.asyncio
async def test_consistency_sloc_check_falls_back_to_parsed_screen_count() -> None:
    # Screen count comes from parsed_context when stage2 is absent (mirrors code_review's order).
    state = {
        "pass2_estimates": [_dev_with_ksloc(20.0), _phase(Phase.QA_TESTING, ai_mid=300)],
        "parsed_context": {"screen_count_estimate": 10},
    }
    warns = (await consistency_check(state))["consistency_warnings"]
    assert any("boilerplate" in w for w in warns)


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
    # Brooks coordination overhead scales BOTH scenario costs by the same factor, so the
    # saving stays manual − ai and both reflect (1 + overhead).
    factor = 1 + final.brooks_overhead_pct / 100
    assert final.total_cost_ai_assisted_usd == pytest.approx(100_000 * factor)
    assert final.total_cost_manual_only_usd == pytest.approx(130_000 * factor)
    assert final.ai_cost_saved_usd == pytest.approx(30_000 * factor)
    # Confidence is the average of per-phase confidences.
    assert final.confidence == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_synthesize_applies_brooks_overhead_for_large_team() -> None:
    # A big project on a short timeline forces a large team → coordination overhead.
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=3000, manual_mid=3600)],
        "total_cost_ai_assisted_usd": 100_000,
        "total_cost_manual_only_usd": 130_000,
        "stage2": Stage2Context(target_timeline_weeks=8),
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    assert final.team_size > 5
    assert final.brooks_overhead_pct > 0
    assert final.optimal_team_size >= 1
    assert 0 < final.staffing_efficiency_pct <= 100
    factor = 1 + final.brooks_overhead_pct / 100
    # Brooks stretches both cost (vs the supplied base) and the target window.
    assert final.total_cost_ai_assisted_usd == pytest.approx(100_000 * factor)
    assert final.duration_weeks_high == pytest.approx(8 * 1.25 * factor)


@pytest.mark.asyncio
async def test_synthesize_no_overhead_when_team_within_free_size(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Coefficients with a high free-team-size disable coordination overhead → cost parity,
    # while the diminishing-returns efficiency readout is still emitted.
    async def _coeffs() -> dict[str, float]:
        return {"free_team_size": 1000.0}

    monkeypatch.setattr(
        "orchestrator.nodes.synthesize_estimate.get_staffing_coefficients", _coeffs
    )
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=1000)],
        "total_cost_ai_assisted_usd": 50_000,
        "total_cost_manual_only_usd": 60_000,
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    assert final.brooks_overhead_pct == 0.0
    assert final.total_cost_ai_assisted_usd == pytest.approx(50_000)  # unchanged by overhead
    assert final.staffing_efficiency_pct > 0


def test_distribute_team_staffs_each_active_role_by_effort() -> None:
    roles = [
        CustomRole(role_id="pm", description="PM", category=RoleCategory.PRODUCT,
                   seniority=RoleSeniority.SENIOR, rate_per_hour=100, percentage=10),
        CustomRole(role_id="eng", description="Eng", category=RoleCategory.ENGINEERING,
                   seniority=RoleSeniority.MID, rate_per_hour=100, percentage=70),
        CustomRole(role_id="qa", description="QA", category=RoleCategory.QA,
                   seniority=RoleSeniority.MID, rate_per_hour=100, percentage=20),
    ]
    hours = {"pm": 100.0, "eng": 700.0, "qa": 200.0}
    alloc = _distribute_team(6, hours, roles)
    assert sum(alloc.values()) == 6  # exact requested team size
    assert all(alloc[r.role_id] >= 1 for r in roles)  # every working role staffed
    assert alloc["eng"] == max(alloc.values())  # most effort → most heads
    # Roles with no hours stay unstaffed; the total floors at the #active roles.
    alloc2 = _distribute_team(1, {"pm": 0.0, "eng": 700.0, "qa": 200.0}, roles)
    assert alloc2["pm"] == 0
    assert sum(alloc2.values()) == 2


@pytest.mark.asyncio
async def test_synthesize_no_target_team_size_matches_headcount() -> None:
    # No target timeline → the recommended team is DISTRIBUTED across the roster, so the reported
    # team_size, the headcount table, and the Brooks overhead all describe ONE coherent team
    # (regression: team_size used to be the decoupled `optimal`, not the sum of the table).
    state = {
        "pass2_estimates": [_phase(Phase.DEVELOPMENT, ai_mid=4000, manual_mid=4800)],
        "stage2": Stage2Context(),  # no target_timeline_weeks → no-target regime
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    table_total = sum(h.headcount for h in final.headcount_by_role)
    assert final.team_size == table_total
    assert final.team_size >= 1
    assert 0 <= final.brooks_overhead_pct
    assert 0 < final.staffing_efficiency_pct <= 100
    assert final.duration_weeks_high >= final.duration_weeks_low > 0


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


# ---- synthesize_estimate: variance-combine (_combine_range / _lognormal_band) ----
#
# The Monte Carlo path independence-combines per-phase ranges (variances add in
# quadrature) instead of the comonotonic Σ-percentile sum, so the project band is
# NARROWER. The legacy/stub path (no std) must reproduce the comonotonic sum exactly.


def _mc_phase(
    phase: Phase, *, manual_mid: float, std: float, reduction: float = 0.3
) -> PhaseEstimate:
    """A phase whose hour ranges carry Monte Carlo stats (std/mean set), mirroring
    montecarlo.result_to_hour_range. ai.most_likely == manual.most_likely×(1-r)."""
    ai_mid = manual_mid * (1.0 - reduction)

    def _rng(mid: float) -> HourRange:
        return HourRange(
            optimistic=max(0.0, mid - 1.2816 * std),
            most_likely=mid,
            pessimistic=mid + 1.2816 * std,
            std=std,
            mean=mid,
        )

    return PhaseEstimate(
        phase=phase,
        twin_name=f"{phase.value}_twin",
        algorithm="mc",
        ai_assisted_hours=_rng(ai_mid),
        manual_only_hours=_rng(manual_mid),
        ai_assisted_role_hours=_default_role_hours(ai_mid),
        manual_only_role_hours=_default_role_hours(manual_mid),
        confidence=0.7,
        effective_ai_reduction_pct=reduction * 100,
    )


def test_combine_range_mc_band_is_narrower_than_comonotonic_sum() -> None:
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
        _mc_phase(Phase.QA_TESTING, manual_mid=300, std=60),
    ]
    # Pin ρ=0 so this exercises the pure-independence variance-combine regardless
    # of the PHASE_CORRELATION default.
    combined = _combine_range(phases, ai=False, rho=0.0)

    # most_likely is always the comonotonic Σ of mids.
    assert combined.most_likely == pytest.approx(200 + 800 + 300)
    # Independence-combine: std is the root-sum-square, strictly less than the
    # linear sum of stds.
    expected_std = (40**2 + 120**2 + 60**2) ** 0.5
    assert combined.std == pytest.approx(expected_std)
    assert expected_std < (40 + 120 + 60)

    # The MC band is NARROWER than the comonotonic Σ-percentile sum of the same
    # phases (the comonotonic pessimistic adds every per-phase upper bound).
    comonotonic_pess = sum(p.manual_only_hours.pessimistic for p in phases)
    comonotonic_opt = sum(p.manual_only_hours.optimistic for p in phases)
    assert combined.pessimistic < comonotonic_pess
    assert combined.optimistic > comonotonic_opt
    assert (combined.pessimistic - combined.optimistic) < (comonotonic_pess - comonotonic_opt)

    # Fan-chart percentiles are materialized and ordered.
    assert combined.percentiles is not None
    pcts = combined.percentiles
    assert pcts["p5"] <= pcts["p10"] <= pcts["p50"] <= pcts["p90"] <= pcts["p95"]
    # optimistic/pessimistic align with P10/P90.
    assert combined.optimistic == pytest.approx(pcts["p10"])
    assert combined.pessimistic == pytest.approx(pcts["p90"])
    # most_likely stays inside the band.
    assert combined.optimistic <= combined.most_likely <= combined.pessimistic


# ---- synthesize_estimate: cross-phase correlation (ρ-blend) ----
#
# total_std = sqrt( (1−ρ)·Σstd_i²  +  ρ·(Σstd_i)² ). ρ=0 → independence (variances
# add in quadrature); ρ=1 → comonotonic (std's add linearly); 0<ρ<1 lies strictly
# between (wider band than independence, narrower than comonotonic).


def test_combine_std_rho_zero_is_independence_quadrature() -> None:
    stds = [40.0, 120.0, 60.0]
    assert _combine_std(stds, 0.0) == pytest.approx((40**2 + 120**2 + 60**2) ** 0.5)


def test_combine_std_rho_one_is_linear_sum() -> None:
    stds = [40.0, 120.0, 60.0]
    assert _combine_std(stds, 1.0) == pytest.approx(40 + 120 + 60)


def test_combine_std_intermediate_rho_lies_strictly_between() -> None:
    stds = [40.0, 120.0, 60.0]
    indep = _combine_std(stds, 0.0)
    comon = _combine_std(stds, 1.0)
    blended = _combine_std(stds, 0.3)
    assert indep < blended < comon
    # Closed form for ρ=0.3: sqrt(0.7·Σstd² + 0.3·(Σstd)²).
    expected = (0.7 * (40**2 + 120**2 + 60**2) + 0.3 * (40 + 120 + 60) ** 2) ** 0.5
    assert blended == pytest.approx(expected)


def test_combine_std_is_monotonic_increasing_in_rho() -> None:
    stds = [40.0, 120.0, 60.0]
    vals = [_combine_std(stds, r) for r in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)]
    assert vals == sorted(vals)
    # Strictly increasing while >1 phase carries nonzero std.
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))


def test_combine_range_rho_zero_equals_independence_combine() -> None:
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
        _mc_phase(Phase.QA_TESTING, manual_mid=300, std=60),
    ]
    combined = _combine_range(phases, ai=False, rho=0.0)
    assert combined.std == pytest.approx((40**2 + 120**2 + 60**2) ** 0.5)
    assert combined.most_likely == pytest.approx(200 + 800 + 300)


def test_combine_range_rho_one_approaches_comonotonic_linear_std_sum() -> None:
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
        _mc_phase(Phase.QA_TESTING, manual_mid=300, std=60),
    ]
    combined = _combine_range(phases, ai=False, rho=1.0)
    # ρ=1: the combined std is the linear sum of the per-phase std's.
    assert combined.std == pytest.approx(40 + 120 + 60)


def test_combine_range_intermediate_rho_band_between_independence_and_comonotonic() -> None:
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
        _mc_phase(Phase.QA_TESTING, manual_mid=300, std=60),
    ]
    indep = _combine_range(phases, ai=False, rho=0.0)
    comon = _combine_range(phases, ai=False, rho=1.0)
    blended = _combine_range(phases, ai=False, rho=0.3)

    # Combined std strictly between the two extremes.
    assert indep.std is not None and comon.std is not None and blended.std is not None
    assert indep.std < blended.std < comon.std

    # The lognormal band widens monotonically with the std, so the blended band is
    # wider than independence and narrower than comonotonic; the deterministic mid
    # is shared across all three.
    assert blended.most_likely == pytest.approx(indep.most_likely)
    indep_width = indep.pessimistic - indep.optimistic
    comon_width = comon.pessimistic - comon.optimistic
    blended_width = blended.pessimistic - blended.optimistic
    assert indep_width < blended_width < comon_width
    # And the blended band stays narrower than the raw comonotonic Σ-percentile sum.
    raw_comonotonic = sum(p.manual_only_hours.pessimistic for p in phases) - sum(
        p.manual_only_hours.optimistic for p in phases
    )
    assert blended_width < raw_comonotonic


def test_combine_range_uses_phase_correlation_default(monkeypatch) -> None:
    # With no rho kwarg, _combine_range reads _phase_correlation() (the
    # PHASE_CORRELATION default / env override).
    monkeypatch.delenv("PHASE_CORRELATION", raising=False)
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
    ]
    default_combined = _combine_range(phases, ai=False)
    explicit_combined = _combine_range(phases, ai=False, rho=PHASE_CORRELATION)
    assert default_combined.std == pytest.approx(explicit_combined.std)
    # The default (0.3) sits strictly above the independence std.
    assert default_combined.std is not None
    assert default_combined.std > (40**2 + 120**2) ** 0.5


def test_phase_correlation_env_override_and_clamp(monkeypatch) -> None:
    monkeypatch.setenv("PHASE_CORRELATION", "0.5")
    assert _phase_correlation() == pytest.approx(0.5)
    # Out-of-range values clamp to [0, 1].
    monkeypatch.setenv("PHASE_CORRELATION", "5")
    assert _phase_correlation() == pytest.approx(1.0)
    monkeypatch.setenv("PHASE_CORRELATION", "-2")
    assert _phase_correlation() == pytest.approx(0.0)
    # Unparseable → falls back to the module default.
    monkeypatch.setenv("PHASE_CORRELATION", "not-a-number")
    assert _phase_correlation() == pytest.approx(PHASE_CORRELATION)
    # Unset → module default.
    monkeypatch.delenv("PHASE_CORRELATION", raising=False)
    assert _phase_correlation() == pytest.approx(PHASE_CORRELATION)


def test_combine_range_legacy_path_equals_comonotonic_sum() -> None:
    # No std anywhere → must reproduce the old _sum_range behavior exactly.
    phases = [
        _phase(Phase.DISCOVERY, ai_mid=200, manual_mid=240),
        _phase(Phase.DEVELOPMENT, ai_mid=800, manual_mid=1000),
    ]
    combined = _combine_range(phases, ai=False)
    assert combined.optimistic == pytest.approx(240 * 0.8 + 1000 * 0.8)
    assert combined.most_likely == pytest.approx(240 + 1000)
    assert combined.pessimistic == pytest.approx(240 * 1.3 + 1000 * 1.3)
    assert combined.std is None
    assert combined.mean is None
    assert combined.percentiles is None


def test_combine_range_mixed_std_falls_back_to_comonotonic() -> None:
    # If ANY phase range lacks std, the whole combine uses the legacy path.
    mc = _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120)
    legacy = _phase(Phase.DISCOVERY, ai_mid=140, manual_mid=200)  # no std
    combined = _combine_range([mc, legacy], ai=False)
    assert combined.std is None
    assert combined.percentiles is None
    assert combined.most_likely == pytest.approx(800 + 200)
    assert combined.pessimistic == pytest.approx(
        mc.manual_only_hours.pessimistic + legacy.manual_only_hours.pessimistic
    )


def test_combine_range_empty_phases_is_zero() -> None:
    combined = _combine_range([], ai=True)
    assert combined.optimistic == 0
    assert combined.most_likely == 0
    assert combined.pessimistic == 0


@pytest.mark.asyncio
async def test_synthesize_uses_variance_combine_for_mc_phases(monkeypatch) -> None:
    # Pin ρ=0 via the env override so the end-to-end std equals the pure
    # independence root-sum-square (synthesize_estimate has no rho kwarg).
    monkeypatch.setenv("PHASE_CORRELATION", "0")
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=40),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=120),
    ]
    state = {
        "pass2_estimates": phases,
        "stage2": Stage2Context(target_timeline_weeks=20),
    }
    final = (await synthesize_estimate(state))["final_estimate"]
    total = final.total_manual_only_hours
    assert total.most_likely == pytest.approx(1000)
    assert total.std == pytest.approx((40**2 + 120**2) ** 0.5)
    assert total.percentiles is not None
    # Narrower than the comonotonic pessimistic sum.
    assert total.pessimistic < sum(p.manual_only_hours.pessimistic for p in phases)


# ---- _lognormal_band direct unit tests ----


def test_lognormal_band_normal_case_ordering_and_no_naninf() -> None:
    lo, hi, pcts = _lognormal_band(mean=1000.0, std=150.0, anchor=1000.0)
    assert math.isfinite(lo) and math.isfinite(hi)
    assert 0 < lo < 1000.0 < hi
    assert pcts is not None
    vals = [pcts[k] for k in ("p5", "p10", "p25", "p50", "p75", "p90", "p95")]
    assert vals == sorted(vals)
    assert all(math.isfinite(v) and v > 0 for v in vals)
    # P10/P90 are returned as optimistic/pessimistic.
    assert lo == pytest.approx(pcts["p10"])
    assert hi == pytest.approx(pcts["p90"])
    # P50 (median) of a lognormal = exp(mu) < mean.
    assert pcts["p50"] < 1000.0


def test_lognormal_band_zero_mean_guard_symmetric_band() -> None:
    # mean<=1e-9 → symmetric ±std band around the anchor, percentiles None.
    lo, hi, pcts = _lognormal_band(mean=0.0, std=50.0, anchor=120.0)
    assert lo == pytest.approx(70.0)
    assert hi == pytest.approx(170.0)
    assert pcts is None
    assert math.isfinite(lo) and math.isfinite(hi)


def test_lognormal_band_zero_mean_clamps_low_at_zero() -> None:
    lo, hi, pcts = _lognormal_band(mean=0.0, std=200.0, anchor=120.0)
    assert lo == 0.0  # max(0, 120-200)
    assert hi == pytest.approx(320.0)
    assert pcts is None


def test_lognormal_band_degenerate_zero_std_collapses_to_point() -> None:
    # cv^2 ~ 0 → lo == hi == anchor, percentiles None, no NaN/inf.
    lo, hi, pcts = _lognormal_band(mean=500.0, std=0.0, anchor=500.0)
    assert lo == pytest.approx(500.0)
    assert hi == pytest.approx(500.0)
    assert pcts is None


def test_combine_range_degenerate_zero_std_no_naninf() -> None:
    # All phases std=0 (degenerate) → combined std 0, band collapses to the mid,
    # no NaN/inf anywhere.
    phases = [
        _mc_phase(Phase.DISCOVERY, manual_mid=200, std=0.0),
        _mc_phase(Phase.DEVELOPMENT, manual_mid=800, std=0.0),
    ]
    combined = _combine_range(phases, ai=False)
    assert combined.std == pytest.approx(0.0)
    assert math.isfinite(combined.optimistic)
    assert math.isfinite(combined.pessimistic)
    assert combined.optimistic == pytest.approx(1000.0)
    assert combined.pessimistic == pytest.approx(1000.0)
    assert combined.most_likely == pytest.approx(1000.0)
