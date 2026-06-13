"""Code Review Sentinel — Fagan inspection model per planning outline §3.4."""

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

# Inspection rates in LOC per hour.
INSPECTION_RATE = {
    "java": 175,
    "csharp": 175,
    "go": 175,
    "typescript": 210,
    "javascript": 210,
    "python": 175,
    "ruby": 175,
    "c": 125,
    "cpp": 125,
    "hcl_yaml": 250,
    "cobol_legacy": 100,
}


class CodeReviewInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_ksloc: float = Field(ge=0)
    primary_language: str = Field(default="typescript")

    kickback_rate_pct: float = Field(default=20.0, ge=0, le=60)
    pr_complexity_factor: float = Field(default=1.0, ge=0.7, le=1.6)
    ai_quality_adjustment_pct: float = Field(default=0.0, ge=0, le=40)
    tooling_setup_hours: float = Field(default=0.0, ge=0, le=200)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_review_hours(inputs: CodeReviewInputs) -> tuple[float, dict]:
    rate = INSPECTION_RATE.get(inputs.primary_language.lower(), 200)
    base = (inputs.total_ksloc * 1000) / rate
    prep = base * 0.5
    rework_mul = 1 + (inputs.kickback_rate_pct / 100) * 0.5
    review_hours = (base + prep) * inputs.pr_complexity_factor * rework_mul
    manual_mid = review_hours + inputs.tooling_setup_hours
    return manual_mid, {
        "inspection_rate_loc_per_hr": rate,
        "review_hours_pre_tooling": round(review_hours, 1),
        "rework_multiplier": round(rework_mul, 3),
        "tooling_setup_hours": inputs.tooling_setup_hours,
    }


def build_phase_estimate(
    inputs: CodeReviewInputs, *, effective_reduction: float, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_review_hours(inputs)
    ai_mid = manual_mid * (1 - effective_reduction)

    return PhaseEstimate(
        phase=Phase.CODE_REVIEW,
        twin_name="code_review_sentinel",
        algorithm="Fagan",
        ai_assisted_hours=pert_range(ai_mid),
        manual_only_hours=pert_range(manual_mid),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, Phase.CODE_REVIEW),
        manual_only_role_hours=attribute_roles(manual_mid, roster, Phase.CODE_REVIEW),
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


def _proposed_reduction(inputs: CodeReviewInputs) -> float:
    return inputs.ai_quality_adjustment_pct / 100


code_review_pass1, code_review_pass2 = make_twin_nodes(
    phase=Phase.CODE_REVIEW,
    prompt_name="code_review_sentinel",
    tool_name="submit_fagan_assessment",
    response_model=CodeReviewInputs,
    build_fn=build_phase_estimate,
    stub_algorithm="Fagan",
    stub_ai_mid=100,
    stub_manual_mid=130,
    proposed_reduction_fn=_proposed_reduction,
    trace_name="twin.code_review",
)
