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

# Effective AI reduction caps by maturity (Development phase).
_MATURITY_CAP_DEV = {1: 0.0, 2: 0.10, 3: 0.25, 4: 0.40, 5: 0.55}

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

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
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


def ai_reduction_for_maturity(level: int) -> float:
    return _MATURITY_CAP_DEV.get(level, 0.0)


def build_phase_estimate(
    inputs: DevCOCOMOInputs, *, maturity_level: int, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_cocomo_hours(inputs)
    cap = ai_reduction_for_maturity(maturity_level)
    effective_ai_reduction = min(inputs.ai_reduction_pct / 100.0, cap)
    ai_mid = manual_mid * (1 - effective_ai_reduction)

    return PhaseEstimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development_architect",
        algorithm="COCOMO_II",
        ai_assisted_hours=pert_range(ai_mid),
        manual_only_hours=pert_range(manual_mid),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.DEVELOPMENT),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.DEVELOPMENT),
        assumptions=[Assumption(text=a, impact_hours=manual_mid * 0.05) for a in inputs.assumptions],
        risks=[
            Risk(description=r, likelihood=0.4, impact_hours_low=manual_mid * 0.05, impact_hours_high=manual_mid * 0.2)
            for r in inputs.risks
        ],
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        notes=(
            f"COCOMO breakdown: {breakdown}. AI cap L{maturity_level} = {int(cap*100)}%, "
            f"effective reduction = {int(effective_ai_reduction*100)}%. {inputs.notes}"
        ).strip(),
    )


async def _run_dev(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    maturity = stage3.development_maturity
    try:
        inputs = await call_structured(
            system=load_prompt("development_architect"),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value="development"),
            response_model=DevCOCOMOInputs,
            tool_name="submit_cocomo_assessment",
        )
        return build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Development twin failed (%s); returning stub", exc)
        return stub_phase_estimate(
            Phase.DEVELOPMENT, "development_architect", "COCOMO_II", 3400, 4000, roster
        )


@traced(name="twin.development.p1")
async def development_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_dev(state, pass_num=1)]}


@traced(name="twin.development.p2")
async def development_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_dev(state, pass_num=2)]}
