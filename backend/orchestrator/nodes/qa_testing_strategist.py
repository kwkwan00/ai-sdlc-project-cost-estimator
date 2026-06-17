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
from orchestrator.montecarlo import (
    DEFAULT_DRAWS,
    Range3,
    ReductionSampler,
    propagate_phase,
    resolve_size_band,
)

from ._twin_base import assemble_phase_estimate, make_twin_nodes, risk_specs_from

logger = logging.getLogger(__name__)

# Plan baselines per planning outline §3.6.
PLAN_A_HARNESS_BASE = 352  # eval harness build
PLAN_B_TEAM_BASE = 480     # dedicated QA team baseline
PLAN_C_HARNESS_BASE = 312  # reduced harness in hybrid
PLAN_C_TEAM_BASE = 208     # reduced QA team in hybrid (312 + 208 = 520 combined floor)

PLAN_A_TP_FACTOR = 0.5
PLAN_B_TP_FACTOR = 1.25
PLAN_C_TP_FACTOR = 0.35


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

    has_ai_features: bool = False
    has_regulatory_requirements: bool = False
    recommended_plan: QAPlan = QAPlan.PLAN_A
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=30)

    # Monte Carlo uncertainty (optional). The dominant size driver is the function-point
    # count (it flows through compute_test_points → the plan totals); the LLM may give an
    # ~80% band for it, a fallback CoV, and/or a low/high band on the AI reduction it
    # proposes.
    fp_range: Range3 | None = None
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


def auto_select_plan(has_ai: bool, has_reg: bool) -> QAPlan:
    if has_ai and has_reg:
        return QAPlan.PLAN_C
    if has_ai and not has_reg:
        return QAPlan.PLAN_A
    if not has_ai and has_reg:
        return QAPlan.PLAN_B
    return QAPlan.PLAN_A


def _uncertain_fields_qa(inputs: QATPAInputs) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the function-point count (it flows through
    compute_test_points → plan totals). No bounds beyond the field's ``ge=0``.
    Mirrors ``_uncertain_fields_dev``."""
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

    point_mid, breakdown = compute_qa_hours(inputs)
    manual_mc, ai_mc = propagate_phase(
        inputs,
        compute_qa_hours,
        size_fields=_uncertain_fields_qa(inputs),
        reduction_sampler=reduction_sampler,
        risk_specs=risk_specs_from(inputs.risks),
        eff_point=effective_reduction,
        n_draws=DEFAULT_DRAWS,
        rng=rng,
    )
    ai_mid = point_mid * (1 - effective_reduction)

    notes = f"Selected plan {selected.value} (eval harness / QA team / hybrid). {inputs.notes}".strip()

    return assemble_phase_estimate(
        phase=Phase.QA_TESTING,
        twin_name="qa_testing_strategist",
        algorithm=f"TPA_Plan_{selected.value}",
        point_mid=point_mid,
        ai_mid=ai_mid,
        manual_mc=manual_mc,
        ai_mc=ai_mc,
        roster=roster,
        inputs=inputs,
        breakdown=breakdown,
        effective_reduction=effective_reduction,
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
    trace_name="twin.qa_testing",
)
