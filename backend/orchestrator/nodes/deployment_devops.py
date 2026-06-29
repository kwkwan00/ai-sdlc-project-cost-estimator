"""Deployment & DevOps Engineer — CMP + WBS per planning outline §3.5."""

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

# Bounds the CMP-score driver (matches the field's ge/le).
_CMP_LO, _CMP_HI = 1.0, 3.0


class CMPInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmp_score: float = Field(ge=1.0, le=3.0)
    cicd_components: int = Field(default=0, ge=0, le=15)
    monitoring_components: int = Field(default=0, ge=0, le=10)
    handoff_hours: float = Field(default=40.0, ge=0, le=300)

    regulatory_multiplier: float = Field(default=1.0, ge=1.0, le=1.5)
    conservative_bias_pct: float = Field(default=6.0, ge=0, le=25)
    ai_reduction_pct: float = Field(default=0.0, ge=0, le=30)

    # Monte Carlo uncertainty (optional). The dominant size driver is the CMP score;
    # the LLM may give an ~80% band for it, a fallback CoV, and/or a low/high band on
    # the AI reduction it proposes.
    cmp_score_range: Range3 | None = None
    reduction_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_cmp_hours(inputs: CMPInputs) -> tuple[float, dict]:
    infra = inputs.cmp_score * 80.0
    cicd = inputs.cicd_components * 12.0
    monitoring = inputs.monitoring_components * 12.0
    # Regulatory overhead scopes to the compliance-bearing CI/CD + monitoring work (audit gates,
    # security scans, compliance dashboards), NOT base infra provisioning or operational handoff.
    after_reg = infra + (cicd + monitoring) * inputs.regulatory_multiplier + inputs.handoff_hours
    manual_mid = after_reg * (1 + inputs.conservative_bias_pct / 100)
    return manual_mid, {
        "infra_hours": round(infra, 1),
        "cicd_hours": round(cicd, 1),
        "monitoring_hours": round(monitoring, 1),
        "handoff_hours": inputs.handoff_hours,
        "regulatory_multiplier": inputs.regulatory_multiplier,
        "conservative_bias_pct": inputs.conservative_bias_pct,
    }


def _uncertain_fields_dep(inputs: CMPInputs) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the CMP score, clamped to its [1.0, 3.0] bounds.
    Mirrors ``_uncertain_fields_dev``."""
    band = resolve_size_band(
        point_value=inputs.cmp_score,
        explicit=inputs.cmp_score_range,
        estimate_cov=inputs.estimate_cov,
        confidence=inputs.confidence,
        lo_bound=_CMP_LO,
        hi_bound=_CMP_HI,
    )
    return {"cmp_score": band} if band else {}


def build_phase_estimate(
    inputs: CMPInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
) -> PhaseEstimate:
    return build_phase_from_compute(
        inputs,
        phase=Phase.DEPLOYMENT,
        twin_name="deployment_devops",
        algorithm="CMP",
        compute_fn=compute_cmp_hours,
        size_fields=_uncertain_fields_dep(inputs),
        effective_reduction=effective_reduction,
        roster=roster,
        rng=rng,
        reduction_sampler=reduction_sampler,
        assumption_impact_factor=0.1,
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
)
