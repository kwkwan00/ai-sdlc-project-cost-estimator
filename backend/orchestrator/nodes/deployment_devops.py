"""Deployment & DevOps Engineer — CMP + WBS per planning outline §3.5."""

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

# DevOps is least AI-mature; reduction caps are smaller.
_MATURITY_CAP_DEVOPS = {1: 0.0, 2: 0.05, 3: 0.10, 4: 0.15, 5: 0.25}


class CMPInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmp_score: float = Field(ge=1.0, le=3.0)
    cicd_components: int = Field(default=0, ge=0, le=15)
    monitoring_components: int = Field(default=0, ge=0, le=10)
    handoff_hours: float = Field(default=40.0, ge=0, le=300)

    regulatory_multiplier: float = Field(default=1.0, ge=1.0, le=1.5)
    conservative_bias_pct: float = Field(default=12.0, ge=0, le=25)
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=30)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_cmp_hours(inputs: CMPInputs) -> tuple[float, dict]:
    infra = inputs.cmp_score * 80.0
    cicd = inputs.cicd_components * 12.0
    monitoring = inputs.monitoring_components * 12.0
    subtotal = infra + cicd + monitoring + inputs.handoff_hours
    after_reg = subtotal * inputs.regulatory_multiplier
    manual_mid = after_reg * (1 + inputs.conservative_bias_pct / 100)
    return manual_mid, {
        "infra_hours": round(infra, 1),
        "cicd_hours": round(cicd, 1),
        "monitoring_hours": round(monitoring, 1),
        "handoff_hours": inputs.handoff_hours,
        "regulatory_multiplier": inputs.regulatory_multiplier,
        "conservative_bias_pct": inputs.conservative_bias_pct,
    }


def ai_reduction_for_maturity(level: int) -> float:
    return _MATURITY_CAP_DEVOPS.get(level, 0.0)


def build_phase_estimate(
    inputs: CMPInputs, *, maturity_level: int, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_cmp_hours(inputs)
    cap = ai_reduction_for_maturity(maturity_level)
    effective = min(inputs.ai_reduction_pct / 100.0, cap)
    ai_mid = manual_mid * (1 - effective)

    return PhaseEstimate(
        phase=Phase.DEPLOYMENT,
        twin_name="deployment_devops",
        algorithm="CMP",
        ai_assisted_hours=pert_range(ai_mid),
        manual_only_hours=pert_range(manual_mid),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.DEPLOYMENT),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.DEPLOYMENT),
        assumptions=[Assumption(text=a, impact_hours=manual_mid * 0.1) for a in inputs.assumptions],
        risks=[
            Risk(description=r, likelihood=0.4, impact_hours_low=manual_mid * 0.1, impact_hours_high=manual_mid * 0.3)
            for r in inputs.risks
        ],
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        notes=(
            f"CMP breakdown: {breakdown}. AI cap L{maturity_level} = {int(cap*100)}%. {inputs.notes}"
        ).strip(),
    )


async def _run_deploy(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    maturity = stage3.deployment_maturity
    try:
        inputs = await call_structured(
            system=load_prompt("deployment_devops"),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value="deployment"),
            response_model=CMPInputs,
            tool_name="submit_cmp_assessment",
        )
        return build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deployment twin failed (%s); returning stub", exc)
        return stub_phase_estimate(
            Phase.DEPLOYMENT, "deployment_devops", "CMP", 340, 390, roster
        )


@traced(name="twin.deployment.p1")
async def deployment_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_deploy(state, pass_num=1)]}


@traced(name="twin.deployment.p2")
async def deployment_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_deploy(state, pass_num=2)]}
