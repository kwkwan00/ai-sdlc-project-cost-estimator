"""Code Review Sentinel — Fagan inspection model per planning outline §3.4."""

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
from orchestrator.montecarlo import (
    DEFAULT_DRAWS,
    Range3,
    ReductionSampler,
    propagate_phase,
    resolve_size_band,
)

from ._twin_base import assemble_phase_estimate, make_twin_nodes, risk_specs_from

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

    # Monte Carlo uncertainty (optional). The dominant size driver is the reviewed
    # volume (KSLOC); the LLM may give an ~80% band for it, a fallback CoV, and/or a
    # low/high band on the AI reduction it proposes.
    ksloc_range: Range3 | None = None
    reduction_range: Range3 | None = None
    estimate_cov: float | None = Field(default=None, ge=0, le=0.6)

    assumptions: list[str] = Field(default_factory=list, max_length=6)
    risks: RiskInputList = Field(default_factory=list, max_length=5)
    gaps: list[Gap] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


def compute_review_hours(inputs: CodeReviewInputs) -> tuple[float, dict]:
    rate = INSPECTION_RATE.get(inputs.primary_language.lower(), 200)
    base = (inputs.total_ksloc * 1000) / rate
    prep = base * 0.3
    rework_mul = 1 + (inputs.kickback_rate_pct / 100) * 0.5
    review_hours = (base + prep) * inputs.pr_complexity_factor * rework_mul
    manual_mid = review_hours + inputs.tooling_setup_hours
    return manual_mid, {
        "inspection_rate_loc_per_hr": rate,
        "review_hours_pre_tooling": round(review_hours, 1),
        "rework_multiplier": round(rework_mul, 3),
        "tooling_setup_hours": inputs.tooling_setup_hours,
    }


def _uncertain_fields_cr(inputs: CodeReviewInputs) -> dict[str, tuple[float, float, float]]:
    """Resolve the size band onto the reviewed volume (KSLOC). No bounds beyond the
    field's ``ge=0``. Mirrors ``_uncertain_fields_dev``."""
    band = resolve_size_band(
        point_value=inputs.total_ksloc,
        explicit=inputs.ksloc_range,
        estimate_cov=inputs.estimate_cov,
        confidence=inputs.confidence,
    )
    return {"total_ksloc": band} if band else {}


def build_phase_estimate(
    inputs: CodeReviewInputs,
    *,
    effective_reduction: float,
    roster: RoleRoster,
    rng,
    reduction_sampler: ReductionSampler,
) -> PhaseEstimate:
    point_mid, breakdown = compute_review_hours(inputs)
    manual_mc, ai_mc = propagate_phase(
        inputs,
        compute_review_hours,
        size_fields=_uncertain_fields_cr(inputs),
        reduction_sampler=reduction_sampler,
        risk_specs=risk_specs_from(inputs.risks),
        eff_point=effective_reduction,
        n_draws=DEFAULT_DRAWS,
        rng=rng,
    )
    ai_mid = point_mid * (1 - effective_reduction)

    return assemble_phase_estimate(
        phase=Phase.CODE_REVIEW,
        twin_name="code_review_sentinel",
        algorithm="Fagan",
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
