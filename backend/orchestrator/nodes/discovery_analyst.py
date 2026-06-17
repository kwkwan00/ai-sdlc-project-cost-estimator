"""Discovery Analyst twin — Use Case Points (UCP) per planning outline §3.1.

Flow:
1. Ask Claude to extract structured UCP inputs (use case / actor counts, TFactor, EFactor,
   stakeholder factors, project-type hints, assumptions/risks/gaps).
2. Deterministically apply the UCP formula in Python:
     UUCW = 5*simple + 10*average + 15*complex
     UAW  = 1*simple + 2*average + 3*complex
     TCF  = 0.6 + 0.01 * TFactor
     ECF  = 1.4 - 0.03 * EFactor
     UCP  = (UUCW + UAW) * TCF * ECF
     Hours_manual_mid = UCP * productivity_factor * phase_ratio * stakeholder_multiplier
     Three-point PERT around the mid.
3. Apply AI maturity reduction (capped per planning outline §3.1.3 worked example).
4. Split hours across the four roles using role_attribution (Discovery is senior-biased).
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.project_schema import RoleRoster
from models.twin_outputs import (
    Gap,
    HourRange,
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

# Bounds the continuous productivity-factor driver (matches the field's ge/le); the
# sampled size band is clamped into this so compute_ucp_hours never runs out of range.
_PRODUCTIVITY_LO, _PRODUCTIVITY_HI = 18.0, 32.0


class DecisionMakerAccessibility(str, Enum):
    READILY_AVAILABLE = "readily_available"
    GATEKEEPER = "gatekeeper"
    EXECUTIVE_ONLY_OR_MULTI_TZ = "executive_only_or_multi_tz"


class AlignmentDifficulty(str, Enum):
    PRE_ALIGNED = "pre_aligned"
    COMPETING_PRIORITIES = "competing_priorities"


class DiscoveryUCPInputs(BaseModel):
    """Structured UCP inputs extracted by Claude."""

    model_config = ConfigDict(extra="forbid")

    simple_use_cases: int = Field(ge=0)
    average_use_cases: int = Field(ge=0)
    complex_use_cases: int = Field(ge=0)

    simple_actors: int = Field(ge=0)
    average_actors: int = Field(ge=0)
    complex_actors: int = Field(ge=0)

    tfactor: int = Field(ge=0, le=65, description="Sum of 13 technical factors (each 0-5)")
    efactor: int = Field(ge=0, le=40, description="Sum of 8 environmental factors (each 0-5)")

    stakeholder_group_count: int = Field(ge=1)
    decision_maker_accessibility: DecisionMakerAccessibility
    alignment_difficulty: AlignmentDifficulty

    phase_ratio_hint: float = Field(default=0.08, ge=0.05, le=0.15)
    productivity_factor: float = Field(default=24.0, ge=18.0, le=32.0)

    # Monte Carlo uncertainty (optional). The least-certain size driver here is the
    # continuous productivity factor (hrs/UCP); the LLM may give an ~80% band for it,
    # or a fallback coefficient-of-variation. No `reduction_range`: Discovery does not
    # propose an AI reduction (it spreads the guardrail band instead).
    productivity_factor_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def _stakeholder_multiplier(inputs: DiscoveryUCPInputs) -> float:
    group_mult = 1.0
    if 3 <= inputs.stakeholder_group_count <= 5:
        group_mult = 1.08
    elif inputs.stakeholder_group_count >= 6:
        group_mult = 1.18

    access_mult = {
        DecisionMakerAccessibility.READILY_AVAILABLE: 1.0,
        DecisionMakerAccessibility.GATEKEEPER: 1.10,
        DecisionMakerAccessibility.EXECUTIVE_ONLY_OR_MULTI_TZ: 1.20,
    }[inputs.decision_maker_accessibility]

    align_mult = {
        AlignmentDifficulty.PRE_ALIGNED: 1.0,
        AlignmentDifficulty.COMPETING_PRIORITIES: 1.12,
    }[inputs.alignment_difficulty]

    return min(1.5, group_mult * access_mult * align_mult)


def compute_ucp_hours(inputs: DiscoveryUCPInputs) -> tuple[float, dict]:
    """Apply the UCP formula. Returns (manual_mid_hours, breakdown_dict)."""
    uucw = 5 * inputs.simple_use_cases + 10 * inputs.average_use_cases + 15 * inputs.complex_use_cases
    uaw = 1 * inputs.simple_actors + 2 * inputs.average_actors + 3 * inputs.complex_actors
    tcf = 0.6 + 0.01 * inputs.tfactor
    # Note: ECF formula in planning outline uses subtraction (1.4 - 0.03 * EFactor); the
    # written `+ (-0.03)` form is equivalent.
    ecf = 1.4 - 0.03 * inputs.efactor
    ucp = (uucw + uaw) * tcf * ecf
    stakeholder_mult = _stakeholder_multiplier(inputs)
    base_hours = ucp * inputs.productivity_factor * inputs.phase_ratio_hint
    manual_mid = base_hours * stakeholder_mult

    return manual_mid, {
        "uucw": uucw,
        "uaw": uaw,
        "tcf": round(tcf, 3),
        "ecf": round(ecf, 3),
        "ucp": round(ucp, 1),
        "base_hours": round(base_hours, 1),
        "stakeholder_multiplier": round(stakeholder_mult, 3),
    }


def pert_range(mid: float, *, opt_factor: float = 0.78, pess_factor: float = 1.35) -> HourRange:
    """Three-point PERT around `mid`. Defaults give ±~25-35% spread, matching the
    healthcare worked example (155 / 199 / 268).
    """
    return HourRange(
        optimistic=max(0.0, mid * opt_factor),
        most_likely=mid,
        pessimistic=mid * pess_factor,
    )


def _uncertain_fields_discovery(
    inputs: DiscoveryUCPInputs,
) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the continuous productivity factor (the least-certain
    driver), clamped to its [18, 32] bounds. Mirrors ``_uncertain_fields_dev``."""
    band = resolve_size_band(
        point_value=inputs.productivity_factor,
        explicit=inputs.productivity_factor_range,
        estimate_cov=inputs.estimate_cov,
        confidence=inputs.confidence,
        lo_bound=_PRODUCTIVITY_LO,
        hi_bound=_PRODUCTIVITY_HI,
    )
    return {"productivity_factor": band} if band else {}


def build_phase_estimate(
    inputs: DiscoveryUCPInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
) -> PhaseEstimate:
    point_mid, breakdown = compute_ucp_hours(inputs)
    manual_mc, ai_mc = propagate_phase(
        inputs,
        compute_ucp_hours,
        size_fields=_uncertain_fields_discovery(inputs),
        reduction_sampler=reduction_sampler,
        risk_specs=risk_specs_from(inputs.risks),
        eff_point=effective_reduction,
        n_draws=DEFAULT_DRAWS,
        rng=rng,
    )
    ai_mid = point_mid * (1 - effective_reduction)

    return assemble_phase_estimate(
        phase=Phase.DISCOVERY,
        twin_name="discovery_analyst",
        algorithm="UCP",
        point_mid=point_mid,
        ai_mid=ai_mid,
        manual_mc=manual_mc,
        ai_mc=ai_mc,
        roster=roster,
        inputs=inputs,
        breakdown=breakdown,
        effective_reduction=effective_reduction,
        assumption_impact_factor=0.1,
        notes=inputs.notes.strip(),
    )


discovery_analyst_pass1, discovery_analyst_pass2 = make_twin_nodes(
    phase=Phase.DISCOVERY,
    prompt_name="discovery_analyst",
    tool_name="submit_ucp_assessment",
    response_model=DiscoveryUCPInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="UCP",
    stub_ai_mid=200,
    stub_manual_mid=240,
    trace_name="twin.discovery_analyst",
)
