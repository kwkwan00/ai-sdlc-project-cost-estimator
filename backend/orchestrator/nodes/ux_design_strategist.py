"""UX/Design Strategist — Screen Complexity Points per planning outline §3.2."""

from __future__ import annotations

import logging

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
        "responsive_modifier": RESPONSIVE_MODIFIER if inputs.is_responsive else 1.0,
    }


def build_phase_estimate(
    inputs: UXSCPInputs, *, effective_reduction: float, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_scp_hours(inputs)
    ai_mid = manual_mid * (1 - effective_reduction)

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
        breakdown=breakdown,
        effective_ai_reduction_pct=round(effective_reduction * 100, 1),
        notes=inputs.notes.strip(),
    )


ux_design_pass1, ux_design_pass2 = make_twin_nodes(
    phase=Phase.UX_DESIGN,
    prompt_name="ux_design_strategist",
    tool_name="submit_scp_assessment",
    response_model=UXSCPInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="SCP",
    stub_ai_mid=230,
    stub_manual_mid=260,
    trace_name="twin.ux_design",
)
