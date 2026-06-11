"""Code Review Sentinel — Fagan inspection model per planning outline §3.4."""

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

_MATURITY_CAP_REVIEW = {1: 0.0, 2: 0.10, 3: 0.20, 4: 0.25, 5: 0.30}


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


def ai_reduction_for_maturity(level: int) -> float:
    return _MATURITY_CAP_REVIEW.get(level, 0.0)


def build_phase_estimate(
    inputs: CodeReviewInputs, *, maturity_level: int, roster: RoleRoster
) -> PhaseEstimate:
    manual_mid, breakdown = compute_review_hours(inputs)
    cap = ai_reduction_for_maturity(maturity_level)
    effective = min(inputs.ai_quality_adjustment_pct / 100.0, cap)
    ai_mid = manual_mid * (1 - effective)

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
        notes=(
            f"Fagan breakdown: {breakdown}. AI cap L{maturity_level} = {int(cap*100)}%. {inputs.notes}"
        ).strip(),
    )


async def _run_review(state: EstimationState, pass_num: int) -> PhaseEstimate:
    stage3 = state.get("stage3") or Stage3Maturity()
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    maturity = stage3.code_review_maturity
    try:
        inputs = await call_structured(
            system=load_prompt("code_review_sentinel"),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value="code_review"),
            response_model=CodeReviewInputs,
            tool_name="submit_fagan_assessment",
        )
        est = build_phase_estimate(inputs, maturity_level=maturity, roster=roster)
        logger.info(
            "code_review twin done: pass=%s ai_ml=%.0fh manual_ml=%.0fh",
            pass_num,
            est.ai_assisted_hours.most_likely,
            est.manual_only_hours.most_likely,
        )
        return est
    except Exception as exc:  # noqa: BLE001
        logger.warning("Code review twin failed (%s); returning stub", exc)
        return stub_phase_estimate(
            Phase.CODE_REVIEW, "code_review_sentinel", "Fagan", 100, 130, roster
        )


@traced(name="twin.code_review.p1")
async def code_review_pass1(state: EstimationState) -> dict:
    return {"pass1_estimates": [await _run_review(state, pass_num=1)]}


@traced(name="twin.code_review.p2")
async def code_review_pass2(state: EstimationState) -> dict:
    return {"pass2_estimates": [await _run_review(state, pass_num=2)]}
