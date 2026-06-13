"""Deployment & DevOps Engineer — CMP + WBS per planning outline §3.5."""

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


def build_phase_estimate(
    inputs: CMPInputs, *, effective_reduction: float, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_cmp_hours(inputs)
    ai_mid = manual_mid * (1 - effective_reduction)

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
        breakdown=breakdown,
        effective_ai_reduction_pct=round(effective_reduction * 100, 1),
        notes=inputs.notes.strip(),
    )


def _proposed_reduction(inputs: CMPInputs) -> float:
    return inputs.ai_reduction_pct / 100


deployment_pass1, deployment_pass2 = make_twin_nodes(
    phase=Phase.DEPLOYMENT,
    prompt_name="deployment_devops",
    tool_name="submit_cmp_assessment",
    response_model=CMPInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="CMP",
    stub_ai_mid=340,
    stub_manual_mid=390,
    proposed_reduction_fn=_proposed_reduction,
    trace_name="twin.deployment",
)
