"""UX/Design Strategist — Screen Complexity Points per planning outline §3.2."""

from __future__ import annotations

import logging

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

RESPONSIVE_MODIFIER = 1.15

# Bounds the continuous iteration-factor driver (matches the field's ge/le).
_IF_LO, _IF_HI = 1.0, 2.5


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

    # Monte Carlo uncertainty (optional). The least-certain size driver here is the
    # continuous iteration factor (design rounds); the LLM may give an ~80% band for
    # it, or a fallback coefficient-of-variation. No `reduction_range`: UX does not
    # propose an AI reduction (it spreads the guardrail band instead).
    iteration_factor_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
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


def _uncertain_fields_ux(inputs: UXSCPInputs) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the continuous iteration factor (the least-certain
    driver), clamped to its [1.0, 2.5] bounds. Mirrors ``_uncertain_fields_dev``."""
    band = resolve_size_band(
        point_value=inputs.iteration_factor,
        explicit=inputs.iteration_factor_range,
        estimate_cov=inputs.estimate_cov,
        confidence=inputs.confidence,
        lo_bound=_IF_LO,
        hi_bound=_IF_HI,
    )
    return {"iteration_factor": band} if band else {}


def build_phase_estimate(
    inputs: UXSCPInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
) -> PhaseEstimate:
    return build_phase_from_compute(
        inputs,
        phase=Phase.UX_DESIGN,
        twin_name="ux_design_strategist",
        algorithm="SCP",
        compute_fn=compute_scp_hours,
        size_fields=_uncertain_fields_ux(inputs),
        effective_reduction=effective_reduction,
        roster=roster,
        rng=rng,
        reduction_sampler=reduction_sampler,
        assumption_impact_factor=0.1,
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
