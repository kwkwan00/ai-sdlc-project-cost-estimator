"""Development Architect — simplified COCOMO II per planning outline §3.3.

Simplifications vs. full COCOMO II:
- Single composite EAF (0.5..2.0) instead of 17 individual cost drivers
- Single scale-factor sum (0..25) instead of 5 individual scale factors
- Stack multipliers reduced from full taxonomy to 11 categories with mid-band values
- Infrastructure leverage as a single percentage instead of per-component scorecard

Math is still anchored to COCOMO II's `PM = 2.94 × KSLOC^E × EAF`, then hours = PM × 152.
"""

from __future__ import annotations

import logging
import statistics
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

# SLOC per Function Point by primary language (Capers Jones backfiring ratios).
LANGUAGE_SLOC_PER_FP = {
    "javascript": 47,
    "typescript": 47,
    "python": 32,
    "java": 53,
    "csharp": 53,
    "go": 40,
    "ruby": 27,
    "php": 32,
    "swift": 40,
    "kotlin": 43,
}

STACK_MULTIPLIER = {
    "modern_web": 1.0,
    "jvm_enterprise": 1.2,
    "dotnet": 1.2,
    "mobile_native": 1.45,
    "mobile_cross_platform": 1.25,
    "legacy_web": 2.0,
    "legacy_enterprise": 3.0,
    "data_ml": 1.35,
    "infrastructure": 1.25,
    "embedded": 2.0,
    "blockchain": 1.75,
}

HOURS_PER_PM = 152  # COCOMO II person-month conversion (8 hrs/day × 19 days/month)


class StackCategory(str, Enum):
    MODERN_WEB = "modern_web"
    JVM_ENTERPRISE = "jvm_enterprise"
    DOTNET = "dotnet"
    MOBILE_NATIVE = "mobile_native"
    MOBILE_CROSS_PLATFORM = "mobile_cross_platform"
    LEGACY_WEB = "legacy_web"
    LEGACY_ENTERPRISE = "legacy_enterprise"
    DATA_ML = "data_ml"
    INFRASTRUCTURE = "infrastructure"
    EMBEDDED = "embedded"
    BLOCKCHAIN = "blockchain"


class DevCOCOMOInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function_points: float | None = Field(default=None, ge=0)
    sloc_estimate: float | None = Field(default=None, ge=0)
    primary_language: str = Field(default="typescript")

    scale_factor_sum: int = Field(default=12, ge=0, le=25)
    eaf_composite: float = Field(default=1.0, ge=0.5, le=2.0)
    stack_category: StackCategory = StackCategory.MODERN_WEB
    infrastructure_leverage_pct: float = Field(default=0.0, ge=0, le=60)
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=60)

    # Monte Carlo uncertainty (all optional): a low/high SLOC interval the propagation
    # samples (the dominant nonlinear driver), a low/high band on the proposed AI
    # reduction %, and a fallback coefficient-of-variation when no range is given.
    sloc_range: Range3 | None = None
    reduction_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def resolve_sloc(inputs: DevCOCOMOInputs) -> float:
    if inputs.sloc_estimate is not None and inputs.sloc_estimate > 0:
        return inputs.sloc_estimate
    if inputs.function_points and inputs.function_points > 0:
        ratio = LANGUAGE_SLOC_PER_FP.get(inputs.primary_language.lower(), 47)
        return inputs.function_points * ratio
    # Last-resort default; flagged in notes downstream.
    return 5000.0


def compute_cocomo_hours(inputs: DevCOCOMOInputs) -> tuple[float, dict]:
    sloc = resolve_sloc(inputs)
    ksloc = sloc / 1000.0
    e = 0.91 + 0.01 * inputs.scale_factor_sum
    pm = 2.94 * (ksloc**e) * inputs.eaf_composite
    base_hours = pm * HOURS_PER_PM
    stack_mul = STACK_MULTIPLIER.get(inputs.stack_category.value, 1.0)
    after_stack = base_hours * stack_mul
    after_leverage = after_stack * (1 - inputs.infrastructure_leverage_pct / 100.0)
    return after_leverage, {
        "ksloc": round(ksloc, 2),
        "scale_exponent_E": round(e, 3),
        "person_months": round(pm, 2),
        "base_hours": round(base_hours, 1),
        "stack_multiplier": stack_mul,
        "after_stack_hours": round(after_stack, 1),
        "leverage_pct": inputs.infrastructure_leverage_pct,
    }


def _uncertain_fields_dev(inputs: DevCOCOMOInputs) -> dict[str, tuple[float, float, float]]:
    """Resolve the SLOC band onto whichever input `resolve_sloc` actually reads
    (R7): `sloc_estimate` if present, else `function_points` (converting the
    SLOC-expressed `sloc_range` into FP units via the language ratio), else nothing."""
    ratio = LANGUAGE_SLOC_PER_FP.get(inputs.primary_language.lower(), 47)
    if inputs.sloc_estimate is not None and inputs.sloc_estimate > 0:
        field, point, explicit = "sloc_estimate", inputs.sloc_estimate, inputs.sloc_range
    elif inputs.function_points and inputs.function_points > 0:
        field, point = "function_points", inputs.function_points
        explicit = (
            Range3(low=inputs.sloc_range.low / ratio, high=inputs.sloc_range.high / ratio)
            if inputs.sloc_range is not None
            else None
        )
    else:
        return {}
    band = resolve_size_band(
        point_value=point, explicit=explicit, estimate_cov=inputs.estimate_cov, confidence=inputs.confidence
    )
    return {field: band} if band else {}


def build_phase_estimate(
    inputs: DevCOCOMOInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
) -> PhaseEstimate:
    point_mid, breakdown = compute_cocomo_hours(inputs)
    manual_mc, ai_mc = propagate_phase(
        inputs,
        compute_cocomo_hours,
        size_fields=_uncertain_fields_dev(inputs),
        reduction_sampler=reduction_sampler,
        risk_specs=risk_specs_from(inputs.risks),
        eff_point=effective_reduction,
        n_draws=DEFAULT_DRAWS,
        rng=rng,
    )
    ai_mid = point_mid * (1 - effective_reduction)

    return assemble_phase_estimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development_architect",
        algorithm="COCOMO_II",
        point_mid=point_mid,
        ai_mid=ai_mid,
        manual_mc=manual_mc,
        ai_mc=ai_mc,
        roster=roster,
        inputs=inputs,
        breakdown=breakdown,
        effective_reduction=effective_reduction,
        assumption_impact_factor=0.05,
        notes=inputs.notes.strip(),
    )


def _proposed_reduction(inputs: DevCOCOMOInputs) -> float:
    return inputs.ai_reduction_pct / 100


# Pass-2 self-consistency. COCOMO's most-likely is a PRODUCT of several independently LLM-sampled
# drivers (SLOC^E · EAF · stack · leverage), so its run-to-run noise compounds — development is the
# one high-variance twin. Folding K samples by the MEDIAN of each numeric driver shrinks that noise
# ~1/sqrt(K); the qualitative fields ride on the MEDOID (the sample whose point hours is the median)
# so assumptions/risks/ranges stay coherent with the chosen sizing.
_ENSEMBLE_K = 5


def _aggregate_cocomo(samples: list[DevCOCOMOInputs]) -> DevCOCOMOInputs:
    by_hours = sorted(samples, key=lambda s: compute_cocomo_hours(s)[0])
    medoid = by_hours[len(by_hours) // 2]
    return medoid.model_copy(
        update={
            "sloc_estimate": statistics.median(resolve_sloc(s) for s in samples),
            "function_points": None,
            "scale_factor_sum": round(statistics.median(s.scale_factor_sum for s in samples)),
            "eaf_composite": statistics.median(s.eaf_composite for s in samples),
            "infrastructure_leverage_pct": statistics.median(
                s.infrastructure_leverage_pct for s in samples
            ),
        }
    )


development_pass1, development_pass2 = make_twin_nodes(
    phase=Phase.DEVELOPMENT,
    prompt_name="development_architect",
    tool_name="submit_cocomo_assessment",
    response_model=DevCOCOMOInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="COCOMO_II",
    stub_ai_mid=3400,
    stub_manual_mid=4000,
    proposed_reduction_fn=_proposed_reduction,
    ensemble_k=_ENSEMBLE_K,
    ensemble_aggregate_fn=_aggregate_cocomo,
    trace_name="twin.development",
)
