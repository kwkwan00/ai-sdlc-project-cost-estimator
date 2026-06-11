"""UX/Design Strategist — Screen Complexity Points per planning outline §3.2."""

from __future__ import annotations

import logging

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

# Maturity caps for UX. Planning outline shows L3 strong-discovery → 15-25%, L4-5 → 25-40%.
_MATURITY_CAP_UX = {1: 0.0, 2: 0.10, 3: 0.20, 4: 0.30, 5: 0.40}

RESPONSIVE_MODIFIER = 1.35


class UXSCPInputs(BaseModel):
    """Structured SCP inputs extracted by Claude."""

    model_config = ConfigDict(extra="forbid")

    simple_screens: int = Field(ge=0)
    average_screens: int = Field(ge=0)
    complex_screens: int = Field(ge=0)
    novel_screens: int = Field(ge=0)

    design_system_factor: float = Field(ge=0.4, le=1.5, description="DSF")
    interaction_complexity_multiplier: float = Field(ge=1.0, le=1.5, description="ICM")
    iteration_factor: float = Field(ge=1.0, le=2.5, description="IF")

    is_responsive: bool = False

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_scp_hours(inputs: UXSCPInputs) -> tuple[float, dict]:
    raw_pts = (
        3 * inputs.simple_screens
        + 8 * inputs.average_screens
        + 16 * inputs.complex_screens
        + 30 * inputs.novel_screens
    )
    pre_responsive = (
        raw_pts
        * inputs.design_system_factor
        * inputs.interaction_complexity_multiplier
        * inputs.iteration_factor
    )
    manual_mid = pre_responsive * (RESPONSIVE_MODIFIER if inputs.is_responsive else 1.0)
    return manual_mid, {
        "raw_screen_points": raw_pts,
        "pre_responsive_hours": round(pre_responsive, 1),
        "responsive_applied": inputs.is_responsive,
    }


def ai_reduction_for_maturity(level: int) -> float:
    return _MATURITY_CAP_UX.get(level, 0.0)


def build_phase_estimate(
    inputs: UXSCPInputs, *, maturity_level: int, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_scp_hours(inputs)
    ai_mid = manual_mid * (1 - ai_reduction_for_maturity(maturity_level))

    return PhaseEstimate(
        phase=Phase.UX_DESIGN,
        twin_name="ux_design_strategist",
        algorithm="SCP",
        ai_assisted_hours=pert_range(ai_mid),
        manual_only_hours=pert_range(manual_mid),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.UX_DESIGN),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.UX_DESIGN),
        assumptions=[Assumption(text=a, impact_hours=manual_mid * 0.1) for a in inputs.assumptions],
        risks=[
            Risk(description=r, likelihood=0.4, impact_hours_low=manual_mid * 0.1, impact_hours_high=manual_mid * 0.3)
            for r in inputs.risks
        ],
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        notes=f"SCP breakdown: {breakdown}. Maturity L{maturity_level}. {inputs.notes}".strip(),
    )


async def _run_ux(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    maturity = stage3.ux_design_maturity
    try:
        inputs = await call_structured(
            system=load_prompt("ux_design_strategist"),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value="ux_design"),
            response_model=UXSCPInputs,
            tool_name="submit_scp_assessment",
        )
        est = build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
        logger.info(
            "ux_design twin done: pass=%s ai_ml=%.0fh manual_ml=%.0fh",
            pass_num,
            est.ai_assisted_hours.most_likely,
            est.manual_only_hours.most_likely,
        )
        return est
    except Exception as exc:  # noqa: BLE001
        logger.warning("UX twin failed (%s); returning stub", exc)
        return stub_phase_estimate(Phase.UX_DESIGN, "ux_design_strategist", "SCP", 230, 260, roster)


@traced(name="twin.ux_design.p1")
async def ux_design_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_ux(state, pass_num=1)]}


@traced(name="twin.ux_design.p2")
async def ux_design_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_ux(state, pass_num=2)]}
