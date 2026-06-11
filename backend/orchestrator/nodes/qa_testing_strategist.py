"""QA & Testing Strategist — TPA + three-plan recommendation per planning outline §3.6.

MVP: returns hours for the recommended plan (A/B/C) only. The other two plans are
stashed in `notes` for transparency; expanded side-by-side UI rendering is post-MVP.
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster, Stage3Maturity
from models.twin_outputs import (
    Assumption,
    Gap,
    Phase,
    PhaseEstimate,
    Risk,
)
from observability.langfuse_wrapper import traced
from orchestrator.llm import call_structured
from orchestrator.role_attribution import attribute_roles

from ._twin_base import build_twin_user_prompt, load_prompt, stub_phase_estimate
from .discovery_analyst import pert_range

logger = logging.getLogger(__name__)

_MATURITY_CAP_QA = {1: 0.0, 2: 0.08, 3: 0.18, 4: 0.25, 5: 0.30}

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


def ai_reduction_for_maturity(level: int) -> float:
    return _MATURITY_CAP_QA.get(level, 0.0)


def build_phase_estimate(
    inputs: QATPAInputs, *, maturity_level: int, roster: RoleRoster
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
    cap = ai_reduction_for_maturity(maturity_level)
    effective = min(inputs.ai_reduction_pct / 100.0, cap)
    ai_mid = manual_mid * (1 - effective)

    notes = (
        f"TPA breakdown: {tp_breakdown}. Selected plan: {selected.value}. "
        f"Plan A: {plans[QAPlan.PLAN_A]:.0f}h, Plan B: {plans[QAPlan.PLAN_B]:.0f}h, "
        f"Plan C: {plans[QAPlan.PLAN_C]:.0f}h. AI cap L{maturity_level} = {int(cap*100)}%. "
        f"{inputs.notes}"
    ).strip()

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
        notes=notes,
    )


async def _run_qa(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    maturity = stage3.qa_testing_maturity
    try:
        inputs = await call_structured(
            system=load_prompt("qa_testing_strategist"),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value="qa_testing"),
            response_model=QATPAInputs,
            tool_name="submit_tpa_assessment",
        )
        est = build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
        logger.info(
            "qa_testing twin done: pass=%s ai_ml=%.0fh manual_ml=%.0fh",
            pass_num,
            est.ai_assisted_hours.most_likely,
            est.manual_only_hours.most_likely,
        )
        return est
    except Exception as exc:  # noqa: BLE001
        logger.warning("QA twin failed (%s); returning stub", exc)
        return stub_phase_estimate(
            Phase.QA_TESTING, "qa_testing_strategist", "TPA", 1000, 1180, roster
        )


@traced(name="twin.qa_testing.p1")
async def qa_testing_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_qa(state, pass_num=1)]}


@traced(name="twin.qa_testing.p2")
async def qa_testing_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_qa(state, pass_num=2)]}
