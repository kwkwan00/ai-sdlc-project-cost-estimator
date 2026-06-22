"""Offline, deterministic tests for the evals harness.

These run with NO ANTHROPIC_API_KEY: every LLM (judge) call is monkeypatched. They
cover the applicability matrix, the deterministic correctness rubrics (one passing
and one failing construction each), the estimate_accuracy banding + skip path, the
judge-rubric score mapping, runner aggregation (including skipped-score exclusion),
and the reporter.
"""

from __future__ import annotations

import pytest

from evals import models, report, rubrics, runner
from evals.agents import _is_stub_estimate  # noqa: F401  (behavior under test)
from evals.models import (
    AGENT_RUBRICS,
    RUBRIC_THRESHOLDS,
    AgentSample,
    EvalCase,
)
from models.project_schema import AiToolingLevel, PhaseToolingLevels, RoleRoster
from models.twin_outputs import (
    Gap,
    HourRange,
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)
from orchestrator.nodes._twin_base import stub_phase_estimate
from prefill import IndustryOption, NormalizedProjectContext, RegulatoryRequirement
from tooling_classifier import ToolingClassification

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _role(role_id: str, cat: RoleCategory, sen: RoleSeniority, hours: float) -> RoleHours:
    return RoleHours(
        role_id=role_id, role_description=role_id, category=cat, seniority=sen, hours=hours
    )


def _make_estimate(
    *,
    phase: Phase = Phase.DEVELOPMENT,
    manual_ml: float = 100.0,
    ai_ml: float = 70.0,
    reduction_pct: float = 30.0,
    ai_roles: list[RoleHours] | None = None,
    manual_roles: list[RoleHours] | None = None,
    breakdown: dict[str, float] | None = None,
) -> PhaseEstimate:
    """A PhaseEstimate whose hour points satisfy ai = manual×(1-r) by default."""
    return PhaseEstimate(
        phase=phase,
        twin_name=phase.value,
        algorithm="X",
        manual_only_hours=HourRange(
            optimistic=manual_ml * 0.8, most_likely=manual_ml, pessimistic=manual_ml * 1.3
        ),
        ai_assisted_hours=HourRange(
            optimistic=ai_ml * 0.8, most_likely=ai_ml, pessimistic=ai_ml * 1.3
        ),
        manual_only_role_hours=manual_roles
        or [_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, manual_ml)],
        ai_assisted_role_hours=ai_roles
        or [_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, ai_ml)],
        confidence=0.8,
        effective_ai_reduction_pct=reduction_pct,
        breakdown=breakdown or {},
    )


def _roster(*roles: tuple[str, RoleCategory, RoleSeniority, float]) -> RoleRoster:
    from models.project_schema import CustomRole

    return RoleRoster(
        roles=[
            CustomRole(role_id=rid, description=rid, category=c, seniority=s, percentage=p)
            for rid, c, s, p in roles
        ]
    )


def _twin_sample(
    est: PhaseEstimate,
    *,
    phase: Phase,
    tooling: str,
    roster: RoleRoster | None = None,
    gold: dict | None = None,
) -> AgentSample:
    r = roster or _roster(("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0))
    return AgentSample(
        case_id="c",
        agent=phase.value,
        output_obj=est,
        gold=gold or {},
        eval_context={
            "phase": phase.value,
            "tooling_level": tooling,
            "reduction_bands": {},
            "roster": r.model_dump(),
        },
    )


async def _fake_verdict(score_value: float):
    async def _call(*, system, user, response_model, tool_name, model, **kwargs):  # type: ignore[no-untyped-def]
        return response_model(reasoning="ok", score=score_value)

    return _call


# --------------------------------------------------------------------------- #
# Applicability matrix
# --------------------------------------------------------------------------- #


def test_agent_rubrics_matrix_matches_spec() -> None:
    twins = {"discovery", "ux_design", "development", "code_review", "deployment", "qa_testing"}
    assert set(AGENT_RUBRICS) == set(models.ALL_AGENTS)
    assert len(AGENT_RUBRICS) == 11  # 6 twins + prefill/roster/tooling/consolidator/wbs

    # The deleted RAG rubrics appear nowhere.
    for rubric_list in AGENT_RUBRICS.values():
        assert "context_precision" not in rubric_list
        assert "contextual_recall" not in rubric_list

    twin_set = [
        "json_correctness",
        "faithfulness",
        "band_adherence",
        "algorithm_conformance",
        "role_attribution_validity",
        "estimate_accuracy",
        "interval_calibration",
        "consistency",
    ]
    for agent in twins:
        assert AGENT_RUBRICS[agent] == twin_set

    assert AGENT_RUBRICS["prefill"] == ["summarization", "extraction_accuracy"]
    assert AGENT_RUBRICS["roster"] == [
        "plan_quality",
        "faithfulness",
        "staffing_adequacy",
        "roster_catalog_selection",
    ]
    assert AGENT_RUBRICS["tooling"] == [
        "classification_accuracy",
        "enum_constraint_adherence",
        "consistency",
    ]
    assert AGENT_RUBRICS["consolidator"] == ["plan_quality", "partition_correctness"]
    assert AGENT_RUBRICS["wbs"] == ["wbs_structural", "plan_quality"]


# --------------------------------------------------------------------------- #
# json_correctness
# --------------------------------------------------------------------------- #


async def test_json_correctness_valid_estimate_scores_one() -> None:
    est = stub_phase_estimate(Phase.DISCOVERY, "discovery", "UCP", 200, 240, RoleRoster.default())
    sample = AgentSample(case_id="c1", agent="discovery", output_obj=est, is_stub=False)
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.score == 1.0
    assert result.passed is True


async def test_json_correctness_stub_flagged_fails() -> None:
    est = stub_phase_estimate(Phase.DISCOVERY, "discovery", "UCP", 200, 240, RoleRoster.default())
    sample = AgentSample(case_id="c1", agent="discovery", output_obj=est, is_stub=True)
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "stub" in result.reasoning.lower()


async def test_json_correctness_missing_output_scores_zero() -> None:
    sample = AgentSample(case_id="c1", agent="qa_testing", output_obj=None, error="boom")
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.score == 0.0
    assert result.passed is False


# --------------------------------------------------------------------------- #
# band_adherence
# --------------------------------------------------------------------------- #


async def test_band_adherence_passes_within_band() -> None:
    # development+agentic band is (0.36, 0.66); 30% is inside. ai<manual consistent.
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=70, reduction_pct=30)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is True


async def test_band_adherence_fails_reduction_above_band() -> None:
    # 80% reduction exceeds development+agentic hi (0.66).
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=20, reduction_pct=80)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False
    assert "outside" in result.reasoning


async def test_band_adherence_fails_sign_inconsistency() -> None:
    # Positive reduction but AI scenario is HEAVIER than manual — contradiction.
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=70, ai_ml=100, reduction_pct=30)
    # ai = manual×(1-r) won't hold, but band_adherence only checks sign here.
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False


async def test_band_adherence_none_tooling_requires_zero_reduction() -> None:
    # NONE tooling → hi=0 → any nonzero reduction must fail.
    est = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=80, reduction_pct=20)
    sample = _twin_sample(est, phase=Phase.DISCOVERY, tooling="none")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False

    est_zero = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=100, reduction_pct=0)
    sample_zero = _twin_sample(est_zero, phase=Phase.DISCOVERY, tooling="none")
    ok = await rubrics.score("band_adherence", sample_zero, judge_model="x")
    assert ok.passed is True


async def test_band_adherence_autocomplete_on_discovery_has_no_band() -> None:
    # discovery has no AUTOCOMPLETE band → reduction must be zero.
    est = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=90, reduction_pct=10)
    sample = _twin_sample(est, phase=Phase.DISCOVERY, tooling="autocomplete")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False


# --------------------------------------------------------------------------- #
# algorithm_conformance
# --------------------------------------------------------------------------- #


async def test_algorithm_conformance_passes_identity() -> None:
    # New-shape range: most_likely identity holds exactly (ai=70=100×0.7), PERT
    # ordering valid, ai≤manual at every percentile. MC extras (std/percentiles)
    # may be present or absent — the rubric reads only the three points.
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30, breakdown={"sloc": 1000.0})
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_algorithm_conformance_fails_broken_most_likely_identity() -> None:
    # The most_likely identity is STILL exact: r=30% so expected ai_ml=70, but 40.
    est = _make_estimate(manual_ml=100, ai_ml=40, reduction_pct=30)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is False
    assert result.score < 1.0


async def test_algorithm_conformance_fails_sign_violation() -> None:
    # r>=0 but the AI band crosses ABOVE manual at a percentile (ai pessimistic >
    # manual pessimistic): the dropped per-percentile equality used to mask this;
    # the new sign check (iv) catches it. Build it by hand so most_likely still
    # satisfies the (kept) identity while the upper tail violates ai<=manual.
    est = PhaseEstimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development",
        algorithm="X",
        manual_only_hours=HourRange(optimistic=80, most_likely=100, pessimistic=130),
        # most_likely=70=100×(1-0.3) ✓ but pessimistic 300 >> manual 130 ✗.
        ai_assisted_hours=HourRange(optimistic=56, most_likely=70, pessimistic=300),
        manual_only_role_hours=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100)],
        ai_assisted_role_hours=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 70)],
        confidence=0.8,
        effective_ai_reduction_pct=30.0,
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is False
    assert result.score < 1.0
    assert "manual" in result.reasoning


async def test_algorithm_conformance_allows_per_percentile_band_spread() -> None:
    # Regression guard: the OLD rubric required ai==manual×(1-r) at optimistic AND
    # pessimistic. With MC variance the tails legitimately diverge from that exact
    # identity while staying ai<=manual; the reworked rubric must still score 1.0.
    est = PhaseEstimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development",
        algorithm="X",
        manual_only_hours=HourRange(
            optimistic=70, most_likely=100, pessimistic=150, std=25.0, mean=104.0
        ),
        # most_likely=70 (identity holds); tails are NOT 70×(0.7..1.3) but ai<=manual.
        ai_assisted_hours=HourRange(
            optimistic=44, most_likely=70, pessimistic=120, std=22.0, mean=74.0
        ),
        manual_only_role_hours=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100)],
        ai_assisted_role_hours=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 70)],
        confidence=0.8,
        effective_ai_reduction_pct=30.0,
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_algorithm_conformance_fails_negative_breakdown() -> None:
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30, breakdown={"bad": -5.0})
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is False


# --------------------------------------------------------------------------- #
# interval_calibration (reference-based; skips without actuals)
# --------------------------------------------------------------------------- #


async def test_interval_calibration_skips_without_actuals() -> None:
    est = _make_estimate()
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", gold={})
    result = await rubrics.score("interval_calibration", sample, judge_model="x")
    assert result.skipped is True


async def test_interval_calibration_inside_band_scores_one() -> None:
    # manual band = [80, 130], ai band = [56, 91]; both actuals land inside.
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30)
    sample = _twin_sample(
        est,
        phase=Phase.DEVELOPMENT,
        tooling="agentic",
        gold={"actual_manual_ml": 110, "actual_ai_ml": 75},
    )
    result = await rubrics.score("interval_calibration", sample, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.passed is True
    assert result.skipped is False


async def test_interval_calibration_far_outside_scores_zero() -> None:
    # manual band = [80, 130]; actual 400 is >0.60 rel-distance from the nearer
    # bound (130) → banded score 0.0.
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30)
    sample = _twin_sample(
        est,
        phase=Phase.DEVELOPMENT,
        tooling="agentic",
        gold={"actual_manual_ml": 400},
    )
    result = await rubrics.score("interval_calibration", sample, judge_model="x")
    assert result.score == pytest.approx(0.0)
    assert result.passed is False


# --------------------------------------------------------------------------- #
# role_attribution_validity
# --------------------------------------------------------------------------- #


async def test_role_attribution_passes() -> None:
    roster = _roster(
        ("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60.0),
        ("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40.0),
    )
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40),
        ],
        ai_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 42),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 28),
        ],
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is True


async def test_role_attribution_fails_sum_mismatch() -> None:
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 50)],  # != 100
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "sum" in result.reasoning


async def test_role_attribution_fails_unknown_role_id() -> None:
    roster = _roster(("known", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0))
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[_role("ghost", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100)],
        ai_roles=[_role("ghost", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 70)],
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "not in roster" in result.reasoning


async def test_role_attribution_fails_code_review_junior_cap() -> None:
    roster = _roster(
        ("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60.0),
        ("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40.0),
    )
    # CODE_REVIEW caps juniors at 15%; here junior holds 40%.
    est = _make_estimate(
        phase=Phase.CODE_REVIEW,
        manual_ml=100,
        ai_ml=80,
        reduction_pct=20,
        manual_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40),
        ],
        ai_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 48),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 32),
        ],
    )
    sample = _twin_sample(est, phase=Phase.CODE_REVIEW, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "junior" in result.reasoning


# --------------------------------------------------------------------------- #
# estimate_accuracy (banding + skip)
# --------------------------------------------------------------------------- #


async def test_estimate_accuracy_skips_without_targets() -> None:
    est = _make_estimate()
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", gold={})
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.skipped is True


async def test_estimate_accuracy_perfect_when_on_target() -> None:
    est = _make_estimate(manual_ml=203, ai_ml=164, reduction_pct=19)
    sample = _twin_sample(
        est,
        phase=Phase.DISCOVERY,
        tooling="chat",
        gold={"target_manual_ml": 203, "target_ai_ml": 164},
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.skipped is False


async def test_estimate_accuracy_zero_when_far_off() -> None:
    # rel_err >= 0.60 → 0.0 for both targets.
    est = _make_estimate(manual_ml=20, ai_ml=10, reduction_pct=50)
    sample = _twin_sample(
        est,
        phase=Phase.DISCOVERY,
        tooling="chat",
        gold={"target_manual_ml": 203, "target_ai_ml": 164},
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx(0.0)


async def test_estimate_accuracy_banding_is_linear_midrange() -> None:
    # rel_err = 0.40 → (0.60-0.40)/(0.60-0.25) ≈ 0.571.
    est = _make_estimate(manual_ml=140, ai_ml=98, reduction_pct=30)
    sample = _twin_sample(
        est, phase=Phase.DEVELOPMENT, tooling="agentic", gold={"target_manual_ml": 100}
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx((0.60 - 0.40) / (0.60 - 0.25), abs=1e-3)


# --------------------------------------------------------------------------- #
# extraction_accuracy (prefill)
# --------------------------------------------------------------------------- #


def _prefill_obj(
    *, industry: IndustryOption, screens: int, integrations: list[str], regs: list[RegulatoryRequirement]
) -> NormalizedProjectContext:
    return NormalizedProjectContext(
        industry=industry,
        screen_count_estimate=screens,
        integrations=integrations,
        regulatory_requirements=regs,
        summary="s",
    )


async def test_extraction_accuracy_all_match() -> None:
    obj = _prefill_obj(
        industry=IndustryOption.HEALTHCARE,
        screens=25,
        integrations=["a", "b", "c", "d"],
        regs=[RegulatoryRequirement.HIPAA],
    )
    sample = AgentSample(
        case_id="c",
        agent="prefill",
        output_obj=obj,
        gold={
            "industry": "healthcare",
            "project_type": "greenfield",
            "regulatory_requirements": ["HIPAA"],
            "screen_count": 25,
            "integration_count": 4,
        },
    )
    result = await rubrics.score("extraction_accuracy", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_extraction_accuracy_fails_on_industry_mismatch() -> None:
    obj = _prefill_obj(
        industry=IndustryOption.FINTECH,  # wrong
        screens=25,
        integrations=["a", "b", "c", "d"],
        regs=[RegulatoryRequirement.HIPAA],
    )
    sample = AgentSample(
        case_id="c",
        agent="prefill",
        output_obj=obj,
        gold={
            "industry": "healthcare",
            "project_type": "greenfield",
            "regulatory_requirements": ["HIPAA"],
            "screen_count": 25,
            "integration_count": 4,
        },
    )
    result = await rubrics.score("extraction_accuracy", sample, judge_model="x")
    assert result.passed is False
    assert result.score < 1.0


# --------------------------------------------------------------------------- #
# staffing_adequacy (roster)
# --------------------------------------------------------------------------- #


class _FakeProposedRole:
    def __init__(self, category: RoleCategory, percentage: float) -> None:
        self.category = category
        self.percentage = percentage


class _FakeProposal:
    def __init__(self, roles: list[_FakeProposedRole]) -> None:
        self.roles = roles


async def test_staffing_adequacy_passes_with_required_categories() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 40),
            _FakeProposedRole(RoleCategory.PRODUCT, 20),
            _FakeProposedRole(RoleCategory.UI_UX, 20),
            _FakeProposedRole(RoleCategory.QA, 20),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 25, "regulatory": ["HIPAA"]}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is True


async def test_staffing_adequacy_fails_missing_qa_when_regulated() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 50),
            _FakeProposedRole(RoleCategory.PRODUCT, 30),
            _FakeProposedRole(RoleCategory.UI_UX, 20),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 25, "regulatory": ["HIPAA"]}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is False
    assert "qa" in result.reasoning


async def test_staffing_adequacy_fails_role_over_60pct() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 70),  # > 60%
            _FakeProposedRole(RoleCategory.PRODUCT, 30),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 0, "regulatory": []}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is False
    assert "60%" in result.reasoning


# --------------------------------------------------------------------------- #
# roster_catalog_selection (roster)
# --------------------------------------------------------------------------- #


def _roster_proposal(*catalog_ids: str | None):
    from roster_agent import ProposedRole, RosterProposal

    return RosterProposal(
        project_plan=[],
        staffing_rationale="r",
        roles=[
            ProposedRole(
                description=f"role {i}",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.SENIOR,
                percentage=100 / max(1, len(catalog_ids)),
                catalog_role_id=cid,
            )
            for i, cid in enumerate(catalog_ids)
        ],
    )


async def test_roster_catalog_selection_passes_when_agent_selects_gold_role() -> None:
    sample = AgentSample(
        case_id="c", agent="roster",
        output_obj=_roster_proposal("hipaa_compliance_lead", None),
        gold={"expected_catalog_role_id": "hipaa_compliance_lead"},
    )
    result = await rubrics.score("roster_catalog_selection", sample, judge_model="x")
    assert result.passed is True and not result.skipped


async def test_roster_catalog_selection_fails_when_gold_role_not_selected() -> None:
    sample = AgentSample(
        case_id="c", agent="roster",
        output_obj=_roster_proposal(None, "some_other_role"),
        gold={"expected_catalog_role_id": "hipaa_compliance_lead"},
    )
    result = await rubrics.score("roster_catalog_selection", sample, judge_model="x")
    assert result.passed is False and not result.skipped


async def test_roster_catalog_selection_skips_without_gold_or_output() -> None:
    # No gold target → not applicable (skip).
    s1 = AgentSample(case_id="c", agent="roster", output_obj=_roster_proposal(None))
    r1 = await rubrics.score("roster_catalog_selection", s1, judge_model="x")
    assert r1.skipped is True
    # Gold present but no LLM ran (e.g. CI without a key) → skip, not fail.
    s2 = AgentSample(
        case_id="c", agent="roster", output_obj=None, error="no api key",
        gold={"expected_catalog_role_id": "hipaa_compliance_lead"},
    )
    r2 = await rubrics.score("roster_catalog_selection", s2, judge_model="x")
    assert r2.skipped is True


# --------------------------------------------------------------------------- #
# wbs_structural (wbs planner)
# --------------------------------------------------------------------------- #


def _wbs_leaf(tid: str, phase: str, role_id: str, o: float, m: float, p: float):
    from models.wbs_task import WbsTaskInput

    return WbsTaskInput(
        id=tid, name=tid, phase=phase, role_id=role_id, optimistic=o, most_likely=m, pessimistic=p
    )


def _wbs_tree(*leaves):
    from models.wbs_task import WbsTaskInput

    return [WbsTaskInput(id="pkg", name="Work package", children=list(leaves))]


async def test_wbs_structural_passes_for_well_formed_tree() -> None:
    tree = _wbs_tree(
        _wbs_leaf("l1", "development", "sr_engineer", 4, 8, 16),
        _wbs_leaf("l2", "qa_testing", "sr_engineer", 2, 4, 8),
        _wbs_leaf("l3", "ux_design", "sr_product", 3, 6, 12),
    )
    sample = AgentSample(
        case_id="c", agent="wbs", output_obj=tree,
        eval_context={"roster_role_ids": ["sr_engineer", "sr_product"]},
    )
    result = await rubrics.score("wbs_structural", sample, judge_model="x")
    assert result.passed is True


async def test_wbs_structural_fails_role_not_in_roster() -> None:
    tree = _wbs_tree(
        _wbs_leaf("l1", "development", "ghost_role", 4, 8, 16),
        _wbs_leaf("l2", "qa_testing", "sr_engineer", 2, 4, 8),
        _wbs_leaf("l3", "ux_design", "sr_engineer", 3, 6, 12),
    )
    sample = AgentSample(
        case_id="c", agent="wbs", output_obj=tree,
        eval_context={"roster_role_ids": ["sr_engineer"]},
    )
    result = await rubrics.score("wbs_structural", sample, judge_model="x")
    assert result.passed is False and "ghost_role" in result.reasoning


async def test_wbs_structural_fails_single_phase_or_too_few_leaves() -> None:
    # All in one phase → fails the ≥2-phase check.
    one_phase = _wbs_tree(
        _wbs_leaf("l1", "development", "sr_engineer", 4, 8, 16),
        _wbs_leaf("l2", "development", "sr_engineer", 2, 4, 8),
        _wbs_leaf("l3", "development", "sr_engineer", 3, 6, 12),
    )
    r1 = await rubrics.score(
        "wbs_structural",
        AgentSample(case_id="c", agent="wbs", output_obj=one_phase,
                    eval_context={"roster_role_ids": ["sr_engineer"]}),
        judge_model="x",
    )
    assert r1.passed is False
    # Fewer than 3 leaves → fails.
    tiny = _wbs_tree(_wbs_leaf("l1", "development", "sr_engineer", 4, 8, 16))
    r2 = await rubrics.score(
        "wbs_structural",
        AgentSample(case_id="c", agent="wbs", output_obj=tiny,
                    eval_context={"roster_role_ids": ["sr_engineer"]}),
        judge_model="x",
    )
    assert r2.passed is False


# --------------------------------------------------------------------------- #
# classification_accuracy + enum_constraint_adherence (tooling)
# --------------------------------------------------------------------------- #


def _tooling_obj(**levels: str) -> ToolingClassification:
    return ToolingClassification(
        ai_tooling=PhaseToolingLevels(**{k: AiToolingLevel(v) for k, v in levels.items()})
    )


async def test_classification_accuracy_exact_match() -> None:
    obj = _tooling_obj(development="agentic", code_review="agentic", ux_design="chat")
    sample = AgentSample(
        case_id="c",
        agent="tooling",
        output_obj=obj,
        gold={
            "ai_tooling": {
                "discovery": "none",
                "ux_design": "chat",
                "development": "agentic",
                "code_review": "agentic",
                "deployment": "none",
                "qa_testing": "none",
            }
        },
    )
    result = await rubrics.score("classification_accuracy", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_classification_accuracy_partial_mismatch() -> None:
    obj = _tooling_obj(development="chat")  # gold wants agentic
    sample = AgentSample(
        case_id="c",
        agent="tooling",
        output_obj=obj,
        gold={
            "ai_tooling": {
                "discovery": "none",
                "ux_design": "none",
                "development": "agentic",
                "code_review": "none",
                "deployment": "none",
                "qa_testing": "none",
            }
        },
    )
    result = await rubrics.score("classification_accuracy", sample, judge_model="x")
    assert result.passed is False
    assert result.score == pytest.approx(5 / 6)


async def test_enum_constraint_adherence_passes_clean() -> None:
    obj = _tooling_obj(development="autocomplete", deployment="autocomplete")
    sample = AgentSample(case_id="c", agent="tooling", output_obj=obj)
    result = await rubrics.score("enum_constraint_adherence", sample, judge_model="x")
    assert result.passed is True


async def test_enum_constraint_adherence_fails_autocomplete_on_discovery() -> None:
    obj = _tooling_obj(discovery="autocomplete")
    sample = AgentSample(case_id="c", agent="tooling", output_obj=obj)
    result = await rubrics.score("enum_constraint_adherence", sample, judge_model="x")
    assert result.passed is False
    assert "autocomplete" in result.reasoning


# --------------------------------------------------------------------------- #
# partition_correctness (consolidator)
# --------------------------------------------------------------------------- #


def _gap(topic: str) -> Gap:
    return Gap(topic=topic, question_text=topic, impact_hours=10, suggested_default="d")


async def test_partition_correctness_passes() -> None:
    # 3 merged outputs, gold expects 3 clusters; phase coverage preserved.
    merged = [
        (_gap("a"), [Phase.DEVELOPMENT]),
        (_gap("b"), [Phase.DEPLOYMENT]),
        (_gap("c"), [Phase.QA_TESTING]),
    ]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0], [1], [2]]},
        eval_context={
            "input_phases": [["development"], ["deployment"], ["qa_testing"]]
        },
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is True


async def test_partition_correctness_fails_wrong_cluster_count() -> None:
    merged = [(_gap("a"), [Phase.DEVELOPMENT]), (_gap("b"), [Phase.DEPLOYMENT])]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0, 1], [2]]},  # expects 2 clusters but from 3 inputs
        eval_context={"input_phases": [["development"], ["deployment"]]},
    )
    # output count 2 == gold count 2 here, so make a real mismatch:
    sample.gold = {"clusters": [[0], [1], [2]]}  # expects 3
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "count" in result.reasoning


async def test_partition_correctness_fails_dropped_phase() -> None:
    merged = [(_gap("a"), [Phase.DEVELOPMENT]), (_gap("b"), [Phase.DEPLOYMENT])]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0], [1]]},
        eval_context={
            # qa_testing in inputs but never appears in merged output → dropped.
            "input_phases": [["development"], ["deployment", "qa_testing"]]
        },
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "dropped" in result.reasoning


# --------------------------------------------------------------------------- #
# merge_pass1._consolidate_with_partition surfaces the cluster→index mapping
# --------------------------------------------------------------------------- #


class _FakeMergeSettings:
    def __init__(self, key: str) -> None:
        self.anthropic_api_key = key
        self.anthropic_model_merge = "claude-haiku-4-5"


async def test_consolidate_with_partition_returns_llm_mapping(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.nodes import merge_pass1 as mp

    cands = [
        (_gap("function_points"), [Phase.DEVELOPMENT]),
        (_gap("dr_posture"), [Phase.DEPLOYMENT]),
        (_gap("ksloc"), [Phase.CODE_REVIEW]),
    ]
    monkeypatch.setattr(mp, "get_settings", lambda: _FakeMergeSettings("test-key"))

    async def _fake_call(**kwargs):  # type: ignore[no-untyped-def]
        return mp._ConsolidationResult(
            clusters=[
                mp._GapCluster(member_indices=[0, 2], merged_question="dev sizing?"),
                mp._GapCluster(member_indices=[1], merged_question="DR?"),
            ]
        )

    monkeypatch.setattr(mp, "call_structured", _fake_call)

    merged, partition = await mp._consolidate_with_partition(cands)
    assert partition == [[0, 2], [1]]  # the exact cluster→input-index mapping
    assert len(merged) == 2  # runtime behavior unchanged (merged list still produced)


async def test_consolidate_with_partition_identity_on_degrade(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.nodes import merge_pass1 as mp

    cands = [
        (_gap("a"), [Phase.DEVELOPMENT]),
        (_gap("b"), [Phase.DEPLOYMENT]),
        (_gap("c"), [Phase.QA_TESTING]),
    ]
    # No API key → LLM skipped → identity partition + unchanged candidates.
    monkeypatch.setattr(mp, "get_settings", lambda: _FakeMergeSettings(""))
    merged, partition = await mp._consolidate_with_partition(cands)
    assert partition == [[0], [1], [2]]
    assert merged == cands


# --------------------------------------------------------------------------- #
# partition_correctness EXACT mode (predicted_partition recorded by the adapter)
# --------------------------------------------------------------------------- #


def _consolidator_sample(
    *,
    predicted: list[list[int]],
    gold_clusters: list[list[int]],
    n_merged: int,
) -> AgentSample:
    """A consolidator sample carrying an explicit predicted partition (the mapping
    the merge_pass1 wrapper surfaces) so partition_correctness scores it EXACTLY."""
    merged = [(_gap(chr(ord("a") + i)), [Phase.DEVELOPMENT]) for i in range(n_merged)]
    return AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": gold_clusters},
        eval_context={"predicted_partition": predicted},
    )


async def test_partition_correctness_exact_perfect_scores_one() -> None:
    # Predicted partition == gold partition → pairwise-F1 1.0.
    sample = _consolidator_sample(
        predicted=[[0, 1], [2], [3, 4]],
        gold_clusters=[[0, 1], [2], [3, 4]],
        n_merged=3,
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.passed is True
    assert "pairwise-F1" in result.reasoning


async def test_partition_correctness_exact_lost_question_below_one() -> None:
    # Gold merges {0,1,2}; predicted splits 2 out (a "lost" co-membership) → recall
    # drops, score < 1.0.
    sample = _consolidator_sample(
        predicted=[[0, 1], [2], [3]],
        gold_clusters=[[0, 1, 2], [3]],
        n_merged=3,
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.score < 1.0
    assert result.passed is False


async def test_partition_correctness_exact_spurious_merge_below_one() -> None:
    # Gold keeps {0,1,2} apart; predicted spuriously merges {0,1} → precision drops.
    sample = _consolidator_sample(
        predicted=[[0, 1], [2], [3]],
        gold_clusters=[[0], [1], [2], [3]],
        n_merged=3,
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.score < 1.0
    assert result.passed is False


async def test_partition_correctness_exact_non_cover_hard_fails() -> None:
    # Predicted partition drops index 3 entirely → not an exact cover → hard fail.
    sample = _consolidator_sample(
        predicted=[[0, 1], [2]],
        gold_clusters=[[0, 1], [2], [3]],
        n_merged=2,
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "cover" in result.reasoning


async def test_partition_correctness_falls_back_to_proxy_without_mapping() -> None:
    # No predicted_partition in eval_context → proxy path (count + coverage).
    merged = [(_gap("a"), [Phase.DEVELOPMENT]), (_gap("b"), [Phase.DEPLOYMENT])]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0], [1]]},
        eval_context={"input_phases": [["development"], ["deployment"]]},
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is True
    assert "proxy" in result.reasoning


# --------------------------------------------------------------------------- #
# consistency rubric (multi-sample; deterministic)
# --------------------------------------------------------------------------- #


def _twin_consistency_sample(manual_ml: float, *, agent: str = "development") -> AgentSample:
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=manual_ml, ai_ml=manual_ml * 0.7)
    return AgentSample(case_id="c", agent=agent, output_obj=est, is_stub=False)


async def test_consistency_skips_single_run() -> None:
    result = await rubrics.score("consistency", _twin_consistency_sample(100), judge_model="x")
    assert result.skipped is True


async def test_consistency_identical_runs_scores_one() -> None:
    samples = [_twin_consistency_sample(120.0) for _ in range(4)]
    result = await rubrics.score_multi("consistency", samples, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.passed is True
    assert result.skipped is False


async def test_consistency_divergent_runs_below_one() -> None:
    # manual_ml swings 80 → 240 between identical runs → high CoV → low score.
    samples = [_twin_consistency_sample(v) for v in (80.0, 160.0, 240.0)]
    result = await rubrics.score_multi("consistency", samples, judge_model="x")
    assert result.score < 1.0
    assert result.passed is False


async def test_consistency_fails_on_stub_run() -> None:
    good = _twin_consistency_sample(100.0)
    stub = _twin_consistency_sample(100.0)
    stub.is_stub = True
    result = await rubrics.score_multi("consistency", [good, stub], judge_model="x")
    assert result.passed is False
    assert "consistency" in result.reasoning.lower() or "stub" in result.reasoning.lower()


async def test_consistency_tooling_label_agreement() -> None:
    # All runs agree on every phase → 1.0.
    agree = [
        AgentSample(
            case_id="c",
            agent="tooling",
            output_obj=_tooling_obj(development="agentic", code_review="chat"),
        )
        for _ in range(3)
    ]
    result = await rubrics.score_multi("consistency", agree, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.passed is True

    # One run flips development → disagreement on 1/6 phases → score 5/6.
    disagree = [
        AgentSample(case_id="c", agent="tooling", output_obj=_tooling_obj(development="agentic")),
        AgentSample(case_id="c", agent="tooling", output_obj=_tooling_obj(development="chat")),
    ]
    result2 = await rubrics.score_multi("consistency", disagree, judge_model="x")
    assert result2.score == pytest.approx(5 / 6)
    assert "development" in result2.reasoning


# --------------------------------------------------------------------------- #
# Judge rubrics (monkeypatched judge_structured)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rubric", ["faithfulness", "plan_quality", "summarization"])
async def test_judge_rubric_maps_verdict_to_score(monkeypatch, rubric) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    sample = AgentSample(
        case_id="c1",
        agent="prefill",
        task_input="t",
        output_text="o",
        retrieval_context=["item"],
        source_text="src",
        expected_output="ref",
    )
    result = await rubrics.score(rubric, sample, judge_model="judge")
    assert result.score == 0.9
    assert result.passed is (0.9 >= RUBRIC_THRESHOLDS[rubric])


async def test_judge_rubric_below_threshold_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.4))
    sample = AgentSample(case_id="c1", agent="roster", expected_output="ref")
    result = await rubrics.score("plan_quality", sample, judge_model="judge")
    assert result.score == 0.4
    assert result.passed is False


async def test_judge_rubric_error_is_captured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("no api key")

    monkeypatch.setattr(rubrics, "judge_structured", _boom)
    sample = AgentSample(case_id="c1", agent="roster", expected_output="ref")
    result = await rubrics.score("faithfulness", sample, judge_model="judge")
    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None


# --------------------------------------------------------------------------- #
# faithfulness multi-run averaging (score_multi)
# --------------------------------------------------------------------------- #


def _verdict_sequence(scores):  # type: ignore[no-untyped-def]
    """A judge_structured stub that returns a different verdict score per call."""
    it = iter(scores)

    async def _call(*, system, user, response_model, tool_name, model, **kwargs):  # type: ignore[no-untyped-def]
        return response_model(reasoning="ok", score=next(it))

    return _call


async def test_faithfulness_averages_over_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Three judge verdicts (0.6, 0.9, 0.9) → mean 0.8, which clears the 0.7 threshold
    # even though one run was below it: averaging damps the judge noise.
    monkeypatch.setattr(rubrics, "judge_structured", _verdict_sequence([0.6, 0.9, 0.9]))
    samples = [
        AgentSample(case_id="c", agent="roster", task_input="t", output_text="o", expected_output="r")
        for _ in range(3)
    ]
    result = await rubrics.score_multi("faithfulness", samples, judge_model="judge")
    assert result.score == pytest.approx(0.8)
    assert result.passed is True
    assert "averaged over 3" in result.reasoning


async def test_faithfulness_single_sample_matches_score(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # repeats==1 path: score_multi over one sample == the single judge verdict.
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.85))
    sample = AgentSample(case_id="c", agent="roster", task_input="t", output_text="o", expected_output="r")
    result = await rubrics.score_multi("faithfulness", [sample], judge_model="judge")
    assert result.score == pytest.approx(0.85)


async def test_faithfulness_averaging_skips_errored_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Second judge call raises; the mean is taken over the two SUCCESSFUL runs only.
    calls = {"n": 0}

    async def _flaky(*, system, user, response_model, tool_name, model, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient judge error")
        return response_model(reasoning="ok", score=0.9)

    monkeypatch.setattr(rubrics, "judge_structured", _flaky)
    samples = [
        AgentSample(case_id="c", agent="roster", task_input="t", output_text="o", expected_output="r")
        for _ in range(3)
    ]
    result = await rubrics.score_multi("faithfulness", samples, judge_model="judge")
    assert result.score == pytest.approx(0.9)  # mean of the two non-errored 0.9s
    assert "averaged over 2/3" in result.reasoning


# --------------------------------------------------------------------------- #
# Runner aggregation (stubbed adapters + judge) + skipped exclusion
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    def __init__(self, agent: str) -> None:
        self.agent = agent

    async def run(self, case: EvalCase) -> AgentSample:
        output_obj = None
        eval_context: dict = {}
        gold: dict = case.gold
        if self.agent in models.TWIN_AGENTS:
            # A valid estimate that satisfies all deterministic twin invariants for
            # development+agentic (band 0.36-0.66, here r=40%).
            output_obj = _make_estimate(
                phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=60, reduction_pct=40
            )
            eval_context = {
                "phase": "development",
                "tooling_level": "agentic",
                "reduction_bands": {},
                "roster": _roster(
                    ("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0)
                ).model_dump(),
            }
        return AgentSample(
            case_id=case.id,
            agent=self.agent,
            task_input="task",
            output_text="output",
            output_obj=output_obj,
            retrieval_context=["ctx"],
            source_text="src" if self.agent == "prefill" else None,
            expected_output=case.expected_output,
            gold=gold,
            eval_context=eval_context,
        )


async def test_run_evals_aggregates_and_respects_matrix(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.85))
    monkeypatch.setattr(
        runner,
        "ADAPTERS",
        {"development": _FakeAdapter("development"), "prefill": _FakeAdapter("prefill")},
    )

    def _fake_load(agent: str | None = None):  # type: ignore[no-untyped-def]
        cases = {
            "development": [EvalCase(id="d1", agent="development", expected_output="ref")],
            "prefill": [
                EvalCase(
                    id="p1",
                    agent="prefill",
                    expected_output="ref",
                    gold={
                        "industry": "healthcare",
                        "project_type": "greenfield",
                        "regulatory_requirements": [],
                        "screen_count": 0,
                        "integration_count": 0,
                    },
                )
            ],
        }
        return cases.get(agent, []) if agent else []

    monkeypatch.setattr(runner, "load_cases", _fake_load)

    rep = await runner.run_evals(
        agents=["development", "prefill"], rubrics=None, judge_model="judge", concurrency=2
    )
    by_agent = {a.agent: a for a in rep.agents}

    # development twin: json_correctness + the deterministic correctness rubrics all
    # present EXCEPT estimate_accuracy, which SKIPS (no targets) → excluded from means.
    dev_means = by_agent["development"].rubric_means
    assert "json_correctness" in dev_means
    assert by_agent["development"].rubric_means["json_correctness"] == 1.0
    assert by_agent["development"].rubric_means["band_adherence"] == 1.0
    assert "estimate_accuracy" not in dev_means  # skipped → not aggregated
    assert by_agent["development"].rubric_means["faithfulness"] == pytest.approx(0.85)

    # prefill: summarization (judge) + extraction_accuracy (deterministic).
    pf_means = by_agent["prefill"].rubric_means
    assert set(pf_means) == {"summarization", "extraction_accuracy"}
    assert "json_correctness" not in pf_means

    assert 0.0 <= rep.overall_pass_rate <= 1.0


async def test_run_evals_respects_requested_rubric_filter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    monkeypatch.setattr(runner, "ADAPTERS", {"development": _FakeAdapter("development")})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"], rubrics=["faithfulness"], judge_model="judge", concurrency=1
    )
    assert set(rep.agents[0].rubric_means) == {"faithfulness"}


class _CountingTwinAdapter:
    """A twin adapter that records how many times it ran and returns a STABLE
    estimate every run (so consistency scores 1.0)."""

    def __init__(self) -> None:
        self.agent = "development"
        self.runs = 0

    async def run(self, case: EvalCase) -> AgentSample:
        self.runs += 1
        return AgentSample(
            case_id=case.id,
            agent="development",
            output_obj=_make_estimate(phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=60, reduction_pct=40),
            eval_context={
                "phase": "development",
                "tooling_level": "agentic",
                "reduction_bands": {},
                "roster": _roster(
                    ("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0)
                ).model_dump(),
            },
            gold=case.gold,
        )


async def test_run_evals_repeats_reruns_adapter_and_scores_consistency(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    adapter = _CountingTwinAdapter()
    monkeypatch.setattr(runner, "ADAPTERS", {"development": adapter})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"],
        rubrics=["consistency"],
        judge_model="judge",
        concurrency=2,
        repeats=3,
    )
    # The adapter ran 3 times for the single case (multi-sample rubric in play).
    assert adapter.runs == 3
    means = rep.agents[0].rubric_means
    assert means["consistency"] == pytest.approx(1.0)  # identical runs → stable


async def test_run_evals_repeats_one_runs_adapter_once_and_skips_consistency(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    adapter = _CountingTwinAdapter()
    monkeypatch.setattr(runner, "ADAPTERS", {"development": adapter})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"], rubrics=["consistency"], judge_model="judge", concurrency=1
    )
    # Default repeats=1 → adapter runs once, consistency SKIPS → not in means.
    assert adapter.runs == 1
    assert "consistency" not in rep.agents[0].rubric_means


async def test_run_evals_folds_in_synthetic_cases(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from evals.synthetic import generate_cases_by_agent

    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    # A twin adapter that echoes a band BRACKETING the synthetic gold so
    # interval_calibration scores 1.0 on the folded-in synthetic cases.
    class _BracketAdapter:
        def __init__(self) -> None:
            self.agent = "development"

        async def run(self, case: EvalCase) -> AgentSample:
            gm = case.gold["actual_manual_ml"]
            ga = case.gold["actual_ai_ml"]
            est = _make_estimate(
                phase=Phase.DEVELOPMENT, manual_ml=gm, ai_ml=min(ga, gm), reduction_pct=30
            )
            return AgentSample(
                case_id=case.id,
                agent="development",
                output_obj=est,
                eval_context={
                    "phase": "development",
                    "tooling_level": "agentic",
                    "reduction_bands": {},
                    "roster": _roster(
                        ("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0)
                    ).model_dump(),
                },
                gold=case.gold,
            )

    monkeypatch.setattr(runner, "ADAPTERS", {"development": _BracketAdapter()})
    monkeypatch.setattr(runner, "load_cases", lambda agent=None: [])  # no disk cases

    synthetic = generate_cases_by_agent(2, seed=11)
    rep = await runner.run_evals(
        agents=["development"],
        rubrics=["interval_calibration"],
        judge_model="judge",
        concurrency=2,
        synthetic_cases=synthetic,
    )
    dev = rep.agents[0]
    # Two synthetic development cases were folded in and scored.
    assert dev.case_count == 2
    assert dev.rubric_means["interval_calibration"] == pytest.approx(1.0)


def test_skipped_scores_excluded_from_report_means() -> None:
    from evals.models import AgentReport, CaseResult, EvalReport, RubricScore

    scores = [
        RubricScore(rubric="estimate_accuracy", score=0.0, passed=True, skipped=True),
        RubricScore(rubric="band_adherence", score=1.0, passed=True),
    ]
    rep = EvalReport(
        judge_model="j",
        agents=[
            AgentReport(
                agent="development",
                case_count=1,
                results=[CaseResult(case_id="c", agent="development", scores=scores)],
            )
        ],
    )
    means = rep.rubric_means()
    assert "estimate_accuracy" not in means  # skipped excluded
    assert means["band_adherence"] == 1.0
    assert rep.overall_pass_rate == 1.0  # only the non-skipped score counts


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #


async def test_report_renders_and_serializes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "judge_structured", await _fake_verdict(0.9))
    monkeypatch.setattr(runner, "ADAPTERS", {"development": _FakeAdapter("development")})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"], rubrics=None, judge_model="judge", concurrency=1
    )
    text = report.render_text(rep)
    assert "Eval report" in text
    assert "development" in text
    assert "Overall pass-rate" in text

    payload = report.to_dict(rep)
    assert payload["judge_model"] == "judge"
    assert "overall_pass_rate" in payload
    assert "rubric_means" in payload
    assert isinstance(payload["agents"], list)
