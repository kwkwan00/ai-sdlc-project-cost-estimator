"""QA & Testing Strategist — TPA + three-plan recommendation per planning outline §3.6.

The recommended plan (A/B/C) drives the phase hours. All three plan totals are also
computed and emitted structurally in `breakdown.plan_a/b/c_hours` for transparency;
expanded side-by-side UI rendering is post-MVP.
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.project_schema import RoleRoster
from models.twin_outputs import (
    Gap,
    Phase,
    PhaseEstimate,
    RiskInputList,
)
from orchestrator.montecarlo import Range3, ReductionSampler, resolve_size_band

from ._twin_base import build_phase_from_compute, make_twin_nodes

logger = logging.getLogger(__name__)

# Plan baselines per planning outline §3.6.
PLAN_A_HARNESS_BASE = 352  # eval harness build
PLAN_B_TEAM_BASE = 480     # dedicated QA team baseline
PLAN_C_HARNESS_BASE = 312  # reduced harness in hybrid
PLAN_C_TEAM_BASE = 208     # reduced QA team in hybrid (312 + 208 = 520 combined floor)

PLAN_A_TP_FACTOR = 0.5
PLAN_B_TP_FACTOR = 1.25
PLAN_C_TP_FACTOR = 0.35

# Test Case Point Analysis (TCPA) — the selectable alternative to TPA. Sizes testing off the
# planned test-case count weighted by complexity (verification checkpoints), instead of TPA's
# function-point base. Both feed the SAME plan machinery (compute_plan_hours) so the two methods
# stay comparable; the constants below are tuned so a nominal project lands near the TPA baseline
# and they diverge as the test-case count / checkpoint complexity depart from the FP-implied norm.
TEST_CASES_PER_FP = 1.2     # FP→test-case fallback ratio when no explicit count is given
NOMINAL_CHECKPOINTS = 5.0   # a "standard" test case has ~5 verification checkpoints → weight 1.0
TP_PER_WEIGHTED_CASE = 0.5  # a nominal weighted test case ≈ 0.5 test-point-equivalents

# Capers-Jones defect-removal — the third selectable QA method. Sizes testing off the *defects* a
# project of this size will contain (Jones's defect-potential model) rather than a transaction/test
# count: defect_potential = FP × density, the share testing must remove × an effort-per-defect
# factor → test-point-equivalents that feed the SAME plan machinery (compute_plan_hours) so the
# methods stay comparable. The constants are Jones/ISBSG-ish defaults tuned so a nominal project
# lands near the TPA baseline (200·4·0.5·0.3 = 120 ≈ ~119 TP for FP=200), diverging as the project's
# defect density departs from the norm. Calibrate against real actuals if available.
DEFECTS_PER_FP = 4.0        # Jones defect potential per FP (all origins; ~2–7, US avg ~5)
TEST_REMOVAL_SHARE = 0.5    # fraction of defect potential testing (vs inspection/static) must find
DRP_PER_DEFECT = 0.3        # test-point-equivalents of effort per defect testing removes

# Selectable QA sizing algorithms (the Settings screen switches between them; the QA twin reads
# the choice off EstimationState, defaulting to TPA).
DEFAULT_QA_SIZING_METHOD = "tpa"
QA_SIZING_METHODS: tuple[str, ...] = ("tpa", "test_case_point", "defect_removal")


class QAPlan(str, Enum):
    PLAN_A = "A"
    PLAN_B = "B"
    PLAN_C = "C"


class QATPAInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_function_points: float = Field(ge=0)
    df_weighted: float = Field(default=1.0, ge=0.5, le=1.5)
    qd_score: float = Field(default=12.0, ge=0, le=24, description="Sum of 4 dynamic chars, each 0-6")
    qi_score: float = Field(default=48.0, ge=0, le=96, description="Sum of 6 static chars, each 0 or 16")

    supplementary_hours: float = Field(default=90.0, ge=0, le=600)

    # Test Case Point Analysis inputs (used only when the QA sizing method is
    # ``test_case_point``; TPA ignores them). ``test_case_count`` is the planned number of test
    # cases; ``avg_checkpoints_per_case`` is the complexity proxy (5 ≈ nominal). When the count is
    # absent it falls back to ``total_function_points × TEST_CASES_PER_FP`` (mirrors dev's
    # ``resolve_fp``).
    test_case_count: float | None = Field(default=None, ge=0)
    avg_checkpoints_per_case: float = Field(default=5.0, ge=1, le=20)

    # Capers-Jones defect-removal input (used only when the QA sizing method is ``defect_removal``;
    # the other methods ignore it). Optional override of the base defect potential per FP — raise it
    # for regulated/safety-critical/novel work, lower it for simple/proven domains. Absent → the
    # ``DEFECTS_PER_FP`` benchmark default.
    defect_density_per_fp: float | None = Field(default=None, ge=0, le=20)

    has_ai_features: bool = False
    has_regulatory_requirements: bool = False
    recommended_plan: QAPlan = QAPlan.PLAN_A
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=30)

    # Monte Carlo uncertainty (optional). The dominant size driver is the function-point
    # count (it flows through compute_test_points → the plan totals); the LLM may give an
    # ~80% band for it, a fallback CoV, and/or a low/high band on the AI reduction it
    # proposes.
    fp_range: Range3 | None = None
    test_case_range: Range3 | None = None  # MC band on test_case_count under the TCPA method
    reduction_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_test_points(inputs: QATPAInputs) -> tuple[float, dict]:
    dynamic_tp = inputs.total_function_points * inputs.df_weighted * (inputs.qd_score / 24)
    static_tp = (inputs.total_function_points * inputs.qi_score) / 500
    total_tp = dynamic_tp + static_tp
    return total_tp, {
        "dynamic_tp": round(dynamic_tp, 1),
        "static_tp": round(static_tp, 1),
        "total_tp": round(total_tp, 1),
    }


def compute_plan_hours(total_tp: float, supplementary: float) -> dict[QAPlan, float]:
    return {
        QAPlan.PLAN_A: PLAN_A_HARNESS_BASE + total_tp * PLAN_A_TP_FACTOR + supplementary,
        QAPlan.PLAN_B: PLAN_B_TEAM_BASE + total_tp * PLAN_B_TP_FACTOR + supplementary,
        QAPlan.PLAN_C: PLAN_C_HARNESS_BASE + PLAN_C_TEAM_BASE + total_tp * PLAN_C_TP_FACTOR + supplementary,
    }


def compute_qa_hours(inputs: QATPAInputs) -> tuple[float, dict]:
    """Single-callable adapter for ``propagate_phase``: run TPA → plan hours and return
    ``(hours_for_recommended_plan, breakdown)``.

    The Monte Carlo layer perturbs ``total_function_points`` and re-runs this per draw
    (2000×), so it does NO logging and NO plan-selection side effects — it just reads
    ``inputs.recommended_plan`` (already sanity-checked once in ``build_phase_estimate``)
    and selects that plan's total. The breakdown carries the TP components plus all three
    plan totals for transparency."""
    total_tp, tp_breakdown = compute_test_points(inputs)
    plans = compute_plan_hours(total_tp, inputs.supplementary_hours)
    return plans[inputs.recommended_plan], {
        **tp_breakdown,
        "plan_a_hours": round(plans[QAPlan.PLAN_A], 1),
        "plan_b_hours": round(plans[QAPlan.PLAN_B], 1),
        "plan_c_hours": round(plans[QAPlan.PLAN_C], 1),
    }


def resolve_test_cases(inputs: QATPAInputs) -> float:
    """Planned test-case count for the TCPA method: the LLM's ``test_case_count`` if given, else
    derived from the function-point count via ``TEST_CASES_PER_FP``. Mirrors dev's ``resolve_fp``."""
    if inputs.test_case_count is not None and inputs.test_case_count > 0:
        return inputs.test_case_count
    return inputs.total_function_points * TEST_CASES_PER_FP


def compute_test_case_points(inputs: QATPAInputs) -> tuple[float, dict]:
    """TCPA size: weighted test cases → test-point-equivalents. ``checkpoint_weight`` scales each
    case by its verification-checkpoint complexity (NOMINAL_CHECKPOINTS → 1.0)."""
    cases = resolve_test_cases(inputs)
    weight = inputs.avg_checkpoints_per_case / NOMINAL_CHECKPOINTS
    total_tcp = cases * weight * TP_PER_WEIGHTED_CASE
    return total_tcp, {
        "test_cases": round(cases, 1),
        "checkpoint_weight": round(weight, 2),
        "total_tcp": round(total_tcp, 1),
    }


def compute_qa_hours_tcpa(inputs: QATPAInputs) -> tuple[float, dict]:
    """TCPA adapter for ``propagate_phase`` (mirrors ``compute_qa_hours``): test-case points →
    the shared plan machinery → the recommended plan's hours. The MC perturbs ``test_case_count``
    and re-runs this per draw."""
    total_tcp, tcp_breakdown = compute_test_case_points(inputs)
    plans = compute_plan_hours(total_tcp, inputs.supplementary_hours)
    return plans[inputs.recommended_plan], {
        **tcp_breakdown,
        "plan_a_hours": round(plans[QAPlan.PLAN_A], 1),
        "plan_b_hours": round(plans[QAPlan.PLAN_B], 1),
        "plan_c_hours": round(plans[QAPlan.PLAN_C], 1),
    }


def resolve_defect_density(inputs: QATPAInputs) -> float:
    """Defect potential per FP for the Capers-Jones method: the LLM's ``defect_density_per_fp`` if
    given, else the ``DEFECTS_PER_FP`` benchmark default. Mirrors ``resolve_test_cases``/dev's
    ``resolve_fp`` (explicit-first, constant fallback)."""
    if inputs.defect_density_per_fp is not None and inputs.defect_density_per_fp > 0:
        return inputs.defect_density_per_fp
    return DEFECTS_PER_FP


def compute_defect_removal_points(inputs: QATPAInputs) -> tuple[float, dict]:
    """Capers-Jones size: function points → defect potential → the share testing must remove →
    test-point-equivalents. ``defect_potential = FP × density``; testing is tasked with
    ``TEST_REMOVAL_SHARE`` of those; each removed defect costs ``DRP_PER_DEFECT`` test-point units."""
    density = resolve_defect_density(inputs)
    defect_potential = inputs.total_function_points * density
    test_defects = defect_potential * TEST_REMOVAL_SHARE
    total_drp = test_defects * DRP_PER_DEFECT
    return total_drp, {
        "defect_density_per_fp": round(density, 2),
        "defect_potential": round(defect_potential, 1),
        "test_removal_defects": round(test_defects, 1),
        "total_drp": round(total_drp, 1),
    }


def compute_qa_hours_defect(inputs: QATPAInputs) -> tuple[float, dict]:
    """Capers-Jones adapter for ``propagate_phase`` (mirrors ``compute_qa_hours``): defect-removal
    points → the shared plan machinery → the recommended plan's hours. The MC perturbs
    ``total_function_points`` (the defect-potential driver) and re-runs this per draw."""
    total_drp, drp_breakdown = compute_defect_removal_points(inputs)
    plans = compute_plan_hours(total_drp, inputs.supplementary_hours)
    return plans[inputs.recommended_plan], {
        **drp_breakdown,
        "plan_a_hours": round(plans[QAPlan.PLAN_A], 1),
        "plan_b_hours": round(plans[QAPlan.PLAN_B], 1),
        "plan_c_hours": round(plans[QAPlan.PLAN_C], 1),
    }


# Maps the selected QA sizing method → (per-draw compute adapter, algorithm-label prefix). The
# final label is f"{prefix}_Plan_{plan}" so the recommended plan still rides on the algorithm.
_COMPUTE_BY_METHOD: dict[str, tuple] = {
    "tpa": (compute_qa_hours, "TPA"),
    "test_case_point": (compute_qa_hours_tcpa, "TCPA"),
    "defect_removal": (compute_qa_hours_defect, "DEFECT"),
}


def auto_select_plan(has_ai: bool, has_reg: bool) -> QAPlan:
    if has_ai and has_reg:
        return QAPlan.PLAN_C
    if has_ai and not has_reg:
        return QAPlan.PLAN_A
    if not has_ai and has_reg:
        return QAPlan.PLAN_B
    return QAPlan.PLAN_A


def _uncertain_fields_qa(
    inputs: QATPAInputs, sizing_method: str = DEFAULT_QA_SIZING_METHOD
) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the driver the active compute fn reads, so the MC re-runs that
    fn over the perturbed field: ``test_case_count`` under TCPA (it flows through
    compute_test_case_points → plan totals), else ``total_function_points`` under TPA and Capers-Jones
    defect-removal (both scale off FP — defect potential = FP × density). An FP-expressed ``fp_range``
    is converted into test-case units via ``TEST_CASES_PER_FP`` when the band lands on
    ``test_case_count``. Mirrors ``_uncertain_fields_dev``."""
    if sizing_method == "test_case_point":
        cases = resolve_test_cases(inputs)
        if cases <= 0:
            return {}
        explicit = inputs.test_case_range
        if explicit is None and inputs.fp_range is not None:
            explicit = Range3(
                low=inputs.fp_range.low * TEST_CASES_PER_FP,
                high=inputs.fp_range.high * TEST_CASES_PER_FP,
            )
        band = resolve_size_band(
            point_value=cases,
            explicit=explicit,
            estimate_cov=inputs.estimate_cov,
            confidence=inputs.confidence,
        )
        return {"test_case_count": band} if band else {}
    band = resolve_size_band(
        point_value=inputs.total_function_points,
        explicit=inputs.fp_range,
        estimate_cov=inputs.estimate_cov,
        confidence=inputs.confidence,
    )
    return {"total_function_points": band} if band else {}


def build_phase_estimate(
    inputs: QATPAInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
    sizing_method: str = DEFAULT_QA_SIZING_METHOD,
) -> PhaseEstimate:
    # Sanity-check the recommended plan against the rules (logged ONCE here, never in
    # the per-draw compute_qa_hours wrapper).
    auto_pick = auto_select_plan(inputs.has_ai_features, inputs.has_regulatory_requirements)
    selected = inputs.recommended_plan
    if selected != auto_pick:
        logger.info(
            "QA twin chose plan %s but rules say %s; honoring twin's choice",
            selected.value,
            auto_pick.value,
        )

    compute_fn, algo_prefix = _COMPUTE_BY_METHOD.get(
        sizing_method, _COMPUTE_BY_METHOD[DEFAULT_QA_SIZING_METHOD]
    )
    notes = f"Selected plan {selected.value} (eval harness / QA team / hybrid). {inputs.notes}".strip()

    return build_phase_from_compute(
        inputs,
        phase=Phase.QA_TESTING,
        twin_name="qa_testing_strategist",
        algorithm=f"{algo_prefix}_Plan_{selected.value}",
        compute_fn=compute_fn,
        size_fields=_uncertain_fields_qa(inputs, sizing_method),
        effective_reduction=effective_reduction,
        roster=roster,
        rng=rng,
        reduction_sampler=reduction_sampler,
        assumption_impact_factor=0.05,
        notes=notes,
    )


def _proposed_reduction(inputs: QATPAInputs) -> float:
    return inputs.ai_reduction_pct / 100


qa_testing_pass1, qa_testing_pass2 = make_twin_nodes(
    phase=Phase.QA_TESTING,
    prompt_name="qa_testing_strategist",
    tool_name="submit_tpa_assessment",
    response_model=QATPAInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="TPA",
    stub_ai_mid=1000,
    stub_manual_mid=1180,
    proposed_reduction_fn=_proposed_reduction,
    sizing_method_key="qa_sizing_method",
    sizing_method_default=DEFAULT_QA_SIZING_METHOD,
    trace_name="twin.qa_testing",
)
