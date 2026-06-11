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

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster, Stage3Maturity
from models.twin_outputs import (
    Assumption,
    Gap,
    HourRange,
    Phase,
    PhaseEstimate,
    Risk,
)
from observability.langfuse_wrapper import traced
from orchestrator.llm import call_structured
from orchestrator.role_attribution import attribute_roles

from ._twin_base import build_twin_user_prompt, load_prompt, stub_phase_estimate

logger = logging.getLogger(__name__)


# Maturity-level effective AI reduction caps (Discovery phase).
# Drawn from planning outline §3.1.3 (Level 3 example showed ~30% cap).
_MATURITY_CAP = {1: 0.0, 2: 0.15, 3: 0.30, 4: 0.50, 5: 0.65}


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

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def _stakeholder_multiplier(inputs: DiscoveryUCPInputs) -> float:
    group_mult = 1.0
    if 3 <= inputs.stakeholder_group_count <= 5:
        group_mult = 1.15
    elif inputs.stakeholder_group_count >= 6:
        group_mult = 1.35

    access_mult = {
        DecisionMakerAccessibility.READILY_AVAILABLE: 1.0,
        DecisionMakerAccessibility.GATEKEEPER: 1.2,
        DecisionMakerAccessibility.EXECUTIVE_ONLY_OR_MULTI_TZ: 1.4,
    }[inputs.decision_maker_accessibility]

    align_mult = {
        AlignmentDifficulty.PRE_ALIGNED: 1.0,
        AlignmentDifficulty.COMPETING_PRIORITIES: 1.25,
    }[inputs.alignment_difficulty]

    return group_mult * access_mult * align_mult


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


def ai_reduction_for_maturity(maturity_level: int) -> float:
    """Return the effective AI reduction factor (0..1) for Discovery at this maturity."""
    return _MATURITY_CAP.get(maturity_level, 0.0)


def build_phase_estimate(
    inputs: DiscoveryUCPInputs,
    *,
    maturity_level: int,
    roster: RoleRoster,
) -> PhaseEstimate:
    manual_mid, breakdown = compute_ucp_hours(inputs)
    manual_range = pert_range(manual_mid)

    ai_reduction = ai_reduction_for_maturity(maturity_level)
    ai_mid = manual_mid * (1 - ai_reduction)
    ai_range = pert_range(ai_mid)

    notes = (
        f"UCP breakdown: {breakdown}. Maturity L{maturity_level} → "
        f"{int(ai_reduction * 100)}% AI reduction. {inputs.notes}".strip()
    )

    return PhaseEstimate(
        phase=Phase.DISCOVERY,
        twin_name="discovery_analyst",
        algorithm="UCP",
        ai_assisted_hours=ai_range,
        manual_only_hours=manual_range,
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.DISCOVERY),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.DISCOVERY),
        assumptions=[Assumption(text=a, impact_hours=manual_mid * 0.1) for a in inputs.assumptions],
        risks=[
            Risk(
                description=r,
                likelihood=0.4,
                impact_hours_low=manual_mid * 0.1,
                impact_hours_high=manual_mid * 0.3,
            )
            for r in inputs.risks
        ],
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        notes=notes,
    )


def _roster_for(state: EstimationState) -> RoleRoster:
    """Pull the roster from Stage 2; fall back to the default if absent."""
    stage2 = state.get("stage2")
    return stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()


async def _run_discovery(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    roster = _roster_for(state)
    maturity = stage3.discovery_maturity

    try:
        system = load_prompt("discovery_analyst")
        user_prompt = build_twin_user_prompt(state, pass_num=pass_num, phase_value="discovery")
        inputs = await call_structured(
            system=system,
            user=user_prompt,
            response_model=DiscoveryUCPInputs,
            tool_name="submit_ucp_assessment",
        )
        est = build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
        logger.info(
            "discovery twin done: pass=%s ai_ml=%.0fh manual_ml=%.0fh",
            pass_num,
            est.ai_assisted_hours.most_likely,
            est.manual_only_hours.most_likely,
        )
        return est
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discovery twin failed (%s); returning stub estimate", exc)
        return stub_phase_estimate(
            Phase.DISCOVERY, "discovery_analyst", "UCP", 200, 240, roster
        )


@traced(name="twin.discovery_analyst.p1")
async def discovery_analyst_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_discovery(state, pass_num=1)]}


@traced(name="twin.discovery_analyst.p2")
async def discovery_analyst_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_discovery(state, pass_num=2)]}
