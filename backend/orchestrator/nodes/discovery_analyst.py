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
3. Propagate input-size / AI-effectiveness / risk uncertainty via the Monte Carlo layer.
4. Apply the AI-reduction guardrail band (system-derived) and split hours across the
   user-defined roster via role_attribution (Discovery is senior-biased).

Admin-switchable sizing: UCP (default) or FP-based analysis effort — see `_COMPUTE_BY_METHOD`.
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

# Bounds the continuous productivity-factor driver (matches the field's ge/le); the
# sampled size band is clamped into this so compute_ucp_hours never runs out of range.
_PRODUCTIVITY_LO, _PRODUCTIVITY_HI = 18.0, 32.0

# Function-Points alternative: discovery/analysis effort as a parametric function of project size
# (ISBSG phase-distribution style — analysis is a documented slice of an FP-anchored total), instead
# of UCP's use-case-points model. `HOURS_PER_FP_ANALYSIS` is discovery's analysis effort per FP
# (the phase share is already folded in); `FP_PER_UUCW` converts the UCP unadjusted use-case weight
# into an FP estimate when the LLM doesn't supply one. Tuned so a nominal project lands near the UCP
# baseline. Calibrate against real actuals if available.
HOURS_PER_FP_ANALYSIS = 1.0   # discovery/analysis hours per function point
FP_PER_UUCW = 1.2             # UUCW → FP fallback ratio when no explicit FP count is given

# Selectable discovery sizing algorithms (the Settings screen switches between them; the discovery
# twin reads the choice off EstimationState, defaulting to UCP).
DEFAULT_DISCOVERY_SIZING_METHOD = "ucp"
DISCOVERY_SIZING_METHODS: tuple[str, ...] = ("ucp", "function_points")


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

    # Function-Points sizing input (used only when the discovery sizing method is
    # ``function_points``; UCP ignores it). The project's IFPUG FP count; absent → derived from the
    # UCP unadjusted use-case weight via ``FP_PER_UUCW``.
    total_function_points: float | None = Field(default=None, ge=0)

    # Monte Carlo uncertainty (optional). The least-certain size driver here is the
    # continuous productivity factor (hrs/UCP); the LLM may give an ~80% band for it,
    # or a fallback coefficient-of-variation. No `reduction_range`: Discovery does not
    # propose an AI reduction (it spreads the guardrail band instead).
    productivity_factor_range: Range3 | None = None
    fp_range: Range3 | None = None  # MC band on total_function_points under the FP method
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


def _uucw(inputs: DiscoveryUCPInputs) -> int:
    """Unadjusted Use Case Weight — the raw use-case size proxy shared by the UCP formula and the
    FP-method's size fallback."""
    return 5 * inputs.simple_use_cases + 10 * inputs.average_use_cases + 15 * inputs.complex_use_cases


def compute_ucp_hours(inputs: DiscoveryUCPInputs) -> tuple[float, dict]:
    """Apply the UCP formula. Returns (manual_mid_hours, breakdown_dict)."""
    uucw = _uucw(inputs)
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


def resolve_fp_discovery(inputs: DiscoveryUCPInputs) -> float:
    """Function-point size for the FP method: the LLM's ``total_function_points`` if given, else
    derived from the UCP unadjusted use-case weight (``UUCW × FP_PER_UUCW``) so the method still
    produces a sane size when the LLM doesn't supply an explicit FP count. Mirrors dev's
    ``resolve_fp`` (explicit-first, size-proxy fallback)."""
    if inputs.total_function_points is not None and inputs.total_function_points > 0:
        return inputs.total_function_points
    return _uucw(inputs) * FP_PER_UUCW


def compute_fp_analysis_hours(inputs: DiscoveryUCPInputs) -> tuple[float, dict]:
    """FP-based discovery/analysis effort, the UCP alternative: ``hours = FP × HOURS_PER_FP_ANALYSIS``
    (analysis effort is linear in functional size, with the phase share folded into the rate),
    moderated by the **same** stakeholder multiplier as UCP so the two methods stay comparable."""
    fp = resolve_fp_discovery(inputs)
    stakeholder_mult = _stakeholder_multiplier(inputs)
    base_hours = fp * HOURS_PER_FP_ANALYSIS
    manual_mid = base_hours * stakeholder_mult
    return manual_mid, {
        "function_points": round(fp, 1),
        "hours_per_fp_analysis": HOURS_PER_FP_ANALYSIS,
        "base_hours": round(base_hours, 1),
        "stakeholder_multiplier": round(stakeholder_mult, 3),
    }


# Maps the selected sizing method → (deterministic compute fn, algorithm label on the estimate).
_COMPUTE_BY_METHOD: dict[str, tuple] = {
    "ucp": (compute_ucp_hours, "UCP"),
    "function_points": (compute_fp_analysis_hours, "FP_ANALYSIS"),
}


def _uncertain_fields_discovery(
    inputs: DiscoveryUCPInputs, sizing_method: str = DEFAULT_DISCOVERY_SIZING_METHOD
) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the driver the active compute fn reads, so the MC re-runs that
    fn over the perturbed field: ``total_function_points`` under the FP method, else the continuous
    ``productivity_factor`` (clamped to its [18, 32] bounds) under UCP. Mirrors
    ``_uncertain_fields_dev``."""
    if sizing_method == "function_points":
        fp = resolve_fp_discovery(inputs)
        if fp <= 0:
            return {}
        band = resolve_size_band(
            point_value=fp,
            explicit=inputs.fp_range,
            estimate_cov=inputs.estimate_cov,
            confidence=inputs.confidence,
        )
        return {"total_function_points": band} if band else {}
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
    sizing_method: str = DEFAULT_DISCOVERY_SIZING_METHOD,
) -> PhaseEstimate:
    compute_fn, algorithm = _COMPUTE_BY_METHOD.get(
        sizing_method, _COMPUTE_BY_METHOD[DEFAULT_DISCOVERY_SIZING_METHOD]
    )
    return build_phase_from_compute(
        inputs,
        phase=Phase.DISCOVERY,
        twin_name="discovery_analyst",
        algorithm=algorithm,
        compute_fn=compute_fn,
        size_fields=_uncertain_fields_discovery(inputs, sizing_method),
        effective_reduction=effective_reduction,
        roster=roster,
        rng=rng,
        reduction_sampler=reduction_sampler,
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
    sizing_method_key="discovery_sizing_method",
    sizing_method_default=DEFAULT_DISCOVERY_SIZING_METHOD,
)
