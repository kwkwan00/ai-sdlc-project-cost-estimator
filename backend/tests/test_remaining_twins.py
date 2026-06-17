"""Tests for the math in the 5 remaining twins (UX, Development, Code Review,
Deployment, QA). LLM-bound paths are exercised by tests/test_graph.py (which patches
parse_input but keeps the twin entry points; in that test all twins fall back to the
stub path because no ANTHROPIC_API_KEY is set during testing).

Per-twin we test:
- The compute_* function math
- The build_phase_estimate end-to-end shape (role attribution sums to total),
  including the effective-AI-reduction application (incl. negative reductions)
"""

from __future__ import annotations

import pytest

from models.project_schema import RoleRoster
from models.twin_outputs import Phase, RiskInput
from orchestrator.montecarlo import Range3, make_rng

# --- Code Review ---
from orchestrator.nodes.code_review_sentinel import (
    CodeReviewInputs,
    compute_review_hours,
)
from orchestrator.nodes.code_review_sentinel import (
    build_phase_estimate as build_cr,
)

# --- Deployment ---
from orchestrator.nodes.deployment_devops import (
    CMPInputs,
    compute_cmp_hours,
)
from orchestrator.nodes.deployment_devops import (
    build_phase_estimate as build_dep,
)

# --- Development ---
from orchestrator.nodes.development_architect import (
    DevCOCOMOInputs,
    StackCategory,
    _aggregate_cocomo,
    compute_cocomo_hours,
    development_pass1,
    development_pass2,
    resolve_sloc,
)
from orchestrator.nodes.development_architect import (
    build_phase_estimate as build_dev,
)

# --- QA ---
from orchestrator.nodes.qa_testing_strategist import (
    QAPlan,
    QATPAInputs,
    auto_select_plan,
    compute_plan_hours,
    compute_qa_hours,
    compute_test_points,
)
from orchestrator.nodes.qa_testing_strategist import (
    build_phase_estimate as build_qa,
)

# --- UX/Design ---
from orchestrator.nodes.ux_design_strategist import (
    UXSCPInputs,
    compute_scp_hours,
)
from orchestrator.nodes.ux_design_strategist import (
    build_phase_estimate as build_ux,
)


def _const_sampler(r: float):
    """A reduction sampler that always returns `r`, so the deterministic identity
    `ai.most_likely == manual.most_likely * (1 - r)` holds exactly in unit tests."""
    return lambda _rng: r


# ============== UX/Design ==============

def test_ux_scp_raw_points_match_screen_weights() -> None:
    inputs = UXSCPInputs(
        simple_screens=10, average_screens=5, complex_screens=2, novel_screens=1,
        design_system_factor=1.0, interaction_complexity_multiplier=1.0, iteration_factor=1.0,
        is_responsive=False, confidence=0.7,
    )
    mid, b = compute_scp_hours(inputs)
    # 10*3 + 5*8 + 2*16 + 1*30 = 30 + 40 + 32 + 30 = 132
    assert b["raw_screen_points"] == 132
    assert mid == 132


def test_ux_scp_responsive_adds_15_percent() -> None:
    base = UXSCPInputs(
        simple_screens=10, average_screens=0, complex_screens=0, novel_screens=0,
        design_system_factor=1.0, interaction_complexity_multiplier=1.0, iteration_factor=1.0,
        is_responsive=False, confidence=0.7,
    )
    resp = base.model_copy(update={"is_responsive": True})
    mid_base, _ = compute_scp_hours(base)
    mid_resp, _ = compute_scp_hours(resp)
    assert mid_resp == pytest.approx(mid_base * 1.15)


def test_ux_build_phase_estimate_role_attribution_sums_to_total() -> None:
    inputs = UXSCPInputs(
        simple_screens=20, average_screens=10, complex_screens=4, novel_screens=0,
        design_system_factor=0.7, interaction_complexity_multiplier=1.2, iteration_factor=1.3,
        is_responsive=True, confidence=0.7,
    )
    est = build_ux(
        inputs,
        effective_reduction=0.30,
        roster=RoleRoster.default(),
        rng=make_rng("ux"),
        reduction_sampler=_const_sampler(0.30),
    )
    assert est.phase is Phase.UX_DESIGN
    assert sum(rh.hours for rh in est.ai_assisted_role_hours) == pytest.approx(
        est.ai_assisted_hours.most_likely, abs=1e-3
    )
    # ai.most_likely == manual.most_likely * (1 - r), exactly (modal draw).
    assert est.ai_assisted_hours.most_likely == pytest.approx(
        est.manual_only_hours.most_likely * 0.70, abs=1e-6
    )
    assert est.manual_only_hours.most_likely == pytest.approx(
        compute_scp_hours(inputs)[0], abs=1e-6
    )


# ============== Development ==============

def test_dev_resolve_sloc_uses_direct_estimate_when_provided() -> None:
    inputs = DevCOCOMOInputs(sloc_estimate=12500, function_points=100, primary_language="java", confidence=0.7)
    assert resolve_sloc(inputs) == 12500


def test_dev_resolve_sloc_converts_fp_with_language_ratio() -> None:
    inputs = DevCOCOMOInputs(function_points=100, primary_language="java", confidence=0.7)
    # Java = 53 SLOC/FP → 100 * 53 = 5300
    assert resolve_sloc(inputs) == 5300


def test_dev_cocomo_pm_uses_2_94_coefficient() -> None:
    # 10 KSLOC, E = 0.91 + 12*0.01 = 1.03, EAF = 1.0 → PM = 2.94 * 10^1.03 ≈ 31.3
    inputs = DevCOCOMOInputs(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        stack_category=StackCategory.MODERN_WEB, infrastructure_leverage_pct=0,
        confidence=0.7,
    )
    _, b = compute_cocomo_hours(inputs)
    assert b["ksloc"] == 10.0
    assert b["scale_exponent_E"] == pytest.approx(1.03, abs=0.001)
    # PM = 2.94 × 10^1.03 ≈ 31.3
    assert b["person_months"] == pytest.approx(31.3, abs=0.5)


def test_dev_stack_multiplier_legacy_is_higher() -> None:
    common = dict(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        infrastructure_leverage_pct=0, confidence=0.7,
    )
    modern_mid, _ = compute_cocomo_hours(
        DevCOCOMOInputs(**common, stack_category=StackCategory.MODERN_WEB)
    )
    legacy_mid, _ = compute_cocomo_hours(
        DevCOCOMOInputs(**common, stack_category=StackCategory.LEGACY_ENTERPRISE)
    )
    assert legacy_mid > modern_mid * 2  # legacy_enterprise multiplier is 3.0


def test_dev_infrastructure_leverage_reduces_hours() -> None:
    common = dict(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        stack_category=StackCategory.MODERN_WEB, confidence=0.7,
    )
    no_leverage, _ = compute_cocomo_hours(
        DevCOCOMOInputs(**common, infrastructure_leverage_pct=0)
    )
    high_leverage, _ = compute_cocomo_hours(
        DevCOCOMOInputs(**common, infrastructure_leverage_pct=40)
    )
    assert high_leverage == pytest.approx(no_leverage * 0.6, rel=1e-3)


def test_dev_aggregate_cocomo_takes_median_drivers() -> None:
    # 5 samples; raw SLOC 2400/3600/4000/4400/5600 → median 4000; scale 10..14 → 12;
    # eaf 0.8..1.2 → 1.0. The consensus uses each numeric driver's median.
    samples = [
        DevCOCOMOInputs(sloc_estimate=sl, scale_factor_sum=sf, eaf_composite=eaf, confidence=0.7)
        for sl, sf, eaf in [
            (2400, 10, 0.8), (3600, 11, 0.9), (4000, 12, 1.0), (4400, 13, 1.1), (5600, 14, 1.2)
        ]
    ]
    agg = _aggregate_cocomo(samples)
    assert agg.sloc_estimate == pytest.approx(4000.0)
    assert agg.scale_factor_sum == 12
    assert agg.eaf_composite == pytest.approx(1.0)
    assert agg.function_points is None


@pytest.mark.asyncio
async def test_dev_pass2_self_consistency_aggregates_k_samples(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Pass 2 fires K=5 concurrent calls and folds them by median; the node's most-likely equals
    # the deterministic compute on the aggregated inputs (proving the ensemble drove the number).
    samples = [
        DevCOCOMOInputs(sloc_estimate=sl, scale_factor_sum=sf, eaf_composite=eaf, confidence=0.7)
        for sl, sf, eaf in [
            (2400, 10, 0.8), (3600, 11, 0.9), (4000, 12, 1.0), (4400, 13, 1.1), (5600, 14, 1.2)
        ]
    ]
    calls = {"n": 0}

    async def _fake(**kwargs: object) -> DevCOCOMOInputs:
        i = calls["n"]
        calls["n"] += 1
        return samples[i]

    monkeypatch.setattr("orchestrator.nodes._twin_base.call_structured", _fake)
    out = await development_pass2({"estimate_id": "ens", "raw_input": "x"})
    est = out["pass2_estimates"][0]
    assert calls["n"] == 5  # K independent samples drawn
    expected = compute_cocomo_hours(_aggregate_cocomo(samples))[0]
    assert est.manual_only_hours.most_likely == pytest.approx(expected, rel=1e-6)


@pytest.mark.asyncio
async def test_dev_pass1_does_not_ensemble(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sample = DevCOCOMOInputs(sloc_estimate=4000, confidence=0.7)
    calls = {"n": 0}

    async def _fake(**kwargs: object) -> DevCOCOMOInputs:
        calls["n"] += 1
        return sample

    monkeypatch.setattr("orchestrator.nodes._twin_base.call_structured", _fake)
    out = await development_pass1({"estimate_id": "ens", "raw_input": "x"})
    est = out["pass1_estimates"][0]
    assert calls["n"] == 1  # Pass 1 is a single call — no self-consistency
    expected = compute_cocomo_hours(sample)[0]
    assert est.manual_only_hours.most_likely == pytest.approx(expected, rel=1e-6)


def test_dev_ai_reduction_applies_effective_reduction() -> None:
    inputs = DevCOCOMOInputs(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        stack_category=StackCategory.MODERN_WEB, ai_reduction_pct=50,
        confidence=0.7,
    )
    est = build_dev(
        inputs,
        effective_reduction=0.10,
        roster=RoleRoster.default(),
        rng=make_rng("dev"),
        reduction_sampler=_const_sampler(0.10),
    )
    ratio = est.ai_assisted_hours.most_likely / est.manual_only_hours.most_likely
    # 10% reduction → ratio should be 0.9.
    assert ratio == pytest.approx(0.9, abs=0.001)
    # Deterministic mid preserved through the Monte Carlo.
    assert est.manual_only_hours.most_likely == pytest.approx(
        compute_cocomo_hours(inputs)[0], abs=1e-6
    )


def test_dev_negative_reduction_makes_ai_slower() -> None:
    inputs = DevCOCOMOInputs(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        stack_category=StackCategory.MODERN_WEB, ai_reduction_pct=50,
        confidence=0.7,
    )
    est = build_dev(
        inputs,
        effective_reduction=-0.10,
        roster=RoleRoster.default(),
        rng=make_rng("dev"),
        reduction_sampler=_const_sampler(-0.10),
    )
    # Negative reduction → AI hours exceed manual hours.
    assert est.ai_assisted_hours.most_likely > est.manual_only_hours.most_likely


# ============== Code Review ==============

def test_review_hours_scale_with_ksloc() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=10, primary_language="java", kickback_rate_pct=0,
        pr_complexity_factor=1.0, tooling_setup_hours=0, confidence=0.7,
    )
    # Java rate = 175; base = 10000 / 175 ≈ 57.14; prep = base*0.3 ≈ 17.14; total ≈ 74.29 (no rework/tooling)
    mid, b = compute_review_hours(inputs)
    assert b["inspection_rate_loc_per_hr"] == 175
    assert mid == pytest.approx(74.3, abs=0.2)


def test_review_build_phase_estimate_emits_structured_breakdown() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=26, primary_language="typescript", kickback_rate_pct=20,
        pr_complexity_factor=1.0, tooling_setup_hours=12, confidence=0.7,
        notes="Manual-only review on a greenfield TS portal.",
    )
    est = build_cr(
        inputs,
        effective_reduction=0.0,
        roster=RoleRoster.default(),
        rng=make_rng("cr"),
        reduction_sampler=_const_sampler(0.0),
    )
    # The Fagan components are structured data, not embedded in prose.
    assert {
        "inspection_rate_loc_per_hr",
        "review_hours_pre_tooling",
        "rework_multiplier",
        "tooling_setup_hours",
    } <= est.breakdown.keys()
    assert est.breakdown["tooling_setup_hours"] == 12
    assert est.effective_ai_reduction_pct == 0.0
    # notes is now prose only — no "breakdown:" / "Effective AI reduction" boilerplate.
    assert "breakdown" not in est.notes.lower()
    assert est.notes == "Manual-only review on a greenfield TS portal."


def test_review_kickback_increases_via_rework_multiplier() -> None:
    base = CodeReviewInputs(
        total_ksloc=10, primary_language="typescript", kickback_rate_pct=0,
        pr_complexity_factor=1.0, tooling_setup_hours=0, confidence=0.7,
    )
    high_kickback = base.model_copy(update={"kickback_rate_pct": 40})
    base_mid, _ = compute_review_hours(base)
    high_mid, _ = compute_review_hours(high_kickback)
    # 40% kickback → rework_mul = 1 + 0.4 * 0.5 = 1.2 → 20% more hours
    assert high_mid == pytest.approx(base_mid * 1.2, abs=0.5)


def test_review_tooling_setup_adds_flat_hours() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=5, primary_language="python", kickback_rate_pct=20,
        pr_complexity_factor=1.0, tooling_setup_hours=50, confidence=0.7,
    )
    mid_with_tooling, _ = compute_review_hours(inputs)
    no_tooling_mid, _ = compute_review_hours(inputs.model_copy(update={"tooling_setup_hours": 0}))
    assert mid_with_tooling - no_tooling_mid == 50


def test_review_build_phase_estimate_role_attribution_sums() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=8.5, primary_language="typescript", kickback_rate_pct=25,
        pr_complexity_factor=1.0, tooling_setup_hours=20, confidence=0.7,
    )
    est = build_cr(
        inputs,
        effective_reduction=0.30,
        roster=RoleRoster.default(),
        rng=make_rng("cr"),
        reduction_sampler=_const_sampler(0.30),
    )
    assert est.phase is Phase.CODE_REVIEW
    assert sum(rh.hours for rh in est.ai_assisted_role_hours) == pytest.approx(
        est.ai_assisted_hours.most_likely, abs=1e-3
    )


# ============== Deployment ==============

def test_cmp_subtotal_math() -> None:
    inputs = CMPInputs(
        cmp_score=1.8, cicd_components=5, monitoring_components=4, handoff_hours=60,
        regulatory_multiplier=1.0, conservative_bias_pct=0, confidence=0.7,
    )
    # infra = 1.8 * 80 = 144; cicd = 60; monitoring = 48; handoff = 60 → 312
    mid, b = compute_cmp_hours(inputs)
    assert b["infra_hours"] == 144
    assert b["cicd_hours"] == 60
    assert b["monitoring_hours"] == 48
    assert mid == 312


def test_cmp_regulatory_multiplier_scopes_to_cicd_and_monitoring() -> None:
    inputs = CMPInputs(
        cmp_score=2.0, cicd_components=5, monitoring_components=5, handoff_hours=40,
        regulatory_multiplier=1.25, conservative_bias_pct=12, confidence=0.7,
    )
    # infra = 2.0*80 = 160; cicd = 5*12 = 60; monitoring = 5*12 = 60; handoff = 40.
    # Regulatory 1.25 scopes to cicd+monitoring ONLY (120 -> 150), NOT infra or handoff:
    # after_reg = 160 + 150 + 40 = 350; after bias = 350 * 1.12 = 392.
    mid, _ = compute_cmp_hours(inputs)
    assert mid == pytest.approx(392.0, abs=0.01)


def test_deployment_build_phase_estimate_role_attribution_sums() -> None:
    inputs = CMPInputs(
        cmp_score=1.8, cicd_components=4, monitoring_components=3, handoff_hours=40,
        regulatory_multiplier=1.25, conservative_bias_pct=12, confidence=0.7,
    )
    est = build_dep(
        inputs,
        effective_reduction=0.30,
        roster=RoleRoster.default(),
        rng=make_rng("dep"),
        reduction_sampler=_const_sampler(0.30),
    )
    assert est.phase is Phase.DEPLOYMENT
    assert sum(rh.hours for rh in est.manual_only_role_hours) == pytest.approx(
        est.manual_only_hours.most_likely, abs=1e-3
    )


# ============== QA ==============

def test_qa_tpa_formula() -> None:
    inputs = QATPAInputs(
        total_function_points=200, df_weighted=1.0, qd_score=12, qi_score=48,
        supplementary_hours=0, recommended_plan=QAPlan.PLAN_A, confidence=0.7,
    )
    total_tp, b = compute_test_points(inputs)
    # dynamic = 200 * 1.0 * (12/24) = 100
    # static = (200 * 48) / 500 = 19.2
    assert b["dynamic_tp"] == 100
    assert b["static_tp"] == pytest.approx(19.2)
    assert total_tp == pytest.approx(119.2)


def test_qa_plan_hours_differ_by_plan() -> None:
    plans = compute_plan_hours(total_tp=200.0, supplementary=100.0)
    # A: 352 + 200*0.5 + 100 = 552
    # B: 480 + 200*1.25 + 100 = 830   (softened: base 656→480, per-TP factor 1.5→1.25)
    # C: 312 + 208 + 200*0.35 + 100 = 690   (softened: team base 320→208)
    assert plans[QAPlan.PLAN_A] == 552
    assert plans[QAPlan.PLAN_B] == 830
    assert plans[QAPlan.PLAN_C] == 690


def test_qa_compute_qa_hours_selects_recommended_plan_and_lists_all_three() -> None:
    # The MC adapter returns the recommended plan's hours plus all three plan totals
    # in the breakdown (it runs per-draw, so no logging / no plan selection inside).
    inputs = QATPAInputs(
        total_function_points=200, df_weighted=1.0, qd_score=12, qi_score=48,
        supplementary_hours=100, recommended_plan=QAPlan.PLAN_B, confidence=0.7,
    )
    selected_hours, breakdown = compute_qa_hours(inputs)
    total_tp = compute_test_points(inputs)[0]
    plans = compute_plan_hours(total_tp, inputs.supplementary_hours)
    assert selected_hours == pytest.approx(plans[QAPlan.PLAN_B])
    assert breakdown["plan_a_hours"] == pytest.approx(round(plans[QAPlan.PLAN_A], 1))
    assert breakdown["plan_b_hours"] == pytest.approx(round(plans[QAPlan.PLAN_B], 1))
    assert breakdown["plan_c_hours"] == pytest.approx(round(plans[QAPlan.PLAN_C], 1))
    assert breakdown["total_tp"] == pytest.approx(round(total_tp, 1))


@pytest.mark.parametrize(
    "has_ai,has_reg,expected",
    [
        (True, True, QAPlan.PLAN_C),
        (True, False, QAPlan.PLAN_A),
        (False, True, QAPlan.PLAN_B),
        (False, False, QAPlan.PLAN_A),
    ],
)
def test_qa_auto_select_plan(has_ai: bool, has_reg: bool, expected: QAPlan) -> None:
    assert auto_select_plan(has_ai, has_reg) == expected


def test_qa_build_phase_estimate_records_selected_plan_in_algorithm() -> None:
    inputs = QATPAInputs(
        total_function_points=180, df_weighted=1.0, qd_score=14, qi_score=48,
        supplementary_hours=150, has_ai_features=True, has_regulatory_requirements=True,
        recommended_plan=QAPlan.PLAN_C, confidence=0.7,
    )
    est = build_qa(
        inputs,
        effective_reduction=0.30,
        roster=RoleRoster.default(),
        rng=make_rng("qa"),
        reduction_sampler=_const_sampler(0.30),
    )
    assert est.algorithm == "TPA_Plan_C"
    # Plan hours + TPA components are now structured in `breakdown`, not prose.
    assert {"plan_a_hours", "plan_b_hours", "plan_c_hours", "total_tp"} <= est.breakdown.keys()
    assert est.breakdown["plan_c_hours"] > 0
    assert est.effective_ai_reduction_pct == 30.0
    assert "Selected plan C" in est.notes
    # The compute_qa_hours wrapper selects the recommended plan; its mid survives the MC.
    assert est.manual_only_hours.most_likely == pytest.approx(
        compute_qa_hours(inputs)[0], abs=1e-6
    )


def test_qa_fp_range_widens_band() -> None:
    # A wide explicit FP range widens the manual band vs. a tight one (same seed,
    # no risks). The size driver flows through compute_test_points → plan totals.
    tight = QATPAInputs(
        total_function_points=200, df_weighted=1.0, qd_score=12, qi_score=48,
        supplementary_hours=100, recommended_plan=QAPlan.PLAN_A, confidence=0.7,
        fp_range=Range3(low=195, high=205),
    )
    wide = tight.model_copy(update={"fp_range": Range3(low=120, high=320)})
    est_tight = build_qa(
        tight, effective_reduction=0.0, roster=RoleRoster.default(),
        rng=make_rng("qa"), reduction_sampler=_const_sampler(0.0),
    )
    est_wide = build_qa(
        wide, effective_reduction=0.0, roster=RoleRoster.default(),
        rng=make_rng("qa"), reduction_sampler=_const_sampler(0.0),
    )
    span_tight = est_tight.manual_only_hours.pessimistic - est_tight.manual_only_hours.optimistic
    span_wide = est_wide.manual_only_hours.pessimistic - est_wide.manual_only_hours.optimistic
    assert span_wide > span_tight


def test_qa_risks_raise_mean_not_most_likely() -> None:
    # A risk raises the manual MEAN but leaves the deterministic most_likely (the modal
    # no-risk draw) unchanged.
    no_risk = QATPAInputs(
        total_function_points=200, df_weighted=1.0, qd_score=12, qi_score=48,
        supplementary_hours=100, recommended_plan=QAPlan.PLAN_A, confidence=0.9,
    )
    with_risk = no_risk.model_copy(
        update={
            "risks": [
                RiskInput(
                    description="flaky integration suite",
                    probability=0.5, impact_hours_low=100, impact_hours_high=300,
                )
            ]
        }
    )
    est_no = build_qa(
        no_risk, effective_reduction=0.0, roster=RoleRoster.default(),
        rng=make_rng("qa"), reduction_sampler=_const_sampler(0.0),
    )
    est_yes = build_qa(
        with_risk, effective_reduction=0.0, roster=RoleRoster.default(),
        rng=make_rng("qa"), reduction_sampler=_const_sampler(0.0),
    )
    assert est_yes.manual_only_hours.most_likely == pytest.approx(
        est_no.manual_only_hours.most_likely, abs=1e-6
    )
    assert est_yes.manual_only_hours.mean is not None
    assert est_no.manual_only_hours.mean is not None
    assert est_yes.manual_only_hours.mean > est_no.manual_only_hours.mean
