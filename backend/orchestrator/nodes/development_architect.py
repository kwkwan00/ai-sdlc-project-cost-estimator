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
from orchestrator.montecarlo import Range3, ReductionSampler, resolve_size_band

from ._twin_base import build_phase_from_compute, make_twin_nodes

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

# Function-Points alternative: hours per IFPUG function point (ISBSG-ish productivity for new
# development). Admin-tunable behavior lives in the band/coeff stacks; this is the code default.
HOURS_PER_FP = 13.0

# COSMIC Function Points (ISO 19761) alternative: functional size measured in data movements
# (Entry/Exit/Read/Write) rather than IFPUG transactions/data — better for real-time, embedded,
# and service-oriented systems. Linear in size like IFPUG FP. `HOURS_PER_CFP` and the FP→CFP
# fallback ratio are tuned so a nominal project lands near the FP/COCOMO baseline (CFP ≈ 1.2·FP
# for typical business apps, so 1.2 × 11 ≈ 13.2 ≈ HOURS_PER_FP), diverging as the explicit CFP
# count departs from the FP-implied norm. Calibrate against real actuals if available.
HOURS_PER_CFP = 11.0
CFP_PER_FP = 1.2  # rough COSMIC-CFP per IFPUG-FP ratio for the FP/SLOC fallback

# Selectable development sizing algorithms (the Settings screen switches between them; the dev
# twin reads the choice off EstimationState, defaulting to COCOMO).
DEFAULT_DEV_SIZING_METHOD = "cocomo"
DEV_SIZING_METHODS: tuple[str, ...] = ("cocomo", "function_points", "cosmic_function_points")


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
    # COSMIC functional size (data movements) — used only when the COSMIC sizing method is active;
    # the other methods ignore it. Falls back to the IFPUG FP count × CFP_PER_FP when absent.
    cosmic_cfp: float | None = Field(default=None, ge=0)
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
    cfp_range: Range3 | None = None  # MC band on cosmic_cfp under the COSMIC method
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


def resolve_fp(inputs: DevCOCOMOInputs) -> float:
    """Function-point size for the FP method: the LLM's `function_points` if given, else the
    `sloc_estimate` converted via the per-language SLOC/FP ratio, else a small default (flagged
    downstream). The mirror of `resolve_sloc`, with FP taking priority."""
    if inputs.function_points is not None and inputs.function_points > 0:
        return inputs.function_points
    if inputs.sloc_estimate is not None and inputs.sloc_estimate > 0:
        ratio = LANGUAGE_SLOC_PER_FP.get(inputs.primary_language.lower(), 47)
        return inputs.sloc_estimate / ratio
    return 100.0


def compute_fp_hours(inputs: DevCOCOMOInputs) -> tuple[float, dict]:
    """IFPUG Function-Point effort, the COCOMO alternative: hours = FP × HOURS_PER_FP, **linear**
    in size (no KSLOC^E scale diseconomy — the defining difference from COCOMO II), then moderated
    by the same EAF / stack / infrastructure-leverage drivers so the two methods stay comparable."""
    fp = resolve_fp(inputs)
    base_hours = fp * HOURS_PER_FP * inputs.eaf_composite
    stack_mul = STACK_MULTIPLIER.get(inputs.stack_category.value, 1.0)
    after_stack = base_hours * stack_mul
    after_leverage = after_stack * (1 - inputs.infrastructure_leverage_pct / 100.0)
    return after_leverage, {
        "function_points": round(fp, 1),
        "hours_per_fp": HOURS_PER_FP,
        "eaf_composite": inputs.eaf_composite,
        "base_hours": round(base_hours, 1),
        "stack_multiplier": stack_mul,
        "after_stack_hours": round(after_stack, 1),
        "leverage_pct": inputs.infrastructure_leverage_pct,
    }


def resolve_cfp(inputs: DevCOCOMOInputs) -> float:
    """COSMIC functional size (CFP) for the COSMIC method: the LLM's `cosmic_cfp` if given, else the
    IFPUG FP count (explicit or SLOC-derived via `resolve_fp`) scaled by `CFP_PER_FP`. Mirrors
    `resolve_fp`/`resolve_sloc`, with the explicit CFP count taking priority, so the methods stay
    comparable on a nominal project and diverge only when a real CFP count is supplied."""
    if inputs.cosmic_cfp is not None and inputs.cosmic_cfp > 0:
        return inputs.cosmic_cfp
    return resolve_fp(inputs) * CFP_PER_FP


def compute_cosmic_hours(inputs: DevCOCOMOInputs) -> tuple[float, dict]:
    """COSMIC Function Point effort (ISO 19761), the second linear alternative to COCOMO: hours =
    CFP × HOURS_PER_CFP, **linear** in functional size (no KSLOC^E scale diseconomy), then moderated
    by the same EAF / stack / infrastructure-leverage drivers as the FP method so the three methods
    stay comparable."""
    cfp = resolve_cfp(inputs)
    base_hours = cfp * HOURS_PER_CFP * inputs.eaf_composite
    stack_mul = STACK_MULTIPLIER.get(inputs.stack_category.value, 1.0)
    after_stack = base_hours * stack_mul
    after_leverage = after_stack * (1 - inputs.infrastructure_leverage_pct / 100.0)
    return after_leverage, {
        "cosmic_cfp": round(cfp, 1),
        "hours_per_cfp": HOURS_PER_CFP,
        "eaf_composite": inputs.eaf_composite,
        "base_hours": round(base_hours, 1),
        "stack_multiplier": stack_mul,
        "after_stack_hours": round(after_stack, 1),
        "leverage_pct": inputs.infrastructure_leverage_pct,
    }


# Maps the selected sizing method → (deterministic compute fn, algorithm label on the estimate).
_COMPUTE_BY_METHOD: dict[str, tuple] = {
    "cocomo": (compute_cocomo_hours, "COCOMO_II"),
    "function_points": (compute_fp_hours, "FUNCTION_POINTS"),
    "cosmic_function_points": (compute_cosmic_hours, "COSMIC_FFP"),
}


def _uncertain_fields_dev(
    inputs: DevCOCOMOInputs, sizing_method: str = DEFAULT_DEV_SIZING_METHOD
) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the input the active compute fn actually reads, so the MC
    re-runs that fn over the perturbed driver: `function_points` under the FP method, else
    whichever of `sloc_estimate` / `function_points` `resolve_sloc` reads under COCOMO. A
    SLOC-expressed `sloc_range` is converted into FP units via the language ratio when the band
    lands on `function_points`."""
    ratio = LANGUAGE_SLOC_PER_FP.get(inputs.primary_language.lower(), 47)
    sloc_to_fp = lambda r: Range3(low=r.low / ratio, high=r.high / ratio)  # noqa: E731
    if sizing_method == "cosmic_function_points":
        cfp = resolve_cfp(inputs)
        if cfp <= 0:
            return {}
        explicit = inputs.cfp_range
        if explicit is None and inputs.sloc_range is not None:
            fp_band = sloc_to_fp(inputs.sloc_range)  # SLOC → FP → CFP
            explicit = Range3(low=fp_band.low * CFP_PER_FP, high=fp_band.high * CFP_PER_FP)
        field, point = "cosmic_cfp", cfp
    elif sizing_method == "function_points":
        fp = resolve_fp(inputs)
        if fp <= 0:
            return {}
        explicit = sloc_to_fp(inputs.sloc_range) if inputs.sloc_range is not None else None
        field, point = "function_points", fp
    elif inputs.sloc_estimate is not None and inputs.sloc_estimate > 0:
        field, point, explicit = "sloc_estimate", inputs.sloc_estimate, inputs.sloc_range
    elif inputs.function_points and inputs.function_points > 0:
        field, point = "function_points", inputs.function_points
        explicit = sloc_to_fp(inputs.sloc_range) if inputs.sloc_range is not None else None
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
    sizing_method: str = DEFAULT_DEV_SIZING_METHOD,
) -> PhaseEstimate:
    compute_fn, algorithm = _COMPUTE_BY_METHOD.get(
        sizing_method, _COMPUTE_BY_METHOD[DEFAULT_DEV_SIZING_METHOD]
    )
    return build_phase_from_compute(
        inputs,
        phase=Phase.DEVELOPMENT,
        twin_name="development_architect",
        algorithm=algorithm,
        compute_fn=compute_fn,
        size_fields=_uncertain_fields_dev(inputs, sizing_method),
        effective_reduction=effective_reduction,
        roster=roster,
        rng=rng,
        reduction_sampler=reduction_sampler,
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


def _aggregate_dev(samples: list[DevCOCOMOInputs]) -> DevCOCOMOInputs:
    """Method-agnostic ensemble fold. Sorts by the SLOC size proxy (`resolve_sloc`, defined for
    every method) to pick the medoid for the qualitative fields, then medians **all three** size
    drivers — `sloc_estimate` (via `resolve_sloc`), `function_points` (via `resolve_fp`), and
    `cosmic_cfp` (via `resolve_cfp`) — to consistent consensus values. Whichever driver the active
    method reads gets a properly-medianed size; keeping all three (rather than nulling the unused
    ones) makes the fold work for COCOMO, FP, and COSMIC alike."""
    by_size = sorted(samples, key=resolve_sloc)
    medoid = by_size[len(by_size) // 2]
    return medoid.model_copy(
        update={
            "sloc_estimate": statistics.median(resolve_sloc(s) for s in samples),
            "function_points": statistics.median(resolve_fp(s) for s in samples),
            "cosmic_cfp": statistics.median(resolve_cfp(s) for s in samples),
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
    ensemble_aggregate_fn=_aggregate_dev,
    sizing_method_key="development_sizing_method",
    sizing_method_default=DEFAULT_DEV_SIZING_METHOD,
)
