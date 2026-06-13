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
    Assumption,
    Gap,
    Phase,
    PhaseEstimate,
    Risk,
)
from orchestrator.role_attribution import attribute_roles

from ._twin_base import make_twin_nodes
from .discovery_analyst import pert_range

logger = logging.getLogger(__name__)

# Plan baselines per planning outline §3.6.
PLAN_A_HARNESS_BASE = 352  # eval harness build
PLAN_B_TEAM_BASE = 656     # dedicated QA team baseline
PLAN_C_HARNESS_BASE = 312  # reduced harness in hybrid
PLAN_C_TEAM_BASE = 320     # reduced QA team in hybrid

PLAN_A_TP_FACTOR = 0.5
PLAN_B_TP_FACTOR = 1.5
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

    supplementary_hours: float = Field(default=150.0, ge=0, le=600)

    has_ai_features: bool = False
    has_regulatory_requirements: bool = False
    recommended_plan: QAPlan = QAPlan.PLAN_A
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=30)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
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


def auto_select_plan(has_ai: bool, has_reg: bool) -> QAPlan:
    if has_ai and has_reg:
        return QAPlan.PLAN_C
    if has_ai and not has_reg:
        return QAPlan.PLAN_A
    if not has_ai and has_reg:
        return QAPlan.PLAN_B
    return QAPlan.PLAN_A


def build_phase_estimate(
    inputs: QATPAInputs, *, effective_reduction: float, roster: RoleRoster
) -> PhaseEstimate:
    total_tp, tp_breakdown = compute_test_points(inputs)
    plans = compute_plan_hours(total_tp, inputs.supplementary_hours)

    # Sanity-check the recommended plan against the rules.
    auto_pick = auto_select_plan(inputs.has_ai_features, inputs.has_regulatory_requirements)
    selected = inputs.recommended_plan
    if selected != auto_pick:
        logger.info(
            "QA twin chose plan %s but rules say %s; honoring twin's choice",
            selected.value,
            auto_pick.value,
        )

    manual_mid = plans[selected]
    ai_mid = manual_mid * (1 - effective_reduction)

    breakdown = {
        **tp_breakdown,
        "plan_a_hours": round(plans[QAPlan.PLAN_A], 1),
        "plan_b_hours": round(plans[QAPlan.PLAN_B], 1),
        "plan_c_hours": round(plans[QAPlan.PLAN_C], 1),
    }
    notes = f"Selected plan {selected.value} (eval harness / QA team / hybrid). {inputs.notes}".strip()

    return PhaseEstimate(
        phase=Phase.QA_TESTING,
        twin_name="qa_testing_strategist",
        algorithm=f"TPA_Plan_{selected.value}",
        ai_assisted_hours=pert_range(ai_mid),
        manual_only_hours=pert_range(manual_mid),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.QA_TESTING),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.QA_TESTING),
        assumptions=[Assumption(text=a, impact_hours=manual_mid * 0.05) for a in inputs.assumptions],
        risks=[
            Risk(description=r, likelihood=0.4, impact_hours_low=manual_mid * 0.05, impact_hours_high=manual_mid * 0.2)
            for r in inputs.risks
        ],
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        breakdown=breakdown,
        effective_ai_reduction_pct=round(effective_reduction * 100, 1),
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
