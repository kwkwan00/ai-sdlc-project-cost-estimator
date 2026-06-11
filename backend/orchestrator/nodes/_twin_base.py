"""Shared helpers for the six twin nodes.

Every twin follows the same shape:
1. Load its prompt from prompts/<twin>.md
2. Render the parsed context + (optional) Stage 2/3 inputs into the user message
3. Call Claude with a Pydantic response model that mirrors PhaseEstimate
4. Apply role attribution + return {"pass1_estimates": [PhaseEstimate(...)]} or
   {"pass2_estimates": [...]} based on the `pass` arg
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster, Stage3Maturity
from models.twin_outputs import (
    Assumption,
    HourRange,
    Phase,
    PhaseEstimate,
    Risk,
)
from orchestrator.llm import render_context_block

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a twin's system prompt from prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _calibration_for_phase(state: EstimationState, phase_value: str) -> list[dict[str, Any]]:
    """Filter the global calibration_examples down to entries for one phase."""
    rows = state.get("calibration_examples") or []
    return [r for r in rows if r.get("phase") == phase_value]


def build_twin_user_prompt(
    state: EstimationState, pass_num: int, *, phase_value: str | None = None
) -> str:
    cal_rows = len(_calibration_for_phase(state, phase_value)) if phase_value else 0
    logger.debug(
        "build_twin_user_prompt phase=%s pass=%s calibration_rows=%d",
        phase_value,
        pass_num,
        cal_rows,
    )
    parsed = state.get("parsed_context", {})
    stage2 = state.get("stage2")
    stage3 = state.get("stage3") or Stage3Maturity()

    extras: dict[str, Any] = {
        "stage2": stage2.model_dump() if stage2 else None,
        "stage3": stage3.model_dump(),
        "pass": pass_num,
    }
    if pass_num == 2:
        questions = state.get("clarifying_questions", [])
        extras["user_answers"] = [
            {"question": q.text, "answer": q.answer or q.suggested_default}
            for q in questions
        ]

    # Historical calibration for the calling phase, if known. Helps the twin
    # anchor its UCP/FP/SLOC → hours mapping against prior projects with
    # matching industry / project_type / maturity. Absent on cold start.
    if phase_value:
        cal = _calibration_for_phase(state, phase_value)
        if cal:
            extras["calibration"] = cal

    return (
        f"## Pass {pass_num}\n\n"
        f"Project description (raw):\n```\n{state.get('raw_input', '')}\n```\n\n"
        f"Structured context:\n{render_context_block(parsed, extras)}\n\n"
        f"Produce your phase estimate using the algorithm in your system prompt."
    )


def stub_phase_estimate(
    phase: Phase,
    twin_name: str,
    algorithm: str,
    ai_mid: float,
    manual_mid: float,
    roster: RoleRoster,
) -> PhaseEstimate:
    """Build a deterministic placeholder estimate.

    Used by the stub-twin path so the graph runs end-to-end without an LLM call,
    and as a fallback if a real twin's LLM call fails.
    """
    from orchestrator.role_attribution import attribute_roles

    return PhaseEstimate(
        phase=phase,
        twin_name=twin_name,
        algorithm=algorithm,
        ai_assisted_hours=HourRange(
            optimistic=ai_mid * 0.8, most_likely=ai_mid, pessimistic=ai_mid * 1.3
        ),
        manual_only_hours=HourRange(
            optimistic=manual_mid * 0.8, most_likely=manual_mid, pessimistic=manual_mid * 1.3
        ),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, phase),
        manual_only_role_hours=attribute_roles(manual_mid, roster, phase),
        assumptions=[Assumption(text="Stub estimate — twin not yet implemented", impact_hours=0)],
        risks=[Risk(description="Placeholder", likelihood=0.0, impact_hours_low=0, impact_hours_high=0)],
        gaps=[],
        confidence=0.3,
        notes="Stub output. Replace with real twin implementation.",
    )
