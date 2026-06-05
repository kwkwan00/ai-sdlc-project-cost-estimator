"""Tests for the math in the 5 remaining twins (UX, Development, Code Review,
Deployment, QA). LLM-bound paths are exercised by tests/test_graph.py (which patches
parse_input but keeps the twin entry points; in that test all twins fall back to the
stub path because no ANTHROPIC_API_KEY is set during testing).

Per-twin we test:
- The compute_* function math
- The maturity-level AI reduction cap
- The build_phase_estimate end-to-end shape (role attribution sums to total)
"""

from __future__ import annotations

import pytest

from models.project_schema import RoleRoster
from models.twin_outputs import Phase

# --- Code Review ---
from orchestrator.nodes.code_review_sentinel import (
    CodeReviewInputs,
    compute_review_hours,
)
from orchestrator.nodes.code_review_sentinel import (
    ai_reduction_for_maturity as cr_cap,
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
    ai_reduction_for_maturity as dep_cap,
)
from orchestrator.nodes.deployment_devops import (
    build_phase_estimate as build_dep,
)

# --- Development ---
from orchestrator.nodes.development_architect import (
    DevCOCOMOInputs,
    StackCategory,
    compute_cocomo_hours,
    resolve_sloc,
)
from orchestrator.nodes.development_architect import (
    ai_reduction_for_maturity as dev_cap,
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
    compute_test_points,
)
from orchestrator.nodes.qa_testing_strategist import (
    ai_reduction_for_maturity as qa_cap,
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
    ai_reduction_for_maturity as ux_cap,
)
from orchestrator.nodes.ux_design_strategist import (
    build_phase_estimate as build_ux,
)

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


def test_ux_scp_responsive_adds_35_percent() -> None:
    base = UXSCPInputs(
        simple_screens=10, average_screens=0, complex_screens=0, novel_screens=0,
        design_system_factor=1.0, interaction_complexity_multiplier=1.0, iteration_factor=1.0,
        is_responsive=False, confidence=0.7,
    )
    resp = base.model_copy(update={"is_responsive": True})
    mid_base, _ = compute_scp_hours(base)
    mid_resp, _ = compute_scp_hours(resp)
    assert mid_resp == pytest.approx(mid_base * 1.35)


@pytest.mark.parametrize("level,expected", [(1, 0.0), (3, 0.20), (5, 0.40)])
def test_ux_maturity_cap(level: int, expected: float) -> None:
    assert ux_cap(level) == expected


def test_ux_build_phase_estimate_role_attribution_sums_to_total() -> None:
    inputs = UXSCPInputs(
        simple_screens=20, average_screens=10, complex_screens=4, novel_screens=0,
        design_system_factor=0.7, interaction_complexity_multiplier=1.2, iteration_factor=1.3,
        is_responsive=True, confidence=0.7,
    )
    est = build_ux(inputs, maturity_level=3, roster=RoleRoster.default())
    assert est.phase is Phase.UX_DESIGN
    assert sum(rh.hours for rh in est.ai_assisted_role_hours) == pytest.approx(
        est.ai_assisted_hours.most_likely, abs=1e-3
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


def test_dev_ai_reduction_is_capped_by_maturity_level() -> None:
    # User asks for 50% AI reduction, but maturity is 2 (cap = 10%).
    inputs = DevCOCOMOInputs(
        sloc_estimate=10000, scale_factor_sum=12, eaf_composite=1.0,
        stack_category=StackCategory.MODERN_WEB, ai_reduction_pct=50,
        confidence=0.7,
    )
    est = build_dev(inputs, maturity_level=2, roster=RoleRoster.default())
    ratio = est.ai_assisted_hours.most_likely / est.manual_only_hours.most_likely
    # Cap is 10% → ratio should be 0.9.
    assert ratio == pytest.approx(0.9, abs=0.001)


@pytest.mark.parametrize("level,expected", [(1, 0.0), (3, 0.25), (5, 0.55)])
def test_dev_maturity_cap(level: int, expected: float) -> None:
    assert dev_cap(level) == expected


# ============== Code Review ==============

def test_review_hours_scale_with_ksloc() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=10, primary_language="java", kickback_rate_pct=0,
        pr_complexity_factor=1.0, tooling_setup_hours=0, confidence=0.7,
    )
    # Java rate = 175; base = 10000 / 175 ≈ 57.14; prep = 28.57; total = 85.71 (no rework, no tooling)
    mid, b = compute_review_hours(inputs)
    assert b["inspection_rate_loc_per_hr"] == 175
    assert mid == pytest.approx(85.7, abs=0.2)


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


@pytest.mark.parametrize("level,expected", [(1, 0.0), (3, 0.20), (5, 0.30)])
def test_review_maturity_cap(level: int, expected: float) -> None:
    assert cr_cap(level) == expected


def test_review_build_phase_estimate_role_attribution_sums() -> None:
    inputs = CodeReviewInputs(
        total_ksloc=8.5, primary_language="typescript", kickback_rate_pct=25,
        pr_complexity_factor=1.0, tooling_setup_hours=20, confidence=0.7,
    )
    est = build_cr(inputs, maturity_level=3, roster=RoleRoster.default())
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


def test_cmp_regulatory_multiplier_compounds_with_conservative_bias() -> None:
    inputs = CMPInputs(
        cmp_score=2.0, cicd_components=0, monitoring_components=0, handoff_hours=0,
        regulatory_multiplier=1.25, conservative_bias_pct=12, confidence=0.7,
    )
    # infra only = 2.0 * 80 = 160; after reg = 200; after bias = 200 * 1.12 = 224
    mid, _ = compute_cmp_hours(inputs)
    assert mid == pytest.approx(224.0, abs=0.01)


@pytest.mark.parametrize("level,expected", [(1, 0.0), (3, 0.10), (5, 0.25)])
def test_deployment_maturity_cap(level: int, expected: float) -> None:
    assert dep_cap(level) == expected


def test_deployment_build_phase_estimate_role_attribution_sums() -> None:
    inputs = CMPInputs(
        cmp_score=1.8, cicd_components=4, monitoring_components=3, handoff_hours=40,
        regulatory_multiplier=1.25, conservative_bias_pct=12, confidence=0.7,
    )
    est = build_dep(inputs, maturity_level=2, roster=RoleRoster.default())
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
    # B: 656 + 200*1.5 + 100 = 1056
    # C: 312 + 320 + 200*0.35 + 100 = 802
    assert plans[QAPlan.PLAN_A] == 552
    assert plans[QAPlan.PLAN_B] == 1056
    assert plans[QAPlan.PLAN_C] == 802


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


@pytest.mark.parametrize("level,expected", [(1, 0.0), (3, 0.18), (5, 0.30)])
def test_qa_maturity_cap(level: int, expected: float) -> None:
    assert qa_cap(level) == expected


def test_qa_build_phase_estimate_records_selected_plan_in_algorithm() -> None:
    inputs = QATPAInputs(
        total_function_points=180, df_weighted=1.0, qd_score=14, qi_score=48,
        supplementary_hours=150, has_ai_features=True, has_regulatory_requirements=True,
        recommended_plan=QAPlan.PLAN_C, confidence=0.7,
    )
    est = build_qa(inputs, maturity_level=3, roster=RoleRoster.default())
    assert est.algorithm == "TPA_Plan_C"
    assert "Plan A:" in est.notes
    assert "Plan B:" in est.notes
    assert "Plan C:" in est.notes
